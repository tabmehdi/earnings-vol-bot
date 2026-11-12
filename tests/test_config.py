from pathlib import Path

import pytest

from evbot.config import CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH, load_config


def test_default_config_loads_strategy_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)

    config = load_config()

    assert config.filters.min_avg_volume_30d == 1_500_000
    assert config.filters.min_iv30_rv30 == 1.25
    assert config.filters.max_term_slope == -0.00406
    assert config.structure.back_target_days == 30
    assert config.structure.back_min_gap_days == 21


def test_misspelled_key_is_rejected(tmp_path: Path) -> None:
    text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    path = tmp_path / "strategy.toml"
    path.write_text(text.replace("min_iv30_rv30", "min_iv30_rv3"), encoding="utf-8")

    with pytest.raises(TypeError):
        load_config(path)


def test_env_var_overrides_default_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    path = tmp_path / "strategy.toml"
    path.write_text(text.replace("min_iv30_rv30 = 1.25", "min_iv30_rv30 = 2.5"), "utf-8")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(path))

    assert load_config().filters.min_iv30_rv30 == 2.5


def test_default_config_loads_pricing_and_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)

    config = load_config()

    assert config.pricing.risk_free_rate == 0.045
    assert config.metrics.iv_days == 30
    assert config.metrics.rv_window == 30
    assert config.metrics.volume_window == 30
    assert config.metrics.trading_days_per_year == 252
    assert config.metrics.max_iv_input_spread_pct == 0.50


def test_default_config_loads_market_data_and_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)

    config = load_config()

    assert config.market_data.options_feed == "indicative"
    assert config.market_data.spot_feed == "iex"
    assert config.market_data.history_feed == "sip"
    assert config.market_data.strike_range_pct == 0.25
    assert config.market_data.max_quote_age_seconds == 300
    assert config.market_data.alpaca_requests_per_minute == 180
    assert config.market_data.nasdaq_requests_per_minute == 30
    assert config.market_data.fred_url == "https://api.stlouisfed.org/fred/series/observations"
    assert config.market_data.fred_series == "DGS3MO"
    assert config.market_data.fred_requests_per_minute == 30
    assert config.storage.snapshot_dir == "data/snapshots"
    assert config.storage.signals_dir == "data/signals"
    assert config.storage.logs_dir == "data/logs"
    assert config.storage.locks_dir == "data/locks"
    assert config.schedule.scan_start == "15:25"
