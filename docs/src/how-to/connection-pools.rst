.. _howto-connection-pools:

How to manage engines and connection pools
==========================================

:ref:`tutorial-first-query` builds one engine, registers it, and never mentions it
again. That is fine for a script. A service has to decide how big the pool is, when it
closes, and what happens when a second warehouse needs an engine of its own.

This page is about that lifecycle. For the credentials and settings a particular
warehouse needs, see :ref:`howto-backends`.

An :py:class:`Engine <semolina.engines.base.Engine>` owns one ADBC connection
pool and the dialect for a warehouse. You build it once with
:py:func:`~semolina.config.create_engine`, then run queries through it.

An :py:class:`AsyncEngine <semolina.engines.abase.AsyncEngine>` is its sibling,
built with :py:func:`~semolina.config.create_async_engine` from the same configs and
carrying the same dialect. Each section below covers both. Which kind you get is
fixed by which constructor you called, so an engine is never switched between
modes.

Two ways to use an engine
-------------------------

There are two patterns, and you can mix them in the same application.

The **direct engine** pattern keeps a reference to the engine and calls it
yourself. It mirrors SQLAlchemy: ``create_engine(...)`` hands you an engine, and
:py:meth:`engine.execute(query) <semolina.engines.base.Engine.execute>` runs a query
through its pool.

.. code-block:: python

   from adbc_poolhouse import SnowflakeConfig

   from semolina import (
       SemanticView,
       Metric,
       Dimension,
       create_engine,
   )


   class Sales(SemanticView, view="sales"):
       revenue = Metric()
       country = Dimension()


   engine = create_engine(
       SnowflakeConfig(
           account="xy12345.us-east-1",
           user="svc_analytics",
           password="...",
           database="analytics",
           warehouse="compute_wh",
       )
   )

   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
   )
   cursor = engine.execute(query)
   for row in cursor.fetchall_rows():
       print(row.country, row.revenue)

The **named registry** pattern registers an engine under a name and lets the
query resolve it. It mirrors Django's database aliases: register once at startup,
then ``.execute()`` finds the engine for you. A query with no ``.using()`` clause
resolves the ``"default"`` engine. ``register=True`` builds and registers in one
call, under the connection name:

.. code-block:: python

   from semolina import create_engine

   create_engine("default", register=True)

   # No .using() -> resolves the "default" engine
   cursor = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
       .execute()
   )
   for row in cursor.fetchall_rows():
       print(row.country, row.revenue)

The registry pattern is the better fit for web applications, where the engine is
created once at startup and endpoints query without passing it around. The direct
pattern suits scripts and notebooks where you already hold the engine.

Build an engine from a config object or a connection name
---------------------------------------------------------

:py:func:`~semolina.config.create_engine` accepts either an ``adbc-poolhouse`` config
object or the name of a ``.semolina.toml`` connection section. The dialect is
derived from the config type, so you never select a backend by hand. What comes
back is the matching subclass, one of
:py:class:`~semolina.engines.snowflake.SnowflakeEngine`,
:py:class:`~semolina.engines.databricks.DatabricksEngine`, or
:py:class:`~semolina.engines.duckdb.DuckDBEngine`. Annotate against
:py:class:`Engine <semolina.engines.base.Engine>` rather than the subclass, since
that is what the factory's return type declares.

Pass a config object when credentials come from a vault, a secrets manager, or
your own code. The example below is Snowflake; the fields each backend takes are in
:ref:`howto-backends`.

.. code-block:: python

   from adbc_poolhouse import SnowflakeConfig

   from semolina import create_engine

   engine = create_engine(
       SnowflakeConfig(
           account="xy12345.us-east-1",
           user="svc_analytics",
           password="...",
           database="analytics",
           warehouse="compute_wh",
       )
   )

Pass a connection name to read settings from a ``[connections.<name>]`` section of
``.semolina.toml`` instead:

.. code-block:: python

   from semolina import create_engine

   engine = create_engine(
       "default"
   )  # reads [connections.default]

``create_engine()`` with no argument is the same as ``create_engine("default")``.
Point at a different file with ``create_engine("default", config_path="config/warehouse.toml")``.

:py:func:`~semolina.config.create_async_engine` has the same signature and accepts the same
two argument forms:

.. code-block:: python

   from adbc_poolhouse import SnowflakeConfig

   from semolina import create_async_engine

   # From a config object
   engine = create_async_engine(
       SnowflakeConfig(
           account="xy12345.us-east-1",
           user="svc_analytics",
           password="...",
           database="analytics",
           warehouse="compute_wh",
       )
   )

   # Or from a .semolina.toml connection name
   engine = create_async_engine("default")

Note the missing ``await``. ``create_async_engine()`` is an ordinary call you can make at
import time or outside a running event loop. It does block briefly: building the pool opens
one connection to the warehouse, the source the pooled connections are cloned from, so a bad
credential fails here rather than on the first query. Teardown is the half that is awaited;
see `Manage the engine lifecycle`_ below. Async support needs the ``semolina[async]`` extra;
see :ref:`tutorial-installation`.

Size the pool
-------------

A pool keeps ``pool_size`` connections open and hands them out one query at a time. When
all of them are busy, it opens up to ``max_overflow`` more for the burst and closes each
one when it comes back. A query that finds every connection busy, overflow included,
waits up to ``timeout`` seconds for one to be returned and then fails.

Pool sizing lives on the config object, for async pools as much as synchronous ones.
The config classes carry ``pool_size`` (steady-state connections), ``max_overflow``
(burst capacity above ``pool_size``), ``timeout``, and ``recycle``. For Snowflake and
Databricks the defaults are 5 and 3, so up to 8 concurrent connections:

.. code-block:: python

   from adbc_poolhouse import SnowflakeConfig

   from semolina import create_engine

   engine = create_engine(
       SnowflakeConfig(
           account="xy12345.us-east-1",
           user="svc_analytics",
           password="...",
           database="analytics",
           warehouse="compute_wh",
           pool_size=10,
           max_overflow=5,
           timeout=30,
           recycle=1800,
       )
   )

Set the same fields in a ``.semolina.toml`` section to size a pool you build by
name:

.. code-block:: toml
   :caption: .semolina.toml

   [connections.default]
   type = "snowflake"
   account = "xy12345.us-east-1"
   user = "svc_analytics"
   password = "..."
   database = "analytics"
   warehouse = "compute_wh"
   pool_size = 10
   max_overflow = 5
   recycle = 1800

The pool parameters control connection behaviour:

.. list-table::
   :header-rows: 1

   * - Parameter
     - Default
     - Description
   * - ``pool_size``
     - ``5``
     - Number of connections kept open and reused
   * - ``max_overflow``
     - ``3``
     - Extra connections allowed above ``pool_size`` under burst load
   * - ``timeout``
     - ``30``
     - Seconds to wait for a connection before raising an error
   * - ``recycle``
     - ``3600``
     - Seconds before a connection is replaced with a fresh one

Size the pool per process, for the number of queries in flight at once in that process.
Under FastAPI, sync ``def`` handlers run in a threadpool that holds 40 threads by default,
so that is how many queries one process can have in flight; the number of uvicorn workers
does not enter into it. Each worker process builds its own pool, so the warehouse sees at
most workers × (``pool_size`` + ``max_overflow``) connections from the service. Set
``max_overflow`` to 50--100% of ``pool_size`` for traffic spikes. A ``recycle`` of 1800
seconds keeps connections from going stale through a quiet period.

Both pool kinds read these fields from the config you pass, so an async pool is sized
the same way a synchronous one is. Nothing extra is needed to size it, and there is no
async-only tuning knob.

.. warning:: The checkout timeout raises SQLAlchemy's ``TimeoutError``, not Python's

   When every connection is busy and none frees up within ``timeout`` seconds, the
   checkout fails with ``sqlalchemy.exc.TimeoutError``. That class derives from
   ``SQLAlchemyError``, **not** from the builtin :py:class:`TimeoutError`, so a bare
   ``except TimeoutError:`` does not catch it.

   Import it explicitly to map pool exhaustion onto a 503:

   .. code-block:: python

      from sqlalchemy.exc import TimeoutError as PoolTimeout

      try:
          cursor = query.execute()
      except PoolTimeout:
          raise HTTPException(
              status_code=503,
              detail="No warehouse connection available",
          )

   This applies to async engines too. The async pool offloads the same checkout to a
   worker thread, so the same exception surfaces from ``aexecute()``.

DuckDB pools default to 5 connections whether the database is a file or ``":memory:"``.
All of an in-memory pool's connections share one database, so set it up once:
:ref:`howto-backends-duckdb` has the details.

