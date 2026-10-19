.. _howto-backends-overview:
.. _howto-backends:

How to configure your warehouse backend
=======================================

Your semantic view lives in Snowflake, Databricks, or DuckDB.
:ref:`tutorial-first-query` pointed at none of them, building a throwaway local DuckDB
file instead. This page swaps that for the real warehouse and leaves the query code
alone.

The steps are the same for every backend: install an extra, describe the connection,
build and register an engine. Only the connection details and the generated SQL differ,
so each step below is written once with a tab per backend. The per-backend sections at
the end cover the settings and quirks that belong to one warehouse only.

.. tip::

   New to semantic views? :ref:`explanation-semantic-views` explains what one is in each
   warehouse and how Semolina's metrics and dimensions map onto it.

Pick your backend
-----------------

.. list-table::
   :header-rows: 1
   :widths: 20 28 30 22

   * - Backend
     - Extra
     - ADBC driver
     - Metric syntax
   * - Snowflake semantic views
     - ``semolina[snowflake]``
     - Installed by the extra
     - ``AGG()``
   * - Databricks metric views
     - ``semolina[databricks]``
     - Separate install from the ADBC Driver Foundry
     - ``MEASURE()``
   * - DuckDB semantic views
     - ``semolina[duckdb]``
     - Installed by the extra
     - ``semantic_view()`` table function

Install the extra
-----------------

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: bash

         pip install "semolina[snowflake]"
         # or
         uv add "semolina[snowflake]"

      The extra installs ``adbc-poolhouse[snowflake]``, which brings the ADBC Snowflake
      driver, and ``pyarrow``, which ADBC reads every row through. Nothing else to fetch.

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: bash

         pip install "semolina[databricks]"
         # or
         uv add "semolina[databricks]"

      The extra installs ``databricks-sql-connector[pyarrow]`` and ``pyarrow``. It does
      **not** install the ADBC driver, which Databricks distributes through the ADBC
      Driver Foundry rather than PyPI. See :ref:`howto-backends-databricks` below.

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: bash

         pip install "semolina[duckdb]"
         # or
         uv add "semolina[duckdb]"

      The extra installs ``duckdb`` and ``pyarrow``. The ``duckdb`` version is pinned,
      because the ``semantic_views`` community extension publishes binaries only for
      specific DuckDB core releases.

Configure with .semolina.toml
-----------------------------

:py:func:`~semolina.config.create_engine` reads its settings from a
``[connections.<name>]`` section of ``.semolina.toml`` in your working directory. The
section's ``type`` selects the backend, and every other key is passed to the matching
``adbc-poolhouse`` config class. That ``type`` value is a member of the
:py:class:`~semolina.dialect.Dialect` enum, so ``"snowflake"``, ``"databricks"`` and
``"duckdb"`` are the accepted values.

Create the file in your project root:

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: toml
         :caption: .semolina.toml

         [connections.default]
         type = "snowflake"
         account = "xy12345.us-east-1"
         user = "svc_analytics"
         password = "..."
         database = "analytics"
         warehouse = "compute_wh"
         # role = "analyst"
         # schema = "public"

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: toml
         :caption: .semolina.toml

         [connections.default]
         type = "databricks"
         host = "workspace.cloud.databricks.com"
         http_path = "/sql/1.0/warehouses/abc123"
         token = "dapi..."
         # catalog = "main"
         # schema = "analytics"

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: toml
         :caption: .semolina.toml

         [connections.default]
         type = "duckdb"
         database = "/path/to/warehouse.db"
         # read_only = true

A field the section leaves out is read from the backend's prefixed environment variable,
such as ``SNOWFLAKE_PASSWORD``, ``DATABRICKS_TOKEN`` or ``DUCKDB_DATABASE``. That lets you
commit ``.semolina.toml`` without its secret: drop the ``password`` or ``token`` line and
set the variable in the environment that runs your code. The same variables feed
``semolina codegen``, which also reads a ``.env`` file; see
:ref:`howto-codegen-credentials`.

