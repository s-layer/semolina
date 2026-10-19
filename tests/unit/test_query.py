"""
Tests for the query builder, through the surface a user holds.

Every query here starts from ``Model.query()`` and every assertion is on something a caller
can observe: the SQL ``to_sql()`` renders, query equality, the error a misuse raises, the
``repr``, or the rows an execution returns. Nothing reads the query's private fields, so the
builder's internal representation can change without these tests noticing, and a change a
user would notice cannot pass them.

SQL is compared whole. A substring check such as ``'AGG("REVENUE")' in sql`` passes against a
statement that selects the column twice, orders the clauses wrongly, or carries a stray one.

Snowflake is ``to_sql()``'s default dialect, so that is the dialect most expectations here are
written in; per-dialect rendering is covered in ``test_sql.py``.

Execution tests run against the in-memory DuckDB fixtures from ``tests/conftest.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from semolina import Dimension, Fact, Metric, Row, SemanticView
from semolina.cursor import SemolinaCursor
from semolina.dialect import Dialect
from semolina.fields import NullsOrdering
from semolina.filters import Exact, Gt

if TYPE_CHECKING:
    from collections.abc import Callable

    from semolina.engines.duckdb import DuckDBEngine


class Sales(SemanticView, view="sales_view"):
    """Test model for query tests."""

    revenue = Metric()
    cost = Metric()
    country = Dimension()
    region = Dimension()
    unit_price = Fact()


def _create_duckdb_engine(
    *,
    view_name: str = "sales_view",
    table_data: list[tuple[int, int, int, str, str, int]] | None = None,
) -> Any:
    """
    Build an in-memory DuckDB Engine with a semantic_view for testing.

    ``create_engine(DuckDBConfig(...))`` owns the ADBC pool and attaches the
    ``_load_semantic_views`` connect listener. A second connect listener seeds the test data
    so it persists across ADBC clone connections. The caller disposes the engine.
    """
    pytest.importorskip("adbc_driver_duckdb")
    from adbc_poolhouse import DuckDBConfig
    from sqlalchemy import event

    from semolina.config import create_engine

    engine = create_engine(DuckDBConfig(database=":memory:", pool_size=1))

    def _setup_data(dbapi_conn: Any, _connection_record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sales_data (
                id INTEGER, revenue INTEGER, cost INTEGER,
                country VARCHAR, region VARCHAR, unit_price INTEGER
            )
        """)
        cur.execute("DELETE FROM sales_data")
        if table_data is not None:
            for row in table_data:
                cur.execute(
                    "INSERT INTO sales_data VALUES (?, ?, ?, ?, ?, ?)",
                    row,
                )
        cur.execute(f"""
            CREATE OR REPLACE SEMANTIC VIEW {view_name} AS
            TABLES (s AS sales_data PRIMARY KEY (id))
            DIMENSIONS (
                s.country AS country,
                s.region AS region,
                s.unit_price AS unit_price
            )
            METRICS (
                s.revenue AS SUM(s.revenue),
                s.cost AS SUM(s.cost)
            )
        """)
        cur.close()
        dbapi_conn.commit()

    event.listen(engine._pool, "connect", _setup_data, once=True)
    return engine


class TestSelectingMetrics:
    """``.metrics()`` selects metrics, and only metrics."""

    def test_metrics_single_field(self):
        """One metric is selected, wrapped in the dialect's aggregate."""
        q = Sales.query().metrics(Sales.revenue)
        assert q.to_sql() == 'SELECT AGG("REVENUE") AS "revenue"\nFROM "SALES_VIEW"'

    def test_metrics_multiple_fields(self):
        """Several metrics are selected in the order given."""
        q = Sales.query().metrics(Sales.revenue, Sales.cost)
        assert (
            q.to_sql()
            == 'SELECT AGG("REVENUE") AS "revenue", AGG("COST") AS "cost"\nFROM "SALES_VIEW"'
        )

    def test_metrics_accumulates(self):
        """Two ``.metrics()`` calls accumulate: the result equals one call with both."""
        q = Sales.query().metrics(Sales.revenue).metrics(Sales.cost)
        assert q == Sales.query().metrics(Sales.revenue, Sales.cost)
        assert (
            q.to_sql()
            == 'SELECT AGG("REVENUE") AS "revenue", AGG("COST") AS "cost"\nFROM "SALES_VIEW"'
        )

    def test_metrics_rejects_dimension(self):
        """A dimension passed to ``.metrics()`` is refused, pointing at ``.dimensions()``."""
        with pytest.raises(TypeError, match=r"Did you mean \.dimensions\(\)\?"):
            Sales.query().metrics(Sales.country)  # pyright: ignore[reportArgumentType]

    def test_metrics_rejects_fact(self):
        """A fact passed to ``.metrics()`` is refused the same way."""
        with pytest.raises(TypeError, match=r"Did you mean \.dimensions\(\)\?"):
            Sales.query().metrics(Sales.unit_price)  # pyright: ignore[reportArgumentType]

    def test_metrics_empty_raises_error(self):
        """An empty call is refused rather than read as a no-op."""
        with pytest.raises(ValueError, match="At least one metric"):
            Sales.query().metrics()

    @pytest.mark.parametrize(
        "build",
        [
            lambda: Sales.query().metrics(Sales.revenue, Sales.revenue),
            lambda: Sales.query().metrics(Sales.revenue).metrics(Sales.revenue),
            lambda: Sales.query().metrics(Sales.revenue, Sales.cost).metrics(Sales.revenue),
        ],
        ids=["one-call", "two-calls", "among-others"],
    )
    def test_the_same_metric_twice_is_refused(self, build: Callable[[], object]):
        """
        A metric selected twice is refused where it is selected.

        Both copies would come back under one column name, and a result row keyed by column
        name can hold only one of them.
        """
        with pytest.raises(ValueError, match="'revenue' is selected twice"):
            build()


