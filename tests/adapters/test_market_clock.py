from datetime import timedelta

from evbot.adapters.market_clock import MarketClock


def test_now_is_timezone_aware_in_new_york() -> None:
    now = MarketClock().now()

    assert now.utcoffset() in (timedelta(hours=-5), timedelta(hours=-4))
