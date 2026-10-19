"""
Tests for the engine's DuckDB pool: extension loading, and connections coming back.

Test classes:
- TestExtensionLoading: INSTALL + LOAD via connect event
- TestInMemorySharedDatabase: pooled :memory: connections share one database
- TestExecuteWithPool: end-to-end execute() via pool registry
- TestExecuteErrorPathReleasesConnection: a failed execute still returns its connection
- TestEngineDispose: dispose() closes the pool
"""

from __future__ import annotations

from typing import Any

import pytest
from models import Sales

pytest.importorskip("adbc_driver_duckdb")


# ---------------------------------------------------------------------------
# TestExtensionLoading: INSTALL + LOAD via connect event
# ---------------------------------------------------------------------------


class TestExtensionLoading:
    """Test DuckDB semantic_views extension auto-loading."""

    def test_extension_installed_and_loaded(self, duckdb_pool: Any):
        """semantic_views extension is installed and loaded after pool connect."""
        with duckdb_pool.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT installed, loaded FROM duckdb_extensions()"
                " WHERE extension_name = 'semantic_views'"
            )
            row = cur.fetchone()
            assert row is not None, "semantic_views extension not found"
            installed, loaded = row
            assert installed, "semantic_views extension not installed"
            assert loaded, "semantic_views extension not loaded"
            cur.close()

    def test_extension_auto_loads_on_new_connection(self):
        """Fresh DuckDB pool with event listener loads extension automatically."""
        from adbc_poolhouse import DuckDBConfig, close_pool, create_pool
        from sqlalchemy import event

        from semolina.config import _load_semantic_views

        config = DuckDBConfig(database=":memory:", pool_size=1)
        pool = create_pool(config)
        event.listen(pool, "connect", _load_semantic_views)

        try:
            with pool.connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT installed, loaded FROM duckdb_extensions()"
                    " WHERE extension_name = 'semantic_views'"
                )
                row = cur.fetchone()
                assert row is not None, "semantic_views extension not found"
                installed, loaded = row
                assert installed
                assert loaded
                cur.close()
        finally:
            close_pool(pool)


# ---------------------------------------------------------------------------
# TestInMemorySharedDatabase: pooled :memory: connections share one database
# ---------------------------------------------------------------------------


class TestInMemorySharedDatabase:
    """Every pooled ``:memory:`` connection opens the database the pool's source holds."""

    def test_new_connection_sees_committed_data_and_seed_runs_once(self, duckdb_pool: Any):
        """
        A second physical connection sees a row the first committed.

        ``duckdb_pool`` holds one connection, so checking out a second while the first
        is held opens an overflow connection. If connections had separate databases the
        table would be missing; if the seed listener ran on every connection, its
        ``DELETE`` would wipe the extra row and the count would be back to 3.
        """
        first = duckdb_pool.connect()
        try:
            cur = first.cursor()
            cur.execute("INSERT INTO sales_data VALUES (4, 10, 1, 'FR', 'East', 1)")
            cur.close()
            first.commit()

            second = duckdb_pool.connect()
            try:
                assert second.dbapi_connection is not first.dbapi_connection
                cur = second.cursor()
                cur.execute("SELECT count(*) FROM sales_data")
                assert cur.fetchone() == (4,)
                cur.close()
            finally:
                second.close()
        finally:
            first.close()


# ---------------------------------------------------------------------------
# TestExecuteWithPool: end-to-end execute() via pool registry
# ---------------------------------------------------------------------------


