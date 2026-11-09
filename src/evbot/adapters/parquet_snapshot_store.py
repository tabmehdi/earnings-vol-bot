from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from evbot.domain.options import OptionChain, OptionContract, OptionQuote, OptionRight
from evbot.domain.prices import Bar, PriceHistory
from evbot.domain.snapshots import ChainSnapshot, DataFeeds
from evbot.ports.errors import SnapshotError

CHAIN_SCHEMA = pa.schema(
    [
        ("expiry", pa.date32()),
        ("strike", pa.float64()),
        ("right", pa.string()),
        ("multiplier", pa.int32()),
        ("bid", pa.float64()),
        ("ask", pa.float64()),
        ("iv", pa.float64()),
        ("open_interest", pa.int64()),
        ("volume", pa.int64()),
    ]
)
HISTORY_SCHEMA = pa.schema(
    [
        ("day", pa.date32()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.int64()),
    ]
)
READ_ERRORS = (OSError, pa.ArrowException, KeyError, ValueError)


class ParquetSnapshotStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def save_chain(self, snapshot: ChainSnapshot) -> None:
        chain = snapshot.chain
        rows = [
            {
                "expiry": q.contract.expiry,
                "strike": q.contract.strike,
                "right": q.contract.right.value,
                "multiplier": q.contract.multiplier,
                "bid": q.bid,
                "ask": q.ask,
                "iv": q.iv,
                "open_interest": q.open_interest,
                "volume": q.volume,
            }
            for q in chain.quotes
        ]
        metadata = {
            "underlying": chain.underlying,
            "spot": repr(chain.spot),
            "as_of": chain.as_of.isoformat(),
            "stale_quotes_dropped": str(snapshot.stale_quotes_dropped),
            **self._feed_metadata(snapshot.feeds),
        }
        self._write(
            self._path("chains", chain.underlying, chain.as_of), rows, CHAIN_SCHEMA, metadata
        )

    def load_chain(self, symbol: str, as_of: datetime) -> ChainSnapshot:
        try:
            table = pq.read_table(self._path("chains", symbol, as_of))
            metadata = self._metadata(table)
            quotes = tuple(self._quote(symbol, row) for row in table.to_pylist())
            chain = OptionChain(
                metadata["underlying"],
                float(metadata["spot"]),
                datetime.fromisoformat(metadata["as_of"]),
                quotes,
            )
            feeds = DataFeeds(
                metadata["options_feed"], metadata["spot_feed"], metadata["history_feed"]
            )
            return ChainSnapshot(chain, int(metadata["stale_quotes_dropped"]), feeds)
        except READ_ERRORS as error:
            raise SnapshotError(f"cannot load chain {symbol} at {as_of}: {error!r}") from error

    def save_history(self, history: PriceHistory, as_of: datetime, feeds: DataFeeds) -> None:
        rows = [
            {
                "day": b.day,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in history.bars
        ]
        metadata = {"symbol": history.symbol, **self._feed_metadata(feeds)}
        self._write(self._path("history", history.symbol, as_of), rows, HISTORY_SCHEMA, metadata)

    def load_history(self, symbol: str, as_of: datetime) -> PriceHistory:
        try:
            table = pq.read_table(self._path("history", symbol, as_of))
            bars = tuple(Bar(**row) for row in table.to_pylist())
            return PriceHistory(self._metadata(table)["symbol"], bars)
        except READ_ERRORS as error:
            raise SnapshotError(f"cannot load history {symbol} at {as_of}: {error!r}") from error

    def chain_metadata(self, symbol: str, as_of: datetime) -> dict[str, str]:
        metadata = self._metadata(pq.read_table(self._path("chains", symbol, as_of)))
        return {key: value for key, value in metadata.items() if key.endswith("_feed")}

    def _path(self, kind: str, symbol: str, as_of: datetime) -> Path:
        moment = as_of.astimezone(UTC)
        file_name = f"{symbol.replace('/', '-')}_{moment:%H%M%S%f}.parquet"
        return self._root / kind / f"date={moment:%Y-%m-%d}" / file_name

    @staticmethod
    def _write(
        path: Path, rows: list[dict[str, Any]], schema: pa.Schema, metadata: dict[str, str]
    ) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            table = pa.Table.from_pylist(rows, schema=schema).replace_schema_metadata(metadata)
            pq.write_table(table, path)
        except (OSError, pa.ArrowException) as error:
            raise SnapshotError(f"cannot write {path}: {error!r}") from error

    @staticmethod
    def _metadata(table: pa.Table) -> dict[str, str]:
        return {key.decode(): value.decode() for key, value in table.schema.metadata.items()}

    @staticmethod
    def _feed_metadata(feeds: DataFeeds) -> dict[str, str]:
        return {
            "options_feed": feeds.options,
            "spot_feed": feeds.spot,
            "history_feed": feeds.history,
        }

    @staticmethod
    def _quote(symbol: str, row: dict[str, Any]) -> OptionQuote:
        contract = OptionContract(
            symbol, row["expiry"], row["strike"], OptionRight(row["right"]), row["multiplier"]
        )
        return OptionQuote(
            contract, row["bid"], row["ask"], row["iv"], row["open_interest"], row["volume"]
        )
