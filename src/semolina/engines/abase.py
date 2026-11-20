"""
Async sibling of the backend ``Engine``.

Defines ``AsyncEngine``, which owns exactly one adbc-poolhouse ``AsyncPool``
plus its derived dialect. ``connect()`` checks an ADBC connection out of the
owned pool; ``aexecute()`` runs the same builder + cursor path the synchronous
engine runs, awaiting adbc-poolhouse's coroutines instead of blocking.

``AsyncEngine`` is a *sibling* of :class:`~semolina.engines.base.Engine`, not a
proxy over one: adbc-poolhouse's ``AsyncPool`` builds its own inner synchronous
pool, so there is nothing to proxy, and one engine owns exactly one pool. The
sync/async choice is fixed by which constructor was called.

Posture A: this module imports neither ``asyncio`` nor ``anyio``. Loop-backend
agnosticism is inherited from adbc-poolhouse, which funnels every blocking ADBC
call through a single thread-offload chokepoint; Semolina only awaits it. That
also means query cancellation reaches the warehouse without a line of
cancellation code here, provided no handler in this module ever swallows a
``BaseException``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from semolina.engines.base import _EngineBase
from semolina.exceptions import _connecting

if TYPE_CHECKING:
    from semolina.acursor import AsyncSemolinaCursor
    from semolina.engines.sql import SQLDialect
    from semolina.query import Query

__all__ = [
    "AsyncEngine",
]


class AsyncEngine(_EngineBase):
    """
    Async engine owning one adbc-poolhouse ``AsyncPool`` plus its dialect.

    The async counterpart of :class:`~semolina.engines.base.Engine`. It owns
    exactly one ``AsyncPool`` and the :class:`~semolina.engines.sql.SQLDialect`
    derived from the config type, and it generates SQL with the *same* builder
    the synchronous path uses — there is no second SQL path.

    Unlike ``Engine`` this class is concrete and backend-agnostic: ``introspect``
    is the only method backends specialize, and async introspection is deferred,
    so there are no per-backend async subclasses.

    Async engines are constructed via
    :func:`semolina.config.create_async_engine`, which supplies the pool and
    dialect.

    Example:
        .. code-block:: python

            from adbc_poolhouse import DuckDBConfig

            from semolina.config import create_async_engine

            engine = create_async_engine(DuckDBConfig(database=":memory:"))
            async with await engine.aexecute(query) as cursor:
                async for row in cursor:
                    print(row["country"], row["revenue"])
            await engine.dispose()

    See Also:
        - semolina.engines.base.Engine: The synchronous sibling
        - semolina.config.create_async_engine: Builds an AsyncEngine from a
          config or name
        - semolina.acursor.AsyncSemolinaCursor: The cursor ``aexecute`` returns
    """

    def __init__(self, *, pool: Any, dialect: SQLDialect, config: Any = None) -> None:
        """
        Store the owned async ADBC pool, its derived dialect, and the source config.

        Args:
            pool: The adbc-poolhouse ``AsyncPool`` this engine owns. Typed as
                ``Any`` because the poolhouse async pool surface is untyped and
                its classes are not public importable names — reaching into
                ``adbc_poolhouse._async`` for an annotation would both violate
                that package's API and defeat its lazy-import protection.
            dialect: Concrete :class:`~semolina.engines.sql.SQLDialect` selected
                from the config type by
                :func:`semolina.config.create_async_engine`.
            config: The adbc-poolhouse warehouse config the pool was built from
                (``DuckDBConfig`` etc.). Held so connection metadata is readable
                without re-reading the TOML. Typed as ``Any`` because the union
                of poolhouse config classes is untyped here.
        """
        # The registry name ``create_async_engine(register=...)`` records lives on the shared
        # base. The async registry is a separate store, so ``__aexit__`` drops it from that
        # one only.
        super().__init__(pool=pool, dialect=dialect, config=config)

    async def __aenter__(self) -> AsyncEngine:
        """
        Enter a scope that owns this engine's teardown.

        Returns:
            This engine, so ``async with create_async_engine(...) as engine:`` binds it.
            No ``await`` on construction: the factory is a plain ``def`` that builds the pool
            synchronously. Only the exit awaits, on ``dispose``.
        """
        return self

    async def __aexit__(self, *exc: object) -> None:
        """
        Unregister the engine if this call registered it, then dispose the pool.

        Mirrors :meth:`semolina.engines.base.Engine.__exit__`, including the ordering, the
        ``finally``, and the identity check that leaves a replacement registered under the
        same name alone -- but drops the name from the *async* registry, since the two
        stores are deliberately separate and clearing the wrong one would leave a live name
        pointing at a closed pool.
        """
        try:
            from semolina.registry import unregister_async_engine

            self._drop_registration(unregister_async_engine)
        finally:
            await self.dispose()

    async def connect(self) -> Any:
        """
        Check an ADBC connection out of the owned async pool.

        The async parallel of :meth:`semolina.engines.base.Engine.connect`, but
        with only **one** sanctioned consumption mode rather than two: the
        **long-lived handle**. Keep the returned connection alive past this call
        and return it to the pool with an explicit ``await conn.close()``.

        The context-manager mode the sync engine also permits is not available
        here. Checking the connection back in while an Arrow reader is still
        live invalidates that reader, and the reader created by
        :class:`~semolina.acursor.AsyncSemolinaCursor` outlives the ``aexecute``
        call by design. :meth:`aexecute` therefore hands the live connection to
        the cursor, which closes it — reader first — in ``aclose()``, and
        ``aexecute`` closes it itself on its own error path so a failed
        ``execute()`` does not leak the slot.

        Returns:
            An adbc-poolhouse async connection checked out of the owned pool.
            Typed as ``Any`` because the poolhouse async connection surface is
            untyped.

        Raises:
            SemolinaConnectionError: If the driver cannot open a connection.
        """
        with _connecting("Could not connect to the warehouse"):
            return await self._pool.connect()

    async def dispose(self) -> None:
        """
        Tear down the owned async ADBC pool and release its driver resources.

        The public counterpart to
        :func:`semolina.config.create_async_engine`, and the single sanctioned
        teardown path: callers dispose an engine rather than reaching into
        ``engine._pool``.

        Unlike the synchronous :meth:`semolina.engines.base.Engine.dispose`
        there is no fallback branch. That branch keys on a marker set on the
        *inner* synchronous pool, which an ``AsyncPool`` does not carry, so it
        would fall through to a bare ``pool.close()`` — an un-awaited coroutine
        that closes nothing.
        """
        from adbc_poolhouse import close_async_pool

        await close_async_pool(self._pool)

    async def aexecute(self, query: Query) -> AsyncSemolinaCursor:
        """
        Execute a query through the owned async pool and return an open cursor.

        Builds dialect-specific parameterized SQL with the same builder
        :meth:`semolina.engines.base.Engine.execute` uses — the generated SQL is
        identical on both paths — then checks out a connection, executes the
        statement off the event loop, and wraps the resulting cursor.

        The returned cursor is already open, so the call site reads
        ``async with await engine.aexecute(query) as cursor:``. Closing it (via
        the context manager or ``aclose()``) is what returns the connection to
        the pool.

        Args:
            query: ``Query`` object to execute. Must be valid for execution
                (has metrics and/or dimensions).

        Returns:
            An open :class:`~semolina.acursor.AsyncSemolinaCursor` wrapping the
            post-execute async ADBC cursor.

        Raises:
            ValueError: If query is invalid for execution.
            SemolinaConnectionError: If no connection can be checked out of the pool.
            BaseException: Anything else the underlying driver or the surrounding
                cancellation scope raises is propagated unchanged, after the
                checked-out connection has been returned to the pool.

        Example:
            .. code-block:: python

                async with await engine.aexecute(query) as cursor:
                    rows = await cursor.fetchall_rows()
        """
        from semolina.acursor import AsyncSemolinaCursor

        # Validated here as well as in ``Query.execute()``: a caller holding the engine can
        # pass a query straight in, and the builder below would otherwise fail on its internal
        # assertion, which ``python -O`` strips. Checked before ``connect()``, so an invalid
        # query never takes a pool slot.
        query._validate_for_execution()

        builder = self.dialect.create_builder()
        sql, params = builder.build_select_with_params(query)

        conn = await self.connect()
        try:
            # A plain synchronous accessor: the dbapi cursor() does no I/O, so
            # adbc-poolhouse offloads nothing and there is nothing to await.
            cur = conn.cursor()
            await cur.execute(sql, params)
        except BaseException:
            # Return the checked-out connection to the pool before propagating.
            # Otherwise (cursor()/execute() failures, or cancellation) the slot
            # is leaked, since checkin normally happens only via
            # AsyncSemolinaCursor.aclose() on the success path. Closing here is
            # safe because no Arrow reader exists yet on this path.
            #
            # Never `return` from this handler: under asyncio the cancellation
            # that arrives here is a BaseException, and returning would swallow
            # it and hand back a cursor for a query that was cancelled.
            await conn.close()
            raise

        return AsyncSemolinaCursor(cur, conn)
