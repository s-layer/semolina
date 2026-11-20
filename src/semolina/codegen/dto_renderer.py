"""
Render a Pydantic DTO from a query's *probed* result schema.

The counterpart of :mod:`semolina.codegen.python_renderer`, which generates a Semolina model
from warehouse metadata. This one generates a plain ``pydantic.BaseModel`` from the schema
the driver says a specific query will return, so the annotations describe the query's result
rather than the view's declaration (DTO-07).

Three properties are load-bearing and none of them are local decisions:

* **Every annotation comes from** :func:`semolina.codegen.arrow_map.arrow_type_to_python`
  (D-06). Nothing here re-derives an Arrow type. A ``decimal128`` column annotates
  ``decimal.Decimal``, never ``float`` — which is also what Phase 49's ``.into()`` pre-check
  enforces, so re-deriving would let Semolina emit a class Semolina rejects.
* **Fields bind to result columns by name, never by position.** Column order is each
  builder's choice, not a contract: every builder now projects metrics before dimensions,
  but DuckDB's ``semantic_view()`` returned dimensions first until phase 53 projected it
  explicitly. A positional rule would tie every DTO to whichever order a builder happens to
  emit (threat T-50-06).
* **At most one plain-string alias per field, and usually none.** Since phase 53 every
  builder names a result column after its field, so a field whose column carries its name is
  emitted bare and binds that name (ALIAS-03); only a column spelled otherwise gets a
  ``validation_alias``. Pydantic's multi-alias form — the one that offers a
  list of candidate spellings — is never emitted, and this module contains no mention of it
  by name so that ``grep`` over this file is an honest check rather than one the
  explanation defeats. arrowmodel's ``ArrowModelConverter.__init__`` raises
  ``NotImplementedError`` for it before either conversion path is reachable, and Semolina's
  own ``_has_unsupported_alias`` refuses it at the pre-check for the same reason — so a
  generated DTO that emitted one would be rejected on its first use. Before phase 53 that
  pinned a generated DTO's *names* to the backend it was probed against (the corrected D-04;
  ``50-RESEARCH.md`` R-01 has the measured table). Its *annotations* still are (D-07), and
  its provenance header says so.
* **Every metric annotation is ``T | None``** (D-09), applied through
  :func:`semolina.codegen.python_renderer.metric_annotation` and never written inline here.
  That helper is the one place 47-DECISIONS Decision 2 lives, and ``semolina codegen
  --check`` decorates probed annotations through the same call — two copies would let a
  generated DTO and a drift report disagree about nullability. COUNT never returns NULL and
  is annotated ``int | None`` regardless: the over-approximation is deliberate and
  documented (47-DECISIONS Decision 2), because the two errors are not symmetric. A
  too-wide annotation is an unnecessary ``| None`` in a user's type checker; a too-narrow
  one is a live bug, putting a ``None`` into a non-Optional field on Phase 49's fast path
  and raising ``ValidationError`` under ``validate=True``.

A probe failure is fatal here. ``annotation_check._probe_view`` can afford a broad
``except Exception`` because it degrades to warehouse metadata; DTO codegen has no metadata
route, so degrading would mean emitting an empty or guessed DTO instead of an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader

# `_result_field_names` is private to `annotation_check` and imported anyway, deliberately:
# it is the single place a field's candidate result-column names are derived from the
# dialect that built the SQL, and `semolina codegen --check` resolves annotations through
# exactly the same list. A second copy here would drift from `--check` the first time either
# was revisited, and the Databricks metric spelling fixed in plan 50-01 shows what that costs.
from semolina.codegen.annotation_check import _result_field_names
from semolina.codegen.arrow_map import arrow_type_to_python
from semolina.codegen.dto_reader import DOCSTRING_PREFIX
from semolina.codegen.probe import probe_schema
from semolina.codegen.python_renderer import (
    _BUILTIN_ANNOTATION_NAMES,
    _python_str_literal,
    check_parses,
    format_with_ruff,
    metric_annotation,
)
from semolina.codegen.query_resolver import is_valid_class_name, projection_only, query_fields

if TYPE_CHECKING:
    import pyarrow

    from semolina.engines.base import Engine
    from semolina.engines.sql import SQLDialect
    from semolina.introspection import IntrospectedField
    from semolina.query import Query

__all__ = [
    "DTO_MODULE_NAMES",
    "ProbedQuery",
    "probe_query",
    "render_dtos",
    "render_and_format_dtos",
]


_TEMPLATE_DIR = Path(__file__).parent / "templates"

_UNMAPPED_ANNOTATION = "Any"
"""
The annotation for an Arrow type :func:`arrow_type_to_python` does not resolve.

