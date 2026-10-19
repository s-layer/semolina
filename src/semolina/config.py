"""
TOML configuration loading and the ``create_engine`` factory.

Reads ``.semolina.toml`` connection sections (or accepts an adbc-poolhouse
config object directly) and builds an :class:`~semolina.engines.base.Engine`
that owns one ADBC pool plus the dialect derived from the config type.
"""

from __future__ import annotations

import contextlib
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from adbc_poolhouse import (
    DatabricksConfig,
    DatabricksPythonConfig,
    DuckDBConfig,
    SnowflakeConfig,
    create_pool,
)

from .dialect import Dialect, resolve_dialect
from .engines.databricks import DatabricksEngine
from .engines.duckdb import DuckDBEngine
from .engines.snowflake import SnowflakeEngine
from .exceptions import SemolinaExtensionLoadError, _connecting

if TYPE_CHECKING:
    from .engines.abase import AsyncEngine
    from .engines.base import Engine

__all__ = [
    "WarehouseConfig",
    "create_engine",
    "create_async_engine",
    "warehouse_config",
]


# adbc-poolhouse config objects accepted by create_engine.
WarehouseConfig = SnowflakeConfig | DatabricksConfig | DatabricksPythonConfig | DuckDBConfig


@dataclass(frozen=True)
class _Backend:
    """One backend Semolina ships: its adbc-poolhouse config, its engine, its display name."""

    config_cls: type
    engine_cls: type[Engine]
    label: str


_BACKENDS: dict[Dialect, _Backend] = {
    Dialect.SNOWFLAKE: _Backend(SnowflakeConfig, SnowflakeEngine, "Snowflake"),
    Dialect.DATABRICKS: _Backend(DatabricksConfig, DatabricksEngine, "Databricks"),
    Dialect.DUCKDB: _Backend(DuckDBConfig, DuckDBEngine, "DuckDB"),
}
"""
The one table of Semolina's backends, keyed by :class:`Dialect`.

The SQL dialect for each backend is not here: every built-in ``SQLDialect`` names its own
:attr:`~semolina.engines.sql.SQLDialect.kind`, and :func:`~semolina.dialect.resolve_dialect`
reads that. Adding a backend means a ``Dialect`` member, a dialect class and a row here.
"""

_ALTERNATE_CONFIGS: dict[str, tuple[type, Dialect]] = {
    "databricks-python": (DatabricksPythonConfig, Dialect.DATABRICKS),
}
"""
Further adbc-poolhouse configs that reach a backend in ``_BACKENDS``, keyed by TOML ``type``.

``DatabricksPythonConfig`` pools ``databricks-sql-connector`` connections behind poolhouse's
ADBC-shaped adapter instead of the ADBC driver. It is the only route to Databricks
Lakehouse//RT, which rejects the driver's Thrift transport. The warehouse and its SQL are the
same, so it shares the Databricks dialect and engine.
"""

# TOML ``type`` string -> (config class, dialect). Derived from ``_BACKENDS`` and
# ``_ALTERNATE_CONFIGS`` rather than written out, and kept as its own module attribute because
# tests patch it.
_CONFIG_MAP: dict[str, tuple[type, Dialect]] = {
    **{dialect.value: (backend.config_cls, dialect) for dialect, backend in _BACKENDS.items()},
    **_ALTERNATE_CONFIGS,
}


def _load_semantic_views(dbapi_conn: Any, connection_record: Any) -> None:
    """
    Auto-install and load the semantic_views extension on new DuckDB connections.

    Registered as a SQLAlchemy pool ``connect`` event listener. Fires once per
    physical ADBC connection creation. ``INSTALL`` is idempotent (no-op when
    cached at ``~/.duckdb/extensions/``). ``LOAD`` activates the extension in
    the current connection session.

    Raises:
        SemolinaExtensionLoadError: If DuckDB cannot install or load the extension.
    """
    import adbc_driver_manager

    cur = dbapi_conn.cursor()
    try:
        cur.execute("INSTALL semantic_views FROM community")
        cur.execute("LOAD semantic_views")
    except adbc_driver_manager.Error as e:
        msg = (
            f"Could not install or load the DuckDB semantic_views extension: {e}. The first "
            "INSTALL downloads it from the DuckDB community repository, so it needs network "
            "access, and the build must match your DuckDB version."
        )
        raise SemolinaExtensionLoadError(msg) from e
    finally:
        # An open statement stops the connection closing, and the pool closes it on failure.
        cur.close()


