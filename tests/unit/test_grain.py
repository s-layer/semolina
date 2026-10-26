"""
Tests for :meth:`Dimension.grain`: truncating a date or timestamp dimension at query time.

The SQL each dialect writes is pinned exactly here; the values it returns are checked
against a calendar oracle by the warehouse tests in ``tests/integration/test_queries.py``
(Snowflake and Databricks) and by :class:`TestDuckDBExecution` once the installed
``semantic_views`` extension supports expression dimensions.
"""

from __future__ import annotations

import datetime
from typing import Any, Optional

import pytest

from semolina import Dimension, Fact, Metric, SemanticView
from semolina.fields import GRAINS, GrainedDimension
from semolina.filters import Exact


class Orders(SemanticView, view="orders"):
    """Typed date and timestamp dimensions, and one of each other kind."""

    revenue = Metric[int]()
    ordered_at = Dimension[datetime.datetime]()
    ordered_on = Dimension[datetime.date](source="ORDER_DATE")
    region = Dimension[str]()
    untyped = Dimension()
    unit_price = Fact[int]()


class TestGrain:
    """What ``grain()`` returns, and what it refuses."""

    def test_returns_a_dimension_named_for_the_grain(self):
        """A dimension of the same model, named ``<field>__<unit>``."""
        week = Orders.ordered_at.grain("week")
        assert isinstance(week, GrainedDimension)
        assert isinstance(week, Dimension)
        assert week.name == "ordered_at__week"
        assert week.owner is Orders
        assert week.base is Orders.ordered_at
        assert week.unit == "week"
        assert repr(week) == "Dimension('ordered_at').grain('week')"

    def test_same_unit_gives_the_same_object(self):
        """So a query refuses it twice, as it refuses any field selected twice."""
        assert Orders.ordered_at.grain("month") is Orders.ordered_at.grain("month")
        with pytest.raises(ValueError, match="selected twice"):
            Orders.query().dimensions(
                Orders.ordered_at.grain("month"), Orders.ordered_at.grain("month")
            )

    def test_grain_and_base_select_together(self):
        """Different names, so both columns come back."""
        query = Orders.query().dimensions(Orders.ordered_at, Orders.ordered_at.grain("day"))
        assert [f.name for f in query.selected_dimensions] == ["ordered_at", "ordered_at__day"]

    @pytest.mark.parametrize("unit", GRAINS)
    def test_every_grain_of_a_timestamp(self, unit: str):
        """A timestamp takes every grain; a day or coarser comes back as a date."""
        grained = Orders.ordered_at.grain(unit)  # pyright: ignore[reportArgumentType]
        assert grained.result_is_date == (unit in ("year", "quarter", "month", "week", "day"))

    @pytest.mark.parametrize("unit", ["year", "quarter", "month", "week", "day"])
    def test_a_date_takes_day_and_coarser(self, unit: str):
        """Each of these is a whole number of days."""
        assert Orders.ordered_on.grain(unit).name == f"ordered_on__{unit}"  # pyright: ignore[reportArgumentType]

    @pytest.mark.parametrize("unit", ["hour", "minute", "second"])
    def test_a_date_refuses_finer_than_day(self, unit: str):
        """A date has no hours."""
        with pytest.raises(ValueError, match=f"'ordered_on' is a date, so it has no '{unit}'"):
            Orders.ordered_on.grain(unit)  # pyright: ignore[reportArgumentType]

    def test_an_untyped_dimension_takes_any_grain(self):
        """Without a declared type there is nothing to check against."""
        assert Orders.untyped.grain("second").name == "untyped__second"

    def test_an_optional_declaration_is_checked_as_the_type_it_wraps(self):
        """``X | None`` and ``Optional[X]`` are a nullable column of type ``X``."""

        class Nullable(SemanticView, view="nullable"):
            ordered_at = Dimension[datetime.datetime | None]()
            ordered_on = Dimension[Optional[datetime.date]]()  # noqa: UP045

        assert Nullable.ordered_at.grain("second").name == "ordered_at__second"
        assert Nullable.ordered_on.grain("day").name == "ordered_on__day"
        with pytest.raises(ValueError, match="'ordered_on' is a date, so it has no 'hour'"):
            Nullable.ordered_on.grain("hour")

    def test_a_non_temporal_type_is_refused(self):
        """A string dimension cannot be truncated."""
        with pytest.raises(TypeError, match="'region' is declared as str"):
            Orders.region.grain("month")

    @pytest.mark.parametrize("unit", ["fortnight", "millisecond", "decade", "WEEK", ""])
    def test_unknown_unit(self, unit: str):
        """Only the units all three backends share, spelled in lower case."""
        with pytest.raises(ValueError, match="grain\\(\\) takes one of 'year'"):
            Orders.ordered_at.grain(unit)  # pyright: ignore[reportArgumentType]

    def test_cannot_truncate_twice(self):
        """Truncate the base instead."""
        with pytest.raises(TypeError, match="already truncated. Call grain\\('year'\\) on"):
            Orders.ordered_at.grain("month").grain("year")

    def test_name_clash_with_a_real_field(self):
        """A model field already named like the grain's column wins, and the grain is refused."""

        class Clash(SemanticView, view="clash"):
            ordered_at = Dimension[datetime.datetime]()
            ordered_at__week = Dimension[datetime.date]()

        with pytest.raises(ValueError, match="'ordered_at__week', which is already a field"):
            Clash.ordered_at.grain("week")

    def test_metrics_and_facts_have_no_grain(self):
        """Only a dimension groups, so only a dimension truncates."""
        assert not hasattr(Orders.revenue, "grain")
        assert not hasattr(Orders.unit_price, "grain")

    def test_inherited_dimension_grains_bind_to_the_subclass(self):
        """A subclass's copy of the field does not hand out its parent's grains."""

        class Base(SemanticView, view="base"):
            ordered_at = Dimension[datetime.datetime]()

        parent_week = Base.ordered_at.grain("week")

        class Child(Base, view="child"):
            pass

        child_week = Child.ordered_at.grain("week")
        assert child_week is not parent_week
        assert child_week.owner is Child
        assert parent_week.owner is Base
        sql = Child.query().dimensions(child_week).to_sql("duckdb")
        assert "semantic_view('child'" in sql

    def test_a_grain_from_another_model_is_refused(self):
        """The query's ownership check applies to grains too."""

        class Other(SemanticView, view="other"):
            ordered_at = Dimension[datetime.datetime]()

        with pytest.raises(TypeError, match="Cannot mix fields from different models"):
            Orders.query().dimensions(Other.ordered_at.grain("day"))


