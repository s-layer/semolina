"""Codegen subcommand: introspect warehouse semantic views and generate Python model classes."""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.text import Text

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import TextIO

    from pydantic import ValidationError

    from semolina.codegen.annotation_check import ViewCheckReport
    from semolina.codegen.model_reader import CommittedModel
    from semolina.engines.base import Engine
    from semolina.introspection import IntrospectedView

__all__ = [
    "EXIT_INVALID_BACKEND",
    "EXIT_VIEW_NOT_FOUND",
    "EXIT_CONNECTION_ERROR",
    "EXIT_ANNOTATION_DRIFT",
    "EXIT_PROBE_FAILED",
    "STAGE_SETUP",
    "STAGE_WAREHOUSE",
    "exit_code_for",
    "exit_for",
    "check_output",
    "emit_source",
    "codegen",
]


# Diagnostics-only console: writes to stderr
# NOTE: _stderr is module-level for error messages outside the command function.
# Python source output uses typer.echo() so CliRunner captures it correctly.
#
# `stderr=True` with no `file=`: rich then resolves `sys.stderr` at *write* time. Passing
# `file=sys.stderr` pinned the stream captured at import, so anything that replaces
# `sys.stderr` afterwards — a test runner, an embedding process — silently lost every
# diagnostic this CLI emits. The destination is identical in normal use.
_stderr = Console(stderr=True)


def _labelled(label: str, style: str, body: str) -> Text:
    """
    Build a styled-label diagnostic whose body is never parsed as rich markup.

    ``Console`` renders a bare ``str`` with ``markup=True``, so any interpolated value —
    a warehouse field name, a driver error message, a Python annotation — is run through
    the markup parser before display. ``list[str] | None`` then prints as ``list | None``,
    and a value shaped like a closing tag raises ``MarkupError`` out of a command that
    otherwise reports through documented exit codes. A :class:`~rich.text.Text` carries its
    styling as an attribute instead, so the body is displayed verbatim whatever it contains.

    Args:
        label: The leading word, e.g. ``"Note:"`` or ``"Error:"``.
        style: The rich style applied to the label alone.
        body: Everything after the label. Displayed literally.

    Returns:
        The assembled renderable.
    """
    return Text.assemble((label, style), body)


# Exit code constants for scripted callers.
# Note: Typer also uses exit code 2 for missing required arguments (fires earlier).
# EXIT_INVALID_BACKEND=2 fires when --backend value is provided but unrecognized.
EXIT_INVALID_BACKEND = 2
EXIT_VIEW_NOT_FOUND = 3
EXIT_CONNECTION_ERROR = 4
# --check found annotation drift. Distinct from 1 ("the tool broke") on purpose: a CI job
# that cannot tell "your model is stale" from "codegen crashed" has to treat both the same,
# which means either failing builds on a crash or shipping stale models on a real drift.
EXIT_ANNOTATION_DRIFT = 5
# The warehouse could not describe what was asked for: a driver error after the connection
# was open, or (on codegen-dto) a projected field the probed result carries no column for.
#
# 6 rather than 5: 5 means "your committed file is stale", which is the caller's to fix by
# regenerating. This one means no description came back at all, and nothing was written. A
# CI job that cannot tell those apart has to treat both the same -- the same reasoning that
# gave drift its own code in the first place. Shared by both commands since GEN-04.
EXIT_PROBE_FAILED = 6


def _normalize_database_path(database: str) -> str:
    """
    Normalize a DuckDB database path, preserving the ``:memory:`` sentinel.

    Applies ``expanduser()`` + ``resolve(strict=False)`` to real paths so
    relative paths, absolute paths, and ``~`` expansion all work uniformly.
    The ``:memory:`` sentinel and empty strings pass through unchanged.

    The empty-string passthrough is intentional: ``Path("").resolve()`` would
    silently expand to the current working directory, masking a bug and
    producing an inconsistent error path. Leaving ``""`` unchanged lets
    DuckDB raise the same error it raises for any other invalid path.

    Args:
        database: A DuckDB database path or the ``":memory:"`` sentinel.

    Returns:
        Normalized absolute path, or the input unchanged for ``":memory:"``
        and empty strings.

    Example:
        .. code-block:: python

            from semolina.cli.codegen import _normalize_database_path

            _normalize_database_path(":memory:")
            # ':memory:'

            _normalize_database_path("~/analytics.db")
            # '/Users/you/analytics.db'
    """
    if database and database != ":memory:":
        database = str(Path(database).expanduser().resolve(strict=False))
    return database


STAGE_SETUP = "setup"
"""The stage that resolves ``--backend``, importing or building the engine and its pool."""

STAGE_WAREHOUSE = "warehouse"
"""The stage that asks the warehouse to describe a view or query (introspect, probe, check)."""


