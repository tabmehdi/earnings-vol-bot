import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from evbot.app.execution import ExecutionService
from evbot.app.scanner import Scanner
from evbot.app.trade_scheduler import TradeScheduler
from evbot.domain.events import EarningsEvent
from evbot.domain.options import OptionContract
from evbot.domain.scans import ScanKind, ScanRecord
from evbot.domain.signals import Signal, Verdict
from evbot.domain.trades import Position
from evbot.ports.broker import Broker
from evbot.ports.clock import Clock
from evbot.ports.errors import PortError, TradingCalendarError
from evbot.ports.market_data import MarketDataProvider
from evbot.ports.trade_repository import TradeRepository
from evbot.ports.trading_calendar import TradingCalendar
from evbot.strategy.risk import RiskManager
from evbot.strategy.sizing import FixedRiskSizer

NEW_YORK = ZoneInfo("America/New_York")
EXIT_WALK = timedelta(minutes=5)  # time given to one exit attempt's price walk
FINAL_WALK = timedelta(minutes=2)  # the single natural-price attempt at the deadline

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionSettings:
    exit_minutes_after_open: int
    scan_minutes_before_close: int
    entry_minutes_before_close: int
    entry_cutoff_minutes_before_close: int
    recheck_count: int
    recheck_gap_seconds: float
    exit_retry_minutes: int
    exit_deadline_time: time  # New York, on the front leg's expiry day
    exit_deadline_minutes_before_close: int  # earlier still on an early-close day


