"""Screening must not change any result: each ticker gets the same chain or the same note."""

from typing import Any

import pytest

from evbot.adapters.alpaca_market_data import AlpacaMarketData
from evbot.domain.snapshots import ChainSnapshot
from evbot.ports.errors import MarketDataError
from tests.fixtures.listed_market import CONTRACTS_PATH, ListedMarket, listed

FRONT, BACK, FAR_FUTURE = "2026-11-20", "2026-12-18", "2028-01-21"
SCENARIOS = ("GOOD", "NONE", "FAR", "LEAPS", "ODD", "STALE", "NOSNAP")


def strikes_around(
    symbol: str, expiry: str, *strikes: float, **kwargs: Any
) -> list[dict[str, Any]]:
    return [
        listed(symbol, expiry, k, right, **kwargs) for k in strikes for right in ("call", "put")
    ]


def market() -> ListedMarket:
    """One ticker per case the scan meets in practice."""
    m = ListedMarket(spots={s: 100.0 for s in SCENARIOS})
    m.add(*strikes_around("GOOD", FRONT, 95, 100, 105), *strikes_around("GOOD", BACK, 95, 100, 105))
    # NONE: has no options at all (like BAK).
    m.spots["FAR"] = 0.19  # like CAN: every listed strike is far above spot
    m.add(*strikes_around("FAR", FRONT, 0.5, 1.0, 2.5))
    m.add(*strikes_around("LEAPS", FAR_FUTURE, 100))  # only expiries beyond the 75-day window
    m.add(*strikes_around("ODD", FRONT, 100, root="ODD1", multiplier="10"))  # adjusted only
    m.add(*strikes_around("STALE", FRONT, 100), quote_age_seconds=900)
    m.add(*strikes_around("NOSNAP", FRONT, 100), quote_age_seconds=None)
    return m


def outcome(adapter: AlpacaMarketData, symbol: str) -> ChainSnapshot | str:
    try:
        return adapter.get_chain(symbol)
    except MarketDataError as error:
        return str(error)


def per_ticker(symbols: tuple[str, ...]) -> dict[str, ChainSnapshot | str]:
    adapter = market().adapter()
    return {symbol: outcome(adapter, symbol) for symbol in symbols}


def screened(symbols: tuple[str, ...]) -> tuple[dict[str, ChainSnapshot | str], ListedMarket]:
    fake = market()
    adapter = fake.adapter()
    skips = adapter.screen(symbols)
    results = {s: f"{s}: {skips[s]}" if s in skips else outcome(adapter, s) for s in symbols}
    return results, fake


def test_every_ticker_gets_the_same_chain_or_note_with_and_without_screening() -> None:
    expected = per_ticker(SCENARIOS)

    actual, _ = screened(SCENARIOS)

    assert actual == expected


def test_the_cases_cover_every_note() -> None:
    notes = per_ticker(SCENARIOS)

    assert isinstance(notes["GOOD"], ChainSnapshot)
    assert notes["NONE"] == "NONE: no option contracts available"
    assert notes["FAR"] == "FAR: no contracts within strike range"
    assert notes["LEAPS"] == "LEAPS: no contracts within strike range"
    assert notes["ODD"] == "ODD: no contracts within strike range"
    assert notes["STALE"] == "STALE: no fresh option quotes (2 stale dropped)"
    assert notes["NOSNAP"] == "NOSNAP: no contracts within strike range"


def test_screening_skips_tickers_without_contracts_before_any_quote() -> None:
    _, fake = screened(SCENARIOS)

    asked_for_spot = {r.url.path.split("/")[3] for r in fake.requests if "/stocks/" in r.url.path}
    assert "NONE" not in asked_for_spot
    assert "LEAPS" not in asked_for_spot


def test_screening_makes_fewer_requests() -> None:
    plain = market()
    adapter = plain.adapter()
    for symbol in SCENARIOS:
        outcome(adapter, symbol)

    _, fake = screened(SCENARIOS)

    assert len(fake.requests) < len(plain.requests)


def test_screened_chains_need_no_per_ticker_contracts_request() -> None:
    _, fake = screened(("GOOD",))

    contract_requests = fake.requests_to(CONTRACTS_PATH)
    assert all("strike_price_gte" not in r.url.params for r in contract_requests)


def test_tickers_are_batched_and_paged() -> None:
    fake = market()
    symbols = tuple(f"T{i}" for i in range(250))

    fake.adapter().screen(symbols)

    window_requests = [
        r
        for r in fake.requests_to(CONTRACTS_PATH)
        if r.url.params["expiration_date_lte"] < "2027-06-01"
    ]
    batches = [r for r in window_requests if "page_token" not in r.url.params]
    assert [len(r.url.params["underlying_symbols"].split(",")) for r in batches] == [100, 100, 50]


def test_contracts_are_followed_across_pages() -> None:
    fake = market()
    fake.page_size = 1

    assert fake.adapter().screen(("GOOD",)) == {}


def test_without_contracts_or_spot_the_contract_reason_is_reported() -> None:
    # The per-ticker path fetched spot first and reported the spot failure; screening runs
    # before any quote, so the same AVOID now names the missing contracts instead.
    fake = market()
    del fake.spots["NONE"]

    assert fake.adapter().screen(("NONE",)) == {"NONE": "no option contracts available"}


@pytest.mark.parametrize("symbols", [(), ("GOOD",)])
def test_nothing_to_skip_returns_no_reasons(symbols: tuple[str, ...]) -> None:
    assert market().adapter().screen(symbols) == {}
