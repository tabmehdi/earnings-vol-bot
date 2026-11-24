from collections.abc import Sequence
from dataclasses import dataclass

from evbot.domain.options import OptionQuote
from evbot.domain.spreads import CalendarSpread
from evbot.domain.trades import Position, SpreadCostEstimate

TOLERANCE = 1e-9  # quotes are cents; float noise must not flip a limit exactly on the edge


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason: str = ""
    spread_cost: SpreadCostEstimate | None = None  # set once the quotes give a debit
    warning: str = ""  # a limit breached but not enforced (logged, not a rejection)


@dataclass(frozen=True, slots=True)
class RiskManager:
    """enforce_spread_cost False (paper) still measures and reports the spread cost but does
    not reject on it: indicative quotes are much wider than the market (ADR 0007)."""

    max_spread_cost_pct: float
    max_open_positions: int
    max_committed_pct: float
    enforce_spread_cost: bool

    def approve(
        self,
        spread: CalendarSpread,
        front: OptionQuote,
        back: OptionQuote,
        contracts: int,
        open_positions: Sequence[Position],
        equity: float,
        debit: float | None = None,
    ) -> RiskDecision:
        """debit is what the order will pay per spread, the mid by default. It only sets the
        capital committed: the spread cost is always measured against the mid, so a
        natural-price order is not judged more leniently for paying more."""
        if contracts < 1:
            raise ValueError(f"approve needs at least one contract, got {contracts}")
        still_open = [p for p in open_positions if p.is_open]
        if any(p.symbol == spread.underlying for p in still_open):
            return RiskDecision(False, f"{spread.underlying} is already held")
        if len(still_open) >= self.max_open_positions:
            return RiskDecision(False, f"{len(still_open)} open positions already")
        mid = back.mid - front.mid
        if mid <= 0:
            return RiskDecision(False, f"no debit to pay ({mid:.2f})")
        # Half of each leg's bid-ask width, paid on the way in and again on the way out.
        cost = SpreadCostEstimate((front.ask - front.bid) + (back.ask - back.bid), mid)
        warning = ""
        if cost.share > self.max_spread_cost_pct + TOLERANCE:
            breach = (
                f"round-trip spread cost {cost.round_trip:.2f} is {cost.share:.1%} of the "
                f"{mid:.2f} debit, above {self.max_spread_cost_pct:.0%}"
            )
            if self.enforce_spread_cost:
                return RiskDecision(False, breach, cost)
            warning = f"{breach} (not enforced)"
        paid = mid if debit is None else debit
        multiplier = spread.front.contract.multiplier
        committed = sum(p.cost for p in still_open) + paid * contracts * multiplier
        limit = self.max_committed_pct * equity
        if committed > limit + TOLERANCE:
            return RiskDecision(
                False,
                f"{committed:,.0f} would be committed, above the {limit:,.0f} limit",
                cost,
                warning,
            )
        return RiskDecision(True, spread_cost=cost, warning=warning)
