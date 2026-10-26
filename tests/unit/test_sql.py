"""
Tests for SQL generation with SQLDialect and SQLBuilder classes, dialect by dialect.

``test_query.py`` pins the builder's clauses as exact Snowflake statements. This module pins
what differs by dialect — quoting, metric wrapping, name folding, bound versus inlined
parameters, literal rendering — and the WHERE compiler, whose conditions are read off a real
query built through ``.where()`` rather than off the private compiler method.

Tests cover:
- Query.to_sql() generates valid SQL
- SnowflakeDialect uses double quotes and AGG() wrapping
- DatabricksDialect uses backticks and MEASURE() wrapping
- GROUP BY ALL for automatic dimension derivation
- Proper identifier quoting and escaping
- SQLDialect.placeholder property (qmark "?" across ADBC backends)
- WHERE clause compilation, through ``.where()`` and the dialect's builder
- build_select_with_params parameterized output
- render_inline for display/debugging
"""

import datetime
import enum
import re
import uuid
from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import Any

import pytest
from models import Sales

from semolina.dialect import resolve_dialect
from semolina.engines.sql import (
    DatabricksDialect,
    DuckDBDialect,
    DuckDBSQLBuilder,
    SnowflakeDialect,
    SQLBuilder,
    SQLDialect,
)
from semolina.fields import Dimension, Fact, Metric, NullsOrdering
from semolina.filters import (
    And,
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
    Or,
    StartsWith,
)
from semolina.models import SemanticView


class Filterable(SemanticView, view="sales_view"):
    """
    ``revenue`` as a dimension, so a compiler test's condition on it lands in ``WHERE``.

    The WHERE-compiler tests filter on ``revenue`` as a plain numeric column. On ``Sales`` it
    is a metric, and a metric filter compiles to ``HAVING`` (D4), which
    ``TestMetricFiltersCompileToHaving`` covers.
    """

    total = Metric()
    revenue = Dimension()


class InliningDatabricksDialect(DatabricksDialect):
    """
    Databricks's quoting and literals, with bind parameters switched off.

    No shipped dialect opts out of binding since Foundry driver v0.2.0 gave Databricks bind
    parameters, but ``supports_parameterized_queries = False`` is still the builder's one
    capability switch for a driver that cannot bind. This subclass keeps that inline path
    under test, with the dialect whose string literals are the hardest to get right
    (Spark SQL's backslash escapes).
    """

    supports_parameterized_queries = False


def where_clause(
    predicate: object, dialect: str | SQLDialect = "snowflake"
) -> tuple[str, list[object]]:
    """
    Compile one predicate the way execution does, and return its WHERE condition and params.

    The predicate is attached to a real query with ``.where()`` and built by the dialect's own
    builder, exactly as ``Engine.execute`` builds it, so the condition asserted is the one a
    warehouse would receive. The query is on ``Filterable``, where every name the tests
    filter on is a dimension.

    Args:
        predicate: What to pass to ``.where()``.
        dialect: A dialect name, or a dialect instance.

    Returns:
        The WHERE condition without the ``WHERE`` keyword, and the bound parameters.
    """
    query = Filterable.query().metrics(Filterable.total).where(predicate)  # pyright: ignore[reportArgumentType]
    resolved = resolve_dialect(dialect) if isinstance(dialect, str) else dialect
    sql, params = resolved.create_builder().build_select_with_params(query)
    where = [line for line in sql.split("\n") if line.startswith("WHERE ")]
    assert len(where) == 1, sql
    return where[0].removeprefix("WHERE "), params


class SalesOnFile(SemanticView, view="sales_view"):
    """The ``sales_view`` of the ``duckdb_file_backed_db`` fixture."""

    revenue = Metric()
    country = Dimension()


DUCKDB_SALES = (
    'SELECT "revenue" AS "revenue", "country" AS "country"\n'
    "FROM semantic_view('sales_view', dimensions := ['country'], metrics := ['revenue'])"
)
"""The DuckDB statement head for revenue by country; the clauses under test follow it."""


class TestSnowflakeDialect:
    """Test SnowflakeDialect identifier quoting and metric wrapping."""

    def test_quote_identifier_simple(self):
        """Should quote simple identifiers with double quotes."""
        dialect = SnowflakeDialect()
        assert dialect.quote_identifier("simple_name") == '"simple_name"'

    def test_quote_identifier_preserves_case(self):
        """Should preserve case exactly in quoted identifiers."""
        dialect = SnowflakeDialect()
        assert dialect.quote_identifier("REVENUE") == '"REVENUE"'
        assert dialect.quote_identifier("Revenue") == '"Revenue"'

    def test_quote_identifier_escapes_quotes(self):
        """Should escape internal double quotes by doubling them."""
        dialect = SnowflakeDialect()
        assert dialect.quote_identifier('name"with"quotes') == '"name""with""quotes"'
        assert dialect.quote_identifier('single"quote') == '"single""quote"'

    def test_quote_identifier_multiple_quotes(self):
        """Should handle multiple consecutive quotes."""
        dialect = SnowflakeDialect()
        assert dialect.quote_identifier('a""b') == '"a""""b"'

    def test_wrap_metric_simple(self):
        """Should wrap simple metric with AGG()."""
        dialect = SnowflakeDialect()
        assert dialect.wrap_metric("revenue") == 'AGG("revenue")'

    def test_wrap_metric_with_special_chars(self):
        """Should wrap metric with special characters, escaping as needed."""
        dialect = SnowflakeDialect()
        assert dialect.wrap_metric('revenue"2024') == 'AGG("revenue""2024")'

    def test_wrap_metric_uppercase(self):
        """Should preserve case in wrapped metrics."""
        dialect = SnowflakeDialect()
        assert dialect.wrap_metric("REVENUE") == 'AGG("REVENUE")'


class TestDatabricksDialect:
    """Test DatabricksDialect identifier quoting and metric wrapping."""

    def test_quote_identifier_simple(self):
        """Should quote simple identifiers with backticks."""
        dialect = DatabricksDialect()
        assert dialect.quote_identifier("simple_name") == "`simple_name`"

    def test_quote_identifier_preserves_case(self):
        """Should preserve case exactly in quoted identifiers."""
        dialect = DatabricksDialect()
        assert dialect.quote_identifier("REVENUE") == "`REVENUE`"
        assert dialect.quote_identifier("Revenue") == "`Revenue`"

    def test_quote_identifier_escapes_backticks(self):
        """Should escape internal backticks by doubling them."""
        dialect = DatabricksDialect()
        assert dialect.quote_identifier("name`with`ticks") == "`name``with``ticks`"
        assert dialect.quote_identifier("single`tick") == "`single``tick`"

    def test_quote_identifier_multiple_backticks(self):
        """Should handle multiple consecutive backticks."""
        dialect = DatabricksDialect()
        assert dialect.quote_identifier("a``b") == "`a````b`"

    def test_wrap_metric_simple(self):
        """Should wrap simple metric with MEASURE()."""
        dialect = DatabricksDialect()
        assert dialect.wrap_metric("revenue") == "MEASURE(`revenue`)"

    def test_wrap_metric_with_special_chars(self):
        """Should wrap metric with special characters, escaping as needed."""
        dialect = DatabricksDialect()
        assert dialect.wrap_metric("revenue`2024") == "MEASURE(`revenue``2024`)"

    def test_wrap_metric_uppercase(self):
        """Should preserve case in wrapped metrics."""
        dialect = DatabricksDialect()
        assert dialect.wrap_metric("REVENUE") == "MEASURE(`REVENUE`)"


class TestNoDialectNamesAnUnaliasedResultColumn:
    """
    ``metric_result_column_name`` is gone with the recordings it existed to read (D2-5).

    It spelled the column a warehouse returned for a metric selected *without* an alias:
    ``AGG("GROSS REVENUE")`` on Snowflake, ``measure(revenue)`` on Databricks. Since phase 53
    every builder aliases each column to its field name, so the method only ever matched
    pre-53 cassettes, which 53-04 replaced. Left in place it would keep offering those
    spellings as binding candidates for a column no query returns.
    """

    @pytest.mark.parametrize(
        "dialect",
        [
            pytest.param(SnowflakeDialect(), id="snowflake"),
            pytest.param(DatabricksDialect(), id="databricks"),
            pytest.param(DuckDBDialect(), id="duckdb"),
        ],
    )
    def test_the_dialect_has_no_metric_result_column_name(self, dialect: SQLDialect) -> None:
        """No dialect still answers the pre-53 result-column spelling."""
        assert not hasattr(dialect, "metric_result_column_name")


class TestSupportsParameterizedQueries:
    """Capability flag: True on every shipped dialect."""

    def test_snowflake_supports_parameterized_queries(self):
        """SnowflakeDialect keeps the parameterized (?) path."""
        assert SnowflakeDialect().supports_parameterized_queries is True

    def test_duckdb_supports_parameterized_queries(self):
        """DuckDBDialect keeps the parameterized (?) path."""
        assert DuckDBDialect().supports_parameterized_queries is True

    def test_databricks_supports_parameterized_queries(self):
        """DatabricksDialect binds too: Foundry driver v0.2.0 added bind parameters."""
        assert DatabricksDialect().supports_parameterized_queries is True


class TestRenderLiteralStandardSql:
    """DBX-01c: standard-SQL render_literal (base ABC via SnowflakeDialect)."""

    def test_string_doubles_single_quote(self):
        """Standard SQL escapes a single quote by doubling it."""
        assert SnowflakeDialect().render_literal("O'Reilly") == "'O''Reilly'"

    def test_plain_string(self):
        """A plain string is wrapped in single quotes."""
        assert SnowflakeDialect().render_literal("US") == "'US'"

    def test_none_renders_null(self):
        """None renders as the SQL NULL keyword (unquoted)."""
        assert SnowflakeDialect().render_literal(None) == "NULL"

    def test_bool_renders_uppercase(self):
        """Booleans render as uppercase TRUE/FALSE for standard SQL."""
        assert SnowflakeDialect().render_literal(True) == "TRUE"
        assert SnowflakeDialect().render_literal(False) == "FALSE"

    def test_int_renders_unquoted(self):
        """Integers render unquoted."""
        assert SnowflakeDialect().render_literal(5) == "5"

    def test_unsupported_type_raises_not_implemented(self):
        """An unsupported literal type fails loudly rather than mis-escaping."""
        with pytest.raises(NotImplementedError):
            SnowflakeDialect().render_literal({1, 2})

    def test_non_finite_float_raises(self):
        """inf/-inf/nan are not SQL numeric literals -- fail loudly."""
        for value in (float("inf"), float("-inf"), float("nan")):
            with pytest.raises(ValueError):
                SnowflakeDialect().render_literal(value)

    def test_date_literal(self):
        """A date renders as a typed DATE literal in ISO-8601 form."""
        assert SnowflakeDialect().render_literal(datetime.date(2024, 1, 31)) == "DATE '2024-01-31'"

    def test_naive_datetime_literal(self):
        """A naive datetime renders as TIMESTAMP and keeps its time of day."""
        value = datetime.datetime(2024, 1, 31, 10, 5)
        assert SnowflakeDialect().render_literal(value) == "TIMESTAMP '2024-01-31T10:05:00'"

    def test_aware_datetime_normalises_to_utc_z(self):
        """
        The standard rendering converts an aware datetime to UTC and suffixes it with Z.

        Every shipped dialect now overrides this case (Snowflake casts to ``TIMESTAMP_TZ``,
        DuckDB writes ``TIMESTAMPTZ``), so the base implementation is called directly.
        """
        tz = datetime.timezone(datetime.timedelta(hours=2))
        value = datetime.datetime(2024, 1, 31, 10, 5, tzinfo=tz)
        rendered = SQLDialect.render_literal(DuckDBDialect(), value)
        assert rendered == "TIMESTAMP '2024-01-31T08:05:00Z'"
        assert DuckDBDialect().render_literal(value) == "TIMESTAMPTZ '2024-01-31T08:05:00Z'"

    def test_snowflake_aware_datetime_keeps_its_instant(self):
        """
        Snowflake reads a bare TIMESTAMP literal as NTZ, which drops the Z.

        So an aware value is cast to TIMESTAMP_TZ, and a preview pasted into a session in any
        time zone compares against the same instant a bound execution does.
        """
        tz = datetime.timezone(datetime.timedelta(hours=2))
        value = datetime.datetime(2024, 1, 31, 10, 5, tzinfo=tz)
        assert SnowflakeDialect().render_literal(value) == "'2024-01-31T08:05:00Z'::TIMESTAMP_TZ"

    def test_snowflake_naive_datetime_stays_a_plain_timestamp(self):
        """A naive datetime has no instant to keep, so it stays TIMESTAMP '...'."""
        value = datetime.datetime(2024, 1, 31, 10, 5)
        assert SnowflakeDialect().render_literal(value) == "TIMESTAMP '2024-01-31T10:05:00'"

    def test_datetime_microseconds_survive(self):
        """Sub-second precision reaches the literal unrounded."""
        value = datetime.datetime(2024, 1, 31, 10, 5, 3, 123456)
        assert ".123456" in SnowflakeDialect().render_literal(value)

    def test_decimal_literal_is_bare_fixed_point(self):
        """A Decimal renders as bare fixed-point digits, unquoted and uncast."""
        assert SnowflakeDialect().render_literal(Decimal("10.50")) == "10.50"

    def test_decimal_exponent_form_stays_decimal(self):
        """An exponent-form Decimal renders fixed-point, never as 1E+2."""
        assert SnowflakeDialect().render_literal(Decimal("1E+2")) == "100"

    def test_non_finite_decimal_raises(self):
        """NaN/Infinity Decimals have no SQL literal form -- fail loudly."""
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with pytest.raises(ValueError):
                SnowflakeDialect().render_literal(value)

    def test_date_literal_has_no_unescaped_quote(self):
        """A DATE literal carries exactly its two delimiting quotes."""
        rendered = SnowflakeDialect().render_literal(datetime.date(2024, 1, 31))
        assert rendered.count("'") == 2

    def test_timestamp_literal_has_no_unescaped_quote(self):
        """A TIMESTAMP literal carries exactly its two delimiting quotes."""
        tz = datetime.timezone(datetime.timedelta(hours=-5))
        value = datetime.datetime(2024, 1, 31, 10, 5, tzinfo=tz)
        rendered = SnowflakeDialect().render_literal(value)
        assert rendered.count("'") == 2

    def test_decimal_literal_is_digits_only(self):
        """A Decimal literal is digits, an optional sign and at most one point."""
        rendered = SnowflakeDialect().render_literal(Decimal("-1234.5678"))
        assert re.fullmatch(r"-?\d+(\.\d+)?", rendered) is not None

    def test_time_literal(self):
        """A time of day renders as a typed TIME literal, sub-seconds included."""
        assert SnowflakeDialect().render_literal(datetime.time(9, 30)) == "TIME '09:30:00'"
        assert (
            SnowflakeDialect().render_literal(datetime.time(9, 30, 5, 250))
            == "TIME '09:30:05.000250'"
        )

    def test_aware_time_raises(self):
        """A time with a UTC offset has no portable TIME literal -- fail loudly."""
        value = datetime.time(9, 30, tzinfo=datetime.UTC)
        with pytest.raises(ValueError, match="time zone"):
            SnowflakeDialect().render_literal(value)

    def test_uuid_renders_as_string(self):
        """A UUID renders as its canonical text in a string literal."""
        value = uuid.UUID("12345678-1234-5678-1234-567812345678")
        assert SnowflakeDialect().render_literal(value) == "'12345678-1234-5678-1234-567812345678'"

    def test_numpy_scalars_render_as_their_python_values(self):
        """Numpy scalars (int64 is not an int subclass) render like the Python value."""
        np = pytest.importorskip("numpy")
        dialect = SnowflakeDialect()
        assert dialect.render_literal(np.int64(5)) == "5"
        assert dialect.render_literal(np.float32(0.5)) == "0.5"
        assert dialect.render_literal(np.bool_(True)) == "TRUE"
        assert dialect.render_literal(np.str_("US")) == "'US'"

    @pytest.mark.parametrize(
        "make",
        [
            lambda np: np.datetime64("2024-01-01T00:00:00", "ns"),
            lambda np: np.datetime64("NaT"),
            lambda np: np.timedelta64(5, "ns"),
        ],
        ids=["datetime64-ns", "NaT", "timedelta64"],
    )
    @pytest.mark.parametrize("dialect", [SnowflakeDialect(), DatabricksDialect(), DuckDBDialect()])
    def test_numpy_datetimes_raise_rather_than_render_as_a_number(
        self, make: Callable[[Any], object], dialect: SQLDialect
    ):
        """
        ``item()`` turns a nanosecond datetime64 into an int and ``NaT`` into ``None``.

        Unwrapped, either renders as valid-looking SQL that compares against the wrong value.
        """
        np = pytest.importorskip("numpy")
        with pytest.raises(NotImplementedError, match=r"datetime64|timedelta64"):
            dialect.render_literal(make(np))

    @pytest.mark.parametrize("dialect", [SnowflakeDialect(), DatabricksDialect(), DuckDBDialect()])
    def test_an_int_or_float_subclass_renders_as_its_number(self, dialect: SQLDialect):
        """``repr()`` of an ``IntEnum`` member is ``<Tier.GOLD: 1>``, which is not SQL."""

        class Tier(enum.IntEnum):
            GOLD = 1

        class Ratio(float):
            def __repr__(self) -> str:
                return "Ratio()"

        assert dialect.render_literal(Tier.GOLD) == "1"
        assert dialect.render_literal(Ratio(0.5)) == "0.5"


