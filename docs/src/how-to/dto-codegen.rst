.. _howto-dto-codegen:

How to generate a DTO class with ``semolina codegen-dto``
=========================================================

``semolina codegen-dto`` asks your warehouse what a query would return and writes a Pydantic
class typed for those columns. You tell it which query in one of three ways: point it at a
query you already wrote, name a view and its fields on the command line, or declare the
whole set in ``pyproject.toml``. :ref:`tutorial-warehouse-models` used the second.

The command comes with the ``cli`` extra. Handing the generated class to
:py:meth:`~semolina.cursor.SemolinaCursor.into` needs ``arrowmodel`` as well (see
:ref:`howto-typed-results`), so a CI job that only regenerates DTOs can skip it:

.. code-block:: bash

   pip install "semolina[cli,arrowmodel]"

It is the sibling of ``semolina codegen``, which generates the
:py:class:`~semolina.models.SemanticView` class for a whole view. A view carries every
metric and dimension; a query returns a subset, typed by its own aggregations. One class
per view cannot describe both, so a result DTO is generated from a query.

Generate a DTO from a module-level query
-----------------------------------------

Codegen resolves an importable object, so the query has to live at module scope:

.. code-block:: python

   # myapp/queries.py
   from myapp.models import Sales

   revenue_by_country = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
   )

Point the command at it by dotted path:

.. code-block:: bash

   semolina codegen-dto myapp.queries.revenue_by_country --backend snowflake

The class arrives on stdout:

.. code-block:: python

   """
   Generated result DTOs. Do not edit.

   Backend: snowflake

   Fields bind to result columns by name. Every backend names a column after its field,
   so a field carries an alias only where its column is spelled differently. The
   annotations reflect this backend's own aggregation result typing: regenerate against
   another warehouse where its driver types differ.

   Classes:
       RevenueByCountry -- myapp.queries.revenue_by_country (dialect: SnowflakeDialect, probe route: execute-schema)
   """

   from __future__ import annotations

   import decimal

   import pydantic


   class RevenueByCountry(pydantic.BaseModel):
       """Result DTO for myapp.queries.revenue_by_country (probe route: execute-schema)."""

       revenue: decimal.Decimal | None
       country: str

Field names come from your model, so ``Sales.revenue`` gives the DTO a field named
``revenue``. Semolina aliases every selected column to its field name, so that is also the
column name the result carries, on every backend, and the field needs no
``validation_alias``. The annotation is the type Snowflake said the column would arrive as,
not the model's declared field type.

The class name comes from the attribute name: ``revenue_by_country`` becomes
``RevenueByCountry``. An attribute whose class name would be a keyword or a name the
generated file imports gets a ``Query`` prefix, so ``any`` becomes ``QueryAny`` rather than a
class that hides ``typing.Any``.

No row of your data is fetched. Codegen asks the warehouse to type the query rather than to
run it, the same probe behind :ref:`semolina codegen --check <howto-codegen-check>`.

The generated class is what :py:meth:`~semolina.cursor.SemolinaCursor.into` wants:

.. code-block:: python

   from myapp.dtos import RevenueByCountry
   from myapp.queries import revenue_by_country

   with revenue_by_country.execute() as cursor:
       rows = cursor.into(RevenueByCountry)

Credentials come from the same places ``semolina codegen`` reads them: the
``[connections.<backend>]`` section of ``.semolina.toml``, then prefixed environment
variables, then a ``.env`` file. ``--backend duckdb`` wants a ``--database`` path, or
``DUCKDB_DATABASE`` in the environment. See :ref:`howto-codegen-credentials`.

Generate a DTO without writing a query first
---------------------------------------------

The dotted-path route needs a query object at module scope to point at. If you build your
queries inside a request handler rather than hoisting them to module level, there is nothing
to point at, so name the view and the fields on the command line instead:

.. code-block:: bash

   semolina codegen-dto --backend snowflake \
       --view analytics.sales \
       --metrics revenue \
       --dimensions country

