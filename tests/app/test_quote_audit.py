from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from evbot.app.quote_audit import QuoteAudit, summarize
from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.options import OptionContract, OptionRight, OptionTrade
from evbot.domain.scans import ScanKind, ScanRecord
from evbot.domain.signals import LegPrices, Signal, SignalMetrics, SignalPrices, Verdict
from evbot.domain.spreads import CalendarSpread, Leg, Side
from evbot.ports.errors import MarketDataError
from tests.fakes.ports import InMemoryTradeRepository

NY = ZoneInfo("America/New_York")
DAY = date(2026, 11, 23)
START = datetime.combine(DAY, time(15, 15), NY)
END = datetime.combine(DAY, time(15, 55), NY)
METRICS = SignalMetrics(avg_volume_30d=3_000_000, iv30_rv30=1.6, term_slope=-0.01)


def contract(symbol: str, expiry: date, strike: float) -> OptionContract:
    return OptionContract(symbol, expiry, strike, OptionRight.CALL)


BBY_FRONT = contract("BBY", date(2026, 11, 27), 97.0)
BBY_BACK = contract("BBY", date(2026, 12, 18), 97.0)
BBY = CalendarSpread(Leg(BBY_FRONT, Side.SELL), Leg(BBY_BACK, Side.BUY))
DKS_FRONT = contract("DKS", date(2026, 11, 27), 134.0)
DKS_BACK = contract("DKS", date(2026, 12, 18), 134.0)
DKS = CalendarSpread(Leg(DKS_FRONT, Side.SELL), Leg(DKS_BACK, Side.BUY))


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime.combine(DAY, time(hour, minute, second), NY)


def leg(bid: float, ask: float) -> LegPrices:
    return LegPrices(bid, ask, (bid + ask) / 2, None, at(15, 25))


def signal(
    spread: CalendarSpread,
    front: LegPrices,
    back: LegPrices,
    verdict: Verdict = Verdict.RECOMMENDED,
) -> Signal:
    event = EarningsEvent(spread.underlying, date(2026, 11, 24), ReportTiming.BMO)
    prices = SignalPrices(100.0, at(15, 25), front, back, back.mid - front.mid)
    return Signal(event, at(15, 25), METRICS, (), verdict, spread, prices)


def scan(kind: ScanKind, started: datetime, *signals: Signal) -> ScanRecord:
    return ScanRecord(kind, started, started + timedelta(seconds=30), signals)


@dataclass
class FixtureTrades:
    by_contract: Mapping[OptionContract, Sequence[OptionTrade]]
    refused: frozenset[OptionContract] = frozenset()
    asked: list[tuple[OptionContract, datetime, datetime]] = field(default_factory=list)

    def trades(
        self, contract: OptionContract, start: datetime, end: datetime
    ) -> tuple[OptionTrade, ...]:
        self.asked.append((contract, start, end))
        if contract in self.refused:
            raise MarketDataError("HTTP 403: subscription does not permit")
        return tuple(self.by_contract.get(contract, ()))


def trade(c: OptionContract, when: datetime, price: float, size: int) -> OptionTrade:
    return OptionTrade(c, when, price, size)


# BBY as recorded on 2026-11-23: back 5.06/7.24 while it traded 5.85-6.00.
BBY_SCHEDULED = signal(BBY, leg(4.30, 4.50), leg(5.06, 7.24))
BBY_RECHECK = signal(BBY, leg(4.32, 4.48), leg(5.10, 7.20), Verdict.CONSIDER)
TRADES = {
    BBY_FRONT: [
        trade(BBY_FRONT, at(15, 48, 46), 4.45, 3),
        trade(BBY_FRONT, at(15, 50, 2), 4.40, 1),
    ],
    BBY_BACK: [
        trade(BBY_BACK, at(15, 48, 46), 5.95, 3),  # same second and size: a calendar print
        trade(BBY_BACK, at(15, 48, 47), 5.85, 1),
        trade(BBY_BACK, at(15, 50, 2), 6.00, 2),  # same second, different size: not a pair
    ],
}


def audit(
    *scans: ScanRecord, trades: FixtureTrades | None = None
) -> tuple[QuoteAudit, FixtureTrades]:
    repository = InMemoryTradeRepository(scans=list(scans))
    history = trades or FixtureTrades(TRADES)
    return QuoteAudit(repository, history), history


def test_audits_both_legs_of_every_recommended_signal() -> None:
    auditor, history = audit(
        scan(ScanKind.SCHEDULED, at(15, 25, 5), BBY_SCHEDULED),
        scan(ScanKind.RECHECK, at(15, 45), BBY_RECHECK),
    )

    report = auditor.audit(DAY, START, END)

    (bby,) = report.signals
    assert (bby.symbol, bby.front.contract, bby.back.contract) == ("BBY", BBY_FRONT, BBY_BACK)
    assert {c for c, _, _ in history.asked} == {BBY_FRONT, BBY_BACK}
    assert all((s, e) == (START, END) for _, s, e in history.asked)


