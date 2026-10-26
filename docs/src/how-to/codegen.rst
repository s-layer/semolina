.. _howto-codegen:

How to generate Semolina model classes from warehouse views
============================================================

In :ref:`tutorial-warehouse-models` you ran ``semolina codegen`` once. This page covers
the rest of the command: the backends it reads, the files it writes, and the ``--check``
run that compares a committed model with the warehouse.

``semolina codegen`` introspects a Snowflake or DuckDB semantic view, or a Databricks
metric view, and prints a Python model class to stdout.

.. tip:: Looking for a result DTO rather than a model class?

   ``semolina codegen`` writes the :py:class:`~semolina.models.SemanticView` class that
   describes a whole view. To generate a Pydantic DTO typed for the columns one *query*
   returns, use ``semolina codegen-dto`` instead. See :ref:`howto-dto-codegen`.

Run codegen
-----------

The command needs the ``cli`` extra, along with the extra for your warehouse's driver:

.. code-block:: bash

   pip install "semolina[cli,snowflake]"

.. code-block:: bash

   semolina codegen my_schema.sales_view --backend snowflake

That connects to your warehouse, reads the view's column metadata, and prints a
:py:class:`~semolina.models.SemanticView` subclass.

Introspect multiple views at once
---------------------------------

Pass multiple view names in a single call:

.. code-block:: bash

   semolina codegen schema.sales_view schema.orders_view --backend databricks

All classes appear in one output block with a single shared imports section.

Write the output to a file
--------------------------

Pass ``--output`` (or ``-o``) to write the module to a file:

.. code-block:: bash

   semolina codegen my_schema.sales_view --backend snowflake --output myapp/models.py

Prefer it over redirecting stdout when you regenerate a committed file. The shell
truncates ``models.py`` as soon as you type ``> models.py``, so a run that fails leaves
you with an empty file. ``--output`` writes nothing until every view has been described
and rendered, then replaces the file in one step. A failed run leaves your committed
model as it was.

Replacing the file gives it a new inode. It keeps its permissions, but you become its
owner and any ACL or extended attribute on the old file is gone. If the directory won't let
codegen create a file beside it, as in a locked checkout or a single bind-mounted file,
codegen writes the existing file in place instead, without the all-or-nothing guarantee.

The directory must already exist. Codegen checks it before connecting to the warehouse
and exits ``2`` if it doesn't. On success, stdout stays empty and a ``Wrote:`` line on
stderr names the file.

Redirecting stdout still works, and gives you the same bytes:

.. code-block:: bash

   semolina codegen my_schema.sales_view --backend snowflake > models.py

Format the generated output
---------------------------

By default ``semolina codegen`` prints valid but unformatted Python. Install the
optional ``codegen-lint`` extra and codegen runs the generated source through ruff,
formatting it and sorting its imports, before printing:

.. code-block:: bash

   pip install "semolina[codegen-lint]"
   # or
   uv add "semolina[codegen-lint]"

Without the extra, codegen still prints the model source to stdout and adds a short
reminder on stderr. The reminder stays out of stdout, so redirecting to a file
(``> models.py``) captures only the Python.

With the extra installed, a ruff failure is an error: codegen prints ruff's message on
stderr, writes nothing to stdout and exits ``1``, rather than handing you source that
``ruff format --check`` would reject.

ruff runs with its default settings (line length 88, double quotes), not your project's
``[tool.ruff]`` config, so the output is the same whichever directory you run from. If
your own pre-commit runs ``ruff format --check`` with a different line length or quote
style, exclude the generated file from it rather than reformatting it by hand. To check
the file in CI, run ``ruff format --check --isolated models.py``; without ``--isolated``,
ruff applies your project's settings.

Choose a backend
----------------

Use ``--backend`` (or ``-b``):

.. list-table::
   :header-rows: 1

   * - Value
     - Warehouse
     - Introspects via
   * - ``snowflake``
     - Snowflake semantic views
     - ``SHOW COLUMNS IN VIEW``, then ``DESCRIBE SEMANTIC VIEW`` and one
       ``TAG_REFERENCES`` query for the metadata
   * - ``databricks``
     - Databricks metric views
     - ``DESCRIBE TABLE EXTENDED AS JSON``, then the catalog's
       ``information_schema.column_tags``
   * - ``duckdb``
     - DuckDB semantic views
     - ``DESCRIBE SEMANTIC VIEW``
   * - ``dotted.path.ClassName``
     - Your own engine
     - Its ``introspect()``

