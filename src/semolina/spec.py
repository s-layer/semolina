"""
Build a query from field names and plain data rather than from field objects.

A model's fields are normally reached as attributes (``Sales.revenue``), which suits code
written against a model. A caller that holds names instead, such as an agent's tool call or
a request to an HTTP API, describes the query as JSON-shaped data:

.. code-block:: python

    from semolina.spec import build_query

    query = build_query(
        Sales,
        {
            "metrics": ["revenue"],
            "dimensions": ["country"],
            "where": {"field": "region", "op": "in", "value": ["West", "East"]},
            "order_by": ["-revenue"],
            "limit": 10,
        },
    )

The result is an ordinary :class:`~semolina.query.Query`, so it compiles and runs exactly as
one built from attributes would. :func:`query_spec_schema` describes the accepted data for
one model as a JSON Schema whose enums list that model's own field names, which is what a
tool definition handed to a language model wants.

Every mistake in the data raises :class:`~semolina.exceptions.SemolinaSpecError`, whose ``path`` says where it
is (``where.or[1].value``) so that the caller can hand the message back to whoever wrote
the data.
"""

from __future__ import annotations

import datetime
import decimal
import difflib
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypeAlias, TypedDict, get_args

from .exceptions import SemolinaSpecError
from .fields import Dimension, Fact, Field, Metric, NullsOrdering, OrderTerm
from .filters import (
    IsNull,
    Not,
    Predicate,
)

if TYPE_CHECKING:
    from .models import SemanticView
    from .query import Query

__all__ = [
    "AndFilter",
    "FilterLeaf",
    "FilterOp",
    "FilterSpec",
    "NotFilter",
    "OrderObject",
    "OrderSpec",
    "OrFilter",
    "QuerySpec",
    "build_query",
    "field_by_name",
    "predicate_from_spec",
    "query_spec_schema",
]


FilterOp: TypeAlias = Literal[
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "between",
    "isnull",
    "like",
    "ilike",
    "startswith",
    "istartswith",
    "endswith",
    "iendswith",
    "iexact",
]
"""The ``op`` of a filter leaf. Each names the :class:`~semolina.fields.Field` method it calls."""


class FilterLeaf(TypedDict):
    """
    One comparison on one field: ``{"field": "region", "op": "eq", "value": "West"}``.

    ``value`` depends on ``op``. ``in`` takes a list, ``between`` a list of two bounds, and
    ``isnull`` a boolean (``true`` for ``IS NULL``, ``false`` for ``IS NOT NULL``), which
    defaults to ``true``. The pattern and string operators take a string. ``eq`` and ``ne``
    take a scalar or ``null``, which compiles to ``IS NULL`` or ``IS NOT NULL``. The
    ordering operators take a scalar.
    """

    field: str
    op: FilterOp
    value: NotRequired[Any]


AndFilter = TypedDict("AndFilter", {"and": "list[FilterSpec]"})
"""A filter that holds when every listed filter holds, written ``{"and": [...]}``."""

OrFilter = TypedDict("OrFilter", {"or": "list[FilterSpec]"})
"""A filter that holds when any listed filter holds, written ``{"or": [...]}``."""

NotFilter = TypedDict("NotFilter", {"not": "FilterSpec"})
"""A filter that holds when the one it wraps does not, written ``{"not": {...}}``."""

FilterSpec: TypeAlias = "FilterLeaf | AndFilter | OrFilter | NotFilter"
"""A filter tree, either a leaf or an ``and`` / ``or`` / ``not`` over other filters."""


class OrderObject(TypedDict):
    """An ``order_by`` entry with its direction and NULL placement spelled out."""

    field: str
    direction: NotRequired[Literal["asc", "desc"]]
    nulls: NotRequired[Literal["first", "last"]]


OrderSpec: TypeAlias = "str | OrderObject"
"""
An ``order_by`` entry: a field name, ascending, or the name after ``-``, descending, or an
:class:`OrderObject`.
"""


