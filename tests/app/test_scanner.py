from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from evbot.app.scanner import MARKET_DATA, QUOTE_FRESHNESS, Scanner, ScanUniverse
from evbot.app.trade_scheduler import TradeScheduler
from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.scans import ScanKind, ScanRecord
from evbot.domain.signals import Verdict
from evbot.domain.snapshots import ChainSnapshot, DataFeeds
from tests.fakes.ports import (
    FailingRiskFreeRate,
    FakeEarningsCalendar,
    FakeMarketData,
    FakeTradingCalendar,
    FixedClock,
    FixedRiskFreeRate,
    InMemorySnapshotStore,
    InMemoryTradeRepository,
)
from tests.fixtures.market import AS_OF, BACKWARDATED, FRONT, build_chain, build_history
from tests.strategy.test_evaluator import CALM, LIQUID_VOLUME, evaluator

NEW_YORK = ZoneInfo("America/New_York")
FEEDS = DataFeeds(options="indicative", spot="iex", history="sip")
SNAPSHOT = ChainSnapshot(build_chain(BACKWARDATED), 4, FEEDS)
HISTORY = build_history(CALM, LIQUID_VOLUME)
THURSDAY, FRIDAY, MONDAY = date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 23)
HOLIDAY = date(2026, 11, 26)


def session(day: date) -> tuple[date, tuple[datetime, datetime]]:
    opens = datetime(day.year, day.month, day.day, 9, 30, tzinfo=NEW_YORK)
    closes = datetime(day.year, day.month, day.day, 16, 0, tzinfo=NEW_YORK)
    return day, (opens, closes)


SESSIONS = dict(
    session(day)
    for day in (
        date(2026, 11, 18),
        THURSDAY,
        FRIDAY,
        MONDAY,
        date(2026, 11, 25),
        date(2026, 11, 27),
        date(2026, 12, 18),
        date(2027, 1, 15),
        date(2027, 2, 19),
    )
)
AAPL = EarningsEvent("AAPL", FRONT, ReportTiming.BMO)
NOPE = EarningsEvent("NOPE", FRONT, ReportTiming.BMO)
FRIDAY_AFTERNOON = datetime(2026, 11, 20, 15, 45, tzinfo=NEW_YORK)
FALLBACK_RATE = 0.045


def scanner_for(
    events: tuple[EarningsEvent, ...],
    now: datetime = AS_OF,
    repository: InMemoryTradeRepository | None = None,
    rates: FixedRiskFreeRate | FailingRiskFreeRate | None = None,
    sessions_without: date | None = None,
) -> tuple[Scanner, InMemorySnapshotStore, InMemoryTradeRepository]:
    store = InMemorySnapshotStore()
    repository = repository if repository is not None else InMemoryTradeRepository()
    sessions = {day: times for day, times in SESSIONS.items() if day != sessions_without}
    calendar = FakeTradingCalendar(sessions)
    scanner = Scanner(
        clock=FixedClock(now),
        trading_calendar=calendar,
        earnings=FakeEarningsCalendar(events),
        market_data=FakeMarketData({"AAPL": SNAPSHOT}, {"AAPL": HISTORY}),
        store=store,
        repository=repository,
        evaluator=evaluator(),
        scheduler=TradeScheduler(
            calendar, entry_minutes_before_close=15, exit_minutes_after_open=15
        ),
        history_bars=31,
        monthly_horizon_days=75,
        rates=rates,
        fallback_rate=FALLBACK_RATE,
    )
    return scanner, store, repository


def run(
    events: tuple[EarningsEvent, ...], limit: int | None = None, now: datetime = AS_OF
) -> tuple[ScanRecord, InMemorySnapshotStore, InMemoryTradeRepository]:
    scanner, store, repository = scanner_for(events, now)
    return scanner.run(scanner.find_reports(), ScanKind.SCHEDULED, limit), store, repository


def event(symbol: str, day: date, timing: ReportTiming) -> EarningsEvent:
    return EarningsEvent(symbol, day, timing)


