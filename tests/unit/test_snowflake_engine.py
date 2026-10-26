"""
Unit tests for Snowflake introspection over the ADBC-cursor seam.

Snowflake introspection runs over the Engine's ADBC pool rather than the native
Snowflake driver: ``engine.introspect(view)`` checks out a connection via
``engine.connect()`` and runs ``SHOW COLUMNS IN VIEW`` through the ADBC cursor.
A live measurement showed the ADBC cursor returns the identical 13-column
``SHOW COLUMNS`` result the existing parser expects, so the
mock here feeds those same rows through a mocked ``connect()`` / ``cursor()``
seam rather than a native-driver module stub.

The engine is built with ``create_engine(SnowflakeConfig(...))`` (D1). The mocks
are deliberately untyped (``MagicMock`` cursors and a patched ``connect()``), so
the per-rule scope-disables below keep basedpyright strict quiet on the mock
seam without a ``# type: ignore``.
"""
# Test-only mock seam: MagicMock cursors and patch.object(engine, "connect", ...)
# are untyped by construction. Scope-disable the rules the mock seam triggers
# under basedpyright strict (intentionally not a `# type: ignore`).
# pyright: reportUnknownMemberType=false

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator


def _make_snowflake_engine(**overrides: Any) -> Any:
    """
    Build a SnowflakeEngine via the create_engine factory.

    The engine owns an ADBC pool (mocked at create_pool below) plus the
    Snowflake dialect derived from the config type. Tests then patch
    ``engine.connect`` to drive introspection through a mocked ADBC cursor.
    """
    from adbc_poolhouse import SnowflakeConfig

    from semolina.config import create_engine

    params: dict[str, Any] = {
        "account": "test",
        "user": "user",
        "password": "pass",
    }
    params.update(overrides)
    # Avoid a live ADBC connect: create_pool returns a mock pool. The Engine
    # still owns it; introspection is driven through engine.connect() below.
    with patch("semolina.config.create_pool", return_value=MagicMock(name="pool")):
        return create_engine(SnowflakeConfig(**params))


def _patch_connect(engine: Any, cursor: Any) -> Any:
    """
    Patch ``engine.connect()`` to yield a connection whose cursor is ``cursor``.

    Mirrors the real ADBC checkout seam: ``with engine.connect() as conn:`` then
    ``conn.cursor()``. Both the connection and the cursor support the context
    manager protocol so the engine's ``with`` blocks work unchanged.
    """

    @contextmanager
    def _connect() -> Generator[Any]:
        conn = MagicMock(name="conn")
        conn.cursor.return_value = cursor
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        yield conn

    return patch.object(engine, "connect", side_effect=_connect)


def _show_columns_cursor(
    rows: list[tuple[Any, ...]],
    session: tuple[str | None, str | None] = ("SESSION_DB", "SESSION_SCHEMA"),
    describe_rows: list[tuple[Any, ...]] | None = None,
    tag_rows: list[tuple[str, str, str]] | None = None,
) -> MagicMock:
    """
    Build a mock ADBC cursor returning SHOW COLUMNS IN VIEW rows.

    The ADBC cursor exposes ``.description`` (column_name, kind, data_type,
    comment) and ``.fetchall()`` returning the same row shape the existing
    Snowflake parser consumes. ``.fetchone()`` answers the
    ``SELECT CURRENT_DATABASE(), CURRENT_SCHEMA()`` that qualifies a name given
    with fewer than three parts. A ``DESCRIBE SEMANTIC VIEW`` switches the cursor to
    that statement's five columns and ``describe_rows`` (none by default), and the member
    tags query returns ``tag_rows`` (none by default).
    """
    cursor = MagicMock(name="cursor")

    def execute(sql: str) -> None:
        if sql.startswith("DESCRIBE SEMANTIC VIEW"):
            cursor.description = [
                ("object_kind",),
                ("object_name",),
                ("parent_entity",),
                ("property",),
                ("property_value",),
            ]
            cursor.fetchall.return_value = describe_rows or []
        elif sql.startswith("SELECT MEMBER"):
            cursor.description = [("member",), ("tag_name",), ("tag_value",)]
            cursor.fetchall.return_value = tag_rows or []
        else:
            cursor.description = [("column_name",), ("kind",), ("data_type",), ("comment",)]
            cursor.fetchall.return_value = rows

    cursor.execute.side_effect = execute
    cursor.fetchone.return_value = session
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    return cursor