def exit_code_for(error: BaseException, *, stage: str) -> int | None:
    """
    Map an exception to its documented exit code: the one table both commands use.

    ``codegen`` and ``codegen-dto`` fail at the same two stages -- building the engine, then
    asking the warehouse to describe a view or a query -- and the same exception at the same
    stage means the same thing on both. Before this table each command carried its own
    ``except`` arms, and they drifted: a missing driver extra or an untranslated driver
    error was a code on one and a traceback on the other (GEN-04, A26).

    A driver error is the one row that depends on the stage. While the engine is being
    built, adbc-poolhouse is opening a connection, so the driver failing then is a failure
    to connect (4). Afterwards the connection exists, and a driver error is the warehouse
    declining to describe what was asked for (6).

    ``None`` means the table has no row for the exception, and the caller re-raises it. A
    bug in Semolina should surface as a traceback, not as a tidy exit 1.

    Args:
        error: The exception.
        stage: :data:`STAGE_SETUP` or :data:`STAGE_WAREHOUSE`.

    Returns:
        The exit code, or ``None`` when the exception is not one this CLI expects.
    """
    import adbc_driver_manager

    from semolina.exceptions import (
        SemolinaConnectionError,
        SemolinaIntrospectionError,
        SemolinaViewNotFoundError,
    )

    if isinstance(error, (typer.Exit, typer.Abort)):
        # Both are RuntimeError subclasses; they are typer's own control flow, not failures.
        return None
    if isinstance(error, typer.BadParameter):
        return EXIT_INVALID_BACKEND
    if stage == STAGE_SETUP:
        if isinstance(error, ImportError):
            # The driver for the named backend is not installed.
            return EXIT_INVALID_BACKEND
        if isinstance(error, (adbc_driver_manager.Error, SemolinaConnectionError)):
            return EXIT_CONNECTION_ERROR
        return None
    if isinstance(error, SemolinaViewNotFoundError):
        return EXIT_VIEW_NOT_FOUND
    if isinstance(error, SemolinaConnectionError):
        return EXIT_CONNECTION_ERROR
    if isinstance(error, (adbc_driver_manager.Error, SemolinaIntrospectionError)):
        return EXIT_PROBE_FAILED
    if isinstance(error, (RuntimeError, ValueError)):
        # ValueError: a view name the dialect cannot quote. RuntimeError: an engine's own
        # failure, or a missing optional package. Reported, not a traceback.
        return 1
    return None


def exit_for(error: Exception, *, stage: str, driver_failure: str | None = None) -> None:
    """
    Report ``error`` and exit with its code from :func:`exit_code_for`, if it has one.

    Returns only when the table has no row for the exception, so the caller's next
    statement is a bare ``raise``.

    Args:
        error: The exception being handled.
        stage: :data:`STAGE_SETUP` or :data:`STAGE_WAREHOUSE`.
        driver_failure: What was being described when a driver error is raised at the
            warehouse stage, e.g. ``"could not probe the result schema for x"``. Driver
            messages rarely say which view they were about.

    Raises:
        typer.Exit: With the table's code, whenever it has one.
    """
    import adbc_driver_manager
    from pydantic import ValidationError

    from semolina.exceptions import SemolinaConnectionError

    code = exit_code_for(error, stage=stage)
    if code is None:
        return
    if isinstance(error, ValidationError):
        # A ValueError, so it lands on the table's 1 -- and its str() carries `input_value=`,
        # the settings dict a custom engine validated in connect() or introspect(), password
        # and all. Summarized by field and error type, as every other config error is.
        body = f" ValidationError. {_redacted_validation_summary(error)}"
    elif (
        code == EXIT_CONNECTION_ERROR
        and stage == STAGE_SETUP
        and not isinstance(error, SemolinaConnectionError)
    ):
        # A SemolinaConnectionError already says it could not connect; a raw driver error
        # raised by a custom engine's constructor does not.
        body = f" could not connect to the warehouse: {error}"
    elif isinstance(error, adbc_driver_manager.Error) and not isinstance(
        error, SemolinaConnectionError
    ):
        lead = driver_failure or "the warehouse driver failed"
        body = f" {lead}: {type(error).__name__}: {error}"
    else:
        body = f" {error}"
    _stderr.print(_labelled("Error:", "bold red", body))
    raise typer.Exit(code=code) from error


def _adbc_connection_errors() -> tuple[type[Exception], ...]:
    """
    Name the exceptions a driver raises when it cannot reach the database.

    Wrapped in a function rather than declared as a module-level tuple to keep the
    ``adbc_driver_manager`` import lazy, the way every other consumer in this package
    does — importing the driver manager costs a shared-library load, and ``--help`` must
    not pay it.

    The whole ``adbc_driver_manager.Error`` hierarchy, deliberately, and only at the one
    call site where the sole thing happening is opening a connection: ``_resolve_backend``
    builds the adbc-poolhouse pool, poolhouse connects while doing it, and the driver
    reports a bad path or an unreachable host as whatever subclass it likes
    (``InternalError`` for a DuckDB path that does not exist, measured 2026-08-16).
    Catching the base class there cannot swallow a query failure, because no query has
    been built yet.

    Returns:
        A tuple suitable as an ``except`` clause.
    """
    import adbc_driver_manager

    return (adbc_driver_manager.Error,)


