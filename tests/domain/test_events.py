from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime

import pytest

from evbot.domain.events import EarningsEvent, ReportTiming, TradeWindow


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


ENTRY = datetime(2026, 11, 19, 15, 45, tzinfo=UTC)
EXIT = datetime(2026, 11, 20, 9, 45, tzinfo=UTC)


def test_trade_window_keeps_entry_and_exit() -> None:
    window = TradeWindow(ENTRY, EXIT)

    assert window.entry_at == ENTRY
    assert window.exit_at == EXIT


def test_trade_window_rejects_naive_entry() -> None:
    with pytest.raises(ValueError):
        TradeWindow(ENTRY.replace(tzinfo=None), EXIT)


def test_trade_window_rejects_naive_exit() -> None:
    with pytest.raises(ValueError):
        TradeWindow(ENTRY, EXIT.replace(tzinfo=None))


def test_trade_window_rejects_exit_before_entry() -> None:
    with pytest.raises(ValueError):
        TradeWindow(EXIT, ENTRY)


def test_trade_window_rejects_exit_equal_to_entry() -> None:
    with pytest.raises(ValueError):
        TradeWindow(ENTRY, ENTRY)
