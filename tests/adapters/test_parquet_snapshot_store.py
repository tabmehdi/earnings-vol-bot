from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from evbot.adapters.parquet_snapshot_store import ParquetSnapshotStore
from evbot.domain.options import OptionChain, OptionContract, OptionQuote, OptionRight
from evbot.domain.prices import Bar, PriceHistory
from evbot.domain.snapshots import ChainSnapshot, DataFeeds
from evbot.ports.errors import SnapshotError

FEEDS = DataFeeds(options="indicative", spot="iex", history="sip")
AS_OF = datetime(2026, 11, 19, 20, 45, 1, 250, tzinfo=UTC)


def quote(right: OptionRight, iv: float | None) -> OptionQuote:
    contract = OptionContract("AAPL", date(2026, 11, 20), 100.5, right, multiplier=100)
    return OptionQuote(contract, 1.01, 1.13, iv, open_interest=750, volume=0)


def snapshot(symbol: str = "AAPL") -> ChainSnapshot:
    quotes = (quote(OptionRight.CALL, 0.45), quote(OptionRight.PUT, None))
    if symbol != "AAPL":
        quotes = ()
    return ChainSnapshot(OptionChain(symbol, 100.25, AS_OF, quotes), 3, FEEDS)


def test_chain_round_trip_is_exact(tmp_path: Path) -> None:
    store = ParquetSnapshotStore(tmp_path)

    store.save_chain(snapshot())

    assert store.load_chain("AAPL", AS_OF) == snapshot()


def test_empty_chain_round_trips(tmp_path: Path) -> None:
    store = ParquetSnapshotStore(tmp_path)

    store.save_chain(snapshot("MSFT"))

    assert store.load_chain("MSFT", AS_OF).chain.quotes == ()


def test_history_round_trip_is_exact(tmp_path: Path) -> None:
    store = ParquetSnapshotStore(tmp_path)
    history = PriceHistory(
        "AAPL",
        (
            Bar(date(2026, 11, 17), 100.0, 101.5, 99.5, 101.0, 2_000_000),
            Bar(date(2026, 11, 18), 101.0, 102.5, 100.5, 102.0, 3_000_000),
        ),
    )

    store.save_history(history, AS_OF, FEEDS)

    assert store.load_history("AAPL", AS_OF) == history


def test_feeds_are_recorded_in_the_file_metadata(tmp_path: Path) -> None:
    store = ParquetSnapshotStore(tmp_path)

    store.save_chain(snapshot())

    assert store.load_chain("AAPL", AS_OF).feeds == FEEDS
    assert store.chain_metadata("AAPL", AS_OF) == {
        "options_feed": "indicative",
        "spot_feed": "iex",
        "history_feed": "sip",
    }


def test_files_are_partitioned_by_day(tmp_path: Path) -> None:
    ParquetSnapshotStore(tmp_path).save_chain(snapshot())

    assert len(list((tmp_path / "chains" / "date=2026-11-19").glob("AAPL_*.parquet"))) == 1


def test_a_slash_in_a_symbol_stays_inside_the_store(tmp_path: Path) -> None:
    store = ParquetSnapshotStore(tmp_path)

    store.save_chain(snapshot("BRK/B"))

    assert store.load_chain("BRK/B", AS_OF).chain.underlying == "BRK/B"


def test_missing_chain_raises(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError):
        ParquetSnapshotStore(tmp_path).load_chain("AAPL", AS_OF)


def test_missing_history_raises(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError):
        ParquetSnapshotStore(tmp_path).load_history("AAPL", AS_OF)