A custom engine is a subclass of :py:class:`~semolina.engines.base.Engine` that codegen
builds with no arguments. Its ``__init__`` takes none, makes its own pool and dialect, and
passes them to ``super().__init__(pool=..., dialect=...)``. Its ``introspect()`` returns an
:py:class:`~semolina.introspection.IntrospectedView` built from
:py:class:`~semolina.introspection.IntrospectedField` values, both imported from
``semolina.introspection``:

.. code-block:: python

   from adbc_poolhouse import DuckDBConfig, create_pool

   from semolina import Engine, SemolinaViewNotFoundError
   from semolina.engines.sql import DuckDBDialect
   from semolina.introspection import (
       IntrospectedField,
       IntrospectedView,
   )


   class CatalogueEngine(Engine):
       def __init__(self) -> None:
           super().__init__(
               pool=create_pool(
                   DuckDBConfig(database=":memory:")
               ),
               dialect=DuckDBDialect(),
           )

       def introspect(
           self, view_name: str
       ) -> IntrospectedView:
           if view_name != "sales":
               raise SemolinaViewNotFoundError(
                   f"No view named {view_name!r}"
               )
           return IntrospectedView(
               view_name="sales",
               class_name="Sales",
               fields=[
                   IntrospectedField(
                       "revenue", "metric", "int"
                   ),
                   IntrospectedField(
                       "country", "dimension", "str"
                   ),
               ],
           )

An ``IntrospectedField`` can also carry ``description``, ``label``, ``synonyms`` and
``semantic_format``, which codegen writes as the field's arguments, and an ``IntrospectedView`` a
``description`` for the class. ``--check`` compares only the ones your engine lists in
:py:attr:`~semolina.engines.base.Engine.introspected_metadata`, which is empty by
default, so declare what your ``introspect()`` fills:

.. code-block:: python

   class CatalogueEngine(Engine):
       introspected_metadata = frozenset(
           {"description", "synonyms"}
       )

Save that as ``my_engines.py`` and pass ``--backend my_engines.CatalogueEngine``. Codegen
adds the working directory to the end of ``sys.path`` before importing it, so a module
beside you resolves without being installed, and an installed package of the same name
still wins.

Raise the errors :py:meth:`Engine.introspect() <semolina.engines.base.Engine.introspect>`
lists and codegen exits with the matching code below:
:py:exc:`~semolina.exceptions.SemolinaViewNotFoundError` exits ``3``,
:py:exc:`~semolina.exceptions.SemolinaConnectionError` exits ``4``, and
:py:exc:`~semolina.exceptions.SemolinaIntrospectionError`, for a view the warehouse found
but couldn't describe, exits ``6``.

Credentials come from the ``[connections.<backend>]`` section of
``.semolina.toml``, then from prefixed environment variables (for example
``SNOWFLAKE_ACCOUNT``), then from a ``.env`` file. For DuckDB, pass the database
path with ``--database`` (or set ``DUCKDB_DATABASE``). See
:ref:`howto-codegen-credentials` for the full list of environment variables,
``.env`` file setup, and config file fallback.

.. warning:: Codegen reads a different section from ``create_engine()``

   :py:func:`~semolina.config.create_engine` defaults to ``[connections.default]``.
   ``semolina codegen`` instead reads the section **named after the backend**:
   ``--backend snowflake`` reads ``[connections.snowflake]`` and ``--backend databricks``
   reads ``[connections.databricks]``.

   ``--backend duckdb`` reads no TOML section at all. It takes the database path from
   ``--database`` or ``DUCKDB_DATABASE`` and fails if neither is set, so a ``.semolina.toml``
   makes no difference to it either way.

   So for Snowflake or Databricks, a file with only ``[connections.default]`` works for your
   application and makes codegen exit ``2``. Add a section under the backend name as well, or
   set the ``SNOWFLAKE_*`` / ``DATABRICKS_*`` environment variables. See
   :ref:`howto-codegen-credentials`.


Point DuckDB codegen at a database file
---------------------------------------

The DuckDB backend reads a database file on disk, so ``--backend duckdb`` needs a
``--database`` path. You can write that path three ways:

.. code-block:: bash

   semolina codegen sales_view --backend duckdb --database /data/sales.duckdb
   semolina codegen sales_view --backend duckdb --database ./sales.duckdb
   semolina codegen sales_view --backend duckdb --database ~/data/sales.duckdb

