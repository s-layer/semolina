"""
Tests that cancellation reaches the DuckDB driver and the pool recovers.

Cancellation is delivered entirely upstream: adbc-poolhouse's ``cancellable_offload``
parks a watcher on an event while the worker thread blocks in the driver, fires
``adbc_cancel`` from inside a shield when the surrounding scope is cancelled,
invalidates the aborted connection for poison recovery, and re-raises the
framework cancellation rather than the driver's own interrupt error. Semolina
writes none of that. What Semolina can still break is *transparency* — a
``try/except Exception`` would let the cancellation past unnoticed, and a
``try/except BaseException`` that logged and returned would eat it — so these
tests are the executed proof that its frames stay transparent.

The coverage is split by risk, because the parts fail differently.

The **deterministic** tests carry the slot-release and no-masking-teardown
assertions with no timing involved at all, so they can never be flaky. Most
cancel the scope before the call, so the first checkpoint inside it observes an
already-pending cancellation. One cancels *during* the statement instead, which
is where ``aexecute``'s check-in arm lives: a cancellation observed at the first
checkpoint has not yet checked a connection out, so it cannot show that a
connection already taken comes back.

The **long-query** tests are the half that has to prove the *driver* was reached
rather than that the client merely stopped waiting. A test that greened while
the warehouse query kept running would certify abandonment, which is the exact
outcome these tests exist to rule out — on a metered warehouse the cost keeps
accruing after the user is gone. So the query is measurably expensive, its
uncancelled duration is measured rather than assumed, the deadline is set an
order of magnitude below that measurement, and the abort is proven by the
cancelled call returning in a small fraction of the uncancelled duration. If the
measurement cannot clear the floor even at the top of the cost ladder, the tests
skip with the number they measured. An honest skip beats a race.

That last assertion needs an interruptible statement, and both statements under
test are now interruptible. Two classes carry it rather than one because they
fail differently: :class:`TestCancellationReachesTheDriver` cancels ordinary
DuckDB SQL, so it isolates the driver's own cancellation path from anything the
``semantic_views`` extension does, while
:class:`TestCancellationThroughAexecute` cancels Semolina's real generated
``semantic_view()`` call through ``aexecute``. The second is the one that
matches production; the first survives as the control, and would stay green (and
so localize the fault) if a future extension release regressed the interrupt.

That split is load-bearing history rather than symmetry for its own sake. Until
``semantic_views`` 0.12.0 the extension's table function ran its inner query on a
fresh ``ClientContext`` and never read the interrupt flag ``adbc_cancel`` had
set, so a cancelled ``semantic_view()`` aggregate ran to completion and only
*then* reported ``INTERRUPT Error``. During that window this module could assert
an early abort on plain SQL only. Measured on one machine across the two builds,
interrupting at a tenth of the baseline:

===================  ==================  =================
build                cancelled returns   uncancelled
===================  ==================  =================
0.10.3 (duckdb 1.5.3)  3.22s             3.97s
0.12.0 (duckdb 1.5.5)  0.55s             3.21s
===================  ==================  =================

That is what lets the ``aexecute`` test carry the elapsed-time claim on
Semolina's own SQL, and it is also why the claim is not vacuous: at 0.81 of its
baseline the old build fails the same assertion the new one passes at 0.17. The
``duckdb==1.5.5`` pin in ``pyproject.toml`` is what makes the fixed build the one
installed, so moving that pin backwards turns this file red rather than quietly
weakening it.

Cassettes cannot cover any of this: ``ReplayCursor.adbc_cancel()`` is a
deliberate no-op and replay returns instantly, so nothing can ever land
mid-flight. A real driver is required.

Test classes:
- TestDeterministicCancellation: timing-free cancel propagation and slot release
- TestCancellationReachesTheDriver: the abort lands in the driver, mid-query
- TestCancellationThroughAexecute: aexecute stays transparent, pool recovers
"""
# Test-only: these tests reach the owned async pool's inner sync pool via
# engine._pool._pool to assert checkin, and read the cursor's closed flag.
# Scope-disable the private-access rule (intentionally not a `# type: ignore`).
# pyright: reportPrivateUsage=false