The pool is also your concurrency bound. adbc-poolhouse gives each async pool a
capacity limiter sized to ``pool_size + max_overflow``, so that many queries can be in
flight and the rest wait for a slot. Semolina adds no second bound of its own -- no
semaphore, no worker count to tune. Raise or lower concurrency by changing
``pool_size`` and ``max_overflow``. Wrapping your own semaphore around ``aexecute()``
stacks a second limit on top of the pool's and generally just lowers throughput below
what you configured.

Open a raw connection
---------------------

When you need to run something the query builder does not cover, check a
connection out of the engine's pool with
:py:meth:`engine.connect() <semolina.engines.base.Engine.connect>`. It is a context
manager, so the connection returns to the pool on exit:

.. code-block:: python

   with engine.connect() as conn:
       cur = conn.cursor()
       cur.execute("SHOW VIEWS")
       rows = cur.fetchall()

Handle a warehouse you can't reach
----------------------------------

Semolina raises :py:exc:`~semolina.exceptions.SemolinaConnectionError` wherever it opens a
connection and the driver can't. That covers building the engine, since ``create_engine()``
and ``create_async_engine()`` open the pool's source connection, and every checkout after
that: ``engine.connect()``, the checkout ``.execute()`` and ``aexecute()`` start with, and
``introspect()``. The driver's own error is attached as ``__cause__``, so its message is
still in the traceback:

.. code-block:: python

   from adbc_poolhouse import DuckDBConfig

   from semolina import SemolinaConnectionError, create_engine

   try:
       engine = create_engine(
           DuckDBConfig(
               database="/data/missing.duckdb", read_only=True
           )
       )
   except SemolinaConnectionError as e:
       print(e)
       # Could not connect to the warehouse: INTERNAL: IO Error: Cannot open
       # database "/data/missing.duckdb" in read-only mode: database does not exist
       print(type(e.__cause__).__name__)
       # InternalError

Once a connection is open, a statement the warehouse rejects is not a connection failure.
It raises the driver's own ``adbc_driver_manager.Error`` subclass, unwrapped. Pool
exhaustion is the third case: it never reaches the driver and raises
``sqlalchemy.exc.TimeoutError``, as the warning in `Size the pool`_ describes.
:ref:`howto-web-api` maps all three onto HTTP status codes.

Manage the engine lifecycle
---------------------------

Create the engine at application startup and close it at shutdown. An
:py:class:`Engine <semolina.engines.base.Engine>` is a context manager, so one ``with``
block covers both ends: ``register=True`` registers the engine on the way in, and leaving
the block unregisters that name and disposes the pool on the way out.

.. code-block:: python

   from adbc_poolhouse import SnowflakeConfig

   from semolina import create_engine

   with create_engine(
       SnowflakeConfig(
           account="xy12345.us-east-1",
           user="svc_analytics",
           password="...",
           database="analytics",
           warehouse="compute_wh",
           pool_size=10,
       ),
       register=True,
   ):
       ...  # the application runs here

The registration name is the connection name, and the connection name defaults to
``"default"``. A config object carries no section name, so the engine above is registered
as ``"default"``, while ``create_engine("analytics", register=True)`` is registered as
``"analytics"``. Pass a string such as ``register="reports"`` to choose the name yourself. A
name that is already taken raises ``ValueError`` and leaves the engine holding it alone,
exactly as a hand-written :py:func:`~semolina.registry.register` call does.

An :py:class:`AsyncEngine <semolina.engines.abase.AsyncEngine>` is an async context
manager, and the ``async with`` form is otherwise the same:

.. code-block:: python

   from semolina import create_async_engine

   async with create_async_engine("default", register=True):
       ...  # the application runs here

Note the missing ``await`` on the construction. Construction is the same ordinary call
described in `Build an engine from a config object or a connection name`_, so there is
nothing to await. Disposing the pool closes live driver connections, and the async engine
offloads that work rather than block the event loop during shutdown, so the awaited half
is the block's exit. That block clears the
*async* registry, which is a separate store -- see
`Async engines live in a separate registry`_.

Write the steps out when you need to
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The same lifecycle written out is a build, a registration, and two teardown calls. Nothing
is deprecated here -- the block is shorthand for exactly this:

.. code-block:: python

   from adbc_poolhouse import SnowflakeConfig

   from semolina import register, unregister, create_engine

   # Startup
   engine = create_engine(
       SnowflakeConfig(
           account="xy12345.us-east-1",
           user="svc_analytics",
           password="...",
           database="analytics",
           warehouse="compute_wh",
           pool_size=10,
       )
   )
   register("default", engine)

   # ... application runs ...

   # Shutdown
   unregister("default")
   engine.dispose()