Nothing is imported. Codegen builds the query for you, probes it, and emits the class:

.. code-block:: python

   class Sales(pydantic.BaseModel):
       """Result DTO for view 'analytics.sales' metrics=[revenue] dimensions=[country] (probe route: execute-schema)."""

       revenue: decimal.Decimal | None
       country: str

That is the same class the dotted-path route generates for the same projection, because it
is the same query: the field names go through your dialect's own builder, so the annotations
are whatever that warehouse answered.

The class is named after the view's last segment, so ``analytics.sales`` gives you ``Sales``
and ``analytics.daily_sales`` gives you ``DailySales``. It's the same name ``semolina codegen``
gives the view's model, so a quoted ``analytics."Order Book"`` gives you ``OrderBook``, and
``analytics.any`` or ``analytics.metric`` gives you ``ViewAny`` or ``ViewMetric``. Pass
``--name`` for anything else.

``--metrics`` and ``--dimensions`` take a comma-separated list, and both can be repeated.
These two commands are the same:

.. code-block:: bash

   semolina codegen-dto --view analytics.sales --metrics revenue,order_count -b snowflake
   semolina codegen-dto --view analytics.sales --metrics revenue --metrics order_count -b snowflake

The names you give are the warehouse's field names *and* the generated attribute names, so
each one has to be a plain Python identifier: not a Python keyword, and not one of the names
the query builder reserves (``query``, ``metrics``, ``dimensions``, ``where``, ``filter``,
``order_by``, ``limit``, ``execute``, ``to_sql``, ``using``, and the dict-like ``keys``,
``values``, ``items``, ``get``, ``pop``, ``update``, ``clear``). That is the same rule a
hand-written :py:class:`~semolina.models.SemanticView` field obeys, and a name that breaks it
exits ``2`` before anything connects.

A warehouse field spelled in a way Python cannot reach, such as ``gross revenue``, is the
one case this route has no answer for. It needs a model declaring
``gross_revenue = Metric(source="gross revenue")`` and a query built on that. ``semolina
codegen`` writes that model for you, with a valid name and the column in ``source=``.

A field the view does not carry exits ``6``, and the message lists the columns the result
did carry. That is the mistake this route makes easy: the names are typed by hand rather
than checked by an import, and your editor cannot tell you that ``total_valu`` is a typo.

You still need a model to run the query
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``--view`` replaces the importable *query*, not the model. A DTO describes a result, and
producing a result still goes through :py:meth:`Model.query() <semolina.models.SemanticView.query>`
on a :py:class:`~semolina.models.SemanticView` subclass, whatever generated the class you
convert into:

.. code-block:: python

   from myapp.dtos import Sales
   from myapp.models import Sales as SalesView

   with SalesView.query(
       metrics=[SalesView.revenue],
       dimensions=[SalesView.country],
   ).execute() as cursor:
       rows = cursor.into(Sales)

Generate that model once with ``semolina codegen`` (see :ref:`howto-codegen`) and keep it.
The projection you pass to ``--metrics`` and ``--dimensions`` has to match the projection the
query selects, or the DTO's fields will not bind to the columns that come back.

Point codegen at a filtered query
---------------------------------

A query's ``where()``, ``order_by()`` and ``limit()`` clauses are stripped before the probe.
The DTO describes the projection and nothing else, so these two generate the same class:

.. code-block:: python

   revenue_by_country = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
   )

   top_us_revenue = (
       Sales.query()
       .metrics(Sales.revenue)
       .dimensions(Sales.country)
       .where(Sales.country == "US")
       .order_by(Sales.revenue.desc())
       .limit(10)
   )

A filter decides which rows come back, not what type each column is. You do not need to keep
an unfiltered twin of a query around for codegen to look at: point it at the query your
application runs.

Write the output to a file
--------------------------

.. code-block:: bash

   semolina codegen-dto myapp.queries.revenue_by_country \
       --backend snowflake --output myapp/dtos.py