from __future__ import annotations

import contextlib
import faulthandler
import sys
import time
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest
from duckdb_extension import load_semantic_views

from semolina import Dimension, Metric, SemanticView

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from semolina.acursor import AsyncSemolinaCursor
    from semolina.query import Query
    from semolina.results import Row

pytestmark = pytest.mark.anyio


class HeavySales(SemanticView, view="heavy_view"):
    """
    The deliberately expensive semantic view the long-query tests aggregate.

    ``digest_cost`` is a CPU-heavy aggregate over a per-row string construction,
    which is what makes a query against it long enough to cancel mid-flight.
    ``row_total`` is a plain sum over the same rows and costs milliseconds — it
    is the cheap query used to warm the pool and, afterwards, to prove the pool
    recovered.
    """

    digest_cost = Metric()
    row_total = Metric()
    bucket = Dimension()


#: Escalating (row count, md5 nesting depth) rungs for the expensive aggregate.
#: The first rung already clears the floor on a 2026-era laptop; the later rungs
#: exist so a faster machine still gets a measurable query rather than a race.
COST_LADDER: tuple[tuple[int, int], ...] = (
    (4_000_000, 32),
    (8_000_000, 64),
    (16_000_000, 128),
)

#: The uncancelled aggregate must take at least this long for the long-query
#: tests to mean anything. Below it, they skip and say what was measured.
MIN_MEASURED_SECONDS = 2.0

#: The deadline is this many times shorter than the measured duration, so the
#: query is unambiguously still in flight in the driver when the abort fires.
DEADLINE_MARGIN = 10.0

#: A cancelled call must return within this fraction of the uncancelled
#: duration. It is the observable that separates an abort that reached the
#: driver from one that did not: a query left running to completion takes the
#: whole measured duration whatever the client does. The deadline sits at 1/10
#: of the measurement, so this leaves a fivefold margin — far too wide to race.
ABORT_EVIDENCE_RATIO = 0.5


def _heavy_query() -> Query:
    """Build the expensive aggregate over the heavy semantic view."""
    return HeavySales.query().metrics(HeavySales.digest_cost).dimensions(HeavySales.bucket)


def _cheap_query() -> Query:
    """Build the millisecond-scale aggregate over the same heavy semantic view."""
    return HeavySales.query().metrics(HeavySales.row_total).dimensions(HeavySales.bucket)


def _digest_expression(digest_depth: int, column: str) -> str:
    """
    Build the nested per-row digest that makes the aggregate expensive.

    Args:
        digest_depth: How many times the digest is nested.
        column: The qualified column expression the digest is seeded from.

    Returns:
        A DuckDB scalar expression of the requested nesting depth.
    """
    expression = f"CAST({column} AS VARCHAR) || h.bucket"
    for _ in range(digest_depth):
        expression = f"md5({expression})"
    return expression


def _plain_heavy_sql(digest_depth: int) -> str:
    """
    Build the interruptible plain-SQL twin of the expensive semantic-view query.

    Same table, same rows, same per-row digest, same grouping — the only
    difference is that it is ordinary DuckDB SQL rather than a call into the
    ``semantic_views`` extension's table function. Both honour the interrupt
    promptly; keeping a twin that routes around the extension entirely is what
    makes the extension attributable if one of them stops doing so.

    Args:
        digest_depth: Nesting depth of the per-row digest.

    Returns:
        A SQL statement whose cost matches the semantic-view aggregate's.
    """
    expression = _digest_expression(digest_depth, "h.id")
    return (
        f"SELECT h.bucket, SUM(LENGTH({expression})) AS digest_cost "
        "FROM heavy_facts h GROUP BY h.bucket"
    )


