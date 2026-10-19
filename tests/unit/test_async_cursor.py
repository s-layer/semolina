"""
Tests for AsyncSemolinaCursor result surface, streaming, and close ordering.

Tests cover:
- ``async for row in cursor`` streams Row objects batch by batch off
  the event loop, and the cursor closes in the one order adbc-poolhouse permits
  (reader, then cursor, then connection) without ``ConnectionBusyError``.
- ``fetch_df()`` returns a pandas DataFrame and ``fetch_polars()`` a
  polars DataFrame, awaited, from a live async DuckDB semantic-view result.
- each of the four async Arrow/dataframe methods names the package it
  is missing and the exact command that installs it.

Every test in this module runs twice, once under asyncio and once under Trio,
via the shared ``anyio_backend`` fixture in ``tests/conftest.py``.

Test classes:
- TestAsyncRowMethods: awaited fetchall_rows / fetchone_row / fetchmany_rows
- TestAsyncDuplicateColumnNames: Row methods refuse a result with a repeated column name
- TestAsyncOneReadPatternPerCursor: the first way of reading a result is the only way
- TestAsyncPassthrough: raw-tuple fetches, Arrow passthroughs, sync properties
- TestAsyncStreamingIteration: lazy batch pulls, empty batches, re-iteration
  (ids carry ``stream``)
- TestAsyncCursorClose: ordered close, idempotence, invalidated connections
  (ids carry ``close``)
- TestAsyncCursorRepr: repr in open/closed states
- TestAsyncFetchDf: fetch_df() against a live async DuckDB
- TestAsyncFetchPolars: fetch_polars() against the same
- TestAsyncMissingDependencyGuards: what each async method demands, and what it
  does not — named to match ``test_cursor.py``'s sync class so one
  ``-k MissingDependency`` selects both cursors' cases
"""
# Test-only: the async tests reach the owned async pool's inner sync pool via
# engine._pool._pool to assert checkin, and inspect cursor state such as
# _closed / _reader. Scope-disable the private-access rule (intentionally not a
# `# type: ignore`).
# pyright: reportPrivateUsage=false

from __future__ import annotations

import importlib.util
import warnings
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel
from type_fidelity_probe import TypeFidelityView, setup_probe_view

from semolina.acursor import AsyncSemolinaCursor
from semolina.exceptions import (
    SemolinaDuplicateColumnsError,
    SemolinaMissingDependencyError,
    SemolinaResultConsumedError,
)
from semolina.results import Row

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator

    from semolina.query import Query

pytestmark = pytest.mark.anyio


FIXTURE_DATA: list[dict[str, Any]] = [
    {"revenue": 1000, "country": "US"},
    {"revenue": 2000, "country": "CA"},
    {"revenue": 500, "country": "MX"},
]


class _CountingAsyncReader:
    """
    Duck-typed fake of adbc-poolhouse's async record batch reader.

    Counts calls to ``__anext__`` so tests can assert laziness. We duck-type
    instead of subclassing because pyarrow forbids subclassing
    ``RecordBatchReader`` and poolhouse's async reader is not a public
    importable name.
    """

    def __init__(self, schema: Any, batches: Any, log: list[str] | None = None) -> None:
        """
        Initialize with a schema, an iterator of batches, and an optional close log.

        Args:
            schema: pyarrow schema describing the batches.
            batches: iterator (or iterable) of pyarrow.RecordBatch objects.
            log: shared list appended to on close, so tests can assert the
                reader closed before the cursor and connection.
        """
        self.schema = schema
        self.batches = iter(batches)
        self.read_count = 0
        self.closed = False
        self._log = log

    def __aiter__(self) -> _CountingAsyncReader:
        """Return self so the reader is its own async iterator."""
        return self

    async def __anext__(self) -> Any:
        """
        Return the next batch, raising StopAsyncIteration when exhausted.

        Returns:
            The next pyarrow.RecordBatch from the underlying iterator.
        """
        self.read_count += 1
        try:
            return next(self.batches)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self) -> None:
        """Mark the reader closed and record the close order."""
        self.closed = True
        if self._log is not None:
            self._log.append("reader")


class _FakeAsyncCursor:
    """Minimal duck-typed fake of adbc-poolhouse's ``AsyncCursor``."""

    def __init__(
        self,
        reader: _CountingAsyncReader,
        description: list[tuple[Any, ...]] | None,
        log: list[str] | None = None,
        fetch_error: BaseException | None = None,
    ) -> None:
        """Initialize with the reader to hand out, a description, a close log, and an error."""
        self._reader = reader
        self.description = description
        self.closed = False
        self._log = log
        self._fetch_error = fetch_error

    async def fetch_record_batch(self) -> _CountingAsyncReader:
        """
        Return the counting reader (poolhouse's reader creation is awaited).

        Raises ``fetch_error`` instead when one was configured, which stands in
        for a driver that reports an already-drained result at reader-creation
        time rather than on the first pull.
        """
        if self._fetch_error is not None:
            raise self._fetch_error
        return self._reader

    async def close(self) -> None:
        """Mark the cursor closed and record the close order."""
        self.closed = True
        if self._log is not None:
            self._log.append("cursor")


