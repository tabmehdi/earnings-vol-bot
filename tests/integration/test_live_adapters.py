"""Calls the real services. Run in US market hours: quotes older than 5 min are dropped."""

import os
from datetime import timedelta

import pytest
import requests
from dotenv import load_dotenv

from evbot.adapters.alpaca_market_data import AlpacaMarketData
from evbot.adapters.alpaca_trading_calendar import AlpacaTradingCalendar
from evbot.adapters.fred_risk_free_rate import FredRiskFreeRate
from evbot.adapters.http_client import RequestsJsonHttpClient
from evbot.adapters.market_clock import MarketClock
from evbot.adapters.nasdaq_earnings_calendar import NasdaqEarningsCalendar
from evbot.config import load_config
from evbot.domain.snapshots import DataFeeds

pytestmark = pytest.mark.integration

CONFIG = load_config().market_data


@pytest.fixture
def http() -> RequestsJsonHttpClient:
    """Alpaca's client; Nasdaq gets its own session below, as in main_live."""
    return client_on_new_session()


def client_on_new_session() -> RequestsJsonHttpClient:
    return RequestsJsonHttpClient(
        requests.Session(), CONFIG.timeout_seconds, CONFIG.max_retries, CONFIG.backoff_seconds
    )


@pytest.fixture
def keys() -> tuple[str, str]:
    load_dotenv()
    key_id, secret_key = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not key_id or not secret_key:
        pytest.skip("Alpaca keys are not set")
    return key_id, secret_key


@pytest.fixture
def calendar(http: RequestsJsonHttpClient, keys: tuple[str, str]) -> AlpacaTradingCalendar:
    return AlpacaTradingCalendar(http, CONFIG.trading_base_url, *keys)


@pytest.fixture
def market_data(http: RequestsJsonHttpClient, keys: tuple[str, str]) -> AlpacaMarketData:
    return AlpacaMarketData(
        http,
        MarketClock(),
        CONFIG.data_base_url,
        CONFIG.trading_base_url,
        *keys,
        feeds=DataFeeds(CONFIG.options_feed, CONFIG.spot_feed, CONFIG.history_feed),
        chain_max_days=CONFIG.chain_max_days,
        strike_range_pct=CONFIG.strike_range_pct,
        max_quote_age_seconds=CONFIG.max_quote_age_seconds,
    )


@pytest.fixture
def fred() -> FredRiskFreeRate:
    load_dotenv()
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        pytest.skip("FRED key is not set")
    return FredRiskFreeRate(client_on_new_session(), CONFIG.fred_url, CONFIG.fred_series, api_key)


def test_fred_gives_a_plausible_decimal_rate_for_today(fred: FredRiskFreeRate) -> None:
    rate = fred.rate_on(MarketClock().now().date())

    # A decimal, not percent: 3-month Treasury yields have stayed between 0% and 20%.
    assert 0.0 <= rate < 0.20


def test_fred_answers_on_a_weekend_with_the_last_trading_days_value(
    fred: FredRiskFreeRate,
) -> None:
    today = MarketClock().now().date()
    last_saturday = today - timedelta(days=(today.weekday() - 5) % 7 or 7)
    previous_friday = last_saturday - timedelta(days=1)

    assert fred.rate_on(last_saturday) == fred.rate_on(previous_friday)


def test_nasdaq_returns_a_list_for_the_coming_week() -> None:
    calendar = NasdaqEarningsCalendar(client_on_new_session(), CONFIG.earnings_url)
    today = MarketClock().now().date()

    found = [calendar.events_on(today + timedelta(days=offset)) for offset in range(7)]

    assert any(found), "expected at least one earnings report within a week"


def test_alpaca_calendar_finds_real_sessions(calendar: AlpacaTradingCalendar) -> None:
    today = MarketClock().now().date()

    session = calendar.next_session(today)

    assert calendar.close_time(session) > calendar.open_time(session)
    assert calendar.previous_session(session) < session


def test_history_has_31_completed_sip_bars(
    market_data: AlpacaMarketData, calendar: AlpacaTradingCalendar
) -> None:
    end = calendar.previous_session(MarketClock().now().date())

    history = market_data.get_history("SPY", end, 31)

    assert len(history.bars) == 31
    assert history.bars[-1].day == end
    # SIP is the whole market: IEX alone would be a small fraction of this.
    assert sum(bar.volume for bar in history.bars) / 31 > 10_000_000


def test_chain_has_fresh_standard_contracts(market_data: AlpacaMarketData) -> None:
    snapshot = market_data.get_chain("SPY")
    chain = snapshot.chain

    assert chain.quotes
    assert chain.spot > 0
    assert all(q.contract.multiplier == 100 for q in chain.quotes)
    assert any(q.open_interest > 0 for q in chain.quotes)
    assert all(
        abs(q.contract.strike / chain.spot - 1) <= CONFIG.strike_range_pct + 0.01
        for q in chain.quotes
    )
