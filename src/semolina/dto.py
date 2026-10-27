"""
Structural pre-check between a result schema and a Pydantic DTO's field annotations.

This is how ``.into(DTO)`` satisfies DTO-03. arrowmodel's fast path builds instances with
``model_construct`` and performs no per-value validation, so a ``decimal128`` column landing
in a ``float``-annotated field produces a quietly wrong value and raises nothing. Its
validated path does something worse for the case this project cares most about: it *coerces*
the ``Decimal`` to a ``float``, losing the precision, and also raises nothing. So the check
below is not a nicety layered on top of arrowmodel — on a money column it is the only
mechanism in Semolina that catches the mismatch, on either setting of ``validate=``.

**Schema only. No rows.** The check reads ``cursor.description``, which the driver has
already resolved: it fetches nothing, issues no query, and creates no reader. That is also
what lets the async cursor's ``iter_into`` stay a plain method rather than a coroutine.

**No row value ever reaches a report.** A mismatch names field names, column names, Arrow
type names and Python type names — never data. The check has no values to leak by
construction, and that must stay true of anything added here.

**Confident verdicts only.** A mismatch is reported when *both* sides reduce to a class or a
union of classes. Everything else — :data:`typing.Any`, an Arrow type
:func:`~semolina.codegen.arrow_map.arrow_type_to_runtime_type` does not map, a
``TypeAliasType`` such as :data:`pydantic.JsonValue`, a bare ``Annotated``, an unreduceable
parameterized generic — passes silently with no verdict. DTO-03 asks for "an error rather
than a silently wrong-typed value", not for a second type checker, and every false positive
here is a call site that worked yesterday.

Nothing is imported from :mod:`semolina.codegen.annotation_check`, despite the family
resemblance of the report shape. That comparator is string-against-string over a *textually
parsed* committed model and is driven by a live probe; every one of its inputs is absent at
``.into()`` time.
"""

from __future__ import annotations

import datetime
import types
import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .exceptions import SemolinaSchemaMismatchError

if TYPE_CHECKING:
    from pydantic import BaseModel
    from pydantic.fields import FieldInfo

__all__ = [
    "REASON_TYPE",
    "REASON_MISSING",
    "REASON_ALIAS",
    "REASON_DUPLICATE",
    "FieldMismatch",
    "resolve_column_keys",
    "resolve_column_key",
    "check_result_schema",
]


REASON_TYPE = "type"
"""Mismatch reason for a column that exists but whose Python type the field cannot hold."""

REASON_MISSING = "missing"
"""Mismatch reason for a required DTO field the result has no column for."""

REASON_ALIAS = "alias"
"""
Mismatch reason for a field whose alias construct the converter refuses outright.

Not a disagreement with the *result* at all — the model never gets as far as being compared
against one. Reported through the same report so a DTO with one such field and one mistyped
field still costs the reader a single round trip.
"""

REASON_DUPLICATE = "duplicate"
"""
Mismatch reason for a required field whose column name occurs more than once in the result.

Separate from :data:`REASON_MISSING` only for the sentence it prints. The lookup outcome is
identical — ``pyarrow.Schema.get_field_index`` answers ``-1`` for an ambiguous name and for an
absent one alike — but telling a reader that a result "has no such column" while they can see
it listed twice sends them after the wrong fix. The fix is to alias one of the two columns in
the query.
"""


