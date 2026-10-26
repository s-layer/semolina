"""
Tests for reading a committed generated model's annotations without importing it.

The primary fixture is copied verbatim out of
``tests/unit/codegen/__snapshots__/test_codegen_e2e.ambr`` so the parser is proven against
the exact source ``semolina codegen`` emits, not against a hand-written approximation of it.

The "does not execute" test is the one that matters most: ``--check`` is pointed at a file
path a user (or a CI job, on a repo checkout) supplies, and importing it would run whatever
that file's module level contains.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from semolina.codegen.model_reader import (
    normalize_view_name,
    read_committed_model,
    read_committed_model_file,
)
from semolina.engines.sql import DatabricksDialect, DuckDBDialect, SnowflakeDialect

if TYPE_CHECKING:
    from pathlib import Path

# Copied from tests/unit/codegen/__snapshots__/test_codegen_e2e.ambr, snapshot
# `test_codegen_snowflake_field_types`. One metric with a stdlib-qualified annotation and a
# `| None` suffix, one dimension, one fact, and a raw-type `#` comment line above the metric.
SNOWFLAKE_SNAPSHOT_SOURCE = """\
import datetime
import decimal

from semolina import Dimension, Fact, Metric, SemanticView


class SalesView(SemanticView, view="sales_view"):
    # {"type": "FIXED", "scale": 0}
    revenue = Metric[decimal.Decimal | None]()
    country = Dimension[str]()
    date_key = Fact[datetime.date]()
"""


def _write(tmp_path: Path, source: str, name: str = "models.py") -> Path:
    """
    Write a fixture module and return its path.

    Args:
        tmp_path: pytest's per-test temporary directory.
        source: Python source to write.
        name: File name to write it under.

    Returns:
        Path to the written file.
    """
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


class TestReadsTheRenderersOwnOutput:
    """The parser is proven against the source the renderer actually emits."""

    def test_one_class_yields_one_model(self, tmp_path: Path) -> None:
        models = read_committed_model(_write(tmp_path, SNOWFLAKE_SNAPSHOT_SOURCE))

        assert len(models) == 1
        assert models[0].class_name == "SalesView"
        assert models[0].view_name == "sales_view"

    def test_fields_are_in_source_order(self, tmp_path: Path) -> None:
        models = read_committed_model(_write(tmp_path, SNOWFLAKE_SNAPSHOT_SOURCE))

        assert list(models[0].fields) == ["revenue", "country", "date_key"]

    def test_metric_annotation_is_recovered_exactly_as_written(self, tmp_path: Path) -> None:
        models = read_committed_model(_write(tmp_path, SNOWFLAKE_SNAPSHOT_SOURCE))

        revenue = models[0].fields["revenue"]
        assert revenue.name == "revenue"
        assert revenue.field_class == "Metric"
        assert revenue.annotation == "decimal.Decimal | None"
        assert revenue.source_name is None

    def test_dimension_and_fact_classes_are_recovered(self, tmp_path: Path) -> None:
        fields = read_committed_model(_write(tmp_path, SNOWFLAKE_SNAPSHOT_SOURCE))[0].fields

        assert fields["country"].field_class == "Dimension"
        assert fields["country"].annotation == "str"
        assert fields["date_key"].field_class == "Fact"
        assert fields["date_key"].annotation == "datetime.date"


def test_source_keyword_is_recovered(tmp_path: Path) -> None:
    source = """\
from semolina import Dimension, SemanticView


class V(SemanticView, view="v"):
    country = Dimension[str](source="COUNTRY")
"""
    field = read_committed_model(_write(tmp_path, source))[0].fields["country"]

    assert field.field_class == "Dimension"
    assert field.annotation == "str"
    assert field.source_name == "COUNTRY"


def test_alias_annotation_is_preserved_textually(tmp_path: Path) -> None:
    """A ``JsonValue`` annotation stays the name that was written, unresolved."""
    source = """\
from semolina import Dimension, JsonValue, SemanticView


class V(SemanticView, view="v"):
    payload = Dimension[JsonValue]()
"""
    field = read_committed_model(_write(tmp_path, source))[0].fields["payload"]

    assert field.annotation == "JsonValue"


def test_two_model_classes_yield_two_entries(tmp_path: Path) -> None:
    source = """\
from semolina import Dimension, Metric, SemanticView


class A(SemanticView, view="a_view"):
    revenue = Metric[int | None]()


class B(SemanticView, view="b_view"):
    country = Dimension[str]()
"""
    models = read_committed_model(_write(tmp_path, source))

    assert [m.view_name for m in models] == ["a_view", "b_view"]
    assert [m.class_name for m in models] == ["A", "B"]


def test_class_without_a_view_keyword_is_skipped(tmp_path: Path) -> None:
    """A plain class in the same file is not a generated model and must not crash the parse."""
    source = """\
from semolina import Dimension, SemanticView


