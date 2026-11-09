import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_ENV_VAR = "EVBOT_CONFIG"
# Only valid when running from a source checkout; installed copies must set EVBOT_CONFIG.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "strategy.toml"


@dataclass(frozen=True, slots=True)
class FilterConfig:
    min_avg_volume_30d: int
    min_iv30_rv30: float
    max_term_slope: float
    term_slope_horizon_days: int
    max_leg_spread_pct: float
    min_open_interest: int


@dataclass(frozen=True, slots=True)
class StructureConfig:
    back_expiry_offset_days: int


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    entry_minutes_before_close: int
    exit_minutes_after_open: int


@dataclass(frozen=True, slots=True)
class PricingConfig:
    risk_free_rate: float


@dataclass(frozen=True, slots=True)
class MetricsConfig:
    iv_days: int
    rv_window: int
    volume_window: int
    trading_days_per_year: int


@dataclass(frozen=True, slots=True)
class MarketDataConfig:
    data_base_url: str
    trading_base_url: str
    earnings_url: str
    options_feed: str
    spot_feed: str
    history_feed: str
    chain_max_days: int
    strike_range_pct: float
    max_quote_age_seconds: int
    max_retries: int
    backoff_seconds: float
    timeout_seconds: float
    alpaca_requests_per_minute: int
    nasdaq_requests_per_minute: int


@dataclass(frozen=True, slots=True)
class StorageConfig:
    snapshot_dir: str


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    filters: FilterConfig
    structure: StructureConfig
    schedule: ScheduleConfig
    pricing: PricingConfig
    metrics: MetricsConfig
    market_data: MarketDataConfig
    storage: StorageConfig


def config_path() -> Path:
    override = os.environ.get(CONFIG_ENV_VAR)
    return Path(override) if override else DEFAULT_CONFIG_PATH


def load_config(path: Path | None = None) -> StrategyConfig:
    with (path or config_path()).open("rb") as f:
        raw: dict[str, Any] = tomllib.load(f)
    # Unknown or missing keys raise TypeError here, so a typo in the TOML fails loudly.
    return StrategyConfig(
        filters=FilterConfig(**raw["filters"]),
        structure=StructureConfig(**raw["structure"]),
        schedule=ScheduleConfig(**raw["schedule"]),
        pricing=PricingConfig(**raw["pricing"]),
        metrics=MetricsConfig(**raw["metrics"]),
        market_data=MarketDataConfig(**raw["market_data"]),
        storage=StorageConfig(**raw["storage"]),
    )
