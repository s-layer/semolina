.. _howto-web-api:

How to run Semolina inside FastAPI
===================================

:ref:`tutorial-dashboard-api` builds one synchronous endpoint, from its first ``dict(row)``
response to a typed and filtered one. This guide covers what that endpoint needs once it is
deployed: ``async def`` handlers, closing async cursors, a deadline on a slow query, a client
that hangs up mid-request, the driver exceptions to catch, and endpoints that reach
different warehouses.

It assumes you already have a :py:class:`~semolina.models.SemanticView` subclass and an
engine opened for the application's lifetime by a FastAPI ``lifespan`` handler. Start with
the tutorial if you have not built that yet, and see :ref:`howto-connection-pools` for
sizing the pool it opens.

The snippets reuse this model:

.. code-block:: python

   from semolina import SemanticView, Metric, Dimension


   class Sales(SemanticView, view="sales"):
       revenue = Metric()
       cost = Metric()
       country = Dimension()
       region = Dimension()

.. note:: The examples below return ``dict(row)``

   That keeps the topic under discussion visible; it is not what you should deploy. The keys
   are the model's field names on every backend, but a dictionary has no schema for FastAPI
   to publish, and FastAPI encodes a ``Decimal`` in it as a float. Return a DTO instead:
   :ref:`howto-typed-results` for ``.into()``, :ref:`howto-dto-codegen` for generating the
   class, and :ref:`howto-result-column-names` for how columns are named.

Serve queries from ``async def`` handlers
-----------------------------------------

Reach for the async path when the handler is ``async def`` for reasons of its own: it awaits
another service, streams its response, or races the query against the disconnect watcher
below. Reach for it too when your framework offers no threadpool fallback for blocking
handlers. A synchronous handler that already works is not a problem to fix, and the tutorial
covers why a plain ``def`` is a correct choice under FastAPI.

Open an async engine at startup
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

An ``async def`` handler needs an :py:class:`~semolina.engines.abase.AsyncEngine`, which is a
distinct type with its own registry rather than a mode on the synchronous one. Build it in
the same ``lifespan`` handler the tutorial uses, and register it there in one step:

.. code-block:: python
   :caption: app.py

   from contextlib import asynccontextmanager

   from adbc_poolhouse import SnowflakeConfig
   from fastapi import FastAPI

   from semolina import create_async_engine


   @asynccontextmanager
   async def lifespan(app: FastAPI):
       async with create_async_engine(
           SnowflakeConfig(
               account="xy12345.us-east-1",
               user="svc_dashboard",
               password="...",
               database="analytics",
               warehouse="compute_wh",
               pool_size=10,
               max_overflow=5,
           ),
           register=True,
       ):
           yield


   app = FastAPI(lifespan=lifespan)

Note the missing ``await`` on the construction.
:py:func:`~semolina.config.create_async_engine` is an ordinary call that opens the pool's
first connection before it returns, and raises
:py:exc:`~semolina.exceptions.SemolinaConnectionError` if it can't. Teardown is the half
that awaits, which is why the block is an ``async with``. ``register=True`` files the
engine in the **async** registry under ``"default"``, and leaving the block drops it from
there before awaiting ``dispose()``.

:ref:`howto-connection-pools` covers that asymmetry, pool sizing, registering more than one
engine, and the longhand this block stands in for. The async surface needs the
``semolina[async]`` extra; a plain install of the package does not carry it. See
:ref:`tutorial-installation` for the install command.

Run the query with ``aexecute()``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

In an ``async def`` handler, call ``aexecute()`` on the query instead of ``execute()``, and
hold the cursor open with ``async with``:

.. code-block:: python
   :caption: app.py (continued)

   @app.get("/api/sales")
   async def get_sales():
       query = (
           Sales.query()
           .metrics(Sales.revenue, Sales.cost)
           .dimensions(Sales.country)
       )
       async with await query.aexecute() as cursor:
           rows = await cursor.fetchall_rows()
       return [dict(row) for row in rows]