class TestSelectingDimensions:
    """``.dimensions()`` selects dimensions and facts, and groups by them."""

    def test_dimensions_single_dimension(self):
        """One dimension is selected and grouped by."""
        q = Sales.query().dimensions(Sales.country)
        assert q.to_sql() == 'SELECT "COUNTRY" AS "country"\nFROM "SALES_VIEW"\nGROUP BY ALL'

    def test_dimensions_multiple_dimensions(self):
        """Several dimensions are selected in the order given."""
        q = Sales.query().dimensions(Sales.country, Sales.region)
        assert (
            q.to_sql() == 'SELECT "COUNTRY" AS "country", "REGION" AS "region"\n'
            'FROM "SALES_VIEW"\n'
            "GROUP BY ALL"
        )

    def test_dimensions_accepts_fact(self):
        """A fact is selected through ``.dimensions()``."""
        q = Sales.query().dimensions(Sales.unit_price)
        assert q.to_sql() == 'SELECT "UNIT_PRICE" AS "unit_price"\nFROM "SALES_VIEW"\nGROUP BY ALL'

    @pytest.mark.parametrize(
        ("build", "expected"),
        [
            (lambda: Sales.query().dimensions(Sales.unit_price), [(5,), (10,), (20,)]),
            (
                lambda: Sales.query().dimensions(Sales.country, Sales.unit_price),
                [("CA", 20), ("US", 5), ("US", 10)],
            ),
        ],
        ids=["fact", "dimension-and-fact"],
    )
    def test_a_fact_query_groups_on_duckdb_as_on_the_warehouses(
        self, build: Callable[[], Any], expected: list[tuple[object, ...]]
    ):
        """
        A query with no metric returns each distinct row once on DuckDB too.

        Snowflake and Databricks group every query (``GROUP BY ALL`` above), so two source
        rows with the same fact value come back as one. DuckDB's ``semantic_view()`` returns
        facts row by row, so the same query must not keep the duplicate there either.
        """
        pytest.importorskip("adbc_driver_duckdb")
        from adbc_poolhouse import DuckDBConfig, close_pool
        from sqlalchemy import event

        from semolina.config import create_engine

        engine = create_engine(DuckDBConfig(database=":memory:", pool_size=1))

        def _setup(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("CREATE TABLE sales_data (id INTEGER, country VARCHAR, unit_price INTEGER)")
            cur.execute(
                "INSERT INTO sales_data VALUES (1, 'US', 10), (2, 'CA', 20), (3, 'US', 5),"
                " (4, 'US', 10)"
            )
            cur.execute(
                "CREATE SEMANTIC VIEW sales_view AS TABLES (s AS sales_data PRIMARY KEY (id))"
                " FACTS (s.unit_price AS unit_price) DIMENSIONS (s.country AS country)"
                " METRICS (s.revenue AS COUNT(*))"
            )
            cur.close()
            dbapi_conn.commit()

        event.listen(engine._pool, "connect", _setup, once=True)
        try:
            rows = engine.execute(build()).fetchall()
        finally:
            close_pool(engine._pool)
        assert sorted(rows) == expected

    def test_dimensions_accumulates(self):
        """Two ``.dimensions()`` calls accumulate: the result equals one call with both."""
        q = Sales.query().dimensions(Sales.country).dimensions(Sales.region)
        assert q == Sales.query().dimensions(Sales.country, Sales.region)

    def test_metrics_come_before_dimensions(self):
        """The select list is metrics then dimensions, whichever was called first."""
        q = Sales.query().dimensions(Sales.country).metrics(Sales.revenue)
        assert (
            q.to_sql() == 'SELECT AGG("REVENUE") AS "revenue", "COUNTRY" AS "country"\n'
            'FROM "SALES_VIEW"\n'
            "GROUP BY ALL"
        )

    def test_dimensions_rejects_metric(self):
        """A metric passed to ``.dimensions()`` is refused, pointing at ``.metrics()``."""
        with pytest.raises(TypeError, match=r"Did you mean \.metrics\(\)\?"):
            Sales.query().dimensions(Sales.revenue)  # pyright: ignore[reportArgumentType]

    def test_dimensions_empty_raises_error(self):
        """An empty call is refused rather than read as a no-op."""
        with pytest.raises(ValueError, match="At least one dimension"):
            Sales.query().dimensions()

    @pytest.mark.parametrize(
        ("build", "name"),
        [
            (lambda: Sales.query().dimensions(Sales.country, Sales.country), "country"),
            (lambda: Sales.query().dimensions(Sales.country).dimensions(Sales.country), "country"),
            (lambda: Sales.query().dimensions(Sales.unit_price, Sales.unit_price), "unit_price"),
        ],
        ids=["one-call", "two-calls", "fact"],
    )
    def test_the_same_dimension_twice_is_refused(self, build: Callable[[], object], name: str):
        """A dimension or fact selected twice is refused the same way a metric is."""
        with pytest.raises(ValueError, match=f"'{name}' is selected twice"):
            build()


class TestFiltering:
    """``.where()`` takes predicates, ANDs them together, and ignores ``None``."""

    def test_filter_single_predicate(self):
        """One predicate becomes the WHERE clause."""
        q = Sales.query().metrics(Sales.revenue).where(Sales.country == "US")
        assert (
            q.to_sql()
            == 'SELECT AGG("REVENUE") AS "revenue"\nFROM "SALES_VIEW"\nWHERE "COUNTRY" = \'US\''
        )

    def test_filter_accepts_a_predicate_built_directly(self):
        """A lookup built by hand filters exactly as the field operator does."""
        by_operator = Sales.query().metrics(Sales.revenue).where(Sales.country == "US")
        by_hand = Sales.query().metrics(Sales.revenue).where(Exact("country", "US"))
        assert by_hand.to_sql() == by_operator.to_sql()

    def test_filter_combines_with_and(self):
        """Two ``.where()`` calls AND together, in call order."""
        q = (
            Sales.query()
            .metrics(Sales.revenue)
            .where(Sales.country == "US")
            .where(Sales.region == "West")
        )
        assert q.to_sql() == (
            'SELECT AGG("REVENUE") AS "revenue"\nFROM "SALES_VIEW"\n'
            "WHERE (\"COUNTRY\" = 'US' AND \"REGION\" = 'West')"
        )

    def test_a_metric_filter_applies_after_aggregation(self):
        """A metric condition is ``HAVING`` on the wrapped metric, not ``WHERE`` (D4)."""
        q = (
            Sales.query()
            .metrics(Sales.revenue)
            .where(Sales.country == "US")
            .where(Sales.revenue > 1000)
        )
        assert q.to_sql() == (
            'SELECT AGG("REVENUE") AS "revenue"\nFROM "SALES_VIEW"\n'
            'WHERE "COUNTRY" = \'US\'\nHAVING AGG("REVENUE") > 1000'
        )

    def test_filter_varargs_and_together(self):
        """Several predicates in one call AND together, the same as separate calls."""
        one_call = (
            Sales.query().metrics(Sales.revenue).where(Sales.country == "US", Gt("revenue", 1000))
        )
        two_calls = (
            Sales.query()
            .metrics(Sales.revenue)
            .where(Sales.country == "US")
            .where(Gt("revenue", 1000))
        )
        assert one_call.to_sql() == two_calls.to_sql()

    def test_filter_composed_operators(self):
        """An OR tree is rendered as one parenthesised condition."""
        q = (
            Sales.query()
            .metrics(Sales.revenue)
            .where((Sales.country == "US") | (Sales.country == "CA"))
        )
        assert q.to_sql() == (
            'SELECT AGG("REVENUE") AS "revenue"\nFROM "SALES_VIEW"\n'
            "WHERE (\"COUNTRY\" = 'US' OR \"COUNTRY\" = 'CA')"
        )

    def test_filter_varargs_with_none(self):
        """``None`` among the predicates is skipped, not rendered."""
        q = (
            Sales.query()
            .metrics(Sales.revenue)
            .where(Sales.country == "US", None, Sales.revenue > 1000)
        )
        assert q == Sales.query().metrics(Sales.revenue).where(
            Sales.country == "US", Sales.revenue > 1000
        )

    @pytest.mark.parametrize(
        "args", [(None,), (), (None, None, None)], ids=["none", "empty", "nones"]
    )
    def test_filter_with_nothing_is_the_same_query(self, args: tuple[None, ...]):
        """``where(None)``, ``where()`` and all-``None`` return the very same query."""
        q = Sales.query().metrics(Sales.revenue)
        assert q.where(*args) is q

    @pytest.mark.parametrize(
        ("condition", "named"),
        [
            ("country = 'US'", "str"),
            (Sales.country, "Dimension"),
            (True, "bool"),
        ],
        ids=["sql-string", "bare-field", "bool"],
    )
    def test_filter_refuses_a_non_predicate_at_the_call(self, condition: Any, named: str):
        """
        ``.where()`` raises ``TypeError`` itself, naming what it was given.

        Before phase 54 it accepted the value and the query failed only when SQL was built,
        far from the line that caused it, or, for a lone string, not until execution.
        """
        query = Sales.query().metrics(Sales.revenue)
        with pytest.raises(TypeError, match=rf"where\(\).*{named}"):
            query.where(condition)

    def test_filter_refuses_a_non_predicate_among_predicates(self):
        """A bad value is refused wherever it sits among the arguments."""
        query = Sales.query().metrics(Sales.revenue)
        with pytest.raises(TypeError, match=r"where\(\).*str"):
            query.where(Sales.country == "US", "revenue > 10")  # pyright: ignore[reportArgumentType]


class TestOrdering:
    """``.order_by()`` takes fields or ``asc()``/``desc()`` terms and accumulates."""

    def test_order_by_bare_field_is_ascending(self):
        """A bare field sorts ascending."""
        q = Sales.query().metrics(Sales.revenue).order_by(Sales.revenue)
        assert (
            q.to_sql()
            == 'SELECT AGG("REVENUE") AS "revenue"\nFROM "SALES_VIEW"\nORDER BY AGG("REVENUE") ASC'
        )

    def test_order_by_bare_field_equals_explicit_asc(self):
        """``order_by(field)`` renders exactly as ``order_by(field.asc())``."""
        bare = Sales.query().metrics(Sales.revenue).order_by(Sales.revenue)
        explicit = Sales.query().metrics(Sales.revenue).order_by(Sales.revenue.asc())
        assert bare.to_sql() == explicit.to_sql()

    def test_order_by_descending(self):
        """``desc()`` sorts descending."""
        q = Sales.query().metrics(Sales.revenue).order_by(Sales.revenue.desc())
        assert (
            q.to_sql()
            == 'SELECT AGG("REVENUE") AS "revenue"\nFROM "SALES_VIEW"\nORDER BY AGG("REVENUE") DESC'
        )

    @pytest.mark.parametrize(
        ("term", "rendered"),
        [
            (Sales.country.desc(NullsOrdering.FIRST), 'ORDER BY "COUNTRY" DESC NULLS FIRST'),
            (Sales.country.asc(NullsOrdering.LAST), 'ORDER BY "COUNTRY" ASC NULLS LAST'),
        ],
        ids=["desc-nulls-first", "asc-nulls-last"],
    )
    def test_order_by_with_nulls_placement(self, term: Any, rendered: str):
        """A NULLS placement is rendered after the direction."""
        q = Sales.query().dimensions(Sales.country).order_by(term)
        assert (
            q.to_sql()
            == f'SELECT "COUNTRY" AS "country"\nFROM "SALES_VIEW"\nGROUP BY ALL\n{rendered}'
        )

    def test_order_by_mixes_fields_and_terms_in_order(self):
        """Mixed terms keep their order, direction and NULLS placement each."""
        q = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country, Sales.region)
            .order_by(Sales.revenue.desc(NullsOrdering.FIRST), Sales.country, Sales.region.asc())
        )
        assert q.to_sql().endswith(
            '\nORDER BY AGG("REVENUE") DESC NULLS FIRST, "COUNTRY" ASC, "REGION" ASC'
        )

    def test_order_by_accumulates(self):
        """Two ``.order_by()`` calls accumulate: the result equals one call with both."""
        q = Sales.query().metrics(Sales.revenue).order_by(Sales.revenue).order_by(Sales.country)
        assert q == Sales.query().metrics(Sales.revenue).order_by(Sales.revenue, Sales.country)

    def test_order_by_rejects_non_field(self):
        """A column name as a string is refused; fields are the only way to name a column."""
        with pytest.raises(TypeError, match="requires Field"):
            Sales.query().order_by("revenue")  # pyright: ignore[reportArgumentType]

    def test_order_by_empty_raises_error(self):
        """An empty call is refused rather than read as a no-op."""
        with pytest.raises(ValueError, match="At least one field"):
            Sales.query().order_by()


