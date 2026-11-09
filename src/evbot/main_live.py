"""Composition root for the live bot: wires concrete adapters into the app services."""

import argparse
import json
import os
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from evbot.adapters.alpaca_market_data import AlpacaMarketData
from evbot.adapters.alpaca_trading_calendar import AlpacaTradingCalendar
from evbot.adapters.http_client import JsonHttpClient
from evbot.adapters.market_clock import MarketClock
from evbot.adapters.nasdaq_earnings_calendar import NasdaqEarningsCalendar
from evbot.adapters.parquet_snapshot_store import ParquetSnapshotStore
from evbot.adapters.rate_limiter import TokenBucket
from evbot.app.scan_preview import PreviewLine, PreviewReport, ScanPreview, ScanUniverse
from evbot.config import MarketDataConfig, StrategyConfig, load_config
from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.signals import Signal
from evbot.domain.snapshots import DataFeeds
from evbot.strategy.evaluator import SignalEvaluator
from evbot.strategy.filters import (
    LIQUIDITY,
    AvgVolumeFilter,
    IvRvFilter,
    LiquidityFilter,
    SpreadAvailableFilter,
    TermSlopeFilter,
)
from evbot.strategy.implied_vol import ImpliedVolSolver
from evbot.strategy.metrics import MetricsCalculator
from evbot.strategy.rates import ConstantRate
from evbot.strategy.selection import SpreadSelector
from evbot.strategy.verdict import DefaultVerdictPolicy
from evbot.strategy.volatility import YangZhangEstimator

REPORT_TIMINGS = (ReportTiming.BMO, ReportTiming.AMC, ReportTiming.UNKNOWN)
TICKERS_WARNING = (
    "WARNING: these tickers are scanned as if they reported next session, "
    "so verdicts aren't meaningful for names without an upcoming report."
)


def build_evaluator(config: StrategyConfig) -> SignalEvaluator:
    filters = config.filters
    return SignalEvaluator(
        metrics=MetricsCalculator(
            solver=ImpliedVolSolver(),
            rates=ConstantRate(config.pricing.risk_free_rate),
            volatility=YangZhangEstimator(
                config.metrics.rv_window, config.metrics.trading_days_per_year
            ),
            iv_days=config.metrics.iv_days,
            volume_window=config.metrics.volume_window,
            slope_horizon_days=filters.term_slope_horizon_days,
            max_iv_input_spread_pct=config.metrics.max_iv_input_spread_pct,
        ),
        selector=SpreadSelector(config.structure.back_expiry_offset_days),
        filters=(
            AvgVolumeFilter(filters.min_avg_volume_30d),
            IvRvFilter(filters.min_iv30_rv30),
            TermSlopeFilter(filters.max_term_slope),
            SpreadAvailableFilter(),
        ),
        liquidity=LiquidityFilter(),
        policy=DefaultVerdictPolicy(),
    )


def build_http(
    config: MarketDataConfig, client: httpx.Client, requests_per_minute: int
) -> JsonHttpClient:
    limiter = TokenBucket(requests_per_minute)
    return JsonHttpClient(client, config.max_retries, config.backoff_seconds, limiter=limiter)


def build_preview(
    config: StrategyConfig,
    clock: MarketClock,
    alpaca_http: JsonHttpClient,
    nasdaq_http: JsonHttpClient,
    key_id: str,
    secret_key: str,
) -> ScanPreview:
    market = config.market_data
    return ScanPreview(
        clock=clock,
        trading_calendar=AlpacaTradingCalendar(
            alpaca_http, market.trading_base_url, key_id, secret_key
        ),
        earnings=NasdaqEarningsCalendar(nasdaq_http, market.earnings_url),
        market_data=AlpacaMarketData(
            alpaca_http,
            clock,
            market.data_base_url,
            market.trading_base_url,
            key_id,
            secret_key,
            feeds=DataFeeds(market.options_feed, market.spot_feed, market.history_feed),
            chain_max_days=market.chain_max_days,
            strike_range_pct=market.strike_range_pct,
            max_quote_age_seconds=market.max_quote_age_seconds,
        ),
        store=ParquetSnapshotStore(Path(config.storage.snapshot_dir)),
        evaluator=build_evaluator(config),
        history_bars=max(config.metrics.rv_window + 1, config.metrics.volume_window),
    )


def parse_tickers(text: str) -> tuple[str, ...]:
    return tuple(ticker.strip().upper() for ticker in text.split(",") if ticker.strip())


def scan_log_row(line: PreviewLine) -> dict[str, Any]:
    signal = line.signal
    metrics = signal.metrics if signal else None
    legs = _leg_liquidity(signal) if signal else {}
    return {
        "symbol": line.event.symbol,
        "report_date": line.event.report_date.isoformat(),
        "timing": line.event.timing.value,
        "as_of": signal.as_of.isoformat() if signal else None,
        "verdict": signal.verdict.value if signal else None,
        "avg_volume_30d": metrics.avg_volume_30d if metrics else None,
        "iv30_rv30": metrics.iv30_rv30 if metrics else None,
        "term_slope": metrics.term_slope if metrics else None,
        "front_spread_pct": legs.get("front_spread_pct"),
        "front_open_interest": legs.get("front_open_interest"),
        "back_spread_pct": legs.get("back_spread_pct"),
        "back_open_interest": legs.get("back_open_interest"),
        "failed": [result.name for result in signal.failed()] if signal else [],
        "stale_quotes_dropped": line.stale_quotes_dropped,
        "error": line.error,
    }


