"""
Compare a committed model's annotations against the warehouse's current result schema.

This is the engine behind ``semolina codegen --check``. It answers one question per field:
does the annotation sitting in the user's committed model still describe the type that
column arrives as? The authority is the **result schema** — what the driver says the query
will return — resolved by :func:`semolina.codegen.probe.probe_schema`, with the warehouse's
own metadata as a *labelled* fallback (Decision 3, 47-DECISIONS.md).

**Not a byte-diff.** TYPE-07's wording is "still match the warehouse's current *result
schema*". Regenerating the model and diffing the text would compare against the **metadata**
route, which is the route Phase 47 measured as disagreeing with the result schema — so a
byte-diff would answer a different question, and would also flag pure formatting noise.
Per-field comparison against a probed schema is the design; do not "simplify" it back.

**The route is always reported.** A green ``--check`` that silently fell back to warehouse
metadata would be indistinguishable from a probed one, which is false assurance rather than
assurance (threat T-48-24). Every row carries ``execute-schema``, ``zero-row`` or
:data:`ROUTE_METADATA`.

**No row value ever reaches a report.** Rows carry field names, annotation strings, routes
and a status — nothing else. The probe fetches no rows by construction, so no value is even
in scope (threat T-48-22).

**Metadata drifts too.** A field's ``description=``, ``label=``, ``synonyms=`` and
``semantic_format=`` are compared against what the engine introspects
(``Engine.introspected_metadata``, less any the view's ``unread_metadata`` names), as is the
class's ``description=`` against the view's comment. A mismatch is drift, not a merge: the
warehouse is what the model was generated from, so the committed copy is the stale one.

The generation path is deliberately untouched by all of this: ``semolina codegen`` still
builds models from warehouse metadata. Making generation probe-primary needs a canonical
query builder, an offline fallback chain and route recording in emitted source; that is
Phase 50's DTO-07/DTO-09 and D-01 defers it explicitly. The accepted consequence is that
generating a model and immediately checking it can legitimately report drift wherever the
two routes still disagree — Phase 47's central finding, surfaced rather than suppressed
(D-02).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from semolina.codegen.arrow_map import arrow_type_to_python
from semolina.codegen.probe import probe_schema
from semolina.codegen.python_renderer import metric_annotation
from semolina.codegen.type_compare import compared_annotation, generated_annotation

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pyarrow
    from s_layer_semantic_formats import SLayerBlock

    from semolina.codegen.model_reader import CommittedField, CommittedModel
    from semolina.engines.base import Engine
    from semolina.engines.sql import SQLDialect
    from semolina.introspection import IntrospectedField, IntrospectedView

__all__ = [
    "ROUTE_METADATA",
    "ROUTE_NOT_PROBED",
    "STATUS_MATCH",
    "STATUS_DRIFT",
    "ABSENT",
    "FieldCheckRow",
    "ViewCheckReport",
    "check_view",
]


ROUTE_METADATA = "metadata"
"""
Route used when the probe was unavailable, so the annotation came from warehouse metadata.

The third route alongside :data:`semolina.codegen.probe.ROUTE_EXECUTE_SCHEMA` and
:data:`semolina.codegen.probe.ROUTE_ZERO_ROW`. It is a **labelled** fallback: a row carrying
it was compared against ``IntrospectedField.data_type``, which is the mapped-metadata answer
rather than the result schema, and the CLI says so.
"""

ROUTE_NOT_PROBED = "not-probed"
"""
Route used when no probe examined this field, because the warehouse does not have it.

The row exists because the *committed model* declares the field; ``probed`` is
:data:`ABSENT` and nothing resolved it. Borrowing the probe's route here would make the
report claim a query looked at a column it never selected — the same false assurance
:data:`ROUTE_METADATA` exists to prevent, from the other direction (threat T-48-24).
"""

STATUS_MATCH = "match"
"""Row status meaning the committed annotation is the one the schema implies."""

STATUS_DRIFT = "drift"
"""Row status meaning the committed annotation is not the one the schema implies."""

ABSENT = "(absent)"
"""
Placeholder for a side of the comparison that has no value.

