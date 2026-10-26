"""
Semantic view model base class for Semolina.

This module provides the SemanticView base class that enables ORM-style
model definitions with typed fields and immutable metadata.
"""

import copy
import types
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from .fields import RESERVED_FIELD_NAMES, Dimension, Fact, Field, Metric

if TYPE_CHECKING:
    from .query import Query

__all__ = [
    "SemanticViewMeta",
    "SemanticView",
]


class SemanticViewMeta(type):
    """Metaclass for SemanticView that enforces immutability after creation."""

    def __repr__(cls) -> str:
        """
        Return informative repr for SemanticView subclasses.

        Shows the view name, or ``abstract`` for an abstract model, and fields grouped by
        type (metrics, dimensions, facts). Falls back to default repr for the base
        SemanticView class itself.
        """
        if "_fields" not in cls.__dict__:
            return super().__repr__()
        abstract = cls.__dict__.get("_abstract", False)
        view_name = None if abstract else getattr(cls, "_view_name", None)
        raw_fields = cls.__dict__["_fields"]
        fields_dict: dict[str, Field[Any]] = dict(raw_fields)
        metrics = [n for n, f in fields_dict.items() if isinstance(f, Metric)]
        dims = [n for n, f in fields_dict.items() if isinstance(f, Dimension)]
        facts = [n for n, f in fields_dict.items() if isinstance(f, Fact)]
        parts = ["abstract"] if abstract else [f"view='{view_name}'"]
        if metrics:
            parts.append(f"metrics={metrics}")
        if dims:
            parts.append(f"dimensions={dims}")
        if facts:
            parts.append(f"facts={facts}")
        return f"<SemanticView '{cls.__name__}' {' '.join(parts)}>"

    def __setattr__(cls, name: str, value: Any) -> None:
        """
        Prevent modification of model classes after creation.

        Args:
            name: Attribute name
            value: Attribute value

        Raises:
            AttributeError: If attempting to modify a frozen model
        """
        # Only the class's own flag counts. A subclass inherits its parent's `_frozen = True`
        # through normal attribute lookup, and reading that would refuse the subclass its own
        # metadata while `__init_subclass__` is still setting it.
        if cls.__dict__.get("_frozen", False):
            raise AttributeError(
                f"Cannot modify {cls.__name__}.{name} after class creation. Models are immutable."
            )
        super().__setattr__(name, value)