Every section also accepts the shared pool-tuning keys ``pool_size``, ``max_overflow``,
``timeout``, and ``recycle``, listed under :ref:`reference-config-common-fields` and
explained in :ref:`howto-connection-pools`.

Build an engine from that section. ``register=True`` registers it under the connection
name, so the registry key and the TOML section stay the same word:

.. code-block:: python

   from semolina import create_engine

   # reads [connections.default], registers as "default"
   create_engine("default", register=True)

A query with no ``.using()`` clause resolves the engine registered as ``"default"``.
Pass ``register="reports"`` to pick a different registry name, and see
:ref:`howto-connection-pools` for the ``with`` block that unregisters the engine and
disposes its pool at the end of a scope.

``create_engine()`` with no argument is the same as ``create_engine("default")``. Pass
another section name to load it instead, and ``config_path=`` to read a file somewhere
other than ``./.semolina.toml``:

.. code-block:: python

   engine = create_engine(
       "reports", config_path="config/warehouse.toml"
   )

.. note:: ``semolina codegen`` picks its section differently

   ``semolina codegen`` does not read ``[connections.default]``: it reads the section
   named after ``--backend``, and ``--backend duckdb`` reads none. See
   :ref:`howto-codegen-credentials`.

Configure from a config object
------------------------------

Pass an ``adbc-poolhouse`` config object instead of a name when credentials come from a
vault, a secrets manager, or your own code. The dialect is derived from the config
class, so there is no backend to select by hand:

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: python

         from adbc_poolhouse import SnowflakeConfig

         from semolina import create_engine

         create_engine(
             SnowflakeConfig(
                 account="xy12345.us-east-1",
                 user="svc_analytics",
                 password="...",
                 database="analytics",
                 warehouse="compute_wh",
             ),
             register=True,
         )

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: python

         from adbc_poolhouse import DatabricksConfig

         from semolina import create_engine

         create_engine(
             DatabricksConfig(
                 host="workspace.cloud.databricks.com",
                 http_path="/sql/1.0/warehouses/abc123",
                 token="dapi...",
             ),
             register=True,
         )

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: python

         from adbc_poolhouse import DuckDBConfig

         from semolina import create_engine

         create_engine(
             DuckDBConfig(database="/path/to/warehouse.db"),
             register=True,
         )

A config object carries no section name, so ``register=True`` registers these engines as
``"default"``. Pass a string such as ``register="reports"`` to name one yourself.

:py:func:`~semolina.config.create_async_engine` takes the same two argument forms and
returns an :py:class:`AsyncEngine <semolina.engines.abase.AsyncEngine>`. It needs the
``semolina[async]`` extra; see :ref:`howto-connection-pools`.

Run a query
-----------

Once an engine is registered, the query API is identical across backends:

.. code-block:: python

   from semolina import Dimension, Metric, SemanticView


   class Sales(SemanticView, view="sales"):
       revenue = Metric()
       country = Dimension()


   cursor = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
       .execute()
   )

So is reading the result. Semolina aliases every selected column to its field name, so a
row's keys are the model's field names on every backend:

.. code-block:: python

   for row in cursor.fetchall_rows():
       print(row.country, row.revenue)

Inspect the generated SQL
-------------------------

``.to_sql()`` renders SQL for any dialect without connecting to a warehouse, which is
the quickest way to see what a backend will receive. With no argument it renders for the
engine named in ``.using()``, if one is registered under that name, and for Snowflake
otherwise. Pass ``dialect=`` to preview a particular backend:

.. code-block:: python

   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
   )
   print(query.to_sql(dialect="databricks"))

