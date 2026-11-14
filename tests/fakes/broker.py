from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime

from evbot.domain.options import OptionContract
from evbot.domain.orders import OrderIntent, OrderStatus, OrderTicket, SpreadLeg
from evbot.domain.spreads import CalendarSpread
from evbot.ports.clock import Clock
from evbot.ports.errors import BrokerError


@dataclass
class FakeBroker:
    """A paper broker with scripted behaviour.

    An order fills at its limit on the fill_on_check-th status check after submission (None:
    never). partial_fill limits how many contracts ever fill. fill_at_or_above fills only when
    a calendar's opening limit reaches it (or a closing limit drops to it), to model a real
    market. reject_calendar_closes refuses closing mleg orders, as Alpaca might; unfilled_legs
    names the legs whose single-leg orders never fill.
    """

    clock: Clock
    equity: float = 100_000.0
    fill_on_check: int | None = 1
    partial_fill: int | None = None
    fill_at_or_above: float | None = None
    reject: bool = False
    reject_calendar_closes: bool = False
    reject_late: bool = False  # the refusal arrives on the first status check, not on submit
    unfilled_legs: frozenset[SpreadLeg] = frozenset()
    fill_from: datetime | None = None  # nothing fills before this time
    held: dict[OptionContract, int] = field(default_factory=lambda: defaultdict(int))
    orders: dict[str, OrderTicket] = field(default_factory=dict)
    latest_by_client: dict[str, str] = field(default_factory=dict)
    submitted: list[OrderTicket] = field(default_factory=list)
    replaced: list[tuple[str, float]] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    _checks: dict[str, int] = field(default_factory=dict)

    def account_equity(self) -> float:
        return self.equity

    def submit(
        self,
        spread: CalendarSpread,
        intent: OrderIntent,
        quantity: int,
        limit_price: float,
        client_order_id: str,
        leg: SpreadLeg | None = None,
    ) -> OrderTicket:
        if client_order_id in self.latest_by_client:
            raise BrokerError(f"client_order_id {client_order_id} already used")
        refused = self.reject or (
            self.reject_calendar_closes and leg is None and intent is OrderIntent.CLOSE
        )
        status = OrderStatus.REJECTED if refused and not self.reject_late else OrderStatus.WORKING
        ticket = OrderTicket(
            f"o{len(self.orders) + 1}",
            client_order_id,
            spread,
            intent,
            quantity,
            limit_price,
            status,
            self.clock.now(),
            leg=leg,
        )
        self._store(ticket)
        self._checks[client_order_id] = 0
        self.submitted.append(ticket)
        return ticket

    def order(self, ticket: OrderTicket) -> OrderTicket:
        order_id = ticket.order_id
        ticket = self.orders[order_id]
        if ticket.is_done or self.latest_by_client[ticket.client_order_id] != order_id:
            return ticket
        self._checks[ticket.client_order_id] += 1
        if self.reject_late and self._refuses(ticket):
            ticket = replace(ticket, status=OrderStatus.REJECTED)
            self.orders[order_id] = ticket
            return ticket
        if self._fills_now(ticket):
            ticket = self._fill(ticket)
        return ticket

    def find_order(self, client_order_id: str, spread: CalendarSpread) -> OrderTicket | None:
        order_id = self.latest_by_client.get(client_order_id)
        return None if order_id is None else self.orders[order_id]

    def replace(self, ticket: OrderTicket, limit_price: float) -> OrderTicket:
        order_id = ticket.order_id
        old = self.orders[order_id]
        if old.is_done:
            raise BrokerError(f"order {order_id} is {old.status.value}")
        self.orders[order_id] = replace(old, status=OrderStatus.CANCELLED)
        new = replace(old, order_id=f"o{len(self.orders) + 1}", limit_price=limit_price)
        self._store(new)
        self.replaced.append((order_id, limit_price))
        return new

    def cancel(self, order_id: str) -> None:
        ticket = self.orders[order_id]
        if not ticket.is_done:
            self.orders[order_id] = replace(ticket, status=OrderStatus.CANCELLED)
        self.cancelled.append(order_id)

    def held_contracts(self) -> dict[OptionContract, int]:
        return {contract: qty for contract, qty in self.held.items() if qty}

    def _refuses(self, ticket: OrderTicket) -> bool:
        closing_calendar = ticket.leg is None and ticket.intent is OrderIntent.CLOSE
        return self.reject or (self.reject_calendar_closes and closing_calendar)

    def _store(self, ticket: OrderTicket) -> None:
        self.orders[ticket.order_id] = ticket
        self.latest_by_client[ticket.client_order_id] = ticket.order_id

    def _fills_now(self, ticket: OrderTicket) -> bool:
        if self.fill_on_check is None or self._checks[ticket.client_order_id] < self.fill_on_check:
            return False
        if self.fill_from is not None and self.clock.now() < self.fill_from:
            return False
        if ticket.leg is not None:
            return ticket.leg not in self.unfilled_legs
        if self.fill_at_or_above is None:
            return True
        if ticket.intent is OrderIntent.OPEN:
            return ticket.limit_price >= self.fill_at_or_above
        return ticket.limit_price <= self.fill_at_or_above

    def _fill(self, ticket: OrderTicket) -> OrderTicket:
        quantity = min(ticket.quantity, self.partial_fill or ticket.quantity)
        new_fill = quantity - ticket.filled_quantity
        if new_fill <= 0:
            return ticket
        status = OrderStatus.FILLED if quantity == ticket.quantity else OrderStatus.PARTIALLY_FILLED
        filled = replace(
            ticket, status=status, filled_quantity=quantity, fill_price=ticket.limit_price
        )
        self.orders[ticket.order_id] = filled
        sign = 1 if ticket.intent is OrderIntent.OPEN else -1
        if ticket.leg is not SpreadLeg.FRONT:
            self.held[ticket.spread.back.contract] += sign * new_fill
        if ticket.leg is not SpreadLeg.BACK:
            self.held[ticket.spread.front.contract] -= sign * new_fill
        return filled