Matches ``annotation_check._UNMAPPED_ANNOTATION`` and the model renderer's own rule: a
``None`` answer becomes ``Any`` plus a ``# TODO: <dtype>`` comment naming the type, rather
than a guess.
"""

_STDLIB_MODULE_PREFIXES: dict[str, str] = {
    "datetime.": "import datetime",
    "decimal.": "import decimal",
}
"""
Annotation prefix -> the stdlib import line that makes the annotation resolvable.

Matched by substring containment against the *resolved* annotation, never by equality, for
the same reason :mod:`semolina.codegen.python_renderer` does: a metric annotation carries a
``| None`` suffix (D-09), so an exact-membership test would drop ``import decimal`` for
exactly the fields that need it.
"""

_FUTURE_IMPORT = "annotations"
"""The one ``__future__`` feature every generated DTO module imports."""

_PYDANTIC_MODULE = "pydantic"
"""The module every generated DTO module imports, and the base class comes from."""

DTO_MODULE_NAMES: frozenset[str] = (
    frozenset(line.removeprefix("import ") for line in _STDLIB_MODULE_PREFIXES.values())
    | {_FUTURE_IMPORT, _UNMAPPED_ANNOTATION, _PYDANTIC_MODULE}
    | _BUILTIN_ANNOTATION_NAMES
)
"""
Every name a generated DTO module imports or refers to in an annotation.