The three dialects render as:

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         SELECT AGG("REVENUE") AS "revenue", "COUNTRY" AS "country"
         FROM "SALES"
         GROUP BY ALL

      Metrics are wrapped in ``AGG()``, identifiers are double-quoted, and unquoted
      names are folded to upper case before quoting. Each column is aliased to its field
      name, quoted so the alias keeps its case.

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         SELECT MEASURE(`revenue`) AS `revenue`, `country` AS `country`
         FROM `sales`
         GROUP BY ALL

      Metrics are wrapped in ``MEASURE()``, identifiers are backtick-quoted, and names are
      folded to lower case. Each column is aliased to its field name.

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         SELECT "revenue" AS "revenue", "country" AS "country"
         FROM semantic_view('sales', dimensions := ['country'], metrics := ['revenue'])

      There is no ``AGG()`` or ``MEASURE()`` wrapper. The ``semantic_view()`` table
      function takes the field names as string literals and aggregates internally. The
      outer ``SELECT`` names each field's column after the field.

.. _howto-backends-snowflake:

Snowflake
---------

Connection fields
~~~~~~~~~~~~~~~~~

``account`` is the only field ``SnowflakeConfig`` requires. Everything else depends on
which authentication method and session scope you want:

.. list-table::
   :header-rows: 1
   :widths: 22 14 14 50

   * - Field
     - Type
     - Required
     - Description
   * - ``type``
     - ``str``
     - Yes
     - Must be ``"snowflake"`` in a TOML section
   * - ``account``
     - ``str``
     - Yes
     - Account identifier, region included (e.g. ``xy12345.us-east-1``)
   * - ``user``
     - ``str``
     - No
     - Username. Needed by every auth method except workload identity
   * - ``password``
     - ``str``
     - No
     - Password, for basic auth
   * - ``private_key_path``
     - ``str``
     - No
     - Path to a PKCS1/PKCS8 key file, for key-pair auth. ``~`` is expanded
   * - ``auth_type``
     - ``str``
     - No
     - ``auth_jwt``, ``auth_oauth``, ``auth_okta``, ``auth_pat``, and others
   * - ``database``
     - ``str``
     - No
     - Default database
   * - ``schema``
     - ``str``
     - No
     - Default schema
   * - ``warehouse``
     - ``str``
     - No
     - Compute warehouse that runs the query
   * - ``role``
     - ``str``
     - No
     - Role activated for the session

Semolina requires neither ``database`` nor ``warehouse``: ``account`` is the only field
``SnowflakeConfig`` insists on, and ``semolina codegen`` builds the same config querying
does. What they change is what the connection can resolve. A fully-qualified view name
supplies the database, and the warehouse falls back to your Snowflake user's default, so
a session with neither a ``warehouse`` setting nor a user default cannot run a query.
Introspection issues ``SHOW COLUMNS IN VIEW``, which needs a three-part
``database.schema.view`` name. Semolina fills the parts you leave out from the session's
current database and schema, so without them you have to qualify the view yourself. Set
``database`` whenever you set ``schema``: the Snowflake driver ignores a schema given
without a database and leaves the session in your user's default. See
:ref:`howto-codegen-credentials`.

Set ``role`` explicitly in a service. Semantic views are privilege-scoped, so an engine
that connects under the user's default role can see a different set of views from the
one you tested against.

.. warning:: Leave ``QUOTED_IDENTIFIERS_IGNORE_CASE`` off

   Semolina names each result column after its field with a quoted alias, such as
   ``AS "revenue"``, and relies on Snowflake keeping that quoted name's case. An account or
   session that sets ``QUOTED_IDENTIFIERS_IGNORE_CASE = TRUE`` returns the column as
   ``REVENUE`` instead, so ``row.revenue`` and a plain DTO's ``.into()`` no longer find it,
   and an alias can shadow a measure in ``ORDER BY``. If your account sets it, turn it off
   for Semolina's sessions with ``ALTER USER ... SET QUOTED_IDENTIFIERS_IGNORE_CASE =
   FALSE`` on the user the engine connects as.

.. _howto-backends-databricks:

Databricks
----------