The shutdown order is the part to keep. :py:func:`~semolina.registry.unregister`
removes the engine from the registry so no new query resolves it, and
:py:meth:`engine.dispose() <semolina.engines.base.Engine.dispose>` then closes the pool
and the ADBC source connection behind it. Reverse the two and the disposed engine is still
reachable by name: the next query resolves it, hands it work, and fails inside the driver
with ``ProgrammingError: INVALID_ARGUMENT: [Driver Manager] Database is not initialized``,
a message that never mentions disposal. The ``with`` block runs the two in that order, and
disposes the pool even when the block is left by an exception.

Dispose an engine once. One you opened with ``with`` is disposed by the block, so do not
call ``dispose()`` on it as well.

The async longhand uses the async trio and awaits the disposal:

.. code-block:: python

   from semolina import (
       create_async_engine,
       register_async_engine,
       unregister_async_engine,
   )

   # Startup -- an ordinary call
   engine = create_async_engine("default")
   register_async_engine("default", engine)

   # ... application runs ...

   # Shutdown -- awaited
   unregister_async_engine("default")
   await engine.dispose()

The method is ``dispose()``, awaited, rather than an ``adispose()``. That follows one naming
rule across Semolina's async surface: an async-only object names its methods like the
synchronous one, because the object already tells you the call is awaited.
:py:class:`~semolina.engines.abase.AsyncEngine` has ``connect()`` and ``dispose()``, and
:py:class:`~semolina.acursor.AsyncSemolinaCursor` has ``fetchall()`` and ``into()``, all
awaited. The ``a`` prefix appears only where a name would otherwise be ambiguous or where
Python names the method: ``aexecute()`` on a query, which also has a synchronous
``execute()``, and ``aclose()`` on the async cursor, which is what
:py:func:`contextlib.aclosing` calls.

Reach for the longhand when the engine outlives any one block (a module-level engine a
whole process shares, or one opened in one function and closed in another), and when a
single engine answers to several names. The block undoes only its own registration: it
drops the one name ``create_engine`` gave it, and a name you added with
:py:func:`~semolina.registry.register` is yours to remove, because the engine cannot know
which of several names you meant.

.. warning::

   Dispose the engine rather than reaching into ``engine._pool`` yourself.
   ``dispose()`` closes the underlying ADBC source connection as well as the pool, and
   it picks the right teardown call for the pool kind. On an async engine the pool's own
   ``close()`` is a coroutine, so calling it without awaiting closes nothing and leaks
   the pool silently.

Look up a registered engine
---------------------------

:py:func:`~semolina.registry.get_engine` returns the engine registered under a name, so you
can reach it without keeping your own reference:

.. code-block:: python

   from semolina import get_engine

   engine = get_engine("default")

Call it with no argument (or ``None``) to get the ``"default"`` engine -- the same
lookup ``.execute()`` performs when a query has no ``.using()`` clause:

.. code-block:: python

   engine = get_engine()  # the "default" engine

To close an engine at shutdown without tracking your own reference, look it up,
unregister it, then dispose it:

.. code-block:: python

   from semolina import get_engine, unregister

   engine = get_engine("reports")
   unregister("reports")
   engine.dispose()

If no engine is registered under the name, ``get_engine`` raises
:py:exc:`~semolina.exceptions.SemolinaEngineNotRegisteredError` listing the names that are
available. It is a ``ValueError`` too, so code written for an earlier release still catches
it.

Async engines live in a separate registry
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:py:func:`~semolina.registry.register_async_engine`,
:py:func:`~semolina.registry.get_async_engine`, and
:py:func:`~semolina.registry.unregister_async_engine` are the async trio, and they
operate on their own store. ``create_async_engine(..., register=True)`` writes to that
store too, never to the synchronous one:

.. code-block:: python

   from semolina import (
       create_async_engine,
       get_async_engine,
       register_async_engine,
       unregister_async_engine,
   )

   register_async_engine(
       "reports", create_async_engine("reports")
   )

   # get_async_engine() with no argument resolves "default"
   engine = get_async_engine("reports")

   unregister_async_engine("reports")
   await engine.dispose()

