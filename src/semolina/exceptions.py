"""
Semolina's errors, all deriving from :class:`SemolinaError`, plus the optional-dependency guard.

Catch :class:`SemolinaError` to handle every error class Semolina defines in one clause.
Each error also keeps the base it had before the hierarchy existed --
:class:`RuntimeError` for the result and engine errors, :class:`ValueError` for an
unregistered engine -- so an ``except`` clause written against 0.6.0 still matches.

Plain argument and setup errors (a non-``int`` passed to ``limit()``, one engine name
registered twice, a connection config with no ``type``, say) are not ``SemolinaError``: they
are the caller's mistake, raised as the builtin ``TypeError``, ``ValueError`` or ``KeyError``
Python code conventionally uses for one.

:func:`_require` is how every optional dependency is checked. It uses
``importlib.util.find_spec`` rather than a ``try: import``, so an absent package is detected
without importing a present one — the same mechanism
:func:`semolina.codegen.python_renderer.ruff_available` uses for the ``codegen-lint`` extra.
"""

from __future__ import annotations

import contextlib
import importlib.util
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = [
    "SemolinaConnectionError",
    "SemolinaDuplicateColumnsError",
    "SemolinaEngineNotRegisteredError",
    "SemolinaError",
    "SemolinaExtensionLoadError",
    "SemolinaGeneratedSourceError",
    "SemolinaIntrospectionError",
    "SemolinaMissingDependencyError",
    "SemolinaResultConsumedError",
    "SemolinaSchemaMismatchError",
    "SemolinaSpecError",
    "SemolinaUnresolvableAnnotationError",
    "SemolinaViewNotFoundError",
    "_claim_read",
    "_connecting",
    "_require",
]
"""
The module's interface, ``_require``, ``_claim_read`` and ``_connecting`` included.

The leading underscore means "internal to Semolina", not "internal to this file": both
cursors import them, and it is deliberately not re-exported from the package root because a
guard helper is not part of the public API. Listing it here says that out loud, and stops a
strict type checker reading a private function used only across module boundaries as dead
code.
"""


class SemolinaError(Exception):
    """Base class of every exception class Semolina defines."""


class SemolinaSpecError(SemolinaError, ValueError):
    """
    Raised when query data names an unknown field, misuses one, or has the wrong shape.

    Unlike the plain ``TypeError`` and ``ValueError`` that a mistake in Python code raises,
    this error is about data that usually came from outside the program, such as an agent's
    tool call passed to :func:`semolina.spec.build_query`. It is a ``SemolinaError``, so one
    ``except`` clause catches every such mistake, and a ``ValueError``, which is what it
    would otherwise have been.

    Attributes:
        path: Where in the data the mistake is, such as ``"where.and[0].op"``. Empty when
            it concerns the data as a whole.
    """

    def __init__(self, message: str, path: str = "") -> None:
        """
        Build the error, prefixing the message with its path.

        Args:
            message: What is wrong and, where it helps, what to write instead.
            path: Where in the data the mistake is.
        """
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


class SemolinaViewNotFoundError(SemolinaError, RuntimeError):
    """Raised when the requested semantic view does not exist in the warehouse."""


class SemolinaConnectionError(SemolinaError, RuntimeError):
    """
    Raised when the engine cannot connect to or authenticate with the warehouse.

    Raised wherever Semolina opens a connection: building an engine, checking a connection
    out to run a query, and introspecting a view. The driver's error is chained as
    ``__cause__``. A statement the warehouse rejects once connected is not a connection
    failure, and keeps the driver's own error.
    """


class SemolinaExtensionLoadError(SemolinaConnectionError):
    """
    Raised when a new DuckDB connection cannot install or load the ``semantic_views`` extension.

    Semolina runs ``INSTALL semantic_views FROM community`` and ``LOAD semantic_views`` on each
    new DuckDB connection. The first ``INSTALL`` downloads the extension, so this is what you
    see offline, or when the cached build does not match your DuckDB version. The DuckDB error
    is the ``__cause__``. A :class:`SemolinaConnectionError` as well, since the connection was
    never usable.
    """


class SemolinaIntrospectionError(SemolinaError, RuntimeError):
    """
    Raised when the warehouse is reachable but cannot describe the view asked for.

    A view whose definition no longer binds, say, because a column it reads was dropped. The
    connection worked, so this is not a :class:`SemolinaConnectionError`.
    """


class SemolinaEngineNotRegisteredError(SemolinaError, ValueError):
    """
    Raised when no engine is registered under the name a query or lookup asked for.

    A :class:`ValueError` as well, because that is what ``get_engine`` raised before this
    class existed.
    """


class SemolinaMissingDependencyError(SemolinaError, RuntimeError):
    """Raised when a method needs an optional package that is not installed."""


class SemolinaSchemaMismatchError(SemolinaError, RuntimeError):
    """Raised when a DTO's field annotations do not describe the result schema."""


