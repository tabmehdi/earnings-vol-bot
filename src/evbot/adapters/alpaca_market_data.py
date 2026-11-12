from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
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
SCREEN_BATCH_SIZE = 100  # underlyings per contracts request, to keep URLs short
NONE_NEAR_SPOT = "no contracts within strike range"
NONE_LISTED = "no option contracts available"


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
        # Contracts in the expiry window per screened ticker, any strike, for get_chain.
        self._listed: dict[str, list[Any]] = {}
        self._listed_day: date | None = None

    def screen(self, symbols: Sequence[str]) -> Mapping[str, str]:
        try:
            return self._screen(symbols)
        except HttpRequestError as error:
            raise MarketDataError(f"contract screening: {error}") from error
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise MarketDataError(f"contract screening: unexpected format: {error!r}") from error

    def get_chain(self, symbol: str) -> ChainSnapshot:
        try:
            return self._fetch_chain(symbol)
        except HttpRequestError as error:
            raise MarketDataError(f"{symbol}: {error}") from error
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise MarketDataError(f"{symbol}: unexpected Alpaca format: {error!r}") from error

    def get_leg_quotes(
        self, contracts: Sequence[OptionContract]
    ) -> Mapping[OptionContract, OptionQuote]:
        by_symbol = {occ_symbol(contract): contract for contract in contracts}
        params = {"symbols": ",".join(by_symbol), "feed": self._feeds.options}
        url = f"{self._data_url}/v1beta1/options/quotes/latest"
        try:
            found = self._http.get(url, params, self._headers)["quotes"] or {}
            quotes = {}
            for symbol, contract in by_symbol.items():
                if symbol not in found:
                    raise MarketDataError(f"no quote for {symbol}")
                market = found[symbol]
                quotes[contract] = OptionQuote(
                    contract,
                    float(market["bp"]),
                    float(market["ap"]),
                    None,
                    open_interest=0,  # not in this endpoint; it is on the recorded chain
                    volume=0,
                    quoted_at=_timestamp(market.get("t")),
                )
            return quotes
        except HttpRequestError as error:
            raise MarketDataError(f"leg quotes: {error}") from error
        except (KeyError, TypeError, ValueError) as error:
            raise MarketDataError(f"leg quotes: unusable quote: {error!r}") from error

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
        spot, spot_at = self._spot(symbol)
        scan_day = scan_time.astimezone(NEW_YORK).date()
        low = round(spot * (1 - self._strike_range_pct), 2)
        high = round(spot * (1 + self._strike_range_pct), 2)
        filters: dict[str, str | int | float] = {
            "strike_price_gte": low,
            "strike_price_lte": high,
            "expiration_date_gte": scan_day.isoformat(),
            "expiration_date_lte": (scan_day + timedelta(days=self._chain_max_days)).isoformat(),
        }
        screened = self._listed.get(symbol) if self._listed_day == scan_day else None
        if screened is None:
            contracts = self._standard_contracts(symbol, filters)
            no_overlap_reason = None
        else:
            # The same filter the contracts endpoint would apply, on what screening found.
            near = [r for r in screened if low <= float(r["strike_price"]) <= high]
            if not near:
                raise MarketDataError(f"{symbol}: {NONE_NEAR_SPOT}")
            contracts = {r["symbol"]: r for r in near if _is_standard(r, symbol)}
            no_overlap_reason = NONE_NEAR_SPOT  # contracts exist, so the probe would say so
        snapshots = self._option_snapshots(symbol, filters)

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
            quotes.append(self._quote(contract_row, snapshot, bid, ask, quoted_at))
            latest = quoted_at if latest is None else max(latest, quoted_at)

        if latest is None:
            if not set(snapshots) & set(contracts):
                reason = no_overlap_reason or self._why_no_contracts(symbol, scan_day)
                raise MarketDataError(f"{symbol}: {reason}")
            raise MarketDataError(f"{symbol}: no fresh option quotes ({stale} stale dropped)")
        chain = OptionChain(symbol, spot, latest, tuple(quotes), spot_at=spot_at)
        return ChainSnapshot(chain, stale, self._feeds)

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
        return NONE_NEAR_SPOT if listed else NONE_LISTED

    def _screen(self, symbols: Sequence[str]) -> dict[str, str]:
        scan_day = self._clock.now().astimezone(NEW_YORK).date()
        in_window = self._contracts_by_underlying(symbols, scan_day, self._chain_max_days)
        missing = [s for s in symbols if not in_window.get(s)]
        later = (
            self._contracts_by_underlying(missing, scan_day, AVAILABILITY_HORIZON_DAYS)
            if missing
            else {}
        )
        self._listed = {s: in_window.get(s, []) for s in symbols}
        self._listed_day = scan_day
        return {s: NONE_NEAR_SPOT if later.get(s) else NONE_LISTED for s in missing}

    def _contracts_by_underlying(
        self, symbols: Sequence[str], scan_day: date, horizon_days: int
    ) -> dict[str, list[Any]]:
        found: defaultdict[str, list[Any]] = defaultdict(list)
        url = f"{self._trading_url}/v2/options/contracts"
        for start in range(0, len(symbols), SCREEN_BATCH_SIZE):
            params: dict[str, str | int | float] = {
                "underlying_symbols": ",".join(symbols[start : start + SCREEN_BATCH_SIZE]),
                "status": "active",
                "expiration_date_gte": scan_day.isoformat(),
                "expiration_date_lte": (scan_day + timedelta(days=horizon_days)).isoformat(),
                "limit": CONTRACT_PAGE_LIMIT,
            }
            for page in self._pages(url, params):
                for row in page["option_contracts"] or []:
                    found[row["underlying_symbol"]].append(row)
        return found

    def _spot(self, symbol: str) -> tuple[float, datetime | None]:
        params = {"feed": self._feeds.spot}
        stocks_url = f"{self._data_url}/v2/stocks/{symbol}"
        quote = self._http.get(f"{stocks_url}/quotes/latest", params, self._headers).get("quote")
        if quote:
            bid, ask = float(quote.get("bp") or 0), float(quote.get("ap") or 0)
            if 0 < bid <= ask:
                return (bid + ask) / 2, _timestamp(quote.get("t"))
        trade = self._http.get(f"{stocks_url}/trades/latest", params, self._headers).get("trade")
        if trade and float(trade.get("p") or 0) > 0:
            return float(trade["p"]), _timestamp(trade.get("t"))
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
            if _is_standard(row, symbol)
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
    def _quote(row: Any, snapshot: Any, bid: float, ask: float, quoted_at: datetime) -> OptionQuote:
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
            quoted_at=quoted_at,
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


def _timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _is_standard(row: Any, symbol: str) -> bool:
    """100-share contracts on the plain root; adjusted contracts (e.g. AAPL1) are left out."""
    return (
        row["root_symbol"] == symbol
        and float(row["multiplier"]) == STANDARD_CONTRACT_SIZE
        and float(row["size"]) == STANDARD_CONTRACT_SIZE
    )


def occ_symbol(contract: OptionContract) -> str:
    """Alpaca's option symbol: root, YYMMDD, C or P, strike in thousandths on 8 digits."""
    right = "C" if contract.right is OptionRight.CALL else "P"
    strike = round(contract.strike * 1000)
    return f"{contract.underlying}{contract.expiry:%y%m%d}{right}{strike:08d}"
