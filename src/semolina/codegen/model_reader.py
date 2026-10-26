"""
Read a committed model file's declared fields without importing it.

``semolina codegen --check`` needs one half of its comparison from a file the user points
at: the fields a model *currently* declares. This module recovers them from the file's
source, which interprets the file structurally and **executes nothing**.

Importing the module would work — ``__orig_class__`` survives at runtime, so
``Metric[decimal.Decimal | None]()`` is introspectable — and it is still the wrong route.
Importing runs whatever the file's module level contains, which in CI means running code
out of a repo checkout because someone passed ``--model`` (threat T-48-19). D9 measured a
credential-free subprocess as well, and it does not help: the imported code can still read
the credentials on disk. It also requires the file to be an importable package member.

Since D9 (2026-10-15) the reading goes through :mod:`semolina.codegen.static_reader`, which
uses griffe to resolve names without importing anything:

* a field is recognised by what its constructor *refers to*, so ``semolina.Metric[T]()``,
  ``M[T]()`` after ``from semolina import Metric as M`` and an annotated assignment
  ``x: Dimension[str] = Dimension[str]()`` all read as fields;
* ``view=`` may name a string constant, in the same file or imported from another;
* fields declared on a base class, in the same file or another module, are read as the
  subclass's own, with a subclass's declaration replacing its base's;
* each annotation is also recorded in codegen's spelling with its names resolved
  (:func:`semolina.codegen.type_compare.canonical_annotation`), so ``Optional[Decimal]``
  compares equal to ``decimal.Decimal | None``.

**No field-like statement is skipped silently** (GEN-12). A statement the reader positively
identifies as *not* a field (a class with no ``view=`` keyword, a ``ClassVar``, a call to a
builtin such as ``dict[str, int]()``) is skipped. Anything else that might declare a field
and is not read — a constructor whose name does not resolve, a field declared inside an
``if`` block or in a tuple assignment, a base class that cannot be found, a ``view=`` that is
not a string — becomes a warning naming the class and the statement. The CLI prints each one
beside the report.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from semolina.codegen.static_reader import ClassSource, StaticModule, walk_statement
from semolina.codegen.type_compare import ALLOWED_MODULES, canonical_annotation

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Mapping

    import griffe

    from semolina.engines.sql import SQLDialect

__all__ = [
    "FIELD_CLASSES",
    "CommittedField",
    "CommittedModel",
    "CommittedModelFile",
    "normalize_view_name",
    "read_committed_model_file",
    "read_committed_model",
]


FIELD_CLASSES = frozenset({"Metric", "Dimension", "Fact"})
"""The three field descriptors a generated model can declare."""

_FIELD_QUALNAMES = {
    f"{module}.{name}": name for module in ("semolina", "semolina.fields") for name in FIELD_CLASSES
}
"""Every qualified name a field class can be reached by."""

_UNTYPED_ANNOTATION = "Any"
"""
Annotation recorded for the documented ``Metric()`` shorthand.

