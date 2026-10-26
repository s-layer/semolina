.. _howto-filtering:

How to filter queries
=====================

``.where()`` gets two comparisons in :ref:`tutorial-shaping-a-report` and nothing harder.
A field supports more operators than that, plus named methods that have no operator
spelling at all, and they compose with ``&`` (AND), ``|`` (OR) and ``~`` (NOT).

Every condition on this page is a :py:class:`~semolina.filters.Predicate`. That is the type
to annotate against if you pass conditions between functions, and it is what
``.where()`` accepts. Anything else (a SQL string, a bare field such as ``Sales.region``, or
a ``bool``) raises ``TypeError`` at the ``.where()`` call.

This guide uses the ``Sales`` model from :ref:`tutorial-first-query`:

.. code-block:: python

   from semolina import SemanticView, Metric, Dimension


   class Sales(SemanticView, view="sales"):
       revenue = Metric()
       cost = Metric()
       country = Dimension()
       region = Dimension()

Use comparison operators
------------------------

Standard Python comparison operators work directly on fields:

.. list-table::
   :header-rows: 1

   * - Operator
     - Meaning
     - Example
   * - ``==``
     - Equals
     - ``Sales.country == "US"``
   * - ``!=``
     - Not equals
     - ``Sales.country != "US"``
   * - ``>``
     - Greater than
     - ``Sales.revenue > 1000``
   * - ``>=``
     - Greater than or equal
     - ``Sales.revenue >= 500``
   * - ``<``
     - Less than
     - ``Sales.revenue < 100``
   * - ``<=``
     - Less than or equal
     - ``Sales.revenue <= 999``

.. code-block:: python

   # Revenue greater than 1000
   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .where(Sales.revenue > 1000)
   )

   # Country equals US
   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .where(Sales.country == "US")
   )

   # Revenue between bounds (explicit)
   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .where((Sales.revenue >= 500) & (Sales.revenue <= 2000))
   )

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         SELECT AGG("REVENUE") AS "revenue"
         FROM "SALES"
         HAVING AGG("REVENUE") > 1000

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         SELECT MEASURE(`revenue`) AS `revenue`
         FROM `sales`
         HAVING MEASURE(`revenue`) > 1000

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         SELECT "revenue" AS "revenue"
         FROM semantic_view('sales', metrics := ['revenue'])
         WHERE "revenue" > 1000

``revenue`` is a metric, so the condition filters its aggregated value, the way
``HAVING`` does. Snowflake and Databricks get exactly that: a ``HAVING`` on the wrapped
metric. Both refuse a metric in ``WHERE``. DuckDB applies the same condition in an outer
``WHERE`` after ``semantic_view()`` has aggregated. A condition on a dimension, like
``Sales.country == "US"``, is a ``WHERE`` everywhere. See
:ref:`howto-filtering-mix-kinds`.

.. _howto-filtering-binding:

How filter values reach the warehouse
-------------------------------------

The SQL blocks on this page are previews from ``to_sql()``, which prints values
inline so the statement reads as one piece. That is not how most of them
execute. On Snowflake and Databricks, and for a metric condition on DuckDB, the value
never appears in the SQL string at all:

.. list-table::
   :header-rows: 1

   * - Backend
     - How a ``.where()`` value is sent
     - What the driver receives
   * - Snowflake
     - Bound parameter, except a date, timestamp or time, which is inlined
     - ``WHERE "COUNTRY" = ?`` with ``['US']``, or ``HAVING AGG("REVENUE") > ?`` with
       ``[1000]``; ``WHERE "ORDERED_ON" >= DATE '2024-04-01'`` with no parameter
   * - DuckDB, dimension or fact condition
     - Inlined as a SQL literal, inside one bound ``where_clause`` string
     - ``where_clause := ?`` with the string ``"country" = 'US'``
   * - DuckDB, metric condition
     - Bound parameter
     - ``WHERE "revenue" > ?`` with ``[1000]``
   * - Databricks
     - Bound parameter
     - ``WHERE `country` = ?`` with ``['US']``, or ``HAVING MEASURE(`revenue`) > ?``
       with ``[1000]``