class QuerySpec(TypedDict, total=False):
    """
    A query as plain data. Every key is optional, and ``null`` counts as leaving it out, but
    at least one field must be selected.

    Attributes:
        metrics: Metric names to aggregate.
        dimensions: Dimension and fact names to group by.
        where: One filter, or a list of filters that must all hold.
        order_by: Sort terms, applied in order.
        limit: The most rows to return; a positive integer.
    """

    metrics: list[str]
    dimensions: list[str]
    where: FilterSpec | list[FilterSpec]
    order_by: list[OrderSpec]
    limit: int


_SPEC_KEYS = frozenset(QuerySpec.__annotations__)
_OPS: tuple[str, ...] = get_args(FilterOp)
_STRING_OPS = frozenset(
    {"like", "ilike", "startswith", "istartswith", "endswith", "iendswith", "iexact"}
)
_SCALAR_TYPES = (str, int, float, bool, decimal.Decimal, datetime.date, datetime.time)

_ISO_PARSERS: dict[object, Callable[[str], Any]] = {
    datetime.date: datetime.date.fromisoformat,
    datetime.datetime: datetime.datetime.fromisoformat,
    datetime.time: datetime.time.fromisoformat,
}
"""Parsers for the declared field types a JSON string stands in for."""

_LEAF_BUILDERS: dict[str, Callable[[Field[Any], Any], Predicate]] = {
    "eq": lambda f, v: f == v,
    "ne": lambda f, v: f != v,
    "gt": lambda f, v: f > v,
    "gte": lambda f, v: f >= v,
    "lt": lambda f, v: f < v,
    "lte": lambda f, v: f <= v,
    "in": lambda f, v: f.in_(v),
    "between": lambda f, v: f.between(v[0], v[1]),
    "isnull": lambda f, v: f.lookup(IsNull, v),
    "like": lambda f, v: f.like(v),
    "ilike": lambda f, v: f.ilike(v),
    "startswith": lambda f, v: f.startswith(v),
    "istartswith": lambda f, v: f.istartswith(v),
    "endswith": lambda f, v: f.endswith(v),
    "iendswith": lambda f, v: f.iendswith(v),
    "iexact": lambda f, v: f.iexact(v),
}


def _kind(field: Field[Any]) -> str:
    """
    Name a field's role the way the error messages and the schema do.

    Args:
        field: A model field.

    Returns:
        ``"metric"``, ``"dimension"`` or ``"fact"``.
    """
    if isinstance(field, Metric):
        return "metric"
    if isinstance(field, Fact):
        return "fact"
    return "dimension"