class _PausingCursor:
    """
    A cursor whose ``execute`` announces that it started and then never finishes.

    Standing in for a warehouse still working on the statement, so a
    cancellation can be delivered at a fixed point *inside* ``aexecute``'s
    ``try`` block rather than at whatever checkpoint happens to come first.
    """

    def __init__(self, inner: Any, executing: Any) -> None:
        """Wrap a real poolhouse cursor plus the event that announces the execute."""
        self._inner = inner
        self._executing = executing

    async def execute(self, sql: str, params: Any) -> None:
        """Announce the in-flight execute, then block until cancelled."""
        import anyio

        self._executing.set()
        await anyio.sleep_forever()

    async def close(self) -> None:
        """Close the wrapped cursor."""
        await self._inner.close()


class _PausingConnection:
    """A real checked-out poolhouse connection that hands out a pausing cursor."""

    def __init__(self, inner: Any, executing: Any) -> None:
        """Wrap a real connection plus the event its cursor announces on."""
        self._inner = inner
        self._executing = executing

    def cursor(self) -> _PausingCursor:
        """Return a pausing cursor over the real connection's cursor."""
        return _PausingCursor(self._inner.cursor(), self._executing)

    async def close(self) -> None:
        """Check the real connection back into the real pool."""
        await self._inner.close()


class _PausingPool:
    """
    A real ``AsyncPool``, wrapped so the execute after checkout can be paused.

    Only the statement stalls. The checkout and the check-in are the pool's own,
    so ``checkedout()`` on the inner synchronous pool remains the real measure of
    whether ``aexecute`` returned the slot it took.
    """

    def __init__(self, inner: Any, executing: Any) -> None:
        """Wrap a real async pool plus the event its cursors announce on."""
        self._inner = inner
        self._executing = executing

    async def connect(self) -> _PausingConnection:
        """Check a real connection out of the real pool, wrapped."""
        return _PausingConnection(await self._inner.connect(), self._executing)


@contextlib.contextmanager
def _hard_deadline(seconds: float) -> Generator[None, None, None]:
    """
    Kill the process if the guarded block runs past a real-clock deadline.

    This guards against a *regression* of the upstream cancel/close race that
    deadlocked the DuckDB driver before adbc-poolhouse 1.6.2, in which the
    watcher fired ``adbc_cancel`` and then invalidated the connection without
    waiting for the aborted worker thread to unwind. That deadlock hung
    indefinitely inside a native call, where no anyio-level timeout can reach
    it, so an ``anyio.fail_after`` around the same block would hang with it.

    ``faulthandler`` runs its timer on a separate thread and is therefore the
    one mechanism that still works when the loop thread is wedged in the driver.
    Tripping it dumps every thread's traceback and exits non-zero, which loses
    the rest of the session — a harsh outcome, chosen deliberately over wedging
    CI forever on a hang that would otherwise never resolve.

    Args:
        seconds: Real-clock budget for the guarded block.

    Yields:
        None. The guarded block runs inside the watchdog window.
    """
    faulthandler.dump_traceback_later(seconds, file=sys.__stderr__ or sys.stderr, exit=True)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


class HeavyDatabase(NamedTuple):
    """A provisioned expensive DuckDB database plus its measured query costs."""

    path: Path
    semantic_view_seconds: float
    plain_sql_seconds: float
    plain_sql: str
    rows: int
    digest_depth: int

    @property
    def measured_seconds(self) -> float:
        """The lower of the two measurements, which is the binding one."""
        return min(self.semantic_view_seconds, self.plain_sql_seconds)


