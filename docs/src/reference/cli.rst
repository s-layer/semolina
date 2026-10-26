.. _reference-cli:

CLI reference
=============

The ``semolina`` command-line tool provides warehouse introspection and code
generation utilities. It is installed as a console script with the
``semolina`` package, and its own dependencies come with the ``cli`` extra:

.. code-block:: bash

   pip install "semolina[cli]"

Without the extra, the command prints that install line and exits with code 2.

.. code-block:: console

   $ semolina [OPTIONS] COMMAND [ARGS]...

Global options
--------------

``--version``
   Print the installed version and exit.

``--help``
   Show the help message and exit.

``semolina codegen``
--------------------

Introspect warehouse semantic views and generate
:py:class:`~semolina.models.SemanticView` model classes as Python source code.

.. code-block:: console

   $ semolina codegen [OPTIONS] VIEWS...

Arguments
~~~~~~~~~

``VIEWS``
   One or more schema-qualified view names to introspect
   (e.g. ``my_schema.sales_view``). Required.

Options
~~~~~~~

``--backend``, ``-b`` *TEXT*
   Backend to connect to. Accepts one of:

   - ``snowflake`` -- use the built-in Snowflake backend
   - ``databricks`` -- use the built-in Databricks backend
   - ``duckdb`` -- use the built-in DuckDB backend
   - A dotted import path (e.g. ``mypackage.backends.CustomEngine``) --
     dynamically imported and instantiated with no arguments

   Required.

``--database``, ``-d`` *TEXT*
   Path to a DuckDB database file. Only used with ``--backend duckdb``, where it
   is required. Falls back to the ``DUCKDB_DATABASE`` environment variable if not
   provided; with neither set, the command exits ``2``.

``--output``, ``-o`` *PATH*
   Write the generated module to this file instead of stdout. Nothing is written
   until every view has rendered, and the file is replaced atomically, so a failed
   run leaves it untouched. The directory must exist. If the directory refuses new
   files but the target is writable, the target is written in place instead. With ``--check``,
   the committed file to check.

``--check``
   Compare a committed model's annotations, roles, columns and metadata arguments
   against the warehouse instead of generating one. Reads the file ``--output`` names.
   Writes a per-field report to stderr and nothing to stdout. See :ref:`howto-codegen-check`.

``--model`` *PATH*
   Older spelling of ``--output`` for ``--check``. Passing it without ``--check``,
   or together with ``--output``, exits ``2``.

Exit codes
~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 10 90

   * - Code
     - Meaning
   * - 0
     - Success
   * - 1
     - Unexpected error, including an unparseable committed file under ``--check``,
       generated source that does not parse, an installed ruff that fails on it, or
       ``S_LAYER_*`` tags or a Databricks ``format`` that don't make a valid format for
       their member
   * - 2
     - Invalid option: an unrecognized or omitted ``--backend``, connection config
       that could not be assembled, a backend whose driver is not installed, a
       ``dotted.path.ClassName`` that cannot be built with no arguments,
       ``--backend duckdb`` with no database path, an ``--output`` directory that
       does not exist, ``--check`` with no file to read, with both ``--output`` and
       ``--model``, or with a file that does not exist,
       ``--model`` without ``--check``, a custom engine's class name that is not a
       valid Python identifier or is a name the generated module imports, or two
       views that would generate the same class name
   * - 3
     - View not found in the warehouse
   * - 4
     - Connection or authentication failure
       (:py:exc:`~semolina.exceptions.SemolinaConnectionError`)
   * - 5
     - Drift found by ``--check``: an annotation, role, column or metadata argument that
       no longer matches the warehouse
   * - 6
     - The warehouse was reached but could not describe a view or refused the lookup of
       its ``S_LAYER_*`` tags
       (:py:exc:`~semolina.exceptions.SemolinaIntrospectionError`, or a raw driver error
       from a custom engine), or ``--check`` fell back to warehouse metadata because the
       result-schema probe failed. A probe that ran but answered for only some fields
       does not exit 6; stderr names the fields

Environment variables
~~~~~~~~~~~~~~~~~~~~~

``codegen`` reads the ``[connections.<backend>]`` section of ``.semolina.toml`` first, then
environment variables, then a ``.env`` file. That is **not** the same section
:py:func:`~semolina.config.create_engine` reads: it defaults to ``[connections.default]`` and
takes any section name you pass, while codegen always looks under the backend's own name.
``--backend duckdb`` reads no section at all and takes ``--database`` or ``DUCKDB_DATABASE``.
See :ref:`howto-codegen-credentials` for the full chain.

The variable names below are the config field names with a backend prefix. Which ones you
need depends on the backend.

