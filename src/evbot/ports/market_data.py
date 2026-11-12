from collections.abc import Mapping, Sequence
from datetime import date
from typing import Protocol

from evbot.domain.options import OptionContract, OptionQuote
from evbot.domain.prices import PriceHistory
from evbot.domain.snapshots import ChainSnapshot


class MarketDataProvider(Protocol):
    def screen(self, symbols: Sequence[str]) -> Mapping[str, str]:
        """Tickers with no option contracts to trade, each with the reason, found in bulk
        before any per-ticker request. get_chain for the others may reuse what this found.
        """
        ...

    def get_chain(self, symbol: str) -> ChainSnapshot: ...

    def get_leg_quotes(
        self, contracts: Sequence[OptionContract]
    ) -> Mapping[OptionContract, OptionQuote]:
        """Fresh quotes for a few contracts, for pricing an order. Every contract is
        present, or MarketDataError is raised."""
        ...

    def get_history(self, symbol: str, end: date, bars: int) -> PriceHistory:
        """The most recent bars (at most `bars`) of completed sessions up to and including end."""
        ...
