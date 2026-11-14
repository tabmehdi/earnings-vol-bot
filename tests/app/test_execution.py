from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from evbot.app.execution import ExecutionService
from evbot.domain.options import OptionContract, OptionQuote, OptionRight
from evbot.domain.orders import OrderIntent, OrderStatus, SpreadLeg
from evbot.domain.spreads import CalendarSpread, Leg, Side
from evbot.domain.trades import Fill, Position
from evbot.strategy.pricing import MidWalkPolicy
from tests.fakes.broker import FakeBroker
from tests.fakes.ports import AdvancingClock, FakeMarketData, InMemoryTradeRepository

NEW_YORK = ZoneInfo("America/New_York")
START = datetime(2026, 11, 19, 15, 45, tzinfo=NEW_YORK)
EXIT_AT = datetime(2026, 11, 20, 9, 45, tzinfo=NEW_YORK)
FRONT = OptionContract("AAPL", date(2026, 11, 20), 100.0, OptionRight.CALL)
BACK = OptionContract("AAPL", date(2026, 12, 18), 100.0, OptionRight.CALL)
SPREAD = CalendarSpread(Leg(FRONT, Side.SELL), Leg(BACK, Side.BUY))
POLICY = MidWalkPolicy(steps=5, step_seconds=20, entry_cap_fraction=0.5)
# Front 1.00/1.20, back 3.00/3.40: open walk 2.10 2.13 2.17 2.21 2.25; close walk
# 2.10 2.03 1.95 1.88 1.80; natural credit 1.80.
QUOTES = {
    FRONT: OptionQuote(FRONT, 1.00, 1.20, None, 0, 0, quoted_at=START),
    BACK: OptionQuote(BACK, 3.00, 3.40, None, 0, 0, quoted_at=START),
}
CID = "AAPL-2026-11-20-open"


def service(
    **broker_options: object,
) -> tuple[ExecutionService, FakeBroker, InMemoryTradeRepository]:
    clock = AdvancingClock(START)
    broker = FakeBroker(clock, **broker_options)  # type: ignore[arg-type]
    repository = InMemoryTradeRepository()
    market = FakeMarketData({}, {}, leg_quotes=dict(QUOTES))
    return ExecutionService(broker, market, POLICY, repository, clock), broker, repository


def open_trade(execution: ExecutionService, deadline_minutes: float = 10) -> Position | None:
    return execution.open(
        SPREAD, 3, CID, deadline=START + timedelta(minutes=deadline_minutes), exit_at=EXIT_AT
    )


# Opening


def test_fills_at_the_mid_on_the_first_check() -> None:
    execution, broker, repository = service(fill_on_check=1)

    position = open_trade(execution)

    assert position is not None
    assert (position.entry.net_price, position.entry.quantity) == (2.10, 3)
    assert position.position_id == CID
    assert position.exit_at == EXIT_AT
    assert repository.open_positions() == (position,)
    assert [o.status for o in repository.orders] == [OrderStatus.WORKING, OrderStatus.FILLED]
    assert broker.replaced == []


def test_walks_the_price_up_until_it_fills() -> None:
    execution, broker, _ = service(fill_at_or_above=2.17)

    position = open_trade(execution)

    assert position is not None
    assert position.entry.net_price == 2.17
    assert [price for _, price in broker.replaced] == [2.13, 2.17]


def test_fill_records_both_legs_market_at_fill_time() -> None:
    execution, _, _ = service()

    position = open_trade(execution)

    assert position is not None and position.entry.front is not None
    assert (position.entry.front.bid, position.entry.front.ask) == (1.00, 1.20)
    assert position.entry.back is not None and position.entry.back.mid == pytest.approx(3.20)


def test_an_unfilled_order_is_cancelled_after_the_last_step() -> None:
    execution, broker, repository = service(fill_on_check=None)

    assert open_trade(execution) is None
    assert len(broker.replaced) == 4  # five prices in all
    assert broker.cancelled
    assert repository.orders[-1].status is OrderStatus.CANCELLED
    assert repository.open_positions() == ()


