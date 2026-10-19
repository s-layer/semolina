"""
Unit tests for the Databricks Python-connector backend (``DatabricksPythonConfig``).

adbc-poolhouse's ``DatabricksPythonConfig`` pools ``databricks-sql-connector`` connections
behind an adapter that presents the ADBC DBAPI cursor surface. It is the only route to
Lakehouse//RT, which rejects the Thrift transport the ADBC Databricks driver speaks. To
Semolina it is another way to reach the same warehouse, so it must resolve to the
Databricks dialect and ``DatabricksEngine``.

These tests run through the real poolhouse pool and adapter. Only the connector's own
``databricks.sql.connect`` is replaced, by a fake connector connection serving canned Arrow
results, so what the fake records is exactly what Semolina's statements look like when they
reach the connector.
"""
# The fake connector is untyped by construction, as the real one ships no stubs.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from models import Sales

from semolina.dialect import Dialect

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

pa = pytest.importorskip("pyarrow")
pytest.importorskip("databricks.sql")


class _FakeConnectorCursor:
    """
    The slice of a ``databricks.sql`` cursor that poolhouse's adapter calls.

    Every executed ``(operation, parameters)`` pair is recorded on the owning connection.
    Results come from that connection's ``respond`` callable as a ``pyarrow.Table``, and the
    DB-API row accessors read the same table, as the connector's do.
    """

    def __init__(self, connection: _FakeConnectorConnection) -> None:
        self._connection = connection
        self._table: Any = None
        self._pos = 0
        self.arraysize = 2
        self.rowcount = -1
        self.rownumber = None

    def execute(self, operation: str, parameters: Any = None) -> None:
        self._connection.executed.append((operation, parameters))
        self._table = self._connection.respond(operation)
        self._pos = 0

    @property
    def description(self) -> list[tuple[Any, ...]] | None:
        if self._table is None:
            return None
        # The connector reports a type *name* here, not a pyarrow type.
        return [(f.name, str(f.type), None, None, None, None, True) for f in self._table.schema]

    def _take(self, n: int | None) -> Any:
        end = self._table.num_rows if n is None else min(self._pos + n, self._table.num_rows)
        chunk = self._table.slice(self._pos, end - self._pos)
        self._pos = end
        return chunk

    def _rows(self, table: Any) -> list[tuple[Any, ...]]:
        columns = [col.to_pylist() for col in table.columns]
        return list(zip(*columns, strict=True))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows(self._take(None))

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        return self._rows(self._take(size))

    def fetchone(self) -> tuple[Any, ...] | None:
        rows = self._rows(self._take(1))
        return rows[0] if rows else None

    def fetchall_arrow(self) -> Any:
        return self._take(None)

    def fetchmany_arrow(self, size: int) -> Any:
        return self._take(size)

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeConnectorConnection:
    """A fake ``databricks.sql`` connection: hands out cursors, records statements."""

    def __init__(self, respond: Callable[[str], Any]) -> None:
        self.respond = respond
        self.executed: list[tuple[str, Any]] = []
        self.autocommit = True
        self.closed = False

    def cursor(self) -> _FakeConnectorCursor:
        return _FakeConnectorCursor(self)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


_SALES = pa.table({"revenue": pa.array([3000, 2800], pa.int64()), "country": ["US", "CA"]})


@pytest.fixture
def connector() -> Iterator[list[_FakeConnectorConnection]]:
    """
    Replace ``databricks.sql.connect`` with a factory of fake connector connections.

    Yields the list of connections opened, so a test reads the statements each received.
    Every query answers with ``_SALES``.
    """
    opened: list[_FakeConnectorConnection] = []

    def _connect(**_kwargs: Any) -> _FakeConnectorConnection:
        conn = _FakeConnectorConnection(lambda _sql: _SALES)
        opened.append(conn)
        return conn

    with patch("databricks.sql.connect", _connect):
        yield opened


