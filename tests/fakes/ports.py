from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from evbot.domain.events import EarningsEvent
from evbot.domain.prices import PriceHistory
from evbot.domain.scans import ScanRecord
from evbot.domain.snapshots import ChainSnapshot, DataFeeds
from evbot.ports.errors import (
    MarketDataError,
    RiskFreeRateError,
    SnapshotError,
    TradingCalendarError,
)

NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class FixedClock:
    moment: datetime

    def now(self) -> datetime:
        return self.moment


@dataclass(frozen=True, slots=True)
class FakeEarningsCalendar:
    events: tuple[EarningsEvent, ...]

    def events_on(self, day: date) -> tuple[EarningsEvent, ...]:
        return tuple(event for event in self.events if event.report_date == day)


@dataclass(frozen=True, slots=True)
class FakeMarketData:
    chains: dict[str, ChainSnapshot]
    histories: dict[str, PriceHistory]
    skips: dict[str, str] = field(default_factory=dict)
    screen_fails: bool = False
    asked: list[str] = field(default_factory=list)

    def screen(self, symbols: Sequence[str]) -> dict[str, str]:
        if self.screen_fails:
            raise MarketDataError("contract screening is down")
        return {s: reason for s, reason in self.skips.items() if s in symbols}

    def get_chain(self, symbol: str) -> ChainSnapshot:
        self.asked.append(symbol)
        if symbol not in self.chains:
            raise MarketDataError(f"no chain for {symbol}")
        return self.chains[symbol]

    def get_history(self, symbol: str, end: date, bars: int) -> PriceHistory:
        if symbol not in self.histories:
            raise MarketDataError(f"no history for {symbol}")
        history = self.histories[symbol]
        kept = tuple(bar for bar in history.bars if bar.day <= end)[-bars:]
        return PriceHistory(symbol, kept)


@dataclass(frozen=True, slots=True)
class FakeTradingCalendar:
    sessions: dict[date, tuple[datetime, datetime]]

    def previous_session(self, day: date) -> date:
        earlier = [session for session in self.sessions if session < day]
        if not earlier:
            raise TradingCalendarError(f"no session before {day}")
        return max(earlier)

    def next_session(self, day: date) -> date:
        later = [session for session in self.sessions if session > day]
        if not later:
            raise TradingCalendarError(f"no session after {day}")
        return min(later)

    def open_time(self, day: date) -> datetime:
        return self._session(day)[0]

    def close_time(self, day: date) -> datetime:
        return self._session(day)[1]

    def _session(self, day: date) -> tuple[datetime, datetime]:
        if day not in self.sessions:
            raise TradingCalendarError(f"{day} is not a session")
        return self.sessions[day]


@dataclass(slots=True)
class InMemorySnapshotStore:
    chains: dict[tuple[str, datetime], ChainSnapshot] = field(default_factory=dict)
    histories: dict[tuple[str, datetime], PriceHistory] = field(default_factory=dict)

    def save_chain(self, snapshot: ChainSnapshot) -> None:
        self.chains[(snapshot.chain.underlying, snapshot.chain.as_of)] = snapshot

    def load_chain(self, symbol: str, as_of: datetime) -> ChainSnapshot:
        if (symbol, as_of) not in self.chains:
            raise SnapshotError(f"no chain for {symbol} at {as_of}")
        return self.chains[(symbol, as_of)]

    def save_history(self, history: PriceHistory, as_of: datetime, feeds: DataFeeds) -> None:
        self.histories[(history.symbol, as_of)] = history

    def load_history(self, symbol: str, as_of: datetime) -> PriceHistory:
        if (symbol, as_of) not in self.histories:
            raise SnapshotError(f"no history for {symbol} at {as_of}")
        return self.histories[(symbol, as_of)]


@dataclass(slots=True)
class InMemoryTradeRepository:
    scans: list[ScanRecord] = field(default_factory=list)

    def save_scan(self, scan: ScanRecord) -> None:
        self.scans.append(scan)

    def scans_on(self, day: date) -> tuple[ScanRecord, ...]:
        found = (s for s in self.scans if s.started_at.astimezone(NEW_YORK).date() == day)
        return tuple(sorted(found, key=lambda scan: scan.started_at))


@dataclass(frozen=True, slots=True)
class FixedRiskFreeRate:
    rate: float
    name: str = "fixed"

    def rate_on(self, day: date) -> float:
        return self.rate


@dataclass(frozen=True, slots=True)
class FailingRiskFreeRate:
    name: str = "failing"

    def rate_on(self, day: date) -> float:
        raise RiskFreeRateError("rate source is down")