def _inner_sync_pool(pool: Any) -> Any:
    """
    Return the synchronous pool an adbc-poolhouse ``AsyncPool`` wraps.

    Two things Semolina must do synchronously need that inner pool: attaching
    the DuckDB ``semantic_views`` connect listener (an ``AsyncPool`` is a plain
    wrapper, not a SQLAlchemy event target) and tearing a pool down from
    :func:`semolina.registry.reset`, which cannot await ``AsyncPool.close()``.

    Neither has a supported route to it. ``AsyncPool`` publishes ``connect()``
    and ``close()`` and nothing else, so ``_pool`` is undocumented coupling
    rather than merely private-by-convention, and Semolina's ``adbc-poolhouse``
    floor carries no upper bound. This function exists so a poolhouse release
    that renames the attribute fails with a sentence describing what broke,
    rather than a bare ``AttributeError`` raised somewhere unhelpful.

    Args:
        pool: An adbc-poolhouse ``AsyncPool``. Typed as ``Any`` because that
            class is not a public importable name.

    Returns:
        The inner synchronous pool, which *is* a SQLAlchemy pool.

    Raises:
        RuntimeError: If the pool no longer exposes an inner synchronous pool.
    """
    inner = getattr(pool, "_pool", None)
    if inner is None:
        raise RuntimeError(
            "This adbc-poolhouse release's AsyncPool no longer exposes the inner "
            "synchronous pool Semolina needs to attach connect listeners and to "
            "close a pool without awaiting. Pin an adbc-poolhouse version that "
            "still does, or report the missing public accessor upstream."
        )
    return inner


def _expand_private_key_path(config: Any) -> Any:
    """
    Expand ``~`` in a Snowflake config's ``private_key_path``.

    The native snowflake-connector expands ``~`` itself, but the Go-based
    Snowflake ADBC driver opens the path literally and fails with "could not read
    private key file" on a ``~/...`` path. Expanding here makes both work. Configs
    without a ``private_key_path`` field (Databricks, DuckDB) pass through.
    """
    key_path = getattr(config, "private_key_path", None)
    if key_path is not None:
        return config.model_copy(update={"private_key_path": Path(key_path).expanduser()})
    return config


def _resolve_section(
    connection: str,
    config_path: str | Path,
) -> tuple[type, Dialect, dict[str, Any]]:
    """
    Read and validate a ``[connections.<name>]`` section from the TOML file.

    The single TOML-reading + validation helper shared by the name-dispatch
    arm of :func:`create_engine` (via :func:`_read_connection`): opens the file,
    looks up the named section, pops and validates the ``type`` field, and
    resolves it to a config class + dialect. The remaining section fields are
    returned untouched for the caller to instantiate the config class.

    Args:
        connection: Name of the ``[connections.<name>]`` section.
        config_path: Path to the TOML config file.

    Returns:
        Tuple of ``(config_cls, Dialect, section_fields)`` where
        ``section_fields`` excludes the consumed ``type`` key.

    Raises:
        FileNotFoundError: If the config file does not exist.
        KeyError: If the named connection section is not found.
        ValueError: If the ``type`` field is missing or unsupported.
    """
    path = Path(config_path)
    with path.open("rb") as f:
        data = tomllib.load(f)

    connections: dict[str, Any] = data.get("connections", {})
    if connection not in connections:
        available = list(connections.keys())
        raise KeyError(
            f"Connection '{connection}' not found in {config_path}. "
            f"Available connections: {available}"
        )

    section = dict(connections[connection])
    conn_type = section.pop("type", None)
    if conn_type is None:
        raise ValueError(
            f"Connection '{connection}' in {config_path} is missing "
            "required 'type' field (e.g. type = \"snowflake\")"
        )

    if conn_type not in _CONFIG_MAP:
        supported = list(_CONFIG_MAP.keys())
        raise ValueError(f"Unsupported connection type '{conn_type}'. Supported types: {supported}")

    config_cls, dialect = _CONFIG_MAP[conn_type]
    return config_cls, dialect, section


def _dialect_for_config_type(config: Any) -> Dialect:
    """
    Reverse-look up the :class:`Dialect` for an adbc-poolhouse config object.

    Built off the same ``_CONFIG_MAP`` that the TOML path uses, keyed by the
    config *class* rather than the ``type`` string, so a config object and its
    equivalent ``[connections.<name>]`` section resolve to the same dialect.

    The lookup matches on the exact ``type(config)`` rather than scanning with
    ``isinstance``, so it cannot silently pick the wrong dialect if a future
    config class subclasses another or ``_CONFIG_MAP`` insertion order changes.

    Args:
        config: An adbc-poolhouse config instance (``SnowflakeConfig`` etc.).

    Returns:
        The :class:`Dialect` enum value for the config's backend.

    Raises:
        ValueError: If the config type is not a supported backend.
    """
    dialect_by_cls = {cls: dialect for cls, dialect in _CONFIG_MAP.values()}
    dialect = dialect_by_cls.get(type(config))
    if dialect is None:
        supported = [cls.__name__ for cls, _ in _CONFIG_MAP.values()]
        raise ValueError(
            f"Unsupported config type '{type(config).__name__}'. Supported configs: {supported}"
        )
    return dialect


