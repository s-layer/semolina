.. _changelog:

Changelog
=========

What changed in each Semolina release. The full history of earlier releases is on the
`GitHub releases page <https://github.com/s-layer/semolina/releases>`_.

Unreleased
----------

**Build a query from field names.** :mod:`semolina.spec` builds a query from plain data,
``{"metrics": ["revenue"], "where": {"field": "region", "op": "eq", "value": "West"}}``,
for callers that hold names instead of model attributes, such as an agent's tool call or a
JSON request body. Filters are ``and`` / ``or`` / ``not`` trees over the same operators as
the field methods. Mistakes raise the new ``SemolinaSpecError`` with the path to the bad
value, and ``query_spec_schema()`` gives a JSON Schema listing one model's field names, to
use as a tool's input schema. :ref:`howto-query-by-name` has the details.

0.8.0 (2026-10-19)
------------------

**Databricks Lakehouse//RT.** ``create_engine`` and ``create_async_engine`` accept
adbc-poolhouse's ``DatabricksPythonConfig``, or ``type = "databricks-python"`` in
``.semolina.toml``. It connects through ``databricks-sql-connector`` instead of the ADBC
driver, which is the only way to reach Lakehouse//RT. You get the same ``DatabricksEngine``,
so your models and queries don't change, and ``dispose()`` closes its pool.
:ref:`howto-backends-databricks-rt` has the setup.

**An in-memory DuckDB pool holds more than one connection.** Semolina now requires
adbc-poolhouse 1.7.0, which drops the ``pool_size=1`` limit on ``database = ":memory:"``.
An in-memory pool defaults to 5 connections, and every one of them shares the same
database, so concurrent and async queries no longer need a database file. Create your
tables and views once rather than on each new connection:
:ref:`howto-warehouse-testing` shows how.

The testing tutorial and the warehouse-testing guide seed their in-memory fixtures the
same way.

0.7.0 (2026-10-16)
------------------

0.7.0 adds an async query surface and typed results. It also fixes several 0.6 queries
that ran without complaint and returned the wrong rows. Read the first two sections before
you upgrade from 0.6.

Changes that need action
~~~~~~~~~~~~~~~~~~~~~~~~

**The** ``semolina`` **command needs the** ``[cli]`` **extra.** typer, rich and jinja2 are
no longer base dependencies, so ``pip install semolina`` no longer installs what
``semolina codegen`` and ``semolina codegen-dto`` need. griffelib, which is new in 0.7 and
reads committed files for ``--check``, is in the extra too. Install the command with:

.. code-block:: shell

   pip install "semolina[cli]"

Without the extra, ``semolina`` prints that line and exits with code 2. A service that
only runs queries skips the command's packages, and ``import semolina`` defers
pydantic-settings and SQLAlchemy until you call ``create_engine``.

**The SQL-generation base class is** ``SQLDialect``. Only one thing in Semolina is now
called ``Dialect``: the enum you pass to ``to_sql()``. If you subclassed the base class
through ``semolina.engines.sql.Dialect`` or ``semolina.engines.DialectABC``, rename it.
Both old names still work in 0.7 with a ``DeprecationWarning``.

**Result columns are named after your fields on every warehouse.** Semolina now aliases
each selected column to its Python field name, so ``row.revenue`` works on Snowflake,
Databricks and DuckDB alike. In 0.6 the same query returned ``AGG("REVENUE")`` on
Snowflake and ``measure(revenue)`` on Databricks. If your code read those keys, read
the field names instead.

**A filter given** ``None`` **raises** ``TypeError``. In 0.6 a filter such as
``Sales.revenue >= None`` compiled to a comparison with ``NULL``, which matches no row,
so the query quietly came back empty. Now every comparison and lookup refuses ``None``
when you build it, and so does a ``None`` inside ``in_()``. ``==`` and ``!=`` are the
exception (see below). If your code passes an optional parameter straight into a filter,
leave the filter out when the value is ``None``, or use ``.isnull()`` if you meant to
match missing values.

**A connection failure raises** ``SemolinaConnectionError``. In 0.6 a connection that
could not be opened, in ``create_engine()`` or when ``execute()`` checked one out of the
pool, raised the driver's own error. Now it raises ``SemolinaConnectionError``, with the
driver's error as ``__cause__``, the same as ``introspect()`` already did. If you catch
``adbc_driver_manager.Error`` around those calls to answer a warehouse that is down, catch
``SemolinaConnectionError`` too. A statement the warehouse rejects still raises the
driver's own error. On DuckDB, a connection that cannot install or load the
``semantic_views`` extension raises ``SemolinaExtensionLoadError``, a subclass of
``SemolinaConnectionError`` whose message names the extension.

