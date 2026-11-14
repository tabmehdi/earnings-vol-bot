import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from evbot.adapters.alpaca_market_data import occ_symbol
from evbot.adapters.http_client import HttpRequestError, JsonSender
from evbot.domain.options import OptionContract, OptionRight
from evbot.domain.orders import OrderIntent, OrderStatus, OrderTicket, SpreadLeg
from evbot.domain.spreads import CalendarSpread, Leg, Side
from evbot.ports.clock import Clock
from evbot.ports.errors import BrokerError

PAPER_URL = "https://paper-api.alpaca.markets"
OCC = re.compile(r"^(?P<root>[A-Z][A-Z0-9.]*?)(?P<day>\d{6})(?P<right>[CP])(?P<strike>\d{8})$")
REFUSED = frozenset({403, 422})  # Alpaca's answer when it will not take an order
STATUS = {
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "rejected": OrderStatus.REJECTED,
    # No more fills can come from these; a replaced order lives on under its new id.
    "canceled": OrderStatus.CANCELLED,
    "expired": OrderStatus.CANCELLED,
    "replaced": OrderStatus.CANCELLED,
    "done_for_day": OrderStatus.CANCELLED,
    "stopped": OrderStatus.CANCELLED,
    "suspended": OrderStatus.CANCELLED,
}  # anything else (new, accepted, pending_*...) is still working
OPEN_INTENTS = {Side.SELL: "sell_to_open", Side.BUY: "buy_to_open"}
CLOSE_INTENTS = {Side.SELL: "buy_to_close", Side.BUY: "sell_to_close"}


class AlpacaBroker:
    """Alpaca option orders on the paper account only: multi-leg (mleg) orders for a whole
    calendar, single-leg orders for one leg of it.

    Our prices are always positive. An mleg limit is positive for a debit and negative for a
    credit, so the sign is added and removed here; a single-leg limit is always positive.
    Orders are sent once and never retried automatically: recovery goes through the client
    order id.
    """

    def __init__(
        self,
        http: JsonSender,
        clock: Clock,
        base_url: str,
        key_id: str,
        secret_key: str,
        paper: bool,
    ) -> None:
        if not paper or base_url.rstrip("/") != PAPER_URL:
            raise BrokerError(
                f"AlpacaBroker only trades the paper account: set ALPACA_PAPER=true and use "
                f"{PAPER_URL}"
            )
        self._http = http
        self._clock = clock
        self._url = PAPER_URL
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}

    def account_equity(self) -> float:
        return float(self._get("/v2/account")["equity"])

    def submit(
        self,
        spread: CalendarSpread,
        intent: OrderIntent,
        quantity: int,
        limit_price: float,
        client_order_id: str,
        leg: SpreadLeg | None = None,
    ) -> OrderTicket:
        body = order_body(spread, intent, quantity, limit_price, client_order_id, leg)
        try:
            order = self._http.send("POST", f"{self._url}/v2/orders", body, self._headers)
        except HttpRequestError as error:
            if error.status in REFUSED and "unique" in error.body:
                raise BrokerError(f"{client_order_id}: client_order_id must be unique") from error
            if error.status in REFUSED:
                return OrderTicket(
                    f"rejected:{client_order_id}",
                    client_order_id,
                    spread,
                    intent,
                    quantity,
                    limit_price,
                    OrderStatus.REJECTED,
                    self._clock.now(),
                    leg=leg,
                )
            raise BrokerError(f"{client_order_id}: {error}") from error
        return self._ticket(order, spread)

    def order(self, ticket: OrderTicket) -> OrderTicket:
        return self._ticket(self._get(f"/v2/orders/{ticket.order_id}"), ticket.spread)

    def find_order(self, client_order_id: str, spread: CalendarSpread) -> OrderTicket | None:
        try:
            order = self._http.get(
                f"{self._url}/v2/orders:by_client_order_id",
                {"client_order_id": client_order_id},
                self._headers,
            )
        except HttpRequestError as error:
            if error.status == 404:
                return None
            raise BrokerError(f"{client_order_id}: {error}") from error
        return self._ticket(order, spread)

    def replace(self, ticket: OrderTicket, limit_price: float) -> OrderTicket:
        price = f"{limit_price:.2f}" if ticket.leg else _signed(limit_price, ticket.intent)
        try:
            order = self._http.send(
                "PATCH",
                f"{self._url}/v2/orders/{ticket.order_id}",
                {"limit_price": price},
                self._headers,
            )
        except HttpRequestError as error:
            raise BrokerError(f"replace {ticket.order_id}: {error}") from error
        return self._ticket(order, ticket.spread)

    def cancel(self, order_id: str) -> None:
        try:
            self._http.send("DELETE", f"{self._url}/v2/orders/{order_id}", None, self._headers)
        except HttpRequestError as error:
            if error.status == 422:
                return  # already filled or cancelled: nothing left to cancel
            raise BrokerError(f"cancel {order_id}: {error}") from error

    def held_contracts(self) -> Mapping[OptionContract, int]:
        held = {}
        for position in self._get("/v2/positions"):
            if position.get("asset_class") != "us_option":
                continue
            quantity = abs(int(float(position["qty"])))
            held[_contract(position["symbol"])] = (
                -quantity if position.get("side") == "short" else quantity
            )
        return held

    def _get(self, path: str) -> Any:
        try:
            return self._http.get(f"{self._url}{path}", None, self._headers)
        except HttpRequestError as error:
            raise BrokerError(str(error)) from error

    def _ticket(self, order: Any, spread: CalendarSpread) -> OrderTicket:
        try:
            leg: SpreadLeg | None = None
            if order.get("legs"):
                legs = [(_contract(row["symbol"]), row) for row in order["legs"]]
                # The earlier expiry is the front leg, whatever order Alpaca lists them in.
                (front, intent_row), (back, _) = sorted(legs, key=lambda pair: pair[0].expiry)
                spread = CalendarSpread(Leg(front, Side.SELL), Leg(back, Side.BUY))
            else:
                leg, intent_row = _which_leg(_contract(order["symbol"]), spread), order
            intent = (
                OrderIntent.OPEN
                if intent_row["position_intent"].endswith("open")
                else OrderIntent.CLOSE
            )
            quantity = int(float(order["qty"]))
            filled = int(float(order.get("filled_qty") or 0))
            status = STATUS.get(order["status"], OrderStatus.WORKING)
            if status is OrderStatus.WORKING and filled:
                status = OrderStatus.PARTIALLY_FILLED
            limit = abs(float(order["limit_price"]))
            average = order.get("filled_avg_price")
            fill_price = abs(float(average)) if filled and average is not None else None
            ticket = OrderTicket(
                order["id"],
                order["client_order_id"],
                spread,
                intent,
                quantity,
                limit,
                status,
                datetime.fromisoformat(order["submitted_at"]),
                filled,
                fill_price if fill_price is not None or not filled else limit,
                leg,
            )
        except (KeyError, TypeError, ValueError, IndexError) as error:
            raise BrokerError(f"unexpected Alpaca order format: {error!r}") from error
        return ticket


