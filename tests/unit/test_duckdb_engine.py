"""
Unit tests for DuckDBEngine introspection over a real in-memory ADBC pool.

DuckDB introspection runs over the Engine's ADBC pool rather than the native
``duckdb`` module: ``engine.introspect(view)`` checks out a connection via
``engine.connect()`` and runs ``DESCRIBE SEMANTIC VIEW`` / ``DESCRIBE SELECT``
through the ADBC cursor. The ``semantic_views`` community extension is loaded by
a ``connect`` event listener on the pool, so these tests build a
real in-memory DuckDB Engine via ``create_engine(DuckDBConfig(...))`` and
introspect a real semantic view rather than monkeypatching the native module.

The fixtures reach the pool through ``engine._pool`` for teardown; the
per-rule scope-disable below keeps basedpyright strict quiet on that private
access without a ``# type: ignore``.
"""
# Test-only: the introspection fixtures reach the owned pool via engine._pool
# for close_pool teardown. Scope-disable the private-access rule that triggers
# (intentionally not a `# type: ignore`).
# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from semolina.codegen.introspector import IntrospectedField, IntrospectedView
from semolina.engines.base import SemolinaViewNotFoundError

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

pytest.importorskip("adbc_driver_duckdb")


def _create_view_sql(view_name: str = "orders") -> list[str]:
    """
    Build DDL for a small DuckDB semantic view used by the introspection tests.

    Produces one dimension (region: VARCHAR -> str) and one PUBLIC metric
    (revenue: SUM -> float), plus a PRIVATE metric that introspection must
    exclude.
    """
    return [
        "CREATE TABLE orders_data (id INTEGER, region VARCHAR, amount DOUBLE, cost DOUBLE)",
        "INSERT INTO orders_data VALUES (1, 'US', 100.0, 10.0), (2, 'CA', 200.0, 20.0)",
        f"""
        CREATE OR REPLACE SEMANTIC VIEW {view_name} AS
        TABLES (o AS orders_data PRIMARY KEY (id))
        DIMENSIONS (o.region AS o.region)
        METRICS (
            o.revenue AS SUM(o.amount),
            PRIVATE o.internal_cost AS SUM(o.cost)
        )
        """,
    ]


@pytest.fixture
def duckdb_engine() -> Iterator[Any]:
    """
    Build a real in-memory DuckDB Engine with the semantic_views extension.

    Mirrors the ``duckdb_pool`` conftest analog: ``create_engine(DuckDBConfig)``
    owns an ADBC pool whose ``connect`` event loads ``semantic_views``. The
    semantic view DDL is created on the live connection so ``introspect`` runs
    the real two-pass DESCRIBE path. Teardown closes the pool through the engine.
    """
    from adbc_poolhouse import DuckDBConfig, close_pool

    from semolina.config import create_engine

    engine = create_engine(DuckDBConfig(database=":memory:", pool_size=1))
    with engine.connect() as conn:
        cur = conn.cursor()
        for stmt in _create_view_sql():
            cur.execute(stmt)
        cur.close()
        conn.commit()
    try:
        yield engine
    finally:
        close_pool(engine._pool)