class TestExecuteWithPool:
    """Test Query.execute() wired through the pool registry path."""

    def test_execute_with_duckdb_pool_returns_cursor(self, duckdb_pool: Any):
        """Register DuckDB pool, execute query, get SemolinaCursor with Rows."""
        from semolina.cursor import SemolinaCursor

        cursor = Sales.query().metrics(Sales.revenue).dimensions(Sales.country).execute()
        assert isinstance(cursor, SemolinaCursor)
        rows = cursor.fetchall_rows()
        # DuckDB aggregates: 2 rows (US, CA)
        assert len(rows) == 2
        cursor.close()

    def test_execute_with_named_pool_using(self):
        """Build a DuckDB Engine, register it by name, .using('test') resolves it."""
        from adbc_poolhouse import DuckDBConfig, close_pool

        import semolina
        from semolina.config import create_engine
        from semolina.cursor import SemolinaCursor

        engine = create_engine(DuckDBConfig(database=":memory:", pool_size=1))

        with engine.connect() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE sales_data (
                    id INTEGER, revenue INTEGER, cost INTEGER,
                    country VARCHAR, region VARCHAR, unit_price INTEGER
                )
            """)
            cur.execute("""
                INSERT INTO sales_data VALUES
                (1, 42, 10, 'CA', 'West', 5)
            """)
            cur.execute("""
                CREATE OR REPLACE SEMANTIC VIEW sales_view AS
                TABLES (s AS sales_data PRIMARY KEY (id))
                DIMENSIONS (s.country AS country, s.region AS region)
                METRICS (s.revenue AS SUM(s.revenue), s.cost AS SUM(s.cost))
            """)
            cur.close()
            conn.commit()

        semolina.register("test", engine)
        try:
            cursor = (
                Sales.query()
                .metrics(Sales.revenue)
                .dimensions(Sales.country)
                .using("test")
                .execute()
            )
            assert isinstance(cursor, SemolinaCursor)
            rows = cursor.fetchall_rows()
            assert len(rows) == 1
            assert int(rows[0].revenue) == 42
            cursor.close()
        finally:
            semolina.unregister("test")
            close_pool(engine._pool)

    def test_execute_returns_aggregated_data(self, duckdb_pool: Any):
        """DuckDB actually aggregates metrics (SUM) and groups by dimensions."""
        cursor = Sales.query().metrics(Sales.revenue).dimensions(Sales.country).execute()
        rows = cursor.fetchall_rows()

        # DuckDB aggregates: US (1000+500=1500), CA (2000)
        assert len(rows) == 2
        revenues = {r.country: int(r.revenue) for r in rows}
        assert revenues["US"] == 1500
        assert revenues["CA"] == 2000
        cursor.close()

    def test_execute_with_no_engine_registered_raises(self):
        """execute() raises ValueError when no engine is registered."""
        query = Sales.query().metrics(Sales.revenue).dimensions(Sales.country)
        with pytest.raises(ValueError, match="No engine registered"):
            query.execute()

    def test_execute_cursor_lifecycle(self, duckdb_pool: Any):
        """The cursor holds a pooled connection until close() returns it."""
        cursor = Sales.query().metrics(Sales.revenue).execute()
        assert duckdb_pool.checkedout() == 1

        cursor.close()

        assert duckdb_pool.checkedout() == 0


# ---------------------------------------------------------------------------
# TestExecuteErrorPathReleasesConnection: a failed execute must not leak its connection
# ---------------------------------------------------------------------------


class _RaisingCursor:
    """DBAPI-shaped cursor whose execute() always raises (simulates a SQL error)."""

    def execute(self, sql: Any, params: Any = None) -> None:
        """Raise to simulate a backend execution failure (bad SQL, expired session)."""
        raise RuntimeError("boom from cursor.execute")


class _CursorRaisingConn:
    """Connection whose cursor() raises (simulates failure before execute())."""

    def __init__(self) -> None:
        """Track whether close() (pool checkin) was called."""
        self.closed = False

    def cursor(self) -> Any:
        """Raise to simulate failure at the conn.cursor() step."""
        raise RuntimeError("boom from conn.cursor")

    def close(self) -> None:
        """Mark the connection as returned to the pool (mirrors SemolinaCursor.close)."""
        self.closed = True


class _ExecuteRaisingConn:
    """Connection that hands out a cursor whose execute() raises."""

    def __init__(self) -> None:
        """Track whether close() (pool checkin) was called."""
        self.closed = False

    def cursor(self) -> _RaisingCursor:
        """Return a cursor that raises on execute()."""
        return _RaisingCursor()

    def close(self) -> None:
        """Mark the connection as returned to the pool (mirrors SemolinaCursor.close)."""
        self.closed = True


class TestExecuteErrorPathReleasesConnection:
    """
    Engine.execute() must return the pooled connection on the error path.

    The connection checked out by ``Engine.connect()`` is otherwise only returned
    via ``SemolinaCursor.close()`` -> ``self._conn.close()``, which is unreachable
    when ``conn.cursor()`` or ``cur.execute()`` raises. With ``pool_size=1`` a
    single failed query would permanently consume the only slot. These tests patch
    ``connect()`` to yield a tracking connection and assert ``close()`` is called.
    """

    def _engine(self, monkeypatch: pytest.MonkeyPatch, conn: Any) -> Any:
        """Build a real DuckDB Engine but patch connect() to yield the given conn."""
        from adbc_poolhouse import DuckDBConfig

        from semolina.config import create_engine

        engine = create_engine(DuckDBConfig(database=":memory:", pool_size=1))
        monkeypatch.setattr(engine, "connect", lambda: conn)
        return engine

    def test_connection_returned_when_cursor_execute_raises(self, monkeypatch: pytest.MonkeyPatch):
        """If cur.execute() raises, the connection is returned to the pool."""
        from adbc_poolhouse import close_pool

        conn = _ExecuteRaisingConn()
        engine = self._engine(monkeypatch, conn)
        query = Sales.query().metrics(Sales.revenue).dimensions(Sales.country)
        try:
            with pytest.raises(RuntimeError, match="boom from cursor.execute"):
                engine.execute(query)
            assert conn.closed, "connection was not returned to the pool on execute() failure"
        finally:
            close_pool(engine._pool)

    def test_connection_returned_when_conn_cursor_raises(self, monkeypatch: pytest.MonkeyPatch):
        """If conn.cursor() raises, the connection is returned to the pool."""
        from adbc_poolhouse import close_pool

        conn = _CursorRaisingConn()
        engine = self._engine(monkeypatch, conn)
        query = Sales.query().metrics(Sales.revenue).dimensions(Sales.country)
        try:
            with pytest.raises(RuntimeError, match="boom from conn.cursor"):
                engine.execute(query)
            assert conn.closed, "connection was not returned to the pool on cursor() failure"
        finally:
            close_pool(engine._pool)


class TestEngineDispose:
    """Engine.dispose() is the public pool-teardown entry point."""

    def test_dispose_uses_close_pool_for_adbc_pools(self):
        """dispose() routes an ADBC-backed pool through adbc_poolhouse.close_pool."""
        from unittest.mock import MagicMock, patch

        from semolina.engines.duckdb import DuckDBEngine
        from semolina.engines.sql import DuckDBDialect

        pool = MagicMock()
        pool._adbc_source = MagicMock()  # mark as an ADBC pool
        engine = DuckDBEngine(pool=pool, dialect=DuckDBDialect())

        with patch("adbc_poolhouse.close_pool") as mock_close_pool:
            engine.dispose()
            mock_close_pool.assert_called_once_with(pool)
            pool.close.assert_not_called()

    def test_dispose_disposes_a_real_pool(self):
        """dispose() closes the connections a real DuckDB engine's pool is holding."""
        from adbc_poolhouse import DuckDBConfig

        from semolina.config import create_engine

        engine = create_engine(DuckDBConfig(database=":memory:", pool_size=1))
        # Prime the pool so there is a pooled connection for dispose() to close.
        engine.connect().close()
        assert engine._pool.checkedin() == 1

        engine.dispose()

        assert engine._pool.checkedin() == 0