class Helper:
    country = "not a field"


class V(SemanticView, view="v"):
    country = Dimension[str]()
"""
    models = read_committed_model(_write(tmp_path, source))

    assert [m.class_name for m in models] == ["V"]


def test_untyped_shorthand_reads_as_any(tmp_path: Path) -> None:
    """
    ``Metric()`` is the documented untyped shorthand and has no annotation to compare.

    It is represented as ``Any`` — the untyped fallback the renderer itself emits for an
    unmapped type — rather than dropped, so the field still appears in a drift report.
    """
    source = """\
from semolina import Metric, SemanticView


class V(SemanticView, view="v"):
    revenue = Metric()
"""
    field = read_committed_model(_write(tmp_path, source))[0].fields["revenue"]

    assert field.field_class == "Metric"
    assert field.annotation == "Any"


def test_non_field_assignments_are_skipped(tmp_path: Path) -> None:
    source = """\
from typing import ClassVar

from semolina import Dimension, SemanticView


class V(SemanticView, view="v"):
    label: ClassVar[str] = "v"
    other = dict[str, int]()
    country = Dimension[str]()
"""
    models = read_committed_model(_write(tmp_path, source))

    assert list(models[0].fields) == ["country"]


class TestErrorsAreCleanValueErrors:
    """A bad path or a bad file gives the CLI something to turn into an exit code."""

    def test_syntax_error_is_wrapped_naming_the_path(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "class V(SemanticView, view=:\n")

        with pytest.raises(ValueError, match=r"models\.py") as exc_info:
            read_committed_model(path)

        assert not isinstance(exc_info.value, SyntaxError)
        assert "line" in str(exc_info.value)

    def test_missing_file_is_wrapped_naming_the_path(self, tmp_path: Path) -> None:
        path = tmp_path / "nope.py"

        with pytest.raises(ValueError, match=r"nope\.py"):
            read_committed_model(path)


def test_module_level_code_is_not_executed(tmp_path: Path) -> None:
    """
    Parsing a model must run none of its module-level code.

    The threat made runnable: ``--check`` reads a path the user supplies, and in
    CI that path comes from a repo checkout. The marker file is the evidence — if the module
    were imported or exec'd, it would exist.
    """
    marker = tmp_path / "MARKER"
    source = f"""\
import pathlib

from semolina import Dimension, SemanticView

pathlib.Path({str(marker)!r}).write_text("executed", encoding="utf-8")


class V(SemanticView, view="v"):
    country = Dimension[str]()
"""
    models = read_committed_model(_write(tmp_path, source))

    assert not marker.exists()
    assert [m.view_name for m in models] == ["v"]


# ---------------------------------------------------------------------------
# D9: names, bases and constants resolve; nothing field-like is skipped silently
# ---------------------------------------------------------------------------


def _package(tmp_path: Path, files: dict[str, str]) -> None:
    """
    Write a package tree under ``tmp_path``.

    Args:
        tmp_path: The project root.
        files: Relative path -> source.
    """
    for relative, source in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


class TestAnnotationsAreAlsoRecordedResolved:
    """The written annotation is kept for the report; its resolved form is what is compared."""

    def test_an_imported_name_resolves(self, tmp_path: Path) -> None:
        source = """\
from decimal import Decimal
from typing import Optional

from semolina import Metric, SemanticView


class V(SemanticView, view="v"):
    revenue = Metric[Optional[Decimal]]()
"""
        field = read_committed_model(_write(tmp_path, source))[0].fields["revenue"]

        assert field.annotation == "Optional[Decimal]"
        assert field.resolved_annotation == "decimal.Decimal | None"

    def test_a_name_from_the_users_own_module_is_left_unresolved(self, tmp_path: Path) -> None:
        source = """\
from semolina import Dimension, SemanticView

from app.types import Money


class V(SemanticView, view="v"):
    price = Dimension[Money]()
"""
        field = read_committed_model(_write(tmp_path, source))[0].fields["price"]

        assert field.annotation == "Money"
        assert field.resolved_annotation is None

    def test_a_typing_extensions_name_resolves_like_its_typing_twin(self, tmp_path: Path) -> None:
        """``typing_extensions.Optional`` is compared as ``typing.Optional``, not as text."""
        source = """\
import typing_extensions

from semolina import Metric, SemanticView


class V(SemanticView, view="v"):
    revenue = Metric[typing_extensions.Optional[int]]()
"""
        field = read_committed_model(_write(tmp_path, source))[0].fields["revenue"]

        assert field.annotation == "typing_extensions.Optional[int]"
        assert field.resolved_annotation == "int | None"

    def test_a_forward_reference_that_does_not_parse_is_left_unresolved(
        self, tmp_path: Path
    ) -> None:
        """
        A quoted annotation that is not Python falls back to the text, and nothing crashes.

        Its neighbour, a forward reference that does parse, still resolves.
        """
        source = """\
