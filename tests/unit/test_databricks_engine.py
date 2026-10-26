"""
Unit tests for DatabricksEngine on the Engine API.

Every backend follows the ``create_engine`` / ADBC-pool contract: the engine is built
with ``create_engine(DatabricksConfig(...))``, owns one
ADBC pool plus the Databricks dialect, and executes queries through the
inherited :meth:`~semolina.engines.base.Engine.execute` pool path.

These tests assert the new-API construction, the dialect selection, SQL
generation, and ``introspect()``: its parsing of ``DESCRIBE TABLE EXTENDED ...
AS JSON`` and its ADBC error translation, over a mocked cursor. The real ADBC
introspection path is validated end-to-end against a recorded cassette in
``tests/integration/test_introspect.py``.

The mocks are deliberately untyped (``MagicMock`` pool / cursor and a patched
``connect()``), so the per-rule scope-disable below keeps basedpyright strict
quiet on the mock seam without a ``# type: ignore``.
"""
# Test-only mock seam: MagicMock pool/cursor and patch.object(engine, "connect")
# are untyped by construction. Scope-disable the rules the mock seam triggers
# under basedpyright strict (intentionally not a `# type: ignore`).
# pyright: reportUnknownMemberType=false

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from models import Sales


def _make_databricks_engine(**overrides: Any) -> Any:
    """
    Build a DatabricksEngine via the ``create_engine`` factory.

    The engine owns an ADBC pool (mocked at ``create_pool`` below) plus the
    Databricks dialect derived from the config type. Tests then patch
    ``engine.connect`` to drive execution through a mocked ADBC cursor.
    """
    from adbc_poolhouse import DatabricksConfig
    from pydantic import SecretStr

    from semolina.config import create_engine

    params: dict[str, Any] = {
        "host": "workspace.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc123",
        "token": SecretStr("dapi-test-token"),
    }
    params.update(overrides)
    # Avoid a live ADBC connect: create_pool returns a mock pool. The Engine
    # still owns it; execution is driven through engine.connect() below.
    with patch("semolina.config.create_pool", return_value=MagicMock(name="pool")):
        return create_engine(DatabricksConfig(**params))


def _patch_connect(engine: Any, cursor: Any) -> Any:
    """
    Patch ``engine.connect()`` to return a connection whose cursor is ``cursor``.

    Mirrors the real ADBC checkout seam used by
    :meth:`~semolina.engines.base.Engine.execute`: ``conn = self.connect()``
    then ``conn.cursor()`` (``connect()`` returns the pooled connection
    directly, not a context manager).
    """
    conn = MagicMock(name="conn")
    conn.cursor.return_value = cursor
    return patch.object(engine, "connect", return_value=conn)


def _located(payload: str) -> str:
    """Add the catalog, schema and table a real ``DESCRIBE ... AS JSON`` always reports."""
    data = json.loads(payload)
    data.setdefault("catalog_name", "main")
    data.setdefault("schema_name", "analytics")
    data.setdefault("table_name", "sales_view")
    return json.dumps(data)


class TestDatabricksEngineConstruction:
    """
    DatabricksEngine is built via create_engine and owns pool + dialect.

    Verifies the construction contract: create_engine selects the
    DatabricksEngine subclass, supplies the ADBC pool and the DatabricksDialect,
    and never connects at construction time.
    """

    def test_create_engine_returns_databricks_engine(self) -> None:
        """Should build a DatabricksEngine from a DatabricksConfig."""
        from semolina.engines.databricks import DatabricksEngine

        engine = _make_databricks_engine()
        assert isinstance(engine, DatabricksEngine)

    def test_engine_uses_databricks_dialect(self) -> None:
        """Should attach a DatabricksDialect derived from the config type."""
        from semolina.engines.sql import DatabricksDialect

        engine = _make_databricks_engine()
        assert isinstance(engine.dialect, DatabricksDialect)

    def test_engine_owns_pool_without_connecting(self) -> None:
        """Should hold the pool without checking out a connection at build time."""
        engine = _make_databricks_engine()
        # The mock pool is owned but connect() is not called during construction.
        engine._pool.connect.assert_not_called()  # noqa: SLF001  (test inspects owned pool)


