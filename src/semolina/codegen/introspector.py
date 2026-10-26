"""
Naming helpers shared by the warehouse introspection engines and the code renderer.

The result types, :class:`~semolina.introspection.IntrospectedView` and
:class:`~semolina.introspection.IntrospectedField`, live in :mod:`semolina.introspection`
because ``Engine.introspect()`` returns them. They are re-exported here so imports from this
module keep working.
"""

from __future__ import annotations

import functools
import json
import keyword
import re
import unicodedata
from dataclasses import replace
from typing import TYPE_CHECKING, cast

from semolina.introspection import IntrospectedField, IntrospectedView

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Literal

    from s_layer_semantic_formats import SLayerBlock

__all__ = [
    "PLACEHOLDER_FIELD_NAME",
    "class_name_for",
    "python_field_names",
    "semantic_format_from_tags",
    "synonyms_from_json",
    "IntrospectedField",
    "IntrospectedView",
    "with_python_field_names",
]


PLACEHOLDER_FIELD_NAME = "column"
"""The field name given to a column whose name holds no letter or digit, such as ``?``."""


_SOFT_KEYWORDS = frozenset({"_", "case", "match", "type"}) | frozenset(keyword.softkwlist)
"""
Soft keywords a generated field name avoids, fixed so output does not depend on the interpreter.

``type`` became a soft keyword in Python 3.12, so ``keyword.softkwlist`` on 3.11 lacks it. Codegen
must write the same model whichever Python runs it, or ``codegen --check`` would flip between
versions.
"""


def synonyms_from_json(value: str | None) -> tuple[str, ...]:
    """
    Read a ``SYNONYMS`` property, which ``DESCRIBE SEMANTIC VIEW`` reports as a JSON list.

    Snowflake and the DuckDB ``semantic_views`` extension both spell it this way.

    Args:
        value: The property value, or None (or empty) when the member has no synonyms.

    Returns:
        The synonyms in the order the view declares them.

    Raises:
        ValueError: If the value is not a JSON list of strings.

    Example:
        .. code-block:: python

            synonyms_from_json('["sales","turnover"]')
            # ('sales', 'turnover')
    """
    if not value:
        return ()
    try:
        parsed: object = json.loads(value)
    except ValueError:
        parsed = None
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        msg = f"SYNONYMS should be a JSON list of strings, got {value!r}"
        raise ValueError(msg)
    return tuple(cast("list[str]", parsed))


S_LAYER_KIND = "S_LAYER_KIND"
"""The tag holding a member's S-LAYER kind, on every backend that carries the tags."""

S_LAYER_LABEL = "S_LAYER_LABEL"
"""The tag holding a member's label, where the warehouse has no native slot for one."""

S_LAYER_SPEC = "S_LAYER_SPEC"
"""The tag holding the rest of a member's S-LAYER block, as compact JSON."""


def semantic_format_from_tags(
    member: str,
    tags: Mapping[str, str],
    member_type: Literal["metric", "dimension", "fact"],
) -> SLayerBlock | None:
    """
    Rebuild a member's S-LAYER block from its ``S_LAYER_KIND`` and ``S_LAYER_SPEC`` tags.

    The label and synonyms are not folded in: a field carries them as its own arguments,
    so the block it is given holds only what the two tags hold.

    Args:
        member: The member's name, for the error message.
        tags: Tag name to value for this member. Tags other than the two are ignored.
        member_type: The member's kind, which the block's kind must apply to.

    Returns:
        The block, or None when the member has no ``S_LAYER_KIND`` tag.

    Raises:
        ValueError: If the member has an ``S_LAYER_SPEC`` but no kind, or the tags do not
            make a valid block for this kind of member. The warehouse states what the
            values mean, so a tag that cannot be read is an error rather than a gap.
    """
    from s_layer_semantic_formats import SLayerValidationError, from_compact_json

    kind = tags.get(S_LAYER_KIND)
    spec = tags.get(S_LAYER_SPEC)
    if not kind:
        if spec:
            msg = f"{member!r} has an {S_LAYER_SPEC} tag but no {S_LAYER_KIND} tag"
            raise ValueError(msg)
        return None
    try:
        return from_compact_json(kind, spec, member_type=member_type)
    except SLayerValidationError as e:
        msg = f"The S_LAYER tags on {member!r} do not make a valid block: {e}"
        raise ValueError(msg) from e


