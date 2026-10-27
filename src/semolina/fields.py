"""
Field descriptors for Semolina semantic models.

Descriptor classes for defining typed fields in semantic view models.
Fields use the descriptor protocol for class-level access and validation.
"""

from __future__ import annotations

import datetime
import keyword
import types
from dataclasses import dataclass
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Generic,
    Literal,
    Self,
    TypeAlias,
    TypeVar,
    Union,
    get_args,
    get_origin,
    overload,
)

__all__ = [
    "DATE_GRAINS",
    "GRAINS",
    "RESERVED_FIELD_NAMES",
    "Grain",
    "GrainedDimension",
    "NullsOrdering",
    "Field",
    "Metric",
    "Dimension",
    "Fact",
    "OrderTerm",
    "declared_type",
]


T = TypeVar("T")

# Names that would conflict with query builder methods or dict-like interface
RESERVED_FIELD_NAMES = frozenset(
    {
        "query",  # entry point
        "metrics",  # field selection
        "dimensions",  # field selection
        "where",  # filtering
        "filter",  # internal filtering
        "order_by",  # ordering
        "limit",  # limiting
        "execute",  # eager execution
        "to_sql",  # SQL generation
        "using",  # engine selection
        "keys",
        "values",
        "items",
        "get",
        "pop",
        "update",
        "clear",  # dict methods
    }
)

Grain: TypeAlias = Literal["year", "quarter", "month", "week", "day", "hour", "minute", "second"]
"""A time grain that :meth:`Dimension.grain` accepts, from the units all three backends share."""

GRAINS: tuple[Grain, ...] = get_args(Grain)
"""Every :data:`Grain`, coarsest first."""

DATE_GRAINS = frozenset(GRAINS[: GRAINS.index("day") + 1])
"""The grains a ``DATE`` can be truncated to, and those whose result is a ``DATE``."""

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from s_layer_semantic_formats import MemberType, SemanticFormat

    from .filters import (
        Between,
        EndsWith,
        Exact,
        Gt,
        Gte,
        IEndsWith,
        IExact,
        ILike,
        In,
        IsNull,
        IStartsWith,
        Like,
        Lookup,
        Lt,
        Lte,
        NotEqual,
        StartsWith,
    )


class NullsOrdering(Enum):
    """
    How to position NULL values in ORDER BY clause.

    Attributes:
        FIRST: NULLS FIRST - NULL values appear before non-NULL values
        LAST: NULLS LAST - NULL values appear after non-NULL values
        DEFAULT: No NULLS clause - let backend decide
    """

    FIRST = "FIRST"
    LAST = "LAST"
    DEFAULT = None

    def __repr__(self) -> str:
        """Return readable representation."""
        if self.value is None:
            return "DEFAULT"
        return f"NULLS {self.value}"


def _check_name(name: str | None) -> str:
    """
    Validate that field name has been set via __set_name__.

    Args:
        name: The field name to check.

    Returns:
        The validated field name.

    Raises:
        RuntimeError: If name is None (field used before __set_name__ called).
    """
    if name is None:
        raise RuntimeError("Field operator used before __set_name__ called")
    return name


