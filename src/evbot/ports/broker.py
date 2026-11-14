from collections.abc import Mapping
from typing import Protocol

from evbot.domain.options import OptionContract
from evbot.domain.orders import OrderIntent, OrderTicket, SpreadLeg
from evbot.domain.spreads import CalendarSpread


class Broker(Protocol):
    """Places calendar orders, or orders for one of a calendar's legs (a legged exit).
    Implementations must only ever trade a paper account.

    Calls about an existing order take our ticket or the spread, because the broker's answer
    for a single-leg order names only one contract: we say which calendar it belongs to.
    """

    def account_equity(self) -> float: ...

    def submit(
        self,
        spread: CalendarSpread,
        intent: OrderIntent,
        quantity: int,
        limit_price: float,
        client_order_id: str,
        leg: SpreadLeg | None = None,
    ) -> OrderTicket:
        """The whole calendar, or only `leg` of it. A rejected order comes back with status
        REJECTED; transport failures raise BrokerError. A client_order_id already used raises
        BrokerError."""
        ...

    def order(self, ticket: OrderTicket) -> OrderTicket:
        """The order's current state."""
        ...

    def find_order(self, client_order_id: str, spread: CalendarSpread) -> OrderTicket | None:
        """Our order by its client id, so a restarted session resumes instead of duplicating."""
        ...

    def replace(self, ticket: OrderTicket, limit_price: float) -> OrderTicket:
        """A new limit for a working order; the returned ticket may carry a new order_id."""
        ...

    def cancel(self, order_id: str) -> None: ...

    def held_contracts(self) -> Mapping[OptionContract, int]:
        """Option contracts held, signed: positive long, negative short."""
        ...
