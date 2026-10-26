"""
Shared fixtures for the codegen test package.

Holds the data-fetch guard both ``test_annotation_check.py`` and ``test_cli.py`` use to make
``--check``'s "without executing a query for rows" runnable.
"""
# Test-only: the guard allows DuckDB introspection's version query by its exact text, which
# lives in a private constant of the engine module. Scope-disable the private-access rule
# that triggers (intentionally not a `# type: ignore`).
# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from semolina.engines.duckdb import _EXTENSION_VERSION_SQL

if TYPE_CHECKING:
    from collections.abc import Callable

_METADATA_STATEMENTS = ("DESCRIBE", "SHOW")
"""
Statement prefixes whose rows are catalogue metadata, not view data.

``engine.introspect()`` reads its field list from ``DESCRIBE SEMANTIC VIEW`` /
``DESCRIBE SELECT`` (DuckDB), ``SHOW COLUMNS`` (Snowflake) and ``DESCRIBE TABLE EXTENDED``
(Databricks), and fetching those rows is what introspection *is* — the generation path has
always done it. The guarantee is about the view's **data**, so the guard has to draw
the line here rather than ban ``fetchall`` outright.
"""

_METADATA_QUERIES = (_EXTENSION_VERSION_SQL,)
"""
Exact ``SELECT`` statements that read catalogue metadata, not view data.

DuckDB introspection reads the ``semantic_views`` version from ``duckdb_extensions()`` to
decide whether ``DESCRIBE SEMANTIC VIEW`` reports member types. It is allowed by exact
text rather than by a ``SELECT`` prefix, which would let a fetch of view data through.
"""


class _GuardedRecordBatchReader:
    """
    A record-batch reader whose schema is readable and whose batches are not.

    ``fetch_record_batch`` cannot be poisoned at the call site the way the other four fetch
    methods can: :func:`semolina.codegen.probe.probe_schema`'s zero-row fallback *has* to
    call it, because ``reader.schema`` is the whole point of that branch. What must not
    succeed is pulling a batch out. This proxy draws the line in the one place where the
    distinction exists.

    Attributes:
        _reader: The real reader.
        _statement: The statement it came from, for the failure message.
    """

    def __init__(self, reader: Any, statement: str) -> None:
        self._reader = reader
        self._statement = statement

    @property
    def schema(self) -> Any:
        """The result schema, which reads no rows."""
        return self._reader.schema

    def close(self) -> None:
        """Close the underlying reader."""
        self._reader.close()

    def _refuse(self) -> None:
        raise AssertionError(f"fetched data rows from: {self._statement}")

    def read_next_batch(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse: this would materialize a batch of the view's data."""
        self._refuse()

    def read_all(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse: this would materialize every row of the view's data."""
        self._refuse()

    def read_pandas(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse: this would materialize every row of the view's data."""
        self._refuse()

    def __iter__(self) -> Any:
        """Refuse: iteration is ``read_next_batch`` in a loop."""
        self._refuse()


@pytest.fixture
def data_fetch_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Make any fetch from a non-metadata statement raise.

    Wraps ``adbc_driver_manager.dbapi.Cursor``: ``execute`` records the statement it ran, and
    the fetch methods refuse when that statement is not catalogue introspection. A
    ``--check`` run under this guard therefore proves it never materialized a row of the
    view's data, rather than asserting it in prose.

    ``fetch_record_batch`` is wrapped differently from the other four. The zero-row fallback
    calls it and then reads only ``reader.schema``, so refusing the call itself would break
    the very route the guard most needs to cover — the only route Databricks can take, and
    the one Snowflake takes when parameters are bound. The reader it returns is proxied
    instead: schema yes, batches no.

    Args:
        monkeypatch: pytest's patching fixture; the guard is undone at test teardown.
    """
    import adbc_driver_manager.dbapi

    cursor_cls: Any = adbc_driver_manager.dbapi.Cursor
    real_execute: Any = cursor_cls.execute

    def execute(self: Any, operation: Any, *args: Any, **kwargs: Any) -> Any:
        self._guarded_statement = str(operation)
        return real_execute(self, operation, *args, **kwargs)

    def _statement_of(cursor: Any) -> str:
        return str(getattr(cursor, "_guarded_statement", "")).lstrip()

    def _is_metadata(statement: str) -> bool:
        return statement.upper().startswith(_METADATA_STATEMENTS) or (
            statement in _METADATA_QUERIES
        )

    def guarded(real: Any) -> Callable[..., Any]:
        def fetch(self: Any, *args: Any, **kwargs: Any) -> Any:
            statement = _statement_of(self)
            if not _is_metadata(statement):
                raise AssertionError(f"fetched data rows from: {statement}")
            return real(self, *args, **kwargs)

        return fetch

    real_fetch_record_batch: Any = cursor_cls.fetch_record_batch

    def fetch_record_batch(self: Any, *args: Any, **kwargs: Any) -> Any:
        reader = real_fetch_record_batch(self, *args, **kwargs)
        statement = _statement_of(self)
        if _is_metadata(statement):
            return reader
        return _GuardedRecordBatchReader(reader, statement)

    monkeypatch.setattr(cursor_cls, "execute", execute)
    for name in ("fetchall", "fetchone", "fetchmany", "fetch_arrow_table"):
        monkeypatch.setattr(cursor_cls, name, guarded(getattr(cursor_cls, name)))
    monkeypatch.setattr(cursor_cls, "fetch_record_batch", fetch_record_batch)
