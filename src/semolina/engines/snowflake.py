"""
Snowflake backend engine for semantic view introspection.

Provides the SnowflakeEngine class. Query execution runs through the
:class:`~semolina.engines.base.Engine` ADBC pool path; this subclass adds
Snowflake-specific ``introspect()``, which runs ``SHOW COLUMNS IN VIEW`` over
the engine's owned ADBC pool.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, ClassVar, cast

from semolina.engines.base import Engine, _translate_introspection_errors
from semolina.exceptions import (
    SemolinaViewNotFoundError,
)

if TYPE_CHECKING:
    from typing import Literal

    from semolina.introspection import IntrospectedView

__all__ = [
    "SnowflakeEngine",
]


def _qualify_view_name(cursor: Any, view_name: str) -> str:
    """
    Complete a view name to the ``database.schema.view`` path SHOW COLUMNS requires.

    Snowflake refuses ``SHOW COLUMNS IN VIEW`` on anything shorter, and reads a two-part
    name as ``schema.view``. Missing parts come from the session rather than the engine's
    config: the session is where the connection actually landed (the Snowflake ADBC driver
    ignores a configured schema when no database is set), and its answer is a query result,
    so it replays from a cassette.

    Args:
        cursor: An open ADBC cursor on the engine's connection.
        view_name: ``view``, ``schema.view`` or ``database.schema.view``.

    Returns:
        The three-part name, each part quoted. The caller's parts are folded and quoted as
        a query's ``FROM`` would be; the session's are quoted as returned, since they are
        already the stored names. A three-part name needs no query.

    Raises:
        SemolinaViewNotFoundError: If the session has no current database or schema to
            fill a missing part with.
    """
    from semolina.engines.sql import SnowflakeDialect

    dialect = SnowflakeDialect()
    quoted = dialect.quote_table_name(view_name)
    parts = view_name.split(".")
    if len(parts) >= 3:
        return quoted

    cursor.execute("SELECT CURRENT_DATABASE(), CURRENT_SCHEMA()")
    database, schema = cursor.fetchone()
    missing = [database] if len(parts) == 2 else [database, schema]
    if any(part is None for part in missing):
        msg = (
            f"Cannot introspect {view_name!r}: the session has no current "
            f"{'database' if database is None else 'schema'} to complete it. Pass "
            "database.schema.view, or set database and schema in the connection config."
        )
        raise SemolinaViewNotFoundError(msg)
    return ".".join([*(dialect.quote_identifier(part) for part in missing), quoted])


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


_TAG_DOMAINS = {
    "DIMENSION": "SEMANTIC DIMENSION",
    "FACT": "SEMANTIC FACT",
    "METRIC": "SEMANTIC METRIC",
    "DERIVED_METRIC": "SEMANTIC METRIC",
}
"""``TAG_REFERENCES``' domain for each member kind ``DESCRIBE SEMANTIC VIEW`` reports."""


@dataclass
class _Described:
    """What ``DESCRIBE SEMANTIC VIEW`` adds to ``SHOW COLUMNS``."""

    synonyms: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Member name (as stored) to its synonyms."""
    members: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    """Member name to its kind and logical table (None for a derived metric)."""
    view_comment: str = ""
    """The view's own comment, empty when it has none."""