A relative path resolves against your current working directory, and a leading
``~`` expands to your home directory. Setting ``DUCKDB_DATABASE`` accepts the same
forms, so you can keep the path out of the command line:

.. code-block:: bash

   export DUCKDB_DATABASE=~/data/sales.duckdb
   semolina codegen sales_view --backend duckdb

Codegen has no in-memory default for DuckDB. If you supply neither ``--database``
nor ``DUCKDB_DATABASE``, the command exits ``2`` and tells you to set one.

The first run installs the ``semantic_views`` community extension onto the codegen
connection, which needs one-time network access to ``community.duckdb.org``. DuckDB
caches the extension under ``~/.duckdb/extensions/``, so later runs work offline.

Understand the generated output
--------------------------------

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      Given this semantic view in your warehouse:

      .. code-block:: sql

         CREATE OR REPLACE SEMANTIC VIEW analytics.sales_view
           TABLES (
             s AS source_table PRIMARY KEY (id)
           )
           FACTS (
             s.unit_price AS unit_price
           )
           DIMENSIONS (
             s.country AS country
           )
           METRICS (
             s.revenue AS SUM(s.revenue)
           )
         ;

      Running:

      .. code-block:: bash

         semolina codegen analytics.sales_view --backend snowflake

      Produces:

      .. code-block:: python

         import decimal

         from semolina import Dimension, Fact, Metric, SemanticView


         class SalesView(SemanticView, view="analytics.sales_view"):
             # {"type": "FIXED", "scale": 0}
             revenue = Metric[decimal.Decimal | None]()
             country = Dimension[str]()
             unit_price = Fact[float]()

   .. tab-item:: Databricks
      :sync: databricks

      Given this metric view in your warehouse:

      .. code-block:: sql

         CREATE OR REPLACE VIEW main.analytics.orders_view
           WITH METRICS
           LANGUAGE YAML
           AS $$
             version: 1.1
             source: source_table
             dimensions:
               - name: region
                 expr: region
             measures:
               - name: total_orders
                 expr: COUNT(*)
           $$;

      Running:

      .. code-block:: bash

         semolina codegen main.analytics.orders_view --backend databricks

      Produces:

      .. code-block:: python

         from semolina import Dimension, Fact, Metric, SemanticView


         class OrdersView(
             SemanticView, view="main.analytics.orders_view"
         ):
             total_orders = Metric[int | None]()
             region = Dimension[str]()

   .. tab-item:: DuckDB
      :sync: duckdb

      Given this semantic view in your DuckDB database:

      .. code-block:: sql

         CREATE SEMANTIC VIEW sales_view AS
         TABLES (s AS sales_data PRIMARY KEY (id))
         FACTS (
             s.unit_price AS unit_price
         )
         DIMENSIONS (
             s.country AS country,
             s.region AS region
         )
         METRICS (
             s.revenue AS SUM(s.revenue),
             s.cost AS SUM(s.cost)
         );

      Running:

      .. code-block:: bash

         semolina codegen sales_view --backend duckdb --database ./sales.duckdb

      Produces:

      .. code-block:: python

         import decimal

         from semolina import Dimension, Fact, Metric, SemanticView


         class SalesView(SemanticView, view="sales_view"):
             unit_price = Fact[int]()
             country = Dimension[str]()
             region = Dimension[str]()
             # HUGEINT
             revenue = Metric[decimal.Decimal | None]()
             # HUGEINT
             cost = Metric[decimal.Decimal | None]()

      DuckDB types a ``SUM`` over an ``INTEGER`` column as ``HUGEINT``, a 128-bit integer
      with no Python ``int`` counterpart in Arrow, so it arrives as a ``decimal.Decimal``
      and codegen notes the source type above the field. Cast the sum in the view, as in
      ``CAST(SUM(s.revenue) AS BIGINT)``, if you want an ``int``.

These views carry no comments or synonyms, so each field is a bare constructor call. Where
the warehouse records a comment, synonyms, a display name or ``S_LAYER_*`` tags, codegen
writes them as the field's ``description=``, ``synonyms=``, ``label=`` and
``semantic_format=`` arguments, one per line, and the view's own comment as the class's
``description=``. :ref:`howto-models-metadata` lists what each backend records.

