"""Composition root for the live bot: wires concrete adapters into the app services."""

import argparse
import os
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

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
from evbot.domain.snapshots import DataFeeds
from evbot.strategy.evaluator import SignalEvaluator
from evbot.strategy.filters import (
    AvgVolumeFilter,
    IvRvFilter,
    LiquidityFilter,
    TermSlopeFilter,
)
from evbot.strategy.implied_vol import ImpliedVolSolver
from evbot.strategy.metrics import MetricsCalculator
from evbot.strategy.rates import ConstantRate
from evbot.strategy.selection import SpreadSelector
from evbot.strategy.verdict import DefaultVerdictPolicy
from evbot.strategy.volatility import YangZhangEstimator

REPORT_TIMINGS = (ReportTiming.BMO, ReportTiming.AMC, ReportTiming.UNKNOWN)


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
        ),
        selector=SpreadSelector(config.structure.back_expiry_offset_days),
        filters=(
            AvgVolumeFilter(filters.min_avg_volume_30d),
            IvRvFilter(filters.min_iv30_rv30),
            TermSlopeFilter(filters.max_term_slope),
            LiquidityFilter(filters.max_leg_spread_pct, filters.min_open_interest),
        ),
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
    args = parser.parse_args(argv)

    load_dotenv()
    key_id = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not key_id or not secret_key:
        parser.error("set ALPACA_API_KEY and ALPACA_SECRET_KEY in the environment or .env")

    config = load_config()
    market = config.market_data
    clock = MarketClock()
    started = clock.now()
    with httpx.Client(timeout=market.timeout_seconds) as client:
        alpaca_http = build_http(market, client, market.alpaca_requests_per_minute)
        nasdaq_http = build_http(market, client, market.nasdaq_requests_per_minute)
        preview = build_preview(config, clock, alpaca_http, nasdaq_http, key_id, secret_key)
        universe = preview.find_reports()
        print(format_universe(universe), flush=True)
        report = preview.evaluate(universe, args.limit)
    print(format_report(report))
    seconds = (clock.now() - started).total_seconds()
    print(format_run_stats(alpaca_http.request_count, nasdaq_http.request_count, seconds))


if __name__ == "__main__":
    main()