The directory has to exist; codegen will not create one, because a generated DTO lives in
the package that imports it. Nothing is written until every class has rendered, so a run
that fails partway leaves an already-committed file exactly as it was. The file is then
replaced in one step, so even a failed write can't leave half a module behind.

Redirecting stdout works as well. Every diagnostic goes to stderr, so the
file captures only Python:

.. code-block:: bash

   semolina codegen-dto myapp.queries.revenue_by_country \
       --backend snowflake > myapp/dtos.py

Format the generated output
---------------------------

By default the command prints valid but unformatted Python. Install the optional
``codegen-lint`` extra and the generated source goes through ruff first, formatted with its
imports sorted:

.. code-block:: bash

   pip install "semolina[codegen-lint]"
   # or
   uv add "semolina[codegen-lint]"

Without the extra you still get the DTO on stdout, plus a short reminder on stderr. The
reminder stays out of stdout, so ``> myapp/dtos.py`` captures only the Python.

ruff runs with its default settings (line length 88, double quotes), not your project's
``[tool.ruff]`` config, so the output is the same whichever directory you run from. If
your own pre-commit runs ``ruff format --check`` with a different line length or quote
style, exclude the generated file from it rather than reformatting it by hand.

Generate several DTOs into one file
------------------------------------

Pass several dotted paths in one call:

.. code-block:: bash

   semolina codegen-dto \
       myapp.queries.revenue_by_country \
       myapp.queries.orders_by_month \
       --backend snowflake > myapp/dtos.py

The classes appear in one output block over a single shared import section, and each one
names its own dotted path and probe route in its docstring.

Two paths whose attribute names produce the same class name are refused rather than
generated, and so are two config entries over the same view. A duplicate class in one module
is not an error Python reports: the second definition replaces the first, so you would get a
file that imports cleanly and is missing a DTO. Rename one with ``--name`` or with its
``name`` key.

Declare every DTO in pyproject.toml
------------------------------------

Once you have more than a couple of DTOs, write the set down instead, in the file that
already describes your build:

.. code-block:: toml

   [tool.semolina.dto]
   backend = "snowflake"
   output = "myapp/dtos.py"

   [[tool.semolina.dto.entries]]
   query = "myapp.queries.revenue_by_country"

   [[tool.semolina.dto.entries]]
   query = "myapp.queries.orders_by_month"

   [[tool.semolina.dto.entries]]
   name = "TopProducts"
   view = "analytics.products"
   metrics = ["units_sold"]
   dimensions = ["product_name"]

Then regenerate all of them at once:

.. code-block:: bash

   semolina codegen-dto

The classes arrive in ``myapp/dtos.py`` in the order the file declares them, over one shared
import block. Ordering matters more than it looks: codegen never sorts them, so inserting an
entry produces a diff of that entry rather than of the whole module.

Settings on the section itself:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Key
     - Meaning
   * - ``backend``
     - What ``--backend`` would have said. Required unless you pass the flag.
   * - ``database``
     - The DuckDB database path, for ``backend = "duckdb"``.
   * - ``output``
     - Where to write. Omit it and the module goes to stdout.

And on each ``[[tool.semolina.dto.entries]]`` table, one of two shapes:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Key
     - Meaning
   * - ``query``
     - Dotted path to a module-level query, as the positional argument takes.
   * - ``view``
     - A view name, with ``metrics`` and ``dimensions`` as arrays of field names.
   * - ``name``
     - The generated class name. Omit it and the name is derived, from the query attribute
       or from the view.

Relative ``output`` and ``database`` paths resolve against the directory holding
``pyproject.toml``, not against your shell's working directory, so the declaration means the
same file wherever you run the command from. A path starting with ``~`` means your home
directory.

``--backend``, ``--database`` and ``--output`` still work and override what the section says,
so a test suite can regenerate the same declared DTOs against a local DuckDB without
touching the committed config:

.. code-block:: bash

   semolina codegen-dto --backend duckdb --database tests/fixtures.db \
       --output tests/dtos_duckdb.py

