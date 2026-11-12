import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from evbot.app.trade_scheduler import TradeScheduler
from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.scans import ScanKind, ScanRecord
from evbot.domain.signals import FilterResult, Signal, Verdict
from evbot.ports.clock import Clock
from evbot.ports.earnings_calendar import EarningsCalendar
from evbot.ports.errors import PortError, TradingCalendarError
from evbot.ports.market_data import MarketDataProvider
from evbot.ports.risk_free_rate import RiskFreeRateProvider
from evbot.ports.snapshot_store import SnapshotStore
from evbot.ports.trade_repository import TradeRepository
from evbot.ports.trading_calendar import TradingCalendar
from evbot.strategy.evaluator import MarketContext, SignalEvaluator
from evbot.strategy.expiries import monthly_expiries
from evbot.strategy.rates import ConstantRate

MARKET_DATA = "market_data"
QUOTE_FRESHNESS = "quote_freshness"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScanUniverse:
    today: date
    next_session: date
    today_events: tuple[EarningsEvent, ...]
    next_session_events: tuple[EarningsEvent, ...]

    @property
    def selected(self) -> tuple[EarningsEvent, ...]:
        """What an afternoon scan trades: AMC reports today, BMO reports next session.

        UNKNOWN reports from the next session are kept so they are recorded as an explained AVOID.
        """
        amc_today = (e for e in self.today_events if e.timing is ReportTiming.AMC)
        next_morning = (e for e in self.next_session_events if e.timing is not ReportTiming.AMC)
        return (*amc_today, *next_morning)


@dataclass(frozen=True, slots=True)
class Scanner:
    """Evaluates the scan's earnings reports, saves chains and the scan record. Never trades."""

    clock: Clock
    trading_calendar: TradingCalendar
    earnings: EarningsCalendar
    market_data: MarketDataProvider
    store: SnapshotStore
    repository: TradeRepository
    evaluator: SignalEvaluator
    scheduler: TradeScheduler
    history_bars: int
    monthly_horizon_days: int
    fallback_rate: float
    rates: RiskFreeRateProvider | None = None

    def skip_reason(self) -> str | None:
        """Why the scheduled scan should not run now, or None if it should."""
        today = self.clock.now().date()
        try:
            self.trading_calendar.close_time(today)
        except TradingCalendarError:
            return f"{today} is not a trading session"
        if any(scan.kind is ScanKind.SCHEDULED for scan in self.repository.scans_on(today)):
            return f"a scheduled scan is already recorded for {today}"
        return None

    def find_reports(self) -> ScanUniverse:
        today = self.clock.now().date()
        next_session = self.trading_calendar.next_session(today)
        return ScanUniverse(
            today,
            next_session,
            self.earnings.events_on(today),
            self.earnings.events_on(next_session),
        )

    def universe_for(self, tickers: Sequence[str]) -> ScanUniverse:
        """Scans chosen tickers as if they report before the open next session."""
        today = self.clock.now().date()
        next_session = self.trading_calendar.next_session(today)
        events = tuple(EarningsEvent(t, next_session, ReportTiming.BMO) for t in tickers)
        return ScanUniverse(today, next_session, (), events)

    def run(self, universe: ScanUniverse, kind: ScanKind, limit: int | None = None) -> ScanRecord:
        started = self.clock.now()
        rate, source = self._risk_free_rate(universe.today)
        evaluator = replace(
            self.evaluator, metrics=replace(self.evaluator.metrics, rates=ConstantRate(rate))
        )
        last_completed = self.trading_calendar.previous_session(universe.today)
        horizon = universe.today + timedelta(days=self.monthly_horizon_days)
        monthlies = self._monthlies(universe.today, horizon)
        events = universe.selected[:limit]
        skips = self._screen(events)
        signals = tuple(
            self._data_failure(event, f"{event.symbol}: {skips[event.symbol]}")
            if event.symbol in skips
            else self._signal(event, last_completed, evaluator, monthlies)
            for event in events
        )
        record = ScanRecord(
            kind,
            started,
            self.clock.now(),
            signals,
            self._entry_deadline(events),
            risk_free_rate=rate,
            rate_source=source,
        )
        self.repository.save_scan(record)
        return record

    def _risk_free_rate(self, day: date) -> tuple[float, str]:
        """One rate for the whole scan; never fails, so a rate outage cannot stop a scan."""
        if self.rates is None:
            logger.warning(
                "no risk-free rate source configured; using the config rate %.4f",
                self.fallback_rate,
            )
            return self.fallback_rate, "config"
        try:
            return self.rates.rate_on(day), self.rates.name
        except PortError as error:
            logger.warning("%s; using the config rate %.4f", error, self.fallback_rate)
            return self.fallback_rate, "config"

    def _monthlies(self, today: date, horizon: date) -> tuple[date, ...]:
        """One calendar lookup covering every third Friday up to the horizon's month."""
        next_month = (horizon.replace(day=28) + timedelta(days=4)).replace(day=1)
        sessions = set(self.trading_calendar.sessions_between(today, next_month))
        return monthly_expiries(today, horizon, sessions.__contains__)

    def _screen(self, events: Sequence[EarningsEvent]) -> Mapping[str, str]:
        """Tickers with no contracts are skipped up front; on failure, scan them all."""
        try:
            return self.market_data.screen([event.symbol for event in events])
        except PortError as error:
            logger.warning("%s; scanning every ticker instead", error)
            return {}

    def _data_failure(self, event: EarningsEvent, detail: str) -> Signal:
        logger.warning("%s", detail)
        reason = FilterResult(MARKET_DATA, False, None, None, detail=detail)
        return Signal(event, self.clock.now(), None, (reason,), Verdict.AVOID, spread=None)

    def _signal(
        self,
        event: EarningsEvent,
        last_completed: date,
        evaluator: SignalEvaluator,
        monthlies: tuple[date, ...],
    ) -> Signal:
        symbol = event.symbol
        try:
            snapshot = self.market_data.get_chain(symbol)
            history = self.market_data.get_history(symbol, last_completed, self.history_bars)
            self.store.save_chain(snapshot)
            self.store.save_history(history, snapshot.chain.as_of, snapshot.feeds)
        except PortError as error:
            return self._data_failure(event, str(error))
        signal = evaluator.evaluate(MarketContext(event, snapshot.chain, history, monthlies))
        freshness = FilterResult(
            QUOTE_FRESHNESS,
            True,
            None,
            None,
            detail=f"stale_quotes_dropped={snapshot.stale_quotes_dropped}",
        )
        return replace(signal, filters=(*signal.filters, freshness))

    def _entry_deadline(self, events: Sequence[EarningsEvent]) -> datetime | None:
        entries = []
        for event in events:
            try:
                entries.append(self.scheduler.window_for(event).entry_at)
            except (TradingCalendarError, ValueError):
                continue  # unknown timing or no session: nothing to enter, so no deadline
        return min(entries, default=None)