class TestDatabricksEngineExecute:
    """
    DatabricksEngine.execute runs through the inherited ADBC pool path.

    Verifies that execution checks a connection out of the owned pool, runs the
    generated SQL through the ADBC cursor, and maps result rows -- using the
    base Engine.execute path (no native databricks.sql connector).
    """

    def test_execute_runs_sql_over_pooled_cursor(self) -> None:
        """
        The Databricks statement reaches the pooled cursor exactly, with no bound parameters.

        The expected SQL is a literal. Deriving it from the builder, as this test used to,
        made it pass whatever the builder produced.
        """
        engine = _make_databricks_engine()

        cursor = MagicMock(name="cursor")
        with _patch_connect(engine, cursor):
            engine.execute(Sales.query().metrics(Sales.revenue).dimensions(Sales.country))

        cursor.execute.assert_called_once_with(
            "SELECT MEASURE(`revenue`) AS `revenue`, `country` AS `country`\n"
            "FROM `sales_view`\n"
            "GROUP BY ALL",
            [],
        )

    def test_execute_binds_filter_values_on_the_pooled_cursor(self) -> None:
        """
        A filter value reaches the cursor as a bound parameter, not as a literal in the SQL.

        Foundry driver v0.2.0 accepts ``?`` bind parameters, so Databricks takes the same
        parameterized path as Snowflake.
        """
        engine = _make_databricks_engine()

        cursor = MagicMock(name="cursor")
        with _patch_connect(engine, cursor):
            engine.execute(
                Sales.query()
                .metrics(Sales.revenue)
                .dimensions(Sales.country)
                .where(Sales.country == "US")
            )

        cursor.execute.assert_called_once_with(
            "SELECT MEASURE(`revenue`) AS `revenue`, `country` AS `country`\n"
            "FROM `sales_view`\n"
            "WHERE `country` = ?\n"
            "GROUP BY ALL",
            ["US"],
        )

    def test_execute_returns_semolina_cursor(self) -> None:
        """Should wrap the post-execute ADBC cursor in a SemolinaCursor."""
        from semolina.cursor import SemolinaCursor

        engine = _make_databricks_engine()

        cursor = MagicMock(name="cursor")
        with _patch_connect(engine, cursor):
            query = Sales.query().metrics(Sales.revenue)
            result = engine.execute(query)

        assert isinstance(result, SemolinaCursor)


def _patch_introspect_cursor(engine: Any, cursor: Any) -> Any:
    """
    Patch ``engine.connect()`` for the ``with self.connect() as conn`` seam.

    Unlike :func:`_patch_connect` (used by ``execute()``, which calls
    ``connect()`` directly), :meth:`DatabricksEngine.introspect` uses
    ``connect()`` as a context manager, so the mock's ``__enter__`` must yield a
    connection whose ``cursor()`` returns ``cursor``.
    """
    conn = MagicMock(name="conn")
    conn.cursor.return_value = cursor
    connect_cm = MagicMock(name="connect_cm")
    connect_cm.__enter__.return_value = conn
    return patch.object(engine, "connect", return_value=connect_cm)


