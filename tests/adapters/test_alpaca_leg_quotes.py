from datetime import date, timedelta

import pytest

from evbot.domain.options import OptionContract, OptionRight
from evbot.ports.errors import MarketDataError
from tests.fixtures.alpaca_server import (
    BACK_CALL,
    CALL,
    SCAN_TIME,
    AlpacaServer,
    seconds_before_scan,
)

QUOTES_PATH = "/v1beta1/options/quotes/latest"
FRONT = OptionContract("AAPL", date(2026, 11, 20), 100.0, OptionRight.CALL)
BACK = OptionContract("AAPL", date(2026, 12, 18), 100.0, OptionRight.CALL)


def leg_quote(bid: float, ask: float, age: float = 2.0) -> dict[str, object]:
    return {"bp": bid, "ap": ask, "bs": 5, "as": 7, "t": seconds_before_scan(age)}


def server() -> AlpacaServer:
    return AlpacaServer(
        leg_quotes={CALL: leg_quote(1.00, 1.10), BACK_CALL: leg_quote(3.00, 3.20, 5)}
    )


def test_returns_a_fresh_quote_for_each_leg() -> None:
    quotes = server().adapter().get_leg_quotes((FRONT, BACK))

    assert (quotes[FRONT].bid, quotes[FRONT].ask) == (1.00, 1.10)
    assert (quotes[BACK].bid, quotes[BACK].ask) == (3.00, 3.20)
    assert quotes[BACK].quoted_at == SCAN_TIME - timedelta(seconds=5)


def test_one_request_with_both_occ_symbols_on_the_options_feed() -> None:
    fake = server()

    fake.adapter().get_leg_quotes((FRONT, BACK))

    (request,) = fake.requests_to(QUOTES_PATH)
    assert request.url.params["symbols"] == f"{CALL},{BACK_CALL}"
    assert request.url.params["feed"] == "indicative"


def test_a_missing_quote_is_an_error() -> None:
    fake = AlpacaServer(leg_quotes={CALL: leg_quote(1.00, 1.10)})

    with pytest.raises(MarketDataError, match=BACK_CALL):
        fake.adapter().get_leg_quotes((FRONT, BACK))


def test_a_crossed_quote_is_an_error() -> None:
    fake = AlpacaServer(leg_quotes={CALL: leg_quote(1.20, 1.10), BACK_CALL: leg_quote(3.0, 3.2)})

    with pytest.raises(MarketDataError):
        fake.adapter().get_leg_quotes((FRONT, BACK))


def test_strikes_with_cents_use_the_occ_format() -> None:
    contract = OptionContract("F", date(2026, 11, 20), 12.5, OptionRight.PUT)
    fake = AlpacaServer(leg_quotes={"F261120P00012500": leg_quote(0.10, 0.12)})

    assert fake.adapter().get_leg_quotes((contract,))[contract].ask == 0.12