class Field(Generic[T]):
    """
    Base descriptor for semantic view fields.

    Fields are descriptors that:
    - Validate field names at class creation time (__set_name__)
    - Return themselves for class-level access (Sales.revenue)
    - Prevent instance-level access, modification, and deletion
    - Support Python comparison operators to create Predicate filters

    Operators:
        Field descriptors support Python comparison operators that return
        typed Predicate nodes::

            Users.country == 'US'      # -> Exact(field_name='country', value='US')
            Users.country != 'CA'      # -> NotEqual(field_name='country', value='CA')
            Users.revenue > 1000       # -> Gt(field_name='revenue', value=1000)
            Users.revenue >= 1000      # -> Gte(...)
            Users.revenue < 100        # -> Lt(...)
            Users.revenue <= 100       # -> Lte(...)

        Combine with & (AND) and | (OR)::

            (Users.country == 'US') | (Users.country == 'CA')  # -> Or(...)
            (Users.revenue > 1000) & (Users.country == 'US')   # -> And(...)

    Named filter methods:
        Additional filter methods for common SQL operations::

            Users.country.in_(['US', 'CA'])      # -> In(...)
            Users.revenue.between(100, 1000)     # -> Between(...)
            Users.name.like('%Smith%')            # -> Like(...)
            Users.name.ilike('%smith%')           # -> ILike(...)
            Users.name.startswith('A')            # -> StartsWith(...)
            Users.name.endswith('son')            # -> EndsWith(...)
            Users.country.iexact('united states') # -> IExact(...)
            Users.country.isnull()                # -> IsNull(...)
    """

    _member_type: ClassVar[MemberType | None] = None
    """The S-LAYER member type a ``semantic_format`` is checked against, or None for any."""

    def __init__(
        self,
        source: str | None = None,
        *,
        label: str | None = None,
        description: str | None = None,
        synonyms: Iterable[str] = (),
        semantic_format: SemanticFormat | Mapping[str, Any] | None = None,
    ) -> None:
        """
        Initialize field descriptor.

        Args:
            source: Optional explicit SQL column name. When set, the SQL builder uses this
                string verbatim (after quoting) instead of deriving the column name from the
                Python field name via dialect normalization. Use this when the warehouse column
                name cannot be recovered by normalizing the Python attribute name — for example,
                a Snowflake column created with a quoted lowercase identifier::

                    # Snowflake column "order_id" (quoted lowercase, case-preserved)
                    order_id = Metric(source="order_id")

                When source is None (the default), the SQL builder applies
                ``dialect.normalize_identifier(field.name)`` to derive the column name.
            label: A short display name, such as ``"Revenue (USD)"``.
            description: What the field holds, as the warehouse's ``COMMENT`` says it.
            synonyms: Other names people use for the field, such as ``("sales",)``.
            semantic_format: What the values mean, as an S-LAYER block: a mapping such as
                ``{"value": {"kind": "currency", "currency": "USD"}}``, or an
                :class:`s_layer_semantic_formats.SemanticFormat`. Validated here, so a typo
                fails when the model is defined. The label and synonyms have their own
                arguments and may not appear in the block.

        Raises:
            TypeError: If ``synonyms`` is a string rather than a collection of strings.
            ValueError: If ``semantic_format`` is not a valid block for this kind of field
                (an :class:`s_layer_semantic_formats.SLayerValidationError`), or carries a
                ``label`` or ``synonyms`` of its own.
        """
        self.name: str | None = None
        self.source: str | None = source
        self.owner: type | None = None
        self.label: str | None = label
        self.description: str | None = description
        if isinstance(synonyms, str):
            raise TypeError(
                f"synonyms takes a collection of strings, not the string {synonyms!r}. "
                f"Did you mean ({synonyms!r},)?"
            )
        self.synonyms: tuple[str, ...] = tuple(synonyms)
        self.semantic_format: SemanticFormat | None = _semantic_format(
            semantic_format, self._member_type
        )

    def __set_name__(self, owner: type, name: str) -> None:
        """
        Called when field is assigned to a class attribute.

        Validates the field name to ensure it's a valid identifier and
        doesn't conflict with Python keywords or reserved names.

        Args:
            owner: The class that owns this field
            name: The name of the attribute

        Raises:
            ValueError: If name is not a valid identifier, is a Python keyword,
                       or is a reserved name that would conflict with dict methods
        """
        # Check valid identifier syntax
        if not name.isidentifier():
            raise ValueError(f"Field name '{name}' is not a valid Python identifier")

        # Check Python keywords
        if keyword.iskeyword(name):
            raise ValueError(f"Field name '{name}' is a Python keyword and cannot be used")

        # Check soft keywords (e.g., match, case in Python 3.10+)
        if keyword.issoftkeyword(name):
            raise ValueError(f"Field name '{name}' is a Python soft keyword and cannot be used")

        self.name = name
        self.owner = owner

    @overload
    def __get__(self, obj: None, cls: type) -> Self: ...

    @overload
    def __get__(self, obj: object, cls: type) -> T: ...

    def __get__(self, obj: object | None, cls: type) -> Self | T:
        """
        Return field descriptor for class-level access.

        Args:
            obj: The instance accessing the field (None for class access)
            cls: The class that owns this field

        Returns:
            The field descriptor itself when accessed from the class (obj is None).

        Raises:
            AttributeError: If accessed from an instance rather than class
        """
        if obj is None:
            return self
        raise AttributeError(
            f"Field '{self.name}' cannot be accessed from instances. "
            f"Use {cls.__name__}.{self.name} instead."
        )

    def __set__(self, instance: Any, value: Any) -> None:
        """
        Prevent field assignment.

        Raises:
            AttributeError: Always, as fields are immutable
        """
        raise AttributeError(f"Field '{self.name}' cannot be modified. Fields are immutable.")

    def __delete__(self, instance: Any) -> None:
        """
        Prevent field deletion.

        Raises:
            AttributeError: Always, as fields cannot be deleted
        """
        raise AttributeError(f"Field '{self.name}' cannot be deleted. Fields are immutable.")

    def __repr__(self) -> str:
        """
        Return informative repr showing field type and name.

        Shows ``TypeName('field_name')`` for bound fields,
        ``TypeName('field_name', source='SOURCE')`` when a source is set, or
        ``TypeName(unbound)`` for fields not yet attached to a model.
        """
        cls_name = type(self).__name__
        if self.name is None:
            return f"{cls_name}(unbound)"
        if self.source is not None:
            return f"{cls_name}('{self.name}', source='{self.source}')"
        return f"{cls_name}('{self.name}')"

    def _filter_name(self) -> str:
        """
        Return the name a filter predicate on this field carries.

        Returns:
            The field's name.

        Raises:
            RuntimeError: If the field is not bound to a model yet.
        """
        return _check_name(self.name)

    # -- Comparison operators returning Predicate nodes -----------------------

    def __eq__(self, value: Any) -> Exact:
        """
        Create equality predicate: ``field == value``.

        Returns an Exact predicate node for this field. ``field == None`` compiles to
        ``IS NULL``, not ``= NULL`` (which would match no row).

        Args:
            value: The value to compare against.

        Returns:
            Exact predicate with this field's name and the given value.

        Example:
            .. code-block:: pycon

                >>> Users.country == 'US'  # doctest: +SKIP
                Exact(field_name='country', value='US')
        """
        name = self._filter_name()
        from .filters import Exact as _Exact

        return _Exact(field_name=name, value=value, source=self.source)

    def __ne__(self, value: Any) -> NotEqual:
        """
        Create inequality predicate: ``field != value``.

        ``field != None`` compiles to ``IS NOT NULL``, not ``<> NULL`` (which would match
        no row).

        Returns NotEqual(...) predicate node.

        Args:
            value: The value to compare against.

        Returns:
            NotEqual predicate with this field's name and the given value.

        Example:
            .. code-block:: pycon

                >>> Users.country != 'US'  # doctest: +SKIP
                NotEqual(field_name='country', value='US')
        """
        name = self._filter_name()
        from .filters import NotEqual as _NotEqual

        return _NotEqual(field_name=name, value=value, source=self.source)

    def __lt__(self, value: Any) -> Lt:
        """
        Create less-than predicate: ``field < value``.

        Args:
            value: The value to compare against.

        Returns:
            Lt predicate node.

        Raises:
            TypeError: If the value is ``None``: a comparison with NULL matches no row. Use
                ``.isnull()``, or ``== None`` / ``!= None``, to match missing values.

        Example:
            .. code-block:: pycon

                >>> Users.age < 30  # doctest: +SKIP
                Lt(field_name='age', value=30)
        """
        name = self._filter_name()
        from .filters import Lt as _Lt

        return _Lt(field_name=name, value=value, source=self.source)

    def __le__(self, value: Any) -> Lte:
        """
        Create less-than-or-equal predicate: ``field <= value``.

        Args:
            value: The value to compare against.

        Returns:
            Lte predicate node.

        Raises:
            TypeError: If the value is ``None``: a comparison with NULL matches no row. Use
                ``.isnull()``, or ``== None`` / ``!= None``, to match missing values.

        Example:
            .. code-block:: pycon

                >>> Users.age <= 30  # doctest: +SKIP
                Lte(field_name='age', value=30)
        """
        name = self._filter_name()
        from .filters import Lte as _Lte

        return _Lte(field_name=name, value=value, source=self.source)

    def __gt__(self, value: Any) -> Gt:
        """
        Create greater-than predicate: ``field > value``.

        Args:
            value: The value to compare against.

        Returns:
            Gt predicate node.

        Raises:
            TypeError: If the value is ``None``: a comparison with NULL matches no row. Use
                ``.isnull()``, or ``== None`` / ``!= None``, to match missing values.

        Example:
            .. code-block:: pycon

                >>> Users.revenue > 1000  # doctest: +SKIP
                Gt(field_name='revenue', value=1000)
        """
        name = self._filter_name()
        from .filters import Gt as _Gt

        return _Gt(field_name=name, value=value, source=self.source)

    def __ge__(self, value: Any) -> Gte:
        """
        Create greater-than-or-equal predicate: ``field >= value``.

        Args:
            value: The value to compare against.

        Returns:
            Gte predicate node.

        Raises:
            TypeError: If the value is ``None``: a comparison with NULL matches no row. Use
                ``.isnull()``, or ``== None`` / ``!= None``, to match missing values.

        Example:
            .. code-block:: pycon

                >>> Users.revenue >= 1000  # doctest: +SKIP
                Gte(field_name='revenue', value=1000)
        """
        name = self._filter_name()
        from .filters import Gte as _Gte

        return _Gte(field_name=name, value=value, source=self.source)

    # Preserve identity-based hashing even with __eq__ defined
    # (Python sets __hash__ = None when __eq__ is defined without explicit __hash__)
    __hash__ = object.__hash__

    # -- Named filter methods -------------------------------------------------

    def between(self, lo: Any, hi: Any) -> Between:
        """
        Create range predicate: ``field BETWEEN lo AND hi``.

        Args:
            lo: Lower bound (inclusive).
            hi: Upper bound (inclusive).

        Returns:
            Between predicate with value as (lo, hi) tuple.

        Raises:
            TypeError: If either bound is ``None``. For an open-ended range, use ``>=`` or
                ``<=``.

        Example:
            .. code-block:: pycon

                >>> Sales.revenue.between(100, 1000)  # doctest: +SKIP
                Between(field_name='revenue', value=(100, 1000))
        """
        name = self._filter_name()
        from .filters import Between as _Between

        return _Between(field_name=name, value=(lo, hi), source=self.source)

    def in_(self, values: Iterable[Any]) -> In:
        """
        Create membership predicate: ``field IN (values)``.

        Args:
            values: The values to match: a list, tuple, set, or any other iterable, including
                a generator. They are copied into a tuple now, so changing the list afterwards
                does not change the predicate. An empty collection matches no rows.

        Returns:
            In predicate holding a tuple of the given values.

        Raises:
            TypeError: If ``values`` is a single ``str`` or ``bytes``, which would otherwise be
                read as its characters, or is not iterable. Also if any value is ``None``:
                to include missing values, OR the ``in_()`` with ``.isnull()``.

        Example:
            .. code-block:: pycon

                >>> Sales.country.in_(['US', 'CA', 'UK'])  # doctest: +SKIP
                In(field_name='country', value=('US', 'CA', 'UK'))
        """
        name = self._filter_name()
        from .filters import In as _In

        return _In(field_name=name, value=values, source=self.source)

    def like(self, pattern: str) -> Like:
        """
        Create case-sensitive pattern match: ``field LIKE pattern``.

        Args:
            pattern: SQL LIKE pattern (use % and _ wildcards).

        Returns:
            Like predicate with the given pattern.

        Example:
            .. code-block:: pycon

                >>> Users.name.like('%Smith%')  # doctest: +SKIP
                Like(field_name='name', value='%Smith%')
        """
        name = self._filter_name()
        from .filters import Like as _Like

        return _Like(field_name=name, value=pattern, source=self.source)

    def ilike(self, pattern: str) -> ILike:
        """
        Create case-insensitive pattern match: ``field ILIKE pattern``.

        Args:
            pattern: SQL ILIKE pattern (use % and _ wildcards).

        Returns:
            ILike predicate with the given pattern.

        Example:
            .. code-block:: pycon

                >>> Users.name.ilike('%smith%')  # doctest: +SKIP
                ILike(field_name='name', value='%smith%')
        """
        name = self._filter_name()
        from .filters import ILike as _ILike

        return _ILike(field_name=name, value=pattern, source=self.source)

    def startswith(self, prefix: str) -> StartsWith:
        """
        Create case-sensitive prefix match: ``field LIKE 'prefix%'``.

        The prefix is matched literally: a ``%`` or ``_`` in it is escaped (with
        ``ESCAPE '!'``), not treated as a wildcard.

        Args:
            prefix: The prefix string to match.

        Returns:
            StartsWith predicate with the given prefix.

        Raises:
            TypeError: If the prefix is ``None``.

        Example:
            .. code-block:: pycon

                >>> Users.name.startswith('A')  # doctest: +SKIP
                StartsWith(field_name='name', value='A')
        """
        name = self._filter_name()
        from .filters import StartsWith as _StartsWith

        return _StartsWith(field_name=name, value=prefix, source=self.source)

    def istartswith(self, prefix: str) -> IStartsWith:
        """
        Create case-insensitive prefix match: ``field ILIKE 'prefix%'``.

        The prefix is matched literally: a ``%`` or ``_`` in it is escaped (with
        ``ESCAPE '!'``), not treated as a wildcard.

        Args:
            prefix: The prefix string to match (case-insensitive).

        Returns:
            IStartsWith predicate with the given prefix.

        Raises:
            TypeError: If the prefix is ``None``.

        Example:
            .. code-block:: pycon

                >>> Users.name.istartswith('a')  # doctest: +SKIP
                IStartsWith(field_name='name', value='a')
        """
        name = self._filter_name()
        from .filters import IStartsWith as _IStartsWith

        return _IStartsWith(field_name=name, value=prefix, source=self.source)

    def endswith(self, suffix: str) -> EndsWith:
        """
        Create case-sensitive suffix match: ``field LIKE '%suffix'``.

        The suffix is matched literally: a ``%`` or ``_`` in it is escaped (with
        ``ESCAPE '!'``), not treated as a wildcard.

        Args:
            suffix: The suffix string to match.

        Returns:
            EndsWith predicate with the given suffix.

        Raises:
            TypeError: If the suffix is ``None``.

        Example:
            .. code-block:: pycon

                >>> Users.name.endswith('son')  # doctest: +SKIP
                EndsWith(field_name='name', value='son')
        """
        name = self._filter_name()
        from .filters import EndsWith as _EndsWith

        return _EndsWith(field_name=name, value=suffix, source=self.source)

    def iendswith(self, suffix: str) -> IEndsWith:
        """
        Create case-insensitive suffix match: ``field ILIKE '%suffix'``.

        The suffix is matched literally: a ``%`` or ``_`` in it is escaped (with
        ``ESCAPE '!'``), not treated as a wildcard.

        Args:
            suffix: The suffix string to match (case-insensitive).

        Returns:
            IEndsWith predicate with the given suffix.

        Raises:
            TypeError: If the suffix is ``None``.

        Example:
            .. code-block:: pycon

                >>> Users.name.iendswith('SON')  # doctest: +SKIP
                IEndsWith(field_name='name', value='SON')
        """
        name = self._filter_name()
        from .filters import IEndsWith as _IEndsWith

        return _IEndsWith(field_name=name, value=suffix, source=self.source)

    def iexact(self, value: str) -> IExact:
        """
        Create case-insensitive equality: ``field ILIKE value`` (no wildcards).

        The value is matched literally: a ``%`` or ``_`` in it is escaped (with
        ``ESCAPE '!'``), not treated as a wildcard.

        Args:
            value: The value to compare (case-insensitive).

        Returns:
            IExact predicate with the given value.

        Raises:
            TypeError: If the value is ``None``.

        Example:
            .. code-block:: pycon

                >>> Users.country.iexact('united states')  # doctest: +SKIP
                IExact(field_name='country', value='united states')
        """
        name = self._filter_name()
        from .filters import IExact as _IExact

        return _IExact(field_name=name, value=value, source=self.source)

    def isnull(self) -> IsNull:
        """
        Create null check predicate: ``field IS NULL``.

        Returns:
            IsNull predicate with value=True.

        Example:
            .. code-block:: pycon

                >>> Users.country.isnull()  # doctest: +SKIP
                IsNull(field_name='country', value=True)
        """
        name = self._filter_name()
        from .filters import IsNull as _IsNull

        return _IsNull(field_name=name, value=True, source=self.source)

    # -- Escape hatch ---------------------------------------------------------

    def lookup(self, lookup_cls: type[Lookup[Any]], value: Any) -> Lookup[Any]:
        """
        Build a predicate from a built-in Lookup class.

        The operators and named methods cover every built-in lookup, so you rarely need
        this. It is not an extension point: only the ``Lookup`` subclasses in
        :mod:`semolina.filters` compile, and a subclass of your own raises
        ``NotImplementedError`` at ``to_sql()`` or ``execute()``.

        Args:
            lookup_cls: A built-in Lookup subclass to instantiate.
            value: The value for the lookup.

        Returns:
            Instance of lookup_cls with this field's name and the given value.

        Example:
            .. code-block:: pycon

                >>> from semolina.filters import Exact
                >>> Users.country.lookup(Exact, 'US')  # doctest: +SKIP
                Exact(field_name='country', value='US')
        """
        name = self._filter_name()
        return lookup_cls(field_name=name, value=value, source=self.source)

    # -- Ordering methods -----------------------------------------------------

    def asc(self, nulls: NullsOrdering | None = None) -> OrderTerm:
        """
        Return ascending OrderTerm for use in order_by().

        Args:
            nulls: How to position NULL values (NullsOrdering.FIRST, NullsOrdering.LAST,
                   or None for default)

        Returns:
            OrderTerm with descending=False

        Example:
            .. code-block:: pycon

                >>> term = Sales.revenue.asc()
                >>> term.descending
                False
        """
        nulls_handling = nulls if nulls is not None else NullsOrdering.DEFAULT
        return OrderTerm(field=self, descending=False, nulls=nulls_handling)

    def desc(self, nulls: NullsOrdering | None = None) -> OrderTerm:
        """
        Return descending OrderTerm for use in order_by().

        Args:
            nulls: How to position NULL values (NullsOrdering.FIRST, NullsOrdering.LAST,
                   or None for default)

        Returns:
            OrderTerm with descending=True

        Example:
            .. code-block:: pycon

                >>> term = Sales.revenue.desc()
                >>> term.descending
                True
        """
        nulls_handling = nulls if nulls is not None else NullsOrdering.DEFAULT
        return OrderTerm(field=self, descending=True, nulls=nulls_handling)


