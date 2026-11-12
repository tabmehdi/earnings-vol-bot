from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from evbot import main_live
from evbot.app.scanner import ScanUniverse
from evbot.config import StrategyConfig, load_config
from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.options import OptionContract, OptionRight
from evbot.domain.scans import ScanKind, ScanRecord
from evbot.domain.signals import FilterResult, Signal, SignalMetrics, Verdict
from evbot.domain.spreads import CalendarSpread, Leg, Side
from evbot.ports.errors import BrokerError
from evbot.scan_lock import ScanLock
from tests.fixtures.market import AS_OF, FRONT

NEW_YORK = ZoneInfo("America/New_York")
EVENT = EarningsEvent("AAPL", FRONT, ReportTiming.BMO)
TODAY, NEXT = date(2026, 11, 19), date(2026, 11, 20)
STARTED = datetime(2026, 11, 19, 15, 25, tzinfo=NEW_YORK)
SPREAD = CalendarSpread(
    Leg(OptionContract("AAPL", date(2026, 11, 20), 100.0, OptionRight.CALL), Side.SELL),
    Leg(OptionContract("AAPL", date(2026, 12, 18), 100.0, OptionRight.CALL), Side.BUY),
)


def signal(
    verdict: Verdict = Verdict.CONSIDER, *filters: FilterResult, metrics: bool = True
) -> Signal:
    values = SignalMetrics(avg_volume_30d=2_000_000, iv30_rv30=1.5, term_slope=-0.006)
    spread = SPREAD if verdict is Verdict.RECOMMENDED else None
    return Signal(EVENT, AS_OF, values if metrics else None, filters, verdict, spread)


def record(*signals: Signal, kind: ScanKind = ScanKind.SCHEDULED) -> ScanRecord:
    return ScanRecord(
        kind,
        STARTED,
        STARTED + timedelta(minutes=4),
        signals,
        STARTED + timedelta(minutes=20),
        risk_free_rate=0.0384,
        rate_source="FRED DGS3MO",
    )


FRESHNESS = FilterResult("quote_freshness", True, None, None, "stale_quotes_dropped=4")
DATA_FAILURE = FilterResult("market_data", False, None, None, "AAPL: HTTP 403")


# Output formats


def test_report_shows_metrics_verdict_stale_count_and_failed_filters() -> None:
    failed = FilterResult("avg_volume", False, 1.0, 2.0)

    text = main_live.format_report(record(signal(Verdict.AVOID, failed, FRESHNESS)))

    row = next(row for row in text.splitlines() if row.startswith("AAPL"))
    for expected in ("bmo", "avoid", "2,000,000", "1.50", "-0.00600", "4", "avg_volume"):
        assert expected in row


def test_report_shows_why_metrics_were_unavailable() -> None:
    reason = FilterResult("metrics", False, None, None, detail="need 31 bars, have 5")

    text = main_live.format_report(record(signal(Verdict.AVOID, reason, metrics=False)))

    assert "need 31 bars" in text


def test_report_shows_a_data_failure() -> None:
    text = main_live.format_report(record(signal(Verdict.AVOID, DATA_FAILURE, metrics=False)))

    assert "AAPL: HTTP 403" in text


def test_report_says_when_nothing_was_selected() -> None:
    assert "No earnings" in main_live.format_report(record())


def test_universe_counts_reports_per_day_and_timing() -> None:
    universe = ScanUniverse(
        TODAY,
        NEXT,
        (
            EarningsEvent("A", TODAY, ReportTiming.AMC),
            EarningsEvent("B", TODAY, ReportTiming.AMC),
            EarningsEvent("C", TODAY, ReportTiming.BMO),
        ),
        (
            EarningsEvent("D", NEXT, ReportTiming.BMO),
            EarningsEvent("E", NEXT, ReportTiming.UNKNOWN),
        ),
    )

    lines = main_live.format_universe(universe).splitlines()

    assert "2026-11-19" in lines[0] and "BMO 1, AMC 2, UNKNOWN 0" in lines[0]
    assert "2026-11-20" in lines[1] and "BMO 1, AMC 0, UNKNOWN 1" in lines[1]
    assert "Selected: 4" in lines[2]


def test_summary_counts_verdicts_requests_and_timing() -> None:
    scan = record(
        signal(Verdict.RECOMMENDED),
        signal(Verdict.CONSIDER),
        signal(Verdict.AVOID, DATA_FAILURE, metrics=False),
    )

    text = main_live.format_summary(scan, alpaca_requests=700, nasdaq_requests=2)

    assert "\n" not in text
    for expected in (
        "scheduled",
        "3 signals",
        "1 recommended",
        "1 consider",
        "1 avoid",
        "1 data failures",
        "702 requests",
        "240.0s",
        "before entry: yes",
        "rate 3.84% (FRED DGS3MO)",
    ):
        assert expected in text


def test_parse_tickers_cleans_the_list() -> None:
    assert main_live.parse_tickers(" aapl, MSFT,,nvda ") == ("AAPL", "MSFT", "NVDA")


# Commands, with the scanner stubbed out


class StubScanner:
    def __init__(self, skip: str | None = None, fail: bool = False) -> None:
        self.skip = skip
        self.fail = fail
        self.kinds: list[ScanKind] = []

    def skip_reason(self) -> str | None:
        return self.skip

    def find_reports(self) -> ScanUniverse:
        return ScanUniverse(TODAY, NEXT, (), ())

    def universe_for(self, tickers: tuple[str, ...]) -> ScanUniverse:
        return ScanUniverse(TODAY, NEXT, (), ())

    def run(self, universe: ScanUniverse, kind: ScanKind, limit: int | None = None) -> ScanRecord:
        if self.fail:
            raise RuntimeError("boom")
        self.kinds.append(kind)
        return record(signal(Verdict.RECOMMENDED), kind=kind)


