.. _howto-models:

How to define models
====================

The ``Sales`` class in :ref:`tutorial-first-query` is about the smallest model that works.
A :py:class:`~semolina.models.SemanticView` subclass can declare rather more: three kinds
of field, a type subscript your IDE reads, and the descriptors the query builder sees.

Create a model
--------------

Subclass :py:class:`~semolina.models.SemanticView` and pass the warehouse view name via ``view=``:

.. code-block:: python

   from semolina import SemanticView, Metric, Dimension, Fact


   class Sales(SemanticView, view="sales"):
       revenue = Metric[float]()
       cost = Metric[float]()
       country = Dimension[str]()
       region = Dimension[str]()
       unit_price = Fact[float]()

The type parameter (``[float]``, ``[str]``, etc.) is covered under `Type the subscript`_.

.. note:: ``float`` is used for brevity here; codegen writes ``Decimal | None``

   The examples on this page annotate money metrics ``Metric[float]()`` to keep the
   focus on model structure. A real ``DECIMAL`` column arrives as a
   :py:class:`decimal.Decimal`, and ``semolina codegen`` writes
   ``Metric[decimal.Decimal | None]()`` for it: ``Decimal`` because that is the value
   you get, and ``| None`` because a metric over an empty group is null.

   This matters if you run ``semolina codegen --check``. That command compares the type
   your committed annotation names with the one the result schema implies, so a
   hand-written ``Metric[float]()`` on a decimal column is reported as drift. Prefer
   the annotation codegen would write for models you intend to check.

   ``.into(DTO)`` is a separate question and behaves differently: a hand-written
   ``float`` there is honoured as a deliberate narrowing under ``validate=True``. See
   :ref:`howto-typed-results`.

The ``view=`` parameter is **required** -- it identifies the semantic view in your warehouse.
Omitting it raises a ``TypeError`` at class creation time. The one exception is a base class
that only shares fields; see `Share fields between models`_.

Name a view in another schema
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If your view lives in a specific schema, pass the schema-qualified name:

.. code-block:: python

   class Sales(
       SemanticView,
       view="analytics.sales",
   ):
       revenue = Metric[float]()

For Databricks Unity Catalog, use three-part names:

.. code-block:: python

   class Sales(
       SemanticView,
       view="catalog.schema.sales",
   ):
       revenue = Metric[float]()

.. note:: How the view name is quoted

   Semolina folds the view name to your warehouse's default identifier case --
   the same folding it applies to field names: **uppercase** on Snowflake,
   **lowercase** on Databricks and DuckDB. So ``view="sales"`` resolves against a
   view created the ordinary (unquoted) way, whatever case you write here.
   Schema-qualified names are split on ``.`` and quoted per part, so on Snowflake
   ``view="analytics.sales"`` becomes ``"ANALYTICS"."SALES"``.

   If a view was created with a quoted, case-sensitive name, quote that segment
   yourself to keep it verbatim:

   .. code-block:: python

      class Sales(
          SemanticView,
          view='analytics."Sales"',  # -> "ANALYTICS"."Sales"
      ):
          revenue = Metric[float]()

   Semolina escapes the name between the quotes for you, so a quote inside it can be
   written once, ``view='analytics."My"View"'``, or doubled the standard SQL way,
   ``view='analytics."My""View"'``. Both name the view ``My"View`` on Snowflake.

   On Databricks, quote the segment with backticks instead:
   ``view="analytics.`My View`"``. A backtick inside it is handled the same way, written
   once or doubled. On DuckDB, Semolina passes the view name to ``semantic_view()``
   exactly as you write it, and DuckDB matches names case-insensitively anyway, so
   none of this quoting applies there.

Choose field types
------------------

Each field type corresponds to a role in your semantic view query:

.. list-table::
   :header-rows: 1

   * - Field
     - Use for
     - Accepted by
   * - :py:class:`~semolina.fields.Metric`
     - Aggregated measures: revenue totals, counts, averages
     - ``.metrics()``
   * - :py:class:`~semolina.fields.Dimension`
     - Categorical grouping: country, product category, date
     - ``.dimensions()``
   * - :py:class:`~semolina.fields.Fact`
     - Raw event-level numeric columns (``unit_price``, ``quantity``). Signals
       semantic intent, as against a categorical ``Dimension``.
     - ``.dimensions()``

Metric fields
~~~~~~~~~~~~~

A :py:class:`~semolina.fields.Metric` represents an aggregatable measure. In generated SQL, metrics
are wrapped in the backend's aggregation function:

.. code-block:: python

   class Orders(SemanticView, view="orders"):
       total_revenue = Metric[float]()
       order_count = Metric[int]()

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         SELECT AGG("TOTAL_REVENUE") AS "total_revenue", AGG("ORDER_COUNT") AS "order_count"
         FROM "ORDERS"

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         SELECT MEASURE(`total_revenue`) AS `total_revenue`, MEASURE(`order_count`) AS `order_count`
         FROM `orders`

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         SELECT "total_revenue" AS "total_revenue", "order_count" AS "order_count"
         FROM semantic_view('orders', metrics := ['total_revenue', 'order_count'])

Passing a ``Metric`` to ``.dimensions()`` raises a ``TypeError``. Pass it to ``.metrics()`` instead.

Dimension fields
~~~~~~~~~~~~~~~~

A :py:class:`~semolina.fields.Dimension` represents a categorical attribute used for grouping:

.. code-block:: python

   class Orders(SemanticView, view="orders"):
       country = Dimension[str]()
       product_category = Dimension[str]()

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         SELECT "COUNTRY" AS "country", "PRODUCT_CATEGORY" AS "product_category"
         FROM "ORDERS"
         GROUP BY ALL

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         SELECT `country` AS `country`, `product_category` AS `product_category`
         FROM `orders`
         GROUP BY ALL

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         SELECT "country" AS "country", "product_category" AS "product_category"
         FROM semantic_view('orders', dimensions := ['country', 'product_category'])

Fact fields
~~~~~~~~~~~

A :py:class:`~semolina.fields.Fact` represents a raw numeric value that has not been pre-aggregated.

**Snowflake users:** a column declared in the ``FACTS`` clause of ``CREATE SEMANTIC VIEW``
reports ``kind=FACT`` when you introspect with ``SHOW COLUMNS IN VIEW``, and
``semolina codegen`` emits ``Fact()`` for it automatically. For hand-written models,
use ``Fact`` for raw numeric columns you want to distinguish semantically from categorical
dimensions.

**Databricks users:** Databricks metric views have no native fact concept. Every
non-aggregate field is a ``dimension:`` in the metric view YAML. Use ``Fact`` in your Semolina
model for raw numeric columns you want to distinguish semantically from categorical grouping
attributes -- the warehouse does not enforce the distinction, but your teammates and future
readers will see the intent.

On Snowflake and Databricks, ``Fact`` and ``Dimension`` produce identical SQL
(``SELECT "COL" AS "col" FROM ... GROUP BY ALL``), so the distinction is semantic rather
than a difference in execution. DuckDB's ``semantic_view()`` takes facts in their own
``facts :=`` argument and returns them row by row, so Semolina adds ``SELECT DISTINCT`` to
a fact query there. You get the same rows on all three: one per distinct combination of
values. DuckDB can't select facts and metrics in the same query, so Semolina raises
``ValueError`` if you pass a ``Fact`` to ``.dimensions()`` alongside ``.metrics()`` on a
DuckDB engine.

Default to ``Dimension``. Use ``Fact`` as an intentional opt-in for two situations:

1. **Semantic precision** -- the column is a raw event-level numeric (``unit_price``,
   ``quantity``, ``line_amount``) you want to distinguish from categorical attributes like
   ``country`` or ``product_category``.
