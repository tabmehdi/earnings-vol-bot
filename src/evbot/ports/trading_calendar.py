from datetime import date, datetime
from typing import Protocol


class TradingCalendar(Protocol):
    def previous_session(self, day: date) -> date:
        """Latest session strictly before day."""
        ...

    def next_session(self, day: date) -> date:
        """Earliest session strictly after day."""
        ...

    def open_time(self, day: date) -> datetime:
        """Timezone-aware open of a session; raises TradingCalendarError on a non-session."""
        ...

    def close_time(self, day: date) -> datetime:
        """Timezone-aware close of a session; raises TradingCalendarError on a non-session."""
        ...