class TestLimit:
    """``.limit()`` takes a positive integer."""

    def test_limit_positive_integer(self):
        """The limit is rendered last."""
        q = Sales.query().metrics(Sales.revenue).limit(100)
        assert q.to_sql() == 'SELECT AGG("REVENUE") AS "revenue"\nFROM "SALES_VIEW"\nLIMIT 100'

    @pytest.mark.parametrize("value", [0, -10])
    def test_limit_rejects_non_positive(self, value: int):
        """Zero and negative limits are refused."""
        with pytest.raises(ValueError, match="positive integer"):
            Sales.query().limit(value)

    def test_limit_rejects_float(self):
        """A float is refused rather than truncated."""
        with pytest.raises(TypeError, match="requires int"):
            Sales.query().limit(3.14)  # pyright: ignore[reportArgumentType]

    @pytest.mark.parametrize("value", [True, False])
    def test_limit_rejects_bool(self, value: bool):
        """A bool is an ``int`` to Python and to the type checker, but ``LIMIT True`` is not SQL."""
        with pytest.raises(TypeError, match="requires int"):
            Sales.query().limit(value)


class TestImmutability:
    """Every builder method returns a new query and leaves the one it was called on alone."""

    def test_query_is_frozen(self):
        """Assigning to a query raises."""
        q = Sales.query()
        with pytest.raises(AttributeError):
            q.anything = 1  # pyright: ignore[reportAttributeAccessIssue]

    @pytest.mark.parametrize(
        "derive",
        [
            lambda q: q.metrics(Sales.cost),
            lambda q: q.dimensions(Sales.country),
            lambda q: q.where(Sales.country == "US"),
            lambda q: q.order_by(Sales.revenue),
            lambda q: q.limit(5),
            lambda q: q.using("warehouse"),
        ],
        ids=["metrics", "dimensions", "where", "order_by", "limit", "using"],
    )
    def test_deriving_a_query_leaves_the_original_unchanged(self, derive: Any):
        """The derived query differs; the original still renders and compares as before."""
        base = Sales.query().metrics(Sales.revenue)
        before = base.to_sql()

        derived = derive(base)

        assert derived is not base
        assert derived != base
        assert base.to_sql() == before
        assert base == Sales.query().metrics(Sales.revenue)

    def test_full_method_chain(self):
        """Every builder method composes into one statement, each clause in its place."""
        q = (
            Sales.query()
            .metrics(Sales.revenue, Sales.cost)
            .dimensions(Sales.country, Sales.region)
            .where((Sales.country == "US") | (Sales.country == "CA"))
            .order_by(Sales.revenue.desc(NullsOrdering.FIRST), Sales.country.asc())
            .limit(100)
        )
        assert q.to_sql() == (
            'SELECT AGG("REVENUE") AS "revenue", AGG("COST") AS "cost", "COUNTRY" AS "country", '
            '"REGION" AS "region"\n'
            'FROM "SALES_VIEW"\n'
            "WHERE (\"COUNTRY\" = 'US' OR \"COUNTRY\" = 'CA')\n"
            "GROUP BY ALL\n"
            'ORDER BY AGG("REVENUE") DESC NULLS FIRST, "COUNTRY" ASC\n'
            "LIMIT 100"
        )


