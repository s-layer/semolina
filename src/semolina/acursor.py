"""
Async DBAPI 2.0 cursor wrapper with Row convenience methods.

``AsyncSemolinaCursor`` is the async sibling of
:class:`~semolina.cursor.SemolinaCursor`. It is a separate class rather than a
mode flag on the sync cursor, so a sync-style call on an async cursor cannot
silently hand back a coroutine that nobody awaits.

The two classes differ in one structural way beyond the awaits: teardown. An
async cursor must close its Arrow reader *before* its cursor and connection,
because adbc-poolhouse locks the connection for the reader's whole lifetime.
See :meth:`AsyncSemolinaCursor.aclose`.
"""

from __future__ import annotations

import contextlib
import warnings
from typing import TYPE_CHECKING, Any, TypeVar, cast

from ._cursor_base import _CursorBase
from .exceptions import _claim_read, _require

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    import pandas
    import polars
    import pyarrow
    from pydantic import BaseModel

    from .results import Row

__all__ = [
    "AsyncSemolinaCursor",
]


_M = TypeVar("_M", bound="BaseModel")
"""
The DTO type ``into()`` and ``iter_into()`` produce instances of.

The same ``TypeVar`` the synchronous cursor declares, restated rather than imported: it is a
type parameter of these two methods, not shared state, and ``cursor.py`` has no reason to be
imported for it. A ``TypeVar`` rather than PEP 695 ``def into[M: BaseModel](...)`` syntax,
because ruff's ``target-version`` is ``py311`` where that spelling is a syntax error. The bound
is a string so it stays unevaluated at runtime — pydantic is imported under ``TYPE_CHECKING``
only.
"""