.. important:: The ADBC driver is a separate install

   Databricks distributes its ADBC driver through the `ADBC Driver Foundry
   <https://adbc-drivers.org/drivers/databricks/>`_ rather than PyPI, so
   ``pip install "semolina[databricks]"`` cannot fetch it and neither can any other extra.
   You install it with ``dbc``, the Foundry's own CLI:

   .. code-block:: bash

      uv tool install dbc     # or: pipx install dbc
      dbc install "databricks>=0.2.0"

   Semolina needs driver version 0.2.0 or later. It is the first release that
   accepts bind parameters, which is how Semolina sends filter values, and that
   returns decimals as ``decimal.Decimal`` rather than strings. Run ``dbc list`` to see
   which version you have. On an older driver, unfiltered queries still run but any
   filtered query fails with ``NOT_IMPLEMENTED: parameterized queries``; upgrade with
   ``dbc install "databricks>=0.2.0"``.

   The driver is then found by name through the ADBC driver manifest -- there is no path
   to configure. If it is missing, the first connection fails with an ``ImportError``
   pointing at the Foundry docs, rather than a connection or auth error.

   The rest of this page assumes the driver is installed.

Connection fields
~~~~~~~~~~~~~~~~~

``DatabricksConfig`` accepts two connection forms and requires one of them in full:
either ``uri`` on its own, or all three of ``host``, ``http_path``, and ``token``.
Supplying neither raises a validation error when the config is built.

.. list-table::
   :header-rows: 1
   :widths: 22 14 14 50

   * - Field
     - Type
     - Required
     - Description
   * - ``type``
     - ``str``
     - Yes
     - Must be ``"databricks"`` in a TOML section
   * - ``uri``
     - ``str``
     - One form
     - Full DSN, ``databricks://token:<token>@<host>:443/<http-path>``
   * - ``host``
     - ``str``
     - Other form
     - Workspace hostname (e.g. ``adb-123.azuredatabricks.net``)
   * - ``http_path``
     - ``str``
     - Other form
     - SQL warehouse HTTP path (e.g. ``/sql/1.0/warehouses/abc123``)
   * - ``token``
     - ``str``
     - Other form
     - Personal access token, starting with ``dapi``
   * - ``auth_type``
     - ``str``
     - No
     - ``OAuthU2M`` (browser) or ``OAuthM2M`` (service principal). Omit for PAT auth
   * - ``client_id``
     - ``str``
     - No
     - Service principal client ID, for ``OAuthM2M``
   * - ``client_secret``
     - ``str``
     - No
     - Service principal client secret, for ``OAuthM2M``
   * - ``catalog``
     - ``str``
     - No
     - Default Unity Catalog
   * - ``schema``
     - ``str``
     - No
     - Default schema

.. warning:: ``catalog`` and ``schema`` are ignored in URI mode

   In the decomposed form, ``catalog`` and ``schema`` are appended to the generated DSN
   as query parameters, which is how the Databricks driver picks up a default namespace.
   A ``uri`` you supply yourself is passed through untouched, so anything you want in
   scope has to already be in that string.

Use Unity Catalog three-part names
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Databricks resolves objects through
`Unity Catalog <https://docs.databricks.com/aws/en/data-governance/unity-catalog/index.html>`_,
a three-level namespace of ``catalog.schema.view``. Setting ``catalog`` and ``schema``
on the connection is one way to reach a view. The other is to qualify it in the model,
which keeps the model portable across connections:

.. code-block:: python

   from semolina import Dimension, Metric, SemanticView


   class Sales(SemanticView, view="main.analytics.sales"):
       revenue = Metric()
       country = Dimension()

Semolina quotes each part separately rather than treating the dotted string as one
identifier:

.. code-block:: sql

   SELECT MEASURE(`revenue`) AS `revenue`, `country` AS `country`
   FROM `main`.`analytics`.`sales`
   GROUP BY ALL

Generate a model from a metric view
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``semolina codegen --backend databricks <view>`` runs
``DESCRIBE TABLE EXTENDED ... AS JSON`` over the same ADBC pool and writes a
:py:class:`~semolina.models.SemanticView` subclass. Measures become
:py:class:`~semolina.fields.Metric` fields and dimensions become
:py:class:`~semolina.fields.Dimension` fields. A column type with no clean Python
equivalent is typed ``Any``, with a ``TODO`` comment for you to fill in. See
:ref:`howto-codegen`.