Every column gets a concrete field type. Codegen reads the role each backend
records for the column and emits the matching :py:class:`~semolina.fields.Metric`,
:py:class:`~semolina.fields.Dimension`, or :py:class:`~semolina.fields.Fact`. None of
the backends leave a column unclassified, so you never get a bare ``Field()``
placeholder for a known role.

.. note::

   Databricks metric views model only two roles: measures and dimensions. There
   is no Fact concept, so every non-measure column maps to ``Dimension()``. This
   is intentional, not a missing feature. Snowflake and DuckDB semantic views
   support all three roles (``METRIC``, ``DIMENSION``, ``FACT``), and codegen
   maps each one directly.

Understand field type mapping
-----------------------------

Codegen resolves each backend's native role string to a field type:

.. list-table::
   :header-rows: 1

   * - Warehouse classification
     - Generated field type
   * - Metric / Measure
     - ``Metric[T | None]()``
   * - Dimension
     - ``Dimension[T]()``
   * - Fact (Snowflake and DuckDB)
     - ``Fact[T]()``

Only metrics admit ``None``. A metric is computed over a group, and a group with
nothing to aggregate in it yields a null, so every backend's metric annotation is
optional. Dimensions and facts are columns, and codegen
annotates them with the type the warehouse reports. See
:ref:`explanation-type-fidelity` for the three null cases and why ``COUNT`` is
treated the same way as ``SUM``.

If a backend ever hands back a role string that codegen does not recognize,
generation stops with a ``ValueError`` instead of guessing. A new warehouse
version or a schema change could introduce a role the mapping above doesn't
cover, and silently labelling that column a ``Dimension`` would hide the drift
in your generated model. You find out at codegen time, not when a query returns
the wrong shape.

Handle TODO comments
--------------------

When a field's SQL type has no clean Python equivalent (GEOGRAPHY, ARRAY, MAP,
STRUCT), codegen types the field as ``Any`` and drops the raw warehouse type into a
TODO comment rather than guessing:

.. code-block:: python

   # TODO: {"type": "GEOGRAPHY"}
   territory = Dimension[Any]()

The comment carries the warehouse's own type descriptor verbatim, so you have the
detail you need to pick a concrete type. ``Any`` keeps the generated module valid in
the meantime; codegen adds ``from typing import Any`` for you whenever a field needs it.

Review these fields after generation and replace ``Any`` with the type you want.

Read a VARIANT column's annotation
----------------------------------

Codegen annotates a ``VARIANT`` column
:py:obj:`~semolina.types.JsonValue`, a recursive union over the whole JSON value domain
(``str | int | float | bool | None``, plus lists and string-keyed dicts of the
same), and adds ``JsonValue`` to the ``from semolina import ...`` line for you:

.. code-block:: python

   from semolina import (
       Dimension,
       Fact,
       JsonValue,
       Metric,
       SemanticView,
   )


   class Events(SemanticView, view="events_view"):
       # VARIANT
       payload = Dimension[JsonValue]()

The union is deliberately loose, because what a driver hands over for a semi-structured
column is not settled. On Databricks a ``variant`` value arrives as JSON **text**, so
you get a ``str`` and parse it yourself rather than a ready-made ``dict``. ``JsonValue``
is correct either way, which is why it is a union rather than ``dict[str, Any]``.

Read the raw warehouse type from a field comment
------------------------------------------------

The ``TODO:`` comment above is not the only comment codegen writes. Whenever a
field's annotation does not name its warehouse type, codegen emits that type on
its own line above the field, so the detail survives even though the annotation
is concrete:

.. code-block:: python

   # DECIMAL(10,2)
   max_order_value = Metric[decimal.Decimal | None]()

   # {"type": "FIXED", "scale": 0}
   revenue = Metric[decimal.Decimal | None]()

A ``DECIMAL(10,2)`` and a ``DECIMAL(38,2)`` both annotate
:py:class:`decimal.Decimal`, so without the comment the precision and scale would
be gone from your model. The same applies to a ``UUID``, a ``JSON`` column, or an
``ENUM``: each annotates ``str``, because a ``str`` is what the driver hands over,
and the comment is where the original type name lives.

.. _howto-codegen-check:

Check a committed model for drift
---------------------------------

Once a model is in your codebase, ``--check`` reports whether its annotations still
describe what the warehouse returns:

.. code-block:: bash

   semolina codegen sales_view \
       --check --output path/to/models.py \
       --backend duckdb --database ./analytics.db

