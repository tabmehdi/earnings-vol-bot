"""The same assertions run against each fake and its real adapter, so the fakes stay honest."""

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from evbot.adapters.alpaca_trading_calendar import AlpacaTradingCalendar
from evbot.adapters.jsonl_trade_repository import JsonlTradeRepository
from evbot.adapters.market_clock import MarketClock
from evbot.adapters.nasdaq_earnings_calendar import NasdaqEarningsCalendar
from evbot.adapters.parquet_snapshot_store import ParquetSnapshotStore
from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.prices import Bar, PriceHistory
from evbot.domain.scans import ScanKind, ScanRecord
from evbot.domain.snapshots import ChainSnapshot
from evbot.ports.clock import Clock
from evbot.ports.earnings_calendar import EarningsCalendar
from evbot.ports.errors import MarketDataError, SnapshotError, TradingCalendarError
from evbot.ports.market_data import MarketDataProvider
from evbot.ports.snapshot_store import SnapshotStore
from evbot.ports.trade_repository import TradeRepository
from evbot.ports.trading_calendar import TradingCalendar
from tests.fakes.ports import (
    FakeEarningsCalendar,
    FakeMarketData,
    FakeTradingCalendar,
    FixedClock,
    InMemorySnapshotStore,
    InMemoryTradeRepository,
)
from tests.fixtures.alpaca_server import (
    CALL,
    FEEDS,
    SCAN_TIME,
    AlpacaServer,
    bar,
    contract_row,
    option_snapshot,
)
from tests.fixtures.http import json_client

NEW_YORK = ZoneInfo("America/New_York")
REPORT_DAY = date(2026, 11, 10)
QUIET_DAY = date(2026, 11, 8)


# Clock


@pytest.mark.parametrize("clock", [FixedClock(SCAN_TIME), MarketClock()], ids=["fake", "adapter"])
def test_clock_is_timezone_aware(clock: Clock) -> None:
    assert clock.now().utcoffset() is not None


# EarningsCalendar


def nasdaq() -> NasdaqEarningsCalendar:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["date"] == REPORT_DAY.isoformat():
            row = {"symbol": "AAPL", "time": "time-pre-market"}
            return httpx.Response(200, json={"data": {"rows": [row]}})
        return httpx.Response(200, json={"data": {"rows": None}})

    return NasdaqEarningsCalendar(json_client(handler, max_retries=0), "https://nasdaq.test/x")


@pytest.fixture(params=["fake", "adapter"])
def earnings(request: pytest.FixtureRequest) -> EarningsCalendar:
    if request.param == "fake":
        return FakeEarningsCalendar((EarningsEvent("AAPL", REPORT_DAY, ReportTiming.BMO),))
    return nasdaq()


def test_events_on_returns_that_days_reports(earnings: EarningsCalendar) -> None:
    events = earnings.events_on(REPORT_DAY)

    assert [(e.symbol, e.report_date) for e in events] == [("AAPL", REPORT_DAY)]


def test_events_on_a_quiet_day_is_empty(earnings: EarningsCalendar) -> None:
    assert earnings.events_on(QUIET_DAY) == ()


# MarketDataProvider


def alpaca_server() -> AlpacaServer:
    return AlpacaServer(
        snapshot_pages=[{CALL: option_snapshot()}],
        contract_pages=[[contract_row(CALL)]],
        bar_pages=[[bar("2026-11-16"), bar("2026-11-17"), bar("2026-11-18")]],
    )


@pytest.fixture(params=["fake", "adapter"])
def market_data(request: pytest.FixtureRequest) -> MarketDataProvider:
    adapter = alpaca_server().adapter()
    if request.param == "adapter":
        return adapter
    bars = tuple(Bar(date(2026, 11, day), 100.0, 101.0, 99.0, 100.0, 1_000) for day in (16, 17, 18))
    return FakeMarketData({"AAPL": adapter.get_chain("AAPL")}, {"AAPL": PriceHistory("AAPL", bars)})


def test_get_chain_returns_a_snapshot_for_the_symbol(market_data: MarketDataProvider) -> None:
    snapshot = market_data.get_chain("AAPL")

    assert isinstance(snapshot, ChainSnapshot)
    assert snapshot.chain.underlying == "AAPL"
    assert snapshot.feeds == FEEDS


def test_get_chain_for_an_unknown_symbol_raises(market_data: MarketDataProvider) -> None:
    with pytest.raises(MarketDataError):
        market_data.get_chain("NOPE")


def test_get_history_returns_the_latest_bars_up_to_end(market_data: MarketDataProvider) -> None:
    history = market_data.get_history("AAPL", date(2026, 11, 18), 2)

    assert [b.day.day for b in history.bars] == [17, 18]