def _describe_annotations(cursor: Any, qualified_name: str) -> _Described:
    """
    Read member synonyms, tables and the view's own comment from ``DESCRIBE SEMANTIC VIEW``.

    ``SHOW COLUMNS IN VIEW`` reports each member's comment but not its synonyms, the
    logical table it belongs to (which reading its tags needs), or anything about the view
    itself, so this second statement fills those in.

    Args:
        cursor: An open ADBC cursor on a Snowflake connection.
        qualified_name: The quoted three-part view name.

    Returns:
        The synonyms, members and view comment.

    Raises:
        ValueError: If a ``SYNONYMS`` value is not a JSON list of strings.
    """
    from semolina.codegen.introspector import synonyms_from_json

    cursor.execute(f"DESCRIBE SEMANTIC VIEW {qualified_name}")
    columns = [desc[0].lower() for desc in cursor.description]
    described = _Described()
    for row in cursor.fetchall():
        d: dict[str, Any] = dict(zip(columns, row, strict=True))
        kind, prop, value = d.get("object_kind"), d.get("property"), d.get("property_value")
        if not kind and prop == "COMMENT":
            described.view_comment = str(value or "")
        elif kind in _TAG_DOMAINS:
            name = str(d["object_name"])
            parent = d.get("parent_entity")
            described.members.setdefault(name, (str(kind), str(parent) if parent else None))
            if prop == "SYNONYMS":
                described.synonyms[name] = synonyms_from_json(value)
    return described


def _member_tags_sql(qualified_name: str, members: dict[str, tuple[str, str | None]]) -> str:
    """
    Build one query reading every member's own ``S_LAYER_*`` tags.

    Snowflake keeps semantic view member tags out of ``DESCRIBE``, ``SHOW`` and the
    ``SEMANTIC_*`` information schema views. ``TAG_REFERENCES`` reads them, one member at a
    time and only for constant arguments, so the query is a ``UNION ALL`` of one call per
    member, each labelled with the member it asked about. ``LEVEL = DOMAIN`` keeps the
    member's own tags: a tag set on the view or a logical table is inherited by every
    member and reported with the view's or table's level.

    Args:
        qualified_name: The quoted three-part view name.
        members: Member name to its kind and logical table, from :func:`_describe_annotations`.

    Returns:
        A query returning one ``(MEMBER, TAG_NAME, TAG_VALUE)`` row per tag.
    """
    from semolina.codegen.introspector import S_LAYER_KIND, S_LAYER_LABEL, S_LAYER_SPEC
    from semolina.engines.sql import SnowflakeDialect

    dialect = SnowflakeDialect()
    quote, literal = dialect.quote_identifier, dialect.render_literal
    database = qualified_name.split(".")[0]
    calls = []
    for name, (kind, table) in members.items():
        path = (
            f"{qualified_name}!{quote(table)}.{quote(name)}"
            if table
            else (f"{qualified_name}!{quote(name)}")
        )
        calls.append(
            f"SELECT {literal(name)} AS MEMBER, TAG_NAME, TAG_VALUE, LEVEL, DOMAIN"
            f" FROM TABLE({database}.INFORMATION_SCHEMA.TAG_REFERENCES("
            f"{literal(path)}, {literal(_TAG_DOMAINS[kind])}))"
        )
    names = ", ".join(literal(t) for t in (S_LAYER_KIND, S_LAYER_LABEL, S_LAYER_SPEC))
    return (
        "SELECT MEMBER, TAG_NAME, TAG_VALUE FROM ("
        + " UNION ALL ".join(calls)
        + f") WHERE LEVEL = DOMAIN AND TAG_NAME IN ({names})"
    )


def _member_tags(
    cursor: Any, qualified_name: str, members: dict[str, tuple[str, str | None]]
) -> dict[str, dict[str, str]]:
    """
    Read every member's own ``S_LAYER_*`` tags.

    Args:
        cursor: An open ADBC cursor on a Snowflake connection.
        qualified_name: The quoted three-part view name.
        members: Member name to its kind and logical table.

    Returns:
        Member name (as stored) to tag name to value. A member with no tags is absent.
    """
    if not members:
        return {}
    cursor.execute(_member_tags_sql(qualified_name, members))
    tags: dict[str, dict[str, str]] = {}
    for member, tag_name, tag_value in cursor.fetchall():
        tags.setdefault(str(member), {})[str(tag_name)] = str(tag_value)
    return tags


