"""
Databricks backend engine for metric view introspection.

Provides the DatabricksEngine class. Query execution runs through the
:class:`~semolina.engines.base.Engine` ADBC pool path; this subclass adds
Databricks-specific ``introspect()``, which runs
``DESCRIBE TABLE EXTENDED {view} AS JSON`` over the engine's owned ADBC pool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from semolina.engines.base import Engine, _translate_introspection_errors

if TYPE_CHECKING:
    from typing import Literal

    from semolina.introspection import IntrospectedView

__all__ = [
    "DatabricksEngine",
]


def _to_pascal_case(view_name: str) -> str:
    """
    Convert a warehouse view identifier to a PascalCase Python class name.

    Args:
        view_name: Warehouse view identifier, e.g. ``"sales_view"``.

    Returns:
        PascalCase class name; see :func:`~semolina.codegen.introspector.class_name_for`.
    """
    from semolina.codegen.introspector import class_name_for

    return class_name_for(view_name)


def _introspect_databricks(cursor: Any, view_name: str, *, described: str) -> IntrospectedView:
    """
    Describe a Databricks metric view over an open cursor.

    The parsing half of :meth:`DatabricksEngine.introspect`, kept apart from the connection
    so any path that holds a cursor can reuse it. Driver errors propagate untranslated; wrap
    the call in :func:`~semolina.engines.base._translate_introspection_errors` to get Semolina's.

    Args:
        cursor: An open ADBC cursor on a Databricks connection.
        view_name: The metric view identifier as the caller gave it, kept as the
            ``IntrospectedView``'s name and the source of its class name.
        described: ``view_name`` quoted per part by
            :meth:`~semolina.engines.sql.DatabricksDialect.quote_table_name`. The caller
            quotes it before checking out a connection, so a malformed name fails with its
            ``ValueError`` without taking a pool slot.

    Returns:
        Intermediate representation of the view, ready for code rendering.
    """
    import json

    from semolina.codegen.introspector import (
        with_python_field_names,
    )
    from semolina.codegen.type_map import databricks_type_to_python
    from semolina.introspection import IntrospectedField, IntrospectedView

    cursor.execute(f"DESCRIBE TABLE EXTENDED {described} AS JSON")
    row: Any = cursor.fetchone()
    payload: dict[str, Any] = json.loads(row[0])

    fields: list[IntrospectedField] = []
    columns: list[str] = []
    for col in payload.get("columns", []):
        original_col_name = str(col["name"])
        columns.append(original_col_name)
        # Databricks metric views expose only dimensions and
        # measures; ``is_measure`` marks a metric, everything else is
        # a dimension (there is no Databricks "fact" concept).
        field_type = cast(
            "Literal['metric', 'dimension', 'fact']",
            "metric" if col.get("is_measure") else "dimension",
        )
        type_obj: Any = col.get("type") or {}
        py_type = databricks_type_to_python(type_obj if isinstance(type_obj, dict) else {})
        raw_type_name = type_obj.get("name") if isinstance(type_obj, dict) else str(type_obj)
        data_type = py_type if py_type is not None else f"TODO: {raw_type_name}"
        description = str(col.get("comment") or "")
        metadata: Any = col.get("metadata") or {}
        label = metadata.get("display_name") if isinstance(metadata, dict) else None
        synonyms: Any = metadata.get("synonyms") if isinstance(metadata, dict) else None

        # Databricks folds unquoted identifiers to lowercase (see
        # DatabricksDialect.normalize_identifier). Set source_name
        # only when the lowercased Python name would not round-trip
        # back to the exact warehouse column name.
        python_name = original_col_name.lower()
        source_name = original_col_name if python_name != original_col_name else None

        fields.append(
            IntrospectedField(
                name=python_name,
                field_type=field_type,
                data_type=data_type,
                description=description,
                raw_type=raw_type_name,
                source_name=source_name,
                label=str(label) if label else None,
                synonyms=tuple(str(s) for s in synonyms) if isinstance(synonyms, list) else (),
            )
        )

    return IntrospectedView(
        view_name=view_name,
        class_name=_to_pascal_case(view_name),
        fields=with_python_field_names(fields, columns),
        description=str(payload.get("comment") or ""),
    )


class DatabricksEngine(Engine):
    """
    Databricks backend engine for semantic view queries.

    Built by :func:`semolina.config.create_engine` from a ``DatabricksConfig``;
    it owns one ADBC connection pool (via adbc-poolhouse) plus the Databricks
    dialect, exactly like the Snowflake and DuckDB engines. Query execution runs
    through the :class:`~semolina.engines.base.Engine` pool path
    (:meth:`~semolina.engines.base.Engine.execute`).

    Error Handling (introspect):
        - ADBC ``ProgrammingError`` (invalid view, SQL syntax) ->
          ``SemolinaViewNotFoundError``
        - ADBC ``OperationalError`` or ``InterfaceError`` (connection, authentication) ->
          ``SemolinaConnectionError``
        - Any other ADBC error (the view cannot be described) ->
          ``SemolinaIntrospectionError``

    Connection Lifecycle:
        - One ADBC pool is owned by the Engine for its lifetime
        - ``connect()`` checks a connection out of the pool per call
        - The pool returns the connection on context-manager exit

    SQL Generation:
        - Delegates to SQLBuilder with DatabricksDialect
        - Generates MEASURE() wrapping for metrics
        - Uses backtick-quoted identifiers for case preservation
        - GROUP BY ALL for automatic dimension derivation

    Unity Catalog:
        - Three-part names (catalog.schema.view) work transparently
        - Each part quoted separately with backticks

    Example:
        .. code-block:: python

            from adbc_poolhouse import DatabricksConfig

            import semolina
            from semolina import Dimension, Metric, SemanticView
            from semolina.config import create_engine


            class Sales(SemanticView, view="main.analytics.sales_view"):
                revenue = Metric()
                country = Dimension()


            engine = create_engine(
                DatabricksConfig(
                    host="workspace.cloud.databricks.com",
                    http_path="/sql/1.0/warehouses/abc123",
                    token="dapi...",
                )
            )
            semolina.register("default", engine)
            results = (
                Sales.query()
                .metrics(Sales.revenue)
                .dimensions(Sales.country)
                .execute()
            )
            # Returns: [{"revenue": 1000, "country": "US"}, ...]

    See Also:
        - semolina.config.create_engine: Builds an Engine from a config or name
        - semolina.engines.sql.DatabricksDialect: SQL generation rules
        - semolina.engines.sql.SQLBuilder: Query to SQL converter
    """

    introspected_metadata: ClassVar[frozenset[str]] = frozenset(
        {"description", "label", "synonyms"}
    )

    def introspect(self, view_name: str) -> IntrospectedView:
        """
        Introspect a Databricks metric view and return its intermediate representation.

        Executes ``DESCRIBE TABLE EXTENDED {view_name} AS JSON``, with each part of
        the name folded and backtick-quoted as the query builder does, over the
        engine's owned ADBC pool and parses the single-cell JSON payload into an
        :class:`~semolina.introspection.IntrospectedView`. Each entry in
        the payload's ``columns`` array becomes one field: columns flagged
        ``is_measure`` map to ``metric``, all others to ``dimension`` (Databricks
        metric views expose only dimensions and measures). The ``type.name`` is
        mapped to a Python annotation string; types without a clean mapping
        produce a ``"TODO: ..."`` placeholder so generated code stays valid.

        Args:
            view_name: Databricks metric view identifier to introspect.
                Accepts schema-qualified (``schema.view``) and Unity Catalog
                three-part (``catalog.schema.view``) names. Unqualified names
                resolve against the connection's default catalog/schema.

        Returns:
            Intermediate representation of the view, ready for code rendering.

        Raises:
            SemolinaViewNotFoundError: If the view does not exist or is not
                accessible (wraps an ADBC
                :class:`~adbc_driver_manager.ProgrammingError`).
            SemolinaConnectionError: If the connection or authentication fails
                (wraps an ADBC ``OperationalError`` or ``InterfaceError``).
            SemolinaIntrospectionError: If the warehouse cannot describe the view (wraps
                any other ADBC :class:`~adbc_driver_manager.Error`).
            ValueError: If a backtick-quoted part of ``view_name`` contains a ``.`` or
                leaves a backtick unbalanced.

        Example:
            .. code-block:: python

                from adbc_poolhouse import DatabricksConfig

                from semolina.config import create_engine

                engine = create_engine(
                    DatabricksConfig(
                        host="workspace.cloud.databricks.com",
                        http_path="/sql/1.0/warehouses/abc123",
                        token="dapi...",
                    )
                )
                view = engine.introspect("analytics.sales_view")
                print(view.class_name)
                # SalesView
        """
        from semolina.engines.sql import DatabricksDialect

        # Quoted per part, the way the query builder names the view, so a space or a
        # backtick in the name parses and a backtick-quoted part keeps its case.
        described = DatabricksDialect().quote_table_name(view_name)

        with _translate_introspection_errors("Databricks"), self.connect() as conn:
            return _introspect_databricks(conn.cursor(), view_name, described=described)
