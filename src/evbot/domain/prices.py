from dataclasses import dataclass
from datetime import date
from itertools import pairwise


@dataclass(frozen=True, slots=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: int

    def __post_init__(self) -> None:
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError(f"inconsistent OHLC on {self.day}")
        if self.volume < 0:
            raise ValueError(f"volume must not be negative on {self.day}")


@dataclass(frozen=True, slots=True)
class PriceHistory:
    symbol: str
    bars: tuple[Bar, ...]

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        if any(a.day >= b.day for a, b in pairwise(self.bars)):
            raise ValueError("bars must be in strictly ascending date order")

    def last(self, n: int) -> tuple[Bar, ...]:
        """Raises rather than returning fewer bars, so a 30-day metric never runs on 10 days."""
        if n <= 0:
            raise ValueError(f"n must be positive, got {n}")
        if n > len(self.bars):
            raise ValueError(f"need {n} bars, have {len(self.bars)}")
        return self.bars[-n:]
