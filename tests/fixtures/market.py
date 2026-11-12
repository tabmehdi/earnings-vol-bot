import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.options import OptionChain, OptionContract, OptionQuote, OptionRight
from evbot.domain.prices import Bar, PriceHistory
from evbot.strategy.implied_vol import black_scholes_price
from evbot.strategy.metrics import years_to_expiry

SPOT = 100.0
RATE = 0.045
AS_OF = datetime(2026, 11, 19, 15, 45, tzinfo=ZoneInfo("America/New_York"))
REPORT_DATE = date(2026, 11, 19)
EVENT = EarningsEvent("AAPL", REPORT_DATE, ReportTiming.AMC)
QUOTED_AT = AS_OF - timedelta(seconds=20)
SPOT_AT = AS_OF - timedelta(seconds=5)

SAME_DAY = date(2026, 11, 19)  # expires on the report date, so it is not the front expiry
FRONT = date(2026, 11, 20)  # 1 day
WEEK = date(2026, 11, 27)  # 8 days
BACK = date(2026, 12, 18)  # 29 days, closest to FRONT + 30 (Dec 20)
FAR = date(2027, 1, 15)  # 57 days

# Third Fridays: the standard monthly expiries the back leg may use.
MONTHLIES = (date(2026, 11, 20), date(2026, 12, 18), date(2027, 1, 15))

BACKWARDATED = {FRONT: 0.90, WEEK: 0.60, BACK: 0.50, FAR: 0.45}
CONTANGO = {FRONT: 0.45, WEEK: 0.46, BACK: 0.47, FAR: 0.48}
TIGHT = 0.01  # half spread as a fraction of mid: spread_pct = 2%
WIDE = 0.15  # spread_pct = 30%, fails the 10% limit
RIGHTS = (OptionRight.CALL, OptionRight.PUT)
STRIKES = (95.0, 100.0, 105.0)


def quote_at_iv(
    expiry: date,
    strike: float,
    right: OptionRight,
    vol: float,
    half_spread: float = TIGHT,
    open_interest: int = 1_000,
) -> OptionQuote:
    """iv is left empty: every volatility in these scenarios has to come from the solver."""
    mid = black_scholes_price(SPOT, strike, years_to_expiry(AS_OF, expiry), RATE, vol, right)
    contract = OptionContract("AAPL", expiry, strike, right)
    bid, ask = mid * (1 - half_spread), mid * (1 + half_spread)
    return OptionQuote(contract, bid, ask, None, open_interest, volume=100, quoted_at=QUOTED_AT)


def build_chain(
    ivs: dict[date, float], half_spread: float = TIGHT, open_interest: int = 1_000
) -> OptionChain:
    same_day = tuple(
        OptionQuote(OptionContract("AAPL", SAME_DAY, k, right), 0.50, 0.52, None, 1_000, 100)
        for k in STRIKES
        for right in RIGHTS
    )
    priced = tuple(
        quote_at_iv(expiry, k, right, vol, half_spread, open_interest)
        for expiry, vol in ivs.items()
        for k in STRIKES
        for right in RIGHTS
    )
    return OptionChain("AAPL", SPOT, AS_OF, same_day + priced, spot_at=SPOT_AT)


def build_history(daily_move: float, volume: int) -> PriceHistory:
    """31 bars that alternate up and down by daily_move in log terms.

    Each bar opens at the previous close and has no wick, so open variance and the
    Rogers-Satchell term are 0 and Yang-Zhang RV is about daily_move * 6.06.
    """
    start = date(2026, 10, 19)
    bars = [Bar(start, 100.0, 100.0, 100.0, 100.0, volume)]
    for i in range(1, 31):
        previous = bars[-1].close
        close = previous * math.exp(daily_move if i % 2 else -daily_move)
        bars.append(
            Bar(
                start + timedelta(days=i),
                previous,
                max(previous, close),
                min(previous, close),
                close,
                volume,
            )
        )
    return PriceHistory("AAPL", tuple(bars))