class TestRenderLiteralDatabricks:
    """DBX-01c: Spark-string render_literal (escape backslash first, then quote)."""

    def test_plain_string(self):
        """A plain string is wrapped in single quotes."""
        assert DatabricksDialect().render_literal("US") == "'US'"

    def test_single_quote_backslash_escaped(self):
        r"""Spark escapes a single quote with a backslash (\')."""
        assert DatabricksDialect().render_literal("O'Reilly") == "'O\\'Reilly'"

    def test_backslash_escaped_first(self):
        r"""A backslash is doubled (\\) -- escaped before the quote."""
        assert DatabricksDialect().render_literal("a\\b") == "'a\\\\b'"

    def test_backslash_then_quote_ordering(self):
        r"""Backslash is escaped before the quote so order does not corrupt output."""
        # value: a\'b  ->  backslash doubled, then quote backslash-escaped
        assert DatabricksDialect().render_literal("a\\'b") == "'a\\\\\\'b'"

    def test_injection_attempt_is_escaped(self):
        """An injection-style value stays inside the quoted literal."""
        result = DatabricksDialect().render_literal("'; DROP TABLE x; --")
        assert result == "'\\'; DROP TABLE x; --'"

    def test_non_finite_float_raises(self):
        """inf/-inf/nan are not SQL numeric literals -- fail loudly."""
        for value in (float("inf"), float("-inf"), float("nan")):
            with pytest.raises(ValueError):
                DatabricksDialect().render_literal(value)

    def test_uuid_renders_as_string(self):
        """A UUID renders as its canonical text in a string literal."""
        value = uuid.UUID("12345678-1234-5678-1234-567812345678")
        assert DatabricksDialect().render_literal(value) == "'12345678-1234-5678-1234-567812345678'"

    def test_numpy_scalars_render_as_their_python_values(self):
        """Numpy scalars render like the Python value, in Spark's own spelling."""
        np = pytest.importorskip("numpy")
        assert DatabricksDialect().render_literal(np.int64(5)) == "5"
        assert DatabricksDialect().render_literal(np.bool_(False)) == "false"

    def test_time_raises_not_implemented(self):
        """Spark SQL has no TIME type, so a time of day still fails loudly."""
        with pytest.raises(NotImplementedError):
            DatabricksDialect().render_literal(datetime.time(9, 30))

    def test_none_renders_null(self):
        """None renders as the SQL NULL keyword (unquoted)."""
        assert DatabricksDialect().render_literal(None) == "NULL"

    def test_bool_renders_lowercase(self):
        """Booleans render as lowercase true/false for Spark SQL."""
        assert DatabricksDialect().render_literal(True) == "true"
        assert DatabricksDialect().render_literal(False) == "false"

    def test_int_renders_unquoted(self):
        """Integers render unquoted."""
        assert DatabricksDialect().render_literal(5) == "5"

    def test_unsupported_type_raises_not_implemented(self):
        """An unsupported literal type fails loudly rather than mis-escaping."""
        with pytest.raises(NotImplementedError):
            DatabricksDialect().render_literal({1, 2})

    def test_date_literal(self):
        """A date renders as a typed DATE literal in ISO-8601 form."""
        assert DatabricksDialect().render_literal(datetime.date(2024, 1, 31)) == "DATE '2024-01-31'"

    def test_naive_datetime_literal(self):
        """A naive datetime renders as TIMESTAMP and keeps its time of day."""
        value = datetime.datetime(2024, 1, 31, 10, 5)
        assert DatabricksDialect().render_literal(value) == "TIMESTAMP '2024-01-31T10:05:00'"

    def test_aware_datetime_normalises_to_utc_z(self):
        """An aware datetime is converted to UTC and suffixed with Z."""
        tz = datetime.timezone(datetime.timedelta(hours=2))
        value = datetime.datetime(2024, 1, 31, 10, 5, tzinfo=tz)
        assert DatabricksDialect().render_literal(value) == "TIMESTAMP '2024-01-31T08:05:00Z'"

    def test_datetime_microseconds_survive(self):
        """Sub-second precision reaches the literal unrounded."""
        value = datetime.datetime(2024, 1, 31, 10, 5, 3, 123456)
        assert ".123456" in DatabricksDialect().render_literal(value)

    def test_decimal_literal_is_bare_fixed_point(self):
        """A Decimal renders as bare fixed-point digits, unquoted and uncast."""
        assert DatabricksDialect().render_literal(Decimal("10.50")) == "10.50"

    def test_decimal_exponent_form_stays_decimal(self):
        """An exponent-form Decimal renders fixed-point, never as 1E+2."""
        assert DatabricksDialect().render_literal(Decimal("1E+2")) == "100"

    def test_non_finite_decimal_raises(self):
        """NaN/Infinity Decimals have no SQL literal form -- fail loudly."""
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with pytest.raises(ValueError):
                DatabricksDialect().render_literal(value)

    def test_date_literal_has_no_unescaped_quote(self):
        """A DATE literal carries exactly its two delimiting quotes."""
        rendered = DatabricksDialect().render_literal(datetime.date(2024, 1, 31))
        assert rendered.count("'") == 2

    def test_timestamp_literal_has_no_unescaped_quote(self):
        """A TIMESTAMP literal carries exactly its two delimiting quotes."""
        tz = datetime.timezone(datetime.timedelta(hours=-5))
        value = datetime.datetime(2024, 1, 31, 10, 5, tzinfo=tz)
        rendered = DatabricksDialect().render_literal(value)
        assert rendered.count("'") == 2

    def test_decimal_literal_is_digits_only(self):
        """A Decimal literal is digits, an optional sign and at most one point."""
        rendered = DatabricksDialect().render_literal(Decimal("-1234.5678"))
        assert re.fullmatch(r"-?\d+(\.\d+)?", rendered) is not None


class TestViewNameNormalization:
    """
    View names are folded through the dialect like column identifiers.

    A bare ``view="sales_view"`` resolves against a standard Snowflake view
    (created unquoted, stored UPPERCASE) and a Databricks view (stored
    lowercase). Schema-qualified names are quoted per-part. A pre-quoted
    segment is the escape hatch for a case-sensitive name.
    """

    def test_snowflake_quote_table_name_folds_to_uppercase(self):
        """Bare segment is folded to UPPERCASE then quoted for Snowflake."""
        assert SnowflakeDialect().quote_table_name("sales_view") == '"SALES_VIEW"'

    def test_snowflake_quote_table_name_qualified_per_part(self):
        """Schema-qualified names quote each segment, not the whole string."""
        assert (
            SnowflakeDialect().quote_table_name("my_schema.sales_view")
            == '"MY_SCHEMA"."SALES_VIEW"'
        )

    def test_snowflake_quote_table_name_preserves_prequoted_segment(self):
        """An already-quoted segment is preserved verbatim (escape hatch)."""
        assert SnowflakeDialect().quote_table_name('"sales_view"') == '"sales_view"'
        assert SnowflakeDialect().quote_table_name('analytics."My_View"') == '"ANALYTICS"."My_View"'

    @pytest.mark.parametrize(
        ("dialect", "name", "expected"),
        [
            (SnowflakeDialect(), '"My"View"', '"My""View"'),
            (
                SnowflakeDialect(),
                'analytics."x"; DROP TABLE t; --"',
                '"ANALYTICS"."x""; DROP TABLE t; --"',
            ),
            (DatabricksDialect(), "`my`view`", "`my``view`"),
            (DuckDBDialect(), '"My"View"', '"My""View"'),
        ],
        ids=["snowflake", "snowflake-injection", "databricks", "duckdb"],
    )
    def test_a_prequoted_segment_is_escaped_not_emitted_verbatim(
        self, dialect: SQLDialect, name: str, expected: str
    ):
        """
        The text between a pre-quoted segment's quotes is the name, and is escaped as one.

        Emitted verbatim, as before phase 54, a quote inside it closed the identifier early.
        """
        assert dialect.quote_table_name(name) == expected

    @pytest.mark.parametrize(
        ("dialect", "name", "expected"),
        [
            (SnowflakeDialect(), 'analytics."My""View"', '"ANALYTICS"."My""View"'),
            (DatabricksDialect(), "`my``view`", "`my``view`"),
            (DuckDBDialect(), '"My""View"', '"My""View"'),
            (SnowflakeDialect(), '"x""; DROP TABLE t; --"', '"x""; DROP TABLE t; --"'),
        ],
        ids=["snowflake", "databricks", "duckdb", "snowflake-injection"],
    )
    def test_a_prequoted_segment_already_escaped_the_sql_way_is_not_escaped_twice(
        self, dialect: SQLDialect, name: str, expected: str
    ):
        """
        A doubled quote inside a quoted segment is the standard SQL escape for one quote.

        ``"My""View"`` names ``My"View``, so it must compile to the same identifier as the raw
        ``"My"View"`` does, not to one that names ``My""View`` with both quotes doubled again.
        """
        assert dialect.quote_table_name(name) == expected

    @pytest.mark.parametrize(
        ("dialect", "name"),
        [
            (SnowflakeDialect(), 'analytics."my.view"'),
            (SnowflakeDialect(), '"my.view"'),
            (DatabricksDialect(), "`my.schema`.view"),
            (DuckDBDialect(), '"a.b.c"'),
        ],
        ids=["snowflake-qualified", "snowflake-bare", "databricks", "duckdb"],
    )
    def test_a_dot_inside_a_prequoted_segment_raises(self, dialect: SQLDialect, name: str):
        """
        A ``.`` inside quotes cannot be told from a separator, so it is refused.

        Split naively, as before phase 54, ``"my.view"`` became ``"my`` and ``view"``, and each
        was quoted again as if it were a bare name.
        """
        with pytest.raises(ValueError, match=r"\.' inside a quoted segment"):
            dialect.quote_table_name(name)

    @pytest.mark.parametrize("name", ['"Sales', 'sales"', '"', 'analytics."sales'])
    def test_an_unbalanced_quote_is_named_as_such(self, name: str):
        """A lone quote is not a dot inside quotes, and the message says what it is."""
        with pytest.raises(ValueError, match="unbalanced quote") as excinfo:
            SnowflakeDialect().quote_table_name(name)
        assert "'.' inside" not in str(excinfo.value)

    @pytest.mark.parametrize(
        "name",
        ['"a"_v', '"Order Book" ', 'analytics."Sales"x', 'x"Sales"'],
        ids=["suffix", "trailing-space", "qualified", "prefix"],
    )
    def test_text_outside_the_quotes_is_named_as_such(self, name: str):
        """With no dot in the name, the problem is the stray text, not a dot inside quotes."""
        with pytest.raises(ValueError, match="text outside its quotes") as excinfo:
            SnowflakeDialect().quote_table_name(name)
        assert "'.' inside" not in str(excinfo.value)

    def test_databricks_quote_table_name_folds_to_lowercase(self):
        """Databricks folds to lowercase then backtick-quotes."""
        assert DatabricksDialect().quote_table_name("Sales_View") == "`sales_view`"

    def test_qualified_view_name_in_from_clause(self):
        """A schema-qualified model view name is quoted per-part in FROM."""
        from semolina import Metric, SemanticView

        class QualifiedSales(SemanticView, view="analytics.sales_view"):
            revenue = Metric()

        query = QualifiedSales.query().metrics(QualifiedSales.revenue)
        sql = SQLBuilder(SnowflakeDialect()).build_select(query)
        assert sql == 'SELECT AGG("REVENUE") AS "revenue"\nFROM "ANALYTICS"."SALES_VIEW"'