def friday_universe() -> ScanUniverse:
    scanner, _, _ = scanner_for(
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
    return scanner.find_reports()


# Which reports are scanned


def test_a_friday_scan_looks_at_friday_and_the_next_session_on_monday() -> None:
    universe = friday_universe()

    assert (universe.today, universe.next_session) == (FRIDAY, MONDAY)


def test_keeps_amc_today_and_bmo_plus_unknown_next_session() -> None:
    assert [e.symbol for e in friday_universe().selected] == ["FRI_AMC", "MON_BMO", "MON_UNK"]


def test_universe_keeps_every_report_found_for_the_counts() -> None:
    universe = friday_universe()

    assert (len(universe.today_events), len(universe.next_session_events)) == (3, 3)


def test_given_tickers_are_scanned_as_reporting_next_session() -> None:
    scanner, _, _ = scanner_for(())

    universe = scanner.universe_for(("AAPL", "MSFT"))

    assert universe.today_events == ()
    assert [(e.symbol, e.report_date, e.timing) for e in universe.selected] == [
        ("AAPL", FRONT, ReportTiming.BMO),
        ("MSFT", FRONT, ReportTiming.BMO),
    ]


# What a scan records


def test_scan_is_saved_with_one_signal_per_report() -> None:
    record, _, repository = run((AAPL,))

    assert repository.scans == [record]
    assert record.kind is ScanKind.SCHEDULED
    assert record.signals[0].verdict is Verdict.RECOMMENDED


def test_unknown_timing_is_avoid_with_the_reason() -> None:
    record, _, _ = run((event("AAPL", FRONT, ReportTiming.UNKNOWN),))

    assert "report_timing" in [result.name for result in record.signals[0].failed()]


def test_stale_quote_count_is_recorded_on_the_signal() -> None:
    record, _, _ = run((AAPL,))

    freshness = next(r for r in record.signals[0].filters if r.name == QUOTE_FRESHNESS)
    assert freshness.passed
    assert freshness.detail == "stale_quotes_dropped=4"


def test_history_stops_at_the_last_completed_session() -> None:
    _, store, _ = run((AAPL,))

    assert store.histories[("AAPL", AS_OF)].bars[-1].day == date(2026, 11, 18)


def test_chain_and_history_used_for_the_decision_are_saved() -> None:
    _, store, _ = run((AAPL,))

    assert store.chains[("AAPL", AS_OF)] == SNAPSHOT
    assert ("AAPL", AS_OF) in store.histories


def test_a_data_failure_becomes_an_explained_avoid_and_the_scan_continues() -> None:
    record, store, _ = run((NOPE, AAPL))

    failed = record.signals[0]
    assert failed.verdict is Verdict.AVOID
    assert failed.metrics is None
    assert [r.name for r in failed.failed()] == [MARKET_DATA]
    assert "NOPE" in failed.failed()[0].detail
    assert failed.as_of == AS_OF
    assert record.signals[1].verdict is Verdict.RECOMMENDED
    assert ("NOPE", AS_OF) not in store.chains


def test_limit_keeps_the_first_events() -> None:
    record, _, _ = run((AAPL, NOPE), limit=1)

    assert [s.event.symbol for s in record.signals] == ["AAPL"]


def test_no_limit_scans_everything_selected() -> None:
    record, _, _ = run((AAPL, NOPE))

    assert len(record.signals) == 2


# Entry deadline


def test_deadline_is_the_earliest_entry_among_known_timings() -> None:
    # AAPL reports BMO on Friday, so it is entered on Thursday at 16:00 - 15 min.
    unknown = event("ZZZ", FRONT, ReportTiming.UNKNOWN)

    record, _, _ = run((unknown, AAPL))

    assert record.entry_deadline == datetime(2026, 11, 19, 15, 45, tzinfo=NEW_YORK)
    assert record.finished_before_entry is True


def test_a_scan_that_ends_after_the_entry_time_is_marked_late() -> None:
    record, _, _ = run((AAPL,), now=AS_OF + timedelta(minutes=5))

    assert record.finished_before_entry is False


def test_no_deadline_when_no_report_can_be_scheduled() -> None:
    record, _, _ = run((event("ZZZ", FRONT, ReportTiming.UNKNOWN),))

    assert record.entry_deadline is None


# When the scheduled scan should not run


def test_skip_on_a_market_holiday() -> None:
    scanner, _, _ = scanner_for((), now=datetime(2026, 11, 26, 15, 25, tzinfo=NEW_YORK))

    reason = scanner.skip_reason()

    assert reason is not None
    assert "2026-11-26" in reason


def test_skip_when_a_scheduled_scan_is_already_recorded_today() -> None:
    scanner, _, repository = scanner_for((AAPL,))
    scanner.run(scanner.find_reports(), ScanKind.SCHEDULED)

    reason = scanner.skip_reason()

    assert reason is not None
    assert "already" in reason
    assert len(repository.scans) == 1


def test_a_preview_scan_does_not_block_the_scheduled_one() -> None:
    scanner, _, _ = scanner_for((AAPL,))
    scanner.run(scanner.find_reports(), ScanKind.PREVIEW)

    assert scanner.skip_reason() is None


def test_no_skip_on_a_normal_session() -> None:
    scanner, _, _ = scanner_for(())

    assert scanner.skip_reason() is None
    assert HOLIDAY not in SESSIONS


# Risk-free rate


def scan_with_rates(
    rates: FixedRiskFreeRate | FailingRiskFreeRate | None,
) -> ScanRecord:
    scanner, _, _ = scanner_for((AAPL,), rates=rates)
    return scanner.run(scanner.find_reports(), ScanKind.SCHEDULED)


def test_the_providers_rate_is_used_and_recorded() -> None:
    record = scan_with_rates(FixedRiskFreeRate(0.0384, "FRED DGS3MO"))

    assert record.risk_free_rate == 0.0384
    assert record.rate_source == "FRED DGS3MO"


def test_the_rate_reaches_the_iv_calculation() -> None:
    low = scan_with_rates(FixedRiskFreeRate(0.0)).signals[0].metrics
    high = scan_with_rates(FixedRiskFreeRate(0.20)).signals[0].metrics

    assert low is not None and high is not None
    assert low.iv30_rv30 != high.iv30_rv30


def test_a_failing_provider_falls_back_to_config_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    record = scan_with_rates(FailingRiskFreeRate())

    assert record.risk_free_rate == FALLBACK_RATE
    assert record.rate_source == "config"
    assert record.signals[0].verdict is Verdict.RECOMMENDED
    assert "rate source is down" in caplog.text


def test_no_provider_falls_back_to_config_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    record = scan_with_rates(None)

    assert (record.risk_free_rate, record.rate_source) == (FALLBACK_RATE, "config")
    assert "no risk-free rate source" in caplog.text


# Monthly back leg


def test_the_back_leg_is_the_standard_monthly_from_the_calendar() -> None:
    record, _, _ = run((AAPL,))

    spread = record.signals[0].spread
    assert spread is not None
    assert spread.back.contract.expiry == date(2026, 12, 18)


def test_a_holiday_friday_moves_the_monthly_to_thursday() -> None:
    # Without Dec 18 as a session, December's monthly would be Thursday Dec 17, which the
    # fixture chain does not list: no spread, explained.
    scanner, _, _ = scanner_for((AAPL,), sessions_without=date(2026, 12, 18))

    record = scanner.run(scanner.find_reports(), ScanKind.SCHEDULED)

    failed = {r.name: r.detail for r in record.signals[0].failed()}
    assert "2026-12-17" in failed["spread_available"]


# Screening before the scan


def scanner_with(market: FakeMarketData) -> Scanner:
    scanner, _, _ = scanner_for((AAPL, NOPE))
    return replace(scanner, market_data=market)


def test_a_screened_out_ticker_is_avoid_without_fetching_its_chain() -> None:
    market = FakeMarketData(
        {"AAPL": SNAPSHOT}, {"AAPL": HISTORY}, skips={"NOPE": "no option contracts available"}
    )
    scanner = scanner_with(market)

    record = scanner.run(scanner.find_reports(), ScanKind.SCHEDULED)

    nope = record.signals[1]
    assert nope.verdict is Verdict.AVOID
    assert [(r.name, r.detail) for r in nope.failed()] == [
        (MARKET_DATA, "NOPE: no option contracts available")
    ]
    assert market.asked == ["AAPL"]
    assert record.signals[0].verdict is Verdict.RECOMMENDED


def test_a_screening_failure_falls_back_to_scanning_every_ticker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    market = FakeMarketData({"AAPL": SNAPSHOT}, {"AAPL": HISTORY}, screen_fails=True)
    scanner = scanner_with(market)

    record = scanner.run(scanner.find_reports(), ScanKind.SCHEDULED)

    assert market.asked == ["AAPL", "NOPE"]
    assert record.signals[0].verdict is Verdict.RECOMMENDED
    assert "contract screening is down" in caplog.text