The two registries are separate, which has two consequences.
One name can hold a sync engine and an async engine at the same time, which is
what you want when the same warehouse is queried from both a batch script and a request
handler. And ``get_async_engine`` never falls back to the sync store: a name registered
only with :py:func:`~semolina.registry.register` raises
:py:exc:`~semolina.exceptions.SemolinaEngineNotRegisteredError` when the async path looks it
up. The error names the async registration function, so a lookup that fails this way
tells you which call you skipped rather than failing later inside query execution.

Query several warehouses with .using()
--------------------------------------

Register engines under different names to query multiple warehouses, or to use
different credentials for different workloads:

.. code-block:: python

   from adbc_poolhouse import SnowflakeConfig

   from semolina import create_engine

   # Production engine -- large warehouse, for dashboard queries
   create_engine(
       SnowflakeConfig(
           account="xy12345.us-east-1",
           user="svc_dashboard",
           password="...",
           database="analytics",
           warehouse="large_wh",
           pool_size=20,
           max_overflow=10,
       ),
       register="default",
   )

   # Reporting engine -- small warehouse, for scheduled reports
   create_engine(
       SnowflakeConfig(
           account="xy12345.us-east-1",
           user="svc_reports",
           password="...",
           database="analytics",
           warehouse="small_wh",
           pool_size=3,
       ),
       register="reports",
   )

A config object has no connection name to reuse, so each of these names is given as a
string. Neither engine is held in a variable: `Close every engine at shutdown`_ looks them
up by name when it is time to close them.

Use ``.using()`` on a query to pick which engine to run against:

.. code-block:: python

   from semolina import SemanticView, Metric, Dimension


   class Sales(SemanticView, view="sales"):
       revenue = Metric()
       country = Dimension()


   # Uses the "default" engine (implicit)
   cursor = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
       .execute()
   )

   # Uses the "reports" engine (explicit)
   cursor = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
       .using("reports")
       .execute()
   )

Engine resolution is lazy -- it happens at ``.execute()`` time, not when
``.using()`` is called. You can build queries before any engine is registered.

On the async path the same ``.using()`` clause resolves against the async registry:

.. code-block:: python

   async with await (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
       .using("reports")
       .aexecute()
   ) as cursor:
       rows = await cursor.fetchall_rows()

So a name you use with ``aexecute()`` has to be in the async registry, put there by
``register_async_engine()`` or by ``create_async_engine(..., register=...)``. Registering
``"reports"`` synchronously and then reaching for it from ``aexecute()`` raises rather than
silently running your query somewhere unexpected.

To drive multiple named engines from one TOML file, define a section per
connection and build each by name:

.. code-block:: toml
   :caption: .semolina.toml

   [connections.default]
   type = "snowflake"
   account = "xy12345.us-east-1"
   user = "svc_dashboard"
   password = "..."
   database = "analytics"
   warehouse = "large_wh"

   [connections.reports]
   type = "snowflake"
   account = "xy12345.us-east-1"
   user = "svc_reports"
   password = "..."
   database = "analytics"
   warehouse = "small_wh"

.. code-block:: python

   from semolina import create_engine

   create_engine("default", register=True)
   create_engine("reports", register=True)

``register=True`` reuses each section name, so the registry keys and the TOML sections
cannot drift apart.

Close every engine at shutdown
------------------------------

Engines opened in a ``with`` block close themselves. The ones you registered without a
block are closed by name at shutdown, one at a time:

.. code-block:: python

   from semolina import get_engine, unregister

   for name in ("default", "reports"):
       engine = get_engine(name)
       unregister(name)
       engine.dispose()

Async engines are closed in the same loop, one ``await`` at a time:

.. code-block:: python

   from semolina import (
       get_async_engine,
       unregister_async_engine,
   )

   for name in ("default", "reports"):
       engine = get_async_engine(name)
       unregister_async_engine(name)
       await engine.dispose()

If your application runs both kinds, close both. Neither loop sees the other's engines,
even where the names match.

See also
--------

- :ref:`tutorial-first-query` -- the one-engine version this page takes apart
- :ref:`tutorial-dashboard-api` -- opening and closing an engine in a FastAPI lifespan
- :ref:`howto-backends` -- the credentials and TOML fields each warehouse needs
- :ref:`howto-web-api` -- opening an async engine in a FastAPI lifespan, and choosing an
  engine per endpoint with ``.using()``
- :ref:`howto-streaming` -- stream rows with ``for row in cursor:`` or ``async for``
- :ref:`tutorial-installation` -- install the ``semolina[async]`` extra
- :ref:`reference-config` -- the ``.semolina.toml`` file format