def _resolve_backend(backend_spec: str, *, database: str | None = None) -> Engine:
    """
    Resolve a backend specifier string to an Engine instance.

    Recognizes the shorthand aliases ``'snowflake'``, ``'databricks'``, and
    ``'duckdb'``, and also accepts any fully-qualified ``dotted.path.ClassName``
    string which is dynamically imported and instantiated with no arguments.

    Args:
        backend_spec: One of ``'snowflake'``, ``'databricks'``, ``'duckdb'``,
            or a dotted import path such as ``'mypackage.backends.CustomEngine'``.
        database: DuckDB database file path. Required when ``backend_spec``
            is ``'duckdb'``; ignored for other backends.

    Returns:
        Engine: An instantiated engine ready for introspection calls.

    Raises:
        typer.BadParameter: If the specifier is not recognized or cannot be
            imported, or if ``'duckdb'`` is requested without a database path.
    """
    from semolina.dialect import Dialect

    if backend_spec in {d.value for d in Dialect}:
        from pydantic import ValidationError

        from semolina.config import _BACKENDS, create_engine, warehouse_config

        label = _BACKENDS[Dialect(backend_spec)].label
        if backend_spec == "duckdb":
            if database is None:
                raise typer.BadParameter(
                    "DuckDB backend requires a database path. "
                    "Use --database or set DUCKDB_DATABASE environment variable."
                )
            from adbc_poolhouse import DuckDBConfig

            config = DuckDBConfig(database=_normalize_database_path(database), read_only=True)
        else:
            try:
                config = warehouse_config(backend_spec)
            except tomllib.TOMLDecodeError as e:
                raise typer.BadParameter(f"Invalid .semolina.toml: {e}") from e
            except ValidationError as e:
                section = backend_spec
                env_prefix = backend_spec.upper()
                raise typer.BadParameter(
                    f"{label} connection config missing or invalid. Set [connections.{section}] "
                    f"in .semolina.toml or {env_prefix}_* environment variables. "
                    f"{_redacted_validation_summary(e)}"
                ) from e
        try:
            return create_engine(config)
        except ImportError as e:
            # adbc-poolhouse loads the driver while building the pool, and reports one that is
            # not installed as an ImportError. Snowflake's and DuckDB's drivers come with the
            # extra named after the backend; Databricks' is Foundry-distributed and not on
            # PyPI, so its extra cannot fetch it and only `dbc install` does.
            remedy = (
                'uv tool install dbc && dbc install "databricks>=0.2.0"'
                if backend_spec == "databricks"
                else f"pip install 'semolina[{backend_spec}]'"
            )
            raise typer.BadParameter(
                f"The {label} driver is not installed ({e}). Install it with: {remedy}"
            ) from e
    else:
        import importlib

        from semolina.codegen.query_resolver import append_working_directory

        module_path, _, class_name = backend_spec.rpartition(".")
        if not module_path:
            raise typer.BadParameter(
                f"Unknown backend {backend_spec!r}. "
                "Use 'snowflake', 'databricks', 'duckdb', or a dotted import path."
            )
        append_working_directory()
        try:
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
        except (ImportError, AttributeError) as e:
            raise typer.BadParameter(f"Cannot import backend {backend_spec!r}: {e}") from e
        return _construct_backend(cls, backend_spec)


def _construct_backend(cls: Callable[[], Engine], backend_spec: str) -> Engine:
    """
    Instantiate a ``--backend dotted.path.ClassName`` with no arguments.

    The published contract is "instantiated with no arguments", so a constructor that needs
    one, or that raises reading its own configuration, is an option the user has to fix
    rather than a traceback. A driver or connection failure is let through: a constructor
    that connects and cannot is reported as 4 by :func:`exit_code_for`, the same as the
    built-in backends.

    Args:
        cls: The imported class.
        backend_spec: The ``--backend`` value, for the message.

    Returns:
        The engine.

    Raises:
        typer.BadParameter: If the constructor raised anything but a connection failure. A
            pydantic ``ValidationError`` is summarized by field and error type, never by
            the values it quotes.
    """
    from pydantic import ValidationError

    from semolina.exceptions import SemolinaConnectionError

    try:
        return cls()
    except (*_adbc_connection_errors(), SemolinaConnectionError):
        raise
    except ValidationError as e:
        # An engine that validates its own settings with pydantic raises pydantic's error,
        # and its str() carries `input_value=` -- the settings dict, password and all. It
        # gets the same summary the built-in backends' config errors do.
        raise typer.BadParameter(
            f"Cannot build backend {backend_spec!r} with no arguments: ValidationError. "
            f"{_redacted_validation_summary(e)}"
        ) from e
    except Exception as e:
        raise typer.BadParameter(
            f"Cannot build backend {backend_spec!r} with no arguments: {type(e).__name__}: {e}"
        ) from e