def _show_sql(cursor: MagicMock) -> str:
    """Return the one SHOW COLUMNS statement the cursor ran."""
    (statement,) = [
        c[0][0] for c in cursor.execute.call_args_list if c[0][0].startswith("SHOW COLUMNS")
    ]
    return statement


class TestSnowflakeEngineIntrospect:
    """
    Test SnowflakeEngine.introspect() driven through the ADBC-cursor seam.

    Verifies that introspect() executes SHOW COLUMNS IN VIEW over the Engine's
    pooled ADBC connection, parses the 13-column result into IntrospectedView,
    handles kind casing, maps data_type JSON, and qualifies the view name from
    the session's current database and schema.
    """

    def test_introspect_basic_metric_dimension_fact(self) -> None:
        """Should parse one metric, one dimension, one fact into IntrospectedView."""
        from semolina.codegen.introspector import IntrospectedField, IntrospectedView

        cursor = _show_columns_cursor(
            [
                ("revenue", "METRIC", json.dumps({"type": "FIXED", "scale": 0}), ""),
                ("country", "DIMENSION", json.dumps({"type": "TEXT"}), ""),
                ("date_key", "FACT", json.dumps({"type": "DATE"}), ""),
            ]
        )
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            result = engine.introspect("sales_view")

        assert isinstance(result, IntrospectedView)
        assert result.view_name == "sales_view"
        assert result.class_name == "SalesView"
        assert len(result.fields) == 3

        revenue = result.fields[0]
        assert isinstance(revenue, IntrospectedField)
        assert revenue.name == "revenue"
        assert revenue.field_type == "metric"
        assert revenue.data_type == "decimal.Decimal"

        country = result.fields[1]
        assert country.name == "country"
        assert country.field_type == "dimension"
        assert country.data_type == "str"

        date_key = result.fields[2]
        assert date_key.name == "date_key"
        assert date_key.field_type == "fact"
        assert date_key.data_type == "datetime.date"

    def test_introspect_kind_lowercase_conversion(self) -> None:
        """Should lowercase uppercase METRIC/DIMENSION kind values."""
        cursor = _show_columns_cursor(
            [
                ("total_sales", "METRIC", json.dumps({"type": "FIXED", "scale": 0}), ""),
                ("region", "DIMENSION", json.dumps({"type": "TEXT"}), ""),
            ]
        )
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            result = engine.introspect("report_view")

        assert result.fields[0].field_type == "metric"
        assert result.fields[1].field_type == "dimension"

    def test_introspect_non_identifier_names_become_valid_field_names(self) -> None:
        """A column that is no Python identifier gets a valid name and keeps its column."""
        cursor = _show_columns_cursor(
            [
                ("what?", "DIMENSION", json.dumps({"type": "TEXT"}), ""),
                ("ORDER DATE", "DIMENSION", json.dumps({"type": "DATE"}), ""),
                ("CLASS", "DIMENSION", json.dumps({"type": "TEXT"}), ""),
                ("1ST", "DIMENSION", json.dumps({"type": "TEXT"}), ""),
                ("?", "METRIC", json.dumps({"type": "FIXED", "scale": 0}), ""),
            ]
        )
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            result = engine.introspect("report_view")

        assert [(f.name, f.source_name) for f in result.fields] == [
            ("what_", "what?"),
            ("order_date", "ORDER DATE"),
            ("class_", "CLASS"),
            ("_1st", "1ST"),
            ("column", "?"),
        ]

    def test_introspect_fixed_scale_zero_maps_to_decimal(self) -> None:
        """
        Should map FIXED with scale=0 to 'decimal.Decimal'.

        The Decimal policy covers the whole FIXED family: the driver returns Decimal128 for
        every FIXED column, scale 0 included.
        """
        cursor = _show_columns_cursor(
            [("count", "METRIC", json.dumps({"type": "FIXED", "scale": 0}), "")]
        )
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            result = engine.introspect("count_view")

        assert result.fields[0].data_type == "decimal.Decimal"

    def test_introspect_fixed_nonzero_scale_maps_to_decimal(self) -> None:
        """Should map FIXED with scale=2 to 'decimal.Decimal'."""
        cursor = _show_columns_cursor(
            [("revenue", "METRIC", json.dumps({"type": "FIXED", "scale": 2}), "")]
        )
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            result = engine.introspect("revenue_view")

        assert result.fields[0].data_type == "decimal.Decimal"

    def test_introspect_geography_produces_todo(self) -> None:
        """Should produce data_type starting with 'TODO:' for GEOGRAPHY type."""
        cursor = _show_columns_cursor(
            [("location", "DIMENSION", json.dumps({"type": "GEOGRAPHY"}), "")]
        )
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            result = engine.introspect("geo_view")

        assert result.fields[0].data_type is not None
        assert result.fields[0].data_type.startswith("TODO:")

    def test_introspect_populates_description_from_comment(self) -> None:
        """Should populate field description from 'comment' column when present."""
        cursor = _show_columns_cursor(
            [("revenue", "METRIC", json.dumps({"type": "FIXED", "scale": 0}), "Total revenue")]
        )
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            result = engine.introspect("sales_view")

        assert result.fields[0].description == "Total revenue"

    def test_introspect_reads_synonyms_and_the_view_comment(self) -> None:
        """
        ``DESCRIBE SEMANTIC VIEW`` adds what ``SHOW COLUMNS`` lacks: synonyms, view comment.

        Members are matched by their stored name, so a member that exists on two logical
        tables, or one whose Python name differs from its column, still gets its own.
        """
        cursor = _show_columns_cursor(
            [
                ("REVENUE", "METRIC", json.dumps({"type": "FIXED", "scale": 0}), "Gross"),
                ("country", "DIMENSION", json.dumps({"type": "TEXT"}), ""),
                ("REGION", "DIMENSION", json.dumps({"type": "TEXT"}), ""),
            ],
            describe_rows=[
                (None, None, None, "COMMENT", "Sales by country"),
                ("TABLE", "SALES_DATA", None, "BASE_TABLE_NAME", "SALES_DATA"),
                ("METRIC", "REVENUE", "SALES_DATA", "COMMENT", "Gross"),
                ("METRIC", "REVENUE", "SALES_DATA", "SYNONYMS", '["sales","turnover"]'),
                ("DIMENSION", "country", "SALES_DATA", "SYNONYMS", '["nation"]'),
            ],
        )
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            view = engine.introspect("sales_view")

        assert view.description == "Sales by country"
        synonyms = {f.name: f.synonyms for f in view.fields}
        assert synonyms == {"revenue": ("sales", "turnover"), "country": ("nation",), "region": ()}
        assert view.fields[0].description == "Gross"

    _TAGGED_DESCRIBE: ClassVar[list[tuple[Any, ...]]] = [
        ("TABLE", "SALES_DATA", None, "BASE_TABLE_NAME", "SALES_DATA"),
        ("METRIC", "REVENUE", "SALES_DATA", "DATA_TYPE", "NUMBER(38,0)"),
        ("DIMENSION", "COUNTRY", "SALES_DATA", "DATA_TYPE", "VARCHAR"),
        ("METRIC", "MARGIN", None, "DATA_TYPE", "NUMBER(38,0)"),
    ]
    _TAGGED_SHOW: ClassVar[list[tuple[Any, ...]]] = [
        ("REVENUE", "METRIC", json.dumps({"type": "FIXED", "scale": 0}), ""),
        ("COUNTRY", "DIMENSION", json.dumps({"type": "TEXT"}), ""),
        ("MARGIN", "METRIC", json.dumps({"type": "FIXED", "scale": 0}), ""),
    ]

    def test_introspect_reads_member_tags_into_label_and_semantic_format(self) -> None:
        """``S_LAYER_KIND`` and ``S_LAYER_SPEC`` make the block; ``S_LAYER_LABEL`` the label."""
        from s_layer_semantic_formats import from_compact_json

        spec = '{"value":{"currency":"USD"}}'
        cursor = _show_columns_cursor(
            self._TAGGED_SHOW,
            describe_rows=self._TAGGED_DESCRIBE,
            tag_rows=[
                ("REVENUE", "S_LAYER_KIND", "currency"),
                ("REVENUE", "S_LAYER_SPEC", spec),
                ("REVENUE", "S_LAYER_LABEL", "Revenue"),
                ("COUNTRY", "S_LAYER_LABEL", "Country"),
            ],
        )
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            view = engine.introspect("sales_view")

        fields = {f.name: f for f in view.fields}
        assert fields["revenue"].semantic_format == from_compact_json("currency", spec)
        assert fields["revenue"].label == "Revenue"
        assert fields["country"].label == "Country"
        assert fields["country"].semantic_format is None
        assert fields["margin"].label is None

    def test_introspect_reads_tags_one_member_at_a_time(self) -> None:
        """
        ``TAG_REFERENCES`` takes constants, so the query is one call per member.

        A member names its logical table; a derived metric has none and is named on the
        view. Only the member's own tags count: ``LEVEL = DOMAIN`` drops the ones it
        inherits from the view or its table.
        """
        cursor = _show_columns_cursor(self._TAGGED_SHOW, describe_rows=self._TAGGED_DESCRIBE)
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            engine.introspect("sales_view")

        (sql,) = [
            c[0][0] for c in cursor.execute.call_args_list if c[0][0].startswith("SELECT MEMBER")
        ]
        view = '"SESSION_DB"."SESSION_SCHEMA"."SALES_VIEW"'
        call = (
            "SELECT '{member}' AS MEMBER, TAG_NAME, TAG_VALUE, LEVEL, DOMAIN"
            ' FROM TABLE("SESSION_DB".INFORMATION_SCHEMA.TAG_REFERENCES('
            "'{path}', '{domain}'))"
        )
        assert sql == (
            "SELECT MEMBER, TAG_NAME, TAG_VALUE FROM ("
            + " UNION ALL ".join(
                [
                    call.format(
                        member="REVENUE",
                        path=f'{view}!"SALES_DATA"."REVENUE"',
                        domain="SEMANTIC METRIC",
                    ),
                    call.format(
                        member="COUNTRY",
                        path=f'{view}!"SALES_DATA"."COUNTRY"',
                        domain="SEMANTIC DIMENSION",
                    ),
                    call.format(member="MARGIN", path=f'{view}!"MARGIN"', domain="SEMANTIC METRIC"),
                ]
            )
            + ") WHERE LEVEL = DOMAIN"
            " AND TAG_NAME IN ('S_LAYER_KIND', 'S_LAYER_LABEL', 'S_LAYER_SPEC')"
        )

    def test_introspect_escapes_a_quote_in_a_member_name(self) -> None:
        """A member name is catalogue data inside a string literal, so its quote is doubled."""
        cursor = _show_columns_cursor(
            [("it's", "DIMENSION", json.dumps({"type": "TEXT"}), "")],
            describe_rows=[("DIMENSION", "it's", "T", "DATA_TYPE", "VARCHAR")],
        )
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            engine.introspect("sales_view")

        (sql,) = [
            c[0][0] for c in cursor.execute.call_args_list if c[0][0].startswith("SELECT MEMBER")
        ]
        assert "SELECT 'it''s' AS MEMBER" in sql
        assert '!"T"."it\'\'s"\'' in sql

    def test_introspect_runs_no_tag_query_for_a_view_without_members(self) -> None:
        cursor = _show_columns_cursor([])
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            engine.introspect("sales_view")

        assert not [c for c in cursor.execute.call_args_list if c[0][0].startswith("SELECT MEMBER")]

    def test_introspect_refuses_tags_that_make_no_valid_block(self) -> None:
        """The tags say what the values mean, so an unreadable one is an error."""
        cursor = _show_columns_cursor(
            self._TAGGED_SHOW,
            describe_rows=self._TAGGED_DESCRIBE,
            tag_rows=[("COUNTRY", "S_LAYER_KIND", "currency")],  # not a dimension kind
        )
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor), pytest.raises(ValueError, match="'COUNTRY'"):
            engine.introspect("sales_view")

    def test_introspect_refuses_synonyms_that_are_not_a_json_list(self) -> None:
        """A shape this reader does not know is an error, not a guess."""
        cursor = _show_columns_cursor(
            [("COUNTRY", "DIMENSION", json.dumps({"type": "TEXT"}), "")],
            describe_rows=[("DIMENSION", "COUNTRY", "T", "SYNONYMS", "nation, land")],
        )
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor), pytest.raises(Exception, match="SYNONYMS"):
            engine.introspect("sales_view")

    def test_introspect_executes_correct_sql(self) -> None:
        """Should execute SHOW COLUMNS IN VIEW on the quoted, folded name via the cursor."""
        cursor = _show_columns_cursor([])
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            engine.introspect("my_sales_view")

        executed_sql = _show_sql(cursor)
        assert "SHOW COLUMNS IN VIEW" in executed_sql
        assert executed_sql.endswith('."MY_SALES_VIEW"')

    def test_introspect_sql_uses_in_view_not_semantic_view(self) -> None:
        """
        Should use SHOW COLUMNS IN VIEW, not SHOW COLUMNS IN SEMANTIC VIEW.

        'SHOW COLUMNS IN SEMANTIC VIEW' is invalid Snowflake SQL. The correct
        syntax is 'SHOW COLUMNS IN VIEW', which works for standard, materialized,
        and semantic views alike.
        """
        cursor = _show_columns_cursor([])
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            engine.introspect("my_sales_view")

        executed_sql = _show_sql(cursor)
        assert "SHOW COLUMNS IN VIEW" in executed_sql
        assert "IN SEMANTIC VIEW" not in executed_sql
        assert executed_sql.endswith('."MY_SALES_VIEW"')

    def test_introspect_pascal_case_class_name_simple(self) -> None:
        """Should convert snake_case view name to PascalCase class name."""
        cursor = _show_columns_cursor([])
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            result = engine.introspect("sales_revenue_view")

        assert result.class_name == "SalesRevenueView"

    def test_introspect_pascal_case_schema_qualified_name(self) -> None:
        """Should use last segment after '.' for class name derivation."""
        cursor = _show_columns_cursor([])
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            result = engine.introspect("my_db.my_schema.sales_view")

        assert result.class_name == "SalesView"
        assert result.view_name == "my_db.my_schema.sales_view"

    def test_introspect_qualifies_a_bare_name_from_the_session(self) -> None:
        """
        A bare view name takes the session's current database and schema.

        SHOW COLUMNS IN VIEW refuses anything short of database.schema.view. Measured live
        in 53-04: a bare name prefixed with the configured database alone
        (``MY_DB.sem_orders``) is read as schema.view and refused with ``Must specify the
        full search path starting from database``. The session answers both parts, and
        its answer replays from a cassette where a config value would not.
        """
        cursor = _show_columns_cursor([], session=("SESSION_DB", "SESSION_SCHEMA"))
        engine = _make_snowflake_engine(database="MY_DB")
        with _patch_connect(engine, cursor):
            engine.introspect("sem_orders")

        executed = [c[0][0] for c in cursor.execute.call_args_list]
        assert executed == [
            "SELECT CURRENT_DATABASE(), CURRENT_SCHEMA()",
            'SHOW COLUMNS IN VIEW "SESSION_DB"."SESSION_SCHEMA"."SEM_ORDERS"',
            'DESCRIBE SEMANTIC VIEW "SESSION_DB"."SESSION_SCHEMA"."SEM_ORDERS"',
        ]

    def test_introspect_qualifies_a_two_part_name_with_the_session_database(self) -> None:
        """A schema.view name takes only the database from the session."""
        cursor = _show_columns_cursor([], session=("SESSION_DB", "SESSION_SCHEMA"))
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            engine.introspect("dev.sem_orders")

        executed_sql = _show_sql(cursor)
        assert executed_sql == 'SHOW COLUMNS IN VIEW "SESSION_DB"."DEV"."SEM_ORDERS"'

    def test_introspect_quotes_the_session_parts_it_fills_in(self) -> None:
        """
        The session's database and schema are quoted, since they are the stored names.

        ``CURRENT_SCHEMA()`` keeps the stored case, so a schema created quoted
        (``"analytics"``, or ``"analytics-prod"``) joined in bare is folded to upper case or
        fails to parse, and the session's own view is not found.
        """
        cursor = _show_columns_cursor([], session=("my_db", "analytics-prod"))
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            engine.introspect("sem_orders")

        executed_sql = _show_sql(cursor)
        assert executed_sql == 'SHOW COLUMNS IN VIEW "my_db"."analytics-prod"."SEM_ORDERS"'

    def test_introspect_three_part_name_used_as_is(self) -> None:
        """A fully-qualified three-part name needs no session query; it is only quoted."""
        cursor = _show_columns_cursor([])
        engine = _make_snowflake_engine(database="MY_DB")
        with _patch_connect(engine, cursor):
            engine.introspect("OTHER_DB.dev.sem_orders")

        executed = [c[0][0] for c in cursor.execute.call_args_list]
        assert executed == [
            'SHOW COLUMNS IN VIEW "OTHER_DB"."DEV"."SEM_ORDERS"',
            'DESCRIBE SEMANTIC VIEW "OTHER_DB"."DEV"."SEM_ORDERS"',
        ]

    def test_introspect_keeps_the_default_schema_shorthand(self) -> None:
        """
        Snowflake's ``db..view`` (the default schema) stays a three-part name.

        Quoted, the empty middle part became ``""``, an invalid identifier, and the
        ``ProgrammingError`` read as "view not found".
        """
        cursor = _show_columns_cursor([])
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            engine.introspect("my_db..sem_orders")

        executed = [c[0][0] for c in cursor.execute.call_args_list]
        assert executed == [
            'SHOW COLUMNS IN VIEW "MY_DB".."SEM_ORDERS"',
            'DESCRIBE SEMANTIC VIEW "MY_DB".."SEM_ORDERS"',
        ]

    def test_introspect_quotes_a_name_that_needs_it(self) -> None:
        """
        Each part is quoted, so a space parses and a pre-quoted part keeps its case (FILT-05).

        The session's own names are quoted as the session returned them: they are already
        the stored names, and folding a case-sensitive ``my_db`` would name another database.
        """
        cursor = _show_columns_cursor([], session=("my_db", "SESSION_SCHEMA"))
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            engine.introspect('dev."Order Book"')

        executed_sql = _show_sql(cursor)
        assert executed_sql == 'SHOW COLUMNS IN VIEW "my_db"."DEV"."Order Book"'

    @pytest.mark.parametrize("session", [(None, None), ("SESSION_DB", None)])
    def test_introspect_without_a_session_schema_says_to_qualify(
        self, session: tuple[str | None, str | None]
    ) -> None:
        """No current schema to fill in: the error names the view and says to qualify it."""
        from semolina.engines.base import SemolinaViewNotFoundError

        cursor = _show_columns_cursor([], session=session)
        engine = _make_snowflake_engine()
        with (
            _patch_connect(engine, cursor),
            pytest.raises(SemolinaViewNotFoundError, match=r"sem_orders.*database\.schema"),
        ):
            engine.introspect("sem_orders")

    def test_introspect_uppercase_column_lowercased_no_source_name(self) -> None:
        """
        Standard UPPERCASE column (ORDER_ID) -> name='order_id', source_name=None.

        For standard Snowflake UPPERCASE columns the Python field name is the
        lowercased version; normalize_identifier round-trips it, so source_name
        is not needed.
        """
        cursor = _show_columns_cursor(
            [("ORDER_ID", "DIMENSION", json.dumps({"type": "FIXED", "scale": 0}), "")]
        )
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            result = engine.introspect("orders_view")

        field = result.fields[0]
        assert field.name == "order_id"
        assert field.source_name is None

    def test_introspect_quoted_lowercase_column_gets_source_name(self) -> None:
        """
        Quoted-lowercase column ('order_id') -> name='order_id', source_name='order_id'.

        A column created with a quoted identifier is stored lowercase, so
        upper() != original and source_name preserves the warehouse column name.
        """
        cursor = _show_columns_cursor(
            [("order_id", "DIMENSION", json.dumps({"type": "FIXED", "scale": 0}), "")]
        )
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor):
            result = engine.introspect("orders_view")

        field = result.fields[0]
        assert field.name == "order_id"
        assert field.source_name == "order_id"


