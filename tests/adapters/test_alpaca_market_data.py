from datetime import date, timedelta
from typing import Any

import pytest

from evbot.domain.options import OptionRight
from evbot.ports.errors import MarketDataError
from tests.fixtures.alpaca_server import (
    BACK_CALL,
    BACK_ISO,
    CALL,
    FEEDS,
    PUT,
    SCAN_TIME,
    AlpacaServer,
    bar,
    contract_row,
    option_snapshot,
)

SNAPSHOTS_PATH = "/v1beta1/options/snapshots/AAPL"
CONTRACTS_PATH = "/v2/options/contracts"
BARS_PATH = "/v2/stocks/bars"
LAST_SESSION = date(2026, 11, 18)


def server_with(
    snapshots: dict[str, Any] | None = None, contracts: list[dict[str, Any]] | None = None
) -> AlpacaServer:
    return AlpacaServer(
        snapshot_pages=[{CALL: option_snapshot()} if snapshots is None else snapshots],
        contract_pages=[[contract_row(CALL)] if contracts is None else contracts],
    )


# Merging the two option endpoints


def test_quote_comes_from_the_snapshot_and_open_interest_from_the_contract() -> None:
    quote = server_with().adapter().get_chain("AAPL").chain.quotes[0]

    assert quote.bid == 1.00
    assert quote.ask == 1.10
    assert quote.iv == 0.45
    assert quote.open_interest == 750
    assert quote.contract.strike == 100.0
    assert quote.contract.expiry == date(2026, 11, 20)
    assert quote.contract.right is OptionRight.CALL
    assert quote.contract.multiplier == 100


def test_option_trades_are_never_used() -> None:
    # The fixture snapshot carries latestTrade and a dailyBar volume of 12345; neither may appear.
    assert server_with().adapter().get_chain("AAPL").chain.quotes[0].volume == 0


def test_missing_open_interest_counts_as_zero() -> None:
    server = server_with(contracts=[contract_row(CALL, open_interest=None)])

    assert server.adapter().get_chain("AAPL").chain.quotes[0].open_interest == 0


def test_chain_reports_the_feeds_used() -> None:
    assert server_with().adapter().get_chain("AAPL").feeds == FEEDS


# Spot


def test_spot_is_the_iex_quote_mid() -> None:
    server = server_with()

    # (99.90 + 100.10) / 2 = 100.00
    assert server.adapter().get_chain("AAPL").chain.spot == pytest.approx(100.0)
    assert server.requests_to("/v2/stocks/AAPL/quotes/latest")[0].url.params["feed"] == "iex"
    assert server.requests_to("/v2/stocks/AAPL/trades/latest") == []


@pytest.mark.parametrize(
    "bad_quote",
    [
        None,
        {"bp": 100.20, "ap": 100.10},  # crossed
        {"bp": 0, "ap": 100.10},  # one-sided
        {"bp": 99.90, "ap": 0},
    ],
)
def test_spot_falls_back_to_the_iex_trade(bad_quote: dict[str, Any] | None) -> None:
    server = server_with()
    server.stock_quote = bad_quote

    assert server.adapter().get_chain("AAPL").chain.spot == 98.0
    assert server.requests_to("/v2/stocks/AAPL/trades/latest")[0].url.params["feed"] == "iex"


def test_no_quote_and_no_trade_is_an_error() -> None:
    server = server_with()
    server.stock_quote = None
    server.stock_trade = None

    with pytest.raises(MarketDataError):
        server.adapter().get_chain("AAPL")


# Request filters


@pytest.mark.parametrize("path", [SNAPSHOTS_PATH, CONTRACTS_PATH])
def test_requests_only_strikes_near_spot_and_expiries_in_range(path: str) -> None:
    server = server_with()

    server.adapter().get_chain("AAPL")

    params = server.requests_to(path)[0].url.params
    assert float(params["strike_price_gte"]) == 75.0  # 100 * 0.75
    assert float(params["strike_price_lte"]) == 125.0  # 100 * 1.25
    assert params["expiration_date_gte"] == "2026-11-19"
    assert params["expiration_date_lte"] == (date(2026, 11, 19) + timedelta(days=75)).isoformat()


def test_snapshots_use_the_indicative_feed() -> None:
    server = server_with()

    server.adapter().get_chain("AAPL")

    assert server.requests_to(SNAPSHOTS_PATH)[0].url.params["feed"] == "indicative"


def test_sends_credentials() -> None:
    server = server_with()

    server.adapter().get_chain("AAPL")

    assert all(r.headers["APCA-API-KEY-ID"] == "key" for r in server.requests)
    assert all(r.headers["APCA-API-SECRET-KEY"] == "secret" for r in server.requests)


def test_both_option_endpoints_are_paged() -> None:
    server = AlpacaServer(
        snapshot_pages=[{CALL: option_snapshot()}, {PUT: option_snapshot()}],
        contract_pages=[[contract_row(CALL)], [contract_row(PUT, right="put")]],
    )

    chain = server.adapter().get_chain("AAPL").chain

    assert len(chain.quotes) == 2
    assert len(server.requests_to(SNAPSHOTS_PATH)) == 2
    assert len(server.requests_to(CONTRACTS_PATH)) == 2


# Contracts and quotes that are left out


@pytest.mark.parametrize(
    "contract",
    [
        contract_row(CALL, multiplier="10"),
        contract_row(CALL, size="10"),
        contract_row(CALL, root="AAPL1"),
    ],
)
def test_non_standard_contracts_are_excluded(contract: dict[str, Any]) -> None:
    server = server_with(
        snapshots={CALL: option_snapshot(), PUT: option_snapshot()},
        contracts=[contract, contract_row(PUT, right="put")],
    )

    chain = server.adapter().get_chain("AAPL").chain

    assert [q.contract.right for q in chain.quotes] == [OptionRight.PUT]


