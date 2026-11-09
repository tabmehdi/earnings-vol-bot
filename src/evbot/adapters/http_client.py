import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from evbot.adapters.rate_limiter import RateLimiter

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class HttpRequestError(Exception):
    """Adapters translate this into their port's error."""


class JsonHttpClient:
    def __init__(
        self,
        client: httpx.Client,
        max_retries: int,
        backoff_seconds: float,
        sleep: Callable[[float], None] = time.sleep,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._client = client
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._limiter = limiter
        self._request_count = 0

    @property
    def request_count(self) -> int:
        return self._request_count

    def get(
        self,
        url: str,
        params: Mapping[str, str | int | float] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        failure = "no attempt made"
        for attempt in range(self._max_retries + 1):
            retry_after: float | None = None
            if self._limiter is not None:
                self._limiter.acquire()
            self._request_count += 1
            try:
                response = self._client.get(url, params=params, headers=headers)
            except httpx.TransportError as error:
                failure = f"{type(error).__name__} calling {url}"
            else:
                if response.status_code not in RETRYABLE_STATUSES:
                    return self._decoded(response, url)
                failure = f"HTTP {response.status_code} from {url}"
                retry_after = self._retry_after(response)
            if attempt < self._max_retries:
                self._sleep(retry_after or self._backoff_seconds * 2**attempt)
        raise HttpRequestError(failure)

    @staticmethod
    def _decoded(response: httpx.Response, url: str) -> Any:
        if response.is_error:
            raise HttpRequestError(f"HTTP {response.status_code} from {url}")
        try:
            return response.json()
        except ValueError as error:
            raise HttpRequestError(f"invalid JSON from {url}") from error

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        try:
            return float(response.headers["Retry-After"])
        except (KeyError, ValueError):
            return None