from typing import Optional

from semolina import Metric, SemanticView


class V(SemanticView, view="v"):
    revenue = Metric[Optional["int |"]]()
    cost = Metric[Optional["int"]]()
"""
        read = read_committed_model_file(_write(tmp_path, source))
        fields = read.models[0].fields

        assert fields["revenue"].annotation == "Optional['int |']"
        assert fields["revenue"].resolved_annotation is None
        assert fields["cost"].resolved_annotation == "int | None"
        assert read.warnings == []


class TestInheritedFields:
    """Fields declared on a base class read as the subclass's own (M6)."""

    def test_a_subclass_declaration_replaces_its_bases(self, tmp_path: Path) -> None:
        source = """\
from semolina import Dimension, Metric, SemanticView


class Base(SemanticView, abstract=True):
    region = Dimension[str]()
    revenue = Metric[int | None]()


class V(Base, view="v"):
    revenue = Metric[float | None]()
"""
        read = read_committed_model_file(_write(tmp_path, source))

        assert [m.class_name for m in read.models] == ["V"]
        fields = read.models[0].fields
        assert list(fields) == ["region", "revenue"]
        assert fields["revenue"].annotation == "float | None"
        assert read.warnings == []

    @pytest.mark.parametrize(
        "import_line",
        ["from app.generated import Sales", "from .generated import Sales"],
        ids=["absolute", "relative"],
    )
    def test_a_subclass_named_like_its_imported_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, import_line: str
    ) -> None:
        """``class Sales(Sales)`` extends the imported ``Sales``, not itself."""
        _package(
            tmp_path,
            {
                "app/__init__.py": "",
                "app/generated.py": (
                    "from semolina import Dimension, SemanticView\n\n\n"
                    "class Sales(SemanticView, abstract=True):\n"
                    "    region = Dimension[str]()\n"
                ),
                "app/models.py": (
                    f"from semolina import Metric\n\n{import_line}\n\n\n"
                    'class Sales(Sales, view="sales"):\n'
                    "    extra = Metric[int]()\n"
                ),
            },
        )
        monkeypatch.chdir(tmp_path)

        read = read_committed_model_file(tmp_path / "app" / "models.py")

        assert list(read.models[0].fields) == ["region", "extra"]
        assert read.warnings == []

    def test_a_warning_from_a_base_in_another_module_names_that_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The line number is the base file's, so the warning names the base by its path."""
        _package(
            tmp_path,
            {
                "app/__init__.py": "",
                "app/base.py": (
                    "from semolina import Dimension, SemanticView\n\nFLAG = True\n\n\n"
                    "class Base(SemanticView, abstract=True):\n"
                    "    if FLAG:\n"
                    "        region = Dimension[str]()\n"
                ),
                "app/models.py": (
                    "from semolina import Metric\n\nfrom app.base import Base\n\n\n"
                    'class V(Base, view="v"):\n'
                    "    revenue = Metric[int]()\n"
                ),
            },
        )
        monkeypatch.chdir(tmp_path)

        read = read_committed_model_file(tmp_path / "app" / "models.py")

        assert len(read.warnings) == 1, read.warnings
        assert read.warnings[0].startswith("app.base.Base (line 7)"), read.warnings

    def test_a_base_in_a_module_that_star_imports_semolina(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The star import that brings ``SemanticView`` in is the base's file's, not this one's."""
        _package(
            tmp_path,
            {
                "app/__init__.py": "",
                "app/base.py": (
                    "from semolina import *\n\n\n"
                    "class Base(SemanticView, abstract=True):\n"
                    "    region = Dimension[str]()\n"
                ),
                "app/models.py": (
                    "from semolina import Metric\n\nfrom app.base import Base\n\n\n"
                    'class V(Base, view="v"):\n'
                    "    revenue = Metric[int | None]()\n"
                ),
            },
        )
        monkeypatch.chdir(tmp_path)

        read = read_committed_model_file(tmp_path / "app" / "models.py")

        assert list(read.models[0].fields) == ["region", "revenue"]
        assert read.warnings == []

    def test_a_field_class_reexported_through_the_users_own_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``from app.compat import Metric``, where ``compat`` imports it from semolina."""
        _package(
            tmp_path,
            {
                "app/__init__.py": "",
                "app/compat.py": "from semolina import Metric, SemanticView\n",
                "app/models.py": (
                    "from app.compat import Metric, SemanticView\n\n\n"
                    'class V(SemanticView, view="v"):\n'
                    "    revenue = Metric[int | None]()\n"
                ),
            },
        )
        monkeypatch.chdir(tmp_path)

        read = read_committed_model_file(tmp_path / "app" / "models.py")

        assert list(read.models[0].fields) == ["revenue"]
        assert read.warnings == []

    def test_a_dotted_annotation_follows_a_reexport(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``compat.Decimal`` resolves like ``from app.compat import Decimal`` does."""
        _package(
            tmp_path,
            {
                "app/__init__.py": "",
                "app/compat.py": "from decimal import Decimal\n",
                "app/models.py": (
                    "from semolina import Metric, SemanticView\n\nfrom app import compat\n\n\n"
                    'class V(SemanticView, view="v"):\n'
                    "    revenue = Metric[compat.Decimal | None]()\n"
                ),
            },
        )
        monkeypatch.chdir(tmp_path)

        read = read_committed_model_file(tmp_path / "app" / "models.py")

        assert read.models[0].fields["revenue"].resolved_annotation == "decimal.Decimal | None"

    def test_a_base_reexported_through_a_package_init(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _package(
            tmp_path,
            {
                "app/__init__.py": "from app.base import Base\n",
                "app/base.py": (
                    "from semolina import Dimension, SemanticView\n\n\n"
                    "class Base(SemanticView, abstract=True):\n"
                    "    region = Dimension[str]()\n"
                ),
                "app/models.py": (
                    "from semolina import Metric\n\nfrom app import Base\n\n\n"
                    'class V(Base, view="v"):\n'
                    "    revenue = Metric[int | None]()\n"
                ),
            },
        )
        monkeypatch.chdir(tmp_path)

        read = read_committed_model_file(tmp_path / "app" / "models.py")

        assert list(read.models[0].fields) == ["region", "revenue"]
        assert read.warnings == []

    def test_a_relative_import_in_a_src_layout(self, tmp_path: Path) -> None:
        """The package root is found from ``__init__.py`` files, not from the working directory."""
        _package(
            tmp_path,
            {
                "src/app/__init__.py": "",
                "src/app/base.py": (
                    "from semolina import Dimension, SemanticView\n\n\n"
                    "class Base(SemanticView, abstract=True):\n"
                    "    region = Dimension[str]()\n"
                ),
                "src/app/models.py": (
                    'from .base import Base\n\n\nclass V(Base, view="v"):\n    pass\n'
                ),
            },
        )

        read = read_committed_model_file(tmp_path / "src" / "app" / "models.py")

        assert list(read.models[0].fields) == ["region"]

    def test_imported_modules_are_parsed_and_never_executed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Following a base into another module runs none of that module either."""
        marker = tmp_path / "MARKER"
        _package(
            tmp_path,
            {
                "app/__init__.py": f"import pathlib\npathlib.Path({str(marker)!r}).touch()\n",
                "app/base.py": (
                    "import pathlib\n"
                    "from semolina import Dimension, SemanticView\n\n"
                    f"pathlib.Path({str(marker)!r}).touch()\n\n\n"
                    "class Base(SemanticView, abstract=True):\n"
                    "    region = Dimension[str]()\n"
                ),
                "app/models.py": (
                    'from app.base import Base\n\n\nclass V(Base, view="v"):\n    pass\n'
                ),
            },
        )
        monkeypatch.chdir(tmp_path)

        read = read_committed_model_file(tmp_path / "app" / "models.py")

        assert not marker.exists()
        assert list(read.models[0].fields) == ["region"]

    def test_a_base_that_cannot_be_found_is_warned_about(self, tmp_path: Path) -> None:
        source = """\
from semolina import Metric

from somewhere_else import Base


class V(Base, view="v"):
    revenue = Metric[int | None]()
"""
        read = read_committed_model_file(_write(tmp_path, source))

        assert list(read.models[0].fields) == ["revenue"]
        assert len(read.warnings) == 1
        assert "V" in read.warnings[0]
        assert "Base" in read.warnings[0]
        assert "not followed" in read.warnings[0]

    def test_an_inheritance_cycle_is_warned_about_and_the_rest_are_read(
        self, tmp_path: Path
    ) -> None:
        """
        ``class A(B)`` / ``class B(A)`` reads as text, though Python would refuse to run it.

        The cycle is reported rather than recursed into, ``A`` keeps its own fields, and
        the unrelated class after it is still read.
        """
        source = """\
from semolina import Dimension, Metric, SemanticView


class A(B, view="a"):
    revenue = Metric[int | None]()


class B(A):
    region = Dimension[str]()


class C(SemanticView, view="c"):
    cost = Metric[int | None]()
"""
        read = read_committed_model_file(_write(tmp_path, source))

        assert [(m.class_name, list(m.fields)) for m in read.models] == [
            ("A", ["revenue"]),
            ("C", ["cost"]),
        ]
        assert len(read.warnings) == 1
        assert read.warnings[0].startswith("A: base classes were not followed")
        assert "inheritance cycle" in read.warnings[0]

    def test_bases_with_no_consistent_order_are_warned_about(self, tmp_path: Path) -> None:
        """
        ``class V(X, Y)`` with ``Y(X)`` has no MRO; the class is read alone, with a warning.

        Python raises ``TypeError`` at class creation. The reader cannot pick an order for
        the inherited fields either, so it reads none of them and says so.
        """
        source = """\
from semolina import Dimension, Metric, SemanticView


class X(SemanticView):
    region = Dimension[str]()


class Y(X):
    cost = Metric[int]()


class V(X, Y, view="v"):
    revenue = Metric[int | None]()


class C(SemanticView, view="c"):
    cost = Metric[int | None]()
"""
        read = read_committed_model_file(_write(tmp_path, source))

        assert [(m.class_name, list(m.fields)) for m in read.models] == [
            ("V", ["revenue"]),
            ("C", ["cost"]),
        ]
        assert len(read.warnings) == 1
        assert read.warnings[0].startswith("V: base classes were not followed")
        assert "no consistent method resolution order" in read.warnings[0]

    def test_a_base_behind_a_cyclic_reexport_is_warned_about(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two modules re-exporting a name from each other resolve to nothing, not a hang."""
        _package(
            tmp_path,
            {
                "app/__init__.py": "",
                "app/a.py": "from app.b import Base\n",
                "app/b.py": "from app.a import Base\n",
                "app/models.py": (
                    "from semolina import Metric\n\nfrom app.a import Base\n\n\n"
                    'class V(Base, view="v"):\n'
                    "    revenue = Metric[int | None]()\n"
                ),
            },
        )
        monkeypatch.chdir(tmp_path)

        read = read_committed_model_file(tmp_path / "app" / "models.py")

        assert list(read.models[0].fields) == ["revenue"]
        assert len(read.warnings) == 1
        assert "Base" in read.warnings[0]
        assert "not followed" in read.warnings[0]

    def test_a_base_in_a_package_that_does_not_parse_is_warned_about(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A package on the search path that fails to load is a warning, not a crash."""
        _package(
            tmp_path,
            {
                "app/__init__.py": "",
                "broken/__init__.py": "def (:\n",
                "app/models.py": (
                    "from semolina import Metric\n\nfrom broken import Base\n\n\n"
                    'class V(Base, view="v"):\n'
                    "    revenue = Metric[int | None]()\n"
                ),
            },
        )
        monkeypatch.chdir(tmp_path)

        read = read_committed_model_file(tmp_path / "app" / "models.py")

        assert list(read.models[0].fields) == ["revenue"]
        assert len(read.warnings) == 1
        assert "Base" in read.warnings[0]
        assert "not followed" in read.warnings[0]


class TestViewFromAConstant:
    """``view=`` may name a string constant, however it is reached (M3)."""

    def test_a_constant_reached_through_a_module_attribute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _package(
            tmp_path,
            {
                "app/__init__.py": "",
                "app/names.py": 'SALES_VIEW = "sales_view"\n',
                "app/models.py": (
                    "from semolina import Dimension, SemanticView\n\nfrom app import names\n\n\n"
                    "class V(SemanticView, view=names.SALES_VIEW):\n"
                    "    region = Dimension[str]()\n"
                ),
            },
        )
        monkeypatch.chdir(tmp_path)

        models = read_committed_model(tmp_path / "app" / "models.py")

        assert [m.view_name for m in models] == ["sales_view"]

    def test_a_constant_reexported_from_another_package_is_loaded_on_demand(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``app.names`` re-exports ``lib.names.SALES_VIEW``; ``lib`` is parsed when needed.

        The re-export is an alias griffe cannot resolve until ``lib`` is loaded, so the
        lookup loads that package and tries again.
        """
        _package(
            tmp_path,
            {
                "app/__init__.py": "",
                "app/names.py": "from lib.names import SALES_VIEW\n",
                "lib/__init__.py": "",
                "lib/names.py": 'SALES_VIEW = "sales_view"\n',
                "app/models.py": (
                    "from semolina import Dimension, SemanticView\n\n"
                    "from app.names import SALES_VIEW\n\n\n"
                    "class V(SemanticView, view=SALES_VIEW):\n"
                    "    region = Dimension[str]()\n"
                ),
            },
        )
        monkeypatch.chdir(tmp_path)

        read = read_committed_model_file(tmp_path / "app" / "models.py")

        assert [m.view_name for m in read.models] == ["sales_view"]
        assert read.warnings == []

    def test_a_constant_bound_to_another_constant_is_followed(self, tmp_path: Path) -> None:
        """``NAME = OTHER`` reads the string ``OTHER`` holds."""
        source = """\
from semolina import Dimension, SemanticView

OTHER = "sales_view"
NAME = OTHER


class V(SemanticView, view=NAME):
    region = Dimension[str]()
"""
        read = read_committed_model_file(_write(tmp_path, source))

        assert [m.view_name for m in read.models] == ["sales_view"]
        assert read.warnings == []

    @pytest.mark.parametrize(
        "binding",
        [
            pytest.param("NAME = 42", id="int"),
            pytest.param('NAME = b"sales_view"', id="bytes"),
            pytest.param("NAME = make_name()", id="call"),
            pytest.param('NAME = "sales" + "_view"', id="expression"),
        ],
    )
    def test_a_constant_that_holds_no_string_is_warned_about(
        self, tmp_path: Path, binding: str
    ) -> None:
        """A constant the reader can find but not read as a string skips the class, aloud."""
        source = f"""\
from semolina import Dimension, SemanticView

{binding}


class V(SemanticView, view=NAME):
    region = Dimension[str]()


class W(SemanticView, view="w"):
    region = Dimension[str]()
"""
        read = read_committed_model_file(_write(tmp_path, source))

        assert [m.class_name for m in read.models] == ["W"]
        assert len(read.warnings) == 1
        assert "view=NAME" in read.warnings[0]
        assert "not checked" in read.warnings[0]

    def test_a_constant_behind_a_cyclic_reexport_is_warned_about(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cycle of re-exports names no string, so the class is reported as not checked."""
        _package(
            tmp_path,
            {
                "app/__init__.py": "",
                "app/a.py": "from app.b import NAME\n",
                "app/b.py": "from app.a import NAME\n",
                "app/models.py": (
                    "from semolina import Dimension, SemanticView\n\n"
                    "from app.a import NAME\n\n\n"
                    "class V(SemanticView, view=NAME):\n"
                    "    region = Dimension[str]()\n\n\n"
                    'class W(SemanticView, view="w"):\n'
                    "    region = Dimension[str]()\n"
                ),
            },
        )
        monkeypatch.chdir(tmp_path)

        read = read_committed_model_file(tmp_path / "app" / "models.py")

        assert [m.class_name for m in read.models] == ["W"]
        assert len(read.warnings) == 1
        assert "view=NAME" in read.warnings[0]
        assert "not checked" in read.warnings[0]

    def test_a_view_that_is_not_a_string_is_warned_about(self, tmp_path: Path) -> None:
        source = """\
from semolina import Dimension, SemanticView


class V(SemanticView, view=make_name()):
    region = Dimension[str]()
"""
        read = read_committed_model_file(_write(tmp_path, source))

        assert read.models == []
        assert len(read.warnings) == 1
        assert "V" in read.warnings[0]
        assert "make_name()" in read.warnings[0]


class TestNothingFieldLikeIsSkippedSilently:
    """
    GEN-12: a statement that might declare a field is read or warned about, never dropped.

    Each case is one the reader cannot read. Before D9 every one of them vanished, and the
    field then read as absent with nothing saying why.
    """

    @pytest.mark.parametrize(
        ("statement", "fragment"),
        [
            ("    revenue = Helper[int]()\n", "Helper[int]()"),
            ("    revenue, cost = Metric[int](), Metric[int]()\n", "revenue, cost"),
            ("    revenue = cost = Metric[int]()\n", "revenue = cost"),
            ("    if True:\n        revenue = Metric[int]()\n", "if True"),
            ("    revenue: Metric[int]\n", "revenue: Metric[int]"),
            ("    revenue = Metric[int](source=column_name())\n", "source="),
        ],
        ids=[
            "unknown-constructor",
            "tuple-assignment",
            "chained-assignment",
            "inside-an-if",
            "annotation-only",
            "source-not-a-string",
        ],
    )
    def test_an_unread_statement_is_warned_about(
        self, tmp_path: Path, statement: str, fragment: str
    ) -> None:
        source = (
            "from semolina import Dimension, Metric, SemanticView\n\n\n"
            'class V(SemanticView, view="v"):\n'
            "    region = Dimension[str]()\n" + statement
        )

        read = read_committed_model_file(_write(tmp_path, source))

        assert len(read.warnings) == 1, read.warnings
        assert read.warnings[0].startswith("V (line ")
        assert fragment in read.warnings[0]

    def test_a_nested_class_with_a_view_is_warned_about(self, tmp_path: Path) -> None:
        source = """\
from semolina import Dimension, SemanticView

if True:

    class V(SemanticView, view="v"):
        region = Dimension[str]()
"""
        read = read_committed_model_file(_write(tmp_path, source))

        assert read.models == []
        assert len(read.warnings) == 1
        assert "V" in read.warnings[0]

    def test_statements_known_not_to_be_fields_are_not_warned_about(self, tmp_path: Path) -> None:
        """A builtin, a ``ClassVar`` and a docstring cannot declare a field, so stay quiet."""
        source = """\
from typing import ClassVar

from semolina import Dimension, SemanticView


class V(SemanticView, view="v"):
    \"\"\"A view.\"\"\"

    label: ClassVar[str] = "v"
    other = dict[str, int]()
    country = Dimension[str]()
    \"\"\"The country.\"\"\"
"""
        read = read_committed_model_file(_write(tmp_path, source))

        assert list(read.models[0].fields) == ["country"]
        assert read.warnings == []

    def test_the_renderers_own_output_reads_without_a_warning(self, tmp_path: Path) -> None:
        read = read_committed_model_file(_write(tmp_path, SNOWFLAKE_SNAPSHOT_SOURCE))

        assert read.warnings == []


class TestViewNamesFoldByTheEnginesRules:
    """M4: two spellings of a view name match when the engine would read them as one view."""

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("sales_view", "SALES_VIEW"),
            ("sales_view", '"Sales_View"'),
            ("sales_view", "main.sales_view"),
            ("sales_view", 'MAIN."sales_view"'),
            ("analytics.sales_view", "Analytics.SALES_VIEW"),
            ('"say ""hi"""', '"SAY ""HI"""'),
        ],
    )
    def test_duckdb_ignores_case_quotes_and_the_main_schema(self, left: str, right: str) -> None:
        dialect = DuckDBDialect()
        assert normalize_view_name(left, dialect) == normalize_view_name(right, dialect)

    def test_duckdb_keeps_other_schemas_apart(self) -> None:
        dialect = DuckDBDialect()
        assert normalize_view_name("analytics.sales", dialect) != normalize_view_name(
            "sales", dialect
        )

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("sales", "SALES"),
            ("sales", '"SALES"'),
            ("db.analytics.sales", 'DB.ANALYTICS."SALES"'),
        ],
    )
    def test_snowflake_folds_unquoted_names_to_upper_case(self, left: str, right: str) -> None:
        dialect = SnowflakeDialect()
        assert normalize_view_name(left, dialect) == normalize_view_name(right, dialect)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ('"Sales"', "SALES"),
            ('"sales"', "sales"),
            ("main.sales", "sales"),
        ],
    )
    def test_snowflake_keeps_quoted_case_and_every_schema(self, left: str, right: str) -> None:
        """``"Sales"`` and ``SALES`` are two views on Snowflake, and ``main`` is no default."""
        dialect = SnowflakeDialect()
        assert normalize_view_name(left, dialect) != normalize_view_name(right, dialect)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("sales", "SALES"),
            ("sales", "`sales`"),
            ("sales", "`Sales`"),
            ("main.analytics.sales", "`main`.`analytics`.`SALES`"),
        ],
    )
    def test_databricks_strips_backticks_and_ignores_case(self, left: str, right: str) -> None:
        dialect = DatabricksDialect()
        assert normalize_view_name(left, dialect) == normalize_view_name(right, dialect)

    def test_databricks_keeps_a_schema_called_main(self) -> None:
        """``main`` is an ordinary schema or catalog name on Databricks, not a default."""
        dialect = DatabricksDialect()
        assert normalize_view_name("main.sales", dialect) != normalize_view_name("sales", dialect)

    def test_a_dialect_with_case_sensitive_quoting_keeps_quoted_case(self) -> None:
        """Only a dialect that ignores case in quoted names folds them; others keep it."""

        class CaseSensitiveDialect(DatabricksDialect):
            quoted_identifiers_case_sensitive = True

        dialect = CaseSensitiveDialect()
        assert normalize_view_name("`Sales`", dialect) != normalize_view_name("`sales`", dialect)

    @pytest.mark.parametrize("name", ['"my.view"', '"unbalanced'])
    def test_a_name_the_dialect_cannot_quote_matches_only_itself(self, name: str) -> None:
        dialect = DuckDBDialect()
        assert normalize_view_name(name, dialect) == name


