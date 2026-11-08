from pathlib import Path

import pytest

from evbot.config import CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH, load_config


def test_default_config_loads_strategy_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)

    config = load_config()

    assert config.filters.min_avg_volume_30d == 1_500_000
    assert config.filters.min_iv30_rv30 == 1.25
    assert config.filters.max_term_slope == -0.00406
    assert config.filters.max_leg_spread_pct == 0.10
    assert config.filters.min_open_interest == 500
    assert config.structure.back_expiry_offset_days == 30


def test_misspelled_key_is_rejected(tmp_path: Path) -> None:
    text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    path = tmp_path / "strategy.toml"
    path.write_text(text.replace("min_open_interest", "min_open_intrest"), encoding="utf-8")

    with pytest.raises(TypeError):
        load_config(path)


def test_env_var_overrides_default_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    path = tmp_path / "strategy.toml"
    path.write_text(text.replace("min_open_interest = 500", "min_open_interest = 42"), "utf-8")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(path))

    assert load_config().filters.min_open_interest == 42
