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
    REPORT_DATE,
    SPOT,
    WEEK,
    build_chain,
)


def friday_report(timing: ReportTiming) -> EarningsEvent:
    # FRONT is Friday Nov 20 2026, and the chain has an option expiring that same day.
    return EarningsEvent("AAPL", FRONT, timing)


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


@pytest.mark.parametrize(
    ("timing", "expected"),
    [
        (ReportTiming.BMO, FRONT),
        (ReportTiming.AMC, WEEK),
    ],
)
def test_selector_sells_the_expiry_that_holds_the_friday_report(
    timing: ReportTiming, expected: date
) -> None:
    spread = SpreadSelector(back_offset_days=30).select(
        build_chain(BACKWARDATED), friday_report(timing)
    )

    assert spread is not None
    assert spread.front.contract.expiry == expected


def test_front_expiry_is_none_when_nothing_expires_after_the_report() -> None:
    late = EarningsEvent("AAPL", date(2027, 6, 1), ReportTiming.AMC)

    assert front_expiry(build_chain(BACKWARDATED), late) is None


def test_selects_short_front_long_back_atm_calls() -> None:
    spread = SpreadSelector(back_offset_days=30).select(build_chain(BACKWARDATED), EVENT)

    assert spread is not None
    assert spread.front.contract.expiry == FRONT
    assert spread.front.side is Side.SELL
    assert spread.back.contract.expiry == BACK  # Dec 18 is 2 days from Nov 20 + 30 = Dec 20
    assert spread.back.side is Side.BUY
    assert spread.strike == SPOT
    assert spread.front.contract.right is OptionRight.CALL


def chain_of(*contracts: tuple[date, float]) -> OptionChain:
    quotes = tuple(
        OptionQuote(OptionContract("AAPL", e, k, OptionRight.CALL), 1.0, 1.1, None, 1_000, 10)
        for e, k in contracts
    )
    return OptionChain("AAPL", SPOT, AS_OF, quotes)


def test_back_expiry_tie_goes_to_the_earlier_one() -> None:
    # Target Nov 20 + 30 = Dec 20; Dec 18 and Dec 22 are both 2 days away.
    chain = chain_of((FRONT, 100.0), (date(2026, 12, 18), 100.0), (date(2026, 12, 22), 100.0))

    spread = SpreadSelector(back_offset_days=30).select(chain, EVENT)

    assert spread is not None
    assert spread.back.contract.expiry == date(2026, 12, 18)


def test_none_when_back_expiry_lacks_the_front_strike() -> None:
    chain = chain_of((FRONT, 100.0), (BACK, 105.0))

    assert SpreadSelector(back_offset_days=30).select(chain, EVENT) is None


def test_none_when_there_is_no_later_expiry() -> None:
    chain = chain_of((FRONT, 100.0))

    assert SpreadSelector(back_offset_days=30).select(chain, EVENT) is None


def test_none_when_there_is_no_front_expiry() -> None:
    chain = chain_of((REPORT_DATE, 100.0))

    assert SpreadSelector(back_offset_days=30).select(chain, EVENT) is None