def _introspect_snowflake(cursor: Any, view_name: str) -> IntrospectedView:
    """
    Describe a Snowflake semantic view over an open cursor.

    The parsing half of :meth:`SnowflakeEngine.introspect`, kept apart from the connection so
    any path that holds a cursor can reuse it. Driver errors propagate untranslated; wrap the
    call in :func:`~semolina.engines.base._translate_introspection_errors` to get Semolina's.

    Args:
        cursor: An open ADBC cursor on a Snowflake connection.
        view_name: ``view``, ``schema.view`` or ``database.schema.view``; see
            :func:`_qualify_view_name`.

    Returns:
        Intermediate representation of the view, ready for code rendering.

    Raises:
        SemolinaViewNotFoundError: If the name needs a database or schema the session does
            not have.
        ValueError: If a quoted part of ``view_name`` contains a ``.`` or leaves a quote
            unbalanced.
    """
    import json

    from semolina.codegen.introspector import (
        S_LAYER_LABEL,
        semantic_format_from_tags,
        with_python_field_names,
    )
    from semolina.codegen.type_map import snowflake_json_type_to_python
    from semolina.introspection import IntrospectedField, IntrospectedView

    qualified_name = _qualify_view_name(cursor, view_name)
    cursor.execute(f"SHOW COLUMNS IN VIEW {qualified_name}")

    # Build column name list from cursor description (lowercase for safe access)
    columns = [desc[0].lower() for desc in cursor.description]

    fields: list[IntrospectedField] = []
    column_names: list[str] = []
    for row in cursor.fetchall():
        d: dict[str, Any] = dict(zip(columns, row, strict=True))
        # A view-level derived metric reports its kind as DERIVED_METRIC. It is queried
        # like any other metric, so it is one.
        kind = "METRIC" if d["kind"] == "DERIVED_METRIC" else str(d["kind"])
        field_type = cast("Literal['metric', 'dimension', 'fact']", kind.lower())
        type_json: dict[str, object] = json.loads(d["data_type"])
        py_type = snowflake_json_type_to_python(type_json)
        data_type = f"TODO: {d['data_type']}" if py_type is None else py_type
        description = str(d.get("comment") or "")

        # Lowercase the column name to produce a Pythonic field name.
        # For standard Snowflake UPPERCASE columns (e.g., ORDER_ID),
        # the Python name is 'order_id' and normalize_identifier
        # round-trips it back to 'ORDER_ID' — no source_name needed.
        # For quoted-lowercase columns (e.g., "order_id" stored as-is),
        # upper() != original, so source_name is set to preserve the
        # exact warehouse column name for SQL generation.
        original_col_name = str(d["column_name"])
        column_names.append(original_col_name)
        python_name = original_col_name.lower()
        normalized_back = python_name.upper()  # SnowflakeDialect behavior
        source_name = original_col_name if normalized_back != original_col_name else None

        fields.append(
            IntrospectedField(
                name=python_name,
                field_type=field_type,
                data_type=data_type,
                description=description,
                raw_type=str(d["data_type"]),
                source_name=source_name,
            )
        )

    described = _describe_annotations(cursor, qualified_name)
    tags = _member_tags(cursor, qualified_name, described.members)
    fields = [
        replace(
            f,
            synonyms=described.synonyms.get(column, ()),
            label=tags.get(column, {}).get(S_LAYER_LABEL) or None,
            semantic_format=semantic_format_from_tags(column, tags.get(column, {}), f.field_type),
        )
        for f, column in zip(fields, column_names, strict=True)
    ]

    return IntrospectedView(
        view_name=view_name,
        class_name=_to_pascal_case(view_name),
        fields=with_python_field_names(fields, column_names),
        description=described.view_comment,
    )