Two things differ from the synchronous handler, and nothing else does. The execute call is
awaited, and the fetch methods keep their names but are awaited too. Rows are the same
:py:class:`~semolina.results.Row` objects, and ``cursor.description`` and ``cursor.rowcount``
are still plain attribute reads with no ``await``, because adbc-poolhouse keeps them
synchronous.

``await query.aexecute()`` returns a cursor that is already open, which is why the call site
reads ``async with await ...``. Query building is unchanged: the same ``.metrics()``,
``.dimensions()``, ``.where()``, and ``.limit()`` calls apply, and only the final step
differs.

.. note::

   Your code imports neither ``asyncio`` nor ``anyio``, and the handler above runs unchanged
   under asyncio or Trio. Semolina awaits adbc-poolhouse's coroutines and adds no
   loop-specific code of its own, so the backend is whichever one your framework is already
   running. There is nothing to configure.

.. _howto-web-api-async-cursor-close:

Close async cursors with ``async with``
---------------------------------------

The tutorial's ``with query.execute() as cursor:`` is about promptness, returning the pooled
connection the moment the handler is done rather than whenever the cursor is collected. On
the async path the same shape is not optional:

.. code-block:: python

   @app.get("/api/sales/summary")
   async def get_sales_summary():
       query = Sales.query(
           metrics=[Sales.revenue, Sales.cost],
           dimensions=[Sales.country],
       )
       async with await query.aexecute() as cursor:
           rows = await cursor.fetchall_rows()

       # reader, cursor, and connection are closed here
       return {
           "total_countries": len(rows),
           "results": [dict(row) for row in rows],
       }

.. warning:: ``async with`` is required, not recommended

   The two cursors do not behave the same way when you forget to close them.
   :py:class:`~semolina.cursor.SemolinaCursor` has a finalizer that returns a forgotten
   connection to the pool. :py:class:`~semolina.acursor.AsyncSemolinaCursor` cannot have that
   finalizer: closing it requires awaiting, and a finalizer cannot await.

   An async cursor closed by neither ``async with`` nor ``await cursor.aclose()`` therefore
   holds its pooled connection for the life of the process. Nothing reclaims it later. Enough
   forgotten cursors exhaust the pool, and every subsequent request blocks until its checkout
   times out. The async cursor emits a ``ResourceWarning`` when it is garbage collected
   unclosed, which tells you it happened but does not repair it.

.. _howto-web-api-timeouts:

Time out a slow query
----------------------

Put a deadline around the query so a slow one fails as a ``504`` instead of holding a pooled
connection until the client gives up:

.. code-block:: python

   import asyncio

   from fastapi import HTTPException


   @app.get("/api/sales")
   async def get_sales():
       query = (
           Sales.query()
           .metrics(Sales.revenue, Sales.cost)
           .dimensions(Sales.country)
       )
       try:
           async with asyncio.timeout(10):
               async with await query.aexecute() as cursor:
                   rows = await cursor.fetchall_rows()
       except TimeoutError:
           raise HTTPException(
               status_code=504,
               detail="Query exceeded its time budget",
           )

       return [dict(row) for row in rows]

``asyncio.timeout()`` fits FastAPI, which runs on asyncio. ``anyio.fail_after()`` does the
same job in code that has to run under either loop. Semolina imports neither, so importing
whichever one you need in your own application is fine.

The exception you catch is your framework's, not the driver's. adbc-poolhouse fires the
driver's cancel from inside a shield and re-raises the loop's cancellation in place of ADBC's
interrupt error, and Semolina's frames pass that through rather than catching it, logging it,
or converting it. Caught outside the ``asyncio.timeout()`` block it is ``TimeoutError``;
inside the block, and under ``anyio.fail_after()``, it is the loop's own cancellation class.

A cancelled ``aexecute()`` raises rather than handing back a cursor. There is no half-open
cursor left over to find and close, and the ``async with`` above never starts.

Keep the fetch inside the time budget too, as the snippet does. Some drivers stream: DuckDB
2.0 returns from ``aexecute()`` as soon as the statement is prepared and runs the query while
the rows are fetched, so a budget around ``aexecute()`` alone would cover almost none of the
work.

