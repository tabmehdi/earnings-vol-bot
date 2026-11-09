from dataclasses import dataclass

from evbot.domain.options import OptionChain


@dataclass(frozen=True, slots=True)
class DataFeeds:
    options: str
    spot: str
    history: str


@dataclass(frozen=True, slots=True)
class ChainSnapshot:
    chain: OptionChain
    stale_quotes_dropped: int
    feeds: DataFeeds

    def __post_init__(self) -> None:
        if self.stale_quotes_dropped < 0:
            raise ValueError(f"stale count must not be negative: {self.stale_quotes_dropped}")