def _provision(db_path: Path, rows: int, digest_depth: int) -> None:
    """
    (Re)build the heavy table and semantic view at a given cost rung.

    Both statements are ``CREATE OR REPLACE`` so a later rung can overwrite an
    earlier one without dropping a semantic view that still references the
    table. The connection is closed before returning because a file-backed
    DuckDB database takes a single-writer lock, and ADBC opens the same file
    next.

    Args:
        db_path: File-backed DuckDB database to build into.
        rows: Number of narrow rows in the fact table.
        digest_depth: How many times the per-row digest is nested.
    """
    import duckdb

    expression = _digest_expression(digest_depth, "h.id")
    conn = duckdb.connect(database=str(db_path))
    try:
        load_semantic_views(conn)
        conn.execute(
            "CREATE OR REPLACE TABLE heavy_facts AS "
            "SELECT i AS id, ('grp_' || CAST(i % 4 AS VARCHAR)) AS bucket "
            f"FROM range({rows}) t(i)"
        )
        conn.execute(
            "CREATE OR REPLACE SEMANTIC VIEW heavy_view AS "
            "TABLES (h AS heavy_facts PRIMARY KEY (id)) "
            "DIMENSIONS (h.bucket AS bucket) "
            f"METRICS (h.digest_cost AS SUM(LENGTH({expression})), "
            "h.row_total AS SUM(h.id))"
        )
    finally:
        conn.close()


def _measure_uncancelled(db_path: Path, plain_sql: str) -> tuple[float, float]:
    """
    Time one full, uncancelled run of each expensive query.

    Measured through Semolina's own synchronous engine rather than raw DuckDB,
    so the numbers cover the same driver, the same pool and the same generated
    SQL the async tests cancel. A cheap query runs first to pay the
    pool-construction and extension-loading cost outside the measurements.

    Args:
        db_path: The provisioned file-backed DuckDB database.
        plain_sql: The interruptible plain-SQL twin of the aggregate.

    Returns:
        Wall-clock seconds for the semantic-view aggregate and for the plain-SQL
        aggregate, in that order.
    """
    from adbc_poolhouse import DuckDBConfig, close_pool

    from semolina.config import create_engine

    engine = create_engine(DuckDBConfig(database=str(db_path)))
    try:
        warmup = engine.execute(_cheap_query())
        try:
            warmup.fetchall_rows()
        finally:
            warmup.close()

        start = time.perf_counter()
        cursor = engine.execute(_heavy_query())
        try:
            cursor.fetchall_rows()
        finally:
            cursor.close()
        semantic_view_seconds = time.perf_counter() - start

        conn = engine._pool.connect()
        try:
            raw = conn.cursor()
            start = time.perf_counter()
            raw.execute(plain_sql)
            raw.fetchall()
            plain_sql_seconds = time.perf_counter() - start
            raw.close()
        finally:
            conn.close()

        return semantic_view_seconds, plain_sql_seconds
    finally:
        close_pool(engine._pool)


@pytest.fixture(scope="session")
def heavy_database(tmp_path_factory: pytest.TempPathFactory) -> HeavyDatabase:
    """
    Provision a DuckDB database whose aggregate is measurably expensive.

    Session-scoped because building and measuring it costs several seconds, and
    session scope means that price is paid once per xdist worker. Deliberately
    *measures* rather than assumes: it walks the cost ladder until one full
    uncancelled aggregate clears ``MIN_MEASURED_SECONDS``, and the measurement
    is what the long-query tests derive their deadline from. A sleep-based query
    would prove nothing about the driver, and a hardcoded duration would rot the
    first time it ran on faster hardware.

    Returns:
        The database path, both measured durations, the plain SQL, and the rung
        that produced them.
    """
    pytest.importorskip("adbc_driver_duckdb")

    db_path = tmp_path_factory.mktemp("duckdb_cancel") / "heavy.db"
    rows, depth = COST_LADDER[0]
    semantic_view_seconds, plain_sql_seconds, plain_sql = 0.0, 0.0, ""
    for rows, depth in COST_LADDER:
        plain_sql = _plain_heavy_sql(depth)
        _provision(db_path, rows, depth)
        semantic_view_seconds, plain_sql_seconds = _measure_uncancelled(db_path, plain_sql)
        print(
            f"\n[cancellation] uncancelled aggregate: semantic_view()="
            f"{semantic_view_seconds:.2f}s plain SQL={plain_sql_seconds:.2f}s "
            f"(rows={rows:,}, digest_depth={depth})"
        )
        if min(semantic_view_seconds, plain_sql_seconds) >= MIN_MEASURED_SECONDS:
            break

    return HeavyDatabase(
        path=db_path,
        semantic_view_seconds=semantic_view_seconds,
        plain_sql_seconds=plain_sql_seconds,
        plain_sql=plain_sql,
        rows=rows,
        digest_depth=depth,
    )


