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


def test_table_shows_each_rule_and_its_tickers() -> None:
    rows = [row("BIG", front=(0.30, 1_000), volume=10_000_000), row("ODD", iv_rv=3.5)]

    table = COVERAGE.format_table(COVERAGE.coverage(rows), len(rows))

    assert "5%" in table and "50%" in table
    assert "front" in table and "both" in table
    assert "BIG" in table and "ODD" in table


def test_latest_scan_ignores_ticker_runs(tmp_path: Path) -> None:
    day = tmp_path / "signals" / "date=2026-11-09"
    day.mkdir(parents=True)
    (day / "scan_100000.jsonl").write_text("", encoding="utf-8")
    (day / "tickers_110000.jsonl").write_text("", encoding="utf-8")

    assert COVERAGE.latest_scan(tmp_path).name == "scan_100000.jsonl"
