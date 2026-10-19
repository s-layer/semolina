"""
Abstract base class for backend engines.

Defines the SQLAlchemy-style ``Engine`` that owns one adbc-poolhouse
connection pool plus its derived dialect, for all backends (Snowflake,
Databricks, DuckDB). ``connect()`` checks an ADBC connection out of the
owned pool; ``execute()`` runs the builder + cursor path through it.
"""

from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Self

from semolina.exceptions import (
    SemolinaConnectionError,
    SemolinaIntrospectionError,
    SemolinaViewNotFoundError,
    _connecting,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from semolina.cursor import SemolinaCursor
    from semolina.engines.sql import SQLDialect
    from semolina.introspection import IntrospectedView
    from semolina.query import Query

__all__ = [
    "SemolinaViewNotFoundError",
    "SemolinaConnectionError",
    "Engine",
    "_EngineBase",
    "_translate_introspection_errors",
]
"""
The module's interface, ``_EngineBase`` and ``_translate_introspection_errors`` included.

Both are internal to Semolina, not to this file: :mod:`semolina.engines.abase` and
:mod:`semolina.query` import ``_EngineBase``, and the Snowflake and Databricks engines import the
translator. Listing them says so, as :mod:`semolina.exceptions` does for its own cross-module
helpers. Neither is re-exported from the package.
"""


class _EngineBase:
    """
    State the synchronous and asynchronous engines share, and nothing that blocks or awaits.

    :class:`Engine` and :class:`~semolina.engines.abase.AsyncEngine` are siblings: neither is
    a subclass of the other, because one owns a synchronous pool and the other an
    ``AsyncPool``, and their ``connect``/``dispose`` differ in whether they are awaited. What
    they do have in common lives here -- the owned pool, the dialect, the source config, and
    the registry name the factory recorded -- so code that only needs those (a query asking
    which dialect a registered engine renders for) can name one type for both.

    Not public: build engines with :func:`semolina.config.create_engine` or
    :func:`semolina.config.create_async_engine`, and subclass :class:`Engine` for a custom
    one.
    """

    dialect: SQLDialect
    """The :class:`~semolina.engines.sql.SQLDialect` this engine generates SQL with."""

    def __init__(self, *, pool: Any, dialect: SQLDialect, config: Any = None) -> None:
        """
        Store the owned pool, its derived dialect, and the source config.

        Args:
            pool: The adbc-poolhouse pool, synchronous or async, this engine owns.
            dialect: Concrete :class:`~semolina.engines.sql.SQLDialect` selected from the
                config type.
            config: The adbc-poolhouse warehouse config the pool was built from, or None.
        """
        self._pool = pool
        self.dialect = dialect
        self._config = config
        self._registered_as: str | None = None
        """
        Registry name this engine was registered under by its factory's ``register=``.

        Set only by that call, and read only when the engine's ``with`` block exits, so
        leaving the block undoes exactly what building the engine did. A registration made by
        calling :func:`semolina.registry.register` yourself is yours to undo -- the engine does
        not track it, because it cannot know which of several names you would want dropped.
        """

    def _drop_registration(self, unregister: Callable[[str, Self], None]) -> None:
        """
        Drop the name the factory registered this engine under, if it registered one.

        ``unregister`` removes the name only while it still points at this engine, so a
        replacement registered under the same name mid-block is left alone. The sync and async
        registries are separate stores; each engine passes the function for its own.

        Args:
            unregister: :func:`semolina.registry.unregister` or
                :func:`semolina.registry.unregister_async_engine`, called with the name and
                this engine.
        """
        if self._registered_as is not None:
            unregister(self._registered_as, self)
            self._registered_as = None


class Engine(_EngineBase, ABC):
    """
    SQLAlchemy-style engine owning one ADBC pool plus its derived dialect.

    An ``Engine`` is the single handle used for both query execution and
    semantic-view introspection (codegen). It owns exactly one adbc-poolhouse
    pool and the :class:`~semolina.engines.sql.SQLDialect` derived from the
    config type. ``connect()`` checks an ADBC connection out of the owned pool
    (the SQLAlchemy ``Engine.connect()`` parallel); ``execute()`` builds the
    dialect-specific SQL and runs it through a pooled connection, returning a
    :class:`~semolina.cursor.SemolinaCursor`.

    Engines are constructed via :func:`semolina.config.create_engine`, which
    selects the right subclass and supplies the pool and dialect. Subclasses
    implement only backend-specific :meth:`introspect`.

    A custom engine for ``semolina codegen --backend dotted.path.ClassName`` is built with
    no arguments, so its ``__init__`` takes none and calls ``super().__init__(pool=...,
    dialect=...)`` with a pool and :class:`~semolina.engines.sql.SQLDialect` it makes
    itself. It implements :meth:`introspect`, raising the errors that method documents so
    the CLI picks the right exit code.

    Each Engine:
    - Owns one ADBC connection pool and its dialect
    - Executes queries through the owned pool via ``SemolinaCursor``
    - Introspects semantic views into an intermediate representation

    Example:
        .. code-block:: python

            from adbc_poolhouse import SnowflakeConfig

            from semolina.config import create_engine

            engine = create_engine(SnowflakeConfig(account="xy12345", user="u"))
            view = engine.introspect("my_schema.sales_view")

    See Also:
        - semolina.config.create_engine: Builds an Engine from a config or name
        - semolina.engines.sql.SQLDialect: Backend-specific SQL generation rules
        - semolina.engines.sql.SnowflakeDialect: Snowflake-specific dialect
        - semolina.engines.sql.DatabricksDialect: Databricks-specific dialect
        - semolina.engines.sql.DuckDBDialect: DuckDB-specific dialect
    """

    def __init__(self, *, pool: Any, dialect: SQLDialect, config: Any = None) -> None:
        """
        Store the owned ADBC pool, its derived dialect, and the source config.

        Args:
            pool: The adbc-poolhouse connection pool this engine owns. Typed as
                ``Any`` because the poolhouse/SQLAlchemy pool surface is untyped.
            dialect: Concrete :class:`~semolina.engines.sql.SQLDialect` selected
                from the config type by :func:`semolina.config.create_engine`.
            config: The adbc-poolhouse warehouse config the pool was built from
                (``SnowflakeConfig`` etc.). Held so introspectors can read
                connection metadata (e.g. the Snowflake database for view-name
                qualification) without re-reading the TOML. Typed as ``Any``
                because the union of poolhouse config classes is untyped here.
        """
        super().__init__(pool=pool, dialect=dialect, config=config)

    def __enter__(self) -> Engine:
        """
        Enter a scope that owns this engine's teardown.

        Returns:
            This engine, so ``with create_engine(...) as engine:`` binds it.
        """
        return self

    def __exit__(self, *exc: object) -> None:
        """
        Unregister the engine if this call registered it, then dispose the pool.

        Unregistration comes first so that nothing can resolve a name whose pool is
        already closing: a disposed engine left in the registry is still reachable and
        fails deep inside the driver rather than at lookup.

        The name is dropped only while it still points at *this* engine. Code that swaps
        the engine mid-block leaves the name held by the replacement, and removing it
        regardless would take that replacement down as collateral.

        Disposal runs even if unregistration raises, and even when the block is leaving
        by exception -- the pool is the OS-level resource, so it is the one teardown that
        must not be skipped. Exceptions are not suppressed.
        """
        try:
            from semolina.registry import unregister

            self._drop_registration(unregister)
        finally:
            self.dispose()

    def connect(self) -> Any:
        """
        Check an ADBC connection out of the owned pool.

        The SQLAlchemy ``Engine.connect()`` parallel. The returned poolhouse
        connection supports **two** consumption modes, both of which return the
        slot to the pool exactly once:

        - **Context-manager (one-shot):** ``with self.connect() as conn:`` —
          the connection is returned to the pool on block exit. Used by
          :meth:`introspect` in each backend.
        - **Long-lived handle:** keep the bare return value alive past this call
          and return it to the pool with an explicit ``conn.close()``. Used by
          :meth:`execute`, which hands the live connection to a
          :class:`~semolina.cursor.SemolinaCursor` that closes it on
          ``SemolinaCursor.close()``. ``execute`` is responsible for closing the
          connection on its own error path (so a failed ``cursor()``/``execute()``
          does not leak the slot) — see :meth:`execute`.

        Callers must use exactly one mode per checkout; do not both enter the
        context manager and retain the handle.

        Returns:
            An ADBC DBAPI connection checked out of the owned pool. Typed as
            ``Any`` because the poolhouse/ADBC connection surface is untyped.

        Raises:
            SemolinaConnectionError: If the driver cannot open a connection.
        """
        with _connecting("Could not connect to the warehouse"):
            return self._pool.connect()

    def dispose(self) -> None:
        """
        Tear down the owned ADBC pool and release its driver resources.

        The public counterpart to :func:`semolina.config.create_engine`: closes
        the pool this engine owns. adbc-poolhouse pools are closed via its
        ``close_pool`` so the underlying driver resources are released: an ADBC
        pool (Snowflake, Databricks, DuckDB) carries its ``_adbc_source``, and a
        pool over a non-ADBC backend (``DatabricksPythonConfig``) its
        ``_native_backend``. Any other pool falls back to a plain ``pool.close()``.
        Idempotent only insofar as the underlying pool's ``close`` is — callers
        should dispose an engine once.

        This is the single sanctioned teardown path; :func:`semolina.registry.reset` and
        test fixtures call it instead of reaching into ``engine._pool``.
        """
        pool = self._pool
        if hasattr(pool, "_adbc_source") or hasattr(pool, "_native_backend"):
            from adbc_poolhouse import close_pool

            close_pool(pool)
        else:
            pool.close()

    def execute(self, query: Query) -> SemolinaCursor:
        """
        Execute a query through the owned pool and return a cursor.

        Builds dialect-specific parameterized SQL, checks an ADBC connection
        out of the owned pool, executes the statement, and wraps the resulting
        cursor in a :class:`~semolina.cursor.SemolinaCursor` (passing the live
        connection and owning pool so Arrow allocators are released on checkin).

        Args:
            query: ``Query`` object to execute. Must be valid for execution
                (has metrics and/or dimensions).

        Returns:
            A :class:`~semolina.cursor.SemolinaCursor` wrapping the post-execute
            ADBC cursor. Use ``fetchall_rows()`` for Row objects or
            ``fetchall()`` for raw tuples.

        Raises:
            ValueError: If query is invalid for execution.
            SemolinaConnectionError: If no connection can be checked out of the pool.
            adbc_driver_manager.Error: If the warehouse rejects the statement, passed
                through unchanged.

        Example:
            .. code-block:: python

                with engine.execute(query) as cursor:
                    for row in cursor.fetchall_rows():
                        print(row["country"], row["revenue"])
        """
        from semolina.cursor import SemolinaCursor

        # Validated here as well as in ``Query.execute()``: a caller holding the engine can
        # pass a query straight in, and the builder below would otherwise fail on its internal
        # assertion, which ``python -O`` strips. Checked before ``connect()``, so an invalid
        # query never takes a pool slot.
        query._validate_for_execution()

        builder = self.dialect.create_builder()
        sql, params = builder.build_select_with_params(query)

        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
        except BaseException:
            # Return the checked-out connection to the pool before propagating.
            # Otherwise (cursor()/execute() failures, or cancellation) the slot
            # is leaked, since checkin normally happens only via
            # SemolinaCursor.close() on the success path. Mirrors
            # SemolinaCursor.close()'s ``self._conn.close()``.
            conn.close()
            raise

        return SemolinaCursor(cur, conn)

    @abstractmethod
    def introspect(self, view_name: str) -> IntrospectedView:
        """
        Introspect a semantic view and return its intermediate representation.

        Queries the warehouse metadata API to discover the fields (metrics,
        dimensions, facts) defined on the named semantic view. The returned
        ``IntrospectedView`` is consumed by the Python code renderer to generate
        a SemanticView subclass.

        Args:
            view_name: Warehouse identifier for the semantic view to introspect
                (e.g., ``'sales_view'``). Must exist in the warehouse.

        Returns:
            ``IntrospectedView`` containing the view name, derived class name,
            and all discovered fields with their types and descriptions.

        Raises:
            NotImplementedError: If the engine does not support introspection.
            SemolinaViewNotFoundError: If the warehouse has no view by that name.
            SemolinaConnectionError: If the engine cannot connect or authenticate.
            SemolinaIntrospectionError: If the warehouse was reached but could not
                describe the view, for example because its definition no longer binds.
                Like the two above, it is a ``RuntimeError``.

        Example:
            .. code-block:: python

                view = engine.introspect("sales_view")
                # IntrospectedView(view_name='sales_view', class_name='Sales', ...)
        """
        pass


@contextlib.contextmanager
def _translate_introspection_errors(backend: str) -> Generator[None]:
    """
    Report an ADBC error raised while introspecting as the Semolina error it means.

    Shared by the Snowflake and Databricks engines, whose drivers classify failures the same
    way. DuckDB's driver does not, so its engine matches on the message instead.

    Args:
        backend: The warehouse's display name, which starts each message.

    Yields:
        Nothing; the block runs inside the translation.

    Raises:
        SemolinaViewNotFoundError: For an ADBC ``ProgrammingError`` (invalid view, SQL
            syntax).
        SemolinaConnectionError: For an ADBC ``OperationalError`` or ``InterfaceError``.
        SemolinaIntrospectionError: For any other ADBC ``Error``.
    """
    from adbc_driver_manager import (
        Error,
        InterfaceError,
        OperationalError,
        ProgrammingError,
    )

    try:
        yield
    except ProgrammingError as e:
        # SQL errors, invalid view name, view does not exist
        msg = f"{backend} view not found or inaccessible: {e}"
        raise SemolinaViewNotFoundError(msg) from e

    except (OperationalError, InterfaceError) as e:
        # Connection-level failures: network, authentication, a missing driver.
        msg = f"{backend} introspection failed: {e}"
        raise SemolinaConnectionError(msg) from e

    except Error as e:
        # Every other ADBC error: the warehouse was reached but could not describe the
        # view (internal, data or unsupported-operation failures). None escapes
        # untranslated.
        msg = f"{backend} introspection failed: {e}"
        raise SemolinaIntrospectionError(msg) from e