def test_a_partial_fill_becomes_a_smaller_position_and_the_rest_is_cancelled() -> None:
    execution, broker, _ = service(fill_on_check=1, partial_fill=2)

    position = open_trade(execution)

    assert position is not None and position.entry.quantity == 2
    assert broker.cancelled


def test_a_rejected_order_opens_nothing() -> None:
    execution, broker, repository = service(reject=True)

    assert open_trade(execution) is None
    assert broker.replaced == []
    assert repository.orders[-1].status is OrderStatus.REJECTED


def test_no_order_is_sent_or_repriced_after_the_deadline() -> None:
    # Deadline 30 s in: prices at 0 s and 20 s, then the next step would be at 40 s.
    execution, broker, _ = service(fill_on_check=None)

    assert open_trade(execution, deadline_minutes=0.5) is None
    assert len(broker.submitted) == 1
    assert len(broker.replaced) == 1
    assert broker.cancelled


def test_a_restart_resumes_the_order_instead_of_sending_another() -> None:
    execution, broker, _ = service(fill_on_check=1)
    first = open_trade(execution)

    again = open_trade(execution)

    assert len(broker.submitted) == 1
    assert again == first


def test_missing_quotes_skip_an_attempt_without_crashing() -> None:
    execution, broker, _ = service()
    execution.market_data.leg_quotes.clear()  # type: ignore[attr-defined]

    assert open_trade(execution) is None
    assert broker.submitted == []


# Closing

OPENED = Position(SPREAD, Fill(2.10, 3, START), position_id=CID, exit_at=EXIT_AT)


def close_trade(
    execution: ExecutionService, at_natural: bool = False
) -> tuple[Position | None, Position | None]:
    return execution.close(
        OPENED, f"{CID}-close-1", deadline=START + timedelta(minutes=10), at_natural=at_natural
    )


def test_closes_at_the_mid_credit() -> None:
    execution, broker, repository = service()

    closed, remaining = close_trade(execution)

    assert remaining is None
    assert closed is not None and closed.exit is not None
    assert closed.exit.net_price == 2.10
    assert broker.submitted[0].intent is OrderIntent.CLOSE
    assert repository.open_positions() == ()


def test_closing_walks_the_credit_down() -> None:
    execution, broker, _ = service(fill_at_or_above=1.95)

    closed, _ = close_trade(execution)

    assert closed is not None and closed.exit is not None
    assert closed.exit.net_price == 1.95
    assert [price for _, price in broker.replaced] == [2.03, 1.95]


def test_at_natural_sends_one_order_at_the_natural_credit() -> None:
    execution, broker, _ = service()

    closed, _ = close_trade(execution, at_natural=True)

    assert closed is not None and closed.exit is not None
    assert closed.exit.net_price == 1.80
    assert broker.replaced == []


def test_a_partial_close_splits_the_position() -> None:
    execution, _, repository = service(partial_fill=1)

    closed, remaining = close_trade(execution)

    assert closed is not None and closed.entry.quantity == 1 and not closed.is_open
    assert remaining is not None and remaining.entry.quantity == 2 and remaining.is_open
    assert repository.open_positions() == (remaining,)


def test_an_unfilled_close_leaves_the_position_open() -> None:
    execution, _, _ = service(fill_on_check=None)

    closed, remaining = close_trade(execution)

    assert closed is None
    assert remaining == OPENED


# Closing leg by leg, when the broker refuses the mleg close
# Front 1.00/1.20 is bought back at its mid 1.10; back 3.00/3.40 is sold at its mid 3.20.


def legs_sent(broker: FakeBroker) -> list[SpreadLeg | None]:
    return [ticket.leg for ticket in broker.submitted]


def holding_the_calendar(broker: FakeBroker) -> None:
    broker.held[FRONT], broker.held[BACK] = -3, 3