class _FakeAsyncConn:
    """
    Minimal duck-typed fake of adbc-poolhouse's ``AsyncConnection``.

    ``fail_on_close`` simulates a connection already invalidated by poolhouse's
    cancellation poison-recovery, which the sync cursor never had to face.
    """

    def __init__(self, log: list[str] | None = None, *, fail_on_close: bool = False) -> None:
        """Initialize with a shared close log and an optional close failure."""
        self.closed = False
        self._log = log
        self._fail_on_close = fail_on_close

    async def close(self) -> None:
        """Record the close order, then optionally raise as an invalidated connection would."""
        if self._log is not None:
            self._log.append("conn")
        if self._fail_on_close:
            raise RuntimeError("connection already invalidated")
        self.closed = True


def _fake_cursor(
    batches: Any,
    schema: Any,
    *,
    log: list[str] | None = None,
    fail_on_close: bool = False,
    fetch_error: BaseException | None = None,
) -> tuple[AsyncSemolinaCursor, _CountingAsyncReader, _FakeAsyncConn]:
    """Wire a counting reader, fake cursor, and fake connection into an AsyncSemolinaCursor."""
    reader = _CountingAsyncReader(schema, batches, log)
    description: list[tuple[Any, ...]] = [("revenue", None), ("country", None)]
    inner = _FakeAsyncCursor(reader, description, log, fetch_error)
    conn = _FakeAsyncConn(log, fail_on_close=fail_on_close)
    return AsyncSemolinaCursor(inner, conn), reader, conn


def _batch(pa: Any, schema: Any, revenue: list[int], country: list[str]) -> Any:
    """Build a single pyarrow RecordBatch from column lists."""
    return pa.RecordBatch.from_pydict({"revenue": revenue, "country": country}, schema=schema)


# ---------------------------------------------------------------------------
# TestAsyncRowMethods: awaited Row convenience methods
# ---------------------------------------------------------------------------


