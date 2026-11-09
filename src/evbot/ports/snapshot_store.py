from datetime import datetime
from typing import Protocol

from evbot.domain.prices import PriceHistory
from evbot.domain.snapshots import ChainSnapshot, DataFeeds


class SnapshotStore(Protocol):
    def save_chain(self, snapshot: ChainSnapshot) -> None: ...

    def load_chain(self, symbol: str, as_of: datetime) -> ChainSnapshot: ...

    def save_history(self, history: PriceHistory, as_of: datetime, feeds: DataFeeds) -> None: ...

    def load_history(self, symbol: str, as_of: datetime) -> PriceHistory: ...
