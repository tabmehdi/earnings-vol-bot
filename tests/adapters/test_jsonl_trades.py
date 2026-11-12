from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from evbot.adapters.jsonl_trade_repository import JsonlTradeRepository
from evbot.domain.options import OptionContract, OptionRight
from evbot.domain.orders import OrderIntent, OrderStatus, OrderTicket
from evbot.domain.signals import LegPrices
from evbot.domain.spreads import CalendarSpread, Leg, Side
from evbot.domain.trades import Fill, Position

OPENED = datetime(2026, 11, 19, 20, 45, tzinfo=UTC)
CLOSED = datetime(2026, 11, 20, 14, 45, tzinfo=UTC)
SPREAD = CalendarSpread(
    Leg(OptionContract("AAPL", date(2026, 11, 20), 100.0, OptionRight.CALL), Side.SELL),
    Leg(OptionContract("AAPL", date(2026, 12, 18), 100.0, OptionRight.CALL), Side.BUY),
)
FILL = Fill(
    2.05,
    3,
    OPENED,
    front=LegPrices(1.00, 1.10, 1.05, None, OPENED),
    back=LegPrices(3.00, 3.20, 3.10, None, OPENED - timedelta(seconds=2)),
)
POSITION = Position(SPREAD, FILL, position_id="AAPL-2026-11-20", exit_at=CLOSED)


def repository(tmp_path: Path) -> JsonlTradeRepository:
    return JsonlTradeRepository(tmp_path / "signals", tmp_path / "trades")


def test_an_open_position_round_trips(tmp_path: Path) -> None:
    repo = repository(tmp_path)

    repo.save_position(POSITION)

    assert repo.open_positions() == (POSITION,)


def test_saving_again_replaces_the_position(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    repo.save_position(POSITION)

    repo.save_position(POSITION.close(Fill(2.40, 3, CLOSED)))

    assert repo.open_positions() == ()


def test_an_exit_failed_position_is_still_open(tmp_path: Path) -> None:
    repo = repository(tmp_path)

    repo.save_position(POSITION.mark_exit_failed())

    (found,) = repo.open_positions()
    assert found.exit_failed


def test_a_split_position_id_is_stored_under_a_safe_file_name(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    closing, staying = POSITION.split(1)

    repo.save_position(closing.close(Fill(2.40, 1, CLOSED)))
    repo.save_position(staying)

    assert repo.open_positions() == (staying,)
    assert not any(":" in p.name for p in (tmp_path / "trades" / "positions").iterdir())


def test_orders_are_logged_by_new_york_day_in_order(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    working = OrderTicket(
        "o-1",
        "AAPL-2026-11-20-open",
        SPREAD,
        OrderIntent.OPEN,
        3,
        2.05,
        OrderStatus.WORKING,
        OPENED,
    )
    filled = replace(working, status=OrderStatus.FILLED, filled_quantity=3, fill_price=2.05)

    repo.save_order(working)
    repo.save_order(filled)

    assert repo.orders_on(date(2026, 11, 19)) == (working, filled)


def test_no_positions_or_orders_yet(tmp_path: Path) -> None:
    repo = repository(tmp_path)

    assert repo.open_positions() == ()
    assert repo.orders_on(date(2026, 11, 19)) == ()
