"""
SQL generation dialects for different backends.

Dialect classes encapsulate backend-specific SQL generation rules including
identifier quoting, metric wrapping, placeholder styles, and SQL keyword
variations. Each dialect handles the syntactic differences between Snowflake,
Databricks, and DuckDB.
"""

from __future__ import annotations

import datetime
import decimal
import math
import uuid
import warnings
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, cast

from semolina import dialect as _dialect
from semolina.filters import (
    And,
    Between,
    EndsWith,
    Exact,
    Gt,
    Gte,
    IEndsWith,
    IExact,
    ILike,
    In,
    IsNull,
    IStartsWith,
    Like,
    Lookup,
    Lt,
    Lte,
    Not,
    NotEqual,
    Or,
    Predicate,
    StartsWith,
)

if TYPE_CHECKING:
    # Only for annotations: a runtime ``Dialect`` here would shadow the module
    # ``__getattr__`` that keeps the old ``engines.sql.Dialect`` name working.
    from semolina.dialect import Dialect
    from semolina.query import Query

__all__ = [
    "sql_str_literal",
    "LIKE_ESCAPE",
    "escape_like",
    "SQLDialect",
    "SnowflakeDialect",
    "DatabricksDialect",
    "DuckDBDialect",
    "SQLBuilder",
    "DuckDBSQLBuilder",
]


_DATE_GRAINS = frozenset({"year", "quarter", "month", "week", "day"})
"""The grains whose truncation :meth:`SQLDialect.truncate_to_grain` casts to ``DATE``."""


def sql_str_literal(value: str) -> str:
    """
    Render a value as a single-quoted SQL string literal, escaping quotes.

    Doubles any embedded single quote (``'`` -> ``''``) so a catalog field or view name
    containing a quote cannot break out of the literal. DuckDB's ``semantic_view()`` takes
    its view name and every field name as string *literals* rather than as identifiers, so
    they cannot be parameter-bound and have to be interpolated -- which makes this the one
    place that interpolation is allowed to happen.

    Lives here rather than in ``semolina.engines.duckdb`` so the introspection path and
    :class:`DuckDBSQLBuilder` share one escaper. Two copies would be two audit surfaces, and
    ``semolina codegen --check`` is the first mode that feeds a *catalogue-returned* name
    back into a statement rather than one the user typed into their own model file.

    Args:
        value: The raw string to embed (a field or view name).

    Returns:
        The value wrapped in single quotes with internal quotes doubled.

    Example:
        .. code-block:: python

            from semolina.engines.sql import sql_str_literal

            sql_str_literal("o'brien")
            # "'o''brien'"
    """
    return "'" + value.replace("'", "''") + "'"


def _python_scalar(value: Any) -> Any:
    """
    Unwrap a numpy scalar to the Python value it holds; return anything else unchanged.

    ``numpy.int64`` is not an ``int`` subclass and ``numpy.bool_`` is not a ``bool``, so
    without this a numpy value a driver would have bound fails to render as a literal.
    numpy is not imported: its scalars are recognised by module and their ``item()`` method.
    """
    if type(value).__module__ == "numpy" and hasattr(value, "item"):
        # item() gives a datetime64 or timedelta64 at nanosecond precision (pandas' and
        # Arrow's default) back as a bare int, and NaT as None: either would render as SQL
        # that compares against the wrong value.
        if getattr(getattr(value, "dtype", None), "kind", "") in ("M", "m"):
            msg = (
                f"Cannot render a numpy {value.dtype} as a SQL literal: {value!r}. Convert "
                "it to a datetime.datetime or datetime.timedelta first, e.g. with "
                "pandas.Timestamp(value).to_pydatetime()."
            )
            raise NotImplementedError(msg)
        return value.item()
    return value


def _timestamp_literal_text(value: datetime.datetime) -> str:
    """
    Render a datetime as the text body of a SQL TIMESTAMP literal.

    An aware datetime is converted to UTC and marked with the ``Z`` zone id:
    ``Z`` is the one zone form the Databricks TIMESTAMP grammar lists without
    ambiguity, and normalizing means the same instant is expressed whatever
    zone the caller's value carried. A naive datetime is emitted unchanged --
    no zone id means the warehouse session time zone, which is what the
    grammar documents for a bare timestamp string.

    This function only formats; it performs no escaping. Every value it
    returns still goes out through the caller's own quote-escaping expression
    so ``render_literal`` stays the single audited escaping site.

    Args:
        value: The datetime to render, naive or aware.

    Returns:
        An ISO-8601 timestamp string, suffixed with ``Z`` when the input was
        aware.
    """
    if value.tzinfo is None:
        return value.isoformat()
    utc_value = value.astimezone(datetime.UTC).replace(tzinfo=None)
    return f"{utc_value.isoformat()}Z"


_SLOT = "\x00"
"""
Where a bound value goes, as the compiler writes it.

The dialect's own placeholder is ``?``, which a quoted view or column name may also contain.
Marking each slot with a character no warehouse allows in a name means the inliner finds
exactly the slots the compiler emitted. A bound statement swaps each one for the dialect's
placeholder at the end.
"""

LIKE_ESCAPE = "!"
"""
The escape character for the lookups that match their value literally.

Not the backslash: it is Spark's default ``LIKE`` escape and an escape inside Spark and
Snowflake string literals, so it would need different doubling on every dialect. ``!`` is
ordinary in all three.
"""


def escape_like(value: str) -> str:
    """
    Escape a value so ``LIKE ... ESCAPE '!'`` matches it literally.

    The escape character is escaped first, so the ones added for ``%`` and ``_`` are not
    doubled again.

    Args:
        value: The text to match, wildcards and all.

    Returns:
        ``value`` with ``!``, ``%`` and ``_`` each preceded by ``!``.
    """
    return (
        value.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", f"{LIKE_ESCAPE}%")
        .replace("_", f"{LIKE_ESCAPE}_")
    )


def _qualified_name(field: Any) -> str:
    """
    Name a field the way the user wrote it, for an error message.

    Args:
        field: Field descriptor with a ``name`` and possibly an ``owner``.

    Returns:
        ``Model.field``, or the bare field name when the field has no owner.
    """
    owner = getattr(field, "owner", None)
    return f"{owner.__name__}.{field.name}" if owner is not None else str(field.name)


def _dot_inside_quotes(name: str, quote_char: str) -> bool:
    """
    Decide whether a ``.`` in a view name falls between a pair of quotes.

    Args:
        name: The view name as written.
        quote_char: The dialect's identifier quote.

    Returns:
        True when some ``.`` is inside a quoted span (a doubled quote toggles twice, so it
        stays inside).
    """
    inside = False
    for char in name:
        if char == quote_char:
            inside = not inside
        elif char == "." and inside:
            return True
    return False


def _shadowed_order_by_message(field: Any, kind: str, col_name: str, shadow: Any) -> str:
    """
    Explain why an unselected field shadowed by a select alias cannot be ordered by.

    Args:
        field: The field being ordered by.
        kind: What ``col_name`` is, for the message: ``"measure"`` or ``"column"``.
        col_name: The field's resolved source name.
        shadow: The selected field whose alias is spelled like ``col_name``.

    Returns:
        The ``ValueError`` message.
    """
    return (
        f"Cannot order by {_qualified_name(field)}: it is not selected, and its {kind} "
        f"{col_name!r} has the same name as the selected field {_qualified_name(shadow)}, "
        "which the warehouse would read in its place. Select it too, or rename the other field."
    )


def _and_all(predicates: list[Predicate]) -> Predicate | None:
    """
    Join predicates with ``AND``, left to right.

    Args:
        predicates: Predicates to join.

    Returns:
        ``None`` for no predicates, the predicate itself for one, else a left-deep ``And``.
    """
    if not predicates:
        return None
    joined = predicates[0]
    for predicate in predicates[1:]:
        joined = And(joined, predicate)
    return joined


