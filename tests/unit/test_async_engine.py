"""
Tests for AsyncEngine query execution over a real in-memory DuckDB async pool.

Tests cover:
- ``await engine.aexecute(query)`` returns the same result surface as
  ``.execute()``, with awaited fetches and ``async for`` row streaming.

Every test in this module runs twice, once under asyncio and once under Trio,
via the shared ``anyio_backend`` fixture in ``tests/conftest.py``.

Test classes:
- TestAsyncExecute: end-to-end execution and connection checkin
"""
# Test-only: the async tests reach the owned async pool's inner sync pool via
# engine._pool._pool to assert checkin. Scope-disable the private-access rule
# (intentionally not a `# type: ignore`).
# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from semolina import Metric, SemanticView
from semolina.acursor import AsyncSemolinaCursor
from semolina.results import Row

if TYPE_CHECKING:
    from semolina.query import Query

pytestmark = pytest.mark.anyio


LOOP_BLOCK_TIMEOUT = 2.0
"""
Seconds the gated driver call waits for the sibling task before giving up.

Only reached when the call is blocking the loop, which is the failure being tested for; a
passing run never waits on it.
"""


class MissingSales(SemanticView, view="no_such_view"):
    """A view the DuckDB fixture does not define, so its query fails in the driver."""

    total = Metric()


