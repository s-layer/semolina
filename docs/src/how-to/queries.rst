.. _howto-queries:

How to build queries
====================

:ref:`tutorial-first-query` runs one query from model to rows. The builder in the middle
of it is the subject here. Chain ``.metrics()``, ``.dimensions()``, ``.where()``,
``.order_by()`` and ``.limit()`` to shape a query, then call ``.execute()`` to get results.
The API is fluent and immutable, so every call hands back a new query rather than mutating
the one you had.

This page walks the chain in the order you write it. Filtering has more surface than one
step of a walkthrough can hold, so ``.where()`` gets its place in the chain here and its
operator set in :ref:`howto-filtering`.

This guide uses the ``Sales`` model from :ref:`tutorial-first-query`:

.. code-block:: python

   from semolina import SemanticView, Metric, Dimension


   class Sales(SemanticView, view="sales"):
       revenue = Metric()
       cost = Metric()
       country = Dimension()
       region = Dimension()

Select metrics
--------------

Use ``.metrics()`` to choose which aggregated measures to include:

.. code-block:: python

   query = Sales.query().metrics(Sales.revenue)
   query = Sales.query().metrics(Sales.revenue, Sales.cost)

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         SELECT AGG("REVENUE") AS "revenue", AGG("COST") AS "cost"
         FROM "SALES"

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         SELECT MEASURE(`revenue`) AS `revenue`, MEASURE(`cost`) AS `cost`
         FROM `sales`

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         SELECT "revenue" AS "revenue", "cost" AS "cost"
         FROM semantic_view('sales', metrics := ['revenue', 'cost'])

Passing a non-``Metric`` field raises ``TypeError``:

.. code-block:: python

   Sales.query().metrics(
       Sales.country
   )  # TypeError: metrics() requires Metric fields

The builder's methods are typed, so a type checker such as pyright or mypy reports that
line before it runs: ``.metrics()`` takes ``Metric`` fields, ``.dimensions()`` takes
``Dimension`` or ``Fact`` fields, and ``.limit()`` takes an ``int``.

At least one field is required -- calling ``.metrics()`` with no arguments raises ``ValueError``.

Select dimensions
-----------------

Use ``.dimensions()`` to group results by :py:class:`~semolina.fields.Dimension` or
:py:class:`~semolina.fields.Fact` fields:

.. code-block:: python

   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
   )
   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country, Sales.region)
   )

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         SELECT AGG("REVENUE") AS "revenue", "COUNTRY" AS "country", "REGION" AS "region"
         FROM "SALES"
         GROUP BY ALL

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         SELECT MEASURE(`revenue`) AS `revenue`, `country` AS `country`, `region` AS `region`
         FROM `sales`
         GROUP BY ALL

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         SELECT "revenue" AS "revenue", "country" AS "country", "region" AS "region"
         FROM semantic_view('sales', dimensions := ['country', 'region'], metrics := ['revenue'])

Passing a ``Metric`` field raises ``TypeError``. At least one field is required.

Each field can appear once per query, because its values come back under a single column
named after it. Selecting one twice, in one call or across two, raises ``ValueError`` while
you build the query:

.. code-block:: python

   Sales.query().metrics(Sales.revenue).metrics(Sales.revenue)
   # ValueError: 'revenue' is selected twice. ...

.. _howto-time-grain:

Group by a time grain
---------------------

A date or timestamp dimension groups by its exact value. To roll it up to weeks, months or
any coarser period, select ``.grain()`` of it instead. Its argument is ``"year"``,
``"quarter"``, ``"month"``, ``"week"``, ``"day"``, ``"hour"``, ``"minute"`` or
``"second"``:

.. code-block:: python

   import datetime

   from semolina import SemanticView, Metric, Dimension


   class Orders(SemanticView, view="orders"):
       revenue = Metric[int]()
       ordered_at = Dimension[datetime.datetime]()
       ordered_on = Dimension[datetime.date]()


   week = Orders.ordered_at.grain("week")
   query = (
       Orders.query()
       .metrics(Orders.revenue)
       .dimensions(week)
       .where(Orders.ordered_at >= datetime.date(2024, 1, 1))
       .order_by(week)
   )

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         SELECT AGG("REVENUE") AS "revenue",
                DATEADD('day', 1 - DAYOFWEEKISO("ORDERED_AT"), CAST("ORDERED_AT" AS DATE))
                    AS "ordered_at__week"
         FROM "ORDERS"
         WHERE "ORDERED_AT" >= DATE '2024-01-01'
         GROUP BY ALL
         ORDER BY "ordered_at__week" ASC

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         SELECT MEASURE(`revenue`) AS `revenue`,
                CAST(DATE_TRUNC('week', `ordered_at`) AS DATE) AS `ordered_at__week`
         FROM `orders`
         WHERE `ordered_at` >= DATE '2024-01-01'
         GROUP BY ALL
         ORDER BY `ordered_at__week` ASC

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         SELECT "revenue" AS "revenue", "ordered_at__week" AS "ordered_at__week"
         FROM semantic_view(
             'orders',
             dimensions := ['CAST(DATE_TRUNC(''week'', "ordered_at") AS DATE) AS "ordered_at__week"'],
             metrics := ['revenue'],
             where_clause := '"ordered_at" >= DATE ''2024-01-01'''
         )
         ORDER BY "ordered_at__week" ASC

      Grains on DuckDB need version 0.16.0 or later of the ``semantic_views`` extension,
      which runs on DuckDB 2.0.

The result column is the field's name, two underscores and the grain, so this query's rows
have the keys ``revenue`` and ``ordered_at__week``. The backends disagree on what a period
looks like by default, so Semolina makes them agree:

- A grain of ``"day"`` or coarser returns a ``datetime.date``, the first day of the period.
  A finer grain returns a ``datetime.datetime`` of the same kind as the field: naive for a
  timestamp without a time zone, aware for one with. Databricks' ``DATE_TRUNC`` would turn
  a ``TIMESTAMP_NTZ`` into a ``TIMESTAMP`` in the session time zone, so on Databricks
  Semolina subtracts the minutes and seconds below the grain instead.
- Weeks start on Monday everywhere. Snowflake's ``DATE_TRUNC('week', ...)`` follows the
  session's ``WEEK_START`` parameter, so Semolina doesn't use it there.

A period can sit beside the field it comes from, or beside another grain of the same field:
``.dimensions(Orders.ordered_at.grain("month"), Orders.ordered_at.grain("week"))`` is fine.

Filter on the field rather than on the grain. ``.where()`` narrows the rows before they are
grouped, so the base field already does what a filter on the period would, and filtering
the grain itself raises ``TypeError``:

.. code-block:: python

   week == datetime.date(2024, 1, 1)
   # TypeError: 'ordered_at__week' cannot be filtered. Filter 'ordered_at' instead, ...

Semolina reads the field's declared type to check the grain. A ``Dimension[datetime.date]``
has no grain finer than a day, so ``Orders.ordered_on.grain("hour")`` raises
``ValueError``. Calling ``.grain()`` on a field declared with any type other than ``date``
or ``datetime`` raises ``TypeError``. An optional declaration such as
``Dimension[datetime.date | None]`` counts as the type it wraps. An undeclared
``Dimension()`` takes every grain.

Use query shorthand
-------------------

Pass ``metrics`` and ``dimensions`` directly to ``query()`` as keyword arguments:

.. code-block:: python

   cursor = Sales.query(
       metrics=[Sales.revenue, Sales.cost],
       dimensions=[Sales.country],
   ).execute()

This is equivalent to the fluent chain:

.. code-block:: python

   cursor = (
       Sales.query()
       .metrics(Sales.revenue, Sales.cost)
       .dimensions(Sales.country)
       .execute()
   )

Shorthand and builder methods are additive. Calling ``.metrics()`` after ``query(metrics=...)``
adds to the selection:

.. code-block:: python

   cursor = (
       Sales.query(metrics=[Sales.revenue])
       .metrics(
           Sales.cost
       )  # now selects both revenue and cost
       .dimensions(Sales.country)
       .execute()
   )

Filter with ``.where()``
------------------------

``.where()`` takes conditions built from field operators. Conditions AND together, whether
you pass several to one call or make several calls, and a ``None`` condition is a no-op.
That last part is what turns an optional filter into a line rather than a branch:

.. code-block:: python

   def revenue_by_country(country: str | None):
       return (
           Sales.query()
           .metrics(Sales.revenue)
           .dimensions(Sales.country)
           .where(Sales.revenue > 1000)
           .where(
               Sales.country == country if country else None
           )
       )

Called with ``"US"`` that renders ``WHERE "COUNTRY" = 'US'`` and ``HAVING AGG("REVENUE") >
1000``: the revenue bound is on a metric, so it filters the totals. Called with ``None`` the
country condition drops out and only the revenue bound survives.

:ref:`howto-filtering` covers the rest: the comparison operators, the named methods that
have no operator spelling (``.between()``, ``.in_()``, ``.like()`` and the others),
composition with ``&``, ``|`` and ``~``, custom lookups, and whether a filter value binds
or inlines on each backend.

.. _howto-ordering:

Order results
-------------

Without ``.order_by()`` the warehouse returns groups in whatever order suits it. Pass a
bare field for ascending, or call ``.asc()`` or ``.desc()`` on the field to say so
explicitly:

.. code-block:: python

   # Ascending -- these two build the same query
   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .order_by(Sales.revenue)
   )
   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .order_by(Sales.revenue.asc())
   )

   # Descending
   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .order_by(Sales.revenue.desc())
   )

Metrics and dimensions are both valid sort keys. On Snowflake a metric sort key is wrapped
in ``AGG()``. Databricks and DuckDB sort a selected field by the name it is projected
under. Anything that is neither a field nor an order term raises ``TypeError``.

A metric does not have to be selected to sort on it. A dimension or fact does. Sorting by
one you left out of ``.dimensions()`` would group the result by it as well, which changes
what one row means, so the query raises ``ValueError`` when you run or preview it, on every
backend:

.. code-block:: python

   Sales.query().metrics(Sales.revenue).order_by(
       Sales.country
   ).to_sql()
   # ValueError: order_by() names Sales.country, a dimension the query does not select. ...

   # Fine: cost is a metric, so it can sort without being selected
   Sales.query().metrics(Sales.revenue).dimensions(
       Sales.country
   ).order_by(Sales.cost.desc())

Break ties with a second sort key
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Rows sharing a revenue figure come back in an arbitrary order until you name a tiebreaker.
Extra fields apply left to right, so the second key only decides where the first one ties:

.. code-block:: python

   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
       .order_by(Sales.revenue.desc(), Sales.country.asc())
   )

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         SELECT AGG("REVENUE") AS "revenue", "COUNTRY" AS "country"
         FROM "SALES"
         GROUP BY ALL
         ORDER BY AGG("REVENUE") DESC, "COUNTRY" ASC

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         SELECT MEASURE(`revenue`) AS `revenue`, `country` AS `country`
         FROM `sales`
         GROUP BY ALL
         ORDER BY `revenue` DESC, `country` ASC

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         SELECT "revenue" AS "revenue", "country" AS "country"
         FROM semantic_view('sales', dimensions := ['country'], metrics := ['revenue'])
         ORDER BY "revenue" DESC, "country" ASC

Separate calls append in the same order, so ``.order_by(a).order_by(b)`` and
``.order_by(a, b)`` produce identical SQL.

Put the NULLs where you want them
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Sort on a nullable column and Semolina emits no ``NULLS`` clause, which leaves the position
of the NULL group to the backend -- and the backends do not all default the same way. Pass
a :py:class:`~semolina.fields.NullsOrdering` to ``.asc()`` or ``.desc()`` when the position
matters:

.. code-block:: python

   from semolina import NullsOrdering

   # Rows with no region sort to the bottom, whichever warehouse runs this
   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.region)
       .order_by(Sales.region.asc(NullsOrdering.LAST))
   )

.. list-table::
   :header-rows: 1

   * - Value
     - SQL generated
     - Meaning
   * - ``NullsOrdering.FIRST``
     - ``NULLS FIRST``
     - NULLs sort before non-NULL values
   * - ``NullsOrdering.LAST``
     - ``NULLS LAST``
     - NULLs sort after non-NULL values
   * - ``NullsOrdering.DEFAULT``
     - *(no NULLS clause)*
     - Backend decides (default)

Reuse an order term
~~~~~~~~~~~~~~~~~~~