class TestGrainKeepsMetadata:
    """A truncated dimension describes the same thing as its base, so it carries its metadata."""

    def test_label_description_synonyms_and_format_carry_over(self) -> None:
        class Annotated(SemanticView, view="annotated"):
            ordered_at = Dimension[datetime.datetime](
                label="Ordered at",
                description="When the order was placed",
                synonyms=("order date",),
                semantic_format={"value": {"kind": "datetime"}},
            )

        base = Annotated.ordered_at
        grained = base.grain("month")

        assert grained.label == "Ordered at"
        assert grained.description == "When the order was placed"
        assert grained.synonyms == ("order date",)
        assert grained.semantic_format == base.semantic_format


class TestNoFilters:
    """A grain cannot be filtered; its base can."""

    @pytest.mark.parametrize(
        "build",
        [
            lambda f: f == datetime.date(2024, 1, 1),
            lambda f: f != datetime.date(2024, 1, 1),
            lambda f: f > datetime.date(2024, 1, 1),
            lambda f: f >= datetime.date(2024, 1, 1),
            lambda f: f < datetime.date(2024, 1, 1),
            lambda f: f <= datetime.date(2024, 1, 1),
            lambda f: f.between(datetime.date(2024, 1, 1), datetime.date(2024, 2, 1)),
            lambda f: f.in_([datetime.date(2024, 1, 1)]),
            lambda f: f.isnull(),
            lambda f: f.like("2024%"),
            lambda f: f.ilike("2024%"),
            lambda f: f.startswith("2024"),
            lambda f: f.istartswith("2024"),
            lambda f: f.endswith("01"),
            lambda f: f.iendswith("01"),
            lambda f: f.iexact("2024-01-01"),
            lambda f: f.lookup(Exact, datetime.date(2024, 1, 1)),
        ],
    )
    def test_every_filter_is_refused(self, build: Any):
        """Each operator and named method raises, naming the base to filter instead."""
        with pytest.raises(TypeError, match="Filter 'ordered_at' instead"):
            build(Orders.ordered_at.grain("week"))

    def test_the_base_still_filters(self):
        """Refusing the grain's filters leaves its base's alone."""
        assert (Orders.ordered_at == 1) == Exact(field_name="ordered_at", value=1)


def _sql(dialect: str, *dimensions: Any, order: Any = None) -> str:
    """Revenue by ``dimensions`` in ``dialect``, optionally ordered."""
    query = Orders.query().metrics(Orders.revenue).dimensions(*dimensions)
    if order is not None:
        query = query.order_by(order)
    return query.to_sql(dialect)


