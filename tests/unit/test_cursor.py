"""
Tests for SemolinaCursor DBAPI 2.0 delegation and Row convenience methods.

Tests cover:
- SemolinaCursor wraps DBAPI 2.0 cursor via delegation
- fetchall_rows() returns list[Row]
- fetchmany_rows(size) returns list[Row]
- fetchone_row() returns Row | None
- Row attribute and dict access via SemolinaCursor
- fetch_record_batch() returns pyarrow.RecordBatchReader (ADBC passthrough)
- __iter__/__next__ yield Row objects lazily from RecordBatchReader
- fetch_df() returns a pandas.DataFrame, fetch_polars() a polars.DataFrame
- every Arrow/dataframe method names the missing package and its install command

Test classes:
- TestSemolinaCursor: init, description passthrough
- TestFetchallRows: fetchall_rows with data, empty, attribute/dict access
- TestFetchoneRow: fetchone_row iteration and exhaustion
- TestFetchmanyRows: fetchmany_rows with various sizes
- TestDuplicateColumnNames: Row methods refuse a result with a repeated column name
- TestSemolinaCursorContextManager: context manager lifecycle
- TestSemolinaCursorRepr: repr in open/closed states
- TestSemolinaCursorPassthrough: raw DBAPI passthrough methods
- TestOneReadPatternPerCursor: the first way of reading a result is the only way
- TestFetchArrowTable: ADBC Arrow passthrough (DuckDB in-process)
- TestFetchRecordBatch: ADBC RecordBatchReader passthrough
- TestStreamingIteration: __iter__/__next__ semantics over RecordBatchReader
- TestFetchDf: fetch_df() against a live DuckDB semantic view
- TestFetchPolars: fetch_polars() return type and its first-consuming-call rule
- TestMissingDependencyGuards: what each method demands, and what it does not
"""

from __future__ import annotations

import importlib.util
import warnings
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel
from type_fidelity_probe import TypeFidelityView, make_probe_engine

from semolina.cursor import SemolinaCursor
from semolina.exceptions import (
    SemolinaDuplicateColumnsError,
    SemolinaMissingDependencyError,
    SemolinaResultConsumedError,
)
from semolina.results import Row

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from semolina.engines.base import Engine


_OPEN_CONNECTIONS: list[Any] = []
"""Connections :func:`_make_cursor` opened during the current test, closed after it."""

_SQL_TYPES = {int: "INTEGER", str: "VARCHAR"}
"""Column type per fixture value type, so a value comes back as the type it went in as."""


@pytest.fixture(autouse=True)
def close_made_cursors() -> Generator[None, None, None]:
    """Close every connection ``_make_cursor`` opened, however the test ended."""
    yield
    while _OPEN_CONNECTIONS:
        _OPEN_CONNECTIONS.pop().close()


def _make_cursor(
    fixture_data: list[dict[str, Any]],
    view_name: str = "test_view",
) -> SemolinaCursor:
    """
    Create a SemolinaCursor over a DuckDB table holding ``fixture_data``.

    Each column is typed from its first value, so ``1000`` comes back as ``1000`` rather than
    as the string ``"1000"``. The connection is closed after the test by
    :func:`close_made_cursors`.
    """
    adbc_driver_duckdb = pytest.importorskip("adbc_driver_duckdb")
    import adbc_driver_manager.dbapi as dbapi

    driver = adbc_driver_duckdb.driver_path()
    conn = dbapi.connect(
        driver=driver, entrypoint="duckdb_adbc_init", db_kwargs={"path": ":memory:"}
    )
    _OPEN_CONNECTIONS.append(conn)
    cur = conn.cursor()

    if fixture_data:
        columns = list(fixture_data[0].keys())
        col_defs = ", ".join(
            f"{col} {_SQL_TYPES[type(value)]}" for col, value in fixture_data[0].items()
        )
        cur.execute(f"CREATE TABLE {view_name} ({col_defs})")
        placeholders = ", ".join("?" for _ in columns)
        for row in fixture_data:
            cur.execute(f"INSERT INTO {view_name} VALUES ({placeholders})", list(row.values()))
        cur.execute(f"SELECT {', '.join(columns)} FROM {view_name}")
    else:
        cur.execute(f"CREATE TABLE {view_name} (dummy INTEGER)")
        cur.execute(f"SELECT * FROM {view_name} WHERE 1=0")

    return SemolinaCursor(cur, conn)


def _make_adbc_cursor(
    *,
    create_sql: str,
    insert_sql: str | None = None,
    select_sql: str,
) -> tuple[SemolinaCursor, Any]:
    """
    Create a SemolinaCursor wrapping a real DuckDB ADBC cursor.

    Returns (SemolinaCursor, connection) -- caller must close connection.
    """
    adbc_driver_duckdb = pytest.importorskip("adbc_driver_duckdb")
    import adbc_driver_manager.dbapi as dbapi

    driver = adbc_driver_duckdb.driver_path()
    conn = dbapi.connect(
        driver=driver, entrypoint="duckdb_adbc_init", db_kwargs={"path": ":memory:"}
    )
    cur = conn.cursor()
    cur.execute(create_sql)
    if insert_sql is not None:
        cur.execute(insert_sql)
    cur.execute(select_sql)
    return SemolinaCursor(cur, conn), conn


FIXTURE_DATA: list[dict[str, Any]] = [
    {"revenue": 1000, "country": "US"},
    {"revenue": 2000, "country": "CA"},
    {"revenue": 500, "country": "MX"},
]


