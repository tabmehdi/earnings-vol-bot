from datetime import timedelta

from evbot.adapters.market_clock import MarketClock


def test_now_is_timezone_aware_in_new_york() -> None:
    now = MarketClock().now()

    assert now.utcoffset() in (timedelta(hours=-5), timedelta(hours=-4))


def test_wait_pauses_for_the_given_time() -> None:
    clock = MarketClock()
    before = clock.now()

    clock.wait(0.05)

    assert clock.now() - before >= timedelta(seconds=0.04)
