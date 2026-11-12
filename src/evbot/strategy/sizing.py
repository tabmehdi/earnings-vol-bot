import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SizeDecision:
    contracts: int
    reason: str = ""  # why it is 0, for the log


@dataclass(frozen=True, slots=True)
class FixedRiskSizer:
    """A long calendar can lose at most its debit, so the debit is the risk per contract."""

    max_risk_per_trade_pct: float
    max_contracts: int

    def __post_init__(self) -> None:
        if not 0 < self.max_risk_per_trade_pct <= 1:
            raise ValueError(f"risk per trade must be in (0, 1], got {self.max_risk_per_trade_pct}")
        if self.max_contracts < 1:
            raise ValueError(f"max contracts must be at least 1, got {self.max_contracts}")

    def size(self, equity: float, debit: float, multiplier: int) -> SizeDecision:
        if debit <= 0:
            return SizeDecision(0, f"no debit to size from ({debit:.2f})")
        budget = self.max_risk_per_trade_pct * equity
        per_contract = debit * multiplier
        contracts = min(math.floor(budget / per_contract), self.max_contracts)
        if contracts == 0:
            return SizeDecision(
                0, f"one contract costs {per_contract:,.0f}, more than the {budget:,.0f} budget"
            )
        return SizeDecision(contracts)
