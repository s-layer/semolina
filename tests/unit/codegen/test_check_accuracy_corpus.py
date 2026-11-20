"""
The ``--check`` accuracy corpus, one test per case.

Every case comes from ``.planning/research/2026-09-30-CHECK-ACCURACY.md``. Each test asserts
the outcome ``--check`` *should* give. Plan 55-05 added the cases that failed then as strict
``xfail`` tests, so decision D9 could judge each alternative by which cases it turned green;
plan 55-06 implemented D9 and removed each marker as its case went green. Three cases are
limitations D9 accepts (M10, D3 and D5, recorded in
``.planning/research/2026-10-15-D9-DECISION.md``). Each is a test that asserts the
limitation, so a change to it is a decision rather than an accident.

Cases M7 and M8 are not here. They are GEN-09 (an untyped ``Metric()`` compared with
``Any | None``, and a check that never probed exiting 0) and are tested where that fix lands.

Both commands are driven through their CLI ``_run_check`` functions rather than through the
reader and the comparison separately. The cases are about what a user sees, an exit code,
and the fix may move work between the reader and the comparison. A test pinned to today's
seam would have to be rewritten by the very change it is meant to judge.

Record/replay contract: the model half runs live, in-process, against the in-memory DuckDB
probe view, and must never carry ``pytest.mark.adbc_cassette`` (see
``tests/unit/codegen/test_annotation_check.py``'s module docstring). The DTO half needs no
warehouse: ``ProbedQuery`` carries a hand-written schema.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, cast

import pyarrow
import pytest
import typer

from semolina.cli.codegen import EXIT_ANNOTATION_DRIFT
from semolina.codegen.annotation_check import STATUS_MATCH
from semolina.codegen.dto_check import check_dto
from semolina.codegen.dto_reader import read_committed_dtos
from semolina.codegen.dto_renderer import ProbedQuery, render_and_format_dtos
from semolina.codegen.probe import ROUTE_EXECUTE_SCHEMA

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence
    from pathlib import Path

    from semolina.engines.base import Engine
    from semolina.query import Query

pytest.importorskip("adbc_driver_duckdb")

VIEW = "type_fidelity_view"
"""The in-memory probe view: seven metrics, one dimension, no facts."""

DTO_CLASS = "ValueByRegion"
"""The DTO class every DTO case is generated as."""


@pytest.fixture(scope="module")
def generated_model() -> str:
    """
    Generate the model source for :data:`VIEW` through the real renderer.

    Generated rather than hand-written, so each case is "what codegen writes, then one edit"
    and cannot go stale when the renderer changes.

    Returns:
        The formatted model source.
    """
    from type_fidelity_probe import make_probe_engine

    from semolina.codegen.python_renderer import render_and_format

    engine = make_probe_engine()
    try:
        return render_and_format([engine.introspect(VIEW)])
    finally:
        engine.dispose()


@pytest.fixture
def probe_engine() -> Generator[Engine]:
    """Yield the type-fidelity probe's in-memory DuckDB engine, disposing it on teardown."""
    from type_fidelity_probe import make_probe_engine

    engine = make_probe_engine()
    yield engine
    engine.dispose()


def _edit(source: str, old: str, new: str) -> str:
    """
    Replace one line-fragment of generated source, failing if it is not there exactly once.

    Args:
        source: The generated source.
        old: The text to replace.
        new: Its replacement.

    Returns:
        The edited source.
    """
    assert source.count(old) == 1, f"expected exactly one {old!r} in:\n{source}"
    return source.replace(old, new)


def _model_exit_code(engine: Engine, views: list[str], path: Path) -> int:
    """
    Run ``semolina codegen --check`` for ``views`` against ``path`` and return its exit code.

    Args:
        engine: A live engine.
        views: The view names as the user would pass them on the command line.
        path: The committed model file.

    Returns:
        The exit code: 0 when ``_run_check`` returns normally.
    """
    from semolina.cli.codegen import _run_check

    try:
        _run_check(engine, views, path)
    except typer.Exit as e:
        return e.exit_code
    return 0


