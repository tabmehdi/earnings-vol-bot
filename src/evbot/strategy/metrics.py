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
                raise ValueError(f"no solvable ATM IV for front expiry {front}")
        return TermStructure(tuple(points))

    def _solved_atm_iv(self, chain: OptionChain, expiry: date, rate: float) -> float | None:
        strike = chain.atm_strike(expiry)
        at_the_money = [
            quote
            for quote in chain.quotes
            if quote.contract.expiry == expiry and quote.contract.strike == strike
        ]
        solved = [
            iv
            for iv in (self._solve(chain, quote, rate) for quote in at_the_money)
            if iv is not None
        ]
        return sum(solved) / len(solved) if solved else None

    def _solve(self, chain: OptionChain, quote: OptionQuote, rate: float) -> float | None:
        return self.solver.solve(
            quote.mid,
            chain.spot,
            quote.contract.strike,
            years_to_expiry(chain.as_of, quote.contract.expiry),
            quote.contract.right,
            rate,
        )