class TestSnowflakeEngineIntrospectErrors:
    """
    Test introspect() error translation over the ADBC cursor.

    Over ADBC, warehouse errors surface as ``adbc_driver_manager.Error``
    subclasses (PEP-249) rather than the native-driver error classes. The
    introspector must still raise SemolinaViewNotFoundError /
    SemolinaConnectionError.
    """

    def test_introspect_missing_view_raises_view_not_found(self) -> None:
        """A warehouse error for an unknown view -> SemolinaViewNotFoundError."""
        pytest.importorskip("adbc_driver_manager")
        from adbc_driver_manager import (
            AdbcStatusCode,
            ProgrammingError,
        )

        from semolina.engines.base import SemolinaViewNotFoundError

        cursor = _show_columns_cursor([])
        cursor.execute.side_effect = ProgrammingError(
            "Semantic view 'nonexistent_view' does not exist",
            status_code=AdbcStatusCode.NOT_FOUND,
        )
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor), pytest.raises(SemolinaViewNotFoundError):
            engine.introspect("nonexistent_view")

    @pytest.mark.parametrize(
        ("error_class", "expected"),
        [
            ("InterfaceError", "SemolinaConnectionError"),
            ("DatabaseError", "SemolinaIntrospectionError"),
            ("DataError", "SemolinaIntrospectionError"),
            ("OperationalError", "SemolinaConnectionError"),
            ("IntegrityError", "SemolinaIntrospectionError"),
            ("InternalError", "SemolinaIntrospectionError"),
            ("NotSupportedError", "SemolinaIntrospectionError"),
        ],
    )
    def test_every_other_adbc_error_is_translated(self, error_class: str, expected: str) -> None:
        """
        No ADBC error escapes ``introspect()`` untranslated (FILT-08).

        Only ``ProgrammingError`` means the view is missing. A connection-level error is a
        connection error; any other means the warehouse could not describe the view (COR-2).
        Before phase 54 only
        ``OperationalError`` was translated as well, so the other five reached the caller as
        raw driver errors.
        """
        adbc = pytest.importorskip("adbc_driver_manager")
        import semolina.exceptions

        expected_error: type[Exception] = getattr(semolina.exceptions, expected)
        cls = getattr(adbc, error_class)
        # NotSupportedError takes no status code: the driver manager fixes it at NOT_IMPLEMENTED.
        error = (
            cls("boom")
            if cls is adbc.NotSupportedError
            else cls("boom", status_code=adbc.AdbcStatusCode.UNKNOWN)
        )
        cursor = _show_columns_cursor([])
        cursor.execute.side_effect = error
        engine = _make_snowflake_engine()
        with _patch_connect(engine, cursor), pytest.raises(expected_error) as raised:
            engine.introspect("DB.S.SALES_VIEW")
        assert raised.value.__cause__ is error