class TestValidation:
    """A query that selects nothing cannot be rendered or executed."""

    def test_empty_query_cannot_be_rendered(self):
        """``to_sql()`` on an empty query raises, naming what is missing."""
        with pytest.raises(ValueError, match="at least one metric or dimension"):
            Sales.query().to_sql()

    def test_empty_query_cannot_be_executed(self):
        """``execute()`` raises the same error before looking for an engine."""
        with pytest.raises(ValueError, match="must select at least one metric or dimension"):
            Sales.query().execute()

    def test_an_engine_refuses_an_empty_query_before_checking_out(self, duckdb_pool: Any):
        """
        ``Engine.execute()`` called directly raises the same ``ValueError``.

        Not an ``AssertionError`` from inside the SQL builder, which ``python -O`` would strip
        and turn into an unrelated failure further on. Nothing is checked out of the pool.
        """
        import semolina

        engine = semolina.get_engine()

        with pytest.raises(ValueError, match="must select at least one metric or dimension"):
            engine.execute(Sales.query())

        assert duckdb_pool.checkedout() == 0
        assert duckdb_pool.checkedin() == 0

    @pytest.mark.parametrize(
        "build",
        [
            lambda: Sales.query().metrics(Sales.revenue),
            lambda: Sales.query().dimensions(Sales.country),
            lambda: Sales.query().metrics(Sales.revenue).dimensions(Sales.country),
        ],
        ids=["metrics", "dimensions", "both"],
    )
    def test_a_query_selecting_something_renders(self, build: Any):
        """A metric, a dimension, or both is enough."""
        assert build().to_sql().startswith("SELECT ")