class SemanticView(metaclass=SemanticViewMeta):
    """
    Base class for semantic view models.

    Models are defined by subclassing SemanticView with a view parameter:

        >>> class Orders(SemanticView, view='orders'):
        ...     total = Metric()
        ...     region = Dimension()
        >>> Orders._view_name
        'orders'

    The __init_subclass__ hook collects field descriptors and freezes
    the model metadata to prevent modification after class creation.

    Inheritance:
        A model can extend another model, naming its own view, or share fields through an
        ``abstract=True`` base that names none. Each class gets its own copy of every
        inherited field, bound to itself, so a subclass's queries read the subclass's view.
        A subclass field with an inherited name replaces the inherited one; any other
        attribute with that name removes it. An abstract model lists its fields but cannot
        be queried, and abstractness is not inherited.

        .. code-block:: python

            class Commerce(SemanticView, abstract=True):
                revenue = Metric()
                country = Dimension()


            class Sales(Commerce, view="sales"):
                pass


            class Returns(Commerce, view="returns"):
                refunds = Metric()

    Introspection:
        Models expose field information via class methods:

            Users.metrics()      # List[Metric] - for aggregation
            Users.dimensions()   # List[Dimension | Fact] - for grouping
    """

    _view_name: ClassVar[str]
    _fields: ClassVar[types.MappingProxyType[str, Field[Any]]]
    _frozen: ClassVar[bool] = False
    _abstract: ClassVar[bool] = True
    """True for a model that names no view. Set on every subclass, so it is never inherited."""
    _description: ClassVar[str | None] = None
    """The view's description, from ``description=``. Set on every subclass, never inherited."""

    def __init_subclass__(
        cls,
        view: str | None = None,
        abstract: bool = False,
        description: str | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Called when a class inherits from SemanticView.

        Collects Field descriptors, inherited and declared, and stores immutable metadata.

        Args:
            view: The semantic view name. Required unless ``abstract`` is True.
            abstract: Declare a base that holds shared fields and names no view. It can be
                subclassed and introspected, but not queried.
            description: What the view holds, as the warehouse's view ``COMMENT`` says it.
                Kept as ``_description``. A subclass does not inherit it, since it names a
                view of its own.
            **kwargs: Additional keyword arguments for cooperative inheritance

        Raises:
            TypeError: If view parameter is not provided for a concrete model, or is
                provided for an abstract one
        """
        super().__init_subclass__(**kwargs)

        if abstract and view is not None:
            raise TypeError(
                f"{cls.__name__} is abstract and cannot name a view. Drop view=, or drop "
                f"abstract=True to make it a model of that view."
            )
        if not abstract and view is None:
            raise TypeError(
                f"{cls.__name__} must specify a view parameter. "
                f"Example: class {cls.__name__}(SemanticView, view='view_name'). "
                f"For a base that only shares fields, use abstract=True."
            )

        # Walk every class dictionary from the farthest base to this class, so the nearest
        # definition of a name wins, as attribute lookup does. A non-field removes the name,
        # and must do so at every level: a removal leaves no entry in that class's `_fields`,
        # so merging each base's `_fields` would bring the field back one class further down.
        fields_dict: dict[str, Field[Any]] = {}
        for klass in reversed(cls.__mro__):
            for name, value in klass.__dict__.items():
                if isinstance(value, Field):
                    fields_dict[name] = value
                elif name in fields_dict:
                    del fields_dict[name]

        # An inherited field still names its base as owner. Give the class its own copy, so
        # the query's ownership check and the FROM clause both see this class.
        for name, field in fields_dict.items():
            if field.owner is not cls:
                own = copy.copy(field)
                own.__set_name__(cls, name)
                setattr(cls, name, own)
                fields_dict[name] = own

        # Check reserved names that would conflict with query builder methods
        for name in fields_dict:
            if name in RESERVED_FIELD_NAMES:
                raise ValueError(
                    f"Field name '{name}' is reserved and cannot be used. "
                    f"Did you mean: '{name}_field'? Or use source mapping: "
                    f"metric_field = Metric(source='{name}') for column aliasing."
                )

        # Store immutable metadata
        if view is not None:
            cls._view_name = view
        cls._abstract = abstract
        cls._description = description
        cls._fields = types.MappingProxyType(fields_dict)
        cls._frozen = True

    @classmethod
    def query(
        cls,
        *,
        metrics: Sequence[Metric[Any]] | None = None,
        dimensions: Sequence[Dimension[Any] | Fact[Any]] | None = None,
        using: str | None = None,
    ) -> "Query":
        """
        Create a query for this semantic view.

        Entry point for model-centric query construction. Accepts optional
        shorthand keyword arguments for metrics and dimensions, or returns
        a bare Query for fluent method chaining.

        Shorthand:
            Sales.query(
                metrics=[Sales.revenue],
                dimensions=[Sales.region],
            ).execute()

        Fluent:
            Sales.query().metrics(Sales.revenue).dimensions(
                Sales.region
            ).execute()

        Both styles can be combined -- builder methods are additive:
            Sales.query(metrics=[Sales.revenue]).metrics(Sales.cost)
            # selects both revenue and cost

        Args:
            metrics: Optional list of Metric fields to select
            dimensions: Optional list of Dimension or Fact fields to group by
            using: Optional pool/engine name (defaults to 'default')

        Returns:
            Query instance bound to this model's view

        Raises:
            TypeError: If the model is abstract, and so has no view to query

        Example:
            .. code-block:: python

                cursor = Sales.query(
                    metrics=[Sales.revenue],
                    dimensions=[Sales.region],
                ).execute()
        """
        if cls.__dict__.get("_abstract", False):
            raise TypeError(
                f"{cls.__name__} is abstract and has no view to query. Query a model that "
                f"names one, such as: class Sales({cls.__name__}, view='sales')"
            )
        from .query import Query as QueryImpl

        q = QueryImpl(_using=using)
        object.__setattr__(q, "_model", cls)

        if metrics:
            q = q.metrics(*metrics)
        if dimensions:
            q = q.dimensions(*dimensions)

        return q

    @classmethod
    def metrics(cls) -> list[Metric[Any]]:
        """
        Get all metric fields for this model.

        Returns a list of Metric descriptors with full metadata. Useful for
        REPL exploration and documentation generation.

        Returns:
            List of Metric field objects (may be empty if no metrics defined)

        Example:
            .. code-block:: pycon

                >>> class Users(SemanticView, view="users"):
                ...     revenue = Metric()
                ...     users_count = Metric()
                ...     country = Dimension()
                ...
                >>> metrics = Users.metrics()
                >>> len(metrics)
                2
                >>> metrics[0].name
                'revenue'
        """
        return [f for f in cls._fields.values() if isinstance(f, Metric)]

    @classmethod
    def dimensions(cls) -> list[Dimension[Any] | Fact[Any]]:
        """
        Get all dimension and fact fields for this model.

        Returns a list of Dimension and Fact descriptors with full metadata.
        Useful for REPL exploration and documentation generation.

        Returns:
            List of Dimension and Fact field objects (may be empty)

        Example:
            .. code-block:: pycon

                >>> class Orders(SemanticView, view="orders"):
                ...     revenue = Metric()
                ...     region = Dimension()
                ...     date = Fact()
                ...
                >>> dims = Orders.dimensions()
                >>> len(dims)
                2
        """
        return [f for f in cls._fields.values() if isinstance(f, Dimension | Fact)]
