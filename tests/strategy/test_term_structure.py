import pytest

from evbot.strategy.term_structure import TermStructure

# Reference values below come from running build_term_structure in the legacy calculator.py
# once on these points. That script is not part of the repo, so the numbers are pasted here.
POINTS = ((1, 0.90), (8, 0.60), (29, 0.50), (57, 0.45))


def test_iv_at_a_known_point() -> None:
    assert TermStructure(POINTS).iv_at(29) == pytest.approx(0.50)


@pytest.mark.parametrize(
    ("days", "legacy_iv"),
    [(5, 0.7285714285714284), (30, 0.4982142857142857), (45, 0.4714285714285714)],
)
def test_iv_at_interpolates_like_legacy(days: int, legacy_iv: float) -> None:
    assert TermStructure(POINTS).iv_at(days) == pytest.approx(legacy_iv, abs=1e-12)


def test_iv_is_flat_before_first_point() -> None:
    assert TermStructure(POINTS).iv_at(0) == pytest.approx(0.90)


def test_iv_is_flat_after_last_point() -> None:
    assert TermStructure(POINTS).iv_at(70) == pytest.approx(0.45)


def test_slope_matches_legacy_front_to_45() -> None:
    # Legacy: (spline(45) - spline(1)) / (45 - 1) = -0.009740259740259742
    assert TermStructure(POINTS).slope(1, 45) == pytest.approx(-0.009740259740259742, abs=1e-12)


def test_slope_rejects_equal_days() -> None:
    with pytest.raises(ValueError):
        TermStructure(POINTS).slope(45, 45)


def test_first_day_is_the_earliest_point() -> None:
    assert TermStructure(POINTS).first_day == 1


def test_needs_two_points() -> None:
    with pytest.raises(ValueError):
        TermStructure(((1, 0.5),))


def test_rejects_unsorted_points() -> None:
    with pytest.raises(ValueError):
        TermStructure(((8, 0.5), (1, 0.6)))
