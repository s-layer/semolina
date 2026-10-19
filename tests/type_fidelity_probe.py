"""
Measure a semantic-view field's type along three independent routes and render the comparison.

This module is deliberately **not** a test module: its name matches none of pytest's
``python_files`` patterns, so pytest imports it for doctest collection only (the same way it
imports ``tests/models.py``). It is driven by ``just type-fidelity``
(``uv run python tests/type_fidelity_probe.py --write``), and some of its helpers are shared
with the DuckDB-backed unit tests.

The module is split into two regions that must never be allowed to converge:

* the **metadata half** — :func:`measure_duckdb`'s use of ``Engine.introspect`` and the raw
  ``DESCRIBE SELECT * FROM semantic_view(...)`` statement. Its mapped-annotation column is
  produced by ``semolina.codegen.type_map``, which is exactly the thing being measured;
* the **result half** — :func:`semolina.codegen.probe.probe_schema`, re-imported below, plus
  :func:`probe_values` here. It must never import ``semolina.codegen.type_map`` or any symbol
  from it. Its values come from the driver's own Arrow schema and from ``pyarrow``'s
  conversion of that schema to Python objects.

Two columns sourced from one place would make the comparison circular, and a comparison that
cannot produce a mismatch is not measuring anything. That is the failure this probe exists to
rule out. For the shipped half it is enforced by behaviour in
``tests/unit/codegen/test_annotation_check.py``: with the type map made wrong, ``--check``
must still report drift.

Phase 48 promoted the schema half into ``semolina.codegen.probe`` so a shipped
``semolina codegen --check`` can reach it. This module re-imports those names rather than
keeping a copy of them: two implementations would drift, and the shipped ``--check`` and this
evidence generator disagreeing is exactly what the drift guard exists to prevent.

Record/replay contract: the DuckDB probe runs **live, in-process**, against an in-memory
DuckDB. It records nothing and must never be routed through pytest-adbc-replay cassette
interception — ``adbc_auto_patch`` lists ``adbc_driver_manager.dbapi``, which DuckDB also
routes through, so a cassette marker would silently divert it into replay *and* normalize its
SQL as the Databricks dialect.
"""

from __future__ import annotations

import argparse
import decimal
import difflib
import importlib.util
import json
import sys
import textwrap
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow

from semolina import Dimension, Metric, SemanticView

# The schema half of the result region, promoted into src/ by Phase 48. Re-imported rather
# than reimplemented so this generator and the shipped `semolina codegen --check` cannot
# drift. Only the two names this module actually uses are imported: the probe's other public
# names are reached from `semolina.codegen.probe` directly by the tests that want them, so
# there is exactly one import path to the one implementation and no re-export to rot.
from semolina.codegen.probe import ROUTE_EXECUTE_SCHEMA, probe_schema

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from semolina.engines.base import Engine


# -- Fixture -----------------------------------------------------------------------------

PROBE_VIEW_NAME = "type_fidelity_view"
"""Name of the semantic view the probe measures."""

PROBE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS type_fidelity_orders (
    id INTEGER,
    order_total DECIMAL(10, 2),
    order_count INTEGER,
    region VARCHAR
)
"""
"""
Base table for the probe fixture.

``order_total DECIMAL(10, 2)`` is the phase's headline column: it is the only shape in this
repo where decimal precision widening under ``SUM`` is demonstrable end to end.
"""

PROBE_SEED_DML = """
INSERT INTO type_fidelity_orders VALUES
    (1, 30.75, 3, 'US'),
    (2, 12.50, 1, 'US'),
    (3, 100.00, 7, 'MX'),
    (4, NULL, NULL, 'CA')
"""
"""
Seed rows for the probe fixture.

Row 4 exists so that a group can have a non-NULL dimension key and all-NULL metric inputs,
which is the shape :func:`measure_empty_group_values` measures for metric nullability. It is
the only seed row whose metric inputs are NULL, so :data:`EMPTY_GROUP_REGION` names the group
it creates rather than any group that happens to be empty.
"""

PROBE_VIEW_DDL = """
CREATE OR REPLACE SEMANTIC VIEW type_fidelity_view AS
TABLES (
    o AS type_fidelity_orders PRIMARY KEY (id)
)
DIMENSIONS (
    o.region AS region
)
METRICS (
    o.total_order_value AS SUM(o.order_total),
    o.max_order_value AS MAX(o.order_total),
    o.total_order_count AS SUM(o.order_count),
    o.avg_order_count AS AVG(o.order_count),
    o.min_order_count AS MIN(o.order_count),
    o.n_order_totals AS COUNT(o.order_total),
    o.region_list AS list(o.region)
)
"""
"""
Semantic view over :data:`PROBE_TABLE_DDL`.

The seven metrics here and :data:`DUCKDB_PROBE_METRICS` must stay in step — a metric declared
here and not emitted is a gap nothing else would make visible. Metric names deliberately
differ from the underlying column names: the ``semantic_views`` extension rejects a metric
that collides with a dimension or column name.