class Metric(Field[T]):
    """
    Descriptor for metric fields (aggregatable measures).

    Metrics represent quantitative measurements that can be aggregated,
    such as SUM(revenue), AVG(price), COUNT(*).
    """

    _member_type: ClassVar[MemberType | None] = "metric"


class Dimension(Field[T]):
    """
    Descriptor for dimension fields (groupable attributes).

    Dimensions represent categorical or descriptive attributes used for
    grouping and filtering, such as country, product_category, date.
    """

    _member_type: ClassVar[MemberType | None] = "dimension"

    def __init__(
        self,
        source: str | None = None,
        *,
        label: str | None = None,
        description: str | None = None,
        synonyms: Iterable[str] = (),
        semantic_format: SemanticFormat | Mapping[str, Any] | None = None,
    ) -> None:
        """
        Initialize the dimension.

        Args:
            source: Optional explicit SQL column name. See :class:`Field`.
            label: A short display name. See :class:`Field`.
            description: What the field holds. See :class:`Field`.
            synonyms: Other names for the field. See :class:`Field`.
            semantic_format: What the values mean. See :class:`Field`.
        """
        super().__init__(
            source,
            label=label,
            description=description,
            synonyms=synonyms,
            semantic_format=semantic_format,
        )
        self._grains: dict[str, GrainedDimension] = {}

    def __set_name__(self, owner: type, name: str) -> None:
        """
        Bind the dimension to its model, forgetting grains made for any earlier owner.

        A subclass model gets a shallow copy of each inherited field, so without the reset
        the copy would share its parent's cache and hand out grains bound to the parent.

        Args:
            owner: The class that owns this field.
            name: The name of the attribute.
        """
        super().__set_name__(owner, name)
        self._grains = {}

    def grain(self, unit: Grain) -> GrainedDimension:
        """
        Select this date or timestamp dimension truncated to a coarser time grain.

        Use the result in ``.dimensions()`` and ``.order_by()`` like any dimension. Its
        result column is named ``<field>__<unit>`` (``ordered_at__week``), and the query
        truncates in the warehouse, so a metric is aggregated per week rather than per day.

        The value is the start of the period. Weeks start on Monday on every backend
        (Snowflake's ``WEEK_START`` session parameter does not change it). A grain of a day
        or coarser comes back as a ``DATE``, and an hour, minute or second as a
        ``TIMESTAMP``, whichever backend runs the query.

        Args:
            unit: One of :data:`GRAINS`: ``"year"``, ``"quarter"``, ``"month"``, ``"week"``,
                ``"day"``, ``"hour"``, ``"minute"`` or ``"second"``.

        Returns:
            The truncated dimension. Calling ``grain()`` again with the same unit returns
            the same object.

        Raises:
            ValueError: If ``unit`` is not a grain, is finer than the dimension holds (an
                hour of a ``Dimension[datetime.date]``), or its column name is already a
                field of the model.
            TypeError: If the dimension is declared with a type that is not a date or
                timestamp, or is itself a truncated dimension.

        Example:
            .. code-block:: python

                Orders.query(
                    metrics=[Orders.revenue],
                    dimensions=[Orders.ordered_at.grain("week")],
                )
                # rows keyed {"revenue": ..., "ordered_at__week": datetime.date(...)}
        """
        if unit not in GRAINS:
            raise ValueError(
                f"grain() takes one of {', '.join(repr(g) for g in GRAINS)}, got {unit!r}"
            )
        cached = self._grains.get(unit)
        if cached is not None:
            return cached
        name = _check_name(self.name)
        declared = declared_type(self)
        if declared is datetime.date and unit not in DATE_GRAINS:
            raise ValueError(
                f"{name!r} is a date, so it has no {unit!r} grain. The finest is 'day'."
            )
        if declared is not None and declared not in (datetime.date, datetime.datetime):
            type_name = getattr(declared, "__name__", repr(declared))
            raise TypeError(
                f"grain() truncates a date or timestamp, and {name!r} is declared as {type_name}."
            )
        grained = GrainedDimension(self, unit)
        owner_fields: dict[str, Any] = dict(getattr(self.owner, "_fields", {}))
        if grained.name in owner_fields:
            raise ValueError(
                f"{name}.grain({unit!r}) would return a column named {grained.name!r}, "
                f"which is already a field of {getattr(self.owner, '__name__', 'the model')}."
            )
        self._grains[unit] = grained
        return grained


