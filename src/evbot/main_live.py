"""Composition root for the live bot: wires concrete adapters into the app services."""

import argparse
import logging
import os
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

from evbot.adapters.alpaca_market_data import AlpacaMarketData
from evbot.adapters.alpaca_trading_calendar import AlpacaTradingCalendar
from evbot.adapters.fred_risk_free_rate import FredRiskFreeRate
from evbot.adapters.http_client import JsonGetter, RequestsJsonHttpClient
from evbot.adapters.jsonl_trade_repository import JsonlTradeRepository
from evbot.adapters.market_clock import MarketClock
from evbot.adapters.nasdaq_earnings_calendar import NasdaqEarningsCalendar
from evbot.adapters.parquet_snapshot_store import ParquetSnapshotStore
from evbot.adapters.rate_limiter import TokenBucket
from evbot.app.scanner import MARKET_DATA, QUOTE_FRESHNESS, Scanner, ScanUniverse
from evbot.app.trade_scheduler import TradeScheduler
from evbot.config import MarketDataConfig, StrategyConfig, load_config
from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.scans import ScanKind, ScanRecord
from evbot.domain.signals import Signal, Verdict
from evbot.domain.snapshots import DataFeeds
from evbot.scan_lock import ScanLock
from evbot.strategy.evaluator import SignalEvaluator
from evbot.strategy.filters import (
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

logger = logging.getLogger("evbot")


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
    config: MarketDataConfig, session: requests.Session, requests_per_minute: int
) -> RequestsJsonHttpClient:
    """One client per external service, each on its own session, so nothing set for one
    service (cookies, connection state) is ever sent to another. Keys go per request."""
    return RequestsJsonHttpClient(
        session,
        config.timeout_seconds,
        config.max_retries,
        config.backoff_seconds,
        limiter=TokenBucket(requests_per_minute),
    )


def build_scanner(
    config: StrategyConfig,
    clock: MarketClock,
    alpaca_http: JsonGetter,
    nasdaq_http: JsonGetter,
    key_id: str,
    secret_key: str,
    rates: FredRiskFreeRate | None = None,
) -> Scanner:
    market = config.market_data
    calendar = AlpacaTradingCalendar(alpaca_http, market.trading_base_url, key_id, secret_key)
    return Scanner(
        clock=clock,
        trading_calendar=calendar,
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
        repository=JsonlTradeRepository(Path(config.storage.signals_dir)),
        evaluator=build_evaluator(config),
        scheduler=TradeScheduler(
            calendar,
            config.schedule.entry_minutes_before_close,
            config.schedule.exit_minutes_after_open,
        ),
        history_bars=max(config.metrics.rv_window + 1, config.metrics.volume_window),
        fallback_rate=config.pricing.risk_free_rate,
        rates=rates,
    )


def build_rates(
    config: MarketDataConfig, session: requests.Session, api_key: str | None
) -> FredRiskFreeRate | None:
    """None without a key: the scan then uses [pricing].risk_free_rate and says so."""
    if not api_key:
        return None
    http = build_http(config, session, config.fred_requests_per_minute)
    return FredRiskFreeRate(http, config.fred_url, config.fred_series, api_key)


def parse_tickers(text: str) -> tuple[str, ...]:
    return tuple(ticker.strip().upper() for ticker in text.split(",") if ticker.strip())


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


def format_report(scan: ScanRecord) -> str:
    if not scan.signals:
        return "No earnings reports selected."
    header = (
        f"{'SYMBOL':<8}{'TIMING':<9}{'VERDICT':<13}{'AVG VOL':>13}{'IV/RV':>8}"
        f"{'SLOPE':>11}{'STALE':>7}  NOTE"
    )
    return "\n".join([header, *map(_row, scan.signals)])


def _row(signal: Signal) -> str:
    symbol, timing = signal.event.symbol, signal.event.timing.value
    metrics = signal.metrics
    volume = f"{metrics.avg_volume_30d:,.0f}" if metrics else "-"
    ratio = f"{metrics.iv30_rv30:.2f}" if metrics else "-"
    slope = f"{metrics.term_slope:.5f}" if metrics else "-"
    freshness = next((r.detail for r in signal.filters if r.name == QUOTE_FRESHNESS), "")
    stale = freshness.partition("=")[2] or "-"
    note = ", ".join(r.detail if metrics is None else r.name for r in signal.failed())
    return (
        f"{symbol:<8}{timing:<9}{signal.verdict.value:<13}{volume:>13}{ratio:>8}"
        f"{slope:>11}{stale:>7}  {note}"
    )


def format_summary(scan: ScanRecord, alpaca_requests: int, nasdaq_requests: int) -> str:
    verdicts = Counter(signal.verdict for signal in scan.signals)
    failures = sum(any(r.name == MARKET_DATA for r in signal.failed()) for signal in scan.signals)
    seconds = (scan.finished_at - scan.started_at).total_seconds()
    on_time = {True: "yes", False: "NO", None: "n/a"}[scan.finished_before_entry]
    return (
        f"{scan.kind.value} scan of {scan.started_at:%Y-%m-%d %H:%M %Z} done in {seconds:.1f}s: "
        f"{len(scan.signals)} signals ({verdicts[Verdict.RECOMMENDED]} recommended, "
        f"{verdicts[Verdict.CONSIDER]} consider, {verdicts[Verdict.AVOID]} avoid, "
        f"of which {failures} data failures); "
        f"{alpaca_requests + nasdaq_requests} requests (alpaca {alpaca_requests}, "
        f"nasdaq {nasdaq_requests}); finished before entry: {on_time}"
        f"{_rate_text(scan)}"
    )


