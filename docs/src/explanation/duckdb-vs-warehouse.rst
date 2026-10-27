.. _explanation-duckdb-vs-warehouse:

Developing on DuckDB, deploying elsewhere
=========================================

DuckDB is the backend you can run on a laptop. It needs no credentials, it starts in
memory, no query costs you anything, and a test suite can hold a whole semantic view
in a fixture. So it is where most Semolina code gets written first, and
:ref:`howto-warehouse-testing` builds a test fixture around exactly that.

It is also the only backend many test suites ever run against, and that is where the gap
opens. Semolina makes the three backends agree on column names and on which rows a query
returns. It does not make them agree on the types your sample data never exercises or on
the exception classes a driver raises, and a DuckDB-only suite can't see either. This page
covers what still differs, what has been measured, and what has not.

The same query, one set of column names
---------------------------------------

Left to itself, a warehouse names each result column after the expression it computed.
Two independent decisions inside the warehouse would produce that name: it wraps a metric
in whatever function its dialect uses to evaluate one, and it folds unquoted identifiers to
its preferred case. Semolina takes the choice away by aliasing every selected column to its
Python field name. Take ``Sales.query().metrics(Sales.revenue).dimensions(Sales.country)``:

.. list-table::
   :header-rows: 1
   :widths: 20 20 20 40

   * - Backend
     - Metric wrapper
     - Identifier folding
     - Select list Semolina sends
   * - Snowflake
     - ``AGG()``
     - upper case
     - ``AGG("REVENUE") AS "revenue", "COUNTRY" AS "country"``
   * - Databricks
     - ``MEASURE()``
     - lower case
     - ``MEASURE(`revenue`) AS `revenue`, `country` AS `country```
   * - DuckDB
     - none
     - lower case
     - ``"revenue" AS "revenue", "country" AS "country"``

The aliases are quoted, so the warehouse keeps their case, and a
:py:class:`~semolina.results.Row` takes its keys from them: ``revenue`` and ``country`` on
all three. ``row.revenue`` and ``row["country"]`` read the same column whichever backend ran
the query, and so does a Pydantic DTO with plain field names.

So column names do not differ. The rest of this page is about what still does.

Fact queries return the same rows
---------------------------------

A query that selects facts and no metric is grouped on Snowflake and Databricks with
``GROUP BY ALL``, so it comes back with one row per distinct combination of values.
DuckDB's ``semantic_view()`` returns facts one row per source row, so Semolina sends
``SELECT DISTINCT`` for a DuckDB fact query, and the three backends return the same rows.

What still differs is what DuckDB will accept: ``semantic_view()`` can't select facts and
metrics together, so Semolina refuses that combination with ``ValueError`` on DuckDB and
sends it to Snowflake and Databricks as written.

Decimals are not a DuckDB difference
------------------------------------

Decimals look like another DuckDB difference and are not. A ``DECIMAL`` column arrives as a
:py:class:`decimal.Decimal` on DuckDB and on Snowflake -- both measured, the Snowflake
observation coming from a recorded ``decimal128(38, 0)`` result. On Databricks it does too
from version 0.2.0 of the ADBC driver; earlier releases delivered decimals as Arrow strings.
See :ref:`explanation-type-fidelity-databricks-decimal`.

Treat a decimal metric as arriving as a ``Decimal`` on every backend, then:
``json.dumps`` on a row holding one raises ``TypeError`` on your laptop as readily as in
production.

What makes it *look* DuckDB-specific is the sample data. The database the tutorial builds
declares ``revenue`` as an ``INTEGER`` and casts its sums to ``BIGINT``, so nothing
computed over it is ever a ``Decimal``, and serialization code written against those
examples has never met the type it will meet against your warehouse. Drop the cast and
DuckDB's own ``SUM`` over that column is a ``HUGEINT``, which arrives as a ``Decimal``
too. The forgiving thing is the fixture, not the engine.

So switching to Snowflake will not introduce this bug and staying on DuckDB will not
protect you from it. What surfaces it is a value that arrives as a ``Decimal``: a decimal
column on any backend, or a sum over an integer column on DuckDB or Snowflake.
:ref:`explanation-type-fidelity` covers which columns arrive as a ``Decimal`` and why the
warehouse rather than Semolina decides that;
:ref:`the untyped route <howto-serialization>` covers the encoder to give
``json.dumps``.

Driver errors, mostly unmeasured
--------------------------------

