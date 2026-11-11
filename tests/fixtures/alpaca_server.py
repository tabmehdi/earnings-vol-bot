from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from requests import Response

from evbot.adapters.alpaca_market_data import AlpacaMarketData
from evbot.domain.snapshots import DataFeeds
from tests.fakes.ports import FixedClock
from tests.fixtures.http import SeenRequest, json_client, response

SCAN_TIME = datetime(2026, 11, 19, 15, 45, tzinfo=ZoneInfo("America/New_York"))
FRONT_ISO = "2026-11-20"
BACK_ISO = "2026-12-18"
CALL = "AAPL261120C00100000"
PUT = "AAPL261120P00100000"
BACK_CALL = "AAPL261218C00100000"
FEEDS = DataFeeds(options="indicative", spot="iex", history="sip")


def seconds_before_scan(seconds: float) -> str:
    return (SCAN_TIME - timedelta(seconds=seconds)).astimezone(UTC).isoformat()


def option_snapshot(
    bid: float = 1.00, ask: float = 1.10, age_seconds: float = 10.0, iv: float | None = 0.45
) -> dict[str, Any]:
    return {
        "latestQuote": {"bp": bid, "ap": ask, "t": seconds_before_scan(age_seconds)},
        "latestTrade": {"p": 99.0, "s": 5, "t": seconds_before_scan(900)},
        "dailyBar": {"v": 12345},
        "impliedVolatility": iv,
    }


def contract_row(
    symbol: str,
    expiry: str = FRONT_ISO,
    strike: str = "100",
    right: str = "call",
    root: str = "AAPL",
    multiplier: str = "100",
    size: str = "100",
    open_interest: str | None = "750",
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "root_symbol": root,
        "underlying_symbol": "AAPL",
        "multiplier": multiplier,
        "size": size,
        "open_interest": open_interest,
        "strike_price": strike,
        "expiration_date": expiry,
        "type": right,
        "status": "active",
    }


def bar(day: str, close: float = 100.0, volume: int = 2_000_000) -> dict[str, Any]:
    # Alpaca stamps a daily bar at midnight New York, which is 05:00 UTC in winter.
    return {
        "t": f"{day}T05:00:00Z",
        "o": close,
        "h": close + 1,
        "l": close - 1,
        "c": close,
        "v": volume,
    }


@dataclass
class AlpacaServer:
    """Answers like Alpaca's REST API, one list entry per page, and records every request."""

    stock_quote: dict[str, Any] | None = field(
        default_factory=lambda: {"bp": 99.90, "ap": 100.10, "t": seconds_before_scan(1)}
    )
    stock_trade: dict[str, Any] | None = field(
        default_factory=lambda: {"p": 98.0, "t": seconds_before_scan(1)}
    )
    snapshot_pages: list[dict[str, Any]] = field(default_factory=list)
    contract_pages: list[list[dict[str, Any]]] = field(default_factory=list)
    bar_pages: list[list[dict[str, Any]]] = field(default_factory=list)
    unfiltered_contracts: list[dict[str, Any]] = field(default_factory=list)
    status: int = 200
    requests: list[SeenRequest] = field(default_factory=list)

    def handle(self, request: SeenRequest) -> Response:
        self.requests.append(request)
        if self.status != 200:
            return response(self.status)
        path = request.url.path
        if path == "/v2/stocks/AAPL/quotes/latest":
            return response(200, {"symbol": "AAPL", "quote": self.stock_quote})
        if path == "/v2/stocks/AAPL/trades/latest":
            return response(200, {"symbol": "AAPL", "trade": self.stock_trade})
        if path == "/v1beta1/options/snapshots/AAPL":
            page, token = self._page(self.snapshot_pages, request)
            return response(200, {"snapshots": page, "next_page_token": token})
        if path == "/v2/options/contracts" and request.url.params.get("limit") == "1":
            body = {"option_contracts": self.unfiltered_contracts[:1], "next_page_token": None}
            return response(200, body)
        if path == "/v2/options/contracts":
            page, token = self._page(self.contract_pages, request)
            return response(200, {"option_contracts": page, "next_page_token": token})
        if path == "/v2/stocks/bars":
            page, token = self._page(self.bar_pages, request)
            return response(200, {"bars": {"AAPL": page}, "next_page_token": token})
        return response(404)

    @staticmethod
    def _page(pages: list[Any], request: SeenRequest) -> tuple[Any, str | None]:
        if not pages:
            return None, None
        index = int(request.url.params.get("page_token", 0))
        token = str(index + 1) if index + 1 < len(pages) else None
        return pages[index], token

    def requests_to(self, path: str) -> list[SeenRequest]:
        return [request for request in self.requests if request.url.path == path]

    def adapter(self) -> AlpacaMarketData:
        return AlpacaMarketData(
            json_client(self.handle, max_retries=0),
            FixedClock(SCAN_TIME),
            data_base_url="https://data.test",
            trading_base_url="https://trading.test",
            key_id="key",
            secret_key="secret",
            feeds=FEEDS,
            chain_max_days=75,
            strike_range_pct=0.25,
            max_quote_age_seconds=300,
        )