# ---------------------------------------------------------------------------
# codegen --check: false positives
# ---------------------------------------------------------------------------


class TestM1EquivalentAnnotationSpellings:
    """
    M1: an annotation spelled differently from codegen's, meaning the same type.

    Before D9 ``--check`` compared annotation strings exactly, so each of these drifted. Each
    matches now: the type the field declares is the type the column arrives as.
    """

    def test_optional(self, generated_model: str, probe_engine: Engine, tmp_path: Path) -> None:
        source = _edit(
            generated_model,
            "total_order_value = Metric[decimal.Decimal | None]()",
            "total_order_value = Metric[Optional[decimal.Decimal]]()",
        )
        path = tmp_path / "models.py"
        path.write_text("from typing import Optional\n" + source, encoding="utf-8")

        assert _model_exit_code(probe_engine, [VIEW], path) == 0

    def test_none_first(self, generated_model: str, probe_engine: Engine, tmp_path: Path) -> None:
        source = _edit(
            generated_model,
            "total_order_value = Metric[decimal.Decimal | None]()",
            "total_order_value = Metric[None | decimal.Decimal]()",
        )
        path = tmp_path / "models.py"
        path.write_text(source, encoding="utf-8")

        assert _model_exit_code(probe_engine, [VIEW], path) == 0

    def test_bare_imported_name(
        self, generated_model: str, probe_engine: Engine, tmp_path: Path
    ) -> None:
        source = _edit(
            generated_model,
            "total_order_value = Metric[decimal.Decimal | None]()",
            "total_order_value = Metric[Decimal | None]()",
        )
        path = tmp_path / "models.py"
        path.write_text("from decimal import Decimal\n" + source, encoding="utf-8")

        assert _model_exit_code(probe_engine, [VIEW], path) == 0

    def test_string_annotation(
        self, generated_model: str, probe_engine: Engine, tmp_path: Path
    ) -> None:
        source = _edit(generated_model, "region = Dimension[str]()", "region = Dimension['str']()")
        path = tmp_path / "models.py"
        path.write_text(source, encoding="utf-8")

        assert _model_exit_code(probe_engine, [VIEW], path) == 0

    def test_a_different_type_spelled_the_same_way_still_drifts(
        self, generated_model: str, probe_engine: Engine, tmp_path: Path
    ) -> None:
        """
        The control: ``Optional[int]`` for a decimal column is real drift.

        An alternative that fixed M1 by comparing less than the type would turn this green.
        """
        source = _edit(
            generated_model,
            "total_order_value = Metric[decimal.Decimal | None]()",
            "total_order_value = Metric[Optional[int]]()",
        )
        path = tmp_path / "models.py"
        path.write_text("from typing import Optional\n" + source, encoding="utf-8")

        assert _model_exit_code(probe_engine, [VIEW], path) == EXIT_ANNOTATION_DRIFT

    def test_a_dropped_none_still_drifts(
        self, generated_model: str, probe_engine: Engine, tmp_path: Path
    ) -> None:
        """The control for nullability: ``Decimal`` without ``None`` is not ``Optional``."""
        source = _edit(
            generated_model,
            "total_order_value = Metric[decimal.Decimal | None]()",
            "total_order_value = Metric[Decimal]()",
        )
        path = tmp_path / "models.py"
        path.write_text("from decimal import Decimal\n" + source, encoding="utf-8")

        assert _model_exit_code(probe_engine, [VIEW], path) == EXIT_ANNOTATION_DRIFT


