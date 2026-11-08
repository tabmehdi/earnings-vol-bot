from datetime import date, datetime

import pytest

from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.options import OptionContract, OptionRight
from evbot.domain.signals import FilterResult, Signal, SignalMetrics, Verdict
from evbot.domain.spreads import CalendarSpread, Leg, Side

EVENT = EarningsEvent("AAPL", date(2026, 11, 19), ReportTiming.AMC)
AS_OF = datetime(2026, 11, 19, 15, 45)
METRICS = SignalMetrics(avg_volume_30d=2_000_000, iv30_rv30=1.40, term_slope=-0.006)
SPREAD = CalendarSpread(
    Leg(OptionContract("AAPL", date(2026, 11, 20), 100.0, OptionRight.CALL), Side.SELL),
    Leg(OptionContract("AAPL", date(2026, 12, 18), 100.0, OptionRight.CALL), Side.BUY),
)
PASSING = (
    FilterResult("avg_volume", True, 2_000_000, 1_500_000),
    FilterResult("iv_rv", True, 1.40, 1.25),
)


def test_failed_lists_only_failing_filters() -> None:
    filters = (*PASSING, FilterResult("term_slope", False, -0.001, -0.00406))
    signal = Signal(EVENT, AS_OF, METRICS, filters, Verdict.AVOID, spread=None)

    assert [f.name for f in signal.failed()] == ["term_slope"]


def test_avoid_signal_can_explain_unknown_timing_without_metrics() -> None:
    event = EarningsEvent("AAPL", date(2026, 11, 19), ReportTiming.UNKNOWN)
    reason = FilterResult("report_timing", False, None, None, detail="timing not supplied")

    signal = Signal(event, AS_OF, None, (reason,), Verdict.AVOID, spread=None)

    assert signal.failed()[0].detail == "timing not supplied"


def test_recommended_signal_requires_a_spread() -> None:
    with pytest.raises(ValueError):
        Signal(EVENT, AS_OF, METRICS, PASSING, Verdict.RECOMMENDED, spread=None)


def test_consider_signal_requires_metrics() -> None:
    with pytest.raises(ValueError):
        Signal(EVENT, AS_OF, None, PASSING, Verdict.CONSIDER, spread=SPREAD)


def test_spread_must_be_on_the_event_symbol() -> None:
    msft = EarningsEvent("MSFT", date(2026, 11, 19), ReportTiming.AMC)

    with pytest.raises(ValueError):
        Signal(msft, AS_OF, METRICS, PASSING, Verdict.RECOMMENDED, spread=SPREAD)


def test_filter_names_must_be_unique() -> None:
    with pytest.raises(ValueError):
        Signal(EVENT, AS_OF, METRICS, (*PASSING, PASSING[0]), Verdict.AVOID, spread=None)
