from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from evbot.adapters.alpaca_trading_calendar import AlpacaTradingCalendar
from evbot.ports.errors import TradingCalendarError
from tests.fixtures.http import json_client

NEW_YORK = ZoneInfo("America/New_York")
THANKSGIVING_WEEK = [
    {"date": "2026-11-24", "open": "09:30", "close": "16:00"},
    {"date": "2026-11-25", "open": "09:30", "close": "16:00"},
    {"date": "2026-11-27", "open": "09:30", "close": "13:00"},
    {"date": "2026-11-30", "open": "09:30", "close": "16:00"},
]


def calendar_returning(
    payload: Any, status: int = 200
) -> tuple[AlpacaTradingCalendar, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload)

    calendar = AlpacaTradingCalendar(
        json_client(handler, max_retries=0), "https://alpaca.test", "key", "secret"
    )
    return calendar, seen


def test_previous_session_skips_the_holiday() -> None:
    calendar, _ = calendar_returning(THANKSGIVING_WEEK)

    assert calendar.previous_session(date(2026, 11, 27)) == date(2026, 11, 25)


def test_next_session_skips_the_holiday() -> None:
    calendar, _ = calendar_returning(THANKSGIVING_WEEK)

    assert calendar.next_session(date(2026, 11, 25)) == date(2026, 11, 27)


def test_close_time_reports_an_early_close_in_new_york() -> None:
    calendar, _ = calendar_returning(THANKSGIVING_WEEK)

    assert calendar.close_time(date(2026, 11, 27)) == datetime(2026, 11, 27, 13, 0, tzinfo=NEW_YORK)


def test_open_time_is_timezone_aware() -> None:
    calendar, _ = calendar_returning(THANKSGIVING_WEEK)

    opens = calendar.open_time(date(2026, 11, 24))

    assert opens == datetime(2026, 11, 24, 9, 30, tzinfo=NEW_YORK)
    assert opens.utcoffset() is not None


def test_non_session_has_no_close_time() -> None:
    calendar, _ = calendar_returning(THANKSGIVING_WEEK)

    with pytest.raises(TradingCalendarError):
        calendar.close_time(date(2026, 11, 26))


def test_fetched_range_is_reused() -> None:
    calendar, seen = calendar_returning(THANKSGIVING_WEEK)

    calendar.close_time(date(2026, 11, 24))
    calendar.open_time(date(2026, 11, 25))
    calendar.next_session(date(2026, 11, 25))

    assert len(seen) == 1


def test_sends_credentials_and_the_date_range() -> None:
    calendar, seen = calendar_returning(THANKSGIVING_WEEK)

    calendar.close_time(date(2026, 11, 24))

    assert seen[0].url.path == "/v2/calendar"
    assert seen[0].headers["APCA-API-KEY-ID"] == "key"
    assert seen[0].url.params["start"] == "2026-11-10"
    assert seen[0].url.params["end"] == "2026-12-08"


def test_no_session_nearby_raises() -> None:
    calendar, _ = calendar_returning([])

    with pytest.raises(TradingCalendarError):
        calendar.next_session(date(2026, 11, 24))


def test_http_failure_raises() -> None:
    calendar, _ = calendar_returning({}, status=500)

    with pytest.raises(TradingCalendarError):
        calendar.close_time(date(2026, 11, 24))


def test_malformed_row_raises() -> None:
    calendar, _ = calendar_returning([{"date": "2026-11-24"}])

    with pytest.raises(TradingCalendarError):
        calendar.close_time(date(2026, 11, 24))