``committed == ABSENT`` means the warehouse has a field the model does not declare;
``probed == ABSENT`` means the model declares a field the warehouse does not have. Both are
drift, and distinguishing them is why fields are enumerated from the warehouse rather than
from the model.
"""

_UNMAPPED_ANNOTATION = "Any"
"""
The annotation for a type neither map resolves.

Matches the renderer's own rule: ``IntrospectedField.data_type`` of ``None`` or ``TODO: X``
renders as ``Any``, and :func:`~semolina.codegen.arrow_map.arrow_type_to_python` returning
``None`` means the same thing from the Arrow side.
"""


@dataclass(frozen=True)
class FieldCheckRow:
    """
    One field's verdict.

    Attributes:
        name: The field name, as the warehouse (or the committed model) spells it.
        committed: The annotation the committed model declares, or :data:`ABSENT`.
        probed: The annotation the result schema implies, or :data:`ABSENT`.
        route: What produced ``probed`` — ``execute-schema``, ``zero-row``, or
            :data:`ROUTE_METADATA`.
        status: :data:`STATUS_MATCH` or :data:`STATUS_DRIFT`.
        detail: Why the row drifted for a reason the two annotation columns cannot show
            — a changed role, a stale ``source=``, or metadata (description, label,
            synonyms) that no longer matches the warehouse's. Empty string when the annotations
            are the whole story, which is the usual case. Kept out of the table's five
            columns so the report stays readable; the CLI prints the non-empty ones
            underneath it.
    """

    name: str
    committed: str
    probed: str
    route: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class ViewCheckReport:
    """
    Every field's verdict for one view.

    Attributes:
        view_name: The view checked.
        rows: One row per field, warehouse fields first in warehouse order, then any field
            the committed model declares that the warehouse does not have.
        has_drift: True when any row drifted.
        probe_error: The reason the probe was unavailable, when it was. None on a probed
            run. Carried so the CLI can say *why* it fell back rather than only that it did.
        detail: Drift in the view's own metadata (its description), which no field row can
            show. Empty when there is none; when there is, ``has_drift`` is True.
    """

    view_name: str
    rows: list[FieldCheckRow]
    has_drift: bool
    probe_error: str | None = None
    detail: str = ""


def _canonical_model(view: IntrospectedView) -> Any:
    """
    Build a ``SemanticView`` subclass from an introspected view, at runtime.

    Fields are declared **untyped** (``Metric()``, the documented shorthand for
    ``Metric[Any]()``): the SQL builder needs names and roles, never annotations, and the
    annotations are the thing under measurement.

    Args:
        view: The introspection result.

    Returns:
        A ``SemanticView`` subclass carrying one field per introspected field.
    """
    from semolina.fields import Dimension, Fact, Metric
    from semolina.models import SemanticView, SemanticViewMeta

    role_to_class = {"metric": Metric, "dimension": Dimension, "fact": Fact}
    namespace: dict[str, Any] = {}
    for f in view.fields:
        field_class = role_to_class[f.field_type]
        namespace[f.name] = field_class(source=f.source_name) if f.source_name else field_class()
    return SemanticViewMeta(view.class_name, (SemanticView,), namespace, view=view.view_name)


def _field_groups(view: IntrospectedView, *, split_facts: bool) -> list[list[IntrospectedField]]:
    """
    Split a view's fields into the groups one query each can select.

    Args:
        view: The introspection result.
        split_facts: True for DuckDB, where ``semantic_view()`` cannot take ``facts`` and
            ``metrics`` in one call.

    Returns:
        One non-empty group per query to build.
    """
    metrics = [f for f in view.fields if f.field_type == "metric"]
    dimensions = [f for f in view.fields if f.field_type == "dimension"]
    facts = [f for f in view.fields if f.field_type == "fact"]

    if not split_facts:
        group = metrics + dimensions + facts
        return [group] if group else []

    # DuckDB: mirror how DuckDBEngine.introspect groups its DESCRIBE SELECT probes —
    # dimensions with metrics in one call, facts alone in the other. `DuckDBSQLBuilder` raises
    # ValueError when facts and metrics are both present (engines/sql.py:1234-1240).
    groups = [metrics + dimensions, facts]
    return [group for group in groups if group]


def _build_query(model: Any, group: list[IntrospectedField]) -> Any:
    """
    Build the unfiltered query selecting exactly one field group.

    Unfiltered — all metrics plus all dimensions, no WHERE — because Snowflake refuses
    ``ExecuteSchema`` for a query carrying any bound parameter, and ``--check`` has no filter
    to apply. The consequence is stated rather than hidden: ``--check`` measures the
    unfiltered result shape and says nothing about a filtered one.

    Args:
        model: The runtime ``SemanticView`` subclass.
        group: The fields to select.

    Returns:
        The query object.
    """
    query = model.query()
    metrics = [getattr(model, f.name) for f in group if f.field_type == "metric"]
    non_metrics = [getattr(model, f.name) for f in group if f.field_type != "metric"]
    if metrics:
        query = query.metrics(*metrics)
    if non_metrics:
        query = query.dimensions(*non_metrics)
    return query


def _result_field_names(dialect: SQLDialect, field: IntrospectedField) -> list[str]:
    """
    Name the result column an introspected field could arrive under, most likely first.

    Since phase 53 every builder aliases each selected column to its Python field name, so
    the field name leads the list and is what every backend returns. The rest are
    fallbacks for a schema that names a column some other way: the resolved warehouse name,
    then its SELECT-clause spelling (``wrap_metric`` for a metric, the quoted identifier
    otherwise). Every candidate is derived from the **same dialect that built the SQL**, so
    this stays correct by construction rather than by a table of per-backend spellings.

    The result-column spelling of an *unaliased* metric -- Databricks' ``measure(revenue)``,
    Snowflake's upper-cased ``AGG("GROSS REVENUE")`` -- is no longer offered. It came from
    ``Dialect.metric_result_column_name``, which existed only to read pre-53 recordings and
    was deleted with them in 53-05 (D2-5).

    Args:
        dialect: The engine's dialect.
        field: The introspected field.

    Returns:
        Candidate result-column names.
    """
    resolved = field.source_name or dialect.normalize_identifier(field.name)
    candidates = [field.name, resolved]
    if field.field_type == "metric":
        candidates.append(dialect.wrap_metric(resolved))
    else:
        candidates.append(dialect.quote_identifier(resolved))
    # Preserve order, drop duplicates.
    return list(dict.fromkeys(candidates))


def _arrow_annotation(
    dialect: SQLDialect, field: IntrospectedField, schema: pyarrow.Schema
) -> tuple[str, str] | None:
    """
    Resolve one field's annotation from a probed result schema.

    Uses ``get_all_field_indices`` rather than ``get_field_index``, which answers ``-1``
    both for a name the schema does not carry *and* for a name it carries more than once.
    Collapsing those two cases sent a duplicated column down the metadata route and made the
    CLI report "the result-schema probe was unavailable" — which is not what happened. The
    probe answered; its answer was ambiguous, and saying so is the honest result.

    Args:
        dialect: The engine's dialect, used to derive candidate result-column names.
        field: The introspected field.
        schema: A probed result schema.

    Returns:
        The annotation implied by the schema paired with a detail string (empty when the
        resolution was unambiguous), or None when the schema carries no column for this
        field — which sends the caller to the metadata route for that field alone.
    """
    for name in _result_field_names(dialect, field):
        indices = schema.get_all_field_indices(name)
        if len(indices) == 1:
            mapped = arrow_type_to_python(schema.field(indices[0]).type)
            return (mapped if mapped is not None else _UNMAPPED_ANNOTATION), ""
        if len(indices) > 1:
            return _UNMAPPED_ANNOTATION, (
                f"ambiguous: the result schema carries {len(indices)} columns named {name!r}"
            )
    return None


_ROLE_TO_CLASS = {"metric": "Metric", "dimension": "Dimension", "fact": "Fact"}
"""The field descriptor each warehouse role is declared with, mirroring the renderer's map."""