The work stops in the warehouse, not only in your process. While the worker thread is still
inside the driver, adbc-poolhouse calls ``adbc_cancel`` on the connection from inside a
shield, so the statement is aborted rather than left to finish for nobody: a cancelled
aggregate returns in a small fraction of the time the same query takes when it is left alone.
On a metered warehouse that is the difference between a timed-out request that stops costing
you money and one that keeps billing for a result nobody will read.

The connection whose query was aborted is invalidated. adbc-poolhouse discards it instead of
handing it to the next caller, the pool opens a replacement, and its checkout count returns
to zero. The request after a timeout gets a working connection, and there is nothing for you
to reset.

Wrapping the whole ``async with`` block, as the snippet does, is safe, even though it means
the cursor tears down while the cancellation is propagating. Close runs in order (reader,
then cursor, then connection) and each step suppresses ``Exception`` rather than
``BaseException``, so a ``ConnectionBusyError`` born during teardown cannot replace the
cancellation you need to see.

.. note::

   Aborting a ``semantic_view()`` query on DuckDB needs the ``semantic_views`` community
   extension at 0.12.0 or newer, which is what the pinned ``duckdb==2.0.0`` in the
   ``semolina[duckdb]`` extra installs. Builds below that floor evaluated the inner query on a
   separate client context, where DuckDB's per-context interrupt flag was never read, so a
   cancelled aggregate finished its work first and only then reported the interrupt.

.. _howto-web-api-client-disconnect:

Handle a client disconnect
---------------------------

Start from what your framework does, which is less than most people assume. Starlette, which
FastAPI routes requests through, awaits your handler directly and does not race it against a
disconnect watcher. The ASGI server delivers ``http.disconnect`` on the receive channel, and a
handler that never reads from that channel never finds out the client has gone.

So a browser tab closed mid-request cancels nothing by itself. The query keeps running, the
pooled connection stays checked out, and on a metered warehouse the query keeps billing until
it finishes and the result is thrown away.

Turn the disconnect into a cancellation yourself. Run the query in a task group alongside a
poll of ``await request.is_disconnected()``, and cancel the group's scope when the poll comes
back true:

.. code-block:: python

   import anyio
   from fastapi import HTTPException, Request


   @app.get("/api/sales")
   async def get_sales(request: Request):
       query = (
           Sales.query()
           .metrics(Sales.revenue)
           .dimensions(Sales.country)
       )
       rows = None

       async with anyio.create_task_group() as task_group:

           async def watch_for_disconnect():
               while not await request.is_disconnected():
                   await anyio.sleep(0.5)
               task_group.cancel_scope.cancel()

           async def run_query():
               nonlocal rows
               async with await query.aexecute() as cursor:
                   rows = await cursor.fetchall_rows()
               task_group.cancel_scope.cancel()

           task_group.start_soon(watch_for_disconnect)
           task_group.start_soon(run_query)

       if rows is None:
           raise HTTPException(
               status_code=499,
               detail="Client disconnected",
           )

       return [dict(row) for row in rows]

``is_disconnected()`` is awaited but never blocks: it reads the receive channel inside an
already-cancelled scope, so it takes whatever message is sitting there and moves on. It does
consume from that channel, so pair this with a handler that is not also streaming the request
body.

Once the cancellation lands, everything in :ref:`howto-web-api-timeouts` applies. The
warehouse query aborts, ``aexecute()`` raises instead of handing back a cursor, and the
connection it was running on is invalidated and replaced.

One case the deadline section does not cover needs stating on its own, because it is the
case a disconnect produces. When the cancellation arrives *after* the connection has been
checked out, with the statement already in flight, the slot still comes back: the pool's
checked-out count returns to zero and its checked-in count goes up by one. An abandoned
request costs you a cancelled query, not a connection.

Reach for the watcher only when you need it. A deadline is the simpler bound, it needs no
extra task, and it covers the abandoned request too, since a client that has gone away is not
waiting for the response that the timeout produces. The watcher earns its place when a
request may legitimately outlive any deadline you would be willing to set.