Databricks binds values the same way Snowflake does. That needs version 0.2.0 or
later of the Databricks ADBC driver, the first release that accepts bind
parameters; see :ref:`howto-backends-databricks`. A value of ``US' OR '1'='1``
reaches the warehouse as one country name that matches nothing, not as extra SQL.

The Snowflake ADBC driver can't bind a date, timestamp or time, so Semolina writes those
values into the SQL as the literals ``to_sql()`` shows, through the same escaping
function as below. Every other Snowflake value is bound.

DuckDB inlines more widely, but only for a dimension or fact condition. The condition goes into ``semantic_view()``'s ``where_clause`` argument,
which takes SQL text, and a ``?`` inside that text cannot be bound. So Semolina renders
the condition through a single escaping function, ``render_literal``, which doubles any
single quote, and binds the finished string as one parameter.

.. note:: Passing a value from an HTTP request

   You do not need to escape or allow-list a filter value before handing it to
   ``.where()``. Every path above treats it as data: a bound value never reaches
   the parser, and an inlined one is escaped at the one audited site.

   Validating the *type* is still yours to do. A value of an unsupported Python
   type, such as a ``dict``, raises ``NotImplementedError`` where Semolina inlines
   it, but a bound value is handed to the driver, which fails later and in its own
   vocabulary. Coercing request parameters to
   the type the column expects gives you the better error, whichever backend
   you are on.

   What you should still validate is anything that is *not* a value: a column
   name chosen by the caller selects a field rather than filling one in.

Read a ``to_sql()`` preview
~~~~~~~~~~~~~~~~~~~~~~~~~~~

At execution Snowflake and Databricks bind each value (Snowflake inlines a date,
timestamp or time), and DuckDB binds its ``where_clause``. ``to_sql()`` cannot show a binding, so it writes every value as the
chosen dialect's SQL literal instead: ``Sales.country == "O'Brien"`` previews as
``WHERE "COUNTRY" = 'O''Brien'`` on Snowflake, a date as ``DATE '2024-01-02'``, and a
``Decimal`` as its digits. You can paste a preview into the warehouse's console and run
it.

A value with no literal in that dialect can't be previewed, so ``to_sql()`` raises for it
rather than print SQL that would not run. On Snowflake that is ``bytes``, a ``timedelta``
and a ``time`` with a time zone: the query still executes, because those values are bound,
but preview it with ``to_sql("duckdb")`` instead. Databricks has no literal for ``bytes``,
a ``timedelta`` or a ``time`` either, so the preview raises there too. At execution the
value goes to the driver as a bound parameter; whether the driver and warehouse accept it
depends on the type, and these three are untested.

.. warning:: DuckDB can't filter on ``bytes`` when a source column is named ``blob``

   On DuckDB, a ``bytes`` value reaches the semantic view as a ``'...'::BLOB`` cast. The
   ``semantic_views`` extension rewrites field names inside the filter as text, and if a
   table behind the view has a column named ``blob``, it rewrites the ``BLOB`` type name
   too. The query then fails with a parser error. Renaming that column avoids it. The
   bug is in the extension, and has been reported there.

Use named filter methods
------------------------

Fields provide named methods for common SQL operations beyond simple comparisons.

``.between(lo, hi)``
~~~~~~~~~~~~~~~~~~~~~

Range check (inclusive):

.. code-block:: python

   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .where(Sales.revenue.between(500, 2000))
   )

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         HAVING AGG("REVENUE") BETWEEN 500 AND 2000

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         HAVING MEASURE(`revenue`) BETWEEN 500 AND 2000

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         WHERE "revenue" BETWEEN 500 AND 2000

Both bounds are required. A ``None`` bound raises ``TypeError``, because ``BETWEEN`` with a
``NULL`` bound matches no rows. For an open-ended range, use ``>=`` or ``<=``.

``.in_(values)``
~~~~~~~~~~~~~~~~~

Membership in a collection:

.. code-block:: python

   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .where(Sales.country.in_(["US", "CA", "MX"]))
   )

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         WHERE "COUNTRY" IN ('US', 'CA', 'MX')

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         WHERE `country` IN ('US', 'CA', 'MX')

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         where_clause := '"country" IN (''US'', ''CA'', ''MX'')'

