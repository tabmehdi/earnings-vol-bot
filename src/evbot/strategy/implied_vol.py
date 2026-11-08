import math
from dataclasses import dataclass

from evbot.domain.options import OptionRight


def _normal_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def black_scholes_price(
    spot: float, strike: float, years: float, rate: float, vol: float, right: OptionRight
) -> float:
    """European price without dividends; close enough for short-dated ATM American options."""
    root_time = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + vol**2 / 2) * years) / root_time
    d2 = d1 - root_time
    discounted_strike = strike * math.exp(-rate * years)
    if right is OptionRight.CALL:
        return spot * _normal_cdf(d1) - discounted_strike * _normal_cdf(d2)
    return discounted_strike * _normal_cdf(-d2) - spot * _normal_cdf(-d1)


@dataclass(frozen=True, slots=True)
class ImpliedVolSolver:
    min_vol: float = 1e-4
    max_vol: float = 10.0
    tolerance: float = 1e-9
    max_iterations: int = 100

    def solve(
        self,
        price: float,
        spot: float,
        strike: float,
        years: float,
        right: OptionRight,
        rate: float,
    ) -> float | None:
        if spot <= 0 or strike <= 0 or years <= 0:
            return None
        discounted_strike = strike * math.exp(-rate * years)
        if right is OptionRight.CALL:
            lower_bound, upper_bound = max(spot - discounted_strike, 0.0), spot
        else:
            lower_bound, upper_bound = max(discounted_strike - spot, 0.0), discounted_strike
        if not lower_bound < price < upper_bound:
            return None

        def price_at(vol: float) -> float:
            return black_scholes_price(spot, strike, years, rate, vol, right)

        low, high = self.min_vol, self.max_vol
        if price_at(low) > price or price_at(high) < price:
            return None
        for _ in range(self.max_iterations):
            middle = (low + high) / 2
            if high - low < self.tolerance:
                return middle
            if price_at(middle) < price:
                low = middle
            else:
                high = middle
        return None