@dataclass(frozen=True)
class FieldMismatch:
    """
    One field's disagreement with the result schema.

    Attributes:
        field_name: The DTO field's own name, as the model spells it.
        column_key: The result column this field resolves to. For :data:`REASON_TYPE` it is
            whichever of :func:`resolve_column_keys` the result actually carries — the one
            arrowmodel will read. For :data:`REASON_MISSING` there is no such column, so it is
            the field's *primary* key (``validation_alias``, then ``alias``, then
            ``field_name``), which is the spelling the user wrote and can act on. For
            :data:`REASON_DUPLICATE` it is the key the result carries more than once. Differs
            from ``field_name`` exactly when the DTO declares an alias. Since phase 53 every
            builder names a result column after its field, so that is the case only for a
            DTO written against a column spelled some other way.
        expected: The annotation the DTO declares, rendered for a human.
        got: What the result actually offers. For :data:`REASON_TYPE` this carries both the
            Arrow type and the Python type it implies, e.g.
            ``decimal128(38, 2) (arrives as decimal.Decimal)``; for :data:`REASON_MISSING` it
            is the list of columns the result does have, rendered into a sentence of its own
            because there is no column for the shared "but the column is …" clause to be
            about; for :data:`REASON_DUPLICATE` it counts the columns sharing the name and
            lists them all; for :data:`REASON_ALIAS` it names the unsupported construct and
            the remedy, and is rendered as the whole sentence, because no column was resolved
            to talk about.
        reason: :data:`REASON_TYPE`, :data:`REASON_MISSING`, :data:`REASON_DUPLICATE` or
            :data:`REASON_ALIAS`.
    """

    field_name: str
    column_key: str
    expected: str
    got: str
    reason: str


def resolve_column_keys(
    field_name: str,
    field_info: FieldInfo,
    model: type[BaseModel],
) -> list[str]:
    """
    Return every result-column key arrowmodel would accept for a DTO field, in its order.

    Mirrors arrowmodel's ``_build_field_map`` rather than paraphrasing it, because a pre-check
    that matched on a different key than the converter would either pass a DTO arrowmodel then
    rejects, or reject one it would have converted fine. Two of its rules matter here:

    * **ALIAS-01, the priority order** — ``validation_alias``, then ``alias``, then the field
      name. Exactly one of the three is the field's primary key.
    * **ALIAS-02, accept-by-name** — when the model's ``model_config`` sets ``populate_by_name``
      or ``validate_by_name``, arrowmodel appends the *field name* to its lookup map as well,
      so an aliased field is satisfied by a column spelled either way. The appended names come
      after every alias, which is why the alias still wins when a result carries both
      spellings — measured against arrowmodel 1.0.0, where a result of ``REVENUE=9`` and
      ``revenue=1`` converts to ``revenue=9``.

    Matching is exact :class:`str` equality against the Arrow field name: no case folding, no
    Unicode normalization, no whitespace trimming. A result column spelled ``REVENUE`` is
    therefore reachable from a field named ``revenue`` only through an explicit
    ``Field(validation_alias="REVENUE")``.

    Args:
        field_name: The DTO field's own name.
        field_info: The field's ``pydantic.fields.FieldInfo``.
        model: The model the field belongs to, read for its ``model_config`` only.

    Returns:
        The acceptable column keys, most-preferred first and never empty. A field is present
        in a result when *any* of them names a column; the first entry is the one error
        messages quote.

    Example:
        .. code-block:: python

            import pydantic

            from semolina.dto import resolve_column_keys


            class SalesDTO(pydantic.BaseModel):
                model_config = pydantic.ConfigDict(populate_by_name=True)
                revenue: float = pydantic.Field(alias='AGG("REVENUE")')


            resolve_column_keys("revenue", SalesDTO.model_fields["revenue"], SalesDTO)
            # ['AGG("REVENUE")', 'revenue']
    """
    validation_alias = field_info.validation_alias
    if isinstance(validation_alias, str):
        keys = [validation_alias]
    elif validation_alias is None and isinstance(field_info.alias, str):
        keys = [field_info.alias]
    else:
        keys = [field_name]

    config = model.model_config
    accept_by_name = config.get("validate_by_name", False) or config.get("populate_by_name", False)
    if accept_by_name and field_name not in keys:
        keys.append(field_name)
    return keys