class TestDuckDBEngineIntrospect:
    """
    Test DuckDBEngine.introspect() over a real in-memory ADBC pool.

    Verifies the two-pass introspection (DESCRIBE SEMANTIC VIEW for structure +
    DESCRIBE SELECT for types), PUBLIC/PRIVATE access handling, type mapping,
    PascalCase class names, and schema-qualified name stripping.
    """

    def test_introspect_basic_dimension_and_metric(self, duckdb_engine: Any) -> None:
        """Should parse dimensions and metrics into IntrospectedView."""
        result = duckdb_engine.introspect("orders")

        assert isinstance(result, IntrospectedView)
        assert result.view_name == "orders"
        assert result.class_name == "Orders"

        by_name = {f.name: f for f in result.fields}
        region = by_name["region"]
        assert isinstance(region, IntrospectedField)
        assert region.field_type == "dimension"
        assert region.data_type == "str"

        revenue = by_name["revenue"]
        assert revenue.field_type == "metric"
        assert revenue.data_type == "float"

    def test_introspect_private_fields_excluded(self, duckdb_engine: Any) -> None:
        """Should exclude PRIVATE metric fields from the output."""
        result = duckdb_engine.introspect("orders")
        field_names = [f.name for f in result.fields]
        assert "internal_cost" not in field_names
        assert "region" in field_names
        assert "revenue" in field_names

    def test_introspect_pascal_case_class_name(self) -> None:
        """Should convert snake_case view name to PascalCase class name."""
        from adbc_poolhouse import DuckDBConfig, close_pool

        from semolina.config import create_engine

        engine = create_engine(DuckDBConfig(database=":memory:", pool_size=1))
        with engine.connect() as conn:
            cur = conn.cursor()
            for stmt in _create_view_sql(view_name="sales_revenue_view"):
                cur.execute(stmt)
            cur.close()
            conn.commit()
        try:
            result = engine.introspect("sales_revenue_view")
            assert result.class_name == "SalesRevenueView"
        finally:
            close_pool(engine._pool)

    def test_introspect_schema_qualified_name_preserved(self, duckdb_engine: Any) -> None:
        """A schema-qualified name is kept whole in ``view_name``; the class takes its tail."""
        result = duckdb_engine.introspect("main.orders")
        assert result.view_name == "main.orders"
        assert result.class_name == "Orders"


@pytest.fixture
def annotated_engine() -> Iterator[Any]:
    """A DuckDB engine with one view carrying view, member and synonym annotations."""
    from adbc_poolhouse import DuckDBConfig, close_pool

    from semolina.config import create_engine

    engine = create_engine(DuckDBConfig(database=":memory:", pool_size=1))
    with engine.connect() as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE sales_data (id INTEGER, country VARCHAR, amount DOUBLE)")
        cur.execute(
            """
            CREATE SEMANTIC VIEW annotated COMMENT = 'Sales by country' AS
            TABLES (s AS sales_data PRIMARY KEY (id))
            DIMENSIONS (
                s.country AS s.country WITH SYNONYMS = ('nation', 'land'),
                s.plain AS s.country
            )
            METRICS (
                s.revenue AS SUM(s.amount)
                    COMMENT = 'Gross revenue'
                    WITH SYNONYMS = ('sales')
            )
            """
        )
        cur.close()
        conn.commit()
    try:
        yield engine
    finally:
        close_pool(engine._pool)


class TestDuckDBIntrospectAnnotations:
    """``DESCRIBE SEMANTIC VIEW``'s COMMENT and SYNONYMS rows, at view and member level."""

    def test_view_comment(self, annotated_engine: Any) -> None:
        """The row with no object kind or name is the view's own."""
        assert annotated_engine.introspect("annotated").description == "Sales by country"

    def test_member_comments_and_synonyms(self, annotated_engine: Any) -> None:
        """Synonyms keep their declared order; a member without any has none."""
        fields = {f.name: f for f in annotated_engine.introspect("annotated").fields}
        assert fields["revenue"].description == "Gross revenue"
        assert fields["revenue"].synonyms == ("sales",)
        assert fields["country"].synonyms == ("nation", "land")
        assert fields["plain"].synonyms == ()

    def test_a_view_without_a_comment(self, duckdb_engine: Any) -> None:
        """No view-level row, an empty description."""
        assert duckdb_engine.introspect("orders").description == ""


