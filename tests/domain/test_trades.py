from datetime import date, datetime

import pytest

from evbot.domain.options import OptionContract, OptionRight
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