class TestM2FieldDeclarationSpellings:
    """
    M2: a field declared through a spelling the reader does not recognise.

    The reader knows only ``name = Metric[T]()`` with the bare names ``Metric``,
    ``Dimension`` and ``Fact``. Any other spelling was skipped without a word, and the field
    is then reported as absent.
    """

    def test_qualified_field_class(
        self, generated_model: str, probe_engine: Engine, tmp_path: Path
    ) -> None:
        source = _edit(
            generated_model,
            "total_order_value = Metric[decimal.Decimal | None]()",
            "total_order_value = semolina.Metric[decimal.Decimal | None]()",
        )
        path = tmp_path / "models.py"
        path.write_text("import semolina\n" + source, encoding="utf-8")

        assert _model_exit_code(probe_engine, [VIEW], path) == 0

    def test_aliased_field_class(
        self, generated_model: str, probe_engine: Engine, tmp_path: Path
    ) -> None:
        source = _edit(
            generated_model,
            "total_order_value = Metric[decimal.Decimal | None]()",
            "total_order_value = M[decimal.Decimal | None]()",
        )
        path = tmp_path / "models.py"
        path.write_text("from semolina import Metric as M\n" + source, encoding="utf-8")

        assert _model_exit_code(probe_engine, [VIEW], path) == 0

    def test_annotated_assignment(
        self, generated_model: str, probe_engine: Engine, tmp_path: Path
    ) -> None:
        source = _edit(
            generated_model,
            "region = Dimension[str]()",
            "region: Dimension[str] = Dimension[str]()",
        )
        path = tmp_path / "models.py"
        path.write_text(source, encoding="utf-8")

        assert _model_exit_code(probe_engine, [VIEW], path) == 0

    def test_an_aliased_field_with_the_wrong_type_still_drifts(
        self, generated_model: str, probe_engine: Engine, tmp_path: Path
    ) -> None:
        """
        The control: once ``M`` is read, its annotation is compared like any other.

        This passed before D9 for the wrong reason (the field was absent). It is here so the fix
        cannot make an aliased field match by skipping its comparison.
        """
        source = _edit(
            generated_model,
            "total_order_value = Metric[decimal.Decimal | None]()",
            "total_order_value = M[int | None]()",
        )
        path = tmp_path / "models.py"
        path.write_text("from semolina import Metric as M\n" + source, encoding="utf-8")

        assert _model_exit_code(probe_engine, [VIEW], path) == EXIT_ANNOTATION_DRIFT


class TestM3ViewFromAConstant:
    """M3: ``view=`` given as a name rather than a string literal. The class was skipped."""

    def test_same_module_constant(
        self, generated_model: str, probe_engine: Engine, tmp_path: Path
    ) -> None:
        source = _edit(generated_model, f'view="{VIEW}"', "view=VIEW_NAME")
        source = _edit(
            source,
            "\n\nclass TypeFidelityView",
            f'\n\nVIEW_NAME = "{VIEW}"\n\n\nclass TypeFidelityView',
        )
        path = tmp_path / "models.py"
        path.write_text(source, encoding="utf-8")

        assert _model_exit_code(probe_engine, [VIEW], path) == 0

    def test_constant_from_another_module(
        self,
        generated_model: str,
        probe_engine: Engine,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        package = tmp_path / "app"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "names.py").write_text(f'VIEW_NAME = "{VIEW}"\n', encoding="utf-8")
        source = _edit(generated_model, f'view="{VIEW}"', "view=VIEW_NAME")
        path = package / "models.py"
        path.write_text("from app.names import VIEW_NAME\n" + source, encoding="utf-8")
        # The project root is the working directory, as for `codegen-dto`'s dotted paths.
        monkeypatch.chdir(tmp_path)

        assert _model_exit_code(probe_engine, [VIEW], path) == 0


class TestM4ViewNameSpelling:
    """
    M4: the view named on the command line spelled differently from ``view=``.

    Models are matched to views by exact string. DuckDB treats both spellings below as the
    same view, and the project follows DuckDB's identifier rules.
    """

    def test_schema_qualified_on_the_command_line(
        self, generated_model: str, probe_engine: Engine, tmp_path: Path
    ) -> None:
        path = tmp_path / "models.py"
        path.write_text(generated_model, encoding="utf-8")

        assert _model_exit_code(probe_engine, [f"main.{VIEW}"], path) == 0

    def test_case_differs(self, generated_model: str, probe_engine: Engine, tmp_path: Path) -> None:
        path = tmp_path / "models.py"
        path.write_text(generated_model, encoding="utf-8")

        assert _model_exit_code(probe_engine, [VIEW.upper()], path) == 0


