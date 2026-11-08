from datetime import date

import pytest

from evbot.domain.prices import Bar, PriceHistory


def bar(day: int, close: float = 100.0, volume: int = 1_000_000) -> Bar:
    return Bar(
        date(2026, 11, day), open=close, high=close + 1, low=close - 1, close=close, volume=volume
    )


def test_bar_rejects_high_below_close() -> None:
    with pytest.raises(ValueError):
        Bar(date(2026, 11, 2), open=99.0, high=100.0, low=98.0, close=100.5, volume=1)


def test_bar_rejects_low_above_open() -> None:
    with pytest.raises(ValueError):
        Bar(date(2026, 11, 2), open=97.0, high=101.0, low=98.0, close=100.0, volume=1)


def test_bar_rejects_negative_volume() -> None:
    with pytest.raises(ValueError):
        bar(2, volume=-1)


def test_last_returns_most_recent_bars_in_order() -> None:
    history = PriceHistory("AAPL", (bar(2, 100.0), bar(3, 101.0), bar(4, 102.0)))

    assert [b.close for b in history.last(2)] == [101.0, 102.0]


def test_last_raises_when_history_is_too_short() -> None:
    history = PriceHistory("AAPL", (bar(2), bar(3)))

    with pytest.raises(ValueError):
        history.last(3)


def test_history_rejects_unsorted_bars() -> None:
    with pytest.raises(ValueError):
        PriceHistory("AAPL", (bar(3), bar(2)))


def test_history_rejects_duplicate_days() -> None:
    with pytest.raises(ValueError):
        PriceHistory("AAPL", (bar(2), bar(2)))
