"""
DBAPI 2.0 cursor wrapper with Row convenience methods.

SemolinaCursor delegates to any DBAPI 2.0-compatible cursor and adds
fetchall_rows(), fetchmany_rows(), and fetchone_row() that convert
raw tuples into Row objects using cursor.description column names.
"""

from __future__ import annotations

import contextlib
import warnings
from typing import TYPE_CHECKING, Any, TypeVar, cast

from ._cursor_base import _CursorBase
from .exceptions import _claim_read, _require

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pandas
    import polars
    import pyarrow
    from pydantic import BaseModel

    from .results import Row

__all__ = [
    "SemolinaCursor",
]


_M = TypeVar("_M", bound="BaseModel")
"""
The DTO type ``into()`` and ``iter_into()`` produce instances of.

A ``TypeVar`` rather than PEP 695 ``def into[M: BaseModel](...)`` syntax: ruff's
``target-version`` is ``py311``, where that spelling is a syntax error. The bound is a string
so it stays unevaluated at runtime — pydantic is imported under ``TYPE_CHECKING`` only.
"""


class SemolinaCursor(_CursorBase):
    """
    ADBC cursor wrapper with Row convenience methods.

    Wraps the ADBC cursor :meth:`~semolina.engines.base.Engine.execute` opens, and keeps its
    DBAPI 2.0 fetch methods. Adds fetchall_rows(), fetchmany_rows(), and fetchone_row(),
    which return Row objects, and the Arrow and DataFrame readers ADBC provides.

    A result is read one way. The first read picks it: the DBAPI fetches (``fetchone``,
    ``fetchmany``, ``fetchall`` and their ``_rows`` variants, which interleave freely),
    ``for row in cursor``, ``fetch_record_batch()``, ``iter_into()``, or one of the
    whole-result methods. Reading it a second way raises
    :class:`~semolina.exceptions.SemolinaResultConsumedError` naming both calls, because the
    second would start after whatever the first had buffered and silently miss those rows.

    Every way of reading rows needs pyarrow, because ADBC reads through a pyarrow reader;
    each raises :class:`~semolina.exceptions.SemolinaMissingDependencyError` without it. The
    one exception is ``fetch_polars()``.

    Context manager support releases the reader, cursor and connection on exit.
    """

    _public_name = "SemolinaCursor"

    # The one reader, created by whichever of __next__, fetch_record_batch() or iter_into()
    # runs first and closed by close().
    _reader: pyarrow.RecordBatchReader | None

    def __init__(self, cursor: Any, conn: Any) -> None:
        """
        Initialize SemolinaCursor wrapping an ADBC cursor.

        Args:
            cursor: ADBC DBAPI cursor (post-execute).
            conn: Connection that produced the cursor. Closing it returns it to its pool.
        """
        super().__init__(cursor, conn)

    # -- Row convenience methods --

    def _fetchall(self) -> list[tuple[Any, ...]]:
        """Fetch the remaining rows from the driver, or none once the result is drained."""
        if self._drained:
            return []
        return self._after_fetchall(self._cursor.fetchall())

    def _fetchone(self) -> tuple[Any, ...] | None:
        """Fetch the next row from the driver, or ``None`` once the result is drained."""
        if self._drained:
            return None
        return self._after_fetchone(self._cursor.fetchone())

    def _fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        """Fetch up to ``size`` rows from the driver; a short batch means it is drained."""
        if self._drained:
            return []
        return self._after_fetchmany(self._cursor.fetchmany(size), size)

    def fetchall_rows(self) -> list[Row]:
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
        return self._rows_from(columns, self._fetchall())

    def fetchone_row(self) -> Row | None:
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
        return self._row_from(columns, self._fetchone())

    def fetchmany_rows(self, size: int = 1) -> list[Row]:
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
        return self._rows_from(columns, self._fetchmany(size))

    # -- DBAPI 2.0 passthrough methods --

    def fetchall(self) -> list[tuple[Any, ...]]:
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
        return self._fetchall()

    def fetchone(self) -> tuple[Any, ...] | None:
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
        return self._fetchone()

    def fetchmany(self, size: int = 1) -> list[tuple[Any, ...]]:
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
        return self._fetchmany(size)

    def fetch_arrow_table(self) -> pyarrow.Table:
        """
        Fetch all remaining rows as a PyArrow Table (ADBC passthrough).

        Delegates to the underlying ADBC cursor's ``fetch_arrow_table()``
        method for zero-copy Arrow data transfer.

        Requires an ADBC-capable cursor (Snowflake, Databricks, or DuckDB
        pool connections). Not supported by non-ADBC cursors.

        Returns:
            ``pyarrow.Table`` with the query results.

        Raises:
            SemolinaMissingDependencyError: If pyarrow is not installed. ADBC builds this
                table through a ``pyarrow.RecordBatchReader``; without pyarrow it raises its
                own ``ProgrammingError("This API requires PyArrow to be installed")``, which
                names neither Semolina nor the extra that fixes it.
            SemolinaResultConsumedError: If the result is already being read another way.
            AttributeError: If the underlying cursor does not support
                ``fetch_arrow_table()`` (e.g. a non-ADBC cursor).

        Example:
            .. code-block:: python

                cursor = Sales.query().metrics(Sales.revenue).execute()
                table = cursor.fetch_arrow_table()
                df = table.to_pandas()
        """
        _require("pyarrow", "pyarrow")
        self._reading = _claim_read(self._reading, "fetch_arrow_table()")
        return self._cursor.fetch_arrow_table()

    def fetch_record_batch(self) -> pyarrow.RecordBatchReader:
        """
        Fetch the result as a PyArrow ``RecordBatchReader`` (ADBC passthrough).

        Delegates to the underlying ADBC cursor's ``fetch_record_batch()``
        method for lazy, memory-bounded streaming consumption of Arrow data.

        Requires an ADBC-capable cursor (Snowflake, Databricks, or DuckDB
        pool connections). Not supported by non-ADBC cursors.

        The reader is this cursor's way of reading its result: once it is
        taken, ``fetchone()``, ``fetch_arrow_table()``, iterating the cursor
        and the rest raise :class:`~semolina.exceptions.SemolinaResultConsumedError`.
        A repeat call returns the same reader, which the cursor keeps so
        ``close()`` can close it.

        The cursor must outlive the reader: consume the reader inside the
        context manager (or before ``.close()``). See arrow-adbc issue #1893.

        Returns:
            ``pyarrow.RecordBatchReader`` over the query result.

        Raises:
            SemolinaMissingDependencyError: If pyarrow is not installed. The reader *is* a
                pyarrow object; ADBC calls its own ``_requires_pyarrow()`` here and raises a
                ``ProgrammingError`` that names neither Semolina nor the extra.
            SemolinaResultConsumedError: If the result is already being read another way.
            AttributeError: If the underlying cursor does not support
                ``fetch_record_batch()`` (e.g. a non-ADBC cursor).

        Example:
            .. code-block:: python

                with Sales.query().metrics(Sales.revenue).execute() as cursor:
                    reader = cursor.fetch_record_batch()
                    for batch in reader:
                        process(batch)
        """
        _require("pyarrow", "pyarrow")
        self._reading = _claim_read(self._reading, "fetch_record_batch()")
        return self._open_reader()

    def _open_reader(self) -> pyarrow.RecordBatchReader:
        """
        Return the cursor's one reader, creating it on first use.

        Shared by :meth:`fetch_record_batch`, iteration and :meth:`iter_into`, each of which
        has already claimed the result; recorded so :meth:`close` can close it.

        Returns:
            The underlying ADBC cursor's ``RecordBatchReader``.
        """
        if self._reader is None:
            self._reader = self._cursor.fetch_record_batch()
        return self._reader

    def fetch_df(self) -> pandas.DataFrame:
        """
        Fetch all remaining rows as a pandas ``DataFrame`` (ADBC passthrough).

        Delegates to the underlying ADBC cursor's ``fetch_df()``, which reads the result
        through a pyarrow reader — ``self.reader.read_pandas()`` — so this path needs **both**
        pyarrow and pandas. Semolina converts nothing itself: ADBC's implementation is already
        cancellation-aware, so a long fetch stays interruptible.

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
                pandas.
            SemolinaResultConsumedError: If the result is already being read another way.
            AttributeError: If the underlying cursor does not support ``fetch_df()``
                (e.g. a non-ADBC cursor).

        Example:
            .. code-block:: python

                with Sales.query().metrics(Sales.revenue).execute() as cursor:
                    df = cursor.fetch_df()
                df.head()
        """
        # pyarrow first: ADBC's `fetch_df` is `self.reader.read_pandas()`, and the `reader`
        # property calls its own `_requires_pyarrow()` before pandas is ever imported. Guarding
        # pandas first would let ADBC's ProgrammingError win on a pyarrow-less install.
        _require("pyarrow", "pyarrow")
        _require("pandas", "pandas")
        self._reading = _claim_read(self._reading, "fetch_df()")
        return self._cursor.fetch_df()

    def fetch_polars(self) -> polars.DataFrame:
        """
        Fetch all remaining rows as a polars ``DataFrame`` (ADBC passthrough).

        Delegates to the underlying ADBC cursor's ``fetch_polars()``, which hands polars the
        result's raw Arrow PyCapsule stream — ``polars.from_arrow(self.fetch_arrow())`` — and
        never touches pyarrow. This method is therefore guarded on polars **only**: requiring
        pyarrow here would refuse a call that works on a polars-and-no-pyarrow install.

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
                ``import polars`` inside the fetch, so without this guard the caller gets a
                ``ModuleNotFoundError`` raised several frames deep in someone else's module.
            SemolinaResultConsumedError: If the result is already being read another way.
            AttributeError: If the underlying cursor does not support ``fetch_polars()``
                (e.g. a non-ADBC cursor).

        Example:
            .. code-block:: python

                with Sales.query().metrics(Sales.revenue).execute() as cursor:
                    df = cursor.fetch_polars()
                df.head()
        """
        # polars only, deliberately. ADBC's `fetch_polars` is
        # `polars.from_arrow(self.fetch_arrow())` over the raw PyCapsule stream: no reader is
        # built, so `_requires_pyarrow()` is never reached and pyarrow need not be installed.
        _require("polars", "polars")
        self._reading = _claim_read(self._reading, "fetch_polars()")
        return self._cursor.fetch_polars()

    # -- Typed results --

    def into(self, model: type[_M], *, validate: bool = False) -> list[_M]:
        """
        Convert the whole result into a list of Pydantic model instances.

        Columns are matched to fields by name, resolved through arrowmodel's own key rule —
        ``validation_alias``, then ``alias``, then the field name. Result columns the model
        does not declare are ignored, so one DTO can serve several queries. A declared field
        with no matching column is an error unless it carries a default.

        Any Pydantic ``BaseModel`` subclass works; inheriting from ``arrowmodel.ArrowModel``
        is not required and buys nothing here.

        ``validate`` selects between two coherent behaviours, and the schema check follows it
        (see :func:`semolina.dto.check_result_schema`):

        - ``validate=False`` (default) — **types must match.** Instances are built with
          ``model_construct``, which converts nothing, so an annotation that disagreed with
          its column would leave a wrong-typed value sitting in the field. The structural
          check runs before any row moves and raises
          :class:`~semolina.exceptions.SemolinaSchemaMismatchError` instead, naming every offending
          field at once. Nothing is coerced; nothing is per-value; the cost is one schema
          comparison.
        - ``validate=True`` — **types are coerced.** Pydantic's full pipeline runs per row at
          roughly 2-5x the cost, converting where it legally can (``decimal128`` into a
          ``float`` field, ``int64`` into ``float``) and raising ``ValidationError`` where it
          cannot (a fractional ``decimal128`` into ``int``). The structural type comparison
          is skipped, because Pydantic owns the decision and refusing first would block
          narrowings that work.

        Column *presence* is checked on both settings — a required field with no matching
        column is an error either way, since no amount of coercion invents a column.

        Reads the whole result, like ``fetch_arrow_table()``, so any other way of reading the
        same cursor afterwards raises :class:`~semolina.exceptions.SemolinaResultConsumedError`.

        Args:
            model: The Pydantic model to build. Any ``type[BaseModel]``.
            validate: Coerce values to the declared annotations through Pydantic's per-row
                pipeline, instead of the fast path's exact-type contract. Passed straight
                through to arrowmodel. Defaults to False.

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


                with Sales.query().metrics(Sales.total_order_value).execute() as cursor:
                    rows = cursor.into(SalesDTO)
                # [SalesDTO(region='US', total_order_value=Decimal('43.25')), ...]
        """
        # pyarrow first: reading `description` without it raises ADBC's own ProgrammingError
        # from a _NoOpBackend, which names neither Semolina nor the extra that fixes it.
        _require("pyarrow", "pyarrow")
        _require("arrowmodel", "arrowmodel")

        from .dto import check_result_schema

        check_result_schema(self.description, model, check_types=not validate)
        self._reading = _claim_read(self._reading, "into()")

        from arrowmodel import model_convert

        # arrowmodel types model_convert as `-> list[BaseModel]`, which loses the concrete
        # model. A cast recovers it without a suppression comment.
        return cast(
            "list[_M]",
            model_convert(model, self._cursor.fetch_arrow_table(), validate=validate),
        )

    def iter_into(self, model: type[_M], *, validate: bool = False) -> Iterator[_M]:
        """
        Stream the result as Pydantic model instances, one at a time.

        The streaming sibling of :meth:`into`. Instances are produced individually, but
        conversion happens a whole Arrow batch at a time: consuming one instance pulls exactly
        one batch from the underlying reader, so a result larger than memory is never
        materialized. Column matching, alias resolution and the treatment of extra or
        defaulted fields are identical to :meth:`into` — only the delivery differs.

        **The schema check happens at the call, not on first iteration.** ``iter_into`` is a
        plain method that validates and then returns a generator, rather than a generator
        function whose body would not run until the first ``next()``. That distinction is the
        whole point: arrowmodel's own ``Model.iter(batch)`` is a generator function, so a bad
        DTO handed to it returns quietly and blows up several frames away, inside whatever
        loop or comprehension eventually consumed it. Here a mismatched DTO raises on the
        ``iter_into(...)`` expression itself, with the traceback pointing at the line that
        named the wrong type, and before any reader is created or any batch moves.

        The returned iterator is this cursor's way of reading its result: any other way,
        :meth:`fetch_record_batch` included, then raises
        :class:`~semolina.exceptions.SemolinaResultConsumedError`. The cursor must also outlive
        the iterator: consume it inside the context manager (or
        before ``close()``). See arrow-adbc issue #1893.

        Args:
            model: The Pydantic model to build. Any ``type[BaseModel]``.
            validate: Run Pydantic validation per row instead of the fast path. Set on the
                converter once and reused for every batch. Defaults to False. Read
                :meth:`into`'s note first — ``validate=True`` coerces instead of
                requiring exact types.

        Returns:
            An ``Iterator`` yielding one ``model`` instance per result row. Empty for a
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
                an Arrow ``map`` column. Raised on the first ``next()``.
            ValueError: From arrowmodel, naming a required column it could not find. The
                check above pre-empts this whenever it has a schema to read, so it survives
                only where ``description`` was ``None``. Raised on the first ``next()``.

        Example:
            .. code-block:: python

                import decimal

                import pydantic


                class SalesDTO(pydantic.BaseModel):
                    region: str
                    total_order_value: decimal.Decimal


                with Sales.query().metrics(Sales.total_order_value).execute() as cursor:
                    for dto in cursor.iter_into(SalesDTO):
                        process(dto)
        """
        # Same order as `into`: pyarrow first, because reading `description` without it raises
        # ADBC's own ProgrammingError from a _NoOpBackend, naming neither Semolina nor the
        # extra that fixes it.
        _require("pyarrow", "pyarrow")
        _require("arrowmodel", "arrowmodel")

        from .dto import check_result_schema

        check_result_schema(self.description, model, check_types=not validate)
        self._reading = _claim_read(self._reading, "iter_into()")

        # This method contains no `yield`, deliberately. Everything above has already run by
        # the time the caller holds the iterator; everything lazy lives in the generator
        # function below.
        return self._iter_into_impl(model, validate=validate)

    def _iter_into_impl(self, model: type[_M], *, validate: bool) -> Iterator[_M]:
        """
        Yield model instances batch by batch — the lazy half of :meth:`iter_into`.

        Private because its laziness is the thing :meth:`iter_into` exists to wrap: called
        directly it would skip the dependency guards and the schema pre-check, which is
        precisely the timing D-05 forbids.

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
            reader = self._open_reader()
        except OSError:
            # Some drivers report an already-drained result when the reader is created rather
            # than on the first pull; `__next__` normalizes the same case, as does the async
            # twin. Yielding nothing is what every other consumer of a drained stream does.
            return
        while True:
            try:
                batch = reader.read_next_batch()
            except StopIteration:
                # A generator must not let StopIteration escape its body (PEP 479 turns that
                # into a RuntimeError), so terminate explicitly.
                return
            except OSError:
                # ADBC drivers may surface drained-reader access as OSError rather than
                # StopIteration; normalized to termination, as `__next__` does.
                return
            if batch.num_rows == 0:
                # Mirrors `__next__`: an empty batch is a hole in the stream, not its end.
                continue
            # arrowmodel types `iter` as `-> Iterator[BaseModel]`, losing the concrete model.
            # A cast recovers it without a suppression comment. Never hand the reader itself
            # to arrowmodel: a RecordBatchReader is rejected with
            # `ValueError: Expected an object with dunder __arrow_c_array__`.
            yield from cast("Iterator[_M]", converter.iter(batch))

    # -- DBAPI 2.0 passthrough properties --

    @property
    def description(self) -> list[tuple[Any, ...]] | None:
        """
        Cursor description passthrough.

        Returns:
            List of 7-element tuples describing columns, or None before execute.
        """
        return self._cursor.description

    @property
    def rowcount(self) -> int:
        """
        Row count passthrough.

        Returns:
            Number of rows affected by the last operation.
        """
        return self._cursor.rowcount

    # -- Iteration --

    def __iter__(self) -> SemolinaCursor:
        """
        Return self — SemolinaCursor is its own iterator.

        Single-pass: the cursor's underlying ADBC stream is consumed once.
        Re-iterating an exhausted cursor yields zero rows (matches DBAPI
        ``fetchone() -> None`` semantics on a drained cursor). Iteration
        does NOT auto-close the cursor — call ``close()`` or use the
        context manager.

        Returns:
            ``self`` for use in ``for row in cursor:`` syntax.
        """
        return self

    def __next__(self) -> Row:
        """
        Return the next row from the underlying RecordBatchReader.

        Lazily pulls batches one at a time from the underlying reader,
        yielding ``Row`` objects from the current batch. Zero-row batches
        are skipped (mirrors ADBC's own ``_RowIterator.fetchone()``).

        Raises:
            StopIteration: When the underlying reader is exhausted and the
                current batch is fully consumed. Also raised on re-iteration
                of an exhausted cursor. Does NOT close the cursor.
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
        self._reading = _claim_read(self._reading, "for row in cursor")
        if self._iteration_finished():
            raise StopIteration
        if self._reader is None:
            try:
                self._open_reader()
            except (StopIteration, OSError) as exc:
                # Some drivers report an already-drained result when the reader
                # is created rather than on the first pull. Treat as empty.
                self._stream_exhausted = True
                raise StopIteration from exc
        reader = self._reader
        assert reader is not None  # narrowing for the type checker
        while self._needs_batch():
            try:
                batch = reader.read_next_batch()
            except StopIteration:
                self._stream_exhausted = True
                raise
            except OSError as exc:
                # ADBC drivers may surface drained-reader access as OSError
                # rather than StopIteration; normalize to iteration termination.
                self._stream_exhausted = True
                raise StopIteration from exc
            self._buffer_batch(batch)
        return self._next_buffered_row()

    # -- Lifecycle --

    def close(self) -> None:
        """
        Close the reader, then the cursor, then return the connection to its pool.

        The same order and the same failure handling as the async twin's ``aclose()``. Each
        step is suppressed narrowly — ``Exception``, not ``BaseException`` — so a close that
        fails cannot keep the connection out of the pool, and cannot replace the exception a
        ``with`` body raised. A connection that fails to close is a pool slot that never comes
        back, so that one is reported as a ``ResourceWarning``. Idempotent: the closed flag is
        set first, so a second call is a no-op.
        """
        if self._closed:
            return
        self._closed = True
        if self._reader is not None:
            with contextlib.suppress(Exception):
                self._reader.close()
        with contextlib.suppress(Exception):
            self._cursor.close()
        try:
            self._conn.close()
        # Broad on purpose, and still narrower than BaseException: teardown must not mask the
        # caller's error, but it must not hide its own either.
        except Exception as exc:  # noqa: BLE001 -- see the comment above
            warnings.warn(
                self._leaked_connection_message(exc),
                ResourceWarning,
                stacklevel=2,
            )

    def __del__(self) -> None:
        """
        Best-effort finalizer that returns a leaked connection to the pool.

        Safety net for callers that obtain a cursor and neither ``close()`` it
        nor use the context manager: without this, the pooled connection (and
        its ``QueuePool`` slot) would not be reliably reclaimed by GC. Guarded
        against partial ``__init__`` (``_conn``/``_closed`` may be absent if
        construction raised) and double-close, and never raises — finalizers
        must not propagate exceptions. Prefer ``with ....execute() as cursor:``
        or an explicit ``close()``; this only covers the forgotten path.
        """
        if getattr(self, "_closed", True):
            return
        conn = getattr(self, "_conn", None)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
        self._closed = True

    def __enter__(self) -> SemolinaCursor:
        """Enter context manager."""
        return self

    def __exit__(self, *exc: Any) -> None:
        """Exit context manager, closing reader, cursor, and connection."""
        self.close()
