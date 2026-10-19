"""
Shared pytest fixtures for Semolina test suite.

Provides centralized test data and engine instances for use across all test files.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from semolina.query import Query
from duckdb_extension import load_semantic_views
from models import Sales


def pytest_configure(config: pytest.Config) -> None:
    """
    Suppress ANSI codes in CliRunner output before any test modules are imported.

    Typer's rich_utils reads GITHUB_ACTIONS / FORCE_COLOR / PY_COLORS at *import
    time* and bakes FORCE_TERMINAL=True into a module-level constant when any of
    those vars is set (GitHub Actions always sets GITHUB_ACTIONS=true).  With
    FORCE_TERMINAL=True the Rich Console ignores NO_COLOR and emits ANSI escape
    codes regardless, breaking plain-string assertions on CliRunner output.

    _TYPER_FORCE_DISABLE_TERMINAL overrides that constant to False (see
    typer.rich_utils).  NO_COLOR is kept as defence-in-depth for other cases
    (e.g. FORCE_COLOR=1 in a local dev environment).
    """
    os.environ.setdefault("_TYPER_FORCE_DISABLE_TERMINAL", "1")
    os.environ.setdefault("NO_COLOR", "1")


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    """
    Run every ``pytest.mark.anyio`` test under both asyncio and Trio.

    Defined once here so a new async test module gets both loops by default rather than by
    remembering to copy a fixture. anyio's plugin reads this name; tests without the anyio
    marker never request it and are not parametrized. It lives in this conftest rather than
    in an ini option because ``testpaths`` includes ``src`` under ``--doctest-modules``, and
    this file's reach stops at ``tests/``.

    Returns:
        The backend name for this parametrization.
    """
    backend: str = request.param
    return backend


@pytest.fixture(autouse=True)
def clean_registry():
    """Reset registry after each test to prevent state leaking."""
    yield
    from semolina import registry

    registry.reset()


@pytest.fixture
def sales_model() -> type[Sales]:
    """
    Provides the Sales SemanticView class.

    Returns:
        Sales SemanticView class with revenue, cost, country, region fields

    Usage:
        def test_something(sales_model):
            query = sales_model.query().metrics(sales_model.revenue)
    """
    return Sales


@pytest.fixture
def sales_query() -> Query:
    """
    Build the Sales query every DuckDB fixture in this suite serves.

    ``revenue`` grouped by ``country`` is the one query the ``sales_view``
    semantic view exists to answer, so the async engine, cursor, query-builder,
    and cancellation modules all reach for it. It lives here rather than four
    times over because a query whose shape the fixtures guarantee belongs beside
    the fixtures that guarantee it.

    Safe to reuse within a test: ``Query`` is a frozen dataclass, so
    ``.using(...)`` and the builder methods return new objects rather than
    mutating this one.

    Returns:
        A ``Query`` selecting the ``revenue`` metric by the ``country``
        dimension, bound to the ``Sales`` model.
    """
    return Sales.query().metrics(Sales.revenue).dimensions(Sales.country)


def _setup_sales_data(dbapi_conn: Any, _connection_record: Any) -> None:
    """
    Create the sales_data table and sales_view semantic view.

    Every pooled connection is an ``adbc_clone`` of one source connection and
    opens the same in-memory database, so the fixtures register this with
    ``once=True``: it seeds on the pool's first physical connection, and every
    later connection (overflow or recycled) sees the committed data. It is a
    ``connect`` listener rather than a ``first_connect`` one because it has to
    run after Semolina's own listener has loaded the semantic_views extension.
    """
    cur = dbapi_conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sales_data (
            id INTEGER,
            revenue INTEGER,
            cost INTEGER,
            country VARCHAR,
            region VARCHAR,
            unit_price INTEGER
        )
    """)
    cur.execute("DELETE FROM sales_data")
    cur.execute("""
        INSERT INTO sales_data VALUES
        (1, 1000, 100, 'US', 'West', 10),
        (2, 2000, 200, 'CA', 'West', 20),
        (3, 500, 50, 'US', 'East', 5)
    """)
    cur.execute("""
        CREATE OR REPLACE SEMANTIC VIEW sales_view AS
        TABLES (
            s AS sales_data PRIMARY KEY (id)
        )
        DIMENSIONS (
            s.country AS country,
            s.region AS region,
            s.unit_price AS unit_price
        )
        METRICS (
            s.revenue AS SUM(s.revenue),
            s.cost AS SUM(s.cost)
        )
    """)
    cur.close()
    dbapi_conn.commit()


@pytest.fixture
def duckdb_pool() -> Generator[Any, None, None]:
    """
    In-memory DuckDB Engine with semantic_views extension and sales_view data.

    Builds the Engine via ``create_engine(DuckDBConfig(...))``,
    which owns the ADBC pool and attaches the ``_load_semantic_views`` connect
    listener. A second, ``once=True`` ``connect`` listener seeds the test data on
    the first physical connection; later connections share that database.
    Registers the Engine as ``"default"`` via the 2-arg ``register(name, engine)``.
    Yields the Engine's pool (``engine._pool``) so the pool-lifecycle tests keep
    their ``pool.connect()`` contract, then unregisters and closes on teardown.
    """
    pytest.importorskip("adbc_driver_duckdb")
    from adbc_poolhouse import DuckDBConfig, close_pool
    from sqlalchemy import event

    import semolina
    from semolina.config import create_engine

    engine = create_engine(DuckDBConfig(database=":memory:", pool_size=1))
    event.listen(engine._pool, "connect", _setup_sales_data, once=True)

    semolina.register("default", engine)
    yield engine._pool
    semolina.unregister("default")
    close_pool(engine._pool)


