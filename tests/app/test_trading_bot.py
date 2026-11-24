import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from evbot.app.execution import ExecutionService
from evbot.app.scanner import Scanner
from evbot.app.trade_scheduler import TradeScheduler
from evbot.app.trading_bot import SessionSettings, TradingBot
from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.options import OptionContract, OptionRight
from evbot.domain.orders import OrderIntent, OrderStatus, OrderTicket, SpreadLeg
from evbot.domain.scans import ScanKind
from evbot.domain.snapshots import ChainSnapshot, DataFeeds
from evbot.domain.spreads import CalendarSpread, Leg, Side
from evbot.domain.trades import Fill, Position
from evbot.strategy.pricing import MidWalkPolicy
from evbot.strategy.risk import RiskManager
from evbot.strategy.sizing import FixedRiskSizer
from tests.fakes.broker import FakeBroker
from tests.fakes.ports import (
    AdvancingClock,
    FakeEarningsCalendar,
    FakeMarketData,
    FakeTradingCalendar,
    InMemorySnapshotStore,
    InMemoryTradeRepository,
)
from tests.fixtures.market import (
    BACKWARDATED,
    CONTANGO,
    SAME_DAY,
    build_chain,
    build_history,
)
from tests.strategy.test_evaluator import CALM, LIQUID_VOLUME, evaluator

NY = ZoneInfo("America/New_York")
WED, THU, FRI = date(2026, 11, 18), date(2026, 11, 19), date(2026, 11, 20)
THANKSGIVING = date(2026, 11, 26)
FEEDS = DataFeeds(options="indicative", spot="iex", history="sip")
HISTORY = build_history(CALM, LIQUID_VOLUME)
THU_MORNING = datetime.combine(THU, time(9), NY)
AAPL = EarningsEvent("AAPL", FRI, ReportTiming.BMO)  # entered Thursday, exited Friday 9:45


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), NY)


def session(day: date, close_hour: int = 16) -> tuple[date, tuple[datetime, datetime]]:
    return day, (at(day, 9, 30), at(day, close_hour))


SESSIONS = dict(
    session(day)
    for day in (
        WED,
        THU,
        FRI,
        date(2026, 11, 23),
        date(2026, 11, 25),
        date(2026, 12, 18),
        date(2027, 1, 15),
        date(2027, 2, 19),
    )
)
SETTINGS = SessionSettings(
    exit_minutes_after_open=15,
    scan_minutes_before_close=35,
    entry_minutes_before_close=15,
    entry_cutoff_minutes_before_close=5,
    recheck_count=2,
    recheck_gap_seconds=60,
    exit_retry_minutes=15,
    exit_deadline_time=time(15, 0),
    exit_deadline_minutes_before_close=60,
)
SIZER = FixedRiskSizer(max_risk_per_trade_pct=0.02, max_contracts=10)
RISK = RiskManager(
    max_spread_cost_pct=0.35,
    max_open_positions=5,
    max_committed_pct=0.20,
    enforce_spread_cost=True,
)
SNAPSHOT = ChainSnapshot(build_chain(BACKWARDATED), 0, FEEDS)


@dataclass
class ChangingMarketData:
    """Serves one chain to the scan, then another to every later look (the re-checks)."""

    first: ChainSnapshot
    later: ChainSnapshot
    inner: FakeMarketData = field(init=False)
    served: int = 0

    def __post_init__(self) -> None:
        self.inner = FakeMarketData({"AAPL": self.first}, {"AAPL": HISTORY})

    def screen(self, symbols: Any) -> dict[str, str]:
        return {}

    def get_chain(self, symbol: str) -> ChainSnapshot:
        self.served += 1
        return self.first if self.served == 1 else self.later

    def get_history(self, symbol: str, end: date, bars: int) -> Any:
        return self.inner.get_history(symbol, end, bars)

    def get_leg_quotes(self, contracts: Any) -> Any:
        return self.inner.get_leg_quotes(contracts)


@dataclass
class Harness:
    bot: TradingBot
    broker: FakeBroker
    repository: InMemoryTradeRepository
    clock: AdvancingClock


