from typing import Any

import pytest
import requests

from evbot.adapters.http_client import HttpRequestError, RequestsJsonHttpClient
from tests.fixtures.http import FakeTransport, SeenRequest, response, session_on

URL = "https://example.test/data"


def client_returning(
    responses: list[requests.Response],
    delays: list[float] | None = None,
    max_retries: int = 2,
    limiter: Any = None,
) -> tuple[RequestsJsonHttpClient, FakeTransport]:
    remaining = iter(responses)
    transport = FakeTransport(lambda _: next(remaining))
    client = RequestsJsonHttpClient(
        session_on(transport),
        timeout=12.5,
        max_retries=max_retries,
        backoff_seconds=1.0,
        sleep=(delays if delays is not None else []).append,
        limiter=limiter,
    )
    return client, transport


def test_returns_decoded_json_and_sends_params_and_headers() -> None:
    client, transport = client_returning([response(200, {"a": 1})])

    assert client.get(URL, params={"x": "1"}, headers={"H": "v"}) == {"a": 1}
    assert transport.seen[0].url.params["x"] == "1"
    assert transport.seen[0].headers["H"] == "v"


def test_every_attempt_sends_the_timeout() -> None:
    client, transport = client_returning([response(500), response(200, [])])

    client.get(URL)

    assert transport.timeouts == [12.5, 12.5]


def test_per_request_headers_never_become_session_defaults() -> None:
    transport = FakeTransport(lambda _: response(200, []))
    session = session_on(transport)
    client = RequestsJsonHttpClient(session, timeout=1.0, max_retries=0, backoff_seconds=1.0)

    client.get(URL, headers={"APCA-API-KEY-ID": "key"})
    client.get(URL)

    assert "APCA-API-KEY-ID" not in session.headers
    assert "APCA-API-KEY-ID" not in transport.seen[1].headers


def test_retries_server_errors_with_exponential_backoff() -> None:
    delays: list[float] = []
    client, transport = client_returning([response(500), response(503), response(200, [])], delays)

    assert client.get(URL) == []
    assert len(transport.seen) == 3
    assert delays == [1.0, 2.0]


def test_rate_limit_waits_for_retry_after() -> None:
    delays: list[float] = []
    limited = response(429, headers={"Retry-After": "7"})
    client, _ = client_returning([limited, response(200, [])], delays)

    client.get(URL)

    assert delays == [7.0]


def test_unreadable_retry_after_falls_back_to_backoff() -> None:
    delays: list[float] = []
    limited = response(429, headers={"Retry-After": "soon"})
    client, _ = client_returning([limited, response(200, [])], delays)

    client.get(URL)

    assert delays == [1.0]


def test_gives_up_after_max_retries() -> None:
    client, transport = client_returning([response(500)] * 3)

    with pytest.raises(HttpRequestError, match=f"HTTP 500 from {URL}"):
        client.get(URL)
    assert len(transport.seen) == 3


def test_client_errors_are_not_retried() -> None:
    delays: list[float] = []
    client, transport = client_returning([response(403)], delays)

    with pytest.raises(HttpRequestError, match=f"HTTP 403 from {URL}"):
        client.get(URL)
    assert len(transport.seen) == 1
    assert delays == []


@pytest.mark.parametrize("error", [requests.ConnectionError, requests.Timeout])
def test_network_failures_are_retried_then_reported(error: type[Exception]) -> None:
    def handler(_: SeenRequest) -> requests.Response:
        raise error("down")

    transport = FakeTransport(handler)
    delays: list[float] = []
    client = RequestsJsonHttpClient(
        session_on(transport), timeout=1.0, max_retries=1, backoff_seconds=1.0, sleep=delays.append
    )

    with pytest.raises(HttpRequestError, match=f"{error.__name__} calling {URL}"):
        client.get(URL)
    assert len(transport.seen) == 2
    assert delays == [1.0]


class CountingLimiter:
    def __init__(self) -> None:
        self.acquired = 0

    def acquire(self) -> None:
        self.acquired += 1


def test_every_attempt_takes_a_token_and_is_counted() -> None:
    limiter = CountingLimiter()
    client, _ = client_returning([response(429), response(200, [])], limiter=limiter)

    client.get(URL)

    assert limiter.acquired == 2
    assert client.request_count == 2


def test_invalid_json_is_reported() -> None:
    client, _ = client_returning([response(200, content=b"<html>")])

    with pytest.raises(HttpRequestError, match=f"invalid JSON from {URL}"):
        client.get(URL)


def test_send_posts_json_once_and_never_retries() -> None:
    client, transport = client_returning([response(500), response(200, {"id": "a1"})])

    with pytest.raises(HttpRequestError) as error:
        client.send("POST", URL, {"qty": "1"})

    assert len(transport.seen) == 1
    assert transport.seen[0].method == "POST"
    assert transport.seen[0].body == '{"qty": "1"}'
    assert error.value.status == 500


def test_send_returns_the_decoded_answer() -> None:
    client, _ = client_returning([response(200, {"id": "a1"})])

    assert client.send("PATCH", URL, {"limit_price": "2.00"}) == {"id": "a1"}


def test_an_empty_answer_is_none() -> None:
    client, _ = client_returning([response(204)])

    assert client.send("DELETE", URL) is None


def test_an_error_keeps_the_status_and_body() -> None:
    client, _ = client_returning([response(422, {"message": "client_order_id must be unique"})])

    with pytest.raises(HttpRequestError) as error:
        client.send("POST", URL, {})

    assert error.value.status == 422
    assert "unique" in error.value.body