def class_name_for(view_name: str, *, reserved: frozenset[str] | None = None) -> str:
    """
    Derive a PascalCase Python class name from a warehouse view name.

    Takes the last segment of a qualified name, drops any identifier quotes, and joins the
    runs of letters and digits (any script, so ``café_sales`` is ``CaféSales``), each
    capitalized. The result is NFKC-normalized, because that is how Python reads an
    identifier: ``class Ｓales`` declares ``Sales``, so ``"ｓales"`` is named ``Sales``.

    A name that would start with a digit, has no letters or digits at all, is a Python
    keyword (``none`` capitalizes to ``None``), or is one of the ``reserved`` names is
    prefixed with ``View``. ``reserved`` defaults to the names a generated model module
    imports (:data:`semolina.codegen.python_renderer.GENERATED_MODULE_NAMES`), so
    ``analytics.metric`` is named ``ViewMetric``: a class named ``Metric`` would rebind the
    import for every class after it. Field names avoid the same set the same way, with a
    trailing ``_`` (:func:`python_field_names`).

    Args:
        view_name: The view name as given to ``introspect()``, e.g. ``"sales_view"``,
            ``"analytics.sales_view"`` or ``'"Order Book"'``.
        reserved: The names the generated module binds, which the class may not take.
            ``semolina codegen-dto`` passes its own template's names.

    Returns:
        A valid Python identifier, e.g. ``"SalesView"`` or ``"OrderBook"``.
    """
    if reserved is None:
        from semolina.codegen.python_renderer import GENERATED_MODULE_NAMES

        reserved = GENERATED_MODULE_NAMES
    segment = view_name.rsplit(".", 1)[-1].strip('"`')
    name = "".join(word.capitalize() for word in re.split(r"[\W_]+", segment))
    # \w admits a few characters no identifier may hold (superscript digits, say). Dropped
    # before normalizing, so `sales²` stays `Sales` rather than becoming `Sales2`.
    name = "".join(char for char in name if f"A{char}".isidentifier())
    # Then normalized the way Python will read it, and filtered again in case NFKC
    # expanded a character into one no identifier may hold.
    name = unicodedata.normalize("NFKC", name)
    name = "".join(char for char in name if f"A{char}".isidentifier())
    if not name[:1].isalpha() or keyword.iskeyword(name) or name in reserved:
        return f"View{name}"
    return name


@functools.cache
def _unusable_field_names() -> frozenset[str]:
    """
    Return the valid identifiers a generated field still may not take.

    These are the names the query builder reserves, every attribute the model base class
    defines or declares, and every name the generated module imports or uses in an
    annotation. So a generated field can neither be refused by
    :class:`~semolina.models.SemanticView`, shadow one of its methods, nor rebind
    ``datetime`` for the fields declared after it. Built lazily because
    :mod:`semolina.models` imports far more than this module needs at import time.

    Returns:
        The reserved builder names, the base class's own attribute names and the generated
        module's names.
    """
    from semolina.codegen.python_renderer import GENERATED_MODULE_NAMES
    from semolina.fields import RESERVED_FIELD_NAMES
    from semolina.models import SemanticView

    return (
        RESERVED_FIELD_NAMES
        | GENERATED_MODULE_NAMES
        | set(dir(SemanticView))
        | set(SemanticView.__annotations__)
    )


def _python_identifier(name: str) -> str:
    """
    Apply the per-name half of :func:`python_field_names`, without collision handling.

    Args:
        name: One column name, already folded the way its engine folds it.

    Returns:
        A valid, usable field name; not necessarily unique within its view.
    """
    # Python reads identifiers in NFKC form, so `ﬁeld` would be declared as `field`.
    # Normalizing first keeps the name we compute and the name Python binds the same.
    text = unicodedata.normalize("NFKC", name)
    text = "".join(char if f"_{char}".isidentifier() else "_" for char in text)
    if not text.strip("_"):
        return PLACEHOLDER_FIELD_NAME
    # Two leading underscores would be name-mangled inside the class body (or name a dunder).
    if text.startswith("__"):
        text = "_" + text.lstrip("_")
    # Only a character that cannot start an identifier is left to fix: a digit, usually.
    if not text.isidentifier():
        text = f"_{text}"
    if keyword.iskeyword(text) or text in _SOFT_KEYWORDS or text in _unusable_field_names():
        text = f"{text}_"
    return text


