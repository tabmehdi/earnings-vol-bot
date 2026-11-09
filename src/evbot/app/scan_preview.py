from dataclasses import dataclass
from datetime import date

from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.signals import Signal
from evbot.ports.clock import Clock
from evbot.ports.earnings_calendar import EarningsCalendar
from evbot.ports.errors import PortError
from evbot.ports.market_data import MarketDataProvider
from evbot.ports.snapshot_store import SnapshotStore
from evbot.ports.trading_calendar import TradingCalendar
from evbot.strategy.evaluator import MarketContext, SignalEvaluator


@dataclass(frozen=True, slots=True)
class ScanUniverse:
    today: date
    next_session: date
    today_events: tuple[EarningsEvent, ...]
    next_session_events: tuple[EarningsEvent, ...]

    @property
    def selected(self) -> tuple[EarningsEvent, ...]:
        """What an afternoon scan trades: AMC reports today, BMO reports next session.

        UNKNOWN reports from the next session are kept so they are shown as an explained AVOID.
        """
        amc_today = (e for e in self.today_events if e.timing is ReportTiming.AMC)
        next_morning = (e for e in self.next_session_events if e.timing is not ReportTiming.AMC)
        return (*amc_today, *next_morning)


@dataclass(frozen=True, slots=True)
class PreviewLine:
    event: EarningsEvent
    signal: Signal | None
    stale_quotes_dropped: int | None
    error: str


@dataclass(frozen=True, slots=True)
class PreviewReport:
    lines: tuple[PreviewLine, ...]


@dataclass(frozen=True, slots=True)
class ScanPreview:
    """Evaluates the current scan's earnings reports with live data. It never trades."""

    clock: Clock
    trading_calendar: TradingCalendar
    earnings: EarningsCalendar
    market_data: MarketDataProvider
    store: SnapshotStore
    evaluator: SignalEvaluator
    history_bars: int

    def find_reports(self) -> ScanUniverse:
        today = self.clock.now().date()
        next_session = self.trading_calendar.next_session(today)
        return ScanUniverse(
            today,
            next_session,
            self.earnings.events_on(today),
            self.earnings.events_on(next_session),
        )

    def evaluate(self, universe: ScanUniverse, limit: int | None = None) -> PreviewReport:
        last_completed = self.trading_calendar.previous_session(universe.today)
        events = universe.selected[:limit]
        return PreviewReport(tuple(self._preview(event, last_completed) for event in events))

    def _preview(self, event: EarningsEvent, last_completed: date) -> PreviewLine:
        symbol = event.symbol
        try:
            snapshot = self.market_data.get_chain(symbol)
            history = self.market_data.get_history(symbol, last_completed, self.history_bars)
            self.store.save_chain(snapshot)
            self.store.save_history(history, snapshot.chain.as_of, snapshot.feeds)
        except PortError as error:
            return PreviewLine(event, None, None, str(error))
        signal = self.evaluator.evaluate(MarketContext(event, snapshot.chain, history))
        return PreviewLine(event, signal, snapshot.stale_quotes_dropped, "")