2. **Snowflake alignment** -- ``semolina codegen`` introspected the column as ``kind=FACT`` from
   your warehouse. Match that designation in Semolina.

.. code-block:: python

   class Orders(SemanticView, view="orders"):
       # raw price column, not aggregated
       unit_price = Fact[float]()
       quantity = Fact[int]()
       # categorical grouping attribute
       country = Dimension[str]()
       product_category = Dimension[str]()

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: sql

         SELECT "UNIT_PRICE" AS "unit_price", "QUANTITY" AS "quantity"
         FROM "ORDERS"
         GROUP BY ALL

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: sql

         SELECT `unit_price` AS `unit_price`, `quantity` AS `quantity`
         FROM `orders`
         GROUP BY ALL

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: sql

         SELECT DISTINCT "unit_price" AS "unit_price", "quantity" AS "quantity"
         FROM semantic_view('orders', facts := ['unit_price', 'quantity'])

Type the subscript
------------------

Annotate each field with the Python type of the underlying warehouse column.
The subscript is optional but recommended -- it lets your IDE infer the type when you access
query results:

.. code-block:: python

   revenue = Metric[float]()
   order_id = Metric[int]()
   country = Dimension[str]()
   # date column; requires: import datetime
   created_at = Dimension[datetime.date]()

When the column type has no clean Python equivalent (GEOGRAPHY, ARRAY, STRUCT), use
``Metric[Any]()`` and import ``Any`` from ``typing``. The ``semolina codegen`` command emits a
``# TODO:`` comment for these cases. A ``VARIANT`` column takes
:py:obj:`~semolina.types.JsonValue`; see :ref:`howto-codegen`.

You can also omit the subscript entirely. ``Metric()`` is shorthand for ``Metric[Any]()`` --
valid Python, just without type narrowing.

Put it together
---------------

A complete model with all three field types:

.. code-block:: python

   from semolina import SemanticView, Metric, Dimension, Fact


   class Orders(SemanticView, view="orders"):
       """Order-level metrics and dimensions."""

       total_revenue = Metric[float]()
       order_count = Metric[int]()
       country = Dimension[str]()
       product_category = Dimension[str]()
       unit_price = Fact[float]()  # raw price, not aggregated

Access field descriptors
------------------------

Fields use Python's `descriptor protocol <https://docs.python.org/3/howto/descriptor.html>`_.
Accessing ``Orders.total_revenue`` at the class level returns the
:py:class:`~semolina.fields.Metric` descriptor itself -- the same object you pass into
query methods:

.. code-block:: python

   # Class-level access: returns the descriptor
   field = Orders.total_revenue
   print(type(field))
   # <class 'semolina.fields.Metric'>

   # Pass directly into query methods
   query = Orders.query().metrics(
       Orders.total_revenue,
       Orders.order_count,
   )

Accessing a field on an **instance** raises ``AttributeError``. Semolina models are never
instantiated -- the class is the query target.

Model immutability
------------------

Models are frozen immediately after the class body executes. Attempting to modify a
model attribute after class creation raises ``AttributeError``:

.. code-block:: python

   class Sales(SemanticView, view="sales"):
       revenue = Metric[float]()


   # AttributeError: Cannot modify after creation
   Sales.revenue = Metric[float]()

A model therefore cannot change shape while a query built from it is in flight.

Share fields between models
---------------------------

When several views have the same shape, declare the shared fields once on an
``abstract=True`` base and subclass it for each view:

.. code-block:: python

   class Commerce(SemanticView, abstract=True):
       revenue = Metric[decimal.Decimal | None]()
       country = Dimension[str]()


   class Sales(Commerce, view="sales"):
       pass


   class Returns(Commerce, view="returns"):
       refunds = Metric[decimal.Decimal | None]()

``Sales`` and ``Returns`` each have ``revenue`` and ``country``, and ``Returns`` adds
``refunds``. Each subclass gets its own copy of every inherited field, so
``Sales.query().metrics(Sales.revenue)`` reads from ``sales`` and never from another view.
Using ``Sales.revenue`` in a ``Returns`` query raises ``TypeError``, as mixing fields from
two unrelated models does.

