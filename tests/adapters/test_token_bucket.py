import pytest

from evbot.adapters.rate_limiter import TokenBucket


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


def bucket(rate: int, capacity: int | None = None) -> tuple[TokenBucket, FakeTime]:
    fake = FakeTime()
    return TokenBucket(rate, capacity, monotonic=fake.monotonic, sleep=fake.sleep), fake


def test_a_short_burst_up_to_capacity_does_not_wait() -> None:
    limiter, fake = bucket(180)  # capacity defaults to one second of requests: 3

    for _ in range(3):
        limiter.acquire()

    assert fake.waits == []


def test_the_request_after_the_burst_waits_for_one_token() -> None:
    limiter, fake = bucket(180)
    for _ in range(3):
        limiter.acquire()

    limiter.acquire()

    # 180 per minute is 3 per second, so one token takes 1/3 s.
    assert fake.waits == [pytest.approx(1 / 3)]


def test_sustained_use_stays_at_the_configured_rate() -> None:
    limiter, fake = bucket(180)

    for _ in range(183):
        limiter.acquire()

    # 3 from the initial burst, then 180 at 1/3 s each = 60 s, so under Alpaca's 200 per minute.
    assert fake.now == pytest.approx(60.0)


def test_idle_time_refills_but_never_above_capacity() -> None:
    limiter, fake = bucket(180)
    for _ in range(3):
        limiter.acquire()
    fake.now += 1_000.0

    for _ in range(3):
        limiter.acquire()
    assert fake.waits == []

    limiter.acquire()
    assert len(fake.waits) == 1


def test_a_low_rate_still_allows_one_request_at_once() -> None:
    limiter, fake = bucket(30)

    limiter.acquire()

    assert fake.waits == []


def test_rejects_a_non_positive_rate() -> None:
    with pytest.raises(ValueError):
        TokenBucket(0)
