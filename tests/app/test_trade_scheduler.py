from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from evbot.app.trade_scheduler import TradeScheduler
from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.ports.errors import TradingCalendarError
from tests.fakes.ports import FakeTradingCalendar

NEW_YORK = ZoneInfo("America/New_York")


def session(day: date, close_hour: int = 16) -> tuple[date, tuple[datetime, datetime]]:
    opens = datetime(day.year, day.month, day.day, 9, 30, tzinfo=NEW_YORK)
    closes = datetime(day.year, day.month, day.day, close_hour, 0, tzinfo=NEW_YORK)
    return day, (opens, closes)


# Thanksgiving week 2026: Thursday Nov 26 is closed and Friday Nov 27 closes at 13:00.
CALENDAR = FakeTradingCalendar(
    dict(
        [
            session(date(2026, 11, 20)),  # Friday
            session(date(2026, 11, 23)),  # Monday
            session(date(2026, 11, 24)),
            session(date(2026, 11, 25)),  # Wednesday
            session(date(2026, 11, 27), close_hour=13),  # Friday, early close
            session(date(2026, 11, 30)),  # Monday
        ]
    )
)


def at(day: date, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK)


def window_for(day: date, timing: ReportTiming) -> tuple[datetime, datetime]:
    scheduler = TradeScheduler(CALENDAR, entry_minutes_before_close=15, exit_minutes_after_open=15)
    window = scheduler.window_for(EarningsEvent("AAPL", day, timing))
    return window.entry_at, window.exit_at


def test_amc_enters_on_the_report_day_and_exits_next_session() -> None:
    entry, exit_at = window_for(date(2026, 11, 24), ReportTiming.AMC)

    assert entry == at(date(2026, 11, 24), 15, 45)
    assert exit_at == at(date(2026, 11, 25), 9, 45)


def test_bmo_on_monday_enters_friday_and_exits_monday() -> None:
    entry, exit_at = window_for(date(2026, 11, 23), ReportTiming.BMO)

    assert entry == at(date(2026, 11, 20), 15, 45)
    assert exit_at == at(date(2026, 11, 23), 9, 45)


def test_amc_before_a_holiday_exits_after_the_holiday() -> None:
    entry, exit_at = window_for(date(2026, 11, 25), ReportTiming.AMC)

    assert entry == at(date(2026, 11, 25), 15, 45)
    assert exit_at == at(date(2026, 11, 27), 9, 45)


def test_bmo_after_a_holiday_enters_before_the_holiday() -> None:
    entry, _ = window_for(date(2026, 11, 27), ReportTiming.BMO)

    assert entry == at(date(2026, 11, 25), 15, 45)


def test_amc_on_an_early_close_enters_before_that_close() -> None:
    entry, exit_at = window_for(date(2026, 11, 27), ReportTiming.AMC)

    assert entry == at(date(2026, 11, 27), 13, 0) - timedelta(minutes=15)
    assert exit_at == at(date(2026, 11, 30), 9, 45)


def test_amc_on_a_market_holiday_raises() -> None:
    with pytest.raises(TradingCalendarError):
        window_for(date(2026, 11, 26), ReportTiming.AMC)


def test_unknown_timing_cannot_be_scheduled() -> None:
    with pytest.raises(ValueError):
        window_for(date(2026, 11, 24), ReportTiming.UNKNOWN)