.. tab-set::
   :sync-group: warehouse

   .. tab-item:: Snowflake
      :sync: snowflake

      .. list-table::
         :header-rows: 1
         :widths: 35 65

         * - Variable
           - Description
         * - ``SNOWFLAKE_ACCOUNT``
           - Account identifier (e.g. ``xy12345.us-east-1``). Required.
         * - ``SNOWFLAKE_USER``
           - Username
         * - ``SNOWFLAKE_PASSWORD``
           - Password. Use this or the key-pair variables below.
         * - ``SNOWFLAKE_PRIVATE_KEY_PATH``
           - Path to a PKCS1 or PKCS8 private key file (key-pair auth)
         * - ``SNOWFLAKE_PRIVATE_KEY_PASSPHRASE``
           - Passphrase for an encrypted private key
         * - ``SNOWFLAKE_DATABASE``
           - Database name. Needed to resolve the view name.
         * - ``SNOWFLAKE_WAREHOUSE``
           - Warehouse name. Needed to run the introspection query.
         * - ``SNOWFLAKE_ROLE``
           - Role name (optional)

   .. tab-item:: Databricks
      :sync: databricks

      .. list-table::
         :header-rows: 1
         :widths: 35 65

         * - Variable
           - Description
         * - ``DATABRICKS_HOST``
           - Workspace hostname
         * - ``DATABRICKS_HTTP_PATH``
           - SQL warehouse HTTP path
         * - ``DATABRICKS_TOKEN``
           - Personal access token
         * - ``DATABRICKS_CATALOG``
           - Unity Catalog name (optional)

   .. tab-item:: DuckDB
      :sync: duckdb

      .. list-table::
         :header-rows: 1
         :widths: 35 65

         * - Variable
           - Description
         * - ``DUCKDB_DATABASE``
           - Path to DuckDB database file (fallback for ``--database``)

A ``.env`` file in the working directory is read automatically. Set
``SEMOLINA_ENV_FILE`` to read a different path instead. It fills only the fields
the TOML section and the environment leave unset.

Output
~~~~~~

Generated Python source is written to **stdout**, or to the file ``--output``
names. Diagnostic messages (errors, warnings) go to stderr. The two routes give
the same bytes:

.. code-block:: console

   $ semolina codegen my_schema.sales_view -b snowflake -o models.py
   $ semolina codegen my_schema.sales_view -b snowflake > models.py

With the ``codegen-lint`` extra, the module is formatted with ruff's default
settings (``--isolated``), whatever directory you run from.

The output contains one :py:class:`~semolina.models.SemanticView` subclass per
introspected view, with typed :py:class:`~semolina.fields.Metric`,
:py:class:`~semolina.fields.Dimension`, and :py:class:`~semolina.fields.Fact` fields.


``semolina codegen-dto``
------------------------

Probe a query and generate a Pydantic result DTO as Python source code, typed
for the columns that query returns on that backend. Every result column carries
its field's name, so the fields are bare and the same class reads results from
any backend. See :ref:`howto-dto-codegen`.

.. code-block:: console

   $ semolina codegen-dto [OPTIONS] [QUERY_PATHS]...

Which query to probe comes from exactly one of three routes, and giving more
than one exits ``2``:

- one or more ``QUERY_PATHS``
- ``--view`` with ``--metrics`` and/or ``--dimensions``
- the ``[tool.semolina.dto]`` section of ``pyproject.toml``, used when neither
  of the other two is given

Arguments
~~~~~~~~~

``QUERY_PATHS``
   Zero or more dotted paths to module-level query objects (e.g.
   ``myapp.queries.revenue_by_region``). Resolving one imports the module that
   holds it, which runs that module. The working directory is appended to
   ``sys.path``. Several paths are emitted into one output block.

Options
~~~~~~~

``--backend``, ``-b`` *TEXT*
   Backend to connect to. Takes the same values as ``codegen``'s ``--backend``:

   - ``snowflake`` -- use the built-in Snowflake backend
   - ``databricks`` -- use the built-in Databricks backend
   - ``duckdb`` -- use the built-in DuckDB backend
   - A dotted import path (e.g. ``mypackage.backends.CustomEngine``) --
     dynamically imported and instantiated with no arguments

   Required unless ``[tool.semolina.dto]`` sets ``backend``. The flag wins over
   the config.

``--database``, ``-d`` *TEXT*
   Path to a DuckDB database file. Only used with ``--backend duckdb``, where it
   is required. Falls back to the ``DUCKDB_DATABASE`` environment variable, then
   to the config's ``database``.

``--view`` *TEXT*
   Generate from a view name and a field list instead of an importable query.
   Needs ``--metrics`` and/or ``--dimensions``, and imports nothing. Cannot be
   combined with ``QUERY_PATHS`` or ``--config``.

``--metrics`` *TEXT*
   Metric field names for ``--view``. Repeatable, and each value may be a
   comma-separated list. Each name is both the warehouse field name and the
   generated attribute name, so it must be a plain Python identifier, not a
   Python keyword or soft keyword, and not one of the names the query builder
   reserves: ``query``, ``metrics``, ``dimensions``, ``where``, ``filter``,
   ``order_by``, ``limit``, ``execute``, ``to_sql``, ``using``, ``keys``,
   ``values``, ``items``, ``get``, ``pop``, ``update``, ``clear``. Anything else
   exits ``2``, naming the offending value.