class TestSQL:
    """The expression each dialect writes, pinned exactly."""

    @pytest.mark.parametrize(
        ("unit", "expression"),
        [
            ("year", """CAST(DATE_TRUNC('year', "ORDERED_AT") AS DATE)"""),
            ("quarter", """CAST(DATE_TRUNC('quarter', "ORDERED_AT") AS DATE)"""),
            ("month", """CAST(DATE_TRUNC('month', "ORDERED_AT") AS DATE)"""),
            (
                "week",
                """DATEADD('day', 1 - DAYOFWEEKISO("ORDERED_AT"), CAST("ORDERED_AT" AS DATE))""",
            ),
            ("day", """CAST(DATE_TRUNC('day', "ORDERED_AT") AS DATE)"""),
            ("hour", """DATE_TRUNC('hour', "ORDERED_AT")"""),
            ("minute", """DATE_TRUNC('minute', "ORDERED_AT")"""),
            ("second", """DATE_TRUNC('second', "ORDERED_AT")"""),
        ],
    )
    def test_snowflake(self, unit: str, expression: str):
        """
        Snowflake: weeks from ``DAYOFWEEKISO``, which ``WEEK_START`` does not change.

        ``DATE_TRUNC('week', ...)`` would start on whatever day the session parameter names.
        """
        grained = Orders.ordered_at.grain(unit)  # pyright: ignore[reportArgumentType]
        assert _sql("snowflake", grained) == (
            f'SELECT AGG("REVENUE") AS "revenue", {expression} AS "ordered_at__{unit}"\n'
            'FROM "ORDERS"\n'
            "GROUP BY ALL"
        )

    @pytest.mark.parametrize(
        ("unit", "expression"),
        [
            ("year", "CAST(DATE_TRUNC('year', `ordered_at`) AS DATE)"),
            ("week", "CAST(DATE_TRUNC('week', `ordered_at`) AS DATE)"),
            ("day", "CAST(DATE_TRUNC('day', `ordered_at`) AS DATE)"),
            (
                "hour",
                "(`ordered_at` - MAKE_DT_INTERVAL(0, 0, EXTRACT(MINUTE FROM `ordered_at`), "
                "EXTRACT(SECOND FROM `ordered_at`)))",
            ),
            (
                "minute",
                "(`ordered_at` - MAKE_DT_INTERVAL(0, 0, 0, EXTRACT(SECOND FROM `ordered_at`)))",
            ),
            (
                "second",
                "(`ordered_at` - MAKE_DT_INTERVAL(0, 0, 0, "
                "MOD(EXTRACT(SECOND FROM `ordered_at`), 1)))",
            ),
        ],
    )
    def test_databricks(self, unit: str, expression: str):
        """
        Databricks: ``DATE_TRUNC`` weeks start on Monday; day and coarser cast to DATE.

        Finer grains subtract the part below the grain instead of calling ``DATE_TRUNC``,
        which returns a ``TIMESTAMP`` even for a ``TIMESTAMP_NTZ`` column and so would turn
        a naive value into one in the session time zone.
        """
        grained = Orders.ordered_at.grain(unit)  # pyright: ignore[reportArgumentType]
        assert _sql("databricks", grained) == (
            f"SELECT MEASURE(`revenue`) AS `revenue`, {expression} AS `ordered_at__{unit}`\n"
            "FROM `orders`\n"
            "GROUP BY ALL"
        )

    def test_duckdb_computes_inside_the_call(self):
        """
        DuckDB: an expression dimension inside ``semantic_view()``, aliased.

        The outer query then selects and orders that column by name, as it does a plain
        dimension. Expression dimensions need duckdb-semantic-views 0.16.0 or later.
        """
        week = Orders.ordered_at.grain("week")
        assert _sql("duckdb", week, order=week.desc()) == (
            'SELECT "revenue" AS "revenue", "ordered_at__week" AS "ordered_at__week"\n'
            "FROM semantic_view('orders', dimensions := ['CAST(DATE_TRUNC(''week'', "
            '"ordered_at") AS DATE) AS "ordered_at__week"\'], metrics := [\'revenue\'])\n'
            'ORDER BY "ordered_at__week" DESC'
        )

    @pytest.mark.parametrize(
        ("dialect", "column"),
        [("snowflake", '"ORDER_DATE"'), ("databricks", "`ORDER_DATE`"), ("duckdb", '"ORDER_DATE"')],
    )
    def test_source_column_is_truncated(self, dialect: str, column: str):
        """A ``source=`` base truncates its source column, under the grain's own name."""
        sql = _sql(dialect, Orders.ordered_on.grain("month"))
        assert f"DATE_TRUNC('month', {column})" in sql.replace("''", "'")
        assert "ordered_on__month" in sql

    @pytest.mark.parametrize(
        ("dialect", "alias"),
        [("snowflake", '"ordered_at__day"'), ("databricks", "`ordered_at__day`")],
    )
    def test_order_by_names_the_alias(self, dialect: str, alias: str):
        """Ordering by a grain orders by its column, not by the base's."""
        day = Orders.ordered_at.grain("day")
        assert _sql(dialect, day, order=day.desc()).endswith(f"ORDER BY {alias} DESC")

    def test_order_by_unselected_grain_is_refused(self):
        """Like any dimension, a grain must be selected to order by it."""
        query = (
            Orders.query()
            .metrics(Orders.revenue)
            .dimensions(Orders.region)
            .order_by(Orders.ordered_at.grain("day"))
        )
        with pytest.raises(ValueError, match="ordered_at__day.*does not select"):
            query.to_sql()

    def test_filter_on_base_with_grain_selected(self):
        """The base's filter goes to WHERE; the grain groups."""
        sql = (
            Orders.query()
            .metrics(Orders.revenue)
            .dimensions(Orders.ordered_at.grain("month"))
            .where(Orders.ordered_at >= datetime.date(2024, 1, 1))
            .to_sql("snowflake")
        )
        assert "WHERE \"ORDERED_AT\" >= DATE '2024-01-01'\nGROUP BY ALL" in sql


