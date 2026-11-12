from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from evbot.domain.spreads import CalendarSpread


class OrderIntent(Enum):
    OPEN = "open"  # buy the calendar: pay a debit
    CLOSE = "close"  # sell the calendar: receive a credit


class OrderStatus(Enum):
    WORKING = "working"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


DONE = frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED})


@dataclass(frozen=True, slots=True)
class OrderTicket:
    """One broker order for a whole calendar. Prices are per spread: a debit to open,
    a credit to close. client_order_id is ours, so a restarted session can find it again."""

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
