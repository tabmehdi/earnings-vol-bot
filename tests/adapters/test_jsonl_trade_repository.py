import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from evbot.adapters.jsonl_trade_repository import JsonlTradeRepository
from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.scans import ScanKind, ScanRecord
from evbot.domain.signals import FilterResult, Signal, SignalMetrics, Verdict
from evbot.strategy.evaluator import MarketContext
from tests.fixtures.market import AS_OF, BACKWARDATED, EVENT, build_chain
from tests.strategy.test_evaluator import CALM, LIQUID_VOLUME, build_history, evaluator

# 20:25 UTC on Nov 9 is 15:25 in New York; 02:00 UTC on Nov 10 is still Nov 9 in New York.
STARTED = datetime(2026, 11, 9, 20, 25, tzinfo=UTC)
NY_DAY = date(2026, 11, 9)


def recommended_signal() -> Signal:
    context = MarketContext(EVENT, build_chain(BACKWARDATED), build_history(CALM, LIQUID_VOLUME))
    return evaluator().evaluate(context)


def failed_signal() -> Signal:
    event = EarningsEvent("BAK", date(2026, 11, 10), ReportTiming.UNKNOWN)
    reason = FilterResult("market_data", False, None, None, detail="no option contracts available")
    return Signal(event, AS_OF, None, (reason,), Verdict.AVOID, spread=None)


def record(kind: ScanKind = ScanKind.SCHEDULED, started: datetime = STARTED) -> ScanRecord:
    return ScanRecord(
        kind,
        started,
        started + timedelta(minutes=4),
        (recommended_signal(), failed_signal()),
        entry_deadline=started + timedelta(minutes=20),
    )


def test_round_trip_is_exact(tmp_path: Path) -> None:
    repository = JsonlTradeRepository(tmp_path)

    repository.save_scan(record())

    assert repository.scans_on(NY_DAY) == (record(),)


def test_signal_with_infinite_spread_and_no_metrics_round_trips(tmp_path: Path) -> None:
    metrics = SignalMetrics(2_000_000, 1.5, -0.01)
    wide = FilterResult("liquidity", True, None, None, detail="front_spread_pct=inf")
    signal = Signal(EVENT, AS_OF, metrics, (wide,), Verdict.AVOID, spread=None)
    scan = ScanRecord(ScanKind.PREVIEW, STARTED, STARTED, (signal,))
    repository = JsonlTradeRepository(tmp_path)

    repository.save_scan(scan)

    assert repository.scans_on(NY_DAY) == (scan,)


def test_files_are_grouped_by_new_york_day_and_named_by_kind(tmp_path: Path) -> None:
    late_evening = datetime(2026, 11, 10, 2, 0, tzinfo=UTC)
    repository = JsonlTradeRepository(tmp_path)

    repository.save_scan(record(ScanKind.TICKERS, late_evening))

    assert [p.name for p in (tmp_path / "date=2026-11-09").iterdir()] == ["tickers_210000.jsonl"]


def test_scans_on_returns_every_scan_of_the_day_in_time_order(tmp_path: Path) -> None:
    repository = JsonlTradeRepository(tmp_path)
    afternoon = record(ScanKind.SCHEDULED)
    morning = record(ScanKind.PREVIEW, STARTED - timedelta(hours=5))

    repository.save_scan(afternoon)
    repository.save_scan(morning)

    assert [scan.kind for scan in repository.scans_on(NY_DAY)] == [
        ScanKind.PREVIEW,
        ScanKind.SCHEDULED,
    ]


def test_a_day_without_scans_is_empty(tmp_path: Path) -> None:
    assert JsonlTradeRepository(tmp_path).scans_on(NY_DAY) == ()


def test_signal_lines_carry_flat_fields_for_tools(tmp_path: Path) -> None:
    repository = JsonlTradeRepository(tmp_path)

    repository.save_scan(record())

    path = next((tmp_path / "date=2026-11-09").iterdir())
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["type"] == "scan"
    signal_line = lines[1]
    assert signal_line["type"] == "signal"
    assert signal_line["symbol"] == "AAPL"
    assert signal_line["verdict"] == "recommended"
    assert signal_line["front_open_interest"] == 1000


def test_rate_and_its_source_round_trip(tmp_path: Path) -> None:
    scan = ScanRecord(
        ScanKind.SCHEDULED, STARTED, STARTED, (), risk_free_rate=0.0384, rate_source="FRED DGS3MO"
    )
    repository = JsonlTradeRepository(tmp_path)

    repository.save_scan(scan)

    assert repository.scans_on(NY_DAY) == (scan,)


def test_files_written_before_the_rate_was_recorded_still_read(tmp_path: Path) -> None:
    folder = tmp_path / "date=2026-11-09"
    folder.mkdir()
    header = {
        "type": "scan",
        "kind": "scheduled",
        "started_at": STARTED.isoformat(),
        "finished_at": STARTED.isoformat(),
        "entry_deadline": None,
    }
    (folder / "scheduled_152500.jsonl").write_text(json.dumps(header) + "\n", encoding="utf-8")

    scan = JsonlTradeRepository(tmp_path).scans_on(NY_DAY)[0]

    assert (scan.risk_free_rate, scan.rate_source) == (None, None)
