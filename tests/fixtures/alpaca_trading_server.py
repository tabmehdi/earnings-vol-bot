"""A fake Alpaca Trading API for orders, the account and positions."""

import json
from dataclasses import dataclass, field
from typing import Any

from requests import Response

from evbot.adapters.alpaca_broker import AlpacaBroker
from tests.fakes.ports import FixedClock
from tests.fixtures.alpaca_server import SCAN_TIME
from tests.fixtures.http import SeenRequest, json_client, response

PAPER_URL = "https://paper-api.alpaca.markets"
FRONT_OCC = "AAPL261120C00100000"
BACK_OCC = "AAPL261218C00100000"


def order_json(
    order_id: str = "a1",
    client_order_id: str = "AAPL-2026-11-20-open",
    status: str = "new",
    filled_qty: str = "0",
    filled_avg_price: str | None = None,
    qty: str = "3",
    limit_price: str = "2.10",
    opening: bool = True,
) -> dict[str, Any]:
    front_intent, back_intent = (
        ("sell_to_open", "buy_to_open") if opening else ("buy_to_close", "sell_to_close")
    )
    return {
        "id": order_id,
        "client_order_id": client_order_id,
        "order_class": "mleg",
        "status": status,
        "qty": qty,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg_price,
        "limit_price": limit_price,
        "submitted_at": "2026-11-19T20:45:00.123456789Z",
        "legs": [
            {
                "symbol": BACK_OCC,
                "side": "buy" if opening else "sell",
                "position_intent": back_intent,
            },
            {
                "symbol": FRONT_OCC,
                "side": "sell" if opening else "buy",
                "position_intent": front_intent,
            },
        ],
    }


@dataclass
class AlpacaTradingServer:
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    equity: str = "100000.55"
    positions: list[dict[str, Any]] = field(default_factory=list)
    reject_with: tuple[int, str] | None = None
    next_status: str = "new"
    requests: list[SeenRequest] = field(default_factory=list)
    bodies: list[dict[str, Any]] = field(default_factory=list)

    def handle(self, request: SeenRequest) -> Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if request.body:
            self.bodies.append(json.loads(request.body))
        if path == "/v2/account":
            return response(200, {"equity": self.equity, "options_trading_level": 3})
        if path == "/v2/positions":
            return response(200, self.positions)
        if path == "/v2/orders" and method == "POST":
            return self._submit(self.bodies[-1])
        if path == "/v2/orders:by_client_order_id":
            wanted = request.url.params["client_order_id"]
            found = [o for o in self.orders.values() if o["client_order_id"] == wanted]
            return response(200, found[-1]) if found else response(404, {"message": "not found"})
        if path.startswith("/v2/orders/"):
            return self._one(path.rsplit("/", 1)[1], method)
        return response(404)

    def _submit(self, body: dict[str, Any]) -> Response:
        if self.reject_with is not None:
            status, message = self.reject_with
            return response(status, {"code": 40310000, "message": message})
        if any(o["client_order_id"] == body["client_order_id"] for o in self.orders.values()):
            return response(422, {"message": "client_order_id must be unique"})
        order_id = f"a{len(self.orders) + 1}"
        opening = body["legs"][0]["position_intent"].endswith("open")
        order = order_json(
            order_id,
            body["client_order_id"],
            self.next_status,
            qty=body["qty"],
            limit_price=body["limit_price"],
            opening=opening,
        )
        self.orders[order_id] = order
        return response(200, order)

    def _one(self, order_id: str, method: str) -> Response:
        if order_id not in self.orders:
            return response(404, {"message": "order not found"})
        order = self.orders[order_id]
        if method == "GET":
            return response(200, order)
        if method == "DELETE":
            if order["status"] == "filled":
                return response(422, {"message": "order is not cancelable"})
            order["status"] = "canceled"
            return response(204)
        if method == "PATCH":
            order["status"] = "replaced"
            new_id = f"a{len(self.orders) + 1}"
            new = {
                **order,
                "id": new_id,
                "status": "new",
                "limit_price": self.bodies[-1]["limit_price"],
            }
            self.orders[new_id] = new
            return response(200, new)
        return response(405)

    def broker(self, paper: bool = True, base_url: str = PAPER_URL) -> AlpacaBroker:
        return AlpacaBroker(
            json_client(self.handle, max_retries=0),
            FixedClock(SCAN_TIME),
            base_url,
            "key",
            "secret",
            paper,
        )
