.. _howto-query-by-name:

How to build a query from field names
=====================================

You usually write a query against a model's attributes: ``Sales.revenue``,
``Sales.country``. A caller that has only names, such as a language model making a tool
call or a JSON body posted to your API, can't do that. :mod:`semolina.spec` takes the
query as plain data instead, looks each name up on the model, and returns an ordinary
:py:class:`~semolina.query.Query`.

This guide uses this model:

.. code-block:: python

   import datetime

   from semolina import SemanticView, Metric, Dimension


   class Sales(SemanticView, view="sales"):
       revenue = Metric[int]()
       cost = Metric[int]()
       country = Dimension[str]()
       region = Dimension[str]()
       sold_on = Dimension[datetime.date]()

Build the query
---------------

Pass the model and a dict to :py:func:`~semolina.spec.build_query`:

.. code-block:: python

   from semolina.spec import build_query

   query = build_query(
       Sales,
       {
           "metrics": ["revenue"],
           "dimensions": ["country"],
           "where": {
               "field": "region",
               "op": "in",
               "value": ["West", "East"],
           },
           "order_by": ["-revenue"],
           "limit": 10,
       },
   )

   with query.execute() as cursor:
       rows = cursor.fetchall_rows()

You get the same query as this attribute-based version, and it compiles to the same SQL on
every backend:

.. code-block:: python

   (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
       .where(Sales.region.in_(["West", "East"]))
       .order_by(Sales.revenue.desc())
       .limit(10)
   )

Every key is optional, and ``null`` means the same as leaving the key out, but the data
has to select at least one metric or dimension. Names are the model's Python attribute
names, which are also the keys of each result row. They are never the warehouse column that
a ``source=`` field reads. ``using=`` picks the engine, as :py:meth:`Query.using()
<semolina.query.Query.using>` does.

Sort terms are a field name for ascending, the name after ``-`` for descending, or an
object when you need ``NULLS FIRST`` or ``NULLS LAST``:

.. code-block:: python

   {
       "order_by": [
           {
               "field": "revenue",
               "direction": "desc",
               "nulls": "last",
           }
       ]
   }

Write filters as data
---------------------

A filter is either a leaf that compares one field, or ``and``, ``or`` or ``not`` over other
filters. Pass ``where`` a list and every filter in it must hold.

.. code-block:: python

   {
       "where": [
           {
               "or": [
                   {
                       "field": "country",
                       "op": "eq",
                       "value": "US",
                   },
                   {
                       "and": [
                           {
                               "field": "region",
                               "op": "eq",
                               "value": "West",
                           },
                           {
                               "not": {
                                   "field": "country",
                                   "op": "in",
                                   "value": ["CA", "MX"],
                               }
                           },
                       ]
                   },
               ]
           },
           {
               "not": {
                   "field": "revenue",
                   "op": "lt",
                   "value": 1000,
               }
           },
       ]
   }

Each ``op`` calls the field method of the same meaning, so the filtering rules in
:ref:`howto-filtering` apply unchanged. Filters on metrics still go to ``HAVING``, for
example, which is why the ``revenue`` condition above sits at the top of ``where``. A
dimension filter applies before aggregation and a metric filter after it, so the two can
only be joined by the list or a top-level ``and``. Combining them under ``or`` or ``not``
raises :py:class:`~semolina.exceptions.SemolinaSpecError`.

.. list-table::
   :header-rows: 1

   * - ``op``
     - ``value``
     - Same as
   * - ``eq``, ``ne``
     - a value, or ``null`` for ``IS NULL`` / ``IS NOT NULL``
     - ``==``, ``!=``
   * - ``gt``, ``gte``, ``lt``, ``lte``
     - a value
     - ``>``, ``>=``, ``<``, ``<=``
   * - ``in``
     - a list
     - ``.in_()``
   * - ``between``
     - ``[low, high]``
     - ``.between()``
   * - ``isnull``
     - ``true`` (the default) or ``false`` for ``IS NOT NULL``
     - ``.isnull()``
   * - ``like``, ``ilike``
     - a pattern with ``%`` and ``_`` wildcards
     - ``.like()``, ``.ilike()``
   * - ``startswith``, ``istartswith``, ``endswith``, ``iendswith``, ``iexact``
     - a string, matched literally
     - the method of the same name

