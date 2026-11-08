from dataclasses import dataclass
from datetime import date, timedelta

from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.options import OptionChain, OptionContract, OptionRight
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

        strike = chain.atm_strike(front)
        front_call = self._call(chain, front, strike)
        back_call = self._call(chain, back, strike)
        if front_call is None or back_call is None:
            return None
        return CalendarSpread(Leg(front_call, Side.SELL), Leg(back_call, Side.BUY))

    @staticmethod
    def _call(chain: OptionChain, expiry: date, strike: float) -> OptionContract | None:
        for quote in chain.quotes:
            contract = quote.contract
            if (
                contract.expiry == expiry
                and contract.strike == strike
                and contract.right is OptionRight.CALL
            ):
                return contract
        return None
