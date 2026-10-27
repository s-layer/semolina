.. _howto-typed-results:

How to map results into Pydantic models with ``.into()``
=========================================================

:ref:`tutorial-dashboard-api` hands one query's rows to one Pydantic DTO and gets
on with serving the endpoint. This guide is the rest of that mechanism.

A dashboard backend usually wants objects, not rows.
:py:meth:`~semolina.cursor.SemolinaCursor.into` converts a query result straight
into Pydantic instances, so your response layer gets something it can serialize
and your editor gets something it can autocomplete.

`The untyped route: dictionaries and JSON`_ at the end covers the alternative.

This guide assumes you already have a :py:class:`~semolina.models.SemanticView`
subclass and a registered engine. See :ref:`howto-queries` if you need
setup first.

Typed results need the ``arrowmodel`` extra:

.. code-block:: bash

   pip install "semolina[arrowmodel]"

That one extra is enough. It brings PyArrow with it, which both the
schema check and the whole-result form read through.

The snippets reuse this model and this DTO:

.. code-block:: python

   from semolina import SemanticView, Metric, Dimension


   class Sales(SemanticView, view="sales"):
       revenue = Metric()
       country = Dimension()

.. code-block:: python

   import decimal

   import pydantic


   class RevenueByCountry(pydantic.BaseModel):
       country: str
       revenue: decimal.Decimal

Any ``pydantic.BaseModel`` subclass works. arrowmodel ships an
``ArrowModel`` base class and you can use it, but ``.into()`` does not ask
for it and you gain nothing here by inheriting from it.

``revenue`` is annotated :py:class:`decimal.Decimal` because a money
column, declared ``DECIMAL`` in the warehouse, arrives as one on every
backend. If the metric in your view
sums an integer column, annotate it ``int`` instead. See
:ref:`explanation-type-fidelity` for why the warehouse decides this and
you do not.

.. note:: Databricks needs driver 0.2.0 or later

   ADBC driver releases up to 0.1.3 hand decimal columns over as Arrow
   **strings**, and the schema check refuses a ``decimal.Decimal`` field
   fed one. See :ref:`explanation-type-fidelity-databricks-decimal`.

Convert the whole result
-------------------------

Call :py:meth:`~semolina.cursor.SemolinaCursor.into` with the DTO class:

.. code-block:: python

   with Sales.query().metrics(Sales.revenue).dimensions(
       Sales.country
   ).execute() as cursor:
       rows = cursor.into(RevenueByCountry)

   # [RevenueByCountry(country='US',
   #                   revenue=Decimal('43.25')), ...]

You get a plain ``list``, one instance per result row, empty if the query
matched nothing. Columns are matched to fields by name. Result columns
your DTO does not declare are dropped, so one DTO can serve several
queries and a query can gain a column without breaking the DTOs already
reading it.

Because these are Pydantic models, a FastAPI handler can return the list
directly and let the framework serialize it:

.. code-block:: python

   @app.get("/revenue")
   def revenue() -> list[RevenueByCountry]:
       with Sales.query().metrics(Sales.revenue).dimensions(
           Sales.country
       ).execute() as cursor:
           return cursor.into(RevenueByCountry)

On the default path, conversion happens in Rust, over the Arrow buffers
the driver already produced, with no intermediate Python dictionaries.
``validate=True`` gives that up and runs Pydantic per value in Python;
see `Choose exact types or coercion`_ for when the trade is worth making.
Either way the whole result is read into memory first, the same as
:py:meth:`~semolina.cursor.SemolinaCursor.fetch_arrow_table` and the other
:ref:`whole-result fetch methods <howto-arrow-output>`.

Stream instances one at a time
-------------------------------

When the result is larger than you want to hold,
:py:meth:`~semolina.cursor.SemolinaCursor.iter_into` gives you the same
instances lazily:

.. code-block:: python

   with Sales.query().metrics(Sales.revenue).dimensions(
       Sales.country
   ).execute() as cursor:
       for dto in cursor.iter_into(RevenueByCountry):
           handle(dto)

Instances arrive one at a time, but conversion still happens a whole
Arrow batch at a time, which is where the speed comes from. Consuming one
instance pulls exactly one batch from the underlying reader, so peak
memory is bounded by a batch rather than by the result.