.. _howto-backends-duckdb:

DuckDB
------

DuckDB stands in for a warehouse throughout the tutorials, and it is also a backend you
can ship on. Shipping on it means a database file you keep, rather than the throwaway
:ref:`tutorial-first-query` builds. For the in-memory fixture a test suite wants
instead, see :ref:`howto-warehouse-testing`.

Connection fields
~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 22 14 14 50

   * - Field
     - Type
     - Required
     - Description
   * - ``type``
     - ``str``
     - Yes
     - Must be ``"duckdb"`` in a TOML section
   * - ``database``
     - ``str``
     - No
     - File path or ``":memory:"`` (default: ``":memory:"``)
   * - ``read_only``
     - ``bool``
     - No
     - Open the database in read-only mode (default: ``false``)

.. note:: An in-memory database is shared by the whole pool

   Every pooled connection is cloned from one source connection and opens its
   database, so with ``":memory:"`` all of them see the same tables and views, including
   connections the pool opens later to replace recycled ones. The database lasts until
   the engine is disposed. Create your tables and views once and commit, rather than on
   every new connection.

   An in-memory pool defaults to 5 connections like any other, so concurrent queries
   work without a database file.

The semantic_views extension
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

DuckDB's ``semantic_view()`` table function comes from the ``semantic_views`` community
extension. :py:func:`~semolina.config.create_engine` runs
``INSTALL semantic_views FROM community`` and ``LOAD semantic_views`` on each new
connection in the pool, so you do not need to install or load it yourself. ``INSTALL``
is a no-op once the extension is cached locally.

The first ``INSTALL`` downloads the extension, so it needs network access. If either step
fails, for example because you are offline or the cached build does not match your DuckDB
version, the checkout raises :py:exc:`~semolina.exceptions.SemolinaExtensionLoadError`
with DuckDB's error as its ``__cause__``. It is a subclass of
:py:exc:`~semolina.exceptions.SemolinaConnectionError`, so a handler for connection
failures catches it too.

The ``semolina[duckdb]`` extra pins its DuckDB version deliberately: the community
extension publishes binaries only for specific DuckDB core releases, and a mismatched
pair fails to load.

.. note:: Commit after ``CREATE SEMANTIC VIEW`` on a shared connection

   If you create a semantic view and query it on the same connection, call ``commit()``
   after the DDL. ADBC connections open with ``autocommit=False``, and the extension
   expands ``semantic_view()`` on a separate read connection that sees only committed
   state, so an uncommitted view looks like a missing one. Code that creates views
   through a separate session, or that sets ``autocommit=True``, is unaffected.

A semantic view records the tables it reads under the catalog name DuckDB took from the
database file's name. ``CREATE SEMANTIC VIEW`` in ``orig.duckdb`` stores
``"orig"."main"."sales_data"``, so the view breaks when the file is copied or renamed:

.. warning:: A copied or renamed database file breaks its semantic views

   Opened as ``copy.duckdb``, the database's catalog is ``copy``, and any query on a view
   created in ``orig.duckdb`` fails with ``Binder Error: Catalog "orig" does not exist!``.
   Run ``CREATE OR REPLACE SEMANTIC VIEW`` again in the new file to point the view at its
   own tables. Keep the DDL in a script so you can rerun it, rather than copying a
   database file between environments.

.. _howto-backends-duckdb-setup:

Attach catalogs and create secrets at startup
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Your semantic views may read tables that live outside the database file: an Iceberg
catalog such as an Amazon S3 Tables bucket, another DuckDB file, or Parquet on S3. DuckDB
saves none of that setup in the database file. Extensions you ``LOAD``, secrets you
``CREATE``, and catalogs you ``ATTACH`` last only while the database is open, so your
application has to run them each time it starts.