class TestDuckDBIntrospectNamesTheViewItWasGiven:
    """
    ``introspect()`` describes the view its name names: schema and quoting included (FILT-05).

    Before phase 54 it stripped the schema, so ``analytics.orders`` described ``main.orders``
    whenever both existed, and it pasted the bare name into ``DESCRIBE SEMANTIC VIEW``
    unquoted, so a name with a space was a syntax error.
    """

    @pytest.fixture
    def two_schemas(self, duckdb_engine: Any) -> Any:
        """Add ``analytics.orders``, with a ``channel`` where ``main.orders`` has ``region``."""
        with duckdb_engine.connect() as conn:
            cur = conn.cursor()
            for stmt in [
                "CREATE SCHEMA analytics",
                "CREATE TABLE analytics.orders_data (id INTEGER, channel VARCHAR, amount DOUBLE)",
                "INSERT INTO analytics.orders_data VALUES (1, 'web', 5.0)",
                """
                CREATE SEMANTIC VIEW analytics.orders AS
                TABLES (o AS analytics.orders_data PRIMARY KEY (id))
                DIMENSIONS (o.channel AS o.channel)
                METRICS (o.revenue AS SUM(o.amount))
                """,
            ]:
                cur.execute(stmt)
            cur.close()
            conn.commit()
        return duckdb_engine

    def test_a_schema_prefix_selects_that_schemas_view(self, two_schemas: Any) -> None:
        """``analytics.orders`` is the analytics view, not ``main.orders``."""
        fields = {f.name for f in two_schemas.introspect("analytics.orders").fields}
        assert fields == {"channel", "revenue"}

    def test_a_bare_name_is_still_the_default_schemas_view(self, two_schemas: Any) -> None:
        """Without a prefix, the search path decides, as it does for a query."""
        fields = {f.name for f in two_schemas.introspect("orders").fields}
        assert fields == {"region", "revenue"}

    @pytest.fixture
    def spaced_view(self, duckdb_engine: Any) -> Any:
        """Add a view whose name has a space, so it needs quoting everywhere it is written."""
        with duckdb_engine.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE SEMANTIC VIEW "Order Book" AS
                TABLES (o AS orders_data PRIMARY KEY (id))
                DIMENSIONS (o.region AS o.region)
                METRICS (o.revenue AS SUM(o.amount))
                """
            )
            cur.close()
            conn.commit()
        return duckdb_engine

    @pytest.mark.parametrize("name", ["Order Book", '"Order Book"'])
    def test_a_name_needing_quotes_is_introspected(self, spaced_view: Any, name: str) -> None:
        """A space in the name is quoted, whether or not the caller quoted it."""
        result = spaced_view.introspect(name)
        assert {f.name for f in result.fields} == {"region", "revenue"}
        assert result.class_name == "OrderBook"

    def test_a_name_needing_quotes_round_trips_through_codegen(self, spaced_view: Any) -> None:
        """
        The generated model is valid Python and queries the view it was generated from.

        ``"Order Book"`` used to become the class name ``Order book``, which is not one.
        """
        import sys
        import types

        import semolina
        from semolina.codegen.python_renderer import render_views

        source = render_views([spaced_view.introspect('"Order Book"')])
        module = types.ModuleType("_generated_model_under_test")
        sys.modules[module.__name__] = module
        try:
            exec(compile(source, module.__name__, "exec"), module.__dict__)  # noqa: S102
        finally:
            sys.modules.pop(module.__name__, None)
        model = module.__dict__["OrderBook"]

        semolina.register("default", spaced_view)
        try:
            cursor = (
                model.query()
                .metrics(model.revenue)
                .dimensions(model.region)
                .order_by(model.region)
                .execute()
            )
            rows = [(row.region, row.revenue) for row in cursor.fetchall_rows()]
            cursor.close()
        finally:
            semolina.unregister("default")
        assert rows == [("CA", 200.0), ("US", 100.0)]


_ODD_NAMES_VIEW_SQL = [
    'CREATE TABLE odd_data (id INTEGER, "Order Date" VARCHAR, amount DOUBLE)',
    "INSERT INTO odd_data VALUES (1, 'a', 1.5), (2, 'b', 2.5)",
    """
    CREATE OR REPLACE SEMANTIC VIEW odd_names AS
    TABLES (o AS odd_data PRIMARY KEY (id))
    DIMENSIONS (
        o."Order Date" AS o."Order Date",
        o."what?" AS o.id,
        o."class" AS o.id + 10,
        o."1st" AS o.id + 20,
        o."a b" AS o.id + 30,
        o.a_b AS o.id + 40,
        o."?" AS o.id + 50
    )
    METRICS (o."Total!" AS SUM(o.amount))
    """,
]


class TestDuckDBIntrospectNonIdentifierNames:
    """
    Field names that are not Python identifiers become valid ones that read the right column.

    The ``semantic_views`` extension reports a quoted name with its quotes
    (``"Order Date"``), so the engine also has to unquote it before naming the field.
    """

    @pytest.fixture
    def odd_view(self, duckdb_engine: Any) -> Any:
        """Add a semantic view whose field names need quoting in SQL."""
        with duckdb_engine.connect() as conn:
            cur = conn.cursor()
            for stmt in _ODD_NAMES_VIEW_SQL:
                cur.execute(stmt)
            cur.close()
            conn.commit()
        return duckdb_engine

    def test_names_are_sanitized_and_keep_their_column(self, odd_view: Any) -> None:
        """Each field gets a valid name and its exact column as ``source_name``."""
        result = odd_view.introspect("odd_names")

        assert [(f.name, f.source_name) for f in result.fields] == [
            ("Order_Date", "Order Date"),
            ("what_", "what?"),
            ("class_", "class"),
            ("_1st", "1st"),
            ("a_b_2", "a b"),
            ("a_b", None),
            ("column", "?"),
            ("Total_", "Total!"),
        ]

    def test_quoted_names_still_resolve_their_types(self, odd_view: Any) -> None:
        """A quoted field's type comes from the DESCRIBE result, not a TODO."""
        result = odd_view.introspect("odd_names")

        types = {f.source_name or f.name: f.data_type for f in result.fields}
        assert types["Order Date"] == "str"
        assert types["what?"] == "int"
        assert types["Total!"] == "float"

    def test_generated_model_queries_every_column(self, odd_view: Any) -> None:
        """The generated module compiles, and each field reads its own column."""
        import sys
        import types

        import semolina
        from semolina.codegen.python_renderer import render_views

        source = render_views([odd_view.introspect("odd_names")])
        module = types.ModuleType("_generated_odd_model_under_test")
        sys.modules[module.__name__] = module
        try:
            exec(compile(source, module.__name__, "exec"), module.__dict__)  # noqa: S102
        finally:
            sys.modules.pop(module.__name__, None)
        model = module.__dict__["OddNames"]

        semolina.register("default", odd_view)
        try:
            cursor = (
                model.query()
                .metrics(model.Total_)
                .dimensions(
                    model.Order_Date,
                    model.what_,
                    model.class_,
                    model._1st,
                    model.a_b_2,
                    model.a_b,
                    model.column,
                )
                .order_by(model.what_)
                .execute()
            )
            rows = [
                (
                    row.Order_Date,
                    row.what_,
                    row.class_,
                    row._1st,
                    row.a_b_2,
                    row.a_b,
                    row.column,
                    row.Total_,
                )
                for row in cursor.fetchall_rows()
            ]
            cursor.close()
        finally:
            semolina.unregister("default")
        assert rows == [
            ("a", 1, 11, 21, 31, 41, 51, 1.5),
            ("b", 2, 12, 22, 32, 42, 52, 2.5),
        ]