def _config(**overrides: Any) -> Any:
    from adbc_poolhouse import DatabricksPythonConfig
    from pydantic import SecretStr

    params: dict[str, Any] = {
        "host": "workspace.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc123",
        "token": SecretStr("dapi-test-token"),
        "pool_size": 1,
        "max_overflow": 0,
    }
    params.update(overrides)
    return DatabricksPythonConfig(**params)


def _statements(opened: list[_FakeConnectorConnection]) -> list[tuple[str, Any]]:
    return [stmt for conn in opened for stmt in conn.executed]


_FILTERED_SQL = (
    "SELECT MEASURE(`revenue`) AS `revenue`, `country` AS `country`\n"
    "FROM `sales_view`\n"
    "WHERE `country` = ?\n"
    "GROUP BY ALL"
)


class TestResolution:
    """``DatabricksPythonConfig`` resolves to Semolina's Databricks backend."""

    def test_config_maps_to_databricks_dialect(self) -> None:
        from semolina.config import _dialect_for_config_type

        assert _dialect_for_config_type(_config()) is Dialect.DATABRICKS

    def test_create_engine_builds_a_databricks_engine(
        self, connector: list[_FakeConnectorConnection]
    ) -> None:
        from semolina.config import create_engine
        from semolina.engines.databricks import DatabricksEngine
        from semolina.engines.sql import DatabricksDialect

        engine = create_engine(_config())
        try:
            assert type(engine) is DatabricksEngine
            assert isinstance(engine.dialect, DatabricksDialect)
        finally:
            engine.dispose()

    def test_dispose_closes_the_pooled_connector_connections(
        self, connector: list[_FakeConnectorConnection]
    ) -> None:
        """
        ``dispose()`` tears the pool down through poolhouse's ``close_pool``.

        A pool over a non-ADBC backend has no ``_adbc_source``, so a check on that attribute
        alone sent it to ``QueuePool.close()``, which does not exist.
        """
        from semolina.config import create_engine

        engine = create_engine(_config())
        with engine.execute(Sales.query().metrics(Sales.revenue)) as cursor:
            cursor.fetchall()

        engine.dispose()

        assert len(connector) == 1
        assert connector[0].closed

    def test_toml_type_databricks_python(
        self, connector: list[_FakeConnectorConnection], tmp_path: Path
    ) -> None:
        """``type = "databricks-python"`` builds a ``DatabricksPythonConfig`` engine."""
        from adbc_poolhouse import DatabricksPythonConfig

        from semolina.config import create_engine
        from semolina.engines.databricks import DatabricksEngine

        toml_file = tmp_path / ".semolina.toml"
        toml_file.write_text(
            "[connections.rt]\n"
            'type = "databricks-python"\n'
            'host = "workspace.cloud.databricks.com"\n'
            'http_path = "/sql/1.0/warehouses/abc123"\n'
            'token = "dapi-test-token"\n'
        )

        engine = create_engine("rt", config_path=toml_file)
        try:
            assert type(engine) is DatabricksEngine
            assert type(engine._config) is DatabricksPythonConfig  # noqa: SLF001
        finally:
            engine.dispose()

    def test_toml_type_databricks_still_means_the_adbc_driver(self, tmp_path: Path) -> None:
        """Adding the connector type must not change what ``type = "databricks"`` builds."""
        from adbc_poolhouse import DatabricksConfig

        from semolina.config import _read_connection

        toml_file = tmp_path / ".semolina.toml"
        toml_file.write_text(
            "[connections.default]\n"
            'type = "databricks"\n'
            'host = "workspace.cloud.databricks.com"\n'
            'http_path = "/sql/1.0/warehouses/abc123"\n'
            'token = "dapi-test-token"\n'
        )

        wh_config, dialect = _read_connection("default", toml_file)

        assert type(wh_config) is DatabricksConfig
        assert dialect is Dialect.DATABRICKS

    def test_warehouse_config_reads_databricks_python_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from adbc_poolhouse import DatabricksPythonConfig

        from semolina.config import warehouse_config

        monkeypatch.setenv("DATABRICKS_PYTHON_HOST", "rt.cloud.databricks.com")
        monkeypatch.setenv("DATABRICKS_PYTHON_HTTP_PATH", "/sql/1.0/warehouses/rt")
        monkeypatch.setenv("DATABRICKS_PYTHON_TOKEN", "dapi-env-token")
        monkeypatch.setenv("SEMOLINA_ENV_FILE", str(tmp_path / "absent.env"))

        config = warehouse_config("databricks-python", config_path=tmp_path / "absent.toml")

        assert type(config) is DatabricksPythonConfig
        assert config.host == "rt.cloud.databricks.com"