``region_list`` is the only metric here that is not an arithmetic aggregate. It exists so the
view always carries at least one field the type map has no entry for: ``list(o.region)``
describes as ``VARCHAR[]``, which stays a ``TODO:`` annotation for the whole of Phase 48. It
is what
``tests/unit/test_type_fidelity_duckdb.py::test_an_unmapped_type_still_disagrees_by_value``
measures, and it keeps the probe able to demonstrate a disagreement now that the decimal
columns agree. Replacing it with a mapped type would leave that guard nothing to assert.
"""


class TypeFidelityView(SemanticView, view="type_fidelity_view"):
    """
    Model of :data:`PROBE_VIEW_DDL`, used to build probe SQL through Semolina's own builder.

    Every field of the view is declared. The SQL is built rather than pasted so the probe
    measures the statement users' queries actually produce.
    """

    total_order_value = Metric()
    max_order_value = Metric()
    total_order_count = Metric()
    avg_order_count = Metric()
    min_order_count = Metric()
    n_order_totals = Metric()
    region_list = Metric()
    region = Dimension()


def setup_probe_view(dbapi_conn: Any, _connection_record: Any) -> None:
    """
    Create the probe table, seed it, and create the semantic view.

    Registered as a ``once=True`` ``connect`` listener, so it runs on the pool's first
    physical connection, after ``create_engine``'s own listener has loaded the extension.
    Every pooled connection is an ``adbc_clone`` of one source and opens the same
    in-memory database, so later connections see the committed data.

    Args:
        dbapi_conn: The freshly opened ADBC DBAPI connection.
        _connection_record: SQLAlchemy pool bookkeeping object; unused.
    """
    cur = dbapi_conn.cursor()
    cur.execute(PROBE_TABLE_DDL)
    cur.execute("DELETE FROM type_fidelity_orders")
    cur.execute(PROBE_SEED_DML)
    cur.execute(PROBE_VIEW_DDL)
    cur.close()
    dbapi_conn.commit()


def make_probe_engine() -> Engine:
    """
    Build an in-memory DuckDB engine carrying the probe fixture.

    ``create_engine`` attaches the ``_load_semantic_views`` connect listener, which installs
    the community extension on first use; :func:`setup_probe_view` is attached to the same
    pool afterwards so the extension is loaded before the view DDL runs.

    Returns:
        A DuckDB :class:`~semolina.engines.base.Engine` whose in-memory database carries
        ``type_fidelity_view``. The caller owns it and must call ``dispose()``.
    """
    from adbc_poolhouse import DuckDBConfig
    from sqlalchemy import event

    from semolina.config import create_engine

    engine = create_engine(DuckDBConfig(database=":memory:", pool_size=1))
    event.listen(engine._pool, "connect", setup_probe_view, once=True)
    return engine


# -- Result half: value probing. Must not import semolina.codegen.type_map ----------------
#
# The schema half of this region now ships from ``semolina.codegen.probe`` and is re-imported
# at the top of this module. The same prohibition applies to everything below: these values
# come from ``pyarrow``'s conversion of the driver's own buffer, never from Semolina's
# mapping of warehouse types to Python annotations.


def python_value_type_name(value: object) -> str:
    """
    Name the Python type a warehouse value actually arrived as.

    Builtins are named bare (``int``); anything else is qualified by module so the artifact
    reads ``decimal.Decimal`` rather than an ambiguous ``Decimal``.

    Args:
        value: A single value taken from ``RecordBatch.to_pylist()``.

    Returns:
        The type's name, module-qualified unless it is a builtin.
    """
    cls = type(value)
    if cls.__module__ == "builtins":
        return cls.__qualname__
    return f"{cls.__module__}.{cls.__qualname__}"


def probe_values(cursor: Any, sql: str, params: list[Any]) -> dict[str, object]:
    """
    Execute a query once and return the first non-NULL value of every one of its columns.

    This is the user-visible consequence of the result schema: ``pyarrow`` converts the Arrow
    buffer, and Semolina's row path calls ``to_pylist()`` on exactly this data. NULLs are
    skipped per column — a group with no non-NULL inputs says nothing about the value.

    One execution covers every column, so the measured fields cannot end up describing
    separately-planned queries.

    The object rather than its type name, because an annotation is checked against a value
    by ``isinstance``: a sound over-approximation (a ``pandas.Timestamp`` under a
    ``datetime.datetime`` annotation) has to be allowed to pass, and a type *name* cannot
    express a subclass relation.

    Args:
        cursor: An ADBC DBAPI cursor.
        sql: The query to execute.
        params: Bind parameters for that query.

    Returns:
        Result column name -> its first non-NULL value, or ``None`` when every value in that
        column is NULL.
    """
    cursor.execute(sql, params or None)
    table = cursor.fetch_arrow_table()
    rows: list[dict[str, object]] = table.to_pylist()
    column_names: list[str] = list(table.column_names)

    values: dict[str, object] = {}
    for name in column_names:
        values[name] = None
        for row in rows:
            value = row.get(name)
            if value is not None:
                values[name] = value
                break
    return values


def probe_value_types(cursor: Any, sql: str, params: list[Any]) -> dict[str, str]:
    """
    Execute a query once and report the Python type every one of its columns arrives as.

    A naming view of :func:`probe_values`, which does the measuring. Both are derived from
    one execution of one query, so the artifact's value column and any ``isinstance`` check
    over the same query can never describe two different runs.

    Args:
        cursor: An ADBC DBAPI cursor.
        sql: The query to execute.
        params: Bind parameters for that query.

    Returns:
        Result column name -> the name of the Python type its first non-NULL value has, or
        ``"NoneType"`` when every value in that column is NULL.
    """
    return {
        name: python_value_type_name(value)
        for name, value in probe_values(cursor, sql, params).items()
    }


def probe_value_type(cursor: Any, sql: str, params: list[Any], field_name: str) -> str:
    """
    Execute a query and report the Python type one column's values arrive as.

    A single-column view of :func:`probe_value_types`.

    Args:
        cursor: An ADBC DBAPI cursor.
        sql: The query to execute.
        params: Bind parameters for that query.
        field_name: The result column to read.

    Returns:
        The name of the Python type the field's first non-NULL value has, or ``"NoneType"``
        when every value is NULL.
    """
    return probe_value_types(cursor, sql, params)[field_name]


# -- Metadata half: introspection and the raw warehouse type ------------------------------

TODO_PREFIX = "TODO: "
"""Prefix ``IntrospectedField.data_type`` carries when the type map produced no annotation."""

VERDICT_MATCH = "match"
"""The mapped annotation names the Python type the values actually arrive as."""

VERDICT_MISMATCH = "mismatch"
"""The mapped annotation does not name the Python type the values actually arrive as."""


def classify_verdict(mapped_annotation: str, value_type: str) -> str:
    """
    Compare the annotation codegen would emit against the type the values actually have.

    A ``TODO: `` annotation is classified as a mismatch rather than as a third
    "mapping-gap" verdict: the renderer turns it into ``Any``, so it names no Python type at
    all, which is the strongest form of disagreement rather than a separate kind. Keeping the
    vocabulary to two values also keeps the canary honest — if a future refactor ever sourced
    both columns from one place, this would read ``match`` and the canary test would go red.

    Args:
        mapped_annotation: ``IntrospectedField.data_type`` for the field.
        value_type: The name produced by :func:`python_value_type_name`.

    Returns:
        :data:`VERDICT_MATCH` or :data:`VERDICT_MISMATCH`.
    """
    if mapped_annotation.startswith(TODO_PREFIX):
        return VERDICT_MISMATCH
    return VERDICT_MATCH if mapped_annotation == value_type else VERDICT_MISMATCH


def describe_raw_types(
    cursor: Any, view_name: str, dimensions: list[str], metrics: list[str]
) -> dict[str, str]:
    """
    Read the raw warehouse type of each field, before any Semolina mapping is applied.

    Re-runs the same ``DESCRIBE SELECT * FROM semantic_view(...)`` statement
    ``DuckDBEngine.introspect`` runs, and keeps column 1 verbatim. The raw type is captured
    here rather than parsed back out of the ``TODO: `` prefix, because that prefix only
    survives for types the map has no entry for — raw and mapped are two separate values.

    Args:
        cursor: An ADBC DBAPI cursor.
        view_name: Unqualified semantic view name.
        dimensions: Dimension field names to include in the ``semantic_view()`` call.
        metrics: Metric field names to include in the ``semantic_view()`` call.

    Returns:
        Field name -> raw SQL type string, e.g. ``{"total_order_value": "DECIMAL(38,2)"}``.

    Raises:
        ValueError: If neither ``dimensions`` nor ``metrics`` names a field. ``semantic_view()``
            requires at least one of ``dimensions``, ``metrics``, or ``facts``, so there is no
            statement to run. Interpolating the empty case instead produced
            ``semantic_view('view', )``, a trailing comma DuckDB rejects with a parser error
            naming a paren rather than naming the caller that asked for nothing.
    """
    from semolina.engines.duckdb import _sql_str_literal

    parts: list[str] = []
    if dimensions:
        dim_list = ", ".join(_sql_str_literal(name) for name in dimensions)
        parts.append(f"dimensions := [{dim_list}]")
    if metrics:
        metric_list = ", ".join(_sql_str_literal(name) for name in metrics)
        parts.append(f"metrics := [{metric_list}]")
    if not parts:
        msg = (
            f"describe_raw_types({view_name!r}) was given neither dimensions nor metrics. "
            "semantic_view() needs at least one field list, so there is nothing to describe."
        )
        raise ValueError(msg)

    view_literal = _sql_str_literal(view_name)
    cursor.execute(f"DESCRIBE SELECT * FROM semantic_view({view_literal}, {', '.join(parts)})")
    return {str(row[0]): str(row[1]) for row in cursor.fetchall()}


# -- Row model and rendering --------------------------------------------------------------


@dataclass(frozen=True)
class FidelityRow:
    """
    One measured ``(backend, field)`` comparison.

    There is deliberately no sample-value field. The artifact is committed and public, so it
    records types only; a value column would give warehouse row data a path into git.

    Attributes:
        backend: ``duckdb``, ``snowflake``, or ``databricks``.
        field_name: The semantic view field measured.
        role: ``metric``, ``dimension``, or ``fact``.
        metadata_raw_type: The warehouse's own type name, before Semolina maps it.
        metadata_provenance: How ``metadata_raw_type`` and ``mapped_annotation`` were obtained.
        mapped_annotation: The Python annotation string codegen would emit for this field.
        result_arrow_type: The Arrow type the driver resolves for the query result.
        result_provenance: How ``result_arrow_type`` was obtained, including the probe route.
        python_value_type: The Python type the field's values actually arrive as.
        verdict: :data:`VERDICT_MATCH` or :data:`VERDICT_MISMATCH`.
    """

    backend: str
    field_name: str
    role: str
    metadata_raw_type: str
    metadata_provenance: str
    mapped_annotation: str
    result_arrow_type: str
    result_provenance: str
    python_value_type: str
    verdict: str

    def as_cells(self) -> tuple[str, ...]:
        """
        Return this row's values in :data:`ARTIFACT_HEADERS` order.

        Returns:
            One string per artifact column.
        """
        return (
            self.backend,
            self.field_name,
            self.role,
            self.metadata_raw_type,
            self.metadata_provenance,
            self.mapped_annotation,
            self.result_arrow_type,
            self.result_provenance,
            self.python_value_type,
            self.verdict,
        )


ARTIFACT_HEADERS: tuple[str, ...] = (
    "Backend",
    "Field",
    "Role",
    "Warehouse type",
    "Metadata provenance",
    "Mapped annotation",
    "Result Arrow type",
    "Result provenance",
    "Python value type",
    "Verdict",
)
"""Column headers of the artifact's comparison table, in :meth:`FidelityRow.as_cells` order."""

BACKEND_ORDER: tuple[str, ...] = ("duckdb", "snowflake", "databricks")
"""
Fixed backend order for the artifact's rows.

Row order is ``(BACKEND_ORDER index, field_name)``. Determinism is load-bearing: the drift
guard compares bytes, so ordering must never be a source of diff.
"""

REPO_ROOT = Path(__file__).resolve().parents[1]
"""
Repository root, one level above ``tests/``.

The default anchor for :func:`resolve_artifact_path` and the base for :func:`_cassette_root`.
"""

ARTIFACT_PHASE_DIR = "47-type-fidelity-probe-decision-doc"
"""The phase directory holding the committed artifact, live or archived."""

ARTIFACT_FILENAME = "47-TYPE-FIDELITY.md"
"""File name of the committed artifact."""


def resolve_artifact_path(repo_root: Path = REPO_ROOT, *, required: bool = True) -> Path:
    """
    Locate the committed artifact, following its phase directory through archival.

    ``gsd-cleanup`` moves a completed phase directory out of ``.planning/phases/`` and into
    ``.planning/milestones/<version>-phases/`` when its milestone closes. A path pinned to the
    live location would stop resolving at that moment, and ``--check`` would then report the
    artifact stale on every run — a red ``just test`` that looks like drift in the evidence
    when it is really a moved file. Both locations are searched instead, live first.

    Args:
        repo_root: The directory holding ``.planning/``. Defaults to :data:`REPO_ROOT`.
        required: Raise when the artifact is in neither location, rather than returning the
            live path for a caller that is about to create it.

    Returns:
        The first existing candidate, or the live phase-directory path when nothing exists
        and ``required`` is false.

    Raises:
        FileNotFoundError: If ``required`` and the artifact is in neither location. There is
            deliberately no fall back to an empty document: reading "not found" as "nothing
            committed yet" would make the staleness guard fail for a reason that has nothing
            to do with the generator.
    """
    planning = repo_root / ".planning"
    live = planning / "phases" / ARTIFACT_PHASE_DIR / ARTIFACT_FILENAME
    archived = sorted(
        planning.glob(f"milestones/*-phases/{ARTIFACT_PHASE_DIR}/{ARTIFACT_FILENAME}")
    )
    for candidate in (live, *archived):
        if candidate.exists():
            return candidate
    if required:
        msg = (
            f"{ARTIFACT_FILENAME} is committed nowhere under {planning}. Looked in "
            f"phases/{ARTIFACT_PHASE_DIR}/ and milestones/*-phases/{ARTIFACT_PHASE_DIR}/."
        )
        raise FileNotFoundError(msg)
    return live