_CASE_NAMES_VIEW_SQL = [
    'CREATE TABLE case_data (id INTEGER, "Order Date" VARCHAR, order_date VARCHAR)',
    "INSERT INTO case_data VALUES (1, 'b', 'x'), (2, 'a', 'y')",
    """
    CREATE OR REPLACE SEMANTIC VIEW case_names AS
    TABLES (o AS case_data PRIMARY KEY (id))
    DIMENSIONS (
        o."Order Date" AS o."Order Date",
        o.order_date AS o.order_date
    )
    METRICS (o.total AS COUNT(o.id))
    """,
]


class TestDuckDBIntrospectCaseOnlyCollisions:
    """
    A renamed field that matches another field except in case still gets its own alias.

    ``Order Date`` sanitizes to ``Order_Date``. DuckDB reads that as the same identifier as
    ``order_date``, so two fields spelled that way would share one select alias.
    """

    @pytest.fixture
    def case_view(self, duckdb_engine: Any) -> Any:
        """Add a semantic view whose two dimensions differ only by case once renamed."""
        with duckdb_engine.connect() as conn:
            cur = conn.cursor()
            for stmt in _CASE_NAMES_VIEW_SQL:
                cur.execute(stmt)
            cur.close()
            conn.commit()
        return duckdb_engine

    def test_renamed_field_is_numbered(self, case_view: Any) -> None:
        """The renamed field takes a suffix; the already-valid one keeps its spelling."""
        result = case_view.introspect("case_names")

        assert [(f.name, f.source_name) for f in result.fields] == [
            ("Order_Date_2", "Order Date"),
            ("order_date", None),
            ("total", None),
        ]

    def test_generated_model_queries_both_columns(self, case_view: Any) -> None:
        """The generated model selects and orders by both columns, each reading its own."""
        import sys
        import types

        import semolina
        from semolina.codegen.python_renderer import render_views

        source = render_views([case_view.introspect("case_names")])
        module = types.ModuleType("_generated_case_model_under_test")
        sys.modules[module.__name__] = module
        try:
            exec(compile(source, module.__name__, "exec"), module.__dict__)  # noqa: S102
        finally:
            sys.modules.pop(module.__name__, None)
        model = module.__dict__["CaseNames"]
        # Found by column rather than by name, so the query runs whatever codegen named them.
        by_column = {f.source or name: getattr(model, name) for name, f in model._fields.items()}
        spaced, plain = by_column["Order Date"], by_column["order_date"]

        semolina.register("default", case_view)
        try:
            cursor = model.query().dimensions(plain, spaced).order_by(plain).execute()
            rows = [
                (getattr(row, plain.name), getattr(row, spaced.name))
                for row in cursor.fetchall_rows()
            ]
            cursor.close()
        finally:
            semolina.unregister("default")
        assert rows == [("x", "b"), ("y", "a")]


