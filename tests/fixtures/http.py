from collections.abc import Callable

import httpx

from evbot.adapters.http_client import JsonHttpClient


def json_client(
    handler: Callable[[httpx.Request], httpx.Response], max_retries: int = 2
) -> JsonHttpClient:
    """A real JsonHttpClient on an in-process transport, so no test touches the network."""
    transport = httpx.MockTransport(handler)
    return JsonHttpClient(
        httpx.Client(transport=transport),
        max_retries=max_retries,
        backoff_seconds=1.0,
        sleep=lambda _: None,
    )