def resolve_column_key(field_name: str, field_info: FieldInfo) -> str:
    """
    Return the *primary* result-column key a DTO field matches on.

    The first entry of :func:`resolve_column_keys` — ``validation_alias``, then ``alias``, then
    the field name. This is the key an error message quotes, not the whole set the pre-check
    tests against: a model configured for ``populate_by_name`` accepts its field name too, and
    only :func:`resolve_column_keys` knows that, because only it is handed the model.

    Args:
        field_name: The DTO field's own name.
        field_info: The field's ``pydantic.fields.FieldInfo``.

    Returns:
        The primary column key. When ``validation_alias`` is an ``AliasChoices`` or
        ``AliasPath`` rather than a plain string, this falls back to ``field_name`` — and
        :func:`check_result_schema` refuses such a model outright, because arrowmodel does.

    Example:
        .. code-block:: python

            import pydantic

            from semolina.dto import resolve_column_key


            class SalesDTO(pydantic.BaseModel):
                revenue: float = pydantic.Field(validation_alias='AGG("REVENUE")')


            resolve_column_key("revenue", SalesDTO.model_fields["revenue"])
            # 'AGG("REVENUE")'
    """
    validation_alias = field_info.validation_alias
    if isinstance(validation_alias, str):
        return validation_alias
    if validation_alias is None and isinstance(field_info.alias, str):
        return field_info.alias
    return field_name


def _has_unsupported_alias(field_info: FieldInfo) -> bool:
    """
    Report whether the field's ``validation_alias`` is one arrowmodel will not accept.

    ``AliasChoices`` offers a list of alternative spellings and ``AliasPath`` addresses a
    position inside a nested structure. arrowmodel supports neither: ``_build_field_map``
    raises ``NotImplementedError`` for both (its ALIAS-03) before any column is consulted, so
    the model is refused wholesale rather than matched against a different column.

    This was previously treated as a case to *skip*, on the reasoning that a verdict about a
    column the converter may never look at is worse than no verdict. That reasoning assumed a
    converter that would go on to do something; it does not. Skipping bought no safety and
    cost D-05: the ``NotImplementedError`` is raised inside ``ArrowModelConverter.__init__``,
    which the ``iter_into`` implementations run from inside the generator body, so it surfaced
    on the first ``next()`` as a bare third-party error naming neither Semolina nor a remedy.

    Args:
        field_info: The field's ``pydantic.fields.FieldInfo``.

    Returns:
        True when the field's alias construct makes the whole model unconvertible.
    """
    validation_alias = field_info.validation_alias
    return validation_alias is not None and not isinstance(validation_alias, str)


def _unsupported_alias_mismatch(field_name: str, field_info: FieldInfo) -> FieldMismatch:
    """
    Build the report line for a field :func:`_has_unsupported_alias` has already flagged.

    Args:
        field_name: The DTO field's own name.
        field_info: The field's ``pydantic.fields.FieldInfo``, whose ``validation_alias`` is
            an ``AliasChoices`` or ``AliasPath``.

    Returns:
        A :data:`REASON_ALIAS` mismatch. ``column_key`` is the field name only because the
        dataclass requires one — no column was resolved, and the rendered line names none.
    """
    return FieldMismatch(
        field_name=field_name,
        column_key=field_name,
        expected=_render_annotation(field_info.annotation),
        got=(
            f"uses {type(field_info.validation_alias).__name__} as its validation_alias, "
            "which the converter does not support — declare a plain string alias naming one "
            "column instead"
        ),
        reason=REASON_ALIAS,
    )


def _render_annotation(annotation: object) -> str:
    """
    Render an annotation for an error message.

    Args:
        annotation: Anything ``model_fields[name].annotation`` can hold.

    Returns:
        ``'float'`` for a builtin, ``'decimal.Decimal'`` for a class from a module, and
        ``str()`` of anything else (``'int | None'``, ``'typing.Any'``).
    """
    if isinstance(annotation, type):
        if annotation.__module__ == "builtins":
            return annotation.__qualname__
        return f"{annotation.__module__}.{annotation.__qualname__}"
    return str(annotation)