def _redacted_validation_summary(error: ValidationError) -> str:
    """
    Name the config fields a ``ValidationError`` is about, and nothing they hold.

    pydantic's own message carries ``input_value=`` -- for a missing field, the whole dict
    the settings were built from, password included (GEN-03, A25). This prints each error's
    field path and error type only. Not even ``msg``: a validator's message is free text and
    may quote the value it refused.

    Args:
        error: The validation error.

    Returns:
        e.g. ``"Problems: account (missing)."``
    """
    parts = [
        f"{'.'.join(str(part) for part in err['loc']) or '(config)'} ({err['type']})"
        for err in error.errors(include_input=False, include_url=False, include_context=False)
    ]
    return f"Problems: {'; '.join(parts)}."


def _resolve_check_model(*, check: bool, model: Path | None, output: Path | None) -> Path | None:
    """
    Validate the ``--check`` options and return the committed file to check.

    ``--check`` reads the file ``--output`` names -- the file a generation run with the same
    options writes -- so CI checks exactly what regeneration would replace (GEN-06). That is
    how ``codegen-dto`` has always paired them. ``--model`` predates ``--output`` and is
    kept as the older spelling for existing scripts; passing both is refused rather than one
    silently winning.

    Args:
        check: Whether ``--check`` was passed.
        model: The ``--model`` path, if any.
        output: The ``--output`` path, if any.

    Returns:
        The file to check when a check was requested, None when it was not.

    Raises:
        typer.BadParameter: If ``--check`` has no file, has two, names one that does not
            exist, or ``--model`` came without ``--check``. Converted to
            ``EXIT_INVALID_BACKEND`` (2) by the caller.
    """
    if model is not None and not check:
        raise typer.BadParameter("--model is only meaningful with --check.")
    if not check:
        return None
    if model is not None and output is not None:
        raise typer.BadParameter(
            "--check reads one committed file, but both --output and --model name one. Pass "
            "--output PATH; --model is its older spelling."
        )
    target = output if output is not None else model
    if target is None:
        raise typer.BadParameter(
            "--check needs the committed model file to check against the warehouse's current "
            "result schema. Pass --output PATH (or the older --model PATH)."
        )
    if not target.is_file():
        # The same refusal, and so the same exit code, as codegen-dto's for the same mistake.
        # Checked here, before the backend is resolved, rather than surfacing as
        # read_committed_model's ValueError (1) after connecting.
        raise typer.BadParameter(f"--check cannot read {target}: no such file. Generate it first.")
    return target


def _check_class_names(views: list[IntrospectedView]) -> None:
    """
    Refuse a set of views whose class names one generated module could not carry.

    Three checks, and only one of them can fire for every engine:

    - Two views folding to one name -- ``emea.sales`` and ``apac.sales`` both become
      ``Sales`` -- are refused on any backend: the second class would silently replace the
      first. The remedy is to generate them into separate files.
    - A name that is not a valid identifier is refused. The built-in engines name a class
      with :func:`~semolina.codegen.introspector.class_name_for`, which always returns one;
      a ``--backend dotted.path.ClassName`` engine builds its own ``IntrospectedView``, and
      the class name is written into the module as a bare token, so it is checked here the
      way ``codegen-dto`` checks its own (threat T-50-01).
    - A name the generated module imports (``Metric``, ``Any``) is refused, because the
      class would rebind the import for every class after it. ``class_name_for`` already
      steers clear of those (``analytics.metric`` becomes ``ViewMetric``), so this too is
      only a backstop for a custom engine's own choice, and the remedy is that engine's.

    Names are compared in NFKC form, the form Python binds, so ``Ｍetric`` is ``Metric``.

    Args:
        views: The introspected views, in emission order.

    Raises:
        typer.BadParameter: Naming the view and class name at fault. Converted to
            ``EXIT_INVALID_BACKEND`` (2) by the caller: the views to generate are the
            command's arguments.
    """
    from semolina.codegen.python_renderer import GENERATED_MODULE_NAMES
    from semolina.codegen.query_resolver import find_class_name_problem

    problem = find_class_name_problem(
        ((v.view_name, v.class_name) for v in views), module_names=GENERATED_MODULE_NAMES
    )
    if problem is None:
        return
    if problem.shadows_import:
        msg = (
            f"View {problem.origin!r} would generate a class named {problem.quoted}, "
            "which is a name the generated module imports or uses in its annotations; the "
            "class would shadow it for every class declared after it. The engine that "
            "described the view chose that name, so change the engine to choose another: "
            "the built-in backends prefix such a name with View (ViewMetric)."
        )
    elif problem.clashes_with is None:
        msg = (
            f"View {problem.origin!r} would generate a class named {problem.class_name!r}, "
            "which is not a valid Python class name. The engine that described the view "
            "chose that name; it has to be a single Python identifier and not a keyword."
        )
    else:
        msg = (
            f"Views {problem.clashes_with!r} and {problem.origin!r} both generate a class "
            f"named {problem.quoted}, and one module cannot carry two. If they are two "
            "spellings of the same view, pass it once; otherwise generate them into "
            "separate files."
        )
    raise typer.BadParameter(msg)


