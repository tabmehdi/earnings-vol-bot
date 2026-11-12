from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from evbot.domain.signals import Signal


class ScanKind(Enum):
    SCHEDULED = "scheduled"  # the unattended daily run
    PREVIEW = "preview"  # a manual run of the same scan
    TICKERS = "tickers"  # a manual run on chosen tickers, not real earnings dates
    RECHECK = "recheck"  # a fresh look at the candidates just before entering


@dataclass(frozen=True, slots=True)
class ScanRecord:
    kind: ScanKind
    started_at: datetime
    finished_at: datetime
    signals: tuple[Signal, ...]
    entry_deadline: datetime | None = None
    risk_free_rate: float | None = None
    rate_source: str | None = None

    def __post_init__(self) -> None:
        times = (self.started_at, self.finished_at, self.entry_deadline)
        if any(t is not None and t.utcoffset() is None for t in times):
            raise ValueError("scan times must be timezone-aware")
        if self.finished_at < self.started_at:
            raise ValueError("a scan cannot finish before it starts")

    @property
    def finished_before_entry(self) -> bool | None:
        if self.entry_deadline is None:
            return None
        return self.finished_at <= self.entry_deadline