class TestUsing:
    """``.using()`` names the engine a query executes on."""

    def test_using_with_non_string_raises(self):
        """Only an engine name is accepted."""
        q = Sales.query().metrics(Sales.revenue)
        with pytest.raises(TypeError, match="requires engine name string"):
            q.using(123)  # pyright: ignore[reportArgumentType]
        with pytest.raises(TypeError, match="requires engine name string"):
            q.using(None)  # pyright: ignore[reportArgumentType]

    def test_using_can_be_called_anywhere_in_chain(self):
        """Where ``.using()`` sits in the chain makes no difference."""
        first = Sales.query().using("warehouse").metrics(Sales.revenue)
        last = Sales.query().metrics(Sales.revenue).using("warehouse")
        assert first == last

    def test_query_keyword_equals_using(self):
        """``Model.query(using=...)`` is ``Model.query().using(...)``."""
        assert Sales.query(using="warehouse") == Sales.query().using("warehouse")


class TestFieldOwnership:
    """A query built from one model refuses another model's fields."""

    def test_cannot_mix_different_model_fields_in_metrics(self):
        """A metric from another model is refused."""

        class Orders(SemanticView, view="orders"):
            total = Metric()

        with pytest.raises(TypeError, match="different models"):
            Sales.query().metrics(Sales.revenue, Orders.total)

    def test_cannot_mix_different_model_fields_in_dimensions(self):
        """A dimension from another model is refused."""

        class Orders(SemanticView, view="orders"):
            region = Dimension()

        with pytest.raises(TypeError, match="different models"):
            Sales.query().dimensions(Sales.country, Orders.region)