class SemolinaResultConsumedError(SemolinaError, RuntimeError):
    """
    Raised when a cursor's result is read a second way after the first has started.

    A result is one stream. Each way of reading it — the DBAPI fetches, ``for row in cursor``,
    ``fetch_record_batch()``, or one of the whole-result methods — keeps its own place in that
    stream, so a second way would start wherever the first had buffered up to and silently
    miss the rows in between. The message names both calls. To read a result twice, run the
    query twice.
    """


class SemolinaDuplicateColumnsError(SemolinaError, ValueError):
    """
    Raised when a result has two columns of one name, which a ``Row`` cannot hold.

    Semolina's own queries never produce one, since the builder refuses a field selected
    twice; a custom engine's result can. A :class:`ValueError` as well, because that is what
    0.6 raised.
    """


class SemolinaGeneratedSourceError(SemolinaError, RuntimeError):
    """
    Generated source failed a check it must pass before it is emitted.

    Raised by codegen when the rendered module does not parse, or when ruff is installed and
    fails on it. Either way nothing should be written: a module that is emitted anyway
    either fails on import or is not what ``--check`` and ``ruff format --check`` will later
    expect. Also importable as ``semolina.codegen.python_renderer.GeneratedSourceError``.
    """


class SemolinaUnresolvableAnnotationError(SemolinaError):
    """
    An annotation uses syntax or a name ``codegen --check`` does not compare by meaning.

    Also importable as ``semolina.codegen.type_compare.Unresolvable``.
    """


@contextlib.contextmanager
def _connecting(what: str) -> Generator[None]:
    """
    Report a driver error raised while opening a connection as :class:`SemolinaConnectionError`.

    Wraps only Semolina's own calls into its pool (creating it, checking a connection out),
    never statement execution, so a query the warehouse rejects keeps the driver's error.

    Args:
        what: What was being attempted, for the message, e.g. ``"Could not connect"``.

    Yields:
        Nothing; the block runs inside the translation.

    Raises:
        SemolinaConnectionError: If the block raises an ADBC driver error.
    """
    import adbc_driver_manager

    try:
        yield
    except adbc_driver_manager.Error as e:
        raise SemolinaConnectionError(f"{what}: {e}") from e


def _require(package: str, extra: str) -> None:
    """
    Raise unless ``package`` is importable, naming the extra that installs it.

    The check is ``importlib.util.find_spec``, so a package that *is* installed is never
    imported as a side effect of asking about it — the caller does its own function-local
    import afterwards.

    Two properties of this function are load-bearing and must survive any refactor: this
    module imports ``importlib.util`` (not ``from importlib.util import find_spec``), and the
    call is spelled ``importlib.util.find_spec(...)`` inside the body with no caching. That is
    exactly what lets a test reach it with ``patch("importlib.util.find_spec", ...)``. Hoisting
    the lookup or memoizing the answer would silently stop every one of those tests from
    patching anything, and they would keep passing.

    Args:
        package: The distribution's importable module name, e.g. ``"arrowmodel"``.
        extra: The Semolina extra that installs it, e.g. ``"arrowmodel"``. Rendered into the
            message as ``pip install semolina[{extra}]``.

    Raises:
        SemolinaMissingDependencyError: If ``package`` cannot be found.

    Example:
        .. code-block:: python

            from semolina.exceptions import _require

            _require("polars", "polars")
            # SemolinaMissingDependencyError: polars is required by this method but is
            # not installed. Install it with: pip install semolina[polars]
    """
    if importlib.util.find_spec(package) is None:
        raise SemolinaMissingDependencyError(
            f"{package} is required by this method but is not installed. "
            f"Install it with: pip install semolina[{extra}]"
        )


_DBAPI_FETCHES = "the DBAPI fetches"
"""
The one way of reading shared by ``fetchone``/``fetchmany``/``fetchall`` and their ``_rows``
variants, which all go through the driver's single DBAPI row iterator and so interleave
correctly, as DBAPI specifies.
"""


def _claim_read(
    reading: tuple[str, str] | None, call: str, *, dbapi: bool = False
) -> tuple[str, str]:
    """
    Record ``call`` as a cursor's way of reading its result, or refuse a second way.

    Called by both cursors before a method reads anything, and only once its dependency and
    schema checks have passed, so a call that fails before reading leaves the cursor free.

    Args:
        reading: The cursor's current claim — ``(way, first call)`` — or None before any read.
        call: How the reading method is named in the error, e.g. ``"fetchall_rows()"``.
        dbapi: True for the DBAPI fetches, which share one way of reading between them.

    Returns:
        The claim the cursor stores: unchanged if this is the same way of reading, new if it
        is the first.

    Raises:
        SemolinaResultConsumedError: If the result is already being read a different way.
    """
    way = _DBAPI_FETCHES if dbapi else call
    if reading is None:
        return (way, call)
    if reading[0] != way:
        first = reading[1]
        raise SemolinaResultConsumedError(
            f"{call} cannot read this result: {first} is already reading it. A cursor's result "
            f"is one stream, and a second way of reading it would silently skip the rows the "
            f"first has buffered. Keep to {first}, or run the query again to read it another way."
        )
    return reading
