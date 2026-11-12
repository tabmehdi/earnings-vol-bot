"""A fake Alpaca that holds a catalog of listed contracts and applies the request filters.

Unlike AlpacaServer, it serves several underlyings and filters contracts and snapshots by
underlying, strike and expiry the way the real endpoints do, with small pages, so the
per-ticker path and the batched screening path can be compared on the same data.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from requests import Response

from evbot.adapters.alpaca_market_data import AlpacaMarketData
from tests.fakes.ports import FixedClock
from tests.fixtures.alpaca_server import FEEDS, SCAN_TIME, option_snapshot, seconds_before_scan
from tests.fixtures.http import SeenRequest, json_client, response

CONTRACTS_PATH = "/v2/options/contracts"


def occ_symbol(root: str, expiry: str, right: str, strike: float) -> str:
    day = date.fromisoformat(expiry)
    return f"{root}{day:%y%m%d}{right[0].upper()}{round(strike * 1000):08d}"


def listed(
    underlying: str,
    expiry: str,
    strike: float,
    right: str = "call",
    root: str | None = None,
    multiplier: str = "100",
) -> dict[str, Any]:
    root = root or underlying
    return {
        "symbol": occ_symbol(root, expiry, right, strike),
        "root_symbol": root,
        "underlying_symbol": underlying,
        "multiplier": multiplier,
        "size": multiplier,
        "open_interest": "750",
        "strike_price": f"{strike:g}",
        "expiration_date": expiry,
        "type": right,
        "status": "active",
    }


def _matches(row: dict[str, Any], params: dict[str, str]) -> bool:
    strike, expiry = float(row["strike_price"]), row["expiration_date"]
    if "strike_price_gte" in params and strike < float(params["strike_price_gte"]):
        return False
    if "strike_price_lte" in params and strike > float(params["strike_price_lte"]):
        return False
    if "expiration_date_gte" in params and expiry < params["expiration_date_gte"]:
        return False
    if "expiration_date_lte" in params and expiry > params["expiration_date_lte"]:
        return False
    return "status" not in params or row["status"] == params["status"]


@dataclass
class ListedMarket:
    spots: dict[str, float] = field(default_factory=dict)
    contracts: list[dict[str, Any]] = field(default_factory=list)
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    page_size: int = 3
    requests: list[SeenRequest] = field(default_factory=list)

    def add(self, *rows: dict[str, Any], quote_age_seconds: float | None = 10.0) -> None:
        """Lists the contracts; with an age, also gives each one a quote that old."""
        for row in rows:
            self.contracts.append(row)
            if quote_age_seconds is not None:
                self.snapshots[row["symbol"]] = option_snapshot(age_seconds=quote_age_seconds)

    def handle(self, request: SeenRequest) -> Response:
        self.requests.append(request)
        parts = request.url.path.strip("/").split("/")
        params = request.url.params
        if parts[:2] == ["v2", "stocks"] and parts[-1] == "latest":
            return self._stock(parts[2], parts[3])
        if parts[:3] == ["v1beta1", "options", "snapshots"]:
            rows = [c for c in self._filtered(params) if c["underlying_symbol"] == parts[3]]
            quoted = [(c["symbol"], self.snapshots.get(c["symbol"])) for c in rows]
            quoted = [(s, q) for s, q in quoted if q is not None]
            page, token = self._page(quoted, params)
            return response(200, {"snapshots": dict(page), "next_page_token": token})
        if request.url.path == CONTRACTS_PATH:
            wanted = set(params["underlying_symbols"].split(","))
            rows = [c for c in self._filtered(params) if c["underlying_symbol"] in wanted]
            page, token = self._page(rows, params)
            return response(200, {"option_contracts": page, "next_page_token": token})
        return response(404)

    def _stock(self, symbol: str, kind: str) -> Response:
        spot = self.spots.get(symbol)
        if kind == "quotes":
            quote = None if spot is None else {"bp": spot, "ap": spot, "t": seconds_before_scan(1)}
            return response(200, {"symbol": symbol, "quote": quote})
        trade = None if spot is None else {"p": spot, "t": seconds_before_scan(1)}
        return response(200, {"symbol": symbol, "trade": trade})

    def _filtered(self, params: dict[str, str]) -> list[dict[str, Any]]:
        return [row for row in self.contracts if _matches(row, params)]

    def _page(self, rows: list[Any], params: dict[str, str]) -> tuple[list[Any], str | None]:
        size = min(self.page_size, int(params.get("limit", self.page_size)))
        start = int(params.get("page_token", 0))
        end = start + size
        return rows[start:end], (str(end) if end < len(rows) else None)

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

    def requests_to(self, path: str) -> list[SeenRequest]:
        return [r for r in self.requests if r.url.path == path]