``.in_()`` takes any iterable: a list, tuple, set, or a generator. It copies the values when
you call it, so changing your list afterwards does not change a query you have already
built. An empty collection matches no rows.

A single string is refused with a ``TypeError``. Python would otherwise read ``"US"`` as the
two values ``'U'`` and ``'S'``. To match one value, pass ``["US"]`` or write
``Sales.country == "US"``.

``None`` among the values raises ``TypeError`` too, since ``IN`` never matches ``NULL``. To
include missing values, combine the two:
``Sales.country.in_(["US", "CA"]) | Sales.country.isnull()``.

``.isnull()``
~~~~~~~~~~~~~~

Null check:

.. code-block:: python

   # Find rows where region IS NULL
   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .where(Sales.region.isnull())
   )

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         WHERE "REGION" IS NULL

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         WHERE `region` IS NULL

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         where_clause := '"region" IS NULL'

``Sales.region == None`` compiles to the same ``IS NULL``, and ``Sales.region != None`` to
``IS NOT NULL``. Every other comparison with ``None`` (``>``, ``.like()``,
``.startswith()`` and the rest) raises ``TypeError`` when you build it. In SQL a comparison
with ``NULL`` matches no rows, so the query would quietly return nothing.

``.like(pattern)`` and ``.ilike(pattern)``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

SQL LIKE pattern matching with ``%`` and ``_`` wildcards. ``.ilike()`` is
case-insensitive. Semolina passes the pattern through as you wrote it, wildcards included:

.. code-block:: python

   # Case-sensitive
   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .where(Sales.country.like("U%"))
   )

   # Case-insensitive
   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .where(Sales.country.ilike("u%"))
   )

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         WHERE "COUNTRY" LIKE 'U%'

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         WHERE `country` LIKE 'U%'

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         where_clause := '"country" LIKE ''U%'''

``.startswith(prefix)`` and ``.istartswith(prefix)``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Prefix match. ``.istartswith()`` is case-insensitive.

Unlike ``.like()``, the prefix, suffix and ``.iexact()`` methods match their value
literally. A ``%`` or ``_`` in it is escaped, so ``Sales.country.startswith("U_")`` finds
only countries that begin with a ``U`` and an underscore, and not ``US``. The ``ESCAPE '!'``
in the generated SQL is what makes that work:

.. code-block:: python

   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .where(Sales.country.startswith("U"))
   )

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         WHERE "COUNTRY" LIKE 'U%' ESCAPE '!'

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         WHERE `country` LIKE 'U%' ESCAPE '!'

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         where_clause := '"country" LIKE ''U%'' ESCAPE ''!'''

``.endswith(suffix)`` and ``.iendswith(suffix)``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Suffix match. ``.iendswith()`` is case-insensitive:

.. code-block:: python

   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .where(Sales.region.endswith("est"))
   )

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         WHERE "REGION" LIKE '%est' ESCAPE '!'

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         WHERE `region` LIKE '%est' ESCAPE '!'

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         where_clause := '"region" LIKE ''%est'' ESCAPE ''!'''

``.iexact(value)``
~~~~~~~~~~~~~~~~~~~

Case-insensitive equality (no wildcards):

.. code-block:: python

   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .where(Sales.country.iexact("united states"))
   )

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         WHERE "COUNTRY" ILIKE 'united states' ESCAPE '!'

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         WHERE `country` ILIKE 'united states' ESCAPE '!'

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         where_clause := '"country" ILIKE ''united states'' ESCAPE ''!'''

Combine conditions with OR
---------------------------

Use ``|`` to combine two conditions with OR logic:

.. code-block:: python

   # country = 'US' OR country = 'CA'
   condition = (Sales.country == "US") | (
       Sales.country == "CA"
   )

   cursor = (
       Sales.query()
       .metrics(Sales.revenue)
       .where(condition)
       .execute()
   )

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         SELECT AGG("REVENUE") AS "revenue"
         FROM "SALES"
         WHERE ("COUNTRY" = 'US' OR "COUNTRY" = 'CA')

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         SELECT MEASURE(`revenue`) AS `revenue`
         FROM `sales`
         WHERE (`country` = 'US' OR `country` = 'CA')

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         SELECT "revenue" AS "revenue"
         FROM semantic_view('sales', metrics := ['revenue'],
                            where_clause := '("country" = ''US'' OR "country" = ''CA'')')