class TestAsyncRowMethods:
    """Test the awaited Row convenience methods against real DuckDB."""

    async def test_fetchall_rows_returns_all_rows(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """fetchall_rows() returns every remaining row as a Row keyed by column name."""
        async with await async_duckdb_engine.aexecute(sales_query) as cur:
            rows = await cur.fetchall_rows()

        assert len(rows) == 2
        assert all(isinstance(row, Row) for row in rows)
        assert {row["country"]: row["revenue"] for row in rows} == {"US": 1500, "CA": 2000}

    async def test_fetchone_row_then_none(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """fetchone_row() returns Rows until drained, then None."""
        async with await async_duckdb_engine.aexecute(sales_query) as cur:
            first = await cur.fetchone_row()
            second = await cur.fetchone_row()
            third = await cur.fetchone_row()

        assert isinstance(first, Row)
        assert isinstance(second, Row)
        assert third is None

    async def test_fetchmany_rows_respects_size(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """fetchmany_rows(2) returns at most two Row objects."""
        async with await async_duckdb_engine.aexecute(sales_query) as cur:
            rows = await cur.fetchmany_rows(2)

        assert len(rows) <= 2
        assert all(isinstance(row, Row) for row in rows)


class TestAsyncReadingPastTheEnd:
    """
    An awaited DBAPI fetch after the result is drained returns an empty result.

    The async twin of ``test_cursor.py``'s ``TestReadingPastTheEnd``: DuckDB's driver raises
    for any fetch after one that came back short.
    """

    async def test_fetchmany_rows_after_a_short_batch(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """The two-row result read three at a time ends with an empty batch."""
        async with await async_duckdb_engine.aexecute(sales_query) as cur:
            assert len(await cur.fetchmany_rows(3)) == 2
            assert await cur.fetchmany_rows(3) == []

    async def test_fetchmany_after_a_short_batch(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """The raw passthrough ends the same way."""
        async with await async_duckdb_engine.aexecute(sales_query) as cur:
            assert len(await cur.fetchmany(3)) == 2
            assert await cur.fetchmany(3) == []

    async def test_fetchone_row_after_fetchall_rows(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """A drained result has no next row."""
        async with await async_duckdb_engine.aexecute(sales_query) as cur:
            await cur.fetchall_rows()
            assert await cur.fetchone_row() is None

    async def test_fetchall_after_fetchall(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """A drained result has no rows left."""
        async with await async_duckdb_engine.aexecute(sales_query) as cur:
            await cur.fetchall()
            assert await cur.fetchall() == []


# ---------------------------------------------------------------------------
# TestAsyncDuplicateColumnNames: a result Rows cannot represent
# ---------------------------------------------------------------------------


async def _read_first_row(cur: AsyncSemolinaCursor) -> object:
    """Pull one Row by iteration, the way ``async for`` would."""
    return await anext(aiter(cur))


class TestAsyncDuplicateColumnNames:
    """A result with two columns of one name cannot become Rows, on the async path either."""

    @pytest.mark.parametrize(
        "read_rows",
        [
            lambda c: c.fetchall_rows(),
            lambda c: c.fetchone_row(),
            lambda c: c.fetchmany_rows(1),
            _read_first_row,
        ],
        ids=["fetchall_rows", "fetchone_row", "fetchmany_rows", "iteration"],
    )
    async def test_each_row_method_refuses_duplicate_column_names(
        self,
        async_duckdb_engine: Any,
        read_rows: Callable[[AsyncSemolinaCursor], Awaitable[object]],
    ) -> None:
        """Every way of reading Rows raises, naming the duplicated column."""
        conn = await async_duckdb_engine.connect()
        inner = conn.cursor()
        await inner.execute("SELECT 1 AS x, 2 AS x")

        async with AsyncSemolinaCursor(inner, conn) as cur:
            with pytest.raises(
                SemolinaDuplicateColumnsError, match=r"duplicate column names: \['x'\]"
            ):
                await read_rows(cur)


# ---------------------------------------------------------------------------
# TestAsyncOneReadPatternPerCursor: a result is read one way
# ---------------------------------------------------------------------------


class _CountryRow(BaseModel):
    """A DTO for the sales result's ``country`` column; ``revenue`` is ignored."""

    country: str


async def _all_dtos(cur: AsyncSemolinaCursor) -> object:
    """Read the result through ``iter_into()`` to the end, so its generator finishes."""
    return [dto async for dto in cur.iter_into(_CountryRow)]


ASYNC_READS: dict[str, Callable[[AsyncSemolinaCursor], Awaitable[object]]] = {
    "fetchone()": lambda c: c.fetchone(),
    "fetchall()": lambda c: c.fetchall(),
    "fetchone_row()": lambda c: c.fetchone_row(),
    "fetchall_rows()": lambda c: c.fetchall_rows(),
    "async for row in cursor": _read_first_row,
    "fetch_record_batch()": lambda c: c.fetch_record_batch(),
    "fetch_arrow_table()": lambda c: c.fetch_arrow_table(),
    "fetch_df()": lambda c: c.fetch_df(),
    "fetch_polars()": lambda c: c.fetch_polars(),
    "into()": lambda c: c.into(_CountryRow),
    "iter_into()": _all_dtos,
}
"""Each way of reading an async cursor's result, keyed by how the error message names it."""


class TestAsyncOneReadPatternPerCursor:
    """
    The async cursor holds the same rule as the sync one: the first way of reading is the only.

    Mixing used to either lose rows, as on the sync cursor, or raise poolhouse's
    ``ConnectionBusyError``, whose advice to check out a connection per task does not fit a
    single task reading one cursor two ways.
    """

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("async for row in cursor", "fetchall_rows()"),
            ("fetchone_row()", "async for row in cursor"),
            ("fetchone_row()", "fetch_arrow_table()"),
            ("fetch_arrow_table()", "async for row in cursor"),
            ("fetch_record_batch()", "async for row in cursor"),
            ("async for row in cursor", "fetch_record_batch()"),
            ("fetch_record_batch()", "fetch_polars()"),
            ("fetchall()", "into()"),
            ("iter_into()", "fetchone()"),
            ("fetch_df()", "fetch_arrow_table()"),
        ],
    )
    async def test_a_second_way_of_reading_is_refused(
        self, sales_query: Query, async_duckdb_engine: Any, first: str, second: str
    ) -> None:
        """The second read raises, naming both calls, and the connection still goes back."""
        pytest.importorskip("pandas")
        pytest.importorskip("polars")

        async with await async_duckdb_engine.aexecute(sales_query) as cur:
            await ASYNC_READS[first](cur)
            with pytest.raises(SemolinaResultConsumedError) as excinfo:
                await ASYNC_READS[second](cur)

        message = str(excinfo.value)
        assert first in message
        assert second in message
        assert async_duckdb_engine._pool._pool.checkedout() == 0

    async def test_the_dbapi_fetches_share_one_read(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """``fetchone``, ``fetchmany`` and ``fetchall``, raw or as Rows, interleave freely."""
        async with await async_duckdb_engine.aexecute(sales_query) as cur:
            first = await cur.fetchone_row()
            rest = await cur.fetchall()

        assert first is not None
        assert len(rest) == 1

    async def test_repeating_a_read_carries_on_from_where_it_stopped(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """Iterating again after a ``break`` resumes; it is the same read, not a second one."""
        async with await async_duckdb_engine.aexecute(sales_query) as cur:
            async for _row in cur:
                break
            rest = [row async for row in cur]

        assert len(rest) == 1


# ---------------------------------------------------------------------------
# TestAsyncPassthrough: raw tuples, Arrow, and the synchronous properties
# ---------------------------------------------------------------------------


class TestAsyncPassthrough:
    """Test raw DBAPI passthroughs, Arrow passthroughs, and property reads."""

    async def test_fetchall_returns_raw_tuples(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """fetchall() returns raw tuples, not Row objects."""
        async with await async_duckdb_engine.aexecute(sales_query) as cur:
            rows = await cur.fetchall()

        assert len(rows) == 2
        assert all(isinstance(row, tuple) for row in rows)

    async def test_fetchone_and_fetchmany_return_raw_tuples(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """fetchone() and fetchmany() return raw tuples."""
        async with await async_duckdb_engine.aexecute(sales_query) as cur:
            one = await cur.fetchone()
            rest = await cur.fetchmany(5)

        assert isinstance(one, tuple)
        assert all(isinstance(row, tuple) for row in rest)

    async def test_fetch_arrow_table_matches_fetchall_rows(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """fetch_arrow_table()'s row count matches fetchall_rows() on a fresh cursor."""
        pytest.importorskip("pyarrow")

        async with await async_duckdb_engine.aexecute(sales_query) as cur:
            table = await cur.fetch_arrow_table()
        async with await async_duckdb_engine.aexecute(sales_query) as cur2:
            rows = await cur2.fetchall_rows()

        assert table.num_rows == len(rows)

    async def test_fetch_record_batch_returns_a_reader(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """fetch_record_batch() hands back an async reader that yields batches."""
        pytest.importorskip("pyarrow")

        cursor = await async_duckdb_engine.aexecute(sales_query)
        reader = await cursor.fetch_record_batch()
        try:
            batch = await reader.__anext__()
            assert batch.num_rows > 0
        finally:
            await reader.close()
            await cursor.aclose()

    async def test_fetch_record_batch_is_idempotent(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """
        A second fetch_record_batch() hands back the reader the first one created.

        poolhouse locks the connection for a reader's whole lifetime and rejects
        a second reader on the same connection, so the only safe answer to a
        repeat call is the reader already in flight.
        """
        pytest.importorskip("pyarrow")

        async with await async_duckdb_engine.aexecute(sales_query) as cur:
            first = await cur.fetch_record_batch()
            second = await cur.fetch_record_batch()

            assert second is first

    async def test_description_and_rowcount_are_sync_properties(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """Both description and rowcount are readable without an await."""
        async with await async_duckdb_engine.aexecute(sales_query) as cur:
            description = cur.description
            rowcount = cur.rowcount

        assert description is not None
        assert {d[0] for d in description} == {"revenue", "country"}
        assert isinstance(rowcount, int)


# ---------------------------------------------------------------------------
# TestAsyncStreamingIteration: laziness and batch semantics
# ---------------------------------------------------------------------------


class TestAsyncStreamingIteration:
    """Test async streaming semantics over the record batch reader."""

    def test_aiter_returns_self_for_stream(self) -> None:
        """aiter(cur) is cur — the cursor is its own async iterator."""
        pa = pytest.importorskip("pyarrow")

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        cur, _reader, _conn = _fake_cursor(iter([]), schema)
        assert cur.__aiter__() is cur

    async def test_stream_yields_rows_from_multiple_batches(self) -> None:
        """Iteration yields rows from every non-empty batch, in order."""
        pa = pytest.importorskip("pyarrow")

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        batches = [
            _batch(pa, schema, [1, 2], ["US", "CA"]),
            _batch(pa, schema, [3, 4], ["MX", "FR"]),
            _batch(pa, schema, [5, 6], ["DE", "JP"]),
        ]
        cur, _reader, _conn = _fake_cursor(iter(batches), schema)

        rows = [row async for row in cur]
        assert [row.revenue for row in rows] == [1, 2, 3, 4, 5, 6]
        assert [row.country for row in rows] == ["US", "CA", "MX", "FR", "DE", "JP"]

    async def test_stream_pulls_one_batch_at_a_time(self) -> None:
        """
        Each batch is pulled only when its first row is needed.

        This is the discriminator against materializing the whole result behind
        a streaming interface: a cursor that drained the reader up front would
        show read_count == 4 after the first row.
        """
        pa = pytest.importorskip("pyarrow")

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        batches = [
            _batch(pa, schema, [1, 2], ["US", "CA"]),
            _batch(pa, schema, [3, 4], ["MX", "FR"]),
            _batch(pa, schema, [5, 6], ["DE", "JP"]),
        ]
        cur, reader, _conn = _fake_cursor(iter(batches), schema)

        # First row forces the first batch to be pulled.
        first = await cur.__anext__()
        assert first.revenue == 1
        assert reader.read_count == 1

        # Second row comes from the same batch — no new pull.
        second = await cur.__anext__()
        assert second.revenue == 2
        assert reader.read_count == 1

        # Third row exhausts batch 1 -> pulls batch 2.
        third = await cur.__anext__()
        assert third.revenue == 3
        assert reader.read_count == 2

        # Abandon iteration — batch 3 must NOT have been pulled.
        assert reader.read_count == 2

    async def test_stream_skips_empty_batches(self) -> None:
        """A zero-row batch mid-stream is skipped, not yielded as a row."""
        pa = pytest.importorskip("pyarrow")

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        empty = _batch(pa, schema, [], [])
        cur, _reader, _conn = _fake_cursor(
            iter(
                [
                    empty,
                    _batch(pa, schema, [10, 20], ["US", "CA"]),
                    empty,
                    _batch(pa, schema, [30, 40], ["MX", "FR"]),
                ]
            ),
            schema,
        )

        rows = [row async for row in cur]
        assert [row.revenue for row in rows] == [10, 20, 30, 40]

    async def test_stream_reiteration_yields_nothing(self) -> None:
        """Re-entering the loop on an exhausted cursor yields zero rows and does not raise."""
        pa = pytest.importorskip("pyarrow")

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        cur, _reader, _conn = _fake_cursor(iter([_batch(pa, schema, [1], ["US"])]), schema)

        first_pass = [row async for row in cur]
        second_pass = [row async for row in cur]
        assert len(first_pass) == 1
        assert second_pass == []

    async def test_stream_normalises_a_drained_reader_creation_error(self) -> None:
        """
        A driver reporting the drain when the reader is created also stops cleanly.

        DuckDB reports it on the first pull, so the real-engine test above cannot
        reach this arm. Other ADBC drivers report it at creation instead, which
        is why the synchronous cursor guards both call sites — this is the async
        half of that parity.
        """
        pa = pytest.importorskip("pyarrow")

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        cur, _reader, _conn = _fake_cursor(
            iter([]),
            schema,
            fetch_error=OSError("Attempting to execute an unsuccessful or closed query result"),
        )

        assert [row async for row in cur] == []

    async def test_stream_normalises_a_drained_reader_pull_error(self) -> None:
        """
        A driver that reports the drain on a batch pull stops iteration cleanly.

        That ``OSError`` crosses poolhouse's thread boundary unchanged, unlike the driver's
        ordinary end of stream, so the cursor has to normalize it itself.
        """
        pa = pytest.importorskip("pyarrow")

        def drained() -> Any:
            raise OSError("Attempting to execute an unsuccessful or closed query result")
            yield  # unreachable; it makes this a generator, so the raise comes on the first pull

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        cur, _reader, _conn = _fake_cursor(drained(), schema)

        async with cur:
            assert [row async for row in cur] == []
            assert [row async for row in cur] == []

    async def test_stream_does_not_auto_close(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """Iterating to exhaustion does NOT close the cursor."""
        cursor = await async_duckdb_engine.aexecute(sales_query)
        try:
            rows = [row async for row in cursor]
            assert len(rows) == 2
            assert cursor._closed is False
        finally:
            await cursor.aclose()


# ---------------------------------------------------------------------------
# TestAsyncCursorClose: the mandatory close order and its edge cases
# ---------------------------------------------------------------------------


class TestAsyncCursorClose:
    """Test the ordered close, its idempotence, and its failure tolerance."""

    async def test_close_order_is_reader_cursor_connection(self) -> None:
        """aclose() closes the reader first, then the cursor, then the connection."""
        pa = pytest.importorskip("pyarrow")

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        log: list[str] = []
        cur, reader, _conn = _fake_cursor(
            iter([_batch(pa, schema, [1, 2], ["US", "CA"])]), schema, log=log
        )

        await cur.__anext__()
        await cur.aclose()

        assert log == ["reader", "cursor", "conn"]
        assert reader.closed is True

    async def test_close_with_live_reader_returns_connection_to_pool(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """
        Closing a partially-consumed real cursor does not raise and frees the slot.

        A fake cannot prove this: the guard being satisfied is a property of
        adbc-poolhouse's real connection, which rejects a foreign close while a
        reader is live.
        """
        cursor = await async_duckdb_engine.aexecute(sales_query)
        first = await cursor.__anext__()
        assert isinstance(first, Row)
        # The reader is live and undrained at this point.
        assert cursor._reader is not None
        assert async_duckdb_engine._pool._pool.checkedout() == 1

        await cursor.aclose()

        assert async_duckdb_engine._pool._pool.checkedout() == 0

    async def test_close_without_iterating_returns_connection_to_pool(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """Closing a cursor that was never iterated also frees the slot."""
        cursor = await async_duckdb_engine.aexecute(sales_query)
        assert cursor._reader is None

        await cursor.aclose()

        assert async_duckdb_engine._pool._pool.checkedout() == 0

    async def test_close_returns_the_slot_after_public_fetch_record_batch(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """
        A reader taken through the public passthrough is still closed by aclose().

        The reader the cursor creates for its own iteration is not the only one
        that locks the connection: one handed to the caller by
        ``fetch_record_batch()`` locks it identically, and draining it does not
        release the lock. If ``aclose()`` does not own that reader, the cursor
        and connection closes both raise ``ConnectionBusyError`` into the
        suppressor and the pool slot never comes back.
        """
        pytest.importorskip("pyarrow")

        async with await async_duckdb_engine.aexecute(sales_query) as cur:
            reader = await cur.fetch_record_batch()
            async for _batch in reader:
                pass

        assert async_duckdb_engine._pool._pool.checkedout() == 0

    async def test_close_is_idempotent(self) -> None:
        """A second aclose() is a no-op rather than a second teardown."""
        pa = pytest.importorskip("pyarrow")

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        log: list[str] = []
        cur, _reader, _conn = _fake_cursor(iter([_batch(pa, schema, [1], ["US"])]), schema, log=log)

        await cur.__anext__()
        await cur.aclose()
        await cur.aclose()

        assert log == ["reader", "cursor", "conn"]

    async def test_close_tolerates_invalidated_connection(self) -> None:
        """aclose() completes when the connection was already invalidated."""
        pa = pytest.importorskip("pyarrow")

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        log: list[str] = []
        cur, reader, _conn = _fake_cursor(
            iter([_batch(pa, schema, [1], ["US"])]), schema, log=log, fail_on_close=True
        )

        await cur.__anext__()
        await cur.aclose()

        assert cur._closed is True
        assert reader.closed is True
        assert log == ["reader", "cursor", "conn"]

    async def test_close_warns_when_the_connection_cannot_be_returned(self) -> None:
        """
        A connection close that fails is suppressed, but reported as a ResourceWarning.

        Suppressing the failure is right — teardown must not mask the caller's
        own error — but suppressing it *silently* turns a failed check-in into a
        permanent pool leak that nothing anywhere records. The class already
        warns about the leak it cannot prevent in ``__del__``; the leak it can
        detect deserves the same vocabulary.
        """
        pa = pytest.importorskip("pyarrow")

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        cur, _reader, _conn = _fake_cursor(
            iter([_batch(pa, schema, [1], ["US"])]), schema, fail_on_close=True
        )

        await cur.__anext__()
        with pytest.warns(ResourceWarning, match="could not return its pooled connection"):
            await cur.aclose()

        assert cur._closed is True

    async def test_close_does_not_warn_on_a_clean_teardown(self) -> None:
        """A teardown that succeeds emits no ResourceWarning."""
        pa = pytest.importorskip("pyarrow")

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        cur, _reader, _conn = _fake_cursor(iter([_batch(pa, schema, [1], ["US"])]), schema)

        await cur.__anext__()
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            await cur.aclose()

    async def test_close_via_async_context_manager(self) -> None:
        """`async with` runs the same ordered close on exit."""
        pa = pytest.importorskip("pyarrow")

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        log: list[str] = []
        cur, _reader, _conn = _fake_cursor(iter([_batch(pa, schema, [1], ["US"])]), schema, log=log)

        async with cur as entered:
            assert entered is cur
            await cur.__anext__()

        assert log == ["reader", "cursor", "conn"]
        assert cur._closed is True


# ---------------------------------------------------------------------------
# TestAsyncCursorRepr
# ---------------------------------------------------------------------------


class TestAsyncCursorRepr:
    """Test repr in the open and closed states."""

    async def test_repr_open_lists_columns(self) -> None:
        """An open cursor's repr lists its columns."""
        pa = pytest.importorskip("pyarrow")

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        cur, _reader, _conn = _fake_cursor(iter([]), schema)

        text = repr(cur)
        assert "AsyncSemolinaCursor" in text
        assert "open" in text
        assert "revenue" in text

    async def test_repr_closed(self) -> None:
        """A closed cursor's repr says so and lists no columns."""
        pa = pytest.importorskip("pyarrow")

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        cur, _reader, _conn = _fake_cursor(iter([]), schema)

        await cur.aclose()
        assert repr(cur) == "<AsyncSemolinaCursor closed>"


# ---------------------------------------------------------------------------
# Dataframe returns, and the optional-dependency guards
# ---------------------------------------------------------------------------


@pytest.fixture
def async_probe_engine() -> Generator[Any, None, None]:
    """
    Yield an async in-memory DuckDB engine carrying the probe view, closing its pool after.

    The async twin of ``test_cursor.py``'s ``probe_engine``, and the same view: the dataframe
    methods make a claim about what comes back from a real semantic-view result, so the fakes the
    rest of this module uses cannot answer it. Teardown is the inline synchronous ``close_pool`` on
    the inner pool, as ``conftest.py``'s ``async_duckdb_engine`` does — this fixture is synchronous
    and cannot await.
    """
    pytest.importorskip("adbc_driver_duckdb")
    from adbc_poolhouse import DuckDBConfig, close_pool
    from sqlalchemy import event

    from semolina.config import create_async_engine

    engine = create_async_engine(DuckDBConfig(database=":memory:", pool_size=1))
    event.listen(engine._pool._pool, "connect", setup_probe_view, once=True)

    yield engine
    close_pool(engine._pool._pool)


async def _async_probe_cursor(engine: Any) -> AsyncSemolinaCursor:
    """
    Execute the region-by-decimal-metric query and return its open async cursor.

    A fresh cursor per call, deliberately. ``fetch_polars()`` must be the first consuming call
    on a cursor, so a shared fixture would make one of these tests fail for a reason that has
    nothing to do with the return type it is asserting.

    Args:
        engine: The async probe engine.

    Returns:
        An open :class:`~semolina.acursor.AsyncSemolinaCursor`. The caller closes it.
    """
    query = (
        TypeFidelityView.query()
        .metrics(TypeFidelityView.total_order_value)
        .dimensions(TypeFidelityView.region)
    )
    cursor: AsyncSemolinaCursor = await engine.aexecute(query)
    return cursor


def _find_spec_without(missing: str) -> Callable[..., Any]:
    """
    Build a ``find_spec`` replacement that reports exactly one package absent.

    A blanket ``return_value=None`` would make the *pyarrow* guard fire first inside
    ``fetch_df``, so a test written that way would assert the wrong error's message and still
    pass. Same helper as ``tests/unit/test_cursor.py``'s, restated so this module stays
    self-contained.

    Args:
        missing: The importable name to report as absent.

    Returns:
        A drop-in for ``importlib.util.find_spec`` that defers to the real one for every other
        name.
    """
    real = importlib.util.find_spec

    def fake(name: str, package: str | None = None) -> Any:
        if name == missing:
            return None
        return real(name, package)

    return fake


def _guarded_cursor(inner: Any = None) -> AsyncSemolinaCursor:
    """
    Wrap a stub in an ``AsyncSemolinaCursor`` whose teardown is clean and awaitable.

    The stub carries only what the test under it needs. A guard that failed to fire therefore
    surfaces as an ``AttributeError`` rather than as a passing test, which is the point. The
    ``close`` coroutines exist because this cursor has no ``__del__`` rescue: a cursor dropped
    unclosed emits a ``ResourceWarning`` about a leaked pool slot, and a leak warning that is
    really a test artifact is worse than no warning at all.

    Args:
        inner: The stub cursor to delegate to. Defaults to one carrying nothing but ``close``.

    Returns:
        An open :class:`~semolina.acursor.AsyncSemolinaCursor`.
    """

    async def _close() -> None:
        return None

    cursor = inner if inner is not None else SimpleNamespace(close=_close)
    return AsyncSemolinaCursor(cursor, SimpleNamespace(close=_close))


class TestAsyncFetchDf:
    """fetch_df() returns a real pandas DataFrame from the live async path."""

    async def test_returns_a_pandas_dataframe(self, async_probe_engine: Any) -> None:
        """
        ``await cursor.fetch_df()`` returns a ``pandas.DataFrame`` carrying the query's rows.

        Asserted by ``isinstance`` against the class imported here, not against a name in the
        annotation: the annotation is what this test exists to check, so trusting it would
        prove only that Semolina agrees with itself.
        """
        pandas = pytest.importorskip("pandas")
        pytest.importorskip("pyarrow")

        async with await _async_probe_cursor(async_probe_engine) as cursor:
            frame = await cursor.fetch_df()

        assert isinstance(frame, pandas.DataFrame)
        assert "region" in frame.columns
        assert set(frame["region"]) == {"US", "MX", "CA"}


class TestAsyncFetchPolars:
    """fetch_polars() returns a polars DataFrame across the thread offload."""

    async def test_returns_a_polars_dataframe(self, async_probe_engine: Any) -> None:
        """
        ``await cursor.fetch_polars()`` returns a ``polars.DataFrame``, called first.

        First consuming call, deliberately: ADBC's implementation takes the cursor's Arrow
        stream handle, and that rule is a property of the driver rather than of threading, so
        it holds identically on this side of poolhouse's offload.
        """
        polars = pytest.importorskip("polars")

        async with await _async_probe_cursor(async_probe_engine) as cursor:
            frame = await cursor.fetch_polars()

        assert isinstance(frame, polars.DataFrame)
        assert "region" in frame.columns
        assert set(frame.get_column("region")) == {"US", "MX", "CA"}


class TestAsyncMissingDependencyGuards:
    """
    Every async Arrow/dataframe method names its own package and install command.

    The guard set per method is what ADBC's implementation actually imports, read at
    ``adbc_driver_manager/dbapi.py``, and unchanged here, because adbc-poolhouse
    offloads those same calls rather than reimplementing them. The guard has to live on this
    side of the offload: poolhouse never imports pandas or polars and states that it lets the
    driver's native ``ModuleNotFoundError`` surface unchanged, so without these lines the
    caller gets that error raised on a worker thread instead.
    """

    @pytest.mark.parametrize(
        ("method_name", "missing", "extra"),
        [
            ("fetch_arrow_table", "pyarrow", "pyarrow"),
            ("fetch_record_batch", "pyarrow", "pyarrow"),
            ("fetch_df", "pandas", "pandas"),
            ("fetch_polars", "polars", "polars"),
        ],
    )
    async def test_each_method_names_its_own_extra(
        self, method_name: str, missing: str, extra: str
    ) -> None:
        """
        The raised message names the absent package AND the exact install command.

        The literal ``pip install semolina[<extra>]`` string is checked per method, so a
        copy-paste error giving every method the same extra fails here rather than shipping.
        """
        cursor = _guarded_cursor()

        async with cursor:
            with (
                patch("importlib.util.find_spec", side_effect=_find_spec_without(missing)),
                pytest.raises(SemolinaMissingDependencyError) as excinfo,
            ):
                await getattr(cursor, method_name)()

            message = str(excinfo.value)
            assert missing in message
            assert f"pip install semolina[{extra}]" in message

    @pytest.mark.parametrize(
        "read",
        [
            lambda c: c.fetchall_rows(),
            lambda c: c.fetchone_row(),
            lambda c: c.fetchmany_rows(),
            lambda c: c.fetchall(),
            lambda c: c.fetchone(),
            lambda c: c.fetchmany(),
            _read_first_row,
        ],
        ids=[
            "fetchall_rows",
            "fetchone_row",
            "fetchmany_rows",
            "fetchall",
            "fetchone",
            "fetchmany",
            "iteration",
        ],
    )
    async def test_every_row_method_names_the_pyarrow_extra(
        self, read: Callable[[AsyncSemolinaCursor], Awaitable[object]]
    ) -> None:
        """
        Reading rows without pyarrow raises Semolina's error, naming ``semolina[pyarrow]``.

        Before, only iteration was guarded. The row methods reached ``description`` or the
        driver's row iterator first, both of which need pyarrow, and failed on a worker thread
        with the driver's own error.
        """
        cursor = _guarded_cursor()

        async with cursor:
            with (
                patch("importlib.util.find_spec", side_effect=_find_spec_without("pyarrow")),
                pytest.raises(
                    SemolinaMissingDependencyError, match=r"pip install semolina\[pyarrow\]"
                ),
            ):
                await read(cursor)

    async def test_fetch_df_reports_pyarrow_before_pandas(self) -> None:
        """
        With pyarrow absent, fetch_df() says pyarrow — because ADBC gets there first.

        ADBC's ``fetch_df`` is ``self.reader.read_pandas()``, and the ``reader`` property calls
        ``_requires_pyarrow()`` before anything imports pandas. Guarding pandas first would let
        ADBC's own ``ProgrammingError`` win on a pyarrow-less install, from inside a worker
        thread, naming neither Semolina nor the extra.
        """
        cursor = _guarded_cursor()

        async with cursor:
            with (
                patch("importlib.util.find_spec", side_effect=_find_spec_without("pyarrow")),
                pytest.raises(SemolinaMissingDependencyError) as excinfo,
            ):
                await cursor.fetch_df()

            assert "pip install semolina[pyarrow]" in str(excinfo.value)

    async def test_fetch_polars_does_not_require_pyarrow(self) -> None:
        """
        With pyarrow absent and polars present, fetch_polars() still delegates.

        The same deliberate asymmetry as on the sync cursor, restated here for the same reason:
        it stops a later "let's make these guards consistent" tidy-up from silently breaking a
        call that works on a polars-and-no-pyarrow install.
        """
        sentinel = object()

        async def _fetch_polars() -> object:
            return sentinel

        async def _close() -> None:
            return None

        cursor = _guarded_cursor(SimpleNamespace(fetch_polars=_fetch_polars, close=_close))

        async with cursor:
            with patch("importlib.util.find_spec", side_effect=_find_spec_without("pyarrow")):
                assert await cursor.fetch_polars() is sentinel