def _non_annotation_drift(
    dialect: SQLDialect, field: IntrospectedField, committed_field: CommittedField
) -> str:
    """
    Describe any drift the two annotation columns cannot show.

    A committed model can be wrong about a field in two ways that leave the annotation
    intact, and both change the SQL it builds:

    * the **role**. ``Metric`` and ``Dimension`` land in different ``semantic_view()``
      clauses. A metric's probed annotation gains ``| None``, so *some* role changes surface
      as annotation drift already — but ``Dimension[int | None]`` against a warehouse metric
      compares equal, and the model is still wrong.
    * the **resolved column name**. A ``source=`` naming a column the warehouse has since
      renamed makes every query select something that does not exist, while both sides still
      read ``str``.

    The column comparison is on the *resolved* name — ``source or normalize_identifier(name)``,
    which is exactly ``SQLBuilder._resolve_col_name``'s rule — rather than on the raw
    override. ``docs/src/how-to/codegen.rst`` documents ``source=`` as something you may add
    by hand, and the warehouse reports ``source_name=None`` for a name that already
    round-trips; comparing the raw values would report drift for a model that builds
    byte-identical SQL.

    Args:
        dialect: The engine's dialect, for the identifier folding rule.
        field: The introspected field.
        committed_field: The parsed committed declaration.

    Returns:
        A human-readable description, or the empty string when neither differs.
    """
    reasons: list[str] = []

    expected_class = _ROLE_TO_CLASS.get(field.field_type)
    if expected_class is not None and committed_field.field_class != expected_class:
        reasons.append(f"role: committed {committed_field.field_class}, warehouse {expected_class}")

    committed_column = committed_field.source_name or dialect.normalize_identifier(
        committed_field.name
    )
    warehouse_column = field.source_name or dialect.normalize_identifier(field.name)
    if committed_column != warehouse_column:
        reasons.append(f"column: committed {committed_column!r}, warehouse {warehouse_column!r}")

    return "; ".join(reasons)