@dataclass(frozen=True, slots=True)
class TradingBot:
    """One trading day: exits at open + 15, the scan at close - 35, entries at close - 15.

    Every time comes from that day's calendar, so early closes move the whole day earlier.
    Running it again on the same day (after a crash and a restart) resumes without
    duplicating a scan, an order or a position.
    """

    clock: Clock
    calendar: TradingCalendar
    scanner: Scanner
    execution: ExecutionService
    broker: Broker
    market_data: MarketDataProvider
    repository: TradeRepository
    scheduler: TradeScheduler
    sizer: FixedRiskSizer
    risk: RiskManager
    settings: SessionSettings

    def run_session(self) -> None:
        today = self.clock.now().astimezone(NEW_YORK).date()
        try:
            opens, closes = self.calendar.open_time(today), self.calendar.close_time(today)
        except TradingCalendarError:
            logger.info("%s is not a trading session; nothing to do", today)
            return
        s = self.settings
        scan_at = closes - timedelta(minutes=s.scan_minutes_before_close)
        entry_at = closes - timedelta(minutes=s.entry_minutes_before_close)
        cutoff = closes - timedelta(minutes=s.entry_cutoff_minutes_before_close)

        self._reconcile(today)
        self._wait_until(opens + timedelta(minutes=s.exit_minutes_after_open))
        self._exit_due_positions(until=scan_at)
        self._wait_until(scan_at)
        if self.clock.now() >= cutoff:
            logger.warning("session reached the entry cutoff %s; no entries today", cutoff)
            return
        record = self._todays_scan(today)
        self._wait_until(entry_at)
        self._enter(record, cutoff)

    # Exits

    def _exit_due_positions(self, until: datetime) -> None:
        while True:
            due = self._due_positions()
            if not due:
                return
            round_started = self.clock.now()
            for position in due:
                self._exit(position)
            remaining = self._due_positions()
            if not remaining:
                return
            now = self.clock.now()
            # Rounds start every retry interval, however long the previous round's walks took.
            wake = round_started + timedelta(minutes=self.settings.exit_retry_minutes)
            upcoming = [d for d in map(self._exit_deadline, remaining) if d > now]
            if upcoming:
                wake = min(wake, min(upcoming))
            if wake >= until:
                return  # the next session tries again
            self._wait_until(wake)

    def _due_positions(self) -> list[Position]:
        now = self.clock.now()
        return [
            p
            for p in self.repository.open_positions()
            if not p.exit_failed and p.exit_at is not None and p.exit_at <= now
        ]

    def _exit(self, position: Position) -> None:
        now = self.clock.now()
        deadline = self._exit_deadline(position)
        final = now >= deadline
        walk_deadline = now + FINAL_WALK if final else min(now + EXIT_WALK, deadline)
        client_order_id = f"{position.position_id}-close-{now:%Y%m%dT%H%M}"
        try:
            closed, remaining = self.execution.close(
                position, client_order_id, walk_deadline, at_natural=final
            )
        except PortError as error:
            logger.error("%s: exit attempt failed: %s", position.position_id, error)
            closed, remaining = None, position
        if closed is not None and closed.exit is not None:
            logger.info(
                "%s: closed %d at %.2f",
                position.position_id,
                closed.entry.quantity,
                closed.exit.net_price,
            )
        # Only after the last attempt at natural. The part left may be long back only now,
        # with a later deadline of its own.
        if remaining is not None and final and now >= self._exit_deadline(remaining):
            self.repository.save_position(remaining.mark_exit_failed())
            logger.error(
                "%s: exit_failed: %d still open after the last attempt at natural (%s)",
                remaining.position_id,
                remaining.entry.quantity,
                deadline,
            )

    def _exit_deadline(self, position: Position) -> datetime:
        """The last attempt is due on the short front's expiry day (assignment risk). With the
        front bought back, only the back is left, and its own expiry sets the deadline."""
        legs = position.spread
        expiry = (legs.back if position.long_back_only else legs.front).contract.expiry
        try:
            closes = self.calendar.close_time(expiry)
        except TradingCalendarError:
            closes = datetime.combine(expiry, time(16), NEW_YORK)
        latest = closes - timedelta(minutes=self.settings.exit_deadline_minutes_before_close)
        return min(datetime.combine(expiry, self.settings.exit_deadline_time, NEW_YORK), latest)

    # Scan and entries

    def _todays_scan(self, today: date) -> ScanRecord:
        done = [s for s in self.repository.scans_on(today) if s.kind is ScanKind.SCHEDULED]
        if done:
            logger.info("using today's scan from %s", done[-1].started_at)
            return done[-1]
        return self.scanner.run(self.scanner.find_reports(), ScanKind.SCHEDULED)

    def _enter(self, record: ScanRecord, cutoff: datetime) -> None:
        held = {p.symbol for p in self.repository.open_positions()}
        candidates: list[EarningsEvent] = []
        for signal in record.signals:
            if signal.verdict is not Verdict.RECOMMENDED or signal.spread is None:
                continue
            if signal.event.symbol in held:
                logger.info("%s: already held; not opening another", signal.event.symbol)
                continue
            candidates.append(signal.event)
        latest = self._recheck(candidates, cutoff)
        for event in candidates:
            held_up = latest.get(event.symbol)
            if held_up is None:
                continue
            if self.clock.now() >= cutoff:
                logger.info("entry cutoff %s reached; not opening %s", cutoff, event.symbol)
                continue
            self._open(held_up, cutoff)

    def _recheck(self, candidates: list[EarningsEvent], cutoff: datetime) -> dict[str, Signal]:
        """All candidates together; a symbol stays only if every re-check still recommends it."""
        latest: dict[str, Signal] = {}
        remaining = list(candidates)
        for round_number in range(1, self.settings.recheck_count + 1):
            if not remaining:
                break
            if round_number > 1:
                self.clock.wait(self.settings.recheck_gap_seconds)
            if self.clock.now() >= cutoff:
                logger.info("entry cutoff %s reached during re-checks; no orders", cutoff)
                return {}
            by_symbol = {s.event.symbol: s for s in self.scanner.recheck(remaining).signals}
            for event in list(remaining):
                signal = by_symbol.get(event.symbol)
                if signal is None or signal.verdict is not Verdict.RECOMMENDED or not signal.spread:
                    verdict = signal.verdict.value if signal else "missing"
                    logger.info(
                        "%s: RECOMMENDED did not hold on re-check %d (%s)",
                        event.symbol,
                        round_number,
                        verdict,
                    )
                    remaining.remove(event)
                    latest.pop(event.symbol, None)
                else:
                    latest[event.symbol] = signal
        return latest

    def _open(self, signal: Signal, cutoff: datetime) -> None:
        spread, event = signal.spread, signal.event
        assert spread is not None
        symbol = event.symbol
        try:
            quotes = self.market_data.get_leg_quotes((spread.front.contract, spread.back.contract))
            equity = self.broker.account_equity()
            window = self.scheduler.window_for(event)
        except (PortError, ValueError) as error:
            logger.warning("%s: not opened: %s", symbol, error)
            return
        front, back = quotes[spread.front.contract], quotes[spread.back.contract]
        size = self.sizer.size(equity, back.mid - front.mid, spread.front.contract.multiplier)
        if size.contracts == 0:
            logger.info("%s: not opened: %s", symbol, size.reason)
            return
        decision = self.risk.approve(
            spread, front, back, size.contracts, self.repository.open_positions(), equity
        )
        if not decision.approved:
            logger.info("%s: not opened: %s", symbol, decision.reason)
            return
        client_order_id = f"{symbol}-{event.report_date.isoformat()}-open"
        try:
            position = self.execution.open(
                spread, size.contracts, client_order_id, deadline=cutoff, exit_at=window.exit_at
            )
        except PortError as error:
            logger.error("%s: entry failed: %s", symbol, error)
            return
        if position is None:
            logger.info("%s: no fill before %s", symbol, cutoff)
        else:
            logger.info(
                "%s: opened %d at %.2f, exit at %s",
                symbol,
                position.entry.quantity,
                position.entry.net_price,
                position.exit_at,
            )

    # Reconciliation

    def _reconcile(self, today: date) -> None:
        """Cancel orders an earlier session left working, then compare holdings."""
        latest = {t.client_order_id: t for t in self.repository.orders_on(today)}
        for ticket in latest.values():
            if ticket.is_done:
                continue
            try:
                current = self.broker.order(ticket)
                if not current.is_done:
                    self.broker.cancel(ticket.order_id)
                    logger.warning(
                        "cancelled %s (%s), left working by an earlier session",
                        ticket.client_order_id,
                        ticket.order_id,
                    )
            except PortError as error:
                logger.error("could not check %s: %s", ticket.client_order_id, error)

        positions = self.repository.open_positions()
        expected: defaultdict[OptionContract, int] = defaultdict(int)
        for position in positions:
            if not position.long_back_only:
                expected[position.spread.front.contract] -= position.entry.quantity
            expected[position.spread.back.contract] += position.entry.quantity
            if position.exit_failed:
                logger.error(
                    "%s: exit_failed position still open; close it by hand", position.position_id
                )
        try:
            held = self.broker.held_contracts()
        except PortError as error:
            logger.error("could not read broker holdings: %s", error)
            return
        for contract in sorted(set(expected) | set(held), key=str):
            if expected.get(contract, 0) != held.get(contract, 0):
                logger.error(
                    "holdings mismatch for %s: positions say %d, broker holds %d",
                    contract,
                    expected.get(contract, 0),
                    held.get(contract, 0),
                )

    def _wait_until(self, moment: datetime) -> None:
        remaining = (moment - self.clock.now()).total_seconds()
        if remaining > 0:
            self.clock.wait(remaining)