class SQLDialect(ABC):
    """
    Abstract base class for SQL generation dialects.

    Encapsulates backend-specific SQL generation rules. Each dialect defines
    how to quote identifiers (table and column names) and wrap metric fields
    for aggregation. SQL generators use SQLDialect instances to produce
    correct SQL for each backend without duplicating dialect logic.

    Identifier Quoting:
        - Identifiers are table names, column names, and aliases
        - String literals use single quotes ('text'), never quotes
        - Identifiers must be quoted to preserve case sensitivity and handle
          special characters or reserved keywords
        - Each dialect has one correct quoting character and escaping rule

    Metric Wrapping:
        - Metrics are wrapped differently depending on backend
        - Snowflake uses AGG(identifier) - metric aggregation function
        - Databricks uses MEASURE(identifier) - semantic metric reference
        - DuckDB aggregates inside the semantic_view() table function

    Example:
        .. code-block:: python

            dialect = SnowflakeDialect()
            quoted = dialect.quote_identifier(
                "my_column"
            )  # Returns: "my_column"
            wrapped = dialect.wrap_metric(
                "revenue"
            )  # Returns: AGG("revenue")
    """

    kind: ClassVar[Dialect | None] = None
    """
    The backend this dialect renders for, or ``None`` for one outside Semolina's three.

    A subclass of a built-in dialect inherits its parent's ``kind``.
    """

    supports_parameterized_queries: bool = True
    """
    Whether the backend's driver accepts bound parameters.

    Defaults to ``True`` (the qmark ``?`` + params path), and every shipped
    dialect keeps the default: Databricks binds too since Foundry driver v0.2.0.
    A dialect whose driver rejects bind parameters overrides this to ``False``,
    in which case the builder inlines values as safe SQL literals via
    :meth:`render_literal` and returns empty params. This is the single
    capability switch the builder branches on -- it never ``isinstance``-checks
    a concrete dialect.
    """

    orders_selected_metrics_by_alias: bool = False
    """
    Whether ``ORDER BY`` names a selected metric by its alias rather than its expression.

    Defaults to ``False``: ``ORDER BY AGG("REVENUE")``. A dialect whose alias shadows the
    measure it names (so the wrapped expression would resolve to the alias) overrides this
    to ``True``. Whatever this says, a metric whose quoted measure name *is* a select alias
    orders by the alias, and an unselected one in that position raises (it has no alias).
    """

    quoted_identifiers_case_sensitive: bool = True
    """
    Whether two quoted identifiers differing only in case name different objects.

    ``True`` for Snowflake's double quotes. Databricks and DuckDB override it to ``False``:
    Databricks's backtick-quoted identifiers and DuckDB's double-quoted ones both ignore
    case. The builder uses it to tell when a select alias shadows a column or measure name.
    A Snowflake account that sets ``QUOTED_IDENTIFIERS_IGNORE_CASE = TRUE`` breaks the
    assumption; the backends how-to tells users to leave it off.
    """

    def render_literal(self, value: Any) -> str:
        """
        Render a Python value as a safe SQL literal (standard SQL escaping).

        Used wherever a value is written into the SQL rather than bound: every
        filter value when :attr:`supports_parameterized_queries` is ``False``,
        every value in a ``to_sql()`` preview, DuckDB's ``where_clause``, and
        the ``ESCAPE`` character of a LIKE lookup. This is the single audited
        SQL-literal escaping site; subclasses override it for dialect-specific
        string escaping rules.

        Standard SQL escaping: a single quote is escaped by doubling it.

        Args:
            value: The Python value to render (str, int, float, bool,
                ``decimal.Decimal``, ``datetime.date``, ``datetime.datetime``,
                a naive ``datetime.time``, ``uuid.UUID``, a numpy scalar of any
                of these, or None).

        Returns:
            A SQL literal: ``NULL`` for None, ``TRUE``/``FALSE`` for bool,
            the unquoted number for int/float, the bare fixed-point digits for
            a Decimal, ``DATE 'yyyy-mm-dd'`` for a date,
            ``TIMESTAMP 'yyyy-mm-ddThh:mm:ss'`` for a datetime,
            ``TIME 'hh:mm:ss'`` for a time, or a single-quoted string with
            internal quotes doubled (a UUID is written as its canonical text).

        Raises:
            ValueError: If the value is a non-finite float (inf/-inf/nan), a
                non-finite Decimal (NaN/Infinity), or a time with a time zone,
                none of which has a portable SQL literal form.
            NotImplementedError: If the value type is not supported (a numpy
                ``datetime64`` or ``timedelta64`` included), so the caller fails
                loudly rather than emitting mis-escaped SQL.
        """
        value = _python_scalar(value)
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, float) and not math.isfinite(value):
            msg = f"Cannot render non-finite float as a SQL literal: {value!r}."
            raise ValueError(msg)
        if isinstance(value, int | float):
            # A subclass's repr() is its own (an IntEnum member's is <Tier.GOLD: 1>).
            return repr(int(value) if isinstance(value, int) else float(value))
        if isinstance(value, decimal.Decimal):
            if value.is_nan() or value.is_infinite():
                msg = f"Cannot render non-finite Decimal as a SQL literal: {value!r}."
                raise ValueError(msg)
            # Fixed-point, never str(): an exponent form (1E+2) is what makes a
            # fractional literal a DOUBLE in Spark SQL. Bare digits are already
            # DECIMAL, so no CAST wrapper is needed or wanted.
            return format(value, "f")
        # datetime is tested BEFORE date: datetime.datetime subclasses
        # datetime.date, exactly as bool subclasses int above. A date-first
        # branch would silently truncate a timestamp to a whole day.
        if isinstance(value, datetime.datetime):
            prefix, text = "TIMESTAMP ", _timestamp_literal_text(value)
        elif isinstance(value, datetime.date):
            prefix, text = "DATE ", value.isoformat()
        elif isinstance(value, datetime.time):
            if value.tzinfo is not None:
                msg = (
                    f"Cannot render a time with a time zone as a SQL literal: {value!r}. "
                    "Pass a naive time."
                )
                raise ValueError(msg)
            prefix, text = "TIME ", value.isoformat()
        elif isinstance(value, uuid.UUID):
            prefix, text = "", str(value)
        elif isinstance(value, str):
            prefix, text = "", value
        else:
            type_name = type(cast("object", value)).__name__
            msg = (
                f"Cannot render SQL literal for unsupported type: {type_name}. "
                f"Add handling in render_literal() for this type."
            )
            raise NotImplementedError(msg)
        escaped = text.replace("'", "''")
        return f"{prefix}'{escaped}'"

    @property
    @abstractmethod
    def placeholder(self) -> str:
        """
        Parameterized query placeholder for this dialect.

        Returns the placeholder string used in parameterized SQL queries.
        Each dialect uses its database driver's native placeholder format.

        Returns:
            Placeholder string. All ADBC drivers use qmark ('?').
        """
        ...

    @abstractmethod
    def quote_identifier(self, name: str) -> str:
        """
        Quote an identifier for use in SQL.

        Identifiers are table names, column names, schema names, and aliases.
        This method adds the appropriate quote character for the dialect and
        escapes any internal quotes to prevent SQL injection and syntax errors.

        Args:
            name: Unquoted identifier name (e.g., 'my_column', 'my_field')

        Returns:
            Quoted identifier safe for SQL (e.g., '"my_column"', '`my_field`')

        Note:
            - This method handles identifiers, not string literals
            - Single quotes (') are NEVER used for identifier quoting
            - Always escape internal quotes per dialect rules
            - Preserves case exactly as provided (not uppercased/lowercased)

        Example for Snowflake (double quotes, " escaped as ""):
            'my_field' -> '"my_field"'
            'field"with"quotes' -> '"field""with""quotes"'
            'REVENUE' -> '"REVENUE"'  # Preserves case

        Example for Databricks (backticks, ` escaped as ``):
            'my_field' -> '`my_field`'
            'field`with`ticks' -> '`field``with``ticks`'
        """
        pass

    @abstractmethod
    def wrap_metric(self, field_name: str) -> str:
        """
        Wrap a metric field for aggregation in SELECT clause.

        Metrics must be wrapped with backend-specific functions that signal
        to the database that this field is using semantic metric aggregation.
        This method returns the wrapped metric including proper identifier
        quoting.

        Args:
            field_name: Metric field name to wrap (e.g., 'revenue', 'cost')

        Returns:
            Wrapped metric with appropriate function (e.g., 'AGG("revenue")',
            'MEASURE(`cost`)')

        Note:
            - Snowflake requires AGG() for semantic metrics
            - Databricks requires MEASURE() for semantic metrics
            - The returned string includes proper identifier quoting

        Example:
            .. code-block:: python

                dialect = SnowflakeDialect()
                dialect.wrap_metric("revenue")  # Returns: AGG("revenue")

                dialect = DatabricksDialect()
                dialect.wrap_metric(
                    "revenue"
                )  # Returns: MEASURE(`revenue`)
        """
        pass

    def truncate_to_grain(self, column: str, unit: str) -> str:
        """
        Return the expression that truncates a date or timestamp column to a time grain.

        The result is the start of the period. A grain of a day or coarser is cast to
        ``DATE``, so the column has one type on every backend: Snowflake returns a ``DATE``
        for a ``DATE`` input while Databricks and DuckDB return a ``TIMESTAMP``. Finer grains
        stay timestamps. ``DATE_TRUNC('week', ...)`` starts weeks on Monday on Databricks
        and DuckDB; Snowflake overrides this method because its week depends on a session
        parameter.

        Args:
            column: The quoted column (or other SQL expression) to truncate.
            unit: One of :data:`semolina.fields.GRAINS`.

        Returns:
            A SQL expression.
        """
        truncated = f"DATE_TRUNC('{unit}', {column})"
        if unit in _DATE_GRAINS:
            return f"CAST({truncated} AS DATE)"
        return truncated

    @abstractmethod
    def normalize_identifier(self, name: str) -> str:
        """
        Fold a Python field name to the SQL column name the warehouse stores.

        Called when field.source is None. Each dialect knows its default
        identifier folding: Snowflake stores unquoted identifiers as UPPERCASE;
        Databricks and DuckDB as lowercase.

        Args:
            name: Python field name (e.g., 'order_id', 'revenue')

        Returns:
            SQL column name as stored in the warehouse (e.g., 'ORDER_ID' for
            Snowflake, 'order_id' for Databricks/DuckDB)
        """
        ...

    def quote_table_name(self, name: str) -> str:
        """
        Fold and quote a (possibly schema-qualified) table or view name.

        Splits ``name`` on ``.`` and processes each segment independently,
        mirroring how column identifiers are handled (see
        :meth:`normalize_identifier`):

        - A bare segment is folded via :meth:`normalize_identifier` (UPPERCASE
          for Snowflake, lowercase for Databricks/DuckDB) and then quoted, so a
          model declared ``view="sales_view"`` resolves against a standard
          view created unquoted in the warehouse.
        - A segment that is already quoted (wrapped in the dialect's quote
          character) is not folded — the escape hatch for a case-sensitive
          name, e.g. ``view='analytics."My_View"'``. The text between its
          quotes is the name, and is escaped like any other, so a quote inside
          it cannot end the identifier early. A doubled quote inside it is
          read as the standard SQL escape for one quote, so
          ``'"My""View"'`` and ``'"My"View"'`` both name ``My"View``.
        - An empty middle segment stays empty, so Snowflake's ``db..view`` shorthand for
          the default schema survives as ``"DB".."VIEW"``.

        Args:
            name: A table/view name, optionally schema-qualified
                (e.g. ``"sales_view"``, ``"analytics.sales_view"``).

        Returns:
            The fully-quoted reference (e.g. ``'"ANALYTICS"."SALES_VIEW"'``).

        Raises:
            ValueError: If a quoted segment contains a ``.``, which cannot be told
                apart from the separator, or a quote is left unbalanced.
        """
        quote_char = self.quote_identifier("x")[0]
        segments: list[str] = []
        parts = name.split(".")
        for index, segment in enumerate(parts):
            if not segment and 0 < index < len(parts) - 1:
                # Snowflake's db..view names the default schema; "" would be no name at all.
                segments.append(segment)
                continue
            opens, closes = segment.startswith(quote_char), segment.endswith(quote_char)
            if opens and closes and len(segment) >= 2:
                # A doubled quote is the standard SQL escape for one, so undo it first and
                # both "My""View" and the raw "My"View" name the view My"View.
                inner = segment[1:-1].replace(quote_char * 2, quote_char)
                segments.append(self.quote_identifier(inner))
            elif opens or closes:
                # A half-quoted segment is a dot inside a quoted pair only when the quotes in
                # the whole name pair up; otherwise a quote is simply missing.
                if name.count(quote_char) % 2:
                    msg = (
                        f"View name {name!r} has an unbalanced quote. Close the quoted "
                        f"segment with {quote_char}, or drop the quote."
                    )
                elif not _dot_inside_quotes(name, quote_char):
                    msg = (
                        f"View name {name!r} has text outside its quotes. Quote the whole "
                        f"segment, or drop the quotes."
                    )
                else:
                    msg = (
                        f"View name {name!r} has a '.' inside a quoted segment. Semolina splits "
                        f"a view name on '.', so it cannot tell that dot from a separator. "
                        f"Rename the view, or query it through a view whose name has no dot."
                    )
                raise ValueError(msg)
            else:
                segments.append(self.quote_identifier(self.normalize_identifier(segment)))
        return ".".join(segments)

    def create_builder(self) -> SQLBuilder:
        """
        Create the SQL builder for this dialect.

        Returns the appropriate SQLBuilder subclass instance. The default
        implementation returns a base SQLBuilder. DuckDB overrides this
        to return DuckDBSQLBuilder.

        Returns:
            SQLBuilder instance configured for this dialect.
        """
        return SQLBuilder(self)