class TestEachDialectRendersTheFullStatement:
    """The same fully built query, as each dialect renders it."""

    @pytest.mark.parametrize(
        ("dialect", "expected"),
        [
            (
                "snowflake",
                'SELECT AGG("REVENUE") AS "revenue", AGG("COST") AS "cost", '
                '"COUNTRY" AS "country", "REGION" AS "region"\n'
                'FROM "SALES_VIEW"\n'
                "WHERE (\"COUNTRY\" = 'US' OR \"COUNTRY\" = 'CA')\n"
                "GROUP BY ALL\n"
                'ORDER BY AGG("REVENUE") DESC NULLS FIRST, "COUNTRY" ASC\n'
                "LIMIT 100",
            ),
            (
                "databricks",
                "SELECT MEASURE(`revenue`) AS `revenue`, MEASURE(`cost`) AS `cost`, "
                "`country` AS `country`, `region` AS `region`\n"
                "FROM `sales_view`\n"
                "WHERE (`country` = 'US' OR `country` = 'CA')\n"
                "GROUP BY ALL\n"
                "ORDER BY `revenue` DESC NULLS FIRST, `country` ASC\n"
                "LIMIT 100",
            ),
            (
                "duckdb",
                'SELECT "revenue" AS "revenue", "cost" AS "cost", '
                '"country" AS "country", "region" AS "region"\n'
                "FROM semantic_view('sales_view', dimensions := ['country', 'region'], "
                "metrics := ['revenue', 'cost'], "
                "where_clause := '(\"country\" = ''US'' OR \"country\" = ''CA'')')\n"
                'ORDER BY "revenue" DESC NULLS FIRST, "country" ASC\n'
                "LIMIT 100",
            ),
        ],
    )
    def test_full_chain(self, dialect: str, expected: str):
        """
        Quoting, metric wrapping and name folding differ, and DuckDB's filter moves into the call.

        The select list is the same on all three: every field under its Python name, metrics
        first.
        """
        query = (
            Sales.query()
            .metrics(Sales.revenue, Sales.cost)
            .dimensions(Sales.country, Sales.region)
            .where((Sales.country == "US") | (Sales.country == "CA"))
            .order_by(Sales.revenue.desc(NullsOrdering.FIRST), Sales.country.asc())
            .limit(100)
        )

        assert query.to_sql(dialect) == expected


class TestDialectEscaping:
    """Test edge cases for identifier escaping."""

    def test_snowflake_empty_identifier(self):
        """Should handle empty identifier (edge case)."""
        dialect = SnowflakeDialect()
        assert dialect.quote_identifier("") == '""'

    def test_databricks_empty_identifier(self):
        """Should handle empty identifier (edge case)."""
        dialect = DatabricksDialect()
        assert dialect.quote_identifier("") == "``"

    def test_snowflake_only_quotes(self):
        """Should handle identifier that is only quotes."""
        dialect = SnowflakeDialect()
        # Three quotes -> escaped to six quotes inside, wrapped = 8 total
        assert dialect.quote_identifier('"""') == '""""""""'

    def test_databricks_only_backticks(self):
        """Should handle identifier that is only backticks."""
        dialect = DatabricksDialect()
        # Three backticks -> escaped to six backticks inside, wrapped = 8 total
        assert dialect.quote_identifier("```") == "````````"

    def test_snowflake_mixed_quotes_and_text(self):
        """Should handle mixed quotes and text."""
        dialect = SnowflakeDialect()
        result = dialect.quote_identifier('a"b"c')
        assert result == '"a""b""c"'
        # Verify unescaping would work: remove outer quotes, replace "" with "
        inner = result[1:-1]  # Remove outer quotes
        assert inner.replace('""', '"') == 'a"b"c'

    def test_databricks_mixed_backticks_and_text(self):
        """Should handle mixed backticks and text."""
        dialect = DatabricksDialect()
        result = dialect.quote_identifier("a`b`c")
        assert result == "`a``b``c`"
        # Verify unescaping would work
        inner = result[1:-1]  # Remove outer quotes
        assert inner.replace("``", "`") == "a`b`c"


# ---------------------------------------------------------------------------
# SQLDialect.placeholder, WHERE compiler, parameterization
# ---------------------------------------------------------------------------


class TestDialectPlaceholder:
    """Test SQLDialect.placeholder property returns backend-specific placeholder."""

    def test_snowflake_placeholder(self):
        """SnowflakeDialect.placeholder should return ?."""
        assert SnowflakeDialect().placeholder == "?"

    def test_databricks_placeholder(self):
        """DatabricksDialect.placeholder should return ?."""
        assert DatabricksDialect().placeholder == "?"


class TestWhereClauseCompiler:
    """Every node type compiles to its condition, read off a real ``.where()`` query."""

    # -- Leaf lookups (15 types) -----------------------------------------------

    def test_compile_exact(self):
        """Exact(f, v) -> '{quote(f)} = {ph}', [v]."""
        sql, params = where_clause(Exact("country", "US"))
        assert sql == '"COUNTRY" = ?'
        assert params == ["US"]

    def test_compile_not_equal(self):
        """NotEqual(f, v) -> '{quote(f)} != {ph}', [v]."""
        sql, params = where_clause(NotEqual("country", "US"))
        assert sql == '"COUNTRY" != ?'
        assert params == ["US"]

    def test_compile_gt(self):
        """Gt(f, v) -> '{quote(f)} > {ph}', [v]."""
        sql, params = where_clause(Gt("revenue", 1000))
        assert sql == '"REVENUE" > ?'
        assert params == [1000]

    def test_compile_gte(self):
        """Gte(f, v) -> '{quote(f)} >= {ph}', [v]."""
        sql, params = where_clause(Gte("revenue", 500))
        assert sql == '"REVENUE" >= ?'
        assert params == [500]

    def test_compile_lt(self):
        """Lt(f, v) -> '{quote(f)} < {ph}', [v]."""
        sql, params = where_clause(Lt("revenue", 100))
        assert sql == '"REVENUE" < ?'
        assert params == [100]

    def test_compile_lte(self):
        """Lte(f, v) -> '{quote(f)} <= {ph}', [v]."""
        sql, params = where_clause(Lte("revenue", 50))
        assert sql == '"REVENUE" <= ?'
        assert params == [50]

    def test_compile_in_with_values(self):
        """In(f, [a,b,c]) -> '{quote(f)} IN ({ph}, {ph}, {ph})', [a, b, c]."""
        sql, params = where_clause(In("country", ["US", "CA", "UK"]))
        assert sql == '"COUNTRY" IN (?, ?, ?)'
        assert params == ["US", "CA", "UK"]

    def test_compile_in_single_value(self):
        """In(f, [a]) -> '{quote(f)} IN ({ph})', [a]."""
        sql, params = where_clause(In("country", ["US"]))
        assert sql == '"COUNTRY" IN (?)'
        assert params == ["US"]

    def test_compile_in_empty(self):
        """In(f, []) -> '1 = 0', [] (always false, Django precedent)."""
        sql, params = where_clause(In("country", []))
        assert sql == "1 = 0"
        assert params == []

    def test_compile_in_from_a_generator(self):
        """
        A generator's values are all bound.

        A generator can be read only once. Compiling the placeholders and then the parameters
        from it separately would leave the placeholders with no values behind them.
        """
        sql, params = where_clause(Sales.country.in_(c for c in ["US", "CA"]))
        assert sql == '"COUNTRY" IN (?, ?)'
        assert params == ["US", "CA"]

    @pytest.mark.parametrize(
        "values",
        [
            lambda: ["US", "CA"],
            lambda: ("US", "CA"),
            lambda: {"US": 1, "CA": 2},
            lambda: iter(["US", "CA"]),
            lambda: (c for c in ["US", "CA"]),
        ],
        ids=["list", "tuple", "dict-keys", "iterator", "generator"],
    )
    def test_compile_in_binds_one_parameter_per_placeholder(
        self, values: Callable[[], Iterable[str]]
    ):
        """Whatever iterable ``in_()`` is given, the statement has a value for every ``?``."""
        sql, params = where_clause(Sales.country.in_(values()))
        assert sql.count("?") == len(params) == 2

    def test_compile_between(self):
        """Between(f, (lo, hi)) -> '{quote(f)} BETWEEN {ph} AND {ph}', [lo, hi]."""
        sql, params = where_clause(Between("revenue", (100, 500)))
        assert sql == '"REVENUE" BETWEEN ? AND ?'
        assert params == [100, 500]

    def test_compile_is_null_true(self):
        """IsNull(f, True) -> '{quote(f)} IS NULL', []."""
        sql, params = where_clause(IsNull("country", True))
        assert sql == '"COUNTRY" IS NULL'
        assert params == []

    def test_compile_is_null_false(self):
        """IsNull(f, False) -> '{quote(f)} IS NOT NULL', []."""
        sql, params = where_clause(IsNull("country", False))
        assert sql == '"COUNTRY" IS NOT NULL'
        assert params == []

    def test_compile_like(self):
        """Like(f, v) -> '{quote(f)} LIKE {ph}', [v]."""
        sql, params = where_clause(Like("name", "%test%"))
        assert sql == '"NAME" LIKE ?'
        assert params == ["%test%"]

    def test_compile_ilike(self):
        """ILike(f, v) -> '{quote(f)} ILIKE {ph}', [v]."""
        sql, params = where_clause(ILike("name", "%test%"))
        assert sql == '"NAME" ILIKE ?'
        assert params == ["%test%"]

    def test_compile_starts_with(self):
        """StartsWith(f, v) -> '{quote(f)} LIKE {ph} ESCAPE '!'', [escaped v + '%']."""
        sql, params = where_clause(StartsWith("name", "test"))
        assert sql == "\"NAME\" LIKE ? ESCAPE '!'"
        assert params == ["test%"]

    def test_compile_istarts_with(self):
        """IStartsWith(f, v) -> '{quote(f)} ILIKE {ph} ESCAPE '!'', [escaped v + '%']."""
        sql, params = where_clause(IStartsWith("name", "test"))
        assert sql == "\"NAME\" ILIKE ? ESCAPE '!'"
        assert params == ["test%"]

    def test_compile_ends_with(self):
        """EndsWith(f, v) -> '{quote(f)} LIKE {ph} ESCAPE '!'', ['%' + escaped v]."""
        sql, params = where_clause(EndsWith("name", "test"))
        assert sql == "\"NAME\" LIKE ? ESCAPE '!'"
        assert params == ["%test"]

    def test_compile_iends_with(self):
        """IEndsWith(f, v) -> '{quote(f)} ILIKE {ph} ESCAPE '!'', ['%' + escaped v]."""
        sql, params = where_clause(IEndsWith("name", "test"))
        assert sql == "\"NAME\" ILIKE ? ESCAPE '!'"
        assert params == ["%test"]

    def test_compile_iexact(self):
        """IExact(f, v) -> '{quote(f)} ILIKE {ph} ESCAPE '!'', [escaped v] (no wildcards)."""
        sql, params = where_clause(IExact("name", "Test"))
        assert sql == "\"NAME\" ILIKE ? ESCAPE '!'"
        assert params == ["Test"]

    # -- Composite nodes -------------------------------------------------------

    def test_compile_and(self):
        """And(l, r) -> '({l_sql} AND {r_sql})', l_params + r_params."""
        pred = Exact("country", "US") & Gt("revenue", 1000)
        sql, params = where_clause(pred)
        assert sql == '("COUNTRY" = ? AND "REVENUE" > ?)'
        assert params == ["US", 1000]

    def test_compile_or(self):
        """Or(l, r) -> '({l_sql} OR {r_sql})', l_params + r_params."""
        pred = Exact("country", "US") | Exact("country", "CA")
        sql, params = where_clause(pred)
        assert sql == '("COUNTRY" = ? OR "COUNTRY" = ?)'
        assert params == ["US", "CA"]

    def test_compile_not(self):
        """Not(i) -> 'NOT ({i_sql})', i_params."""
        pred = ~Exact("country", "US")
        sql, params = where_clause(pred)
        assert sql == 'NOT ("COUNTRY" = ?)'
        assert params == ["US"]

    def test_compile_nested_and_or_not(self):
        """(a & b) | ~c compiles correctly."""
        a = Exact("country", "US")
        b = Gt("revenue", 1000)
        c = Exact("region", "West")
        pred = (a & b) | ~c
        sql, params = where_clause(pred)
        assert sql == '(("COUNTRY" = ? AND "REVENUE" > ?) OR NOT ("REGION" = ?))'
        assert params == ["US", 1000, "West"]

    def test_compile_double_not(self):
        """~~predicate -> NOT (NOT ({sql}))."""
        pred = ~~Exact("country", "US")
        sql, params = where_clause(pred)
        assert sql == 'NOT (NOT ("COUNTRY" = ?))'
        assert params == ["US"]

    # -- Param accumulation ----------------------------------------------------

    def test_params_accumulated_correctly(self):
        """Composite predicates accumulate params from all children in order."""
        pred = And(
            left=Or(
                left=Exact("a", 1),
                right=Exact("b", 2),
            ),
            right=Gt("c", 3),
        )
        _sql, params = where_clause(pred)
        assert params == [1, 2, 3]

    # -- SQLDialect-specific placeholders -----------------------------------------

    # -- Error cases -----------------------------------------------------------

    def test_unknown_lookup_raises_not_implemented(self):
        """Unknown Lookup subclass raises NotImplementedError."""

        class CustomLookup(Lookup[str]):
            """A custom lookup that the compiler doesn't know about."""

        pred = CustomLookup("field", "value")
        with pytest.raises(NotImplementedError):
            where_clause(pred)

    def test_non_predicate_raises_type_error(self):
        """Non-Predicate input raises TypeError."""
        with pytest.raises(TypeError):
            where_clause("not a predicate")


