import math
from datetime import date, datetime

import pytest

from evbot.domain.options import OptionChain, OptionContract, OptionQuote, OptionRight

FRONT = date(2026, 11, 20)
BACK = date(2026, 12, 18)
AS_OF = datetime(2026, 11, 18, 15, 45)


def contract(strike: float, right: OptionRight, expiry: date = FRONT) -> OptionContract:
    return OptionContract("AAPL", expiry, strike, right)


def quote(
    strike: float,
    right: OptionRight,
    iv: float | None = 0.40,
    bid: float = 1.00,
    ask: float = 1.20,
    expiry: date = FRONT,
) -> OptionQuote:
    return OptionQuote(contract(strike, right, expiry), bid, ask, iv, open_interest=1000, volume=50)


# OptionContract


def test_contract_multiplier_defaults_to_100() -> None:
    assert contract(100.0, OptionRight.CALL).multiplier == 100


def test_contract_rejects_non_positive_strike() -> None:
    with pytest.raises(ValueError):
        contract(0.0, OptionRight.CALL)


# OptionQuote


def test_mid_is_halfway_between_bid_and_ask() -> None:
    # (1.00 + 1.20) / 2 = 1.10
    assert quote(100.0, OptionRight.CALL).mid == pytest.approx(1.10)


def test_spread_pct_is_spread_over_mid() -> None:
    # (1.20 - 1.00) / 1.10 = 0.181818...
    assert quote(100.0, OptionRight.CALL).spread_pct == pytest.approx(0.2 / 1.1)


def test_spread_pct_is_infinite_when_mid_is_zero() -> None:
    assert math.isinf(quote(100.0, OptionRight.CALL, bid=0.0, ask=0.0).spread_pct)


def test_quote_rejects_bid_above_ask() -> None:
    with pytest.raises(ValueError):
        quote(100.0, OptionRight.CALL, bid=1.30, ask=1.20)


def test_quote_rejects_negative_bid() -> None:
    with pytest.raises(ValueError):
        quote(100.0, OptionRight.CALL, bid=-0.01)


# OptionChain


def chain(spot: float, quotes: list[OptionQuote]) -> OptionChain:
    return OptionChain("AAPL", spot, AS_OF, tuple(quotes))


def straddles(strikes: list[float], expiry: date = FRONT) -> list[OptionQuote]:
    rights = (OptionRight.CALL, OptionRight.PUT)
    return [quote(k, right, expiry=expiry) for k in strikes for right in rights]


def test_expiries_are_sorted_and_unique() -> None:
    c = chain(100.0, straddles([100.0], BACK) + straddles([95.0, 100.0]))

    assert c.expiries() == (FRONT, BACK)


def test_atm_strike_is_closest_to_spot() -> None:
    # |101 - 100| = 1 beats |101 - 105| = 4
    c = chain(101.0, straddles([95.0, 100.0, 105.0]))

    assert c.atm_strike(FRONT) == 100.0


def test_atm_strike_tie_goes_to_lower_strike() -> None:
    # spot 102.5 is 2.5 from both 100 and 105
    c = chain(102.5, straddles([100.0, 105.0]))

    assert c.atm_strike(FRONT) == 100.0


def test_atm_iv_averages_call_and_put_at_atm_strike() -> None:
    # ATM strike is 100 (spot 101); (0.50 + 0.30) / 2 = 0.40
    c = chain(
        101.0,
        [
            quote(95.0, OptionRight.CALL, iv=0.90),
            quote(95.0, OptionRight.PUT, iv=0.90),
            quote(100.0, OptionRight.CALL, iv=0.50),
            quote(100.0, OptionRight.PUT, iv=0.30),
            quote(105.0, OptionRight.CALL, iv=0.90),
            quote(105.0, OptionRight.PUT, iv=0.90),
        ],
    )

    assert c.atm_iv(FRONT) == pytest.approx(0.40)


def test_atm_iv_uses_the_one_side_that_has_iv() -> None:
    c = chain(
        100.0,
        [quote(100.0, OptionRight.CALL, iv=0.45), quote(100.0, OptionRight.PUT, iv=None)],
    )

    assert c.atm_iv(FRONT) == pytest.approx(0.45)


def test_atm_iv_raises_when_neither_side_has_iv() -> None:
    c = chain(
        100.0,
        [quote(100.0, OptionRight.CALL, iv=None), quote(100.0, OptionRight.PUT, iv=None)],
    )

    with pytest.raises(ValueError):
        c.atm_iv(FRONT)


def test_unknown_expiry_raises() -> None:
    with pytest.raises(KeyError):
        chain(100.0, straddles([100.0])).atm_strike(BACK)


def test_quote_looks_up_a_contract() -> None:
    c = chain(100.0, straddles([95.0, 100.0]))

    assert c.quote(contract(95.0, OptionRight.PUT)).contract.strike == 95.0


def test_chain_rejects_quotes_for_another_underlying() -> None:
    other = OptionQuote(
        OptionContract("MSFT", FRONT, 100.0, OptionRight.CALL), 1.0, 1.2, 0.4, 10, 1
    )

    with pytest.raises(ValueError):
        chain(100.0, [other])


def test_chain_rejects_duplicate_contracts() -> None:
    with pytest.raises(ValueError):
        chain(100.0, [quote(100.0, OptionRight.CALL), quote(100.0, OptionRight.CALL)])