.. warning::

   ``DUCKDB_DATABASE`` in the environment beats the section's ``database`` too, not only the
   absence of ``--database``. The flag reads that variable as its own fallback, and it does
   so before the config is consulted, so a stray export probes a different database than the
   committed file names. When that happens, a ``Note:`` line on stderr names both paths and
   the one being probed.

Credentials are not part of this. The section names a *backend*, which is a label. The
connection details still come from ``.semolina.toml`` and the environment, which is where
they belong: ``pyproject.toml`` gets committed and those two do not. See
:ref:`howto-codegen-credentials`.

To read a file other than ``./pyproject.toml``, pass ``--config``. It cannot be combined
with a query path or ``--view``: the config declares what a project generates, and
generating something else alongside it would leave the file describing only part of its own
output.

An unrecognized key is an error rather than something quietly ignored -- ``dimension`` for
``dimensions``, ``outputs`` for ``output``. A config is written once and trusted for a long
time, and a typo that generates a subtly wrong DTO is found by whoever notices the missing
column, not by whoever made it.

.. _howto-dto-codegen-check:

Check a committed DTO in CI
----------------------------

``--check`` compares the committed file against what the warehouse would produce now. It
writes nothing, and exits ``5`` if they have diverged:

.. code-block:: bash

   semolina codegen-dto --check

With a ``[tool.semolina.dto]`` section that is the whole invocation: it reads the file
``output`` names, so it verifies exactly what the bare command would write. Without one,
point it at the file with ``--output``:

.. code-block:: bash

   semolina codegen-dto myapp.queries.revenue_by_country \
       --backend snowflake --output myapp/dtos.py --check

The report goes to stderr, one row per field:

.. code-block:: text

   semolina codegen-dto --check: RevenueByCountry
   ┏━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┓
   ┃ Field   ┃ Committed              ┃ Generated              ┃ Status ┃
   ┡━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━┩
   │ revenue │ decimal.Decimal | None │ decimal.Decimal | None │ match  │
   │ country │ str                    │ str                    │ match  │
   └─────────┴────────────────────────┴────────────────────────┴────────┘

Two alias columns appear when a field's alias has moved. Codegen writes an alias only where
a column is spelled differently from its field, so most reports omit them. Annotations are
what drift across backends: a file generated against Snowflake and checked against
Databricks drifts wherever the two drivers type a column differently.

Drift is also reported when the query gained or lost a field, when the committed file has no
class of that name, and when it has a class nothing generates any more, which is what a
config entry deleted without regenerating leaves behind.

.. tip:: Hand-edited ``Any`` annotations do not drift

   Replacing a generated ``Any`` with a real type is what the
   `Replace the Any annotations`_ section tells you to do, so ``--check`` treats a generated
   ``Any`` as agreeing with whatever you wrote. Codegen has no opinion about that column, so
   it has none to contradict. The row still appears, with a note saying the annotation is
   yours.

   It works one way only. A committed ``Any`` against a resolved annotation *is* drift:
   there codegen has learned a type your file does not know, and regenerating gains you a
   real one.

You can edit the committed file and keep checking it. ``--check`` parses it without
importing it and resolves each name through the file's imports, so ``Optional[Decimal]``
after ``from decimal import Decimal`` matches a generated ``decimal.Decimal | None``.
Fields inherited from a base class you added count as the class's own. A class you renamed
is still found through the docstring codegen wrote, which names its query, and a note says
which class stood in for which. A field you changed to
``validation_alias=pydantic.AliasChoices(...)`` matches when one of the choices is the
column, with a note that ``.into()`` refuses
``AliasChoices``. Pydantic binds it, but the Arrow conversion behind ``.into()`` reads
plain string aliases only.

Anything the check can't read is printed as a warning naming the class, the line and the
statement. That includes a field with a default, which binds no result column, and an alias
that is neither a string nor an ``AliasChoices`` of strings.