``Metric()`` is legal and means ``Metric[Any]()``. Recording it as ``Any`` rather than
dropping the field keeps it in the drift report as the untyped fallback it is — which is also
exactly what the renderer emits for a warehouse type the map has no entry for.
"""


@dataclass(frozen=True)
class CommittedField:
    """
    One field declaration read out of a committed model.

    Attributes:
        name: The Python attribute name the field is assigned to.
        field_class: ``'Metric'``, ``'Dimension'`` or ``'Fact'``.
        annotation: The subscript exactly as written, e.g. ``'Optional[Decimal]'``. Shown
            in the report, so a drifted row reads as the file does.
        source_name: The ``source="..."`` override when the declaration carries one,
            otherwise None.
        resolved_annotation: The same annotation with its names resolved, in codegen's
            spelling (``'decimal.Decimal | None'``), which is what the check compares. None
            when it uses a name or syntax the comparison cannot resolve, and the text as
            written is compared instead.
        metadata: The metadata arguments the declaration passes, by name (``label``,
            ``description``, ``synonyms``, ``semantic_format``), as literal values: a string,
            a tuple of strings, or the format's dict. A field with no ``description=`` but
            an attribute docstring right after it reads the docstring as its description,
            which is how codegen wrote it before ``description=`` existed. An argument the
            reader cannot evaluate is warned about and left out, so it is not compared.
    """

    name: str
    field_class: str
    annotation: str
    source_name: str | None
    resolved_annotation: str | None = None
    metadata: Mapping[str, object] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True)
class CommittedModel:
    """
    One ``SemanticView`` subclass read out of a committed model file.

    Attributes:
        class_name: The Python class name.
        view_name: The ``view=`` keyword's value — the warehouse identifier.
        fields: Field name -> declaration: inherited fields first, then the class's own,
            each in source order.
        description: The class's ``description=`` keyword, or None when it passes none or
            passes something the reader cannot evaluate (warned about).
    """

    class_name: str
    view_name: str
    fields: dict[str, CommittedField]
    description: str | None = None


@dataclass(frozen=True)
class CommittedModelFile:
    """
    Everything read out of a committed model file.

    Attributes:
        models: One entry per ``SemanticView`` subclass, in source order.
        warnings: One line per statement that might declare a field and was not read, or
            per class or base class that was not followed. Printed by the CLI, never
            dropped.
    """

    models: list[CommittedModel]
    warnings: list[str]


def normalize_view_name(name: str, dialect: SQLDialect) -> str:
    """
    Fold a view name the way the engine compares identifiers.

    Two spellings fold to the same key when the engine reads them as the same view:

    * **DuckDB** matches identifiers case-insensitively, quoted or not, and a name in its
      default ``main`` schema may be written with or without it, so ``main.sales``,
      ``SALES`` and ``"sales"`` all name the ``view="sales"`` class.
    * **Snowflake** folds an unquoted name to upper case and keeps a quoted one exactly,
      so ``sales`` matches ``"SALES"`` but ``"Sales"`` is a different view.
    * **Databricks** strips backticks and ignores case, but keeps every schema, since
      ``main`` is an ordinary name there.

    Args:
        name: A view name as written on the command line or in ``view=``, possibly
            schema-qualified and possibly quoted.
        dialect: The engine's dialect, whose identifier rules decide the folding.

    Returns:
        The folded name. A name the dialect cannot quote (an unbalanced quote, a dot inside
        quotes) is returned as written, so it matches only the same spelling.

    Example:
        .. code-block:: python

            from semolina.codegen.model_reader import normalize_view_name
            from semolina.engines.sql import DuckDBDialect, SnowflakeDialect

            normalize_view_name('main."Sales"', DuckDBDialect())
            # '"sales"'
            normalize_view_name('"Sales"', SnowflakeDialect()) == normalize_view_name(
                "SALES", SnowflakeDialect()
            )
            # False
    """
    from semolina.engines.sql import DuckDBDialect

    try:
        folded = dialect.quote_table_name(name)
    except ValueError:
        return name
    if dialect.quoted_identifiers_case_sensitive:
        return folded
    # DuckDB and Databricks match identifiers case-insensitively, quoted or not.
    folded = folded.casefold()
    parts = folded.split(".")
    if isinstance(dialect, DuckDBDialect) and len(parts) == 2 and parts[0] == '"main"':
        return parts[1]
    return folded


def _callee(call: ast.Call) -> tuple[ast.expr, ast.expr | None]:
    """
    Split a constructor call into the class it calls and its subscript.

    Args:
        call: ``Metric[T](...)`` or ``Metric(...)``.

    Returns:
        The callee (``Metric``) and the subscript (``T``), or None for an unsubscripted call.
    """
    if isinstance(call.func, ast.Subscript):
        return call.func.value, call.func.slice
    return call.func, None


class _ClassReader:
    """Reads one class body's field declarations, recording what it cannot read."""

    def __init__(self, static: StaticModule, source: ClassSource, warnings: list[str]) -> None:
        self.static = static
        self.source = source
        self.scope: griffe.Object | griffe.Alias = source.scope
        self.warnings = warnings
        self.removed: set[str] = set()

    def warn(self, statement: ast.AST, reason: str) -> None:
        """
        Record a statement that was not read.

        Args:
            statement: The statement.
            reason: Why it was not read.
        """
        text = ast.unparse(statement).splitlines()[0]
        self.warnings.append(
            f"{self.source.label} (line {getattr(statement, 'lineno', '?')}): {text!r} {reason}"
        )

    def field_class(self, callee: ast.expr) -> str | None:
        """
        Name the field class a constructor refers to.

        Args:
            callee: The called expression, e.g. ``Metric`` or ``semolina.Metric``.

        Returns:
            ``'Metric'``, ``'Dimension'`` or ``'Fact'``, or None when it is not one.
        """
        qualname = self.static.qualname(callee, self.scope)
        if qualname is not None:
            return _FIELD_QUALNAMES.get(qualname)
        # A name that does not resolve at all, e.g. after `from semolina import *`, which
        # griffe cannot expand without reading semolina. The bare names keep their meaning.
        if isinstance(callee, ast.Name) and callee.id in FIELD_CLASSES:
            return callee.id
        return None

    def is_known_non_field(self, callee: ast.expr) -> bool:
        """
        Decide whether a constructor is positively something other than a field class.

        Args:
            callee: The called expression.

        Returns:
            True for a builtin or a name from a standard module (``dict[str, int]()``,
            ``datetime.date.today()``), which cannot declare a field. False for anything
            unknown, which might.
        """
        qualname = self.static.qualname(callee, self.scope)
        if qualname is None or qualname in _FIELD_QUALNAMES:
            return False
        return any(
            qualname.startswith(f"{module}.") for module in ALLOWED_MODULES if module != "semolina"
        )

    def contains_field_call(self, node: ast.AST) -> bool:
        """
        Decide whether a statement constructs a field anywhere inside it.

        Args:
            node: The statement.

        Returns:
            True when any call in it constructs ``Metric``, ``Dimension`` or ``Fact``.
        """
        return any(
            isinstance(child, ast.Call) and self.field_class(_callee(child)[0]) is not None
            for child in walk_statement(node)
        )

    def read_assignment(self, statement: ast.stmt) -> CommittedField | None:
        """
        Read a field from an assignment, warning when it might be one and cannot be read.

        Args:
            statement: A statement from the class body.

        Returns:
            The field, or None when the statement declares none that could be read.
        """
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            target, value = statement.target, statement.value
        else:
            return self.read_other(statement)
        if not isinstance(target, ast.Name) or not isinstance(value, ast.Call):
            return self.read_other(statement)

        callee, subscript = _callee(value)
        field_class = self.field_class(callee)
        if field_class is None:
            if not self.is_known_non_field(callee):
                self.warn(
                    statement,
                    "was not read as a field: its constructor does not resolve to "
                    "semolina's Metric, Dimension or Fact",
                )
            return None

        if subscript is None:
            annotation = resolved = _UNTYPED_ANNOTATION
        else:
            annotation = ast.unparse(subscript)
            resolved = canonical_annotation(
                subscript, self.static.resolver(self.scope), self.static.final_path
            )
        source_name = self.source_name_of(statement, value)
        return CommittedField(
            name=target.id,
            field_class=field_class,
            annotation=annotation,
            source_name=source_name,
            resolved_annotation=resolved,
            metadata=self.metadata_of(statement, value),
        )

    def metadata_of(self, statement: ast.stmt, call: ast.Call) -> dict[str, object]:
        """
        Read a field declaration's metadata arguments as literal values.

        Args:
            statement: The declaring statement, for a warning.
            call: The field constructor call.

        Returns:
            Argument name -> value, for each metadata argument the reader could evaluate.
        """
        metadata: dict[str, object] = {}
        for keyword in call.keywords:
            if keyword.arg not in METADATA_ARGUMENTS:
                continue
            value = self.literal(keyword.value)
            if value is _UNREADABLE:
                self.warn(
                    statement,
                    f"has a {keyword.arg}= the reader cannot evaluate; it was not compared",
                )
                continue
            metadata[keyword.arg] = value
        return metadata

    def literal(self, node: ast.expr) -> object:
        """
        Evaluate a metadata argument: a literal, or a string constant by name.

        Args:
            node: The argument's expression.

        Returns:
            The value, with lists read as tuples so ``synonyms`` compares by value, or
            :data:`_UNREADABLE`.
        """
        text = self.static.string_constant(node, self.scope)
        if text is not None:
            return text
        try:
            value: object = ast.literal_eval(node)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            return _UNREADABLE
        return tuple(cast("list[object]", value)) if isinstance(value, list) else value

    def source_name_of(self, statement: ast.stmt, call: ast.Call) -> str | None:
        """
        Read a field declaration's ``source=`` override.

        Args:
            statement: The declaring statement, for a warning.
            call: The field constructor call.

        Returns:
            The override, or None when the call carries none. A ``source=`` that is not a
            string or a string constant is warned about and read as absent.
        """
        for keyword in call.keywords:
            if keyword.arg != "source":
                continue
            value = self.static.string_constant(keyword.value, self.scope)
            if value is None:
                self.warn(statement, "has a source= that is not a string; read without it")
            return value
        return None

    def read_other(self, statement: ast.stmt) -> None:
        """
        Warn about a statement that is not a plain assignment but constructs a field.

        Args:
            statement: A statement from the class body: a tuple assignment, a chained
                assignment, an ``if`` block, an annotation with no value, and so on.
        """
        if isinstance(statement, ast.AnnAssign) and statement.value is None:
            callee = (
                statement.annotation.value
                if isinstance(statement.annotation, ast.Subscript)
                else statement.annotation
            )
            if self.field_class(callee) is not None:
                self.warn(statement, "is an annotation with no value, which declares no field")
            return
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # A method's locals and a nested class's attributes declare no field.
            return
        if self.contains_field_call(statement):
            names = [
                ast.unparse(target)
                for node in walk_statement(statement)
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and self.contains_field_call(node)
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            ]
            declares = f"declares {', '.join(names)} " if names else ""
            self.warn(
                statement,
                f"{declares}in a statement the reader does not read; declare each field as "
                "`name = Metric[T]()` directly in the class body",
            )

    def read(self) -> dict[str, CommittedField]:
        """
        Read every field the class body declares.

        Returns:
            Field name -> declaration, in source order.
        """
        fields: dict[str, CommittedField] = {}
        previous: CommittedField | None = None
        for statement in self.source.node.body:
            docstring = _string_statement(statement)
            if previous is not None and docstring is not None:
                if "description" not in previous.metadata:
                    previous = dataclasses.replace(
                        previous, metadata={**previous.metadata, "description": docstring}
                    )
                    fields[previous.name] = previous
                previous = None
                continue
            field = self.read_assignment(statement)
            previous = field
            if field is not None:
                fields[field.name] = field
                self.removed.discard(field.name)
                continue
            for name in self.non_field_names(statement):
                fields.pop(name, None)
                self.removed.add(name)
        return fields

    def non_field_names(self, statement: ast.stmt) -> list[str]:
        """
        Name what a statement positively binds to something other than a field.

        ``models.py`` removes an inherited field whose name a subclass binds to a non-field
        (``country = None``), so such a name must not be read as inherited. Only plain
        names count, bound to a value that is plainly not a field: a constant, a name, a
        known non-field call, a ``def`` or a nested ``class``. Anything the reader could
        not tell apart from a field leaves the name alone.

        Args:
            statement: A statement from the class body.

        Returns:
            The names it binds to a non-field.
        """
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return [statement.name]
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets, value = [statement.target], statement.value
        else:
            return []
        if self.contains_field_call(value):
            return []
        if isinstance(value, ast.Call) and not self.is_known_non_field(_callee(value)[0]):
            return []
        return [target.id for target in targets if isinstance(target, ast.Name)]


