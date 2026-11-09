from datetime import UTC, datetime

import pytest

from evbot.domain.options import OptionChain
from evbot.domain.snapshots import ChainSnapshot, DataFeeds

FEEDS = DataFeeds(options="indicative", spot="iex", history="sip")
CHAIN = OptionChain("AAPL", 100.0, datetime(2026, 11, 19, 20, 45, tzinfo=UTC), ())


def test_snapshot_keeps_chain_feeds_and_stale_count() -> None:
    snapshot = ChainSnapshot(CHAIN, stale_quotes_dropped=3, feeds=FEEDS)

    assert snapshot.chain is CHAIN
    assert snapshot.stale_quotes_dropped == 3
    assert snapshot.feeds.options == "indicative"


def test_snapshot_rejects_a_negative_stale_count() -> None:
    with pytest.raises(ValueError):
        ChainSnapshot(CHAIN, stale_quotes_dropped=-1, feeds=FEEDS)