class SnowflakeDialect(SQLDialect):
    """
    SQL dialect for Snowflake semantic views.

    Snowflake uses double quotes (") for identifier quoting and AGG() for
    metric aggregation. Internal quotes are escaped by doubling ("").

    Identifiers:
        - Unquoted identifiers are case-insensitive and stored as UPPERCASE
        - Quoted identifiers preserve case exactly
        - Always use this class to quote identifiers

    Metrics:
        - Wrapped with AGG() function defined in semantic view
        - AGG() only valid in queries against semantic views
    """

    kind: ClassVar[Dialect | None] = _dialect.Dialect.SNOWFLAKE

    @property
    def placeholder(self) -> str:
        """Return ? placeholder for the Snowflake ADBC driver (qmark paramstyle)."""
        return "?"

    def render_literal(self, value: Any) -> str:
        r"""
        Render a Python value as a Snowflake SQL literal.

        Snowflake binds its values, so this renders only what ``to_sql()`` shows. A Snowflake
        single-quoted string reads the backslash as an escape, so a string's backslashes are
        doubled before the standard rendering doubles its quotes: ``a\b`` is ``'a\\b'``.

        An aware datetime is cast to ``TIMESTAMP_TZ`` (``'...Z'::TIMESTAMP_TZ``): a bare
        ``TIMESTAMP`` literal is ``TIMESTAMP_NTZ`` by default, which would read it in the
        session's time zone rather than as the instant a bound execution compares against.

        Args:
            value: The Python value to render; see :meth:`SQLDialect.render_literal`.

        Returns:
            The literal, as :meth:`SQLDialect.render_literal` renders it, with a string's
            backslashes doubled and an aware datetime cast to ``TIMESTAMP_TZ``.
        """
        if isinstance(value, str):
            value = value.replace("\\", "\\\\")
        if isinstance(value, datetime.datetime) and value.tzinfo is not None:
            # A bare TIMESTAMP literal is TIMESTAMP_NTZ under Snowflake's default mapping,
            # which drops the Z and reads the value in the session's time zone.
            return f"'{_timestamp_literal_text(value)}'::TIMESTAMP_TZ"
        return super().render_literal(value)

    def quote_identifier(self, name: str) -> str:
        """
        Quote identifier using double quotes with escaping.

        Internal double quotes are escaped by doubling them ("" -> "").
        This prevents SQL injection and handles identifiers with quotes.

        Args:
            name: Unquoted identifier

        Returns:
            Double-quoted identifier with internal " escaped as ""

        Example:
            .. code-block:: text

                'column' -> '"column"'
                'my"field' -> '"my""field"'
                'REVENUE' -> '"REVENUE"'
        """
        escaped = name.replace('"', '""')
        return f'"{escaped}"'

    def wrap_metric(self, field_name: str) -> str:
        """
        Wrap metric using AGG() function.

        Args:
            field_name: Metric field name

        Returns:
            AGG() wrapped metric with quoted identifier

        Example:
            .. code-block:: text

                'revenue' -> 'AGG("revenue")'
        """
        return f"AGG({self.quote_identifier(field_name)})"

    def truncate_to_grain(self, column: str, unit: str) -> str:
        """
        Truncate to a grain, with weeks that start on Monday whatever ``WEEK_START`` says.

        Snowflake's ``DATE_TRUNC('week', ...)`` starts the week on the day the
        ``WEEK_START`` session parameter names, so it would differ between accounts and
        from the other backends. ``DAYOFWEEKISO`` ignores the parameter (Monday is 1), so
        stepping back to the Monday gives the ISO week start everywhere. Other grains use
        :meth:`SQLDialect.truncate_to_grain`.

        Args:
            column: The quoted column to truncate.
            unit: One of :data:`semolina.fields.GRAINS`.

        Returns:
            A SQL expression.
        """
        if unit == "week":
            return f"DATEADD('day', 1 - DAYOFWEEKISO({column}), CAST({column} AS DATE))"
        return super().truncate_to_grain(column, unit)

    def normalize_identifier(self, name: str) -> str:
        """
        Fold Python field name to Snowflake SQL column name.

        Snowflake stores unquoted identifiers as UPPERCASE.

        Args:
            name: Python field name

        Returns:
            Uppercase SQL column name
        """
        return name.upper()