.. warning:: A DTO narrowed for ``validate=True`` reads as drift

   ``--check`` judges a field by the type the fast path needs. A ``float`` you chose on
   purpose for a decimal column works under ``.into(..., validate=True)``, but it drifts
   here, and there is no option to judge by the validated path instead. Keep a DTO you
   narrowed on purpose out of the file ``--check`` reads.

A run that exits ``5`` has worked correctly. Exit ``1`` means the committed file could not
be parsed, and ``2`` means no file was named or the named file does not exist.

Rename the generated class
--------------------------

.. code-block:: bash

   semolina codegen-dto myapp.queries.revenue_by_country \
       --backend snowflake --name CountryRevenueRow

``--name`` renames a single class, so it takes a single query: one dotted path, or one
``--view``. Passing it alongside several exits ``2`` before anything is imported, and so
does passing it with ``--config``, where each entry names itself with its own ``name`` key.

The value becomes the class's own name in the generated file, so it has to be a valid Python
identifier and not a keyword; anything else exits ``2`` as well, before the query is
resolved.

Regenerate when you change warehouse
-------------------------------------

A generated DTO's annotations belong to the backend it was probed against, and its header
says which one. The field names do not: every backend returns the same query's columns under
the same names. What can differ is the type each one arrives as:

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. code-block:: python

         revenue: decimal.Decimal | None
         country: str

   .. tab-item:: Databricks
      :sync: databricks

      .. code-block:: python

         revenue: int | None
         country: str

      A ``SUM`` over an integer column, which Snowflake types ``NUMBER(38,0)`` and delivers
      as a ``Decimal``, comes back here as a ``BIGINT``.

   .. tab-item:: DuckDB
      :sync: duckdb

      .. code-block:: python

         revenue: decimal.Decimal | None
         country: str

      DuckDB types a ``SUM`` over an integer column ``HUGEINT``, a 128-bit integer that
      arrives as a ``Decimal``. A view that casts the sum to ``BIGINT`` gives
      ``int | None`` here instead.

Aggregation result typing is the warehouse's own decision: a ``SUM`` over an integer column
comes back as a ``BIGINT`` from one warehouse and a ``DECIMAL`` from another. Codegen prints
the type it measured and does not try to paper over the difference, because there is no
annotation that would be right on all three at once.

So regenerate a DTO against the warehouse you deploy to, and read the header when you want
to know which warehouse a committed file came from. See :ref:`howto-result-column-names` for
how result columns get their names.

Replace the Any annotations
---------------------------

When a result column's Arrow type has no clean Python equivalent, codegen annotates the
field ``Any`` and writes the Arrow type into a comment above it rather than guessing:

.. code-block:: python

   # TODO: struct<iso: string>
   origin: Any

The comment is the signal, not the annotation. ``Any`` satisfies a type checker by
definition, so a DTO carrying one passes basedpyright strict while telling you nothing about
that column. Search the generated file for ``TODO:`` and replace each one with the type you
want the column to arrive as.

Know why every metric is optional
----------------------------------

Every metric field is annotated ``| None``, whatever the query:

.. code-block:: python

   revenue: decimal.Decimal | None

A group whose inputs are all NULL returns NULL from ``SUM``, ``AVG``, ``MIN`` and ``MAX``.
``COUNT`` returns ``0`` instead, so a ``COUNT`` metric is annotated wider than it needs to
be. Dimensions and facts are columns rather than aggregates, so codegen annotates them with
the type the warehouse reports. See :ref:`explanation-type-fidelity` for the null cases and
why that trade is deliberate.

Read the probe route in the header
-----------------------------------

Codegen prefers to ask the driver to type the query without running it. Not every driver
offers that call. When one refuses, codegen runs the query wrapped to return no rows and
reads the schema off the empty result instead. No data row is fetched on either route, and
the generated file records which one answered:

.. code-block:: text

   RevenueByCountry -- myapp.queries.revenue_by_country (dialect: DatabricksDialect, probe route: zero-row)