Fixed
~~~~~

Each of these ran without an error in 0.6 and returned the wrong result. 0.7 either
returns the right rows or refuses the query with an exception.

- ``in_()`` given a string, such as ``Sales.country.in_("US")``, treated each character
  as a separate value. It now raises ``TypeError``. It also accepts a generator, and the SQL always
  has as many placeholders as there are values.
- ``field == None`` and ``field != None`` compiled to ``= NULL`` and ``<> NULL``, which
  match nothing. They now compile to ``IS NULL`` and ``IS NOT NULL``. Every other
  comparison or lookup given ``None``, ``between()`` and a ``None`` inside ``in_()``
  among them, raises ``TypeError`` and points you at ``.isnull()``.
- ``startswith``, ``istartswith``, ``endswith``, ``iendswith`` and ``iexact`` treated
  ``%`` and ``_`` in your value as wildcards. They now match them literally on every
  dialect. ``like`` and ``ilike`` still pass your pattern through as written.
- A result with two columns of the same name, which selecting one field twice produces,
  silently kept the last value. Selecting a field twice now raises ``ValueError`` in the
  builder, and a ``Row`` refuses duplicate column names.
- On DuckDB, a query that selects facts and no metric returned one row per source row,
  where Snowflake and Databricks group the same query. DuckDB now returns each distinct
  row once too.

Two more raised an error where they should have worked:

- On DuckDB, a DBAPI fetch after the result was drained, such as the last call of a
  ``while batch := cursor.fetchmany_rows(100)`` loop, raised ``InternalError``. Every
  DBAPI fetch now returns ``[]`` or ``None`` once the result is drained.
- ``semolina codegen --backend my_module.MyEngine`` could not import a module in the
  working directory unless it was installed. Both codegen commands now add the working
  directory to the end of ``sys.path`` first.

Changed
~~~~~~~

- ``to_sql()`` with no argument previews the dialect of the engine ``.using()`` names,
  when that engine is registered. Without ``.using()``, it still previews Snowflake.
- Every exception class Semolina defines derives from ``SemolinaError``, so one
  ``except`` clause can catch them all. Each keeps its 0.6 base class, so existing
  ``except RuntimeError`` and ``except ValueError`` clauses still match. Looking up an
  engine that was never registered raises ``SemolinaEngineNotRegisteredError``, which is
  still a ``ValueError``. A result with two columns of one name, which only a custom
  engine can produce, raises ``SemolinaDuplicateColumnsError``, still a ``ValueError``.
  Both new errors are importable from ``semolina``.
- ``introspect()``, and so ``semolina codegen``, reports a view the warehouse reached but
  could not describe, such as one whose definition no longer binds, as the new
  ``SemolinaIntrospectionError`` rather than ``SemolinaConnectionError``, and
  ``semolina codegen`` exits 6 for it rather than 4. Connection and authentication
  failures still raise ``SemolinaConnectionError``.
- ``Query`` is public, and ``Field``, ``Engine``, ``AsyncEngine``, ``And``, ``Or``,
  ``Not`` and ``Lookup`` are importable from ``semolina``. The query builder's methods
  are typed, so a type checker flags ``.metrics(Sales.country)`` or ``.limit("10")``.
  A query reports its state through read-only properties: ``selected_metrics``,
  ``selected_dimensions``, ``filters``, ``ordering``, ``limit_value``, ``engine_name``
  and ``model``.
- ``IntrospectedView`` and ``IntrospectedField``, the types ``introspect()`` returns, live
  in ``semolina.introspection``. Importing them from ``semolina.codegen.introspector``
  still works.

Added
~~~~~

- ``aexecute()`` and async row streaming, under asyncio or Trio, with the ``[async]``
  extra. Cancelling a query stops it inside the driver.
- ``.into(Model)`` turns a result into Pydantic models, and ``fetch_df()`` and
  ``fetch_polars()`` return a pandas or polars DataFrame.
- ``semolina codegen-dto`` writes the Pydantic model for a query's result, typed from
  the warehouse's own result schema.
- ``semolina codegen --check`` reports where your models no longer match the warehouse,
  and ``--output`` writes the generated module to a file.
- ``to_sql()`` takes a dialect object as well as a name, so you can preview a custom
  dialect's SQL without registering an engine. Each built-in dialect names its backend in
  ``kind``.

See also
--------

- :ref:`tutorial-installation` covers the extras, ``[cli]`` among them.
- :ref:`howto-web-api` shows how to catch ``SemolinaError`` in a request handler.
- :ref:`howto-filtering` covers filters, including ``None`` and the string lookups.