def field_by_name(model: type[SemanticView], name: str, *, path: str = "") -> Field[Any]:
    """
    Return the field a model declares under ``name``.

    The name is the Python attribute name, the one a query's result columns are keyed by,
    not the warehouse column a ``source=`` field reads from.

    Args:
        model: A :class:`~semolina.models.SemanticView` subclass.
        name: The field's attribute name.
        path: Where the name sits in the caller's data, for the error message.

    Returns:
        The model's own field descriptor, the same object as ``getattr(model, name)``.

    Raises:
        SemolinaSpecError: If ``name`` is not a string, or the model has no field by that
            name. The message suggests the closest names the model does have.
        TypeError: If ``model`` is not a ``SemanticView`` subclass.

    Example:
        .. code-block:: python

            field_by_name(Sales, "revenue") is Sales.revenue  # True
    """
    fields = _model_fields(model)
    if not isinstance(name, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise SemolinaSpecError(
            f"a field name must be a string, got {type(name).__name__}: {name!r}", path
        )
    found = fields.get(name)
    if found is not None:
        return found
    close = difflib.get_close_matches(name, list(fields), n=3)
    hint = f" Did you mean {_quoted_list(close)}?" if close else ""
    raise SemolinaSpecError(
        f"{model.__name__} has no field {name!r}.{hint} Its fields are {_quoted_list(fields)}.",
        path,
    )


def _model_fields(model: type[SemanticView]) -> Mapping[str, Field[Any]]:
    """
    Return a model's field mapping, refusing anything that is not a model.

    Args:
        model: The object passed as a model.

    Returns:
        The model's ``_fields`` mapping.

    Raises:
        TypeError: If ``model`` is not a ``SemanticView`` subclass.
    """
    from .models import SemanticView

    if not (isinstance(model, type) and issubclass(model, SemanticView)):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(f"expected a SemanticView subclass, got {model!r}")
    return model._fields


def _quoted_list(names: Sequence[str] | Mapping[str, Any]) -> str:
    """
    Join names as ``'a', 'b', 'c'``.

    Args:
        names: The names, or a mapping keyed by them.

    Returns:
        The names, each quoted, separated by commas.
    """
    return ", ".join(repr(n) for n in names)


def _field_value_type(field: Field[Any]) -> object:
    """
    Return the Python type a field was declared with, as in ``Dimension[datetime.date]()``.

    Args:
        field: A model field.

    Returns:
        The type argument, or ``None`` for a field declared without one.
    """
    orig = getattr(field, "__orig_class__", None)
    args = get_args(orig) if orig is not None else ()
    return args[0] if args else None


def _coerce_scalar(field: Field[Any], value: Any, path: str) -> Any:
    """
    Convert an ISO-format string to the date or time type its field was declared with.

    JSON has no date type, so data from outside the program spells ``2024-01-31`` as a
    string. A field declared ``Dimension[datetime.date]`` gets a ``datetime.date``, which
    each dialect writes as a typed literal, and a malformed date is caught here, with its
    path, rather than by the warehouse. A field with no declared type, or one of another
    type, keeps the value as given.

    Args:
        field: The field being compared.
        value: One comparison value.
        path: Where the value sits, for the error message.

    Returns:
        The value, converted when the field's type calls for it.

    Raises:
        SemolinaSpecError: If the string is not a valid ISO date, datetime or time.
    """
    if not isinstance(value, str):
        return value
    parse = _ISO_PARSERS.get(_field_value_type(field))
    if parse is None:
        return value
    try:
        return parse(value)
    except ValueError:
        raise SemolinaSpecError(
            f"{value!r} is not an ISO {parse.__qualname__.split('.')[0]} for {field.name!r}",
            path,
        ) from None


def _check_scalar(field: Field[Any], value: Any, path: str, *, allow_null: bool) -> Any:
    """
    Accept one scalar comparison value, converting a date string where the field asks.

    Args:
        field: The field being compared.
        value: The value.
        path: Where the value sits.
        allow_null: Whether ``null`` is meaningful here (``eq`` and ``ne`` only).

    Returns:
        The value, converted by :func:`_coerce_scalar`.

    Raises:
        SemolinaSpecError: If the value is a list, an object, or a ``null`` that would
            compare with ``NULL``.
    """
    if value is None:
        if allow_null:
            return None
        raise SemolinaSpecError(
            "null matches no row in this comparison. Use the 'isnull' op, or 'eq' / 'ne' "
            "with null, to match missing values",
            path,
        )
    if not isinstance(value, _SCALAR_TYPES):
        raise SemolinaSpecError(
            f"expected a single value, got {type(value).__name__}: {value!r}", path
        )
    return _coerce_scalar(field, value, path)


def _leaf_value(field: Field[Any], op: str, spec: Mapping[str, Any], path: str) -> Any:
    """
    Check and convert a leaf's ``value`` for its operator.

    Args:
        field: The field the leaf compares.
        op: The leaf's operator, already known to be valid.
        spec: The leaf.
        path: Where the leaf sits.

    Returns:
        The value in the form the field method for ``op`` takes.

    Raises:
        SemolinaSpecError: If the value is missing or has the wrong shape for ``op``.
    """
    vpath = f"{path}.value"
    if op == "isnull":
        value = spec.get("value", True)
        if not isinstance(value, bool):
            raise SemolinaSpecError(
                f"'isnull' takes true or false, got {type(value).__name__}: {value!r}", vpath
            )
        return value
    if "value" not in spec:
        raise SemolinaSpecError(f"op {op!r} needs a 'value'", path)
    value = spec["value"]
    if op in ("in", "between"):
        if not isinstance(value, list | tuple):
            raise SemolinaSpecError(
                f"{op!r} takes a list, got {type(value).__name__}: {value!r}", vpath
            )
        items: Sequence[Any] = value
        if op == "between" and len(items) != 2:
            raise SemolinaSpecError(f"'between' takes [low, high], got {len(items)} values", vpath)
        return [
            _check_scalar(field, item, f"{vpath}[{i}]", allow_null=False)
            for i, item in enumerate(items)
        ]
    if op in _STRING_OPS:
        if not isinstance(value, str):
            raise SemolinaSpecError(
                f"{op!r} takes a string, got {type(value).__name__}: {value!r}", vpath
            )
        return value
    return _check_scalar(field, value, vpath, allow_null=op in ("eq", "ne"))


def _single_key(spec: Mapping[str, Any], key: str, path: str) -> None:
    """
    Refuse other keys beside a composite filter's ``and`` / ``or`` / ``not``.

    Args:
        spec: The composite filter.
        key: Its one allowed key.
        path: Where it sits.

    Raises:
        SemolinaSpecError: If the mapping has any other key.
    """
    extra = sorted(set(spec) - {key})
    if extra:
        raise SemolinaSpecError(
            f"a {key!r} filter has no other keys, got {_quoted_list(extra)}", path
        )


def predicate_from_spec(model: type[SemanticView], spec: Any, *, path: str = "where") -> Predicate:
    """
    Build a filter predicate from plain data.

    Accepts a :class:`FilterLeaf` (``{"field", "op", "value"}``), a composite
    (``{"and": [...]}``, ``{"or": [...]}``, ``{"not": {...}}``), or a list of filters,
    which must all hold. The result is the same predicate the field operators build, so
    ``{"field": "country", "op": "eq", "value": "US"}`` equals ``Sales.country == "US"``.

    Args:
        model: The :class:`~semolina.models.SemanticView` subclass the fields belong to.
        spec: The filter data.
        path: Where the filter sits in the caller's data, for error messages.

    Returns:
        The predicate.

    Raises:
        SemolinaSpecError: If the data names an unknown field or operator, gives a value of
            the wrong shape, has keys a filter does not take, or is an empty ``and`` / ``or``.
        TypeError: If ``model`` is not a ``SemanticView`` subclass.

    Example:
        .. code-block:: python

            predicate_from_spec(
                Sales,
                {
                    "or": [
                        {"field": "country", "op": "eq", "value": "US"},
                        {"field": "revenue", "op": "gt", "value": 1000},
                    ]
                },
            )
            # == (Sales.country == "US") | (Sales.revenue > 1000)
    """
    _model_fields(model)
    if isinstance(spec, list | tuple):
        return _combine(model, spec, path, "and")
    if not isinstance(spec, Mapping):
        raise SemolinaSpecError(f"a filter is an object, got {type(spec).__name__}: {spec!r}", path)
    node: Mapping[str, Any] = spec
    for key in ("and", "or"):
        if key in node:
            _single_key(node, key, path)
            children = node[key]
            if not isinstance(children, list | tuple):
                raise SemolinaSpecError(
                    f"{key!r} takes a list of filters, got {type(children).__name__}",
                    f"{path}.{key}",
                )
            return _combine(model, children, f"{path}.{key}", key)
    if "not" in node:
        _single_key(node, "not", path)
        return Not(predicate_from_spec(model, node["not"], path=f"{path}.not"))
    return _leaf(model, node, path)


def _combine(model: type[SemanticView], children: Sequence[Any], path: str, key: str) -> Predicate:
    """
    Build each child filter and join them with AND or OR.

    Args:
        model: The model the fields belong to.
        children: The child filters.
        path: Where the list sits.
        key: ``"and"`` or ``"or"``.

    Returns:
        The joined predicate.

    Raises:
        SemolinaSpecError: If the list is empty, or a child is invalid.
    """
    if not children:
        raise SemolinaSpecError(f"{key!r} needs at least one filter", path)
    built = [
        predicate_from_spec(model, child, path=f"{path}[{i}]") for i, child in enumerate(children)
    ]
    combined = built[0]
    for predicate in built[1:]:
        combined = combined & predicate if key == "and" else combined | predicate
    return combined


def _leaf(model: type[SemanticView], spec: Mapping[str, Any], path: str) -> Predicate:
    """
    Build one comparison from a :class:`FilterLeaf`.

    Args:
        model: The model the field belongs to.
        spec: The leaf.
        path: Where it sits.

    Returns:
        The comparison predicate.

    Raises:
        SemolinaSpecError: If a key is missing or unknown, the field or operator is unknown,
            or the value has the wrong shape.
    """
    extra = sorted(set(spec) - {"field", "op", "value"})
    if extra:
        raise SemolinaSpecError(
            f"a filter takes 'field', 'op' and 'value' (or one of 'and', 'or', 'not'), "
            f"got {_quoted_list(extra)}",
            path,
        )
    for key in ("field", "op"):
        if key not in spec:
            raise SemolinaSpecError(f"a filter needs {key!r}", path)
    field = field_by_name(model, spec["field"], path=f"{path}.field")
    op = spec["op"]
    if op not in _OPS:
        close = difflib.get_close_matches(str(op), _OPS, n=1)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        raise SemolinaSpecError(
            f"unknown op {op!r}.{hint} The ops are {_quoted_list(_OPS)}.", f"{path}.op"
        )
    value = _leaf_value(field, op, spec, path)
    return _LEAF_BUILDERS[op](field, value)


def _names(spec: Mapping[str, Any], key: str) -> list[str]:
    """
    Read a list of names from the query data.

    Args:
        spec: The query data.
        key: ``"metrics"`` or ``"dimensions"``.

    Returns:
        The names, or an empty list when the key is absent.

    Raises:
        SemolinaSpecError: If the value is not a list.
    """
    names = spec.get(key)
    if names is None:
        return []
    if not isinstance(names, list | tuple):
        raise SemolinaSpecError(
            f"{key!r} takes a list of field names, got {type(names).__name__}: {names!r}", key
        )
    return list(names)


def _selected(model: type[SemanticView], spec: Mapping[str, Any], key: str) -> list[Any]:
    """
    Look up the fields a ``metrics`` or ``dimensions`` list names, checking each one's role.

    Args:
        model: The model.
        spec: The query data.
        key: ``"metrics"`` or ``"dimensions"``.

    Returns:
        The fields, in the order named.

    Raises:
        SemolinaSpecError: If a name is unknown, named twice, or names a field of the
            other role (a dimension under ``metrics``, say).
    """
    fields: list[Any] = []
    seen: set[str] = set()
    for i, name in enumerate(_names(spec, key)):
        path = f"{key}[{i}]"
        field = field_by_name(model, name, path=path)
        wanted = isinstance(field, Metric) if key == "metrics" else not isinstance(field, Metric)
        if not wanted:
            other = "dimensions" if key == "metrics" else "metrics"
            raise SemolinaSpecError(
                f"{name!r} is a {_kind(field)}, so it belongs in {other!r}, not {key!r}", path
            )
        if name in seen:
            raise SemolinaSpecError(f"{name!r} is selected twice", path)
        seen.add(name)
        fields.append(field)
    return fields


def _order_term(
    model: type[SemanticView], entry: Any, path: str, selected: set[str]
) -> Field[Any] | OrderTerm:
    """
    Build one ``order_by`` term.

    Args:
        model: The model.
        entry: A name, ``-name``, or an :class:`OrderObject`.
        path: Where the entry sits.
        selected: The names the query selects.

    Returns:
        The field (ascending) or an :class:`~semolina.fields.OrderTerm`.

    Raises:
        SemolinaSpecError: If the entry has the wrong shape, names an unknown field, or
            orders by a dimension or fact the query does not select, which no backend
            can do.
    """
    if isinstance(entry, str):
        descending = entry.startswith("-")
        name = entry.removeprefix("-")
        nulls = NullsOrdering.DEFAULT
        field = field_by_name(model, name, path=path)
    elif isinstance(entry, Mapping):
        obj: Mapping[str, Any] = entry
        extra = sorted(set(obj) - {"field", "direction", "nulls"})
        if extra:
            raise SemolinaSpecError(
                f"an order_by object takes 'field', 'direction' and 'nulls', got "
                f"{_quoted_list(extra)}",
                path,
            )
        if "field" not in obj:
            raise SemolinaSpecError("an order_by object needs 'field'", path)
        field = field_by_name(model, obj["field"], path=f"{path}.field")
        direction = obj.get("direction", "asc")
        if direction not in ("asc", "desc"):
            raise SemolinaSpecError(
                f"direction is 'asc' or 'desc', got {direction!r}", f"{path}.direction"
            )
        descending = direction == "desc"
        nulls_value = obj.get("nulls")
        if nulls_value not in (None, "first", "last"):
            raise SemolinaSpecError(
                f"nulls is 'first' or 'last', got {nulls_value!r}", f"{path}.nulls"
            )
        nulls = (
            NullsOrdering.DEFAULT
            if nulls_value is None
            else NullsOrdering.FIRST
            if nulls_value == "first"
            else NullsOrdering.LAST
        )
    else:
        raise SemolinaSpecError(
            f"an order_by entry is a field name or an object, got {type(entry).__name__}: "
            f"{entry!r}",
            path,
        )
    assert field.name is not None
    if not isinstance(field, Metric) and field.name not in selected:
        raise SemolinaSpecError(
            f"ordering by {_kind(field)} {field.name!r} needs it in 'dimensions' too", path
        )
    if not descending and nulls is NullsOrdering.DEFAULT:
        return field
    return field.desc(nulls) if descending else field.asc(nulls)


def build_query(
    model: type[SemanticView], spec: Mapping[str, Any], *, using: str | None = None
) -> Query:
    """
    Build a :class:`~semolina.query.Query` from a :class:`QuerySpec`.

    Every field is looked up by name on ``model`` with :func:`field_by_name`, and every
    filter is built with :func:`predicate_from_spec`, so the query is the one the
    equivalent attribute-based code would build. The data is checked in full here, before
    any SQL is written, and the first mistake raises with its path.

    Args:
        model: The :class:`~semolina.models.SemanticView` subclass to query.
        spec: The query data. See :class:`QuerySpec` for its keys.
        using: The engine name, as :meth:`~semolina.query.Query.using` takes it. ``None`` uses the
            ``"default"`` engine.

    Returns:
        The query, ready for ``execute()``, ``aexecute()`` or ``to_sql()``.

    Raises:
        SemolinaSpecError: If the data selects nothing, has an unknown key, names an
            unknown field or puts one in the wrong list, orders by an unselected dimension,
            gives a limit that is not a positive integer, or has a malformed filter.
        TypeError: If ``model`` is not a ``SemanticView`` subclass, or is an abstract one.

    Example:
        .. code-block:: python

            query = build_query(
                Sales,
                {
                    "metrics": ["revenue"],
                    "dimensions": ["country"],
                    "where": [{"field": "region", "op": "eq", "value": "West"}],
                    "order_by": [{"field": "revenue", "direction": "desc"}],
                    "limit": 5,
                },
            )
            # == Sales.query(metrics=[Sales.revenue], dimensions=[Sales.country])
            #        .where(Sales.region == "West").order_by(Sales.revenue.desc()).limit(5)
    """
    _model_fields(model)
    if not isinstance(spec, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise SemolinaSpecError(f"a query is an object, got {type(spec).__name__}: {spec!r}")
    extra = sorted(set(spec) - _SPEC_KEYS)
    if extra:
        raise SemolinaSpecError(
            f"unknown key {_quoted_list(extra)}; a query takes {_quoted_list(sorted(_SPEC_KEYS))}"
        )
    metrics = _selected(model, spec, "metrics")
    dimensions = _selected(model, spec, "dimensions")
    if not metrics and not dimensions:
        raise SemolinaSpecError("a query selects at least one field, in 'metrics' or 'dimensions'")

    query = model.query(metrics=metrics, dimensions=dimensions, using=using)

    where = spec.get("where")
    # An empty list is no conditions, as an empty `where()` call is; an empty "and" is not.
    if where is not None and where != []:
        query = query.where(predicate_from_spec(model, where, path="where"))

    order_by = spec.get("order_by")
    if order_by is not None:
        if not isinstance(order_by, list | tuple):
            raise SemolinaSpecError(
                f"'order_by' takes a list, got {type(order_by).__name__}: {order_by!r}",
                "order_by",
            )
        selected = {f.name for f in dimensions}
        terms = [
            _order_term(model, entry, f"order_by[{i}]", selected)
            for i, entry in enumerate(order_by)
        ]
        if terms:
            query = query.order_by(*terms)

    limit = spec.get("limit")
    if limit is not None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise SemolinaSpecError(f"'limit' is a positive integer, got {limit!r}", "limit")
        query = query.limit(limit)

    return query


_SCALAR_SCHEMA: dict[str, Any] = {"type": ["string", "number", "boolean"]}


def query_spec_schema(model: type[SemanticView]) -> dict[str, Any]:
    """
    Describe the :class:`QuerySpec` data :func:`build_query` accepts for one model.

    A JSON Schema (draft 2020-12) whose ``metrics``, ``dimensions``, filter ``field`` and
    ``order_by`` entries are enums of the model's own field names, and whose filter leaves
    tie each ``op`` to the shape of ``value`` it takes. Hand it to a language model as a
    tool's input schema and the model can only name fields that exist.

    Data the schema accepts builds without a :class:`~semolina.exceptions.SemolinaSpecError`, with two
    exceptions the schema cannot express: an ``order_by`` on a dimension the query does
    not select, and a date string the field's declared type cannot parse.

    Args:
        model: The :class:`~semolina.models.SemanticView` subclass.

    Returns:
        The schema, as a JSON-serialisable dict.

    Raises:
        TypeError: If ``model`` is not a ``SemanticView`` subclass.
    """
    fields = _model_fields(model)
    metric_names = [n for n, f in fields.items() if isinstance(f, Metric)]
    dimension_names = [n for n, f in fields.items() if isinstance(f, Dimension | Fact)]
    all_names = list(fields)

    properties: dict[str, Any] = {}
    if metric_names:
        properties["metrics"] = _name_list_schema(metric_names, "Metrics to aggregate.")
    if dimension_names:
        properties["dimensions"] = _name_list_schema(
            dimension_names, "Dimensions and facts to group by."
        )
    if all_names:
        properties["where"] = {
            "description": "One filter, or a list of filters that must all hold.",
            "anyOf": [
                {"$ref": "#/$defs/filter"},
                {"type": "array", "items": {"$ref": "#/$defs/filter"}},
                {"type": "null"},
            ],
        }
        properties["order_by"] = {
            "description": (
                "Sort terms in order: a field name for ascending, '-' then the name for "
                "descending, or an object. A dimension must also be selected."
            ),
            "type": ["array", "null"],
            "items": {
                "anyOf": [
                    {"enum": [*all_names, *(f"-{n}" for n in all_names)]},
                    {"$ref": "#/$defs/order"},
                ]
            },
        }
    properties["limit"] = {
        "type": ["integer", "null"],
        "minimum": 1,
        "description": "Maximum rows.",
    }

    defs: dict[str, Any] = {}
    if all_names:
        field_enum = {"enum": all_names}
        leaf_variants: list[dict[str, Any]] = [
            _leaf_schema(field_enum, ["eq", "ne"], {"anyOf": [_SCALAR_SCHEMA, {"type": "null"}]}),
            _leaf_schema(field_enum, ["gt", "gte", "lt", "lte"], _SCALAR_SCHEMA),
            _leaf_schema(field_enum, ["in"], {"type": "array", "items": _SCALAR_SCHEMA}),
            _leaf_schema(
                field_enum,
                ["between"],
                {"type": "array", "items": _SCALAR_SCHEMA, "minItems": 2, "maxItems": 2},
            ),
            _leaf_schema(field_enum, ["isnull"], {"type": "boolean"}, required=False),
            _leaf_schema(field_enum, sorted(_STRING_OPS), {"type": "string"}),
        ]
        defs["filter"] = {
            "anyOf": [
                *leaf_variants,
                _composite_schema("and"),
                _composite_schema("or"),
                {
                    "type": "object",
                    "properties": {"not": {"$ref": "#/$defs/filter"}},
                    "required": ["not"],
                    "additionalProperties": False,
                },
            ]
        }
        defs["order"] = {
            "type": "object",
            "properties": {
                "field": field_enum,
                "direction": {"enum": ["asc", "desc"]},
                "nulls": {"enum": ["first", "last"]},
            },
            "required": ["field"],
            "additionalProperties": False,
        }

    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"{model.__name__} query",
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    selectable = [
        {"required": [key], "properties": {key: {"type": "array", "minItems": 1}}}
        for key in ("metrics", "dimensions")
        if key in properties
    ]
    if selectable:
        schema["anyOf"] = selectable
    if defs:
        schema["$defs"] = defs
    return schema


def _name_list_schema(names: list[str], description: str) -> dict[str, Any]:
    """
    Describe a list of distinct, non-empty selections from ``names``.

    Args:
        names: The allowed names.
        description: What the list is for.

    Returns:
        The schema.
    """
    return {
        "description": description,
        "type": ["array", "null"],
        "items": {"enum": names},
        "uniqueItems": True,
    }


def _leaf_schema(
    field_enum: dict[str, Any],
    ops: list[str],
    value: dict[str, Any],
    *,
    required: bool = True,
) -> dict[str, Any]:
    """
    Describe the filter leaves for a group of operators that take one shape of value.

    Args:
        field_enum: The schema of a field name.
        ops: The operators.
        value: The schema of their ``value``.
        required: Whether ``value`` must be present.

    Returns:
        The schema.
    """
    return {
        "type": "object",
        "properties": {"field": field_enum, "op": {"enum": ops}, "value": value},
        "required": ["field", "op", "value"] if required else ["field", "op"],
        "additionalProperties": False,
    }


def _composite_schema(key: str) -> dict[str, Any]:
    """
    Describe an ``and`` or ``or`` filter over a non-empty list of filters.

    Args:
        key: ``"and"`` or ``"or"``.

    Returns:
        The schema.
    """
    return {
        "type": "object",
        "properties": {key: {"type": "array", "items": {"$ref": "#/$defs/filter"}, "minItems": 1}},
        "required": [key],
        "additionalProperties": False,
    }