def test_field_calls_inside_methods_are_not_warned_about(tmp_path: Path) -> None:
    """A classmethod returning ``Dimension[str]()`` declares no field, so nothing was skipped."""
    source = SNOWFLAKE_SNAPSHOT_SOURCE + (
        "\n    @classmethod\n"
        "    def make(cls):\n"
        "        extra = Dimension[str]()\n"
        "        return extra\n\n"
        "    class Nested:\n"
        "        other = Metric[int]()\n"
    )

    read = read_committed_model_file(_write(tmp_path, source))

    assert list(read.models[0].fields) == ["revenue", "country", "date_key"]
    assert read.warnings == []


@pytest.mark.parametrize("name", ["models.v2.py", "models.py.bak"])
def test_a_file_stem_with_a_dot_reads_without_crashing(tmp_path: Path, name: str) -> None:
    """``models.v2.py`` has no ``models`` package beside it; it is still read on its own."""
    models = read_committed_model(_write(tmp_path, SNOWFLAKE_SNAPSHOT_SOURCE, name=name))

    assert [m.class_name for m in models] == ["SalesView"]


def test_a_star_imported_semantic_view_base_is_skipped_without_a_warning(
    tmp_path: Path,
) -> None:
    """``from semolina import *`` leaves ``SemanticView`` unresolved; it still ends the chain."""
    source = SNOWFLAKE_SNAPSHOT_SOURCE.replace(
        "from semolina import Dimension, Fact, Metric, SemanticView", "from semolina import *"
    )

    read = read_committed_model_file(_write(tmp_path, source))

    assert [m.class_name for m in read.models] == ["SalesView"]
    assert read.warnings == []


