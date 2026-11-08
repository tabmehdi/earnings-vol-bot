from dataclasses import dataclass, replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.options import OptionChain, OptionQuote, OptionRight
from evbot.domain.prices import Bar, PriceHistory
from evbot.strategy.implied_vol import ImpliedVolSolver, black_scholes_price
from evbot.strategy.metrics import MetricsCalculator, days_to_expiry, years_to_expiry
from evbot.strategy.rates import ConstantRate
from tests.fixtures.market import (
    AS_OF,
    BACKWARDATED,
    EVENT,
    FRONT,
    RATE,
    SPOT,
    WEEK,
    build_chain,
    build_history,
    quote_at_iv,
)

NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class FixedVolatility:
    value: float

    def estimate(self, history: PriceHistory) -> float:
        return self.value


def calculator(rv: float = 0.25, volume_window: int = 30) -> MetricsCalculator:
    return MetricsCalculator(
        solver=ImpliedVolSolver(),
        rates=ConstantRate(RATE),
        volatility=FixedVolatility(rv),
        iv_days=30,
        volume_window=volume_window,
        slope_horizon_days=45,
    )


HISTORY = build_history(daily_move=0.04, volume=2_000_000)

# AS_OF is 15:45 New York, so every expiry is its whole-day count plus 15 min (0.0104167 days):
# 1.0104167 / 0.90, 8.0104167 / 0.60, 29.0104167 / 0.50, 57.0104167 / 0.45


def test_iv_rv_ratio_uses_solved_iv30() -> None:
    # IV30 = 0.50 - 0.05 * (30 - 29.0104167) / 28 = 0.4982329
    # 0.4982329 / 0.25 = 1.992932
    metrics = calculator().compute(build_chain(BACKWARDATED), HISTORY, EVENT)

    assert metrics.iv30_rv30 == pytest.approx(1.992932, abs=1e-5)


def test_term_slope_runs_from_front_expiry_to_45_days() -> None:
    # IV45 = 0.50 - 0.05 * (45 - 29.0104167) / 28 = 0.4714472
    # (0.4714472 - 0.90) / (45 - 1.0104167) = -0.00974214
    metrics = calculator().compute(build_chain(BACKWARDATED), HISTORY, EVENT)

    assert metrics.term_slope == pytest.approx(-0.00974214, abs=1e-6)


def test_friday_bmo_report_starts_the_term_structure_at_the_friday_expiry() -> None:
    # Same front as above: the Friday expiry at 1.0104167 days, IV 0.90
    event = EarningsEvent("AAPL", FRONT, ReportTiming.BMO)

    metrics = calculator().compute(build_chain(BACKWARDATED), HISTORY, event)

    assert metrics.term_slope == pytest.approx(-0.00974214, abs=1e-6)


@pytest.mark.parametrize("timing", [ReportTiming.AMC, ReportTiming.UNKNOWN])
def test_friday_amc_report_excludes_the_friday_expiry(timing: ReportTiming) -> None:
    # Front moves to next Friday: (0.4714472 - 0.60) / (45 - 8.0104167) = -0.0034754
    event = EarningsEvent("AAPL", FRONT, timing)

    metrics = calculator().compute(build_chain(BACKWARDATED), HISTORY, event)

    assert metrics.term_slope == pytest.approx(-0.0034754, abs=1e-6)


def test_days_to_expiry_counts_to_the_close_in_new_york() -> None:
    # Thursday 15:45 to Friday 16:00 is 1 day 15 minutes: 1 + 15/1440 = 1.0104167
    assert days_to_expiry(AS_OF, FRONT) == pytest.approx(1.0104167, abs=1e-6)


def test_years_to_expiry_is_calendar_time_over_365() -> None:
    # 1.0104167 / 365 = 0.00276826
    assert years_to_expiry(AS_OF, FRONT) == pytest.approx(0.00276826, abs=1e-7)


def test_days_to_expiry_accepts_any_timezone() -> None:
    # 15:45 New York in November is 20:45 UTC
    utc_as_of = datetime(2026, 11, 19, 20, 45, tzinfo=ZoneInfo("UTC"))

    assert days_to_expiry(utc_as_of, FRONT) == pytest.approx(1.0104167, abs=1e-6)


def test_days_to_expiry_counts_the_extra_hour_when_clocks_go_back() -> None:
    # US clocks go back on Nov 1 2026, so this span is 7 days 15 min plus 1 hour:
    # 7 + 75/1440 = 7.0520833
    as_of = datetime(2026, 10, 30, 15, 45, tzinfo=NEW_YORK)

    assert days_to_expiry(as_of, date(2026, 11, 6)) == pytest.approx(7.0520833, abs=1e-6)