Two behaviours to know before you use it.

A bad DTO raises on the ``iter_into(...)`` line itself, not on the first
loop pass. The method runs the schema check and then returns a generator,
rather than being a generator function whose body waits for the first
``next()``. That is the opposite of what a returned iterator usually does,
and it is deliberate: the traceback points at the line that named the
wrong type.

The iterator is the cursor's one way of reading its result. Any other read on the
same cursor, :py:meth:`~semolina.cursor.SemolinaCursor.fetch_record_batch` included,
raises :py:class:`~semolina.exceptions.SemolinaResultConsumedError`; run the query again
if you need the rows a second way. Keep the cursor open until the loop ends; the
``with`` block does that for you.
:ref:`howto-streaming` covers the other batched entry points alongside this one.

Do the same from an async handler
----------------------------------

Both methods have async twins on
:py:class:`~semolina.acursor.AsyncSemolinaCursor`. Execute with ``aexecute()``,
then await ``into()``:

.. code-block:: python

   async with await Sales.query().metrics(
       Sales.revenue
   ).dimensions(Sales.country).aexecute() as cursor:
       rows = await cursor.into(RevenueByCountry)

The streaming form is not awaited. ``iter_into()`` is a plain method that
hands back an async iterator, so it goes straight into ``async for``:

.. code-block:: python

   async with await Sales.query().metrics(
       Sales.revenue
   ).dimensions(Sales.country).aexecute() as cursor:
       async for dto in cursor.iter_into(RevenueByCountry):
           await handle(dto)

Writing ``await cursor.iter_into(...)`` is the mistake to expect here.
The schema check has already run by the time you hold the iterator, for
the same reason it has on the synchronous cursor.

Each batch is pulled on a worker thread, so the event loop stays free
while the warehouse computes the next one.

If the loop may end early, close the iterator with
:py:func:`contextlib.aclosing`:

.. code-block:: python

   import contextlib

   async with await Sales.query().metrics(
       Sales.revenue
   ).dimensions(Sales.country).aexecute() as cursor:
       stream = cursor.iter_into(RevenueByCountry)
       async with contextlib.aclosing(stream):
           async for dto in stream:
               if await handle(dto):
                   break

A ``break``, a ``return`` or an exception leaves an async generator
suspended rather than finished, and nothing runs its cleanup until the
garbage collector gets to it. Under Trio that is a ``ResourceWarning``
naming ``_aiter_into_impl``; under asyncio it is silent until the loop
shuts down. ``aclosing`` closes the generator at the end of the block
either way. A loop you always run to completion needs none of this.

.. warning::

   Close an async cursor with ``async with`` or ``await cursor.aclose()``.
   Unlike :py:class:`~semolina.cursor.SemolinaCursor`, the async cursor has no
   finalizer that can reclaim a forgotten connection, so one that is
   never closed holds its pooled connection permanently. See
   :ref:`howto-web-api-async-cursor-close`.

.. _howto-result-column-names:

How result columns are named
----------------------------

Semolina aliases every selected column to its Python field name in the
SQL it sends, so a result column is named after the field you declared on
the model, whichever backend runs the query:

.. list-table::
   :header-rows: 1

   * - Warehouse
     - ``revenue`` is selected as
     - and arrives as
   * - Snowflake
     - ``AGG("REVENUE") AS "revenue"``
     - ``revenue``
   * - Databricks
     - ``MEASURE(`revenue`) AS `revenue```
     - ``revenue``
   * - DuckDB
     - ``"revenue" AS "revenue"``
     - ``revenue``

A DTO whose field names match the model's field names needs no aliases,
and the same class reads the result on all three, the decimal included:

.. code-block:: python

   class RevenueByCountry(pydantic.BaseModel):
       country: str
       revenue: decimal.Decimal

Other column types can still arrive differently by backend: a Databricks interval or a
DuckDB ``UUID`` arrives as ``str``. See :ref:`explanation-type-fidelity` for the full list.

.. tip:: You do not have to write these by hand

   ``semolina codegen-dto`` probes a query and prints a DTO annotated
   with the types the backend reported. See :ref:`howto-dto-codegen`.