class _CountingReader:
    """
    Duck-typed fake of ``pyarrow.RecordBatchReader`` for streaming tests.

    Counts calls to ``read_next_batch`` so tests can assert laziness. We
    duck-type instead of subclassing because pyarrow forbids subclassing
    ``RecordBatchReader``.
    """

    def __init__(self, schema: Any, batches: Any, log: list[str] | None = None) -> None:
        """
        Initialize with a schema, an iterator of batches, and an optional close log.

        Args:
            schema: pyarrow schema describing the batches.
            batches: iterator (or iterable) of pyarrow.RecordBatch objects.
            log: shared list appended to on close, so tests can assert the reader closed
                before the cursor and connection.
        """
        self.schema = schema
        self.batches = iter(batches)
        self.read_count = 0
        self.closed = False
        self._log = log

    def __iter__(self) -> _CountingReader:
        """Return self so the reader is its own iterator."""
        return self

    def read_next_batch(self) -> Any:
        """
        Return the next batch, raising StopIteration when exhausted.

        Returns:
            The next pyarrow.RecordBatch from the underlying iterator.
        """
        self.read_count += 1
        return next(self.batches)

    def __next__(self) -> Any:
        """Delegate to ``read_next_batch`` for iterator protocol parity."""
        return self.read_next_batch()

    def close(self) -> None:
        """Mark the reader as closed and record the close order."""
        self.closed = True
        if self._log is not None:
            self._log.append("reader")


def _logging_cursor(
    log: list[str],
    *,
    fail_cursor_close: bool = False,
    fail_conn_close: bool = False,
) -> tuple[SemolinaCursor, _CountingReader]:
    """
    Wire a one-row reader, a fake cursor and a fake connection that log their closes.

    Args:
        log: Shared list each close appends to, in the order the closes happen.
        fail_cursor_close: Make the inner cursor's ``close()`` raise, as a driver can.
        fail_conn_close: Make the connection's ``close()`` raise, as an invalidated one does.

    Returns:
        The cursor under test and the reader its inner cursor hands out.
    """
    pa = pytest.importorskip("pyarrow")

    schema = pa.schema([("revenue", pa.int64())])
    batch = pa.RecordBatch.from_pydict({"revenue": [1]}, schema=schema)
    reader = _CountingReader(schema, iter([batch]), log)

    def close_cursor() -> None:
        log.append("cursor")
        if fail_cursor_close:
            raise RuntimeError("cursor close failed")

    def close_conn() -> None:
        log.append("conn")
        if fail_conn_close:
            raise RuntimeError("connection already invalidated")

    inner = SimpleNamespace(
        fetch_record_batch=lambda: reader, description=[("revenue", None)], close=close_cursor
    )
    return SemolinaCursor(inner, SimpleNamespace(close=close_conn)), reader


# ---------------------------------------------------------------------------
# TestSemolinaCursor: init and description passthrough
# ---------------------------------------------------------------------------


class TestSemolinaCursor:
    """Test SemolinaCursor construction and basic property delegation."""

    def test_description_delegates_to_underlying_cursor(self) -> None:
        """Description property returns underlying cursor's description."""
        sc = _make_cursor(FIXTURE_DATA)
        desc = sc.description
        assert desc is not None
        col_names = [d[0] for d in desc]
        assert "revenue" in col_names
        assert "country" in col_names

    def test_rowcount_delegates_to_underlying_cursor(self) -> None:
        """Rowcount is whatever the underlying cursor reports, unchanged."""
        sc = SemolinaCursor(SimpleNamespace(rowcount=7), object())
        assert sc.rowcount == 7


# ---------------------------------------------------------------------------
# TestFetchallRows: fetchall_rows convenience method
# ---------------------------------------------------------------------------


class TestFetchallRows:
    """Test fetchall_rows() returns list[Row] with correct values."""

    def test_fetchall_rows_returns_list_of_rows(self) -> None:
        """fetchall_rows() after execute returns list[Row]."""
        sc = _make_cursor(FIXTURE_DATA)
        rows = sc.fetchall_rows()
        assert isinstance(rows, list)
        assert len(rows) == 3
        assert all(isinstance(r, Row) for r in rows)

    def test_fetchall_rows_empty_results(self) -> None:
        """fetchall_rows() with empty results returns []."""
        sc = _make_cursor([])
        rows = sc.fetchall_rows()
        assert rows == []

    def test_fetchall_rows_attribute_access(self) -> None:
        """fetchall_rows() Row objects support attribute access."""
        sc = _make_cursor(FIXTURE_DATA)
        rows = sc.fetchall_rows()
        assert [row.revenue for row in rows] == [1000, 2000, 500]

    def test_fetchall_rows_dict_access(self) -> None:
        """fetchall_rows() Row objects support dict access."""
        sc = _make_cursor(FIXTURE_DATA)
        rows = sc.fetchall_rows()
        assert rows[0]["country"] == "US"
        assert rows[1]["country"] == "CA"
        assert rows[2]["country"] == "MX"


# ---------------------------------------------------------------------------
# TestFetchoneRow: fetchone_row convenience method
# ---------------------------------------------------------------------------


class TestFetchoneRow:
    """Test fetchone_row() returns Row or None."""

    def test_fetchone_row_returns_first_then_next(self) -> None:
        """fetchone_row() returns first Row, then next, then None when exhausted."""
        sc = _make_cursor(FIXTURE_DATA)
        row1 = sc.fetchone_row()
        assert isinstance(row1, Row)
        assert row1.revenue == 1000

        row2 = sc.fetchone_row()
        assert isinstance(row2, Row)
        assert row2.revenue == 2000

        row3 = sc.fetchone_row()
        assert isinstance(row3, Row)
        assert row3.revenue == 500

        row4 = sc.fetchone_row()
        assert row4 is None

    def test_fetchone_row_empty_cursor(self) -> None:
        """fetchone_row() on empty cursor returns None."""
        sc = _make_cursor([])
        assert sc.fetchone_row() is None


# ---------------------------------------------------------------------------
# TestFetchmanyRows: fetchmany_rows convenience method
# ---------------------------------------------------------------------------