METADATA_ARGUMENTS = ("label", "description", "synonyms", "semantic_format")
"""The field constructor arguments that carry metadata, in the order codegen writes them."""

_UNREADABLE = object()
"""What :meth:`_ClassReader.literal` returns for an argument it cannot evaluate."""


def _string_statement(statement: ast.stmt) -> str | None:
    """
    Return the string a bare string statement holds, as an attribute docstring does.

    Args:
        statement: A statement from a class body.

    Returns:
        The string, cleaned as :func:`inspect.cleandoc` cleans a docstring, or None when the
        statement is anything else.
    """
    if (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    ):
        return inspect.cleandoc(statement.value.value)
    return None


def _read_class(
    static: StaticModule, node: ast.ClassDef, warnings: list[str]
) -> CommittedModel | None:
    """
    Read one top-level class: its ``view=`` and its fields, inherited ones included.

    Args:
        static: The loaded file.
        node: A top-level class definition.
        warnings: Receives a line for everything that was not read.

    Returns:
        The model, or None when the class declares no ``view=`` (not a model) or its
        ``view=`` cannot be read (warned about).
    """
    view_keyword = next((k for k in node.keywords if k.arg == "view"), None)
    if view_keyword is None:
        return None
    source = ClassSource(node=node, scope=static.class_scope(node), label=node.name)
    view_name = static.string_constant(view_keyword.value, source.scope)
    if view_name is None:
        warnings.append(
            f"{node.name} (line {node.lineno}): view={ast.unparse(view_keyword.value)} is not "
            "a string or a string constant the reader can find; the class was not checked"
        )
        return None

    fields: dict[str, CommittedField] = {}
    # Bases first, so a subclass's declaration replaces its base's, as Python's would.
    for cls in reversed(static.inheritance_chain(source, warnings)):
        reader = _ClassReader(static, cls, warnings)
        declared = reader.read()
        # A name this class binds to a non-field removes the field it inherited.
        for name in reader.removed:
            fields.pop(name, None)
        fields.update(declared)
    description_keyword = next((k for k in node.keywords if k.arg == "description"), None)
    description = None
    if description_keyword is not None:
        description = static.string_constant(description_keyword.value, source.scope)
        if description is None:
            warnings.append(
                f"{node.name} (line {node.lineno}): description="
                f"{ast.unparse(description_keyword.value)} is not a string or a string "
                "constant the reader can find; it was not compared"
            )
    return CommittedModel(
        class_name=node.name, view_name=view_name, fields=fields, description=description
    )