class SnowflakeEngine(Engine):
    """
    Snowflake backend engine for semantic view queries and introspection.

    Built by :func:`semolina.config.create_engine` from a ``SnowflakeConfig``;
    it owns one ADBC connection pool (via adbc-poolhouse) plus the Snowflake
    dialect. Query execution runs through the
    :class:`~semolina.engines.base.Engine` pool path (``execute()``); this
    subclass adds Snowflake-specific :meth:`introspect`, which runs
    ``SHOW COLUMNS IN VIEW`` over a pooled ADBC connection.

    Connection Lifecycle:
        - One ADBC pool is owned by the Engine for its lifetime
        - ``connect()`` checks a connection out of the pool per call
        - The pool returns the connection on context-manager exit

    Error Handling (introspect):
        - ADBC ``ProgrammingError`` (invalid view, SQL syntax) ->
          ``SemolinaViewNotFoundError``
        - ADBC ``OperationalError`` or ``InterfaceError`` (connection, authentication) ->
          ``SemolinaConnectionError``
        - Any other ADBC error (the view cannot be described) ->
          ``SemolinaIntrospectionError``

    SQL Generation:
        - Delegates to SQLBuilder with SnowflakeDialect
        - Generates AGG() wrapping for metrics
        - Uses double-quoted identifiers for case preservation
        - GROUP BY ALL for automatic dimension derivation

    Example:
        .. code-block:: python

            from adbc_poolhouse import SnowflakeConfig

            import semolina
            from semolina import Dimension, Metric, SemanticView
            from semolina.config import create_engine


            class Sales(SemanticView, view="sales_view"):
                revenue = Metric()
                country = Dimension()


            engine = create_engine(
                SnowflakeConfig(account="xy12345.us-east-1", user="username")
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
        - semolina.engines.sql.SnowflakeDialect: SQL generation rules
        - semolina.engines.sql.SQLBuilder: Query to SQL converter
    """

    introspected_metadata: ClassVar[frozenset[str]] = frozenset(
        {"description", "label", "synonyms", "semantic_format"}
    )

    def introspect(self, view_name: str) -> IntrospectedView:
        """
        Introspect a Snowflake semantic view and return its intermediate representation.

        Executes ``SHOW COLUMNS IN VIEW {view_name}`` against Snowflake
        and parses the result rows into an :class:`~semolina.introspection.IntrospectedView`.
        Column ``kind`` values (``METRIC``, ``DIMENSION``, ``FACT``) are
        lowercased before use. The ``data_type`` JSON column is parsed and mapped
        to a Python annotation string; types without a clean mapping produce a
        ``"TODO: ..."`` placeholder so generated code remains syntactically valid.

        Args:
            view_name: Snowflake semantic view identifier to introspect: a bare
                ``view``, ``schema.view`` or ``database.schema.view``. Missing parts
                are filled from the session's ``CURRENT_DATABASE()`` and
                ``CURRENT_SCHEMA()``, because ``SHOW COLUMNS IN VIEW`` refuses
                anything less than the full path.

        Returns:
            Intermediate representation of the view, ready for code rendering.

        Raises:
            SemolinaViewNotFoundError: If the view does not exist or is not
                accessible (wraps an ADBC :class:`~adbc_driver_manager.ProgrammingError`),
                or if the name needs a database or schema the session does not have.
            SemolinaConnectionError: If the connection or authentication fails
                (wraps an ADBC ``OperationalError`` or ``InterfaceError``).
            SemolinaIntrospectionError: If the warehouse cannot describe the view (wraps
                any other ADBC :class:`~adbc_driver_manager.Error`).
            ValueError: If a quoted part of ``view_name`` contains a ``.`` or leaves a
                quote unbalanced.

        Example:
            .. code-block:: python

                from adbc_poolhouse import SnowflakeConfig

                from semolina.config import create_engine

                engine = create_engine(
                    SnowflakeConfig(account="xy12345.us-east-1", user="myuser")
                )
                view = engine.introspect("analytics.sales_view")
                print(view.class_name)
                # SalesView
        """
        with _translate_introspection_errors("Snowflake"), self.connect() as conn:
            return _introspect_snowflake(conn.cursor(), view_name)