class TestClassBodyNamesResolveInTheModule:
    """A class body's right-hand sides see the module, not the class's own attributes."""

    def test_a_field_named_after_its_imported_type_resolves_to_the_type(
        self, tmp_path: Path
    ) -> None:
        """``date = Dimension[date]()``: the ``date`` inside is the import, as in Python."""
        source = """\
from datetime import date

from semolina import Dimension, SemanticView


class Orders(SemanticView, view="orders"):
    date = Dimension[date]()
"""
        field = read_committed_model(_write(tmp_path, source))[0].fields["date"]

        assert field.resolved_annotation == "datetime.date"


class TestRemovedInheritedFields:
    """A subclass binding a field's name to a non-field removes it, as ``models.py`` does."""

    def test_a_name_rebound_to_none_is_not_inherited(self, tmp_path: Path) -> None:
        source = """\
from semolina import Dimension, Metric, SemanticView


class Sales(SemanticView, view="sales"):
    revenue = Metric[int]()
    country = Dimension[str]()


class SalesV2(Sales, view="sales_v2"):
    country = None
"""
        models = {m.class_name: m for m in read_committed_model(_write(tmp_path, source))}

        assert list(models["Sales"].fields) == ["revenue", "country"]
        assert list(models["SalesV2"].fields) == ["revenue"]

    def test_a_name_redeclared_after_removal_is_a_field_again(self, tmp_path: Path) -> None:
        source = """\
from semolina import Dimension, SemanticView


class V(SemanticView, view="v"):
    country = None
    country = Dimension[str]()
"""
        assert list(read_committed_model(_write(tmp_path, source))[0].fields) == ["country"]


