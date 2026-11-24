"""Checks the indicative quotes the scans recorded against the trades the market printed."""

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from evbot.domain.options import OptionContract, OptionTrade
from evbot.domain.scans import ScanRecord
from evbot.domain.signals import LegPrices, Signal, Verdict
from evbot.domain.spreads import CalendarSpread
from evbot.ports.errors import PortError
from evbot.ports.option_trades import OptionTradeHistory
from evbot.ports.trade_repository import TradeRepository

NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class RecordedQuote:
    scan: str  # its kind and New York start time, e.g. "scheduled 15:25:05"
    verdict: Verdict
    prices: LegPrices


@dataclass(frozen=True, slots=True)
class ContractAudit:
    """trades is None when the history could not be read; error then says why."""

    name: str  # e.g. "BBY front"
    contract: OptionContract
    recorded: tuple[RecordedQuote, ...]
    trades: tuple[OptionTrade, ...] | None
    error: str = ""


@dataclass(frozen=True, slots=True)
class CalendarPrint:
    """A front and a back trade in the same second with the same size: most likely one
    multi-leg calendar order, so back minus front is the debit it traded at."""

    at: datetime
    size: int
    front_price: float
    back_price: float

    @property
    def debit(self) -> float:
        return self.back_price - self.front_price


@dataclass(frozen=True, slots=True)
class SignalAudit:
    symbol: str
    spread: CalendarSpread
    front: ContractAudit
    back: ContractAudit
    recorded_debits: tuple[tuple[str, float], ...]  # (scan, debit_mid) for each scan
    prints: tuple[CalendarPrint, ...]


@dataclass(frozen=True, slots=True)
class AuditReport:
    day: date
    start: datetime
    end: datetime
    signals: tuple[SignalAudit, ...]


@dataclass(frozen=True, slots=True)
class TradeSummary:
    prices: tuple[float, ...]
    sizes: tuple[int, ...]

    @property
    def count(self) -> int:
        return len(self.prices)

    @property
    def contracts(self) -> int:
        return sum(self.sizes)

    @property
    def low(self) -> float:
        return min(self.prices)

    @property
    def high(self) -> float:
        return max(self.prices)

    @property
    def median(self) -> float:
        return statistics.median(self.prices)

    def inside(self, quote: LegPrices) -> int:
        return sum(quote.bid <= price <= quote.ask for price in self.prices)

    def outside(self, quote: LegPrices) -> int:
        return self.count - self.inside(quote)


def summarize(trades: Sequence[OptionTrade]) -> TradeSummary:
    return TradeSummary(tuple(t.price for t in trades), tuple(t.size for t in trades))


@dataclass(frozen=True, slots=True)
class QuoteAudit:
    """Reads only: saved scans from the repository and printed trades from the history."""

    repository: TradeRepository
    history: OptionTradeHistory

    def audit(
        self,
        day: date,
        start: datetime,
        end: datetime,
        symbols: Sequence[str] | None = None,
    ) -> AuditReport:
        """Every spread of a RECOMMENDED signal that day, or with symbols, every spread of those
        symbols whatever its verdict. Each contract's trades are fetched once."""
        scans = self.repository.scans_on(day)
        wanted = {s.upper() for s in symbols} if symbols else None
        spreads: dict[CalendarSpread, None] = {}
        for scan in scans:
            for signal in scan.signals:
                if signal.spread is None:
                    continue
                if wanted is not None:
                    chosen = signal.event.symbol in wanted
                else:
                    chosen = signal.verdict is Verdict.RECOMMENDED
                if chosen:
                    spreads.setdefault(signal.spread)
        fetched: dict[OptionContract, tuple[tuple[OptionTrade, ...] | None, str]] = {}

        def trades_of(contract: OptionContract) -> tuple[tuple[OptionTrade, ...] | None, str]:
            if contract not in fetched:
                try:
                    fetched[contract] = (self.history.trades(contract, start, end), "")
                except PortError as error:
                    fetched[contract] = (None, str(error))
            return fetched[contract]

        audits = []
        for spread in spreads:
            symbol = spread.underlying
            legs = []
            for name, contract in (
                ("front", spread.front.contract),
                ("back", spread.back.contract),
            ):
                trades, error = trades_of(contract)
                legs.append(
                    ContractAudit(
                        f"{symbol} {name}",
                        contract,
                        _recorded(scans, contract, name),
                        trades,
                        error,
                    )
                )
            front, back = legs
            audits.append(
                SignalAudit(
                    symbol,
                    spread,
                    front,
                    back,
                    _recorded_debits(scans, spread),
                    _calendar_prints(front.trades or (), back.trades or ()),
                )
            )
        return AuditReport(day, start, end, tuple(audits))


def scan_label(scan: ScanRecord) -> str:
    return f"{scan.kind.value} {scan.started_at.astimezone(NEW_YORK):%H:%M:%S}"


def _recorded(
    scans: Sequence[ScanRecord], contract: OptionContract, leg: str
) -> tuple[RecordedQuote, ...]:
    """What each scan recorded for this contract, on whichever signal had it as this leg."""
    found = []
    for scan in scans:
        for signal in scan.signals:
            prices = _leg_prices(signal, contract, leg)
            if prices is not None:
                found.append(RecordedQuote(scan_label(scan), signal.verdict, prices))
    return tuple(found)


def _leg_prices(signal: Signal, contract: OptionContract, leg: str) -> LegPrices | None:
    if signal.spread is None or signal.prices is None:
        return None
    if leg == "front" and signal.spread.front.contract == contract:
        return signal.prices.front
    if leg == "back" and signal.spread.back.contract == contract:
        return signal.prices.back
    return None


def _recorded_debits(
    scans: Sequence[ScanRecord], spread: CalendarSpread
) -> tuple[tuple[str, float], ...]:
    return tuple(
        (scan_label(scan), signal.prices.debit_mid)
        for scan in scans
        for signal in scan.signals
        if signal.spread == spread
        and signal.prices is not None
        and signal.prices.debit_mid is not None
    )


def _calendar_prints(
    front: Sequence[OptionTrade], back: Sequence[OptionTrade]
) -> tuple[CalendarPrint, ...]:
    """Each front trade pairs with the first unused back trade of the same second and size."""
    unused = list(back)
    prints = []
    for front_trade in front:
        second = front_trade.traded_at.replace(microsecond=0)
        match = next(
            (
                b
                for b in unused
                if b.traded_at.replace(microsecond=0) == second and b.size == front_trade.size
            ),
            None,
        )
        if match is not None:
            unused.remove(match)
            prints.append(
                CalendarPrint(
                    front_trade.traded_at, front_trade.size, front_trade.price, match.price
                )
            )
    return tuple(prints)