def _nested_view_classes(tree: ast.Module) -> list[ast.ClassDef]:
    """
    Find classes with a ``view=`` keyword that are not at the top level of the module.

    Args:
        tree: The module.

    Returns:
        Each such class: one defined inside an ``if`` block, a function or another class.
    """
    top_level = {id(node) for node in tree.body}
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and id(node) not in top_level
        and any(k.arg == "view" for k in node.keywords)
    ]


def read_committed_model_file(path: pathlib.Path) -> CommittedModelFile:
    """
    Read the model classes in a committed model file, and everything that was not read.

    The file is parsed, never imported: no code in it runs. Modules it imports from are
    parsed too, when they are found under its package root or the working directory, so a
    base class or a ``view=`` constant declared there is followed.

    Args:
        path: Path to the committed model file.

    Returns:
        The models, and a warning for each field-like statement, class or base class that
        was not read.

    Raises:
        ValueError: If the file cannot be read, or does not parse. The message names the
            path (and, for a syntax error, the line) and nothing else, so the CLI can print
            it instead of a traceback.

    Example:
        .. code-block:: python

            import pathlib

            from semolina.codegen.model_reader import read_committed_model_file

            read = read_committed_model_file(pathlib.Path("models.py"))
            read.models[0].fields["revenue"].annotation
            # 'decimal.Decimal | None'
            read.warnings
            # []
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"Cannot read model file {path}: {e.strerror or e}") from e

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        raise ValueError(f"Cannot parse model file {path} at line {e.lineno}: {e.msg}") from e

    static = StaticModule.load(path, source, tree)
    warnings: list[str] = []
    models: list[CommittedModel] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            model = _read_class(static, node, warnings)
            if model is not None:
                models.append(model)
    for node in _nested_view_classes(tree):
        warnings.append(
            f"{node.name} (line {node.lineno}): a class with view= that is not at the top "
            "level of the module was not checked"
        )
    return CommittedModelFile(models=models, warnings=list(dict.fromkeys(warnings)))


def read_committed_model(path: pathlib.Path) -> list[CommittedModel]:
    """
    Read the model classes declared in a committed generated model file.

    The models half of :func:`read_committed_model_file`, for a caller that reports its
    warnings some other way or has none to report. The CLI uses
    :func:`read_committed_model_file`, so nothing it reads is dropped without a word.

    Args:
        path: Path to the committed model file.

    Returns:
        One entry per ``SemanticView`` subclass, in source order.

    Raises:
        ValueError: If the file cannot be read, or does not parse.

    Example:
        .. code-block:: python

            import pathlib

            from semolina.codegen.model_reader import read_committed_model

            models = read_committed_model(pathlib.Path("models.py"))
            models[0].fields["revenue"].annotation
            # 'decimal.Decimal | None'
    """
    return read_committed_model_file(path).models
