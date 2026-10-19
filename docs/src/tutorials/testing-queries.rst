.. _tutorial-testing-queries:

Test queries without a warehouse
================================

The endpoint from the last tutorial works, and nothing checks that it keeps
working. In this tutorial you will build a pytest fixture that creates a
semantic view inside the test process, point your application's own query code
at it, and assert on the rows that come back.

DuckDB runs the SQL the query builder generates, so the aggregation and the
filtering are real. There is no mock and no fake result. Tests need no
credentials and reach no network.

**Prerequisites:** :ref:`tutorial-dashboard-api` and its ``app.py``, plus:

.. code-block:: bash

   pip install "semolina[duckdb]" pytest

1. Move the query out of the handler
------------------------------------

``app.py`` currently builds its query inside the request handler, where a test
can only reach it by starting a web server. Move that code into its own module
so a test can call it as a function.

Create ``reports.py`` next to ``app.py``:

.. code-block:: python
   :caption: reports.py

   """Query code the dashboard endpoint calls."""

   import pydantic

   from semolina import Dimension, Metric, SemanticView


   class Sales(SemanticView, view="sales"):
       revenue = Metric()
       cost = Metric()
       country = Dimension()
       region = Dimension()


   class RevenueByCountry(pydantic.BaseModel):
       country: str
       revenue: int


   def revenue_by_country(
       country: str | None = None,
   ) -> list[RevenueByCountry]:
       query = (
           Sales.query()
           .metrics(Sales.revenue)
           .dimensions(Sales.country)
           .where(
               Sales.country == country if country else None
           )
           .order_by(Sales.revenue.desc())
       )
       with query.execute() as cursor:
           return cursor.into(RevenueByCountry)

Nothing in that module knows which engine it will run against. The query does
not name one, so it resolves whichever engine is registered as ``"default"``
when it executes. That is what lets the same function serve a request against
``tutorial.db`` and a test against a database that exists only in memory.

``app.py`` becomes the HTTP layer and nothing else:

.. code-block:: python
   :caption: app.py

   from contextlib import asynccontextmanager

   from adbc_driver_manager import Error
   from adbc_poolhouse import DuckDBConfig
   from fastapi import FastAPI, HTTPException, Query

   from reports import RevenueByCountry, revenue_by_country
   from semolina import SemolinaConnectionError, create_engine


   @asynccontextmanager
   async def lifespan(app: FastAPI):
       with create_engine(
           DuckDBConfig(database="tutorial.db"), register=True
       ):
           yield


   app = FastAPI(lifespan=lifespan)


   @app.get("/revenue")
   def revenue(
       country: str | None = Query(default=None),
   ) -> list[RevenueByCountry]:
       try:
           return revenue_by_country(country)
       except (Error, SemolinaConnectionError):
           raise HTTPException(
               status_code=503,
               detail="Data warehouse is unavailable",
           )

Start it with ``uvicorn app:app`` and the two requests from the last tutorial
answer exactly as before:

.. code-block:: console

   $ curl -s http://127.0.0.1:8000/revenue
   [{"country":"CA","revenue":2000},{"country":"US","revenue":1500}]

   $ curl -s "http://127.0.0.1:8000/revenue?country=US"
   [{"country":"US","revenue":1500}]

2. Build the semantic view in a fixture
---------------------------------------

The fixture has three jobs: create an in-memory DuckDB engine, build the table
and the semantic view inside it, and register the engine as ``"default"`` for
the duration of one test.

The middle job runs once per test, on a connection checked out from the
engine. Every connection in the engine's pool opens the same ``":memory:"``
database, so a table one connection creates is there for the connection that
runs your query.

Create ``conftest.py``:

.. code-block:: python
   :caption: conftest.py

   import pytest
   from adbc_poolhouse import DuckDBConfig

   from semolina import create_engine


   def seed(conn):
       cur = conn.cursor()
       cur.execute("""
           CREATE TABLE sales_data (
               revenue INTEGER, cost INTEGER,
               country VARCHAR, region VARCHAR
           )
       """)
       cur.execute("""
           INSERT INTO sales_data VALUES
           (1000, 100, 'US', 'West'),
           (2000, 200, 'CA', 'West'),
           (500, 50, 'US', 'East')
       """)
       cur.execute("""
           CREATE OR REPLACE SEMANTIC VIEW sales AS
           TABLES (s AS sales_data)
           DIMENSIONS (
               s.country AS country,
               s.region AS region
           )
           METRICS (
               s.revenue AS SUM(s.revenue),
               s.cost AS SUM(s.cost)
           )
       """)
       cur.close()
       conn.commit()


   @pytest.fixture
   def sales_engine():
       config = DuckDBConfig(database=":memory:")
       with create_engine(config, register=True) as engine:
           with engine.connect() as conn:
               seed(conn)
           yield engine

Three details in there are load-bearing.