class TestDuckDBEngineIntrospectErrors:
    """
    Test DuckDBEngine.introspect() error translation over the ADBC pool.

    A DESCRIBE against a non-existent semantic view surfaces an ADBC error that
    introspect() must translate to SemolinaViewNotFoundError.
    """

    @pytest.mark.parametrize("name", ["nosuch.sales_view", "nosuchdb.main.sales_view"])
    def test_a_missing_schema_or_catalog_raises_view_not_found(
        self, duckdb_engine: Any, name: str
    ) -> None:
        """A typo in the schema or catalog part is a missing view, not a connection failure."""
        from semolina.engines.base import SemolinaViewNotFoundError

        with pytest.raises(SemolinaViewNotFoundError):
            duckdb_engine.introspect(name)

    def test_missing_view_raises_view_not_found(self, duckdb_engine: Any) -> None:
        """Introspecting a non-existent view -> SemolinaViewNotFoundError."""
        with pytest.raises(SemolinaViewNotFoundError):
            duckdb_engine.introspect("does_not_exist_view")

    @pytest.mark.parametrize(
        ("error_class", "message", "expected"),
        [
            ("OperationalError", "IO Error: database file not found", "SemolinaConnectionError"),
            (
                "InterfaceError",
                "driver does not exist in the search path",
                "SemolinaConnectionError",
            ),
            ("DataError", "Conversion Error: did you mean INTEGER?", "SemolinaIntrospectionError"),
        ],
        ids=["operational", "interface", "data"],
    )
    def test_the_driver_error_type_decides_before_the_message(
        self, error_class: str, message: str, expected: str
    ) -> None:
        """
        The driver error's type decides, whatever its message says (FILT-08).

        Before phase 54 any message containing "not found", "does not exist" or "did you
        mean" was reported as a missing view, so a missing database file read as a missing
        view. A connection-level error is a connection error; any other is the view failing
        to describe (COR-2).
        """
        from contextlib import contextmanager
        from unittest.mock import MagicMock, patch

        import adbc_driver_manager as adbc
        from adbc_poolhouse import DuckDBConfig, close_pool

        import semolina.exceptions
        from semolina.config import create_engine

        expected_error: type[Exception] = getattr(semolina.exceptions, expected)
        error = getattr(adbc, error_class)(message, status_code=adbc.AdbcStatusCode.IO)

        @contextmanager
        def failing_connect() -> Generator[Any]:
            conn = MagicMock(name="conn")
            conn.cursor.return_value.execute.side_effect = error
            yield conn

        engine = create_engine(DuckDBConfig(database=":memory:", pool_size=1))
        try:
            with (
                patch.object(engine, "connect", side_effect=failing_connect),
                pytest.raises(expected_error) as raised,
            ):
                engine.introspect("orders")
        finally:
            close_pool(engine._pool)
        assert raised.value.__cause__ is error

    def test_a_view_that_does_not_bind_is_not_a_connection_error(self, duckdb_engine: Any) -> None:
        """
        A driver error describing a reachable view is an introspection error (COR-2).

        The connection works; the view's metric reads a column that was dropped. Reporting
        that as a failure to connect sends the user to check their credentials.
        """
        from semolina.exceptions import SemolinaConnectionError, SemolinaError

        with duckdb_engine.connect() as conn:
            cur = conn.cursor()
            cur.execute("ALTER TABLE orders_data DROP COLUMN amount")
            cur.close()
            conn.commit()

        with pytest.raises(SemolinaError) as raised:
            duckdb_engine.introspect("orders")
        assert not isinstance(raised.value, SemolinaConnectionError), raised.value
        assert "amount" in str(raised.value)


