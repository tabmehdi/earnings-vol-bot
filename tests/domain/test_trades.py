from dataclasses import replace
from datetime import date, datetime

import pytest

from evbot.domain.options import OptionContract, OptionRight
from evbot.domain.signals import LegPrices
from evbot.domain.spreads import CalendarSpread, Leg, Side
from evbot.domain.trades import Fill, Position

SPREAD = CalendarSpread(
    Leg(OptionContract("AAPL", date(2026, 11, 20), 100.0, OptionRight.CALL), Side.SELL),
    Leg(OptionContract("AAPL", date(2026, 12, 18), 100.0, OptionRight.CALL), Side.BUY),
)
OPENED = datetime(2026, 11, 19, 15, 45)
CLOSED = datetime(2026, 11, 20, 9, 45)


def open_position(price: float = 2.00, quantity: int = 3) -> Position:
    return Position(SPREAD, Fill(price, quantity, OPENED))


def test_new_position_is_open() -> None:
    assert open_position().is_open


def test_cost_is_debit_times_quantity_times_multiplier() -> None:
    # 2.00 * 3 * 100 = 600
    assert open_position().cost == pytest.approx(600.0)


def test_pnl_on_a_winning_calendar() -> None:
    # bought at 2.00, sold at 2.50: (2.50 - 2.00) * 3 * 100 = 150
    closed = open_position().close(Fill(2.50, 3, CLOSED))

    assert closed.pnl() == pytest.approx(150.0)


def test_pnl_on_a_losing_calendar() -> None:
    # bought at 2.00, sold at 1.20: (1.20 - 2.00) * 3 * 100 = -240
    closed = open_position().close(Fill(1.20, 3, CLOSED))

    assert closed.pnl() == pytest.approx(-240.0)


def test_close_returns_a_new_position_and_keeps_the_original_open() -> None:
    position = open_position()

    closed = position.close(Fill(2.50, 3, CLOSED))

    assert position.is_open
    assert not closed.is_open


def test_pnl_raises_while_position_is_open() -> None:
    with pytest.raises(ValueError):
        open_position().pnl()


def test_close_rejects_a_different_quantity() -> None:
    with pytest.raises(ValueError):
        open_position(quantity=3).close(Fill(2.50, 2, CLOSED))


def test_close_rejects_closing_twice() -> None:
    closed = open_position().close(Fill(2.50, 3, CLOSED))

    with pytest.raises(ValueError):
        closed.close(Fill(2.60, 3, CLOSED))


def test_exit_cannot_precede_entry() -> None:
    with pytest.raises(ValueError):
        open_position().close(Fill(2.50, 3, datetime(2026, 11, 19, 10, 0)))


def test_fill_rejects_non_positive_quantity() -> None:
    with pytest.raises(ValueError):
        Fill(2.00, 0, OPENED)


# Market prices at fill time, position identity and exit state

FRONT_AT_FILL = LegPrices(1.00, 1.10, 1.05, None, OPENED)
BACK_AT_FILL = LegPrices(3.00, 3.20, 3.10, None, OPENED)


def test_a_fill_keeps_both_legs_market_at_fill_time() -> None:
    fill = Fill(2.05, 3, OPENED, front=FRONT_AT_FILL, back=BACK_AT_FILL)

    assert fill.front == FRONT_AT_FILL
    assert fill.back == BACK_AT_FILL
    assert fill.back.mid - fill.front.mid == pytest.approx(2.05)


def test_split_keeps_the_entry_price_on_both_parts() -> None:
    position = replace(open_position(quantity=3), position_id="p-1")

    closing, staying = position.split(1)

    assert (closing.entry.quantity, staying.entry.quantity) == (1, 2)
    assert closing.entry.net_price == staying.entry.net_price == 2.00
    assert closing.position_id == "p-1:1"
    assert staying.position_id == "p-1"


@pytest.mark.parametrize("quantity", [0, 3, 4])
def test_split_needs_a_part_smaller_than_the_position(quantity: int) -> None:
    with pytest.raises(ValueError):
        open_position(quantity=3).split(quantity)


def test_an_exit_failure_is_marked_and_still_open() -> None:
    failed = open_position().mark_exit_failed()

    assert failed.exit_failed
    assert failed.is_open


def test_symbol_is_the_spreads_underlying() -> None:
    assert open_position().symbol == "AAPL"


# Closing leg by leg

FRONT_BOUGHT = Fill(0.90, 3, CLOSED, front=FRONT_AT_FILL)


def test_buying_back_the_front_leaves_a_long_back_only_position() -> None:
    back_only = open_position().close_front(FRONT_BOUGHT)

    assert back_only.long_back_only
    assert back_only.is_open
    assert back_only.front_closed == FRONT_BOUGHT


def test_a_calendar_position_is_not_long_back_only() -> None:
    assert not open_position().long_back_only


def test_a_closed_position_is_not_long_back_only() -> None:
    closed = open_position().close_front(FRONT_BOUGHT).close(Fill(1.50, 3, CLOSED))

    assert not closed.long_back_only
    assert not closed.is_open


def test_the_front_can_only_be_bought_back_once() -> None:
    with pytest.raises(ValueError):
        open_position().close_front(FRONT_BOUGHT).close_front(FRONT_BOUGHT)


def test_the_front_buy_back_must_cover_the_whole_position() -> None:
    with pytest.raises(ValueError):
        open_position(quantity=3).close_front(Fill(0.90, 2, CLOSED))


def test_a_legged_exit_can_cost_more_than_it_brings_in() -> None:
    # Front bought back at 2.40, back sold at 2.10: net exit -0.30; (-0.30 - 2.00) * 3 * 100
    closed = open_position().close(Fill(-0.30, 3, CLOSED))

    assert closed.pnl() == pytest.approx(-690.0)


def test_an_entry_price_cannot_be_negative() -> None:
    with pytest.raises(ValueError):
        Position(SPREAD, Fill(-0.10, 3, OPENED))


def test_split_divides_a_front_buy_back_too() -> None:
    back_only = replace(open_position(quantity=3), position_id="p-1").close_front(FRONT_BOUGHT)

    part, rest = back_only.split(1)

    assert part.front_closed is not None and part.front_closed.quantity == 1
    assert rest.front_closed is not None and rest.front_closed.quantity == 2
    assert part.front_closed.net_price == rest.front_closed.net_price == 0.90
