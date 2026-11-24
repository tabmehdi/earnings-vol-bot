from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from evbot.domain.signals import LegPrices
from evbot.domain.spreads import CalendarSpread


class OrderIntent(Enum):
    OPEN = "open"  # buy the calendar: pay a debit
    CLOSE = "close"  # sell the calendar: receive a credit


class SpreadLeg(Enum):
    FRONT = "front"
    BACK = "back"


class OrderStatus(Enum):
    WORKING = "working"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


DONE = frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED})


@dataclass(frozen=True, slots=True)
class OrderPricing:
    """What one attempt's limit was computed from, so a missed fill can be explained later.

    attempt is the walk step (None for the single natural-price attempt). scheduled is the
    step's limit in the schedule built from attempt 0's quotes; limit is what was sent after
    capping it at these quotes' natural price. held means the walk kept the previous limit
    because the capped price would have moved it backwards. fetched_at is
    our clock when the quotes arrived; each leg's quoted_at is the broker's quote time. A leg
    order only has its own leg's prices.
    """

    attempt: int | None
    limit: float
    scheduled: float
    held: bool
    fetched_at: datetime
    front: LegPrices | None
    back: LegPrices | None


@dataclass(frozen=True, slots=True)
class OrderTicket:
    """One broker order: the whole calendar, or one of its legs when leg is set (a legged exit).

    A calendar order is priced per spread (a debit to open, a credit to close); a leg order per
    contract of that leg. Prices are always positive. client_order_id is ours, so a restarted
    session can find the order again.
    """

    order_id: str
    client_order_id: str
    spread: CalendarSpread
    intent: OrderIntent
    quantity: int
    limit_price: float
    status: OrderStatus
    submitted_at: datetime
    filled_quantity: int = 0
    fill_price: float | None = None
    leg: SpreadLeg | None = None
    priced_from: OrderPricing | None = None  # set by us, never by the broker
    broker_filled_at: datetime | None = None  # the broker's own fill time

    def __post_init__(self) -> None:
        if self.submitted_at.utcoffset() is None:
            raise ValueError("submitted_at must be timezone-aware")
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")
        if not 0 <= self.filled_quantity <= self.quantity:
            raise ValueError(f"filled {self.filled_quantity} of {self.quantity}")
        if self.filled_quantity > 0 and self.fill_price is None:
            raise ValueError("a fill needs a fill price")
        if self.status is OrderStatus.FILLED and self.filled_quantity != self.quantity:
            raise ValueError("a filled order must be filled in full")

    @property
    def is_done(self) -> bool:
        return self.status in DONE
