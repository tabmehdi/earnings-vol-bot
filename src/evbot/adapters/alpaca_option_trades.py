from datetime import datetime
from typing import Any

from evbot.adapters.alpaca_market_data import occ_symbol
from evbot.adapters.http_client import HttpRequestError, JsonGetter
from evbot.domain.options import OptionContract, OptionTrade
from evbot.ports.errors import MarketDataError

PAGE_LIMIT = 1000  # Alpaca's maximum per page


class AlpacaOptionTradeHistory:
    """Alpaca's historical option trades. Reads market data only; no feed parameter, which
    this endpoint does not take (data older than 15 minutes is open to the free plan)."""

    def __init__(self, http: JsonGetter, data_url: str, key_id: str, secret_key: str) -> None:
        self._http = http
        self._url = f"{data_url.rstrip('/')}/v1beta1/options/trades"
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}

    def trades(
        self, contract: OptionContract, start: datetime, end: datetime
    ) -> tuple[OptionTrade, ...]:
        symbol = occ_symbol(contract)
        found: list[OptionTrade] = []
        token: str | None = None
        while True:
            params: dict[str, str | int | float] = {
                "symbols": symbol,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": PAGE_LIMIT,
                "sort": "asc",
            }
            if token:
                params["page_token"] = token
            try:
                page = self._http.get(self._url, params, self._headers)
                found += [_trade(contract, row) for row in (page["trades"] or {}).get(symbol, [])]
            except HttpRequestError as error:
                raise MarketDataError(f"{symbol}: {error} {error.body}".strip()) from error
            except (KeyError, TypeError, ValueError) as error:
                raise MarketDataError(f"{symbol}: unexpected Alpaca format: {error!r}") from error
            token = page.get("next_page_token")
            if not token:
                return tuple(found)


def _trade(contract: OptionContract, row: Any) -> OptionTrade:
    return OptionTrade(contract, datetime.fromisoformat(row["t"]), float(row["p"]), int(row["s"]))