Combine conditions with AND
-----------------------------

Use ``&`` to combine two conditions with AND logic:

.. code-block:: python

   # country = 'US' AND revenue > 500
   condition = (Sales.country == "US") & (Sales.revenue > 500)

   cursor = (
       Sales.query()
       .metrics(Sales.revenue)
       .where(condition)
       .execute()
   )

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         WHERE "COUNTRY" = 'US'
         HAVING AGG("REVENUE") > 500

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         WHERE `country` = 'US'
         HAVING MEASURE(`revenue`) > 500

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         -- inside semantic_view(...)
         where_clause := '"country" = ''US'''
         -- after it
         WHERE "revenue" > 500

On every backend the AND is split: the dimension condition filters rows before
aggregation and the metric condition filters the aggregated result. See
:ref:`howto-filtering-mix-kinds`.

Multiple ``.where()`` calls are also ANDed together:

.. code-block:: python

   # Equivalent to the & example above
   cursor = (
       Sales.query()
       .metrics(Sales.revenue)
       .where(Sales.country == "US")
       .where(Sales.revenue > 500)
       .execute()
   )

You can also pass multiple conditions as arguments to a single ``.where()`` call:

.. code-block:: python

   # Also equivalent -- varargs are ANDed together
   cursor = (
       Sales.query()
       .metrics(Sales.revenue)
       .where(Sales.country == "US", Sales.revenue > 500)
       .execute()
   )

Negate conditions with NOT
---------------------------

Use ``~`` to negate a condition:

.. code-block:: python

   # NOT (country = 'US')
   condition = ~(Sales.country == "US")

   cursor = (
       Sales.query()
       .metrics(Sales.revenue)
       .where(condition)
       .execute()
   )

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         WHERE NOT ("COUNTRY" = 'US')

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         WHERE NOT (`country` = 'US')

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         where_clause := 'NOT ("country" = ''US'')'

Negate a metric condition the same way:

.. code-block:: python

   # NOT (revenue < 100)
   condition = ~(Sales.revenue < 100)

Build complex nested conditions
--------------------------------

Combine ``|``, ``&``, and ``~`` to express arbitrary conditions. Use parentheses to
control grouping:

.. code-block:: python

   # (country = 'US' OR country = 'CA') AND NOT (revenue < 100)
   condition = (
       (Sales.country == "US") | (Sales.country == "CA")
   ) & ~(Sales.revenue < 100)

   cursor = (
       Sales.query()
       .metrics(Sales.revenue)
       .where(condition)
       .execute()
   )

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         WHERE ("COUNTRY" = 'US' OR "COUNTRY" = 'CA')
         HAVING NOT (AGG("REVENUE") < 100)

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         WHERE (`country` = 'US' OR `country` = 'CA')
         HAVING NOT (MEASURE(`revenue`) < 100)

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         -- inside semantic_view(...)
         where_clause := '("country" = ''US'' OR "country" = ''CA'')'
         -- after it
         WHERE NOT ("revenue" < 100)

.. warning:: Operator precedence: ``&`` binds tighter than ``|``

   Python evaluates ``&`` before ``|`` -- the same precedence as bitwise operators.
   This can produce unexpected results when mixing them:

   .. code-block:: python

      # DANGEROUS: reads as a | (b & c)
      condition = (Sales.country == "US") | (
          Sales.region == "West"
      ) & (Sales.revenue > 500)

      # SAFE: parentheses make intent explicit
      condition = (
          (Sales.country == "US") | (Sales.region == "West")
      ) & (Sales.revenue > 500)

   Here the misread version also joins a dimension and a metric under ``|``, so it raises
   ``ValueError`` (see :ref:`howto-filtering-mix-kinds`). The parenthesized one ORs the
   two dimensions and ANDs the metric condition at the top level, which splits cleanly.

   **Always use parentheses when mixing** ``|`` **and** ``&`` **in the same expression.**

