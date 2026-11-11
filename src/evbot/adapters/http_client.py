import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import requests

from evbot.adapters.rate_limiter import RateLimiter

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

Params = Mapping[str, str | int | float]


class HttpRequestError(Exception):
    """Adapters translate this into their port's error."""


class JsonGetter(Protocol):
    """What adapters need from an HTTP client, so another library can be plugged in."""

    def get(
        self,
        url: str,
        params: Params | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any: ...


class RequestsJsonHttpClient:
    def __init__(
        self,
        session: requests.Session,
        timeout: float,
        max_retries: int,
        backoff_seconds: float,
        sleep: Callable[[float], None] = time.sleep,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._session = session
        self._timeout = timeout
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
        params: Params | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        failure = "no attempt made"
        for attempt in range(self._max_retries + 1):
            retry_after: float | None = None
            if self._limiter is not None:
                self._limiter.acquire()
            self._request_count += 1
            try:
                response = self._session.get(
                    url, params=params, headers=headers, timeout=self._timeout
                )
            except (requests.ConnectionError, requests.Timeout) as error:
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
    def _decoded(response: requests.Response, url: str) -> Any:
        if response.status_code >= 400:
            raise HttpRequestError(f"HTTP {response.status_code} from {url}")
        try:
            return response.json()
        except ValueError as error:
            raise HttpRequestError(f"invalid JSON from {url}") from error

    @staticmethod
    def _retry_after(response: requests.Response) -> float | None:
        try:
            return float(response.headers["Retry-After"])
        except (KeyError, ValueError):
            return None
