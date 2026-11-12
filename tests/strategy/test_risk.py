from datetime import UTC, date, datetime

import pytest

from evbot.domain.options import OptionContract, OptionQuote, OptionRight
from evbot.domain.spreads import CalendarSpread, Leg, Side
from evbot.domain.trades import Fill, Position
from evbot.strategy.risk import RiskManager

RISK = RiskManager(max_spread_cost_pct=0.35, max_open_positions=5, max_committed_pct=0.20)
EQUITY = 100_000.0
NOW = datetime(2026, 11, 19, 20, 45, tzinfo=UTC)


def calendar(symbol: str = "AAPL") -> CalendarSpread:
    front = OptionContract(symbol, date(2026, 11, 20), 100.0, OptionRight.CALL)
    back = OptionContract(symbol, date(2026, 12, 18), 100.0, OptionRight.CALL)
    return CalendarSpread(Leg(front, Side.SELL), Leg(back, Side.BUY))


def legs(front: tuple[float, float], back: tuple[float, float]) -> tuple[OptionQuote, OptionQuote]:
    spread = calendar()
    return (
        OptionQuote(spread.front.contract, *front, None, 500, 0),
        OptionQuote(spread.back.contract, *back, None, 500, 0),
    )


# Front 1.00/1.10, back 3.00/3.20: debit mid 3.10 - 1.05 = 2.05.
# Round trip = front width 0.10 + back width 0.20 = 0.30, which is 14.6% of 2.05.
TIGHT = legs((1.00, 1.10), (3.00, 3.20))


def held(symbol: str, debit: float = 2.00, quantity: int = 1) -> Position:
    return Position(calendar(symbol), Fill(debit, quantity, NOW))


def test_approves_a_tight_calendar_within_limits() -> None:
    decision = RISK.approve(calendar(), *TIGHT, contracts=5, open_positions=(), equity=EQUITY)

    assert decision.approved
    assert decision.reason == ""


def test_rejects_when_the_round_trip_spread_cost_is_too_large_a_share_of_the_debit() -> None:
    # Front 1.00/1.40, back 3.00/3.60: debit mid 3.30 - 1.20 = 2.10; round trip 0.40 + 0.60 = 1.00
    # 1.00 / 2.10 = 47.6% > 35%
    wide = legs((1.00, 1.40), (3.00, 3.60))

    decision = RISK.approve(calendar(), *wide, contracts=1, open_positions=(), equity=EQUITY)

    assert not decision.approved
    assert "47.6%" in decision.reason and "35%" in decision.reason


def test_a_round_trip_cost_exactly_at_the_limit_is_allowed() -> None:
    # Front 1.00/1.35, back 3.00/3.35: debit 3.175 - 1.175 = 2.00; round trip 0.35 + 0.35 = 35%
    exact = legs((1.00, 1.35), (3.00, 3.35))

    assert RISK.approve(calendar(), *exact, contracts=1, open_positions=(), equity=EQUITY).approved


def test_never_a_second_position_on_a_symbol_already_held() -> None:
    decision = RISK.approve(
        calendar(), *TIGHT, contracts=1, open_positions=(held("AAPL"),), equity=EQUITY
    )

    assert not decision.approved
    assert "AAPL" in decision.reason


def test_rejects_beyond_the_open_position_limit() -> None:
    five = tuple(held(s) for s in ("A", "B", "C", "D", "E"))

    decision = RISK.approve(calendar(), *TIGHT, contracts=1, open_positions=five, equity=EQUITY)

    assert not decision.approved
    assert "5 open positions" in decision.reason


def test_rejects_when_total_committed_would_exceed_its_share_of_equity() -> None:
    # Held: 2.00 * 90 * 100 = 18,000. New: 2.05 * 10 * 100 = 2,050. Total 20,050 > 20,000.
    decision = RISK.approve(
        calendar(), *TIGHT, contracts=10, open_positions=(held("MSFT", 2.00, 90),), equity=EQUITY
    )

    assert not decision.approved
    assert "20,050" in decision.reason


def test_a_non_positive_debit_is_rejected() -> None:
    inverted = legs((3.00, 3.20), (1.00, 1.10))

    assert not RISK.approve(
        calendar(), *inverted, contracts=1, open_positions=(), equity=EQUITY
    ).approved


def test_closed_positions_do_not_count() -> None:
    closed = held("AAPL").close(Fill(2.5, 1, NOW))

    assert RISK.approve(
        calendar(), *TIGHT, contracts=1, open_positions=(closed,), equity=EQUITY
    ).approved


@pytest.mark.parametrize("contracts", [0, -1])
def test_needs_at_least_one_contract(contracts: int) -> None:
    with pytest.raises(ValueError):
        RISK.approve(calendar(), *TIGHT, contracts=contracts, open_positions=(), equity=EQUITY)
