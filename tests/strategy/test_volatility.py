import math
from datetime import date, timedelta

import pytest

from evbot.domain.prices import Bar, PriceHistory
from evbot.strategy.volatility import CloseToCloseEstimator, YangZhangEstimator

OHLC = (
    (100.0, 101.5, 99.5, 101.0),
    (101.0, 103.0, 100.5, 102.0),
    (102.5, 103.5, 100.5, 101.0),
    (101.5, 103.5, 101.0, 103.0),
    (103.0, 105.0, 102.5, 104.5),
    (104.0, 105.5, 103.0, 104.0),
)


def history(rows: tuple[tuple[float, float, float, float], ...]) -> PriceHistory:
    start = date(2026, 11, 2)
    bars = tuple(
        Bar(start + timedelta(days=i), o, h, low, c, 1_000) for i, (o, h, low, c) in enumerate(rows)
    )
    return PriceHistory("AAPL", bars)


def test_yang_zhang_matches_legacy_function() -> None:
    # 0.2852145716475709 is what yang_zhang(prices, window=5) returned when the legacy
    # calculator.py was run once on OHLC. That script is not part of the repo.
    estimator = YangZhangEstimator(window=5, trading_days_per_year=252)

    assert estimator.estimate(history(OHLC)) == pytest.approx(0.2852145716475709, abs=1e-12)


def test_yang_zhang_only_uses_the_last_window() -> None:
    estimator = YangZhangEstimator(window=5, trading_days_per_year=252)
    older = ((50.0, 90.0, 40.0, 80.0),)
    # Prepended bars move the dates, not the last six rows' values.
    longer = PriceHistory(
        "AAPL",
        (
            Bar(date(2026, 11, 1), *older[0], 1_000),
            *history(OHLC).bars,
        ),
    )

    assert estimator.estimate(longer) == pytest.approx(0.2852145716475709, abs=1e-12)


def test_yang_zhang_raises_on_short_history() -> None:
    with pytest.raises(ValueError):
        YangZhangEstimator(window=6, trading_days_per_year=252).estimate(history(OHLC))


def test_yang_zhang_rejects_window_below_two() -> None:
    with pytest.raises(ValueError):
        YangZhangEstimator(window=1, trading_days_per_year=252)


def test_close_to_close_is_annualized_sample_stdev_of_log_returns() -> None:
    # r1 = ln(110/100) = 0.0953102, r2 = ln(99/110) = -0.1053605, mean = -0.0050252
    # sample variance = ((r1 - mean)^2 + (r2 - mean)^2) / 1 = 0.0201344
    # sqrt(0.0201344) * sqrt(252) = 2.25253
    rows = (
        (100.0, 100.0, 100.0, 100.0),
        (110.0, 110.0, 110.0, 110.0),
        (99.0, 99.0, 99.0, 99.0),
    )
    r1, r2 = math.log(110 / 100), math.log(99 / 110)
    mean = (r1 + r2) / 2
    expected = math.sqrt((r1 - mean) ** 2 + (r2 - mean) ** 2) * math.sqrt(252)

    estimate = CloseToCloseEstimator(window=2, trading_days_per_year=252).estimate(history(rows))

    assert estimate == pytest.approx(expected)
    assert estimate == pytest.approx(2.25253, abs=1e-4)