@pytest.fixture
def heavy_async_engine(heavy_database: HeavyDatabase) -> Generator[Any, None, None]:
    """
    An AsyncEngine over the expensive file-backed database.

    File-backed because the heavy database is expensive to build, so it is
    generated once per session; the pool-recovery assertion wants a pool that
    can hand out a fresh connection after one has been invalidated.
    """
    pytest.importorskip("adbc_driver_duckdb")
    from adbc_poolhouse import DuckDBConfig, close_pool

    from semolina.config import create_async_engine

    engine = create_async_engine(DuckDBConfig(database=str(heavy_database.path)))
    yield engine
    close_pool(engine._pool._pool)


def _skip_unless_measurably_slow(heavy_database: HeavyDatabase) -> None:
    """
    Skip, naming the measurement, when the aggregate ran too fast to cancel.

    Args:
        heavy_database: The provisioned database and its measurements.
    """
    if heavy_database.measured_seconds >= MIN_MEASURED_SECONDS:
        return
    pytest.skip(
        "the expensive aggregate completed in "
        f"{heavy_database.measured_seconds:.2f}s at the top of the cost ladder "
        f"({heavy_database.rows:,} rows, digest depth "
        f"{heavy_database.digest_depth}), below the {MIN_MEASURED_SECONDS:.1f}s "
        "floor these tests need to cancel a query that is genuinely still "
        "running in the driver"
    )


