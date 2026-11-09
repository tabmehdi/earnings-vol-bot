from dataclasses import dataclass
from datetime import date, timedelta

from evbot.domain.events import EarningsEvent, ReportTiming, TradeWindow
from evbot.ports.trading_calendar import TradingCalendar


@dataclass(frozen=True, slots=True)
class TradeScheduler:
    calendar: TradingCalendar
    entry_minutes_before_close: int
    exit_minutes_after_open: int

    def window_for(self, event: EarningsEvent) -> TradeWindow:
        entry_session = self._entry_session(event)
        exit_session = self.calendar.next_session(entry_session)
        return TradeWindow(
            entry_at=self.calendar.close_time(entry_session)
            - timedelta(minutes=self.entry_minutes_before_close),
            exit_at=self.calendar.open_time(exit_session)
            + timedelta(minutes=self.exit_minutes_after_open),
        )

    def _entry_session(self, event: EarningsEvent) -> date:
        if event.timing is ReportTiming.AMC:
            return event.report_date
        if event.timing is ReportTiming.BMO:
            return self.calendar.previous_session(event.report_date)
        raise ValueError(f"cannot schedule {event.symbol}: report timing is unknown")