def escape_cell(text: str) -> str:
    """
    Render one measured value as a single markdown table cell.

    Every cell in this document is measured, so no cell's content is under the renderer's
    control: a DuckDB composite type or a ``json.dumps`` descriptor can carry a literal ``|``,
    and a raw ``|`` opens a new column. The row would then be one cell wider than the header
    and every value after the pipe would sit under the wrong column.

    The escape is reversible: a reader that splits on unescaped pipes and unescapes the rest
    gets the literal back, so the value round-trips rather than being sanitized away.

    Args:
        text: The cell's measured value.

    Returns:
        The value with each ``|`` backslash-escaped and any line break flattened to a space.
    """
    return text.replace("|", "\\|").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def sort_rows(rows: Sequence[FidelityRow]) -> list[FidelityRow]:
    """
    Order rows deterministically by backend then field name.

    Args:
        rows: Measured rows in collection order.

    Returns:
        A new list ordered by ``(BACKEND_ORDER index, field_name)``.
    """
    return sorted(rows, key=lambda row: (BACKEND_ORDER.index(row.backend), row.field_name))


def render_artifact(
    rows: Sequence[FidelityRow],
    sections: Sequence[str] = (),
    leading_sections: Sequence[str] = (),
) -> str:
    """
    Render the committed comparison artifact.

    Emits no timestamp and no other value that changes between runs, so regenerating an
    unchanged measurement leaves the file byte-identical.

    Args:
        rows: Measured rows, in any order.
        sections: Pre-rendered markdown sections appended after the comparison table, in
            order. Each must begin with its own ``##`` heading. Defaults to none, so a caller
            holding only rows still renders a valid table-only document.
        leading_sections: Pre-rendered sections placed between the provenance legend and the
            comparison table. The driver-capability table goes here: it answers a different
            question from the comparison table and a reader has to meet that distinction
            before reading a single result type.

    Returns:
        The full markdown document, newline-terminated.

    Raises:
        ValueError: If a row does not carry exactly one cell per artifact column.
    """
    lines: list[str] = [
        "# Phase 47: warehouse type fidelity, measured",
        "",
        "Generated by `just type-fidelity`. Do not edit by hand — edits are overwritten on",
        "regeneration.",
        "",
        "Every cell below was measured. Nothing here is asserted from Semolina's own type map,",
        "except the `Mapped annotation` column, which *is* the type map and is the thing under",
        "measurement.",
        "",
        "## Provenance legend",
        "",
        "- `live` — measured in this process against a real driver connection.",
        "- `cassette-file` — read from a committed pytest-adbc-replay cassette. Real warehouse",
        "  evidence about result types; no evidence about what the live driver implements.",
        "- `derived-from-code` — produced by reading Semolina's own source rather than a",
        "  warehouse. Any `derived-from-code` in a result column would be circular evidence.",
        "- `driver-source` — read from the ADBC driver's published source at a pinned version.",
        "",
        "A result-provenance cell names the probe route in parentheses: `execute-schema` when",
        "the driver answered `adbc_execute_schema`, `zero-row` when it refused and the",
        "`SELECT * FROM (...) WHERE 1=0` fallback was used instead.",
    ]
    for section in leading_sections:
        lines.append("")
        lines.extend(section.splitlines())
    lines += [
        "",
        "## Field type comparison",
        "",
        "| " + " | ".join(escape_cell(head) for head in ARTIFACT_HEADERS) + " |",
        "|" + "---|" * len(ARTIFACT_HEADERS),
    ]
    for row in sort_rows(rows):
        cells = row.as_cells()
        if len(cells) != len(ARTIFACT_HEADERS):
            msg = (
                f"FidelityRow emitted {len(cells)} cells but the table has "
                f"{len(ARTIFACT_HEADERS)} columns."
            )
            raise ValueError(msg)
        lines.append("| " + " | ".join(escape_cell(cell) for cell in cells) + " |")
    for section in sections:
        lines.append("")
        lines.extend(section.splitlines())
    lines.append("")
    return "\n".join(lines)


# -- Collection ---------------------------------------------------------------------------

DUCKDB_PROBE_METRICS: tuple[str, ...] = (
    "total_order_value",
    "max_order_value",
    "total_order_count",
    "avg_order_count",
    "min_order_count",
    "n_order_totals",
    "region_list",
)
"""
The seven metrics of :data:`PROBE_VIEW_DDL`, in DDL declaration order.

They are the measurement surface for TYPE-01, chosen so each named disagreement has both a
positive case and a contrast case: ``total_order_value``/``max_order_value`` for decimal
widening, ``avg_order_count``/``total_order_count`` for ``AVG``, and
``n_order_totals``/``min_order_count`` for ``COUNT``. This tuple and the ``METRICS`` block of
:data:`PROBE_VIEW_DDL` must stay in step — a metric declared in the DDL and missing here is a
gap nothing else would make visible.
"""

DUCKDB_PROBE_DIMENSIONS: tuple[str, ...] = ("region",)
"""The dimensions of :data:`PROBE_VIEW_DDL`, grouped by in the probe query."""

DUCKDB_PROBE_FIELDS: tuple[str, ...] = DUCKDB_PROBE_METRICS + DUCKDB_PROBE_DIMENSIONS
"""Every field the DuckDB half emits a row for."""

EMPTY_GROUP_REGION = "CA"
"""
The seed group whose dimension key is non-NULL and whose every metric input is NULL.

Row 4 of :data:`PROBE_SEED_DML` is ``(4, NULL, NULL, 'CA')``, so the ``CA`` group exists but
has nothing to aggregate. This is the shape that separates NULL-able aggregates from ``COUNT``.
"""

UNMATCHED_FILTER_REGION = "ZZ-NO-SUCH-REGION"
"""
A dimension value no seed row carries, used to measure the *other* empty shape.

Filtering on it produces a ``GROUP BY`` that matches nothing, which is a different observation
from :data:`EMPTY_GROUP_REGION` and yields a different answer.
"""


def probe_sql_for(field_name: str) -> tuple[str, list[Any]]:
    """
    Build the probe SQL for one metric through Semolina's own DuckDB builder.

    Built rather than pasted so the probe measures the statement a user's query produces.
    Probing through ``semantic_view(...)`` matters: a hand-written ``SELECT SUM(...)`` over
    the base table reports types the extension casts away before users ever see them.

    This is the *minimal* query for a single metric, quoted in the artifact's named
    disagreements. :func:`probe_sql_all` is what the comparison table is measured through.

    Args:
        field_name: A metric declared on :class:`TypeFidelityView`.

    Returns:
        The SQL template and its bind parameters.
    """
    from semolina.engines.sql import DuckDBDialect

    builder = DuckDBDialect().create_builder()
    query = TypeFidelityView.query().metrics(getattr(TypeFidelityView, field_name))
    return builder.build_select_with_params(query)


def probe_sql_all() -> tuple[str, list[Any]]:
    """
    Build the probe SQL selecting every metric, grouped by every dimension.

    One query rather than eight: every measured field then comes out of a single planned
    statement, so the comparison table cannot describe eight differently-planned queries that
    happen to share a view name.

    Returns:
        The SQL template and its bind parameters.
    """
    from semolina.engines.sql import DuckDBDialect

    builder = DuckDBDialect().create_builder()
    query = (
        TypeFidelityView.query()
        .metrics(*(getattr(TypeFidelityView, name) for name in DUCKDB_PROBE_METRICS))
        .dimensions(*(getattr(TypeFidelityView, name) for name in DUCKDB_PROBE_DIMENSIONS))
    )
    return builder.build_select_with_params(query)


def measure_empty_group_values(engine: Engine) -> dict[str, object]:
    """
    Observe what each metric returns for a group whose inputs are all NULL.

    The Arrow ``nullable`` flag cannot answer this — it reads ``True`` for every field on
    every backend measured, ``COUNT`` included. The answer comes from observed values on the
    :data:`EMPTY_GROUP_REGION` group instead.

    Args:
        engine: A DuckDB engine carrying the probe fixture.

    Returns:
        Metric name -> the Python value observed for that metric on the all-NULL group.

    Raises:
        LookupError: If the seed group is absent, which would mean the measurement silently
            described a different group than the one it names.
    """
    sql, params = probe_sql_all()
    with engine.connect() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(sql, params or None)
            rows: list[dict[str, object]] = cursor.fetch_arrow_table().to_pylist()
        finally:
            cursor.close()

    for row in rows:
        if row.get("region") == EMPTY_GROUP_REGION:
            return {name: row[name] for name in DUCKDB_PROBE_METRICS}

    msg = (
        f"No {EMPTY_GROUP_REGION!r} group in the probe result; the all-NULL seed row is "
        "missing, so nothing here would be measuring empty-group nullability."
    )
    raise LookupError(msg)