class _ScriptedCursor:
    """
    A cursor that answers introspection's queries from a script and records them.

    ``DESCRIBE SEMANTIC VIEW`` returns ``describe_rows``; the extension-version query returns
    ``version``; a ``DESCRIBE SELECT`` probe returns ``probe_types`` for the columns its
    ``semantic_view()`` call names, so a test can tell a probed type from a described one.
    """

    def __init__(
        self,
        describe_rows: list[tuple[str, str, str, str, str]],
        *,
        version: str | None,
        probe_types: dict[str, str],
    ) -> None:
        self.describe_rows = describe_rows
        self.version = version
        self.probe_types = probe_types
        self.executed: list[str] = []
        self._rows: list[tuple[Any, ...]] = []

    @property
    def probes(self) -> list[str]:
        """The ``DESCRIBE SELECT`` statements run, in order."""
        return [sql for sql in self.executed if sql.startswith("DESCRIBE SELECT")]

    def execute(self, sql: str) -> None:
        import re

        self.executed.append(sql)
        if sql.startswith("DESCRIBE SEMANTIC VIEW"):
            self._rows = list(self.describe_rows)
        elif "duckdb_extensions()" in sql:
            self._rows = [] if self.version is None else [(self.version,)]
        elif sql.startswith("DESCRIBE SELECT"):
            names = re.findall(r"'((?:[^']|'')*)'", sql.split(",", 1)[1])
            self._rows = []
            for name in names:
                column = name.replace("''", "'")
                if column.startswith('"') and column.endswith('"'):
                    column = column[1:-1]
                self._rows.append((column, self.probe_types[column]))
        else:  # pragma: no cover - a query introspection does not run
            msg = f"unexpected SQL: {sql}"
            raise AssertionError(msg)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


def _member(
    kind: str, name: str, data_type: str, *, access: str = "PUBLIC"
) -> list[tuple[str, str, str, str, str]]:
    """``DESCRIBE SEMANTIC VIEW`` rows for one member, as the extension reports them."""
    rows = [
        (kind, name, "o", "TABLE", "o"),
        (kind, name, "o", "EXPRESSION", f"o.{name}"),
        (kind, name, "o", "DATA_TYPE", data_type),
    ]
    if kind != "DIMENSION":
        rows.append((kind, name, "o", "ACCESS_MODIFIER", access))
    return rows