``--dimensions`` *TEXT*
   Dimension field names for ``--view``, under the same rules as ``--metrics``.

``--name`` *TEXT*
   Override the generated class name, which otherwise comes from the query
   attribute (``revenue_by_region`` becomes ``RevenueByRegion``) or from the
   view's last segment (``analytics.sales`` becomes ``Sales``). Renames a single
   class, so it takes a single query; passing it with more than one, or with
   ``--config``, exits ``2``.

``--output``, ``-o`` *PATH*
   Write the generated module to this file instead of stdout. The directory must
   already exist. The file is written once, after every class has rendered, so a
   failed run leaves a previously generated file untouched. Overrides the
   config's ``output``.

``--check``
   Compare the committed DTO file against the warehouse's current result schema
   instead of generating. Writes nothing to stdout, reports per field on stderr,
   and exits ``5`` on drift. Reads the file ``--output`` names (or the config's
   ``output``); with neither, exits ``2``. Annotations are compared, and so is the
   result column each field binds (its ``validation_alias``, or its own name when it
   has none). Every backend names a column after its field, so a file generated
   against another backend drifts only where an annotation differs. A generated
   ``Any`` agrees with whatever annotation you replaced it with.

``--config`` *PATH*
   Read ``[tool.semolina.dto]`` from this file instead of ``./pyproject.toml``.
   The file must exist and must carry the section. Cannot be combined with
   ``QUERY_PATHS`` or ``--view``.

Configuration
~~~~~~~~~~~~~

With no ``QUERY_PATHS`` and no ``--view``, the command generates what
``pyproject.toml`` declares:

.. code-block:: toml

   [tool.semolina.dto]
   backend = "snowflake"
   database = "sales.db"
   output = "myapp/dtos.py"

   [[tool.semolina.dto.entries]]
   query = "myapp.queries.revenue_by_region"

   [[tool.semolina.dto.entries]]
   name = "TopProducts"
   view = "analytics.products"
   metrics = ["units_sold"]
   dimensions = ["product_name"]

``backend``, ``database`` and ``output`` are optional and are overridden by the
matching flags. Each ``[[tool.semolina.dto.entries]]`` table carries either
``query`` or ``view`` (with ``metrics`` and ``dimensions``), never both, plus an
optional ``name``. Classes are emitted in the order the file declares them.

Relative ``output`` and ``database`` paths resolve against the directory holding
the config file, not against the working directory. Unrecognized keys are
errors, not ignored.

Exit codes
~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 10 90

   * - Code
     - Meaning
   * - 0
     - Success
   * - 1
     - Unexpected error, including generated source that does not parse, an
       installed ruff that fails on it, or a committed file under ``--check``
       that does not parse
   * - 2
     - Invalid option: an unrecognized or omitted ``--backend``, a backend whose
       driver is not installed or that cannot be built with no arguments, a
       ``QUERY_PATH`` that does not resolve to a query, a ``--view`` field list a
       model could not declare, a malformed ``[tool.semolina.dto]`` section, two
       routes given at once, ``--name`` passed with more than one DTO, a
       class name that is not a valid Python identifier, is a name the generated
       module imports (``Any``, ``pydantic``), or collides with another entry's, an
       ``--output`` directory that does not exist, or ``--check`` with no file to
       read or one that does not exist
   * - 3
     - View not found, as reported by a custom ``--backend`` engine. The built-in
       backends report a missing view as ``6``
   * - 4
     - Connection or authentication failure
   * - 5
     - Annotation drift -- a committed DTO no longer matches the result schema
   * - 6
     - Probe failed, or a projected field matched no result column -- no DTO was
       written

``5`` means here what it means for ``codegen --check``: a committed generated
file no longer matches the result schema. Only a ``--check`` run returns it.
``4`` is a connection failure on every backend. The built-in backends report a view
that does not exist, like any other query the warehouse refuses to describe, as ``6``;
``3`` comes only from a custom ``--backend`` engine that raises
:py:exc:`~semolina.exceptions.SemolinaViewNotFoundError`.

Environment variables
~~~~~~~~~~~~~~~~~~~~~

``codegen-dto`` reads credentials from the same sources, in the same order, as
``codegen``. See the table above.

Output
~~~~~~

Generated Python source is written to **stdout** unless ``--output`` (or the
config's ``output``) names a file. Diagnostic messages always go to stderr, so
redirecting stdout also works:

.. code-block:: console

   $ semolina codegen-dto myapp.queries.revenue_by_region -b snowflake -o dtos.py
   $ semolina codegen-dto myapp.queries.revenue_by_region -b snowflake > dtos.py

The output contains one ``pydantic.BaseModel`` subclass per query, over a single
shared import block, with a provenance header naming the backend that was probed
and, per class, where the query came from. No row of your data is fetched.

See also
--------

- :ref:`howto-codegen` -- how to generate models from your warehouse
- :ref:`howto-dto-codegen` -- how to generate a result DTO from a query
- :ref:`howto-codegen-credentials` -- credential configuration for codegen
- :ref:`howto-models` -- understanding the generated model classes