def _metadata_drift(
    compared: frozenset[str], field: IntrospectedField, committed_field: CommittedField
) -> str:
    """
    Describe how a committed field's metadata arguments differ from the warehouse's.

    Only the attributes the engine reads are compared (``Engine.introspected_metadata``): a
    backend with no slot for a label reports none, and a committed ``label=`` is then not
    drift but something the warehouse cannot hold. A metadata argument the declaration
    leaves out reads as empty, which is what the warehouse reports for a member without one;
    one the reader could not evaluate (``CommittedField.unreadable``, already warned about)
    is not compared. Synonyms compare as a set, since their order carries no meaning.

    Args:
        compared: The metadata attribute names the engine introspects.
        field: The introspected field.
        committed_field: The parsed committed declaration.

    Returns:
        A human-readable description, or the empty string when nothing differs.
    """
    reasons: list[str] = []
    metadata = committed_field.metadata
    compared = compared - committed_field.unreadable
    if "description" in compared:
        committed = metadata.get("description") or ""
        if committed != field.description:
            reasons.append(f"description: committed {committed!r}, warehouse {field.description!r}")
    if "label" in compared:
        committed_label = metadata.get("label")
        if committed_label != field.label:
            reasons.append(f"label: committed {committed_label!r}, warehouse {field.label!r}")
    if "synonyms" in compared:
        committed_synonyms = metadata.get("synonyms", ())
        if not isinstance(committed_synonyms, tuple) or set(
            cast("tuple[object, ...]", committed_synonyms)
        ) != set(field.synonyms):
            reasons.append(
                f"synonyms: committed {committed_synonyms!r}, warehouse {field.synonyms!r}"
            )
    if "semantic_format" in compared:
        format_drift = _format_drift(metadata.get("semantic_format"), field.semantic_format)
        if format_drift:
            reasons.append(format_drift)
    return "; ".join(reasons)


