from datetime import UTC, date, datetime

import pytest

from evbot.domain.options import OptionContract, OptionQuote, OptionRight
from evbot.domain.spreads import CalendarSpread, Leg, Side
from evbot.domain.trades import Fill, Position
from evbot.strategy.risk import RiskManager

RISK = RiskManager(
    max_spread_cost_pct=0.35,
    max_open_positions=5,
    max_committed_pct=0.20,
    enforce_spread_cost=True,
)
LOG_ONLY = RiskManager(
    max_spread_cost_pct=0.35,
    max_open_positions=5,
    max_committed_pct=0.20,
    enforce_spread_cost=False,
)
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


def test_committed_capital_can_be_checked_at_the_debit_actually_paid() -> None:
    # 20% of 10,000 = 2,000. At the 2.05 mid, 9 contracts commit 1,845; at the 2.30 natural,
    # 2,070, above the limit.
    at_mid = RISK.approve(calendar(), *TIGHT, contracts=9, open_positions=(), equity=10_000)
    at_natural = RISK.approve(
        calendar(), *TIGHT, contracts=9, open_positions=(), equity=10_000, debit=2.30
    )

    assert at_mid.approved
    assert not at_natural.approved and "committed" in at_natural.reason


# Log-only spread cost (paper: indicative widths are inflated, ADR 0007)

# Front 1.00/1.60 (mid 1.30), back 2.70/3.90 (mid 3.30): debit mid 2.00.
# Round trip = 0.60 + 1.20 = 1.80, which is 90% of 2.00.
WIDE = legs((1.00, 1.60), (2.70, 3.90))


def test_a_90_percent_spread_cost_is_recorded_but_not_rejected_when_not_enforced() -> None:
    decision = LOG_ONLY.approve(calendar(), *WIDE, contracts=1, open_positions=(), equity=EQUITY)

    assert decision.approved
    assert decision.spread_cost is not None
    assert decision.spread_cost.share == pytest.approx(0.90)
    assert "90.0%" in decision.warning and "not enforced" in decision.warning


def test_a_90_percent_spread_cost_is_rejected_when_enforced() -> None:
    decision = RISK.approve(calendar(), *WIDE, contracts=1, open_positions=(), equity=EQUITY)

    assert not decision.approved
    assert "round-trip spread cost" in decision.reason
    assert decision.spread_cost is not None and decision.spread_cost.share == pytest.approx(0.90)


def test_the_other_checks_still_apply_when_spread_cost_is_not_enforced() -> None:
    decision = LOG_ONLY.approve(
        calendar("AAPL"), *WIDE, contracts=1, open_positions=(held("AAPL"),), equity=EQUITY
    )

    assert not decision.approved and "already held" in decision.reason


def test_an_approval_within_the_limit_carries_its_estimate_and_no_warning() -> None:
    decision = LOG_ONLY.approve(calendar(), *TIGHT, contracts=1, open_positions=(), equity=EQUITY)

    assert decision.approved and decision.warning == ""
    assert decision.spread_cost is not None
    assert (decision.spread_cost.round_trip, decision.spread_cost.mid_debit) == pytest.approx(
        (0.30, 2.05)
    )