An abstract model names no view, so it cannot be queried: ``Commerce.query()`` raises
``TypeError``. You can still list its fields with ``Commerce.metrics()`` and
``Commerce.dimensions()``. Passing ``view=`` together with ``abstract=True`` is refused, and
abstractness is not inherited, so each subclass needs its own ``view=``. Several abstract
bases combine the way mixins do, in method resolution order, and a plain class that is not
a model can contribute fields the same way.

You can also extend a concrete model. The subclass names its own view and inherits the
parent's fields; the parent is unchanged:

.. code-block:: python

   class SalesV2(Sales, view="sales_v2"):
       revenue = Metric[decimal.Decimal | None](
           source="net_revenue"
       )
       discount = Metric[decimal.Decimal | None]()

Redeclaring ``revenue`` replaces the inherited field. It keeps its place in
``SalesV2.metrics()``, ahead of ``discount``. To drop an inherited field, assign the name
something that is not a field, such as ``country = None``.

.. note:: ``codegen --check`` follows base classes

   ``semolina codegen --check`` counts an inherited field as the subclass's own, so
   ``SalesV2`` above is checked with ``country`` from ``Sales`` and its own ``revenue``
   in place of the inherited one. The base can sit in the same file or in another module
   of your project; ``--check`` parses that module without importing it. A base it can't
   find, such as one from an installed package, is named in a warning, and the fields it
   would have supplied report as absent. See :ref:`howto-codegen-check`.

.. _howto-models-metadata:

Describe your fields
--------------------

A field takes four optional keyword arguments that say what it is, and a model takes a
``description=`` for the view as a whole:

.. code-block:: python

   class Orders(
       SemanticView,
       view="orders",
       description="One row per order line, refunds excluded.",
   ):
       total_revenue = Metric[decimal.Decimal | None](
           label="Total revenue",
           description="Sum of revenue, tax excluded.",
           synonyms=("sales", "turnover"),
           semantic_format={
               "value": {"kind": "currency", "currency": "USD"}
           },
       )

``label`` is a display name, ``description`` is the prose a reader or an agent sees, and
``synonyms`` are other names people use for the field. ``synonyms`` takes any iterable of
strings except a bare string, which raises ``TypeError`` because it would read as one
synonym per character.

``semantic_format`` says what the values mean: a currency, a percentage, a date, an
identifier. It takes an S-LAYER block, either as a mapping or as an ``SLayerBlock`` from
`s-layer-semantic-formats <https://pypi.org/project/s-layer-semantic-formats/>`_, and
validates it when the class is defined. A block whose kind doesn't fit the field, such as
a currency on a ``Dimension``, raises ``ValueError``. So does a block that carries its own
``label`` or ``synonyms``: pass those as the field's arguments instead, so each one has a
single home.

Semolina doesn't change your queries based on any of these. They're there for whatever
reads your models: a BI front end, a docs page, an agent choosing which field to ask for.
Each one is an attribute of the field, and
:py:meth:`~semolina.models.SemanticView.metrics` and
:py:meth:`~semolina.models.SemanticView.dimensions` list a model's fields, so an endpoint
can serve them beside the data:

.. code-block:: python

   field_info = {
       field.name: {
           "label": field.label,
           "description": field.description,
           "synonyms": list(field.synonyms),
           "format": (
               field.semantic_format.model_dump(
                   mode="json", exclude_none=True
               )
               if field.semantic_format is not None
               else None
           ),
       }
       for field in Orders.metrics()
   }

An argument you didn't pass reads as ``None``, or ``()`` for ``synonyms``. A
``semantic_format`` comes back as an ``SLayerBlock``, which is a Pydantic model, so
``model_dump()`` turns it into JSON-ready data with the defaults filled in.

