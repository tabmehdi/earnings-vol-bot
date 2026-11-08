from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from evbot.domain.events import EarningsEvent
from evbot.domain.spreads import CalendarSpread


class Verdict(Enum):
    RECOMMENDED = "recommended"
    CONSIDER = "consider"
    AVOID = "avoid"


@dataclass(frozen=True, slots=True)
class SignalMetrics:
    avg_volume_30d: float
    iv30_rv30: float
    term_slope: float


@dataclass(frozen=True, slots=True)
class FilterResult:
    """value/threshold are None for checks that are not numeric, e.g. unknown report timing."""

    name: str
    passed: bool
    value: float | None
    threshold: float | None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Signal:
    event: EarningsEvent
    as_of: datetime
    metrics: SignalMetrics | None
    filters: tuple[FilterResult, ...]
    verdict: Verdict
    spread: CalendarSpread | None

    def __post_init__(self) -> None:
        names = [f.name for f in self.filters]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate filter names in {names}")
        if self.verdict is not Verdict.AVOID and self.metrics is None:
            raise ValueError(f"a {self.verdict.value} signal needs metrics")
        if self.verdict is Verdict.RECOMMENDED and self.spread is None:
            raise ValueError("a recommended signal needs a spread")
        if self.spread is not None and self.spread.underlying != self.event.symbol:
            raise ValueError(f"spread on {self.spread.underlying} for {self.event.symbol} event")

    def failed(self) -> tuple[FilterResult, ...]:
        return tuple(f for f in self.filters if not f.passed)