class TestNoneInAFilter:
    """
    ``== None`` and ``!= None`` mean ``IS NULL`` and ``IS NOT NULL``; ``None`` elsewhere raises.

    That is D3. ``"COUNTRY" = NULL`` is never true, so before phase 54 ``Sales.country ==
    None`` compiled to a condition that matched no row, and said nothing.
    """

    @pytest.mark.parametrize(
        ("dialect", "column"), [("snowflake", '"COUNTRY"'), ("databricks", "`country`")]
    )
    def test_equal_to_none_is_is_null(self, dialect: str, column: str):
        """``field == None`` compiles to ``IS NULL`` with nothing bound."""
        sql, params = where_clause(Sales.country == None, dialect)  # noqa: E711
        assert sql == f"{column} IS NULL"
        assert params == []

    @pytest.mark.parametrize(
        ("dialect", "column"), [("snowflake", '"COUNTRY"'), ("databricks", "`country`")]
    )
    def test_not_equal_to_none_is_is_not_null(self, dialect: str, column: str):
        """``field != None`` compiles to ``IS NOT NULL`` with nothing bound."""
        sql, params = where_clause(Sales.country != None, dialect)  # noqa: E711
        assert sql == f"{column} IS NOT NULL"
        assert params == []

    def test_a_hand_built_exact_none_is_is_null_too(self):
        """The rewrite is the compiler's, so a lookup built directly gets it as well."""
        assert where_clause(Exact("country", None)) == ('"COUNTRY" IS NULL', [])
        assert where_clause(NotEqual("country", None)) == ('"COUNTRY" IS NOT NULL', [])

    def test_equal_to_none_composes(self):
        """The rewritten condition sits inside ``AND`` and ``NOT`` like any other."""
        pred = ~(Sales.country == None) & (Sales.revenue > 10)  # noqa: E711
        assert where_clause(pred) == ('(NOT ("COUNTRY" IS NULL) AND "REVENUE" > ?)', [10])

    def test_duckdb_binds_is_null_inside_its_where_clause(self):
        """DuckDB's bound ``where_clause`` string carries ``IS NULL``, not ``= NULL``."""
        query = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .where(Sales.country == None)  # noqa: E711
        )
        _sql, params = DuckDBSQLBuilder(DuckDBDialect()).build_select_with_params(query)
        assert params == ['"country" IS NULL']

    @pytest.mark.parametrize(
        "build",
        [
            lambda: Sales.revenue > None,
            lambda: Sales.revenue >= None,
            lambda: Sales.revenue < None,
            lambda: Sales.revenue <= None,
            lambda: Sales.country.like(None),  # pyright: ignore[reportArgumentType]
            lambda: Sales.country.ilike(None),  # pyright: ignore[reportArgumentType]
            lambda: Sales.country.startswith(None),  # pyright: ignore[reportArgumentType]
            lambda: Sales.country.istartswith(None),  # pyright: ignore[reportArgumentType]
            lambda: Sales.country.endswith(None),  # pyright: ignore[reportArgumentType]
            lambda: Sales.country.iendswith(None),  # pyright: ignore[reportArgumentType]
            lambda: Sales.country.iexact(None),  # pyright: ignore[reportArgumentType]
            lambda: Gt("revenue", None),
        ],
        ids=[
            "gt",
            "gte",
            "lt",
            "lte",
            "like",
            "ilike",
            "startswith",
            "istartswith",
            "endswith",
            "iendswith",
            "iexact",
            "hand-built",
        ],
    )
    def test_any_other_comparison_with_none_raises_when_built(self, build: Callable[[], object]):
        """A comparison that could only ever match nothing raises, pointing at ``isnull()``."""
        with pytest.raises(TypeError, match=r"None.*isnull\(\)"):
            build()

    @pytest.mark.parametrize(
        ("lo", "hi"), [(None, 10), (1, None), (None, None)], ids=["lo", "hi", "both"]
    )
    def test_between_with_a_none_bound_raises(self, lo: object, hi: object):
        """An open-ended range is not ``between()``: it raises, pointing at ``isnull()``."""
        with pytest.raises(TypeError, match=r"between\(\).*None.*isnull\(\)"):
            Sales.revenue.between(lo, hi)

    def test_between_checks_its_bounds_for_none_by_identity(self):
        """A bound whose ``==`` answers ``True`` for anything is not taken for ``None``."""

        class EqualsEverything:
            def __eq__(self, other: object) -> bool:
                return True

            __hash__ = object.__hash__

        lo = EqualsEverything()
        assert Sales.revenue.between(lo, 10).value == (lo, 10)

    def test_in_with_none_among_its_values_raises(self):
        """``IN`` never matches ``NULL``, so a ``None`` value would be dropped silently."""
        with pytest.raises(TypeError, match=r"in_\(\).*None.*isnull\(\)"):
            Sales.country.in_(["US", None])


class TestLikeEscaping:
    """
    The prefix, suffix and case-insensitive-equality lookups match their value literally.

    ``%`` and ``_`` in the value, and the escape character ``!`` itself, are escaped with
    ``!``, and the condition carries ``ESCAPE '!'``. Before phase 54 ``startswith("50%")``
    matched anything starting with ``50``. ``like()`` and ``ilike()`` take a pattern, so their
    wildcards still mean what they say and no ``ESCAPE`` is added.
    """

    @pytest.mark.parametrize(
        ("predicate", "operator", "pattern"),
        [
            (Sales.country.startswith("50%_off"), "LIKE", "50!%!_off%"),
            (Sales.country.istartswith("50%_off"), "ILIKE", "50!%!_off%"),
            (Sales.country.endswith("50%_off"), "LIKE", "%50!%!_off"),
            (Sales.country.iendswith("50%_off"), "ILIKE", "%50!%!_off"),
            (Sales.country.iexact("50%_off"), "ILIKE", "50!%!_off"),
        ],
        ids=["startswith", "istartswith", "endswith", "iendswith", "iexact"],
    )
    def test_wildcards_in_the_value_are_escaped(
        self, predicate: object, operator: str, pattern: str
    ):
        """Each lookup binds its escaped pattern and names the escape character."""
        assert where_clause(predicate) == (f"\"COUNTRY\" {operator} ? ESCAPE '!'", [pattern])

    def test_the_escape_character_is_escaped_too(self):
        """A literal ``!`` is doubled, so it cannot escape the character after it."""
        assert where_clause(Sales.country.startswith("Hi!_")) == (
            "\"COUNTRY\" LIKE ? ESCAPE '!'",
            ["Hi!!!_%"],
        )

    def test_a_value_without_wildcards_is_unchanged(self):
        """Nothing to escape means the pattern is the value plus its wildcard."""
        assert where_clause(Sales.country.startswith("US")) == (
            "\"COUNTRY\" LIKE ? ESCAPE '!'",
            ["US%"],
        )

    @pytest.mark.parametrize("method", ["like", "ilike"])
    def test_like_and_ilike_pass_their_pattern_through(self, method: str):
        """A pattern's own wildcards are the point of ``like()``: no escaping, no ``ESCAPE``."""
        operator = method.upper()
        assert where_clause(getattr(Sales.country, method)("5_%")) == (
            f'"COUNTRY" {operator} ?',
            ["5_%"],
        )

    def test_databricks_binds_a_backslash_as_an_ordinary_character(self):
        r"""
        Databricks binds the pattern with its backslash untouched.

        Spark's default ``LIKE`` escape is the backslash, so without ``ESCAPE '!'`` a value of
        ``a\_`` would escape the underscore. With it, the backslash is an ordinary character,
        and a bound value needs no string-literal escaping.
        """
        sql, params = where_clause(Sales.country.startswith("a\\_"), "databricks")
        assert sql == "`country` LIKE ? ESCAPE '!'"
        assert params == ["a\\!_%"]

    def test_an_inlined_databricks_pattern_keeps_a_backslash_literal(self):
        r"""
        Inlined, the same pattern doubles the backslash because Spark string literals need it.

        The inline path no longer runs for Databricks itself; see
        :class:`InliningDatabricksDialect`.
        """
        sql, params = where_clause(Sales.country.startswith("a\\_"), InliningDatabricksDialect())
        assert sql == "`country` LIKE 'a\\\\!_%' ESCAPE '!'"
        assert params == []

    def test_duckdb_binds_the_escape_inside_its_where_clause(self):
        """DuckDB's bound ``where_clause`` string carries the escaped pattern and ``ESCAPE``."""
        query = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .where(Sales.country.startswith("U_"))
        )
        _sql, params = DuckDBSQLBuilder(DuckDBDialect()).build_select_with_params(query)
        assert params == ["\"country\" LIKE 'U!_%' ESCAPE '!'"]


class TestMetricFiltersCompileToHaving:
    """
    A filter on a metric compiles to ``HAVING`` on the wrapped metric (D4, FILT-04).

    Both warehouses refuse a metric in ``WHERE``, bare or wrapped, and accept ``HAVING`` on
    the wrapped metric with post-aggregation results, including an unselected metric and a
    metric-only query with no ``GROUP BY`` (measured live in ``53-D4-METRIC-FILTER.md``).
    Before phase 54 Semolina sent the bare metric in ``WHERE``.
    """

    @pytest.mark.parametrize(
        ("dialect", "expected"),
        [
            (
                "snowflake",
                'SELECT AGG("REVENUE") AS "revenue", "COUNTRY" AS "country"\n'
                'FROM "SALES_VIEW"\nGROUP BY ALL\nHAVING AGG("REVENUE") > 1500',
            ),
            (
                "databricks",
                "SELECT MEASURE(`revenue`) AS `revenue`, `country` AS `country`\n"
                "FROM `sales_view`\nGROUP BY ALL\nHAVING MEASURE(`revenue`) > 1500",
            ),
        ],
    )
    def test_a_metric_filter_is_having_on_the_wrapped_metric(self, dialect: str, expected: str):
        """The shape the live probe accepted as ``having_wrapped``."""
        query = Sales.query().metrics(Sales.revenue).dimensions(Sales.country)
        assert query.where(Sales.revenue > 1500).to_sql(dialect) == expected

    def test_a_dimension_filter_stays_in_where_and_binds_first(self):
        """``AND`` at the top splits: the dimension before aggregation, the metric after."""
        query = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .where(Sales.country == "US", Sales.revenue > 1500)
            .order_by(Sales.country)
            .limit(5)
        )
        sql, params = SnowflakeDialect().create_builder().build_select_with_params(query)
        assert sql == (
            'SELECT AGG("REVENUE") AS "revenue", "COUNTRY" AS "country"\n'
            'FROM "SALES_VIEW"\n'
            'WHERE "COUNTRY" = ?\n'
            "GROUP BY ALL\n"
            'HAVING AGG("REVENUE") > ?\n'
            'ORDER BY "COUNTRY" ASC\n'
            "LIMIT 5"
        )
        assert params == ["US", 1500]

    def test_an_unselected_metric_is_filtered_by_its_expression(self):
        """It has no alias to name, so ``HAVING`` wraps it: ``having_wrapped_unselected``."""
        query = Sales.query().metrics(Sales.cost).dimensions(Sales.country)
        assert query.where(Sales.revenue > 1500).to_sql() == (
            'SELECT AGG("COST") AS "cost", "COUNTRY" AS "country"\n'
            'FROM "SALES_VIEW"\nGROUP BY ALL\nHAVING AGG("REVENUE") > 1500'
        )

    def test_a_metric_only_query_has_having_without_group_by(self):
        """No dimensions, no ``GROUP BY``; ``HAVING`` alone: ``having_wrapped_no_dimensions``."""
        query = Sales.query().metrics(Sales.revenue).where(Sales.revenue > 1000)
        assert query.to_sql("databricks") == (
            "SELECT MEASURE(`revenue`) AS `revenue`\nFROM `sales_view`\n"
            "HAVING MEASURE(`revenue`) > 1000"
        )

    def test_metric_conditions_keep_their_shape_inside_having(self):
        """``OR`` and ``NOT`` among metrics alone compile as they would anywhere."""
        query = (
            Sales.query().metrics(Sales.revenue).where((Sales.revenue > 10) | ~(Sales.cost <= 5))
        )
        assert query.to_sql().endswith('\nHAVING (AGG("REVENUE") > 10 OR NOT (AGG("COST") <= 5))')

    @pytest.mark.parametrize("dialect", ["snowflake", "databricks"])
    def test_a_dimension_and_a_metric_under_or_raise(self, dialect: str):
        """There is no clause both can go in, so the error names both fields."""
        query = Sales.query().metrics(Sales.revenue).dimensions(Sales.country)
        with pytest.raises(ValueError, match=r"Sales\.country.*Sales\.revenue.*OR or NOT"):
            query.where((Sales.country == "US") | (Sales.revenue > 10)).to_sql(dialect)


class TestBoundVersusInlinedParameters:
    """
    Snowflake and Databricks bind WHERE values as ``?``; DuckDB binds one string.

    DuckDB's bound string is the dimension filter's ``where_clause``, with its values rendered
    into it as literals. A dialect that cannot bind at all inlines every value; no shipped
    dialect does that any more, so :class:`InliningDatabricksDialect` stands in for one.

    Each case is the whole statement a warehouse receives plus its bound parameters, so a
    value that leaked into a bound template, or a placeholder left behind in an inlined one,
    fails the comparison without needing its own assertion.
    """

    @pytest.mark.parametrize(
        ("dialect", "expected_sql", "expected_params"),
        [
            (
                "snowflake",
                'SELECT AGG("REVENUE") AS "revenue", "COUNTRY" AS "country"\n'
                'FROM "SALES_VIEW"\n'
                'WHERE "COUNTRY" = ?\n'
                "GROUP BY ALL",
                ["US"],
            ),
            (
                "duckdb",
                'SELECT "revenue" AS "revenue", "country" AS "country"\n'
                "FROM semantic_view('sales_view', dimensions := ['country'], "
                "metrics := ['revenue'], where_clause := ?)",
                ["\"country\" = 'US'"],
            ),
            (
                "databricks",
                "SELECT MEASURE(`revenue`) AS `revenue`, `country` AS `country`\n"
                "FROM `sales_view`\n"
                "WHERE `country` = ?\nGROUP BY ALL",
                ["US"],
            ),
        ],
    )
    def test_a_filtered_query_per_dialect(
        self, dialect: str, expected_sql: str, expected_params: list[object]
    ):
        """The same filtered query, as each dialect sends it."""
        query = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .where(Sales.country == "US")
        )
        builder = resolve_dialect(dialect).create_builder()

        assert builder.build_select_with_params(query) == (expected_sql, expected_params)

    def test_a_dialect_that_cannot_bind_inlines_the_filter(self):
        """The same query from a non-binding dialect: the value is a literal, nothing is bound."""
        query = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .where(Sales.country == "US")
        )
        builder = InliningDatabricksDialect().create_builder()

        assert builder.build_select_with_params(query) == (
            "SELECT MEASURE(`revenue`) AS `revenue`, `country` AS `country`\n"
            "FROM `sales_view`\n"
            "WHERE `country` = 'US'\nGROUP BY ALL",
            [],
        )

    def test_a_query_without_filters_binds_nothing(self):
        """No WHERE clause, and no parameters."""
        query = Sales.query().metrics(Sales.revenue).dimensions(Sales.country)
        builder = resolve_dialect("snowflake").create_builder()

        assert builder.build_select_with_params(query) == (
            'SELECT AGG("REVENUE") AS "revenue", "COUNTRY" AS "country"\n'
            'FROM "SALES_VIEW"\n'
            "GROUP BY ALL",
            [],
        )

    def test_params_are_accumulated_in_order(self):
        """A composite filter binds its values in reading order."""
        condition, params = where_clause(Exact("country", "US") & Gt("revenue", 500))

        assert condition == '("COUNTRY" = ? AND "REVENUE" > ?)'
        assert params == ["US", 500]

    @pytest.mark.parametrize(
        ("predicate", "condition"),
        [
            (In("country", ["US", "CA"]), "`country` IN ('US', 'CA')"),
            (Exact("country", "O'Reilly"), "`country` = 'O\\'Reilly'"),
            # A '?' inside a value must not be read as a placeholder, in a list or beside a
            # second condition.
            (In("country", ["a?b", "CA"]), "`country` IN ('a?b', 'CA')"),
            (
                Exact("country", "a?b") & Exact("region", "WEST"),
                "(`country` = 'a?b' AND `region` = 'WEST')",
            ),
            (Exact("date_key", datetime.date(2024, 1, 31)), "`date_key` = DATE '2024-01-31'"),
        ],
        ids=["in-list", "quote", "placeholder-in-list", "placeholder-then-condition", "date"],
    )
    def test_an_inlining_dialect_renders_each_value_safely(self, predicate: object, condition: str):
        """Every value is rendered as an escaped literal, and nothing is left to bind."""
        assert where_clause(predicate, InliningDatabricksDialect()) == (condition, [])

    @pytest.mark.parametrize(
        ("predicate", "condition", "params"),
        [
            (In("country", ["US", "CA"]), "`country` IN (?, ?)", ["US", "CA"]),
            (Exact("country", "O'Reilly"), "`country` = ?", ["O'Reilly"]),
            (In("country", ["a?b", "CA"]), "`country` IN (?, ?)", ["a?b", "CA"]),
            (
                Exact("country", "a?b") & Exact("region", "WEST"),
                "(`country` = ? AND `region` = ?)",
                ["a?b", "WEST"],
            ),
            (
                Exact("date_key", datetime.date(2024, 1, 31)),
                "`date_key` = ?",
                [datetime.date(2024, 1, 31)],
            ),
        ],
        ids=["in-list", "quote", "placeholder-in-list", "placeholder-then-condition", "date"],
    )
    def test_databricks_binds_each_value(
        self, predicate: object, condition: str, params: list[object]
    ):
        """Databricks binds every value as-is, in reading order, and writes none into the SQL."""
        assert where_clause(predicate, "databricks") == (condition, params)

    def test_build_select_renders_parameters_inline(self):
        """``build_select()`` is the display form: the bound values rendered in place."""
        query = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .where(Sales.country == "US")
        )

        assert SQLBuilder(SnowflakeDialect()).build_select(query) == (
            'SELECT AGG("REVENUE") AS "revenue", "COUNTRY" AS "country"\n'
            'FROM "SALES_VIEW"\n'
            "WHERE \"COUNTRY\" = 'US'\n"
            "GROUP BY ALL"
        )


