from datetime import date

import pytest

from evbot.domain.options import OptionContract, OptionQuote, OptionRight
from evbot.domain.orders import OrderIntent
from evbot.strategy.pricing import MidWalkPolicy

POLICY = MidWalkPolicy(steps=5, step_seconds=20, entry_cap_fraction=0.5)


def quote(expiry: date, bid: float, ask: float) -> OptionQuote:
    contract = OptionContract("AAPL", expiry, 100.0, OptionRight.CALL)
    return OptionQuote(contract, bid, ask, None, open_interest=500, volume=0)


# Front 1.00/1.20 (mid 1.10), back 3.00/3.40 (mid 3.20).
FRONT = quote(date(2026, 11, 20), 1.00, 1.20)
BACK = quote(date(2026, 12, 18), 3.00, 3.40)


def limits(intent: OrderIntent) -> list[float]:
    return [POLICY.limit(intent, attempt, FRONT, BACK) for attempt in range(POLICY.steps)]


def test_opening_starts_at_the_mid_debit() -> None:
    # back mid 3.20 - front mid 1.10 = 2.10
    assert POLICY.limit(OrderIntent.OPEN, 0, FRONT, BACK) == pytest.approx(2.10)


def test_opening_walks_up_to_the_cap_in_equal_steps() -> None:
    # Natural debit = back ask 3.40 - front bid 1.00 = 2.40; half-width = 2.40 - 2.10 = 0.30
    # Cap = 2.10 + 0.5 * 0.30 = 2.25; steps of 0.15 / 4 = 0.0375, floored to cents.
    assert limits(OrderIntent.OPEN) == pytest.approx([2.10, 2.13, 2.17, 2.21, 2.25])


def test_opening_never_pays_more_than_the_cap() -> None:
    assert max(limits(OrderIntent.OPEN)) <= 2.25 + 1e-9


def test_closing_starts_at_the_mid_credit() -> None:
    assert POLICY.limit(OrderIntent.CLOSE, 0, FRONT, BACK) == pytest.approx(2.10)


def test_closing_ends_at_the_natural_credit() -> None:
    # Natural credit = back bid 3.00 - front ask 1.20 = 1.80
    assert limits(OrderIntent.CLOSE) == pytest.approx([2.10, 2.03, 1.95, 1.88, 1.80])


def test_closing_at_natural_jumps_straight_to_it() -> None:
    assert POLICY.natural(OrderIntent.CLOSE, FRONT, BACK) == pytest.approx(1.80)


def test_a_credit_is_never_below_one_cent() -> None:
    inverted = quote(date(2026, 12, 18), 0.80, 1.40)

    assert POLICY.natural(OrderIntent.CLOSE, FRONT, inverted) == 0.01


def test_attempts_outside_the_walk_are_an_error() -> None:
    with pytest.raises(ValueError):
        POLICY.limit(OrderIntent.OPEN, 5, FRONT, BACK)


@pytest.mark.parametrize("steps", [0, 1])
def test_needs_at_least_two_steps(steps: int) -> None:
    with pytest.raises(ValueError):
        MidWalkPolicy(steps=steps, step_seconds=20, entry_cap_fraction=0.5)
