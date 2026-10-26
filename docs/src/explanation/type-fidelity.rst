.. _explanation-type-fidelity:

Warehouse types and what lands in your row
==========================================

A semantic view defines a metric as an aggregation expression, not as a column
with a declared type. ``SUM(o.order_total)`` has whatever type the warehouse
works out when it plans the query, and that type depends on the aggregate as
much as on the column underneath it.

Two consequences, and they are what this page is about. The type your warehouse
reports for a metric before you query it is not always the type you get back.
And a money column arrives in Python as a :py:class:`decimal.Decimal` rather
than a ``float``.

What the warehouse tells you before you query
---------------------------------------------

Semolina's codegen reads field types out of your warehouse's catalogue. How much
there is to read varies.

**Snowflake** answers ``SHOW COLUMNS IN VIEW`` with a resolved type per metric
and dimension. Snowflake infers the metric's type server-side and reports the
result, so there is a real answer waiting for you.

**Databricks** answers ``DESCRIBE TABLE EXTENDED ... AS JSON`` with a type per
column, measures included, alongside the metric view's own YAML definition.

**DuckDB** answers ``DESCRIBE SEMANTIC VIEW`` with a type per field from
``semantic_views`` v0.14.0. The extension works it out when the command runs,
as the type the field's column has in a ``semantic_view()`` query. Older
releases return an empty type, so for them Semolina runs
``DESCRIBE SELECT * FROM semantic_view(...)`` instead, which is a query-shaped
question rather than a catalogue lookup. It does the same for any field the
extension could not type.

Older DuckDB extensions just make the general case visible. Definition-time type information
for a metric is a convenience the warehouse may offer, and it is derived from
the underlying columns; the authoritative answer exists only once a query is
planned.

Where the catalogue and the result disagree
-------------------------------------------

Measured against DuckDB 2.0.0 with the ``semantic_views`` extension, over a
table whose ``order_total`` column is ``DECIMAL(10, 2)`` and whose
``order_count`` column is ``INTEGER``:

- ``SUM(order_total)`` comes back at precision 38, not 10. ``MAX(order_total)``
  over the same column stays at ``DECIMAL(10, 2)``. Accumulating aggregates
  widen; picking one existing value out of a column does not.
- ``AVG(order_count)`` comes back as a double, so the row holds a ``float`` for
  a column that only ever held whole numbers. ``SUM(order_count)`` over the same
  column comes back as DuckDB's 128-bit ``HUGEINT``, which arrives as a ``Decimal``.
- ``COUNT(order_total)`` comes back as a 64-bit integer while
  ``MIN(order_count)`` comes back as a 32-bit one.

The width difference in the third case is invisible in a :py:class:`~semolina.results.Row`,
where both arrive as a Python ``int``. It surfaces if you take results as Arrow
with ``fetch_arrow_table()``, or hand the result schema to anything that cares
about integer width. The first two cases show up everywhere: a summed decimal
column is a ``Decimal``, and an average is a ``float`` no matter what you
averaged.

Rules for this are published unevenly. Databricks documents its
`sum <https://docs.databricks.com/aws/en/sql/language-manual/functions/sum>`_ and
`avg <https://docs.databricks.com/aws/en/sql/language-manual/functions/avg>`_
return types, including the precision it widens a decimal to. Snowflake
publishes no precision rule for ``SUM`` and no return type for ``AVG`` at all,
so on Snowflake the exact widths are something to measure against your own view
rather than something to look up.

Why money comes back as a Decimal
---------------------------------

Your warehouse holds a decimal column, the ADBC driver hands it over as Arrow
``decimal128``, and PyArrow turns that into a :py:class:`decimal.Decimal`.
Semolina neither rounds it nor casts it to ``float`` on the way through, so what
you get is the value the warehouse computed, at full precision.

.. code-block:: python

   cursor = (
       Orders.query().metrics(Orders.order_total).execute()
   )
   row = cursor.fetchone_row()

   print(row.order_total)
   # Decimal('30.75')

   # Decimal and float raise TypeError if you mix them in arithmetic, and
   # json.dumps has no encoder for Decimal. Convert at the boundary.
   payload = {"order_total": float(row.order_total)}

Two boundaries need that explicit ``float()``: charting libraries, which want
native floats, and hand-rolled JSON serialization. Pydantic v2 does not, since
it handles ``Decimal`` fields directly, and neither does
``to_pandas()``, which gives you an ``object``-dtype column holding ``Decimal``
values rather than silently converting to ``float64``.

On Databricks this needs ADBC driver 0.2.0 or later; see
`Databricks decimals and intervals`_.

The alternative would be to ask the driver for floats. Snowflake's ADBC driver
has a low-precision mode that hands ``NUMBER`` columns over as 64-bit ints and
floats instead of decimals. It is not the default, and Semolina does not switch
it on, for the reason that decides every question on this page: it would round
your revenue to whatever a double can hold, so the annotation would stop
describing the value. Turning it on would also make Snowflake the one backend
where money does not arrive as a ``Decimal``.

What a DTO's annotations are checked against
---------------------------------------------

