from datetime import UTC, date, datetime

import pytest
from requests import Response

from evbot.adapters.alpaca_option_trades import AlpacaOptionTradeHistory
from evbot.domain.options import OptionContract, OptionRight, OptionTrade
from evbot.ports.errors import MarketDataError
from tests.fixtures.http import SeenRequest, json_client, response

CONTRACT = OptionContract("BBY", date(2026, 12, 18), 97.0, OptionRight.CALL)
OCC = "BBY261218C00097000"
START = datetime(2026, 11, 23, 20, 15, tzinfo=UTC)
END = datetime(2026, 11, 23, 20, 55, tzinfo=UTC)


def trade(second: int, price: float, size: int) -> dict[str, object]:
    return {"t": f"2026-11-23T20:48:{second:02d}.123456789Z", "x": "C", "p": price, "s": size}


def history(handler: object) -> AlpacaOptionTradeHistory:
    client = json_client(handler, max_retries=0)  # type: ignore[arg-type]
    return AlpacaOptionTradeHistory(client, "https://data.alpaca.markets", "key", "secret")


def test_reads_every_page_of_trades_in_the_window() -> None:
    seen: list[SeenRequest] = []

    def handler(request: SeenRequest) -> Response:
        seen.append(request)
        if "page_token" not in request.url.params:
            return response(200, {"trades": {OCC: [trade(45, 5.85, 2)]}, "next_page_token": "p2"})
        return response(200, {"trades": {OCC: [trade(46, 6.00, 1)]}, "next_page_token": None})

    trades = history(handler).trades(CONTRACT, START, END)

    assert trades == (
        OptionTrade(CONTRACT, datetime(2026, 11, 23, 20, 48, 45, 123456, tzinfo=UTC), 5.85, 2),
        OptionTrade(CONTRACT, datetime(2026, 11, 23, 20, 48, 46, 123456, tzinfo=UTC), 6.00, 1),
    )
    first = seen[0]
    assert first.url.path == "/v1beta1/options/trades"
    assert first.url.params["symbols"] == OCC
    assert first.url.params["start"] == START.isoformat()
    assert first.url.params["end"] == END.isoformat()
    assert first.url.params["sort"] == "asc"
    assert "feed" not in first.url.params
    assert seen[1].url.params["page_token"] == "p2"
    assert first.headers["APCA-API-KEY-ID"] == "key"


@pytest.mark.parametrize("body", [{"trades": {}}, {"trades": None}, {"trades": {OCC: []}}])
def test_no_trades_is_an_empty_result(body: dict[str, object]) -> None:
    assert history(lambda _: response(200, body)).trades(CONTRACT, START, END) == ()


def test_a_refusal_is_a_market_data_error_with_alpacas_reason() -> None:
    refused = history(lambda _: response(403, {"message": "subscription does not permit"}))

    with pytest.raises(MarketDataError, match=r"403.*subscription does not permit"):
        refused.trades(CONTRACT, START, END)