Matching is exact string equality, with no case folding, so a DTO field
called ``Revenue`` does not find ``revenue``. When the name you want in
your DTO differs from the model's field name, say because your API
contract calls the figure ``total``, declare the column it reads with
``Field(validation_alias=...)``:

.. code-block:: python

   class RevenueByCountry(pydantic.BaseModel):
       country: str
       total: decimal.Decimal = pydantic.Field(
           validation_alias="revenue"
       )

A field the result cannot fill is loud: the schema check names every
field it could not place and prints the column names the result
carried.

Which alias forms are supported
--------------------------------

A field resolves to one column key, in this order:

#. ``Field(validation_alias="...")``
#. ``Field(alias="...")``
#. the field name

Set ``populate_by_name`` (or ``validate_by_name``) on the model and the
field name is accepted *as well as* the alias, so the DTO reads a result
column named either way:

.. code-block:: python

   class RevenueByCountry(pydantic.BaseModel):
       model_config = pydantic.ConfigDict(
           populate_by_name=True
       )

       country: str
       total: decimal.Decimal = pydantic.Field(alias="revenue")

Should a result carry both spellings, the alias is the one that is read.

Pydantic offers three further alias features that Semolina refuses,
because the Rust converter underneath does not implement them:

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Refused
     - Write instead
   * - ``validation_alias=AliasChoices(...)``
     - One plain string alias. Where the second spelling you wanted was
       the field name, ``populate_by_name`` covers it.
   * - ``validation_alias=AliasPath(...)``
     - One plain string alias. A result column is flat, so there is no
       path to address.
   * - ``ConfigDict(alias_generator=...)``
     - An explicit alias on each field.

Each of the three raises
:py:class:`~semolina.exceptions.SemolinaSchemaMismatchError` naming the
construct, at the ``into()`` or ``iter_into()`` call. The generator is
the one to watch for: Pydantic writes the generated spelling onto every
field, so the DTO looks correct right up to the point it is used.

Choose exact types or coercion
-------------------------------

Both methods take a keyword-only ``validate``, defaulting to ``False``.
It picks between two behaviours:

.. code-block:: python

   rows = cursor.into(RevenueByCountry)  # types must match
   rows = cursor.into(
       RevenueByCountry, validate=True
   )  # types are coerced

Keep the default: annotations must match
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

With ``validate=False``, your annotations must match the columns.
Instances are built through ``model_construct``, which converts nothing
and runs none of your validators. Because nothing converts, an
annotation that disagreed with its column would leave a wrong-typed
value sitting in the field. Semolina compares the DTO against the
result's Arrow schema first and refuses the call instead, naming every
offending field at once. The cost is one schema comparison, no matter
how many rows come back.

Pass ``validate=True``: convert to your annotations
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

With ``validate=True``, your annotations are what you want, so Semolina
converts to them. Each row goes through Pydantic's full pipeline at roughly two to
five times the cost. It converts where the conversion is legal and
raises ``ValidationError`` where it is not:

.. list-table::
   :header-rows: 1

   * - Column arrives as
     - Field declares
     - ``validate=True``
   * - ``decimal128``
     - ``float``
     - converts, precision lost
   * - ``int64``
     - ``float``
     - converts
   * - ``float64``
     - ``Decimal``
     - converts
   * - ``decimal128``
     - ``int``
     - converts a whole number, ``ValidationError`` for a fraction
   * - ``string``
     - ``int``
     - ``ValidationError`` unless the text parses

The structural type check is skipped on this path, because Pydantic is
already deciding and refusing first would block conversions that work.

A ``decimal128`` with scale 0 only ever holds whole numbers, so it converts to
``int`` row by row. On DuckDB that is a ``SUM`` over an integer column, which
arrives as a 128-bit ``HUGEINT``: declare the field ``int`` and pass
``validate=True``, or cast the sum to ``BIGINT`` in the view and keep the fast
path. A ``decimal128`` with a fractional value raises ``ValidationError`` on the
row that holds it.

So: leave ``validate`` alone and Semolina holds you to the warehouse's
types. Pass ``validate=True`` when you have deliberately declared
something narrower and want the values converted to it.