def _semantic_format(
    value: SemanticFormat | Mapping[str, Any] | None, member_type: MemberType | None
) -> SemanticFormat | None:
    """
    Validate a field's ``semantic_format`` argument.

    Args:
        value: The argument as given.
        member_type: The kind of field, checked against the block's kind; None for a plain
            :class:`Field`, which takes any kind.

    Returns:
        The validated block, or None when none was given.

    Raises:
        ValueError: If the block is invalid, or carries a ``label`` or ``synonyms``.
    """
    if value is None:
        return None
    from s_layer_semantic_formats import parse_block, validate_block

    block = parse_block(value) if member_type is None else validate_block(value, member_type)
    carried = [key for key in ("label", "synonyms") if getattr(block, key) is not None]
    if carried:
        names = " and ".join(carried)
        raise ValueError(
            f"semantic_format carries {names}, which a field takes as its own "
            f"argument{'s' if len(carried) > 1 else ''}: pass "
            f"{', '.join(f'{key}=' for key in carried)} instead."
        )
    return block


def declared_type(field: Field[Any]) -> object:
    """
    Return the type a field was declared with, as in ``Dimension[datetime.date]()``.

    An optional type, as in ``Dimension[datetime.date | None]()``, gives the type it
    wraps: the column is nullable, and its values are still dates.

    Args:
        field: A field.

    Returns:
        The type argument, or ``None`` for a field declared without one.
    """
    orig = getattr(field, "__orig_class__", None)
    args = get_args(orig) if orig is not None else ()
    if not args:
        return None
    declared = args[0]
    if get_origin(declared) in (Union, types.UnionType):
        members = [arg for arg in get_args(declared) if arg is not type(None)]
        if len(members) == 1:
            return members[0]
    return declared