def config_in(tmp_path: Path) -> StrategyConfig:
    config = load_config()
    storage = replace(
        config.storage,
        snapshot_dir=str(tmp_path / "snapshots"),
        signals_dir=str(tmp_path / "signals"),
        logs_dir=str(tmp_path / "logs"),
        locks_dir=str(tmp_path / "locks"),
        trades_dir=str(tmp_path / "trades"),
    )
    return replace(config, storage=storage)


def run_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scanner: StubScanner,
    *arguments: str,
) -> None:
    monkeypatch.setattr(main_live, "load_dotenv", lambda: False)
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    monkeypatch.setattr(main_live, "load_config", lambda: config_in(tmp_path))
    monkeypatch.setattr(main_live, "build_scanner", lambda *_: scanner)
    main_live.main(list(arguments))


def scan_log(tmp_path: Path) -> str:
    logs = list((tmp_path / "logs").glob("scan_*.log"))
    assert len(logs) == 1
    return logs[0].read_text(encoding="utf-8")


def test_scheduled_scan_logs_start_counts_and_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scanner = StubScanner()

    run_main(monkeypatch, tmp_path, scanner, "scan")

    assert scanner.kinds == [ScanKind.SCHEDULED]
    log = scan_log(tmp_path)
    assert "scan started" in log
    assert "Selected: 0" in log
    assert "1 recommended" in log


def test_scheduled_scan_that_should_not_run_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scanner = StubScanner(skip="a scheduled scan is already recorded for 2026-11-19")

    run_main(monkeypatch, tmp_path, scanner, "scan")

    assert scanner.kinds == []
    assert "already recorded" in scan_log(tmp_path)


def test_a_second_concurrent_scan_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scanner = StubScanner()
    held = ScanLock(tmp_path / "locks" / f"scan_{main_live.MarketClock().now():%Y-%m-%d}.lock")
    held.acquire()

    run_main(monkeypatch, tmp_path, scanner, "scan")

    assert scanner.kinds == []
    assert "another scan is running" in scan_log(tmp_path)


def test_a_crash_is_logged_with_its_traceback_and_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run_main(monkeypatch, tmp_path, StubScanner(fail=True), "scan")

    assert exit_info.value.code == 1
    log = scan_log(tmp_path)
    assert "Traceback" in log
    assert "RuntimeError: boom" in log
    assert not list((tmp_path / "locks").glob("*.lock"))


def test_preview_is_recorded_as_a_preview(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scanner = StubScanner()

    run_main(monkeypatch, tmp_path, scanner, "preview")

    assert scanner.kinds == [ScanKind.PREVIEW]
    assert not any(line.startswith("WARNING") for line in capsys.readouterr().out.splitlines())


def test_tickers_run_warns_first_and_is_recorded_as_tickers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scanner = StubScanner()

    run_main(monkeypatch, tmp_path, scanner, "preview", "--tickers", "AAPL,MSFT")

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("WARNING")
    assert "as if they reported next session" in lines[0]
    assert scanner.kinds == [ScanKind.TICKERS]


def test_tickers_option_with_no_symbols_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        run_main(monkeypatch, tmp_path, StubScanner(), "preview", "--tickers", " , ")

    assert "--tickers" in capsys.readouterr().err


def test_refuses_to_start_without_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(main_live, "load_dotenv", lambda: False)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    with pytest.raises(SystemExit):
        main_live.main(["scan"])

    assert "ALPACA_API_KEY" in capsys.readouterr().err


def test_a_subcommand_is_required() -> None:
    with pytest.raises(SystemExit):
        main_live.main([])


# The trading session


class StubBot:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.runs = 0

    def run_session(self) -> None:
        self.runs += 1
        if self.fail:
            raise RuntimeError("session boom")


def run_session_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bot: StubBot | Exception
) -> None:
    def build(*_: object) -> StubBot:
        if isinstance(bot, Exception):
            raise bot
        return bot

    monkeypatch.setattr(main_live, "build_trading_bot", build)
    run_main(monkeypatch, tmp_path, StubScanner(), "session")


def session_log(tmp_path: Path) -> str:
    (log,) = (tmp_path / "logs").glob("session_*.log")
    return log.read_text(encoding="utf-8")


def test_session_runs_the_bot_and_logs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bot = StubBot()

    run_session_command(monkeypatch, tmp_path, bot)

    assert bot.runs == 1
    assert "session started" in session_log(tmp_path)
    assert not list((tmp_path / "locks").glob("*.lock"))


def test_a_crashed_session_exits_1_so_systemd_restarts_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run_session_command(monkeypatch, tmp_path, StubBot(fail=True))

    assert exit_info.value.code == 1
    assert "session boom" in session_log(tmp_path)


def test_refusing_live_trading_exits_2_so_systemd_does_not_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run_session_command(monkeypatch, tmp_path, BrokerError("only trades the paper account"))

    assert exit_info.value.code == 2
    assert "paper" in session_log(tmp_path)


def test_a_second_session_the_same_day_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    held = ScanLock(tmp_path / "locks" / f"session_{main_live.MarketClock().now():%Y-%m-%d}.lock")
    held.acquire()
    bot = StubBot()

    run_session_command(monkeypatch, tmp_path, bot)

    assert bot.runs == 0
    assert "another session is running" in session_log(tmp_path)