A generated class may not take one of these. ``class Any(pydantic.BaseModel)`` rebinds the
``Any`` the module imports, and pydantic resolves the deferred annotations through the
module's globals, so every ``Any``-typed field then names the class. The set differs from
:data:`semolina.codegen.python_renderer.GENERATED_MODULE_NAMES` because the template does:
a DTO module imports ``pydantic`` and ``__future__.annotations`` and nothing from semolina.
Built from the constants :func:`_build_dto_import_lines` uses, so the two cannot drift.
"""


def _docstring_body(text: str) -> str:
    r"""
    Escape text for the inside of a triple-double-quoted Python string.

    ``repr`` is used for every other interpolated string, but not here: it would collapse a
    multi-line column comment onto one line with ``\n`` escapes, and a field docstring is
    something the user reads. Keeping the triple-quoted form means escaping only the two
    characters that can end it early -- a backslash, and a quote that could complete the
    closing delimiter.

    Every ``"`` is escaped rather than only the runs that would actually terminate the
    literal. A rule that has to reason about run lengths and trailing positions is a rule
    that gets an edge case wrong; ``""`` immediately before the closing delimiter is enough
    to break it, and so is a lone trailing ``"``.

    Args:
        text: The raw description.

    Returns:
        The body to place between the triple-quote delimiters.
    """
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _known_backend_label(dialect: SQLDialect) -> str | None:
    """
    Name the backend a dialect belongs to, when this repo knows it.

    Args:
        dialect: The dialect that built the probed SQL.

    Returns:
        The canonical backend label, the dialect's
        :attr:`~semolina.engines.sql.SQLDialect.kind`, or ``None`` for a dialect without
        one: a custom backend reached through ``--backend dotted.path.ClassName``, where the
        caller's label is the only name there is. A subclass of a built-in dialect inherits
        its parent's ``kind``, so it still answers that backend's label.
    """
    return dialect.kind.value if dialect.kind is not None else None


@dataclass(frozen=True)
class ProbedQuery:
    """
    One query's probed result schema, plus everything needed to render it.

    The query and the dialect travel with the schema rather than being passed alongside it,
    so a caller cannot render a schema against a dialect that did not build it. That pairing
    is the whole of threat T-50-06's mitigation: the candidate result-column names a field
    is bound through are derived from the dialect that produced the columns, and a mismatch
    would silently bind fields to the wrong columns instead of failing.

    Attributes:
        class_name: The generated DTO's Python class name, from
            :func:`semolina.codegen.query_resolver.class_name_for` or from the CLI's
            ``--name``. Validated here rather than trusted; see :meth:`__post_init__`.
        origin: Where the query came from, recorded so the generated file names its own
            provenance: a dotted path for an importable query, or the view-and-fields
            description :func:`semolina.codegen.query_resolver.ad_hoc_origin` builds for one
            named on the command line. Free text rather than a path, because there are two
            routes in and only one of them has a path — and a header that printed a
            view-and-fields description under a heading that said "dotted path" would be
            provenance the reader cannot act on.
        query: The user's query. Read for its projection only; the probe ran against the
            stripped twin (D-02).
        dialect: The dialect that built the probed SQL.
        schema: The result schema the driver resolved for the query's projection.
        route: :data:`semolina.codegen.probe.ROUTE_EXECUTE_SCHEMA` or
            :data:`semolina.codegen.probe.ROUTE_ZERO_ROW`. Carried into the provenance
            header so the reader knows which answer the schema is, rather than assuming the
            primary one.
    """

    class_name: str
    origin: str
    query: Query
    dialect: SQLDialect
    schema: pyarrow.Schema
    route: str

    def __post_init__(self) -> None:
        """
        Refuse a class name that is not a plain Python identifier.

        The template writes ``class {{ model.class_name }}(pydantic.BaseModel):``, so this
        is the one value the generator interpolates that is a bare token rather than a
        string literal — nothing quotes it, and a value carrying anything else closes the
        ``class`` statement and adds module-level code to a file the documented workflow
        tells the reader to import (threat T-50-01).

        Checked on the dataclass rather than inside :func:`render_dtos`, so the invariant
        holds for every route into the renderer: ``render_dtos`` is public API and
        :func:`probe_query` takes ``class_name`` from its caller, so validating in the CLI
        alone would leave the library path open. It also mirrors what the class already does
        for the dialect/schema pairing — the record refuses to exist in a state the renderer
        would have to defend against.

        Raises:
            ValueError: If ``class_name`` is not a Python identifier, or is a keyword.
        """
        if not is_valid_class_name(self.class_name):
            msg = (
                f"class_name={self.class_name!r} is not a valid Python identifier. It is "
                "written into the generated file as the class's own name -- a bare token, "
                "not a string literal -- so there is nothing to escape it with, and a value "
                "carrying anything else would add module-level code to a file that is meant "
                "to be imported."
            )
            raise ValueError(msg)


def probe_query(engine: Engine, query: Query, *, class_name: str, origin: str) -> ProbedQuery:
    """
    Build a query's projection-only SQL and resolve its result schema from the driver.

    The query is stripped through :func:`semolina.codegen.query_resolver.projection_only`
    before the SQL is built (D-02), which is also what guarantees the probed statement binds
    no parameters — the condition Snowflake's ``ExecuteSchema`` needs.

    There is deliberately no broad ``except Exception`` here. A probe failure propagates to
    the caller and becomes a reported error, because DTO codegen has no metadata route to
    degrade to and an empty DTO is worse than no DTO.

    Args:
        engine: The engine to probe against. Its dialect builds the SQL, so the result
            columns and the candidate names later matched against them come from one place.
        query: The user's query, filtered or not.
        class_name: The generated class's name. Must be a valid Python identifier and not a
            keyword; it is written into the generated file as a bare token.
        origin: Where the query came from — a dotted path, or a view-and-fields
            description. Recorded in the generated file's provenance header.

    Returns:
        The probed schema plus its route and naming.

    Raises:
        ValueError: If ``class_name`` is not a usable Python class name. Raised by
            :class:`ProbedQuery` on the way out, so a probe that succeeded still yields no
            record rather than an unusable one.
        Exception: Whatever the driver raises. Connection failures, missing views and
            malformed queries all surface rather than degrading.

    Example:
        .. code-block:: python

            from semolina.codegen.dto_renderer import probe_query

            probed = probe_query(
                engine,
                query,
                class_name="RevenueByRegion",
                origin="myapp.queries.revenue_by_region",
            )
            probed.route
            # 'execute-schema'
    """
    builder = engine.dialect.create_builder()
    sql, params = builder.build_select_with_params(projection_only(query))

    with engine.connect() as conn:
        cursor = conn.cursor()
        try:
            probed = probe_schema(cursor, sql, params)
        finally:
            cursor.close()

    return ProbedQuery(
        class_name=class_name,
        origin=origin,
        query=query,
        dialect=engine.dialect,
        schema=probed.schema,
        route=probed.route,
    )


def _alias_for(dialect: SQLDialect, field: IntrospectedField, schema: pyarrow.Schema) -> str:
    """
    Name the result column a projected field arrives under, checked against the schema.

    Walks the dialect-derived candidates most-likely-first and returns the first one the
    probed schema carries exactly once. This is D-03 and the corrected D-04 together: the
    DTO's *field name* comes from the model, the *alias* is generated, and it is validated
    against the columns the warehouse actually returned rather than assumed from the
    dialect's own spelling rules.

    ``get_all_field_indices`` rather than ``get_field_index``, which answers ``-1`` both for
    a name the schema lacks and for one it carries twice. Collapsing those would let an
    ambiguous column silently fall through to the next candidate and bind the field to a
    different column than the one it named.

    No candidate matching is a hard error, never a guess. A silently mis-bound alias
    produces a DTO that loads and reports the wrong number (threat T-50-06).

    Args:
        dialect: The dialect that built the probed SQL.
        field: The projected field, adapted by
            :func:`semolina.codegen.query_resolver.query_fields`.
        schema: The probed result schema.

    Returns:
        The result-column name the field binds to. The renderer emits it as a
        ``validation_alias`` only when it differs from the field name.

    Raises:
        ValueError: If no candidate matches a column, or the first matching candidate names
            more than one. The message carries the field, every candidate tried and the
            schema's own column names, so the mismatch is diagnosable from the error alone.
    """
    candidates = _result_field_names(dialect, field)
    for name in candidates:
        indices = schema.get_all_field_indices(name)
        if len(indices) == 1:
            return name
        if len(indices) > 1:
            msg = (
                f"Field {field.name!r} matches {len(indices)} result columns named {name!r}. "
                f"Candidates tried: {candidates!r}. The result carries: {list(schema.names)!r}."
            )
            raise ValueError(msg)
    msg = (
        f"Field {field.name!r} matches no result column. Candidates tried: {candidates!r}. "
        f"The result carries: {list(schema.names)!r}."
    )
    raise ValueError(msg)


@dataclass
class _DtoFieldContext:
    """
    Intermediate rendering context for a single DTO field.

    Attributes:
        name: The Python attribute name, taken from the model's own field name (D-03).
        annotation: The resolved Python annotation, already carrying ``| None`` for a
            metric. Never empty.
        alias: The result-column name as the warehouse spells it, unquoted. Read by
            ``semolina codegen-dto --check``, which compares it against the alias a
            committed DTO declares. The template never sees this one -- it interpolates
            ``alias_literal`` -- but a check that re-derived the alias itself would be a
            second implementation of :func:`_alias_for`, and the per-backend spellings are
            exactly where that drifts.
        alias_literal: The same name as a ready-quoted Python string literal, or the empty
            string when the alias equals ``name`` and the template emits a bare annotation
            (ALIAS-03). Pre-quoted rather than raw so the template never interpolates an
            unescaped warehouse string into importable source (threat T-50-01).
        type_comment: The ``TODO: <dtype>`` text emitted above the field for an Arrow type
            with no clean Python equivalent, collapsed to a single line. Empty otherwise.
    """

    name: str
    annotation: str
    alias: str
    alias_literal: str
    type_comment: str


@dataclass
class _DtoContext:
    """
    Intermediate rendering context for a single generated DTO class.

    Attributes:
        class_name: The PascalCase Python class name.
        docstring_body: The class docstring, escaped for the inside of a triple-quoted
            literal by ``_docstring_body``.
        fields: Ordered list of field contexts, metrics first.
    """

    class_name: str
    docstring_body: str
    fields: list[_DtoFieldContext]

    @property
    def has_alias(self) -> bool:
        """
        Whether any field binds under a spelling other than its own name.

        Only then does the class need ``populate_by_name``: an alias *replaces* the field
        name during validation unless that config is set, and with no alias there is
        nothing for it to restore.

        Returns:
            True when at least one field carries a ``validation_alias``.
        """
        return any(field.alias_literal for field in self.fields)


def _check_dto_field_name(name: str, column: str) -> None:
    """
    Refuse a model field name pydantic cannot make a DTO field out of.

    Pydantic reserves the leading underscore for private attributes and raises
    ``NameError`` when a model declares a field named that way. Nothing upstream stops the
    name getting here: the field descriptor validates identifiers, keywords and the
    reserved builder names, and ``_id`` is none of those. Emitting it anyway produces a
    file that dies on *import*, with the traceback pointing at the generated module rather
    than at the model attribute that chose the name.

    The message carries the way out, because the name is the user's to change and the
    warehouse column's spelling is not: ``source=`` keeps the column while the Python
    attribute takes a legal name.

    Args:
        name: The model field's Python attribute name.
        column: The warehouse column the field reads (its ``source=``, or its name folded to
            the dialect's storage case), quoted into the suggestion. Not the result alias:
            every column is aliased to its field name, so that would just repeat ``name``.

    Raises:
        ValueError: If ``name`` starts with an underscore.
    """
    if name.startswith("_"):
        raise ValueError(
            f"Field {name!r} cannot become a DTO field: pydantic reserves names with a "
            f"leading underscore for private attributes. Rename the model field and give "
            f"it the warehouse spelling instead, e.g. "
            f"{name.lstrip('_')} = Dimension(source={column!r})."
        )


def _build_dto_context(probed: ProbedQuery) -> _DtoContext:
    """
    Resolve one query's fields against its probed schema into a rendering context.

    Args:
        probed: The probed query: its schema, the dialect that built it, and its naming.

    Returns:
        The context the template renders.

    Raises:
        ValueError: If any field's alias cannot be resolved against the schema.
    """
    fields: list[_DtoFieldContext] = []
    for field in query_fields(probed.query):
        alias = _alias_for(probed.dialect, field, probed.schema)
        _check_dto_field_name(
            field.name, field.source_name or probed.dialect.normalize_identifier(field.name)
        )
        dtype = probed.schema.field(alias).type

        # D-06: the annotation comes from the Arrow map and from nowhere else. A `None`
        # answer means "no clean Python equivalent", which becomes `Any` plus a comment
        # naming the type — the same contract the three SQL mappers and the model renderer
        # use. The comment is whitespace-collapsed so a pretty-printed nested type
        # descriptor can never span two physical lines and break the generated file.
        mapped = arrow_type_to_python(dtype)
        annotation = mapped if mapped is not None else _UNMAPPED_ANNOTATION
        type_comment = "" if mapped is not None else " ".join(f"TODO: {dtype}".split())

        if field.field_type == "metric":
            # D-09, applied through the shared helper so a generated DTO and a generated
            # model decorate nullability by exactly one rule. See `metric_annotation`.
            annotation = metric_annotation(annotation)

        fields.append(
            _DtoFieldContext(
                name=field.name,
                annotation=annotation,
                alias=alias,
                # ALIAS-03: since phase 53 every builder names a column after its field, so
                # an alias equal to the name would only restate it. A column the warehouse
                # still spells differently keeps one, or the field would never bind.
                alias_literal="" if alias == field.name else _python_str_literal(alias),
                type_comment=type_comment,
            )
        )

    # The probe route is repeated here even though the module header already lists it per
    # class. A class is the unit that gets read in an editor's hover, quoted into a review
    # and copied into another file, and provenance that only exists at the top of the module
    # does not survive any of those. Both values are measured: the dotted path is what was
    # resolved, the route is `ProbeResult`'s own answer (threat T-50-07).
    return _DtoContext(
        class_name=probed.class_name,
        docstring_body=_docstring_body(
            f"{DOCSTRING_PREFIX}{probed.origin} (probe route: {probed.route})."
        ),
        fields=fields,
    )


def _build_dto_import_lines(models: list[_DtoContext]) -> list[str]:
    """
    Derive a generated DTO module's import block from its resolved annotations.

    Reads ``_DtoFieldContext.annotation`` — the annotation as it will be written — so metric
    nullability, applied during context building, cannot desynchronize an annotation from
    its import.

    There is deliberately no ``from semolina import ...`` line. A generated DTO is a plain
    ``pydantic.BaseModel``: it is usable in a service that has no Semolina dependency at
    runtime, and keeping it that way is what lets it be type-checked in isolation.

    Args:
        models: Contexts already built by :func:`_build_dto_context`.

    Returns:
        Import lines in emission order: ``from __future__ import annotations``, then stdlib
        imports sorted alphabetically, then ``from typing import Any`` when any field needs
        it, then ``import pydantic``. Deterministic for a given input, so repeated renders
        are byte-identical.
    """
    annotations = [f.annotation for model in models for f in model.fields]

    # Unconditional, and it must stay the first statement after the module docstring. Every
    # metric annotation is a `T | None` union (D-09), which is 3.10+ syntax at runtime
    # without it — and a generated DTO's whole value is that it can be committed into a
    # service this repo knows nothing about, including one still on an older interpreter.
    # Pydantic resolves the deferred annotations through the generated module's own globals,
    # which is why the stdlib imports below have to be there rather than merely be implied.
    lines = [f"from __future__ import {_FUTURE_IMPORT}"]

    lines += sorted(
        {
            import_line
            for prefix, import_line in _STDLIB_MODULE_PREFIXES.items()
            if any(prefix in annotation for annotation in annotations)
        }
    )

    # Split on the union operator so "Any" is matched as a whole annotation token rather
    # than as a substring of some future annotation that merely contains those letters.
    if any(
        _UNMAPPED_ANNOTATION in annotation.replace("|", " ").split() for annotation in annotations
    ):
        lines.append(f"from typing import {_UNMAPPED_ANNOTATION}")

    lines.append(f"import {_PYDANTIC_MODULE}")
    return lines


def _build_header_lines(
    models: list[_DtoContext],
    probed: list[ProbedQuery],
    *,
    backend_label: str,
) -> list[str]:
    """
    Build the generated module's provenance header.

    The header exists because a generated DTO's annotations reflect one warehouse's
    aggregation result typing, which is warehouse-defined and genuinely differs (D-07).
    Since phase 53 its field *names* are portable -- every backend aliases each column to
    its field (ALIAS-03) -- but its types are not, and codegen cannot paper over that and
    must not try. So the file says which backend answered and which probe route produced
    the schema.

    Every interpolated value is escaped by ``_docstring_body`` before it lands between the
    triple quotes: the backend label and the dotted paths are caller input, and a stray
    quote or backslash in either would end the docstring early in a file the user imports.

    Two of the three provenance values are measured rather than claimed — the probe route
    comes from ``ProbeResult`` and the dialect is the object that built the SQL — and both
    are printed beside the caller's ``backend_label``. A header that named a backend which
    never answered would be worse than no header: it reads as evidence and is not. The
    caller's label is also checked against the dialect in :func:`render_dtos` wherever this
    repo knows the dialect, so disagreement is an error and not a footnote.

    Args:
        models: The rendering contexts, in the same order as ``probed``.
        probed: The probed queries the classes were built from.
        backend_label: The backend the probe ran against, e.g. ``'duckdb'``.

    Returns:
        Docstring lines, already escaped, without the triple-quote delimiters.
    """
    lines: list[str] = [
        "Generated result DTOs. Do not edit.",
        "",
        f"Backend: {backend_label}",
        "",
        "Fields bind to result columns by name. Every backend names a column after its field,",
        "so a field carries an alias only where its column is spelled differently. The",
        "annotations reflect this backend's own aggregation result typing: regenerate against",
        "another warehouse where its driver types differ.",
        "",
        "Classes:",
    ]
    lines += [
        f"    {model.class_name} -- {p.origin} "
        f"(dialect: {type(p.dialect).__name__}, probe route: {p.route})"
        for model, p in zip(models, probed, strict=True)
    ]
    return [_docstring_body(line) for line in lines]


def render_dtos(
    probed: list[ProbedQuery],
    *,
    backend_label: str,
) -> str:
    """
    Render probed queries into a single Python source string.

    Emits the provenance header, then a shared import block, then one
    ``pydantic.BaseModel`` subclass per query. The returned source is *not* passed through
    ruff; call :func:`render_and_format_dtos` for that.

    A list rather than a single value, so emitting several DTOs into one file (plan 50-03)
    needs no signature change.

    Args:
        probed: The probed queries, in emission order.
        backend_label: The backend the probe ran against, e.g. ``'duckdb'``. Checked against
            the dialect that actually answered.

    Returns:
        Raw Python source (not yet formatted by ruff).

    Raises:
        ValueError: If ``backend_label`` names a different backend from the dialect that
            built the probed SQL, or if any field's alias cannot be resolved against its
            schema.
    """
    for p in probed:
        # The provenance header is the generated file's evidence about where its aliases and
        # annotations came from. `backend_label` is the caller's word for it and the dialect
        # is the thing that answered, so where the two are comparable they are compared:
        # a header claiming Snowflake over a DuckDB probe would be a lie the reader has no
        # way to catch. An unrecognized dialect (a custom backend) is not checked, because
        # there is nothing to check it against — the header names the dialect class there.
        known = _known_backend_label(p.dialect)
        if known is not None and known != backend_label:
            msg = (
                f"backend_label={backend_label!r} disagrees with the dialect that answered "
                f"for {p.origin!r}: {type(p.dialect).__name__} is {known!r}. The "
                "provenance header must name the backend that was actually probed."
            )
            raise ValueError(msg)

    models = [_build_dto_context(p) for p in probed]
    import_lines = _build_dto_import_lines(models)
    header_lines = _build_header_lines(models, probed, backend_label=backend_label)

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        # Deliberate: Jinja's autoescape escapes for HTML, which would turn an `&` in a
        # column name into `&amp;` while leaving a quote just as dangerous. Escaping here is
        # `_python_str_literal`'s job and happens before the template sees a value.
        autoescape=False,
    )
    template = env.get_template("python_dto.py.jinja2")
    rendered: Any = template.render(
        header_lines=header_lines,
        import_lines=import_lines,
        models=models,
    )
    return str(rendered)


def render_and_format_dtos(
    probed: list[ProbedQuery],
    *,
    backend_label: str,
) -> str:
    """
    Render probed queries to Python source and format it with ruff.

    Convenience wrapper over :func:`render_dtos` and
    :func:`semolina.codegen.python_renderer.format_with_ruff`, which degrades to unformatted
    source when the optional ``codegen-lint`` extra is absent. This is what the CLI calls.

    Args:
        probed: The probed queries, in emission order.
        backend_label: The backend the probe ran against. Checked against the dialect that
            actually answered.

    Returns:
        Formatted Python source, or unformatted source if ruff is not installed.

    Raises:
        ValueError: If ``backend_label`` names a different backend from the dialect that
            built the probed SQL, or if any field's alias cannot be resolved against its
            schema.
        SemolinaGeneratedSourceError: If the rendered source does not parse, or ruff is installed
            and fails on it.
    """
    return format_with_ruff(check_parses(render_dtos(probed, backend_label=backend_label)))
