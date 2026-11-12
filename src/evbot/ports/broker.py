from collections.abc import Mapping
from typing import Protocol

from evbot.domain.options import OptionContract
from evbot.domain.orders import OrderIntent, OrderTicket
from evbot.domain.spreads import CalendarSpread


class Broker(Protocol):
    """Places whole-calendar orders. Implementations must only ever trade a paper account."""

    def account_equity(self) -> float: ...

    def submit(
        self,
        spread: CalendarSpread,
        intent: OrderIntent,
        quantity: int,
        limit_price: float,
        client_order_id: str,
    ) -> OrderTicket:
        """A rejected order comes back with status REJECTED; transport failures raise
        BrokerError. A client_order_id already used raises BrokerError."""
        ...

    def order(self, order_id: str) -> OrderTicket:
        """The order's current state."""
        ...

    def find_order(self, client_order_id: str) -> OrderTicket | None:
        """Our order by its client id, so a restarted session resumes instead of duplicating."""
        ...

    def replace(self, order_id: str, limit_price: float) -> OrderTicket:
        """A new limit for a working order; the returned ticket may carry a new order_id."""
        ...

    def cancel(self, order_id: str) -> None: ...

    def held_contracts(self) -> Mapping[OptionContract, int]:
        """Option contracts held, signed: positive long, negative short."""
        ...
