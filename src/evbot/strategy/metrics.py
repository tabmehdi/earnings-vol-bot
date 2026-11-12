from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from evbot.domain.events import EarningsEvent
from evbot.domain.options import OptionChain, OptionQuote
from evbot.domain.prices import PriceHistory
from evbot.domain.signals import SignalMetrics
from evbot.strategy.implied_vol import ImpliedVolSolver
from evbot.strategy.rates import RateSource
from evbot.strategy.selection import front_expiry
from evbot.strategy.term_structure import TermStructure
from evbot.strategy.volatility import VolatilityEstimator

CALENDAR_DAYS_PER_YEAR = 365
SECONDS_PER_DAY = 86_400
MARKET_TIMEZONE = ZoneInfo("America/New_York")
EXPIRY_TIME = time(16, 0)


class UnreliableQuotesError(ValueError):
    """The front expiry's quotes are too wide for their mids to mean anything."""


def days_to_expiry(as_of: datetime, expiry: date) -> float:
    """Calendar days, with the fraction, from as_of to the 16:00 New York close on expiry."""
    expires_at = datetime.combine(expiry, EXPIRY_TIME, MARKET_TIMEZONE)
    # Convert first: subtracting two datetimes in one zone ignores daylight-saving changes.
    return (expires_at.astimezone(UTC) - as_of.astimezone(UTC)).total_seconds() / SECONDS_PER_DAY


def years_to_expiry(as_of: datetime, expiry: date) -> float:
    return days_to_expiry(as_of, expiry) / CALENDAR_DAYS_PER_YEAR


@dataclass(frozen=True, slots=True)
class MetricsCalculator:
    solver: ImpliedVolSolver
    rates: RateSource
    volatility: VolatilityEstimator
    iv_days: int
    volume_window: int
    slope_horizon_days: int
    max_iv_input_spread_pct: float

    def compute(
        self, chain: OptionChain, history: PriceHistory, event: EarningsEvent
    ) -> SignalMetrics:
        """Raises ValueError when the market inputs cannot support the metrics."""
        term_structure = self._term_structure(chain, event)
        realized_vol = self.volatility.estimate(history)
        if realized_vol <= 0:
            raise ValueError(f"realized volatility must be positive, got {realized_vol}")

        bars = history.last(self.volume_window)
        return SignalMetrics(
            avg_volume_30d=sum(bar.volume for bar in bars) / len(bars),
            iv30_rv30=term_structure.iv_at(self.iv_days) / realized_vol,
            term_slope=term_structure.slope(term_structure.first_day, self.slope_horizon_days),
        )

    def _term_structure(self, chain: OptionChain, event: EarningsEvent) -> TermStructure:
        front = front_expiry(chain, event)
        if front is None:
            raise ValueError(f"no expiry holds the report on {event.report_date}")
        rate = self.rates.rate_for(chain.as_of.date())

        points: list[tuple[float, float]] = []
        for expiry in chain.expiries():
            if expiry < front:
                continue
            atm_iv = self._solved_atm_iv(chain, expiry, rate)
            if atm_iv is not None:
                points.append((days_to_expiry(chain.as_of, expiry), atm_iv))
            elif expiry == front:
                # Starting at a later expiry would measure a slope without the earnings move.
                raise self._front_failure(chain, front)
        return TermStructure(tuple(points))

    def _solved_atm_iv(self, chain: OptionChain, expiry: date, rate: float) -> float | None:
        usable = [
            quote
            for quote in self._at_the_money(chain, expiry)
            if quote.spread_pct <= self.max_iv_input_spread_pct
        ]
        solved = [iv for iv in (self._solve(chain, q, rate) for q in usable) if iv is not None]
        return sum(solved) / len(solved) if solved else None

    @staticmethod
    def _at_the_money(chain: OptionChain, expiry: date) -> list[OptionQuote]:
        strike = chain.atm_strike(expiry)
        return [
            quote
            for quote in chain.quotes
            if quote.contract.expiry == expiry and quote.contract.strike == strike
        ]

    def _front_failure(self, chain: OptionChain, front: date) -> ValueError:
        quotes = self._at_the_money(chain, front)
        if all(quote.spread_pct > self.max_iv_input_spread_pct for quote in quotes):
            spreads = ", ".join(f"{q.contract.right.value} {q.spread_pct:.1%}" for q in quotes)
            limit = f"{self.max_iv_input_spread_pct:.0%}"
            return UnreliableQuotesError(f"front expiry {front} ATM spreads {spreads} over {limit}")
        return ValueError(f"no solvable ATM IV for front expiry {front}")

    def implied_vol(self, chain: OptionChain, quote: OptionQuote) -> float | None:
        """One quote's IV from its mid at the configured rate; None if it cannot be solved."""
        return self._solve(chain, quote, self.rates.rate_for(chain.as_of.date()))

    def _solve(self, chain: OptionChain, quote: OptionQuote, rate: float) -> float | None:
        return self.solver.solve(
            quote.mid,
            chain.spot,
            quote.contract.strike,
            years_to_expiry(chain.as_of, quote.contract.expiry),
            quote.contract.right,
            rate,
        )
