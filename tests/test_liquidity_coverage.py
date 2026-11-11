import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

SCRIPT = Path(__file__).parents[1] / "scripts" / "liquidity_coverage.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("liquidity_coverage", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COVERAGE = load_script()


def row(
    symbol: str,
    front: tuple[float, int] = (0.04, 1_000),
    back: tuple[float, int] = (0.04, 1_000),
    volume: float | None = 2_000_000,
    iv_rv: float | None = 1.5,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "avg_volume_30d": volume,
        "iv30_rv30": iv_rv,
        "front_spread_pct": front[0],
        "front_open_interest": front[1],
        "back_spread_pct": back[0],
        "back_open_interest": back[1],
    }


def result_for(rows: list[dict[str, Any]], spread: float, oi: int, both: bool) -> Any:
    results = COVERAGE.coverage(rows)
    return next(
        r
        for r in results
        if r.rule.max_spread == spread and r.rule.min_oi == oi and r.rule.both_legs is both
    )


def test_grid_has_every_combination() -> None:
    # 5 spread limits * 3 open-interest minimums * 2 leg choices
    assert len(COVERAGE.coverage([row("A")])) == 30


def test_front_only_ignores_the_back_leg() -> None:
    rows = [row("A", back=(0.90, 0))]

    assert result_for(rows, 0.10, 100, both=False).passed == 1
    assert result_for(rows, 0.10, 100, both=True).passed == 0


def test_limits_are_inclusive() -> None:
    assert result_for([row("A", front=(0.10, 500))], 0.10, 500, both=False).passed == 1


def test_infinite_spread_never_passes() -> None:
    rows = [row("A", front=(float("inf"), 1_000))]

    assert result_for(rows, 0.50, 0, both=False).passed == 0


def test_lists_high_volume_tickers_removed() -> None:
    rows = [row("BIG", front=(0.30, 1_000), volume=10_000_000), row("OK", volume=20_000_000)]

    assert result_for(rows, 0.10, 0, both=False).removed_high_volume == ["BIG"]


def test_lists_high_iv_rv_tickers_kept() -> None:
    rows = [row("ODD", iv_rv=3.1), row("EDGE", iv_rv=3.0), row("GONE", front=(0.9, 0), iv_rv=9.0)]

    assert result_for(rows, 0.10, 0, both=False).kept_high_iv_rv == ["ODD"]


def test_missing_metrics_are_never_listed() -> None:
    rows = [row("A", front=(0.9, 0), volume=None, iv_rv=None)]

    result = result_for(rows, 0.10, 0, both=False)

    assert result.removed_high_volume == []
    assert result.kept_high_iv_rv == []


def test_load_keeps_only_rows_with_both_legs(tmp_path: Path) -> None:
    path = tmp_path / "scan_1.jsonl"
    no_legs = {"symbol": "B", "front_spread_pct": None, "back_spread_pct": None}
    path.write_text("\n".join(json.dumps(r) for r in (row("A"), no_legs)), encoding="utf-8")

    assert [r["symbol"] for r in COVERAGE.load_rows(path)] == ["A"]


def test_read_rows_keeps_every_ticker_in_the_scan(tmp_path: Path) -> None:
    path = tmp_path / "tickers_1.jsonl"
    no_legs = {"symbol": "B", "front_spread_pct": None, "back_spread_pct": None}
    path.write_text("\n".join(json.dumps(r) for r in (row("A"), no_legs)), encoding="utf-8")

    assert [r["symbol"] for r in COVERAGE.read_rows(path)] == ["A", "B"]


def test_leg_table_shows_both_legs_of_every_ticker() -> None:
    rows = [row("AAPL", front=(0.031, 4_210), back=(0.125, 88))]

    aapl = next(line for line in COVERAGE.format_legs(rows).splitlines() if "AAPL" in line)

    for expected in ("3.1%", "4,210", "12.5%", "88"):
        assert expected in aapl


def test_leg_table_marks_a_ticker_without_legs() -> None:
    no_legs = {
        "symbol": "NOPE",
        "front_spread_pct": None,
        "front_open_interest": None,
        "back_spread_pct": None,
        "back_open_interest": None,
    }

    line = next(ln for ln in COVERAGE.format_legs([no_legs]).splitlines() if "NOPE" in ln)

    assert line.split()[1:] == ["-", "-", "-", "-"]


def test_table_shows_each_rule_and_its_tickers() -> None:
    rows = [row("BIG", front=(0.30, 1_000), volume=10_000_000), row("ODD", iv_rv=3.5)]

    table = COVERAGE.format_table(COVERAGE.coverage(rows), len(rows))

    assert "5%" in table and "50%" in table
    assert "front" in table and "both" in table
    assert "BIG" in table and "ODD" in table


def touch(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_latest_scan_prefers_the_newest_scheduled_scan(tmp_path: Path) -> None:
    signals, legacy = tmp_path / "signals", tmp_path / "snapshots"
    touch(legacy / "signals" / "date=2026-11-09" / "scan_153658.jsonl")
    newest = touch(signals / "date=2026-11-11" / "scheduled_152500.jsonl")
    touch(signals / "date=2026-11-11" / "preview_160000.jsonl")
    touch(signals / "date=2026-11-11" / "tickers_170000.jsonl")

    assert COVERAGE.latest_scan(signals, legacy) == newest


def test_latest_scan_falls_back_to_the_old_scan_files(tmp_path: Path) -> None:
    signals, legacy = tmp_path / "signals", tmp_path / "snapshots"
    old = touch(legacy / "signals" / "date=2026-11-09" / "scan_153658.jsonl")
    touch(legacy / "signals" / "date=2026-11-09" / "tickers_154817.jsonl")

    assert COVERAGE.latest_scan(signals, legacy) == old


def test_reads_the_new_scan_files_and_skips_the_scan_line(tmp_path: Path) -> None:
    scan_line = {"type": "scan", "kind": "scheduled"}
    signal_line = {"type": "signal", **row("AAPL")}
    path = touch(
        tmp_path / "scheduled_152500.jsonl",
        "\n".join(json.dumps(line) for line in (scan_line, signal_line)),
    )

    assert [r["symbol"] for r in COVERAGE.load_rows(path)] == ["AAPL"]