class TestDatabricksEngineIntrospect:
    """
    DatabricksEngine.introspect parses DESCRIBE TABLE EXTENDED ... AS JSON.

    These tests drive the JSON parsing and ADBC error translation over a mocked
    cursor. The real ADBC path is validated end-to-end against a recorded
    cassette in ``tests/integration/test_introspect.py``.
    """

    # Representative payload: two string dimensions and two bigint measures
    # (``is_measure``), as Databricks returns for a metric view.
    _DESCRIBE_JSON = json.dumps(
        {
            "columns": [
                {"name": "country", "type": {"name": "string", "collation": "UTF8_BINARY"}},
                {"name": "region", "type": {"name": "string"}},
                {"name": "revenue", "type": {"name": "bigint"}, "is_measure": True},
                {"name": "cost", "type": {"name": "bigint"}, "is_measure": True},
            ]
        }
    )

    def _introspect(
        self,
        engine: Any,
        payload: str,
        view: str = "sales_view",
        tag_rows: list[tuple[str, str, str]] | None = None,
    ) -> Any:
        """
        Run introspect() with a cursor whose fetchone() returns ``payload``.

        The payload gains the catalog, schema and table every real ``DESCRIBE`` reports,
        where it leaves them out, and the column-tags query returns ``tag_rows``.
        """
        cursor = MagicMock(name="cursor")
        cursor.fetchone.return_value = (_located(payload),)
        cursor.fetchall.return_value = tag_rows or []
        with _patch_introspect_cursor(engine, cursor):
            return engine.introspect(view)

    def test_parses_dimensions_and_measures(self) -> None:
        """is_measure -> metric, everything else -> dimension; view -> class name."""
        engine = _make_databricks_engine()
        view = self._introspect(engine, self._DESCRIBE_JSON)

        assert view.view_name == "sales_view"
        assert view.class_name == "SalesView"
        roles = {f.name: f.field_type for f in view.fields}
        assert roles == {
            "country": "dimension",
            "region": "dimension",
            "revenue": "metric",
            "cost": "metric",
        }

    def test_maps_types_to_python(self) -> None:
        """type.name -> Python annotation via databricks_type_to_python."""
        engine = _make_databricks_engine()
        view = self._introspect(engine, self._DESCRIBE_JSON)

        types = {f.name: f.data_type for f in view.fields}
        assert types == {"country": "str", "region": "str", "revenue": "int", "cost": "int"}

    def test_unmapped_type_becomes_todo(self) -> None:
        """A type with no clean Python equivalent yields a TODO placeholder."""
        payload = json.dumps({"columns": [{"name": "geo", "type": {"name": "geography"}}]})
        engine = _make_databricks_engine()
        view = self._introspect(engine, payload)

        assert view.fields[0].data_type == "TODO: geography"

    def test_reads_s_layer_column_tags_into_semantic_format(self) -> None:
        """``S_LAYER_KIND`` and ``S_LAYER_SPEC`` column tags make the block."""
        from s_layer_semantic_formats import from_compact_json

        spec = '{"value":{"currency":"USD"}}'
        view = self._introspect(
            _make_databricks_engine(),
            self._DESCRIBE_JSON,
            tag_rows=[
                ("revenue", "S_LAYER_KIND", "currency"),
                ("revenue", "S_LAYER_SPEC", spec),
                ("country", "S_LAYER_KIND", "category"),
            ],
        )

        fields = {f.name: f for f in view.fields}
        assert fields["revenue"].semantic_format == from_compact_json("currency", spec)
        assert fields["country"].semantic_format == from_compact_json("category", None)
        assert fields["region"].semantic_format is None

    def test_reads_tags_for_the_view_describe_resolved(self) -> None:
        """The catalog, schema and table come from ``DESCRIBE``, whatever the name spelling."""
        engine = _make_databricks_engine()
        cursor = MagicMock(name="cursor")
        cursor.fetchone.return_value = (
            _located(
                json.dumps(
                    {
                        "catalog_name": "main",
                        "schema_name": "it's",
                        "table_name": "sales_view",
                        "columns": [],
                    }
                )
            ),
        )
        cursor.fetchall.return_value = []
        with _patch_introspect_cursor(engine, cursor):
            engine.introspect("`it's`.sales_view")

        assert cursor.execute.call_args_list[1][0][0] == (
            "SELECT column_name, tag_name, tag_value FROM `main`.information_schema.column_tags"
            " WHERE schema_name = 'it\\'s' AND table_name = 'sales_view'"
            " AND tag_name IN ('S_LAYER_KIND', 'S_LAYER_SPEC')"
        )

    def test_refuses_tags_that_make_no_valid_block(self) -> None:
        with pytest.raises(ValueError, match="'country'"):
            self._introspect(
                _make_databricks_engine(),
                self._DESCRIBE_JSON,
                tag_rows=[("country", "S_LAYER_KIND", "currency")],
            )

    def test_reads_comment_display_name_and_synonyms(self) -> None:
        """
        ``comment`` sits on the column; ``display_name`` and ``synonyms`` under ``metadata``.

        The shape was checked live (DBSQL 2026.39) on a YAML 1.1 metric view. The view's
        own comment is the payload's top-level ``comment``.
        """
        payload = json.dumps(
            {
                "comment": "Gross sales by country",
                "columns": [
                    {
                        "name": "revenue",
                        "type": {"name": "bigint"},
                        "is_measure": True,
                        "comment": "Gross revenue",
                        "metadata": {
                            "display_name": "Revenue",
                            "synonyms": ["sales", "turnover"],
                        },
                    },
                    {"name": "country", "type": {"name": "string"}},
                ],
            }
        )
        engine = _make_databricks_engine()
        view = self._introspect(engine, payload)

        assert view.description == "Gross sales by country"
        revenue, country = view.fields
        assert (revenue.description, revenue.label, revenue.synonyms) == (
            "Gross revenue",
            "Revenue",
            ("sales", "turnover"),
        )
        assert (country.description, country.label, country.synonyms) == ("", None, ())

    def test_non_round_tripping_name_sets_source_name(self) -> None:
        """A mixed-case column name preserves the exact warehouse name in source_name."""
        payload = json.dumps(
            {"columns": [{"name": "MyMetric", "type": {"name": "bigint"}, "is_measure": True}]}
        )
        engine = _make_databricks_engine()
        view = self._introspect(engine, payload)

        assert view.fields[0].name == "mymetric"
        assert view.fields[0].source_name == "MyMetric"

    def test_non_identifier_names_become_valid_field_names(self) -> None:
        """A column that is no Python identifier gets a valid name and keeps its column."""
        payload = json.dumps(
            {
                "columns": [
                    {"name": "what?", "type": {"name": "string"}},
                    {"name": "Order Date", "type": {"name": "date"}},
                    {"name": "class", "type": {"name": "string"}},
                    {"name": "1st", "type": {"name": "string"}},
                    {"name": "order_date", "type": {"name": "string"}},
                ]
            }
        )
        engine = _make_databricks_engine()
        view = self._introspect(engine, payload)

        assert [(f.name, f.source_name) for f in view.fields] == [
            ("what_", "what?"),
            ("order_date_2", "Order Date"),
            ("class_", "class"),
            ("_1st", "1st"),
            ("order_date", None),
        ]

    @pytest.mark.parametrize(
        ("view", "described"),
        [
            ("sales_view", "`sales_view`"),
            ("analytics.Sales_View", "`analytics`.`sales_view`"),
            ("main.analytics.`Order Book`", "`main`.`analytics`.`Order Book`"),
            ("`it`s`", "`it``s`"),
        ],
    )
    def test_quotes_the_view_name(self, view: str, described: str) -> None:
        """
        Each part of the name is quoted, so a space or backtick parses (FILT-05).

        A bare part folds to lowercase, as the query builder folds it; a backtick-quoted
        part keeps its case and has its own backticks escaped.
        """
        engine = _make_databricks_engine()
        cursor = MagicMock(name="cursor")
        cursor.fetchone.return_value = (_located(self._DESCRIBE_JSON),)
        cursor.fetchall.return_value = []
        with _patch_introspect_cursor(engine, cursor):
            result = engine.introspect(view)

        assert cursor.execute.call_args_list[0][0][0] == (
            f"DESCRIBE TABLE EXTENDED {described} AS JSON"
        )
        assert result.view_name == view

    def test_dotted_quoted_view_name_raises(self) -> None:
        """A '.' inside a backtick-quoted part cannot be told from a separator (FILT-06)."""
        engine = _make_databricks_engine()
        with pytest.raises(ValueError, match="inside a quoted segment"):
            engine.introspect("`a.b`")

    def test_view_not_found_raises_semolina_error(self) -> None:
        """ADBC ProgrammingError -> SemolinaViewNotFoundError."""
        pytest.importorskip("adbc_driver_manager")
        from adbc_driver_manager import (
            AdbcStatusCode,
            ProgrammingError,
        )

        from semolina.engines.base import SemolinaViewNotFoundError

        engine = _make_databricks_engine()
        cursor = MagicMock(name="cursor")
        cursor.execute.side_effect = ProgrammingError(
            "Table or view 'missing_view' not found",
            status_code=AdbcStatusCode.NOT_FOUND,
        )
        with _patch_introspect_cursor(engine, cursor), pytest.raises(SemolinaViewNotFoundError):
            engine.introspect("missing_view")

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
        ``OperationalError`` was translated as well.
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
        engine = _make_databricks_engine()
        cursor = MagicMock(name="cursor")
        cursor.execute.side_effect = error
        with (
            _patch_introspect_cursor(engine, cursor),
            pytest.raises(expected_error) as raised,
        ):
            engine.introspect("sales_view")
        assert raised.value.__cause__ is error

    def test_connection_error_raises_semolina_error(self) -> None:
        """ADBC OperationalError -> SemolinaConnectionError."""
        pytest.importorskip("adbc_driver_manager")
        from adbc_driver_manager import (
            AdbcStatusCode,
            OperationalError,
        )

        from semolina.engines.base import SemolinaConnectionError

        engine = _make_databricks_engine()
        cursor = MagicMock(name="cursor")
        cursor.execute.side_effect = OperationalError(
            "connection failed",
            status_code=AdbcStatusCode.IO,
        )
        with _patch_introspect_cursor(engine, cursor), pytest.raises(SemolinaConnectionError):
            engine.introspect("sales_view")