class AsyncSemolinaCursor(_CursorBase):
    """
    Async DBAPI 2.0 cursor wrapper with Row convenience methods.

    Wraps an adbc-poolhouse async cursor via delegation and adds ``Row``-mapping
    convenience over it. Returned already-open by
    :meth:`semolina.engines.abase.AsyncEngine.aexecute`.

    Fetch methods keep the names their synchronous counterparts have and are
    awaited: ``await cursor.fetchall_rows()``. ``description`` and ``rowcount``
    stay plain property reads, because adbc-poolhouse keeps them synchronous.

    ``async with`` is the canonical form. It is the only shape that reliably
    returns the pooled connection, because closing requires awaiting and a
    finalizer cannot await — see :meth:`__del__`.

    A result is read one way, exactly as on the synchronous cursor: the first
    read picks it, and reading a second way raises
    :class:`~semolina.exceptions.SemolinaResultConsumedError` naming both calls.
    The DBAPI fetches (``fetchone``, ``fetchmany``, ``fetchall`` and their
    ``_rows`` variants) count as one way and interleave freely. Every way of
    reading rows needs pyarrow, except ``fetch_polars()``.

    Sharing one cursor or connection across concurrent tasks raises
    adbc-poolhouse's ``ConnectionBusyError``, which propagates unwrapped: an
    ADBC connection allows serialized but not concurrent access, and that
    exception's own message tells you to check out a separate connection per
    task. It rejects rather than serializing on purpose, since serializing
    would let two tasks' statements interleave inside one transaction —
    driver-safe, logically corrupt, and silent. A single task reading one
    cursor two ways used to reach the same error, whose advice did not fit;
    that case now raises ``SemolinaResultConsumedError`` before poolhouse is
    involved.

    Example:
        .. code-block:: python

            async with await engine.aexecute(query) as cursor:
                async for row in cursor:
                    print(row["country"], row["revenue"])

    See Also:
        - semolina.cursor.SemolinaCursor: The synchronous sibling
        - semolina.engines.abase.AsyncEngine: Produces this cursor
    """

    _public_name = "AsyncSemolinaCursor"

    def __init__(self, cursor: Any, conn: Any) -> None:
        """
        Initialize AsyncSemolinaCursor wrapping an async DBAPI 2.0 cursor.

        Args:
            cursor: adbc-poolhouse async cursor (post-execute).
            conn: Async connection that produced the cursor. Closing it returns it to its
                pool.
        """
        # The streaming state the base sets up is created lazily by whichever of __anext__,
        # fetch_record_batch() or iter_into() runs first and shared thereafter: one reader per
        # cursor, owned by the cursor so aclose() can close it before the cursor and
        # connection. It stays typed ``Any`` because adbc-poolhouse's async reader is not a
        # public importable name; reaching into its private module for an annotation would
        # violate that package's API and defeat its lazy-import protection.
        super().__init__(cursor, conn)

    # -- Row convenience methods --

    async def _fetchall(self) -> list[tuple[Any, ...]]:
        """Fetch the remaining rows from the driver, or none once the result is drained."""
        if self._drained:
            return []
        return self._after_fetchall(await self._cursor.fetchall())

    async def _fetchone(self) -> tuple[Any, ...] | None:
        """Fetch the next row from the driver, or ``None`` once the result is drained."""
        if self._drained:
            return None
        return self._after_fetchone(await self._cursor.fetchone())

    async def _fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        """Fetch up to ``size`` rows from the driver; a short batch means it is drained."""
        if self._drained:
            return []
        return self._after_fetchmany(await self._cursor.fetchmany(size), size)

    async def fetchall_rows(self) -> list[Row]:
        """
        Fetch all remaining rows as Row objects.

        Returns:
            List of Row objects with attribute and dict access.

        Raises:
            SemolinaMissingDependencyError: If pyarrow is not installed.
            SemolinaResultConsumedError: If the result is already being read another way.
            ValueError: If two result columns share a name, which a Row cannot represent.
        """
        _require("pyarrow", "pyarrow")
        self._reading = _claim_read(self._reading, "fetchall_rows()", dbapi=True)
        columns = self._row_columns()
        return self._rows_from(columns, await self._fetchall())

    async def fetchone_row(self) -> Row | None:
        """
        Fetch next row as a Row, or None if exhausted.

        Returns:
            Row object, or None if no rows remain.

        Raises:
            SemolinaMissingDependencyError: If pyarrow is not installed.
            SemolinaResultConsumedError: If the result is already being read another way.
            ValueError: If two result columns share a name, which a Row cannot represent.
        """
        _require("pyarrow", "pyarrow")
        self._reading = _claim_read(self._reading, "fetchone_row()", dbapi=True)
        columns = self._row_columns()
        return self._row_from(columns, await self._fetchone())

    async def fetchmany_rows(self, size: int = 1) -> list[Row]:
        """
        Fetch up to size rows as Row objects.

        Args:
            size: Maximum number of rows to fetch. Defaults to 1.

        Returns:
            List of Row objects (may be shorter than size).

        Raises:
            SemolinaMissingDependencyError: If pyarrow is not installed.
            SemolinaResultConsumedError: If the result is already being read another way.
            ValueError: If two result columns share a name, which a Row cannot represent.
        """
        _require("pyarrow", "pyarrow")
        self._reading = _claim_read(self._reading, "fetchmany_rows()", dbapi=True)
        columns = self._row_columns()
        return self._rows_from(columns, await self._fetchmany(size))

    # -- DBAPI 2.0 passthrough methods --

    async def fetchall(self) -> list[tuple[Any, ...]]:
        """
        Fetch all remaining rows as raw tuples (DBAPI passthrough).

        Returns:
            List of tuple rows.

        Raises:
            SemolinaMissingDependencyError: If pyarrow is not installed.
            SemolinaResultConsumedError: If the result is already being read another way.
        """
        _require("pyarrow", "pyarrow")
        self._reading = _claim_read(self._reading, "fetchall()", dbapi=True)
        return await self._fetchall()

    async def fetchone(self) -> tuple[Any, ...] | None:
        """
        Fetch next row as raw tuple (DBAPI passthrough).

        Returns:
            Tuple row, or None if exhausted.

        Raises:
            SemolinaMissingDependencyError: If pyarrow is not installed.
            SemolinaResultConsumedError: If the result is already being read another way.
        """
        _require("pyarrow", "pyarrow")
        self._reading = _claim_read(self._reading, "fetchone()", dbapi=True)
        return await self._fetchone()

    async def fetchmany(self, size: int = 1) -> list[tuple[Any, ...]]:
        """
        Fetch up to size rows as raw tuples (DBAPI passthrough).

        Args:
            size: Maximum number of rows to fetch.

        Returns:
            List of tuple rows.

        Raises:
            SemolinaMissingDependencyError: If pyarrow is not installed.
            SemolinaResultConsumedError: If the result is already being read another way.
        """
        _require("pyarrow", "pyarrow")
        self._reading = _claim_read(self._reading, "fetchmany()", dbapi=True)
        return await self._fetchmany(size)

    async def fetch_arrow_table(self) -> pyarrow.Table:
        """
        Fetch all remaining rows as a PyArrow Table (ADBC passthrough).

        Delegates to the underlying async ADBC cursor. Unlike
        :meth:`fetch_record_batch` this creates no live reader, so it places no
        constraint on close ordering: the whole result is materialized off the
        event loop and the connection is left unlocked.

        Returns:
            ``pyarrow.Table`` with the query results.

        Raises:
            SemolinaMissingDependencyError: If pyarrow is not installed. ADBC builds this
                table through a ``pyarrow.RecordBatchReader``; without pyarrow it raises its
                own ``ProgrammingError("This API requires PyArrow to be installed")``, which
                names neither Semolina nor the extra that fixes it — and which would be
                raised inside the worker thread rather than here.
            SemolinaResultConsumedError: If the result is already being read another way.
            AttributeError: If the underlying cursor does not support
                ``fetch_arrow_table()`` (e.g. a non-ADBC cursor).

        Example:
            .. code-block:: python

                async with await engine.aexecute(query) as cursor:
                    table = await cursor.fetch_arrow_table()
                    df = table.to_pandas()
        """
        _require("pyarrow", "pyarrow")
        self._reading = _claim_read(self._reading, "fetch_arrow_table()")
        return await self._cursor.fetch_arrow_table()

    async def fetch_record_batch(self) -> Any:
        """
        Fetch the result as an async record batch reader (ADBC passthrough).

        Delegates to the underlying async ADBC cursor for lazy, memory-bounded
        streaming, with each batch pull offloaded off the event loop. Most
        callers want ``async for row in cursor`` instead, which drives this
        reader and maps each batch to ``Row`` objects.

        The reader locks its connection for its whole lifetime, and draining it
        does not clear that lock. Close the reader before the cursor, or let
        :meth:`aclose` do it — closing the cursor or connection first raises
        ``ConnectionBusyError``. The cursor therefore *keeps* the reader it
        hands you, so ``aclose()`` can close it and return the pooled
        connection; a reader the cursor did not record would leak its slot
        silently, since the resulting teardown errors are suppressed.

        One reader per cursor, deliberately. There is only ever one underlying
        result stream, and adbc-poolhouse rejects a second reader on the same
        connection outright, so a repeat call returns the reader already in
        flight rather than a fresh stream or a ``ConnectionBusyError`` whose
        message ("check out a separate connection per task") misdescribes the
        situation. The reader is this cursor's way of reading its result: once it
        is taken, iterating the cursor or any other read raises
        :class:`~semolina.exceptions.SemolinaResultConsumedError`, as on the
        synchronous cursor.

        Returns:
            An adbc-poolhouse async record batch reader, owned by this cursor.
            Typed as ``Any`` because that class is not a public importable name.

        Raises:
            SemolinaMissingDependencyError: If pyarrow is not installed. The reader *is* a
                pyarrow object; ADBC calls its own ``_requires_pyarrow()`` here and raises a
                ``ProgrammingError`` that names neither Semolina nor the extra.
            SemolinaResultConsumedError: If the result is already being read another way.
            AttributeError: If the underlying cursor does not support
                ``fetch_record_batch()`` (e.g. a non-ADBC cursor).

        Example:
            .. code-block:: python

                async with await engine.aexecute(query) as cursor:
                    reader = await cursor.fetch_record_batch()
                    async for batch in reader:
                        process(batch)
        """
        _require("pyarrow", "pyarrow")
        self._reading = _claim_read(self._reading, "fetch_record_batch()")
        return await self._open_reader()

    async def _open_reader(self) -> Any:
        """
        Return the cursor's one reader, creating it on first use.

        Shared by :meth:`fetch_record_batch`, iteration and :meth:`iter_into`, each of which
        has already claimed the result; recorded so :meth:`aclose` can close it first.

        Returns:
            The adbc-poolhouse async record batch reader.
        """
        if self._reader is None:
            self._reader = await self._cursor.fetch_record_batch()
        return self._reader

    async def fetch_df(self) -> pandas.DataFrame:
        """
        Fetch all remaining rows as a pandas ``DataFrame`` (ADBC passthrough).

        Delegates to adbc-poolhouse, which offloads ADBC's own ``fetch_df()`` onto a worker
        thread through the pool limiter, with cancellation and poison recovery handled there.
        ADBC reads the result through a pyarrow reader — ``self.reader.read_pandas()`` — so
        this path needs **both** pyarrow and pandas. Semolina converts nothing itself, and
        reimplements nothing: the offload is already cancellation-aware, so a long fetch stays
        interruptible.

        Requires an ADBC-capable cursor (Snowflake, Databricks, or DuckDB pool connections).
        Not supported by non-ADBC cursors.

        Reads the whole result, like ``fetch_arrow_table()``, so any other way of reading the
        same cursor afterwards raises :class:`~semolina.exceptions.SemolinaResultConsumedError`.

        A ``DECIMAL`` metric arrives as an ``object`` column holding ``decimal.Decimal``
        values — pandas has no native decimal dtype, so precision survives but the column is
        untyped. ``fetch_polars()`` does better; see its note.

        Returns:
            ``pandas.DataFrame`` with the query results.

        Raises:
            SemolinaMissingDependencyError: If pyarrow or pandas is not installed. pyarrow is
                checked first, because ADBC reaches its pyarrow reader before anything imports
                pandas. Both are checked *before* the await: poolhouse never imports either
                package and lets the driver's native ``ModuleNotFoundError`` cross the thread
                boundary unchanged, several frames deep in someone else's module.
            SemolinaResultConsumedError: If the result is already being read another way.
            AttributeError: If the underlying cursor does not support ``fetch_df()``
                (e.g. a non-ADBC cursor).

        Example:
            .. code-block:: python

                async with await engine.aexecute(query) as cursor:
                    df = await cursor.fetch_df()
                df.head()
        """
        # pyarrow first: ADBC's `fetch_df` is `self.reader.read_pandas()`, and the `reader`
        # property calls its own `_requires_pyarrow()` before pandas is ever imported. Guarding
        # pandas first would let ADBC's ProgrammingError win on a pyarrow-less install.
        _require("pyarrow", "pyarrow")
        _require("pandas", "pandas")
        self._reading = _claim_read(self._reading, "fetch_df()")
        return await self._cursor.fetch_df()

    async def fetch_polars(self) -> polars.DataFrame:
        """
        Fetch all remaining rows as a polars ``DataFrame`` (ADBC passthrough).

        Delegates to adbc-poolhouse, which offloads ADBC's own ``fetch_polars()`` onto a
        worker thread through the pool limiter, with cancellation handled there. ADBC hands
        polars the result's raw Arrow PyCapsule stream — ``polars.from_arrow(self.fetch_arrow())``
        — and never touches pyarrow. This method is therefore guarded on polars **only**:
        requiring pyarrow here would refuse a call that works on a polars-and-no-pyarrow
        install.

        Requires an ADBC-capable cursor (Snowflake, Databricks, or DuckDB pool connections).
        Not supported by non-ADBC cursors.

        **This must be the first read on the cursor.** After any other way of reading it —
        iterating, ``fetch_record_batch()``, ``fetch_arrow_table()``, ``into()`` — this raises
        :class:`~semolina.exceptions.SemolinaResultConsumedError`, naming the earlier call.
        ADBC's implementation *takes* the cursor's Arrow stream handle and leaves ``None``
        behind, so calling ``fetch_polars()`` twice reaches the driver's own
        ``ProgrammingError("Result set has been closed or consumed")``. Reading ``description``
        first is safe; it does not import the stream.

        A ``DECIMAL`` metric keeps its precision and its type: polars 1.43.2 gives a warehouse
        ``decimal128(38, 2)`` column a native ``Decimal(precision=38, scale=2)`` dtype holding
        ``decimal.Decimal`` values, measured on this project's own type-fidelity probe. That is
        better than ``fetch_df()``, where the same column falls back to an untyped ``object``
        dtype. One condition, recorded because it is reachable in principle and not in
        practice: polars was measured raising a Rust ``PanicException`` on a ``decimal256``
        column, and no backend Semolina supports has been observed producing one — a Snowflake
        ``NUMBER`` stops at precision 38, and Databricks and DuckDB decimals stop there too.

        Returns:
            ``polars.DataFrame`` with the query results.

        Raises:
            SemolinaMissingDependencyError: If polars is not installed. ADBC does a bare
                ``import polars`` inside the fetch, which poolhouse runs in a worker thread
                and deliberately does not pre-check, so without this guard the caller gets a
                ``ModuleNotFoundError`` raised across a thread boundary in someone else's
                module.
            SemolinaResultConsumedError: If the result is already being read another way.
            AttributeError: If the underlying cursor does not support ``fetch_polars()``
                (e.g. a non-ADBC cursor).

        Example:
            .. code-block:: python

                async with await engine.aexecute(query) as cursor:
                    df = await cursor.fetch_polars()
                df.head()
        """
        # polars only, deliberately. ADBC's `fetch_polars` is
        # `polars.from_arrow(self.fetch_arrow())` over the raw PyCapsule stream: no reader is
        # built, so `_requires_pyarrow()` is never reached and pyarrow need not be installed.
        _require("polars", "polars")
        self._reading = _claim_read(self._reading, "fetch_polars()")
        return await self._cursor.fetch_polars()

    # -- Typed results --

    async def into(self, model: type[_M], *, validate: bool = False) -> list[_M]:
        """
        Convert the whole result into a list of Pydantic model instances.

        The async twin of :meth:`semolina.cursor.SemolinaCursor.into`, and identical to it in
        everything except the ``await``: columns are matched to fields by name through
        arrowmodel's own key rule — ``validation_alias``, then ``alias``, then the field name.
        Result columns the model does not declare are ignored, so one DTO can serve several
        queries. A declared field with no matching column is an error unless it carries a
        default.

        Any Pydantic ``BaseModel`` subclass works; inheriting from ``arrowmodel.ArrowModel``
        is not required and buys nothing here.

        Before any row moves, the result schema is checked against the model's annotations
        (see :func:`semolina.dto.check_result_schema`). That check reads ``description``, which
        is a plain property on this cursor, so it happens *before* the first ``await``.

        ``validate`` selects between two coherent behaviours, exactly as on the synchronous
        cursor:

        - ``validate=False`` (default) — **types must match.** ``model_construct`` converts
          nothing, so a disagreeing annotation would leave a wrong-typed value in the field;
          the structural check raises
          :class:`~semolina.exceptions.SemolinaSchemaMismatchError` first, naming every
          offending field.
        - ``validate=True`` — **types are coerced.** Pydantic converts per row where it legally
          can (``decimal128`` into ``float``) and raises ``ValidationError`` where it cannot
          (a fractional ``decimal128`` into ``int``). The structural type comparison is
          skipped so it cannot refuse a narrowing the validated path performs correctly.

        Column *presence* is checked on both settings: no amount of coercion invents a column.

        Materializes the whole result as an Arrow table, off the event loop. It reads the
        whole result, so any other way of reading the same cursor afterwards raises
        :class:`~semolina.exceptions.SemolinaResultConsumedError`.

        Args:
            model: The Pydantic model to build. Any ``type[BaseModel]``.
            validate: Run Pydantic validation per row instead of the fast path. Passed
                straight through to arrowmodel. Defaults to False.

        Returns:
            A list of ``model`` instances, one per result row. Empty for a zero-row result.

        Raises:
            SemolinaMissingDependencyError: If pyarrow or arrowmodel is not installed.
            SemolinaSchemaMismatchError: If the model's annotations do not describe the
                result schema.
            SemolinaResultConsumedError: If the result is already being read another way.
            pydantic.ValidationError: Under ``validate=True`` only, for a value Pydantic
                cannot convert to its declared annotation. A NULL in a non-optional field is
                the common one, since the structural check says nothing about nullability.
            TypeError: From arrowmodel, for a field annotated ``dict`` or ``Mapping`` against
                an Arrow ``map`` column, which it materializes as a list of pairs instead.
                The structural check records no verdict on a parameterized generic, so this
                one stays arrowmodel's to raise.
            ValueError: From arrowmodel, naming a required column it could not find. The
                check above pre-empts this with a better message whenever it has a schema to
                read, so it survives only where ``description`` was ``None``.

        Example:
            .. code-block:: python

                import decimal

                import pydantic


                class SalesDTO(pydantic.BaseModel):
                    region: str
                    total_order_value: decimal.Decimal


                async with await engine.aexecute(query) as cursor:
                    rows = await cursor.into(SalesDTO)
                # [SalesDTO(region='US', total_order_value=Decimal('43.25')), ...]

        See Also:
            - semolina.cursor.SemolinaCursor.into: The synchronous sibling
            - AsyncSemolinaCursor.iter_into: The streaming form
        """
        # pyarrow first, as on the sync cursor: reading `description` without it raises ADBC's
        # own ProgrammingError from a _NoOpBackend, which names neither Semolina nor the extra
        # that fixes it.
        _require("pyarrow", "pyarrow")
        _require("arrowmodel", "arrowmodel")

        from .dto import check_result_schema

        check_result_schema(self.description, model, check_types=not validate)
        self._reading = _claim_read(self._reading, "into()")

        from arrowmodel import model_convert

        table = await self._cursor.fetch_arrow_table()
        # arrowmodel types model_convert as `-> list[BaseModel]`, which loses the concrete
        # model. A cast recovers it without a suppression comment.
        return cast("list[_M]", model_convert(model, table, validate=validate))

    def iter_into(self, model: type[_M], *, validate: bool = False) -> AsyncIterator[_M]:
        """
        Stream the result as Pydantic model instances, one at a time.

        The streaming sibling of :meth:`into`, and the async twin of
        :meth:`semolina.cursor.SemolinaCursor.iter_into`. Instances are produced individually,
        but conversion happens a whole Arrow batch at a time, and each batch is pulled through
        adbc-poolhouse's thread offload as the previous one runs out — so a result larger than
        memory is never materialized and the event loop is never blocked on a pull. Column
        matching, alias resolution and the treatment of extra or defaulted fields are identical
        to :meth:`into`; only the delivery differs.

        **The schema check happens at the call, not on the first ``async for``.** This is a
        plain method, deliberately: it is neither a coroutine function nor an async generator
        function, so its body runs when you call it and the returned object is ready to hand
        straight to ``async for`` without awaiting it first. Both of the obvious alternatives
        would defer the check — an ``async def`` until someone awaits, an ``async def``
        containing ``yield`` until someone iterates — and the deferred error would then arrive
        several frames away, inside whatever loop eventually consumed the stream. Reading the
        schema from ``description`` rather than from the reader is what makes this possible:
        ``description`` is synchronous here and creates no reader, so nothing needs awaiting
        before the check can run.

        The returned iterator is this cursor's way of reading its result: any other way,
        :meth:`fetch_record_batch` included, then raises
        :class:`~semolina.exceptions.SemolinaResultConsumedError`.

        Close the cursor. The reader locks its pooled connection for its whole lifetime and
        draining it does not clear that lock — only :meth:`aclose` does, which is why the
        iterator obtains its reader through this cursor rather than behind its back. And unlike
        the synchronous cursor, this one has **no ``__del__`` rescue**: a cursor closed by
        neither ``async with`` nor ``aclose()`` leaks its pooled connection permanently, and
        enough of them exhaust the pool. Consume the iterator inside ``async with``.

        Args:
            model: The Pydantic model to build. Any ``type[BaseModel]``.
            validate: Run Pydantic validation per row instead of the fast path. Set on the
                converter once and reused for every batch. Defaults to False. Read
                :meth:`into`'s note first — ``validate=True`` coerces instead of
                requiring exact types.

        Returns:
            An ``AsyncIterator`` yielding one ``model`` instance per result row. Empty for a
            zero-row result; zero-row batches mid-stream are skipped rather than ending it.

        Raises:
            SemolinaMissingDependencyError: If pyarrow or arrowmodel is not installed. Raised
                at the call.
            SemolinaSchemaMismatchError: If the model's annotations do not describe the
                result schema. Raised at the call.
            SemolinaResultConsumedError: If the result is already being read another way.
            pydantic.ValidationError: Under ``validate=True`` only, for a value Pydantic
                cannot convert to its declared annotation — a NULL in a non-optional field
                being the common one. Raised from the batch that carries the value, not at
                the call, since no value has been read yet when the call returns.
            TypeError: From arrowmodel, for a field annotated ``dict`` or ``Mapping`` against
                an Arrow ``map`` column. Raised on the first ``__anext__``.
            ValueError: From arrowmodel, naming a required column it could not find. The
                check above pre-empts this whenever it has a schema to read, so it survives
                only where ``description`` was ``None``. Raised on the first ``__anext__``.

        Example:
            .. code-block:: python

                import decimal

                import pydantic


                class SalesDTO(pydantic.BaseModel):
                    region: str
                    total_order_value: decimal.Decimal


                async with await engine.aexecute(query) as cursor:
                    async for dto in cursor.iter_into(SalesDTO):
                        process(dto)

        See Also:
            - semolina.cursor.SemolinaCursor.iter_into: The synchronous sibling
            - AsyncSemolinaCursor.into: The whole-result form
        """
        # Same order as `into`: pyarrow first, because reading `description` without it raises
        # ADBC's own ProgrammingError from a _NoOpBackend, naming neither Semolina nor the
        # extra.
        _require("pyarrow", "pyarrow")
        _require("arrowmodel", "arrowmodel")

        from .dto import check_result_schema

        check_result_schema(self.description, model, check_types=not validate)
        self._reading = _claim_read(self._reading, "iter_into()")

        # No `await` and no `yield` in this body, deliberately — either one would move the
        # work above into the caller's first await or first `async for`, which is the timing
        # D-05 forbids. Everything lazy lives in the async generator function below.
        return self._aiter_into_impl(model, validate=validate)

    async def _aiter_into_impl(self, model: type[_M], *, validate: bool) -> AsyncIterator[_M]:
        """
        Yield model instances batch by batch — the lazy half of :meth:`iter_into`.

        Private because its laziness is the thing :meth:`iter_into` exists to wrap: called
        directly it would skip the dependency guards and the schema pre-check, which is
        precisely the timing D-05 forbids.

        The reader comes from :meth:`_open_reader`, this cursor's own bookkeeping, and not
        from the underlying poolhouse cursor. That is what records the reader so :meth:`aclose`
        can close it before the cursor and the connection; a reader the cursor never saw would
        leak its pool slot in silence, because the resulting teardown errors are suppressed.

        Args:
            model: The Pydantic model to build.
            validate: Passed to the converter's constructor, not to its ``iter()`` — the
                per-call methods take no such keyword, so setting it anywhere else is a
                silent no-op.

        Yields:
            One ``model`` instance per row of each batch, in reader order.
        """
        from arrowmodel import ArrowModelConverter

        # Built once, outside the loop: the converter compiles its alias-aware field map at
        # init and reuses it across batches.
        converter = ArrowModelConverter(model, validate=validate)

        try:
            reader = await self._open_reader()
        except OSError:
            # Some drivers report an already-drained result when the reader is created rather
            # than on the first pull; `__anext__` normalizes the same case.
            return
        while True:
            try:
                # One offloaded, cancellable pull.
                batch = await reader.__anext__()
            except StopAsyncIteration:
                # PEP 525's analogue of PEP 479: a StopAsyncIteration escaping an async
                # generator body becomes a RuntimeError, so terminate explicitly rather than
                # copying `__anext__`'s bare `raise`.
                return
            except OSError:
                # A drained stream can raise rather than end, and that OSError crosses
                # poolhouse's thread boundary unchanged. Normalized to termination, as
                # `__anext__` does.
                return
            if batch.num_rows == 0:
                # Mirrors `__anext__`: an empty batch is a hole in the stream, not its end.
                continue
            # arrowmodel types `iter` as `-> Iterator[BaseModel]`, losing the concrete model.
            # A cast recovers it without a suppression comment. `yield from` is not available
            # in an async generator, so the loop is spelled out. Never hand the reader itself
            # to arrowmodel: it is rejected with
            # `ValueError: Expected an object with dunder __arrow_c_array__`.
            for dto in cast("Iterator[_M]", converter.iter(batch)):
                yield dto

    # -- DBAPI 2.0 passthrough properties --

    @property
    def description(self) -> list[tuple[Any, ...]] | None:
        """
        Cursor description passthrough.

        Synchronous, with no await, because adbc-poolhouse keeps it a plain
        property read: there is no I/O to offload.

        Returns:
            List of 7-element tuples describing columns, or None before execute.
        """
        return self._cursor.description

    @property
    def rowcount(self) -> int:
        """
        Row count passthrough.

        Synchronous for the same reason as :attr:`description`.

        Returns:
            Number of rows affected by the last operation.
        """
        return self._cursor.rowcount

    # -- Iteration --

    def __aiter__(self) -> AsyncSemolinaCursor:
        """
        Return self — AsyncSemolinaCursor is its own async iterator.

        Single-pass: the underlying ADBC stream is consumed once. Re-iterating
        an exhausted cursor yields zero rows. Iteration does NOT auto-close the
        cursor — call ``aclose()`` or use ``async with``.

        Returns:
            ``self`` for use in ``async for row in cursor:`` syntax.
        """
        return self

    async def __anext__(self) -> Row:
        """
        Return the next row from the underlying async record batch reader.

        Lazily pulls batches one at a time, each pull offloaded off the event
        loop by adbc-poolhouse, yielding ``Row`` objects from the current batch.
        Zero-row batches are skipped.

        Raises:
            StopAsyncIteration: When the underlying reader is exhausted and the
                current batch is fully consumed. Also raised on re-iteration of
                an exhausted cursor, and when a driver reports the drained stream
                as ``OSError``. Does NOT close the cursor.
            SemolinaMissingDependencyError: If pyarrow is not installed.
            SemolinaResultConsumedError: If the result is already being read another way.
            ValueError: If two columns in a batch share a name, which a Row cannot
                represent.

        Returns:
            ``Row`` constructed from the next batch row, keyed by the batch
            schema's column names.
        """
        if self._reader is None:
            _require("pyarrow", "pyarrow")
        self._reading = _claim_read(self._reading, "async for row in cursor")
        if self._iteration_finished():
            raise StopAsyncIteration
        try:
            reader = await self._open_reader()
        except OSError as exc:
            # Some drivers report an already-drained result when the reader is
            # created rather than on the first pull; the sync cursor normalizes
            # the same case here.
            self._stream_exhausted = True
            raise StopAsyncIteration from exc
        while self._needs_batch():
            try:
                # One offloaded, cancellable pull.
                batch = await reader.__anext__()
            except StopAsyncIteration:
                # poolhouse converts the driver's end-of-stream into its own
                # sentinel before it can cross the thread boundary, because a
                # bare StopIteration crossing that boundary becomes a
                # RuntimeError. That covers a stream this cursor drained itself.
                self._stream_exhausted = True
                raise
            except OSError as exc:
                # It does not cover a stream drained by something else: the
                # driver raises rather than ending the stream, and that OSError
                # crosses the boundary unchanged. Normalize it to iteration
                # termination, as the sync cursor does, so both cursors return
                # zero rows where DBAPI's fetchone() would return None.
                self._stream_exhausted = True
                raise StopAsyncIteration from exc
            self._buffer_batch(batch)
        return self._next_buffered_row()

    # -- Lifecycle --

    async def aclose(self) -> None:
        """
        Close in the one order adbc-poolhouse permits: reader, cursor, connection.

        A live Arrow reader locks its connection for the reader's whole
        lifetime, and draining the reader does not clear that lock — only
        ``reader.close()`` does. Both the async cursor's and the async
        connection's ``close()`` take the foreign tier of that guard, so closing
        either before the reader raises ``ConnectionBusyError`` from inside
        teardown. The order therefore holds even when the caller iterated
        partially or not at all.

        Each step is suppressed narrowly — ``Exception``, deliberately not
        ``BaseException`` — so teardown cannot mask the caller's error while a
        cancellation arriving during teardown still propagates. Idempotent: the
        closed flag is set first, so a second call is a no-op. Tolerant of an
        already-invalidated connection, which is a real state here because
        adbc-poolhouse invalidates a connection whose in-flight query it
        aborted on cancellation.

        Suppressed is not silent for the last step. A connection that fails to
        close is a pool slot that never comes back, so that failure is reported
        as a ``ResourceWarning`` — the same vocabulary :meth:`__del__` uses for
        the leak it cannot prevent. The reader and cursor closes stay quiet:
        neither leaks anything on its own, and a connection still holding a
        reader open reports the consequence itself on the step that matters.
        """
        if self._closed:
            return
        self._closed = True
        if self._reader is not None:
            with contextlib.suppress(Exception):
                await self._reader.close()
        with contextlib.suppress(Exception):
            await self._cursor.close()
        try:
            await self._conn.close()
        # Broad on purpose, and still narrower than BaseException: teardown must
        # not mask the caller's error, but it must not hide its own either.
        except Exception as exc:  # noqa: BLE001 -- see the comment above
            warnings.warn(
                self._leaked_connection_message(exc),
                ResourceWarning,
                stacklevel=2,
            )

    def __del__(self) -> None:
        """
        Warn — and only warn — that a cursor was never closed.

        This is **not** parity with :meth:`semolina.cursor.SemolinaCursor.__del__`,
        which really does return a leaked connection to the pool. The async twin
        cannot: closing requires awaiting, and a finalizer cannot await. Calling
        ``aclose()`` here would leave an un-awaited coroutine and emit the
        "coroutine was never awaited" runtime warning — which is exactly why
        adbc-poolhouse's own reader finalizer refuses to do it either.

        So a cursor closed by neither ``async with`` nor ``aclose()`` leaks its
        pooled connection permanently, and enough of them exhaust the pool. Using
        ``async with`` is the whole mitigation; this warning only tells you when
        you forgot. Guarded against a partial ``__init__`` and never raises,
        because finalizers must not propagate exceptions.
        """
        if getattr(self, "_closed", True):
            return
        with contextlib.suppress(Exception):
            warnings.warn(
                "AsyncSemolinaCursor was garbage collected without being closed; "
                "its pooled connection is leaked and will not be reclaimed. Use "
                "'async with await engine.aexecute(query) as cursor:' or await "
                "'cursor.aclose()'.",
                ResourceWarning,
                stacklevel=2,
            )

    async def __aenter__(self) -> AsyncSemolinaCursor:
        """Enter async context manager."""
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Exit async context manager, closing reader, cursor, and connection."""
        await self.aclose()