def _format_text(block: SLayerBlock | None) -> str:
    """Write a block's normal form as compact JSON for a ``Detail`` line, or ``None``."""
    if block is None:
        return "None"
    return json.dumps(block.normal_form(), sort_keys=True, separators=(",", ":"))


def _format_drift(committed: object, warehouse: SLayerBlock | None) -> str:
    """
    Describe how a committed ``semantic_format=`` differs from the warehouse's block.

    The two compare by normal form, so a committed format spelled with defaults left out,
    or keys in another order, matches the block codegen would write.

    Args:
        committed: The committed argument as the reader evaluated it, or None for none.
        warehouse: The block the warehouse's tags make, or None for none.

    Returns:
        A human-readable description, or the empty string when they match.
    """
    from s_layer_semantic_formats import SLayerValidationError, parse_block

    if committed is None:
        committed_block = None
    else:
        try:
            committed_block = parse_block(cast("Mapping[str, Any]", committed))
        except (SLayerValidationError, TypeError):
            return (
                "semantic_format: committed is not a valid block, "
                f"warehouse {_format_text(warehouse)}"
            )
    if committed_block == warehouse:
        return ""
    return (
        f"semantic_format: committed {_format_text(committed_block)}, "
        f"warehouse {_format_text(warehouse)}"
    )


def _view_metadata_drift(
    compared: frozenset[str], view: IntrospectedView, committed: CommittedModel | None
) -> str:
    """
    Describe how a committed class's ``description=`` differs from the view's comment.

    A ``description=`` the reader could not evaluate (already warned about) is not
    compared.

    Args:
        compared: The metadata attribute names the engine introspects.
        view: The introspected view.
        committed: The parsed committed model, or None when the file declares no class for
            the view (the field rows already report that).

    Returns:
        A human-readable description, or the empty string when nothing differs.
    """
    if committed is None or "description" not in compared - committed.unreadable:
        return ""
    committed_description = committed.description or ""
    if committed_description == view.description:
        return ""
    return f"description: committed {committed_description!r}, warehouse {view.description!r}"


def _metadata_annotation(field: IntrospectedField) -> str:
    """
    Resolve one field's annotation from warehouse metadata — the fallback route.

    Args:
        field: The introspected field, carrying the type map's answer in ``data_type``.

    Returns:
        The mapped annotation, or ``'Any'`` for an unmapped type, matching the renderer.
    """
    if field.data_type is None or field.data_type.startswith("TODO:"):
        return _UNMAPPED_ANNOTATION
    return field.data_type