Catch the exception the driver actually raised
-----------------------------------------------

``.execute()`` and ``aexecute()`` build the SQL, check a connection out of the pool, and run
it. If the driver cannot open a connection, the checkout raises
:py:exc:`~semolina.exceptions.SemolinaConnectionError`, as does any other place Semolina
opens one (:ref:`howto-connection-pools` lists them), with the driver's error as its
``__cause__``. If the statement itself fails, Semolina returns the connection to the pool and
re-raises the driver's exception unchanged. It does not wrap that one, so the type to catch
for a failed query is ``adbc_driver_manager.Error``. The tutorial catches it alongside
``SemolinaConnectionError`` and turns either into a ``503``. What it leaves to this page is
which subclass you get and the one failure that never reaches the driver at all.

The DBAPI hierarchy applies: ``Error`` is the base, ``DatabaseError`` sits under it, and
``ProgrammingError``, ``OperationalError``, ``DataError``, ``IntegrityError``,
``InternalError``, and ``NotSupportedError`` sit under ``DatabaseError``. ``InterfaceError``
sits directly under ``Error``.

**Which subclass you get is the driver's decision, not Semolina's, and it is not the one you
would guess.** Measured against the DuckDB driver:

.. list-table::
   :header-rows: 1
   :widths: 30 35 35

   * - Failure
     - DuckDB
     - Snowflake / Databricks
   * - View or table does not exist
     - ``InternalError``
     - not yet measured
   * - Invalid SQL syntax
     - ``InternalError``
     - not yet measured
   * - Column does not exist
     - ``InternalError``
     - not yet measured
   * - Value fails to cast
     - ``ProgrammingError``
     - not yet measured

A missing view arriving as ``InternalError`` rather than ``ProgrammingError`` is the useful
lesson here: the subclass does not describe the failure the way its name suggests. So catch
``Error`` and let the specific subclasses be an optimization you add only after observing your
own driver. Log the message; it carries the detail the class does not.

The Snowflake and Databricks columns are blank because measuring them needs a live warehouse.
The recorded test cassettes contain only successful queries. Treat those two drivers as
"catch ``Error``" until someone fills the column in.

One failure never reaches the driver at all. If every pooled connection is busy and none
frees up within the pool's ``timeout``, the checkout fails first, with
``sqlalchemy.exc.TimeoutError``. That is SQLAlchemy's class, not the builtin
:py:class:`TimeoutError`, so a bare ``except TimeoutError:`` misses it. It is the clearest
503 signal you have, because it means your pool is undersized rather than your warehouse
being down:

.. code-block:: python

   from sqlalchemy.exc import TimeoutError as PoolTimeout

   try:
       cursor = query.execute()
   except PoolTimeout:
       raise HTTPException(
           status_code=503,
           detail="No warehouse connection available",
       )

See :ref:`howto-connection-pools` for sizing the pool so this stays rare.

.. warning:: A missing view is not a 404

   There is no exception type that means "this semantic view does not exist" on the query
   path. A missing or misspelled view fails inside the warehouse like any other bad
   identifier, and arrives as a driver error. If your API needs to answer 404 for an unknown
   view, validate the view name against a list your application controls before building the
   query. Do not try to pattern-match the driver's message.

   :py:exc:`~semolina.exceptions.SemolinaViewNotFoundError` won't help either: only
   :py:meth:`Engine.introspect() <semolina.engines.base.Engine.introspect>` raises it, for
   :ref:`howto-codegen`, so nothing on the query path does.

.. _howto-web-api-errors:

Catch Semolina's own errors in one place
----------------------------------------

Every exception class Semolina defines derives from
:py:exc:`~semolina.exceptions.SemolinaError`. On the query path that means a query naming an
engine nobody registered (:py:exc:`~semolina.exceptions.SemolinaEngineNotRegisteredError`), a
result read a second way (:py:exc:`~semolina.exceptions.SemolinaResultConsumedError`), a DTO
that does not describe the result (:py:exc:`~semolina.exceptions.SemolinaSchemaMismatchError`),
and a method whose optional package is missing
(:py:exc:`~semolina.exceptions.SemolinaMissingDependencyError`).

