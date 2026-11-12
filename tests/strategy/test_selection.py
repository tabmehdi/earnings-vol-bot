from dataclasses import replace
from datetime import date

import pytest

from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.options import OptionChain, OptionContract, OptionQuote, OptionRight
from evbot.domain.spreads import Side
from evbot.strategy.selection import SpreadSelector, front_expiry
from tests.fixtures.market import (
    AS_OF,
    BACK,
    BACKWARDATED,
    EVENT,
    FRONT,
    MONTHLIES,
    REPORT_DATE,
    SPOT,
    WEEK,
    build_chain,
)

SELECTOR = SpreadSelector(back_target_days=30, back_min_gap_days=21)
# Standard monthlies around the test dates: Nov 20, Dec 18, Jan 15 (third Fridays).
DEC_MONTHLY, JAN_MONTHLY = date(2026, 12, 18), date(2027, 1, 15)


def friday_report(timing: ReportTiming) -> EarningsEvent:
    # FRONT is Friday Nov 20 2026, and the chain has an option expiring that same day.
    return EarningsEvent("AAPL", FRONT, timing)


# Front expiry (unchanged)


def test_amc_front_expiry_skips_an_expiry_on_the_report_date() -> None:
    assert front_expiry(build_chain(BACKWARDATED), EVENT) == FRONT


@pytest.mark.parametrize(
    ("timing", "expected"),
    [
        (ReportTiming.BMO, FRONT),  # the Friday 16:00 expiry already holds a pre-market report
        (ReportTiming.AMC, WEEK),  # the Friday expiry is gone before an after-close report
        (ReportTiming.UNKNOWN, WEEK),  # treated like AMC, the conservative choice
    ],
)
def test_friday_report_with_a_friday_expiry(timing: ReportTiming, expected: date) -> None:
    assert front_expiry(build_chain(BACKWARDATED), friday_report(timing)) == expected


def test_front_expiry_is_none_when_nothing_expires_after_the_report() -> None:
    late = EarningsEvent("AAPL", date(2027, 6, 1), ReportTiming.AMC)

    assert front_expiry(build_chain(BACKWARDATED), late) is None


# Back expiry: the standard monthly


def chain_of(*contracts: tuple[date, float], spot: float = SPOT) -> OptionChain:
    quotes = tuple(
        OptionQuote(OptionContract("AAPL", e, k, OptionRight.CALL), 1.0, 1.1, None, 1_000, 10)
        for e, k in contracts
    )
    return OptionChain("AAPL", spot, AS_OF, quotes)


def report_before(front: date) -> EarningsEvent:
    """A BMO report on the front expiry's day, so that expiry is the front."""
    return EarningsEvent("AAPL", front, ReportTiming.BMO)


def test_weekly_front_takes_the_monthly_closest_to_thirty_days() -> None:
    # Front Nov 13 (weekly). Target Dec 13. Monthlies 21+ days out: Dec 18 (35), Jan 15 (63).
    front = date(2026, 11, 13)
    chain = chain_of((front, 100.0), (date(2026, 11, 20), 100.0), (DEC_MONTHLY, 100.0))

    choice = SELECTOR.select(chain, report_before(front), MONTHLIES)

    assert choice.spread is not None
    assert choice.spread.back.contract.expiry == DEC_MONTHLY


def test_a_monthly_front_takes_the_next_monthly() -> None:
    # Front Nov 20 is itself the November monthly; the back is December's.
    choice = SELECTOR.select(build_chain(BACKWARDATED), EVENT, MONTHLIES)

    assert choice.spread is not None
    assert (choice.spread.front.contract.expiry, choice.spread.back.contract.expiry) == (
        FRONT,
        BACK,
    )
    assert choice.spread.back_days == 28