When a connection cannot be opened, because the warehouse is unreachable or refuses the
credentials, Semolina raises :py:class:`~semolina.exceptions.SemolinaConnectionError` on
every backend. When a query reaches the warehouse and then fails, what reaches your handler
is the ADBC driver's own exception, re-raised unchanged. Which subclass you get is the
driver's decision, and the names do not mean what they sound like. Measured against DuckDB,
a missing view or table, invalid SQL syntax, and a reference to a column that does not exist
all raise ``adbc_driver_manager.InternalError``. A value that fails to cast raises
``ProgrammingError`` instead. Nothing about "internal" describes a typo in a view name.

Snowflake and Databricks have not been measured. The recorded cassettes the test suite
replays contain only successful queries, so there is no observation of what either driver
raises for any of those four failures, and guessing from the DuckDB result would be
guessing twice: once about the driver, once about the warehouse behind it.

Error classification is per-driver knowledge you acquire by
watching your own driver, and that a handler written against DuckDB's classification is
a handler written against one measurement. The "Catch the exception the driver actually
raised" section of :ref:`howto-web-api` has the measured table;
:ref:`tutorial-dashboard-api` has the shape of a handler that does not depend on it.

What Semolina smooths over, and what it leaves alone
----------------------------------------------------

You could imagine Semolina normalizing all of this: aliasing every projected column back
to its model field name, casting decimals to floats, mapping driver exceptions onto its
own hierarchy. It does the first, never the second, and the third only for a connection
that could not be opened.

The names are Semolina's to choose, because Semolina wrote the query. The alias is in the
SQL, where ``.to_sql()`` shows it, and the name it gives is the one you declared on the
model, so nothing about the result is hidden by it. Casting decimals would mean rounding
your revenue on the way past. Wrapping a failed statement's exception would mean
Semolina's guess about a failure standing between you and the driver's message, which is
where the detail lives. A failed connection is different: Semolina opened it, so
it can say so without guessing, and the driver's error is still there as ``__cause__``.

The cost of the other two lands on you as portability work, and this page exists because
that cost is easy to defer until deployment day. The benefit is that nothing arrives
rounded or reclassified, and that what you debug is what the warehouse said.

Working across the gap
----------------------

None of this makes DuckDB the wrong place to develop. It makes DuckDB the wrong place to
*validate* the half of your code that reads results.

A DuckDB test proves the parts that are backend-independent: that your query
builder emits the query you meant, that your filters narrow what you expected, that your
aggregation logic is right. It proves nothing about column types, and it cannot fail on a
decimal your sample data does not contain. Treat a green DuckDB suite as evidence about
query construction and as silence about result handling.

Result-reading code is the part to write against the warehouse you deploy to, from the
first line rather than at the end. Converting into typed objects with
:py:meth:`~semolina.cursor.SemolinaCursor.into` is the portable form of this, because the
type each column arrives as is declared once per field, and the schema check refuses a
declaration the warehouse does not honour. :ref:`howto-typed-results` covers it.

You do not need a connection to find out what the other backends would be sent.
``.to_sql(dialect=...)`` renders a query for any dialect from your laptop, which is the
cheapest way to see how a metric will be wrapped, how identifiers will be folded and what
each column is aliased to before you have credentials for anything. See
:ref:`howto-inspect-sql`.

When you want tests that assert against real warehouse behaviour, the route is to record
it once rather than to approximate it. ``pytest-adbc-replay`` captures the real Arrow
results from a credentialed run into a cassette and replays them offline afterwards, so
the column names and column types your assertions see are the ones your warehouse
produced. :ref:`howto-warehouse-testing` covers recording and replay.

See also
--------

- :ref:`explanation-type-fidelity` -- why money arrives as a ``Decimal`` and what the
  warehouse decides about metric types
- :ref:`howto-result-column-names` -- how result columns get their names, and the types
  they arrive as on each backend
- :ref:`howto-typed-results` -- convert a result into Pydantic instances with
  :py:meth:`~semolina.cursor.SemolinaCursor.into`
- :ref:`the untyped route <howto-serialization>` -- ``dict(row)``, and the encoder
  ``json.dumps`` needs before it will touch a ``Decimal``
- :ref:`howto-inspect-sql` -- render a query for another dialect without executing it
- :ref:`howto-warehouse-testing` -- the DuckDB fixture, and recording your warehouse with
  ``pytest-adbc-replay``
- :ref:`howto-web-api` -- error handling in a request handler, including the measured
  driver exception table
- :ref:`explanation-semantic-views` -- how the three warehouses implement semantic views