def _probe_view(
    engine: Engine, view: IntrospectedView
) -> tuple[list[tuple[pyarrow.Schema, str]], str]:
    """
    Probe the view's result schema, one query per field group.

    Each schema is returned **paired with the route that produced it**. A view can need more
    than one query — DuckDB cannot select facts and metrics in one ``semantic_view()`` call —
    and a driver may answer one and refuse another, which is the ordinary Snowflake shape
    (``ExecuteSchema`` for a parameter-free query, a refusal for a parameterized one). One
    route carried across the loop would label every row with whatever the last query did.

    Args:
        engine: A live engine.
        view: The introspection result.

    Returns:
        The probed ``(schema, route)`` pairs, and an error string when the probe was
        unavailable (empty otherwise). A non-empty error means the caller must use the
        metadata route and label every row with it.
    """
    from semolina.engines.sql import DuckDBSQLBuilder

    probes: list[tuple[pyarrow.Schema, str]] = []
    try:
        # Inside the try, not before it. Setting the probe up is part of the probe: a
        # catalogue column named `query` makes `SemanticViewMeta` reject the name, and an
        # unrecognized role makes `_canonical_model` raise KeyError. Both are
        # warehouse-shaped inputs, and outside the try either one escapes `check_view`,
        # escapes `_run_check`'s three narrow `except` clauses, and produces the traceback
        # the comment below says this design avoids.
        builder = engine.dialect.create_builder()
        groups = _field_groups(view, split_facts=isinstance(builder, DuckDBSQLBuilder))
        model = _canonical_model(view)

        with engine.connect() as conn:
            cursor = conn.cursor()
            try:
                for group in groups:
                    sql, params = builder.build_select_with_params(_build_query(model, group))
                    probed = probe_schema(cursor, sql, params)
                    probes.append((probed.schema, probed.route))
            finally:
                cursor.close()
    except Exception as e:  # noqa: BLE001 - any probe failure is a fallback, not a crash
        # Decision 3 makes metadata a *labelled* fallback rather than a failure mode. The
        # catch is broad on purpose: the caller's alternative is a traceback for a mode whose
        # whole job is to report, and the route on every row records that this happened.
        return [], f"{type(e).__name__}: {e}"

    return probes, ""


_ANY_SPELLINGS = frozenset({_UNMAPPED_ANNOTATION, metric_annotation(_UNMAPPED_ANNOTATION)})
"""``Any`` and ``Any | None``: one type, since ``Any`` already admits ``None``."""


def _annotations_agree(committed: str, probed: str) -> bool:
    """
    Compare a committed annotation with the probed one.

    Both arrive in codegen's spelling with their names resolved (D9's type comparison, see
    :mod:`semolina.codegen.type_compare`), so ``Optional[Decimal]`` and
    ``decimal.Decimal | None`` are already the same string here. What is left is string
    equality, with one exception: ``Any`` and ``Any | None`` are the same type. The
    documented ``Metric()`` shorthand is read back as ``Any``, while a probed metric the
    maps cannot type becomes ``Any | None`` by Decision 2, so an untyped metric on an
    untyped column reported drift on every run (GEN-09, A32). ``Any`` is not made a
    wildcard: against a concrete probed type it still drifts, because codegen would now
    write that type.

    Args:
        committed: The committed model's annotation, resolved.
        probed: The annotation the result schema implies, metric nullability applied.

    Returns:
        True when the two agree.
    """
    return committed == probed or {committed, probed} <= _ANY_SPELLINGS


def _match_renamed_fields(
    dialect: SQLDialect, view: IntrospectedView, committed_fields: dict[str, CommittedField]
) -> tuple[dict[str, CommittedField], dict[str, str]]:
    """
    Pair a committed field renamed by hand with the warehouse field whose column it reads.

    Fields are matched by attribute name first. A committed field whose name the warehouse
    does not have, but whose resolved column (``source=`` or its folded name) is the column
    of a warehouse field the model does not declare by name, is the same field under another
    name: ``sales_region = Dimension[str](source="region")`` builds exactly the SQL the
    generated ``region = Dimension[str]()`` builds (case M5). Hand-edited generated files
    are a supported input (D9).

    Args:
        dialect: The engine's dialect, for the identifier folding rule.
        view: The introspection result.
        committed_fields: The committed model's fields, by attribute name.

    Returns:
        The committed fields keyed by the warehouse name each one answers for, and for each
        renamed one, warehouse name -> the attribute name it was declared under.
    """
    names = {field.name for field in view.fields}
    by_column = {
        (field.source_name or dialect.normalize_identifier(field.name)): field.name
        for field in view.fields
    }
    matched: dict[str, CommittedField] = {}
    renamed: dict[str, str] = {}
    for name, field in committed_fields.items():
        if name not in names:
            column = field.source_name or dialect.normalize_identifier(field.name)
            target = by_column.get(column)
            if target is not None and target not in committed_fields and target not in matched:
                matched[target] = field
                renamed[target] = name
                continue
        matched[name] = field
    return matched, renamed