class TestExecute:
    """``.execute()`` resolves an engine and returns a cursor over the aggregated rows."""

    def test_execute_returns_semolina_cursor(self, duckdb_pool: Any):
        """The cursor yields the aggregate: 1000 + 2000 + 500."""
        cursor = Sales.query().metrics(Sales.revenue).execute()

        assert isinstance(cursor, SemolinaCursor)
        rows = cursor.fetchall_rows()
        assert rows == [Row({"revenue": 3500})]
        cursor.close()

    def test_execute_groups_by_the_dimension(self, duckdb_pool: Any):
        """Rows arrive one per group, readable by attribute and by key."""
        cursor = Sales.query().metrics(Sales.revenue).dimensions(Sales.country).execute()
        rows = cursor.fetchall_rows()
        cursor.close()

        assert {row.country: row["revenue"] for row in rows} == {"US": 1500, "CA": 2000}

    @pytest.mark.parametrize(
        ("predicate", "countries"),
        [
            (lambda: Sales.country.startswith("U_"), set[str]()),
            (lambda: Sales.country.iexact("u_"), set[str]()),
            (lambda: Sales.country.like("U_"), {"US"}),
            (lambda: Sales.country.istartswith("u"), {"US"}),
        ],
        ids=["startswith-literal", "iexact-literal", "like-pattern", "istartswith"],
    )
    def test_a_wildcard_in_a_lookup_value_is_matched_literally(
        self, duckdb_pool: Any, predicate: Callable[[], Any], countries: set[str]
    ):
        """
        On the extension, ``startswith("U_")`` finds no ``US``; ``like("U_")`` does.

        The first two match only a literal underscore, which no country has. Unescaped, as
        before phase 54, both matched ``US``.
        """
        cursor = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .where(predicate())
            .execute()
        )
        rows = cursor.fetchall_rows()
        cursor.close()

        assert {row.country for row in rows} == countries

    def test_iterating_the_cursor_yields_the_rows(self, duckdb_pool: Any):
        """``for row in cursor`` yields the same aggregated rows ``fetchall_rows()`` would."""
        cursor = Sales.query().metrics(Sales.revenue).dimensions(Sales.country).execute()
        streamed = {(row.country, row.revenue) for row in cursor}
        cursor.close()

        assert streamed == {("US", 1500), ("CA", 2000)}

    def test_execute_orders_the_rows(self, duckdb_pool: Any):
        """``order_by`` reaches the warehouse: revenue ascending puts US first."""
        cursor = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .order_by(Sales.revenue)
            .execute()
        )
        rows = cursor.fetchall_rows()
        cursor.close()

        assert [(row.country, row.revenue) for row in rows] == [("US", 1500), ("CA", 2000)]

    def test_execute_with_cost_and_limit(self, duckdb_pool: Any):
        """Several metrics come back per group, and a generous limit keeps every group."""
        cursor = (
            Sales.query()
            .metrics(Sales.revenue, Sales.cost)
            .dimensions(Sales.country)
            .limit(10)
            .execute()
        )
        rows = cursor.fetchall_rows()
        cursor.close()

        assert {row.country: (row.revenue, row.cost) for row in rows} == {
            "US": (1500, 150),
            "CA": (2000, 200),
        }

    @pytest.mark.parametrize(
        ("table_data", "expected"),
        [
            ([], []),
            ([(1, 1000, 100, "US", "West", 10)], [Row({"country": "US", "revenue": 1000})]),
        ],
        ids=["empty", "one-row"],
    )
    def test_execute_empty_vs_nonempty(
        self, table_data: list[tuple[int, int, int, str, str, int]], expected: list[Row]
    ):
        """``fetchall_rows()`` is empty for no data, and the aggregated rows otherwise."""
        import semolina

        engine = _create_duckdb_engine(table_data=table_data)
        semolina.register("sized_test", engine)
        try:
            query = (
                Sales.query().using("sized_test").metrics(Sales.revenue).dimensions(Sales.country)
            )
            with query.execute() as cursor:
                assert cursor.fetchall_rows() == expected
        finally:
            semolina.unregister("sized_test")
            engine.dispose()

    def test_execute_no_engine_raises(self):
        """With nothing registered, execution says so."""
        with pytest.raises(ValueError, match="No engine registered"):
            Sales.query().metrics(Sales.revenue).execute()

    def test_execute_wrong_engine_name_raises(self, duckdb_pool: Any):
        """A name nothing is registered under is refused by name."""
        q = Sales.query().metrics(Sales.revenue).using("other")
        with pytest.raises(ValueError, match="No engine registered with name 'other'"):
            q.execute()

    def test_execute_lazy_resolution(self):
        """The engine is resolved at ``execute()``, not when the query is built."""
        import semolina

        q = Sales.query().metrics(Sales.revenue).using("later")

        engine = _create_duckdb_engine(table_data=[(1, 1500, 150, "US", "West", 15)])
        semolina.register("later", engine)
        try:
            with q.execute() as cursor:
                assert cursor.fetchall_rows() == [Row({"revenue": 1500})]
        finally:
            semolina.unregister("later")
            engine.dispose()

    def test_one_query_runs_on_different_engines(self):
        """The same query object, pointed at two engines, returns each engine's data."""
        import semolina

        prod = _create_duckdb_engine(table_data=[(1, 100, 10, "US", "West", 10)])
        test = _create_duckdb_engine(table_data=[(1, 200, 20, "US", "West", 10)])
        semolina.register("prod", prod)
        semolina.register("test", test)
        try:
            base = Sales.query().metrics(Sales.revenue).dimensions(Sales.country)

            with base.using("prod").execute() as cursor:
                prod_rows = cursor.fetchall_rows()
            with base.using("test").execute() as cursor:
                test_rows = cursor.fetchall_rows()

            assert prod_rows == [Row({"country": "US", "revenue": 100})]
            assert test_rows == [Row({"country": "US", "revenue": 200})]
        finally:
            semolina.unregister("prod")
            semolina.unregister("test")
            prod.dispose()
            test.dispose()

    def test_filtering_on_an_unselected_dimension_keeps_the_selected_grain(self, duckdb_pool: Any):
        """
        A filter narrows the rows; it does not change what one row means.

        Revenue by region, filtered to two countries, is one row per region. On Snowflake and
        Databricks the filter is a plain WHERE under GROUP BY ALL. On DuckDB it goes into
        ``semantic_view()``'s ``where_clause``, which applies before aggregation, so country
        is never requested and cannot regroup the result (ALIAS-05).
        """
        cursor = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.region)
            .where((Sales.country == "US") | (Sales.country == "CA"))
            .execute()
        )
        rows = cursor.fetchall_rows()
        cursor.close()

        assert sorted(rows, key=lambda row: row.region) == [
            Row({"region": "East", "revenue": 500}),
            Row({"region": "West", "revenue": 3000}),
        ]

    def test_a_dimension_filter_and_a_metric_filter_compose(self, duckdb_pool: Any):
        """
        The dimension filter applies before aggregation and the metric filter after it.

        US revenue by region is West 1000 and East 500; only West clears 600.
        """
        cursor = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.region)
            .where((Sales.country == "US") & (Sales.revenue > 600))
            .execute()
        )
        rows = cursor.fetchall_rows()
        cursor.close()

        assert rows == [Row({"revenue": 1000, "region": "West"})]

    def test_filtering_on_an_unselected_metric_returns_only_the_selected_columns(
        self, duckdb_pool: Any
    ):
        """Cost is requested to filter on, and is not returned: US cost is 150, CA 200."""
        cursor = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .where(Sales.cost > 150)
            .execute()
        )
        rows = cursor.fetchall_rows()
        cursor.close()

        assert rows == [Row({"revenue": 2000, "country": "CA"})]

    def test_ordering_by_an_unselected_metric_returns_only_the_selected_columns(
        self, duckdb_pool: Any
    ):
        """Cost descending puts CA (200) before US (150), and cost is not a column."""
        cursor = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .order_by(Sales.cost.desc())
            .execute()
        )
        rows = cursor.fetchall_rows()
        cursor.close()

        assert rows == [
            Row({"revenue": 2000, "country": "CA"}),
            Row({"revenue": 1500, "country": "US"}),
        ]

    def test_a_source_override_returns_under_the_field_name(self, duckdb_pool: Any):
        """The member is requested by ``source=``, and the row key is the Python name."""

        class Renamed(SemanticView, view="sales_view"):
            takings = Metric(source="revenue")
            nation = Dimension(source="country")

        cursor = (
            Renamed.query()
            .metrics(Renamed.takings)
            .dimensions(Renamed.nation)
            .where(Renamed.nation == "CA")
            .execute()
        )
        rows = cursor.fetchall_rows()
        cursor.close()

        assert rows == [Row({"takings": 2000, "nation": "CA"})]