Where the warehouse holds them
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``semolina codegen`` reads what your warehouse records and writes it as these arguments:

.. list-table::
   :header-rows: 1

   * - Backend
     - ``description``
     - ``label``
     - ``synonyms``
     - ``semantic_format``
   * - Snowflake
     - the member's ``COMMENT``
     - the ``S_LAYER_LABEL`` tag
     - ``WITH SYNONYMS``
     - the ``S_LAYER_KIND`` and ``S_LAYER_SPEC`` tags
   * - Databricks
     - the column's ``comment``
     - ``display_name``
     - ``synonyms``
     - the ``S_LAYER_KIND`` and ``S_LAYER_SPEC`` column tags, else the native ``format``
   * - DuckDB
     - the member's ``COMMENT``
     - (not read)
     - ``WITH SYNONYMS``
     - (not read)

The view's own comment becomes the model's ``description=``.

The ``S_LAYER_*`` tags are the ones
`dbt-s-layer <https://github.com/s-layer/dbt-s-layer>`_ writes from a model's ``s_layer``
block. Semolina reads only a member's own tags, not ones it inherits from the view or a
logical table, and rebuilds the block from them. A tag that doesn't make a valid block for
that kind of member, such as a ``currency`` kind on a dimension, makes introspection
raise ``ValueError`` naming the member, rather than leaving the format out. The role
codegen connects as has to be allowed to read the tags: on Snowflake it calls
``INFORMATION_SCHEMA.TAG_REFERENCES`` in the view's database, and on Databricks it reads
the catalog's ``information_schema.column_tags``. If the warehouse refuses the tag lookup,
introspection raises :py:exc:`~semolina.exceptions.SemolinaIntrospectionError` saying so,
rather than reporting the view as missing.

A Databricks metric view written by hand has no ``S_LAYER_*`` tags, so a column without
them gets its ``semantic_format`` from the ``format`` in the view's YAML instead. The
format type gives the kind: ``number``, ``currency`` (with its ``currency_code``),
``percentage``, ``byte`` (``bytes``), ``date`` and ``date_time`` (``datetime``). Its
options give the display hints that have a slot: ``decimal_places`` of type ``exact`` or
``max``, ``abbreviation`` (``compact`` is ``compact: auto``), ``hide_group_separator`` and
``date_format``. ``scientific``, ``leading_zeros`` and ``time_format`` have no slot and
aren't read. A numeric format on a dimension, or a date format on a measure, has no
S-LAYER kind, so that column gets no ``semantic_format``. When a column has both, the tags
win, because they hold more than ``format`` can (a ``count`` rather than a ``number``, say).

Semolina reads no ``S_LAYER_*`` tags on DuckDB, so a model generated from a DuckDB view
never gets a ``label=`` or ``semantic_format=``. You can add them by hand.

The metadata travels from the warehouse to your model. Change a description in the
warehouse and regenerate, rather than editing the Python, since ``codegen --check``
reports committed metadata that no longer matches the warehouse as drift. Formats compare
by what they mean, so a ``semantic_format=`` that leaves out a default matches the block
the warehouse holds. Where the table says "not read", ``--check`` leaves your argument
alone. See :ref:`howto-codegen-check`.

A docstring you put under a field is yours: nothing reads it, and ``--check`` compares
only ``description=``. Models generated by earlier releases carry the warehouse's comment
as such a docstring and have no ``synonyms=`` or class ``description=``, so ``--check``
reports them as drifted wherever the warehouse records any of these. Regenerate them once
to clear it.

See also
--------

- :ref:`tutorial-first-query` -- the minimal model this page expands on
- :ref:`howto-queries` -- use your model to build and execute queries
- :ref:`howto-filtering` -- filter queries with field operators
- :ref:`howto-codegen` -- generate models from existing warehouse views
- :ref:`howto-codegen-check` -- check a committed model for drift from the warehouse
- :ref:`explanation-type-fidelity` -- which Python type each warehouse column arrives as