def measure_unmatched_filter_rows(engine: Engine) -> int:
    """
    Count the rows a ``GROUP BY`` returns when its filter matches nothing.

    A separate observation from :func:`measure_empty_group_values`, and it answers
    differently. The success criterion's phrase "metric nullability on empty groups" reads as
    though an unmatched filter yields a row of NULLs; it does not, and the artifact has to say
    which of the two shapes it measured.

    Args:
        engine: A DuckDB engine carrying the probe fixture.

    Returns:
        The number of result rows, which is expected to be zero.
    """
    from semolina.engines.duckdb import _sql_str_literal

    sql, params = probe_sql_all()
    literal = _sql_str_literal(UNMATCHED_FILTER_REGION)
    with engine.connect() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(f"SELECT * FROM ({sql}) WHERE region = {literal}", params or None)
            rows: list[dict[str, object]] = cursor.fetch_arrow_table().to_pylist()
        finally:
            cursor.close()
    return len(rows)


def measure_versions(engine: Engine) -> tuple[str, str]:
    """
    Read the DuckDB and ``semantic_views`` versions the measurement actually ran against.

    Read rather than quoted from ``pyproject.toml``: a measurement is only reproducible if the
    artifact names the versions that produced it, and a hand-written version string is one
    more claim nobody checked.

    Args:
        engine: A DuckDB engine carrying the probe fixture.

    Returns:
        The DuckDB version and the ``semantic_views`` extension version.
    """
    with engine.connect() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT version()")
            duckdb_version = str(cursor.fetchall()[0][0])
            cursor.execute(
                "SELECT extension_version FROM duckdb_extensions() "
                "WHERE extension_name = 'semantic_views'"
            )
            extension_rows = cursor.fetchall()
        finally:
            cursor.close()
    extension_version = str(extension_rows[0][0]) if extension_rows else "unknown"
    return duckdb_version, extension_version


@dataclass(frozen=True)
class ProbeEvidence:
    """
    The measured facts the artifact states outside the comparison table.

    Grouped rather than passed loose because they are read off one probe run, and a renderer
    holding only some of them would state the rest unmeasured.

    Attributes:
        empty_group_values: Metric name -> value observed on the all-NULL group.
        nullable_flags: Field name -> the Arrow schema's ``nullable`` flag for that field.
        unmatched_filter_row_count: Rows returned when the filter matches nothing.
        duckdb_version: The DuckDB version the measurement ran against.
        extension_version: The ``semantic_views`` extension version it ran against.
        probe_route: The route :func:`probe_schema` took on DuckDB. Carried so the driver
            capability table's DuckDB row is a live measurement rather than a quoted claim
            — the one row in that table not answered from driver source.
    """

    empty_group_values: dict[str, object]
    nullable_flags: dict[str, bool]
    unmatched_filter_row_count: int
    duckdb_version: str
    extension_version: str
    probe_route: str


@dataclass(frozen=True)
class DuckDBMeasurement:
    """
    Everything the DuckDB half measures in one probe run.

    Attributes:
        rows: One :class:`FidelityRow` per entry in :data:`DUCKDB_PROBE_FIELDS`.
        evidence: The observations that are not table columns.
    """

    rows: list[FidelityRow]
    evidence: ProbeEvidence


def measure_duckdb() -> DuckDBMeasurement:
    """
    Measure the DuckDB half of the comparison, live and in-process.

    Reaches every field by two independent routes: ``Engine.introspect`` plus a raw
    ``DESCRIBE`` for the metadata half, and :func:`probe_schema` plus ``to_pylist()`` for the
    result half. One ``semantic_view(...)`` query covers all eight fields.

    Returns:
        The measured rows plus the nullability evidence.
    """
    engine = make_probe_engine()
    try:
        view = engine.introspect(PROBE_VIEW_NAME)
        by_name = {field.name: field for field in view.fields}
        dimensions = sorted(f.name for f in view.fields if f.field_type == "dimension")
        metrics = sorted(f.name for f in view.fields if f.field_type == "metric")

        sql, params = probe_sql_all()
        rows: list[FidelityRow] = []
        with engine.connect() as conn:
            cursor = conn.cursor()
            try:
                raw_types = describe_raw_types(cursor, PROBE_VIEW_NAME, dimensions, metrics)
                probed = probe_schema(cursor, sql, params)
                value_types = probe_value_types(cursor, sql, params)
                nullable_flags = {
                    name: bool(probed.schema.field(name).nullable) for name in DUCKDB_PROBE_FIELDS
                }
                for field_name in DUCKDB_PROBE_FIELDS:
                    introspected = by_name[field_name]
                    mapped = introspected.data_type or ""
                    value_type = value_types[field_name]
                    rows.append(
                        FidelityRow(
                            backend="duckdb",
                            field_name=field_name,
                            role=introspected.field_type,
                            metadata_raw_type=raw_types[field_name],
                            metadata_provenance="live",
                            mapped_annotation=mapped,
                            result_arrow_type=str(probed.schema.field(field_name).type),
                            result_provenance=f"live ({probed.route})",
                            python_value_type=value_type,
                            verdict=classify_verdict(mapped, value_type),
                        )
                    )
            finally:
                cursor.close()

        duckdb_version, extension_version = measure_versions(engine)
        evidence = ProbeEvidence(
            empty_group_values=measure_empty_group_values(engine),
            nullable_flags=nullable_flags,
            unmatched_filter_row_count=measure_unmatched_filter_rows(engine),
            duckdb_version=duckdb_version,
            extension_version=extension_version,
            probe_route=probed.route,
        )
        return DuckDBMeasurement(rows=rows, evidence=evidence)
    finally:
        engine.dispose()


# -- Recorded cassettes: the Snowflake and Databricks halves -------------------------------
#
# Neither collector below touches pytest or the pytest-adbc-replay plugin. Both read the
# committed cassettes straight off disk with ``pyarrow.ipc.open_file``, so
# ``just type-fidelity`` stays a plain script — and so the artifact's numbers come from the
# same bypass path a reviewer would use by hand. That the replay cursor agrees with these
# reads is asserted separately, by ``tests/integration/test_type_fidelity.py``.


def _cassette_root() -> Path:
    """
    Resolve the cassette directory from ``pyproject.toml``'s ``adbc_cassette_dir``.

    Read rather than hard-coded, for the same reason
    ``tests/integration/test_type_fidelity.py`` reads it: the replayed half and this raw
    half must land on one tree, and a second hard-coded copy of the path is exactly how
    they would drift apart.

    Returns:
        The absolute cassette root.
    """
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        config: dict[str, Any] = tomllib.load(handle)
    configured = config["tool"]["pytest"]["ini_options"]["adbc_cassette_dir"]
    return REPO_ROOT / str(configured)


CASSETTE_ROOT = _cassette_root()
"""The committed cassette tree, per ``adbc_cassette_dir``."""

SNOWFLAKE_PROBE_CASSETTE = (
    CASSETTE_ROOT
    / "integration"
    / "test_type_fidelity"
    / "test_snowflake_probe_copied_cassette"
    / "adbc_driver_snowflake.dbapi"
)
"""Snowflake's recorded ``sales_view`` query, copied from the ``test_queries`` recording."""

DATABRICKS_PROBE_CASSETTE = (
    CASSETTE_ROOT
    / "integration"
    / "test_type_fidelity"
    / "test_databricks_probe_copied_cassette"
    / "adbc_driver_manager.dbapi"
    / "databricks"
)
"""Databricks' recorded ``sales_view`` query; note the extra dialect path segment."""

DATABRICKS_INTROSPECT_CASSETTE = (
    CASSETTE_ROOT
    / "integration"
    / "test_introspect"
    / "test_databricks_introspect_metric_view"
    / "adbc_driver_manager.dbapi"
    / "databricks"
)
"""
Databricks' recorded ``DESCRIBE EXTENDED sales_view AS JSON`` payload.

The only real warehouse introspection evidence this phase has. Snowflake has no
counterpart, which is why its metadata cells are labelled ``derived-from-code``.
"""

PROVENANCE_CASSETTE = "cassette-file"
"""Read from a committed recording: real warehouse evidence about result types."""

PROVENANCE_DERIVED = "derived-from-code"
"""Produced by reading Semolina's own source rather than a warehouse."""

PROVENANCE_DRIVER_SOURCE = "driver-source"
"""Read from the ADBC driver's published source at a pinned version."""

PROVENANCE_LIVE = "live"
"""Measured in this process against a real driver connection."""


def _read_cassette_table(cassette_dir: Path) -> pyarrow.Table:
    """
    Read a cassette's recorded result table.

    Cassettes are Arrow IPC *file* format, so ``open_file`` is correct and ``open_stream``
    raises ``ArrowInvalid`` on them.

    Args:
        cassette_dir: A cassette directory holding ``000_result.arrow``.

    Returns:
        The recorded table.
    """
    with pyarrow.ipc.open_file(cassette_dir / "000_result.arrow") as reader:
        return reader.read_all()


