"""
Warehouse introspection integration test (pytest-adbc-replay).

Validates :meth:`DatabricksEngine.introspect` and :meth:`SnowflakeEngine.introspect`
end-to-end over the real ADBC pool, against the ``sales_view`` the recording fixtures
build: ``DESCRIBE TABLE EXTENDED {view} AS JSON`` on Databricks, and on Snowflake
``SELECT CURRENT_DATABASE(), CURRENT_SCHEMA()`` followed by ``SHOW COLUMNS IN VIEW
{database}.{schema}.sales_view``. Runs against recorded cassettes by default (incl. CI,
no credentials); re-record with::

    pytest --adbc-record=all tests/integration/test_introspect.py

Both views are introspected by their bare name, the way the Databricks one always has
been. The Snowflake session query is what makes that replay: the temp schema's random
name reaches ``SHOW COLUMNS`` from the recorded session answer, so the replayed
statement matches the recorded one. DuckDB introspection (``DESCRIBE SEMANTIC VIEW``)
is covered by its own engine unit tests.

See docs/src/how-to/warehouse-testing.rst for the full record/replay workflow.
"""

from __future__ import annotations

from typing import Any

import pytest

# Records/replays an ADBC cassette; the plugin intercepts the pool's connection.
pytestmark = pytest.mark.adbc_cassette


def test_databricks_introspect_metric_view(databricks_engine: Any) -> None:
    """DESCRIBE TABLE EXTENDED AS JSON over ADBC -> IntrospectedView."""
    view = databricks_engine.introspect("sales_view")

    assert view.view_name == "sales_view"
    assert view.class_name == "SalesView"

    by_name = {field.name: field for field in view.fields}
    assert set(by_name) == {"country", "region", "is_online", "revenue", "cost"}

    # is_measure -> metric; everything else -> dimension. type.name -> Python type.
    assert by_name["country"].field_type == "dimension"
    assert by_name["country"].data_type == "str"
    assert by_name["region"].field_type == "dimension"
    assert by_name["region"].data_type == "str"
    assert by_name["revenue"].field_type == "metric"
    assert by_name["revenue"].data_type == "int"
    assert by_name["cost"].field_type == "metric"
    assert by_name["cost"].data_type == "int"
    assert by_name["is_online"].field_type == "dimension"
    assert by_name["is_online"].data_type == "bool"

    # The fixture view sets no column comments and uses round-tripping names.
    assert by_name["country"].description == ""
    assert by_name["country"].source_name is None


def test_databricks_introspect_decimal_and_date_types(databricks_engine: Any) -> None:
    """
    DECIMAL columns and measures introspect as ``decimal.Decimal``; DATE as ``datetime.date``.

    The codegen half of driver 0.2.0's native decimals: the generated model annotates
    what the cursor now returns.
    """
    view = databricks_engine.introspect("typed_view")

    types = {f.name: (f.field_type, f.data_type, f.source_name) for f in view.fields}
    assert types == {
        "country": ("dimension", "str", None),
        "sold": ("dimension", "datetime.date", None),
        "price": ("dimension", "decimal.Decimal", None),
        # `what?` is not a Python name: the field is sanitized and reads the column by source.
        "what_": ("dimension", "str", "what?"),
        "total_price": ("metric", "decimal.Decimal", None),
        "avg_price": ("metric", "decimal.Decimal", None),
        "total_qty": ("metric", "decimal.Decimal", None),
    }


def test_databricks_introspect_reads_comments_display_names_and_synonyms(
    databricks_engine: Any,
) -> None:
    """Column comments, display names and synonyms, and the view's own comment."""
    view = databricks_engine.introspect("annotated_view")

    assert view.description == "Sales by country"
    by_name = {field.name: field for field in view.fields}
    assert by_name["country"].description == "Ship-to country"
    assert by_name["country"].label == "Country"
    assert by_name["country"].synonyms == ("nation", "land")
    assert by_name["revenue"].description == "Gross revenue"
    assert by_name["revenue"].label == "Revenue"
    assert by_name["revenue"].synonyms == ("sales",)
    assert by_name["region"].description == ""
    assert by_name["region"].label is None
    assert by_name["region"].synonyms == ()


def test_snowflake_introspect_semantic_view(snowflake_engine: Any) -> None:
    """
    SHOW COLUMNS IN VIEW over ADBC, qualified from the session -> IntrospectedView.

    The replay checks the session query and the parsing of the recorded rows, not the
    ``SHOW COLUMNS`` statement: pytest-adbc-replay 1.1.1 parses with the Snowflake dialect
    but renders with sqlglot's generic one, which cannot print ``SHOW``, so the statement
    is stored and matched as an empty string. The exact statement is pinned by
    ``tests/unit/test_snowflake_engine.py`` and was accepted live when this was recorded.
    """
    view = snowflake_engine.introspect("sales_view")

    assert view.view_name == "sales_view"
    assert view.class_name == "SalesView"

    by_name = {field.name: field for field in view.fields}
    assert set(by_name) == {"country", "region", "is_online", "revenue", "cost"}

    # kind -> field type. SUM over a bare NUMBER column reports FIXED scale 0, which maps
    # to Decimal; VARCHAR reports TEXT.
    assert by_name["country"].field_type == "dimension"
    assert by_name["country"].data_type == "str"
    assert by_name["region"].field_type == "dimension"
    assert by_name["region"].data_type == "str"
    assert by_name["revenue"].field_type == "metric"
    assert by_name["revenue"].data_type == "decimal.Decimal"
    assert by_name["cost"].field_type == "metric"
    assert by_name["cost"].data_type == "decimal.Decimal"
    assert by_name["is_online"].field_type == "dimension"
    assert by_name["is_online"].data_type == "bool"

    # The fixture creates every identifier unquoted, so Snowflake stores them UPPERCASE
    # and the lower-cased Python name folds back to them: no source name is needed.
    assert by_name["country"].description == ""
    assert by_name["country"].source_name is None
    assert by_name["country"].synonyms == ()
    assert view.description == ""


def test_snowflake_introspect_reads_comments_and_synonyms(snowflake_engine: Any) -> None:
    """
    Member comments from ``SHOW COLUMNS``; synonyms and the view's comment from ``DESCRIBE``.

    Snowflake has no display-name slot, so every label is None.
    """
    view = snowflake_engine.introspect("annotated_view")

    assert view.description == "Sales by country"
    by_name = {field.name: field for field in view.fields}
    assert by_name["country"].description == "Ship-to country"
    assert by_name["country"].synonyms == ("nation", "land")
    assert by_name["revenue"].description == "Gross revenue"
    assert by_name["revenue"].synonyms == ("sales",)
    assert by_name["region"].synonyms == ()
    assert {field.label for field in view.fields} == {None}