def harness(
    start: datetime = THU_MORNING,
    events: tuple[EarningsEvent, ...] = (AAPL,),
    market: Any = None,
    settings: SessionSettings = SETTINGS,
    risk: RiskManager = RISK,
    repository: InMemoryTradeRepository | None = None,
    **broker_options: Any,
) -> Harness:
    clock = AdvancingClock(start)
    calendar = FakeTradingCalendar(SESSIONS)
    market = market or FakeMarketData({"AAPL": SNAPSHOT}, {"AAPL": HISTORY})
    repository = repository if repository is not None else InMemoryTradeRepository()
    broker = FakeBroker(clock, **broker_options)
    scheduler = TradeScheduler(calendar, 15, 15)
    scanner = Scanner(
        clock=clock,
        trading_calendar=calendar,
        earnings=FakeEarningsCalendar(events),
        market_data=market,
        store=InMemorySnapshotStore(),
        repository=repository,
        evaluator=evaluator(),
        scheduler=scheduler,
        history_bars=31,
        monthly_horizon_days=75,
        fallback_rate=0.045,
    )
    pricing = MidWalkPolicy(steps=5, step_seconds=20, entry_cap_fraction=0.5)
    execution = ExecutionService(broker, market, pricing, repository, clock, 0.05)
    bot = TradingBot(
        clock,
        calendar,
        scanner,
        execution,
        broker,
        market,
        repository,
        scheduler,
        SIZER,
        risk,
        settings,
    )
    return Harness(bot, broker, repository, clock)


def held_position(front_expiry: date = FRI, exit_at: datetime | None = None) -> Position:
    front = OptionContract("AAPL", front_expiry, 100.0, OptionRight.CALL)
    back = OptionContract("AAPL", date(2026, 12, 18), 100.0, OptionRight.CALL)
    spread = CalendarSpread(Leg(front, Side.SELL), Leg(back, Side.BUY))
    return Position(
        spread,
        Fill(2.00, 1, at(WED, 15, 45)),
        position_id="AAPL-2026-11-19-open",
        exit_at=exit_at or at(THU, 9, 45),
    )


def opens(h: Harness) -> list[OrderTicket]:
    return [t for t in h.broker.submitted if t.intent is OrderIntent.OPEN]


def closes(h: Harness) -> list[OrderTicket]:
    return [t for t in h.broker.submitted if t.intent is OrderIntent.CLOSE]


# A trading day


def test_a_session_scans_rechecks_and_opens_the_recommended_calendar() -> None:
    h = harness()

    h.bot.run_session()

    (position,) = h.repository.open_positions()
    assert position.symbol == "AAPL"
    assert position.exit_at == at(FRI, 9, 45)
    assert [s.kind for s in h.repository.scans] == [
        ScanKind.SCHEDULED,
        ScanKind.RECHECK,
        ScanKind.RECHECK,
    ]


def test_the_scan_and_the_orders_happen_at_their_times() -> None:
    h = harness()

    h.bot.run_session()

    assert h.repository.scans[0].started_at == at(THU, 15, 25)
    (order,) = opens(h)
    assert at(THU, 15, 45) <= order.submitted_at < at(THU, 15, 55)


def test_the_two_rechecks_are_about_a_minute_apart() -> None:
    h = harness()

    h.bot.run_session()

    first, second = (s.started_at for s in h.repository.scans[1:])
    assert second - first == timedelta(seconds=60)


def test_an_early_close_moves_every_time_earlier() -> None:
    sessions = {**SESSIONS, THU: (at(THU, 9, 30), at(THU, 13))}
    h = harness()
    h.bot = replace(h.bot, calendar=FakeTradingCalendar(sessions))

    h.bot.run_session()

    assert h.repository.scans[0].started_at == at(THU, 12, 25)
    assert opens(h)[0].submitted_at < at(THU, 12, 55)


def test_a_holiday_does_nothing() -> None:
    h = harness(start=at(THANKSGIVING, 9))

    h.bot.run_session()

    assert h.repository.scans == []
    assert h.broker.submitted == []


# Entry safety


def test_a_verdict_that_flips_in_a_recheck_is_not_traded(caplog: pytest.LogCaptureFixture) -> None:
    flipping = ChangingMarketData(SNAPSHOT, ChainSnapshot(build_chain(CONTANGO), 0, FEEDS))
    h = harness(market=flipping)

    with caplog.at_level(logging.INFO):
        h.bot.run_session()

    assert opens(h) == []
    assert "AAPL" in caplog.text and "did not hold" in caplog.text