def _leg_liquidity(signal: Signal) -> dict[str, float | int]:
    detail = next((r.detail for r in signal.filters if r.name == LIQUIDITY), "")
    pairs = (item.split("=") for item in detail.split() if "=" in item)
    return {key: int(value) if key.endswith("interest") else float(value) for key, value in pairs}


def write_scan_log(report: PreviewReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = (json.dumps(scan_log_row(line)) for line in report.lines)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def format_universe(universe: ScanUniverse) -> str:
    return "\n".join(
        [
            f"{universe.today} (today): {_timing_counts(universe.today_events)}",
            f"{universe.next_session} (next session): "
            f"{_timing_counts(universe.next_session_events)}",
            f"Selected: {len(universe.selected)} (AMC today + BMO and UNKNOWN next session)",
        ]
    )


def _timing_counts(events: Sequence[EarningsEvent]) -> str:
    counts = Counter(event.timing for event in events)
    return ", ".join(f"{timing.name} {counts[timing]}" for timing in REPORT_TIMINGS)


def format_report(report: PreviewReport) -> str:
    if not report.lines:
        return "No earnings reports selected."
    header = (
        f"{'SYMBOL':<8}{'TIMING':<9}{'VERDICT':<13}{'AVG VOL':>13}{'IV/RV':>8}"
        f"{'SLOPE':>11}{'STALE':>7}  NOTE"
    )
    return "\n".join([header, *map(_row, report.lines)])


def format_run_stats(alpaca_requests: int, nasdaq_requests: int, seconds: float) -> str:
    total = alpaca_requests + nasdaq_requests
    return (
        f"Requests: {total} (alpaca {alpaca_requests}, nasdaq {nasdaq_requests}). "
        f"Scan duration: {seconds:.1f}s"
    )


def _row(line: PreviewLine) -> str:
    signal = line.signal
    symbol, timing = line.event.symbol, line.event.timing.value
    if signal is None:
        return (
            f"{symbol:<8}{timing:<9}{'error':<13}{'-':>13}{'-':>8}{'-':>11}{'-':>7}  {line.error}"
        )
    metrics = signal.metrics
    volume = f"{metrics.avg_volume_30d:,.0f}" if metrics else "-"
    ratio = f"{metrics.iv30_rv30:.2f}" if metrics else "-"
    slope = f"{metrics.term_slope:.5f}" if metrics else "-"
    note = ", ".join(
        result.detail if metrics is None else result.name for result in signal.failed()
    )
    return (
        f"{symbol:<8}{timing:<9}{signal.verdict.value:<13}{volume:>13}{ratio:>8}"
        f"{slope:>11}{line.stale_quotes_dropped:>7}  {note}"
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="evbot.main_live")
    commands = parser.add_subparsers(dest="command", required=True)
    preview_command = commands.add_parser(
        "preview", help="print this scan's earnings tickers with live metrics; never trades"
    )
    preview_command.add_argument("--limit", type=int, help="only evaluate the first N tickers")
    preview_command.add_argument(
        "--tickers", help="comma-separated symbols to scan as if they report next session"
    )
    args = parser.parse_args(argv)
    tickers = parse_tickers(args.tickers) if args.tickers is not None else None
    if tickers is not None and not tickers:
        parser.error("--tickers needs at least one symbol")

    load_dotenv()
    key_id = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not key_id or not secret_key:
        parser.error("set ALPACA_API_KEY and ALPACA_SECRET_KEY in the environment or .env")
    if tickers:
        print(TICKERS_WARNING, flush=True)

    config = load_config()
    market = config.market_data
    clock = MarketClock()
    started = clock.now()
    with httpx.Client(timeout=market.timeout_seconds) as client:
        alpaca_http = build_http(market, client, market.alpaca_requests_per_minute)
        nasdaq_http = build_http(market, client, market.nasdaq_requests_per_minute)
        preview = build_preview(config, clock, alpaca_http, nasdaq_http, key_id, secret_key)
        universe = preview.universe_for(tickers) if tickers else preview.find_reports()
        print(format_universe(universe), flush=True)
        report = preview.evaluate(universe, args.limit)
    print(format_report(report))
    prefix = "tickers" if tickers else "scan"
    log_dir = Path(config.storage.snapshot_dir) / "signals" / f"date={started:%Y-%m-%d}"
    write_scan_log(report, log_dir / f"{prefix}_{started:%H%M%S}.jsonl")
    seconds = (clock.now() - started).total_seconds()
    print(format_run_stats(alpaca_http.request_count, nasdaq_http.request_count, seconds))


if __name__ == "__main__":
    main()