def test_get_history_excludes_bars_after_end(market_data: MarketDataProvider) -> None:
    history = market_data.get_history("AAPL", date(2026, 11, 17), 5)

    assert history.bars[-1].day == date(2026, 11, 17)


# TradingCalendar

OPEN_DAYS = (date(2026, 11, 25), date(2026, 11, 27))
HOLIDAY = date(2026, 11, 26)


def alpaca_calendar() -> AlpacaTradingCalendar:
    rows = [{"date": d.isoformat(), "open": "09:30", "close": "16:00"} for d in OPEN_DAYS]
    client = json_client(lambda _: httpx.Response(200, json=rows), max_retries=0)
    return AlpacaTradingCalendar(client, "https://alpaca.test", "key", "secret")


@pytest.fixture(params=["fake", "adapter"])
def trading_calendar(request: pytest.FixtureRequest) -> TradingCalendar:
    if request.param == "adapter":
        return alpaca_calendar()
    sessions = {
        d: (
            datetime(d.year, d.month, d.day, 9, 30, tzinfo=NEW_YORK),
            datetime(d.year, d.month, d.day, 16, 0, tzinfo=NEW_YORK),
        )
        for d in OPEN_DAYS
    }
    return FakeTradingCalendar(sessions)


def test_neighbouring_sessions_skip_closed_days(trading_calendar: TradingCalendar) -> None:
    assert trading_calendar.next_session(OPEN_DAYS[0]) == OPEN_DAYS[1]
    assert trading_calendar.previous_session(OPEN_DAYS[1]) == OPEN_DAYS[0]


def test_session_times_are_aware_and_ordered(trading_calendar: TradingCalendar) -> None:
    opens = trading_calendar.open_time(OPEN_DAYS[0])
    closes = trading_calendar.close_time(OPEN_DAYS[0])

    assert opens.utcoffset() is not None
    assert closes - opens == timedelta(hours=6, minutes=30)


def test_closed_day_has_no_times(trading_calendar: TradingCalendar) -> None:
    with pytest.raises(TradingCalendarError):
        trading_calendar.close_time(HOLIDAY)


# SnapshotStore


@pytest.fixture(params=["fake", "adapter"])
def snapshot_store(request: pytest.FixtureRequest, tmp_path: Path) -> SnapshotStore:
    if request.param == "fake":
        return InMemorySnapshotStore()
    return ParquetSnapshotStore(tmp_path)


def test_store_returns_the_chain_it_saved(snapshot_store: SnapshotStore) -> None:
    snapshot = alpaca_server().adapter().get_chain("AAPL")

    snapshot_store.save_chain(snapshot)

    assert snapshot_store.load_chain("AAPL", snapshot.chain.as_of) == snapshot


def test_store_returns_the_history_it_saved(snapshot_store: SnapshotStore) -> None:
    history = alpaca_server().adapter().get_history("AAPL", date(2026, 11, 18), 3)

    snapshot_store.save_history(history, SCAN_TIME, FEEDS)

    assert snapshot_store.load_history("AAPL", SCAN_TIME) == history


def test_store_raises_for_an_unknown_chain(snapshot_store: SnapshotStore) -> None:
    with pytest.raises(SnapshotError):
        snapshot_store.load_chain("AAPL", SCAN_TIME)


# TradeRepository


@pytest.fixture(params=["fake", "adapter"])
def trade_repository(request: pytest.FixtureRequest, tmp_path: Path) -> TradeRepository:
    if request.param == "fake":
        return InMemoryTradeRepository()
    return JsonlTradeRepository(tmp_path)


def scan_at(started: datetime, kind: ScanKind = ScanKind.SCHEDULED) -> ScanRecord:
    return ScanRecord(kind, started, started + timedelta(minutes=4), ())


def test_repository_returns_the_days_scans_in_order(trade_repository: TradeRepository) -> None:
    afternoon = scan_at(SCAN_TIME)
    morning = scan_at(SCAN_TIME - timedelta(hours=5), ScanKind.PREVIEW)

    trade_repository.save_scan(afternoon)
    trade_repository.save_scan(morning)

    assert trade_repository.scans_on(SCAN_TIME.date()) == (morning, afternoon)


def test_repository_day_is_the_new_york_day(trade_repository: TradeRepository) -> None:
    # 23:30 New York on Nov 19 is already Nov 20 in UTC.
    evening = datetime(2026, 11, 19, 23, 30, tzinfo=NEW_YORK)

    trade_repository.save_scan(scan_at(evening))

    assert len(trade_repository.scans_on(date(2026, 11, 19))) == 1
    assert trade_repository.scans_on(date(2026, 11, 20)) == ()