def test_a_closest_monthly_under_21_days_is_skipped_for_the_next_one() -> None:
    # Front Nov 30, target Dec 30: Dec 18 is closest (12 days off) but only 18 days after
    # the front, so January's monthly is used.
    front = date(2026, 11, 30)
    chain = chain_of((front, 100.0), (DEC_MONTHLY, 100.0), (JAN_MONTHLY, 100.0))

    choice = SELECTOR.select(chain, report_before(front), MONTHLIES)

    assert choice.spread is not None
    assert choice.spread.back.contract.expiry == JAN_MONTHLY


def test_a_weekly_near_thirty_days_is_never_the_back_leg() -> None:
    # Dec 11 is exactly front + 28 and a weekly; the back is still the Dec 18 monthly.
    front = date(2026, 11, 13)
    chain = chain_of((front, 100.0), (date(2026, 12, 11), 100.0), (DEC_MONTHLY, 100.0))

    choice = SELECTOR.select(chain, report_before(front), MONTHLIES)

    assert choice.spread is not None
    assert choice.spread.back.contract.expiry == DEC_MONTHLY


def test_missing_monthly_in_the_chain_is_no_spread_with_a_note() -> None:
    front = date(2026, 11, 13)
    chain = chain_of((front, 100.0), (date(2026, 12, 11), 100.0))

    choice = SELECTOR.select(chain, report_before(front), MONTHLIES)

    assert choice.spread is None
    assert "2026-12-18" in choice.note and "not in the chain" in choice.note


def test_no_shared_strike_with_the_monthly_is_no_spread_with_a_note() -> None:
    front = date(2026, 11, 13)
    chain = chain_of((front, 100.0), (DEC_MONTHLY, 105.0))

    choice = SELECTOR.select(chain, report_before(front), MONTHLIES)

    assert choice.spread is None
    assert "no strike listed in both" in choice.note


def test_no_monthly_far_enough_out_is_no_spread_with_a_note() -> None:
    front = date(2026, 11, 13)
    chain = chain_of((front, 100.0), (DEC_MONTHLY, 100.0))

    choice = SELECTOR.select(chain, report_before(front), (date(2026, 11, 20),))

    assert choice.spread is None
    assert "21 days" in choice.note


def test_no_front_expiry_is_no_spread_with_a_note() -> None:
    choice = SELECTOR.select(chain_of((REPORT_DATE, 100.0)), EVENT, MONTHLIES)

    assert choice.spread is None
    assert choice.note


# Strike (ADR 0003, unchanged)


def test_sells_the_front_and_buys_the_monthly_at_the_atm_call() -> None:
    choice = SELECTOR.select(build_chain(BACKWARDATED), EVENT, MONTHLIES)

    assert choice.spread is not None
    assert choice.spread.front.side is Side.SELL
    assert choice.spread.back.side is Side.BUY
    assert choice.spread.strike == SPOT
    assert choice.spread.front.contract.right is OptionRight.CALL


def test_uses_the_shared_strike_closest_to_spot() -> None:
    # Shared strikes are 95 and 105. |101 - 105| = 4 beats |101 - 95| = 6.
    chain = chain_of(
        (FRONT, 95.0), (FRONT, 100.0), (FRONT, 105.0), (BACK, 95.0), (BACK, 105.0), spot=101.0
    )

    choice = SELECTOR.select(chain, EVENT, MONTHLIES)

    assert choice.spread is not None
    assert choice.spread.strike == 105.0


def test_shared_strike_tie_goes_to_the_lower_strike() -> None:
    chain = chain_of((FRONT, 100.0), (FRONT, 105.0), (BACK, 100.0), (BACK, 105.0), spot=102.5)

    choice = SELECTOR.select(chain, EVENT, MONTHLIES)

    assert choice.spread is not None
    assert choice.spread.strike == 100.0


def test_a_strike_listed_only_as_a_put_does_not_count() -> None:
    back_put = OptionQuote(
        OptionContract("AAPL", BACK, 100.0, OptionRight.PUT), 1.0, 1.1, None, 1_000, 10
    )
    calls = chain_of((FRONT, 100.0), (BACK, 110.0))
    chain = replace(calls, quotes=(*calls.quotes, back_put))

    assert SELECTOR.select(chain, EVENT, MONTHLIES).spread is None
