import json
from datetime import date
from pathlib import Path

import pytest

from evbot import main_live
from evbot.app.scan_preview import PreviewLine, PreviewReport, ScanUniverse
from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.signals import FilterResult, Signal, SignalMetrics, Verdict
from tests.fixtures.market import AS_OF, FRONT

EVENT = EarningsEvent("AAPL", FRONT, ReportTiming.BMO)
TODAY, NEXT = date(2026, 11, 19), date(2026, 11, 20)


def test_format_shows_metrics_verdict_stale_count_and_failed_filters() -> None:
    metrics = SignalMetrics(avg_volume_30d=2_000_000, iv30_rv30=1.5, term_slope=-0.006)
    failed = FilterResult("avg_volume", False, 1.0, 2.0)
    signal = Signal(EVENT, AS_OF, metrics, (failed,), Verdict.AVOID, spread=None)
    line = PreviewLine(EVENT, signal, stale_quotes_dropped=4, error="")

    text = main_live.format_report(PreviewReport((line,)))

    row = next(row for row in text.splitlines() if row.startswith("AAPL"))
    for expected in ("bmo", "avoid", "2,000,000", "1.50", "-0.00600", "4", "avg_volume"):
        assert expected in row


def test_format_shows_why_metrics_were_unavailable() -> None:
    reason = FilterResult("metrics", False, None, None, detail="need 31 bars, have 5")
    signal = Signal(EVENT, AS_OF, None, (reason,), Verdict.AVOID, spread=None)
    line = PreviewLine(EVENT, signal, stale_quotes_dropped=0, error="")

    assert "need 31 bars" in main_live.format_report(PreviewReport((line,)))


def test_format_shows_a_ticker_error() -> None:
    line = PreviewLine(EVENT, None, None, error="AAPL: HTTP 403")

    assert "AAPL: HTTP 403" in main_live.format_report(PreviewReport((line,)))


def test_format_says_when_nothing_was_selected() -> None:
    assert "No earnings" in main_live.format_report(PreviewReport(()))


def test_universe_counts_reports_per_day_and_timing() -> None:
    universe = ScanUniverse(
        today=TODAY,
        next_session=NEXT,
        today_events=(
            EarningsEvent("A", TODAY, ReportTiming.AMC),
            EarningsEvent("B", TODAY, ReportTiming.AMC),
            EarningsEvent("C", TODAY, ReportTiming.BMO),
        ),
        next_session_events=(
            EarningsEvent("D", NEXT, ReportTiming.BMO),
            EarningsEvent("E", NEXT, ReportTiming.UNKNOWN),
        ),
    )

    lines = main_live.format_universe(universe).splitlines()

    assert "2026-11-19" in lines[0] and "BMO 1, AMC 2, UNKNOWN 0" in lines[0]
    assert "2026-11-20" in lines[1] and "BMO 1, AMC 0, UNKNOWN 1" in lines[1]
    assert "Selected: 4" in lines[2]


def test_run_stats_show_requests_per_source_and_duration() -> None:
    text = main_live.format_run_stats(alpaca_requests=700, nasdaq_requests=2, seconds=78.25)

    assert "702" in text
    assert "alpaca 700" in text
    assert "nasdaq 2" in text
    assert "78.2" in text


def test_parse_tickers_cleans_the_list() -> None:
    assert main_live.parse_tickers(" aapl, MSFT,,nvda ") == ("AAPL", "MSFT", "NVDA")


def test_tickers_option_with_no_symbols_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(main_live, "load_dotenv", lambda: False)
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")

    with pytest.raises(SystemExit):
        main_live.main(["preview", "--tickers", " , "])

    assert "--tickers" in capsys.readouterr().err


def scanned_line() -> PreviewLine:
    metrics = SignalMetrics(avg_volume_30d=2_000_000, iv30_rv30=1.5, term_slope=-0.006)
    liquidity = FilterResult(
        "liquidity",
        True,
        None,
        None,
        detail=(
            "front_spread_pct=0.1380 front_open_interest=317 "
            "back_spread_pct=inf back_open_interest=3"
        ),
    )
    signal = Signal(EVENT, AS_OF, metrics, (liquidity,), Verdict.CONSIDER, spread=None)
    return PreviewLine(EVENT, signal, stale_quotes_dropped=1, error="")


def test_scan_log_records_verdict_metrics_and_both_legs() -> None:
    row = main_live.scan_log_row(scanned_line())

    assert row["symbol"] == "AAPL"
    assert row["timing"] == "bmo"
    assert row["verdict"] == "consider"
    assert row["avg_volume_30d"] == 2_000_000
    assert row["front_spread_pct"] == 0.138
    assert row["front_open_interest"] == 317
    assert row["back_spread_pct"] == float("inf")
    assert row["back_open_interest"] == 3


def test_scan_log_row_for_a_failed_ticker_has_no_legs() -> None:
    row = main_live.scan_log_row(PreviewLine(EVENT, None, None, error="AAPL: HTTP 403"))

    assert row["error"] == "AAPL: HTTP 403"
    assert row["verdict"] is None
    assert row["front_spread_pct"] is None


def test_write_scan_log_writes_one_json_line_per_ticker(tmp_path: Path) -> None:
    path = tmp_path / "signals" / "date=2026-11-09" / "scan_100000.jsonl"

    main_live.write_scan_log(PreviewReport((scanned_line(), scanned_line())), path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["symbol"] == "AAPL"


class StubPreview:
    def universe_for(self, tickers: tuple[str, ...]) -> ScanUniverse:
        return ScanUniverse(TODAY, NEXT, (), ())

    def find_reports(self) -> ScanUniverse:
        return ScanUniverse(TODAY, NEXT, (), ())

    def evaluate(self, universe: ScanUniverse, limit: int | None) -> PreviewReport:
        return PreviewReport(())


def run_preview_stubbed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *arguments: str
) -> list[str]:
    monkeypatch.setattr(main_live, "load_dotenv", lambda: False)
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    monkeypatch.setattr(main_live, "build_preview", lambda *_: StubPreview())
    monkeypatch.setattr(main_live, "write_scan_log", lambda *_: None)
    main_live.main(["preview", *arguments])
    return capsys.readouterr().out.splitlines()


def test_tickers_run_starts_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = run_preview_stubbed(monkeypatch, capsys, "--tickers", "AAPL,MSFT")

    assert lines[0].startswith("WARNING")
    assert "as if they reported next session" in lines[0]


def test_a_normal_scan_has_no_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = run_preview_stubbed(monkeypatch, capsys)

    assert not any(line.startswith("WARNING") for line in lines)


def test_preview_refuses_to_start_without_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(main_live, "load_dotenv", lambda: False)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    with pytest.raises(SystemExit):
        main_live.main(["preview"])

    assert "ALPACA_API_KEY" in capsys.readouterr().err


def test_a_subcommand_is_required() -> None:
    with pytest.raises(SystemExit):
        main_live.main([])
