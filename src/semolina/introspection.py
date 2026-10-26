"""
The result types of :meth:`Engine.introspect() <semolina.engines.base.Engine.introspect>`.

Frozen dataclasses describing a semantic view as the warehouse reports it. Each engine's
``introspect()`` produces them, and ``semolina codegen`` renders them into a
``SemanticView`` subclass. They live outside :mod:`semolina.codegen` because
``introspect()`` is part of the core API, and the CLI's package is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from s_layer_semantic_formats import SLayerBlock

__all__ = ["IntrospectedField", "IntrospectedView"]


@dataclass(frozen=True)
class IntrospectedField:
    """
    Intermediate representation of a single field from an introspected semantic view.

    Produced by warehouse introspection engines and consumed by the Python code
    renderer to generate Metric, Dimension, or Fact field declarations.

    Attributes:
        name: Python attribute name for the field (e.g., 'revenue', 'country'): the
            column name folded by its engine (lower-cased for Snowflake and Databricks),
            then made a valid, unique identifier by
            :func:`~semolina.codegen.introspector.python_field_names`.
        field_type: Semantic role of the field: 'metric', 'dimension', or 'fact'.
        data_type: Python annotation string (e.g., 'int', 'str', 'datetime.date'),
            or None if the SQL type has no clean Python equivalent (triggers a
            TODO comment in the generated output).
        description: Human-readable description of the field. Defaults to empty
            string if the warehouse provides no description metadata.
        source_name: Original warehouse column name when ``name`` does not lead back
            to it. Set when the column was created with a quoted identifier (e.g.,
            ``"order_id"`` in Snowflake, stored as lowercase), and whenever
            :func:`~semolina.codegen.introspector.python_field_names` had to change the
            name (``what?`` becomes ``what_``). None for standard columns that round-trip
            correctly through normalize_identifier.
        raw_type: The warehouse's own spelling of the column type (e.g.
            ``'DECIMAL(38,2)'``, ``'{"type": "FIXED", "scale": 2}'``), carried
            so the renderer can preserve it as a comment for annotations that
            do not name it. None means the engine supplied no raw type, and the
            renderer then emits no comment rather than guessing one.
        label: The field's display name, where the warehouse has one (Databricks'
            ``display_name``). None when it has none.
        synonyms: Other names for the field, from the warehouse's synonyms clause, in the
            order it reports them. Empty when it has none.
        semantic_format: What the values mean, as an S-LAYER block, where the warehouse
            carries one. None when it carries none.
    """

    name: str
    field_type: Literal["metric", "dimension", "fact"]
    data_type: str | None
    description: str = ""
    source_name: str | None = None
    raw_type: str | None = None
    label: str | None = None
    synonyms: tuple[str, ...] = ()
    semantic_format: SLayerBlock | None = None


@dataclass(frozen=True)
class IntrospectedView:
    """
    Intermediate representation of a semantic view returned by engine introspection.

    Produced by Engine.introspect() and consumed by the Python code renderer to
    generate a complete SemanticView subclass.

    Attributes:
        view_name: Warehouse identifier for the view (e.g., 'sales_view'). Used
            as the ``view=`` parameter in the generated SemanticView subclass.
        class_name: Python class name to use in the generated code (e.g., 'Sales').
            Derived from view_name by the introspection layer (snake_case → PascalCase).
        fields: All fields discovered in the view, in the order returned by the
            warehouse API. Includes metrics, dimensions, and facts.
        description: The view's own comment. Defaults to empty string if the warehouse
            provides none.
        unread_metadata: Metadata the engine reads in general
            (:attr:`~semolina.engines.base.Engine.introspected_metadata`) but could not read
            for this view, because the warehouse it ran against has no slot for it: DuckDB
            before ``semantic_views`` 0.17.0 stores no tags, so its labels and formats are
            unread. ``semolina codegen --check`` does not compare these.
    """

    view_name: str
    class_name: str
    fields: list[IntrospectedField] = field(default_factory=list)
    description: str = ""
    unread_metadata: frozenset[str] = frozenset()