None of these is the client's fault. Each one means your service is misconfigured or has a
bug, so a handler that catches the base class should answer ``500`` and log the message,
which names the cause. The one to treat differently is
:py:exc:`~semolina.exceptions.SemolinaConnectionError`, which means the warehouse could not
be reached.

Each error also keeps the base class it had before ``SemolinaError`` existed: three of the
four above are still :py:class:`RuntimeError`, and the unregistered engine is still
:py:class:`ValueError`. An ``except`` clause written for an earlier release keeps matching.

One ``SemolinaError`` is the client's fault.
:py:exc:`~semolina.exceptions.SemolinaSpecError` comes from building a query out of field
names and plain data with :mod:`semolina.spec` (see :ref:`howto-query-by-name`), and it
means the request named a field the model doesn't have or sent data of the wrong shape.
Answer it with a ``400`` and its message, which names the bad value and what to send
instead. It is also a :py:class:`ValueError`.

Mistakes in how a query is built, such as passing a string to ``.limit()`` or a dimension to
``.metrics()``, raise the builtin :py:class:`TypeError` or :py:class:`ValueError` instead.
So do setup mistakes, such as registering one engine name twice. Those are bugs to fix
rather than conditions to handle, and a type checker reports most of the query ones before
the code runs. The exception is a query built from request input: a negative ``limit``
taken from a query parameter is the client's mistake, and deserves a ``400``.

Put together, each failure maps to one status:

.. list-table::
   :header-rows: 1
   :widths: 55 15 30

   * - Exception
     - Status
     - Meaning
   * - ``sqlalchemy.exc.TimeoutError``
     - ``503``
     - Pool exhausted; no connection freed up within ``timeout``
   * - :py:exc:`~semolina.exceptions.SemolinaConnectionError`
     - ``503``
     - The warehouse is unreachable or refused the credentials
   * - ``adbc_driver_manager.Error``
     - ``503`` or ``500``
     - The warehouse rejected the statement. ``500`` unless you have observed which
       subclasses your driver raises for transient failures
   * - :py:exc:`~semolina.exceptions.SemolinaSpecError`
     - ``400``
     - The request's query data names an unknown field or has the wrong shape
   * - Any other :py:exc:`~semolina.exceptions.SemolinaError`
     - ``500``
     - Your service is misconfigured or has a bug
   * - :py:class:`TypeError` / :py:class:`ValueError` from building the query out of
       request input
     - ``400``
     - The request asked for something the query cannot express

Catch them in that order, with the query built inside the ``try`` so a bad request value
lands there too. The order matters at the end:
:py:exc:`~semolina.exceptions.SemolinaEngineNotRegisteredError` is also a
:py:class:`ValueError`, so ``except SemolinaError`` has to come before
``except (TypeError, ValueError)`` or a missing engine would read as the client's fault.
``except SemolinaSpecError`` has to come before ``except SemolinaError``, or a client's
typo would read as your bug.

.. code-block:: python

   import logging

   import adbc_driver_manager
   from fastapi import HTTPException
   from sqlalchemy.exc import TimeoutError as PoolTimeout

   from semolina import (
       SemolinaConnectionError,
       SemolinaError,
       SemolinaSpecError,
   )

   logger = logging.getLogger(__name__)

   try:
       query = (
           Sales.query()
           .metrics(Sales.revenue)
           .dimensions(Sales.country)
           .limit(limit)  # from the request
       )
       cursor = query.execute()
   except PoolTimeout:
       logger.warning("Connection pool exhausted")
       raise HTTPException(
           status_code=503,
           detail="No warehouse connection available",
       )
   except SemolinaConnectionError:
       logger.exception("Could not reach the warehouse")
       raise HTTPException(
           status_code=503, detail="Warehouse unavailable"
       )
   except adbc_driver_manager.Error:
       logger.exception("The warehouse rejected the query")
       raise HTTPException(
           status_code=500, detail="Internal error"
       )
   except SemolinaSpecError as exc:
       # Only raised when the query is built from request data
       # with semolina.spec, such as build_query(Sales, body).
       raise HTTPException(status_code=400, detail=str(exc))
   except SemolinaError:
       logger.exception("Semolina refused the query")
       raise HTTPException(
           status_code=500, detail="Internal error"
       )
   except (TypeError, ValueError) as exc:
       raise HTTPException(status_code=400, detail=str(exc))