class TestFetchmanyRows:
    """Test fetchmany_rows(size) returns list[Row] of up to size rows."""

    def test_fetchmany_rows_returns_requested_count(self) -> None:
        """fetchmany_rows(2) on 3-row cursor returns 2 Rows, next call returns 1."""
        sc = _make_cursor(FIXTURE_DATA)
        batch1 = sc.fetchmany_rows(2)
        assert len(batch1) == 2
        assert all(isinstance(r, Row) for r in batch1)

        batch2 = sc.fetchmany_rows(2)
        assert len(batch2) == 1

    def test_fetchmany_rows_defaults_to_one(self) -> None:
        """fetchmany_rows() defaults to size=1."""
        sc = _make_cursor(FIXTURE_DATA)
        batch = sc.fetchmany_rows()
        assert len(batch) == 1
        assert batch[0].revenue == 1000

    def test_fetchmany_rows_larger_than_available(self) -> None:
        """fetchmany_rows(10) on 3-row cursor returns all 3."""
        sc = _make_cursor(FIXTURE_DATA)
        rows = sc.fetchmany_rows(10)
        assert len(rows) == 3


class TestReadingPastTheEnd:
    """
    A DBAPI fetch after the result is drained returns an empty result, as DB-API says.

    DuckDB's ADBC driver raises ``InternalError`` ("closed pending query result") for any
    fetch after one that came back short, so ``while batch := fetchmany_rows(n)`` crashed on
    its last call instead of stopping.
    """

    @pytest.mark.parametrize(
        ("drain", "again", "expected"),
        [
            pytest.param(
                lambda sc: (sc.fetchmany_rows(2), sc.fetchmany_rows(2)),
                lambda sc: sc.fetchmany_rows(2),
                [],
                id="fetchmany_rows-after-short-batch",
            ),
            pytest.param(
                lambda sc: (sc.fetchmany(2), sc.fetchmany(2)),
                lambda sc: sc.fetchmany(2),
                [],
                id="fetchmany-after-short-batch",
            ),
            pytest.param(
                lambda sc: sc.fetchall_rows(),
                lambda sc: sc.fetchone_row(),
                None,
                id="fetchone_row-after-fetchall_rows",
            ),
            pytest.param(
                lambda sc: sc.fetchall(),
                lambda sc: sc.fetchone(),
                None,
                id="fetchone-after-fetchall",
            ),
            pytest.param(
                lambda sc: sc.fetchall(),
                lambda sc: sc.fetchall(),
                [],
                id="fetchall-after-fetchall",
            ),
            pytest.param(
                lambda sc: sc.fetchmany(5),
                lambda sc: sc.fetchall_rows(),
                [],
                id="fetchall_rows-after-short-batch",
            ),
        ],
    )
    def test_a_fetch_after_the_end_is_empty(
        self,
        drain: Callable[[SemolinaCursor], object],
        again: Callable[[SemolinaCursor], object],
        expected: object,
    ) -> None:
        """Each DBAPI fetch, run after the result is drained, returns its empty value."""
        sc = _make_cursor(FIXTURE_DATA)
        drain(sc)
        assert again(sc) == expected

    def test_a_fetchmany_rows_loop_stops_on_the_empty_batch(self) -> None:
        """The DB-API loop reads every row once and ends."""
        sc = _make_cursor(FIXTURE_DATA)
        seen: list[Row] = []
        while batch := sc.fetchmany_rows(2):
            seen.extend(batch)
        assert len(seen) == len(FIXTURE_DATA)


# ---------------------------------------------------------------------------
# TestDuplicateColumnNames: a result Rows cannot represent
# ---------------------------------------------------------------------------


class TestDuplicateColumnNames:
    """A result with two columns of one name cannot become Rows."""

    @pytest.mark.parametrize(
        "read_rows",
        [
            lambda c: c.fetchall_rows(),
            lambda c: c.fetchone_row(),
            lambda c: c.fetchmany_rows(1),
            lambda c: next(iter(c)),
        ],
        ids=["fetchall_rows", "fetchone_row", "fetchmany_rows", "iteration"],
    )
    def test_each_row_method_refuses_duplicate_column_names(
        self, read_rows: Callable[[SemolinaCursor], object]
    ) -> None:
        """
        Every way of reading Rows raises, naming the duplicated column.

        A Row is keyed by column name, so building one would keep the last ``x`` and drop the
        first without a word.
        """
        sc, conn = _make_adbc_cursor(
            create_sql="CREATE TABLE unused (a INTEGER)", select_sql="SELECT 1 AS x, 2 AS x"
        )
        try:
            with pytest.raises(
                SemolinaDuplicateColumnsError, match=r"duplicate column names: \['x'\]"
            ):
                read_rows(sc)
        finally:
            conn.close()

    def test_raw_tuples_still_carry_both_columns(self) -> None:
        """The positional passthrough is unaffected: both values are there."""
        sc, conn = _make_adbc_cursor(
            create_sql="CREATE TABLE unused (a INTEGER)", select_sql="SELECT 1 AS x, 2 AS x"
        )
        try:
            assert sc.fetchall() == [(1, 2)]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TestSemolinaCursorContextManager: lifecycle management
# ---------------------------------------------------------------------------