def test_snapshot_without_contract_details_is_excluded() -> None:
    server = server_with(
        snapshots={CALL: option_snapshot(), BACK_CALL: option_snapshot()},
        contracts=[contract_row(CALL)],
    )

    assert len(server.adapter().get_chain("AAPL").chain.quotes) == 1


@pytest.mark.parametrize(
    "unusable",
    [
        {"latestQuote": None},
        {},
        option_snapshot(bid=1.20, ask=1.10),  # crossed
        option_snapshot(bid=0.0, ask=0.0),  # no market
    ],
)
def test_unusable_quotes_are_excluded(unusable: dict[str, Any]) -> None:
    server = server_with(
        snapshots={CALL: option_snapshot(), PUT: unusable},
        contracts=[contract_row(CALL), contract_row(PUT, right="put")],
    )

    snapshot = server.adapter().get_chain("AAPL")

    assert len(snapshot.chain.quotes) == 1
    assert snapshot.stale_quotes_dropped == 0


def test_negative_vendor_iv_is_recorded_as_missing() -> None:
    server = server_with(snapshots={CALL: option_snapshot(iv=-1.0)})

    assert server.adapter().get_chain("AAPL").chain.quotes[0].iv is None


# Quote freshness and as_of


def test_quote_exactly_five_minutes_old_is_kept() -> None:
    snapshot = server_with(snapshots={CALL: option_snapshot(age_seconds=300)}).adapter()

    assert snapshot.get_chain("AAPL").stale_quotes_dropped == 0


def test_stale_quotes_are_dropped_and_counted() -> None:
    server = server_with(
        snapshots={
            CALL: option_snapshot(age_seconds=10),
            PUT: option_snapshot(age_seconds=301),
            BACK_CALL: option_snapshot(age_seconds=4000),
        },
        contracts=[
            contract_row(CALL),
            contract_row(PUT, right="put"),
            contract_row(BACK_CALL, expiry=BACK_ISO),
        ],
    )

    snapshot = server.adapter().get_chain("AAPL")

    assert len(snapshot.chain.quotes) == 1
    assert snapshot.stale_quotes_dropped == 2


def test_as_of_is_the_latest_kept_quote_time() -> None:
    server = server_with(
        snapshots={CALL: option_snapshot(age_seconds=40), PUT: option_snapshot(age_seconds=25)},
        contracts=[contract_row(CALL), contract_row(PUT, right="put")],
    )

    assert server.adapter().get_chain("AAPL").chain.as_of == SCAN_TIME - timedelta(seconds=25)


def test_nanosecond_timestamps_are_understood() -> None:
    fresh = option_snapshot()
    fresh["latestQuote"]["t"] = "2026-11-19T20:44:50.123456789Z"
    server = server_with(snapshots={CALL: fresh})

    assert server.adapter().get_chain("AAPL").chain.as_of.microsecond == 123456


def test_all_quotes_stale_is_an_error_that_says_how_many() -> None:
    server = server_with(snapshots={CALL: option_snapshot(age_seconds=9000)})

    with pytest.raises(MarketDataError, match="1 stale"):
        server.adapter().get_chain("AAPL")


# Failures


def test_http_failure_is_a_market_data_error() -> None:
    server = server_with()
    server.status = 403

    with pytest.raises(MarketDataError):
        server.adapter().get_chain("AAPL")


def test_malformed_response_is_a_market_data_error() -> None:
    bad = option_snapshot()
    del bad["latestQuote"]["t"]
    server = server_with(snapshots={CALL: bad})

    with pytest.raises(MarketDataError):
        server.adapter().get_chain("AAPL")


# History


def test_history_requests_split_adjusted_sip_daily_bars_up_to_end() -> None:
    server = AlpacaServer(bar_pages=[[bar("2026-11-18")]])

    server.adapter().get_history("AAPL", LAST_SESSION, 31)

    params = server.requests_to(BARS_PATH)[0].url.params
    assert params["symbols"] == "AAPL"
    assert params["timeframe"] == "1Day"
    assert params["adjustment"] == "split"
    assert params["feed"] == "sip"
    assert params["end"] == "2026-11-18"


def test_history_maps_bars_to_new_york_session_days() -> None:
    server = AlpacaServer(bar_pages=[[bar("2026-11-17", 101.0), bar("2026-11-18", 102.0)]])

    history = server.adapter().get_history("AAPL", LAST_SESSION, 31)

    assert [b.day for b in history.bars] == [date(2026, 11, 17), date(2026, 11, 18)]
    assert history.bars[1].close == 102.0
    assert history.bars[1].volume == 2_000_000


def test_history_keeps_only_the_most_recent_bars() -> None:
    server = AlpacaServer(bar_pages=[[bar("2026-11-16"), bar("2026-11-17"), bar("2026-11-18")]])

    history = server.adapter().get_history("AAPL", LAST_SESSION, 2)

    assert [b.day.day for b in history.bars] == [17, 18]


def test_history_is_paged() -> None:
    server = AlpacaServer(bar_pages=[[bar("2026-11-17")], [bar("2026-11-18")]])

    assert len(server.adapter().get_history("AAPL", LAST_SESSION, 31).bars) == 2


def test_history_with_no_bars_is_empty() -> None:
    assert AlpacaServer().adapter().get_history("AAPL", LAST_SESSION, 31).bars == ()


def test_history_failure_is_a_market_data_error() -> None:
    server = AlpacaServer(status=500)

    with pytest.raises(MarketDataError):
        server.adapter().get_history("AAPL", LAST_SESSION, 31)