Run them once, straight after :py:func:`~semolina.config.create_engine`, on a connection
checked out with :py:meth:`engine.connect() <semolina.engines.base.Engine.connect>`. Every
connection in the engine's pool opens the same DuckDB database, and extensions, secrets
and attached catalogs belong to that database rather than to one connection. A single
setup run therefore covers every connection the pool holds now or opens later, at any
``pool_size``.

.. tab-set::
   :sync-group: sync-async

   .. tab-item:: Sync
      :sync: sync

      .. code-block:: python

         from adbc_poolhouse import DuckDBConfig

         from semolina import create_engine

         SETUP_SQL = [
             "INSTALL aws",
             "INSTALL httpfs",
             "INSTALL iceberg",
             "LOAD aws",
             "LOAD httpfs",
             "LOAD iceberg",
             "CREATE OR REPLACE SECRET s3tables (TYPE s3, PROVIDER credential_chain)",
             "ATTACH IF NOT EXISTS 'arn:aws:s3tables:eu-west-2:111122223333:bucket/analytics' "
             "AS s3t (TYPE iceberg, ENDPOINT_TYPE s3_tables)",
         ]

         engine = create_engine(
             DuckDBConfig(database="views.duckdb", pool_size=4),
             register=True,
         )

         with engine.connect() as conn:
             with conn.cursor() as cur:
                 for statement in SETUP_SQL:
                     cur.execute(statement)
             conn.commit()

   .. tab-item:: Async
      :sync: async

      .. code-block:: python

         from adbc_poolhouse import DuckDBConfig

         from semolina import create_async_engine

         engine = create_async_engine(
             DuckDBConfig(database="views.duckdb", pool_size=4),
             register=True,
         )

         # SETUP_SQL is the same list as in the Sync tab
         async with await engine.connect() as conn:
             async with conn.cursor() as cur:
                 for statement in SETUP_SQL:
                     await cur.execute(statement)
             await conn.commit()

      ``engine.connect()`` is awaited first, and the connection it returns is then used as
      an async context manager, which hands it back to the pool on exit.

Put this next to the code that builds the engine, such as your web framework's startup
hook, so it runs once in each process before the first query. :ref:`howto-web-api` shows
that hook for FastAPI, including disposing the engine at shutdown.
``PROVIDER credential_chain`` reads AWS credentials from the environment, an instance
profile or ``~/.aws``, so no key appears in your code.

.. warning:: Commit the setup, or the pool undoes it

   ADBC connections open with ``autocommit=False``, so the setup statements run inside a
   transaction. Without ``commit()``, other connections in the pool cannot see the secret,
   and when the setup connection goes back to the pool its rollback undoes the
   ``ATTACH``. The next query then fails with
   ``Binder Error: Catalog "s3t" does not exist!``, even on the connection that ran the
   setup.

Settings that DuckDB scopes to one connection, such as ``SET search_path``, are the
exception to the run-once rule: they do not carry over to the pool's other connections.
Name tables by their full ``catalog.schema.table`` path in your ``CREATE SEMANTIC VIEW``
DDL instead of relying on a search path.

See also
--------

- :ref:`tutorial-first-query` -- the query code that runs unchanged above any of these
  engines
- :ref:`howto-connection-pools` -- pool sizing, engine lifecycle, and several named
  engines at once
- :ref:`howto-warehouse-testing` -- run your query code against a local DuckDB fixture
  instead of a warehouse
- :ref:`howto-typed-results` -- get the same field names on every backend
- :ref:`howto-web-api` -- build an engine in your web framework's startup hook and dispose
  it at shutdown
- :ref:`howto-codegen-credentials` -- the credentials ``semolina codegen`` reads, and how
  they differ from these
- :ref:`explanation-semantic-views` -- what a semantic view is in each warehouse
- :ref:`explanation-duckdb-vs-warehouse` -- what a query that works on DuckDB proves
  about the same query on Snowflake, and what it does not
- :ref:`reference-config` -- the full ``.semolina.toml`` file format