JSON has no date type, so dates arrive as strings. If the model declares a field's type,
as ``Dimension[datetime.date]()`` does, an ISO string in its filter becomes a
``datetime.date``, and Semolina writes a ``DATE`` literal for it. Fields declared with
``datetime.datetime`` or ``datetime.time`` work the same way. Any other field keeps the
string as it was given.

.. code-block:: python

   {
       "where": {
           "field": "sold_on",
           "op": "between",
           "value": ["2024-01-01", "2024-03-31"],
       }
   }

Report mistakes back to the caller
----------------------------------

Every mistake in the data raises :py:class:`~semolina.exceptions.SemolinaSpecError`, which
is a ``ValueError``. Its ``path`` says where the mistake is, and the message says what to write
instead. An unknown name lists the closest field names the model does have:

.. code-block:: python

   from semolina import SemolinaSpecError

   try:
       build_query(Sales, {"metrics": ["revnue"]})
   except SemolinaSpecError as error:
       print(error.path)  # metrics[0]
       print(error)
       # metrics[0]: Sales has no field 'revnue'. Did you mean 'revenue'? Its fields are ...

The whole query is checked before any SQL is written, so a tool can return the message to
the model in the same turn and let it try again. In a web handler, answer it with a ``400``,
and catch it before any ``except SemolinaError`` clause, since it is a ``SemolinaError``
too: :ref:`howto-web-api-errors` shows the order.

Give a model the schema
-----------------------

:py:func:`~semolina.spec.query_spec_schema` describes the data one model accepts as a JSON
Schema (draft 2020-12). Its enums hold that model's own metric and dimension names, and
each filter ``op`` is tied to the ``value`` shape it takes. Use it as a tool's input schema:

.. code-block:: python

   from semolina.spec import build_query, query_spec_schema

   tool = {
       "name": "query_sales",
       "description": "Revenue and cost by country and region.",
       "input_schema": query_spec_schema(Sales),
   }


   def run_tool(arguments: dict) -> list[dict]:
       query = build_query(Sales, arguments)
       with query.execute() as cursor:
           return [dict(row) for row in cursor.fetchall_rows()]

To type the handler's argument, annotate it with
:py:class:`~semolina.spec.QuerySpec`, the ``TypedDict`` that ``build_query()`` accepts:
``def run_tool(arguments: QuerySpec) -> list[dict]``.

Data the schema accepts builds, with three exceptions that JSON Schema can't express:
ordering by a dimension the query doesn't select, a date string that doesn't parse, and a
dimension and a metric combined under ``or`` or ``not``. All three still raise
``SemolinaSpecError``, so catch it in the tool either way.

Grains work by name too. ``"sold_on__month"`` in ``dimensions`` or ``order_by`` selects
``Sales.sold_on.grain("month")``, and the schema lists those names for every dimension
declared as a ``date`` or ``datetime``, with only the grains its type allows: ``year``
through ``day`` for a ``date``. A filter leaf names the base field, as
:ref:`howto-time-grain` explains.

Build part of a query
---------------------

When your code picks the metrics and dimensions and only the filter comes from outside,
build the filter alone with :py:func:`~semolina.spec.predicate_from_spec`. It takes the
same filter data as ``where`` and returns the predicate the field operators would build,
so you pass it to ``.where()`` yourself:

.. code-block:: python

   from semolina.spec import predicate_from_spec

   region_filter = predicate_from_spec(
       Sales,
       {
           "field": "region",
           "op": "in",
           "value": ["West", "East"],
       },
   )
   query = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
       .where(region_filter)
   )

It raises ``SemolinaSpecError`` for the same mistakes in the filter that ``build_query()``
does.

To look up a single field, use :py:func:`~semolina.spec.field_by_name`. It returns the same
descriptor as attribute access, so ``field_by_name(Sales, "revenue") is Sales.revenue``.

See also
--------

- :ref:`howto-queries`: the attribute-based query builder this module produces queries for
- :ref:`howto-time-grain`: grouping a date dimension by week, month or another period
- :ref:`howto-filtering`: every filter operator and how it compiles on each backend
- :ref:`howto-web-api`: running queries inside a FastAPI service