class GrainedDimension(Dimension[Any]):
    """
    A date or timestamp dimension truncated to a coarser grain, from :meth:`Dimension.grain`.

    It selects and orders like a dimension, under the name ``<field>__<unit>``. It cannot
    be filtered: filter the dimension it came from, which narrows rows before they are
    grouped (``Orders.ordered_at >= datetime.date(2024, 1, 1)``).

    Attributes:
        base: The dimension being truncated.
        unit: The grain it is truncated to.
    """

    def __init__(self, base: Dimension[Any], unit: Grain) -> None:
        """
        Bind the truncated dimension to its base's model.

        Args:
            base: The dimension being truncated, already bound to a model.
            unit: The grain.
        """
        # No source of its own: the builder reads the base's column and truncates it.
        super().__init__()
        # It describes the same thing at a coarser grain, so it keeps what the base says
        # about it. Copied, not passed in: the base already validated them.
        self.label = base.label
        self.description = base.description
        self.synonyms = base.synonyms
        self.semantic_format = base.semantic_format
        self.base = base
        self.unit: Grain = unit
        self.name = f"{_check_name(base.name)}__{unit}"
        self.owner = base.owner

    @property
    def result_is_date(self) -> bool:
        """Whether the truncated value is a ``DATE`` (a day or coarser) or a ``TIMESTAMP``."""
        return self.unit in DATE_GRAINS

    def __repr__(self) -> str:
        """Show the base field and the grain, as the call that made it reads."""
        return f"{self.base!r}.grain({self.unit!r})"

    def grain(self, unit: Grain) -> GrainedDimension:
        """
        Refuse to truncate twice; truncate the base dimension instead.

        Args:
            unit: The grain asked for.

        Raises:
            TypeError: Always.
        """
        raise TypeError(
            f"{self.name!r} is already truncated. Call grain({unit!r}) on "
            f"{self.base.name!r} instead."
        )

    def _filter_name(self) -> str:
        """
        Refuse a filter on a truncated dimension.

        Raises:
            TypeError: Always, naming the base dimension to filter instead.
        """
        raise TypeError(
            f"{self.name!r} cannot be filtered. Filter {self.base.name!r} instead, which "
            f"narrows the rows before they are grouped by {self.unit}."
        )