class TestQueryPath:
    """Queries reach the connector as the ADBC Databricks driver would receive them."""

    def test_filter_value_reaches_the_connector_as_a_positional_parameter(
        self, connector: list[_FakeConnectorConnection]
    ) -> None:
        """
        The statement keeps its ``?`` marker and the value travels as a parameter list.

        The connector's native parameter mode binds ``?`` markers positionally from a list,
        so Semolina's qmark SQL needs no rewriting for this backend.
        """
        from semolina.config import create_engine

        engine = create_engine(_config())
        try:
            query = (
                Sales.query()
                .metrics(Sales.revenue)
                .dimensions(Sales.country)
                .where(Sales.country == "US")
            )
            with engine.execute(query) as cursor:
                rows = cursor.fetchall_rows()
        finally:
            engine.dispose()

        assert _statements(connector) == [(_FILTERED_SQL, ["US"])]
        assert [(row.revenue, row.country) for row in rows] == [(3000, "US"), (2800, "CA")]

    def test_arrow_reads_go_through_the_adapter(
        self, connector: list[_FakeConnectorConnection]
    ) -> None:
        """Both Arrow reads return the connector's table, streamed in ``arraysize`` chunks."""
        from semolina.config import create_engine

        engine = create_engine(_config())
        try:
            query = Sales.query().metrics(Sales.revenue).dimensions(Sales.country)
            with engine.execute(query) as cursor:
                table = cursor.fetch_arrow_table()
            with engine.execute(query) as cursor:
                streamed = pa.Table.from_batches(list(cursor.fetch_record_batch()))
        finally:
            engine.dispose()

        assert table.equals(_SALES)
        assert streamed.equals(_SALES)

    def test_introspect_reads_describe_json(
        self, connector: list[_FakeConnectorConnection]
    ) -> None:
        from semolina.config import create_engine

        describe = json.dumps(
            {
                "columns": [
                    {"name": "country", "type": {"name": "string"}},
                    {"name": "revenue", "type": {"name": "bigint"}, "is_measure": True},
                ]
            }
        )

        def _connect(**_kwargs: Any) -> _FakeConnectorConnection:
            conn = _FakeConnectorConnection(lambda _sql: pa.table({"json_metadata": [describe]}))
            connector.append(conn)
            return conn

        with patch("databricks.sql.connect", _connect):
            engine = create_engine(_config())
            try:
                view = engine.introspect("sales_view")
            finally:
                engine.dispose()

        assert _statements(connector) == [("DESCRIBE TABLE EXTENDED `sales_view` AS JSON", None)]
        assert {f.name: f.field_type for f in view.fields} == {
            "country": "dimension",
            "revenue": "metric",
        }


@pytest.mark.anyio
async def test_async_engine_runs_the_same_statement(
    connector: list[_FakeConnectorConnection],
) -> None:
    """``create_async_engine`` accepts the config and sends the same statement and parameters."""
    pytest.importorskip("anyio")

    from semolina.config import create_async_engine
    from semolina.engines.sql import DatabricksDialect

    engine = create_async_engine(_config())
    try:
        assert isinstance(engine.dialect, DatabricksDialect)
        query = (
            Sales.query()
            .metrics(Sales.revenue)
            .dimensions(Sales.country)
            .where(Sales.country == "US")
        )
        async with await engine.aexecute(query) as cursor:
            rows = await cursor.fetchall_rows()
    finally:
        await engine.dispose()

    assert _statements(connector) == [(_FILTERED_SQL, ["US"])]
    assert [(row.revenue, row.country) for row in rows] == [(3000, "US"), (2800, "CA")]
