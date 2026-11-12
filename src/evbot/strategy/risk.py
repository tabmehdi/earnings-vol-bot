from collections.abc import Sequence
from dataclasses import dataclass

from evbot.domain.options import OptionQuote
from evbot.domain.spreads import CalendarSpread
from evbot.domain.trades import Position

TOLERANCE = 1e-9  # quotes are cents; float noise must not flip a limit exactly on the edge


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RiskManager:
    max_spread_cost_pct: float
    max_open_positions: int
    max_committed_pct: float

    def approve(
        self,
        spread: CalendarSpread,
        front: OptionQuote,
        back: OptionQuote,
        contracts: int,
        open_positions: Sequence[Position],
        equity: float,
    ) -> RiskDecision:
        if contracts < 1:
            raise ValueError(f"approve needs at least one contract, got {contracts}")
        still_open = [p for p in open_positions if p.is_open]
        if any(p.symbol == spread.underlying for p in still_open):
            return RiskDecision(False, f"{spread.underlying} is already held")
        if len(still_open) >= self.max_open_positions:
            return RiskDecision(False, f"{len(still_open)} open positions already")
        debit = back.mid - front.mid
        if debit <= 0:
            return RiskDecision(False, f"no debit to pay ({debit:.2f})")
        # Half of each leg's bid-ask width, paid on the way in and again on the way out.
        round_trip = (front.ask - front.bid) + (back.ask - back.bid)
        share = round_trip / debit
        if share > self.max_spread_cost_pct + TOLERANCE:
            return RiskDecision(
                False,
                f"round-trip spread cost {round_trip:.2f} is {share:.1%} of the "
                f"{debit:.2f} debit, above {self.max_spread_cost_pct:.0%}",
            )
        multiplier = spread.front.contract.multiplier
        committed = sum(p.cost for p in still_open) + debit * contracts * multiplier
        limit = self.max_committed_pct * equity
        if committed > limit + TOLERANCE:
            return RiskDecision(
                False, f"{committed:,.0f} would be committed, above the {limit:,.0f} limit"
            )
        return RiskDecision(True)