def _rate_text(scan: ScanRecord) -> str:
    if scan.risk_free_rate is None:
        return ""
    return f"; rate {scan.risk_free_rate:.2%} ({scan.rate_source})"


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="evbot.main_live")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("scan", help="the unattended daily scan; logs to the logs folder")
    preview_command = commands.add_parser(
        "preview", help="run this scan's earnings tickers and print the table; never trades"
    )
    preview_command.add_argument("--limit", type=int, help="only evaluate the first N tickers")
    preview_command.add_argument(
        "--tickers", help="comma-separated symbols to scan as if they report next session"
    )
    args = parser.parse_args(argv)
    tickers = None
    if args.command == "preview" and args.tickers is not None:
        tickers = parse_tickers(args.tickers)
        if not tickers:
            parser.error("--tickers needs at least one symbol")

    load_dotenv()
    key_id = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    fred_key = os.environ.get("FRED_API_KEY")
    if not key_id or not secret_key:
        parser.error("set ALPACA_API_KEY and ALPACA_SECRET_KEY in the environment or .env")

    config = load_config()
    if args.command == "scan":
        _run_scheduled(config, key_id, secret_key, fred_key)
    else:
        _run_preview(config, key_id, secret_key, fred_key, tickers, args.limit)


def _run_preview(
    config: StrategyConfig,
    key_id: str,
    secret_key: str,
    fred_key: str | None,
    tickers: tuple[str, ...] | None,
    limit: int | None,
) -> None:
    if tickers:
        print(TICKERS_WARNING, flush=True)
    market = config.market_data
    clock = MarketClock()
    with (
        requests.Session() as alpaca_session,
        requests.Session() as nasdaq_session,
        requests.Session() as fred_session,
    ):
        alpaca_http = build_http(market, alpaca_session, market.alpaca_requests_per_minute)
        nasdaq_http = build_http(market, nasdaq_session, market.nasdaq_requests_per_minute)
        rates = build_rates(market, fred_session, fred_key)
        scanner = build_scanner(config, clock, alpaca_http, nasdaq_http, key_id, secret_key, rates)
        universe = scanner.universe_for(tickers) if tickers else scanner.find_reports()
        print(format_universe(universe), flush=True)
        kind = ScanKind.TICKERS if tickers else ScanKind.PREVIEW
        scan = scanner.run(universe, kind, limit)
    print(format_report(scan))
    print(format_summary(scan, alpaca_http.request_count, nasdaq_http.request_count))


def _run_scheduled(
    config: StrategyConfig, key_id: str, secret_key: str, fred_key: str | None
) -> None:
    clock = MarketClock()
    today = clock.now().date()
    handlers = _log_to(Path(config.storage.logs_dir) / f"scan_{today:%Y-%m-%d}.log")
    lock = ScanLock(Path(config.storage.locks_dir) / f"scan_{today:%Y-%m-%d}.lock")
    try:
        if not lock.acquire():
            logger.info("another scan is running (lock %s); exiting", lock.path)
            return
        _scheduled_scan(config, clock, today, (key_id, secret_key, fred_key))
    except Exception:
        logger.exception("scan failed")
        raise SystemExit(1) from None
    finally:
        lock.release()
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()


def _scheduled_scan(
    config: StrategyConfig,
    clock: MarketClock,
    today: date,
    keys: tuple[str, str, str | None],
) -> None:
    key_id, secret_key, fred_key = keys
    logger.info("scan started for %s", today)
    market = config.market_data
    with (
        requests.Session() as alpaca_session,
        requests.Session() as nasdaq_session,
        requests.Session() as fred_session,
    ):
        alpaca_http = build_http(market, alpaca_session, market.alpaca_requests_per_minute)
        nasdaq_http = build_http(market, nasdaq_session, market.nasdaq_requests_per_minute)
        rates = build_rates(market, fred_session, fred_key)
        scanner = build_scanner(config, clock, alpaca_http, nasdaq_http, key_id, secret_key, rates)
        reason = scanner.skip_reason()
        if reason is not None:
            logger.info("skipped: %s", reason)
            return
        universe = scanner.find_reports()
        for line in format_universe(universe).splitlines():
            logger.info(line)
        scan = scanner.run(universe, ScanKind.SCHEDULED)
    logger.info(format_summary(scan, alpaca_http.request_count, nasdaq_http.request_count))
    if scan.finished_before_entry is False:
        logger.warning("scan finished after the earliest entry time %s", scan.entry_deadline)


def _log_to(path: Path) -> list[logging.Handler]:
    path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers: list[logging.Handler] = [
        logging.FileHandler(path, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return handlers


if __name__ == "__main__":
    main()
