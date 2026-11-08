import pytest

from evbot.domain.options import OptionRight
from evbot.strategy.implied_vol import ImpliedVolSolver, black_scholes_price

RATE = 0.045
SPOT = 100.0


def test_black_scholes_call_matches_textbook_value() -> None:
    # S=K=100, T=1, r=5%, vol=20%: d1=0.35, d2=0.15; 100*N(0.35) - 100*exp(-0.05)*N(0.15) = 10.4506
    price = black_scholes_price(100.0, 100.0, 1.0, 0.05, 0.20, OptionRight.CALL)

    assert price == pytest.approx(10.4506, abs=1e-4)


def test_black_scholes_put_matches_textbook_value() -> None:
    # Put-call parity: 10.4506 - 100 + 100*exp(-0.05) = 5.5735
    price = black_scholes_price(100.0, 100.0, 1.0, 0.05, 0.20, OptionRight.PUT)

    assert price == pytest.approx(5.5735, abs=1e-4)


@pytest.mark.parametrize("right", [OptionRight.CALL, OptionRight.PUT])
@pytest.mark.parametrize(
    ("strike", "years", "vol"),
    [
        (100.0, 1 / 365, 0.90),
        (100.0, 8 / 365, 0.60),
        (100.0, 29 / 365, 0.20),
        (100.0, 0.25, 1.50),
        (105.0, 0.25, 0.35),
        (95.0, 1.0, 0.25),
    ],
)
def test_round_trip_recovers_the_vol(
    right: OptionRight, strike: float, years: float, vol: float
) -> None:
    price = black_scholes_price(SPOT, strike, years, RATE, vol, right)

    solved = ImpliedVolSolver().solve(price, SPOT, strike, years, right, RATE)

    assert solved == pytest.approx(vol, abs=1e-6)


def test_call_above_spot_is_out_of_bounds() -> None:
    assert ImpliedVolSolver().solve(100.5, SPOT, 100.0, 0.1, OptionRight.CALL, RATE) is None


def test_call_below_intrinsic_is_out_of_bounds() -> None:
    # Lower bound: 100 - 90 * exp(-0.045 * 0.1) = 10.40
    assert ImpliedVolSolver().solve(10.0, SPOT, 90.0, 0.1, OptionRight.CALL, RATE) is None


def test_put_above_discounted_strike_is_out_of_bounds() -> None:
    assert ImpliedVolSolver().solve(100.0, SPOT, 100.0, 0.1, OptionRight.PUT, RATE) is None


def test_zero_price_is_out_of_bounds() -> None:
    assert ImpliedVolSolver().solve(0.0, SPOT, 100.0, 0.1, OptionRight.CALL, RATE) is None


def test_expired_option_has_no_iv() -> None:
    assert ImpliedVolSolver().solve(1.0, SPOT, 100.0, 0.0, OptionRight.CALL, RATE) is None


def test_vol_outside_search_range_returns_none() -> None:
    price = black_scholes_price(SPOT, 100.0, 0.1, RATE, 2.0, OptionRight.CALL)

    assert (
        ImpliedVolSolver(max_vol=1.0).solve(price, SPOT, 100.0, 0.1, OptionRight.CALL, RATE) is None
    )


def test_returns_none_when_not_converged() -> None:
    price = black_scholes_price(SPOT, 100.0, 0.1, RATE, 0.5, OptionRight.CALL)
    solver = ImpliedVolSolver(max_iterations=2)

    assert solver.solve(price, SPOT, 100.0, 0.1, OptionRight.CALL, RATE) is None
