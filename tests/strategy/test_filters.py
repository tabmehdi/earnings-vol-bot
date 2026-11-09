from dataclasses import replace

import pytest

from evbot.domain.options import OptionRight
from evbot.domain.signals import SignalMetrics
from evbot.domain.spreads import CalendarSpread
from evbot.strategy.filters import (
    LIQUIDITY,
    SPREAD_AVAILABLE,
    AvgVolumeFilter,
    IvRvFilter,
    LiquidityFilter,
    SignalCandidate,
    SpreadAvailableFilter,
    TermSlopeFilter,
)
from evbot.strategy.selection import SpreadSelector
from tests.fixtures.market import BACK, BACKWARDATED, EVENT, FRONT, SPOT, build_chain

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


def test_spread_available_passes_with_a_spread() -> None:
    result = SpreadAvailableFilter().check(candidate())

    assert result.name == SPREAD_AVAILABLE
    assert result.passed


def test_spread_available_fails_without_a_spread() -> None:
    result = SpreadAvailableFilter().check(SignalCandidate(candidate().metrics, CHAIN, None))

    assert not result.passed
    assert result.detail


def chain_with_call_quotes(front_bid: float, front_ask: float, front_oi: int) -> SignalCandidate:
    quotes = tuple(
        replace(q, bid=front_bid, ask=front_ask, open_interest=front_oi)
        if q.contract.expiry == FRONT
        and q.contract.strike == SPOT
        and q.contract.right is OptionRight.CALL
        else replace(q, bid=2.00, ask=2.20, open_interest=9)
        if q.contract.expiry == BACK
        and q.contract.strike == SPOT
        and q.contract.right is OptionRight.CALL
        else q
        for q in CHAIN.quotes
    )
    return SignalCandidate(candidate().metrics, replace(CHAIN, quotes=quotes), spread())


def test_liquidity_reports_spread_and_open_interest_of_both_legs() -> None:
    # Front: 1.00/1.30 -> 0.30 / 1.15 = 0.2609. Back: 2.00/2.20 -> 0.20 / 2.10 = 0.0952.
    subject = chain_with_call_quotes(1.00, 1.30, front_oi=317)

    result = LiquidityFilter().describe(subject.chain, subject.spread)

    assert result.name == LIQUIDITY
    assert "front_spread_pct=0.2609" in result.detail
    assert "front_open_interest=317" in result.detail
    assert "back_spread_pct=0.0952" in result.detail
    assert "back_open_interest=9" in result.detail


def test_liquidity_is_informational_and_never_fails() -> None:
    # A 200% spread and zero open interest are recorded, but nothing is rejected.
    subject = chain_with_call_quotes(0.00, 1.00, front_oi=0)

    result = LiquidityFilter().describe(subject.chain, subject.spread)

    assert result.passed
    assert result.value is None
    assert result.threshold is None


def test_liquidity_without_a_spread_says_so_and_does_not_fail() -> None:
    result = LiquidityFilter().describe(CHAIN, None)

    assert result.passed
    assert result.detail == "no calendar spread"