class TestModelCentricWorkflow:
    """Integration test demonstrating the complete model-centric workflow."""

    def test_model_centric_workflow_complete(self):
        """
        Demonstrate complete workflow.

        - Model definition with introspection
        - Query via model.query()
        - Field operators for filtering
        - Eager execution with SemolinaCursor
        """
        import semolina

        # 1. Define model
        class SalesWorkflow(SemanticView, view="sales"):
            revenue = Metric()
            users_count = Metric()
            region = Dimension()
            country = Dimension()

        # 2. Introspect fields
        assert {m.name for m in SalesWorkflow.metrics()} == {"revenue", "users_count"}
        assert {d.name for d in SalesWorkflow.dimensions()} == {"region", "country"}

        # 3. Create DuckDB engine with "sales" view name
        engine = _create_duckdb_engine(
            view_name="sales",
            table_data=[
                (1, 1000, 10, "US", "West", 0),
                (2, 2000, 20, "US", "East", 0),
                (3, 1500, 15, "CA", "West", 0),
            ],
        )
        semolina.register("default", engine)

        try:
            # 4. Build query with field operators
            cursor = (
                SalesWorkflow.query()
                .metrics(SalesWorkflow.revenue)
                .dimensions(SalesWorkflow.region)
                .where((SalesWorkflow.region == "West") | (SalesWorkflow.region == "East"))
                .order_by(SalesWorkflow.revenue.desc())
                .limit(1)
                .execute()
            )

            # 5. Verify SemolinaCursor
            assert isinstance(cursor, SemolinaCursor)
            rows = cursor.fetchall_rows()
            cursor.close()

            # 6. The larger region only: West (1000 + 1500) beats East (2000).
            assert [(row.region, row["revenue"]) for row in rows] == [("West", 2500)]
        finally:
            semolina.unregister("default")
            engine.dispose()


class TestQueryRepr:
    """A query's repr says what it will run."""

    def test_bound_query_shows_model(self) -> None:
        """The repr names the model."""
        repr_str = repr(Sales.query())
        assert "<Query" in repr_str
        assert "model=Sales" in repr_str

    def test_query_shows_metrics(self) -> None:
        """The repr lists metric field names."""
        repr_str = repr(Sales.query().metrics(Sales.revenue, Sales.cost))
        assert "metrics=" in repr_str
        assert "'revenue'" in repr_str
        assert "'cost'" in repr_str

    def test_query_shows_dimensions(self) -> None:
        """The repr lists dimension field names."""
        repr_str = repr(Sales.query().dimensions(Sales.country))
        assert "dimensions=" in repr_str
        assert "'country'" in repr_str

    def test_query_shows_limit(self) -> None:
        """The repr shows the limit."""
        assert "limit=10" in repr(Sales.query().metrics(Sales.revenue).limit(10))

    def test_query_shows_where(self) -> None:
        """The repr shows that the query filters."""
        assert "where=" in repr(Sales.query().where(Sales.country == "US"))

    def test_query_shows_order_by(self) -> None:
        """The repr shows the ordering."""
        q = Sales.query().metrics(Sales.revenue).order_by(Sales.revenue.desc())
        assert "order_by=" in repr(q)

    def test_query_shows_using(self) -> None:
        """The repr shows the engine binding."""
        assert "using='warehouse'" in repr(Sales.query(using="warehouse"))

    def test_model_propagates_through_chain(self) -> None:
        """The model is still named after every builder method has been applied."""
        q = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .where(Sales.country == "US")
            .order_by(Sales.revenue.desc())
            .limit(10)
        )
        assert "model=Sales" in repr(q)


class TestQueryShorthand:
    """``Model.query(metrics=..., dimensions=...)`` is the builder chain, spelled once."""

    def test_shorthand_metrics_only(self) -> None:
        """``metrics=`` is ``.metrics()``."""
        assert Sales.query(metrics=[Sales.revenue]) == Sales.query().metrics(Sales.revenue)

    def test_shorthand_dimensions_only(self) -> None:
        """``dimensions=`` is ``.dimensions()``."""
        assert Sales.query(dimensions=[Sales.region]) == Sales.query().dimensions(Sales.region)

    def test_shorthand_equivalent_to_builder(self) -> None:
        """Shorthand and builder chain produce equal queries."""
        q_shorthand = Sales.query(metrics=[Sales.revenue], dimensions=[Sales.region])
        q_builder = Sales.query().metrics(Sales.revenue).dimensions(Sales.region)
        assert q_shorthand == q_builder

    def test_shorthand_multiple_metrics(self) -> None:
        """Several metrics keep their order."""
        assert Sales.query(metrics=[Sales.revenue, Sales.cost]) == Sales.query().metrics(
            Sales.revenue, Sales.cost
        )

    def test_shorthand_with_using(self) -> None:
        """``using=`` combines with the shorthand."""
        assert Sales.query(metrics=[Sales.revenue], using="warehouse") == (
            Sales.query().metrics(Sales.revenue).using("warehouse")
        )

    def test_shorthand_fact_in_dimensions(self) -> None:
        """A fact goes through ``dimensions=``."""
        assert Sales.query(dimensions=[Sales.unit_price]) == Sales.query().dimensions(
            Sales.unit_price
        )

    @pytest.mark.parametrize("value", [[], None], ids=["empty-list", "none"])
    def test_shorthand_nothing_is_an_empty_query(self, value: Any) -> None:
        """An empty or absent list selects nothing, rather than raising."""
        assert Sales.query(metrics=value) == Sales.query()

    def test_shorthand_rejects_dimension_as_metric(self) -> None:
        """A dimension in ``metrics=`` is refused as ``.metrics()`` would refuse it."""
        with pytest.raises(TypeError, match=r"Did you mean \.dimensions\(\)"):
            Sales.query(metrics=[Sales.region])  # type: ignore[reportArgumentType]

    def test_shorthand_rejects_metric_as_dimension(self) -> None:
        """A metric in ``dimensions=`` is refused as ``.dimensions()`` would refuse it."""
        with pytest.raises(TypeError, match=r"Did you mean \.metrics\(\)"):
            Sales.query(dimensions=[Sales.revenue])  # type: ignore[reportArgumentType]

    def test_shorthand_rejects_cross_model_field(self) -> None:
        """Another model's field is refused."""

        class Other(SemanticView, view="other"):
            some_metric = Metric()

        with pytest.raises(TypeError, match="Cannot mix fields from different models"):
            Sales.query(metrics=[Other.some_metric])

    def test_shorthand_keyword_only(self) -> None:
        """Fields cannot be passed positionally."""
        with pytest.raises(TypeError):
            Sales.query([Sales.revenue])  # type: ignore[call-arg]


