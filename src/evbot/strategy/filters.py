from dataclasses import dataclass
from typing import Protocol

from evbot.domain.options import OptionChain
from evbot.domain.signals import FilterResult, SignalMetrics
from evbot.domain.spreads import CalendarSpread

AVG_VOLUME = "avg_volume"
IV_RV = "iv_rv"
TERM_SLOPE = "term_slope"
SPREAD_AVAILABLE = "spread_available"
LIQUIDITY = "liquidity"
REPORT_TIMING = "report_timing"


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
class SpreadAvailableFilter:
    def check(self, candidate: SignalCandidate) -> FilterResult:
        if candidate.spread is None:
            return FilterResult(
                SPREAD_AVAILABLE, False, None, None, detail="no strike listed in both expiries"
            )
        return FilterResult(SPREAD_AVAILABLE, True, None, None)


@dataclass(frozen=True, slots=True)
class LiquidityFilter:
    """Records both legs' spread and open interest; informational, so it always passes (ADR 0004).

    It is separate from the Filter protocol because it needs no metrics: a Signal whose
    metrics could not be computed still records it.
    """

    def describe(self, chain: OptionChain, spread: CalendarSpread | None) -> FilterResult:
        if spread is None:
            return FilterResult(LIQUIDITY, True, None, None, detail="no calendar spread")
        front, back = chain.quote(spread.front.contract), chain.quote(spread.back.contract)
        detail = (
            f"front_spread_pct={front.spread_pct:.4f} front_open_interest={front.open_interest} "
            f"back_spread_pct={back.spread_pct:.4f} back_open_interest={back.open_interest}"
        )
        return FilterResult(LIQUIDITY, True, None, None, detail=detail)
