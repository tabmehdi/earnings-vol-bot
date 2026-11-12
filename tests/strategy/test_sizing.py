import pytest

from evbot.strategy.sizing import FixedRiskSizer

SIZER = FixedRiskSizer(max_risk_per_trade_pct=0.02, max_contracts=10)


def test_contracts_are_the_risk_budget_over_one_contracts_cost_rounded_down() -> None:
    # 2% of 100,000 = 2,000; one contract costs 2.10 * 100 = 210; 2,000 / 210 = 9.52 -> 9
    decision = SIZER.size(equity=100_000, debit=2.10, multiplier=100)

    assert decision.contracts == 9
    assert decision.reason == ""


def test_never_more_than_max_contracts() -> None:
    # 2,000 / 50 = 40, capped at 10
    assert SIZER.size(equity=100_000, debit=0.50, multiplier=100).contracts == 10


def test_a_contract_above_the_budget_means_skip_with_a_reason() -> None:
    # One contract costs 25.00 * 100 = 2,500, above the 2,000 budget
    decision = SIZER.size(equity=100_000, debit=25.00, multiplier=100)

    assert decision.contracts == 0
    assert "2,500" in decision.reason and "2,000" in decision.reason


@pytest.mark.parametrize("debit", [0.0, -0.10])
def test_no_debit_means_skip(debit: float) -> None:
    decision = SIZER.size(equity=100_000, debit=debit, multiplier=100)

    assert decision.contracts == 0
    assert decision.reason


def test_rejects_settings_outside_their_range() -> None:
    with pytest.raises(ValueError):
        FixedRiskSizer(max_risk_per_trade_pct=1.5, max_contracts=10)
    with pytest.raises(ValueError):
        FixedRiskSizer(max_risk_per_trade_pct=0.02, max_contracts=0)