class Fact(Field[T]):
    """
    Descriptor for fact fields (raw numeric values).

    Facts represent raw numeric values in fact tables that may be used
    in calculations but aren't pre-aggregated, such as unit_price, quantity.
    """

    _member_type: ClassVar[MemberType | None] = "fact"


@dataclass(frozen=True, eq=False)
class OrderTerm:
    """
    Wrapper specifying sort direction and NULL handling for a field in order_by().

    Created via Field.asc() or Field.desc() methods.

    Attributes:
        field: The Field to sort by
        descending: If True, sort descending (DESC). If False, sort ascending (ASC).
        nulls: How to position NULL values (NullsOrdering.FIRST, NullsOrdering.LAST,
               or NullsOrdering.DEFAULT)

    Example:
        .. code-block:: python

            >>> term = Sales.revenue.desc()
            >>> term.descending
            True
            >>> term_nulls = Sales.revenue.desc(NullsOrdering.FIRST)
            >>> term_nulls.nulls
            NULLS FIRST
    """

    field: Field[Any]
    descending: bool = False
    nulls: NullsOrdering = NullsOrdering.DEFAULT

    def __eq__(self, other: object) -> bool:
        """
        Compare by field *identity*, then by direction and NULL handling.

        Declared ``eq=False`` on the dataclass and written out here because the generated
        ``__eq__`` compared ``self.field == other.field``, which is
        :meth:`Field.__eq__` — the filter DSL's operator, returning a truthy ``Exact``
        predicate rather than a bool. Python treats any truthy result as equal, so every
        order term with the same direction compared equal regardless of its field.

        Args:
            other: Object to compare.

        Returns:
            True when both are OrderTerms over the same field object with the same direction
            and NULL handling.
        """
        if not isinstance(other, OrderTerm):
            return NotImplemented
        return (
            self.field is other.field
            and self.descending == other.descending
            and self.nulls == other.nulls
        )

    def __hash__(self) -> int:
        """
        Hash on the field's identity, matching :meth:`__eq__`.

        Returns:
            The hash of this term.
        """
        return hash((id(self.field), self.descending, self.nulls))

    def __repr__(self) -> str:
        """Return readable representation."""
        direction = "DESC" if self.descending else "ASC"
        nulls_str = f", {repr(self.nulls)}" if self.nulls != NullsOrdering.DEFAULT else ""
        return f"OrderTerm({self.field.name}, {direction}{nulls_str})"