def test_one_day_option_at_1545_solves_differently_from_a_whole_day() -> None:
    # Priced at 0.90 over 1 day 15 min. Solving as exactly 1 day needs a higher vol to
    # reach the same price: about 0.90 * sqrt(1.0104167) = 0.9047.
    years = years_to_expiry(AS_OF, FRONT)
    price = black_scholes_price(SPOT, SPOT, years, RATE, 0.90, OptionRight.CALL)
    solver = ImpliedVolSolver()

    with_minutes = solver.solve(price, SPOT, SPOT, years, OptionRight.CALL, RATE)
    whole_day = solver.solve(price, SPOT, SPOT, 1 / 365, OptionRight.CALL, RATE)

    assert with_minutes == pytest.approx(0.90, abs=1e-6)
    assert whole_day == pytest.approx(0.9047, abs=2e-3)


def test_avg_volume_is_the_mean_of_the_window() -> None:
    # Last two of 1M, 2M, 4M: (2M + 4M) / 2 = 3M
    bars = tuple(
        Bar(date(2026, 11, d), 100.0, 100.0, 100.0, 100.0, v)
        for d, v in ((2, 1_000_000), (3, 2_000_000), (4, 4_000_000))
    )
    history = PriceHistory("AAPL", bars)

    metrics = calculator(volume_window=2).compute(build_chain(BACKWARDATED), history, EVENT)

    assert metrics.avg_volume_30d == pytest.approx(3_000_000)


def straddle(expiry: date, call_vol: float, put_vol: float) -> tuple[OptionQuote, OptionQuote]:
    return (
        quote_at_iv(expiry, SPOT, OptionRight.CALL, call_vol),
        quote_at_iv(expiry, SPOT, OptionRight.PUT, put_vol),
    )


def test_atm_iv_averages_the_solved_call_and_put() -> None:
    # Front = (0.50 + 0.30) / 2 = 0.40, week = 0.60; IV45 is flat at 0.60
    # slope = (0.60 - 0.40) / (45 - 1.0104167) = 0.00454653
    quotes = (*straddle(FRONT, 0.50, 0.30), *straddle(WEEK, 0.60, 0.60))
    chain = OptionChain("AAPL", SPOT, AS_OF, quotes)

    metrics = calculator().compute(chain, HISTORY, EVENT)

    assert metrics.term_slope == pytest.approx(0.00454653, abs=1e-6)


def test_atm_iv_falls_back_to_the_side_that_solves() -> None:
    # Front put has no market, so front = call = 0.50
    # slope = (0.60 - 0.50) / (45 - 1.0104167) = 0.00227326
    call, put = straddle(FRONT, 0.50, 0.30)
    dead_put = replace(put, bid=0.0, ask=0.0)
    chain = OptionChain("AAPL", SPOT, AS_OF, (call, dead_put, *straddle(WEEK, 0.60, 0.60)))

    metrics = calculator().compute(chain, HISTORY, EVENT)

    assert metrics.term_slope == pytest.approx(0.00227326, abs=1e-6)


def test_raises_when_the_front_expiry_cannot_be_solved() -> None:
    call, put = straddle(FRONT, 0.50, 0.30)
    dead = (replace(call, bid=0.0, ask=0.0), replace(put, bid=0.0, ask=0.0))
    later = (*straddle(WEEK, 0.60, 0.60), *straddle(date(2026, 12, 4), 0.5, 0.5))
    chain = OptionChain("AAPL", SPOT, AS_OF, (*dead, *later))

    with pytest.raises(ValueError):
        calculator().compute(chain, HISTORY, EVENT)


def test_raises_when_no_expiry_follows_the_report() -> None:
    late = EarningsEvent("AAPL", date(2027, 6, 1), ReportTiming.AMC)

    with pytest.raises(ValueError):
        calculator().compute(build_chain(BACKWARDATED), HISTORY, late)


def test_raises_with_a_single_usable_expiry() -> None:
    chain = OptionChain("AAPL", SPOT, AS_OF, straddle(FRONT, 0.5, 0.5))

    with pytest.raises(ValueError):
        calculator().compute(chain, HISTORY, EVENT)


def test_raises_when_realized_vol_is_zero() -> None:
    with pytest.raises(ValueError):
        calculator(rv=0.0).compute(build_chain(BACKWARDATED), HISTORY, EVENT)
