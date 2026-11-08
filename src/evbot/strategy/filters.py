from dataclasses import dataclass
from typing import Protocol

from evbot.domain.options import OptionChain
from evbot.domain.signals import FilterResult, SignalMetrics
from evbot.domain.spreads import CalendarSpread

AVG_VOLUME = "avg_volume"
IV_RV = "iv_rv"
TERM_SLOPE = "term_slope"
LIQUIDITY = "liquidity"


@dataclass(frozen=True, slots=True)
class SignalCandidate:
    metrics: SignalMetrics
    chain: OptionChain
    spread: CalendarSpread | None


class Filter(Protocol):
    def check(self, candidate: SignalCandidate) -> FilterResult: ...


@dataclass(frozen=True, slots=True)
class AvgVolumeFilter:
    min_avg_volume: float

    def check(self, candidate: SignalCandidate) -> FilterResult:
        value = candidate.metrics.avg_volume_30d
        return FilterResult(AVG_VOLUME, value >= self.min_avg_volume, value, self.min_avg_volume)


@dataclass(frozen=True, slots=True)
class IvRvFilter:
    min_ratio: float

    def check(self, candidate: SignalCandidate) -> FilterResult:
        value = candidate.metrics.iv30_rv30
        return FilterResult(IV_RV, value >= self.min_ratio, value, self.min_ratio)


@dataclass(frozen=True, slots=True)
class TermSlopeFilter:
    max_slope: float

    def check(self, candidate: SignalCandidate) -> FilterResult:
        value = candidate.metrics.term_slope
        return FilterResult(TERM_SLOPE, value <= self.max_slope, value, self.max_slope)


@dataclass(frozen=True, slots=True)
class LiquidityFilter:
    max_spread_pct: float
    min_open_interest: int

    def check(self, candidate: SignalCandidate) -> FilterResult:
        spread = candidate.spread
        if spread is None:
            return FilterResult(
                LIQUIDITY, False, None, self.max_spread_pct, detail="no valid calendar spread"
            )
        quotes = [candidate.chain.quote(leg.contract) for leg in (spread.front, spread.back)]
        worst_spread = max(quote.spread_pct for quote in quotes)
        lowest_interest = min(quote.open_interest for quote in quotes)
        passed = worst_spread <= self.max_spread_pct and lowest_interest >= self.min_open_interest
        detail = f"lowest open interest {lowest_interest}, minimum {self.min_open_interest}"
        return FilterResult(LIQUIDITY, passed, worst_spread, self.max_spread_pct, detail)
