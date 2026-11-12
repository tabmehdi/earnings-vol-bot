from datetime import date, datetime
from typing import Protocol


class TradingCalendar(Protocol):
    def previous_session(self, day: date) -> date:
        """Latest session strictly before day."""
        ...

    def next_session(self, day: date) -> date:
        """Earliest session strictly after day."""
        ...

    def sessions_between(self, start: date, end: date) -> tuple[date, ...]:
        """Every session from start to end, inclusive, in order; one lookup for a range."""
        ...

    def open_time(self, day: date) -> datetime:
        """Timezone-aware open of a session; raises TradingCalendarError on a non-session."""
        ...

    def close_time(self, day: date) -> datetime:
        """Timezone-aware close of a session; raises TradingCalendarError on a non-session."""
        ...
