from datetime import UTC, date, datetime

import pytest

from evbot.domain.options import OptionContract, OptionRight
from evbot.domain.orders import OrderIntent, OrderStatus, OrderTicket, SpreadLeg
from evbot.domain.spreads import CalendarSpread, Leg, Side
from evbot.ports.errors import BrokerError
from tests.fixtures.alpaca_trading_server import (
    BACK_OCC,
    FRONT_OCC,
    AlpacaTradingServer,
    leg_order_json,
    order_json,
)

FRONT = OptionContract("AAPL", date(2026, 11, 20), 100.0, OptionRight.CALL)
BACK = OptionContract("AAPL", date(2026, 12, 18), 100.0, OptionRight.CALL)
SPREAD = CalendarSpread(Leg(FRONT, Side.SELL), Leg(BACK, Side.BUY))
CID = "AAPL-2026-11-20-open"
CLOSE_CID = "AAPL-2026-11-20-close"


def known(order_id: str, leg: SpreadLeg | None = None) -> OrderTicket:
    """The ticket we hold for an order, as passed back to the broker to refresh it."""
    return OrderTicket(
        order_id,
        CID,
        SPREAD,
        OrderIntent.OPEN,
        3,
        2.10,
        OrderStatus.WORKING,
        datetime(2026, 11, 19, 20, 45, tzinfo=UTC),
        leg=leg,
    )


# Paper only


def test_refuses_to_start_unless_paper_mode_is_set() -> None:
    with pytest.raises(BrokerError, match="paper"):
        AlpacaTradingServer().broker(paper=False)


def test_refuses_the_live_trading_url_even_in_paper_mode() -> None:
    with pytest.raises(BrokerError, match="paper"):
        AlpacaTradingServer().broker(base_url="https://api.alpaca.markets")


# Submitting