Converting a result into Pydantic objects with ``into()`` raises the same
question one layer up: your DTO declares a type per field, and the warehouse
decided a type per column. Semolina compares the two before any row moves,
reading the result's Arrow schema and nothing else, and reports every field
that disagrees in one error rather than one per run.

The comparison costs nothing worth counting. The schema is already in memory
by the time the cursor exists, so the check fetches no rows, issues no query
and creates no reader. It also has no values to look at, which is why its
error message can name field names, column names and types but never data.

Two silences in that check are deliberate. It says nothing about nullability,
because the Arrow ``nullable`` flag was measured ``True`` for every field on
every query, ``COUNT`` included, so it distinguishes nothing. And it stays
quiet about any annotation it cannot reduce to a class or a union of classes,
including :py:data:`typing.Any` and a recursive alias like
``pydantic.JsonValue``. A missed mismatch costs one wrong value in a field;
a false positive costs a call site that worked yesterday, and the second is
worse.

The check exists because the default conversion path cannot catch the case this
page has been about. It builds instances through ``model_construct`` and
performs no per-value validation by design, so a ``Decimal`` in a field you
declared ``float`` would sit there quietly. That same instance then serializes
as a ``Decimal`` through ``model_dump()`` and as a rounded float through
``model_dump_json()``, which is a worse outcome than either type on its
own. Comparing the declared type against the result schema is the only place
that disagreement can be caught on that path, so that is where it is caught.

The validated path does not need it. ``validate=True`` runs Pydantic per value,
which converts the ``Decimal`` to a ``float`` and accepts the rounding. That is
a real answer rather than a silent wrong type, and it is one a person can
legitimately want, since a chart does not need the pence. Semolina therefore refuses
the narrowing only where nothing would perform it, and steps aside where
something will. What it will not do is round on the way through while you
believed you were getting a ``Decimal``.

See :ref:`howto-typed-results` for writing the DTO, including the annotation
each backend's driver needs.

What can be NULL
----------------

A metric is a value computed over a group, so it goes NULL when there is a group
but nothing to aggregate in it. Three cases, and they behave differently:

A group whose metric inputs are all NULL gives you a row, with ``None`` for
``SUM``, ``AVG``, ``MIN``, and ``MAX``. Say a region has one order whose total
was never recorded: the region still appears in your results, with a null total.

``COUNT`` in that same group gives you ``0``, not ``None``. Counting nothing is
a real answer.

A filter that matches nothing gives you no rows at all. There is no group, so
there is nothing to be NULL. If you filter on a region that does not exist, you
get an empty result rather than a row of nulls.

So a metric annotated as non-optional is optimistic in the general case, and
that is why generated metric annotations admit ``None``. ``COUNT`` is the
exception that gets treated the same way.

Semolina could in principle sharpen that. All three warehouses expose the
aggregate expression behind a metric in their catalogue, so codegen could read
it and drop the ``None`` wherever it found a ``COUNT``. It does not, because
recognizing an aggregate from its expression text is a guess, and the two ways
of guessing wrong are not equally costly. Failing to spot a ``COUNT`` leaves you
with an ``is None`` check you did not need. Mistaking something else for one (``COUNT_IF``,
an alias wrapping the aggregate, a ``SUM(CASE WHEN ... THEN 1 ELSE 0 END)``) drops
the ``None`` from a column that can produce one, and nothing catches it until a null arrives in
production. One rule that is always safe is worth more than a sharper rule that
is usually right.

Asking the warehouse, or reading the catalogue
----------------------------------------------

Given the gap between the two, Semolina prefers the answer the warehouse gives
when it plans your query, and falls back to the catalogue when it cannot ask.
Both happen when you generate or check a model, never while your application is
running a query.

Asking is cheaper than it sounds. ADBC has a call that returns a query's result
schema without executing it, and on Snowflake that maps to the warehouse's
describe-only mode: a metadata round trip, no compute. On DuckDB it is answered
in-process.

Two things get in the way. Snowflake refuses the describe-only call when the
query carries bound parameters, which is the shape a ``.where()`` produces on
that backend, so a filtered query cannot be described. And the Databricks driver
does not implement the call at all, so the only route there is to run the query
wrapped in something that returns no rows, which does reach the warehouse and
does cost you a wake-up on a serverless one.

The catalogue is what codegen uses when neither route is open, including when
you generate a model with no live connection. It is a good estimate. It is
not the same source as the query itself.

What a generated annotation names
---------------------------------

The annotation ``semolina codegen`` writes describes the value you will hold,
not the type the warehouse declares. A decimal column annotates
:py:class:`decimal.Decimal` on all three backends, because all three drivers
deliver ``decimal128``. Where a driver delivers something else, the annotation
follows the driver: Databricks intervals annotate ``str``, for the reason given
under :ref:`explanation-type-fidelity-databricks-decimal`. Metric fields annotate
``T | None``, for the reason given under `What can be NULL`_.

Where the annotation does not name the warehouse's own type, codegen keeps that
type as a comment above the field, so a precision and scale are not lost the
moment the annotation says only ``decimal.Decimal``:

.. code-block:: python

   # DECIMAL(10,2)
   max_order_value = Metric[decimal.Decimal | None]()

Several DuckDB columns annotate ``str`` on the same principle. A ``UUID``
arrives as text rather than as a :py:class:`uuid.UUID`, a ``JSON`` column as
unparsed JSON text, and an ``ENUM`` as the member's label. Writing
:py:class:`uuid.UUID` there would name the type you expected rather than the
object in your row, which is the gap this page exists to close.

One annotation is an over-approximation rather than an exact description. A
DuckDB ``TIMESTAMP_NS`` column annotates :py:class:`datetime.datetime`, and what
lands in your row depends on whether pandas can be imported. When it can, the
value is a ``pandas.Timestamp``, a subclass of :py:class:`datetime.datetime`
that keeps the nanoseconds. When it cannot, PyArrow truncates the value to
microsecond resolution, and raises :py:exc:`ValueError` outright on a value
carrying sub-microsecond precision. Semolina does not depend on pandas. It
arrives with the ``pandas`` extra, which the ``all`` extra includes, so whether
you have it is a property of your environment rather than of Semolina.

.. _explanation-type-fidelity-databricks-decimal:

Databricks decimals and intervals
---------------------------------

Databricks decimals follow the same rule as the other two backends, as long as
the ADBC driver is version 0.2.0 or later. Releases up to 0.1.3 hand every
decimal over as an Arrow string, at every precision and scale, so a money column
held ``'30.75'`` rather than ``Decimal('30.75')`` and Semolina annotated it
``str``. Version 0.2.0 turns the driver's ``useArrowNativeDecimal`` option on by
default, so a decimal arrives as ``decimal128`` and annotates
:py:class:`decimal.Decimal`, as it does on Snowflake and DuckDB. That is why
Semolina needs 0.2.0 at least; :ref:`howto-backends-databricks` shows how to
install it. Both behaviours were measured against a live workspace: strings on
0.1.2 and 0.1.3, and ``decimal128`` on 0.2.0 for columns, scale-0 columns,
literals and ``SUM``/``AVG`` measures.

If you set ``useArrowNativeDecimal=false`` on the connection, the driver goes
back to strings and a generated ``decimal.Decimal`` annotation no longer matches
what arrives. The schema check in ``.into()`` refuses that DTO rather than
coercing it. To keep the option off and still get ``Decimal`` objects, ask for
them with ``validate=True``:

.. code-block:: python

   import decimal

   import pydantic


   class RevenueDTO(pydantic.BaseModel):
       region: str
       revenue: decimal.Decimal


   rows = cursor.into(RevenueDTO, validate=True)
   # [RevenueDTO(region='US', revenue=Decimal('12345678901234567890.99')), ...]

Pydantic parses the digit string exactly, so nothing is lost. ``validate=`` is a
per-*call* flag, though: it switches the structural type check off for every
field of that DTO, not only the decimal. Annotate the rest of the DTO exactly,
because on that call nothing is left to tell you if you have not.

Both **interval** families arrive as strings on every driver version:
``'3 04:05:06.789000000'`` for a ``DAY TO SECOND``, ``'2-6'`` for a
``YEAR TO MONTH``. This is the wire format rather than a driver choice, and the
Databricks SQL connector returns the same strings, so an interval column
annotates ``str``.

Why a fresh model can fail its own check
----------------------------------------

``semolina codegen`` builds a model from the catalogue. ``semolina codegen
--check`` resolves annotations from the result schema instead, dropping back to
the catalogue only when it cannot probe and labelling every row it did that for.
The two commands read the two sources this page has been comparing all along, so
a ``--check`` run straight after a ``codegen`` run can report drift on a model
that was correct by the route which generated it.

A DuckDB ``INTERVAL`` fact is the clearest case. The catalogue route
annotates it ``datetime.timedelta``. The result-schema route resolves no type
for it at all, because the value arrives as a ``pyarrow.MonthDayNano`` and no
type in the standard library describes one. ``--check`` calls that drift, and
the probe is the half telling the truth.

So a reported drift is a question about which source to believe, rather than a
fault in either command. See :ref:`howto-codegen-check` for the mechanics of
running the check and reading its per-field report.

See also
--------

- :ref:`explanation-semantic-views` -- what a semantic view is, and how the three
  warehouses implement them
- :ref:`howto-codegen` -- generate models from your warehouse, and edit the
  annotations codegen produces
- :ref:`howto-codegen-check` -- run ``semolina codegen --check`` against a
  committed model and read its report
- :ref:`howto-models` -- field types and model configuration
- :ref:`howto-arrow-output` -- fetch results as an Arrow table, where the exact
  result schema is visible
- :ref:`howto-typed-results` -- convert a result into Pydantic instances, and
  what the schema check refuses
- :ref:`howto-filtering` -- filter a query with ``.where()``
- `Databricks: sum <https://docs.databricks.com/aws/en/sql/language-manual/functions/sum>`_
  -- the documented decimal widening rule for sums