def _cassette_result_cells(cassette_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    """
    Read a recording's result Arrow types and the Python types its values arrive as.

    The value half mirrors :func:`probe_value_types` — one pass over the recorded rows,
    NULLs skipped per column — so a recorded field and a live one are read the same way.

    Args:
        cassette_dir: A cassette directory holding ``000_result.arrow``.

    Returns:
        Field name -> Arrow type name, and field name -> Python value type name.
    """
    table = _read_cassette_table(cassette_dir)
    schema: Any = table.schema
    arrow_types = {str(field.name): str(field.type) for field in schema}

    rows: list[dict[str, Any]] = table.to_pylist()
    value_types: dict[str, str] = {}
    for name in arrow_types:
        value_types[name] = "NoneType"
        for row in rows:
            value = row.get(name)
            if value is not None:
                value_types[name] = python_value_type_name(value)
                break
    return arrow_types, value_types


SNOWFLAKE_RECORDING_DDL = (
    "CREATE TABLE sales_data (revenue NUMBER, cost NUMBER, country VARCHAR, region VARCHAR)"
)
"""
The DDL the Snowflake cassettes were recorded against, from ``tests/integration/conftest.py``.

Quoted here because it is the *whole* provenance of the Snowflake metadata cells: no
Snowflake introspection cassette exists, so those cells are derived by running this DDL's
declared types through ``snowflake_json_type_to_python`` rather than read from a warehouse.
"""

SNOWFLAKE_DERIVED_METADATA: dict[str, tuple[str, dict[str, object]]] = {
    "revenue": ("metric", {"type": "FIXED", "scale": 0}),
    "country": ("dimension", {"type": "TEXT"}),
}
"""
Result field -> its role and the Snowflake JSON type descriptor its column reports.

Derived from :data:`SNOWFLAKE_RECORDING_DDL`: a bare ``NUMBER`` is ``NUMBER(38,0)``, which
Snowflake's metadata API reports as ``{"type": "FIXED", "scale": 0}``, and a ``VARCHAR``
reports as ``{"type": "TEXT"}``. Deliberately *not* taken from the hand-fed rows in
``tests/unit/test_snowflake_engine.py``: that mock asserts the answer the type map already
produces, so quoting it would make the comparison circular (RESEARCH.md option (c), ruled
out). The derivation is labelled ``derived-from-code`` in the artifact instead.
"""

DATABRICKS_FIELD_SOURCES: dict[str, str] = {
    "revenue": "revenue",
    "country": "country",
}
"""
Result field name -> the introspection column it corresponds to.

Since ALIAS-01 the result columns carry the query's aliases, so both halves share a
spelling. Unaliased, Databricks named the metric's result column ``measure(revenue)``
while ``DESCRIBE EXTENDED ... AS JSON`` named the same field ``revenue``.
"""


def _databricks_introspection_columns() -> dict[str, dict[str, Any]]:
    """
    Read the recorded ``DESCRIBE EXTENDED sales_view AS JSON`` column descriptors.

    Only ``type.name`` and ``is_measure`` are consumed downstream. The payload also carries
    a ``nullable`` key per column, which ``DatabricksEngine.introspect``'s parse loop reads
    past — so "introspection does not capture nullability" is a fact about Semolina's code
    on this backend, not a warehouse limitation.

    Returns:
        Column name -> its recorded descriptor.
    """
    rows: list[dict[str, Any]] = _read_cassette_table(DATABRICKS_INTROSPECT_CASSETTE).to_pylist()
    payload: dict[str, Any] = json.loads(str(rows[0]["json_metadata"]))
    columns: list[dict[str, Any]] = payload["columns"]
    return {str(column["name"]): column for column in columns}


def collect_snowflake_rows() -> list[FidelityRow]:
    """
    Build the Snowflake half: recorded result cells, derived metadata cells.

    The asymmetry is the point. The result and value cells come from a real recording; the
    metadata cells are a derivation and say so, because Snowflake introspection has no
    cassette anywhere in this repo.

    Returns:
        One :class:`FidelityRow` per entry in :data:`SNOWFLAKE_DERIVED_METADATA`.
    """
    from semolina.codegen.type_map import snowflake_json_type_to_python

    arrow_types, value_types = _cassette_result_cells(SNOWFLAKE_PROBE_CASSETTE)

    rows: list[FidelityRow] = []
    for field_name, (role, descriptor) in SNOWFLAKE_DERIVED_METADATA.items():
        mapped = snowflake_json_type_to_python(descriptor)
        annotation = mapped if mapped is not None else f"{TODO_PREFIX}{json.dumps(descriptor)}"
        value_type = value_types[field_name]
        rows.append(
            FidelityRow(
                backend="snowflake",
                field_name=field_name,
                role=role,
                metadata_raw_type=json.dumps(descriptor),
                metadata_provenance=PROVENANCE_DERIVED,
                mapped_annotation=annotation,
                result_arrow_type=arrow_types[field_name],
                result_provenance=PROVENANCE_CASSETTE,
                python_value_type=value_type,
                verdict=classify_verdict(annotation, value_type),
            )
        )
    return rows


def collect_databricks_rows() -> list[FidelityRow]:
    """
    Build the Databricks half, with both cells read from recordings.

    Databricks is the only backend here whose metadata *and* result cells are both real
    warehouse evidence: the query cassette supplies the result schema and the introspection
    cassette supplies the raw column types.

    Returns:
        One :class:`FidelityRow` per entry in :data:`DATABRICKS_FIELD_SOURCES`.
    """
    from semolina.codegen.type_map import databricks_type_to_python

    arrow_types, value_types = _cassette_result_cells(DATABRICKS_PROBE_CASSETTE)
    columns = _databricks_introspection_columns()

    rows: list[FidelityRow] = []
    for field_name, source_column in DATABRICKS_FIELD_SOURCES.items():
        column = columns[source_column]
        type_obj: dict[str, object] = column["type"]
        raw_type = str(type_obj["name"])
        mapped = databricks_type_to_python(type_obj)
        annotation = mapped if mapped is not None else f"{TODO_PREFIX}{raw_type}"
        value_type = value_types[field_name]
        rows.append(
            FidelityRow(
                backend="databricks",
                field_name=field_name,
                role="metric" if column.get("is_measure") else "dimension",
                metadata_raw_type=raw_type,
                metadata_provenance=PROVENANCE_CASSETTE,
                mapped_annotation=annotation,
                result_arrow_type=arrow_types[field_name],
                result_provenance=PROVENANCE_CASSETTE,
                python_value_type=value_type,
                verdict=classify_verdict(annotation, value_type),
            )
        )
    return rows


# There is deliberately no ``collect_rows()`` aggregator over the three collectors above.
# ``main`` needs both halves of one :class:`DuckDBMeasurement` — ``rows`` for the table and
# ``evidence`` for the capability table and the disagreement prose — so it calls
# :func:`measure_duckdb` once and concatenates the cassette collectors onto ``measurement.rows``
# itself. Routing the table through an aggregator would run the live DuckDB probe a second
# time and let the table describe a different run from the prose beside it.


# -- Named disagreements ------------------------------------------------------------------

DISAGREEMENT_HEADINGS: tuple[str, ...] = (
    "Decimal precision widening under SUM",
    "`AVG(int)` -> double",
    "`COUNT` -> int64",
    "Metric nullability on empty groups",
)
"""
The four disagreements ROADMAP success criterion 2 requires to be named individually.

Criterion 2 explicitly rejects a pass/fail summary, so each of these gets its own subsection
carrying the minimal query that produced it, the measured Arrow type, and a contrast case that
makes it a finding rather than a coincidence.
"""


def _minimal_query_block(field_name: str) -> list[str]:
    """
    Render the minimal ``semantic_view(...)`` query for one metric as a fenced code block.

    Built through :func:`probe_sql_for` rather than pasted, so the quoted query cannot drift
    away from the statement the probe actually issues.

    Args:
        field_name: A metric declared on :class:`TypeFidelityView`.

    Returns:
        Markdown lines, ready to extend into a document.
    """
    sql, _params = probe_sql_for(field_name)
    return ["```sql", *sql.splitlines(), "```"]


def _render_observed_value(value: object) -> str:
    """
    Render an empty-group observation without giving warehouse row data a path into the file.

    Only ``None`` and ``0`` are printed verbatim — they are the two answers the nullability
    question turns on and neither is data. Anything else is reduced to its type name, so a
    future re-seed against a real dataset cannot leak a value into a committed artifact
    (threat T-47-01).

    Args:
        value: A single observed metric value.

    Returns:
        A markdown fragment naming the observation.
    """
    if value is None:
        return "`None`"
    if isinstance(value, int) and not isinstance(value, bool) and value == 0:
        return "`0`"
    return f"non-NULL (`{python_value_type_name(value)}`)"


PROSE_WIDTH = 92
"""
Wrap width for generated prose paragraphs.

Interpolated Arrow types vary in length, so paragraphs are assembled as single strings and
wrapped here rather than hand-broken. Wrapping is a pure function of the text, which keeps
regeneration byte-stable for the drift guard.
"""


def _paragraph(text: str) -> list[str]:
    """
    Wrap one prose paragraph to :data:`PROSE_WIDTH`, followed by a blank line.

    Hyphen and long-word breaking are both disabled. Generated prose is dense with inline
    code spans, and a hyphenated one broken across lines (``derived-from-code`` becoming
    ``derived-`` + ``from-code``) renders with a stray space inside the code span — a
    provenance label a reviewer greps for, silently corrupted by the wrapper.

    Args:
        text: The paragraph, with any incidental newlines and runs of spaces.

    Returns:
        Wrapped markdown lines plus a trailing blank line.
    """
    collapsed = " ".join(text.split())
    wrapped = textwrap.wrap(
        collapsed, width=PROSE_WIDTH, break_long_words=False, break_on_hyphens=False
    )
    return [*wrapped, ""]


def render_disagreements(rows: Sequence[FidelityRow], evidence: ProbeEvidence) -> str:
    """
    Render the ``## Named disagreements`` section.

    Reads its measured types out of the same :class:`FidelityRow` list the comparison table
    renders, so the prose and the table cannot drift apart.

    Args:
        rows: The measured rows.
        evidence: The observations that are not table columns.

    Returns:
        A markdown section beginning with its own ``##`` heading.

    Raises:
        KeyError: If a field named by a subsection was not measured.
    """
    by_field = {row.field_name: row for row in rows if row.backend == "duckdb"}

    def arrow(field_name: str) -> str:
        return f"`{by_field[field_name].result_arrow_type}`"

    def python(field_name: str) -> str:
        return f"`{by_field[field_name].python_value_type}`"

    def warehouse(field_name: str) -> str:
        return f"`{by_field[field_name].metadata_raw_type}`"

    lines: list[str] = ["## Named disagreements", ""]
    lines += _paragraph(
        "Four disagreements between what the catalogue says a field is and what the "
        "warehouse actually returns. Each names the minimal query that produced it, the "
        "measured Arrow type, the Python type the value arrives as, and a contrast case — "
        "without the contrast a single measurement is a coincidence rather than a rule."
    )
    lines += _paragraph(
        f"All four were measured through `semantic_view(...)` against DuckDB "
        f"{evidence.duckdb_version} with `semantic_views` {evidence.extension_version}, both "
        "read from the running database rather than quoted from `pyproject.toml`. Vendor "
        "rules are cited where a vendor publishes one and marked undocumented where none "
        "exists; a rule is never inferred from a measurement."
    )

    lines += [f"### 1. {DISAGREEMENT_HEADINGS[0]}", "", "Minimal query:", ""]
    lines += [*_minimal_query_block("total_order_value"), ""]
    lines += _paragraph(
        f"`total_order_value` is `SUM(o.order_total)` over an `order_total DECIMAL(10, 2)` "
        f"column. The catalogue reports {warehouse('total_order_value')}, the result schema "
        f"resolves to {arrow('total_order_value')}, and the value arrives as "
        f"{python('total_order_value')}. Precision went to 38: an aggregate's result type is "
        "not its input's type."
    )
    lines += _paragraph(
        f"**Contrast:** `max_order_value` is `MAX(o.order_total)` over the *same* column and "
        f"measures {arrow('max_order_value')} — no widening. Two aggregates reading one input "
        "column do not collapse into one result type, and only accumulating aggregates widen."
    )
    lines += _paragraph(
        "**Vendor rules.** Databricks documents `sum(DECIMAL(p, s))` as "
        "`DECIMAL(p + min(10, 31-p), s)`. Snowflake publishes no SUM precision rule at all — "
        'its SUM page says only that values are summed into "an equivalent or larger data '
        'type" — so the Snowflake cell is undocumented and measured only.'
    )

    lines += [f"### 2. {DISAGREEMENT_HEADINGS[1]}", "", "Minimal query:", ""]
    lines += [*_minimal_query_block("avg_order_count"), ""]
    lines += _paragraph(
        f"`avg_order_count` is `AVG(o.order_count)` over an `order_count INTEGER` column. The "
        f"catalogue reports {warehouse('avg_order_count')}, the result schema resolves to "
        f"{arrow('avg_order_count')}, and the value arrives as {python('avg_order_count')}. "
        "Averaging leaves the integer domain entirely."
    )
    lines += _paragraph(
        f"**Contrast:** `total_order_count` is `SUM(o.order_count)` over the same `INTEGER` "
        f"column and measures {arrow('total_order_count')} -> {python('total_order_count')}. "
        "It is the aggregate that decides, not the column."
    )
    lines += _paragraph(
        "**Vendor rules.** Databricks documents `avg(DECIMAL(p, s))` as `DECIMAL(p+4, s+4)` "
        'and DOUBLE "in all other cases", which puts Databricks and DuckDB in disagreement on '
        "`AVG(decimal)` specifically. Snowflake documents no AVG return type at all: "
        "undocumented, and unmeasured here."
    )

    lines += [f"### 3. {DISAGREEMENT_HEADINGS[2]}", "", "Minimal query:", ""]
    lines += [*_minimal_query_block("n_order_totals"), ""]
    lines += _paragraph(
        f"`n_order_totals` is `COUNT(o.order_total)`. The catalogue reports "
        f"{warehouse('n_order_totals')}, the result schema resolves to "
        f"{arrow('n_order_totals')}, and the value arrives as {python('n_order_totals')}."
    )
    lines += _paragraph(
        f"**Contrast:** `min_order_count` is `MIN(o.order_count)` over an `INTEGER` column and "
        f'measures {arrow("min_order_count")}, not {arrow("n_order_totals")}. "Integer '
        'metric" is therefore not one Arrow type, and a type map keyed on the column type '
        "would get one of these two wrong whichever width it picked."
    )
    lines += _paragraph(
        "**The trap this row encodes.** A hand-written "
        "`SELECT SUM(order_count) FROM type_fidelity_orders` over that same column measures "
        "`decimal128(38, 0)` and arrives as `decimal.Decimal`, because plain DuckDB sums an "
        "`INTEGER` into a `HUGEINT`. Through `semantic_view(...)` the extension casts down and "
        f"the user receives {arrow('total_order_count')} -> {python('total_order_count')}. "
        "Probing outside `semantic_view(...)` would record a type nobody ever receives, so "
        "`tests/unit/test_type_fidelity_duckdb.py` asserts both halves and the probe cannot be "
        "moved onto the wrong path unnoticed."
    )

    lines += [f"### 4. {DISAGREEMENT_HEADINGS[3]}", ""]
    lines += _paragraph(
        f"Minimal query — every metric grouped by `region`, where the seed row "
        f"`(4, NULL, NULL, '{EMPTY_GROUP_REGION}')` gives the `{EMPTY_GROUP_REGION}` group a "
        "non-NULL key and all-NULL metric inputs:"
    )
    sql, _params = probe_sql_all()
    lines += ["```sql", *sql.splitlines(), "```", ""]
    lines += [f"Observed on the `{EMPTY_GROUP_REGION}` group:", ""]
    lines += [
        f"- `{name}` -> {_render_observed_value(evidence.empty_group_values[name])}"
        for name in DUCKDB_PROBE_METRICS
    ]
    lines.append("")
    lines += _paragraph(
        "So metric nullability is **not uniform**. `SUM`, `AVG`, `MIN`, and `MAX` all go NULL "
        "on a group with nothing to aggregate, while `COUNT` returns `0`. A blanket "
        "`T | None` over every metric would be wrong for `COUNT`; a blanket `T` would be wrong "
        "for the other four."
    )
    lines += _paragraph(
        "**A GROUP BY that matches nothing returns zero rows, not a row of NULLs.** Filtering "
        f"the same query on a region no seed row carries returned "
        f"{evidence.unmatched_filter_row_count} rows. The NULLs above appear only because the "
        'group exists and has no non-NULL inputs. The phrase "metric nullability on empty '
        'groups" covers those two different shapes and only one of them produces a NULL; both '
        "were measured here."
    )
    lines += _paragraph(
        "**The Arrow `nullable` flag carries no information.** Every field measured reports "
        "`nullable` as True, `n_order_totals` included, even though `COUNT` demonstrably never "
        "returns NULL. Nullability cannot be read off the probe, so no acceptance criterion in "
        "this phase is built on that flag: it is a policy call decided from the aggregate's "
        "semantics. The measured flags, in full:"
    )
    lines += [
        f"- `{name}` -> `nullable={evidence.nullable_flags[name]}`" for name in DUCKDB_PROBE_FIELDS
    ]

    return "\n".join(lines)


# -- Downstream Decimal consumers ----------------------------------------------------------

DECIMAL_PROBE_FIELD = "total_order_value"
"""The decimal metric every downstream consumer is measured against."""

STATUS_MEASURED = "measured"
"""The consumer was exercised in this process and its answer recorded."""

STATUS_NOT_MEASURED = "not measured"
"""The consumer was not exercised; the observed cell says why, naming the package."""

DOWNSTREAM_CONSUMERS: tuple[str, ...] = ("to_pylist", "pandas", "pydantic", "polars")
"""
The four consumers of a decimal metric, in the order the artifact lists them.

Fixed rather than derived from a dict's insertion order so a consumer cannot be dropped from
the artifact by a refactor that only touches the measuring code.
"""


@dataclass(frozen=True)
class DownstreamObservation:
    """
    What one downstream consumer does with a ``decimal128`` metric.

    Attributes:
        consumer: One of :data:`DOWNSTREAM_CONSUMERS`.
        observed: The measured answer, or a reason naming the package that made it
            unmeasurable.
        status: :data:`STATUS_MEASURED` or :data:`STATUS_NOT_MEASURED`.
        assumption: The RESEARCH.md assumption this row closes or leaves open, by A-number,
            or ``"—"`` when the row corresponds to no assumption.
    """

    consumer: str
    observed: str
    status: str
    assumption: str


def _measure_pandas(table: Any) -> DownstreamObservation:
    """
    Measure what ``pyarrow.Table.to_pandas()`` does with the decimal column.

    Closes RESEARCH.md assumption A2, which predicted an ``object`` dtype holding
    ``decimal.Decimal`` rather than ``float64``. Imported inside the function so an absent
    pandas produces an honest artifact row instead of an import error.

    Args:
        table: The probe result table.

    Returns:
        The observation for the ``pandas`` row.
    """
    try:
        import pandas
    except ImportError:
        return DownstreamObservation(
            consumer="pandas",
            observed="not measured — pandas not installed",
            status=STATUS_NOT_MEASURED,
            assumption="A2",
        )

    column = table.to_pandas()[DECIMAL_PROBE_FIELD]
    element_type = python_value_type_name(column.iloc[0])
    return DownstreamObservation(
        consumer="pandas",
        observed=(
            f"pandas {pandas.__version__}: dtype `{column.dtype}`, elements `{element_type}`"
        ),
        status=STATUS_MEASURED,
        assumption="A2",
    )


def _measure_pydantic(value: object) -> DownstreamObservation:
    """
    Measure whether a pydantic v2 ``Decimal`` field accepts the observed value unchanged.

    Closes RESEARCH.md assumption A1. "Without coercion loss" is checked by equality *and* by
    the validated field still being a ``decimal.Decimal`` — a model that quietly returned a
    ``float`` would satisfy equality for these seed values and still have lost the guarantee.

    Args:
        value: A single decimal metric value straight off ``to_pylist()``.

    Returns:
        The observation for the ``pydantic`` row.
    """
    try:
        import pydantic
    except ImportError:
        return DownstreamObservation(
            consumer="pydantic",
            observed="not measured — pydantic not installed",
            status=STATUS_NOT_MEASURED,
            assumption="A1",
        )

    class DecimalModel(pydantic.BaseModel):
        """One decimal field, the shape a generated DTO would carry for a money metric."""

        amount: decimal.Decimal

    validated = DecimalModel.model_validate({"amount": value}).amount
    lossless = validated == value and type(validated) is decimal.Decimal
    verdict = "accepted unchanged" if lossless else "COERCED — value or type changed"
    return DownstreamObservation(
        consumer="pydantic",
        observed=f"pydantic {pydantic.VERSION}: `decimal.Decimal` field {verdict}",
        status=STATUS_MEASURED,
        assumption="A1",
    )


def _measure_polars(table: Any) -> DownstreamObservation:
    """
    Measure what ``polars.from_arrow()`` does with the decimal column.

    Closes RESEARCH.md assumption A3, which Phase 47 had to leave open for one reason only:
    polars was not installed. Phase 49's ``[polars]`` extra put it in the test environment, so
    the row is a measurement rather than a gap. Presence is still detected with ``find_spec``
    rather than a ``try`` / ``except ImportError``, so an environment without the extra
    produces an honest artifact row and polars is never imported there.

    Args:
        table: The probe result table.

    Returns:
        The observation for the ``polars`` row.
    """
    if importlib.util.find_spec("polars") is None:
        return DownstreamObservation(
            consumer="polars",
            observed="not measured — polars not installed",
            status=STATUS_NOT_MEASURED,
            assumption="A3",
        )

    import polars

    # `from_arrow` is the call `fetch_polars()` itself makes, so it is the one measured here
    # rather than the equivalent `polars.DataFrame(table)`. Its return is annotated
    # `DataFrame | Series` unconditionally — a Table always yields a DataFrame — so the union
    # is narrowed rather than indexed, which is what polars' own docstring recommends.
    frame = polars.from_arrow(table)
    assert isinstance(frame, polars.DataFrame), f"polars returned a {type(frame).__name__}"
    column = frame.get_column(DECIMAL_PROBE_FIELD)
    element_type = python_value_type_name(column[0])
    return DownstreamObservation(
        consumer="polars",
        observed=(
            f"polars {polars.__version__}: dtype `{column.dtype}`, elements `{element_type}`"
        ),
        status=STATUS_MEASURED,
        assumption="A3",
    )


def measure_downstream_decimal() -> dict[str, DownstreamObservation]:
    """
    Measure what each downstream consumer does with a ``decimal128`` metric.

    The Decimal policy Phase 48 has to choose turns on these four answers, and three of them
    were assumptions in RESEARCH.md. Measuring them here costs one probe query and no new
    dependency.

    Returns:
        Consumer name -> its observation, one entry per :data:`DOWNSTREAM_CONSUMERS`.
    """
    engine = make_probe_engine()
    try:
        sql, params = probe_sql_for(DECIMAL_PROBE_FIELD)
        with engine.connect() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(sql, params or None)
                table = cursor.fetch_arrow_table()
            finally:
                cursor.close()
    finally:
        engine.dispose()

    rows: list[dict[str, object]] = table.to_pylist()
    value = rows[0][DECIMAL_PROBE_FIELD]

    return {
        "to_pylist": DownstreamObservation(
            consumer="to_pylist",
            observed=f"`{python_value_type_name(value)}`",
            status=STATUS_MEASURED,
            assumption="—",
        ),
        "pandas": _measure_pandas(table),
        "pydantic": _measure_pydantic(value),
        "polars": _measure_polars(table),
    }


def render_downstream_decimal(observations: Mapping[str, DownstreamObservation]) -> str:
    """
    Render the ``## Downstream Decimal behaviour`` section.

    Args:
        observations: The mapping :func:`measure_downstream_decimal` returns.

    Returns:
        A markdown section beginning with its own ``##`` heading.

    Raises:
        KeyError: If a consumer named in :data:`DOWNSTREAM_CONSUMERS` was not measured.
    """
    lines: list[str] = ["## Downstream Decimal behaviour", ""]
    lines += _paragraph(
        f"What each consumer of a `decimal128` metric does with it, measured on "
        f"`{DECIMAL_PROBE_FIELD}`. The Decimal policy turns on these answers, and three of "
        "them were assumptions in RESEARCH.md rather than measurements."
    )
    lines += [
        "| Consumer | Observed | Status | RESEARCH.md assumption |",
        "|---|---|---|---|",
    ]
    for consumer in DOWNSTREAM_CONSUMERS:
        row = observations[consumer]
        cells = (row.consumer, row.observed, row.status, row.assumption)
        lines.append("| " + " | ".join(escape_cell(cell) for cell in cells) + " |")
    lines.append("")
    lines += _paragraph(
        "**A1 (pydantic v2 supports `Decimal` fields natively)**, **A2 (`to_pandas()` renders "
        "decimal128 as an `object` dtype holding `Decimal`, not `float64`)** and **A3 (polars "
        "Decimal support is partial)** are all closed by the rows above. A3 was left open in "
        "Phase 47 only because polars was not installed; Phase 49's `polars` extra put it in "
        "the test environment and the answer is better than pandas' — polars gives the column "
        "a native `Decimal(precision, scale)` dtype holding `decimal.Decimal`, where pandas "
        "falls back to an untyped `object` column. One condition this probe does not exercise: "
        "polars was measured during Phase 49 research raising a Rust `PanicException` "
        "(`operator does not support primitive Int256`) on a `decimal256` column, and no "
        "backend this project supports has been observed producing one — a Snowflake `NUMBER` "
        "stops at precision 38, and no Databricks decimal column has ever been recorded in "
        "this repository."
    )
    lines += _paragraph(
        "One caveat on reproducing the pandas and polars rows. Both are declared extras of "
        "this project rather than base dependencies — Phase 49 added `pandas` and `polars` as "
        "published extras and put both inside `all` — so they are present only when `all` is "
        "synced. CI syncs `--dev --extra all`, so both rows are measured there; regenerating "
        "after a sync that omits `--extra all` will legitimately flip them to `not measured`, "
        "and that is the artifact reporting its environment rather than a fault."
    )
    return "\n".join(lines).rstrip("\n")


# -- Driver capability ---------------------------------------------------------------------

CAPABILITY_HEADERS: tuple[str, ...] = (
    "Driver",
    "Version checked",
    "`adbc_execute_schema` implemented",
    "Caveat",
    "Fallback needed",
    "Capability provenance",
)
"""
Column headers of the ``## Driver capability`` table.

Deliberately shares no header text with :data:`ARTIFACT_HEADERS`. The two tables answer
different questions from different sources, and a shared column would be the first step
towards a cell that carries both a capability claim and a result-type claim.
"""

SNOWFLAKE_FILTERED_CASSETTE = (
    CASSETTE_ROOT
    / "integration"
    / "test_queries"
    / "test_filtered_by_dimension_snowflake_engine_"
    / "adbc_driver_snowflake.dbapi"
)
"""
The recorded Snowflake query carrying a bind parameter.

Quoted in the evidence limitations rather than paraphrased: the claim "Semolina generates a
parameterized shape on Snowflake" is checkable against this file, and a paraphrase would not
be.
"""


def render_capability_table(evidence: ProbeEvidence) -> str:
    """
    Render the ``## Driver capability`` section.

    Two of the three rows are answered from driver source at a pinned version, because that
    is the only source that can answer them: a replayed ``adbc_execute_schema`` succeeds
    whatever the real driver does. The DuckDB row is the exception and is measured live.

    Args:
        evidence: The DuckDB probe's observations, for the measured route and version.

    Returns:
        A markdown section beginning with its own ``##`` heading.
    """
    answered = evidence.probe_route == ROUTE_EXECUTE_SCHEMA
    rows: tuple[tuple[str, ...], ...] = (
        (
            "snowflake",
            "`adbc-driver-snowflake` 1.10.0 (Foundry tag `go/v1.10.0`)",
            "yes",
            "implemented in `go/statement.go` via `gosnowflake.WithDescribeOnly`, which is a"
            " describe-only metadata round trip rather than a warehouse execution; refuses with"
            ' `StatusNotImplemented` ("executing schema with bound params not yet implemented")'
            " whenever bind parameters are present",
            "only for parameterized queries",
            PROVENANCE_DRIVER_SOURCE,
        ),
        (
            "databricks",
            "Foundry `go/v0.1.3`",
            "no",
            "`go/statement.go` embeds `driverbase.StatementImplBase` and defines no"
            " `ExecuteSchema`, so the inherited `driverbase-go` default returns"
            " `StatusNotImplemented`",
            "yes, the zero-row fallback is the only path",
            PROVENANCE_DRIVER_SOURCE,
        ),
        (
            "duckdb",
            f"duckdb {evidence.duckdb_version}",
            "yes" if answered else "no",
            f"probed in this process and answered by the `{evidence.probe_route}` route; the"
            " `WHERE 1=0` fallback returns an equal schema"
            " (`test_zero_row_fallback_matches_execute_schema`)",
            "no" if answered else "yes",
            PROVENANCE_LIVE,
        ),
    )

    lines: list[str] = ["## Driver capability", ""]
    lines += _paragraph(
        "Whether each driver implements ADBC `ExecuteSchema`. Phases 48 and 50 read this "
        "table so they stop rediscovering the answer per driver."
    )
    lines += [
        "| " + " | ".join(escape_cell(head) for head in CAPABILITY_HEADERS) + " |",
        "|" + "---|" * len(CAPABILITY_HEADERS),
    ]
    lines += ["| " + " | ".join(escape_cell(cell) for cell in row) + " |" for row in rows]
    lines.append("")
    lines += _paragraph(
        '"Driver X implements `adbc_execute_schema`" and "field F came back as type T" are '
        "two different claims with two different sources. The first is answered from driver "
        "source at a pinned version; the second is answered from a recording. This table "
        "holds only the first and `## Field type comparison` holds only the second, and the "
        "two share no column, so no cell in this document carries both."
    )
    lines += _paragraph(
        "Cassette replay in particular is no evidence of capability: pytest-adbc-replay "
        "serves `adbc_execute_schema` by reading the schema off the recorded result table, "
        "whatever the real driver does. That is why the Databricks row above reads `no` while "
        "a replayed Databricks probe still returns a schema, and why the provenance column "
        "here reads `driver-source` rather than `cassette-file`."
    )
    return "\n".join(lines).rstrip("\n")


# -- Evidence limitations --------------------------------------------------------------------


def render_evidence_limitations() -> str:
    """
    Render the ``## Evidence limitations`` section.

    Each entry names what is missing, why it is missing, and what would close it. A gap
    stated in writing can be closed later; a gap left as a silent absence reads as coverage.

    Returns:
        A markdown section beginning with its own ``##`` heading.
    """
    filtered_sql = (SNOWFLAKE_FILTERED_CASSETTE / "000_query.sql").read_text(encoding="utf-8")

    lines: list[str] = ["## Evidence limitations", ""]
    lines += _paragraph(
        "What this document does not establish. Every item here is a gap in the evidence, "
        "not a finding, and none of it is worked around by asserting the answer."
    )

    lines += ["### No Snowflake introspection cassette exists", ""]
    lines += _paragraph(
        "`tests/integration/test_introspect.py` is Databricks-only, and Snowflake "
        "introspection is covered nowhere else by a recording. Its only coverage is a "
        "hand-fed mock in `tests/unit/test_snowflake_engine.py`, which feeds "
        '`{"type": "FIXED", "scale": 0}` in and asserts `decimal.Decimal` comes out, so it '
        "asserts the answer the type map already produces. That mock is deliberately **not** "
        "used as "
        "evidence here: quoting it would make the comparison circular. The Snowflake "
        "metadata cells are instead derived by running the recording fixture's declared "
        "types through `snowflake_json_type_to_python`, and are labelled "
        "`derived-from-code` so a reviewer sees the derivation rather than inferring a "
        "measurement. The fixture DDL those cells derive from:"
    )
    lines += ["```sql", SNOWFLAKE_RECORDING_DDL, "```", ""]
    lines += _paragraph(
        "**What would close it:** one recording session against a live Snowflake account, "
        "adding a `SHOW COLUMNS IN VIEW` cassette. Nothing else about the phase changes."
    )

    lines += ["### Snowflake decimal widening is not demonstrable", ""]
    lines += _paragraph(
        "The Snowflake recording fixture declares `revenue NUMBER`, which is "
        "`NUMBER(38,0)` — already at maximum precision. A `SUM` over it cannot widen, so "
        "the measured `decimal128(38, 0)` is consistent with widening but demonstrates "
        "none of it. The DuckDB rows are the only place in this document where widening is "
        "shown end to end. **What would close it:** a `NUMBER(10,2)` column in the "
        "recording fixture, which makes Snowflake widening measurable in a single "
        "re-record."
    )

    lines += ["### Databricks has no decimal column to measure", ""]
    lines += _paragraph(
        "The Databricks fixture declares `revenue BIGINT, cost BIGINT` and no decimal "
        "column at all, so this document carries no Databricks decimal row. That is an "
        "absence, not a negative result: Databricks publishes a widening rule "
        "(`sum(DECIMAL(p, s))` -> `DECIMAL(p + min(10, 31-p), s)`) that nothing here "
        "checks. **What would close it:** a decimal column in the Databricks recording "
        "fixture."
    )

    lines += ["### The Databricks zero-row fallback works — measured 2026-08-15", ""]
    lines += _paragraph(
        "This entry was open from 2026-08-12 to 2026-08-15 and is kept, answered, rather "
        "than deleted: the capability table states that Databricks needs the `WHERE 1=0` "
        "fallback because the driver implements no `ExecuteSchema`, and whether that "
        "fallback actually works there was a separate question nobody had answered. It is "
        "answered now, by a live session against the Databricks workspace during Phase 50 "
        "UAT. `adbc_execute_schema` raised `NotSupportedError` — a genuine refusal, so the "
        "capability row above is confirmed from behaviour as well as from driver source — "
        "`probe_schema` fell through and returned `route='zero-row'`, and the metric-view "
        "planner accepted `SELECT * FROM (<MEASURE(...) ... GROUP BY ALL>) WHERE 1=0`. The "
        "rejection branch, which would have made this a Phase 48 blocker rather than a "
        "footnote, did not occur. Reproduce: "
        "`.planning/phases/50-codegen-d-typed-dtos/verify_databricks_live.py`. **What is "
        "still not measured:** the schemas of the wrapped and unwrapped queries were not "
        "compared field for field on Databricks — the wrapped route typed a query whose "
        "generated DTO then round-tripped through `.into()` against real rows, which is "
        "stronger evidence for the same claim but not that comparison."
    )

    lines += ["### Snowflake's AVG return type is undocumented and unmeasured", ""]
    lines += _paragraph(
        "Snowflake publishes no return type for `AVG` and no precision rule for `SUM`; its "
        '`SUM` page says only that values are summed into "an equivalent or larger data '
        'type". No recording in this repo carries a Snowflake `AVG` metric either, so this '
        "document states neither a rule nor a measurement for it. **What would close it:** "
        "an `AVG` metric on the Snowflake recording fixture. Until then, do not infer "
        "Snowflake's `AVG` behaviour from the DuckDB row."
    )

    lines += ["### Snowflake refuses to probe a query carrying bind parameters", ""]
    lines += _paragraph(
        "A forward constraint on Phase 48's `--check` mode rather than a gap in this "
        "document. Semolina keeps `?` placeholders on Snowflake, so a filtered canonical "
        "query is a parameterized statement, and the Snowflake driver refuses "
        "`ExecuteSchema` outright whenever parameters are bound. The recorded filtered "
        "query, from "
        "`cassettes/integration/test_queries/test_filtered_by_dimension_snowflake_engine_/`:"
    )
    lines += ["```sql", *filtered_sql.strip().splitlines(), "```", ""]
    lines += ['- The refusing shape: `WHERE "COUNTRY" = ?`. One bound parameter is enough.', ""]
    lines += _paragraph(
        "So a Phase 48 `--check` over a filtered canonical query hits the refusal on "
        "Snowflake and gets nothing back. It has to probe the unfiltered query shape, or "
        "inline literals for the probe only. Note that the zero-row fallback is not a way "
        "out here: it would run a real query against the warehouse, which is exactly what "
        "the describe-only route exists to avoid."
    )

    return "\n".join(lines).rstrip("\n")


# -- Entry point --------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """
    Regenerate or verify the committed comparison artifact.

    Args:
        argv: Command-line arguments, excluding the program name. Defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` on success. ``1`` when ``--check`` finds the committed file out of date; the
        unified diff is written to stderr.

    Raises:
        FileNotFoundError: Under ``--check``, when the committed artifact is in neither the
            live phase directory nor the milestone archive. That is a broken checkout, not
            drift, and it is reported as itself rather than as a stale artifact.
    """
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.strip().splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Regenerate and write the artifact.")
    mode.add_argument("--check", action="store_true", help="Fail if the artifact is stale.")
    args = parser.parse_args(argv)

    measurement = measure_duckdb()
    rows = measurement.rows + collect_snowflake_rows() + collect_databricks_rows()
    sections = [
        render_disagreements(measurement.rows, measurement.evidence),
        render_downstream_decimal(measure_downstream_decimal()),
        render_evidence_limitations(),
    ]
    rendered = render_artifact(
        rows,
        sections,
        leading_sections=[render_capability_table(measurement.evidence)],
    )

    if args.write:
        target = resolve_artifact_path(required=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_text(rendered, encoding="utf-8")
        return 0

    target = resolve_artifact_path()
    committed = target.read_text(encoding="utf-8")
    if committed == rendered:
        return 0
    diff = difflib.unified_diff(
        committed.splitlines(keepends=True),
        rendered.splitlines(keepends=True),
        fromfile=f"{target} (committed)",
        tofile=f"{target} (regenerated)",
    )
    sys.stderr.writelines(diff)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