_SUBCLASSES_THAT_DO_NOT_FIT: frozenset[tuple[type, type]] = frozenset(
    {(datetime.datetime, datetime.date)}
)
"""
``(value class, annotation)`` pairs :func:`issubclass` accepts but the fast path refuses.

``datetime`` subclasses ``date``, yet a ``datetime`` left in a ``date`` field keeps its time
and compares unequal to the day it names: ``datetime(2024, 1, 2, 3) == date(2024, 1, 2)`` is
False. Pydantic's own ``validate=True`` path refuses the same value as
``date_from_datetime_inexact``, so accepting it here would make the fast path the looser of
the two. ``bool`` into ``int`` is not listed: ``True == 1``, so that subclass does fit.
"""


def _annotation_accepts(annotation: object, runtime_type: type) -> bool | None:
    """
    Report whether a DTO annotation can legally hold values of ``runtime_type``.

    Subtype-tolerant by plain :func:`issubclass`, with no numeric tower and two special
    cases: :data:`typing.Any`, and the pairs in :data:`_SUBCLASSES_THAT_DO_NOT_FIT`. Several
    consequences fall out of that and are intended: ``bool`` into ``int`` passes, ``datetime``
    into ``date`` does not (it is listed), ``date`` into ``datetime`` does not,
    ``decimal.Decimal`` into ``float`` does not — the last being the case Phase 47's whole
    Decimal policy exists to protect — and ``int`` into ``float`` does not either, because
    Python has no nominal numeric tower and the fast path really does leave an ``int`` in a
    field declared ``float``.

    Nullability is not consulted at all. Phase 47 measured the Arrow ``nullable`` flag as True
    for every DuckDB field including ``COUNT``, so it carries no information; ``NoneType`` is
    dropped from a union and never compared.

    Args:
        annotation: The DTO field's declared annotation.
        runtime_type: The Python class the Arrow type implies.

    Returns:
        True when the annotation accepts, False when it demonstrably does not, and ``None``
        when the annotation does not reduce to a class or a union of classes — in which case
        the caller records no verdict at all.
    """
    if annotation is Any:
        # Must be checked before the class branch. On Python 3.11 `issubclass(x, Any)` raises
        # TypeError; on 3.14 `typing.Any` is a class for which `issubclass` quietly answers
        # False. Either way, falling through would turn a deliberate opt-out into a crash or
        # a false mismatch. `object` needs no such case: it is a real class and everything is
        # a subclass of it, so the opt-out comes for free.
        return True

    origin = typing.get_origin(annotation)
    if origin is types.UnionType or origin is typing.Union:
        members = [arg for arg in typing.get_args(annotation) if arg is not types.NoneType]
        if not members:
            return None
        verdicts = [_annotation_accepts(member, runtime_type) for member in members]
        if any(verdict is True for verdict in verdicts):
            return True
        if any(verdict is None for verdict in verdicts):
            # One arm was unreadable, so "no arm accepts" is not something we know.
            return None
        return False

    if origin is None and isinstance(annotation, type):
        if (runtime_type, annotation) in _SUBCLASSES_THAT_DO_NOT_FIT:
            return False
        return issubclass(runtime_type, annotation)

    # A parameterized generic, a TypeAliasType such as `pydantic.JsonValue`, a bare
    # `Annotated`, or anything else that does not reduce. No verdict.
    return None