def _engine_cls_for_dialect(dialect: Dialect) -> type[Engine]:
    """
    Select the backend ``Engine`` subclass for a dialect.

    Reads :data:`_BACKENDS`, the one backend table, so callers never pick an Engine
    subclass by hand.

    Args:
        dialect: The :class:`Dialect` derived from the config type.

    Returns:
        The concrete ``Engine`` subclass for the backend.
    """
    return _BACKENDS[dialect].engine_cls


def _registration_name(register: bool | str, config: WarehouseConfig | str) -> str | None:
    """
    Resolve the registry name a ``register=`` argument asks for.

    One rule covers every call shape: the registration name is the connection name, and
    the connection name defaults to ``"default"``. A config object carries no connection
    name -- that is the form the tutorials use so they need no TOML file -- so it falls
    back to the same ``"default"`` that :func:`semolina.registry.get_engine` resolves when
    called with no argument.

    Args:
        register: ``False`` for no registration, ``True`` to reuse the connection name,
            or an explicit name.
        config: The config object or connection name passed to the factory.

    Returns:
        The name to register under, or ``None`` when no registration was asked for.
    """
    if register is False:
        return None
    if register is True:
        return config if isinstance(config, str) else "default"
    return register


def create_engine(
    config: WarehouseConfig | str = "default",
    *,
    register: bool | str = False,
    config_path: str | Path = ".semolina.toml",
) -> Engine:
    """
    Build an :class:`~semolina.engines.base.Engine` from a config object or name.

    The single public construction entry point (the SQLAlchemy
    ``create_engine`` parallel). Accepts **either** an adbc-poolhouse config
    object (``SnowflakeConfig(...)`` / ``DatabricksConfig(...)`` /
    ``DatabricksPythonConfig(...)`` / ``DuckDBConfig(...)``) **or** a
    ``.semolina.toml`` connection name. It
    creates one ADBC pool, derives the dialect from the config type, wires the
    DuckDB ``semantic_views`` connect listener when applicable, and returns the
    matching backend Engine subclass owning that pool and dialect.

    Args:
        config: An adbc-poolhouse config object, or the name of a
            ``[connections.<name>]`` section in ``.semolina.toml`` (defaults to
            ``"default"``). There is no URL-string form.
        register: Register the new engine in one step. ``True`` registers it under the
            connection name -- the section name you passed, or ``"default"`` for a config
            object, which has no section. A string registers it under that name instead.
            The engine remembers the name only so that leaving a ``with`` block undoes
            this registration; see :meth:`~semolina.engines.base.Engine.__exit__`.
        config_path: Path to the TOML config file. Only consulted when ``config``
            is a connection-name string.

    Returns:
        An :class:`~semolina.engines.base.Engine` owning one ADBC pool plus the
        dialect derived from the config type.

    Raises:
        FileNotFoundError: If a connection name is given but the config file
            does not exist.
        KeyError: If the named connection section is not found.
        ValueError: If the connection ``type`` (or config object type) is
            missing or unsupported, or if ``register`` names an engine that is
            already registered.
        SemolinaConnectionError: If the pool's first connection cannot be opened.

    Example:
        .. code-block:: python

            from adbc_poolhouse import SnowflakeConfig

            from semolina.config import create_engine

            engine = create_engine(SnowflakeConfig(account="xy12345", user="u"))
            # or, reading [connections.default] from .semolina.toml:
            engine = create_engine("default")

        Build, register and scope teardown in one statement:

        .. code-block:: python

            with create_engine("analytics", register=True):
                ...  # registered as "analytics" for the block

        On exit the name is unregistered and the pool disposed, in that order.
    """
    if isinstance(config, str):
        wh_config, dialect = _read_connection(config, config_path)
    else:
        wh_config = _expand_private_key_path(config)
        dialect = _dialect_for_config_type(config)

    with _connecting("Could not connect to the warehouse"):
        pool = create_pool(wh_config)

    if dialect is Dialect.DUCKDB:
        from sqlalchemy import event

        event.listen(pool, "connect", _load_semantic_views)

    dialect_instance = resolve_dialect(dialect)
    engine_cls = _engine_cls_for_dialect(dialect)
    engine = engine_cls(pool=pool, dialect=dialect_instance, config=wh_config)

    name = _registration_name(register, config)
    if name is not None:
        from .registry import register as _register

        # Registration comes last, so a duplicate name cannot leave a half-built engine in
        # the registry. But the pool already exists by then, and the caller never receives
        # the engine to close -- so this failure has to release it here or leak it. The
        # ValueError is re-raised either way: it names the clash the caller has to fix.
        try:
            _register(name, engine)
        except Exception:
            # Same narrow suppression as `registry.reset`: a flaky close must not mask the
            # registration error, while a genuine programming error still surfaces (chained
            # onto the ValueError as its context).
            with contextlib.suppress(OSError, RuntimeError):
                engine.dispose()
            raise
        engine._registered_as = name  # noqa: SLF001

    return engine