def check_output(output: Path | None) -> None:
    """
    Refuse an output path whose directory does not exist, before anything is probed.

    Shared by both commands. Checked before the backend is resolved rather than at the
    write, which happens after every view or query has been described. A mistyped
    ``--output`` is worth catching before that work, not after it.

    The directory is not created. A generated module is imported by the code that uses it,
    so its directory is a package that already exists; creating one would turn a typo into
    a stray directory and a module nothing imports.

    Args:
        output: The resolved output path, or ``None`` for stdout.

    Raises:
        typer.BadParameter: If the path's parent directory does not exist, or if the path
            names an existing directory.
    """
    if output is None:
        return
    if output.is_dir():
        msg = f"--output {output} is a directory. Name the file to write."
        raise typer.BadParameter(msg)
    parent = output.parent
    if not parent.is_dir():
        msg = f"--output {output} names a directory that does not exist: {parent}."
        raise typer.BadParameter(msg)


def emit_source(source: str, output: Path | None, *, count: int) -> None:
    """
    Write a generated module to stdout or to ``--output``: the one writer both commands use.

    Called once, after every class has rendered and passed the parse check, never per class.
    To stdout the source is echoed with ``nl=False``: it already ends in a newline, and a
    second one made ``> models.py`` fail ``ruff format --check`` while ``--output`` passed
    (GEN-05, A27). The two routes now produce the same bytes.

    To a file it is written atomically (GEN-06, A29): the module goes to a temporary file
    beside the target, which is then renamed over it with :func:`os.replace`. A run that
    fails -- even in the write itself -- leaves a committed module as it was, rather than
    truncated or half-replaced. The temporary file is created with ``open(..., "x")`` so it
    gets the umask's permissions, and takes the target's mode when there is one. It is
    removed on any exception, ``KeyboardInterrupt`` included.

    The target is resolved first, so a symlinked ``--output`` is written through: the file
    it points at is replaced and the link stays a link. A target that exists and is not a
    regular file -- a FIFO, a device -- or that names an open descriptor (``/dev/stdout``,
    even redirected to a file, ``/dev/fd/N``, ``/proc/<pid>/fd/N``) is written in place,
    because renaming a file over a stream is not a write to it.

    Renaming over the target gives it a new inode. It keeps its mode, but it is owned by
    whoever ran codegen and loses any ACL or extended attribute the old file carried. When
    the directory refuses the temporary file (permission denied) but the target exists, the
    module is written in place instead: a locked checkout or a single bind-mounted file
    can still be regenerated, without the atomicity. Any other failure to create the
    temporary file is exit 1, with the target untouched.

    Args:
        source: The rendered module.
        output: The file to write, or ``None`` for stdout.
        count: How many classes it carries, for the confirmation line.

    Raises:
        typer.Exit: With 1 if the file cannot be written. On the atomic route the target is
            untouched.
    """
    import uuid

    if output is None:
        typer.echo(source, nl=False)
        return
    if (output.exists() and not output.is_file()) or _names_an_open_stream(output):
        # Checked on the path as given, before resolving: `/dev/stdout` resolves through
        # `/proc/self/fd/1` to a name like `pipe:[1234]`, which is no path at all, and when
        # stdout is redirected to a file, renaming over it would swap the inode under the
        # shell's redirect.
        _write_in_place(output, source, output)
    else:
        target = output.resolve()
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:12]}.tmp")
        try:
            handle = temp.open("x", encoding="utf-8", newline="")
        except PermissionError as e:
            if not target.is_file():
                _stderr.print(_labelled("Error:", "bold red", f" could not write {output}: {e}"))
                raise typer.Exit(code=1) from e
            # The directory refuses new entries but the file may still be writable: a locked
            # checkout, or one file bind-mounted into a container. Writing in place gives up
            # atomicity, which is better than refusing a write the user is allowed to make.
            _write_in_place(target, source, output)
        except OSError as e:
            # A read-only mount, a name too long for the temporary suffix, a symlink into a
            # missing directory. No in-place fallback: after, say, a full disk it would
            # truncate the committed file, so the file is left as it was.
            _stderr.print(_labelled("Error:", "bold red", f" could not write {output}: {e}"))
            raise typer.Exit(code=1) from e
        else:
            _replace_atomically(handle, temp, target, source, output)
    classes = "class" if count == 1 else "classes"
    _stderr.print(_labelled("Wrote:", "bold green", f" {output} ({count} {classes})"))