def to_sql_where(predicate: object, dialect: str) -> str:
    """
    Return the WHERE line ``to_sql()`` renders for one predicate.

    Args:
        predicate: What to pass to ``.where()``.
        dialect: ``"snowflake"`` or ``"databricks"``.

    Returns:
        The WHERE condition with its values rendered, without the ``WHERE`` keyword.
    """
    query = Filterable.query().metrics(Filterable.total)
    sql = query.where(predicate).to_sql(dialect)  # pyright: ignore[reportArgumentType]
    where = [line for line in sql.split("\n") if line.startswith("WHERE ")]
    assert len(where) == 1, sql
    return where[0].removeprefix("WHERE ")


class Dated(SemanticView, view="orders"):
    """A model with temporal dimensions, for the Snowflake temporal-binding tests."""

    revenue = Metric()
    ordered_on = Dimension()
    ordered_at = Dimension()
    country = Dimension()


class TestSnowflakeInlinesTemporalValues:
    """
    Snowflake writes date, timestamp and time values as literals and binds the rest.

    The Snowflake ADBC driver refuses a temporal bind parameter (``NOT_IMPLEMENTED:
    Unsupported bind param type date32``, and the same for ``timestamp[us]``), so a filter
    on a date column failed at execute time while ``to_sql()`` showed working SQL.
    """

    @pytest.mark.parametrize(
        ("value", "literal"),
        [
            (datetime.date(2024, 4, 1), "DATE '2024-04-01'"),
            (datetime.datetime(2024, 4, 1, 12, 30), "TIMESTAMP '2024-04-01T12:30:00'"),
            (
                datetime.datetime(2024, 4, 1, 12, 30, tzinfo=datetime.UTC),
                "'2024-04-01T12:30:00Z'::TIMESTAMP_TZ",
            ),
            (datetime.time(17, 45), "TIME '17:45:00'"),
        ],
    )
    def test_a_temporal_value_is_inlined(self, value: object, literal: str):
        """The literal ``to_sql()`` shows is the SQL that runs, with nothing bound for it."""
        query = Dated.query().metrics(Dated.revenue).where(Dated.ordered_on >= value)
        sql, params = SnowflakeDialect().create_builder().build_select_with_params(query)
        assert sql == (
            f'SELECT AGG("REVENUE") AS "revenue"\nFROM "ORDERS"\nWHERE "ORDERED_ON" >= {literal}'
        )
        assert params == []

    def test_other_values_beside_it_still_bind_in_order(self):
        """Only the temporal values leave the parameter list; the rest keep their slots."""
        query = (
            Dated.query()
            .metrics(Dated.revenue)
            .where(
                Dated.country == "US",
                Dated.ordered_on.between(datetime.date(2024, 1, 1), datetime.date(2024, 3, 31)),
                Dated.country.in_(["CA", "MX"]),
                Dated.revenue > 10,
            )
        )
        sql, params = SnowflakeDialect().create_builder().build_select_with_params(query)
        assert sql == (
            'SELECT AGG("REVENUE") AS "revenue"\n'
            'FROM "ORDERS"\n'
            'WHERE (("COUNTRY" = ? AND "ORDERED_ON" BETWEEN DATE \'2024-01-01\' AND '
            "DATE '2024-03-31') AND \"COUNTRY\" IN (?, ?))\n"
            'HAVING AGG("REVENUE") > ?'
        )
        assert params == ["US", "CA", "MX", 10]

    def test_a_time_with_a_time_zone_is_refused_while_building(self):
        """
        It has no Snowflake literal and the driver cannot bind it, so it fails before execute.

        The error names the value, where a bound one would fail inside the driver.
        """
        query = (
            Dated.query()
            .metrics(Dated.revenue)
            .where(Dated.ordered_at == datetime.time(9, 0, tzinfo=datetime.UTC))
        )
        with pytest.raises(ValueError, match="time with a time zone"):
            SnowflakeDialect().create_builder().build_select_with_params(query)

    @pytest.mark.parametrize("dialect", ["databricks", "duckdb"])
    def test_other_dialects_are_unchanged(self, dialect: str):
        """Databricks still binds a date; DuckDB still renders it into ``where_clause``."""
        query = (
            Dated.query()
            .metrics(Dated.revenue)
            .where(Dated.ordered_on >= datetime.date(2024, 4, 1))
        )
        _sql, params = resolve_dialect(dialect).create_builder().build_select_with_params(query)
        if dialect == "databricks":
            assert params == [datetime.date(2024, 4, 1)]
        else:
            assert params == ["\"ordered_on\" >= DATE '2024-04-01'"]


class TestToSqlRendersEachDialectsLiterals:
    """
    ``to_sql()`` renders values as the chosen dialect's SQL literals, so its output runs.

    Before phase 54 it showed ``repr()``: ``"O'Brien"`` in double quotes, which Snowflake and
    DuckDB read as an identifier, ``datetime.date(2024, 1, 2)`` and ``Decimal('1.50')``.
    """

    @pytest.mark.parametrize(
        ("dialect", "expected"),
        [
            ("snowflake", "\"COUNTRY\" = 'O''Brien'"),
            ("databricks", "`country` = 'O\\'Brien'"),
        ],
    )
    def test_an_apostrophe_is_escaped_the_dialects_way(self, dialect: str, expected: str):
        """Snowflake doubles the quote; Spark SQL backslash-escapes it."""
        assert to_sql_where(Sales.country == "O'Brien", dialect) == expected

    def test_snowflake_doubles_a_backslash(self):
        r"""A Snowflake string literal reads ``\`` as an escape, so a backslash is doubled."""
        assert to_sql_where(Sales.country == "a\\b", "snowflake") == "\"COUNTRY\" = 'a\\\\b'"

    @pytest.mark.parametrize(
        ("dialect", "column"), [("snowflake", '"COUNTRY"'), ("databricks", "`country`")]
    )
    def test_a_date_is_a_date_literal(self, dialect: str, column: str):
        """A ``date`` is ``DATE 'yyyy-mm-dd'``, not its ``repr()``."""
        predicate = Sales.country == datetime.date(2024, 1, 2)
        assert to_sql_where(predicate, dialect) == f"{column} = DATE '2024-01-02'"

    @pytest.mark.parametrize(
        ("dialect", "column"), [("snowflake", '"REVENUE"'), ("databricks", "`revenue`")]
    )
    def test_a_decimal_is_its_fixed_point_digits(self, dialect: str, column: str):
        """A ``Decimal`` keeps its scale and loses its constructor."""
        assert to_sql_where(Sales.revenue > Decimal("1.50"), dialect) == f"{column} > 1.50"

    def test_a_question_mark_inside_a_value_is_not_a_placeholder(self):
        """
        Each value fills its own placeholder, whatever the values before it contain.

        Filling placeholders by searching the growing string put ``5`` inside ``'a?b'``.
        """
        predicate = (Sales.country == "a?b") & (Sales.revenue > 5)
        assert to_sql_where(predicate, "snowflake") == '("COUNTRY" = \'a?b\' AND "REVENUE" > 5)'

    def test_duckdb_renders_its_outer_where_values_as_literals(self):
        """DuckDB's metric filter is outer SQL, so its ``Decimal`` is a literal too."""
        sql = Sales.query().metrics(Sales.revenue).where(Sales.revenue > Decimal("1.50"))
        assert sql.to_sql("duckdb").endswith('\nWHERE "revenue" > 1.50')

    def test_duckdbs_to_sql_output_runs(self, duckdb_file_backed_db: Any):
        """The DuckDB statement ``to_sql()`` prints, apostrophe and ``Decimal`` included, runs."""
        import duckdb

        sql = (
            SalesOnFile.query()
            .metrics(SalesOnFile.revenue)
            .dimensions(SalesOnFile.country)
            .where(SalesOnFile.country != "O'Brien", SalesOnFile.revenue > Decimal("1500.5"))
            .to_sql("duckdb")
        )
        conn = duckdb.connect(database=str(duckdb_file_backed_db), read_only=True)
        try:
            conn.execute("LOAD semantic_views")
            rows = conn.execute(sql).fetchall()
        finally:
            conn.close()
        assert rows == [(2000, "CA")]


class Flags(SemanticView, view="flags?"):
    """A view and a column whose names contain ``?``, which is also the placeholder."""

    total = Metric()
    active = Dimension(source="is_active?")


class TestAQuestionMarkInAnIdentifier:
    """
    A ``?`` in a view or column name is never taken for a placeholder (FILT-07).

    Databricks used to inline its values, and the inliner found placeholders by searching
    the finished statement for ``?``. A ``?`` inside a quoted name then counted as one, so
    the statement either failed with a placeholder count mismatch or had a value pasted
    into the name. Placeholders are now marked where the compiler emits them.
    """

    def test_an_inlining_dialect_inlines_the_value_and_keeps_the_names(self):
        """Every ``?`` left in the statement is part of a name."""
        query = Flags.query().metrics(Flags.total).dimensions(Flags.active)
        sql, params = (
            InliningDatabricksDialect()
            .create_builder()
            .build_select_with_params(query.where(Flags.active == "y"))
        )
        assert sql == (
            "SELECT MEASURE(`total`) AS `total`, `is_active?` AS `active`\n"
            "FROM `flags?`\n"
            "WHERE `is_active?` = 'y'\n"
            "GROUP BY ALL"
        )
        assert params == []

    def test_databricks_binds_the_value_and_keeps_the_names(self):
        """The bound statement has one placeholder, for the one value, beside the names' ``?``."""
        query = Flags.query().metrics(Flags.total).dimensions(Flags.active)
        sql, params = (
            DatabricksDialect()
            .create_builder()
            .build_select_with_params(query.where(Flags.active == "y"))
        )
        assert sql == (
            "SELECT MEASURE(`total`) AS `total`, `is_active?` AS `active`\n"
            "FROM `flags?`\n"
            "WHERE `is_active?` = ?\n"
            "GROUP BY ALL"
        )
        assert params == ["y"]

    def test_snowflake_binds_the_value_and_keeps_the_names(self):
        """The bound statement has one placeholder, for the one value."""
        query = Flags.query().metrics(Flags.total).where(Flags.active == "y")
        sql, params = SnowflakeDialect().create_builder().build_select_with_params(query)
        assert sql == ('SELECT AGG("TOTAL") AS "total"\nFROM "FLAGS?"\nWHERE "is_active?" = ?')
        assert params == ["y"]

    def test_to_sql_fills_only_the_placeholders(self):
        """``to_sql()`` renders the value where it belongs and leaves the names alone."""
        sql = Flags.query().metrics(Flags.total).where(Flags.active == "y").to_sql()
        assert sql == 'SELECT AGG("TOTAL") AS "total"\nFROM "FLAGS?"\nWHERE "is_active?" = \'y\''

    def test_duckdb_renders_the_value_into_its_where_clause(self):
        """DuckDB's bound ``where_clause`` keeps the name's ``?`` and holds the value."""
        query = Flags.query().metrics(Flags.total).where(Flags.active == "y")
        sql, params = DuckDBSQLBuilder(DuckDBDialect()).build_select_with_params(query)
        assert sql == (
            'SELECT "total" AS "total"\n'
            "FROM semantic_view('flags?', metrics := ['total'], where_clause := ?)"
        )
        assert params == ["\"is_active?\" = 'y'"]


class TestRenderInline:
    """``render_inline`` fills each placeholder with the dialect's SQL literal."""

    def test_render_inline_single_param(self):
        """render_inline substitutes a single param as a string literal."""
        builder = SQLBuilder(SnowflakeDialect())
        result = builder.render_inline('"country" = ?', ["US"])
        assert result == "\"country\" = 'US'"

    def test_render_inline_multiple_params(self):
        """render_inline substitutes multiple params in order."""
        builder = SQLBuilder(SnowflakeDialect())
        result = builder.render_inline(
            '"country" = ? AND "revenue" > ?',
            ["US", 1000],
        )
        assert result == '"country" = \'US\' AND "revenue" > 1000'

    def test_render_inline_no_params(self):
        """render_inline with no params returns unchanged SQL."""
        builder = SQLBuilder(SnowflakeDialect())
        result = builder.render_inline("SELECT 1", [])
        assert result == "SELECT 1"

    @pytest.mark.parametrize(
        "sql",
        ["WHERE `country` = 'a?b'", 'SELECT "vip?" FROM "t"'],
        ids=["inlined-value", "quoted-name"],
    )
    def test_render_inline_with_no_params_leaves_a_question_mark_alone(self, sql: str):
        """
        With nothing to fill, a ``?`` is not a placeholder, so the SQL comes back unchanged.

        A non-binding dialect's ``build_select_with_params`` inlines its values and returns no
        params; a ``?`` in one of those values, or in a quoted name, made ``render_inline``
        raise.
        """
        builder = SQLBuilder(DatabricksDialect())
        assert builder.render_inline(sql, []) == sql

    def test_render_inline_databricks_placeholder(self):
        """render_inline works with ? placeholder for Databricks."""
        builder = SQLBuilder(DatabricksDialect())
        result = builder.render_inline("`country` = ?", ["US"])
        assert result == "`country` = 'US'"


# ---------------------------------------------------------------------------
# normalize_identifier, and how source= bypasses it
# ---------------------------------------------------------------------------