def python_field_names(names: Sequence[str]) -> list[str]:
    """
    Turn a view's column names into valid, unique Python field names.

    This is the one rule every backend's introspection uses to name generated fields.
    Names are not lower-cased here: each engine folds its own column names first, the way
    its warehouse folds unquoted identifiers, and passes the folded names in.

    The rule, applied to each name:

    1. The name is NFKC-normalized (Python reads identifiers in that form), then every
       character that cannot appear in a Python identifier becomes ``_``:
       ``order date`` is ``order_date`` and ``what?`` is ``what_``.
    2. A name left with no letter or digit (``?``, ``_`` or the empty string) becomes
       ``column``. A lone ``_`` is not used: it is a soft keyword, which a field may not be.
    3. Two or more leading underscores shrink to one, so the name is not mangled inside the
       class body: ``__secret`` is ``_secret``.
    4. A name that would start with a character an identifier cannot start with, a digit
       usually, takes a leading ``_``: ``1st`` is ``_1st``.
    5. A keyword, a soft keyword (``match``, ``case``, ``type``), a name the query builder
       reserves (``query``, ``keys``), an attribute of
       :class:`~semolina.models.SemanticView` (``_fields``) or a name the generated module
       imports or annotates with (``datetime``, ``Dimension``, ``str``) takes a trailing
       ``_``: ``class`` is ``class_``.

    Then collisions are settled, ignoring case: every supported warehouse reads
    ``Order_Date`` and ``order_date`` as the same identifier, so two such fields would share
    one select alias. The first name that the rule above leaves unchanged keeps it. Every
    other name, in column order, takes the first of ``base``, ``base_2``, ``base_3``, ...
    that no other field holds in any case, where a trailing ``_`` from step 5 is dropped
    before the number is added (``class_`` then ``class_2``). The same input always gives
    the same output.

    The caller is responsible for keeping the original column reachable: a field whose
    name changed needs ``source=`` set to the exact column name, which
    :func:`with_python_field_names` does.

    Args:
        names: Column names in the order the warehouse reports them, each already folded
            by its engine.

    Returns:
        One field name per input name, in the same order.

    Example:
        .. code-block:: python

            from semolina.codegen.introspector import python_field_names

            python_field_names(["what?", "order date", "class", "1st", "?", "a b", "a_b"])
            # ['what_', 'order_date', 'class_', '_1st', 'column', 'a_b_2', 'a_b']
    """
    bases = [_python_identifier(name) for name in names]
    result: list[str | None] = [None] * len(names)
    # Held casefolded: the warehouses match identifiers, and so select aliases, ignoring case.
    taken: set[str] = set()

    # A name that is already valid keeps it, so a renamed column never displaces it.
    for index, (name, base) in enumerate(zip(names, bases, strict=True)):
        if base == name and base.casefold() not in taken:
            result[index] = base
            taken.add(base.casefold())

    for index, base in enumerate(bases):
        if result[index] is not None:
            continue
        candidate = base
        stem = base.rstrip("_")
        number = 2
        while candidate.casefold() in taken:
            candidate = f"{stem}_{number}"
            number += 1
        result[index] = candidate
        taken.add(candidate.casefold())

    return [name for name in result if name is not None]


def with_python_field_names(
    fields: Sequence[IntrospectedField], columns: Sequence[str]
) -> list[IntrospectedField]:
    """
    Rename fields by :func:`python_field_names`, keeping each renamed field's column.

    Every engine's ``introspect()`` passes its fields through here before building the
    :class:`~semolina.introspection.IntrospectedView`, so ``semolina codegen`` and
    ``semolina codegen --check`` see the same names whatever the backend. A field whose name
    is unchanged is returned as it came, ``source_name`` included. A renamed one gets
    ``source_name`` set to its exact column, so the generated ``source=`` makes queries hit
    that column.

    Args:
        fields: The view's fields, named by their folded column names.
        columns: Each field's column exactly as the warehouse spells it, in the same order.

    Returns:
        The fields, with valid and unique names.

    Raises:
        ValueError: If ``fields`` and ``columns`` differ in length.
    """
    if len(fields) != len(columns):
        raise ValueError(f"Got {len(fields)} fields but {len(columns)} column names.")
    names = python_field_names([f.name for f in fields])
    return [
        f if name == f.name else replace(f, name=name, source_name=column)
        for f, name, column in zip(fields, names, columns, strict=True)
    ]