class TestMetadataArgumentsAreRead:
    """``label=``, ``description=``, ``synonyms=`` and ``semantic_format=``, as literals."""

    SOURCE = """\
from semolina import Dimension, Metric, SemanticView

LABEL = "Gross revenue"


class V(SemanticView, view="v", description="Sales by country"):
    revenue = Metric[float | None](
        label=LABEL,
        description="Revenue before refunds",
        synonyms=["sales", "turnover"],
        semantic_format={"value": {"kind": "currency", "currency": "USD"}},
    )
    country = Dimension[str]()
"""

    def test_each_argument_is_read_by_value(self, tmp_path: Path) -> None:
        model = read_committed_model(_write(tmp_path, self.SOURCE))[0]

        assert model.fields["revenue"].metadata == {
            "label": "Gross revenue",
            "description": "Revenue before refunds",
            "synonyms": ("sales", "turnover"),
            "semantic_format": {"value": {"kind": "currency", "currency": "USD"}},
        }
        assert model.fields["country"].metadata == {}

    def test_the_class_description_is_read(self, tmp_path: Path) -> None:
        assert read_committed_model(_write(tmp_path, self.SOURCE))[0].description == (
            "Sales by country"
        )

    def test_an_attribute_docstring_is_not_read_as_the_description(self, tmp_path: Path) -> None:
        """A docstring is documentation you wrote; only ``description=`` is compared."""
        source = '''\
from semolina import Dimension, Metric, SemanticView


class V(SemanticView, view="v"):
    revenue = Metric[float | None]()
    """Revenue in cents; see the finance wiki."""
    country = Dimension[str]()
'''
        model = read_committed_model(_write(tmp_path, source))[0]

        assert model.fields["revenue"].metadata == {}

    def test_a_set_of_synonyms_reads_as_a_tuple(self, tmp_path: Path) -> None:
        """``Field`` takes any iterable of strings, so a set literal is a valid spelling."""
        source = """\
from semolina import Dimension, SemanticView


class V(SemanticView, view="v"):
    country = Dimension[str](synonyms={"nation"})
"""
        field = read_committed_model(_write(tmp_path, source))[0].fields["country"]

        assert field.metadata == {"synonyms": ("nation",)}

    def test_an_unevaluable_argument_is_warned_about_not_compared(self, tmp_path: Path) -> None:
        source = """\
from semolina import Metric, SemanticView


class V(SemanticView, view="v", description=make_description()):
    revenue = Metric[float | None](label=make_label())
"""
        read = read_committed_model_file(_write(tmp_path, source))
        model = read.models[0]

        assert model.fields["revenue"].metadata == {}
        assert model.fields["revenue"].unreadable == frozenset({"label"})
        assert model.description is None
        assert model.unreadable == frozenset({"description"})
        assert any("label=" in w and "not compared" in w for w in read.warnings), read.warnings
        assert any("description=" in w and "not compared" in w for w in read.warnings), (
            read.warnings
        )
