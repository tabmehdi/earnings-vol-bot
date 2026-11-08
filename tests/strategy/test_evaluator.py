from datetime import date

import pytest

from evbot.config import DEFAULT_CONFIG_PATH, load_config
from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.options import OptionChain
from evbot.domain.prices import PriceHistory
from evbot.domain.signals import Signal, Verdict
from evbot.strategy.evaluator import MarketContext, SignalEvaluator
from evbot.strategy.filters import (
    AVG_VOLUME,
    IV_RV,
    LIQUIDITY,
    TERM_SLOPE,
    AvgVolumeFilter,
    IvRvFilter,
    LiquidityFilter,
    TermSlopeFilter,
)
from evbot.strategy.implied_vol import ImpliedVolSolver
from evbot.strategy.metrics import MetricsCalculator
from evbot.strategy.rates import ConstantRate
from evbot.strategy.selection import SpreadSelector
from evbot.strategy.verdict import DefaultVerdictPolicy
from evbot.strategy.volatility import YangZhangEstimator
from tests.fixtures.market import (
    AS_OF,
    BACK,
    BACKWARDATED,
    CONTANGO,
    EVENT,
    FRONT,
    WIDE,
    build_chain,
    build_history,
)

# Daily log moves of 4% give Yang-Zhang RV = 0.2425, so IV30/RV30 is about 0.498 / 0.2425 = 2.05.
CALM = 0.04
# Daily log moves of 12% give RV = 0.727, so IV30/RV30 is about 0.498 / 0.727 = 0.69.
WILD = 0.12
LIQUID_VOLUME = 2_000_000
THIN_VOLUME = 1_000_000


def evaluator() -> SignalEvaluator:
    config = load_config(DEFAULT_CONFIG_PATH)
    filters = config.filters
    return SignalEvaluator(
        metrics=MetricsCalculator(
            solver=ImpliedVolSolver(),
            rates=ConstantRate(config.pricing.risk_free_rate),
            volatility=YangZhangEstimator(
                config.metrics.rv_window, config.metrics.trading_days_per_year
            ),
            iv_days=config.metrics.iv_days,
            volume_window=config.metrics.volume_window,
            slope_horizon_days=filters.term_slope_horizon_days,
        ),
        selector=SpreadSelector(config.structure.back_expiry_offset_days),
        filters=(
            AvgVolumeFilter(filters.min_avg_volume_30d),
            IvRvFilter(filters.min_iv30_rv30),
            TermSlopeFilter(filters.max_term_slope),
            LiquidityFilter(filters.max_leg_spread_pct, filters.min_open_interest),
        ),
        policy=DefaultVerdictPolicy(),
    )


def evaluate(chain: OptionChain, history: PriceHistory) -> Signal:
    return evaluator().evaluate(MarketContext(EVENT, chain, history))


def chain_recommended() -> Signal:
    return evaluate(build_chain(BACKWARDATED), build_history(CALM, LIQUID_VOLUME))


def passed_by_name(signal: Signal) -> dict[str, bool]:
    return {result.name: result.passed for result in signal.filters}


def test_chain_recommended_passes_every_filter() -> None:
    signal = chain_recommended()

    assert signal.verdict is Verdict.RECOMMENDED
    assert all(passed_by_name(signal).values())
    assert signal.spread is not None
    assert signal.spread.front.contract.expiry == FRONT
    assert signal.spread.back.contract.expiry == BACK


def test_chain_consider_fails_only_volume() -> None:
    signal = evaluate(build_chain(BACKWARDATED), build_history(CALM, THIN_VOLUME))

    assert signal.verdict is Verdict.CONSIDER
    assert [result.name for result in signal.failed()] == [AVG_VOLUME]


def test_chain_consider_fails_only_iv_rv() -> None:
    signal = evaluate(build_chain(BACKWARDATED), build_history(WILD, LIQUID_VOLUME))

    assert signal.verdict is Verdict.CONSIDER
    assert [result.name for result in signal.failed()] == [IV_RV]


def test_chain_avoid_has_a_flat_term_structure() -> None:
    signal = evaluate(build_chain(CONTANGO), build_history(CALM, LIQUID_VOLUME))

    assert signal.verdict is Verdict.AVOID
    assert [result.name for result in signal.failed()] == [TERM_SLOPE]


def test_liquidity_failure_is_avoid_even_when_all_else_passes() -> None:
    signal = evaluate(
        build_chain(BACKWARDATED, half_spread=WIDE), build_history(CALM, LIQUID_VOLUME)
    )

    assert signal.verdict is Verdict.AVOID
    assert [result.name for result in signal.failed()] == [LIQUIDITY]
    assert signal.metrics is not None


def test_low_open_interest_is_avoid() -> None:
    signal = evaluate(
        build_chain(BACKWARDATED, open_interest=100), build_history(CALM, LIQUID_VOLUME)
    )

    assert signal.verdict is Verdict.AVOID
    assert [result.name for result in signal.failed()] == [LIQUIDITY]


def test_short_history_gives_avoid_explaining_why() -> None:
    long_history = build_history(CALM, LIQUID_VOLUME)
    short_history = PriceHistory("AAPL", long_history.bars[-5:])

    signal = evaluate(build_chain(BACKWARDATED), short_history)

    assert signal.verdict is Verdict.AVOID
    assert signal.metrics is None
    assert [result.name for result in signal.filters] == ["metrics"]
    assert "bars" in signal.filters[0].detail


def test_empty_chain_gives_avoid_instead_of_raising() -> None:
    chain = OptionChain("AAPL", 100.0, AS_OF, ())

    signal = evaluate(chain, build_history(CALM, LIQUID_VOLUME))

    assert signal.verdict is Verdict.AVOID
    assert signal.metrics is None


def test_as_of_comes_from_the_chain() -> None:
    assert chain_recommended().as_of == AS_OF


def test_context_rejects_a_chain_for_another_symbol() -> None:
    msft = EarningsEvent("MSFT", date(2026, 11, 19), ReportTiming.AMC)

    with pytest.raises(ValueError):
        MarketContext(msft, build_chain(BACKWARDATED), build_history(CALM, LIQUID_VOLUME))


def test_context_rejects_history_for_another_symbol() -> None:
    other = PriceHistory("MSFT", build_history(CALM, LIQUID_VOLUME).bars)

    with pytest.raises(ValueError):
        MarketContext(EVENT, build_chain(BACKWARDATED), other)
