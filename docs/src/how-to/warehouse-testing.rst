.. _howto-warehouse-testing:

How to test query code without a warehouse
==========================================

Three tests pass against an in-memory fixture at the end of
:ref:`tutorial-testing-queries`. What that lesson left out is how to assert on the SQL a
query generates, how to replay traffic recorded from your real warehouse, and which
cleanup rules keep one test out of the next one's results.

The fixture runs your queries against an in-memory DuckDB semantic view instead of a live
warehouse. DuckDB executes the SQL your query builder generates, so tests see real
aggregation and filtering, and your application code calls ``Model.query().execute()``
exactly as it does in production.

This page covers the *testing* fixture. To connect an application to a DuckDB
database as a backend, see :ref:`howto-backends-duckdb`. For engine lifecycle and
pooling, see :ref:`howto-connection-pools`.

Install the DuckDB extra
------------------------

.. code-block:: bash

   uv add "semolina[duckdb]"
   # or
   pip install "semolina[duckdb]"

Set up an in-memory engine fixture
----------------------------------

Build a DuckDB engine backed by ``":memory:"``, then create the table, semantic
view, and seed rows once on a connection checked out with
:py:meth:`engine.connect() <semolina.engines.base.Engine.connect>`. Every
connection in the engine's pool opens the same in-memory database, so the rows are
there for whichever connection runs the query. Wrapping the ``yield`` in the
engine's own ``with`` block is the teardown: it drops the registration and then
closes the pool, which discards the database.

.. code-block:: python

   import pytest
   from adbc_poolhouse import DuckDBConfig

   from semolina import (
       Dimension,
       Metric,
       SemanticView,
       create_engine,
   )


   class Sales(SemanticView, view="sales_view"):
       revenue = Metric()
       country = Dimension()


   def _seed(conn):
       cur = conn.cursor()
       cur.execute(
           "CREATE TABLE sales_data (id INTEGER, revenue INTEGER, country VARCHAR)"
       )
       cur.execute(
           "INSERT INTO sales_data VALUES (1, 1000, 'US'), (2, 2000, 'CA'), (3, 500, 'US')"
       )
       cur.execute("""
           CREATE OR REPLACE SEMANTIC VIEW sales_view AS
           TABLES (s AS sales_data PRIMARY KEY (id))
           DIMENSIONS (s.country AS country)
           METRICS (s.revenue AS SUM(s.revenue))
           """)
       cur.close()
       conn.commit()


   @pytest.fixture
   def sales_engine():
       config = DuckDBConfig(database=":memory:")
       with create_engine(config, register=True) as engine:
           with engine.connect() as conn:
               _seed(conn)
           yield

The ``commit()`` after ``CREATE SEMANTIC VIEW`` matters: ADBC connections open
with ``autocommit=False``, and the ``semantic_views`` extension resolves the
view on a separate read connection that only sees committed state. You do not
load the extension yourself: :py:func:`~semolina.config.create_engine` loads it
on every connection. See :ref:`howto-backends-duckdb` for more on the extension.

Write a test
------------

Query your model the same way your application does. DuckDB aggregates the
metric, so ``US`` returns ``1500`` (``1000 + 500``):

.. code-block:: python

   def test_revenue_by_country(sales_engine):
       cursor = (
           Sales.query()
           .metrics(Sales.revenue)
           .dimensions(Sales.country)
           .execute()
       )
       rows = {
           row.country: row.revenue
           for row in cursor.fetchall_rows()
       }
       cursor.close()

       assert rows == {"US": 1500, "CA": 2000}

The values are ``decimal.Decimal``, not ``int``: DuckDB types a ``SUM`` over an
``INTEGER`` column as a 128-bit ``HUGEINT``. ``Decimal(1500) == 1500`` holds, so the
assertion reads naturally. Cast the sum to ``BIGINT`` in the view if the code under test
expects an ``int``, as :ref:`tutorial-testing-queries` does.

Because the SQL runs, ``.where()`` filters return only matching rows:

.. code-block:: python

   def test_filtered_query(sales_engine):
       cursor = (
           Sales.query()
           .metrics(Sales.revenue)
           .dimensions(Sales.country)
           .where(Sales.country == "US")
           .execute()
       )
       rows = cursor.fetchall_rows()
       cursor.close()

       assert len(rows) == 1
       assert rows[0].country == "US"
       assert rows[0].revenue == 1500

Assert on generated SQL
-----------------------

``.to_sql()`` renders a query without executing it, which makes it the tool for
structural assertions. :ref:`howto-inspect-sql` covers what it emits for each dialect;
in a test it looks like this:

.. code-block:: python

   def test_sql_generation():
       sql = (
           Sales.query()
           .metrics(Sales.revenue)
           .dimensions(Sales.country)
           .where(Sales.country == "US")
           .to_sql()
       )
       assert 'AGG("REVENUE")' in sql
       assert '"COUNTRY"' in sql

Use ``.to_sql()`` for assertions about the SQL, and the DuckDB fixture above for
assertions about behaviour.

Record your warehouse with pytest-adbc-replay
---------------------------------------------

DuckDB runs the SQL, but it is not your warehouse: its results can differ from
Snowflake or Databricks in numeric type and precision, and in how your semantic
view resolves. When you want tests to match what your warehouse
returns, record the real responses once with
`pytest-adbc-replay <https://s-layer.github.io/pytest-adbc-replay/>`_ and
replay them from disk on every later run.

The plugin wraps the ADBC connection your engine's pool hands out. A credentialed
run captures each query and its Arrow result into a *cassette*; after that, tests
read the cassette and reach no warehouse.

Install it as a dev dependency:

.. code-block:: bash

   uv add --dev pytest-adbc-replay

Point ``adbc_auto_patch`` at the driver module your engine connects through, and
set ``adbc_dialect`` so recorded SQL is matched correctly on replay:

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: toml

         [tool.pytest.ini_options]
         adbc_auto_patch = ["adbc_driver_snowflake.dbapi"]
         adbc_dialect = ["adbc_driver_snowflake.dbapi: snowflake"]

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: toml

         [tool.pytest.ini_options]
         adbc_auto_patch = ["adbc_driver_manager.dbapi"]
         adbc_dialect = ["adbc_driver_manager.dbapi: databricks"]

      adbc-poolhouse connects to Databricks through the ADBC driver manager, so
      the module to patch is ``adbc_driver_manager.dbapi`` rather than a
      Databricks-specific one.

   .. tab-item:: DuckDB
      :sync: duckdb

      Nothing to configure. There is no credentialed run to capture and no
      network call to avoid, so a local DuckDB is simply run rather than
      recorded -- use the in-memory engine fixture from the top of this page.

      Keep it that way: do not mark a DuckDB test with ``adbc_cassette``. The
      plugin serves a recorded result whatever the driver would really have
      done, so a cassette-backed DuckDB test looks like evidence and is none.

Register your warehouse engine, not the in-memory DuckDB fixture from the top
of this page, then mark the test with ``adbc_cassette`` so the plugin records
or replays its connections:

.. code-block:: python

   import pytest


   @pytest.mark.adbc_cassette
   def test_revenue_by_country(snowflake_engine):
       cursor = (
           Sales.query()
           .metrics(Sales.revenue)
           .dimensions(Sales.country)
           .execute()
       )
       rows = {
           row.country: row.revenue
           for row in cursor.fetchall_rows()
       }
       cursor.close()

       assert rows == {"US": 1500, "CA": 2000}

A cassette replays the result your warehouse produced, with the column types
its driver reported, which is what the DuckDB fixture cannot show you. The
column names match DuckDB's, because Semolina aliases each column to its field
name on every backend. See :ref:`howto-result-column-names`.

Record once against the real warehouse, then replay with no credentials:

.. code-block:: bash

   # Record: reads warehouse credentials from your environment
   pytest --adbc-record=once

   # Replay (the default): reads cassettes, reaches no warehouse
   pytest

Commit the cassette files next to your tests. They are matched by normalized
SQL, so they only need re-recording when the query your code generates changes.

Clean up between tests
----------------------

A registration that outlives its test resolves for the next one, which then queries a
pool that is closing or gone. ``register=True`` inside a ``with`` block closes that
window for you: the fixtures above yield from inside the block, so the name is dropped
and the pool disposed as the test ends, in that order.

Names you register yourself are yours to clean up. The engine drops only the name
:py:func:`~semolina.config.create_engine` gave it, so a second registration made with
:py:func:`~semolina.registry.register` needs its own
:py:func:`~semolina.registry.unregister` call in teardown.

See also
--------

- :ref:`tutorial-testing-queries` -- building the fixture and the first test, step by step
- :ref:`howto-backends-duckdb` -- ship on a DuckDB database, and the
  ``semantic_views`` extension in more detail
- :ref:`howto-backends` -- configure a real Snowflake or Databricks connection
- :ref:`howto-queries` -- the full query API
- :ref:`explanation-duckdb-vs-warehouse` -- what a green DuckDB suite does and does not
  prove about code that will run against Snowflake or Databricks
- `pytest-adbc-replay <https://s-layer.github.io/pytest-adbc-replay/>`_ --
  record and replay ADBC responses