def test_a_rejected_mleg_close_buys_back_the_front_then_sells_the_back() -> None:
    execution, broker, repository = service(reject_calendar_closes=True)
    holding_the_calendar(broker)

    closed, remaining = close_trade(execution)

    assert legs_sent(broker) == [None, SpreadLeg.FRONT, SpreadLeg.BACK]
    assert [t.client_order_id for t in broker.submitted[1:]] == [
        f"{CID}-close-1-front",
        f"{CID}-close-1-back",
    ]
    assert remaining is None
    assert closed is not None and closed.exit is not None and closed.front_closed is not None
    assert closed.front_closed.net_price == 1.10
    assert closed.exit.net_price == pytest.approx(3.20 - 1.10)
    assert repository.open_positions() == ()
    assert broker.held_contracts() == {}


def test_a_close_refused_after_it_was_accepted_also_legs_out() -> None:
    execution, broker, _ = service(reject_calendar_closes=True, reject_late=True)

    closed, remaining = close_trade(execution)

    assert legs_sent(broker) == [None, SpreadLeg.FRONT, SpreadLeg.BACK]
    assert broker.replaced == []  # a refused order is never re-priced
    assert remaining is None and closed is not None and not closed.is_open


def test_a_legged_exit_records_both_legs_market_at_their_fills() -> None:
    execution, _, _ = service(reject_calendar_closes=True)

    closed, _ = close_trade(execution)

    assert closed is not None and closed.exit is not None
    assert closed.exit.front is not None and closed.exit.front.ask == 1.20
    assert closed.exit.back is not None and closed.exit.back.bid == 3.00


def test_when_the_back_does_not_sell_the_position_is_long_back_only() -> None:
    execution, broker, repository = service(
        reject_calendar_closes=True, unfilled_legs=frozenset({SpreadLeg.BACK})
    )
    holding_the_calendar(broker)

    closed, remaining = close_trade(execution)

    assert closed is None
    assert remaining is not None and remaining.long_back_only
    assert repository.open_positions() == (remaining,)
    assert broker.held_contracts() == {BACK: 3}  # the short front is covered
    assert broker.cancelled  # the back order's walk ran out


def test_a_long_back_only_position_only_sells_the_back() -> None:
    execution, broker, repository = service()
    back_only = OPENED.close_front(Fill(1.10, 3, START))

    closed, remaining = execution.close(
        back_only, f"{CID}-close-2", START + timedelta(minutes=10), at_natural=False
    )

    assert legs_sent(broker) == [SpreadLeg.BACK]
    assert remaining is None
    assert closed is not None and closed.exit is not None
    assert closed.exit.net_price == pytest.approx(3.20 - 1.10)
    assert repository.open_positions() == ()


def test_selling_the_back_needs_no_quote_for_the_expired_front() -> None:
    execution, _, _ = service()
    del execution.market_data.leg_quotes[FRONT]  # type: ignore[attr-defined]
    back_only = OPENED.close_front(Fill(1.10, 3, START))

    closed, _ = execution.close(
        back_only, f"{CID}-close-2", START + timedelta(minutes=10), at_natural=False
    )

    assert closed is not None and not closed.is_open


def test_when_the_front_is_not_bought_back_the_back_is_left_alone() -> None:
    execution, broker, _ = service(
        reject_calendar_closes=True, unfilled_legs=frozenset({SpreadLeg.FRONT})
    )

    closed, remaining = close_trade(execution)

    assert legs_sent(broker) == [None, SpreadLeg.FRONT]
    assert closed is None
    assert remaining == OPENED


def test_a_partly_bought_back_front_splits_off_the_rest_as_a_calendar() -> None:
    execution, _, repository = service(reject_calendar_closes=True, partial_fill=1)

    closed, remaining = close_trade(execution)

    assert closed is not None and closed.entry.quantity == 1 and not closed.is_open
    assert remaining is not None and remaining.entry.quantity == 2
    assert not remaining.long_back_only
    assert repository.open_positions() == (remaining,)


def test_legs_at_natural_buy_at_the_ask_and_sell_at_the_bid() -> None:
    execution, broker, _ = service(reject_calendar_closes=True)

    closed, _ = close_trade(execution, at_natural=True)

    assert [t.limit_price for t in broker.submitted[1:]] == [1.20, 3.00]
    assert closed is not None and closed.exit is not None
    assert closed.exit.net_price == pytest.approx(3.00 - 1.20)
