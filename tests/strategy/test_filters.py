from dataclasses import replace

import pytest

from evbot.domain.options import OptionRight
from evbot.domain.signals import SignalMetrics
from evbot.domain.spreads import CalendarSpread
from evbot.strategy.filters import (
    AvgVolumeFilter,
    IvRvFilter,
    LiquidityFilter,
    SignalCandidate,
    TermSlopeFilter,
)
from evbot.strategy.selection import SpreadSelector
from tests.fixtures.market import BACK, BACKWARDATED, EVENT, SPOT, TIGHT, build_chain

CHAIN = build_chain(BACKWARDATED)


def spread() -> CalendarSpread:
    selected = SpreadSelector(back_offset_days=30).select(CHAIN, EVENT)
    assert selected is not None
    return selected


def candidate(
    avg_volume: float = 2_000_000, iv_rv: float = 1.5, slope: float = -0.01
) -> SignalCandidate:
    return SignalCandidate(SignalMetrics(avg_volume, iv_rv, slope), CHAIN, spread())


@pytest.mark.parametrize(
    ("volume", "passed"), [(1_500_000, True), (1_499_999, False), (2_000_000, True)]
)
def test_avg_volume_is_inclusive_at_the_minimum(volume: float, passed: bool) -> None:
    result = AvgVolumeFilter(1_500_000).check(candidate(avg_volume=volume))

    assert result.passed is passed
    assert result.value == volume
    assert result.threshold == 1_500_000


@pytest.mark.parametrize(("ratio", "passed"), [(1.25, True), (1.2499, False)])
def test_iv_rv_is_inclusive_at_the_minimum(ratio: float, passed: bool) -> None:
    assert IvRvFilter(1.25).check(candidate(iv_rv=ratio)).passed is passed


@pytest.mark.parametrize(("slope", "passed"), [(-0.00406, True), (-0.00405, False)])
def test_term_slope_is_inclusive_at_the_maximum(slope: float, passed: bool) -> None:
    assert TermSlopeFilter(-0.00406).check(candidate(slope=slope)).passed is passed


def liquidity() -> LiquidityFilter:
    return LiquidityFilter(max_spread_pct=0.10, min_open_interest=500)


def with_changed_back_call(**changes: float) -> SignalCandidate:
    quotes = tuple(
        replace(q, **changes)  # type: ignore[arg-type]
        if q.contract.expiry == BACK
        and q.contract.strike == SPOT
        and q.contract.right is OptionRight.CALL
        else q
        for q in CHAIN.quotes
    )
    return SignalCandidate(candidate().metrics, replace(CHAIN, quotes=quotes), spread())


def test_liquidity_passes_tight_deep_legs_and_reports_worst_spread() -> None:
    result = liquidity().check(candidate())

    assert result.passed
    # bid = mid * 0.99, ask = mid * 1.01, so (ask - bid) / mid = 0.02
    assert result.value == pytest.approx(2 * TIGHT)
    assert result.threshold == 0.10


def test_liquidity_fails_when_one_leg_is_wide() -> None:
    # bid 1.00, ask 1.30: 0.30 / 1.15 = 0.2609 > 0.10
    result = liquidity().check(with_changed_back_call(bid=1.00, ask=1.30))

    assert not result.passed
    assert result.value == pytest.approx(0.30 / 1.15)


def test_liquidity_fails_on_low_open_interest() -> None:
    result = liquidity().check(with_changed_back_call(open_interest=499))

    assert not result.passed
    assert "499" in result.detail


def test_liquidity_passes_exactly_at_the_open_interest_minimum() -> None:
    assert liquidity().check(with_changed_back_call(open_interest=500)).passed


def test_liquidity_fails_without_a_spread() -> None:
    result = liquidity().check(SignalCandidate(candidate().metrics, CHAIN, None))

    assert not result.passed
    assert result.detail == "no valid calendar spread"