def order_body(
    spread: CalendarSpread,
    intent: OrderIntent,
    quantity: int,
    limit_price: float,
    client_order_id: str,
    leg: SpreadLeg | None = None,
) -> dict[str, Any]:
    """What is POSTed to /v2/orders: the whole calendar as one mleg order, or one leg."""
    intents = OPEN_INTENTS if intent is OrderIntent.OPEN else CLOSE_INTENTS
    if leg is not None:
        traded = spread.front if leg is SpreadLeg.FRONT else spread.back
        return {
            "symbol": occ_symbol(traded.contract),
            "qty": str(quantity),
            "side": _side(traded.side, intent).value,
            "type": "limit",
            "limit_price": f"{limit_price:.2f}",
            "time_in_force": "day",
            "position_intent": intents[traded.side],
            "client_order_id": client_order_id,
        }
    legs = [
        {
            "symbol": occ_symbol(each.contract),
            "ratio_qty": "1",
            "side": _side(each.side, intent).value,
            "position_intent": intents[each.side],
        }
        for each in (spread.front, spread.back)
    ]
    return {
        "order_class": "mleg",
        "qty": str(quantity),
        "type": "limit",
        "limit_price": _signed(limit_price, intent),
        "time_in_force": "day",
        "client_order_id": client_order_id,
        "legs": legs,
    }


def _which_leg(contract: OptionContract, spread: CalendarSpread) -> SpreadLeg:
    if contract == spread.front.contract:
        return SpreadLeg.FRONT
    if contract == spread.back.contract:
        return SpreadLeg.BACK
    raise ValueError(f"{contract} is not a leg of the {spread.underlying} calendar")


def _side(leg_side: Side, intent: OrderIntent) -> Side:
    """This leg's order side: as in the calendar to open it, the opposite to close it."""
    return leg_side if intent is OrderIntent.OPEN else _opposite(leg_side)


def _signed(price: float, intent: OrderIntent) -> str:
    return f"{price:.2f}" if intent is OrderIntent.OPEN else f"{-price:.2f}"


def _opposite(side: Side) -> Side:
    return Side.BUY if side is Side.SELL else Side.SELL


def _contract(symbol: str) -> OptionContract:
    match = OCC.match(symbol)
    if match is None:
        raise ValueError(f"not an option symbol: {symbol}")
    day = match["day"]
    return OptionContract(
        match["root"],
        date(2000 + int(day[:2]), int(day[2:4]), int(day[4:])),
        int(match["strike"]) / 1000,
        OptionRight.CALL if match["right"] == "C" else OptionRight.PUT,
    )