With ``--check``, ``--output`` names the committed file to read rather than one to write.
That way the same options check exactly the file a generation run would replace. Older
scripts that pass ``--model`` instead still work. ``--check`` with neither, or with both,
exits ``2``. Credentials come from the environment exactly as they do for generation, so
nothing secret belongs on this command line.

The run writes nothing to stdout. One table per view goes to stderr:

.. code-block:: text

   semolina codegen --check: sales_view
   ┏━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━┓
   ┃ Field      ┃ Committed         ┃ Probed (result    ┃ Route          ┃ Status ┃
   ┃            ┃                   ┃ schema)           ┃                ┃        ┃
   ┡━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━┩
   │ unit_price │ int               │ int               │ execute-schema │ match  │
   │ country    │ str               │ str               │ execute-schema │ match  │
   │ region     │ str               │ str               │ execute-schema │ match  │
   │ revenue    │ int | None        │ decimal.Decimal | │ execute-schema │ drift  │
   │            │                   │ None              │                │        │
   │ cost       │ decimal.Decimal | │ decimal.Decimal | │ execute-schema │ match  │
   │            │ None              │ None              │                │        │
   └────────────┴───────────────────┴───────────────────┴────────────────┴────────┘

The comparison is per field, and the right-hand side is the **result schema** of a
query against the view: the types the warehouse says it would return. It is not a
regenerated model diffed against yours, so reformatting your file, reordering fields,
or renaming the class changes nothing. A row moves to ``drift`` when its annotation names
a different type, when the field's role, column or metadata changed (see
`Read the Detail lines`_), or when the field is declared on one side only.

No row of your data is fetched. The check reads the view's catalogue entry and asks
the warehouse to type a query, which is why it is cheap enough to put in CI.

Read the route on every row
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``Route`` column names where each probed type came from, so a green check can
never quietly mean "I could not ask, so I compared against the catalogue instead":

.. list-table::
   :header-rows: 1

   * - Route
     - What happened
   * - ``execute-schema``
     - The driver answered a describe-only call. No query ran.
   * - ``zero-row``
     - The driver has no describe-only call, so the query ran wrapped to return no rows.
   * - ``metadata``
     - Neither route was open, or the probe ran but no result column answered for this
       field, so the annotation was compared against the warehouse catalogue. A note on
       stderr says which.
   * - ``not-probed``
     - Your model declares a field the view does not have, so no query looked at it.
       The ``Probed`` column reads ``(absent)``.

A ``metadata`` row is a weaker answer than the other two, because the catalogue is
the source ``codegen`` already used. Comparing a model against the source that wrote
it can only ever agree with itself. That's why a probe that failed makes the run exit
``6`` rather than ``0`` or ``5``: the check you asked for didn't happen. A probe that ran
but carried no column for one field doesn't change the exit code on its own; the note
names the fields that fell back, so you can see which rows to distrust.

A view that needs more than one query gets a route per query, not one for the run:
DuckDB cannot select facts and metrics in a single ``semantic_view()`` call, and a
driver is free to answer one query and refuse the other.

Read the Detail lines
~~~~~~~~~~~~~~~~~~~~~

Some drift does not show up in the ``Committed`` and ``Probed`` columns, because
both sides still read the same annotation:

- the field's **role** changed. A ``Metric`` committed as a ``Dimension`` lands in the
  wrong ``semantic_view()`` clause.
- the field's **column** changed. A ``source=`` naming a column the warehouse has since
  renamed makes every query select something that is not there.
- the field's **metadata** changed. Its ``description=``, ``label=``, ``synonyms=`` or
  ``semantic_format=`` no longer match what the warehouse records (see
  :ref:`howto-models-metadata`).

Each moves the row to ``drift`` and prints a line under the table naming what moved:

.. code-block:: text

   Detail: revenue: role: committed Dimension, warehouse Metric
   Detail: country: column: committed 'OLD_NAME', warehouse 'NEW_NAME'
   Detail: revenue: synonyms: committed ('sales',), warehouse ('sales', 'turnover')

Only what the backend records is compared (see :ref:`howto-models-metadata`). Semolina
reads no label or format on DuckDB, so a ``label=`` or ``semantic_format=`` you added
there is never drift. Formats compare by meaning rather than by spelling. Synonyms compare
as a set, so their order doesn't matter. The model's own ``description=`` is compared
with the view's comment, and a difference prints as ``Detail: (view): description: ...``
and makes the run exit ``5`` even when every row matches.