class DatabricksDialect(SQLDialect):
    """
    SQL dialect for Databricks semantic views.

    Databricks uses backticks (`) for identifier quoting in standard mode and
    MEASURE() for metric aggregation. Internal backticks are escaped by
    doubling (`` -> ``).

    Identifiers:
        - Unquoted identifiers are case-insensitive and stored as lowercase
        - Backticks preserve case exactly
        - Always use this class to quote identifiers

    Metrics:
        - Wrapped with MEASURE() function defined in metric view
        - MEASURE() only valid in queries against metric views
        - Requires Databricks Runtime 12.2 LTS+
    """

    kind: ClassVar[Dialect | None] = _dialect.Dialect.DATABRICKS

    orders_selected_metrics_by_alias = True
    """
    Identifiers are case-insensitive, so the alias `` `revenue` `` shadows the measure.

    ``ORDER BY MEASURE(`revenue`)`` then resolves ``revenue`` to the select-list column and
    Databricks refuses it with ``METRIC_VIEW_INVALID_MEASURE_FUNCTION_INPUT``. Ordering by
    the alias names the same value.
    """

    quoted_identifiers_case_sensitive = False
    """
    Backtick-quoted identifiers ignore case.

    `` `Revenue` `` and `` `revenue` `` name one object, so either shadows a measure ``revenue``.
    """

    def render_literal(self, value: Any) -> str:
        r"""
        Render a Python value as a safe Spark-SQL literal.

        Spark SQL treats the backslash as an escape character inside string
        literals, so a backslash is doubled (``\`` -> ``\\``) *before* the
        single quote is escaped (``'`` -> ``\'``). Doing this in the wrong
        order would corrupt a value containing both characters.

        Args:
            value: The Python value to render (str, int, float, bool,
                ``decimal.Decimal``, ``datetime.date``, ``datetime.datetime``,
                ``uuid.UUID``, a numpy scalar of any of these, or None).

        Returns:
            A SQL literal: ``NULL`` for None, lowercase ``true``/``false`` for
            bool, the unquoted number for int/float, the bare fixed-point
            digits for a Decimal (already a Spark DECIMAL, so no CAST),
            ``DATE 'yyyy-mm-dd'`` for a date,
            ``TIMESTAMP 'yyyy-mm-ddThh:mm:ss'`` for a datetime (aware values
            normalized to UTC and suffixed ``Z``), or a single-quoted string
            with backslashes and single quotes backslash-escaped.

        Raises:
            ValueError: If the value is a non-finite float (inf/-inf/nan) or a
                non-finite Decimal (NaN/Infinity), neither of which has a valid
                SQL numeric-literal form.
            NotImplementedError: If the value type is not supported (a numpy
                ``datetime64`` or ``timedelta64`` included), so the caller fails
                loudly rather than emitting mis-escaped SQL.
        """
        value = _python_scalar(value)
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, float) and not math.isfinite(value):
            msg = f"Cannot render non-finite float as a SQL literal: {value!r}."
            raise ValueError(msg)
        if isinstance(value, int | float):
            # A subclass's repr() is its own (an IntEnum member's is <Tier.GOLD: 1>).
            return repr(int(value) if isinstance(value, int) else float(value))
        if isinstance(value, decimal.Decimal):
            if value.is_nan() or value.is_infinite():
                msg = f"Cannot render non-finite Decimal as a SQL literal: {value!r}."
                raise ValueError(msg)
            # Fixed-point, never str(): a D suffix or an exponent is what makes
            # a fractional literal a DOUBLE in Spark SQL, and a bare fractional
            # literal is already DECIMAL -- so no CAST wrapper is needed.
            return format(value, "f")
        # datetime is tested BEFORE date: datetime.datetime subclasses
        # datetime.date, exactly as bool subclasses int above. A date-first
        # branch would silently truncate a timestamp to a whole day.
        if isinstance(value, datetime.datetime):
            prefix, text = "TIMESTAMP ", _timestamp_literal_text(value)
        elif isinstance(value, datetime.date):
            prefix, text = "DATE ", value.isoformat()
        elif isinstance(value, uuid.UUID):
            prefix, text = "", str(value)
        elif isinstance(value, str):
            prefix, text = "", value
        else:
            type_name = type(cast("object", value)).__name__
            msg = (
                f"Cannot render SQL literal for unsupported type: {type_name}. "
                f"Add handling in render_literal() for this type."
            )
            raise NotImplementedError(msg)
        escaped = text.replace("\\", "\\\\").replace("'", "\\'")
        return f"{prefix}'{escaped}'"

    @property
    def placeholder(self) -> str:
        """Return the qmark ``?`` placeholder the Foundry ADBC driver binds."""
        return "?"

    def quote_identifier(self, name: str) -> str:
        """
        Quote identifier using backticks with escaping.

        Internal backticks are escaped by doubling them (`` -> ``).
        This prevents SQL injection and handles identifiers with backticks.

        Args:
            name: Unquoted identifier

        Returns:
            Backtick-quoted identifier with internal ` escaped as ``

        Example:
            .. code-block:: text

                'column' -> '`column`'
                'my`field' -> '`my``field`'
                'REVENUE' -> '`REVENUE`'
        """
        escaped = name.replace("`", "``")
        return f"`{escaped}`"

    def wrap_metric(self, field_name: str) -> str:
        """
        Wrap metric using MEASURE() function.

        Args:
            field_name: Metric field name

        Returns:
            MEASURE() wrapped metric with quoted identifier

        Example:
            .. code-block:: text

                'revenue' -> 'MEASURE(`revenue`)'
        """
        return f"MEASURE({self.quote_identifier(field_name)})"

    def normalize_identifier(self, name: str) -> str:
        """
        Fold Python field name to Databricks SQL column name.

        Databricks stores unquoted identifiers as lowercase.

        Args:
            name: Python field name

        Returns:
            Lowercase SQL column name
        """
        return name.lower()


class DuckDBDialect(SQLDialect):
    """
    SQL dialect for DuckDB semantic views.

    DuckDB semantic views use the ``semantic_view()`` table function instead
    of ``SELECT cols FROM view GROUP BY ALL``. Double quotes for identifier
    quoting, ``?`` placeholder (qmark paramstyle), and lowercase identifier
    normalization.

    Metric wrapping returns a plain quoted identifier because
    ``semantic_view()`` handles aggregation internally -- there is no
    ``AGG()`` or ``MEASURE()`` wrapper in DuckDB.
    """

    kind: ClassVar[Dialect | None] = _dialect.Dialect.DUCKDB

    quoted_identifiers_case_sensitive = False
    """
    DuckDB matches identifiers case-insensitively, quoted or not.

    ``"Territory"`` and ``"territory"`` name one column, so either shadows the other alias.
    """

    @property
    def placeholder(self) -> str:
        """Return ? placeholder for DuckDB ADBC qmark paramstyle."""
        return "?"

    def quote_identifier(self, name: str) -> str:
        """
        Quote identifier using double quotes with escaping.

        Args:
            name: Unquoted identifier

        Returns:
            Double-quoted identifier with internal " escaped as ""
        """
        escaped = name.replace('"', '""')
        return f'"{escaped}"'

    def wrap_metric(self, field_name: str) -> str:
        """
        Return plain quoted identifier (no AGG/MEASURE wrapping).

        DuckDB's semantic_view() handles aggregation internally.
        Metrics are referenced as plain quoted identifiers in ORDER BY.

        Args:
            field_name: Metric field name

        Returns:
            Quoted identifier without wrapping function
        """
        return self.quote_identifier(field_name)

    def render_literal(self, value: Any) -> str:
        r"""
        Render a Python value as a DuckDB SQL literal.

        A DuckDB dimension or fact filter goes into ``semantic_view()``'s ``where_clause``
        string, so its values are rendered rather than bound. This covers the values the
        DuckDB driver used to bind that the standard rendering has no form for, and leaves the
        rest to :meth:`SQLDialect.render_literal`.

        Args:
            value: The Python value to render: anything :meth:`SQLDialect.render_literal`
                takes, an aware ``datetime.time``, a ``datetime.timedelta``, or ``bytes``.

        Returns:
            ``TIMESTAMPTZ '...Z'`` for an aware datetime, ``TIMETZ 'hh:mm:ss+hh:mm'`` for an
            aware time, ``INTERVAL 'd days s seconds us microseconds'`` for a timedelta,
            ``'\xHH...'::BLOB`` (every byte escaped) for bytes, and otherwise the standard
            literal.

        Raises:
            ValueError: As :meth:`SQLDialect.render_literal`.
            NotImplementedError: As :meth:`SQLDialect.render_literal`.
        """
        value = _python_scalar(value)
        if isinstance(value, datetime.datetime) and value.tzinfo is not None:
            return f"TIMESTAMPTZ '{_timestamp_literal_text(value)}'"
        # utcoffset(), not tzinfo: a ZoneInfo time has a zone but, with no date to resolve
        # DST against, no offset, so it falls through to the base rendering's refusal.
        if isinstance(value, datetime.time) and value.utcoffset() is not None:
            return f"TIMETZ '{value.isoformat()}'"
        if isinstance(value, datetime.timedelta):
            return (
                f"INTERVAL '{value.days} days {value.seconds} seconds "
                f"{value.microseconds} microseconds'"
            )
        if isinstance(value, bytes | bytearray):
            return "'" + "".join(f"\\x{byte:02X}" for byte in value) + "'::BLOB"
        return super().render_literal(value)

    def normalize_identifier(self, name: str) -> str:
        """
        Fold Python field name to DuckDB SQL column name.

        DuckDB stores unquoted identifiers as lowercase.

        Args:
            name: Python field name

        Returns:
            Lowercase SQL column name
        """
        return name.lower()

    def create_builder(self) -> SQLBuilder:
        """
        Create DuckDBSQLBuilder for DuckDB semantic_view() SQL.

        Returns:
            DuckDBSQLBuilder instance configured for this dialect.
        """
        return DuckDBSQLBuilder(self)