class TestNormalizeIdentifier:
    """Test normalize_identifier on each dialect."""

    def test_snowflake_normalizes_to_uppercase(self):
        """SnowflakeDialect.normalize_identifier returns uppercase."""
        dialect = SnowflakeDialect()
        assert dialect.normalize_identifier("order_id") == "ORDER_ID"
        assert dialect.normalize_identifier("revenue") == "REVENUE"
        assert dialect.normalize_identifier("my_field_name") == "MY_FIELD_NAME"

    def test_databricks_normalizes_to_lowercase(self):
        """DatabricksDialect.normalize_identifier returns lowercase."""
        dialect = DatabricksDialect()
        assert dialect.normalize_identifier("ORDER_ID") == "order_id"
        assert dialect.normalize_identifier("Revenue") == "revenue"
        assert dialect.normalize_identifier("MY_FIELD") == "my_field"


class TestWhereClauseNormalization:
    """Test WHERE clause field_name normalization through dialect."""

    def test_snowflake_where_normalizes_field_name_to_uppercase(self):
        """Snowflake WHERE clause normalizes Python field_name to UPPERCASE."""
        sql, params = where_clause(Exact("order_id", "ORD-001"))
        assert sql == '"ORDER_ID" = ?'
        assert params == ["ORD-001"]

    def test_databricks_where_normalizes_field_name_to_lowercase(self):
        """Databricks WHERE clause normalizes Python field_name to lowercase, value bound."""
        sql, params = where_clause(Exact("ORDER_ID", "ORD-001"), "databricks")
        assert sql == "`order_id` = ?"
        assert params == ["ORD-001"]


class TestWhereClauseSourceOverride:
    """Regression tests: source= override propagates through WHERE clause compiler."""

    def test_metric_with_source_uses_source_in_having(self):
        """
        ``source=`` is the column selected and filtered on, verbatim and unfolded.

        The result still comes back under the Python attribute name: the alias is the one
        place that name appears, so a renamed warehouse column needs no change to the code
        that reads the row.
        """

        class MyView(SemanticView, view="my_view"):
            revenue_usd_field = Metric[int](source="revenue_usd")

        query = (
            MyView.query().metrics(MyView.revenue_usd_field).where(MyView.revenue_usd_field > 100)
        )

        assert query.to_sql() == (
            'SELECT AGG("revenue_usd") AS "revenue_usd_field"\nFROM "MY_VIEW"\n'
            'HAVING AGG("revenue_usd") > 100'
        )

    @pytest.mark.parametrize(
        ("dialect", "expected"),
        [
            ("snowflake", 'SELECT "COUNTRY_CODE" AS "country"\nFROM "V"\nGROUP BY ALL'),
            ("databricks", "SELECT `COUNTRY_CODE` AS `country`\nFROM `v`\nGROUP BY ALL"),
        ],
    )
    def test_source_is_verbatim_on_every_dialect(self, dialect: str, expected: str):
        """A ``source=`` column is not folded by the dialect, and returns under the field name."""

        class V(SemanticView, view="v"):
            country = Dimension[str](source="COUNTRY_CODE")

        assert V.query().dimensions(V.country).to_sql(dialect) == expected

    def test_field_without_source_where_still_normalized(self):
        """Field without source= still gets dialect normalization in WHERE."""
        sql, params = where_clause(Exact("revenue", "US"))
        assert sql == '"REVENUE" = ?'
        assert params == ["US"]


# ---------------------------------------------------------------------------
# DuckDB SQLDialect tests
# ---------------------------------------------------------------------------


class TestDuckDBDialect:
    """Test DuckDBDialect identifier quoting, metric wrapping, and factory."""

    def test_placeholder(self):
        """DuckDBDialect uses ? placeholder (qmark paramstyle)."""
        assert DuckDBDialect().placeholder == "?"

    def test_quote_identifier_simple(self):
        """Should quote simple identifiers with double quotes."""
        assert DuckDBDialect().quote_identifier("col") == '"col"'

    def test_quote_identifier_escapes(self):
        """Should escape internal double quotes by doubling them."""
        assert DuckDBDialect().quote_identifier('a"b') == '"a""b"'

    def test_wrap_metric_returns_plain_quoted(self):
        """wrap_metric returns plain quoted identifier (no AGG/MEASURE)."""
        assert DuckDBDialect().wrap_metric("revenue") == '"revenue"'

    def test_normalize_identifier_lowercase(self):
        """DuckDB normalizes identifiers to lowercase."""
        assert DuckDBDialect().normalize_identifier("REVENUE") == "revenue"

    def test_create_builder_returns_duckdb_builder(self):
        """create_builder() returns DuckDBSQLBuilder instance."""
        assert isinstance(DuckDBDialect().create_builder(), DuckDBSQLBuilder)


# ---------------------------------------------------------------------------
# create_builder() factory method tests
# ---------------------------------------------------------------------------


class TestCreateBuilderFactory:
    """Test that create_builder() returns the correct builder for each dialect."""

    def test_snowflake_creates_base_builder(self):
        """SnowflakeDialect.create_builder() returns base SQLBuilder."""
        builder = SnowflakeDialect().create_builder()
        assert isinstance(builder, SQLBuilder)
        assert not isinstance(builder, DuckDBSQLBuilder)

    def test_databricks_creates_base_builder(self):
        """DatabricksDialect.create_builder() returns base SQLBuilder."""
        builder = DatabricksDialect().create_builder()
        assert isinstance(builder, SQLBuilder)
        assert not isinstance(builder, DuckDBSQLBuilder)


# ---------------------------------------------------------------------------
# DuckDB SQL Builder tests
# ---------------------------------------------------------------------------


class TestDuckDBSQLBuilder:
    """
    Test DuckDBSQLBuilder generates correct semantic_view() SQL.

    The outer ``SELECT`` names every selected field and aliases it to its Python name (D2-1),
    so the result keys match Snowflake's and Databricks'. Dimension and fact filters go into
    the call as a bound ``where_clause``, metric filters into an outer ``WHERE`` (D2-3).
    """

    def test_grouped_query_sql(self):
        """Dimensions + metrics: each is projected under its field name, metrics first."""
        query = Sales.query().metrics(Sales.revenue).dimensions(Sales.country)
        builder = DuckDBSQLBuilder(DuckDBDialect())
        sql, params = builder.build_select_with_params(query)
        assert sql == (
            'SELECT "revenue" AS "revenue", "country" AS "country"\n'
            "FROM semantic_view('sales_view', dimensions := ['country'], "
            "metrics := ['revenue'])"
        )
        assert params == []

    def test_facts_only_query(self):
        """Facts only produces semantic_view() with facts arg."""
        query = Sales.query().dimensions(Sales.unit_price)
        builder = DuckDBSQLBuilder(DuckDBDialect())
        sql, params = builder.build_select_with_params(query)
        assert sql == (
            'SELECT DISTINCT "unit_price" AS "unit_price"\n'
            "FROM semantic_view('sales_view', facts := ['unit_price'])"
        )
        assert params == []

    def test_facts_and_dimensions_query(self):
        """Dimensions + facts are projected in the order they were selected."""
        query = Sales.query().dimensions(Sales.country, Sales.unit_price)
        builder = DuckDBSQLBuilder(DuckDBDialect())
        sql, params = builder.build_select_with_params(query)
        assert sql == (
            'SELECT DISTINCT "country" AS "country", "unit_price" AS "unit_price"\n'
            "FROM semantic_view('sales_view', "
            "dimensions := ['country'], facts := ['unit_price'])"
        )
        assert params == []

    def test_a_source_override_is_requested_by_source_and_returned_by_field_name(self):
        """``source=`` names the member asked for; the alias is still the field's own name."""

        class Renamed(SemanticView, view="sales_view"):
            revenue = Metric[int](source="net_revenue")
            country = Dimension[str](source="COUNTRY_CODE")

        query = Renamed.query().metrics(Renamed.revenue).dimensions(Renamed.country)
        sql, _params = DuckDBSQLBuilder(DuckDBDialect()).build_select_with_params(query)
        assert sql == (
            'SELECT "net_revenue" AS "revenue", "COUNTRY_CODE" AS "country"\n'
            "FROM semantic_view('sales_view', dimensions := ['COUNTRY_CODE'], "
            "metrics := ['net_revenue'])"
        )

    def test_facts_and_metrics_raises(self):
        """ValueError when both facts and metrics are present."""
        query = Sales.query().metrics(Sales.revenue).dimensions(Sales.unit_price)
        builder = DuckDBSQLBuilder(DuckDBDialect())
        with pytest.raises(ValueError, match="combining facts and metrics"):
            builder.build_select_with_params(query)

    def test_where_clause(self):
        """A dimension filter goes into the call as one bound ``where_clause`` string."""
        query = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .where(Sales.country == "US")
        )
        builder = DuckDBSQLBuilder(DuckDBDialect())
        sql, params = builder.build_select_with_params(query)
        assert sql == (
            'SELECT "revenue" AS "revenue", "country" AS "country"\n'
            "FROM semantic_view('sales_view', dimensions := ['country'], "
            "metrics := ['revenue'], where_clause := ?)"
        )
        assert params == ["\"country\" = 'US'"]

    def test_where_dimension_not_selected_filters_without_regrouping(self):
        """
        A filter on an unselected dimension does not add it to ``dimensions``.

        Adding it would regroup the result by it (ALIAS-05). ``where_clause`` is applied
        before aggregation, so the dimension never has to be returned.
        """
        query = Sales.query().metrics(Sales.revenue).where(Sales.country == "US")
        builder = DuckDBSQLBuilder(DuckDBDialect())
        sql, params = builder.build_select_with_params(query)
        assert sql == (
            'SELECT "revenue" AS "revenue"\n'
            "FROM semantic_view('sales_view', metrics := ['revenue'], where_clause := ?)"
        )
        assert params == ["\"country\" = 'US'"]

    def test_a_whole_or_of_dimensions_stays_one_where_clause(self):
        """A filter over dimensions only is not split, whatever its shape."""
        query = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.region)
            .where((Sales.country == "US") | ~(Sales.country == "CA"))
        )
        _sql, params = DuckDBSQLBuilder(DuckDBDialect()).build_select_with_params(query)
        assert params == ["(\"country\" = 'US' OR NOT (\"country\" = 'CA'))"]

    def test_where_clause_values_are_rendered_as_escaped_literals(self):
        """A quote in a value is doubled inside the bound clause, never left to close it."""
        query = (
            Sales.query()
            .metrics(Sales.revenue)
            .where(Sales.country.in_(["O'Neill", "x' OR 1=1 --"]))
        )
        _sql, params = DuckDBSQLBuilder(DuckDBDialect()).build_select_with_params(query)
        assert params == ["\"country\" IN ('O''Neill', 'x'' OR 1=1 --')"]

    def test_a_fact_filter_goes_into_the_where_clause(self):
        """Facts are row-level, so a fact filter is pre-aggregation too."""
        query = Sales.query().dimensions(Sales.unit_price).where(Sales.unit_price > 5)
        sql, params = DuckDBSQLBuilder(DuckDBDialect()).build_select_with_params(query)
        assert sql == (
            'SELECT DISTINCT "unit_price" AS "unit_price"\n'
            "FROM semantic_view('sales_view', facts := ['unit_price'], where_clause := ?)"
        )
        assert params == ['"unit_price" > 5']

    def test_a_metric_filter_goes_into_an_outer_where(self):
        """The extension refuses a metric in ``where_clause``; an outer WHERE is HAVING."""
        query = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .where(Sales.revenue > 100)
        )
        sql, params = DuckDBSQLBuilder(DuckDBDialect()).build_select_with_params(query)
        assert sql == DUCKDB_SALES + '\nWHERE "revenue" > ?'
        assert params == [100]

    def test_an_unselected_metric_filter_is_requested_but_not_projected(self):
        """A metric does not group, so requesting it changes no grain."""
        query = (
            Sales.query().metrics(Sales.revenue).dimensions(Sales.country).where(Sales.cost > 100)
        )
        sql, params = DuckDBSQLBuilder(DuckDBDialect()).build_select_with_params(query)
        assert sql == (
            'SELECT "revenue" AS "revenue", "country" AS "country"\n'
            "FROM semantic_view('sales_view', dimensions := ['country'], "
            "metrics := ['revenue', 'cost'])\n"
            'WHERE "cost" > ?'
        )
        assert params == [100]

    def test_a_top_level_and_splits_into_where_clause_and_outer_where(self):
        """Each conjunct goes where it can be applied; the clause is bound first."""
        query = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .where((Sales.country == "US") & (Sales.revenue > 100) & (Sales.region == "West"))
        )
        sql, params = DuckDBSQLBuilder(DuckDBDialect()).build_select_with_params(query)
        assert sql == (
            'SELECT "revenue" AS "revenue", "country" AS "country"\n'
            "FROM semantic_view('sales_view', dimensions := ['country'], "
            "metrics := ['revenue'], where_clause := ?)\n"
            'WHERE "revenue" > ?'
        )
        assert params == ["(\"country\" = 'US' AND \"region\" = 'West')", 100]

    @pytest.mark.parametrize(
        "predicate",
        [
            (Sales.country == "US") | (Sales.revenue > 100),
            ~((Sales.country == "US") & (Sales.revenue > 100)),
            (Sales.region == "West") & ((Sales.country == "US") | (Sales.revenue > 100)),
        ],
        ids=["or", "not-and", "and-of-mixed-or"],
    )
    def test_a_dimension_and_a_metric_under_or_or_not_is_refused(self, predicate: object):
        """
        Such a filter has no pre-aggregation and post-aggregation halves to split into.

        The error names both kinds of field so the reader can see which part to move.
        """
        query = Sales.query().metrics(Sales.revenue).where(predicate)  # pyright: ignore[reportArgumentType]
        builder = DuckDBSQLBuilder(DuckDBDialect())
        with pytest.raises(ValueError, match=r"country.*revenue|revenue.*country"):
            builder.build_select_with_params(query)

    @pytest.mark.parametrize(
        ("predicate", "clause", "outer"),
        [
            (
                (Sales.country == "US") & ((Sales.region == "West") & (Sales.unit_price > 1)),
                '("country" = \'US\' AND ("region" = \'West\' AND "unit_price" > 1))',
                None,
            ),
            (
                (Sales.revenue > 1) & ((Sales.cost > 2) & (Sales.revenue < 9)),
                None,
                '("revenue" > ? AND ("cost" > ? AND "revenue" < ?))',
            ),
        ],
        ids=["dimensions", "metrics"],
    )
    def test_a_filter_of_one_kind_is_sent_as_written(
        self, predicate: object, clause: str | None, outer: str | None
    ):
        """Only a filter that mixes both kinds is split; the rest keep their grouping."""
        query = Sales.query().metrics(Sales.revenue).where(predicate)  # pyright: ignore[reportArgumentType]
        sql, params = DuckDBSQLBuilder(DuckDBDialect()).build_select_with_params(query)
        if clause is not None:
            assert params == [clause]
            assert "\nWHERE" not in sql
        if outer is not None:
            assert sql.split("\n")[-1] == f"WHERE {outer}"
            assert "where_clause" not in sql

    def test_a_query_without_a_bound_model_still_routes_a_metric_filter(self):
        """
        A query built without ``Model.query()`` finds its model through its fields.

        Without one the metric filter would reach ``where_clause``, which the extension
        refuses.
        """
        from semolina.query import Query

        query = Query().metrics(Sales.revenue).where(Sales.revenue > 100)
        sql, params = DuckDBSQLBuilder(DuckDBDialect()).build_select_with_params(query)
        assert sql == (
            'SELECT "revenue" AS "revenue"\n'
            "FROM semantic_view('sales_view', metrics := ['revenue'])\n"
            'WHERE "revenue" > ?'
        )
        assert params == [100]

    def test_a_metric_filter_on_a_facts_query_is_refused(self):
        """A metric filter needs the metric, and facts and metrics cannot be combined."""
        query = Sales.query().dimensions(Sales.unit_price).where(Sales.revenue > 100)
        with pytest.raises(ValueError, match="combining facts and metrics"):
            DuckDBSQLBuilder(DuckDBDialect()).build_select_with_params(query)

    def test_order_by_metric_no_agg_wrap(self):
        """ORDER BY uses plain quoted identifier for metrics (no AGG/MEASURE)."""
        query = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .order_by(Sales.revenue.desc())
        )
        builder = DuckDBSQLBuilder(DuckDBDialect())
        sql, _params = builder.build_select_with_params(query)
        assert sql == DUCKDB_SALES + '\nORDER BY "revenue" DESC'

    def test_order_by_dimension(self):
        """ORDER BY dimension uses plain quoted identifier."""
        query = (
            Sales.query().metrics(Sales.revenue).dimensions(Sales.country).order_by(Sales.country)
        )
        builder = DuckDBSQLBuilder(DuckDBDialect())
        sql, _params = builder.build_select_with_params(query)
        assert sql == DUCKDB_SALES + '\nORDER BY "country" ASC'

    def test_order_by_an_unselected_metric_is_requested_but_not_projected(self):
        """The outer ORDER BY can only see what the call returns, so the metric is asked for."""
        query = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .order_by(Sales.cost.desc())
        )
        sql, params = DuckDBSQLBuilder(DuckDBDialect()).build_select_with_params(query)
        assert sql == (
            'SELECT "revenue" AS "revenue", "country" AS "country"\n'
            "FROM semantic_view('sales_view', dimensions := ['country'], "
            "metrics := ['revenue', 'cost'])\n"
            'ORDER BY "cost" DESC'
        )
        assert params == []

    def test_limit(self):
        """LIMIT N as outer SQL."""
        query = Sales.query().metrics(Sales.revenue).dimensions(Sales.country).limit(10)
        builder = DuckDBSQLBuilder(DuckDBDialect())
        sql, _params = builder.build_select_with_params(query)
        assert sql == DUCKDB_SALES + "\nLIMIT 10"

    def test_full_query_with_where_order_limit(self):
        """Full query with WHERE + ORDER BY + LIMIT produces correct multi-line SQL."""
        query = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .where((Sales.country == "US") & (Sales.revenue > 10))
            .order_by(Sales.revenue.desc())
            .limit(10)
        )
        builder = DuckDBSQLBuilder(DuckDBDialect())
        sql, params = builder.build_select_with_params(query)
        expected = (
            'SELECT "revenue" AS "revenue", "country" AS "country"\n'
            "FROM semantic_view('sales_view', "
            "dimensions := ['country'], metrics := ['revenue'], where_clause := ?)\n"
            'WHERE "revenue" > ?\n'
            'ORDER BY "revenue" DESC\n'
            "LIMIT 10"
        )
        assert sql == expected
        assert params == ["\"country\" = 'US'", 10]

    def test_multiple_dimensions_and_metrics(self):
        """Multiple dimensions and metrics are listed correctly."""
        query = (
            Sales.query().metrics(Sales.revenue, Sales.cost).dimensions(Sales.country, Sales.region)
        )
        builder = DuckDBSQLBuilder(DuckDBDialect())
        sql, _params = builder.build_select_with_params(query)
        assert sql == (
            'SELECT "revenue" AS "revenue", "cost" AS "cost", '
            '"country" AS "country", "region" AS "region"\n'
            "FROM semantic_view('sales_view', dimensions := ['country', 'region'], "
            "metrics := ['revenue', 'cost'])"
        )

    def test_build_select_renders_inline(self):
        """
        build_select() shows the bound clause as the SQL string literal it is sent as.

        ``repr()`` would show it with backslash escapes, which is not SQL: the displayed
        statement must be one DuckDB would run.
        """
        query = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .where(Sales.country == "US")
        )
        builder = DuckDBSQLBuilder(DuckDBDialect())
        sql = builder.build_select(query)
        assert sql == (
            'SELECT "revenue" AS "revenue", "country" AS "country"\n'
            "FROM semantic_view('sales_view', dimensions := ['country'], "
            "metrics := ['revenue'], where_clause := '\"country\" = ''US''')"
        )

    def test_dimensions_only_query(self):
        """Dimensions only (no metrics, no facts) produces correct SQL."""
        query = Sales.query().dimensions(Sales.country)
        builder = DuckDBSQLBuilder(DuckDBDialect())
        sql, params = builder.build_select_with_params(query)
        assert sql == (
            'SELECT "country" AS "country"\n'
            "FROM semantic_view('sales_view', dimensions := ['country'])"
        )
        assert params == []


