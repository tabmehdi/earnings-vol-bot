from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from evbot.domain.events import EarningsEvent, ReportTiming


def test_event_keeps_report_date_and_timing() -> None:
    event = EarningsEvent("AAPL", date(2026, 10, 29), ReportTiming.AMC)

    assert event.report_date == date(2026, 10, 29)
    assert event.timing is ReportTiming.AMC


def test_event_is_immutable() -> None:
    event = EarningsEvent("AAPL", date(2026, 10, 29), ReportTiming.BMO)

    with pytest.raises(FrozenInstanceError):
        event.symbol = "MSFT"  # type: ignore[misc]


def test_empty_symbol_is_rejected() -> None:
    with pytest.raises(ValueError):
        EarningsEvent("", date(2026, 10, 29), ReportTiming.UNKNOWN)