.. warning::

   A money column declared ``float`` converts under ``validate=True``,
   and the precision goes with it:
   ``12345678901234567890.99`` becomes ``1.2345678901234567e+19``. That
   is a reasonable thing to ask for in a chart and a bad thing to ask
   for in a ledger. Declare :py:class:`decimal.Decimal` and let the
   default path hold you to it unless you specifically want the
   narrowing.

One asymmetry to know: ``validate=True`` is more permissive about types
but *stricter* about nulls. The structural check says nothing about
nullability (see :ref:`explanation-type-fidelity` for why the Arrow
flag carries no information), so a NULL can land in a
non-optional field on the fast path. Pydantic rejects it. If you turn
validation on and start seeing errors about ``None``, annotate the field
``| None`` rather than turning validation back off.

Annotate a VARIANT column
--------------------------

A semi-structured column, a Snowflake ``VARIANT`` for instance, has no
fixed shape to annotate. Use ``pydantic.JsonValue``:

.. code-block:: python

   import pydantic


   class EventDTO(pydantic.BaseModel):
       payload: pydantic.JsonValue

.. warning::

   Not :py:obj:`semolina.JsonValue <semolina.types.JsonValue>`. Semolina exports a name
   spelled the same way, for use in generated
   :py:class:`~semolina.models.SemanticView` models,
   where it is only ever read as text. Used as a DTO annotation it sends
   Pydantic into a ``RecursionError`` while your class is still being
   created, with a traceback containing no Semolina frames at all.

The schema check records no verdict for a field annotated this way. It
compares classes, and ``pydantic.JsonValue`` is a recursive type alias
rather than a class, so the field passes through unexamined.

When the DTO does not match
----------------------------

A mismatch raises :py:class:`~semolina.exceptions.SemolinaSchemaMismatchError` and
lists every field at fault at once, rather than making you fix one and
re-run:

.. code-block:: text

   RevenueByCountry does not match the result schema (2 mismatched fields):
     revenue (column 'revenue'): declared float, but the column is decimal128(38, 2) (arrives as decimal.Decimal)
     currency (column 'currency'): declared str, but the result has no such column (it carries ['revenue', 'country'])
   Annotate each field with the type its column arrives as, or use Field(validation_alias=...) if the result spells the column differently.
   If a narrowing is deliberate, pass validate=True: Pydantic then converts each value, coercing where it legally can (decimal -> float) and raising ValidationError where it cannot (decimal -> int).

The first mismatch is the type case. The second is a declared field the
result has no column for, which is an error only while the field is
required: give it a default (``= None`` counts) and it becomes optional
in the result.

Only the type half is skipped by ``validate=True``. A required field with
no matching column is still refused on either path, because no
conversion invents a column.

One refusal looks like a bug until you see the rule behind it. An
integer column does not satisfy a field annotated ``float``:

.. code-block:: text

   R2 does not match the result schema (1 mismatched field):
     revenue (column 'revenue'): declared float, but the column is int64 (arrives as int)

Python has no numeric tower to lean on here, and the fast path really
would leave an ``int`` sitting in a field you declared ``float``. It is
the same rule that catches the ``Decimal`` case, applied consistently:
on the default path the annotation has to name the type the column
arrives as. If you wanted the float, ``validate=True`` converts it.
Where you do not want a verdict at all, ``typing.Any`` and ``object``
opt out.

A timestamp column is refused for a field annotated ``datetime.date``,
even though ``datetime`` is a subclass of ``date``:

.. code-block:: text

   Orders does not match the result schema (1 mismatched field):
     ordered_on (column 'ordered_on'): declared datetime.date, but the column is timestamp[us] (arrives as datetime.datetime)

The fast path converts nothing, so the field would keep the time of day,
and ``order.ordered_on == date(2024, 1, 2)`` would be ``False`` for the
day it names. Annotate the field ``datetime.datetime``, or expose a
``DATE`` dimension in the semantic view if you only want the day.
``validate=True`` is no way round it here: Pydantic refuses a timestamp
with a non-zero time for a ``date`` field too.

Annotate a dimension with an enum
----------------------------------

Enums are where this rule surprises people, because a status or category
column is a natural thing to want as one. Declaring it is refused on the
default path:

.. code-block:: python

   import enum

   import pydantic


   class Country(str, enum.Enum):
       US = "US"
       GB = "GB"


   class RevenueByCountry(pydantic.BaseModel):
       country: Country
       revenue: decimal.Decimal

.. code-block:: text

   RevenueByCountry does not match the result schema (1 mismatched field):
     country (column 'country'): declared myapp.Country, but the column is string (arrives as str)

The refusal is accurate rather than pedantic. ``model_construct``
converts nothing, so the field would hold the plain string ``'US'``.
With a ``str`` mixin that string compares equal to ``Country.US`` and
still fails ``isinstance(value, Country)``, which is the kind of bug
that surfaces three layers away from the query. Without the mixin it
does not even compare equal.

Pass ``validate=True`` and you get the members you asked for, mixin or
not:

.. code-block:: python

   rows = cursor.into(RevenueByCountry, validate=True)
   # [RevenueByCountry(country=<Country.US: 'US'>,
   #                   revenue=Decimal('43.25')), ...]

Pydantic raises ``ValidationError`` for a value outside the enum, so a
new country appearing in the warehouse becomes a loud failure rather
than a silent one. ``validate=`` covers the whole call, though:
switching it on for ``country`` also stops the type check watching
``revenue``.

.. _howto-serialization:

The untyped route: dictionaries and JSON
-----------------------------------------

Not every response needs a class. A :py:class:`~semolina.results.Row` is already a
mapping, so ``dict(row)`` gets you something a JSON encoder can work with, and for
an internal endpoint, a debugging script, or a shape that changes per request that
is the shortest path there is.

It costs you the two things ``.into()`` was handling quietly. Renaming a column for
your clients is now done at every call site rather than once on a class, and a
``Decimal`` metric needs an encoder before ``json.dumps`` will touch it.

Convert a row to a dictionary
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:py:class:`~semolina.results.Row` implements the mapping protocol (``__iter__``
yields keys, ``__getitem__`` returns values), so ``dict()`` converts it directly:

.. code-block:: python

   with Sales.query().metrics(Sales.revenue).dimensions(
       Sales.country
   ).execute() as cursor:
       row = cursor.fetchone_row()
       data = dict(row)

   # {'revenue': Decimal('1000.50'), 'country': 'US'}

``.keys()``, ``.values()`` and ``.items()`` are there as well, for when you want the
parts rather than the whole:

.. code-block:: python

   row.keys()  # dict_keys(['revenue', 'country'])
   row.values()  # dict_values([Decimal('1000.50'), 'US'])
   row.items()  # dict_items([('revenue', Decimal('1000.50')), ('country', 'US')])

Those keys are the model's field names on every backend. The order is the order of the
``SELECT`` list Semolina builds, metrics first and then dimensions, whichever order you
called ``.metrics()`` and ``.dimensions()`` in. When your clients expect different names,
`Map the field names your API exposes`_ below shows how to rename them.

Encode the types ``json.dumps`` refuses
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``json.dumps(dict(row))`` works only while every value happens to be a JSON-native
type. It is not safe for money. A metric over a ``DECIMAL`` column arrives as a
:py:class:`decimal.Decimal`, which the standard encoder refuses:

.. code-block:: python

   json.dumps(dict(row))
   # TypeError: Object of type Decimal is not JSON serializable

Give ``json.dumps()`` a ``default=`` encoder so it knows what to do with the types
the warehouse returns:

.. code-block:: python

   import datetime
   import decimal
   import json


   def encode(value):
       """Encode warehouse types the standard JSON encoder does not handle."""
       if isinstance(value, decimal.Decimal):
           return str(value)
       if isinstance(
           value, (datetime.date, datetime.datetime)
       ):
           return value.isoformat()
       raise TypeError(
           f"Object of type {type(value).__name__} is not JSON serializable"
       )


   json.dumps(dict(row), default=encode)
   # '{"revenue": "1000.50", "country": "US"}'

``str(value)`` keeps every digit, at the cost of sending the number as a JSON
string. ``float(value)`` gives you a JSON number instead and loses precision: a
value that needs more than about 15 significant digits will not survive the round
trip. Choose per field, not globally. A chart axis can take the float; a ledger
total cannot.

