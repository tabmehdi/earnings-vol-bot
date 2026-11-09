from datetime import date

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
