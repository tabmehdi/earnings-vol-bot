from collections.abc import Sequence
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
class SpreadChoice:
    """The calendar to trade, or why there is none."""

    spread: CalendarSpread | None
    note: str = ""


@dataclass(frozen=True, slots=True)
class SpreadSelector:
    """Front: first expiry holding the report (weeklies allowed). Back: a standard monthly
    (ADR 0006), never a weekly, closest to front + back_target_days and at least
    back_min_gap_days after the front. Strike: closest to spot listed in both (ADR 0003)."""

    back_target_days: int
    back_min_gap_days: int

    def select(
        self, chain: OptionChain, event: EarningsEvent, monthlies: Sequence[date]
    ) -> SpreadChoice:
        front = front_expiry(chain, event)
        if front is None:
            return SpreadChoice(None, "no expiry holds the report")
        eligible = sorted(m for m in monthlies if (m - front).days >= self.back_min_gap_days)
        if not eligible:
            return SpreadChoice(
                None,
                f"no standard monthly at least {self.back_min_gap_days} days "
                f"after the front expiry {front}",
            )
        target = front + timedelta(days=self.back_target_days)
        # eligible is ascending and min keeps the first match, so a tie takes the earlier one.
        back = min(eligible, key=lambda expiry: abs((expiry - target).days))
        if back not in chain.expiries():
            return SpreadChoice(None, f"monthly {back} is not in the chain")

        calls = {
            (q.contract.expiry, q.contract.strike): q.contract
            for q in chain.quotes
            if q.contract.right is OptionRight.CALL
        }
        front_strikes = {strike for expiry, strike in calls if expiry == front}
        back_strikes = {strike for expiry, strike in calls if expiry == back}
        shared = front_strikes & back_strikes
        if not shared:
            return SpreadChoice(None, f"no strike listed in both {front} and the {back} monthly")
        # Sorting first makes a tie between two strikes resolve to the lower one.
        strike = min(sorted(shared), key=lambda k: abs(k - chain.spot))
        spread = CalendarSpread(
            Leg(calls[(front, strike)], Side.SELL), Leg(calls[(back, strike)], Side.BUY)
        )
        return SpreadChoice(spread)