class TestOrderByAnUnselectedField:
    """
    Ordering by a dimension the query does not select is refused on every dialect (D2-4).

    On DuckDB it would have to be requested from ``semantic_view()``, which regroups the
    result by it; the outer ``ORDER BY`` cannot see it otherwise. On Snowflake and Databricks
    an aggregate query cannot order by a column it does not group by. A metric does not group,
    so ordering by an unselected metric stays allowed.
    """

    @pytest.mark.parametrize("dialect", ["snowflake", "databricks", "duckdb"])
    def test_an_unselected_dimension_is_refused(self, dialect: str):
        """The error names the field and says to select it."""
        query = Sales.query().metrics(Sales.revenue).order_by(Sales.country)
        builder = resolve_dialect(dialect).create_builder()
        with pytest.raises(ValueError, match=r"Sales\.country.*\.dimensions\(\)"):
            builder.build_select_with_params(query)

    @pytest.mark.parametrize("dialect", ["snowflake", "databricks", "duckdb"])
    def test_an_unselected_fact_is_refused(self, dialect: str):
        """A fact is row-level, so it is refused like a dimension."""
        query = Sales.query().dimensions(Sales.country).order_by(Sales.unit_price.desc())
        builder = resolve_dialect(dialect).create_builder()
        with pytest.raises(ValueError, match=r"Sales\.unit_price"):
            builder.build_select_with_params(query)

    def test_to_sql_refuses_it_too(self):
        """The display path builds the same statement, so it refuses the same query."""
        query = Sales.query().metrics(Sales.revenue).order_by(Sales.region.desc())
        with pytest.raises(ValueError, match=r"Sales\.region"):
            query.to_sql()

    @pytest.mark.parametrize(
        ("dialect", "order_by"),
        [
            ("snowflake", 'ORDER BY AGG("COST") DESC'),
            ("databricks", "ORDER BY MEASURE(`cost`) DESC"),
            ("duckdb", 'ORDER BY "cost" DESC'),
        ],
    )
    def test_an_unselected_metric_is_allowed(self, dialect: str, order_by: str):
        """The statement orders by the metric's expression without selecting it."""
        query = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .order_by(Sales.cost.desc())
        )
        sql, _params = resolve_dialect(dialect).create_builder().build_select_with_params(query)
        assert sql.split("\n")[-1] == order_by
        assert 'AS "cost"' not in sql
        assert "AS `cost`" not in sql


class TestOrderByASelectedMetric:
    """
    Databricks orders a selected metric by its alias, not by ``MEASURE()``.

    Databricks identifiers are case-insensitive, so the alias `` `revenue` `` shadows the
    measure of the same name and ``ORDER BY MEASURE(`revenue`)`` resolves to the select-list
    column. Measured live in 53-04: ``METRIC_VIEW_INVALID_MEASURE_FUNCTION_INPUT ... got
    "revenue"``. Snowflake's quoted alias ``"revenue"`` and measure ``"REVENUE"`` are
    distinct identifiers, and ``ORDER BY AGG("REVENUE")`` was recorded working beside it.
    A Snowflake measure stored quoted in lower case is the alias's identifier, though, so it
    orders by the alias too; an unselected metric in that position has no alias and raises.
    """

    @pytest.mark.parametrize(
        ("dialect", "order_by"),
        [
            ("snowflake", 'ORDER BY AGG("REVENUE") ASC'),
            ("databricks", "ORDER BY `revenue` ASC"),
            ("duckdb", 'ORDER BY "revenue" ASC'),
        ],
    )
    def test_a_selected_metric(self, dialect: str, order_by: str):
        """The last line of the statement orders by the metric."""
        query = Sales.query().metrics(Sales.revenue).order_by(Sales.revenue)
        sql, _params = resolve_dialect(dialect).create_builder().build_select_with_params(query)
        assert sql.split("\n")[-1] == order_by

    def test_a_snowflake_metric_stored_in_lower_case_orders_by_its_alias(self):
        """
        A quoted lower-case measure is the same identifier as its alias, so AGG() would nest.

        Measured live: ``ORDER BY AGG("revenue")`` beside ``AGG("revenue") AS "revenue"``
        fails with ``002082 ... Aggregate functions cannot be nested``, and ``ORDER BY
        "revenue"`` returns the ordered rows.
        """

        class LowerCase(SemanticView, view="probe_view"):
            revenue = Metric(source="revenue")
            country = Dimension()

        query = (
            LowerCase.query()
            .metrics(LowerCase.revenue)
            .dimensions(LowerCase.country)
            .order_by(LowerCase.revenue.desc())
        )
        sql, _params = SQLBuilder(SnowflakeDialect()).build_select_with_params(query)
        assert sql.split("\n")[-1] == 'ORDER BY "revenue" DESC'

    @pytest.mark.parametrize("dialect", ["snowflake", "databricks"])
    def test_an_unselected_metric_shadowed_by_a_select_alias_raises(self, dialect: str):
        """
        An unselected metric has no alias to order by, and its measure resolves to the alias.

        Measured live on both warehouses: Snowflake ``010274 ... Invalid metric expression
        'AGG(revenue)'`` and Databricks ``METRIC_VIEW_INVALID_MEASURE_FUNCTION_INPUT``.
        """

        class Shadowed(SemanticView, view="probe_view"):
            revenue = Dimension(source="COUNTRY" if dialect == "snowflake" else "country")
            rev = Metric(source="revenue")

        query = Shadowed.query().dimensions(Shadowed.revenue).order_by(Shadowed.rev.desc())
        builder = resolve_dialect(dialect).create_builder()
        with pytest.raises(ValueError, match=r"Shadowed\.rev.*Shadowed\.revenue"):
            builder.build_select_with_params(query)

    def test_the_shadow_check_follows_each_dialects_identifier_case_rules(self):
        """Databricks ignores case in backtick identifiers; Snowflake's quoted ones keep it."""

        class Cased(SemanticView, view="probe_view"):
            Revenue = Dimension(source="country")
            rev = Metric(source="revenue")

        query = Cased.query().dimensions(Cased.Revenue).order_by(Cased.rev.desc())
        with pytest.raises(ValueError, match=r"Cased\.rev"):
            SQLBuilder(DatabricksDialect()).build_select_with_params(query)
        sql, _params = SQLBuilder(SnowflakeDialect()).build_select_with_params(query)
        assert sql.split("\n")[-1] == 'ORDER BY AGG("revenue") DESC'


class TestOrderByAShadowedColumn:
    """
    A field whose source column is spelled like another field's select alias orders by its own.

    Every select item carries an alias, and ORDER BY resolves a bare name to a select alias
    before a source column. So ``nation_code AS nation`` captures ``ORDER BY nation`` meant
    for ``nation AS region``, and the rows come back sorted by the wrong column. (Measured on
    DuckDB: ``SELECT "revenue" AS "total", "gross" AS "revenue" ... ORDER BY "revenue"``
    sorts by ``gross``.)
    """

    @pytest.mark.parametrize(
        ("dialect", "order_by"),
        [
            ("snowflake", 'ORDER BY "region" ASC'),
            ("databricks", "ORDER BY `region` ASC"),
            ("duckdb", 'ORDER BY "region" ASC'),
        ],
    )
    def test_a_selected_dimension_orders_by_its_alias(self, dialect: str, order_by: str):
        class Geo(SemanticView, view="geo"):
            region = Dimension(source="nation")
            nation = Dimension(source="nation_code")
            revenue = Metric(source="REVENUE")

        query = (
            Geo.query().metrics(Geo.revenue).dimensions(Geo.region, Geo.nation).order_by(Geo.region)
        )
        sql, _params = resolve_dialect(dialect).create_builder().build_select_with_params(query)
        assert sql.split("\n")[-1] == order_by

    def test_a_selected_duckdb_metric_orders_by_its_alias(self):
        class Money(SemanticView, view="money"):
            total = Metric(source="revenue")
            revenue = Metric(source="gross")

        query = Money.query().metrics(Money.total, Money.revenue).order_by(Money.total.desc())
        sql, _params = DuckDBSQLBuilder(DuckDBDialect()).build_select_with_params(query)
        assert sql.split("\n")[-1] == 'ORDER BY "total" DESC'

    def test_duckdb_matches_a_shadow_whatever_its_case(self):
        """
        DuckDB folds case even inside double quotes, so ``"Territory"`` binds ``"territory"``.

        Measured: ``SELECT "Territory" AS "region", "region" AS "territory" ... ORDER BY
        "Territory"`` sorts by ``territory``.
        """

        class Geo(SemanticView, view="geo"):
            region = Dimension(source="Territory")
            territory = Dimension(source="region")

        query = Geo.query().dimensions(Geo.region, Geo.territory).order_by(Geo.region)
        sql, _params = DuckDBSQLBuilder(DuckDBDialect()).build_select_with_params(query)
        assert sql.split("\n")[-1] == 'ORDER BY "region" ASC'

    def test_an_unselected_shadowed_duckdb_metric_raises(self):
        """
        With no alias of its own to order by, the metric cannot be named at all.

        (An unselected dimension never gets this far: ``order_by()`` refuses it first.)
        """

        class Money(SemanticView, view="money"):
            total = Metric(source="revenue")
            revenue = Metric(source="gross")

        query = Money.query().metrics(Money.revenue).order_by(Money.total)
        with pytest.raises(ValueError, match=r"Money\.total.*column 'revenue'.*Money\.revenue"):
            DuckDBSQLBuilder(DuckDBDialect()).build_select_with_params(query)

    @pytest.mark.parametrize(
        ("dialect", "order_by"),
        [
            ("snowflake", 'ORDER BY "COUNTRY" ASC'),
            ("databricks", "ORDER BY `country` ASC"),
            ("duckdb", 'ORDER BY "country" ASC'),
        ],
    )
    def test_an_unshadowed_dimension_still_orders_by_its_column(self, dialect: str, order_by: str):
        """Only a shadowed column changes, so no recorded statement does."""
        query = (
            Sales.query().metrics(Sales.revenue).dimensions(Sales.country).order_by(Sales.country)
        )
        sql, _params = resolve_dialect(dialect).create_builder().build_select_with_params(query)
        assert sql.split("\n")[-1] == order_by


