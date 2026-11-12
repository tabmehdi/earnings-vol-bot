import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.options import OptionContract, OptionRight
from evbot.domain.orders import OrderIntent, OrderStatus, OrderTicket
from evbot.domain.scans import ScanKind, ScanRecord
from evbot.domain.signals import (
    FilterResult,
    LegPrices,
    Signal,
    SignalMetrics,
    SignalPrices,
    Verdict,
)
from evbot.domain.spreads import CalendarSpread, Leg, Side
from evbot.domain.trades import Fill, Position
from evbot.ports.errors import RepositoryError

NEW_YORK = ZoneInfo("America/New_York")
LIQUIDITY = "liquidity"


class JsonlTradeRepository:
    """One file per scan: a scan line, then one line per signal.

    Signal lines also carry flat copies of the fields tools read most (verdict, metrics,
    per-leg liquidity) so scripts do not need to rebuild domain objects.
    """

    def __init__(self, root: Path, trades_root: Path | None = None) -> None:
        self._root = root
        self._trades = trades_root if trades_root is not None else root / "trades"

    def save_order(self, ticket: OrderTicket) -> None:
        day = ticket.submitted_at.astimezone(NEW_YORK).date()
        path = self._trades / "orders" / f"date={day.isoformat()}.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as log:
                log.write(json.dumps(_order_fields(ticket)) + "\n")
        except OSError as error:
            raise RepositoryError(f"cannot write {path}: {error!r}") from error

    def orders_on(self, day: date) -> tuple[OrderTicket, ...]:
        path = self._trades / "orders" / f"date={day.isoformat()}.jsonl"
        if not path.exists():
            return ()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            return tuple(_order(json.loads(line)) for line in lines if line)
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise RepositoryError(f"cannot read orders for {day}: {error!r}") from error

    def save_position(self, position: Position) -> None:
        if not position.position_id:
            raise RepositoryError("a position needs an id to be stored")
        # ':' marks a split position's id but is not allowed in Windows file names.
        name = position.position_id.replace(":", "_")
        path = self._trades / "positions" / f"{name}.json"
        temporary = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(_position_fields(position)), encoding="utf-8")
            os.replace(temporary, path)  # atomic: a crash never leaves half a file
        except OSError as error:
            raise RepositoryError(f"cannot write {path}: {error!r}") from error

    def open_positions(self) -> tuple[Position, ...]:
        folder = self._trades / "positions"
        try:
            stored = [
                _position(json.loads(path.read_text(encoding="utf-8")))
                for path in sorted(folder.glob("*.json"))
            ]
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise RepositoryError(f"cannot read positions: {error!r}") from error
        return tuple(p for p in stored if p.is_open)

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
            risk_free_rate=header.get("risk_free_rate"),
            rate_source=header.get("rate_source"),
        )

    @staticmethod
    def _scan_line(scan: ScanRecord) -> dict[str, Any]:
        return {
            "type": "scan",
            "kind": scan.kind.value,
            "started_at": scan.started_at.isoformat(),
            "finished_at": scan.finished_at.isoformat(),
            "entry_deadline": scan.entry_deadline.isoformat() if scan.entry_deadline else None,
            "risk_free_rate": scan.risk_free_rate,
            "rate_source": scan.rate_source,
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
            "back_days": signal.spread.back_days if signal.spread else None,
            "debit_mid": signal.prices.debit_mid if signal.prices else None,
            "failed": [result.name for result in signal.failed()],
            "prices": _prices_fields(signal.prices),
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
            _prices(line.get("prices")),
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


def _time_text(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _time(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def _leg_fields(leg: LegPrices | None) -> dict[str, Any] | None:
    if leg is None:
        return None
    return {
        "bid": leg.bid,
        "ask": leg.ask,
        "mid": leg.mid,
        "iv": leg.iv,
        "quoted_at": _time_text(leg.quoted_at),
    }


def _prices_fields(prices: SignalPrices | None) -> dict[str, Any] | None:
    if prices is None:
        return None
    return {
        "spot": prices.spot,
        "spot_at": _time_text(prices.spot_at),
        "front": _leg_fields(prices.front),
        "back": _leg_fields(prices.back),
        "debit_mid": prices.debit_mid,
    }


def _leg_prices(fields: dict[str, Any] | None) -> LegPrices | None:
    if fields is None:
        return None
    return LegPrices(
        fields["bid"], fields["ask"], fields["mid"], fields["iv"], _time(fields["quoted_at"])
    )


def _prices(fields: dict[str, Any] | None) -> SignalPrices | None:
    """None for signals written before prices were recorded."""
    if fields is None:
        return None
    return SignalPrices(
        fields["spot"],
        _time(fields["spot_at"]),
        _leg_prices(fields["front"]),
        _leg_prices(fields["back"]),
        fields["debit_mid"],
    )


def _spread(fields: dict[str, Any]) -> CalendarSpread:
    return CalendarSpread(_leg(fields["front"]), _leg(fields["back"]))


def _order_fields(ticket: OrderTicket) -> dict[str, Any]:
    return {
        "order_id": ticket.order_id,
        "client_order_id": ticket.client_order_id,
        "spread": JsonlTradeRepository._spread_fields(ticket.spread),
        "intent": ticket.intent.value,
        "quantity": ticket.quantity,
        "limit_price": ticket.limit_price,
        "status": ticket.status.value,
        "submitted_at": ticket.submitted_at.isoformat(),
        "filled_quantity": ticket.filled_quantity,
        "fill_price": ticket.fill_price,
    }


def _order(fields: dict[str, Any]) -> OrderTicket:
    return OrderTicket(
        fields["order_id"],
        fields["client_order_id"],
        _spread(fields["spread"]),
        OrderIntent(fields["intent"]),
        fields["quantity"],
        fields["limit_price"],
        OrderStatus(fields["status"]),
        datetime.fromisoformat(fields["submitted_at"]),
        fields["filled_quantity"],
        fields["fill_price"],
    )


def _fill_fields(fill: Fill | None) -> dict[str, Any] | None:
    if fill is None:
        return None
    return {
        "net_price": fill.net_price,
        "quantity": fill.quantity,
        "filled_at": fill.filled_at.isoformat(),
        "front": _leg_fields(fill.front),
        "back": _leg_fields(fill.back),
    }


def _fill(fields: dict[str, Any] | None) -> Fill | None:
    if fields is None:
        return None
    return Fill(
        fields["net_price"],
        fields["quantity"],
        datetime.fromisoformat(fields["filled_at"]),
        _leg_prices(fields["front"]),
        _leg_prices(fields["back"]),
    )


def _position_fields(position: Position) -> dict[str, Any]:
    return {
        "position_id": position.position_id,
        "symbol": position.symbol,
        "spread": JsonlTradeRepository._spread_fields(position.spread),
        "entry": _fill_fields(position.entry),
        "exit": _fill_fields(position.exit),
        "exit_at": _time_text(position.exit_at),
        "exit_failed": position.exit_failed,
    }


def _position(fields: dict[str, Any]) -> Position:
    entry = _fill(fields["entry"])
    assert entry is not None
    return Position(
        _spread(fields["spread"]),
        entry,
        _fill(fields["exit"]),
        fields["position_id"],
        _time(fields["exit_at"]),
        fields["exit_failed"],
    )
