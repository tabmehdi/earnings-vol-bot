from datetime import date, datetime
from zoneinfo import ZoneInfo

from evbot.app.scan_preview import PreviewReport, ScanPreview, ScanUniverse
from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.signals import Verdict
from evbot.domain.snapshots import ChainSnapshot, DataFeeds
from tests.fakes.ports import (
    FakeEarningsCalendar,
    FakeMarketData,
    FakeTradingCalendar,
    FixedClock,
    InMemorySnapshotStore,
)
from tests.fixtures.market import AS_OF, BACKWARDATED, FRONT, build_chain, build_history
from tests.strategy.test_evaluator import CALM, LIQUID_VOLUME, evaluator

FEEDS = DataFeeds(options="indicative", spot="iex", history="sip")
SNAPSHOT = ChainSnapshot(build_chain(BACKWARDATED), 4, FEEDS)
HISTORY = build_history(CALM, LIQUID_VOLUME)
SESSIONS: dict[date, tuple[datetime, datetime]] = {
    date(2026, 11, 18): (AS_OF, AS_OF),
    date(2026, 11, 19): (AS_OF, AS_OF),
    date(2026, 11, 20): (AS_OF, AS_OF),
    date(2026, 11, 23): (AS_OF, AS_OF),
}
AAPL = EarningsEvent("AAPL", FRONT, ReportTiming.BMO)
NOPE = EarningsEvent("NOPE", FRONT, ReportTiming.BMO)
FRIDAY_AFTERNOON = datetime(2026, 11, 20, 15, 45, tzinfo=ZoneInfo("America/New_York"))
FRIDAY, MONDAY = date(2026, 11, 20), date(2026, 11, 23)


def preview_for(
    events: tuple[EarningsEvent, ...], now: datetime = AS_OF
) -> tuple[ScanPreview, InMemorySnapshotStore]:
    store = InMemorySnapshotStore()
    preview = ScanPreview(
        clock=FixedClock(now),
        trading_calendar=FakeTradingCalendar(SESSIONS),
        earnings=FakeEarningsCalendar(events),
        market_data=FakeMarketData({"AAPL": SNAPSHOT}, {"AAPL": HISTORY}),
        store=store,
        evaluator=evaluator(),
        history_bars=31,
    )
    return preview, store


def run(
    events: tuple[EarningsEvent, ...], limit: int | None = None
) -> tuple[PreviewReport, InMemorySnapshotStore]:
    preview, store = preview_for(events)
    return preview.evaluate(preview.find_reports(), limit), store


def event(symbol: str, day: date, timing: ReportTiming) -> EarningsEvent:
    return EarningsEvent(symbol, day, timing)


def friday_universe() -> ScanUniverse:
    preview, _ = preview_for(
        (
            event("FRI_AMC", FRIDAY, ReportTiming.AMC),
            event("FRI_BMO", FRIDAY, ReportTiming.BMO),
            event("FRI_UNK", FRIDAY, ReportTiming.UNKNOWN),
            event("MON_BMO", MONDAY, ReportTiming.BMO),
            event("MON_UNK", MONDAY, ReportTiming.UNKNOWN),
            event("MON_AMC", MONDAY, ReportTiming.AMC),
        ),
        now=FRIDAY_AFTERNOON,
    )
    return preview.find_reports()


def test_a_friday_scan_looks_at_friday_and_the_next_session_on_monday() -> None:
    universe = friday_universe()

    assert universe.today == FRIDAY
    assert universe.next_session == MONDAY


def test_keeps_amc_today_and_bmo_plus_unknown_next_session() -> None:
    selected = [e.symbol for e in friday_universe().selected]

    assert selected == ["FRI_AMC", "MON_BMO", "MON_UNK"]


def test_universe_keeps_every_report_found_for_the_counts() -> None:
    universe = friday_universe()

    assert len(universe.today_events) == 3
    assert len(universe.next_session_events) == 3


def test_unknown_timing_is_shown_as_avoid_with_the_reason() -> None:
    report, _ = run((event("AAPL", FRONT, ReportTiming.UNKNOWN),))

    signal = report.lines[0].signal
    assert signal is not None
    assert signal.verdict is Verdict.AVOID
    assert "report_timing" in [result.name for result in signal.failed()]


def test_given_tickers_are_scanned_as_reporting_next_session() -> None:
    preview, _ = preview_for(())

    universe = preview.universe_for(("AAPL", "MSFT"))

    assert universe.today_events == ()
    assert [(e.symbol, e.report_date) for e in universe.selected] == [
        ("AAPL", FRONT),
        ("MSFT", FRONT),
    ]
    assert all(e.timing is ReportTiming.BMO for e in universe.selected)


def test_evaluates_the_selected_reports() -> None:
    report, _ = run((AAPL,))

    line = report.lines[0]
    assert line.signal is not None
    assert line.signal.verdict is Verdict.RECOMMENDED


def test_reports_how_many_stale_quotes_were_dropped() -> None:
    report, _ = run((AAPL,))

    assert report.lines[0].stale_quotes_dropped == 4


def test_history_stops_at_the_last_completed_session() -> None:
    _, store = run((AAPL,))

    assert store.histories[("AAPL", AS_OF)].bars[-1].day == date(2026, 11, 18)


def test_chain_and_history_used_for_the_decision_are_saved() -> None:
    _, store = run((AAPL,))

    assert store.chains[("AAPL", AS_OF)] == SNAPSHOT
    assert ("AAPL", AS_OF) in store.histories


def test_a_failing_ticker_is_reported_and_the_run_continues() -> None:
    report, _ = run((NOPE, AAPL))

    assert report.lines[0].signal is None
    assert "NOPE" in report.lines[0].error
    assert report.lines[1].signal is not None


def test_limit_keeps_the_first_events() -> None:
    report, _ = run((AAPL, NOPE), limit=1)

    assert [line.event.symbol for line in report.lines] == ["AAPL"]


def test_no_limit_evaluates_everything_selected() -> None:
    report, _ = run((AAPL, NOPE))

    assert len(report.lines) == 2
