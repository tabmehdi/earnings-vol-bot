from datetime import UTC, date, datetime

import pytest

from evbot.domain.options import OptionContract, OptionRight
from evbot.domain.orders import OrderIntent, OrderStatus, OrderTicket
from evbot.domain.spreads import CalendarSpread, Leg, Side

SPREAD = CalendarSpread(
    Leg(OptionContract("AAPL", date(2026, 11, 20), 100.0, OptionRight.CALL), Side.SELL),
    Leg(OptionContract("AAPL", date(2026, 12, 18), 100.0, OptionRight.CALL), Side.BUY),
)
SUBMITTED = datetime(2026, 11, 19, 20, 45, tzinfo=UTC)


def ticket(
    status: OrderStatus = OrderStatus.WORKING,
    filled: int = 0,
    fill_price: float | None = None,
    quantity: int = 3,
) -> OrderTicket:
    return OrderTicket(
        order_id="o-1",
        client_order_id="AAPL-2026-11-20-open",
        spread=SPREAD,
        intent=OrderIntent.OPEN,
        quantity=quantity,
        limit_price=2.10,
        status=status,
        submitted_at=SUBMITTED,
        filled_quantity=filled,
        fill_price=fill_price,
    )


def test_a_working_order_is_not_done() -> None:
    assert not ticket().is_done


@pytest.mark.parametrize("status", [OrderStatus.CANCELLED, OrderStatus.REJECTED])
def test_cancelled_and_rejected_orders_are_done(status: OrderStatus) -> None:
    assert ticket(status).is_done


def test_a_filled_order_is_done_with_its_price() -> None:
    filled = ticket(OrderStatus.FILLED, filled=3, fill_price=2.05)

    assert filled.is_done
    assert filled.fill_price == 2.05


def test_a_partial_fill_keeps_working() -> None:
    partial = ticket(OrderStatus.PARTIALLY_FILLED, filled=1, fill_price=2.05)

    assert not partial.is_done


def test_filled_needs_the_whole_quantity_and_a_price() -> None:
    with pytest.raises(ValueError):
        ticket(OrderStatus.FILLED, filled=2, fill_price=2.05)
    with pytest.raises(ValueError):
        ticket(OrderStatus.FILLED, filled=3, fill_price=None)


def test_filled_quantity_must_fit_the_order() -> None:
    with pytest.raises(ValueError):
        ticket(OrderStatus.PARTIALLY_FILLED, filled=4, fill_price=2.0)


def test_any_fill_needs_a_price() -> None:
    with pytest.raises(ValueError):
        ticket(OrderStatus.CANCELLED, filled=1, fill_price=None)


def test_rejects_a_naive_submission_time() -> None:
    with pytest.raises(ValueError):
        OrderTicket(
            "o-1",
            "c",
            SPREAD,
            OrderIntent.OPEN,
            1,
            2.0,
            OrderStatus.WORKING,
            SUBMITTED.replace(tzinfo=None),
        )