def check_view(engine: Engine, view_name: str, committed: CommittedModel | None) -> ViewCheckReport:
    """
    Report whether a committed model's annotations still match a view's result schema.

    Fields are enumerated from the **warehouse**, not from the committed model, which is what
    lets the report tell "this field was removed from the view" apart from "this field was
    added to the view". Both are drift.

    Args:
        engine: A live engine for the backend the view lives in.
        view_name: The view to check.
        committed: The parsed committed model for that view, or None when the committed file
            declares no class for it (every warehouse field then reads as :data:`ABSENT`).

    Returns:
        One row per field plus the aggregate verdict.

    Example:
        .. code-block:: python

            from semolina.codegen.annotation_check import check_view

            report = check_view(engine, "sales_view", committed)
            report.has_drift
            # False
    """
    view = engine.introspect(view_name)
    compared = engine.introspected_metadata - view.unread_metadata
    probes, probe_error = _probe_view(engine, view)

    committed_fields, renamed = (
        _match_renamed_fields(engine.dialect, view, committed.fields)
        if committed is not None
        else ({}, {})
    )
    rows: list[FieldCheckRow] = []

    for field in view.fields:
        probed_annotation: str | None = None
        probe_detail = ""
        field_route = ROUTE_METADATA
        # The route comes from the probe that *answered for this field*, not from the last
        # one the loop ran. A two-group view can be answered by two different routes.
        for schema, schema_route in probes:
            resolved = _arrow_annotation(engine.dialect, field, schema)
            if resolved is not None:
                probed_annotation, probe_detail = resolved
                field_route = schema_route
                break

        if probed_annotation is None:
            # No probed column for this field: fall back to metadata for this row alone and
            # say so, rather than dropping the field or reporting a spurious absence.
            probed_annotation = _metadata_annotation(field)
            field_route = ROUTE_METADATA

        if field.field_type == "metric":
            probed_annotation = metric_annotation(probed_annotation)

        committed_field = committed_fields.pop(field.name, None)
        committed_annotation = ABSENT if committed_field is None else committed_field.annotation
        # An ambiguous probe explains the row without deciding it: the status stays a
        # statement about the *model*, and an ambiguous probe resolves to `Any`, which
        # already drifts against any concrete committed annotation.
        model_detail = (
            ""
            if committed_field is None
            else "; ".join(
                d
                for d in (
                    _non_annotation_drift(engine.dialect, field, committed_field),
                    _metadata_drift(compared, field, committed_field),
                )
                if d
            )
        )
        rename_detail = (
            f"declared as {renamed[field.name]!r}, which reads the same column"
            if field.name in renamed
            else ""
        )
        detail = "; ".join(d for d in (probe_detail, model_detail, rename_detail) if d)
        status = (
            STATUS_MATCH
            if committed_field is not None
            and _annotations_agree(
                compared_annotation(committed_field), generated_annotation(probed_annotation)
            )
            and not model_detail
            else STATUS_DRIFT
        )
        rows.append(
            FieldCheckRow(
                name=field.name,
                committed=committed_annotation,
                probed=probed_annotation,
                route=field_route,
                status=status,
                detail=detail,
            )
        )

    # Anything left declares a field the warehouse does not have. No probe examined it, so
    # it gets its own label rather than borrowing one from a query that never selected it.
    for name, committed_field in committed_fields.items():
        rows.append(
            FieldCheckRow(
                name=name,
                committed=committed_field.annotation,
                probed=ABSENT,
                route=ROUTE_NOT_PROBED,
                status=STATUS_DRIFT,
            )
        )

    view_detail = _view_metadata_drift(compared, view, committed)
    return ViewCheckReport(
        view_name=view_name,
        rows=rows,
        has_drift=bool(view_detail) or any(row.status == STATUS_DRIFT for row in rows),
        probe_error=probe_error or None,
        detail=view_detail,
    )