class TestDeterministicCancellation:
    """
    Cancellation propagates and releases its slot, with no timing involved.

    These tests import anyio directly. The TID251 Posture A ban scopes to
    ``src/semolina/`` only, and the Trio half of the matrix needs it.
    """

    async def test_cancel_before_execute_completes_propagates_and_releases_slot(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """
        An already-cancelled scope makes ``aexecute`` raise rather than return.

        No sleep and no elapsed-time comparison: the scope is cancelled *before*
        the call, so the first checkpoint inside ``aexecute`` observes a pending
        cancellation and this is deterministic on both backends.

        A cancelled ``aexecute`` that handed back a cursor would be the first
        warning sign that a handler swallowed the cancellation, so the
        ``cursor is None`` assertion is load-bearing rather than incidental.

        The ``checkedout()`` assertion is *not* the other half, despite reading
        like it. Cancelling this early means the first checkpoint that observes
        it is inside ``AsyncPool.connect()``, so no connection is ever checked
        out and the assertion holds even against an ``aexecute`` that leaks
        every slot it takes. Only a cancellation landing after checkout can show
        otherwise; ``test_cancel_during_execute_returns_the_slot`` carries that
        claim.
        """
        import anyio

        cancelled_exc_class = anyio.get_cancelled_exc_class()
        cursor: AsyncSemolinaCursor | None = None
        observed: BaseException | None = None

        with anyio.CancelScope() as scope:
            scope.cancel()
            try:
                cursor = await async_duckdb_engine.aexecute(sales_query)
            except cancelled_exc_class as exc:
                observed = exc
                raise

        assert scope.cancelled_caught
        assert cursor is None, "a cancelled aexecute must raise, never return a cursor"
        assert isinstance(observed, cancelled_exc_class)
        assert async_duckdb_engine._pool._pool.checkedout() == 0

    async def test_cancel_during_execute_returns_the_slot(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """
        A cancellation landing mid-statement gives the checked-out slot back.

        This is the one place ``aexecute``'s ``except BaseException`` arm is the
        only thing that can return the connection: the checkout has happened, no
        cursor exists yet, and nothing downstream will ever call ``aclose()``.
        The pool, the checkout and the check-in are all real; only the statement
        is a stand-in, so the cancellation can be delivered at a fixed point
        inside the ``try`` rather than at whichever checkpoint comes first.

        Deterministic on both backends: the execute announces itself on an event
        and then blocks, so the cancel cannot arrive early and cannot be missed.
        """
        import anyio

        from semolina.engines.abase import AsyncEngine

        inner_pool = async_duckdb_engine._pool._pool
        executing = anyio.Event()
        engine = AsyncEngine(
            pool=_PausingPool(async_duckdb_engine._pool, executing),
            dialect=async_duckdb_engine.dialect,
        )
        cancelled_exc_class = anyio.get_cancelled_exc_class()
        cursor: AsyncSemolinaCursor | None = None
        observed: BaseException | None = None

        async def _execute() -> None:
            nonlocal cursor, observed
            try:
                cursor = await engine.aexecute(sales_query)
            except cancelled_exc_class as exc:
                observed = exc
                raise

        with anyio.CancelScope() as scope:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(_execute)
                await executing.wait()
                # The slot is out and the statement is in flight: this is the
                # state the pre-cancelled test can never reach.
                assert inner_pool.checkedout() == 1
                scope.cancel()

        assert scope.cancelled_caught
        assert cursor is None, "a cancelled aexecute must raise, never return a cursor"
        assert isinstance(observed, cancelled_exc_class)
        assert inner_pool.checkedout() == 0
        assert inner_pool.checkedin() == 1

    async def test_cancel_midstream_propagates_out_of_async_for(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """
        Cancelling inside the ``async for`` body propagates out of the iteration.

        The cancel is followed by an explicit checkpoint inside the loop body so
        the cancellation is delivered at a fixed point rather than whenever the
        next batch pull happens to await — one row consumed, on both backends,
        every run.

        Teardown here runs after the scope has exited, which is the ordinary
        shape: the connection goes back to the pool, so ``checkedout()`` returns
        to 0.
        """
        import anyio

        cancelled_exc_class = anyio.get_cancelled_exc_class()
        rows: list[Row] = []
        observed: BaseException | None = None

        async with await async_duckdb_engine.aexecute(sales_query) as cur:
            with anyio.CancelScope() as scope:
                try:
                    async for row in cur:
                        rows.append(row)
                        scope.cancel()
                        await anyio.sleep(0)
                except cancelled_exc_class as exc:
                    observed = exc
                    raise

        assert scope.cancelled_caught
        assert len(rows) == 1
        assert isinstance(observed, cancelled_exc_class)
        assert cur._closed
        assert async_duckdb_engine._pool._pool.checkedout() == 0

    async def test_cancel_around_the_cursor_block_is_not_masked_by_teardown(
        self, sales_query: Query, async_duckdb_engine: Any
    ) -> None:
        """
        A cancellation spanning the ``async with`` survives the cursor's teardown.

        This is the realistic deadline shape — the scope wraps the whole block,
        so ``aclose()`` runs *while* the cancellation is propagating, with a live
        Arrow reader still holding its lock on the connection. That is precisely
        where a second error would be born: closing the cursor or connection
        before the reader raises ``ConnectionBusyError``, and a teardown error
        raised over a cancellation replaces the exception the caller needs to
        see with one that says nothing about why the work stopped.

        So the assertion is on the *type* of what escaped the block. Anything
        other than the cancellation class — a busy error above all — means
        teardown masked it.
        """
        import anyio

        cancelled_exc_class = anyio.get_cancelled_exc_class()
        rows: list[Row] = []
        observed: BaseException | None = None

        with anyio.CancelScope() as scope:
            try:
                async with await async_duckdb_engine.aexecute(sales_query) as cur:
                    async for row in cur:
                        rows.append(row)
                        scope.cancel()
                        await anyio.sleep(0)
            except cancelled_exc_class as exc:
                observed = exc
                raise

        assert scope.cancelled_caught
        assert len(rows) == 1
        assert isinstance(observed, cancelled_exc_class), (
            "the cursor's teardown raised over the cancellation and masked it: "
            f"got {type(observed).__name__}"
        )


@pytest.mark.timing
class TestCancellationReachesTheDriver:
    """
    A deadline expiring mid-query aborts the query inside the driver.

    This is the class that carries the central claim, and the assertion
    that carries it is the *elapsed time of the cancelled call*. Nothing else
    distinguishes an abort that reached the driver from one that did not: a
    query left running to completion costs its full duration on the warehouse
    whether or not the client is still listening, and a test that asserted only
    "the caller saw a cancellation" would green in both worlds.

    The statement is ordinary SQL rather than Semolina's generated
    ``semantic_view()`` call, which makes this the control for its sibling:
    nothing the ``semantic_views`` extension does can affect it, so if a future
    extension release regresses the interrupt this class stays green and
    :class:`TestCancellationThroughAexecute` turns red, localizing the fault to
    the extension in one bisect step rather than implicating poolhouse or
    Semolina. It still runs through Semolina's async surface —
    ``AsyncEngine.connect()``, the engine's own pool, adbc-poolhouse's offload —
    so only the SQL text differs from the production path.
    """

    async def test_deadline_aborts_the_query_inside_the_driver(
        self, heavy_async_engine: Any, heavy_database: HeavyDatabase
    ) -> None:
        """
        The cancelled call returns in a fraction of the uncancelled duration.

        The measurement is used for two things only: to set the deadline, and to
        decide whether the test can run at all. The assertion compares against
        ``ABORT_EVIDENCE_RATIO`` of the measured duration, which with a deadline
        at one tenth leaves a fivefold margin — a gap far too wide to be a race,
        and the only observable proof the warehouse stopped working.

        The expected exception is the framework's cancellation, *not* DuckDB's
        ``INTERRUPT Error``. poolhouse swallows the driver's interrupt on the
        cancel path and re-raises the cancellation in its place, so asserting on
        the driver error would either pass for the wrong reason or never pass at
        all. A driver error surfacing here instead would mean transparency
        broke, and the explicit failure below says so.
        """
        import anyio

        _skip_unless_measurably_slow(heavy_database)

        cancelled_exc_class = anyio.get_cancelled_exc_class()
        measured = heavy_database.plain_sql_seconds
        deadline = measured / DEADLINE_MARGIN
        observed: BaseException | None = None

        # Warm the pool first, so the deadline is spent on the query rather than
        # on connecting and loading the semantic_views extension.
        async with await heavy_async_engine.aexecute(_cheap_query()) as warm:
            assert await warm.fetchall_rows()

        with _hard_deadline(measured * 4 + 30.0):
            conn = await heavy_async_engine.connect()
            try:
                cursor = conn.cursor()
                start = time.perf_counter()
                try:
                    with anyio.move_on_after(deadline) as scope:
                        try:
                            await cursor.execute(heavy_database.plain_sql)
                        except BaseException as exc:
                            observed = exc
                            raise
                except Exception as exc:  # pragma: no cover - transparency regression
                    pytest.fail(
                        "the deadline should surface as a cancellation; the "
                        "driver's own error escaped instead, which means a frame "
                        "between the caller and the driver stopped being "
                        f"transparent: {type(exc).__name__}: {exc}"
                    )
                elapsed = time.perf_counter() - start
            finally:
                # The connection poolhouse aborted is invalidated for poison
                # recovery, so closing it may itself fail; that is expected and
                # is not what this test is about.
                with contextlib.suppress(Exception):
                    await conn.close()

            assert scope.cancelled_caught
            assert isinstance(observed, cancelled_exc_class)
            assert elapsed < measured * ABORT_EVIDENCE_RATIO, (
                f"the cancelled query took {elapsed:.2f}s against an uncancelled "
                f"{measured:.2f}s, so the abort did not stop the work — the "
                "caller stopped waiting while the warehouse kept going, which is "
                "the abandonment this test exists to rule out"
            )

            # Pool recovery: poolhouse invalidates the connection whose in-flight
            # query it aborted, so a working follow-up query means the poisoned
            # connection was replaced rather than reissued.
            async with await heavy_async_engine.aexecute(_cheap_query()) as recovered:
                assert await recovered.fetchall_rows()
            assert heavy_async_engine._pool._pool.checkedout() == 0


@pytest.mark.timing
class TestCancellationThroughAexecute:
    """
    ``aexecute`` stays transparent under a deadline and leaves a usable pool.

    Same deadline, same expensive data, but through Semolina's real generated
    SQL — which on DuckDB means the ``semantic_view()`` table function. This is
    the production path, so it carries the full set of claims: the query stops
    early, the deadline surfaces as the framework's cancellation and not as the
    driver's interrupt error, ``aexecute`` raises instead of handing back a
    cursor for a query that was cancelled, and the pool still serves the next
    caller. That last group is the ``except BaseException`` arm's one job,
    exercised on the one path where the cancellation genuinely arrives
    mid-statement.

    The early-stop assertion here is the one that matters for the cost
    argument, because it is Semolina's own SQL rather than a hand-written twin:
    it is what rules out a cancelled query still being billed by the warehouse
    after the caller has gone.
    """

    async def test_deadline_over_a_semantic_view_query_is_transparent_and_recovers(
        self, heavy_async_engine: Any, heavy_database: HeavyDatabase
    ) -> None:
        """
        A deadline over ``aexecute`` cancels the caller and leaves the pool usable.

        The elapsed-time check is the same one the sibling class makes, against
        the semantic-view measurement rather than the plain-SQL one, and it is
        the assertion that distinguishes an abort which reached the driver from
        a caller that merely stopped listening.
        """
        import anyio

        _skip_unless_measurably_slow(heavy_database)

        cancelled_exc_class = anyio.get_cancelled_exc_class()
        measured = heavy_database.semantic_view_seconds
        deadline = measured / DEADLINE_MARGIN
        cursor: AsyncSemolinaCursor | None = None
        observed: BaseException | None = None

        async with await heavy_async_engine.aexecute(_cheap_query()) as warm:
            assert await warm.fetchall_rows()

        with _hard_deadline(measured * 4 + 30.0):
            start = time.perf_counter()
            try:
                with anyio.move_on_after(deadline) as scope:
                    try:
                        cursor = await heavy_async_engine.aexecute(_heavy_query())
                    except BaseException as exc:
                        observed = exc
                        raise
            except Exception as exc:  # pragma: no cover - transparency regression
                pytest.fail(
                    "the deadline should surface as a cancellation; the driver's "
                    "own error escaped instead, which means a frame between the "
                    "caller and the driver stopped being transparent: "
                    f"{type(exc).__name__}: {exc}"
                )
            elapsed = time.perf_counter() - start

            assert scope.cancelled_caught
            assert cursor is None, "a cancelled aexecute must raise, never return a cursor"
            assert isinstance(observed, cancelled_exc_class)
            assert elapsed < measured * ABORT_EVIDENCE_RATIO, (
                f"the cancelled semantic_view() query took {elapsed:.2f}s against "
                f"an uncancelled {measured:.2f}s, so the abort did not stop the "
                "work — on Semolina's own generated SQL the caller stopped waiting "
                "while the warehouse kept going, which is the abandonment this test "
                "exists to rule out"
            )

            async with await heavy_async_engine.aexecute(_cheap_query()) as recovered:
                assert await recovered.fetchall_rows()
            assert heavy_async_engine._pool._pool.checkedout() == 0