A failed statement's driver exception is not a ``SemolinaError``. Semolina passes it
through unchanged, as the previous section describes, which is why it has a clause of its
own.

Give each concurrent task its own ``aexecute()``
------------------------------------------------

``aexecute()`` uses the identical error path, so the driver exceptions above are what an
``async def`` handler sees too. It has one addition. An ADBC connection permits serialized
access but not concurrent access, so sharing one cursor or one connection between
concurrently running tasks raises ``ConnectionBusyError`` from adbc-poolhouse. Semolina lets
that exception through unwrapped, because its own message already names the fix: check out a
separate connection per task.

Each ``aexecute()`` call checks out its own connection, so a handler that awaits its own query
never reaches this. You get there by holding one cursor and driving it from two tasks. The
remedy is a separate ``aexecute()`` call per task, not a lock around the shared one. The pool
rejects concurrent access on purpose rather than serializing it: a lock would let two
tasks' statements interleave inside one transaction, which the driver accepts and which
quietly corrupts the results.

Query a different engine per endpoint
-------------------------------------

An endpoint that has to reach a different warehouse, or the same warehouse on a different
compute size, names the engine with ``.using()``:

.. code-block:: python

   @app.get("/api/sales")
   def get_sales():
       with (
           Sales.query()
           .metrics(Sales.revenue)
           .dimensions(Sales.country)
           .using("default")
           .execute()
       ) as cursor:
           return [dict(row) for row in cursor.fetchall_rows()]


   @app.get("/api/reports/sales")
   def get_sales_report():
       with (
           Sales.query()
           .metrics(Sales.revenue, Sales.cost)
           .dimensions(Sales.country, Sales.region)
           .using("reports")
           .execute()
       ) as cursor:
           return [dict(row) for row in cursor.fetchall_rows()]

The name is resolved at ``.execute()`` time, not when the query is built, so a query defined
at import time can name an engine the lifespan handler registers later. See
:ref:`howto-connection-pools` for registering the second engine.

On the async path ``.using(name)`` resolves against the async registry, which is a separate
store from the synchronous one. A name registered with
:py:func:`~semolina.registry.register` is invisible to ``aexecute()``, which raises
:py:exc:`~semolina.exceptions.SemolinaEngineNotRegisteredError` rather than quietly running
your query somewhere else, so register the engines
your async endpoints reach with :py:func:`~semolina.registry.register_async_engine`.

See also
--------

- :ref:`tutorial-dashboard-api` -- the endpoint this guide hardens, built step by step
- :ref:`howto-connection-pools` -- pool sizing, engine lifecycle, and multiple named engines
- :ref:`howto-streaming` -- stream rows one batch at a time, and cancel an ``async for``
  mid-iteration
- :ref:`tutorial-installation` -- install the ``semolina[async]`` extra
- :ref:`howto-queries` -- full query builder API
- :ref:`howto-typed-results` -- ``.into()``, and why a DTO beats ``dict(row)`` in a response
  body
- :ref:`howto-dto-codegen` -- generate the DTO from the query with ``semolina codegen-dto``
- :ref:`the untyped route <howto-serialization>` -- ``dict(row)``, and the ``Decimal``
  precision FastAPI silently drops when you return one
- :ref:`howto-filtering` -- field operators, boolean composition, and how a query-parameter
  value reaches the warehouse
- :ref:`explanation-duckdb-vs-warehouse` -- why driver exception classes differ per
  backend, and which of them have been measured
