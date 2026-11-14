from dataclasses import dataclass, replace
from datetime import datetime

from evbot.domain.signals import LegPrices
from evbot.domain.spreads import CalendarSpread


@dataclass(frozen=True, slots=True)
class Fill:
    """A whole-spread fill; net_price is per spread (debit to open, credit to close).
    A legged exit's net is the back sale minus the front buy-back, which can be negative.
    A single leg's fill (Position.front_closed) is priced per contract of that leg.

    front and back hold each leg's market when the fill was seen, so paper fills can later
    be compared with what a real fill at those quotes would have cost.
    """

    net_price: float
    quantity: int
    filled_at: datetime
    front: LegPrices | None = None
    back: LegPrices | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")


@dataclass(frozen=True, slots=True)
class Position:
    spread: CalendarSpread
    entry: Fill
    exit: Fill | None = None
    position_id: str = ""
    exit_at: datetime | None = None  # when the exit job should close it
    exit_failed: bool = False  # still open after the last exit attempt; needs a look
    front_closed: Fill | None = None  # the front bought back on its own: long back only

    def __post_init__(self) -> None:
        if self.entry.net_price < 0:
            raise ValueError(f"an entry debit cannot be negative, got {self.entry.net_price}")
        if self.front_closed is not None and self.front_closed.quantity != self.entry.quantity:
            raise ValueError("the front buy-back must cover the whole position")
        if self.exit is None:
            return
        if self.exit.quantity != self.entry.quantity:
            raise ValueError("exit quantity must match entry quantity")
        if self.exit.filled_at < self.entry.filled_at:
            raise ValueError("exit cannot be filled before entry")

    @property
    def symbol(self) -> str:
        return self.spread.underlying

    @property
    def is_open(self) -> bool:
        return self.exit is None

    @property
    def long_back_only(self) -> bool:
        """The short front is gone; only the long back is left to sell."""
        return self.front_closed is not None and self.exit is None

    @property
    def cost(self) -> float:
        """Debit paid; also the most a long calendar can lose."""
        return self.entry.net_price * self.entry.quantity * self._multiplier

    def close(self, fill: Fill) -> "Position":
        if not self.is_open:
            raise ValueError("position is already closed")
        return replace(self, exit=fill)

    def close_front(self, fill: Fill) -> "Position":
        if not self.is_open or self.front_closed is not None:
            raise ValueError("the front leg is already closed")
        return replace(self, front_closed=fill)

    def split(self, quantity: int) -> tuple["Position", "Position"]:
        """For a partial exit: (the part that filled, the part still open)."""
        if not 0 < quantity < self.entry.quantity:
            raise ValueError(f"cannot split {quantity} from {self.entry.quantity}")
        rest_quantity = self.entry.quantity - quantity
        front = self.front_closed
        return (
            replace(
                self,
                entry=replace(self.entry, quantity=quantity),
                front_closed=None if front is None else replace(front, quantity=quantity),
                position_id=f"{self.position_id}:{quantity}",
            ),
            replace(
                self,
                entry=replace(self.entry, quantity=rest_quantity),
                front_closed=None if front is None else replace(front, quantity=rest_quantity),
            ),
        )

    def mark_exit_failed(self) -> "Position":
        return replace(self, exit_failed=True)

    def pnl(self) -> float:
        if self.exit is None:
            raise ValueError("pnl is only defined for a closed position")
        return (self.exit.net_price - self.entry.net_price) * self.entry.quantity * self._multiplier

    @property
    def _multiplier(self) -> int:
        return self.spread.front.contract.multiplier