def test_each_contract_shows_what_every_scan_recorded_whatever_its_verdict() -> None:
    auditor, _ = audit(
        scan(ScanKind.SCHEDULED, at(15, 25, 5), BBY_SCHEDULED),
        scan(ScanKind.RECHECK, at(15, 45), BBY_RECHECK),
    )

    (bby,) = auditor.audit(DAY, START, END).signals

    assert [(r.scan, r.verdict) for r in bby.back.recorded] == [
        ("scheduled 15:25:05", Verdict.RECOMMENDED),
        ("recheck 15:45:00", Verdict.CONSIDER),
    ]
    assert bby.back.recorded[0].prices == leg(5.06, 7.24)
    assert [d for _, d in bby.recorded_debits] == pytest.approx([6.15 - 4.40, 6.15 - 4.40])


def test_a_signal_that_was_never_recommended_is_left_out() -> None:
    auditor, _ = audit(
        scan(
            ScanKind.SCHEDULED,
            at(15, 25, 5),
            signal(DKS, leg(6, 7), leg(6.38, 8.12), Verdict.AVOID),
        )
    )

    assert auditor.audit(DAY, START, END).signals == ()


def test_symbols_choose_signals_whatever_their_verdict() -> None:
    avoided_dks = signal(DKS, leg(6.0, 7.0), leg(6.38, 8.12), Verdict.AVOID)
    auditor, _ = audit(scan(ScanKind.SCHEDULED, at(15, 25, 5), BBY_SCHEDULED, avoided_dks))

    report = auditor.audit(DAY, START, END, symbols=("DKS",))

    assert [s.symbol for s in report.signals] == ["DKS"]


def test_the_summary_compares_trades_with_the_recorded_quote() -> None:
    auditor, _ = audit(scan(ScanKind.SCHEDULED, at(15, 25, 5), BBY_SCHEDULED))
    (bby,) = auditor.audit(DAY, START, END).signals

    summary = summarize(bby.back.trades or ())

    assert (summary.count, summary.contracts) == (3, 6)
    assert (summary.low, summary.high, summary.median) == (5.85, 6.00, 5.95)
    recorded = bby.back.recorded[0].prices  # 5.06/7.24, mid 6.15
    assert summary.median - recorded.mid == pytest.approx(-0.20)
    assert summary.inside(recorded) == 3 and summary.outside(recorded) == 0
    assert summary.inside(leg(5.90, 6.10)) == 2


def test_a_front_and_back_print_in_the_same_second_and_size_is_a_calendar_print() -> None:
    auditor, _ = audit(scan(ScanKind.SCHEDULED, at(15, 25, 5), BBY_SCHEDULED))

    (bby,) = auditor.audit(DAY, START, END).signals

    (calendar,) = bby.prints  # 15:50:02 has sizes 1 and 2: not matched
    assert calendar.at == at(15, 48, 46)
    assert calendar.size == 3
    assert calendar.debit == pytest.approx(5.95 - 4.45)


def test_a_contract_without_trades_says_so() -> None:
    auditor, _ = audit(
        scan(ScanKind.SCHEDULED, at(15, 25, 5), BBY_SCHEDULED), trades=FixtureTrades({})
    )

    (bby,) = auditor.audit(DAY, START, END).signals

    assert bby.front.trades == () and bby.prints == ()
    assert summarize(()).count == 0


def test_a_refused_request_is_reported_and_the_audit_goes_on() -> None:
    refused = FixtureTrades(TRADES, refused=frozenset({BBY_FRONT}))
    auditor, _ = audit(scan(ScanKind.SCHEDULED, at(15, 25, 5), BBY_SCHEDULED), trades=refused)

    (bby,) = auditor.audit(DAY, START, END).signals

    assert bby.front.trades is None and "403" in bby.front.error
    assert bby.back.trades is not None and len(bby.back.trades) == 3
    assert bby.prints == ()


def test_a_contract_shared_by_two_signals_is_fetched_once() -> None:
    other_back = contract("BBY", date(2027, 1, 15), 97.0)
    longer = CalendarSpread(Leg(BBY_FRONT, Side.SELL), Leg(other_back, Side.BUY))
    auditor, history = audit(
        scan(ScanKind.SCHEDULED, at(15, 25, 5), BBY_SCHEDULED),
        scan(ScanKind.RECHECK, at(15, 45), signal(longer, leg(4.3, 4.5), leg(7.0, 8.0))),
    )

    report = auditor.audit(DAY, START, END)

    assert len(report.signals) == 2
    assert [c for c, _, _ in history.asked].count(BBY_FRONT) == 1
