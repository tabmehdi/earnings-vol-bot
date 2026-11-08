import math
import statistics
from dataclasses import dataclass
from itertools import pairwise
from typing import Protocol

from evbot.domain.prices import PriceHistory


class VolatilityEstimator(Protocol):
    def estimate(self, history: PriceHistory) -> float: ...


@dataclass(frozen=True, slots=True)
class YangZhangEstimator:
    """Matches the legacy script: squared returns are not de-meaned, sums divide by window - 1."""

    window: int
    trading_days_per_year: int

    def __post_init__(self) -> None:
        if self.window < 2:
            raise ValueError(f"window must be at least 2, got {self.window}")

    def estimate(self, history: PriceHistory) -> float:
        bars = history.last(self.window + 1)
        open_sum = close_sum = rogers_satchell_sum = 0.0
        for previous, bar in pairwise(bars):
            open_sum += math.log(bar.open / previous.close) ** 2
            close_sum += math.log(bar.close / previous.close) ** 2
            log_high = math.log(bar.high / bar.open)
            log_low = math.log(bar.low / bar.open)
            log_close = math.log(bar.close / bar.open)
            rogers_satchell_sum += log_high * (log_high - log_close)
            rogers_satchell_sum += log_low * (log_low - log_close)

        n = self.window
        k = 0.34 / (1.34 + (n + 1) / (n - 1))
        variance = (open_sum + k * close_sum + (1 - k) * rogers_satchell_sum) / (n - 1)
        return math.sqrt(variance) * math.sqrt(self.trading_days_per_year)


@dataclass(frozen=True, slots=True)
class CloseToCloseEstimator:
    window: int
    trading_days_per_year: int

    def __post_init__(self) -> None:
        if self.window < 2:
            raise ValueError(f"window must be at least 2, got {self.window}")

    def estimate(self, history: PriceHistory) -> float:
        bars = history.last(self.window + 1)
        returns = [math.log(b.close / a.close) for a, b in pairwise(bars)]
        return statistics.stdev(returns) * math.sqrt(self.trading_days_per_year)