def _create_orders(dbapi_conn: Any, _connection_record: Any) -> None:
    """Create ``orders_data`` and ``orders_view`` on the pool's first connection."""
    from orders import orders_values_sql

    cur = dbapi_conn.cursor()
    cur.execute(
        "CREATE TABLE orders_data (id INTEGER, ordered_at TIMESTAMP, ordered_on DATE,"
        " amount INTEGER)"
    )
    cur.execute(
        "INSERT INTO orders_data SELECT row_number() OVER (), * FROM"
        f" (VALUES {orders_values_sql()})"
    )
    cur.execute(
        "CREATE SEMANTIC VIEW orders_view AS TABLES (o AS orders_data PRIMARY KEY (id))"
        " DIMENSIONS (o.ordered_at AS ordered_at, o.ordered_on AS ordered_on)"
        " METRICS (o.revenue AS SUM(o.amount))"
    )
    cur.close()
    dbapi_conn.commit()


class TestDuckDBExecution:
    """
    Grain queries run on DuckDB and match the calendar oracle.

    duckdb-semantic-views took expression dimensions in 0.16.0, which needs DuckDB 2.0, the
    version Semolina pins. They run rather than skip, so a pin that lost expression
    dimensions fails here.
    """

    @pytest.fixture
    def orders_engine(self) -> Any:
        """An in-memory engine holding ``orders_data`` and ``orders_view``."""
        from adbc_poolhouse import DuckDBConfig, close_pool
        from sqlalchemy import event

        import semolina
        from semolina.config import create_engine

        engine = create_engine(DuckDBConfig(database=":memory:", pool_size=1))
        event.listen(engine._pool, "connect", _create_orders, once=True)
        semolina.register("default", engine)
        yield engine
        semolina.unregister("default")
        close_pool(engine._pool)

    @pytest.mark.parametrize("unit", GRAINS)
    def test_revenue_by_grain(self, orders_engine: Any, unit: str):  # noqa: ARG002
        """Values and types match the oracle, as on Snowflake and Databricks."""
        from orders import expected_by_grain

        period = OrdersView.ordered_at.grain(unit)  # pyright: ignore[reportArgumentType]
        query = OrdersView.query(metrics=[OrdersView.revenue], dimensions=[period]).order_by(period)
        with query.execute() as cursor:
            rows = [(int(r[0]), r[1]) for r in cursor.fetchall()]
        assert rows == expected_by_grain(unit)
        want = datetime.date if period.result_is_date else datetime.datetime
        assert all(type(value) is want for _, value in rows), rows


class OrdersView(SemanticView, view="orders_view"):
    """The DuckDB counterpart of the warehouse tests' ``orders_view``."""

    revenue = Metric[int]()
    ordered_at = Dimension[datetime.datetime]()
    ordered_on = Dimension[datetime.date]()