def outside_string_literals(sql: str) -> str:
    """
    Replace every single-quoted SQL string literal in ``sql`` with ``<literal>``.

    Reduces an injection assertion to the property that actually matters: what the parser
    would see as *SQL* once the literals are removed. Counting substrings does not work,
    because a payload that stayed safely inside its literal still contains the words
    ``FROM`` and ``read_csv``.

    The pattern consumes a doubled ``''`` as literal content, which is exactly the escape
    :func:`semolina.engines.sql.sql_str_literal` produces -- so an unescaped quote leaves
    its payload outside a ``<literal>`` marker and the assertion catches it.

    Args:
        sql: A SQL string.

    Returns:
        The same string with each literal collapsed to a marker.
    """
    return re.sub(r"'(?:[^']|'')*'", "<literal>", sql)


def from_line(sql: str) -> str:
    """
    Return the ``FROM`` line of a built statement.

    Args:
        sql: A statement built one clause per line.

    Returns:
        The line that starts with ``FROM``.
    """
    return next(line for line in sql.split("\n") if line.startswith("FROM "))


class TestDuckDBWhereClauseValueTypes:
    """
    Dimension and fact filter values that DuckDB used to bind still compile.

    Before Phase 53 every DuckDB filter value was bound. A dimension or fact filter now goes
    into ``where_clause``, a string the extension parses, so its values are rendered as
    literals instead. The value types a bind accepted must still reach that string.
    """

    @staticmethod
    def _where_clause_for(value: object) -> str:
        builder = DuckDBSQLBuilder(DuckDBDialect())
        sql, params = builder.build_select_with_params(
            Sales.query().dimensions(Sales.country).where(Sales.country == value)
        )
        assert sql.startswith('SELECT "country" AS "country"\nFROM semantic_view(')
        assert len(params) == 1, "the where_clause string is the call's only bound parameter"
        return params[0]

    def test_time_value(self):
        assert self._where_clause_for(datetime.time(9, 30)) == "\"country\" = TIME '09:30:00'"

    def test_uuid_value(self):
        value = uuid.UUID("12345678-1234-5678-1234-567812345678")
        assert self._where_clause_for(value) == (
            "\"country\" = '12345678-1234-5678-1234-567812345678'"
        )

    def test_numpy_int64_value(self):
        np = pytest.importorskip("numpy")
        assert self._where_clause_for(np.int64(5)) == '"country" = 5'

    def test_aware_datetime_value_is_a_timestamptz(self):
        value = datetime.datetime(
            2024, 1, 1, 14, tzinfo=datetime.timezone(datetime.timedelta(hours=2))
        )
        assert self._where_clause_for(value) == ("\"country\" = TIMESTAMPTZ '2024-01-01T12:00:00Z'")

    def test_naive_datetime_value_stays_a_timestamp(self):
        value = datetime.datetime(2024, 1, 1, 12)
        assert self._where_clause_for(value) == "\"country\" = TIMESTAMP '2024-01-01T12:00:00'"

    def test_a_zoneinfo_time_has_no_offset_and_raises(self):
        """
        ``ZoneInfo`` cannot resolve an offset without a date, so ``utcoffset()`` is ``None``.

        Rendered, it would be ``TIMETZ '10:00:00'``, which DuckDB reads at UTC.
        """
        zoneinfo = pytest.importorskip("zoneinfo")
        try:
            paris = zoneinfo.ZoneInfo("Europe/Paris")
        except zoneinfo.ZoneInfoNotFoundError:
            pytest.skip("no time zone database")
        with pytest.raises(ValueError, match="time zone"):
            self._where_clause_for(datetime.time(10, 0, tzinfo=paris))

    def test_aware_time_value_is_a_timetz(self):
        value = datetime.time(9, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))
        assert self._where_clause_for(value) == "\"country\" = TIMETZ '09:30:00+02:00'"

    @pytest.mark.parametrize(
        ("value", "literal"),
        [
            (datetime.timedelta(days=2), "INTERVAL '2 days 0 seconds 0 microseconds'"),
            (datetime.timedelta(seconds=-1), "INTERVAL '-1 days 86399 seconds 0 microseconds'"),
            (
                datetime.timedelta(hours=1, microseconds=5),
                "INTERVAL '0 days 3600 seconds 5 microseconds'",
            ),
        ],
        ids=["days", "negative", "micros"],
    )
    def test_timedelta_value_is_an_interval(self, value: datetime.timedelta, literal: str):
        assert self._where_clause_for(value) == f'"country" = {literal}'

    @pytest.mark.parametrize("value", [b"A'\\", bytearray(b"A'\\")], ids=["bytes", "bytearray"])
    def test_bytes_value_is_a_blob_of_hex_escapes(self, value: bytes):
        r"""Every byte is a ``\xHH`` escape, so neither a quote nor a backslash survives raw."""
        assert self._where_clause_for(value) == "\"country\" = '\\x41\\x27\\x5C'::BLOB"

    def test_int_enum_value(self):
        class Tier(enum.IntEnum):
            GOLD = 1

        assert self._where_clause_for(Tier.GOLD) == '"country" = 1'


class TypedLiterals(SemanticView, view="typed_literals"):
    """One dimension per typed literal form DuckDB's ``where_clause`` renders."""

    total = Metric[int]()
    day = Dimension[datetime.date]()
    stamp = Dimension[datetime.datetime]()
    stamp_tz = Dimension[datetime.datetime]()
    clock = Dimension[datetime.time]()
    clock_tz = Dimension[datetime.time]()
    span = Dimension[datetime.timedelta]()
    payload = Dimension[bytes]()
    price = Dimension[Decimal]()


TYPED_LITERAL_ROWS = (
    # The row each filter must find.
    "(1, DATE '2024-01-31', TIMESTAMP '2024-01-31 10:05:00', "
    "TIMESTAMPTZ '2024-01-31 10:05:00+00', TIME '09:30:00', TIMETZ '09:30:00+02:00', "
    "INTERVAL '2 days', '\\x41\\x27\\x5C'::BLOB, 1500.50, 7), "
    # Unrelated.
    "(2, DATE '2023-01-01', TIMESTAMP '2023-01-01 00:00:00', "
    "TIMESTAMPTZ '2023-01-01 00:00:00+00', TIME '01:00:00', TIMETZ '01:00:00+00:00', "
    "INTERVAL '1 hour', '\\x00'::BLOB, 1.00, 1), "
    # A near miss in every column: one second, microsecond, byte or cent off, or the wall
    # time a dropped offset would read (12:05 UTC, 09:30 UTC).
    "(3, DATE '2024-01-30', TIMESTAMP '2024-01-31 10:05:01', "
    "TIMESTAMPTZ '2024-01-31 12:05:00+00', TIME '09:30:01', TIMETZ '09:30:00+00:00', "
    "INTERVAL '2 days 1 microsecond', '\\x41\\x27'::BLOB, 1500.49, 100)"
)


@pytest.fixture(scope="module")
def typed_literals_db(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """
    Write a file-backed DuckDB database whose ``typed_literals`` view has one column per type.

    No source column is named ``blob``: the extension mishandles a column of that name, which
    is its bug and not the one under test here.

    Returns:
        The database file's path.
    """
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("adbc_driver_duckdb")
    from duckdb_extension import load_semantic_views

    db_path = tmp_path_factory.mktemp("typed_literals") / "typed.db"
    conn = duckdb.connect(database=str(db_path))
    try:
        load_semantic_views(conn)
        conn.execute(
            "CREATE TABLE typed (id INTEGER, day DATE, stamp TIMESTAMP, stamp_tz TIMESTAMPTZ, "
            "clock TIME, clock_tz TIMETZ, span INTERVAL, payload BLOB, price DECIMAL(10, 2), "
            "amount INTEGER)"
        )
        conn.execute(f"INSERT INTO typed VALUES {TYPED_LITERAL_ROWS}")
        conn.execute(
            "CREATE SEMANTIC VIEW typed_literals AS "
            "TABLES (t AS typed PRIMARY KEY (id)) "
            "DIMENSIONS (t.day AS day, t.stamp AS stamp, t.stamp_tz AS stamp_tz, "
            "t.clock AS clock, t.clock_tz AS clock_tz, t.span AS span, "
            "t.payload AS payload, t.price AS price) "
            "METRICS (t.total AS SUM(t.amount))"
        )
    finally:
        conn.close()
    return db_path


class TestDuckDBWhereClauseValueTypesRunLive:
    """
    Each typed literal the DuckDB ``where_clause`` renders is accepted by the extension.

    ``TestDuckDBWhereClauseValueTypes`` pins the rendered text, which was written from the
    same reasoning as the renderer. Here every form runs through a real ``semantic_view()``
    call, so a form the extension parsed differently (or refused) would fail: the filter
    must select exactly the one matching row, and not its near miss.
    """

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param(TypedLiterals.day, datetime.date(2024, 1, 31), id="date"),
            pytest.param(
                TypedLiterals.stamp, datetime.datetime(2024, 1, 31, 10, 5), id="timestamp"
            ),
            pytest.param(
                TypedLiterals.stamp_tz,
                datetime.datetime(
                    2024, 1, 31, 12, 5, tzinfo=datetime.timezone(datetime.timedelta(hours=2))
                ),
                id="timestamptz",
            ),
            pytest.param(TypedLiterals.clock, datetime.time(9, 30), id="time"),
            pytest.param(
                TypedLiterals.clock_tz,
                datetime.time(9, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=2))),
                id="timetz",
            ),
            pytest.param(TypedLiterals.span, datetime.timedelta(days=2), id="interval"),
            pytest.param(TypedLiterals.payload, b"A'\\", id="blob"),
            pytest.param(TypedLiterals.price, Decimal("1500.50"), id="decimal"),
        ],
    )
    def test_the_filter_selects_the_matching_row(
        self, typed_literals_db: Any, field: Any, value: object
    ) -> None:
        from adbc_poolhouse import DuckDBConfig

        import semolina
        from semolina.config import create_engine

        engine = create_engine(DuckDBConfig(database=str(typed_literals_db), pool_size=1))
        semolina.register("default", engine)
        try:
            query = TypedLiterals.query().metrics(TypedLiterals.total).where(field == value)
            with query.execute() as cursor:
                rows = cursor.fetchall()
        finally:
            semolina.unregister("default")
            engine.dispose()

        assert [tuple(int(cell) for cell in row) for row in rows] == [(7,)]


class TestDuckDBSemanticViewStringLiterals:
    """
    Nothing interpolated into ``semantic_view(...)`` can leave its string literal.

    ``semantic_view()`` takes its view name and every field name as SQL *string literals*,
    not as identifiers, so they cannot be parameter-bound and are interpolated. Field names
    were user-typed text until ``semolina codegen --check`` began feeding the warehouse
    catalogue's own answer back in, which makes a quote in a catalogue name reach a
    statement rather than only a model file.
    """

    def test_a_quote_in_a_source_override_cannot_open_a_new_clause(self):
        class Injected(SemanticView, view="v"):
            country = Dimension[str](source="x') FROM read_csv('/etc/passwd') --")

        builder = DuckDBSQLBuilder(DuckDBDialect())
        sql, params = builder.build_select_with_params(
            Injected.query().dimensions(Injected.country)
        )

        assert sql == (
            'SELECT "x\') FROM read_csv(\'/etc/passwd\') --" AS "country"\n'
            "FROM semantic_view('v', "
            "dimensions := ['x'') FROM read_csv(''/etc/passwd'') --'])"
        )
        assert params == []
        # The payload contributed no SQL of its own: strip the literals and the call is the
        # same shape it would be for a well-behaved field name. (The projection line holds
        # the name as a double-quoted identifier, which a single quote cannot leave.)
        assert outside_string_literals(from_line(sql)) == (
            "FROM semantic_view(<literal>, dimensions := [<literal>])"
        )

    def test_a_quote_in_a_view_name_cannot_open_a_new_clause(self):
        class Injected(SemanticView, view="v', dimensions := ['x'), (SELECT 1) --"):
            country = Dimension[str]()

        builder = DuckDBSQLBuilder(DuckDBDialect())
        sql, _params = builder.build_select_with_params(
            Injected.query().dimensions(Injected.country)
        )

        assert sql == (
            'SELECT "country" AS "country"\n'
            "FROM semantic_view('v'', dimensions := [''x''), (SELECT 1) --', "
            "dimensions := ['country'])"
        )
        assert outside_string_literals(sql) == (
            'SELECT "country" AS "country"\n'
            "FROM semantic_view(<literal>, dimensions := [<literal>])"
        )

    def test_a_quote_in_a_metric_name_is_doubled(self):
        class Injected(SemanticView, view="v"):
            revenue = Metric[int](source="o'brien")
            country = Dimension[str]()

        builder = DuckDBSQLBuilder(DuckDBDialect())
        sql, _params = builder.build_select_with_params(
            Injected.query().metrics(Injected.revenue).dimensions(Injected.country)
        )

        assert sql == (
            'SELECT "o\'brien" AS "revenue", "country" AS "country"\n'
            "FROM semantic_view('v', dimensions := ['country'], metrics := ['o''brien'])"
        )

    def test_a_quote_in_a_fact_name_is_doubled(self):
        class Injected(SemanticView, view="v"):
            unit_price = Fact[int](source="o'brien")

        builder = DuckDBSQLBuilder(DuckDBDialect())
        sql, _params = builder.build_select_with_params(
            Injected.query().dimensions(Injected.unit_price)
        )

        assert sql == (
            'SELECT DISTINCT "o\'brien" AS "unit_price"\n'
            "FROM semantic_view('v', facts := ['o''brien'])"
        )

    def test_a_double_quote_in_a_projected_name_is_doubled(self):
        """The outer projection quotes each name as an identifier, so a ``"`` cannot end it."""

        class Injected(SemanticView, view="v"):
            country = Dimension[str](source="x\" FROM read_csv('/etc/passwd') --")

        builder = DuckDBSQLBuilder(DuckDBDialect())
        sql, _params = builder.build_select_with_params(
            Injected.query().dimensions(Injected.country)
        )

        assert sql.split("\n")[0] == ('SELECT "x"" FROM read_csv(\'/etc/passwd\') --" AS "country"')