class TestSemolinaCursorContextManager:
    """Test SemolinaCursor context manager protocol."""

    def test_context_manager_enter_returns_self(self) -> None:
        """__enter__ returns the SemolinaCursor itself."""
        sc = _make_cursor(FIXTURE_DATA)
        with sc as ctx:
            assert ctx is sc

    def test_close_calls_cursor_and_conn_close(self) -> None:
        """close() closes the cursor, then returns the connection to the pool."""
        calls: list[str] = []
        cursor = SimpleNamespace(close=lambda: calls.append("cursor"))
        conn = SimpleNamespace(close=lambda: calls.append("conn"))

        SemolinaCursor(cursor, conn).close()

        assert calls == ["cursor", "conn"]

    def test_close_order_is_reader_cursor_connection(self) -> None:
        """
        close() closes the reader iteration opened, then the cursor, then the connection.

        The same order ``aclose()`` uses. The sync cursor used to leave its reader for the
        driver to tidy up.
        """
        log: list[str] = []
        sc, reader = _logging_cursor(log)

        next(sc)
        sc.close()

        assert log == ["reader", "cursor", "conn"]
        assert reader.closed is True

    def test_close_closes_a_reader_taken_through_fetch_record_batch(self) -> None:
        """A reader handed to the caller is recorded, so close() closes it too."""
        log: list[str] = []
        sc, reader = _logging_cursor(log)

        assert sc.fetch_record_batch() is reader
        sc.close()

        assert log == ["reader", "cursor", "conn"]

    def test_fetch_record_batch_is_idempotent(self) -> None:
        """A second fetch_record_batch() hands back the reader the first one recorded."""
        log: list[str] = []
        sc, _reader = _logging_cursor(log)

        assert sc.fetch_record_batch() is sc.fetch_record_batch()
        sc.close()

    def test_close_returns_the_connection_when_the_cursor_close_raises(self) -> None:
        """A cursor that fails to close does not keep the connection out of the pool."""
        log: list[str] = []
        sc, _reader = _logging_cursor(log, fail_cursor_close=True)

        sc.close()

        assert log == ["cursor", "conn"]
        assert "closed" in repr(sc)

    def test_exit_does_not_mask_the_bodys_exception(self) -> None:
        """
        An error raised in the ``with`` body reaches the caller, not the teardown's error.

        Before, a cursor close that raised during ``__exit__`` replaced the body's exception,
        so the traceback showed the cleanup failure and hid the real one.
        """
        log: list[str] = []
        sc, _reader = _logging_cursor(log, fail_cursor_close=True)

        with pytest.raises(ValueError, match="the body's own error"), sc:
            raise ValueError("the body's own error")

        assert log == ["cursor", "conn"]

    def test_close_is_idempotent(self) -> None:
        """A second close() is a no-op rather than a second teardown."""
        log: list[str] = []
        sc, _reader = _logging_cursor(log)

        next(sc)
        sc.close()
        sc.close()

        assert log == ["reader", "cursor", "conn"]

    def test_close_warns_when_the_connection_cannot_be_returned(self) -> None:
        """A connection close that fails is suppressed, and reported as a ResourceWarning."""
        log: list[str] = []
        sc, _reader = _logging_cursor(log, fail_conn_close=True)

        with pytest.warns(ResourceWarning, match="could not return its pooled connection"):
            sc.close()

        assert "closed" in repr(sc)

    def test_close_does_not_warn_on_a_clean_teardown(self) -> None:
        """A teardown that succeeds emits no ResourceWarning."""
        log: list[str] = []
        sc, _reader = _logging_cursor(log)

        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            sc.close()

    def test_context_manager_closes_on_exit(self) -> None:
        """With statement closes cursor on exit (repr shows closed)."""
        sc = _make_cursor(FIXTURE_DATA)
        with sc:
            assert "open" in repr(sc).lower() or "columns" in repr(sc).lower()
        assert "closed" in repr(sc).lower()

    def test_del_closes_unclosed_connection(self) -> None:
        """__del__ returns a leaked connection to the pool (best-effort)."""

        class _TrackingConn:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        conn = _TrackingConn()
        sc = SemolinaCursor(cursor=object(), conn=conn)
        # Simulate a caller that forgot to close() and use the context manager.
        sc.__del__()
        assert conn.closed is True

    def test_del_does_not_double_close(self) -> None:
        """__del__ is a no-op once the cursor is already closed."""

        class _TrackingConn:
            def __init__(self) -> None:
                self.close_calls = 0

            def close(self) -> None:
                self.close_calls += 1

        class _NoopCursor:
            def close(self) -> None:
                pass

        conn = _TrackingConn()
        sc = SemolinaCursor(cursor=_NoopCursor(), conn=conn)
        sc.close()
        assert conn.close_calls == 1
        sc.__del__()
        assert conn.close_calls == 1  # finalizer did not re-close

    def test_del_never_raises_on_partial_init(self) -> None:
        """__del__ tolerates a partially-initialized instance without raising."""
        sc = SemolinaCursor.__new__(SemolinaCursor)  # __init__ never ran
        sc.__del__()  # must not raise even though _conn/_closed are absent


# ---------------------------------------------------------------------------
# TestSemolinaCursorRepr: string representation
# ---------------------------------------------------------------------------


class TestSemolinaCursorRepr:
    """Test SemolinaCursor __repr__."""

    def test_repr_shows_columns_when_open(self) -> None:
        """Repr shows column names when cursor is open."""
        sc = _make_cursor(FIXTURE_DATA)
        r = repr(sc)
        assert "SemolinaCursor" in r
        assert "revenue" in r
        assert "country" in r

    def test_repr_shows_closed_when_closed(self) -> None:
        """Repr shows closed state after close()."""
        sc = _make_cursor(FIXTURE_DATA)
        sc.close()
        r = repr(sc)
        assert "SemolinaCursor" in r
        assert "closed" in r.lower()


# ---------------------------------------------------------------------------
# TestSemolinaCursorPassthrough: raw DBAPI passthrough methods
# ---------------------------------------------------------------------------