.. warning:: FastAPI makes that choice for you, and it chooses ``float``

   Return a list of dictionaries from a path operation and FastAPI runs
   ``jsonable_encoder`` over it, which encodes a ``Decimal`` as a float rather than
   raising. ``Decimal('12345678901234567890.99')`` reaches the client as
   ``1.2345678901234567e+19``, with nothing logged and no exception anywhere.
   Construct a ``JSONResponse`` yourself and you get the ``TypeError`` above
   instead, because that class calls ``json.dumps`` directly. Measured against
   FastAPI 0.141.

   Returning the instances from ``.into()`` sidesteps the question. Pydantic
   serializes a ``Decimal`` as a JSON string and keeps every digit.

:ref:`explanation-type-fidelity` covers which columns arrive as a ``Decimal``, and
why Semolina does not convert them for you.

Serialize every row
~~~~~~~~~~~~~~~~~~~~

:py:meth:`~semolina.cursor.SemolinaCursor.fetchall_rows` hands back the whole result
as ``Row`` objects, and a list comprehension turns them into the shape a JSON
response body wants:

.. code-block:: python

   with Sales.query().metrics(Sales.revenue).dimensions(
       Sales.country
   ).execute() as cursor:
       data = [dict(row) for row in cursor.fetchall_rows()]

   # [{'revenue': Decimal('1000.50'), 'country': 'US'},
   #  {'revenue': Decimal('2000.00'), 'country': 'CA'}]

Batch large results with ``fetchmany_rows``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:py:meth:`~semolina.cursor.SemolinaCursor.fetchmany_rows` takes a batch size and
returns up to that many rows, so a result that will not fit in memory can still be
sent as newline-delimited JSON or server-sent events:

.. code-block:: python

   with Sales.query().metrics(Sales.revenue).dimensions(
       Sales.country
   ).execute() as cursor:
       while batch := cursor.fetchmany_rows(100):
           send([dict(row) for row in batch])

The loop ends on the first empty batch. Once the result is drained, every DBAPI fetch
returns an empty result (``[]``, or ``None`` from ``fetchone_row()``) rather than
asking the driver again, so the loop is the same on every backend.

This is the untyped counterpart of ``iter_into()``. The same one-read-per-cursor rule
applies: once ``fetchmany_rows()`` has started, any read other than the DBAPI fetches
raises :py:class:`~semolina.exceptions.SemolinaResultConsumedError`. See
:ref:`how to fetch results in bulk <howto-streaming>` for the other entry points.

Map the field names your API exposes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A dictionary comprehension over each :py:class:`~semolina.results.Row` picks the
columns you want and names them the way your clients will see them:

.. code-block:: python

   rows = cursor.fetchall_rows()
   data = [
       {
           "market": row.country,
           "total": str(row.revenue),
       }
       for row in rows
   ]

The keys on the left are your API's; the attributes on the right are the model's field
names, which every backend returns. This is what ``Field(validation_alias=...)`` does on
the typed route, written out by hand at each call site instead of once on a class.

Attribute access (``row.revenue``) and dict-style access (``row["revenue"]``) reach the
same column. Dict-style access is still the one to use for a key that collides with a
``Row`` method, such as a field called ``items`` or ``keys``.

See also
--------

- :ref:`tutorial-dashboard-api` -- the endpoint this guide extends, end to end
- :ref:`howto-dto-codegen` -- generate the DTO above from the query itself, types included
- :ref:`howto-web-api` -- async endpoints, async cursor closing, and the measured driver
  exception classes
- :ref:`howto-streaming` -- Arrow tables and dataframes, batched reads, and how they share
  one stream
- :ref:`explanation-type-fidelity` -- why money arrives as a ``Decimal``, and
  what the schema check promises
- :ref:`explanation-duckdb-vs-warehouse` -- which differences between DuckDB and your
  warehouse a DuckDB-only test suite cannot catch
- :ref:`tutorial-installation-result-extras` -- the ``arrowmodel`` extra and the other three
- :py:meth:`~semolina.cursor.SemolinaCursor.into` -- API reference
- :py:meth:`~semolina.cursor.SemolinaCursor.iter_into` -- API reference
- :py:class:`~semolina.acursor.AsyncSemolinaCursor` -- async cursor class reference
