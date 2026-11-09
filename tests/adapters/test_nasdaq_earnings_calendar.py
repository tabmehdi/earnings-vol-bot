from datetime import date
from typing import Any

import httpx
import pytest

from evbot.adapters.nasdaq_earnings_calendar import NasdaqEarningsCalendar
from evbot.domain.events import ReportTiming
from evbot.ports.errors import EarningsCalendarError
from tests.fixtures.http import json_client

URL = "https://nasdaq.test/earnings"
DAY = date(2026, 11, 10)


def calendar_returning(payload: Any, status: int = 200) -> NasdaqEarningsCalendar:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["date"] == "2026-11-10"
        assert "Mozilla" in request.headers["User-Agent"]
        return httpx.Response(status, json=payload)

    return NasdaqEarningsCalendar(json_client(handler, max_retries=0), URL)


def rows(*items: dict[str, Any]) -> dict[str, Any]:
    return {"data": {"asOf": "Tue, Nov 10, 2026", "rows": list(items)}}


@pytest.mark.parametrize(
    ("label", "timing"),
    [
        ("time-pre-market", ReportTiming.BMO),
        ("time-after-hours", ReportTiming.AMC),
        ("time-not-supplied", ReportTiming.UNKNOWN),
        ("time-something-new", ReportTiming.UNKNOWN),
    ],
)
def test_maps_report_time_labels(label: str, timing: ReportTiming) -> None:
    events = calendar_returning(rows({"symbol": "TDG", "time": label})).events_on(DAY)

    assert events[0].timing is timing
    assert events[0].symbol == "TDG"
    assert events[0].report_date == DAY


def test_keeps_the_order_nasdaq_returns() -> None:
    payload = rows(
        {"symbol": "PBR", "time": "time-after-hours"}, {"symbol": "TDG", "time": "time-pre-market"}
    )

    assert [e.symbol for e in calendar_returning(payload).events_on(DAY)] == ["PBR", "TDG"]


def test_reads_every_row_returned() -> None:
    payload = rows(*({"symbol": f"T{i}", "time": "time-pre-market"} for i in range(1_500)))

    assert len(calendar_returning(payload).events_on(DAY)) == 1_500


def test_null_rows_mean_no_reports_that_day() -> None:
    payload = {"data": {"asOf": "Sun, Nov 8, 2026", "headers": None, "rows": None}}

    assert calendar_returning(payload).events_on(DAY) == ()


def test_row_without_a_symbol_is_an_error() -> None:
    with pytest.raises(EarningsCalendarError):
        calendar_returning(rows({"time": "time-pre-market"})).events_on(DAY)


def test_missing_data_is_an_error() -> None:
    with pytest.raises(EarningsCalendarError):
        calendar_returning({"data": None}).events_on(DAY)


def test_http_failure_is_an_error() -> None:
    with pytest.raises(EarningsCalendarError):
        calendar_returning({}, status=403).events_on(DAY)
