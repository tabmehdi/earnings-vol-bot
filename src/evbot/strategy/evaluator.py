from dataclasses import dataclass
from datetime import date

from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.options import OptionChain, OptionQuote
from evbot.domain.prices import PriceHistory
from evbot.domain.signals import FilterResult, LegPrices, Signal, SignalPrices, Verdict
from evbot.domain.spreads import CalendarSpread
from evbot.strategy.filters import (
    REPORT_TIMING,
    UNRELIABLE_QUOTES,
    Filter,
    LiquidityFilter,
    SignalCandidate,
)
from evbot.strategy.metrics import MetricsCalculator, UnreliableQuotesError
from evbot.strategy.selection import SpreadSelector
from evbot.strategy.verdict import VerdictPolicy


@dataclass(frozen=True, slots=True)
class MarketContext:
    event: EarningsEvent
    chain: OptionChain
    history: PriceHistory
    monthly_expiries: tuple[date, ...]

    def __post_init__(self) -> None:
        symbol = self.event.symbol
        if self.chain.underlying != symbol or self.history.symbol != symbol:
            raise ValueError(f"chain and history must both be for {symbol}")


@dataclass(frozen=True, slots=True)
class SignalEvaluator:
    metrics: MetricsCalculator
    selector: SpreadSelector
    filters: tuple[Filter, ...]
    liquidity: LiquidityFilter
    policy: VerdictPolicy

    def evaluate(self, context: MarketContext) -> Signal:
        event, chain = context.event, context.chain
        choice = self.selector.select(chain, event, context.monthly_expiries)
        spread = choice.spread
        prices = self._prices(chain, spread)
        recorded: tuple[FilterResult, ...] = (self.liquidity.describe(chain, spread),)
        if event.timing is ReportTiming.UNKNOWN:
            recorded += (
                FilterResult(REPORT_TIMING, False, None, None, detail="report timing not supplied"),
            )
        try:
            metrics = self.metrics.compute(chain, context.history, event)
        except ValueError as error:
            # Every decision is persisted, so bad inputs become an explained AVOID, not a gap.
            name = UNRELIABLE_QUOTES if isinstance(error, UnreliableQuotesError) else "metrics"
            reason = FilterResult(name, False, None, None, detail=str(error))
            return Signal(
                event, chain.as_of, None, (reason, *recorded), Verdict.AVOID, spread, prices
            )

        candidate = SignalCandidate(metrics, chain, spread, choice.note)
        results = tuple(check.check(candidate) for check in self.filters)
        # Without a known timing we cannot tell which session to enter, whatever the metrics say.
        verdict = (
            Verdict.AVOID if event.timing is ReportTiming.UNKNOWN else self.policy.decide(results)
        )
        return Signal(event, chain.as_of, metrics, (*results, *recorded), verdict, spread, prices)

    def _prices(self, chain: OptionChain, spread: CalendarSpread | None) -> SignalPrices:
        """What the market showed at decision time, so trades can be evaluated later."""
        if spread is None:
            return SignalPrices(chain.spot, chain.spot_at, None, None, None)
        front = chain.quote(spread.front.contract)
        back = chain.quote(spread.back.contract)
        return SignalPrices(
            chain.spot,
            chain.spot_at,
            self._leg(chain, front),
            self._leg(chain, back),
            spread.net_debit(front, back),
        )

    def _leg(self, chain: OptionChain, quote: OptionQuote) -> LegPrices:
        iv = self.metrics.implied_vol(chain, quote)
        return LegPrices(quote.bid, quote.ask, quote.mid, iv, quote.quoted_at)
