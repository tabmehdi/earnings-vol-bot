from dataclasses import dataclass
from datetime import date, timedelta

from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.options import OptionChain, OptionRight
from evbot.domain.spreads import CalendarSpread, Leg, Side


def front_expiry(chain: OptionChain, event: EarningsEvent) -> date | None:
    """First expiry that holds the earnings move.

    A BMO report is already priced into an option expiring at 16:00 that day. AMC comes after
    that expiry, and UNKNOWN is treated like AMC to be conservative.
    """
    report_date = event.report_date
    if event.timing is ReportTiming.BMO:
        return next((expiry for expiry in chain.expiries() if expiry >= report_date), None)
    return next((expiry for expiry in chain.expiries() if expiry > report_date), None)


@dataclass(frozen=True, slots=True)
class SpreadSelector:
    back_offset_days: int

    def select(self, chain: OptionChain, event: EarningsEvent) -> CalendarSpread | None:
        front = front_expiry(chain, event)
        if front is None:
            return None
        later = [expiry for expiry in chain.expiries() if expiry > front]
        if not later:
            return None
        target = front + timedelta(days=self.back_offset_days)
        # later is ascending and min keeps the first match, so a tie takes the earlier expiry.
        back = min(later, key=lambda expiry: abs((expiry - target).days))

        calls = {
            (q.contract.expiry, q.contract.strike): q.contract
            for q in chain.quotes
            if q.contract.right is OptionRight.CALL
        }
        front_strikes = {strike for expiry, strike in calls if expiry == front}
        back_strikes = {strike for expiry, strike in calls if expiry == back}
        shared = front_strikes & back_strikes
        if not shared:
            return None
        # Sorting first makes a tie between two strikes resolve to the lower one.
        strike = min(sorted(shared), key=lambda k: abs(k - chain.spot))
        return CalendarSpread(
            Leg(calls[(front, strike)], Side.SELL), Leg(calls[(back, strike)], Side.BUY)
        )
