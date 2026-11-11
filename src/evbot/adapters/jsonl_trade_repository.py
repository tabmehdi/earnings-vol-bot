import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.options import OptionContract, OptionRight
from evbot.domain.scans import ScanKind, ScanRecord
from evbot.domain.signals import FilterResult, Signal, SignalMetrics, Verdict
from evbot.domain.spreads import CalendarSpread, Leg, Side
from evbot.ports.errors import RepositoryError

NEW_YORK = ZoneInfo("America/New_York")
LIQUIDITY = "liquidity"


class JsonlTradeRepository:
    """One file per scan: a scan line, then one line per signal.

    Signal lines also carry flat copies of the fields tools read most (verdict, metrics,
    per-leg liquidity) so scripts do not need to rebuild domain objects.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def save_scan(self, scan: ScanRecord) -> None:
        started = scan.started_at.astimezone(NEW_YORK)
        path = self._root / f"date={started:%Y-%m-%d}" / f"{scan.kind.value}_{started:%H%M%S}.jsonl"
        lines = [self._scan_line(scan), *(self._signal_line(s) for s in scan.signals)]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
        except OSError as error:
            raise RepositoryError(f"cannot write {path}: {error!r}") from error

    def scans_on(self, day: date) -> tuple[ScanRecord, ...]:
        folder = self._root / f"date={day.isoformat()}"
        try:
            scans = [self._read(path) for path in sorted(folder.glob("*.jsonl"))]
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise RepositoryError(f"cannot read scans for {day}: {error!r}") from error
        return tuple(sorted(scans, key=lambda scan: scan.started_at))

    def _read(self, path: Path) -> ScanRecord:
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        header, signal_lines = lines[0], lines[1:]
        deadline = header["entry_deadline"]
        return ScanRecord(
            ScanKind(header["kind"]),
            datetime.fromisoformat(header["started_at"]),
            datetime.fromisoformat(header["finished_at"]),
            tuple(self._signal(line) for line in signal_lines),
            datetime.fromisoformat(deadline) if deadline else None,
        )

    @staticmethod
    def _scan_line(scan: ScanRecord) -> dict[str, Any]:
        return {
            "type": "scan",
            "kind": scan.kind.value,
            "started_at": scan.started_at.isoformat(),
            "finished_at": scan.finished_at.isoformat(),
            "entry_deadline": scan.entry_deadline.isoformat() if scan.entry_deadline else None,
        }

    @classmethod
    def _signal_line(cls, signal: Signal) -> dict[str, Any]:
        metrics = signal.metrics
        legs = _leg_liquidity(signal)
        return {
            "type": "signal",
            "symbol": signal.event.symbol,
            "report_date": signal.event.report_date.isoformat(),
            "timing": signal.event.timing.value,
            "as_of": signal.as_of.isoformat(),
            "verdict": signal.verdict.value,
            "avg_volume_30d": metrics.avg_volume_30d if metrics else None,
            "iv30_rv30": metrics.iv30_rv30 if metrics else None,
            "term_slope": metrics.term_slope if metrics else None,
            "front_spread_pct": legs.get("front_spread_pct"),
            "front_open_interest": legs.get("front_open_interest"),
            "back_spread_pct": legs.get("back_spread_pct"),
            "back_open_interest": legs.get("back_open_interest"),
            "failed": [result.name for result in signal.failed()],
            "filters": [
                {
                    "name": r.name,
                    "passed": r.passed,
                    "value": r.value,
                    "threshold": r.threshold,
                    "detail": r.detail,
                }
                for r in signal.filters
            ],
            "spread": cls._spread_fields(signal.spread),
        }

    @staticmethod
    def _spread_fields(spread: CalendarSpread | None) -> dict[str, Any] | None:
        if spread is None:
            return None
        return {
            leg_name: {
                "underlying": leg.contract.underlying,
                "expiry": leg.contract.expiry.isoformat(),
                "strike": leg.contract.strike,
                "right": leg.contract.right.value,
                "multiplier": leg.contract.multiplier,
                "side": leg.side.value,
            }
            for leg_name, leg in (("front", spread.front), ("back", spread.back))
        }

    @staticmethod
    def _signal(line: dict[str, Any]) -> Signal:
        event = EarningsEvent(
            line["symbol"], date.fromisoformat(line["report_date"]), ReportTiming(line["timing"])
        )
        has_metrics = line["avg_volume_30d"] is not None
        metrics = (
            SignalMetrics(line["avg_volume_30d"], line["iv30_rv30"], line["term_slope"])
            if has_metrics
            else None
        )
        filters = tuple(
            FilterResult(f["name"], f["passed"], f["value"], f["threshold"], f["detail"])
            for f in line["filters"]
        )
        spread = None
        if line["spread"] is not None:
            front, back = (_leg(line["spread"][name]) for name in ("front", "back"))
            spread = CalendarSpread(front, back)
        return Signal(
            event,
            datetime.fromisoformat(line["as_of"]),
            metrics,
            filters,
            Verdict(line["verdict"]),
            spread,
        )


def _leg(fields: dict[str, Any]) -> Leg:
    contract = OptionContract(
        fields["underlying"],
        date.fromisoformat(fields["expiry"]),
        fields["strike"],
        OptionRight(fields["right"]),
        fields["multiplier"],
    )
    return Leg(contract, Side(fields["side"]))


def _leg_liquidity(signal: Signal) -> dict[str, float | int]:
    detail = next((r.detail for r in signal.filters if r.name == LIQUIDITY), "")
    pairs = (item.split("=") for item in detail.split() if "=" in item)
    return {key: int(value) if key.endswith("interest") else float(value) for key, value in pairs}
