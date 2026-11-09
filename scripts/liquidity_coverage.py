"""Shows what candidate liquidity rules would keep and remove, using saved preview scans.

Usage: uv run python scripts/liquidity_coverage.py [--scan PATH] [--snapshot-dir DIR]
"""

import argparse
import json
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any

MAX_SPREADS = (0.05, 0.10, 0.20, 0.30, 0.50)
MIN_OPEN_INTERESTS = (0, 100, 500)
HIGH_VOLUME = 10_000_000
HIGH_IV_RV = 3.0


@dataclass(frozen=True)
class Rule:
    max_spread: float
    min_oi: int
    both_legs: bool

    def passes(self, row: dict[str, Any]) -> bool:
        legs = ("front", "back") if self.both_legs else ("front",)
        return all(
            row[f"{leg}_spread_pct"] <= self.max_spread
            and row[f"{leg}_open_interest"] >= self.min_oi
            for leg in legs
        )


@dataclass(frozen=True)
class RuleResult:
    rule: Rule
    passed: int
    removed_high_volume: list[str]
    kept_high_iv_rv: list[str]


def latest_scan(snapshot_dir: Path) -> Path:
    """The newest full scan; --tickers runs are saved under a different prefix."""
    scans = sorted((snapshot_dir / "signals").glob("date=*/scan_*.jsonl"))
    if not scans:
        raise SystemExit(f"no saved scans under {snapshot_dir}; run the preview command first")
    return scans[-1]


def read_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [
        row
        for row in read_rows(path)
        if row.get("front_spread_pct") is not None and row.get("back_spread_pct") is not None
    ]


def format_legs(rows: list[dict[str, Any]]) -> str:
    header = f"{'SYMBOL':<8}{'FRONT SPREAD':>13}{'FRONT OI':>10}{'BACK SPREAD':>13}{'BACK OI':>10}"
    return "\n".join([header, *map(_leg_line, rows)])


def _leg_line(row: dict[str, Any]) -> str:
    spread = [row.get(f"{leg}_spread_pct") for leg in ("front", "back")]
    interest = [row.get(f"{leg}_open_interest") for leg in ("front", "back")]
    front_spread, back_spread = ("-" if s is None else f"{s:.1%}" for s in spread)
    front_oi, back_oi = ("-" if oi is None else f"{oi:,}" for oi in interest)
    return f"{row['symbol']:<8}{front_spread:>13}{front_oi:>10}{back_spread:>13}{back_oi:>10}"


def coverage(rows: list[dict[str, Any]]) -> list[RuleResult]:
    results = []
    for max_spread, min_oi, both_legs in product(MAX_SPREADS, MIN_OPEN_INTERESTS, (False, True)):
        rule = Rule(max_spread, min_oi, both_legs)
        kept = [row for row in rows if rule.passes(row)]
        removed = [row for row in rows if not rule.passes(row)]
        results.append(
            RuleResult(
                rule,
                len(kept),
                [r["symbol"] for r in removed if (r["avg_volume_30d"] or 0) >= HIGH_VOLUME],
                [r["symbol"] for r in kept if (r["iv30_rv30"] or 0) > HIGH_IV_RV],
            )
        )
    return results


def format_table(results: list[RuleResult], total: int) -> str:
    header = (
        f"{'SPREAD':>6} {'MIN OI':>6} {'LEGS':<5} {'PASS':>9}  "
        f"REMOVED (AVG VOL >= 10M) | KEPT (IV/RV > 3)"
    )
    lines = [header]
    for result in results:
        rule = result.rule
        legs = "both" if rule.both_legs else "front"
        removed = ", ".join(result.removed_high_volume) or "-"
        kept = ", ".join(result.kept_high_iv_rv) or "-"
        passed = f"{result.passed}/{total}"
        lines.append(
            f"{rule.max_spread:>6.0%} {rule.min_oi:>6} {legs:<5} {passed:>9}  {removed} | {kept}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", type=Path, help="a scan file; defaults to the latest full scan")
    parser.add_argument("--snapshot-dir", type=Path, default=Path("data/snapshots"))
    args = parser.parse_args()

    path = args.scan or latest_scan(args.snapshot_dir)
    rows = load_rows(path)
    print(f"Scan: {path}")
    print(format_legs(read_rows(path)))
    print()
    print(f"Tickers with a buildable spread: {len(rows)}")
    print(format_table(coverage(rows), len(rows)))


if __name__ == "__main__":
    main()
