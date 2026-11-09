import httpx
import pytest

from evbot.adapters.http_client import HttpRequestError, JsonHttpClient

URL = "https://example.test/thing"


def client_returning(
    responses: list[httpx.Response], delays: list[float] | None = None
) -> tuple[JsonHttpClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    remaining = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return next(remaining)

    client = JsonHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=2,
        backoff_seconds=1.0,
        sleep=(delays if delays is not None else []).append,
    )
    return client, seen


def test_returns_decoded_json_and_sends_params_and_headers() -> None:
    client, seen = client_returning([httpx.Response(200, json={"a": 1})])

    assert client.get(URL, params={"x": "1"}, headers={"H": "v"}) == {"a": 1}
    assert seen[0].url.params["x"] == "1"
    assert seen[0].headers["H"] == "v"


def test_retries_server_errors_with_exponential_backoff() -> None:
    delays: list[float] = []
    client, seen = client_returning(
        [httpx.Response(500), httpx.Response(503), httpx.Response(200, json=[])], delays
    )

    assert client.get(URL) == []
    assert len(seen) == 3
    assert delays == [1.0, 2.0]


def test_rate_limit_waits_for_retry_after() -> None:
    delays: list[float] = []
    limited = httpx.Response(429, headers={"Retry-After": "7"})
    client, _ = client_returning([limited, httpx.Response(200, json=[])], delays)

    client.get(URL)

    assert delays == [7.0]


def test_gives_up_after_max_retries() -> None:
    client, seen = client_returning([httpx.Response(500)] * 3)

    with pytest.raises(HttpRequestError):
        client.get(URL)
    assert len(seen) == 3


def test_client_errors_are_not_retried() -> None:
    client, seen = client_returning([httpx.Response(403)])

    with pytest.raises(HttpRequestError):
        client.get(URL)
    assert len(seen) == 1


def test_network_failures_are_retried_then_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = JsonHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=1,
        backoff_seconds=1.0,
        sleep=lambda _: None,
    )

    with pytest.raises(HttpRequestError):
        client.get(URL)


class CountingLimiter:
    def __init__(self) -> None:
        self.acquired = 0

    def acquire(self) -> None:
        self.acquired += 1


def test_every_attempt_takes_a_token_and_is_counted() -> None:
    responses = iter([httpx.Response(429), httpx.Response(200, json=[])])
    limiter = CountingLimiter()
    client = JsonHttpClient(
        httpx.Client(transport=httpx.MockTransport(lambda _: next(responses))),
        max_retries=2,
        backoff_seconds=1.0,
        sleep=lambda _: None,
        limiter=limiter,
    )

    client.get(URL)

    assert limiter.acquired == 2
    assert client.request_count == 2


def test_invalid_json_is_reported() -> None:
    client, _ = client_returning([httpx.Response(200, text="<html>")])

    with pytest.raises(HttpRequestError):
        client.get(URL)