``execute-schema`` means the driver typed the query. ``zero-row`` means it declined and the
fallback ran. The two routes produce the same annotations, so the label
tells you how the answer was obtained rather than how much to trust it.

Which route you get is a property of the driver, not of your query. Snowflake and DuckDB
answer the describe-only call, so they report ``execute-schema``. The Databricks ADBC driver
does not implement it and reports ``zero-row``. That was measured against a live workspace
on 2026-08-15, where the wrapped query typed the schema and the generated class
round-tripped through :py:meth:`~semolina.cursor.SemolinaCursor.into`.

Know what a dotted path imports
--------------------------------

Resolving ``myapp.queries.revenue_by_country`` imports ``myapp.queries``, which runs that
module top to bottom: connections open, environment variables are read, decorators fire.
That is inherent to generating code from an importable object, and
``--backend dotted.path.ClassName`` does the same. Point the command at code you trust.

The working directory is appended to ``sys.path``, never prepended. A package sitting
directly in your project root therefore resolves without being installed, while a file in
the working directory cannot shadow an installed distribution of the same name.

On a ``src/`` layout the package is not in the working directory, so that fallback does not
reach it and a dotted path resolves only once the project is installed, with ``uv sync``
or ``pip install -e .``. A path that will not import reports the module it could not find and
exits ``2``.

``--view`` imports nothing at all: it names the view and its fields directly, so there is no
module to run. The same split holds inside a config file: an entry with ``query`` imports,
and an entry with ``view`` does not.

Exit codes
----------

.. list-table::
   :header-rows: 1
   :widths: 10 90

   * - Exit code
     - Meaning
   * - ``0``
     - Success
   * - ``1``
     - Unexpected error, including generated source that does not parse, an
       installed ruff that fails on it, or a committed file under ``--check``
       that does not parse
   * - ``2``
     - Invalid option: an unrecognized or omitted ``--backend``, a backend whose driver is
       not installed or that cannot be built with no arguments, a ``QUERY_PATH`` that does
       not resolve to a query, a ``--view`` field list a model could not declare, a
       malformed ``[tool.semolina.dto]`` section, two routes given at once, ``--name``
       passed with more than one DTO, a class name that is not a valid Python identifier, is
       a name the generated module imports (``Any``, ``pydantic``), or collides with another
       entry's, an ``--output`` directory that does not exist, or ``--check`` with no file
       to read or one that does not exist
   * - ``3``
     - View not found, as reported by a custom ``--backend`` engine (see the note below)
   * - ``4``
     - Connection or authentication failure
   * - ``5``
     - Annotation drift -- a committed DTO no longer matches the result schema
   * - ``6``
     - Probe failed, or a projected field matched no result column -- no DTO was written

.. note::

   ``5`` means here what it means for ``semolina codegen --check``: a committed generated
   file no longer matches the result schema. Only a ``--check`` run can return it.

   ``4`` means Semolina could not connect, on every backend. A view that does not exist
   is different: the built-in backends report it as a query the warehouse would not
   describe, so it exits ``6``, and only a custom ``--backend`` engine that raises
   :py:exc:`~semolina.exceptions.SemolinaViewNotFoundError` exits ``3``. Read stderr
   rather than inferring the cause from the code.

Neither case behind exit ``6`` writes anything to stdout, and stderr names the field along
with every column the result carried.

See also
--------

- :ref:`tutorial-warehouse-models` -- running this command and ``semolina codegen`` in one
  sitting
- :ref:`howto-typed-results` -- passing the generated class to ``.into()``, and the
  hand-written route it replaces
- :ref:`howto-codegen` -- generating the ``SemanticView`` model class the query is built from
- :ref:`explanation-type-fidelity` -- why the warehouse decides the annotation and what a
  generated one promises
- :ref:`explanation-duckdb-vs-warehouse` -- which of the per-backend differences above a
  DuckDB-only test suite cannot catch
- :ref:`howto-codegen-credentials` -- environment variables, .env files, and config file
  fallback
- :ref:`reference-cli` -- every flag, argument, and exit code