class TestM4ViewNameSpellingFollowsTheEngine:
    """
    M4 on Snowflake: ``"Sales"`` and ``SALES`` are two views, so each matches only its own class.

    Folding DuckDB's way on every engine matched both classes to either name, and checked
    one of them against a view it does not describe.
    """

    SOURCE = (
        "from semolina import Dimension, SemanticView\n\n\n"
        "class QuotedSales(SemanticView, view='\"Sales\"'):\n"
        "    region = Dimension[str]()\n\n\n"
        'class UpperSales(SemanticView, view="SALES"):\n'
        "    region = Dimension[str]()\n"
    )

    @pytest.mark.parametrize(
        ("view", "expected"),
        [("SALES", ["UpperSales"]), ("sales", ["UpperSales"]), ('"Sales"', ["QuotedSales"])],
    )
    def test_snowflake_matches_by_its_own_rules(
        self,
        view: str,
        expected: list[str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from types import SimpleNamespace

        from semolina.codegen import annotation_check
        from semolina.engines.sql import SnowflakeDialect

        checked: list[str | None] = []

        def fake_check_view(
            engine: object, view_name: str, committed: object
        ) -> annotation_check.ViewCheckReport:
            checked.append(getattr(committed, "class_name", None))
            return annotation_check.ViewCheckReport(view_name=view_name, rows=[], has_drift=False)

        monkeypatch.setattr(annotation_check, "check_view", fake_check_view)
        path = tmp_path / "models.py"
        path.write_text(self.SOURCE, encoding="utf-8")
        # Only the dialect is read before check_view, which is replaced above.
        engine = cast("Engine", SimpleNamespace(dialect=SnowflakeDialect()))

        assert _model_exit_code(engine, [view], path) == 0
        assert checked == expected


class TestM5RenamedAttributeSameColumn:
    """
    M5: an attribute renamed by hand, still bound to the same column through ``source=``.

    Fields were matched by attribute name, so the warehouse's ``region`` read as
    absent and ``sales_region`` as a field the warehouse does not have.
    """

    def test_renamed_with_source(
        self, generated_model: str, probe_engine: Engine, tmp_path: Path
    ) -> None:
        source = _edit(
            generated_model,
            "region = Dimension[str]()",
            'sales_region = Dimension[str](source="region")',
        )
        path = tmp_path / "models.py"
        path.write_text(source, encoding="utf-8")

        assert _model_exit_code(probe_engine, [VIEW], path) == 0


def _split_into_base(generated_model: str, base_header: str) -> tuple[str, str]:
    """
    Move ``region`` out of the generated class into a base class.

    Args:
        generated_model: The generated source.
        base_header: The base's ``class`` line, e.g. ``'class Base(SemanticView, abstract=True):'``.

    Returns:
        The base class source, and the subclass source with ``region`` removed and the base
        named as its parent.
    """
    subclass = _edit(generated_model, "    region = Dimension[str]()\n", "")
    subclass = _edit(subclass, "(SemanticView, view=", "(Base, view=")
    base = f"{base_header}\n    region = Dimension[str]()\n"
    return base, subclass


class TestM6InheritedFields:
    """M6: fields declared on a base class. Each class body was read alone before D9."""

    def test_base_in_the_same_file(
        self, generated_model: str, probe_engine: Engine, tmp_path: Path
    ) -> None:
        base, subclass = _split_into_base(
            generated_model, "class Base(SemanticView, abstract=True):"
        )
        source = _edit(
            subclass, "\n\nclass TypeFidelityView", f"\n\n{base}\n\nclass TypeFidelityView"
        )
        path = tmp_path / "models.py"
        path.write_text(source, encoding="utf-8")

        assert _model_exit_code(probe_engine, [VIEW], path) == 0

    def test_base_in_another_module(
        self,
        generated_model: str,
        probe_engine: Engine,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        base, subclass = _split_into_base(
            generated_model, "class Base(SemanticView, abstract=True):"
        )
        package = tmp_path / "app"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "base.py").write_text(
            "from semolina import Dimension, SemanticView\n\n\n" + base, encoding="utf-8"
        )
        path = package / "models.py"
        path.write_text("from app.base import Base\n" + subclass, encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert _model_exit_code(probe_engine, [VIEW], path) == 0


# ---------------------------------------------------------------------------
# codegen --check: false negatives
# ---------------------------------------------------------------------------


class TestM9TwoClassesForOneView:
    """
    M9: two classes in one file declare the same view.

    ``by_view`` was a dict, so the last class won and the first was never checked. D9 checks
    every class for the view and reports each; a drifted class must not pass unseen.
    """

    def test_a_drifted_first_class_is_reported(
        self, generated_model: str, probe_engine: Engine, tmp_path: Path
    ) -> None:
        drifted = _edit(
            generated_model,
            "total_order_value = Metric[decimal.Decimal | None]()",
            "total_order_value = Metric[int | None]()",
        )
        drifted = _edit(drifted, "class TypeFidelityView(", "class OldTypeFidelityView(")
        # The drifted class first, then the generated one: before D9 the last one won.
        correct_class = generated_model[generated_model.index("class TypeFidelityView(") :]
        path = tmp_path / "models.py"
        path.write_text(drifted + "\n\n" + correct_class, encoding="utf-8")

        assert _model_exit_code(probe_engine, [VIEW], path) == EXIT_ANNOTATION_DRIFT

    def test_a_drifted_last_class_is_reported(
        self, generated_model: str, probe_engine: Engine, tmp_path: Path
    ) -> None:
        """The control: with the order reversed the drifted class won, so this passed before D9."""
        drifted_class = _edit(
            generated_model[generated_model.index("class TypeFidelityView(") :],
            "total_order_value = Metric[decimal.Decimal | None]()",
            "total_order_value = Metric[int | None]()",
        )
        drifted_class = _edit(
            drifted_class, "class TypeFidelityView(", "class OldTypeFidelityView("
        )
        path = tmp_path / "models.py"
        path.write_text(generated_model + "\n\n" + drifted_class, encoding="utf-8")

        assert _model_exit_code(probe_engine, [VIEW], path) == EXIT_ANNOTATION_DRIFT


class TestM10TheProbeIsUnfiltered:
    """
    M10: ``--check`` measures the unfiltered query's result shape, never a filtered one.

    A documented limitation, not a bug: Snowflake refuses ``ExecuteSchema`` for any query
    carrying a bound parameter, and ``--check`` has no filter to apply. A filter should not
    change a column's type. D9 accepted it (sub-decision 8 in
    ``.planning/research/2026-10-15-D9-DECISION.md``), and this test pins it (GEN-12: "a test
    that passes, or a documented limitation D9 accepts").
    """

    def test_the_probe_query_carries_no_filter(self, probe_engine: Engine) -> None:
        from semolina.codegen.annotation_check import _build_query, _canonical_model, _probe_fields

        view = probe_engine.introspect(VIEW)
        model = _canonical_model(view)
        builder = probe_engine.dialect.create_builder()

        sql, params = builder.build_select_with_params(_build_query(model, _probe_fields(view)))
        assert "WHERE" not in sql.upper()
        assert not params


# ---------------------------------------------------------------------------
# codegen-dto --check
# ---------------------------------------------------------------------------


def _dto_query() -> Query:
    """
    The probe view's headline projection: a decimal metric, a COUNT metric, a dimension.

    Returns:
        The query.
    """
    from type_fidelity_probe import TypeFidelityView as View

    return View.query().metrics(View.total_order_value, View.n_order_totals).dimensions(View.region)


def _probed(columns: Sequence[tuple[str, pyarrow.DataType]] | None = None) -> ProbedQuery:
    """
    Build a DuckDB ``ProbedQuery`` for :func:`_dto_query` from a hand-written schema.

    Args:
        columns: ``(name, type)`` pairs. Defaults to what DuckDB really returns.

    Returns:
        The probed query.
    """
    from semolina.dialect import Dialect, resolve_dialect

    if columns is None:
        columns = [
            ("total_order_value", pyarrow.decimal128(38, 2)),
            ("n_order_totals", pyarrow.int64()),
            ("region", pyarrow.string()),
        ]
    return ProbedQuery(
        class_name=DTO_CLASS,
        origin="myapp.queries.value_by_region",
        query=_dto_query(),
        dialect=resolve_dialect(Dialect.DUCKDB),
        schema=pyarrow.schema([pyarrow.field(n, t) for n, t in columns]),
        route=ROUTE_EXECUTE_SCHEMA,
    )


@pytest.fixture(scope="module")
def generated_dto() -> str:
    """
    Generate the DTO module for :func:`_probed` through the real renderer.

    Returns:
        The formatted DTO source.
    """
    return render_and_format_dtos([_probed()], backend_label="duckdb")


def _dto_exit_code(path: Path, probed: ProbedQuery | None = None) -> int:
    """
    Run ``semolina codegen-dto --check`` against ``path`` and return its exit code.

    Args:
        path: The committed DTO file.
        probed: The probed query. Defaults to :func:`_probed`.

    Returns:
        The exit code: 0 when ``_run_check`` returns normally.
    """
    from semolina.cli.dto_codegen import _run_check

    try:
        _run_check([probed or _probed()], path)
    except typer.Exit as e:
        return e.exit_code
    return 0


class TestD1EquivalentAnnotationSpellings:
    """D1: a DTO annotation spelled differently from codegen's, meaning the same type."""

    def test_optional_qualified(self, generated_dto: str, tmp_path: Path) -> None:
        source = _edit(
            generated_dto,
            "total_order_value: decimal.Decimal | None",
            "total_order_value: Optional[decimal.Decimal]",
        )
        path = tmp_path / "dtos.py"
        path.write_text(
            source.replace("import decimal\n", "import decimal\nfrom typing import Optional\n"),
            encoding="utf-8",
        )

        assert _dto_exit_code(path) == 0

    def test_optional_bare_import(self, generated_dto: str, tmp_path: Path) -> None:
        source = _edit(
            generated_dto,
            "total_order_value: decimal.Decimal | None",
            "total_order_value: Optional[Decimal]",
        )
        source = source.replace(
            "import decimal\n", "from decimal import Decimal\nfrom typing import Optional\n"
        )
        path = tmp_path / "dtos.py"
        path.write_text(source, encoding="utf-8")

        assert _dto_exit_code(path) == 0

    def test_a_different_type_still_drifts(self, generated_dto: str, tmp_path: Path) -> None:
        """The control: ``Optional[int]`` for a decimal column is real drift."""
        source = _edit(
            generated_dto,
            "total_order_value: decimal.Decimal | None",
            "total_order_value: Optional[int]",
        )
        path = tmp_path / "dtos.py"
        path.write_text(
            source.replace("import decimal\n", "from typing import Optional\n"), encoding="utf-8"
        )

        assert _dto_exit_code(path) == EXIT_ANNOTATION_DRIFT


class TestD2HandEditedAlias:
    """D2: a field's binding to its column hand-edited to another pydantic spelling."""

    def test_alias_keyword(self, generated_dto: str, tmp_path: Path) -> None:
        """``Field(alias=...)`` is read since phase 53, so this half of D2 already passes."""
        source = _edit(
            generated_dto,
            "    total_order_value: decimal.Decimal | None\n",
            "    total_order_value: decimal.Decimal | None = pydantic.Field(\n"
            '        alias="total_order_value"\n'
            "    )\n",
        )
        path = tmp_path / "dtos.py"
        path.write_text(source, encoding="utf-8")

        assert _dto_exit_code(path) == 0

    def test_alias_choices_including_the_column(self, generated_dto: str, tmp_path: Path) -> None:
        source = _edit(
            generated_dto,
            "    total_order_value: decimal.Decimal | None\n",
            "    total_order_value: decimal.Decimal | None = pydantic.Field(\n"
            '        validation_alias=pydantic.AliasChoices("total_order_value", "tov")\n'
            "    )\n",
        )
        path = tmp_path / "dtos.py"
        path.write_text(source, encoding="utf-8")

        assert _dto_exit_code(path) == 0


class TestD3DeliberateNarrowing:
    """
    D3: ``float`` for a decimal column, used with ``.into(..., validate=True)``.

    ``how-to/typed-results.rst`` calls that narrowing legitimate on the validated path. The
    fast path refuses it, and ``--check`` judges by the fast path. D9 accepted this as a
    documented limitation rather than adding a ``--validate`` mode (sub-decision 5 in
    ``.planning/research/2026-10-15-D9-DECISION.md``): a DTO narrowed on purpose reads as
    drift, and the advice is to keep such a DTO out of ``--check``.
    """

    def test_judged_by_the_fast_path_it_drifts(self, generated_dto: str, tmp_path: Path) -> None:
        """The only judgement ``--check`` makes, and correct for a DTO used on the fast path."""
        source = _edit(
            generated_dto,
            "total_order_value: decimal.Decimal | None",
            "total_order_value: float | None",
        )
        path = tmp_path / "dtos.py"
        path.write_text(source, encoding="utf-8")

        assert _dto_exit_code(path) == EXIT_ANNOTATION_DRIFT

    def test_there_is_no_way_to_judge_by_validate_true(self) -> None:
        """
        The accepted limitation: neither the comparison nor the command takes a validate mode.

        Pinned so that adding one is a decision that has to revisit D9's sub-decision 5 and
        the docs that describe the limitation, rather than a quiet extension.
        """
        from semolina.cli.dto_codegen import codegen_dto

        assert "validate" not in inspect.signature(check_dto).parameters
        assert "validate" not in inspect.signature(codegen_dto).parameters


class TestD4RenamedClass:
    """
    D4: a generated DTO class renamed by hand, its provenance docstring intact.

    Classes were matched by name, so the generated name read as missing and the
    renamed one as an extra class.
    """

    def test_renamed_class_with_provenance(self, generated_dto: str, tmp_path: Path) -> None:
        source = _edit(generated_dto, f"class {DTO_CLASS}(", "class RegionRevenue(")
        path = tmp_path / "dtos.py"
        path.write_text(source, encoding="utf-8")

        assert _dto_exit_code(path) == 0


class TestD5GeneratedAnyAcceptsAnything:
    """
    D5: any committed annotation agrees with a generated ``Any``. Deliberate and documented.

    Codegen writes ``Any`` with a ``# TODO`` for a type its map has no entry for, and the
    how-to tells you to replace it. So a ``str`` there passes even for a column of lists.
    D9 accepted it (sub-decision 8 in ``.planning/research/2026-10-15-D9-DECISION.md``), and
    this test pins it, as GEN-12 requires.
    """

    def test_a_replaced_any_is_kept(self, tmp_path: Path) -> None:
        listy = _probed(
            [
                ("total_order_value", pyarrow.list_(pyarrow.string())),
                ("n_order_totals", pyarrow.int64()),
                ("region", pyarrow.string()),
            ]
        )
        source = render_and_format_dtos([listy], backend_label="duckdb")
        source = _edit(source, "total_order_value: Any | None", "total_order_value: str | None")
        path = tmp_path / "dtos.py"
        path.write_text(source, encoding="utf-8")

        report = check_dto(listy, read_committed_dtos(path)[0])

        assert report.has_drift is False
        row = next(r for r in report.rows if r.name == "total_order_value")
        assert row.status == STATUS_MATCH
        assert "no opinion" in row.detail