@pytest.fixture
def async_duckdb_engine() -> Generator[Any, None, None]:
    """
    In-memory DuckDB AsyncEngine with semantic_views extension and sales_view data.

    The async analog of ``duckdb_pool``: ``create_async_engine(DuckDBConfig(...))``
    owns the async ADBC pool and attaches the ``_load_semantic_views`` connect
    listener to the inner sync pool it wraps. A second, ``once=True`` ``connect``
    listener seeds the test data once for the shared in-memory database.

    Yields the **engine** (not the pool) because the async tests drive
    ``aexecute``. Teardown is the inline synchronous ``close_pool`` on the inner
    pool rather than ``await engine.dispose()``: this fixture is synchronous and
    cannot await.
    """
    pytest.importorskip("adbc_driver_duckdb")
    from adbc_poolhouse import DuckDBConfig, close_pool
    from sqlalchemy import event

    from semolina.config import create_async_engine

    engine = create_async_engine(DuckDBConfig(database=":memory:", pool_size=1))
    event.listen(engine._pool._pool, "connect", _setup_sales_data, once=True)

    yield engine
    close_pool(engine._pool._pool)


@pytest.fixture
def async_duckdb_engine_two_slots() -> Generator[Any, None, None]:
    """
    ``async_duckdb_engine`` with room for two connections and a short checkout timeout.

    One pooled connection plus one overflow, so a test can run more queries at once than the
    pool holds, and a checkout that has to wait gives up after 3 seconds rather than
    SQLAlchemy's default 30.
    """
    pytest.importorskip("adbc_driver_duckdb")
    from adbc_poolhouse import DuckDBConfig, close_pool
    from sqlalchemy import event

    from semolina.config import create_async_engine

    engine = create_async_engine(
        DuckDBConfig(database=":memory:", pool_size=1, max_overflow=1, timeout=3)
    )
    event.listen(engine._pool._pool, "connect", _setup_sales_data, once=True)

    yield engine
    close_pool(engine._pool._pool)


@pytest.fixture(scope="session")
def duckdb_file_backed_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """
    Generate a file-backed DuckDB database with a sales_view semantic view.

    Session-scoped so the install/setup cost is paid once per xdist worker.
    Uses ``tmp_path_factory`` (per-worker tmp dir) to avoid races under
    ``-n auto``. The .db is created in a pytest tmp dir and cleaned up at
    session end.
    """
    import duckdb

    db_path = tmp_path_factory.mktemp("duckdb_fixture") / "sales.db"
    conn = duckdb.connect(database=str(db_path))
    try:
        load_semantic_views(conn)
        conn.execute(
            "CREATE TABLE sales_data ("
            "id INTEGER, revenue INTEGER, cost INTEGER, "
            "country VARCHAR, region VARCHAR, unit_price INTEGER)"
        )
        conn.execute(
            "INSERT INTO sales_data VALUES "
            "(1, 1000, 100, 'US', 'West', 10), "
            "(2, 2000, 200, 'CA', 'West', 20)"
        )
        conn.execute(
            "CREATE SEMANTIC VIEW sales_view AS "
            "TABLES (s AS sales_data PRIMARY KEY (id)) "
            "FACTS (s.unit_price AS unit_price) "
            "DIMENSIONS ("
            "s.country AS country, "
            "s.region AS region) "
            "METRICS (s.revenue AS SUM(s.revenue), s.cost AS SUM(s.cost))"
        )
    finally:
        conn.close()
    return db_path


@pytest.fixture
def async_duckdb_file_engine(duckdb_file_backed_db: Path) -> Generator[Any, None, None]:
    """
    File-backed DuckDB AsyncEngine whose pool holds more than one connection.

    Built on ``duckdb_file_backed_db`` so the concurrent-query tests also
    cover a file-backed pool; in-memory pools are covered elsewhere. A
    file-backed database defaults to 5.

    ``pool_size`` is deliberately left unset so the config's own file-backed
    default applies — which is exactly what the adbc-poolhouse >=1.6.2 floor
    makes real, since earlier versions ignored the config's tuning fields.

    The data and semantic view already live in the file, so no data-seeding
    connect listener is needed; ``create_async_engine`` still attaches the
    ``semantic_views`` extension loader.
    """
    pytest.importorskip("adbc_driver_duckdb")
    from adbc_poolhouse import DuckDBConfig, close_pool

    from semolina.config import create_async_engine

    engine = create_async_engine(DuckDBConfig(database=str(duckdb_file_backed_db)))

    yield engine
    close_pool(engine._pool._pool)
