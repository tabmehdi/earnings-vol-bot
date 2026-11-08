from datetime import date

from evbot.strategy.rates import ConstantRate


def test_constant_rate_is_the_same_on_any_day() -> None:
    rates = ConstantRate(0.045)

    assert rates.rate_for(date(2020, 1, 2)) == 0.045
    assert rates.rate_for(date(2026, 11, 19)) == 0.045