class TestFieldBearingEqualityIsIdentityBased:
    """
    ``OrderTerm`` and ``Query`` must not inherit ``Field.__eq__``'s answer.

    ``Field.__eq__`` returns a truthy ``Exact`` predicate — that is the filter DSL and is
    correct. But both of these were plain ``eq=True`` dataclasses, so their generated
    ``__eq__`` compared their ``Field`` members with ``==`` and got a predicate back. Python's
    rich comparison treats any truthy result as equal, so two order terms over different
    fields, and two queries selecting different metrics, all compared equal.
    """

    def test_order_terms_over_different_fields_are_not_equal(self) -> None:
        """The bug in its plainest form: direction matched, field ignored."""
        assert Sales.revenue.desc() != Sales.cost.desc()

    def test_order_terms_over_the_same_field_are_equal(self) -> None:
        """Identity-based equality must still call two terms over one field equal."""
        assert Sales.revenue.desc() == Sales.revenue.desc()

    def test_order_terms_differing_only_by_direction_are_not_equal(self) -> None:
        """Direction is still part of the comparison."""
        assert Sales.revenue.desc() != Sales.revenue.asc()

    def test_equal_order_terms_hash_equally(self) -> None:
        """Hashability survives, and holds the equal-implies-same-hash invariant."""
        assert len({Sales.revenue.desc(), Sales.revenue.desc(), Sales.cost.desc()}) == 2

    def test_queries_selecting_different_metrics_are_not_equal(self) -> None:
        """Two queries that would generate different SQL must not compare equal."""
        assert Sales.query().metrics(Sales.revenue) != Sales.query().metrics(Sales.cost)

    def test_queries_selecting_different_dimensions_are_not_equal(self) -> None:
        """Same, for the dimension tuple."""
        assert Sales.query().dimensions(Sales.country) != Sales.query().dimensions(Sales.region)

    def test_identically_built_queries_are_equal(self) -> None:
        """Structural equality is preserved: same fields, same limit, same filters."""
        left = Sales.query().metrics(Sales.revenue).dimensions(Sales.country).limit(10)
        right = Sales.query().metrics(Sales.revenue).dimensions(Sales.country).limit(10)

        assert left == right

    def test_queries_differing_by_filter_are_not_equal(self) -> None:
        """Filters are compared by value, since a Lookup holds strings rather than Fields."""
        base = Sales.query().metrics(Sales.revenue)

        assert base.where(Sales.country == "US") != base.where(Sales.country == "CA")

    def test_equal_queries_hash_equally(self) -> None:
        """A frozen query stays usable as a dict key, with a hash that matches equality."""
        left = Sales.query().metrics(Sales.revenue).limit(10)
        right = Sales.query().metrics(Sales.revenue).limit(10)

        assert hash(left) == hash(right)
        assert len({left, right, Sales.query().metrics(Sales.cost).limit(10)}) == 2


class TestToSqlDefaultsToTheUsingEnginesDialect:
    """
    ``to_sql()`` with no argument renders for the engine ``.using()`` names, when registered.

    Without a registered engine of that name, and without ``.using()``, it renders for
    Snowflake, as it always has: a preview must not fail because the engine is not set up.
    """

    @staticmethod
    def _engine_with(dialect: Any) -> DuckDBEngine:
        """Build an engine shell carrying ``dialect``; ``to_sql()`` never touches the pool."""
        from semolina.engines.duckdb import DuckDBEngine

        return DuckDBEngine(pool=MagicMock(), dialect=dialect)

    def test_a_registered_engine_sets_the_dialect(self) -> None:
        """The engine's own dialect instance renders the SQL, not a lookup by name."""
        import semolina
        from semolina.engines.sql import DatabricksDialect

        semolina.register("dbx", self._engine_with(DatabricksDialect()))
        q = Sales.query().metrics(Sales.revenue).using("dbx")

        assert q.to_sql() == q.to_sql(Dialect.DATABRICKS)
        assert q.to_sql() != q.to_sql(Dialect.SNOWFLAKE)

    def test_a_registered_async_engine_sets_the_dialect(self) -> None:
        """An async engine registered under the name is used the same way."""
        import semolina
        from semolina.engines.abase import AsyncEngine
        from semolina.engines.sql import DuckDBDialect

        engine = AsyncEngine(pool=MagicMock(), dialect=DuckDBDialect())
        semolina.register_async_engine("duck", engine)
        q = Sales.query().metrics(Sales.revenue).using("duck")

        assert q.to_sql() == q.to_sql(Dialect.DUCKDB)

    def test_an_explicit_dialect_wins(self) -> None:
        """Passing a dialect still renders for that dialect, whatever ``.using()`` names."""
        import semolina
        from semolina.engines.sql import DatabricksDialect

        semolina.register("dbx", self._engine_with(DatabricksDialect()))
        q = Sales.query().metrics(Sales.revenue).using("dbx")

        assert q.to_sql(Dialect.SNOWFLAKE) == Sales.query().metrics(Sales.revenue).to_sql()

    def test_an_unregistered_engine_falls_back_to_snowflake(self) -> None:
        """A name nobody registered previews as Snowflake rather than raising."""
        q = Sales.query().metrics(Sales.revenue).using("nowhere")

        assert q.to_sql() == q.to_sql(Dialect.SNOWFLAKE)

    def test_without_using_the_default_engine_is_not_consulted(self) -> None:
        """With no ``.using()``, ``to_sql()`` stays Snowflake even if ``default`` is DuckDB."""
        import semolina
        from semolina.engines.sql import DuckDBDialect

        semolina.register("default", self._engine_with(DuckDBDialect()))
        q = Sales.query().metrics(Sales.revenue)

        assert q.to_sql() == q.to_sql(Dialect.SNOWFLAKE)