class TestAsyncExecute:
    """Test AsyncEngine.aexecute() end to end against real DuckDB."""

    async def test_aexecute_streams_rows_and_returns_connection(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """Executing a query yields Row objects and checks the connection back in."""
        rows: list[Row] = []
        cursor = await async_duckdb_engine.aexecute(sales_query)
        assert isinstance(cursor, AsyncSemolinaCursor)
        async with cursor as cur:
            async for row in cur:
                rows.append(row)

        assert len(rows) == 2
        assert all(isinstance(row, Row) for row in rows)
        assert all(set(row.keys()) == {"revenue", "country"} for row in rows)
        by_country = {row["country"]: row["revenue"] for row in rows}
        assert by_country == {"US": 1500, "CA": 2000}

        # The checked-out slot is returned to the pool by the cursor's close.
        assert async_duckdb_engine._pool._pool.checkedout() == 0

    async def test_failed_execute_returns_the_slot(self, async_duckdb_engine: Any) -> None:
        """
        A statement the driver rejects still checks its connection back in.

        On the success path the check-in belongs to the cursor's ``aclose()``,
        and a failed ``execute()`` never produces a cursor — so ``aexecute``'s
        own error handler is the only thing that can return the slot. Nothing
        else in the suite drives that handler deterministically: the
        cancellation tests either cancel before a connection is checked out or
        depend on a multi-second measured query.
        """
        inner_pool = async_duckdb_engine._pool._pool

        with pytest.raises(Exception, match="no_such_view"):
            await async_duckdb_engine.aexecute(MissingSales.query().metrics(MissingSales.total))

        assert inner_pool.checkedout() == 0
        assert inner_pool.checkedin() == 1


class TestAsyncConcurrency:
    """
    Test that the event loop stays free and two queries run at once.

    These tests import anyio directly. The TID251 Posture A ban scopes to
    ``src/semolina/`` only, and the Trio half of the matrix needs it.
    """

    async def test_concurrency_loop_stays_free_during_query(
        self,
        sales_query: Query,
        async_duckdb_file_engine: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A sibling task runs while the driver's ``execute`` is still in flight.

        The driver call is made to wait until the sibling has run. Offloaded to a worker
        thread, the loop is free, the sibling runs and releases it at once. Run on the loop
        thread, the sibling cannot run at all, the wait times out, and the test fails.

        Only the window inside the driver call counts. Counting scheduling points anywhere
        in the query, as this test used to, also counted the pool checkout and the row
        fetches, so it passed with the driver call blocking the loop.
        """
        import threading

        import adbc_driver_manager.dbapi as dbapi
        import anyio

        in_driver = threading.Event()
        sibling_ran = threading.Event()
        released_by_sibling: list[bool] = []
        real_execute = dbapi.Cursor.execute

        def gated_execute(cursor: Any, operation: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(operation, str) and "semantic_view(" in operation:
                in_driver.set()
                released_by_sibling.append(sibling_ran.wait(timeout=LOOP_BLOCK_TIMEOUT))
            return real_execute(cursor, operation, *args, **kwargs)

        monkeypatch.setattr(dbapi.Cursor, "execute", gated_execute)

        async def sibling() -> None:
            while not in_driver.is_set():
                await anyio.sleep(0)
            sibling_ran.set()

        rows: list[Row] = []

        async def run_query() -> None:
            async with await async_duckdb_file_engine.aexecute(sales_query) as cur:
                rows.extend([row async for row in cur])

        with anyio.fail_after(LOOP_BLOCK_TIMEOUT * 4):
            async with anyio.create_task_group() as tg:
                tg.start_soon(sibling)
                tg.start_soon(run_query)

        assert rows, "the query must actually have returned rows"
        assert released_by_sibling == [True], (
            "the driver call waited out its timeout: the sibling task never ran while it "
            "was in flight, so the call blocked the event loop"
        )

    async def test_concurrency_two_queries_share_the_pool(
        self, sales_query: Query, async_duckdb_file_engine: Any
    ) -> None:
        """
        Two aexecute calls in one task group both return rows, and every slot returns.

        The pool-size assertion is what makes the two-connection claim
        meaningful rather than two serialized checkouts of a single slot.
        """
        import anyio

        inner_pool = async_duckdb_file_engine._pool._pool
        assert inner_pool.size() > 1

        results: list[list[Row]] = []

        async def run_query() -> None:
            async with await async_duckdb_file_engine.aexecute(sales_query) as cur:
                results.append([row async for row in cur])

        async with anyio.create_task_group() as tg:
            tg.start_soon(run_query)
            tg.start_soon(run_query)

        assert len(results) == 2
        for rows in results:
            assert {row["country"]: row["revenue"] for row in rows} == {"US": 1000, "CA": 2000}

        assert inner_pool.checkedout() == 0

    async def test_in_memory_pool_connections_share_one_database(
        self, sales_query: Query, async_duckdb_engine_default_pool: Any
    ) -> None:
        """
        A default in-memory pool holds more than one connection, all over the seeded data.

        The first cursor stays open while the second query runs, so the second must
        check out a different physical connection. It still sees the rows the
        ``once=True`` listener wrote through the first, which it could not if each
        pooled connection opened its own in-memory database.
        """
        engine = async_duckdb_engine_default_pool
        inner_pool = engine._pool._pool
        assert inner_pool.size() == 5

        expected = {"US": 1500, "CA": 2000}
        async with await engine.aexecute(sales_query) as first:
            async with await engine.aexecute(sales_query) as second:
                assert inner_pool.checkedout() == 2
                second_rows = {row["country"]: row["revenue"] async for row in second}
            first_rows = {row["country"]: row["revenue"] async for row in first}

        assert first_rows == expected
        assert second_rows == expected
        assert inner_pool.checkedout() == 0


class TestAsyncEngineScopesItsOwnTeardown:
    """
    ``async with`` on an AsyncEngine mirrors the synchronous scope, against the async store.

    The two registries are deliberately separate, so the async exit must clear the async
    one and leave a synchronous engine of the same name alone. Real DuckDB pools here
    rather than mocks: ``dispose`` is a coroutine on this side, and the point of the test
    is that awaiting it actually happens.
    """

    async def test_leaving_the_block_unregisters_and_disposes(self) -> None:
        """Both halves run, and the name is gone from the async registry afterwards."""
        pytest.importorskip("adbc_driver_duckdb")
        from adbc_poolhouse import DuckDBConfig

        from semolina.config import create_async_engine
        from semolina.registry import _async_engines, get_async_engine

        engine = create_async_engine(DuckDBConfig(database=":memory:", pool_size=1), register=True)
        inner = engine._pool._pool

        async with engine as bound:
            assert bound is engine
            assert get_async_engine("default") is engine

        assert _async_engines == {}
        # A disposed poolhouse pool reports no checked-in connections to hand out.
        assert inner.checkedin() == 0

    async def test_the_async_exit_leaves_a_sync_engine_of_the_same_name_alone(self) -> None:
        """
        One name can hold both kinds at once, which is deliberate and must survive teardown.

        The same warehouse is often wanted from a script and a request handler, so clearing
        the wrong store here would silently unregister the other half.
        """
        pytest.importorskip("adbc_driver_duckdb")
        from adbc_poolhouse import DuckDBConfig

        from semolina.config import create_async_engine, create_engine
        from semolina.registry import get_engine

        sync_engine = create_engine(DuckDBConfig(database=":memory:"), register=True)
        try:
            async with create_async_engine(
                DuckDBConfig(database=":memory:", pool_size=1), register=True
            ):
                pass

            assert get_engine("default") is sync_engine
        finally:
            sync_engine.dispose()

    async def test_a_duplicate_name_releases_the_async_pool_it_could_not_register(self) -> None:
        """
        The losing async engine's pool is closed inline, not leaked.

        ``AsyncEngine.dispose()`` is a coroutine and ``create_async_engine`` is a plain
        ``def``, so cleanup closes the inner synchronous pool directly -- the same route
        ``registry.reset`` takes for the same reason.

        The pool under test is the one built *inside* the failing call, which the caller
        never receives, so it is reached by patching the factory rather than by holding a
        reference. Snowflake config: the DuckDB branch attaches a real SQLAlchemy listener
        to the inner pool, which a mock is not a valid event target for.
        """
        from unittest.mock import MagicMock, patch

        from adbc_poolhouse import SnowflakeConfig
        from pydantic import SecretStr

        from semolina.config import create_async_engine
        from semolina.registry import get_async_engine

        def _config() -> Any:
            return SnowflakeConfig(account="xy12345", user="u", password=SecretStr("p"))

        winning_pool = MagicMock()
        losing_pool = MagicMock()

        # Patched on ``adbc_poolhouse``, not on ``semolina.config``: both names are imported
        # inside the factory (deferred so a non-async install still imports the module), so
        # there is no module attribute to replace.
        with (
            patch("adbc_poolhouse.create_async_pool") as mock_create_async_pool,
            patch("adbc_poolhouse.close_pool") as mock_close_pool,
        ):
            mock_create_async_pool.return_value = winning_pool
            first = create_async_engine(_config(), register=True)

            mock_create_async_pool.return_value = losing_pool
            with pytest.raises(ValueError, match="already registered"):
                create_async_engine(_config(), register=True)

            # Closed through the inner sync pool, and it is the loser's, not the winner's.
            mock_close_pool.assert_called_once_with(losing_pool._pool)
            assert get_async_engine("default") is first

    async def test_a_replacement_under_the_same_name_survives_the_exit(self) -> None:
        """
        The async exit drops the name only while it still points at *this* engine.

        Same reasoning as the synchronous sibling, against the async store: a name
        re-pointed mid-block belongs to the replacement, and clearing it would leave the
        process with no ``"default"`` async engine.
        """
        pytest.importorskip("adbc_driver_duckdb")
        from adbc_poolhouse import DuckDBConfig

        from semolina.config import create_async_engine
        from semolina.registry import (
            get_async_engine,
            register_async_engine,
            unregister_async_engine,
        )

        engine = create_async_engine(DuckDBConfig(database=":memory:", pool_size=1), register=True)
        replacement = create_async_engine(DuckDBConfig(database=":memory:", pool_size=1))

        async with engine:
            unregister_async_engine("default")
            register_async_engine("default", replacement)

        assert get_async_engine("default") is replacement
        # The replacement's pool is untouched: a disposed poolhouse pool raises here.
        assert replacement._pool._pool.checkedout() == 0