def _introspect_scripted(cursor: _ScriptedCursor) -> dict[str, str | None]:
    """Introspect ``orders`` over ``cursor``; map each field's column to its raw SQL type."""
    from semolina.engines.duckdb import _introspect_duckdb

    view = _introspect_duckdb(cursor, "orders", described="orders")
    return {f.source_name or f.name: f.raw_type for f in view.fields}


class TestDescribedMemberTypes:
    """
    Member types come from ``DESCRIBE SEMANTIC VIEW`` when the extension reports them.

    From ``semantic_views`` v0.14.0 the ``DATA_TYPE`` row carries the type the member's column
    has in a ``semantic_view()`` query, so the per-view ``DESCRIBE SELECT`` probes are only
    needed for a member it leaves empty, and for every member on an older extension. The
    probe answers with types the ``DATA_TYPE`` rows never use, so each assertion shows which
    source a type came from.
    """

    _ROWS = (
        _member("DIMENSION", "region", "VARCHAR")
        + _member("DIMENSION", '"Order Date"', "DATE")
        + _member("METRIC", "revenue", "DECIMAL(38,2)")
        + _member("METRIC", "hidden", "BIGINT", access="PRIVATE")
        + _member("FACT", "amount", "DECIMAL(10,2)")
    )
    _PROBED = {
        "region": "PROBED_REGION",
        "Order Date": "PROBED_DATE",
        "revenue": "PROBED_REVENUE",
        "hidden": "PROBED_HIDDEN",
        "amount": "PROBED_AMOUNT",
    }

    @pytest.mark.parametrize("version", ["v0.14.0", "v0.14.2", "v0.16.0", "v1.0.0", "0.15.1"])
    def test_a_reporting_extension_is_not_probed(self, version: str) -> None:
        """Every member is typed by DESCRIBE, quoted names included, with no probe at all."""
        cursor = _ScriptedCursor(list(self._ROWS), version=version, probe_types=self._PROBED)

        types = _introspect_scripted(cursor)

        assert types == {
            "region": "VARCHAR",
            "Order Date": "DATE",
            "revenue": "DECIMAL(38,2)",
            "amount": "DECIMAL(10,2)",
        }
        assert cursor.probes == []

    @pytest.mark.parametrize(
        "version",
        [
            "v0.13.0",
            "v0.9.9",
            "v0.13.99",
            "4f2e1a9",
            "",
            None,
            "v0.14.0-rc1",
            "0.14.0-dev",
            "v0.14.0-3-gdeadbee",
        ],
        ids=repr,
    )
    def test_an_older_or_unknown_extension_is_probed_whatever_describe_says(
        self, version: str | None
    ) -> None:
        """
        Before v0.14.0 a DATA_TYPE row is not trusted, even when it is filled in.

        A view stored by a pre-v0.10.0 release carries the type inferred at ``CREATE``, which
        goes stale once its source column changes; the probe is the only current answer. A
        version that is not a plain release number (a local build's commit hash, or a
        pre-release or ``git describe`` suffix on one) is not trusted either.
        """
        cursor = _ScriptedCursor(list(self._ROWS), version=version, probe_types=self._PROBED)

        types = _introspect_scripted(cursor)

        assert types == {
            "region": "PROBED_REGION",
            "Order Date": "PROBED_DATE",
            "revenue": "PROBED_REVENUE",
            "amount": "PROBED_AMOUNT",
        }
        assert len(cursor.probes) == 2

    def test_only_members_describe_left_untyped_are_probed(self) -> None:
        """
        A member the extension could not type is probed alone, in its own kind's request.

        v0.14.0+ leaves ``DATA_TYPE`` empty for a member whose source no longer binds. The
        probe for it raises the binder error the user needs, as it did before; the members
        DESCRIBE did type keep their described types and are not re-queried.
        """
        rows = (
            _member("DIMENSION", "region", "VARCHAR")
            + _member("METRIC", "revenue", "")
            + _member("FACT", "amount", "DECIMAL(10,2)")
        )
        cursor = _ScriptedCursor(rows, version="v0.14.2", probe_types=self._PROBED)

        types = _introspect_scripted(cursor)

        assert types == {
            "region": "VARCHAR",
            "revenue": "PROBED_REVENUE",
            "amount": "DECIMAL(10,2)",
        }
        assert cursor.probes == [
            "DESCRIBE SELECT * FROM semantic_view('orders', metrics := ['revenue'])"
        ]

    def test_an_untyped_fact_gets_the_facts_request(self) -> None:
        """Facts cannot share a request with metrics, so an untyped fact is probed on its own."""
        rows = _member("DIMENSION", "region", "VARCHAR") + _member("FACT", "amount", "")
        cursor = _ScriptedCursor(rows, version="v0.14.2", probe_types=self._PROBED)

        types = _introspect_scripted(cursor)

        assert types == {"region": "VARCHAR", "amount": "PROBED_AMOUNT"}
        assert cursor.probes == [
            "DESCRIBE SELECT * FROM semantic_view('orders', facts := ['amount'])"
        ]

    def test_an_untyped_quoted_dimension_is_probed_by_its_described_name(self) -> None:
        """The probe names the member as DESCRIBE spelled it and reads back the bare column."""
        rows = _member("DIMENSION", '"Order Date"', "") + _member("METRIC", "revenue", "BIGINT")
        cursor = _ScriptedCursor(rows, version="v0.16.0", probe_types=self._PROBED)

        types = _introspect_scripted(cursor)

        assert types == {"Order Date": "PROBED_DATE", "revenue": "BIGINT"}
        assert cursor.probes == [
            """DESCRIBE SELECT * FROM semantic_view('orders', dimensions := ['"Order Date"'])"""
        ]

    def test_a_described_type_maps_to_its_python_type(self) -> None:
        """A described type goes through the same mapping as a probed one."""
        from semolina.engines.duckdb import _introspect_duckdb

        cursor = _ScriptedCursor(list(self._ROWS), version="v0.14.2", probe_types=self._PROBED)

        view = _introspect_duckdb(cursor, "orders", described="orders")

        data_types = {f.source_name or f.name: f.data_type for f in view.fields}
        assert data_types == {
            "region": "str",
            "Order Date": "datetime.date",
            "revenue": "decimal.Decimal",
            "amount": "decimal.Decimal",
        }

    def test_the_version_query_works_on_the_installed_extension(self, duckdb_engine: Any) -> None:
        """
        The real extension answers the version query with a release number.

        Pinned on the version's shape, not its value, so the test holds as the registry's
        build moves past v0.14.0. A shape the parser cannot read would silently send every
        introspection back to the probes.
        """
        from semolina.engines.duckdb import _EXTENSION_VERSION, _reports_member_types

        with duckdb_engine.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT extension_version FROM duckdb_extensions() "
                "WHERE extension_name = 'semantic_views'"
            )
            (version,) = cursor.fetchone()
            reports = _reports_member_types(cursor)
            cursor.close()

        assert _EXTENSION_VERSION.match(version), version
        release = tuple(int(part) for part in version.removeprefix("v").split(".")[:3])
        assert reports is (release >= (0, 14, 0))


class TestSqlStrLiteral:
    """Test the introspection SQL-string-literal escaping helper."""

    def test_plain_value_is_quoted(self) -> None:
        """A quote-free value is wrapped in single quotes unchanged."""
        from semolina.engines.duckdb import _sql_str_literal

        assert _sql_str_literal("revenue") == "'revenue'"

    def test_embedded_single_quote_is_doubled(self) -> None:
        """An embedded single quote is doubled so it cannot break the literal."""
        from semolina.engines.duckdb import _sql_str_literal

        assert _sql_str_literal("o'brien") == "'o''brien'"

    def test_injection_attempt_stays_inside_literal(self) -> None:
        """A quote-and-paren payload is fully contained within the literal."""
        from semolina.engines.duckdb import _sql_str_literal

        assert _sql_str_literal("x') --") == "'x'') --'"
