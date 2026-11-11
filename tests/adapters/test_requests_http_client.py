from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from evbot.adapters.http_client import HttpRequestError, RequestsJsonHttpClient

URL = "https://example.test/data"


def response(
    status: int, body: bytes = b"", headers: Mapping[str, str] | None = None
) -> requests.Response:
    built = requests.Response()
    built.status_code = status
    built._content = body
    built.headers = CaseInsensitiveDict(headers or {})
    return built


class FakeTransport(BaseAdapter):
    """Answers a real requests.Session from a script, so no test touches the network."""

    def __init__(self, handler: Callable[[requests.PreparedRequest], requests.Response]) -> None:
        super().__init__()
        self._handler = handler
        self.seen: list[requests.PreparedRequest] = []
        self.timeouts: list[Any] = []

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        self.seen.append(request)
        self.timeouts.append(kwargs.get("timeout"))
        answer = self._handler(request)
        answer.request = request
        answer.url = request.url or ""
        return answer

    def close(self) -> None:
        pass


def session_on(transport: FakeTransport) -> requests.Session:
    session = requests.Session()
    session.mount("https://", transport)
    return session


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


def test_returns_decoded_json_and_sends_params_headers_and_timeout() -> None:
    client, transport = client_returning([response(200, b'{"a": 1}')])

    assert client.get(URL, params={"x": "1"}, headers={"H": "v"}) == {"a": 1}
    sent = transport.seen[0]
    assert parse_qs(urlsplit(sent.url or "").query)["x"] == ["1"]
    assert sent.headers["H"] == "v"
    assert transport.timeouts == [12.5]


def test_retries_server_errors_with_exponential_backoff() -> None:
    delays: list[float] = []
    client, transport = client_returning(
        [response(500), response(503), response(200, b"[]")], delays
    )

    assert client.get(URL) == []
    assert len(transport.seen) == 3
    assert delays == [1.0, 2.0]


def test_rate_limit_waits_for_retry_after() -> None:
    delays: list[float] = []
    limited = response(429, headers={"Retry-After": "7"})
    client, _ = client_returning([limited, response(200, b"[]")], delays)

    client.get(URL)

    assert delays == [7.0]


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
    def handler(_: requests.PreparedRequest) -> requests.Response:
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
    client, _ = client_returning([response(429), response(200, b"[]")], limiter=limiter)

    client.get(URL)

    assert limiter.acquired == 2
    assert client.request_count == 2


def test_invalid_json_is_reported() -> None:
    client, _ = client_returning([response(200, b"<html>")])

    with pytest.raises(HttpRequestError, match=f"invalid JSON from {URL}"):
        client.get(URL)
