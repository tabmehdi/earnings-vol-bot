from dataclasses import dataclass
from datetime import date
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
