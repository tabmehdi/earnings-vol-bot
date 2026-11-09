import time
from collections.abc import Callable
from typing import Protocol


class RateLimiter(Protocol):
    def acquire(self) -> None: ...


class TokenBucket:
    """Blocks just long enough to hold requests to a steady rate.

    Capacity defaults to one second of requests. A full minute's capacity would allow twice
    the rate in the first minute, which is what the limiter exists to prevent.
    """

    def __init__(
        self,
        requests_per_minute: int,
        capacity: int | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_minute <= 0:
            raise ValueError(f"rate must be positive, got {requests_per_minute}")
        self._tokens_per_second = requests_per_minute / 60
        self._capacity = float(capacity or max(1, requests_per_minute // 60))
        self._tokens = self._capacity
        self._monotonic = monotonic
        self._sleep = sleep
        self._updated_at = monotonic()

    def acquire(self) -> None:
        now = self._monotonic()
        elapsed = now - self._updated_at
        self._tokens = min(self._capacity, self._tokens + elapsed * self._tokens_per_second)
        self._updated_at = now
        if self._tokens >= 1:
            self._tokens -= 1
            return
        self._sleep((1 - self._tokens) / self._tokens_per_second)
        self._tokens = 0.0
        self._updated_at = self._monotonic()
