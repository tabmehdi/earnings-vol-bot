import math
from dataclasses import dataclass
from typing import Protocol

from evbot.domain.options import OptionQuote
from evbot.domain.orders import OrderIntent
from evbot.domain.spreads import Side

MIN_PRICE = 0.01
CENT_TOLERANCE = 1e-9  # so 2.1000000000000005 rounds as 2.10, not 2.11


class PricingPolicy(Protocol):
    @property
    def steps(self) -> int: ...

    @property
    def step_seconds(self) -> float: ...

    def limit(
        self, intent: OrderIntent, attempt: int, front: OptionQuote, back: OptionQuote
    ) -> float:
        """The net limit per spread for attempt 0..steps-1."""
        ...

    def natural(self, intent: OrderIntent, front: OptionQuote, back: OptionQuote) -> float:
        """The price that crosses both legs' spreads: the last resort for an exit."""
        ...

    def leg_limit(self, side: Side, attempt: int, quote: OptionQuote) -> float:
        """One leg's limit per contract for attempt 0..steps-1 (a legged exit)."""
        ...

    def leg_natural(self, side: Side, quote: OptionQuote) -> float:
        """One leg at its far side: buy at the ask, sell at the bid."""
        ...


@dataclass(frozen=True, slots=True)
class MidWalkPolicy:
    """Starts at the mid and walks toward the market in equal steps.

    Opening (a debit): from the mid up to a cap at entry_cap_fraction of the way to the
    natural debit, so a thin market cannot drag the price far from fair value.
    Closing (a credit): from the mid down to the natural credit, because getting out matters
    more than the price.
    """

    steps: int
    step_seconds: float
    entry_cap_fraction: float

    def __post_init__(self) -> None:
        if self.steps < 2:
            raise ValueError(f"a walk needs at least 2 steps, got {self.steps}")

    def limit(
        self, intent: OrderIntent, attempt: int, front: OptionQuote, back: OptionQuote
    ) -> float:
        if not 0 <= attempt < self.steps:
            raise ValueError(f"attempt {attempt} is outside 0..{self.steps - 1}")
        mid = back.mid - front.mid
        progress = attempt / (self.steps - 1)
        if intent is OrderIntent.OPEN:
            natural = back.ask - front.bid
            cap = mid + self.entry_cap_fraction * (natural - mid)
            # Rounded down: a debit never exceeds the cap.
            return max(_floor_cents(mid + (cap - mid) * progress), MIN_PRICE)
        natural = back.bid - front.ask
        # Rounded up: a credit never undercuts the step.
        return max(_ceil_cents(mid + (natural - mid) * progress), MIN_PRICE)

    def natural(self, intent: OrderIntent, front: OptionQuote, back: OptionQuote) -> float:
        if intent is OrderIntent.OPEN:
            return max(_ceil_cents(back.ask - front.bid), MIN_PRICE)
        return max(_floor_cents(back.bid - front.ask), MIN_PRICE)

    def leg_limit(self, side: Side, attempt: int, quote: OptionQuote) -> float:
        """Legs are only traded to get out, so the walk goes all the way to the far side."""
        if not 0 <= attempt < self.steps:
            raise ValueError(f"attempt {attempt} is outside 0..{self.steps - 1}")
        progress = attempt / (self.steps - 1)
        if side is Side.BUY:
            # Rounded down: never pays above the step.
            return max(_floor_cents(quote.mid + (quote.ask - quote.mid) * progress), MIN_PRICE)
        # Rounded up: never sells below the step.
        return max(_ceil_cents(quote.mid + (quote.bid - quote.mid) * progress), MIN_PRICE)

    def leg_natural(self, side: Side, quote: OptionQuote) -> float:
        if side is Side.BUY:
            return max(_ceil_cents(quote.ask), MIN_PRICE)
        return max(_floor_cents(quote.bid), MIN_PRICE)


def _floor_cents(price: float) -> float:
    return math.floor(price * 100 + CENT_TOLERANCE) / 100


def _ceil_cents(price: float) -> float:
    return math.ceil(price * 100 - CENT_TOLERANCE) / 100