class TestSemolinaCursorPassthrough:
    """Test SemolinaCursor raw DBAPI passthrough methods."""

    def test_fetchall_returns_raw_tuples(self) -> None:
        """fetchall() passthrough returns list[tuple]."""
        sc = _make_cursor(FIXTURE_DATA)
        rows = sc.fetchall()
        assert isinstance(rows, list)
        assert all(isinstance(r, tuple) for r in rows)
        assert len(rows) == 3

    def test_fetchone_returns_raw_tuple(self) -> None:
        """fetchone() passthrough returns tuple."""
        sc = _make_cursor(FIXTURE_DATA)
        row = sc.fetchone()
        assert isinstance(row, tuple)

    def test_fetchmany_returns_raw_tuples(self) -> None:
        """fetchmany(2) passthrough returns list[tuple]."""
        sc = _make_cursor(FIXTURE_DATA)
        rows = sc.fetchmany(2)
        assert isinstance(rows, list)
        assert len(rows) == 2
        assert all(isinstance(r, tuple) for r in rows)


# ---------------------------------------------------------------------------
# TestOneReadPatternPerCursor: a result is read one way
# ---------------------------------------------------------------------------


class _OneColumn(BaseModel):
    """A DTO for the five-row ``x`` result the pattern tests read."""

    x: int


READS: dict[str, Callable[[SemolinaCursor], object]] = {
    "fetchone()": lambda c: c.fetchone(),
    "fetchall()": lambda c: c.fetchall(),
    "fetchone_row()": lambda c: c.fetchone_row(),
    "fetchall_rows()": lambda c: c.fetchall_rows(),
    "for row in cursor": lambda c: next(iter(c)),
    "fetch_record_batch()": lambda c: c.fetch_record_batch(),
    "fetch_arrow_table()": lambda c: c.fetch_arrow_table(),
    "fetch_df()": lambda c: c.fetch_df(),
    "fetch_polars()": lambda c: c.fetch_polars(),
    "into()": lambda c: c.into(_OneColumn),
    "iter_into()": lambda c: next(c.iter_into(_OneColumn)),
}
"""Each way of reading a cursor's result, keyed by how the error message names it."""


def _five_rows() -> tuple[SemolinaCursor, Any]:
    """Return a real DuckDB cursor over ``x`` = 0..4, and its connection for the caller to close."""
    return _make_adbc_cursor(
        create_sql="CREATE TABLE unused (a INTEGER)", select_sql="SELECT * FROM range(5) t(x)"
    )


class TestOneReadPatternPerCursor:
    """
    A cursor's result is one stream, and the first way of reading it is the only way.

    Mixing two used to lose rows without a word: whichever reader went first had already
    pulled a batch into its own buffer, and the second started after it. ``fetchone_row()``
    then ``for row in cursor`` returned one row of five.
    """

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("for row in cursor", "fetchall_rows()"),
            ("fetchone_row()", "for row in cursor"),
            ("fetchone_row()", "fetch_arrow_table()"),
            ("for row in cursor", "fetch_arrow_table()"),
            ("fetch_arrow_table()", "for row in cursor"),
            ("fetch_record_batch()", "for row in cursor"),
            ("for row in cursor", "fetch_record_batch()"),
            ("fetchall()", "into()"),
            ("iter_into()", "fetchone()"),
            ("fetch_df()", "fetch_arrow_table()"),
        ],
    )
    def test_a_second_way_of_reading_is_refused(self, first: str, second: str) -> None:
        """The second read raises, naming both calls, and the first can carry on."""
        pytest.importorskip("pandas")
        sc, conn = _five_rows()
        try:
            READS[first](sc)
            with pytest.raises(SemolinaResultConsumedError) as excinfo:
                READS[second](sc)
        finally:
            conn.close()

        message = str(excinfo.value)
        assert first in message
        assert second in message

    def test_the_dbapi_fetches_share_one_read(self) -> None:
        """
        ``fetchone``, ``fetchmany`` and ``fetchall``, raw or as Rows, interleave freely.

        They all read through the driver's one DBAPI row iterator, so each picks up where the
        last stopped, as DBAPI specifies.
        """
        sc, conn = _five_rows()
        try:
            assert sc.fetchone_row() == Row({"x": 0})
            assert sc.fetchmany(2) == [(1,), (2,)]
            assert sc.fetchall_rows() == [Row({"x": 3}), Row({"x": 4})]
        finally:
            conn.close()

    def test_repeating_a_read_carries_on_from_where_it_stopped(self) -> None:
        """Iterating again after a ``break`` resumes; it is the same read, not a second one."""
        sc, conn = _five_rows()
        try:
            for _row in sc:
                break
            assert [row.x for row in sc] == [1, 2, 3, 4]
        finally:
            conn.close()

    def test_description_and_repr_do_not_count_as_reads(self) -> None:
        """Looking at the columns first leaves every way of reading open."""
        sc, conn = _five_rows()
        try:
            assert sc.description is not None
            assert "x" in repr(sc)
            assert sc.fetch_polars().height == 5
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TestFetchArrowTable: ADBC Arrow passthrough (DuckDB in-process)
# ---------------------------------------------------------------------------


class TestFetchArrowTable:
    """Test fetch_arrow_table() returns pyarrow.Table via ADBC delegation."""

    def test_fetch_arrow_table_column_values(self) -> None:
        """fetch_arrow_table() returns correct column values."""
        pytest.importorskip("pyarrow")

        sc, conn = _make_adbc_cursor(
            create_sql="CREATE TABLE t (id INTEGER, name VARCHAR)",
            insert_sql="INSERT INTO t VALUES (1, 'alice'), (2, 'bob')",
            select_sql="SELECT * FROM t ORDER BY id",
        )
        try:
            table = sc.fetch_arrow_table()
            assert table.column("id").to_pylist() == [1, 2]
            assert table.column("name").to_pylist() == ["alice", "bob"]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TestFetchRecordBatch: ADBC RecordBatchReader passthrough
# ---------------------------------------------------------------------------