class SQLBuilder:
    """
    Composable SQL builder for generating dialect-specific SQL.

    Uses an SQLDialect instance for backend-specific identifier quoting and
    metric wrapping. Generates SQL by composing individual clauses
    (SELECT, FROM, WHERE, GROUP BY, ORDER BY, LIMIT) rather than using
    an AST approach. This keeps SQL generation simple and focused on
    known query structure.

    Attributes:
        dialect: SQLDialect instance for backend-specific SQL rules

    Example:
        .. code-block:: python

            from semolina import SemanticView, Metric, Dimension
            from semolina.engines.sql import SQLBuilder, SnowflakeDialect


            class Sales(SemanticView, view="sales_view"):
                revenue = Metric()
                country = Dimension()


            query = (
                Sales.query()
                .metrics(Sales.revenue)
                .dimensions(Sales.country)
                .limit(100)
            )
            builder = SQLBuilder(SnowflakeDialect())
            sql = builder.build_select(query)
            # SELECT AGG("REVENUE") AS "revenue", "COUNTRY" AS "country"
            # FROM "SALES_VIEW"
            # GROUP BY ALL
            # LIMIT 100
    """

    def __init__(self, dialect: SQLDialect) -> None:
        """
        Initialize SQLBuilder with a dialect.

        Args:
            dialect: SQLDialect instance for backend-specific SQL generation
        """
        self.dialect = dialect

    def _resolve_col_name(self, field: Any) -> str:
        """
        Resolve the SQL-facing column name for a field.

        If the field has an explicit ``source`` set, that takes priority
        (used verbatim). Otherwise, the dialect's ``normalize_identifier``
        is called to fold the Python field name to the warehouse's storage
        convention (e.g., UPPERCASE for Snowflake, lowercase for Databricks).

        Args:
            field: Field descriptor with ``name`` and ``source`` attributes.

        Returns:
            SQL column name to use in generated SQL.
        """
        assert field.name is not None
        if field.source is not None:
            return field.source
        return self.dialect.normalize_identifier(field.name)

    def _dimension_expression(self, field: Any) -> str:
        """
        Return the SQL a selected dimension or fact is computed by.

        The quoted column, or for a :class:`~semolina.fields.GrainedDimension` its base
        column truncated by :meth:`SQLDialect.truncate_to_grain`.

        Args:
            field: A selected dimension or fact.

        Returns:
            A SQL expression.
        """
        from semolina.fields import GrainedDimension

        if isinstance(field, GrainedDimension):
            column = self.dialect.quote_identifier(self._resolve_col_name(field.base))
            return self.dialect.truncate_to_grain(column, field.unit)
        return self.dialect.quote_identifier(self._resolve_col_name(field))

    def _identifier_key(self, quoted: str) -> str:
        """
        Return a key under which two quoted identifiers are equal when they name one object.

        Args:
            quoted: A dialect-quoted identifier.

        Returns:
            ``quoted`` itself, or its case-folded form where quoted identifiers ignore case.
        """
        return quoted if self.dialect.quoted_identifiers_case_sensitive else quoted.casefold()

    def _field_alias(self, field: Any) -> str:
        """
        Return the quoted alias a selected field's result column is named with.

        The Python attribute name exactly, never folded: the point is that one model yields
        the same result keys on every backend.

        Args:
            field: Field descriptor with a ``name``.

        Returns:
            The dialect-quoted field name: ``"revenue"`` on Snowflake and DuckDB, and the
            name in backticks on Databricks.
        """
        assert field.name is not None
        return self.dialect.quote_identifier(field.name)

    def _compile_predicate(self, node: Predicate, *, wrap: bool = False) -> tuple[str, list[Any]]:
        """
        Compile a Predicate tree into a SQL fragment with bind parameters.

        Pattern-matches on the predicate node type and emits the appropriate
        SQL fragment and parameter list. Each value's slot is marked with ``_SLOT``,
        which :meth:`build_select_with_params` turns into the dialect's placeholder.

        Args:
            node: A Predicate tree node (leaf Lookup, or composite And/Or/Not)
            wrap: Name each column through :meth:`SQLDialect.wrap_metric` rather than
                :meth:`SQLDialect.quote_identifier`, for a ``HAVING`` on metrics.

        Returns:
            Tuple of (sql_fragment, params_list)

        Raises:
            NotImplementedError: For unknown Lookup subclasses
            TypeError: For non-Predicate input
        """
        ph = _SLOT
        q = self.dialect.wrap_metric if wrap else self.dialect.quote_identifier
        escape = f" ESCAPE {self.dialect.render_literal(LIKE_ESCAPE)}"

        match node:
            # -- Composite nodes -------------------------------------------
            case And(left=left, right=right):
                l_sql, l_params = self._compile_predicate(left, wrap=wrap)
                r_sql, r_params = self._compile_predicate(right, wrap=wrap)
                return f"({l_sql} AND {r_sql})", l_params + r_params

            case Or(left=left, right=right):
                l_sql, l_params = self._compile_predicate(left, wrap=wrap)
                r_sql, r_params = self._compile_predicate(right, wrap=wrap)
                return f"({l_sql} OR {r_sql})", l_params + r_params

            case Not(inner=inner):
                i_sql, i_params = self._compile_predicate(inner, wrap=wrap)
                return f"NOT ({i_sql})", i_params

            # -- Leaf lookups (order matters: specific before generic) ------
            # Predicates store Python field_name strings AND optional source= override.
            # Column resolution mirrors _resolve_col_name: use source verbatim when set,
            # otherwise apply dialect.normalize_identifier.
            # `== None` and `!= None` mean IS NULL and IS NOT NULL (D3): `= NULL` is never
            # true. Every other lookup refuses None when it is built.
            case Exact(field_name=f, value=v):
                nf = (
                    node.source if node.source is not None else self.dialect.normalize_identifier(f)
                )
                if v is None:
                    return f"{q(nf)} IS NULL", []
                return f"{q(nf)} = {ph}", [v]

            case NotEqual(field_name=f, value=v):
                nf = (
                    node.source if node.source is not None else self.dialect.normalize_identifier(f)
                )
                if v is None:
                    return f"{q(nf)} IS NOT NULL", []
                return f"{q(nf)} != {ph}", [v]

            case Gt(field_name=f, value=v):
                nf = (
                    node.source if node.source is not None else self.dialect.normalize_identifier(f)
                )
                return f"{q(nf)} > {ph}", [v]

            case Gte(field_name=f, value=v):
                nf = (
                    node.source if node.source is not None else self.dialect.normalize_identifier(f)
                )
                return f"{q(nf)} >= {ph}", [v]

            case Lt(field_name=f, value=v):
                nf = (
                    node.source if node.source is not None else self.dialect.normalize_identifier(f)
                )
                return f"{q(nf)} < {ph}", [v]

            case Lte(field_name=f, value=v):
                nf = (
                    node.source if node.source is not None else self.dialect.normalize_identifier(f)
                )
                return f"{q(nf)} <= {ph}", [v]

            case In(field_name=f, value=v):
                if not v:
                    return "1 = 0", []
                nf = (
                    node.source if node.source is not None else self.dialect.normalize_identifier(f)
                )
                placeholders = ", ".join(ph for _ in v)
                return f"{q(nf)} IN ({placeholders})", list(v)

            case Between(field_name=f, value=v):
                lo, hi = v
                nf = (
                    node.source if node.source is not None else self.dialect.normalize_identifier(f)
                )
                return f"{q(nf)} BETWEEN {ph} AND {ph}", [lo, hi]

            case IsNull(field_name=f, value=v):
                nf = (
                    node.source if node.source is not None else self.dialect.normalize_identifier(f)
                )
                if v:
                    return f"{q(nf)} IS NULL", []
                return f"{q(nf)} IS NOT NULL", []

            # The prefix, suffix and iexact lookups match their value literally: its `%`, `_`
            # and `!` are escaped and the condition names the escape. like()/ilike() take a
            # pattern, so they pass it through untouched.
            case StartsWith(field_name=f, value=v):
                nf = (
                    node.source if node.source is not None else self.dialect.normalize_identifier(f)
                )
                return f"{q(nf)} LIKE {ph}{escape}", [escape_like(v) + "%"]

            case IStartsWith(field_name=f, value=v):
                nf = (
                    node.source if node.source is not None else self.dialect.normalize_identifier(f)
                )
                return f"{q(nf)} ILIKE {ph}{escape}", [escape_like(v) + "%"]

            case EndsWith(field_name=f, value=v):
                nf = (
                    node.source if node.source is not None else self.dialect.normalize_identifier(f)
                )
                return f"{q(nf)} LIKE {ph}{escape}", ["%" + escape_like(v)]

            case IEndsWith(field_name=f, value=v):
                nf = (
                    node.source if node.source is not None else self.dialect.normalize_identifier(f)
                )
                return f"{q(nf)} ILIKE {ph}{escape}", ["%" + escape_like(v)]

            case Like(field_name=f, value=v):
                nf = (
                    node.source if node.source is not None else self.dialect.normalize_identifier(f)
                )
                return f"{q(nf)} LIKE {ph}", [v]

            case ILike(field_name=f, value=v):
                nf = (
                    node.source if node.source is not None else self.dialect.normalize_identifier(f)
                )
                return f"{q(nf)} ILIKE {ph}", [v]

            case IExact(field_name=f, value=v):
                nf = (
                    node.source if node.source is not None else self.dialect.normalize_identifier(f)
                )
                return f"{q(nf)} ILIKE {ph}{escape}", [escape_like(v)]

            # -- Catch-all: unknown Lookup subclass or non-Predicate ---------
            case _:
                # Cast through object to avoid reportUnknownMemberType
                # on Lookup[Unknown] from the match narrowing
                cls_name = type(cast("object", node)).__name__
                if isinstance(node, Lookup):
                    msg = (
                        f"Unsupported lookup type: {cls_name}. "
                        f"Add a case for it in _compile_predicate()."
                    )
                    raise NotImplementedError(msg)
                msg = (
                    f"Expected Predicate, got {cls_name}. "
                    f"Use Lookup subclasses or And/Or/Not composites."
                )
                raise TypeError(msg)

    def build_select_with_params(self, query: Query) -> tuple[str, list[Any]]:
        """
        Build a complete SELECT statement with parameterized bind values.

        Same as build_select() but returns (sql_template, params) for
        execution via cursor.execute(sql, params). A dialect that cannot bind
        gets its values inlined as literals and an empty parameter list.

        Args:
            query: Query object to convert to SQL

        Returns:
            Tuple of (sql_template, params_list). The sql_template contains
            dialect-specific placeholders ('?') instead of literal values.

        Raises:
            ValueError: If ``order_by()`` names a dimension or fact the query does not select.
        """
        sql, params = self._build_template(query)
        if not self.dialect.supports_parameterized_queries:
            return self._render_literal_sql(sql, params), []
        return sql.replace(_SLOT, self.dialect.placeholder), params

    def _build_template(self, query: Query) -> tuple[str, list[Any]]:
        """
        Build the statement with each value's slot marked by ``_SLOT``.

        Args:
            query: Query object to convert to SQL

        Returns:
            Tuple of (sql, params): the statement with one ``_SLOT`` per value, and the
            values in slot order.

        Raises:
            ValueError: If ``order_by()`` names a dimension or fact the query does not select.
        """
        self._refuse_unselected_order_dimensions(query)
        pre_filter, post_filter = self._split_filters(
            query.filters,
            self._query_model(query),
        )
        parts: list[str] = []
        all_params: list[Any] = []

        # Build main clauses
        parts.append(self._build_select_clause(query))
        parts.append(self._build_from_clause(query))

        # Dimension and fact filters apply before aggregation, metric filters after it, on
        # the wrapped metric: both warehouses refuse a metric in WHERE (D4).
        if pre_filter is not None:
            where_sql, where_params = self._compile_predicate(pre_filter)
            parts.append(f"WHERE {where_sql}")
            all_params.extend(where_params)

        if query.selected_dimensions:
            parts.append(self._build_group_by_clause(query))

        if post_filter is not None:
            having_sql, having_params = self._compile_predicate(post_filter, wrap=True)
            parts.append(f"HAVING {having_sql}")
            all_params.extend(having_params)

        if query.ordering:
            parts.append(self._build_order_by_clause(query))

        if query.limit_value is not None:
            parts.append(self._build_limit_clause(query))

        return "\n".join(parts), all_params

    def _refuse_unselected_order_dimensions(self, query: Query) -> None:
        """
        Refuse ``order_by()`` on a dimension or fact the query does not select.

        Every dialect refuses it, so one model means the same thing everywhere (D2-4 in
        ``53-DECISIONS.md``). On Snowflake and Databricks an aggregate query cannot order by
        a column it does not group by. On DuckDB the field would have to be requested from
        ``semantic_view()``, which groups the result by it and changes what one row means.
        A metric does not group, so ordering by an unselected metric is allowed.

        Args:
            query: Query object with ``_dimensions`` and ``_order_by_fields``.

        Raises:
            ValueError: Naming the first unselected dimension or fact, and saying to select
                it.
        """
        from semolina.fields import Fact, Metric, OrderTerm

        selected = {field.name for field in query.selected_dimensions}
        for field_spec in query.ordering:
            field = field_spec.field if isinstance(field_spec, OrderTerm) else field_spec
            if isinstance(field, Metric) or field.name in selected:
                continue
            kind = "fact" if isinstance(field, Fact) else "dimension"
            msg = (
                f"order_by() names {_qualified_name(field)}, a {kind} the query does not "
                f"select. Ordering by it would group the result by it too, which changes what "
                f"one row means. Add it to .dimensions(), or order by a selected field or a "
                f"metric."
            )
            raise ValueError(msg)

    def _render_literal_sql(self, sql_template: str, params: list[Any]) -> str:
        """
        Substitute placeholders with safe SQL literals for execution.

        Used when the dialect does not support bind parameters, for DuckDB's
        ``where_clause`` string, and for ``to_sql()``. Splits the template on
        ``_SLOT``, the mark the compiler leaves where a value goes, and
        interleaves rendered literals so each slot is filled exactly once.
        Splitting on ``?`` instead would also split a quoted name containing
        one, and a left-to-right ``str.replace`` would find a ``?`` inside a
        value already rendered. Each value is rendered through
        :meth:`SQLDialect.render_literal` -- never ``repr()``, which is unsafe for
        execution.

        Args:
            sql_template: SQL string with one ``_SLOT`` per value.
            params: Parameter values, in placeholder order.

        Returns:
            SQL with each placeholder replaced by a safe SQL literal.

        Raises:
            ValueError: If the placeholder count does not match ``len(params)``.
        """
        segments = sql_template.split(_SLOT)
        placeholder_count = len(segments) - 1
        if placeholder_count != len(params):
            msg = (
                f"Placeholder/param count mismatch while inlining literals: "
                f"{placeholder_count} placeholders but {len(params)} params."
            )
            raise ValueError(msg)
        out = [segments[0]]
        for segment, param in zip(segments[1:], params, strict=True):
            out.append(self.dialect.render_literal(param))
            out.append(segment)
        return "".join(out)

    def render_inline(self, sql_template: str, params: list[Any]) -> str:
        """
        Fill a bound statement's placeholders with the dialect's SQL literals.

        Each value goes through :meth:`SQLDialect.render_literal`, so the result is SQL the
        dialect would run: an apostrophe is escaped its way, a date is a ``DATE`` literal and
        a ``Decimal`` its digits. Each placeholder is filled once, so a ``?`` inside a value
        is never taken for the next placeholder. With no params there is nothing to fill, so
        the SQL comes back unchanged, as a statement from a non-binding dialect, with its
        values already inlined, does. Otherwise a ``?`` inside a quoted name cannot be told from a
        placeholder here; ``to_sql()`` renders from the compiler's own slots instead.

        Args:
            sql_template: SQL string with placeholders ('?')
            params: List of parameter values

        Returns:
            SQL string with the values substituted in as literals
        """
        if not params:
            return sql_template
        return self._render_literal_sql(
            sql_template.replace(self.dialect.placeholder, _SLOT), params
        )

    def build_select(self, query: Query) -> str:
        r"""
        Build a complete SELECT statement from a Query object.

        Orchestrates SQL generation by composing individual clauses
        (SELECT, FROM, WHERE, GROUP BY, ORDER BY, LIMIT). Each clause
        is built separately and joined with newlines.

        For parameterized output, use build_select_with_params() instead.
        This method fills each value's slot with the dialect's SQL literal.

        Args:
            query: Query object to convert to SQL

        Returns:
            Formatted SQL string with each clause on a new line,
            values rendered inline as the dialect's SQL literals

        Example:
            .. code-block:: python

                query = (
                    Sales.query()
                    .metrics(Sales.revenue)
                    .dimensions(Sales.country)
                )
                sql = builder.build_select(query)
                # SELECT AGG("REVENUE") AS "revenue", "COUNTRY" AS "country"
                # FROM "SALES_VIEW"
                # GROUP BY ALL
        """
        sql, params = self._build_template(query)
        return self._render_literal_sql(sql, params)

    def _build_select_clause(self, query: Query) -> str:
        """
        Build the SELECT clause with metrics and dimensions, each aliased to its field name.

        Metrics are wrapped using the dialect's wrap_metric() method
        (e.g., AGG("REVENUE") for Snowflake). Dimensions and facts are
        quoted using the dialect's quote_identifier() method
        (e.g., "COUNTRY" for Snowflake).

        Every item then carries ``AS`` the quoted Python field name, so the result column is
        named after the model's field on every backend. Without it the warehouse names the
        column after the expression it was sent: Snowflake answers ``AGG("REVENUE")`` and
        ``COUNTRY``, Databricks ``measure(revenue)``, and ``row.revenue`` fails on both. The
        alias is the exact attribute name, quoted so the warehouse keeps its case, and a
        ``source=`` field still selects its source column but returns under its own name.
        WHERE and ORDER BY keep using the expression itself (D2, ``53-DECISIONS.md``).

        Args:
            query: Query object with metrics and dimensions

        Returns:
            SELECT clause (e.g., 'SELECT AGG("REVENUE") AS "revenue", "COUNTRY" AS "country"')
        """
        select_items: list[str] = []

        # Add metrics (wrapped in AGG() or MEASURE())
        for metric in query.selected_metrics:
            col_name = self._resolve_col_name(metric)
            wrapped = self.dialect.wrap_metric(col_name)
            select_items.append(f"{wrapped} AS {self._field_alias(metric)}")

        # Add dimensions and facts (quoted identifiers), a truncated dimension as its expression
        for dim in query.selected_dimensions:
            select_items.append(f"{self._dimension_expression(dim)} AS {self._field_alias(dim)}")

        return "SELECT " + ", ".join(select_items)

    def _build_from_clause(self, query: Query) -> str:
        """
        Build the FROM clause using the view name from query fields.

        Extracts the view name from the first field's owner model
        (either from metrics or dimensions). Uses the dialect's
        quote_table_name() method to fold and quote the (possibly
        schema-qualified) view name.

        Args:
            query: Query object with metrics or dimensions

        Returns:
            FROM clause (e.g., 'FROM "sales_view"')
        """
        # Get view name from first field's owner
        view_name: str | None = None

        if query.selected_metrics:
            owner = query.selected_metrics[0].owner
            assert owner is not None
            view_name = owner._view_name

        elif query.selected_dimensions:
            owner = query.selected_dimensions[0].owner
            assert owner is not None
            view_name = owner._view_name

        assert view_name is not None, "View name not found on field owner"

        quoted_view = self.dialect.quote_table_name(view_name)
        return f"FROM {quoted_view}"

    def _build_group_by_clause(self, query: Query) -> str:
        """
        Build the GROUP BY clause using GROUP BY ALL.

        Both Snowflake and Databricks support GROUP BY ALL, which
        automatically groups by all non-aggregated SELECT columns.
        This is simpler and more maintainable than manually listing
        each dimension field.

        Args:
            query: Query object with dimensions

        Returns:
            GROUP BY clause (e.g., 'GROUP BY ALL')

        Note:
            GROUP BY ALL requires:
            - Snowflake: No version constraint
            - Databricks: Runtime 12.2 LTS+
        """
        return "GROUP BY ALL"

    def _select_aliases(self, query: Query) -> dict[str, Any]:
        """
        Map each select alias, keyed by the dialect's identifier rules, to its field.

        Args:
            query: Query object with metrics and dimensions.

        Returns:
            :meth:`_identifier_key` of each selected field's quoted alias -> the field.
        """
        selected = [*query.selected_metrics, *query.selected_dimensions]
        return {self._identifier_key(self._field_alias(f)): f for f in selected}

    def _order_by_column(self, field: Any, col_name: str, aliases: dict[str, Any]) -> str:
        """
        Name a plain column in ORDER BY, by its own alias when another alias shadows it.

        ORDER BY resolves a bare name to a select alias before a source column, so a column
        spelled like *another* field's alias would sort by that field. A selected field then
        orders by its own alias; an unselected one has none, and raises.

        Args:
            field: The field being ordered by.
            col_name: Its resolved source column.
            aliases: :meth:`_select_aliases` of the query.

        Returns:
            The quoted column, or the field's quoted alias when the column is shadowed.

        Raises:
            ValueError: If the column is shadowed and the field is not selected.
        """
        shadow = aliases.get(self._identifier_key(self.dialect.quote_identifier(col_name)))
        if shadow is None or shadow.name == field.name:
            return self.dialect.quote_identifier(col_name)
        if any(selected.name == field.name for selected in aliases.values()):
            return self._field_alias(field)
        raise ValueError(_shadowed_order_by_message(field, "column", col_name, shadow))

    def _build_order_by_clause(self, query: Query) -> str:
        """
        Build the ORDER BY clause from query order_by_fields.

        Handles both bare Field instances (sorted ascending by default)
        and OrderTerm instances (with explicit direction and NULLS
        handling). Metric fields are wrapped using the dialect's
        wrap_metric() method; other fields use quote_identifier().

        Args:
            query: Query object with order_by_fields

        Returns:
            ORDER BY clause (e.g., 'ORDER BY "revenue" DESC NULLS FIRST')
        """
        from semolina.fields import GrainedDimension, Metric, OrderTerm

        # By name: Field.__eq__ builds a filter, so `field in query.selected_metrics` is always
        # true.
        selected_metrics = {field.name for field in query.selected_metrics}
        aliases = self._select_aliases(query)

        def _quote_field(field: Any) -> str:
            if isinstance(field, GrainedDimension):
                # Selected (unselected dimensions are refused first), so its alias names it.
                return self._field_alias(field)
            col_name = self._resolve_col_name(field)
            if not isinstance(field, Metric):
                return self._order_by_column(field, col_name, aliases)
            # A select alias spelled like the measure shadows it, its own alias included: the
            # wrapped expression would resolve to the alias (measured live on Snowflake and
            # Databricks).
            shadow = aliases.get(self._identifier_key(self.dialect.quote_identifier(col_name)))
            if field.name in selected_metrics:
                if self.dialect.orders_selected_metrics_by_alias or shadow is not None:
                    return self._field_alias(field)
            elif shadow is not None:
                raise ValueError(_shadowed_order_by_message(field, "measure", col_name, shadow))
            return self.dialect.wrap_metric(col_name)

        order_items: list[str] = []

        for field_spec in query.ordering:
            # Handle OrderTerm (from field.asc() or field.desc())
            if isinstance(field_spec, OrderTerm):
                field = field_spec.field
                quoted_field = _quote_field(field)

                # Add direction
                direction = "DESC" if field_spec.descending else "ASC"
                order_item = f"{quoted_field} {direction}"

                # Add NULLS clause if specified
                if field_spec.nulls.value is not None:  # Check if not DEFAULT
                    nulls_clause = field_spec.nulls.value
                    order_item += f" NULLS {nulls_clause}"

                order_items.append(order_item)

            # Handle bare Field (defaults to ascending)
            else:
                order_items.append(f"{_quote_field(field_spec)} ASC")

        return "ORDER BY " + ", ".join(order_items)

    def _build_limit_clause(self, query: Query) -> str:
        """
        Build the LIMIT clause from query limit value.

        Args:
            query: Query object with limit_value set

        Returns:
            LIMIT clause (e.g., 'LIMIT 100')
        """
        return f"LIMIT {query.limit_value}"

    @staticmethod
    def _query_model(query: Query) -> Any:
        """
        Return the model a query's fields belong to, or ``None`` when it cannot tell.

        Args:
            query: Query object.

        Returns:
            ``query.model``, else the owner of its first selected field, else ``None``.
        """
        model = getattr(query, "_model", None)
        if model is not None:
            return model
        for field in (*query.selected_metrics, *query.selected_dimensions):
            if field.owner is not None:
                return field.owner
        return None

    @staticmethod
    def _lookup_field(node: Lookup[Any], model: Any) -> Any:
        """
        Return the model field a lookup filters on, or ``None`` when the model lacks it.

        Args:
            node: A leaf predicate.
            model: The query's model, or ``None``.

        Returns:
            The field descriptor, or ``None``.
        """
        if model is None:
            return None
        return model._fields.get(node.field_name)

    def _leaves(self, node: Predicate) -> list[Lookup[Any]]:
        """
        Return a predicate tree's leaves, left to right.

        Args:
            node: A predicate tree.

        Returns:
            Every ``Lookup`` in the tree.
        """
        match node:
            case And(left=left, right=right) | Or(left=left, right=right):
                return self._leaves(left) + self._leaves(right)
            case Not(inner=inner):
                return self._leaves(inner)
            case Lookup():
                return [cast("Lookup[Any]", node)]
            case _:
                # Not a predicate: _compile_predicate raises the TypeError that says so.
                return []

    def _is_metric_filter(self, node: Lookup[Any], model: Any) -> bool:
        """
        Say whether a leaf filters on a metric, and so must apply after aggregation.

        A field the model does not have is treated as a dimension, so it reaches the
        pre-aggregation clause and the warehouse names it in its own error.

        Args:
            node: A leaf predicate.
            model: The query's model, or ``None``.

        Returns:
            ``True`` for a metric.
        """
        from semolina.fields import Metric

        return isinstance(self._lookup_field(node, model), Metric)

    def _conjuncts(self, node: Predicate) -> list[Predicate]:
        """
        Flatten a tree of top-level ``AND`` into its conjuncts, left to right.

        Args:
            node: A predicate tree.

        Returns:
            The operands of every top-level ``AND``; the node itself when it is not one.
        """
        if isinstance(node, And):
            return self._conjuncts(node.left) + self._conjuncts(node.right)
        return [node]

    def _split_filters(
        self, filters: Predicate | None, model: Any
    ) -> tuple[Predicate | None, Predicate | None]:
        """
        Split a query's filter into its pre-aggregation and post-aggregation parts.

        A filter on dimensions and facts only applies before aggregation whole (``WHERE``, or
        DuckDB's ``where_clause``), and one on metrics only after it whole (``HAVING``, or
        DuckDB's outer ``WHERE``). A mixed filter is split at its top-level ``AND``: each
        conjunct goes to one side or the other. A conjunct that mixes both kinds under
        ``OR`` or ``NOT`` has no such split and is refused.

        Args:
            filters: The query's predicate tree, or ``None``.
            model: The query's model, or ``None``.

        Returns:
            ``(pre, post)``: the pre-aggregation and post-aggregation predicates, either of
            which may be ``None``.

        Raises:
            ValueError: If a dimension or fact and a metric are combined under ``OR`` or
                ``NOT``, naming the fields.
        """
        if filters is None:
            return None, None
        leaves = self._leaves(filters)
        if not any(self._is_metric_filter(leaf, model) for leaf in leaves):
            return filters, None
        if all(self._is_metric_filter(leaf, model) for leaf in leaves):
            return None, filters

        pre: list[Predicate] = []
        post: list[Predicate] = []
        for conjunct in self._conjuncts(filters):
            conjunct_leaves = self._leaves(conjunct)
            metrics = [leaf for leaf in conjunct_leaves if self._is_metric_filter(leaf, model)]
            if not metrics:
                pre.append(conjunct)
            elif len(metrics) == len(conjunct_leaves):
                post.append(conjunct)
            else:
                self._refuse_mixed_filter(conjunct_leaves, model)
        return _and_all(pre), _and_all(post)

    def _refuse_mixed_filter(self, leaves: list[Lookup[Any]], model: Any) -> None:
        """
        Raise the error for a dimension and a metric combined under ``OR`` or ``NOT``.

        Args:
            leaves: The leaves of the offending conjunct.
            model: The query's model, or ``None``.

        Raises:
            ValueError: Always, naming the dimension and metric fields involved.
        """

        def names(want_metric: bool) -> str:
            found: list[str] = []
            for leaf in leaves:
                if self._is_metric_filter(leaf, model) is not want_metric:
                    continue
                field = self._lookup_field(leaf, model)
                name = _qualified_name(field) if field is not None else leaf.field_name
                if name not in found:
                    found.append(name)
            return ", ".join(found)

        msg = (
            f"Semolina cannot apply a filter that combines {names(False)} with the metric "
            f"{names(True)} under OR or NOT. A dimension or fact filter applies before "
            f"aggregation and a metric filter after it, so the two can only be joined with "
            f"AND at the top of the filter. Restate the condition that way if it has an "
            f"equivalent."
        )
        raise ValueError(msg)