def _names_an_open_stream(output: PurePath) -> bool:
    """
    Whether ``output`` names an open descriptor, like ``/dev/stdout`` or ``/proc/self/fd/1``.

    Such a path can be a regular file (stdout redirected with ``>``), but it stands for an
    open descriptor, so it is written in place rather than replaced. Only the descriptor
    paths count: ``/dev/shm/models.py`` is an ordinary file and keeps the atomic route, and
    so does a Windows ``C:/dev/...`` checkout, whose root is not ``/``.

    Args:
        output: The path as the user gave it.

    Returns:
        ``True`` for ``/dev/stdin``, ``/dev/stdout``, ``/dev/stderr``, ``/dev/fd/N`` and
        ``/proc/<pid>/fd/N``.
    """
    parts = output.parts
    if parts[:1] != ("/",):
        return False
    if parts[1:2] == ("dev",):
        return parts[2:3] in (("stdin",), ("stdout",), ("stderr",)) or (
            len(parts) == 4 and parts[2] == "fd"
        )
    return len(parts) == 5 and parts[1] == "proc" and parts[3] == "fd"


def _write_in_place(target: Path, source: str, output: Path) -> None:
    """
    Overwrite ``target`` with ``source`` directly, reporting a failure as exit 1.

    Args:
        target: The file to write.
        source: The module.
        output: The path as the user gave it, for the message.

    Raises:
        typer.Exit: With 1 if the file cannot be written.
    """
    try:
        with target.open("w", encoding="utf-8", newline="") as in_place:
            in_place.write(source)
    except OSError as e:
        _stderr.print(_labelled("Error:", "bold red", f" could not write {output}: {e}"))
        raise typer.Exit(code=1) from e


def _replace_atomically(
    handle: TextIO, temp: Path, target: Path, source: str, output: Path
) -> None:
    """
    Fill the open temporary file and rename it over ``target``.

    Args:
        handle: The temporary file, already created exclusively and open for writing.
        temp: Its path.
        target: The resolved file to replace.
        source: The module.
        output: The path as the user gave it, for the message.

    Raises:
        typer.Exit: With 1 if the file cannot be written. The target is untouched, and the
            temporary file is removed on any exception, ``KeyboardInterrupt`` included.
    """
    import os
    import shutil

    try:
        with handle:
            handle.write(source)
        if target.exists():
            shutil.copymode(target, temp)
        os.replace(temp, target)
    except BaseException as e:
        temp.unlink(missing_ok=True)
        if not isinstance(e, OSError):
            raise
        _stderr.print(_labelled("Error:", "bold red", f" could not write {output}: {e}"))
        raise typer.Exit(code=1) from e


def _render_check_report(report: ViewCheckReport, class_name: str | None = None) -> None:
    """
    Write one view's per-field verdict to stderr.

    Everything here goes to **stderr**: the module's convention is source to stdout and
    diagnostics to stderr, and a ``--check`` run emits no source at all. The table carries
    field names, annotation strings and routes — never a row value (threat T-48-22).

    The Route column is always present, drift or no drift. A green ``--check`` that fell back
    to warehouse metadata must not look identical to a probed one (threat T-48-24).

    Every cell is wrapped in :class:`~rich.text.Text`, which bypasses rich's markup parser.
    A bare ``str`` cell would be parsed for style tags, and both sides of this table are
    populated from strings the user does not control: annotations from a committed model
    (``list[str] | None`` would display as ``list | None``, so two cells that differ could
    print identically on a row marked ``drift``) and field names from the warehouse
    catalogue, which both Snowflake and DuckDB let you spell with arbitrary characters
    inside a quoted identifier. The status style is passed as a ``Text`` style rather than
    as an embedded tag, so the colour survives the change.

    Args:
        report: A ``ViewCheckReport``.
        class_name: The committed class checked, named in the title when the file declares
            more than one class for the view, so the tables can be told apart.
    """
    from rich.table import Table

    from semolina.codegen.annotation_check import ROUTE_METADATA, STATUS_DRIFT

    title = Text.assemble("semolina codegen --check: ", report.view_name)
    if class_name is not None:
        title.append_text(Text.assemble(" (", class_name, ")"))
    table = Table(
        title=title,
        title_justify="left",
    )
    table.add_column("Field")
    table.add_column("Committed")
    table.add_column("Probed (result schema)")
    table.add_column("Route")
    table.add_column("Status")
    for row in report.rows:
        style = "red" if row.status == STATUS_DRIFT else "green"
        table.add_row(
            Text(row.name),
            Text(row.committed),
            Text(row.probed),
            Text(row.route),
            Text(row.status, style=style),
        )
    _stderr.print(table)

    # Drift the two annotation columns cannot show — a changed role, a stale `source=` —
    # printed under the table rather than as a sixth column, which would push the
    # annotations into wrapping on a normal terminal for the sake of a usually-empty cell.
    if report.detail:
        _stderr.print(_labelled("Detail:", "yellow", f" (view): {report.detail}"))
    for row in report.rows:
        if row.detail:
            _stderr.print(_labelled("Detail:", "yellow", f" {row.name}: {row.detail}"))

    if report.probe_error is not None:
        # `probe_error` is `f"{type(e).__name__}: {e}"` off a driver exception, and driver
        # messages quote the offending identifier — `Referenced column "x[0]" not found`.
        _stderr.print(
            _labelled(
                "Note:",
                "yellow",
                f" the result-schema probe was unavailable ({report.probe_error}); the "
                "annotations above were compared against warehouse metadata instead, which "
                "is not the same thing.",
            )
        )
    else:
        unresolved = [row.name for row in report.rows if row.route == ROUTE_METADATA]
        if unresolved:
            _stderr.print(
                _labelled(
                    "Note:",
                    "yellow",
                    " the result-schema probe ran, but no result column answered for "
                    f"{', '.join(unresolved)}; those rows were compared against warehouse "
                    "metadata instead, which is not the same thing.",
                )
            )


