from datetime import date

import pytest

from evbot.config import DEFAULT_CONFIG_PATH, load_config
from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.options import OptionChain
from evbot.domain.prices import PriceHistory
from evbot.domain.signals import FilterResult, Signal, Verdict
from evbot.strategy.evaluator import MarketContext, SignalEvaluator
from evbot.strategy.filters import (
    AVG_VOLUME,
    IV_RV,
    LIQUIDITY,
    REPORT_TIMING,
    SPREAD_AVAILABLE,
    TERM_SLOPE,
    AvgVolumeFilter,
    IvRvFilter,
    LiquidityFilter,
    SpreadAvailableFilter,
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
    RIGHTS,
    SPOT,
    WIDE,
    build_chain,
    build_history,
    quote_at_iv,
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
            max_iv_input_spread_pct=config.metrics.max_iv_input_spread_pct,
        ),
        selector=SpreadSelector(config.structure.back_expiry_offset_days),
        filters=(
            AvgVolumeFilter(filters.min_avg_volume_30d),
            IvRvFilter(filters.min_iv30_rv30),
            TermSlopeFilter(filters.max_term_slope),
            SpreadAvailableFilter(),
        ),
        liquidity=LiquidityFilter(),
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


def liquidity_result(signal: Signal) -> FilterResult:
    return next(result for result in signal.filters if result.name == LIQUIDITY)


def test_wide_spreads_do_not_change_the_verdict_and_are_recorded() -> None:
    signal = evaluate(
        build_chain(BACKWARDATED, half_spread=WIDE), build_history(CALM, LIQUID_VOLUME)
    )

    assert signal.verdict is Verdict.RECOMMENDED
    # half spread 15% of mid: (1.15 - 0.85) / 1.00 = 0.30
    assert "front_spread_pct=0.3000" in liquidity_result(signal).detail


def test_low_open_interest_does_not_change_the_verdict_and_is_recorded() -> None:
    signal = evaluate(
        build_chain(BACKWARDATED, open_interest=100), build_history(CALM, LIQUID_VOLUME)
    )

    assert signal.verdict is Verdict.RECOMMENDED
    assert "back_open_interest=100" in liquidity_result(signal).detail


def test_no_strike_listed_in_both_expiries_is_avoid() -> None:
    # Metrics work (each expiry has its own ATM), but front and back share no strike.
    front = (quote_at_iv(FRONT, 100.0, right, 0.90) for right in RIGHTS)
    later = (quote_at_iv(BACK, 105.0, right, 0.50) for right in RIGHTS)
    chain = OptionChain("AAPL", SPOT, AS_OF, (*front, *later))

    signal = evaluate(chain, build_history(CALM, LIQUID_VOLUME))

    assert signal.verdict is Verdict.AVOID
    assert signal.metrics is not None
    assert signal.spread is None
    assert [result.name for result in signal.failed()] == [SPREAD_AVAILABLE]
    assert liquidity_result(signal).detail == "no calendar spread"


def test_short_history_gives_avoid_explaining_why() -> None:
    long_history = build_history(CALM, LIQUID_VOLUME)
    short_history = PriceHistory("AAPL", long_history.bars[-5:])

    signal = evaluate(build_chain(BACKWARDATED), short_history)

    assert signal.verdict is Verdict.AVOID
    assert signal.metrics is None
    assert [result.name for result in signal.failed()] == ["metrics"]
    assert "bars" in signal.failed()[0].detail


def test_liquidity_is_recorded_even_when_metrics_cannot_be_computed() -> None:
    short_history = PriceHistory("AAPL", build_history(CALM, LIQUID_VOLUME).bars[-5:])

    signal = evaluate(build_chain(BACKWARDATED), short_history)

    assert "front_open_interest=1000" in liquidity_result(signal).detail


def test_empty_chain_gives_avoid_instead_of_raising() -> None:
    chain = OptionChain("AAPL", 100.0, AS_OF, ())

    signal = evaluate(chain, build_history(CALM, LIQUID_VOLUME))

    assert signal.verdict is Verdict.AVOID
    assert signal.metrics is None


def test_unknown_timing_is_avoid_but_still_records_metrics_and_liquidity() -> None:
    event = EarningsEvent("AAPL", EVENT.report_date, ReportTiming.UNKNOWN)
    context = MarketContext(event, build_chain(BACKWARDATED), build_history(CALM, LIQUID_VOLUME))

    signal = evaluator().evaluate(context)

    assert signal.verdict is Verdict.AVOID
    assert [result.name for result in signal.failed()] == [REPORT_TIMING]
    assert signal.metrics is not None
    assert "front_open_interest" in liquidity_result(signal).detail


def test_unusable_front_quotes_give_avoid_naming_the_spread() -> None:
    # half spread 30% of mid: spread_pct = 60%, above the 50% limit for IV inputs
    signal = evaluate(
        build_chain(BACKWARDATED, half_spread=0.30), build_history(CALM, LIQUID_VOLUME)
    )

    assert signal.verdict is Verdict.AVOID
    assert signal.metrics is None
    assert [result.name for result in signal.failed()] == ["unreliable_quotes"]
    assert "60.0%" in signal.failed()[0].detail
    assert "front_open_interest" in liquidity_result(signal).detail


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