class TestFetchRecordBatch:
    """Test fetch_record_batch() returns pyarrow.RecordBatchReader via ADBC delegation."""

    def test_returns_record_batch_reader(self) -> None:
        """fetch_record_batch() returns a pyarrow.RecordBatchReader instance."""
        pyarrow = pytest.importorskip("pyarrow")

        sc, conn = _make_adbc_cursor(
            create_sql="CREATE TABLE t (id INTEGER, name VARCHAR)",
            insert_sql="INSERT INTO t VALUES (1, 'alice'), (2, 'bob')",
            select_sql="SELECT * FROM t",
        )
        try:
            reader = sc.fetch_record_batch()
            assert isinstance(reader, pyarrow.RecordBatchReader)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TestStreamingIteration: __iter__/__next__ over RecordBatchReader
# ---------------------------------------------------------------------------


class TestStreamingIteration:
    """Test SemolinaCursor.__iter__/__next__ lazy row streaming semantics."""

    def test_iter_returns_self(self) -> None:
        """iter(sc) is sc — SemolinaCursor is its own iterator."""
        pa = pytest.importorskip("pyarrow")

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        reader = _CountingReader(schema, iter([]))
        fake_cursor = SimpleNamespace(
            fetch_record_batch=lambda: reader,
            description=[("revenue", None), ("country", None)],
        )
        fake_conn = SimpleNamespace(close=lambda: None)
        sc = SemolinaCursor(fake_cursor, fake_conn)
        assert iter(sc) is sc

    def test_yields_row_objects(self) -> None:
        """
        Iterating a SemolinaCursor over real ADBC data yields Row objects.

        Verifies attribute access works on the resulting Rows.
        """
        pytest.importorskip("pyarrow")

        sc, conn = _make_adbc_cursor(
            create_sql="CREATE TABLE t (id INTEGER, name VARCHAR)",
            insert_sql="INSERT INTO t VALUES (1, 'alice'), (2, 'bob'), (3, 'carol')",
            select_sql="SELECT id, name FROM t ORDER BY id",
        )
        try:
            rows = list(sc)
            assert len(rows) == 3
            assert all(isinstance(r, Row) for r in rows)
            assert rows[0].id == 1
            assert rows[0].name == "alice"
            assert rows[1].id == 2
            assert rows[2].name == "carol"
        finally:
            conn.close()

    def test_multiple_batches(self) -> None:
        """Iteration yields rows from multiple non-empty batches in order."""
        pa = pytest.importorskip("pyarrow")

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        batches = [
            pa.RecordBatch.from_pydict({"revenue": [1, 2], "country": ["US", "CA"]}, schema=schema),
            pa.RecordBatch.from_pydict({"revenue": [3, 4], "country": ["MX", "FR"]}, schema=schema),
            pa.RecordBatch.from_pydict({"revenue": [5, 6], "country": ["DE", "JP"]}, schema=schema),
        ]
        reader = _CountingReader(schema, iter(batches))
        fake_cursor = SimpleNamespace(
            fetch_record_batch=lambda: reader,
            description=[("revenue", None), ("country", None)],
        )
        fake_conn = SimpleNamespace(close=lambda: None)
        sc = SemolinaCursor(fake_cursor, fake_conn)

        rows = list(sc)
        assert len(rows) == 6
        assert [r.revenue for r in rows] == [1, 2, 3, 4, 5, 6]
        assert [r.country for r in rows] == ["US", "CA", "MX", "FR", "DE", "JP"]

    def test_lazy_batch_pull(self) -> None:
        """Iteration pulls batches lazily — partial consumption pulls only the needed batches."""
        pa = pytest.importorskip("pyarrow")

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        batches = [
            pa.RecordBatch.from_pydict({"revenue": [1, 2], "country": ["US", "CA"]}, schema=schema),
            pa.RecordBatch.from_pydict({"revenue": [3, 4], "country": ["MX", "FR"]}, schema=schema),
            pa.RecordBatch.from_pydict({"revenue": [5, 6], "country": ["DE", "JP"]}, schema=schema),
        ]
        reader = _CountingReader(schema, iter(batches))
        fake_cursor = SimpleNamespace(
            fetch_record_batch=lambda: reader,
            description=[("revenue", None), ("country", None)],
        )
        fake_conn = SimpleNamespace(close=lambda: None)
        sc = SemolinaCursor(fake_cursor, fake_conn)

        it = iter(sc)

        # First row forces the first batch to be pulled.
        first = next(it)
        assert first.revenue == 1
        assert reader.read_count == 1

        # Second row comes from the same batch — no new pull.
        second = next(it)
        assert second.revenue == 2
        assert reader.read_count == 1

        # Third row exhausts batch 1 → pulls batch 2.
        third = next(it)
        assert third.revenue == 3
        assert reader.read_count == 2

        # Abandon iteration — batch 3 must NOT have been pulled.
        assert reader.read_count == 2

    def test_skips_empty_batches(self) -> None:
        """Zero-row batches mid-stream are skipped without terminating iteration."""
        pa = pytest.importorskip("pyarrow")

        schema = pa.schema([("revenue", pa.int64()), ("country", pa.string())])
        empty_batch = pa.RecordBatch.from_pydict({"revenue": [], "country": []}, schema=schema)
        non_empty_1 = pa.RecordBatch.from_pydict(
            {"revenue": [10, 20], "country": ["US", "CA"]}, schema=schema
        )
        non_empty_2 = pa.RecordBatch.from_pydict(
            {"revenue": [30, 40], "country": ["MX", "FR"]}, schema=schema
        )
        reader = _CountingReader(schema, iter([empty_batch, non_empty_1, empty_batch, non_empty_2]))
        fake_cursor = SimpleNamespace(
            fetch_record_batch=lambda: reader,
            description=[("revenue", None), ("country", None)],
        )
        fake_conn = SimpleNamespace(close=lambda: None)
        sc = SemolinaCursor(fake_cursor, fake_conn)

        rows = list(sc)
        assert len(rows) == 4
        assert [r.revenue for r in rows] == [10, 20, 30, 40]
        assert [r.country for r in rows] == ["US", "CA", "MX", "FR"]

    def test_normalises_a_drained_reader_creation_error(self) -> None:
        """
        A driver that reports a drained result when the reader is created stops iteration.

        Some ADBC drivers raise ``OSError`` there instead of ending the stream. A ``for`` loop
        should end, as over any exhausted iterator, not surface the driver's error.
        """

        def fetch_record_batch() -> Any:
            raise OSError("Attempting to execute an unsuccessful or closed query result")

        fake_cursor = SimpleNamespace(
            fetch_record_batch=fetch_record_batch, description=[("revenue", None)]
        )
        sc = SemolinaCursor(fake_cursor, SimpleNamespace(close=lambda: None))

        assert list(sc) == []

    def test_normalises_a_drained_reader_pull_error(self) -> None:
        """A driver that reports the drain on a batch pull instead also stops iteration."""
        pa = pytest.importorskip("pyarrow")

        def drained() -> Any:
            raise OSError("Attempting to execute an unsuccessful or closed query result")
            yield  # unreachable; it makes this a generator, so the raise comes on the first pull

        reader = _CountingReader(pa.schema([("revenue", pa.int64())]), drained())
        fake_cursor = SimpleNamespace(
            fetch_record_batch=lambda: reader, description=[("revenue", None)]
        )
        sc = SemolinaCursor(fake_cursor, SimpleNamespace(close=lambda: None))

        assert list(sc) == []
        assert list(sc) == []

    def test_reiteration_yields_nothing(self) -> None:
        """Re-iterating an exhausted cursor yields zero rows (no raise)."""
        pytest.importorskip("pyarrow")

        sc, conn = _make_adbc_cursor(
            create_sql="CREATE TABLE t (id INTEGER, name VARCHAR)",
            insert_sql="INSERT INTO t VALUES (1, 'alice'), (2, 'bob'), (3, 'carol')",
            select_sql="SELECT id, name FROM t ORDER BY id",
        )
        try:
            first_pass = list(sc)
            assert len(first_pass) == 3
            second_pass = list(sc)
            assert second_pass == []
        finally:
            conn.close()

    def test_does_not_auto_close(self) -> None:
        """Iteration to exhaustion does NOT close the cursor."""
        pytest.importorskip("pyarrow")

        sc, conn = _make_adbc_cursor(
            create_sql="CREATE TABLE t (id INTEGER, name VARCHAR)",
            insert_sql="INSERT INTO t VALUES (1, 'alice'), (2, 'bob'), (3, 'carol')",
            select_sql="SELECT id, name FROM t ORDER BY id",
        )
        try:
            rows = list(sc)
            assert len(rows) == 3
            assert sc._closed is False
            assert "closed" not in repr(sc).lower()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Dataframe returns, and the optional-dependency guards