def _run_check(engine: Engine, views: list[str], model: Path) -> None:
    """
    Run ``--check`` over every requested view and exit with the right code.

    Args:
        engine: The resolved engine.
        views: Requested view names.
        model: Path to the committed model file.

    Raises:
        typer.Exit: 0 when every annotation matches, ``EXIT_ANNOTATION_DRIFT`` when any
            drifted, ``EXIT_PROBE_FAILED`` when a view's result-schema probe failed and its
            rows fell back to warehouse metadata, and the file's existing codes for a bad
            file or a warehouse error.
    """
    from semolina.codegen.model_reader import normalize_view_name, read_committed_model_file

    try:
        read = read_committed_model_file(model)
    except ValueError as e:
        # A malformed committed file is the user's, and deserves the message rather than a
        # traceback. A missing one never gets here: _resolve_check_model refuses it (2).
        # The reader's ValueError names the path and nothing else.
        _stderr.print(_labelled("Error:", "bold red", f" {e}"))
        raise typer.Exit(code=1) from e

    # Everything the reader could not read is said, never dropped (GEN-12). A warning does
    # not change the exit code by itself: the field it concerns reads as absent, and that
    # absence is what the report then shows as drift.
    for warning in read.warnings:
        _stderr.print(_labelled("Warning:", "yellow", f" {model}: {warning}"))

    # Views are matched the way the engine compares identifiers (D9): on DuckDB `main.sales`,
    # `SALES` and `sales` name one view, while on Snowflake `"Sales"` and `SALES` are two.
    # Every class declaring a view is checked, not only the last one, so a second class
    # cannot hide a drifted first (M9).
    by_view: dict[str, list[CommittedModel]] = {}
    for committed_model in read.models:
        by_view.setdefault(
            normalize_view_name(committed_model.view_name, engine.dialect), []
        ).append(committed_model)

    drift = False
    probe_failed = False
    probed_drift: list[str] = []
    for view_name in views:
        classes = by_view.get(normalize_view_name(view_name, engine.dialect), [])
        if not classes:
            _stderr.print(
                _labelled(
                    "Note:",
                    "yellow",
                    f" {model} declares no model class for view {view_name!r}; every field "
                    "of it reports as absent.",
                )
            )
        elif len(classes) > 1:
            names = ", ".join(c.class_name for c in classes)
            _stderr.print(
                _labelled(
                    "Note:",
                    "yellow",
                    f" {model} declares {len(classes)} classes for view {view_name!r} "
                    f"({names}); each one is checked.",
                )
            )
        targets: list[CommittedModel | None] = list(classes) or [None]
        for committed in targets:
            report = _check_one_view(engine, view_name, committed)
            _render_check_report(
                report,
                class_name=committed.class_name
                if committed is not None and len(classes) > 1
                else None,
            )
            drift = drift or report.has_drift
            # Keyed on the probe failing, not on any row reading `metadata`. A probe that
            # ran can still carry no column for one field, and that row falls back on its
            # own; an exit 6 for it would recur on every run with nothing the user could
            # do about it.
            probe_failed = probe_failed or report.probe_error is not None
            if report.has_drift and report.probe_error is None and view_name not in probed_drift:
                probed_drift.append(view_name)

    # A probe that failed is a comparison the check did not perform: the catalogue is the
    # source that wrote the model, so it agrees with it by construction and a 0 here was
    # indistinguishable from a probed pass (GEN-09, A33). It outranks drift, because drift
    # found against metadata is no more trustworthy than a match found there.
    if probe_failed:
        # 6 is one code for the whole run, so drift a working probe did find on another view
        # would vanish behind it; a CI job that regenerates on 5 would never learn of it.
        if probed_drift:
            names = ", ".join(repr(name) for name in probed_drift)
            _stderr.print(
                _labelled(
                    "Note:",
                    "yellow",
                    f" the probe did run for {names} and found drift there; exit 6 for the "
                    "failed probe outranks the 5 that drift would give on its own.",
                )
            )
        raise typer.Exit(code=EXIT_PROBE_FAILED)
    if drift:
        raise typer.Exit(code=EXIT_ANNOTATION_DRIFT)


