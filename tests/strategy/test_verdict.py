import pytest

from evbot.domain.signals import FilterResult, Verdict
from evbot.strategy.filters import AVG_VOLUME, IV_RV, SPREAD_AVAILABLE, TERM_SLOPE
from evbot.strategy.verdict import DefaultVerdictPolicy

R, C, A = Verdict.RECOMMENDED, Verdict.CONSIDER, Verdict.AVOID


@pytest.mark.parametrize(
    ("slope", "volume", "iv_rv", "spread", "expected"),
    [
        (True, True, True, True, R),
        (True, True, False, True, C),
        (True, False, True, True, C),
        (True, False, False, True, A),  # slope alone is not enough
        (False, True, True, True, A),
        (False, True, False, True, A),
        (False, False, True, True, A),
        (False, False, False, True, A),
        (True, True, True, False, A),  # no buildable spread is always AVOID
        (True, True, False, False, A),
        (True, False, True, False, A),
        (True, False, False, False, A),
        (False, True, True, False, A),
        (False, True, False, False, A),
        (False, False, True, False, A),
        (False, False, False, False, A),
    ],
)
def test_verdict_for_every_filter_combination(
    slope: bool, volume: bool, iv_rv: bool, spread: bool, expected: Verdict
) -> None:
    filters = (
        FilterResult(TERM_SLOPE, slope, None, None),
        FilterResult(AVG_VOLUME, volume, None, None),
        FilterResult(IV_RV, iv_rv, None, None),
        FilterResult(SPREAD_AVAILABLE, spread, None, None),
    )

    assert DefaultVerdictPolicy().decide(filters) is expected


def test_missing_filter_is_an_error() -> None:
    with pytest.raises(KeyError):
        DefaultVerdictPolicy().decide((FilterResult(TERM_SLOPE, True, None, None),))
