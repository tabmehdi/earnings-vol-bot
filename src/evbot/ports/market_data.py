from datetime import date
from typing import Protocol

from evbot.domain.prices import PriceHistory
from evbot.domain.snapshots import ChainSnapshot


class MarketDataProvider(Protocol):
    def get_chain(self, symbol: str) -> ChainSnapshot: ...

    def get_history(self, symbol: str, end: date, bars: int) -> PriceHistory:
        """The most recent bars (at most `bars`) of completed sessions up to and including end."""
        ...