``seed`` runs once, inside the ``with`` block. ``create_engine`` has already
loaded the ``semantic_views`` extension on that connection, and the database
lives as long as the engine's pool, so each test gets a fresh database with the
rows in it.

The ``conn.commit()`` at the end of ``seed``. ADBC connections open with
``autocommit=False``, and the ``semantic_views`` extension resolves the view on
a separate read connection that only sees committed state. Without the commit,
the view is invisible to the query that needs it.

The ``with`` block around the ``yield``. ``register=True`` registers the engine
as ``"default"`` while the test runs, and leaving the block on teardown removes
that name and disposes the pool, so one test's engine cannot leak into the next
test.

3. Write the tests
------------------

Now call ``revenue_by_country()`` the way ``app.py`` calls it, and assert on
what it returns. Because it returns Pydantic instances, comparing the whole
list against expected instances works and gives a readable failure.

Create ``test_reports.py``:

.. code-block:: python
   :caption: test_reports.py

   from reports import RevenueByCountry, revenue_by_country


   def test_revenue_is_summed_per_country(sales_engine):
       rows = revenue_by_country()

       assert rows == [
           RevenueByCountry(country="CA", revenue=2000),
           RevenueByCountry(country="US", revenue=1500),
       ]


   def test_country_filter_narrows_the_result(sales_engine):
       rows = revenue_by_country(country="US")

       assert rows == [
           RevenueByCountry(country="US", revenue=1500)
       ]


   def test_unknown_country_returns_nothing(sales_engine):
       assert revenue_by_country(country="ZZ") == []

Each test asks for the ``sales_engine`` fixture by name, which is all it takes
to get a registered engine.

The first assertion is the one worth reading twice. ``US`` appears in two rows
of ``sales_data``, at ``1000`` and ``500``, and the test expects a single row
of ``1500``. Nothing in the test computes that. DuckDB ran the ``SUM`` the
semantic view declares, over the SQL the query builder generated, which is what
makes this a test of your query rather than a test of your expectations. The
row order is asserted too, because ``revenue_by_country`` sorts descending.

4. Run them
-----------

.. code-block:: bash

   pytest

.. code-block:: text

   ============================= test session starts ==============================
   platform darwin -- Python 3.12.3, pytest-9.1.1, pluggy-1.6.0
   rootdir: /home/you/semolina-tutorial
   plugins: anyio-4.14.2
   collected 3 items

   test_reports.py ...                                                      [100%]

   ============================== 3 passed in 0.28s ===============================

Three real queries against a real semantic view, in under a third of a second,
with no warehouse and no credentials.

Try breaking one to see what a failure tells you. Change the first test's
``2000`` to ``2001`` and pytest prints the two lists side by side, naming the
instance that differs.

What this suite does not prove
------------------------------

A green run here says your query builds correctly and your aggregation is the
one you meant. It does not say your code works against Snowflake or Databricks,
and the gap is not small.

The clearest example is sitting in ``reports.py``. ``RevenueByCountry.revenue`` is annotated
``int``, which is right for this DuckDB database and wrong for Snowflake, where the same
metric arrives as a :py:class:`decimal.Decimal`, and for any warehouse that stores revenue
as a ``DECIMAL``. :py:meth:`~semolina.cursor.SemolinaCursor.into` would refuse the call
there. The column names are not the problem, since every backend names a result column after
its field. The type is, and no amount of DuckDB testing surfaces it.

The fix is to record your real warehouse once and replay the recording
afterwards, so the tests assert against results your warehouse
produced. :ref:`howto-warehouse-testing` covers that, along with asserting on
generated SQL with ``.to_sql()``.
:ref:`explanation-duckdb-vs-warehouse` is the full list of what a DuckDB-only
suite can and cannot catch.

Next steps
----------

Both the model and the DTO in ``reports.py`` were written by hand against a
warehouse you happened to know the shape of. Next, have Semolina write them for
you:

:ref:`Generate models from your warehouse <tutorial-warehouse-models>`

See also
--------

.. grid:: 1 1 2 2
   :class-row: surface
   :gutter: 2

   .. grid-item-card:: Test query code without a warehouse
      :link: howto-warehouse-testing
      :link-type: ref

      Recording and replaying your real warehouse with pytest-adbc-replay, and
      asserting on generated SQL.

   .. grid-item-card:: DuckDB vs your warehouse
      :link: explanation-duckdb-vs-warehouse
      :link-type: ref

      What a green DuckDB suite proves about code that will run on Snowflake or
      Databricks, and what it does not.

   .. grid-item-card:: Connect to DuckDB
      :link: howto-backends-duckdb
      :link-type: ref

      The ``semantic_views`` extension, and using DuckDB as an application
      backend rather than a test double.

   .. grid-item-card:: Connection pools
      :link: howto-connection-pools
      :link-type: ref

      What ``create_engine`` builds, and how big its pool is.
