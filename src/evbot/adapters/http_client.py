import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import requests

from evbot.adapters.rate_limiter import RateLimiter

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

Params = Mapping[str, str | int | float]


class HttpRequestError(Exception):
    """Adapters translate this into their port's error."""


class _RetryableFailure(Exception):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RetryingJsonClient(ABC):
    """Retry, backoff and rate limiting shared by the HTTP libraries we wrap.

    Subclasses make one attempt: return decoded JSON, raise HttpRequestError for a final
    failure, or raise _RetryableFailure to have the loop wait and try again.
    """

    def __init__(
        self,
        max_retries: int,
        backoff_seconds: float,
        sleep: Callable[[float], None],
        limiter: RateLimiter | None,
    ) -> None:
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
                return self._attempt(url, params, headers)
            except _RetryableFailure as error:
                failure = str(error)
                retry_after = error.retry_after
            if attempt < self._max_retries:
                self._sleep(retry_after or self._backoff_seconds * 2**attempt)
        raise HttpRequestError(failure)

    @abstractmethod
    def _attempt(
        self, url: str, params: Params | None, headers: Mapping[str, str] | None
    ) -> Any: ...


def _decoded(status: int, decode: Callable[[], Any], url: str) -> Any:
    if status >= 400:
        raise HttpRequestError(f"HTTP {status} from {url}")
    try:
        return decode()
    except ValueError as error:
        raise HttpRequestError(f"invalid JSON from {url}") from error


def _retry_after(headers: Mapping[str, str]) -> float | None:
    try:
        return float(headers["Retry-After"])
    except (KeyError, ValueError):
        return None


class JsonHttpClient(RetryingJsonClient):
    def __init__(
        self,
        client: httpx.Client,
        max_retries: int,
        backoff_seconds: float,
        sleep: Callable[[float], None] = time.sleep,
        limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__(max_retries, backoff_seconds, sleep, limiter)
        self._client = client

    def _attempt(self, url: str, params: Params | None, headers: Mapping[str, str] | None) -> Any:
        try:
            response = self._client.get(url, params=params, headers=headers)
        except httpx.TransportError as error:
            raise _RetryableFailure(f"{type(error).__name__} calling {url}") from error
        if response.status_code in RETRYABLE_STATUSES:
            raise _RetryableFailure(
                f"HTTP {response.status_code} from {url}", _retry_after(response.headers)
            )
        return _decoded(response.status_code, response.json, url)


class RequestsJsonHttpClient(RetryingJsonClient):
    """Same behavior as JsonHttpClient on top of `requests`.

    Nasdaq answers 403 to httpx but 200 to requests for the same URL, params and headers.
    """

    def __init__(
        self,
        session: requests.Session,
        timeout: float,
        max_retries: int,
        backoff_seconds: float,
        sleep: Callable[[float], None] = time.sleep,
        limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__(max_retries, backoff_seconds, sleep, limiter)
        self._session = session
        self._timeout = timeout

    def _attempt(self, url: str, params: Params | None, headers: Mapping[str, str] | None) -> Any:
        try:
            response = self._session.get(url, params=params, headers=headers, timeout=self._timeout)
        except (requests.ConnectionError, requests.Timeout) as error:
            raise _RetryableFailure(f"{type(error).__name__} calling {url}") from error
        if response.status_code in RETRYABLE_STATUSES:
            raise _RetryableFailure(
                f"HTTP {response.status_code} from {url}", _retry_after(response.headers)
            )
        return _decoded(response.status_code, response.json, url)