def test_an_opening_order_is_a_positive_debit_selling_the_front_and_buying_the_back() -> None:
    server = AlpacaTradingServer()

    ticket = server.broker().submit(SPREAD, OrderIntent.OPEN, 3, 2.10, CID)

    body = server.bodies[-1]
    assert (body["order_class"], body["type"], body["time_in_force"]) == ("mleg", "limit", "day")
    assert (body["qty"], body["limit_price"], body["client_order_id"]) == ("3", "2.10", CID)
    assert body["legs"] == [
        {"symbol": FRONT_OCC, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
        {"symbol": BACK_OCC, "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
    ]
    assert (ticket.order_id, ticket.status, ticket.limit_price) == ("a1", OrderStatus.WORKING, 2.10)
    assert ticket.spread == SPREAD and ticket.intent is OrderIntent.OPEN


def test_a_closing_order_is_a_negative_credit_buying_back_the_front() -> None:
    server = AlpacaTradingServer()

    ticket = server.broker().submit(SPREAD, OrderIntent.CLOSE, 3, 1.80, f"{CID}-close")

    body = server.bodies[-1]
    assert body["limit_price"] == "-1.80"
    assert [leg["position_intent"] for leg in body["legs"]] == ["buy_to_close", "sell_to_close"]
    assert ticket.intent is OrderIntent.CLOSE
    assert ticket.limit_price == 1.80  # positive in the domain; the sign is Alpaca's


def test_a_refused_order_comes_back_rejected() -> None:
    server = AlpacaTradingServer(reject_with=(403, "insufficient options buying power"))

    ticket = server.broker().submit(SPREAD, OrderIntent.OPEN, 3, 2.10, CID)

    assert ticket.status is OrderStatus.REJECTED


def test_a_reused_client_order_id_is_an_error_not_a_second_order() -> None:
    server = AlpacaTradingServer()
    broker = server.broker()
    broker.submit(SPREAD, OrderIntent.OPEN, 3, 2.10, CID)

    with pytest.raises(BrokerError, match="unique"):
        broker.submit(SPREAD, OrderIntent.OPEN, 3, 2.10, CID)
    assert len(server.orders) == 1


def test_orders_are_never_retried_automatically() -> None:
    server = AlpacaTradingServer(reject_with=(500, "server error"))

    with pytest.raises(BrokerError):
        server.broker().submit(SPREAD, OrderIntent.OPEN, 3, 2.10, CID)
    assert len([r for r in server.requests if r.method == "POST"]) == 1


# Reading orders


@pytest.mark.parametrize(
    ("alpaca_status", "filled", "expected"),
    [
        ("new", "0", OrderStatus.WORKING),
        ("accepted", "0", OrderStatus.WORKING),
        ("pending_replace", "0", OrderStatus.WORKING),
        ("partially_filled", "1", OrderStatus.PARTIALLY_FILLED),
        ("filled", "3", OrderStatus.FILLED),
        ("canceled", "0", OrderStatus.CANCELLED),
        ("canceled", "1", OrderStatus.CANCELLED),
        ("expired", "0", OrderStatus.CANCELLED),
        ("rejected", "0", OrderStatus.REJECTED),
    ],
)
def test_statuses_map_to_ours(alpaca_status: str, filled: str, expected: OrderStatus) -> None:
    price = "2.05" if filled != "0" else None
    server = AlpacaTradingServer(
        orders={
            "a9": order_json("a9", status=alpaca_status, filled_qty=filled, filled_avg_price=price)
        }
    )

    ticket = server.broker().order(known("a9"))

    assert ticket.status is expected
    assert ticket.filled_quantity == int(filled)


def test_an_order_read_back_rebuilds_its_calendar_and_intent() -> None:
    server = AlpacaTradingServer(orders={"a9": order_json("a9")})

    ticket = server.broker().order(known("a9"))

    assert ticket.spread == SPREAD
    assert ticket.intent is OrderIntent.OPEN
    assert ticket.submitted_at == datetime(2026, 11, 19, 20, 45, 0, 123456, tzinfo=UTC)


def test_a_credit_fill_is_positive_in_the_domain() -> None:
    closing = order_json(
        "a9",
        status="filled",
        filled_qty="3",
        filled_avg_price="-1.85",
        limit_price="-1.80",
        opening=False,
    )
    server = AlpacaTradingServer(orders={"a9": closing})

    ticket = server.broker().order(known("a9"))

    assert (ticket.intent, ticket.fill_price, ticket.limit_price) == (OrderIntent.CLOSE, 1.85, 1.80)


def test_find_order_by_client_id() -> None:
    server = AlpacaTradingServer(orders={"a9": order_json("a9")})
    broker = server.broker()

    assert broker.find_order(CID, SPREAD) is not None
    assert broker.find_order("nobody", SPREAD) is None


# Changing orders


def test_replace_sends_the_signed_price_and_returns_the_new_order() -> None:
    server = AlpacaTradingServer()
    broker = server.broker()
    first = broker.submit(SPREAD, OrderIntent.CLOSE, 3, 2.10, f"{CID}-close")

    second = broker.replace(first, 2.03)

    assert server.bodies[-1] == {"limit_price": "-2.03"}
    assert second.order_id != first.order_id
    assert second.limit_price == 2.03


def test_buying_back_the_front_leg_is_a_single_leg_order_at_a_positive_price() -> None:
    server = AlpacaTradingServer()

    ticket = server.broker().submit(
        SPREAD, OrderIntent.CLOSE, 3, 1.10, f"{CLOSE_CID}-front", leg=SpreadLeg.FRONT
    )

    assert server.bodies[-1] == {
        "symbol": FRONT_OCC,
        "qty": "3",
        "side": "buy",
        "type": "limit",
        "limit_price": "1.10",
        "time_in_force": "day",
        "position_intent": "buy_to_close",
        "client_order_id": f"{CLOSE_CID}-front",
    }
    assert (ticket.leg, ticket.intent, ticket.limit_price) == (
        SpreadLeg.FRONT,
        OrderIntent.CLOSE,
        1.10,
    )
    assert ticket.spread == SPREAD


def test_selling_the_back_leg_is_a_single_leg_sale_at_a_positive_price() -> None:
    server = AlpacaTradingServer()

    ticket = server.broker().submit(
        SPREAD, OrderIntent.CLOSE, 3, 3.20, f"{CLOSE_CID}-back", leg=SpreadLeg.BACK
    )

    body = server.bodies[-1]
    assert (body["symbol"], body["side"], body["position_intent"]) == (
        BACK_OCC,
        "sell",
        "sell_to_close",
    )
    assert body["limit_price"] == "3.20"
    assert ticket.leg is SpreadLeg.BACK


def test_a_leg_order_read_back_names_its_leg_and_fill() -> None:
    filled = leg_order_json("a9", status="filled", filled_qty="3", filled_avg_price="1.12")
    server = AlpacaTradingServer(orders={"a9": filled})

    ticket = server.broker().order(known("a9", SpreadLeg.FRONT))

    assert (ticket.leg, ticket.intent, ticket.status) == (
        SpreadLeg.FRONT,
        OrderIntent.CLOSE,
        OrderStatus.FILLED,
    )
    assert (ticket.fill_price, ticket.spread) == (1.12, SPREAD)


def test_find_order_works_out_which_leg_a_single_leg_order_trades() -> None:
    back = leg_order_json("a9", "c-back", BACK_OCC, "sell", "sell_to_close", limit_price="3.20")
    server = AlpacaTradingServer(orders={"a9": back})

    ticket = server.broker().find_order("c-back", SPREAD)

    assert ticket is not None and ticket.leg is SpreadLeg.BACK


def test_a_leg_order_for_another_contract_is_an_error() -> None:
    other = leg_order_json("a9", symbol="AAPL261120C00105000")
    server = AlpacaTradingServer(orders={"a9": other})

    with pytest.raises(BrokerError, match="not a leg"):
        server.broker().order(known("a9", SpreadLeg.FRONT))


def test_replacing_a_leg_order_sends_a_positive_price() -> None:
    server = AlpacaTradingServer()
    broker = server.broker()
    first = broker.submit(SPREAD, OrderIntent.CLOSE, 3, 1.10, "c-front", leg=SpreadLeg.FRONT)

    second = broker.replace(first, 1.15)

    assert server.bodies[-1] == {"limit_price": "1.15"}
    assert (second.leg, second.limit_price) == (SpreadLeg.FRONT, 1.15)


def test_cancel_tolerates_an_order_that_already_filled() -> None:
    server = AlpacaTradingServer(
        orders={"a9": order_json("a9", status="filled", filled_qty="3", filled_avg_price="2.1")}
    )

    server.broker().cancel("a9")

    assert server.orders["a9"]["status"] == "filled"


def test_cancel_cancels_a_working_order() -> None:
    server = AlpacaTradingServer(orders={"a9": order_json("a9")})

    server.broker().cancel("a9")

    assert server.orders["a9"]["status"] == "canceled"


# Account


def test_equity_comes_from_the_account() -> None:
    assert AlpacaTradingServer().broker().account_equity() == 100_000.55


def test_held_option_contracts_are_signed_and_stocks_ignored() -> None:
    positions = [
        {"symbol": FRONT_OCC, "qty": "-3", "side": "short", "asset_class": "us_option"},
        {"symbol": BACK_OCC, "qty": "3", "side": "long", "asset_class": "us_option"},
        {"symbol": "AAPL", "qty": "10", "side": "long", "asset_class": "us_equity"},
    ]

    held = AlpacaTradingServer(positions=positions).broker().held_contracts()

    assert held == {FRONT: -3, BACK: 3}