def _check_one_view(
    engine: Engine, view_name: str, committed: CommittedModel | None
) -> ViewCheckReport:
    """
    Check one committed class against one view, turning a warehouse error into an exit.

    Args:
        engine: The resolved engine.
        view_name: The view, as named on the command line.
        committed: The committed class for it, or None when the file declares none.

    Returns:
        The report.

    Raises:
        typer.Exit: With the exit code a warehouse or driver error maps to.
    """
    from semolina.codegen import annotation_check

    try:
        return annotation_check.check_view(engine, view_name, committed)
    except Exception as e:
        exit_for(
            e,
            stage=STAGE_WAREHOUSE,
            driver_failure=f"the driver failed while checking view {view_name!r}",
        )
        raise


def codegen(
    views: Annotated[
        list[str],
        typer.Argument(help="Schema-qualified view names (e.g. my_schema.my_view)"),
    ],
    backend: Annotated[
        str,
        typer.Option(
            "--backend",
            "-b",
            help="Backend: snowflake, databricks, duckdb, or dotted.path.ClassName",
        ),
    ],
    database: Annotated[
        str | None,
        typer.Option(
            "--database",
            "-d",
            help="DuckDB database file path (or set DUCKDB_DATABASE env var)",
            envvar="DUCKDB_DATABASE",
        ),
    ] = None,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help=(
                "Report whether a committed model's annotations still match the warehouse's "
                "current result schema. Writes nothing to stdout; exits 5 on drift, or 6 "
                "when a view's probe failed and it compared against warehouse metadata. "
                "Reads the file --output names."
            ),
        ),
    ] = False,
    model: Annotated[
        Path | None,
        typer.Option(
            "--model",
            help="Older spelling of --output for --check: the committed model file to check.",
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help=(
                "Write the generated module to this file instead of stdout, atomically and "
                "only once every view has rendered. Its directory must already exist. With "
                "--check, the committed file to check."
            ),
        ),
    ] = None,
) -> None:
    """Introspect warehouse semantic views and generate Semolina model classes."""
    # A view named twice is one request, not two views folding to one class name: drop the
    # repeat, keeping the first occurrence's position, before anything is introspected.
    views = list(dict.fromkeys(views))
    try:
        check_model = _resolve_check_model(check=check, model=model, output=output)
        if check_model is None:
            check_output(output)
        engine = _resolve_backend(backend, database=database)
    except Exception as e:
        # `create_engine` inside `_resolve_backend` builds the pool, and adbc-poolhouse
        # connects while doing it -- so a driver that cannot reach the database fails here,
        # before `introspect()` and its `SemolinaConnectionError` translation. The table
        # reports that as 4, the code this command's epilog documents for it.
        exit_for(e, stage=STAGE_SETUP)
        raise

    if check_model is not None:
        # A --check run emits no model source, so it branches before render_and_format.
        _run_check(engine, views, check_model)
        return

    introspected_views: list[IntrospectedView] = []
    for view_name in views:
        try:
            introspected_views.append(engine.introspect(view_name))
        except Exception as e:
            exit_for(
                e,
                stage=STAGE_WAREHOUSE,
                driver_failure=f"the driver failed while describing view {view_name!r}",
            )
            raise

    try:
        _check_class_names(introspected_views)
    except typer.BadParameter as e:
        _stderr.print(_labelled("Error:", "bold red", f" {e}"))
        raise typer.Exit(code=EXIT_INVALID_BACKEND) from e

    from semolina.codegen.python_renderer import (
        GeneratedSourceError,
        render_and_format,
        ruff_available,
    )

    try:
        source = render_and_format(introspected_views)
    except GeneratedSourceError as e:
        # The module does not parse, or ruff is installed and failed on it. Nothing has been
        # written yet, and nothing will be: emitting the source anyway would hand the user a
        # module that fails on import or fails `ruff format --check`, with exit 0.
        _stderr.print(_labelled("Error:", "bold red", f" {e}"))
        raise typer.Exit(code=1) from e
    emit_source(source, output, count=len(introspected_views))
    if not ruff_available():
        _stderr.print(
            r"[yellow]Note:[/yellow] ruff is not installed; generated output is "
            r"unformatted. Install [bold]semolina\[codegen-lint][/bold] for formatted "
            r"output."
        )