def test_a_symbol_already_held_is_not_opened_again() -> None:
    repository = InMemoryTradeRepository()
    repository.save_position(held_position(exit_at=at(FRI, 9, 45)))
    h = harness(repository=repository)

    h.bot.run_session()

    assert opens(h) == []


def test_a_restarted_session_does_not_open_twice() -> None:
    h = harness()
    h.bot.run_session()
    h.clock.moment = at(THU, 15, 50)  # e.g. systemd restarts the crashed session

    h.bot.run_session()

    assert len(opens(h)) == 1
    assert [s.kind for s in h.repository.scans].count(ScanKind.SCHEDULED) == 1


def test_no_order_after_the_cutoff(caplog: pytest.LogCaptureFixture) -> None:
    slow = replace(SETTINGS, recheck_gap_seconds=600)  # re-checks end at 15:55, the cutoff
    h = harness(settings=slow)

    with caplog.at_level(logging.INFO):
        h.bot.run_session()

    assert opens(h) == []
    assert "cutoff" in caplog.text


def test_too_little_equity_for_one_contract_skips_with_a_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = harness(equity=1_000.0)

    with caplog.at_level(logging.INFO):
        h.bot.run_session()

    assert opens(h) == []
    assert "budget" in caplog.text


def test_a_risk_rejection_skips_with_its_reason(caplog: pytest.LogCaptureFixture) -> None:
    strict = replace(RISK, max_spread_cost_pct=0.001)
    h = harness(risk=strict)

    with caplog.at_level(logging.INFO):
        h.bot.run_session()

    assert opens(h) == []
    assert "round-trip spread cost" in caplog.text


