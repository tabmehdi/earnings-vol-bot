from datetime import date

import pytest

from evbot.domain.options import OptionContract, OptionQuote, OptionRight
from evbot.domain.spreads import CalendarSpread, Leg, Side

FRONT = date(2026, 11, 20)
BACK = date(2026, 12, 18)


def contract(
    expiry: date, strike: float = 100.0, right: OptionRight = OptionRight.CALL
) -> OptionContract:
    return OptionContract("AAPL", expiry, strike, right)


def calendar() -> CalendarSpread:
    return CalendarSpread(Leg(contract(FRONT), Side.SELL), Leg(contract(BACK), Side.BUY))


def test_calendar_exposes_shared_strike_and_underlying() -> None:
    spread = calendar()

    assert spread.strike == 100.0
    assert spread.underlying == "AAPL"


def test_net_debit_is_back_mid_minus_front_mid() -> None:
    # back mid (3.00 + 3.20) / 2 = 3.10; front mid (1.00 + 1.20) / 2 = 1.10; 3.10 - 1.10 = 2.00
    front = OptionQuote(contract(FRONT), 1.00, 1.20, 0.80, 1000, 10)
    back = OptionQuote(contract(BACK), 3.00, 3.20, 0.45, 1000, 10)

    assert calendar().net_debit(front, back) == pytest.approx(2.00)


def test_net_debit_rejects_quotes_for_other_contracts() -> None:
    wrong = OptionQuote(contract(FRONT, strike=105.0), 1.00, 1.20, 0.80, 1000, 10)
    back = OptionQuote(contract(BACK), 3.00, 3.20, 0.45, 1000, 10)

    with pytest.raises(ValueError):
        calendar().net_debit(wrong, back)


def test_calendar_rejects_back_expiry_not_after_front() -> None:
    with pytest.raises(ValueError):
        CalendarSpread(Leg(contract(BACK), Side.SELL), Leg(contract(FRONT), Side.BUY))


def test_calendar_rejects_bought_front_leg() -> None:
    with pytest.raises(ValueError):
        CalendarSpread(Leg(contract(FRONT), Side.BUY), Leg(contract(BACK), Side.BUY))


def test_calendar_rejects_different_strikes() -> None:
    with pytest.raises(ValueError):
        CalendarSpread(Leg(contract(FRONT), Side.SELL), Leg(contract(BACK, strike=105.0), Side.BUY))


def test_calendar_rejects_mixed_rights() -> None:
    with pytest.raises(ValueError):
        CalendarSpread(
            Leg(contract(FRONT), Side.SELL), Leg(contract(BACK, right=OptionRight.PUT), Side.BUY)
        )


def test_back_days_counts_calendar_days_between_the_legs() -> None:
    # Nov 20 to Dec 18 is 28 days.
    assert calendar().back_days == 28