class DuckDBSQLBuilder(SQLBuilder):
    """
    SQL builder for DuckDB semantic_view() table function.

    Overrides ``_build_template()`` to query the ``semantic_view()`` table function
    instead of producing ``SELECT cols FROM view GROUP BY ALL``, while keeping the direct-SQL
    semantics Snowflake has (option A in ``53-DECISIONS.md``)::

        SELECT "revenue" AS "revenue", "country" AS "country"
        FROM semantic_view('sales_view', dimensions := ['country'], metrics := ['revenue'],
                           where_clause := ?)
        WHERE "revenue" > ?
        ORDER BY "revenue" DESC
        LIMIT 10

    - The outer ``SELECT`` names every selected field under its Python name, as the other
      dialects' aliases do. The extension has no query-time alias of its own.
    - Dimension and fact filters go into the call as ``where_clause``, which applies before
      aggregation like Snowflake's ``WHERE``. A filter on an unselected dimension therefore
      narrows the rows without regrouping them.
    - Metric filters go into an outer ``WHERE``, which applies after aggregation like
      Snowflake's ``HAVING``. The extension refuses a metric in ``where_clause``.
    - ``ORDER BY`` and ``LIMIT`` are outer SQL. An unselected metric that is filtered or
      ordered on is requested from the call but not projected; a metric does not group, so
      requesting it changes no grain.

    Note:
        The ``facts`` and ``metrics`` parameters cannot be combined in the
        same ``semantic_view()`` call. This builder raises ``ValueError``
        if both are present.
    """

    def _build_template(self, query: Query) -> tuple[str, list[Any]]:
        """
        Build DuckDB semantic_view() SQL with each value's slot marked.

        The dimension and fact filter is bound as one parameter: the whole ``where_clause``
        string, with its values rendered into it by :meth:`SQLDialect.render_literal`. A ``?``
        inside the clause cannot be bound (measured on the extension), but binding the
        clause itself works, and keeps the one audited escaping site.

        Args:
            query: Query object to convert to SQL

        Returns:
            Tuple of (sql_template, params_list). The ``where_clause`` string, when there is
            one, is the first parameter, then the outer ``WHERE`` values.

        Raises:
            ValueError: If both facts and metrics are present in the query, if a filter mixes
                a dimension and a metric under ``OR`` or ``NOT``, or if ``order_by()`` names
                a dimension or fact the query does not select.
        """
        from semolina.fields import Fact, GrainedDimension

        self._refuse_unselected_order_dimensions(query)
        model = self._query_model(query)
        pre_filter, post_filter = self._split_filters(
            query.filters,
            model,
        )

        # Separate dimensions from facts in the _dimensions tuple
        dim_names: list[str] = []
        fact_names: list[str] = []
        for field in query.selected_dimensions:
            if isinstance(field, GrainedDimension):
                # An expression item (duckdb-semantic-views >= 0.16.0), named by its alias
                # so the outer SELECT and ORDER BY see it as a plain column.
                column = self.dialect.quote_identifier(self._resolve_col_name(field.base))
                truncated = self.dialect.truncate_to_grain(column, field.unit)
                dim_names.append(f"{truncated} AS {self._field_alias(field)}")
                continue
            col_name = self._resolve_col_name(field)
            if isinstance(field, Fact):
                fact_names.append(col_name)
            else:
                dim_names.append(col_name)

        metric_names = [self._resolve_col_name(m) for m in query.selected_metrics]
        for field in self._metrics_to_request(query, post_filter, model):
            col_name = self._resolve_col_name(field)
            if col_name not in metric_names:
                metric_names.append(col_name)

        # Validate: facts and metrics cannot be combined
        if fact_names and metric_names:
            raise ValueError(
                "DuckDB semantic_view() does not support combining facts and metrics "
                "in the same query. Use either .metrics() with .dimensions() for "
                "grouped queries, or .dimensions(fact_field) for fact queries."
            )

        # Get view name from first field's owner
        view_name: str | None = None
        if query.selected_metrics:
            owner = query.selected_metrics[0].owner
            assert owner is not None
            view_name = owner._view_name
        elif query.selected_dimensions:
            owner = query.selected_dimensions[0].owner
            assert owner is not None
            view_name = owner._view_name
        assert view_name is not None, "View name not found on field owner"

        # Build semantic_view() arguments
        # Every name below is interpolated into a SQL string literal, so every one of them
        # goes through `sql_str_literal`. A field's name can reach here from the warehouse
        # catalogue rather than from the user's own model file (`semolina codegen --check`
        # builds its probe query from `DESCRIBE SELECT` / `SHOW COLUMNS` output), and both
        # Snowflake and DuckDB permit a quoted identifier containing a quote.
        sv_args: list[str] = []
        if dim_names:
            dims_list = ", ".join(sql_str_literal(n) for n in dim_names)
            sv_args.append(f"dimensions := [{dims_list}]")
        if metric_names:
            metrics_list = ", ".join(sql_str_literal(n) for n in metric_names)
            sv_args.append(f"metrics := [{metrics_list}]")
        if fact_names:
            facts_list = ", ".join(sql_str_literal(n) for n in fact_names)
            sv_args.append(f"facts := [{facts_list}]")

        all_params: list[Any] = []
        if pre_filter is not None:
            clause_sql, clause_params = self._compile_predicate(pre_filter)
            sv_args.append(f"where_clause := {_SLOT}")
            all_params.append(self._render_literal_sql(clause_sql, clause_params))

        sv_call = f"semantic_view({sql_str_literal(view_name)}, {', '.join(sv_args)})"

        select_clause = self._build_select_clause(query)
        if fact_names:
            # semantic_view() returns facts row by row, while Snowflake and Databricks group
            # every query with GROUP BY ALL. DISTINCT gives a fact query the same rows here.
            # Facts never combine with metrics (refused above), so nothing is aggregated.
            select_clause = "SELECT DISTINCT " + select_clause.removeprefix("SELECT ")
        parts = [select_clause, f"FROM {sv_call}"]

        if post_filter is not None:
            where_sql, where_params = self._compile_predicate(post_filter)
            parts.append(f"WHERE {where_sql}")
            all_params.extend(where_params)

        if query.ordering:
            parts.append(self._build_order_by_clause(query))

        if query.limit_value is not None:
            parts.append(self._build_limit_clause(query))

        return "\n".join(parts), all_params

    def _dimension_expression(self, field: Any) -> str:
        """
        Name a selected dimension in the outer ``SELECT`` by the column the call returns.

        A truncated dimension is computed inside ``semantic_view()`` under its alias, so
        the outer query selects that column rather than repeating the expression.

        Args:
            field: A selected dimension or fact.

        Returns:
            The quoted column.
        """
        from semolina.fields import GrainedDimension

        if isinstance(field, GrainedDimension):
            return self._field_alias(field)
        return super()._dimension_expression(field)

    def _metrics_to_request(
        self, query: Query, post_filter: Predicate | None, model: Any
    ) -> list[Any]:
        """
        Return the metrics the outer ``WHERE`` and ``ORDER BY`` refer to.

        The outer clauses can only see columns the call returns, so each of these metrics
        is requested from ``semantic_view()``, selected or not. An unselected one is not
        projected. Dimensions never appear here: a dimension filter goes into
        ``where_clause``, and an unselected dimension in ``order_by()`` is refused.

        Args:
            query: Query object.
            post_filter: The outer ``WHERE`` predicate, or ``None``.
            model: The query's model, or ``None``.

        Returns:
            Metric field descriptors, in first-reference order.
        """
        from semolina.fields import Metric, OrderTerm

        fields: list[Any] = []
        if post_filter is not None:
            for leaf in self._leaves(post_filter):
                field = self._lookup_field(leaf, model)
                if field is not None:
                    fields.append(field)
        for field_spec in query.ordering:
            field = field_spec.field if isinstance(field_spec, OrderTerm) else field_spec
            if isinstance(field, Metric):
                fields.append(field)
        return fields

    def _build_order_by_clause(self, query: Query) -> str:
        """
        Build ORDER BY clause using plain quoted identifiers for all fields.

        DuckDB semantic_view() returns columns directly -- ORDER BY uses
        plain quoted identifiers for all field types including metrics.
        No AGG()/MEASURE() wrapping.

        Args:
            query: Query object with order_by_fields

        Returns:
            ORDER BY clause
        """
        from semolina.fields import OrderTerm

        aliases = self._select_aliases(query)
        order_items: list[str] = []

        for field_spec in query.ordering:
            if isinstance(field_spec, OrderTerm):
                col_name = self._resolve_col_name(field_spec.field)
                quoted = self._order_by_column(field_spec.field, col_name, aliases)
                direction = "DESC" if field_spec.descending else "ASC"
                order_item = f"{quoted} {direction}"
                if field_spec.nulls.value is not None:
                    order_item += f" NULLS {field_spec.nulls.value}"
                order_items.append(order_item)
            else:
                col_name = self._resolve_col_name(field_spec)
                order_items.append(f"{self._order_by_column(field_spec, col_name, aliases)} ASC")

        return "ORDER BY " + ", ".join(order_items)


_BUILT_IN_DIALECTS: tuple[type[SQLDialect], ...] = (
    SnowflakeDialect,
    DatabricksDialect,
    DuckDBDialect,
)
"""The dialect classes Semolina ships, one per :class:`~semolina.dialect.Dialect` member."""


def __getattr__(name: str) -> Any:
    """
    Resolve ``Dialect``, the name :class:`SQLDialect` had before 0.7.0, with a warning.

    Kept for one release (D7). The enum :class:`semolina.dialect.Dialect` is now the only
    thing called ``Dialect``.

    Args:
        name: The attribute being looked up on the module.

    Returns:
        :class:`SQLDialect` for ``Dialect``.

    Raises:
        AttributeError: For any other name, as a missing module attribute would.
    """
    if name == "Dialect":
        warnings.warn(
            "semolina.engines.sql.Dialect is renamed SQLDialect; the old name will be "
            "removed in the next release.",
            DeprecationWarning,
            stacklevel=2,
        )
        return SQLDialect
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
