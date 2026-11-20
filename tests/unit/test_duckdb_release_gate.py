"""
A DuckDB engine writes each query for the ``semantic_views`` release its connection loaded.

A query that selects a fact beside a dimension is written two ways (``DuckDBSQLBuilder``):
from extension 0.18.0 the fact goes in ``dimensions``, and before it in ``facts`` beside
``dimensions``. Each way is refused by the other side of that release, so the engine has to
know the release before it builds the SQL, including for its very first query, when no
connection existed yet.

The installed extension is a current release, which refuses the pre-0.18 form with
``querying FACTS with DIMENSIONS is not supported``. Patching the recorded release to 0.17.0
therefore shows which form the engine sent: that error means the pre-0.18 SQL reached the
extension, so the engine built the SQL from the release its connection reported.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest
from models import Sales

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytest.importorskip("adbc_driver_duckdb")

_QUERY_FACT_AND_DIMENSION: Callable[[], Any] = lambda: Sales.query().dimensions(  # noqa: E731
    Sales.country, Sales.unit_price
)

_REFUSED_PRE_0_18_FORM = "querying FACTS with DIMENSIONS is not supported"


def _setup(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("CREATE TABLE sales_data (id INTEGER, country VARCHAR, unit_price INTEGER)")
    cur.execute(
        "INSERT INTO sales_data VALUES (1, 'US', 10), (2, 'CA', 20), (3, 'US', 5), (4, 'US', 10)"
    )
    cur.execute(
        "CREATE SEMANTIC VIEW sales_view AS TABLES (s AS sales_data PRIMARY KEY (id))"
        " FACTS (s.unit_price AS unit_price) DIMENSIONS (s.country AS country)"
        " METRICS (s.revenue AS COUNT(*))"
    )
    cur.close()
    dbapi_conn.commit()


def _loaded_release(pool: Any) -> tuple[int, ...]:
    """Read the extension's release straight from DuckDB's catalogue, not through Semolina."""
    with pool.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT extension_version FROM duckdb_extensions()"
            " WHERE extension_name = 'semantic_views' AND loaded"
        )
        (version,) = cur.fetchone()
        cur.close()
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version)
    assert match is not None, f"not a release version: {version!r}"
    return tuple(int(part) for part in match.groups())


@pytest.fixture
def sync_engine() -> Iterator[Any]:
    from adbc_poolhouse import DuckDBConfig, close_pool
    from sqlalchemy import event

    from semolina.config import create_engine

    engine = create_engine(DuckDBConfig(database=":memory:", pool_size=1))
    event.listen(engine._pool, "connect", _setup, once=True)
    yield engine
    close_pool(engine._pool)


@pytest.fixture
def async_engine() -> Iterator[Any]:
    from adbc_poolhouse import DuckDBConfig, close_pool
    from sqlalchemy import event

    from semolina.config import create_async_engine

    engine = create_async_engine(DuckDBConfig(database=":memory:", pool_size=1))
    event.listen(engine._pool._pool, "connect", _setup, once=True)
    yield engine
    close_pool(engine._pool._pool)


def _pretend_release(monkeypatch: pytest.MonkeyPatch, release: tuple[int, ...]) -> None:
    """Make the engine's connections report ``release`` for the extension they loaded."""
    monkeypatch.setattr("semolina.config._extension_release", lambda _cursor: release)


class TestSyncEngine:
    def test_a_new_engine_knows_no_release_until_it_connects(self, sync_engine: Any) -> None:
        assert sync_engine.dialect.semantic_views_release is None

    def test_the_engine_records_the_release_its_connection_loaded(self, sync_engine: Any) -> None:
        with sync_engine.execute(_QUERY_FACT_AND_DIMENSION()) as cursor:
            rows = cursor.fetchall()

        assert sync_engine.dialect.semantic_views_release == _loaded_release(sync_engine._pool)
        assert sorted(rows) == [("CA", 20), ("US", 5), ("US", 10)]

    def test_the_first_query_is_written_for_the_release_its_connection_reported(
        self, sync_engine: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        On the engine's first query, a 0.17.0 release gives the pre-0.18 SQL.

        Built before the checkout, the SQL would assume a current release and run, since the
        installed extension is one. The refusal proves it was built after the connection
        reported 0.17.0.
        """
        import adbc_driver_manager

        _pretend_release(monkeypatch, (0, 17, 0))

        with pytest.raises(adbc_driver_manager.Error, match=_REFUSED_PRE_0_18_FORM):
            sync_engine.execute(_QUERY_FACT_AND_DIMENSION())

        assert sync_engine.dialect.semantic_views_release == (0, 17, 0)


@pytest.mark.anyio
class TestAsyncEngine:
    async def test_the_engine_records_the_release_its_connection_loaded(
        self, async_engine: Any
    ) -> None:
        assert async_engine.dialect.semantic_views_release is None

        async with await async_engine.aexecute(_QUERY_FACT_AND_DIMENSION()) as cursor:
            rows = await cursor.fetchall()

        release = _loaded_release(async_engine._pool._pool)
        assert async_engine.dialect.semantic_views_release == release
        assert sorted(rows) == [("CA", 20), ("US", 5), ("US", 10)]

    async def test_the_first_query_is_written_for_the_release_its_connection_reported(
        self, async_engine: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The async twin: the first ``aexecute`` builds its SQL after the checkout too."""
        import adbc_driver_manager

        _pretend_release(monkeypatch, (0, 17, 0))

        with pytest.raises(adbc_driver_manager.Error, match=_REFUSED_PRE_0_18_FORM):
            await async_engine.aexecute(_QUERY_FACT_AND_DIMENSION())

        assert async_engine.dialect.semantic_views_release == (0, 17, 0)