def check_result_schema(
    description: list[tuple[Any, ...]] | None,
    model: type[BaseModel],
    *,
    check_types: bool = True,
) -> None:
    """
    Raise if a DTO does not describe the result schema.

    Reads only ``cursor.description`` — a list of DBAPI 7-tuples whose second element an ADBC
    cursor fills with a ``pyarrow.DataType``. No rows are fetched and no query is issued.

    The check has three parts. The first refuses a model outright; the other two are gated
    separately because they answer different questions.

    **Supported alias constructs — always checked, and checked first.** arrowmodel implements
    plain string aliases only. An ``AliasChoices`` or ``AliasPath`` ``validation_alias``, or an
    ``alias_generator`` on ``model_config``, makes ``ArrowModelConverter.__init__`` raise
    ``NotImplementedError`` before any column is consulted — so there is nothing to compare a
    schema against, and the refusal is reported here instead, where D-05 promises it. Both
    halves run before ``description`` is read, because neither is a claim about a result: a
    model the converter will not build is wrong against every schema, including the ``None``
    of a cursor that has not executed. The generator case is the one that hides: pydantic
    materializes the generated spelling onto each ``FieldInfo``, so the fields look ordinary
    and only ``model_config`` gives it away.

    **Column presence — always checked.** Result columns the DTO does not declare are ignored,
    so one DTO can serve several queries and a query can gain a column without breaking
    existing DTOs. A declared field with no matching column is an error only when the field is
    required: a field carrying a default (including ``= None``) is honoured as "optional in
    the result", which is also exactly where arrowmodel itself draws the line. A missing
    column is never a coercion decision, so no caller opts out of it — and Semolina's message
    names the field, the column key and what the result actually carried, where arrowmodel's
    own ``ValueError`` names only the column.

    A result column name that occurs *twice* counts as absent, not present, because that is
    what it is to the lookup: ``pyarrow.Schema.get_field_index`` answers ``-1`` for an
    ambiguous name exactly as for an unknown one. Everything the converter does with a
    duplicate follows from that and is mirrored here — the field's next acceptable key is
    tried, a defaulted field keeps its default, a field naming some other column is
    unaffected, and a required one is refused. Only the wording differs: the refusal says the
    name is duplicated rather than missing, since the remedy is in the query, not in an alias.

    **Type compatibility — only when ``check_types``.** This half is skipped under
    ``validate=True``, where Pydantic performs the conversion per value and raises
    ``ValidationError`` on a pair it cannot convert. Running both would mean refusing
    conversions that the validated path performs correctly: a ``decimal128`` column into a
    ``float`` field is a deliberate, working narrowing under ``validate=True``, and only a
    silent wrong-typed value under the fast path, which is exactly what this half catches
    there.

    Every mismatch is reported together in one error rather than one at a time. The whole
    schema is in hand up front, so listing them all costs nothing and saves a
    fix-one-field-and-rerun cycle per field.

    Args:
        description: A cursor's ``description``, or ``None`` on a cursor that has not
            executed. ``None`` skips the presence and type halves, the same defensive
            handling ``SemolinaCursor._column_names`` uses; the alias half still applies,
            since it needs no schema.
        model: The Pydantic model ``.into()`` was asked for. Any ``BaseModel`` subclass;
            inheriting from ``arrowmodel.ArrowModel`` is not required.
        check_types: Compare each field's annotation against its column's Arrow type. Pass
            ``False`` when the caller runs ``validate=True``, so Pydantic owns type
            enforcement and may coerce. Column presence is checked either way.

    Raises:
        SemolinaSchemaMismatchError: If the model uses an alias construct the converter does
            not implement, or a field is required and absent from the result, or — when
            ``check_types`` — a field reduces to a comparable type that disagrees with its
            column.

    Example:
        .. code-block:: python

            import decimal

            import pydantic

            from semolina.dto import check_result_schema


            class SalesDTO(pydantic.BaseModel):
                total_order_value: decimal.Decimal


            cursor = Sales.query().metrics(Sales.total_order_value).execute()
            check_result_schema(cursor.description, SalesDTO)  # returns None
    """
    # Model-level and schema-independent, so it is checked before `description` is consulted
    # at all: a DTO arrowmodel will not build is wrong against every result, including the
    # `None` description of a cursor that has not executed.
    if model.model_config.get("alias_generator") is not None:
        raise SemolinaSchemaMismatchError(
            f"{model.__name__} cannot be converted: model_config sets alias_generator, which "
            "the converter does not support. Declare explicit per-field aliases instead — "
            "Field(alias=...) or Field(validation_alias=...)."
        )

    # The per-field half of the same rule, and hoisted above `description` for the same
    # reason. Collected rather than raised on the spot so a DTO with one refused alias and one
    # mistyped field still costs the reader a single round trip (D-11).
    mismatches: list[FieldMismatch] = [
        _unsupported_alias_mismatch(field_name, field_info)
        for field_name, field_info in model.model_fields.items()
        if _has_unsupported_alias(field_info)
    ]

    if description is None:
        # No schema, so no presence or type verdict is available — but an unsupported alias
        # was never a claim about a schema. Left inside the loop below, it went unreported
        # here, and arrowmodel's own NotImplementedError took its place on the first `next()`.
        if mismatches:
            raise SemolinaSchemaMismatchError(_render_report(model, mismatches))
        return

    import pyarrow

    from .codegen.arrow_map import arrow_type_to_runtime_type

    # Two structures, deliberately. Every column name is available for the presence test, but
    # only columns carrying a real Arrow type are available for the type test: a non-ADBC
    # cursor puts a DBAPI type code in `d[1]`, and a verdict read off one would be invented.
    # Folding the two together would report every field of such a result as missing.
    column_names: list[str] = []
    typed_entries: list[tuple[str, Any]] = []
    for entry in description:
        if not entry:
            continue
        name = str(entry[0])
        column_names.append(name)
        if len(entry) > 1 and isinstance(entry[1], pyarrow.DataType):
            typed_entries.append((name, entry[1]))

    # A name the result carries twice is a name the converter cannot address. arrowmodel looks
    # each key up with `pyarrow.Schema.get_field_index`, which answers -1 for an ambiguous name
    # exactly as it does for an absent one, so a duplicated column resolves nowhere: it falls
    # through to the field's next acceptable key, leaves a defaulted field at its default, and
    # ends as a missing-required-column ValueError for a required one. Modelling it as *present*
    # got all three wrong and, worse, decided the type half against whichever duplicate happened
    # to come last.
    duplicate_columns = {name for name in column_names if column_names.count(name) > 1}
    known_columns = {name for name in column_names if name not in duplicate_columns}
    typed_columns: dict[str, Any] = {
        name: dtype for name, dtype in typed_entries if name in known_columns
    }

    for field_name, field_info in model.model_fields.items():
        if _has_unsupported_alias(field_info):
            # Already reported above, unconditionally. Nothing here can add to it: there is
            # no column key to resolve, and `check_types` is irrelevant either way — Pydantic
            # owning per-value conversion changes nothing about a field map arrowmodel
            # refuses to build in the first place.
            continue

        column_keys = resolve_column_keys(field_name, field_info, model)
        annotation = field_info.annotation

        # arrowmodel resolves in field-map order and takes the first key that names a column,
        # so the type half below must read that same column and not merely *a* matching one.
        column_key = next((key for key in column_keys if key in known_columns), None)
        if column_key is None:
            if field_info.is_required():
                # Which of the two unresolvable shapes this is decides only the sentence.
                # A key present-but-duplicated is reported as such, because "no such column"
                # about a name the reader can see listed twice points at the wrong remedy.
                ambiguous = next((key for key in column_keys if key in duplicate_columns), None)
                if ambiguous is not None:
                    mismatches.append(
                        FieldMismatch(
                            field_name=field_name,
                            column_key=ambiguous,
                            expected=_render_annotation(annotation),
                            got=(
                                f"the result carries {column_names.count(ambiguous)} columns "
                                f"with that name and the converter cannot choose between them "
                                f"(it has {column_names})"
                            ),
                            reason=REASON_DUPLICATE,
                        )
                    )
                else:
                    mismatches.append(
                        FieldMismatch(
                            field_name=field_name,
                            # The primary key, which is what the user wrote and can act on.
                            column_key=column_keys[0],
                            expected=_render_annotation(annotation),
                            got=str(column_names),
                            reason=REASON_MISSING,
                        )
                    )
            continue

        if not check_types:
            # Pydantic owns type enforcement on the validated path and may legitimately
            # coerce. Presence has already been decided above, which is the half that stays.
            continue

        dtype = typed_columns.get(column_key)
        if dtype is None:
            continue

        runtime_type = arrow_type_to_runtime_type(dtype)
        if runtime_type is None:
            # An Arrow type with no clean Python equivalent — struct, list, interval.
            # arrowmodel converts nested structs and `list[str]` correctly, so failing here
            # would break conversions that work.
            continue

        if _annotation_accepts(annotation, runtime_type) is False:
            mismatches.append(
                FieldMismatch(
                    field_name=field_name,
                    column_key=column_key,
                    expected=_render_annotation(annotation),
                    got=(f"{dtype} (arrives as {_render_annotation(runtime_type)})"),
                    reason=REASON_TYPE,
                )
            )

    if mismatches:
        raise SemolinaSchemaMismatchError(_render_report(model, mismatches))


