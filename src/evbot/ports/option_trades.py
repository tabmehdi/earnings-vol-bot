from datetime import datetime
from typing import Protocol

from evbot.domain.options import OptionContract, OptionTrade


class OptionTradeHistory(Protocol):
    """Printed option trades after the fact, to check recorded quotes against the market."""

    def trades(
        self, contract: OptionContract, start: datetime, end: datetime
    ) -> tuple[OptionTrade, ...]:
        """Every trade from start to end, oldest first. Raises MarketDataError."""
        ...
