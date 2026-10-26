"""
Warehouse query integration tests for SQL correctness.

Tests run against recorded cassettes by default (incl. CI) via
pytest-adbc-replay — no credentials, no warehouse. Each cassette holds the
real SQL Semolina generated plus the real Arrow result the warehouse returned,
so replay validates two things at once: the generated SQL still matches what was
recorded (a mismatch raises ``CassetteMissError``), and the cursor returns the
correct aggregated rows.

Most tests assert on raw DBAPI tuples from ``cursor.fetchall()``.
``test_streaming_iteration`` covers the ``Row`` conversion path via
``for row in cursor:`` and pins the ``Row`` keys: every selected column is aliased
to its Python field name (ALIAS-01), so both backends return ``revenue`` and
``country`` rather than ``AGG("REVENUE")`` / ``COUNTRY`` or ``measure(revenue)``.

Every test is marked ``@pytest.mark.adbc_cassette`` (module-wide via
``pytestmark``) so the plugin intercepts the pool's connections. Each runs
against both Snowflake and Databricks through the ``backend_engine`` fixture;
cassettes are stored per test+backend.

To (re)record against real warehouses, configure ``[connections.snowflake]`` /
``[connections.databricks]`` in ``.semolina.toml`` (or ``SNOWFLAKE_*`` /
``DATABRICKS_*`` env vars), then commit the cassettes::

    pytest --adbc-record=all tests/integration/test_queries.py

Assertions compare raw row tuples in SELECT-clause order (metrics first, then
dimensions). Numeric values are normalized (Snowflake returns ``Decimal``,
Databricks ``int``) so expectations are backend-agnostic.

See docs/src/how-to/warehouse-testing.rst for the full workflow.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

import pytest
from orders import expected_by_grain

from semolina import Dimension, Metric, SemanticView
from semolina.fields import GRAINS, NullsOrdering

# Every test in this module records/replays an ADBC cassette. The cassette name
# is auto-derived from the node id (including the [snowflake_engine] /
# [databricks_engine] parameter), so each test+backend gets its own recording.
pytestmark = pytest.mark.adbc_cassette


class Sales(SemanticView, view="sales_view"):
    """
    Synthetic SemanticView for integration query tests.

    View name matches the ``sales_view`` created by the recording fixtures.
    Do not use this model in other test modules.
    """

    revenue = Metric()
    cost = Metric()
    country = Dimension()
    region = Dimension()
    is_online = Dimension()


def _norm(value: Any) -> Any:
    """
    Normalize a numeric cell so it compares across backends.

    Snowflake returns ``Decimal`` where Databricks returns ``int``/``float``.
    Integral values collapse to ``int`` so both backends match; non-integral
    values collapse to ``float`` (never truncated). Non-numerics pass through.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    return value


def _rows(raw: Any) -> list[tuple[Any, ...]]:
    """Normalize an iterable of row tuples for backend-agnostic comparison."""
    return [tuple(_norm(v) for v in row) for row in raw]


def test_single_metric(backend_engine: Any) -> None:  # noqa: ARG001
    """SUM(revenue) across all rows -> single aggregated value."""
    cursor = Sales.query().using("test").metrics(Sales.revenue).order_by(Sales.revenue).execute()
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    assert rows == [(5800,)]