def create_async_engine(
    config: WarehouseConfig | str = "default",
    *,
    register: bool | str = False,
    config_path: str | Path = ".semolina.toml",
) -> AsyncEngine:
    """
    Build an :class:`~semolina.engines.abase.AsyncEngine` from a config object or name.

    The async counterpart of :func:`create_engine`, and a separate constructor
    rather than a flag on it: the returned engine is a distinct type owning
    exactly one async ADBC pool, so the sync/async choice is fixed at
    construction and cannot be switched on a shared engine.

    It stays a plain ``def``: building the pool opens a connection synchronously, which
    can raise :class:`~semolina.exceptions.SemolinaConnectionError`, but nothing here is
    awaited. Teardown is the asymmetric half: ``await engine.dispose()``.

    Requires the optional async dependencies, installed with
    ``pip install 'semolina[async]'``. A plain ``import semolina`` never pulls
    them in; they are resolved inside this function.

    Args:
        config: An adbc-poolhouse config object, or the name of a
            ``[connections.<name>]`` section in ``.semolina.toml`` (defaults to
            ``"default"``). There is no URL-string form.
        register: Register the new engine in the **async** registry in one step. ``True``
            registers it under the connection name -- the section name you passed, or
            ``"default"`` for a config object, which has no section. A string registers it
            under that name instead. The two registries are separate stores, so this never
            shadows a synchronous engine of the same name. The engine remembers the name
            only so that leaving an ``async with`` block undoes this registration.
        config_path: Path to the TOML config file. Only consulted when ``config``
            is a connection-name string.

    Returns:
        An :class:`~semolina.engines.abase.AsyncEngine` owning one async ADBC
        pool plus the dialect derived from the config type.

    Raises:
        ImportError: If the optional async dependencies are not installed.
        FileNotFoundError: If a connection name is given but the config file
            does not exist.
        KeyError: If the named connection section is not found.
        ValueError: If the connection ``type`` (or config object type) is
            missing or unsupported.
        SemolinaConnectionError: If the pool cannot be created.

    Example:
        .. code-block:: python

            from adbc_poolhouse import DuckDBConfig

            from semolina.config import create_async_engine

            engine = create_async_engine(DuckDBConfig(database="sales.db"))
            async with await engine.aexecute(query) as cursor:
                rows = await cursor.fetchall_rows()
            await engine.dispose()
    """
    try:
        # Deferred so that `import semolina` on a plain install never reaches
        # adbc-poolhouse's async entry points, which would pull in anyio.
        from adbc_poolhouse import create_async_pool
    except ImportError as exc:
        # Static literal, interpolating nothing: poolhouse's own message names
        # its extra (`adbc-poolhouse[async]`), which would send the reader to
        # the wrong package.
        raise ImportError(
            "Async support requires the optional async dependencies. "
            "Install them with: pip install 'semolina[async]'"
        ) from exc

    from .engines.abase import AsyncEngine

    if isinstance(config, str):
        wh_config, dialect = _read_connection(config, config_path)
    else:
        wh_config = _expand_private_key_path(config)
        dialect = _dialect_for_config_type(config)

    # Built from the config object, never from a driver path: the native
    # shared-library form bypasses the Python dbapi module entirely, which would
    # defeat cassette interception in the integration tests.
    with _connecting("Could not connect to the warehouse"):
        pool = create_async_pool(wh_config)

    if dialect is Dialect.DUCKDB:
        from sqlalchemy import event

        # The async pool is a plain wrapper, not a SQLAlchemy event target, so
        # the listener attaches to the inner sync pool it wraps — reached
        # through a guard, because that inner pool is not part of
        # adbc-poolhouse's published surface.
        event.listen(_inner_sync_pool(pool), "connect", _load_semantic_views)

    dialect_instance = resolve_dialect(dialect)
    # No engine-subclass lookup: AsyncEngine is concrete and backend-agnostic,
    # because introspect() is the only method backends specialize and async
    # introspection is deferred.
    engine = AsyncEngine(pool=pool, dialect=dialect_instance, config=wh_config)

    name = _registration_name(register, config)
    if name is not None:
        from adbc_poolhouse import close_pool

        from .registry import register_async_engine

        # The synchronous sibling's cleanup, by the one route available here:
        # ``AsyncEngine.dispose()`` is a coroutine and this factory is a plain ``def``, so
        # the pool is closed inline through the inner synchronous pool it wraps -- the same
        # call ``registry.reset`` makes for the same reason. Reaching that inner pool sits
        # outside the suppression deliberately: a poolhouse release that stopped exposing
        # it is a contract break, not a flaky close.
        try:
            register_async_engine(name, engine)
        except Exception:
            inner_pool = _inner_sync_pool(engine._pool)  # noqa: SLF001
            with contextlib.suppress(OSError, RuntimeError):
                close_pool(inner_pool)
            raise
        engine._registered_as = name  # noqa: SLF001

    return engine