``.asc()`` and ``.desc()`` return :py:class:`~semolina.fields.OrderTerm` values that hold
no reference to a query. Build the sorts your API is willing to serve once, then look them
up by name:

.. code-block:: python

   from semolina import NullsOrdering

   SORTS = {
       "revenue": Sales.revenue.desc(NullsOrdering.LAST),
       "country": Sales.country.asc(),
   }


   def top_countries(sort: str = "revenue"):
       return (
           Sales.query()
           .metrics(Sales.revenue)
           .dimensions(Sales.country)
           .order_by(SORTS[sort])
       )

Looking the term up rather than building it from the request parameter also keeps an
unexpected ``sort`` value out of the query: an unknown key raises ``KeyError`` before any
SQL exists.

Limit result count
------------------

``.limit(n)`` caps the number of rows the warehouse returns:

.. code-block:: python

   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
       .limit(10)
   )

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         SELECT AGG("REVENUE") AS "revenue", "COUNTRY" AS "country"
         FROM "SALES"
         GROUP BY ALL
         LIMIT 10

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         SELECT MEASURE(`revenue`) AS `revenue`, `country` AS `country`
         FROM `sales`
         GROUP BY ALL
         LIMIT 10

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         SELECT "revenue" AS "revenue", "country" AS "country"
         FROM semantic_view('sales', dimensions := ['country'], metrics := ['revenue'])
         LIMIT 10

``n`` must be a positive integer. Zero or a negative number raises ``ValueError`` and a
non-integer raises ``TypeError``, both as you build the query rather than at
``.execute()``.

Take the top N
~~~~~~~~~~~~~~

On its own ``.limit()`` returns an arbitrary handful of rows. After an ``.order_by()`` it
returns the top N:

.. code-block:: python

   # The ten highest-revenue countries
   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
       .order_by(Sales.revenue.desc())
       .limit(10)
   )

.. note:: There is no ``.offset()``

   ``.limit(n)`` is the only row-count control on the query builder. Semolina has no
   ``offset()``, so classic ``LIMIT``/``OFFSET`` pagination cannot be expressed through
   the fluent API in this release.

   For a list sorted by a dimension, filter on that dimension instead of skipping rows:
   order by it, take ``.limit(page_size)``, and make the next request ask for rows past
   the last key you received (``.where(Sales.country > last_seen)``). That is keyset
   pagination, and on an aggregate query it is usually cheaper than ``OFFSET``, because
   the warehouse does not have to compute and discard the skipped groups.

   A list sorted by a metric can't be paged that way. Metric values tie, so the next-page
   condition needs a dimension tiebreaker, ``(Sales.revenue < last) | ((Sales.revenue ==
   last) & (Sales.country > last_country))``, and a metric and a dimension joined under
   ``|`` raise ``ValueError`` (see :ref:`howto-filtering-mix-kinds`). For a ranked list,
   fetch ``.limit(page_size * pages)`` once and slice the pages out of it.

Choose the engine
-----------------

Use ``.using()`` to select a different registered engine by name. Engine
resolution is lazy -- it happens at ``.execute()`` time, not during query
construction:

.. code-block:: python

   # Uses the engine registered as "warehouse" instead of "default"
   query = (
       Sales.query().metrics(Sales.revenue).using("warehouse")
   )

If no ``.using()`` call is made, Semolina uses the engine registered as
``"default"``. See :ref:`howto-connection-pools` for how to build and register
engines.

Execute and read results
------------------------

Call ``.execute()`` to run the query and get back a :py:class:`~semolina.cursor.SemolinaCursor`:

.. code-block:: python

   cursor = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
       .execute()
   )

   for row in cursor.fetchall_rows():
       print(row.country, row.revenue)  # attribute access
       print(row["country"])  # dict-style access

``.execute()`` validates the query, resolves the engine, runs the SQL and returns the
cursor. A query selecting neither a metric nor a dimension raises ``ValueError`` at this
point rather than during construction, which is what lets you build a query in pieces.

A row's keys are the Python field names, ``revenue`` and ``country`` here, on every
backend. Semolina aliases each selected column to its field name in the SQL it sends.

Fetch methods
~~~~~~~~~~~~~

:py:class:`~semolina.cursor.SemolinaCursor` provides both ``Row``-based and raw DBAPI fetch methods:

.. code-block:: python

   # Row objects (primary pattern)
   rows = cursor.fetchall_rows()  # list[Row]
   row = cursor.fetchone_row()  # Row | None
   batch = cursor.fetchmany_rows(10)  # list[Row]

   # Raw DBAPI tuples
   raw = cursor.fetchall()  # list[tuple]
   raw_one = cursor.fetchone()  # tuple | None
   raw_batch = cursor.fetchmany(10)  # list[tuple]

   # Context manager (closes cursor + connection on exit)
   with Sales.query(
       metrics=[Sales.revenue]
   ).execute() as cursor:
       rows = cursor.fetchall_rows()

The cursor hands back other result shapes too:

- :ref:`howto-streaming` -- whole-result Arrow tables and dataframes, batched and lazy
  reads for large results, and the async cursor
- :ref:`howto-typed-results` -- Pydantic instances via ``.into()``
- :ref:`the untyped route <howto-serialization>` -- dictionaries and JSON

.. _howto-inspect-sql:

Inspect generated SQL
---------------------

Use ``.to_sql()`` to see the SQL structure without executing the query:

.. code-block:: python

   sql = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
       .to_sql()
   )
   print(sql)

.. code-block:: sql

   SELECT AGG("REVENUE") AS "revenue", "COUNTRY" AS "country"
   FROM "SALES"
   GROUP BY ALL

The Snowflake dialect folds identifiers to upper case to reach the view's columns, and the
quoted ``AS`` aliases give the result columns back their field names -- see
:ref:`howto-result-column-names`.

.. tip::

   ``.to_sql()`` renders for the engine the query names with ``.using()``, when an engine is
   registered under that name. Otherwise it renders the Snowflake dialect (``AGG()``,
   double-quoted identifiers), including for a query with no ``.using()``. Pass a
   ``dialect`` argument to preview another backend's SQL, for example
   ``.to_sql(dialect="databricks")`` or ``.to_sql(dialect="duckdb")``.

Fork and reuse queries
----------------------

Every builder method returns a new query and leaves the original alone, so a base query is
safe to keep around and safe to hand out:

.. code-block:: python

   base = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
   )

   # Fork into specialized variants -- base is unchanged
   us_only = base.where(Sales.country == "US")
   us_top_10 = base.where(Sales.country == "US").limit(10)

   print(base.to_sql())  # no WHERE, no LIMIT
   print(us_top_10.to_sql())  # has WHERE and LIMIT

The same property makes it safe to assemble a query across function boundaries. A helper
that adds a clause cannot disturb the query its caller is still holding. Annotate it with
:py:class:`~semolina.query.Query`, the type every builder method returns:

.. code-block:: python

   from semolina import Query


   def add_revenue_floor(
       query: Query, threshold: int
   ) -> Query:
       return query.where(Sales.revenue > threshold)


   cursor = add_revenue_floor(base, 1000).execute()

Read a query back
-----------------

A query reports what it holds through read-only properties, so code that receives one,
such as a cache, an audit log or a middleware, can inspect it without running it:

.. code-block:: python

   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
       .where(Sales.country == "US")
       .limit(10)
   )

   [
       field.name for field in query.selected_metrics
   ]  # ['revenue']
   [
       field.name for field in query.selected_dimensions
   ]  # ['country']
   query.filters  # Exact(field_name='country', value='US')
   query.limit_value  # 10
   query.engine_name  # None: the "default" engine
   query.model  # the Sales class

``ordering`` holds the ``.order_by()`` terms. The underscore attributes behind these are
private and can change between releases.

See also
--------

- :ref:`tutorial-first-query` -- one query from model to rows, start to finish
- :ref:`tutorial-shaping-a-report` -- filtering, ordering and limiting run step by step
- :ref:`howto-query-by-name` -- build the same queries, grains included, from field names
- :ref:`howto-filtering` -- field operators, named methods and boolean composition
- :ref:`howto-models` -- define :py:class:`~semolina.models.SemanticView` subclasses
  with field types
- :ref:`howto-typed-results` -- map rows into Pydantic models, and the untyped
  dictionary route
- :ref:`howto-connection-pools` -- build and register the engines ``.using()`` resolves
- :ref:`howto-backends` -- the connection settings each warehouse takes, and the SQL its
  dialect renders
