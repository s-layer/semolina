"""
End-to-end codegen tests against DuckDB, Snowflake, and Databricks backends.

The DuckDB case drives the full CLI (it takes ``--database`` and needs no
credentials). The Snowflake and Databricks cases drive ``engine.introspect()``
over a mocked ADBC-cursor seam (built via ``create_engine(<Config>(...))`` with a
mocked ``create_pool``), so they run fully offline: Snowflake parses synthetic
``SHOW COLUMNS`` rows, Databricks parses a synthetic
``DESCRIBE TABLE EXTENDED ... AS JSON`` payload. Both render to a snapshot.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from duckdb_extension import load_semantic_views
from typer.testing import CliRunner

from semolina.cli import app

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from syrupy.assertion import SnapshotAssertion

    from semolina.engines.base import Engine

runner = CliRunner()


@pytest.fixture
def probe_engine() -> Generator[Engine]:
    """
    Yield the type-fidelity probe's in-memory DuckDB engine, closing its pool on teardown.

    Mirrors ``tests/unit/test_type_fidelity_duckdb.py``'s fixture of the same name. That
    module owns the record/replay contract this fixture inherits: the probe runs live and
    in-process, so no test using it may ever carry ``pytest.mark.adbc_cassette``.
    """
    from adbc_poolhouse import close_pool
    from type_fidelity_probe import make_probe_engine

    engine = make_probe_engine()
    yield engine
    close_pool(engine._pool)


def test_codegen_file_backed_duckdb(
    duckdb_file_backed_db: Path,
    snapshot: SnapshotAssertion,
) -> None:
    """
    Codegen against an on-disk DuckDB ``.db`` produces the expected model class.

    Drives the full CLI surface end-to-end (DKGEN-04 success criterion #1):
    ``_resolve_backend`` -> path normalization -> real ``DuckDBEngine`` ->
    real introspection of a generated semantic view -> renderer.
    """
    result = runner.invoke(
        app,
        [
            "codegen",
            "sales_view",
            "--backend",
            "duckdb",
            "--database",
            str(duckdb_file_backed_db),
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.output == snapshot


def test_codegen_names_import_named_views_clear_of_the_imports(tmp_path: Path) -> None:
    """
    ``semolina codegen metric any -b duckdb`` exits 0 with ``ViewMetric`` and ``ViewAny``.

    A built-in backend names the class, so the name is codegen's to choose. These views
    generated before the import-shadowing check existed, and that check refused them with a
    remedy that could not work: alone in a module, ``class Metric`` still rebinds the import.
    Driven through a real ``DuckDBEngine`` so the name comes from the path a user runs.
    """
    import ast

    import duckdb

    db_path = tmp_path / "imports.db"
    conn = duckdb.connect(database=str(db_path))
    try:
        load_semantic_views(conn)
        conn.execute("CREATE TABLE t (id INTEGER, revenue INTEGER, country VARCHAR)")
        for name in ("metric", "any"):
            conn.execute(
                # Quoted: DuckDB 2.0's parser reserves ``any``.
                f'CREATE SEMANTIC VIEW "{name}" AS TABLES (s AS t PRIMARY KEY (id)) '
                "DIMENSIONS (s.country AS country) METRICS (s.revenue AS SUM(s.revenue))"
            )
    finally:
        conn.close()

    result = runner.invoke(
        app, ["codegen", "metric", "any", "--backend", "duckdb", "--database", str(db_path)]
    )

    assert result.exit_code == 0, result.output
    classes = [
        node.name for node in ast.parse(result.stdout).body if isinstance(node, ast.ClassDef)
    ]
    assert classes == ["ViewMetric", "ViewAny"], result.stdout
    namespace: dict[str, object] = {}
    exec(compile(result.stdout, "<generated>", "exec"), namespace)  # noqa: S102
    assert getattr(namespace["Metric"], "__module__", None) == "semolina.fields"


def test_codegen_on_a_view_duckdb_cannot_describe_exits_6(tmp_path: Path) -> None:
    """
    A view whose definition no longer binds exits 6, not 4 (COR-2).

    Exit 4 says the connection or credentials failed. Here the connection is fine: the
    warehouse cannot describe the view, because a column its metric reads was dropped. A
    script branching on the exit code must not be sent to check its credentials.
    """
    import duckdb

    from semolina.cli.dto_codegen import EXIT_PROBE_FAILED

    db_path = tmp_path / "broken.db"
    conn = duckdb.connect(database=str(db_path))
    try:
        load_semantic_views(conn)
        conn.execute("CREATE TABLE t (id INTEGER, region VARCHAR, amount DOUBLE)")
        conn.execute(
            "CREATE SEMANTIC VIEW v AS TABLES (o AS t PRIMARY KEY (id)) "
            "DIMENSIONS (o.region AS o.region) METRICS (o.revenue AS SUM(o.amount))"
        )
        conn.execute("ALTER TABLE t DROP COLUMN amount")
    finally:
        conn.close()

    result = runner.invoke(app, ["codegen", "v", "--backend", "duckdb", "--database", str(db_path)])

    assert result.exit_code == EXIT_PROBE_FAILED, result.output
    assert "amount" in result.stderr, result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_codegen_live_duckdb_decimal_metric(probe_engine: Engine) -> None:
    """
    A live DuckDB ``DECIMAL`` metric renders as ``Metric[decimal.Decimal | None]()``.

    The tracer for the decimal annotation: one column carried from a real in-memory warehouse
    through ``Engine.introspect`` -> ``duckdb_type_to_python`` -> ``_build_model_context``
    -> the Jinja2 template -> ``render_and_format``, asserted on the emitted source. A
    ``decimal.Decimal`` annotation is unusable without the matching ``import decimal``, so
    both halves are asserted together rather than in separate unit tests.
    """
    from semolina.codegen.python_renderer import render_and_format

    view = probe_engine.introspect("type_fidelity_view")
    source = render_and_format([view])

    assert "import decimal" in source, source
    assert "total_order_value = Metric[decimal.Decimal | None]()" in source, source
    assert "TODO: DECIMAL" not in source, source


def test_codegen_snowflake_field_types(snapshot: SnapshotAssertion) -> None:
    """
    Offline Snowflake introspect -> render emits Metric, Dimension, and Fact.

    Drives ``SnowflakeEngine.introspect`` over a mocked ADBC-cursor seam (the
    engine is built via ``create_engine(SnowflakeConfig(...))`` with a mocked
    ``create_pool``, then ``connect()`` is patched to yield the cursor), so the
    test runs fully offline. The synthetic ``SHOW COLUMNS`` rows exercise all
    three Snowflake roles: METRIC -> Metric[int], DIMENSION -> Dimension[str],
    FACT -> Fact[datetime.date].

    The column names are UPPERCASE (``REVENUE``, ``COUNTRY``, ``DATE_KEY``) —
    the default Snowflake casing that round-trips through ``name.lower().upper()``
    back to the original, so no ``source=`` kwarg is emitted. This is the common
    path; the quoted-lowercase path that *does* set ``source=`` is covered by
    ``test_source_name_set_emits_source_kwarg`` in ``test_python_renderer.py``.
    """
    from adbc_poolhouse import SnowflakeConfig

    from semolina.codegen.python_renderer import render_and_format
    from semolina.config import create_engine

    mock_cursor = MagicMock()
    mock_cursor.description = [
        ("column_name",),
        ("kind",),
        ("data_type",),
        ("comment",),
    ]
    mock_cursor.fetchall.return_value = [
        ("REVENUE", "METRIC", json.dumps({"type": "FIXED", "scale": 0}), ""),
        ("COUNTRY", "DIMENSION", json.dumps({"type": "TEXT"}), ""),
        ("DATE_KEY", "FACT", json.dumps({"type": "DATE"}), ""),
    ]
    # The session's database and schema, which complete the bare view name.
    mock_cursor.fetchone.return_value = ("ANALYTICS", "PUBLIC")
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _connect() -> Generator[Any]:
        conn = MagicMock(name="conn")
        conn.cursor.return_value = mock_cursor
        yield conn

    with patch("semolina.config.create_pool", return_value=MagicMock(name="pool")):
        engine = create_engine(SnowflakeConfig(account="test", user="user"))
    with patch.object(engine, "connect", side_effect=_connect):
        view = engine.introspect("sales_view")

    assert render_and_format([view]) == snapshot


def test_codegen_databricks_field_types(snapshot: SnapshotAssertion) -> None:
    """
    Offline Databricks introspect -> render emits Metric and Dimension.

    Drives ``DatabricksEngine.introspect`` over a mocked ADBC-cursor seam (the
    engine is built via ``create_engine(DatabricksConfig(...))`` with a mocked
    ``create_pool``, then ``connect()`` is patched to yield a cursor whose
    ``fetchone()`` returns the ``DESCRIBE TABLE EXTENDED ... AS JSON`` payload),
    so the test runs fully offline. The synthetic columns exercise both
    Databricks roles: ``is_measure`` -> Metric[int], plain column ->
    Dimension[str] (bigint -> int, string -> str).
    """
    from adbc_poolhouse import DatabricksConfig
    from pydantic import SecretStr

    from semolina.codegen.python_renderer import render_and_format
    from semolina.config import create_engine

    describe_json = json.dumps(
        {
            "catalog_name": "main",
            "schema_name": "analytics",
            "table_name": "sales_view",
            "columns": [
                {"name": "revenue", "type": {"name": "bigint"}, "is_measure": True},
                {"name": "cost", "type": {"name": "bigint"}, "is_measure": True},
                {"name": "country", "type": {"name": "string"}},
                {"name": "region", "type": {"name": "string"}},
            ],
        }
    )

    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (describe_json,)
    mock_cursor.fetchall.return_value = []  # no column tags

    @contextmanager
    def _connect() -> Generator[Any]:
        conn = MagicMock(name="conn")
        conn.cursor.return_value = mock_cursor
        yield conn

    with patch("semolina.config.create_pool", return_value=MagicMock(name="pool")):
        engine = create_engine(
            DatabricksConfig(
                host="workspace.cloud.databricks.com",
                http_path="/sql/1.0/warehouses/abc",
                token=SecretStr("dapi-test-token"),
            )
        )
    with patch.object(engine, "connect", side_effect=_connect):
        view = engine.introspect("sales_view")

    assert render_and_format([view]) == snapshot


@pytest.fixture
def duckdb_odd_names_db(tmp_path: Path) -> Path:
    """
    Build an on-disk DuckDB database whose semantic view has non-identifier field names.

    Args:
        tmp_path: pytest's per-test temporary directory.

    Returns:
        Path to the ``.db`` file holding the ``odd_names`` semantic view.
    """
    import duckdb

    db_path = tmp_path / "odd.db"
    conn = duckdb.connect(database=str(db_path))
    try:
        load_semantic_views(conn)
        conn.execute('CREATE TABLE odd_data (id INTEGER, "Order Date" VARCHAR, amount DOUBLE)')
        conn.execute(
            "CREATE SEMANTIC VIEW odd_names AS "
            "TABLES (o AS odd_data PRIMARY KEY (id)) "
            'DIMENSIONS (o."Order Date" AS o."Order Date", o."what?" AS o.id, '
            'o."class" AS o.id, o."1st" AS o.id, o."a b" AS o.id, o.a_b AS o.id, '
            'o."?" AS o.id) '
            'METRICS (o."Total!" AS SUM(o.amount))'
        )
    finally:
        conn.close()
    return db_path


def test_codegen_cli_non_identifier_names_emit_valid_python(duckdb_odd_names_db: Path) -> None:
    """
    ``semolina codegen`` turns non-identifier field names into a module that parses.

    Drives the whole CLI against a real DuckDB semantic view, then parses the output with
    :mod:`ast` and checks every field the generated class declares, by name and ``source``.
    """
    import ast

    result = runner.invoke(
        app,
        ["codegen", "odd_names", "--backend", "duckdb", "--database", str(duckdb_odd_names_db)],
    )
    assert result.exit_code == 0, result.output

    tree = ast.parse(result.stdout)
    (model,) = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    declared: dict[str, str | None] = {}
    for node in model.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            (target,) = node.targets
            assert isinstance(target, ast.Name)
            sources = [
                kw.value.value
                for kw in node.value.keywords
                if kw.arg == "source" and isinstance(kw.value, ast.Constant)
            ]
            declared[target.id] = str(sources[0]) if sources else None
    assert declared == {
        "Order_Date": "Order Date",
        "what_": "what?",
        "class_": "class",
        "_1st": "1st",
        "a_b_2": "a b",
        "a_b": None,
        "column": "?",
        "Total_": "Total!",
    }


@pytest.mark.parametrize("backend", ["snowflake", "databricks"])
def test_codegen_non_identifier_names_render_valid_python(backend: str) -> None:
    """
    Offline Snowflake and Databricks introspection renders non-identifier names as valid Python.

    The same five awkward columns go through each engine's ``introspect()`` over a mocked
    ADBC cursor, then through ``render_and_format``; the result must parse, and each field
    must carry its exact column as ``source=``.
    """
    import ast

    from adbc_poolhouse import DatabricksConfig, SnowflakeConfig
    from pydantic import SecretStr

    from semolina.codegen.python_renderer import render_and_format
    from semolina.config import create_engine

    columns = ["what?", "Order Date", "class", "1st", "?"]
    mock_cursor = MagicMock()
    if backend == "snowflake":
        config: Any = SnowflakeConfig(account="test", user="user")
        mock_cursor.description = [("column_name",), ("kind",), ("data_type",), ("comment",)]
        mock_cursor.fetchall.return_value = [
            (name, "DIMENSION", json.dumps({"type": "TEXT"}), "") for name in columns
        ]
        mock_cursor.fetchone.return_value = ("ANALYTICS", "PUBLIC")
    else:
        config = DatabricksConfig(
            host="workspace.cloud.databricks.com",
            http_path="/sql/1.0/warehouses/abc",
            token=SecretStr("dapi-test-token"),
        )
        payload = {
            "catalog_name": "main",
            "schema_name": "analytics",
            "table_name": "odd_view",
            "columns": [{"name": name, "type": {"name": "string"}} for name in columns],
        }
        mock_cursor.fetchone.return_value = (json.dumps(payload),)
        mock_cursor.fetchall.return_value = []  # no column tags
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _connect() -> Generator[Any]:
        conn = MagicMock(name="conn")
        conn.cursor.return_value = mock_cursor
        yield conn

    with patch("semolina.config.create_pool", return_value=MagicMock(name="pool")):
        engine = create_engine(config)
    with patch.object(engine, "connect", side_effect=_connect):
        view = engine.introspect("odd_view")

    source = render_and_format([view])
    ast.parse(source)
    expected_names = ["what_", "order_date", "class_", "_1st", "column"]
    for name, column in zip(expected_names, columns, strict=True):
        assert f'{name} = Dimension[str](source="{column}")' in source, source


def test_codegen_check_accepts_its_own_output_for_renamed_fields(
    duckdb_odd_names_db: Path, tmp_path: Path
) -> None:
    """
    ``semolina codegen --check`` reports no drift against a model ``codegen`` just wrote.

    Every field of the ``odd_names`` view but one is renamed and carries ``source=``, so this
    is the round trip a user runs in CI right after generating the model.
    """
    database = ["--backend", "duckdb", "--database", str(duckdb_odd_names_db)]
    generated = runner.invoke(app, ["codegen", "odd_names", *database])
    assert generated.exit_code == 0, generated.output
    model_path = tmp_path / "odd_models.py"
    model_path.write_text(generated.stdout, encoding="utf-8")

    checked = runner.invoke(
        app, ["codegen", "odd_names", "--check", "--model", str(model_path), *database]
    )

    assert checked.exit_code == 0, checked.output


def test_codegen_cli_column_named_like_an_import_emits_importable_module(tmp_path: Path) -> None:
    """
    A ``datetime`` column ahead of another date column generates a module that imports.

    Unrenamed, the first field rebinds ``datetime`` in the class body and the second field's
    ``Dimension[datetime.date]`` fails at import with AttributeError.
    """
    import importlib.util

    import duckdb

    db_path = tmp_path / "dated.db"
    conn = duckdb.connect(database=str(db_path))
    try:
        load_semantic_views(conn)
        conn.execute("CREATE TABLE dated_data (id INTEGER, datetime DATE, sold DATE)")
        conn.execute(
            "CREATE SEMANTIC VIEW dated AS "
            "TABLES (d AS dated_data PRIMARY KEY (id)) "
            "DIMENSIONS (d.datetime AS d.datetime, d.sold AS d.sold) "
            "METRICS (d.total AS COUNT(d.id))"
        )
    finally:
        conn.close()

    result = runner.invoke(
        app, ["codegen", "dated", "--backend", "duckdb", "--database", str(db_path)]
    )
    assert result.exit_code == 0, result.output
    module_path = tmp_path / "dated_models.py"
    module_path.write_text(result.stdout, encoding="utf-8")

    spec = importlib.util.spec_from_file_location("_dated_models_under_test", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    fields = module.Dated._fields
    assert {name: f.source for name, f in fields.items()} == {
        "datetime_": "datetime",
        "sold": None,
        "total": None,
    }