.. _howto-filtering-mix-kinds:

Mix dimension and metric conditions
-----------------------------------

A dimension condition and a metric condition apply at different moments. The dimension
condition picks the rows that go into each group; the metric condition picks groups by
their aggregated value. Semolina sends each where it belongs:

.. list-table::
   :header-rows: 1

   * - Backend
     - Dimension and fact conditions
     - Metric conditions
   * - Snowflake
     - ``WHERE``
     - ``HAVING AGG(...)``
   * - Databricks
     - ``WHERE``
     - ``HAVING MEASURE(...)``
   * - DuckDB
     - ``semantic_view()``'s ``where_clause``
     - an outer ``WHERE``

Semolina splits the filter for you at its top-level ``&``. So filtering on a dimension
you have not selected narrows the rows without changing the grain: revenue by region,
filtered to two countries, is still one row per region. Filtering on a metric you have
not selected works too. The condition names the metric, and the metric is not added to
the results.

What cannot be split is a dimension and a metric joined by ``|``, or both under one
``~``. No single clause can apply such a condition, so the query raises ``ValueError``
when you run or preview it, naming the fields:

.. code-block:: python

   # country = 'US' OR revenue > 500
   Sales.query().metrics(Sales.revenue).where(
       (Sales.country == "US") | (Sales.revenue > 500)
   ).to_sql()
   # ValueError: Semolina cannot apply a filter that combines Sales.country with the
   # metric Sales.revenue under OR or NOT. ...

Build filters conditionally
-----------------------------

Each ``.where()`` call ANDs with the accumulated filter, so you can add conditions one
``if`` at a time:

.. code-block:: python

   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
   )

   if region_filter:
       query = query.where(Sales.region == region_filter)

   if min_revenue:
       query = query.where(Sales.revenue >= min_revenue)

   cursor = query.execute()

``.where()`` also accepts ``None`` as a no-op, so each optional filter becomes one expression:

.. code-block:: python

   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
       .where(
           Sales.region == region_filter
           if region_filter
           else None
       )
       .where(
           Sales.revenue >= min_revenue
           if min_revenue
           else None
       )
   )

   cursor = query.execute()

Use custom lookups
-------------------

For filter operations not covered by the built-in operators or named methods,
define a custom :py:class:`~semolina.filters.Lookup` subclass and use ``.lookup()``:

.. code-block:: python

   from semolina.filters import Lookup


   class RegexpMatch(Lookup[str]):
       """Regexp match: ``field REGEXP pattern``."""


   # Use with .lookup()
   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .where(Sales.country.lookup(RegexpMatch, "^U.*S$"))
   )

Defining the subclass and building the query both succeed. Compiling it does not:

.. code-block:: python

   query.to_sql()
   # NotImplementedError: Unsupported lookup type: RegexpMatch.
   # Add a case for it in _compile_predicate().

.. warning:: ``Lookup`` is not a public extension point

   The ``case`` branch that error asks for lives in ``SQLBuilder._compile_predicate`` --
   a private method inside the installed package, not in your code. Reaching it means
   subclassing :py:class:`~semolina.engines.sql.SQLBuilder` to override that private
   method, subclassing the dialect (a :py:class:`~semolina.engines.sql.SQLDialect`) to
   override ``create_builder()`` so your builder is the one used, and then depending on a
   private API across upgrades.

   So treat ``Lookup`` as machinery the built-in operators are made from, rather than a
   seam you can extend from application code. If you need an operator Semolina does not
   have, open an issue -- adding it upstream is the supported route, and it is a small
   change in the place that already handles every other operator.

See also
--------

- :ref:`tutorial-shaping-a-report` -- ``.where()`` and boolean composition, run step by step
- :ref:`howto-queries` -- the full query API with ``.metrics()``, ``.dimensions()``, ``.execute()``
- :ref:`howto-models` -- field types and how they affect filtering
- :ref:`tutorial-dashboard-api` -- passing a request parameter straight into ``.where()``
- :ref:`explanation-duckdb-vs-warehouse` -- what a DuckDB-only test suite proves about
  your filters, and what it does not
