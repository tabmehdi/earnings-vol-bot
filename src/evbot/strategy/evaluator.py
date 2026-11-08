from dataclasses import dataclass

from evbot.domain.events import EarningsEvent
from evbot.domain.options import OptionChain
from evbot.domain.prices import PriceHistory
from evbot.domain.signals import FilterResult, Signal, Verdict
from evbot.strategy.filters import Filter, SignalCandidate
from evbot.strategy.metrics import MetricsCalculator
from evbot.strategy.selection import SpreadSelector
from evbot.strategy.verdict import VerdictPolicy


@dataclass(frozen=True, slots=True)
class MarketContext:
    event: EarningsEvent
    chain: OptionChain
    history: PriceHistory

    def __post_init__(self) -> None:
        symbol = self.event.symbol
        if self.chain.underlying != symbol or self.history.symbol != symbol:
            raise ValueError(f"chain and history must both be for {symbol}")


@dataclass(frozen=True, slots=True)
class SignalEvaluator:
    metrics: MetricsCalculator
    selector: SpreadSelector
    filters: tuple[Filter, ...]
    policy: VerdictPolicy

    def evaluate(self, context: MarketContext) -> Signal:
        event, chain = context.event, context.chain
        try:
            metrics = self.metrics.compute(chain, context.history, event)
        except ValueError as error:
            # Every decision is persisted, so bad inputs become an explained AVOID, not a gap.
            reason = FilterResult("metrics", False, None, None, detail=str(error))
            return Signal(event, chain.as_of, None, (reason,), Verdict.AVOID, spread=None)

        spread = self.selector.select(chain, event)
        candidate = SignalCandidate(metrics, chain, spread)
        results = tuple(check.check(candidate) for check in self.filters)
        return Signal(event, chain.as_of, metrics, results, self.policy.decide(results), spread)
