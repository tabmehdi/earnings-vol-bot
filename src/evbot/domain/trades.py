from dataclasses import dataclass, replace
from datetime import datetime

from evbot.domain.spreads import CalendarSpread


@dataclass(frozen=True, slots=True)
class Fill:
    """A whole-spread fill; net_price is per spread (debit to open, credit to close)."""

    net_price: float
    quantity: int
    filled_at: datetime

    def __post_init__(self) -> None:
        if self.net_price < 0:
            raise ValueError(f"net_price must not be negative, got {self.net_price}")
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")


@dataclass(frozen=True, slots=True)
class Position:
    spread: CalendarSpread
    entry: Fill
    exit: Fill | None = None

    def __post_init__(self) -> None:
        if self.exit is None:
            return
        if self.exit.quantity != self.entry.quantity:
            raise ValueError("exit quantity must match entry quantity")
        if self.exit.filled_at < self.entry.filled_at:
            raise ValueError("exit cannot be filled before entry")

    @property
    def is_open(self) -> bool:
        return self.exit is None

    @property
    def cost(self) -> float:
        """Debit paid; also the most a long calendar can lose."""
        return self.entry.net_price * self.entry.quantity * self._multiplier

    def close(self, fill: Fill) -> "Position":
        if not self.is_open:
            raise ValueError("position is already closed")
        return replace(self, exit=fill)

    def pnl(self) -> float:
        if self.exit is None:
            raise ValueError("pnl is only defined for a closed position")
        return (self.exit.net_price - self.entry.net_price) * self.entry.quantity * self._multiplier

    @property
    def _multiplier(self) -> int:
        return self.spread.front.contract.multiplier
