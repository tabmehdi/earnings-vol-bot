from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from evbot.adapters.http_client import HttpRequestError, JsonGetter
from evbot.domain.options import OptionChain, OptionContract, OptionQuote, OptionRight
from evbot.domain.prices import Bar, PriceHistory
from evbot.domain.snapshots import ChainSnapshot, DataFeeds
from evbot.ports.clock import Clock
from evbot.ports.errors import MarketDataError

NEW_YORK = ZoneInfo("America/New_York")
STANDARD_CONTRACT_SIZE = 100
SNAPSHOT_PAGE_LIMIT = 1_000
CONTRACT_PAGE_LIMIT = 10_000
BAR_PAGE_LIMIT = 10_000
AVAILABILITY_HORIZON_DAYS = 3 * 365


class AlpacaMarketData:
    def __init__(
        self,
        http: JsonGetter,
        clock: Clock,
        data_base_url: str,
        trading_base_url: str,
        key_id: str,
        secret_key: str,
        feeds: DataFeeds,
        chain_max_days: int,
        strike_range_pct: float,
        max_quote_age_seconds: int,
    ) -> None:
        self._http = http
        self._clock = clock
        self._data_url = data_base_url
        self._trading_url = trading_base_url
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
        self._feeds = feeds
        self._chain_max_days = chain_max_days
        self._strike_range_pct = strike_range_pct
        self._max_quote_age = timedelta(seconds=max_quote_age_seconds)

    def get_chain(self, symbol: str) -> ChainSnapshot:
        try:
            return self._fetch_chain(symbol)
        except HttpRequestError as error:
            raise MarketDataError(f"{symbol}: {error}") from error
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise MarketDataError(f"{symbol}: unexpected Alpaca format: {error!r}") from error

    def get_history(self, symbol: str, end: date, bars: int) -> PriceHistory:
        # Twice the session count plus a margin always spans enough weekends and holidays.
        start = end - timedelta(days=bars * 2 + 10)
        params: dict[str, str | int | float] = {
            "symbols": symbol,
            "timeframe": "1Day",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "adjustment": "split",
            "feed": self._feeds.history,
            "sort": "asc",
            "limit": BAR_PAGE_LIMIT,
        }
        try:
            rows = [
                row
                for page in self._pages(f"{self._data_url}/v2/stocks/bars", params)
                for row in (page["bars"] or {}).get(symbol) or []
            ]
            parsed = [self._bar(row) for row in rows]
            return PriceHistory(symbol, tuple(bar for bar in parsed if bar.day <= end)[-bars:])
        except HttpRequestError as error:
            raise MarketDataError(f"{symbol}: {error}") from error
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise MarketDataError(f"{symbol}: unexpected Alpaca format: {error!r}") from error

    def _fetch_chain(self, symbol: str) -> ChainSnapshot:
        scan_time = self._clock.now()
        spot = self._spot(symbol)
        scan_day = scan_time.astimezone(NEW_YORK).date()
        filters: dict[str, str | int | float] = {
            "strike_price_gte": round(spot * (1 - self._strike_range_pct), 2),
            "strike_price_lte": round(spot * (1 + self._strike_range_pct), 2),
            "expiration_date_gte": scan_day.isoformat(),
            "expiration_date_lte": (scan_day + timedelta(days=self._chain_max_days)).isoformat(),
        }
        snapshots = self._option_snapshots(symbol, filters)
        contracts = self._standard_contracts(symbol, filters)

        quotes: list[OptionQuote] = []
        latest: datetime | None = None
        stale = 0
        for occ_symbol, snapshot in snapshots.items():
            contract_row = contracts.get(occ_symbol)
            market = snapshot.get("latestQuote")
            if contract_row is None or not market:
                continue
            bid, ask = float(market["bp"]), float(market["ap"])
            if bid > ask or ask <= 0 or bid < 0:
                continue
            quoted_at = datetime.fromisoformat(market["t"])
            if scan_time - quoted_at > self._max_quote_age:
                stale += 1
                continue
            quotes.append(self._quote(contract_row, snapshot, bid, ask))
            latest = quoted_at if latest is None else max(latest, quoted_at)

        if latest is None:
            if not set(snapshots) & set(contracts):
                raise MarketDataError(f"{symbol}: {self._why_no_contracts(symbol, scan_day)}")
            raise MarketDataError(f"{symbol}: no fresh option quotes ({stale} stale dropped)")
        return ChainSnapshot(OptionChain(symbol, spot, latest, tuple(quotes)), stale, self._feeds)

    def _why_no_contracts(self, symbol: str, scan_day: date) -> str:
        """One extra request, only for empty chains, to tell 'none near spot' from 'none at all'."""
        params: dict[str, str | int | float] = {
            "underlying_symbols": symbol,
            "status": "active",
            "expiration_date_gte": scan_day.isoformat(),
            # The endpoint defaults this to the next weekend, which would hide later expiries.
            "expiration_date_lte": (
                scan_day + timedelta(days=AVAILABILITY_HORIZON_DAYS)
            ).isoformat(),
            "limit": 1,
        }
        url = f"{self._trading_url}/v2/options/contracts"
        listed = self._http.get(url, params, self._headers).get("option_contracts") or []
        return "no contracts within strike range" if listed else "no option contracts available"

    def _spot(self, symbol: str) -> float:
        params = {"feed": self._feeds.spot}
        stocks_url = f"{self._data_url}/v2/stocks/{symbol}"
        quote = self._http.get(f"{stocks_url}/quotes/latest", params, self._headers).get("quote")
        if quote:
            bid, ask = float(quote.get("bp") or 0), float(quote.get("ap") or 0)
            if 0 < bid <= ask:
                return (bid + ask) / 2
        trade = self._http.get(f"{stocks_url}/trades/latest", params, self._headers).get("trade")
        if trade and float(trade.get("p") or 0) > 0:
            return float(trade["p"])
        raise MarketDataError(f"{symbol}: no usable IEX quote or trade for spot")

    def _option_snapshots(
        self, symbol: str, filters: dict[str, str | int | float]
    ) -> dict[str, Any]:
        params = {**filters, "feed": self._feeds.options, "limit": SNAPSHOT_PAGE_LIMIT}
        url = f"{self._data_url}/v1beta1/options/snapshots/{symbol}"
        return {
            occ_symbol: snapshot
            for page in self._pages(url, params)
            for occ_symbol, snapshot in (page["snapshots"] or {}).items()
        }

    def _standard_contracts(
        self, symbol: str, filters: dict[str, str | int | float]
    ) -> dict[str, Any]:
        params = {
            **filters,
            "underlying_symbols": symbol,
            "status": "active",
            "limit": CONTRACT_PAGE_LIMIT,
        }
        url = f"{self._trading_url}/v2/options/contracts"
        return {
            row["symbol"]: row
            for page in self._pages(url, params)
            for row in page["option_contracts"] or []
            if row["root_symbol"] == symbol
            and float(row["multiplier"]) == STANDARD_CONTRACT_SIZE
            and float(row["size"]) == STANDARD_CONTRACT_SIZE
        }

    def _pages(self, url: str, params: dict[str, str | int | float]) -> Iterator[Any]:
        page_params = dict(params)
        while True:
            page = self._http.get(url, page_params, self._headers)
            yield page
            token = page.get("next_page_token")
            if not token:
                return
            page_params["page_token"] = token

    @staticmethod
    def _quote(row: Any, snapshot: Any, bid: float, ask: float) -> OptionQuote:
        contract = OptionContract(
            underlying=row["underlying_symbol"],
            expiry=date.fromisoformat(row["expiration_date"]),
            strike=float(row["strike_price"]),
            right=OptionRight(row["type"]),
            multiplier=int(float(row["multiplier"])),
        )
        vendor_iv = snapshot.get("impliedVolatility")
        return OptionQuote(
            contract,
            bid,
            ask,
            float(vendor_iv) if vendor_iv is not None and vendor_iv >= 0 else None,
            open_interest=int(row.get("open_interest") or 0),
            volume=0,  # option volume comes from delayed indicative trades, which are not used
        )

    @staticmethod
    def _bar(row: Any) -> Bar:
        return Bar(
            day=datetime.fromisoformat(row["t"]).astimezone(NEW_YORK).date(),
            open=float(row["o"]),
            high=float(row["h"]),
            low=float(row["l"]),
            close=float(row["c"]),
            volume=int(row["v"]),
        )