def _read_connection(
    connection: str,
    config_path: str | Path,
) -> tuple[Any, Dialect]:
    """
    Read a ``[connections.<name>]`` section into a (config, Dialect) pair.

    The TOML-reading half of :func:`create_engine`'s name-dispatch arm: reads
    and validates the section via :func:`_resolve_section`, instantiates the
    matching config class, and expands any private key path. The pool is
    created by the caller.

    Args:
        connection: Name of the ``[connections.<name>]`` section.
        config_path: Path to the TOML config file.

    Returns:
        Tuple of ``(config_instance, Dialect)``.

    Raises:
        FileNotFoundError: If the config file does not exist.
        KeyError: If the named connection section is not found.
        ValueError: If the ``type`` field is missing or unsupported.
    """
    config_cls, dialect, section = _resolve_section(connection, config_path)
    wh_config = _expand_private_key_path(config_cls(**section))
    return wh_config, dialect


def warehouse_config(
    backend: str,
    config_path: str | Path = ".semolina.toml",
) -> Any:
    """
    Build an adbc-poolhouse config for a backend without creating a pool.

    Reads the ``[connections.<backend>]`` section of ``.semolina.toml`` (if the
    file exists) and instantiates the matching adbc-poolhouse config class.
    ``SNOWFLAKE_*`` / ``DATABRICKS_*`` / ``DUCKDB_*`` environment variables (and a
    ``.env`` file, overridable with ``SEMOLINA_ENV_FILE``) fill any fields not
    given in the section, so env-only setups work with no TOML file. The
    ``"databricks-python"`` backend reads ``DATABRICKS_PYTHON_*`` instead. Both password
    and key-pair auth are supported (whatever the config class accepts).

    Unlike :func:`create_engine`'s name-dispatch arm, the section is looked up by
    backend *type* (``"snowflake"`` / ``"databricks"`` / ``"databricks-python"`` /
    ``"duckdb"``), matching
    how the codegen CLI and the integration test fixtures select a backend.

    Args:
        backend: ``"snowflake"``, ``"databricks"``, ``"databricks-python"``, or ``"duckdb"``.
        config_path: Path to the TOML config file.

    Returns:
        An adbc-poolhouse config instance (``SnowflakeConfig`` etc.).

    Raises:
        ValueError: If ``backend`` is not a supported type.
        pydantic.ValidationError: If required fields are missing from both the
            config section and the environment.
    """
    if backend not in _CONFIG_MAP:
        supported = list(_CONFIG_MAP.keys())
        raise ValueError(f"Unsupported backend '{backend}'. Supported types: {supported}")

    config_cls, _dialect = _CONFIG_MAP[backend]
    section: dict[str, Any] = {}
    path = Path(config_path)
    if path.exists():
        with path.open("rb") as f:
            data = tomllib.load(f)
        section = dict(data.get("connections", {}).get(backend, {}))
        section.pop("type", None)
    # Precedence: TOML section > environment > .env file (SEMOLINA_ENV_FILE
    # overrides the default ".env"). A missing .env file is ignored.
    env_file = os.getenv("SEMOLINA_ENV_FILE") or ".env"
    return _expand_private_key_path(config_cls(**section, _env_file=env_file))