def _render_report(model: type[BaseModel], mismatches: list[FieldMismatch]) -> str:
    """
    Render the one error raised for a whole schema's worth of mismatches.

    Args:
        model: The DTO that was checked.
        mismatches: Every disagreement found, in field-declaration order.

    Returns:
        The message. Carries field names, column keys and type names only — never a row
        value, because none was fetched.
    """
    plural = "field" if len(mismatches) == 1 else "fields"
    lines = [
        f"{model.__name__} does not match the result schema "
        f"({len(mismatches)} mismatched {plural}):"
    ]
    for mismatch in mismatches:
        if mismatch.reason == REASON_ALIAS:
            # No column was ever resolved, so the "(column X): declared Y, but the column
            # is Z" sentence would name a column that played no part in the refusal.
            lines.append(f"  {mismatch.field_name}: {mismatch.got}")
        elif mismatch.reason == REASON_DUPLICATE:
            lines.append(
                f"  {mismatch.field_name} (column {mismatch.column_key!r}): "
                f"declared {mismatch.expected}, but {mismatch.got}"
            )
        elif mismatch.reason == REASON_MISSING:
            # Its own sentence, because the shared one ends "but the column is {got}" and
            # a missing column has no "is". Sharing it produced "but the column is no such
            # column", which asserts the column exists in the act of denying it.
            lines.append(
                f"  {mismatch.field_name} (column {mismatch.column_key!r}): "
                f"declared {mismatch.expected}, but the result has no such column "
                f"(it carries {mismatch.got})"
            )
        else:
            lines.append(
                f"  {mismatch.field_name} (column {mismatch.column_key!r}): "
                f"declared {mismatch.expected}, but the column is {mismatch.got}"
            )
    if any(mismatch.reason in (REASON_TYPE, REASON_MISSING) for mismatch in mismatches):
        # Suppressed when no mismatch is one this remedy addresses. Telling a reader to reach
        # for Field(validation_alias=...) when their validation_alias is the problem sends them
        # back to the thing that just failed, and an alias cannot name a duplicated column
        # either — no spelling distinguishes two columns that share one.
        lines.append(
            "Annotate each field with the type its column arrives as, or use "
            "Field(validation_alias=...) if the result spells the column differently."
        )
    if any(mismatch.reason == REASON_DUPLICATE for mismatch in mismatches):
        lines.append(
            "Give the duplicated result columns distinct names in the query: a name the "
            "result carries twice cannot be addressed by any alias."
        )
    if any(mismatch.reason == REASON_TYPE for mismatch in mismatches):
        # Only worth saying when a *type* disagreed. A missing column is not something
        # validate=True can convert its way out of, and suggesting it there would send the
        # reader down a path that ends in arrowmodel's own missing-columns ValueError.
        lines.append(
            "If a narrowing is deliberate, pass validate=True: Pydantic then converts each "
            "value, coercing where it legally can (decimal -> float) and raising "
            "ValidationError where it cannot (a fractional decimal -> int)."
        )
    return "\n".join(lines)
