from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class ReportTiming(Enum):
    BMO = "bmo"  # before market open
    AMC = "amc"  # after market close
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EarningsEvent:
    """Entry and exit sessions are resolved by a TradingCalendar, not here, so holidays count."""

    symbol: str
    report_date: date
    timing: ReportTiming

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must not be empty")


@dataclass(frozen=True, slots=True)
class TradeWindow:
    entry_at: datetime
    exit_at: datetime

    def __post_init__(self) -> None:
        if self.entry_at.utcoffset() is None or self.exit_at.utcoffset() is None:
            raise ValueError("entry_at and exit_at must be timezone-aware")
        if self.exit_at <= self.entry_at:
            raise ValueError(f"exit_at {self.exit_at} must be after entry_at {self.entry_at}")