# ---------------------------------------------------------------------------


@pytest.fixture
def probe_engine() -> Generator[Engine, None, None]:
    """
    Yield the probe's own in-memory DuckDB engine, closing its pool on teardown.

    Restated from ``tests/unit/test_dto_duckdb.py`` rather than forked: same engine, same
    register-free contract, same teardown. The dataframe methods make a claim about what
    comes back from a real semantic-view result, and ``_make_adbc_cursor`` above selects
    from a plain table.
    """
    from adbc_poolhouse import close_pool

    engine = make_probe_engine()
    yield engine
    close_pool(engine._pool)


def _probe_cursor(engine: Engine) -> SemolinaCursor:
    """
    Execute the region-by-decimal-metric query and return its open cursor.

    A fresh cursor per call, deliberately. ``fetch_polars()`` must be the first consuming call
    on a cursor, so a shared fixture would make one of these tests fail for a reason that has
    nothing to do with the return type it is asserting.

    Args:
        engine: The probe engine.

    Returns:
        An open :class:`~semolina.cursor.SemolinaCursor`. The caller closes it.
    """
    query = (
        TypeFidelityView.query()
        .metrics(TypeFidelityView.total_order_value)
        .dimensions(TypeFidelityView.region)
    )
    return engine.execute(query)


def _find_spec_without(missing: str) -> Callable[..., Any]:
    """
    Build a ``find_spec`` replacement that reports exactly one package absent.

    A blanket ``return_value=None`` would make the *pyarrow* guard fire first inside
    ``fetch_df``, so a test written that way would assert the wrong error's message and still
    pass. Same helper as ``tests/unit/test_dto.py``'s, restated so this module stays
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


class TestFetchDf:
    """fetch_df() returns a real pandas DataFrame from the live driver path."""

    def test_returns_a_pandas_dataframe(self, probe_engine: Engine) -> None:
        """
        fetch_df() returns a ``pandas.DataFrame`` carrying the query's rows.

        Asserted by ``isinstance`` against the class imported here, not against a name in the
        annotation: the annotation is what this test exists to check, so trusting it would
        prove only that Semolina agrees with itself.
        """
        pandas = pytest.importorskip("pandas")
        pytest.importorskip("pyarrow")

        with _probe_cursor(probe_engine) as cursor:
            frame = cursor.fetch_df()

        assert isinstance(frame, pandas.DataFrame)
        assert "region" in frame.columns
        assert set(frame["region"]) == {"US", "MX", "CA"}


class TestFetchPolars:
    """fetch_polars() returns a polars DataFrame, and owns the stream to do it."""

    def test_returns_a_polars_dataframe(self, probe_engine: Engine) -> None:
        """
        fetch_polars() returns a ``polars.DataFrame``, called first on a fresh cursor.

        First consuming call, deliberately: ADBC's implementation takes the cursor's Arrow
        stream handle, so anything consumed before this would leave it nothing.
        """
        polars = pytest.importorskip("polars")

        with _probe_cursor(probe_engine) as cursor:
            frame = cursor.fetch_polars()

        assert isinstance(frame, polars.DataFrame)
        assert "region" in frame.columns
        assert set(frame.get_column("region")) == {"US", "MX", "CA"}

    def test_returns_a_frame_with_one_column_per_result_column_on_polars_2(
        self, probe_engine: Engine
    ) -> None:
        """
        Under polars 2, fetch_polars() returns a frame of the result's columns, not a struct.

        polars 2 made ``polars.from_arrow`` on an Arrow stream return a one-column struct
        ``Series``, and adbc-driver-manager before 1.13 returned that ``Series`` as it was
        (apache/arrow-adbc#4915). This cursor delegates straight to the driver, so on
        adbc-driver-manager 1.12 or older it handed back ``Series 'duckdb_query_result'
        [struct[2]]``. The ``adbc-poolhouse>=1.8.1`` floor brings adbc-driver-manager 1.13,
        which returns the frame.

        Needs polars 2 itself, not any polars. The lock resolves polars 2, so if it ever
        slips back to polars 1 this test skips, and a skip fails CI (``tests/ci_skips.py``),
        rather than passing on a polars that cannot show the bug. The rows are compared in
        full, with the metric's ``Decimal`` dtype, so a frame built from the struct's fields
        under other names or types would fail too.
        """
        polars = pytest.importorskip("polars", minversion="2.0.0")
        from decimal import Decimal

        with _probe_cursor(probe_engine) as cursor:
            frame = cursor.fetch_polars()

        assert isinstance(frame, polars.DataFrame), type(frame)
        assert sorted(frame.columns) == ["region", "total_order_value"]
        assert frame.schema["total_order_value"] == polars.Decimal(precision=38, scale=2)
        assert frame.sort("region").select("region", "total_order_value").rows() == [
            ("CA", None),
            ("MX", Decimal("100.00")),
            ("US", Decimal("43.25")),
        ]

    def test_after_fetch_record_batch_raises_a_semolina_error_naming_both(
        self, probe_engine: Engine
    ) -> None:
        """
        fetch_polars() after fetch_record_batch() is refused by Semolina, naming both calls.

        It used to reach the driver, whose "Result set has been closed or consumed" says
        something was consumed but not what, or what to do instead.
        """
        pytest.importorskip("polars")
        pytest.importorskip("pyarrow")

        with _probe_cursor(probe_engine) as cursor:
            cursor.fetch_record_batch()
            with pytest.raises(SemolinaResultConsumedError) as excinfo:
                cursor.fetch_polars()

        assert "fetch_record_batch()" in str(excinfo.value)
        assert "fetch_polars()" in str(excinfo.value)


class TestMissingDependencyGuards:
    """
    Every Arrow/dataframe method names its own missing package and install command.

    The guard set per method is derived from what ADBC's implementation actually imports, read
    at ``adbc_driver_manager/dbapi.py``, not from symmetry between siblings. ``fetch_polars``
    is the case that breaks the symmetry, and the last test here is what stops a future
    "let's make these consistent" tidy-up from silently breaking a working call.
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
    def test_each_method_names_its_own_extra(
        self, method_name: str, missing: str, extra: str
    ) -> None:
        """
        The raised message names the absent package AND the exact install command.

        A message that names the package but not the command is precisely the failure these
        guards exist to fix, so both halves are asserted. The literal
        ``pip install semolina[<extra>]`` string is checked per method, so a copy-paste error
        giving every method the same extra fails here rather than shipping.
        """
        sc = SemolinaCursor(object(), object())

        with (
            patch("importlib.util.find_spec", side_effect=_find_spec_without(missing)),
            pytest.raises(SemolinaMissingDependencyError) as excinfo,
        ):
            getattr(sc, method_name)()

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
            lambda c: next(iter(c)),
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
    def test_every_row_method_names_the_pyarrow_extra(
        self, read: Callable[[SemolinaCursor], object]
    ) -> None:
        """
        Reading rows without pyarrow raises Semolina's error, naming ``semolina[pyarrow]``.

        ADBC reads every row through a pyarrow reader, and even ``description`` goes through
        it, so a pyarrow-less install used to fail with the driver's own error. The async
        cursor's iteration was guarded; none of the sync row methods were.
        """
        sc = SemolinaCursor(object(), object())

        with (
            patch("importlib.util.find_spec", side_effect=_find_spec_without("pyarrow")),
            pytest.raises(SemolinaMissingDependencyError, match=r"pip install semolina\[pyarrow\]"),
        ):
            read(sc)

    def test_fetch_df_reports_pyarrow_before_pandas(self) -> None:
        """
        With pyarrow absent, fetch_df() says pyarrow — because ADBC gets there first.

        ADBC's ``fetch_df`` is ``self.reader.read_pandas()``, and the ``reader`` property calls
        ``_requires_pyarrow()`` before anything imports pandas. Guarding pandas first would let
        ADBC's own ``ProgrammingError`` win on a pyarrow-less install, which names neither
        Semolina nor the extra.
        """
        sc = SemolinaCursor(object(), object())

        with (
            patch("importlib.util.find_spec", side_effect=_find_spec_without("pyarrow")),
            pytest.raises(SemolinaMissingDependencyError) as excinfo,
        ):
            sc.fetch_df()

        assert "pip install semolina[pyarrow]" in str(excinfo.value)

    def test_fetch_polars_does_not_require_pyarrow(self) -> None:
        """
        With pyarrow absent and polars present, fetch_polars() still delegates.

        This asymmetry is deliberate. ADBC hands polars the raw PyCapsule stream —
        ``polars.from_arrow(self.fetch_arrow())`` — and builds no reader, so pyarrow is never
        reached. A pyarrow guard here would refuse a call that works.
        """
        sentinel = object()
        inner = SimpleNamespace(fetch_polars=lambda: sentinel)
        sc = SemolinaCursor(inner, object())

        with patch("importlib.util.find_spec", side_effect=_find_spec_without("pyarrow")):
            assert sc.fetch_polars() is sentinel