def test_multiple_metrics(backend_engine: Any) -> None:  # noqa: ARG001
    """SUM(revenue), SUM(cost) across all rows."""
    cursor = (
        Sales.query()
        .using("test")
        .metrics(Sales.revenue, Sales.cost)
        .order_by(Sales.revenue)
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    assert rows == [(5800, 580)]


def test_metric_with_dimension(backend_engine: Any) -> None:  # noqa: ARG001
    """SUM(revenue) grouped by country, ordered by country."""
    cursor = (
        Sales.query()
        .using("test")
        .metrics(Sales.revenue)
        .dimensions(Sales.country)
        .order_by(Sales.country)
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    # SELECT AGG(revenue), country  ->  (revenue, country)
    assert rows == [(2800, "CA"), (1500, "MX"), (1500, "US")]


def test_multiple_metrics_with_dimension(backend_engine: Any) -> None:  # noqa: ARG001
    """SUM(revenue), SUM(cost) grouped by country, ordered by country."""
    cursor = (
        Sales.query()
        .using("test")
        .metrics(Sales.revenue, Sales.cost)
        .dimensions(Sales.country)
        .order_by(Sales.country)
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    # SELECT AGG(revenue), AGG(cost), country
    assert rows == [(2800, 280, "CA"), (1500, 150, "MX"), (1500, 150, "US")]


def test_dimension_only(backend_engine: Any) -> None:  # noqa: ARG001
    """Distinct (country, region) pairs, ordered by region then country."""
    cursor = (
        Sales.query()
        .using("test")
        .dimensions(Sales.country, Sales.region)
        .order_by(Sales.region, Sales.country)
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    # SELECT country, region  ordered by region, country
    assert rows == [
        ("CA", "East"),
        ("US", "East"),
        ("MX", "South"),
        ("CA", "West"),
        ("US", "West"),
    ]


def test_filtered_by_dimension(backend_engine: Any) -> None:  # noqa: ARG001
    """WHERE country = 'US' restricts the aggregation to US rows."""
    cursor = (
        Sales.query()
        .using("test")
        .metrics(Sales.revenue, Sales.cost)
        .dimensions(Sales.country)
        .where(Sales.country == "US")
        .order_by(Sales.country)
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    # US: revenue 1000+500=1500, cost 100+50=150
    assert rows == [(1500, 150, "US")]


def test_streaming_iteration(backend_engine: Any) -> None:  # noqa: ARG001
    """``for row in cursor:`` streams Row objects (ADBC fetch_record_batch path)."""
    cursor = (
        Sales.query()
        .using("test")
        .metrics(Sales.revenue)
        .dimensions(Sales.country)
        .order_by(Sales.country)
        .execute()
    )
    try:
        # Read by field name: the aliases make the Row keys the same on both
        # backends (ALIAS-01). Row.values() order is not a Semolina contract.
        result: dict[str, Any] = {}
        for row in cursor:
            assert set(row.keys()) == {"revenue", "country"}
            result[row.country] = _norm(row.revenue)
    finally:
        cursor.close()
    assert result == {"CA": 2800, "MX": 1500, "US": 1500}


def test_filtered_by_metric(backend_engine: Any) -> None:  # noqa: ARG001
    """
    A metric filter applies to the totals, after grouping (FILT-04, D4).

    CA's two rows are 2000 and 800. Its total of 2800 passes ``> 1500``; filtering the rows
    before adding them up would have returned 2000 instead.
    """
    cursor = (
        Sales.query()
        .using("test")
        .metrics(Sales.revenue)
        .dimensions(Sales.country)
        .where(Sales.revenue > 1500)
        .order_by(Sales.country)
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    assert rows == [(2800, "CA")]


def test_filtered_by_startswith(backend_engine: Any) -> None:  # noqa: ARG001
    """``startswith`` compiles to ``LIKE 'We%' ESCAPE '!'`` and keeps the West rows."""
    cursor = (
        Sales.query()
        .using("test")
        .metrics(Sales.revenue)
        .dimensions(Sales.region)
        .where(Sales.region.startswith("We"))
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    # West: 1000 + 800
    assert rows == [(1800, "West")]


def test_startswith_matches_wildcards_literally(backend_engine: Any) -> None:  # noqa: ARG001
    """
    A ``_`` in a ``startswith`` prefix matches only an underscore (FILT-02).

    No region starts with a literal ``_est``, so only East comes back. Were the ``_`` sent
    unescaped it would match any one character, and West would come back too.
    """
    cursor = (
        Sales.query()
        .using("test")
        .metrics(Sales.revenue)
        .dimensions(Sales.region)
        .where(Sales.region.startswith("E") | Sales.region.startswith("_est"))
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    # East: 2000 + 500
    assert rows == [(2500, "East")]


def test_filtered_by_endswith(backend_engine: Any) -> None:  # noqa: ARG001
    """``endswith`` compiles to ``LIKE '%st' ESCAPE '!'`` and keeps East and West."""
    cursor = (
        Sales.query()
        .using("test")
        .metrics(Sales.revenue)
        .dimensions(Sales.region)
        .where(Sales.region.endswith("st"))
        .order_by(Sales.region)
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    assert rows == [(2500, "East"), (1800, "West")]


def test_endswith_matches_wildcards_literally(backend_engine: Any) -> None:  # noqa: ARG001
    """
    A ``%`` in an ``endswith`` suffix matches only a percent sign (FILT-02).

    No region ends with a literal ``%est``, so only South comes back. Were the ``%`` sent
    unescaped it would match any run of characters, and West would come back too. East
    would not: it ends in ``ast``.
    """
    cursor = (
        Sales.query()
        .using("test")
        .metrics(Sales.revenue)
        .dimensions(Sales.region)
        .where(Sales.region.endswith("th") | Sales.region.endswith("%est"))
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    assert rows == [(1500, "South")]


def test_dimension_and_metric_filters_combine(backend_engine: Any) -> None:  # noqa: ARG001
    """
    ANDed filters split: the dimension one filters rows, the metric one the totals.

    Without MX, the totals are CA 2800 and US 1500, and both pass ``> 1400``. Applying
    ``> 1400`` to rows would keep only CA's 2000.
    """
    cursor = (
        Sales.query()
        .using("test")
        .metrics(Sales.revenue)
        .dimensions(Sales.country)
        .where((Sales.country != "MX") & (Sales.revenue > 1400))
        .order_by(Sales.country)
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    assert rows == [(2800, "CA"), (1500, "US")]


def test_metric_filter_matching_nothing_returns_no_rows(backend_engine: Any) -> None:  # noqa: ARG001
    """A metric filter no total meets returns an empty result rather than raising."""
    cursor = (
        Sales.query()
        .using("test")
        .metrics(Sales.revenue)
        .dimensions(Sales.country)
        .where(Sales.revenue < 0)
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    assert rows == []


def test_filtered_by_boolean(backend_engine: Any) -> None:  # noqa: ARG001
    """A boolean dimension filters with ``== True`` and comes back as ``bool``."""
    cursor = (
        Sales.query()
        .using("test")
        .metrics(Sales.revenue)
        .dimensions(Sales.is_online)
        .where(Sales.is_online == True)  # noqa: E712
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    # Online rows: 1000 + 500 + 800
    assert rows == [(2300, True)]


def test_filtered_by_null(backend_engine: Any) -> None:  # noqa: ARG001
    """``== None`` compiles to IS NULL and matches the NULL row (FILT-01, D3)."""
    cursor = (
        Sales.query()
        .using("test")
        .metrics(Sales.revenue)
        .dimensions(Sales.is_online)
        .where(Sales.is_online == None)  # noqa: E711
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    assert rows == [(1500, None)]


def test_nulls_last_ascending(backend_engine: Any) -> None:  # noqa: ARG001
    """
    ``asc(nulls=LAST)`` puts the NULL group last.

    Databricks sorts NULLs first in an ascending sort unless told otherwise, so this is
    the case that needs the clause there.
    """
    cursor = (
        Sales.query()
        .using("test")
        .metrics(Sales.revenue)
        .dimensions(Sales.is_online)
        .order_by(Sales.is_online.asc(nulls=NullsOrdering.LAST))
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    assert rows == [(2000, False), (2300, True), (1500, None)]


def test_nulls_last_descending(backend_engine: Any) -> None:  # noqa: ARG001
    """
    ``desc(nulls=LAST)`` puts the NULL group last.

    Snowflake sorts NULLs first in a descending sort unless told otherwise, so this is
    the case that needs the clause there.
    """
    cursor = (
        Sales.query()
        .using("test")
        .metrics(Sales.revenue)
        .dimensions(Sales.is_online)
        .order_by(Sales.is_online.desc(nulls=NullsOrdering.LAST))
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    assert rows == [(2300, True), (2000, False), (1500, None)]


class QuotedSales(SemanticView, view="sales_view_quoted"):
    """
    The Snowflake fixture's view whose one measure was created as ``"gross"``.

    The quoted name is stored lower-case, so the field names it with ``source`` rather than
    letting the dialect fold it to ``GROSS``.
    """

    gross = Metric(source="gross")
    country = Dimension()


@pytest.mark.parametrize("backend_engine", ["snowflake_engine"], indirect=True)
def test_filtered_by_quoted_lowercase_metric(backend_engine: Any) -> None:  # noqa: ARG001
    """
    A metric filter on a quoted lower-case Snowflake measure compiles to working HAVING.

    The field is named like the measure, so its select alias ``"gross"`` is spelled the same
    as the measure ``HAVING AGG("gross")`` names. CA's total of 2800 passes ``> 1500``;
    filtering rows first would have returned 2000.
    """
    cursor = (
        QuotedSales.query()
        .using("test")
        .metrics(QuotedSales.gross)
        .dimensions(QuotedSales.country)
        .where(QuotedSales.gross > 1500)
        .order_by(QuotedSales.country)
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    assert rows == [(2800, "CA")]


class Typed(SemanticView, view="typed_view"):
    """
    The Databricks fixture's ``typed_view``: DECIMAL, DATE and a ``what?`` column.

    ``what`` names its column with ``source`` because ``what?`` is not a Python name.
    """

    total_price = Metric()
    avg_price = Metric()
    total_qty = Metric()
    country = Dimension()
    sold = Dimension()
    price = Dimension()
    what = Dimension(source="what?")


@pytest.mark.parametrize("backend_engine", ["databricks_engine"], indirect=True)
def test_decimal_measures_arrive_as_decimal128(backend_engine: Any) -> None:  # noqa: ARG001
    """
    Databricks DECIMAL measures arrive as Arrow ``decimal128``, values exact (driver >= 0.2.0).

    Drivers up to 0.1.3 returned DECIMAL as strings. SUM keeps the column's scale, so the
    scale-0 ``qty`` stays scale 0; AVG widens the scale to 6.
    """
    cursor = (
        Typed.query()
        .using("test")
        .metrics(Typed.total_price, Typed.avg_price, Typed.total_qty)
        .dimensions(Typed.country)
        .order_by(Typed.country)
        .execute()
    )
    try:
        table = cursor.fetch_arrow_table()
    finally:
        cursor.close()
    types = {name: str(t) for name, t in zip(table.schema.names, table.schema.types, strict=True)}
    assert types == {
        "total_price": "decimal128(20, 2)",
        "avg_price": "decimal128(14, 6)",
        "total_qty": "decimal128(20, 0)",
        "country": "string",
    }
    assert table.to_pylist() == [
        {
            "total_price": Decimal("20.00"),
            "avg_price": Decimal("20.000000"),
            "total_qty": Decimal(1),
            "country": "CA",
        },
        {
            "total_price": Decimal("19.98"),
            "avg_price": Decimal("9.990000"),
            "total_qty": Decimal(5),
            "country": "US",
        },
    ]


@pytest.mark.parametrize("backend_engine", ["databricks_engine"], indirect=True)
def test_filtered_by_bound_date_and_decimal(backend_engine: Any) -> None:  # noqa: ARG001
    """
    A ``date`` and a ``Decimal`` filter value bind as parameters on Databricks.

    Only the 2026-01-02 rows priced 9.99 match both: the US row with qty 2. Either filter
    alone would also keep a second row.
    """
    cursor = (
        Typed.query()
        .using("test")
        .metrics(Typed.total_qty)
        .dimensions(Typed.sold, Typed.price)
        .where((Typed.sold == datetime.date(2026, 1, 2)) & (Typed.price == Decimal("9.99")))
        .execute()
    )
    try:
        rows = cursor.fetchall()
    finally:
        cursor.close()
    assert rows == [(Decimal(2), datetime.date(2026, 1, 2), Decimal("9.99"))]


@pytest.mark.parametrize("backend_engine", ["databricks_engine"], indirect=True)
def test_filtered_by_decimal_metric(backend_engine: Any) -> None:  # noqa: ARG001
    """
    A ``Decimal`` bound in HAVING compares exactly against a DECIMAL measure.

    US totals 19.98 and CA 20.00, so ``> 19.99`` keeps only CA.
    """
    cursor = (
        Typed.query()
        .using("test")
        .metrics(Typed.total_price)
        .dimensions(Typed.country)
        .where(Typed.total_price > Decimal("19.99"))
        .execute()
    )
    try:
        rows = cursor.fetchall()
    finally:
        cursor.close()
    assert rows == [(Decimal("20.00"), "CA")]


@pytest.mark.parametrize("backend_engine", ["databricks_engine"], indirect=True)
def test_question_mark_in_identifier_survives_binding(backend_engine: Any) -> None:  # noqa: ARG001
    """
    A ``?`` inside a backticked column name is not taken for a placeholder (FILT-07).

    The statement holds one real placeholder and one ``?`` in `` `what?` ``. Counting both
    would leave the driver one value short and the query would fail.
    """
    cursor = (
        Typed.query()
        .using("test")
        .metrics(Typed.total_qty)
        .dimensions(Typed.what)
        .where(Typed.what == "a?")
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    assert rows == [(3, "a?")]


class Orders(SemanticView, view="orders_view"):
    """The ``orders_view`` the recording fixtures create, for the time grain tests."""

    revenue = Metric[int]()
    ordered_at = Dimension[datetime.datetime]()
    ordered_on = Dimension[datetime.date]()


@pytest.mark.parametrize("unit", GRAINS)
def test_revenue_by_timestamp_grain(backend_engine: Any, unit: str) -> None:  # noqa: ARG001
    """
    Revenue by each grain of a timestamp matches the calendar oracle, values and types.

    The oracle (``orders.truncate``) is written from the calendar, so a backend that
    started weeks on Sunday, or a grain that cut at the wrong boundary, changes a total. A
    day or coarser must come back as ``date`` on both backends, a finer grain as
    ``datetime``.
    """
    period = Orders.ordered_at.grain(unit)  # pyright: ignore[reportArgumentType]
    cursor = (
        Orders.query()
        .using("test")
        .metrics(Orders.revenue)
        .dimensions(period)
        .order_by(period)
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    assert rows == expected_by_grain(unit)
    want = datetime.date if period.result_is_date else datetime.datetime
    assert all(type(value) is want for _, value in rows), rows


@pytest.mark.parametrize("unit", ["year", "quarter", "month", "week", "day"])
def test_revenue_by_date_grain(backend_engine: Any, unit: str) -> None:  # noqa: ARG001
    """A ``DATE`` dimension truncates to the same periods, and stays a ``date``."""
    period = Orders.ordered_on.grain(unit)  # pyright: ignore[reportArgumentType]
    cursor = (
        Orders.query()
        .using("test")
        .metrics(Orders.revenue)
        .dimensions(period)
        .order_by(period.desc())
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    assert rows == list(reversed(expected_by_grain(unit, from_date=True)))
    assert all(type(value) is datetime.date for _, value in rows), rows


def test_grain_beside_its_base_and_filtered_by_it(backend_engine: Any) -> None:  # noqa: ARG001
    """
    A grain and a filter on its base dimension combine: weekly revenue from April on.

    The filter applies to ``ordered_at`` before grouping, so the week of 25 March keeps
    none of its rows.
    """
    week = Orders.ordered_at.grain("week")
    cursor = (
        Orders.query()
        .using("test")
        .metrics(Orders.revenue)
        .dimensions(week)
        .where(Orders.ordered_at >= datetime.datetime(2024, 4, 1))
        .order_by(week)
        .execute()
    )
    try:
        rows = _rows(cursor.fetchall())
    finally:
        cursor.close()
    assert rows == [
        (4 + 8 + 16 + 32 + 64, datetime.date(2024, 4, 1)),
        (128, datetime.date(2024, 4, 8)),
        (256 + 512, datetime.date(2024, 12, 30)),
    ]