The column comparison is on the name the query will use, so adding
``source="COUNTRY"`` to a field the dialect already resolves to ``COUNTRY`` is not drift.

A ``Detail`` line also appears when the result schema came back carrying two columns of
the same name. That one is about the warehouse rather than your model: ``--check`` cannot
say which column the field is, so it reports ``Any`` and tells you why.

Edit the committed file by hand
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A generated model is yours to edit, and ``--check`` reads your edits the way Python would.
It parses the file and never imports it, so no code in it runs, but it does resolve each
name through the file's own imports:

- Annotations are compared by the type they name. ``Optional[Decimal]`` after
  ``from decimal import Decimal``, ``None | decimal.Decimal`` and the quoted
  ``'decimal.Decimal | None'`` all match the ``decimal.Decimal | None`` codegen writes.
  The table still shows your spelling.
- A field can be declared as ``semolina.Metric[T]()``, as ``M[T]()`` after
  ``from semolina import Metric as M``, or as ``revenue: Metric[T] = Metric[T]()``.
- ``view=`` can name a string constant, in the same file or imported from another module.
- Fields inherited from a base class count as the subclass's own, whether the base sits
  in the same file or in another module of your project.
- Metadata arguments (``label=``, ``description=``, ``synonyms=``,
  ``semantic_format=``, and the class's ``description=``) are read as literal values or,
  for a string, as a constant by name. A list or set of synonyms reads the same as a
  tuple. An argument the reader can't evaluate, such as a function call, is named in a
  warning and left out of the comparison.
- An attribute you renamed reads the same column if its ``source=`` says so:
  ``sales_region = Dimension[str](source="region")`` answers for the warehouse's
  ``region``, and a ``Detail`` line names the attribute.
- The view name you pass matches ``view=`` the way your warehouse compares identifiers.
  On DuckDB, ``SALES_VIEW``, ``"sales_view"`` and ``main.sales_view`` all find
  ``view="sales_view"``. On Snowflake, ``sales_view`` and ``"SALES_VIEW"`` match, but
  ``"Sales_View"`` is a different view. On Databricks, case and backticks don't matter.
- If two classes in the file declare the same view, each one is checked, and each table
  names its class in the title.

Modules the file imports from are parsed too, as long as they live under the file's
package root or the directory you run the command from. Nothing outside those is read,
so a base class from an installed package is not followed. The package root is found
through ``__init__.py`` files, so for a namespace package (one without them), run the
command from the directory that contains the package.

Anything ``--check`` can't read is printed as a warning before the tables, naming the class,
the line and the statement:

.. code-block:: text

   Warning: models.py: SalesView (line 12): 'if FEATURE_ON:' declares discount in a
   statement the reader does not read; declare each field as `name = Metric[T]()`
   directly in the class body

A field skipped that way reads as absent, so the warning tells you why its row drifted.
Warnings cover a field declared inside an ``if`` block or a tuple assignment, a
constructor whose name doesn't resolve to ``Metric``, ``Dimension`` or ``Fact``, a
``source=`` or ``view=`` that isn't a string, a metadata argument the reader can't
evaluate, and a base class that can't be found.

Know what ``--check`` does not catch
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

These are deliberate limits rather than bugs:

- **The probe is always unfiltered.** ``--check`` types the query with every field and no
  filter, because Snowflake won't describe a query that carries a bound parameter. A
  filter shouldn't change a column's type, but the check says nothing about one.
- **Only the modules codegen writes from are compared by meaning.** Names from the
  builtins, ``typing``, ``typing_extensions``, ``collections.abc``, ``datetime``,
  ``decimal``, ``uuid``, ``semolina`` and ``pydantic`` resolve. Anything else, such as a
  ``Money`` class of your own or ``pathlib.Path``, is compared with codegen's text as
  written, so a different spelling of it drifts.
- **A field must be declared with its own constructor call.** ``region = Dimension[str]()``
  is read. ``region = SHARED_REGION`` or ``region = Base.region``, which reuse a field
  object from elsewhere, are read as plain attributes rather than fields. Such a field
  reports as absent, with no warning, and it also hides an inherited field of the same
  name.

Interpret the exit code
~~~~~~~~~~~~~~~~~~~~~~~

``0`` means every field matched. ``5`` means at least one drifted: an annotation, a
role, a column or a metadata argument, or the model's own ``description=``. ``6`` means the
result-schema probe failed for a view and its rows fell back to the catalogue, so treat
the run as not checked. ``6`` wins over ``5``; when another view's probe did run and
found drift, stderr names that view so the drift isn't lost. A committed file that
doesn't exist exits ``2`` before codegen connects, and one that doesn't parse exits
``1``. Drift has its own code so a CI job can fail on a stale model without also failing
on a crash.

An untyped ``Metric()`` matches a column the type maps can't type either, which
codegen writes as ``Metric[Any | None]()``. Against a column that does have a type it
still drifts, because codegen would now write that type.

Read the drift before you act on it. ``semolina codegen`` builds a model from
the catalogue while ``--check`` prefers the result schema, so the two can disagree on
a model that was generated moments earlier. See :ref:`explanation-type-fidelity` for
why the two sources differ and which one to believe.

.. warning:: ``--check`` on Databricks

   ``--check`` is confirmed against Snowflake and DuckDB. On Databricks it is
   **unverified**, because the command itself has not been run end to end against a
   live metric view. Treat a Databricks ``--check`` result as unconfirmed in either
   direction for now.

   It also reports drift on every Databricks VARIANT column. The two routes disagree on
   that one type, and the disagreement is expected.
   ``semolina codegen`` reads the catalogue and annotates a VARIANT
   :py:obj:`semolina.JsonValue <semolina.types.JsonValue>`; ``--check`` reads the result
   schema, where the Foundry ADBC driver delivers an Arrow string, and resolves ``str``.
   So a **correct** committed model reports drift, and exit ``5`` on a Databricks model
   carrying a VARIANT column is a false positive rather than a stale annotation.

   The generated annotation is the sound one and is not being narrowed: the string the
   driver returns is a member of the ``JsonValue`` union rather than a contradiction of
   it, and a VARIANT can hold either shape. If you wire ``--check`` into CI
   against Databricks, exclude models with VARIANT columns until the comparison learns
   that a probed ``str`` satisfies a declared ``JsonValue``.

Exit codes
----------

``semolina codegen`` uses distinct exit codes so scripts can handle each failure mode separately:

.. list-table::
   :header-rows: 1

   * - Exit code
     - Meaning
   * - ``0``
     - Success -- model class written to stdout or to the ``--output`` file
   * - ``1``
     - Unexpected error (see stderr for details), including an unparseable
       committed file, generated source that does not parse, an installed ruff
       that fails on it, or ``S_LAYER_*`` tags that don't make a valid format for
       their member (see :ref:`howto-models-metadata`). Nothing is written to stdout
   * - ``2``
     - Invalid option -- an unrecognized or omitted ``--backend``, connection
       config that could not be assembled (the message names the fields, never
       their values), a backend whose driver is not installed, a
       ``dotted.path.ClassName`` that cannot be built with no arguments,
       ``--backend duckdb`` with no database path, an ``--output`` directory that
       does not exist, ``--check`` with no file to read, a file that does not exist,
       or both ``--output`` and ``--model``, ``--model`` without ``--check``, a
       custom engine's class name that is not a valid Python identifier or is a name
       the generated module imports (``Metric``, ``SemanticView``, ``Any``), or two
       views that would generate the same class name
   * - ``3``
     - View not found -- the warehouse has no semantic view with that name
   * - ``4``
     - Connection failure -- the warehouse rejected the credentials or could not be
       reached, or the DuckDB database file is missing or not a DuckDB file
       (:py:exc:`~semolina.exceptions.SemolinaConnectionError`). Credentials that are
       missing altogether exit ``2``
   * - ``5``
     - Drift -- a committed model no longer matches the warehouse: an annotation, a
       role, a column or a metadata argument (see `Read the Detail lines`_)
   * - ``6``
     - The warehouse was reached but could not describe a view, for example because a
       column its definition reads has been dropped, or it refused the lookup of the
       view's ``S_LAYER_*`` tags
       (:py:exc:`~semolina.exceptions.SemolinaIntrospectionError`), or a custom engine
       let a raw driver error through. ``codegen-dto`` uses the same code. Under
       ``--check``, also: the result-schema probe failed and at least one row was
       compared against warehouse metadata instead, so the check did not run as
       advertised. It takes precedence over ``5``

``--backend`` omitted entirely also exits ``2``, from the argument parser, so a script
cannot tell a missing backend from a rejected value or a bad flag pairing. Read stderr for
which option it was.

Override the SQL column name with source=
-----------------------------------------

By default, Semolina maps Python field names to SQL column names using each dialect's
identifier casing rules (Snowflake uppercases unquoted identifiers; Databricks lowercases them).
For a field ``order_id``, Snowflake resolves ``ORDER_ID`` automatically.

If your warehouse stores a column with non-default casing, for example a quoted
lowercase column ``"order_id"`` in Snowflake, you can override the SQL column name
with ``source=``:

.. code-block:: python

   class Orders(SemanticView, view="orders"):
       order_id = Dimension[int](
           source="order_id"
       )  # maps to quoted "order_id", not "ORDER_ID"

``semolina codegen`` emits ``source=`` automatically when introspection detects that a column
uses non-default casing.

Read the class names codegen generates
--------------------------------------

Each view becomes one class, named after the view's last segment in PascalCase:
``analytics.sales_view`` gives ``SalesView``, and a quoted ``analytics."Order Book"`` gives
``OrderBook``. A name that Python couldn't use as is gets a ``View`` prefix. That covers a name
starting with a digit (``2024_orders`` gives ``View2024Orders``), a keyword (``none`` gives
``ViewNone``) and a name the generated file imports. A view called ``metric`` gives
``ViewMetric``, since a class named ``Metric`` would hide the imported ``Metric`` from every
class after it. The same goes for ``fact``, ``dimension``, ``semantic_view``, ``json_value``
and ``any``.

Two views that end up with the same class name, like ``emea.sales`` and ``apac.sales``, exit
``2``. Generate them into separate files.

Read the names codegen gives awkward columns
--------------------------------------------

A warehouse column name doesn't have to be a Python identifier, but a field name does. When a
column is called ``what?``, ``Order Date`` or ``class``, codegen picks a valid field name and
keeps the column's exact spelling in ``source=``, so queries still select the right column:

.. code-block:: python

   class Orders(SemanticView, view="orders"):
       what_ = Dimension[str](source="what?")
       order_date = Dimension[datetime.date](
           source="Order Date"
       )
       class_ = Dimension[str](source="class")

The same rule applies on every backend, after the usual lower-casing on Snowflake and
Databricks:

- Each character that can't appear in an identifier becomes ``_``: ``order date`` gives
  ``order_date``.
- A Python keyword or soft keyword (``class``, ``match``, ``type``) gets a trailing ``_``. So
  does a name the query builder reserves (``query``, ``limit``, ``keys``), an attribute of
  :py:class:`~semolina.models.SemanticView` itself, and any name the generated file uses: its
  imports (``datetime``, ``decimal``, ``Dimension``, ``Any``) and the builtins in its
  annotations (``int``, ``str``). A field called ``datetime`` would hide the module from every
  field after it.
- A name starting with a digit gets a leading ``_``: ``1st`` gives ``_1st``. Two or more
  leading underscores shrink to one, which keeps Python from name-mangling the attribute.
- A name with no letter or digit left, such as ``?``, becomes ``column``.
- When two columns end up with the same name, the second gets ``_2``, the next ``_3``, in
  column order. A column whose name was already valid keeps it, so ``a b`` next to ``a_b``
  gives ``a_b_2`` and ``a_b``. Names that differ only in case count as the same, because
  the warehouse reads ``Order_Date`` and ``order_date`` as one identifier: on DuckDB,
  ``Order Date`` next to ``order_date`` gives ``Order_Date_2`` and ``order_date``.

Running codegen again on the same view gives the same names, so ``--check`` compares like with
like. You can rename a generated field to anything you prefer, as long as you keep its
``source=``.

.. warning::

   A field whose name starts with ``_``, like ``_1st``, can't become a field of a DTO from
   ``semolina codegen-dto``, because pydantic reserves leading underscores. Rename the model
   field (``first = Dimension[str](source="1st")``) if you plan to generate DTOs from it.

See also
--------

- :ref:`tutorial-warehouse-models` -- running this command and ``semolina codegen-dto``
  in one sitting
- :ref:`howto-dto-codegen` -- generating a result DTO for one query instead of a
  model class for a whole view
- :ref:`explanation-type-fidelity` -- why the catalogue and the result schema
  disagree, and what a generated annotation promises
- :ref:`howto-codegen-credentials` -- environment variables, .env files, and config file fallback
- :ref:`reference-cli` -- every flag, argument, and exit code
- :ref:`howto-models` -- model class structure and field types
- :ref:`howto-backends` -- connection configuration for Snowflake, Databricks, and
  DuckDB