def test_a_log_only_spread_cost_is_logged_and_the_calendar_still_opens(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log_only = replace(RISK, max_spread_cost_pct=0.001, enforce_spread_cost=False)
    h = harness(risk=log_only)

    with caplog.at_level(logging.INFO):
        h.bot.run_session()

    (position,) = h.repository.open_positions()
    assert "round-trip spread cost" in caplog.text and "not enforced" in caplog.text
    assert position.spread_cost is not None and position.spread_cost.share > 0.001


def test_an_opened_position_records_the_estimated_spread_cost() -> None:
    h = harness()

    h.bot.run_session()

    (position,) = h.repository.open_positions()
    spread = position.spread
    front = SNAPSHOT.chain.quote(spread.front.contract)
    back = SNAPSHOT.chain.quote(spread.back.contract)
    assert position.spread_cost is not None
    assert position.spread_cost.round_trip == pytest.approx(
        (front.ask - front.bid) + (back.ask - back.bid)
    )
    assert position.spread_cost.mid_debit == pytest.approx(back.mid - front.mid)


# The final entry attempt at the natural price

BASE_ID = "AAPL-2026-11-20-open"
POLICY = MidWalkPolicy(5, 20, 0.5)


def entry_prices() -> tuple[float, float]:
    """(mid debit, natural debit) of the calendar the session picks for AAPL."""
    probe = harness(fill_on_check=None)
    probe.bot.run_session()
    spread = opens(probe)[0].spread
    front = SNAPSHOT.chain.quote(spread.front.contract)
    back = SNAPSHOT.chain.quote(spread.back.contract)
    return back.mid - front.mid, POLICY.natural(OrderIntent.OPEN, front, back)


@dataclass
class WideningMarketData:
    """Leg quotes widen by a dollar on each side from the widen_from-th leg-quote fetch on."""

    inner: FakeMarketData
    widen_from: int
    fetches: int = 0

    def screen(self, symbols: Any) -> dict[str, str]:
        return {}

    def get_chain(self, symbol: str) -> ChainSnapshot:
        return self.inner.get_chain(symbol)

    def get_history(self, symbol: str, end: date, bars: int) -> Any:
        return self.inner.get_history(symbol, end, bars)

    def get_leg_quotes(self, contracts: Any) -> Any:
        self.fetches += 1
        quotes = self.inner.get_leg_quotes(contracts)
        if self.fetches < self.widen_from:
            return quotes
        return {c: replace(q, bid=max(q.bid - 1, 0), ask=q.ask + 1) for c, q in quotes.items()}


def test_an_unfilled_walk_ends_with_one_attempt_at_the_natural() -> None:
    _, natural = entry_prices()
    h = harness(fill_on_check=None)

    h.bot.run_session()

    assert [t.client_order_id for t in opens(h)] == [BASE_ID, f"{BASE_ID}-nat"]
    assert opens(h)[1].limit_price == natural


def test_a_fill_at_the_natural_is_saved_under_the_entry_id() -> None:
    _, natural = entry_prices()
    h = harness(fill_at_or_above=natural)  # the walk's cap stays below the natural

    h.bot.run_session()

    (position,) = h.repository.open_positions()
    assert position.position_id == BASE_ID
    assert position.entry.net_price == natural


def test_a_restart_after_a_natural_fill_does_not_open_again() -> None:
    _, natural = entry_prices()
    h = harness(fill_at_or_above=natural)
    h.bot.run_session()
    h.clock.moment = at(THU, 15, 50)

    h.bot.run_session()

    assert len(opens(h)) == 2  # the walk's order and the natural one, never more
    assert len(h.repository.open_positions()) == 1


def test_a_natural_that_breaches_the_spread_cost_cap_is_not_sent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Fetch 1 sizes the trade and fetches 2-6 walk it; from fetch 7 (the natural's re-check)
    # each leg is two dollars wide.
    market = WideningMarketData(FakeMarketData({"AAPL": SNAPSHOT}, {"AAPL": HISTORY}), 7)
    h = harness(market=market, fill_on_check=None)

    with caplog.at_level(logging.INFO):
        h.bot.run_session()

    assert [t.client_order_id for t in opens(h)] == [BASE_ID]
    assert "round-trip spread cost" in caplog.text


def test_the_natural_order_is_sent_at_the_price_the_risk_check_approved() -> None:
    # Fetch 7 is the re-check that approves the natural; from fetch 8 (the order's own) each
    # leg is a dollar wider on each side, so the natural there is higher.
    _, approved = entry_prices()
    market = WideningMarketData(FakeMarketData({"AAPL": SNAPSHOT}, {"AAPL": HISTORY}), 8)
    h = harness(market=market, fill_on_check=None)

    h.bot.run_session()

    assert [t.client_order_id for t in opens(h)] == [BASE_ID, f"{BASE_ID}-nat"]
    assert opens(h)[1].limit_price == approved


def test_a_natural_that_sizes_to_zero_contracts_is_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Equity between the two: 2% of it buys one contract at the mid, none at the natural.
    mid, natural = entry_prices()
    equity = (mid + natural) / 2 * 100 / 0.02
    h = harness(fill_on_check=None, equity=equity)

    with caplog.at_level(logging.INFO):
        h.bot.run_session()

    assert [t.client_order_id for t in opens(h)] == [BASE_ID]
    assert "natural" in caplog.text and "budget" in caplog.text


def test_no_natural_attempt_after_the_cutoff(caplog: pytest.LogCaptureFixture) -> None:
    # Entry at close - 15, re-checks until close - 14, cutoff at close - 13: the walk runs
    # into the cutoff and nothing is left for the natural attempt.
    settings = replace(SETTINGS, entry_cutoff_minutes_before_close=13)
    h = harness(settings=settings, fill_on_check=None)

    with caplog.at_level(logging.INFO):
        h.bot.run_session()

    assert [t.client_order_id for t in opens(h)] == [BASE_ID]
    assert "cutoff" in caplog.text


# Exits


def due_position_harness(front_expiry: date = FRI, **broker_options: Any) -> Harness:
    repository = InMemoryTradeRepository()
    repository.save_position(held_position(front_expiry))
    return harness(events=(), repository=repository, **broker_options)


def test_a_due_position_is_closed_at_open_plus_15() -> None:
    h = due_position_harness()

    h.bot.run_session()

    assert h.repository.open_positions() == ()
    (order,) = closes(h)
    assert order.submitted_at == at(THU, 9, 45)


def test_an_unfilled_exit_is_retried_every_15_minutes() -> None:
    h = due_position_harness(fill_on_check=None)

    h.bot.run_session()

    starts = [t.submitted_at for t in closes(h)]
    assert starts[:3] == [at(THU, 9, 45), at(THU, 10), at(THU, 10, 15)]
    assert len({t.client_order_id for t in closes(h)}) == len(starts)
    (still_open,) = h.repository.open_positions()
    assert not still_open.exit_failed  # its front leg expires Friday: more chances


def test_on_the_expiry_day_the_last_attempt_is_natural_then_exit_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = due_position_harness(front_expiry=SAME_DAY, fill_on_check=None)

    h.bot.run_session()

    last = closes(h)[-1]
    assert last.submitted_at == at(THU, 15)
    front_quote = SNAPSHOT.chain.quote(held_position(SAME_DAY).spread.front.contract)
    back_quote = SNAPSHOT.chain.quote(held_position(SAME_DAY).spread.back.contract)
    assert last.limit_price == MidWalkPolicy(5, 20, 0.5).natural(
        OrderIntent.CLOSE, front_quote, back_quote
    )
    (failed,) = h.repository.open_positions()
    assert failed.exit_failed
    assert "exit_failed" in caplog.text


def test_an_exit_that_fills_on_a_retry_closes_the_position() -> None:
    # The market only takes the order from 10:10: the 9:45 and 10:00 rounds miss, 10:15 fills.
    h = due_position_harness(fill_from=at(THU, 10, 10))

    h.bot.run_session()

    assert h.repository.open_positions() == ()
    assert [t.submitted_at for t in closes(h)] == [at(THU, 9, 45), at(THU, 10), at(THU, 10, 15)]


def test_a_refused_calendar_exit_is_closed_leg_by_leg() -> None:
    h = due_position_harness(reject_calendar_closes=True)

    h.bot.run_session()

    assert [t.leg for t in closes(h)] == [None, SpreadLeg.FRONT, SpreadLeg.BACK]
    assert h.repository.open_positions() == ()


def test_a_front_not_bought_back_by_the_last_attempt_is_exit_failed() -> None:
    h = due_position_harness(
        front_expiry=SAME_DAY,
        reject_calendar_closes=True,
        unfilled_legs=frozenset({SpreadLeg.FRONT}),
    )

    h.bot.run_session()

    (failed,) = h.repository.open_positions()
    assert failed.exit_failed and not failed.long_back_only


def test_a_long_back_only_position_keeps_retrying_past_the_front_expiry_deadline() -> None:
    h = due_position_harness(
        front_expiry=SAME_DAY,
        reject_calendar_closes=True,
        unfilled_legs=frozenset({SpreadLeg.BACK}),
    )

    h.bot.run_session()

    (back_only,) = h.repository.open_positions()
    assert back_only.long_back_only
    assert not back_only.exit_failed  # no short leg left: no assignment risk
    back_sales = [t for t in closes(h) if t.leg is SpreadLeg.BACK]
    assert len({t.client_order_id for t in back_sales}) > 1  # retried in later windows
    assert all(t.leg is not SpreadLeg.FRONT for t in closes(h)[2:])  # front bought once


def test_a_long_back_only_position_sells_the_back_in_the_next_session() -> None:
    repository = InMemoryTradeRepository()
    repository.save_position(held_position().close_front(Fill(1.00, 1, at(THU, 9, 45))))
    h = harness(events=(), repository=repository)

    h.bot.run_session()

    assert [t.leg for t in closes(h)] == [SpreadLeg.BACK]
    assert h.repository.open_positions() == ()


# Reconciliation


def test_a_working_order_left_by_a_crash_is_cancelled(caplog: pytest.LogCaptureFixture) -> None:
    repository = InMemoryTradeRepository()
    h = harness(events=(), repository=repository, fill_on_check=None)
    orphan = h.broker.submit(
        held_position().spread, OrderIntent.OPEN, 1, 2.0, "AAPL-2026-11-19-open"
    )
    repository.save_order(orphan)

    with caplog.at_level(logging.INFO):
        h.bot.run_session()

    assert h.broker.orders[orphan.order_id].status is OrderStatus.CANCELLED
    assert "AAPL-2026-11-19-open" in caplog.text


def test_a_long_back_only_position_expects_only_its_back_leg(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = InMemoryTradeRepository()
    back_only = held_position().close_front(Fill(1.00, 1, at(THU, 9, 45)))
    repository.save_position(back_only)
    h = harness(events=(), repository=repository, unfilled_legs=frozenset({SpreadLeg.BACK}))
    h.broker.held[back_only.spread.back.contract] = 1

    h.bot.run_session()

    assert "mismatch" not in caplog.text


def test_holdings_that_do_not_match_positions_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    h = harness(events=())
    stray = OptionContract("TSLA", FRI, 300.0, OptionRight.CALL)
    h.broker.held[stray] = 2

    h.bot.run_session()

    assert "TSLA" in caplog.text and "mismatch" in caplog.text
