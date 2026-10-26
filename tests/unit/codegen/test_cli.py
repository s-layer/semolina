"""
Tests for the reverse codegen CLI command.

Uses CliRunner to invoke the full Typer app with a mocked engine injected via
unittest.mock.patch, avoiding any warehouse connections.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from semolina.cli import app
from semolina.cli.codegen import (
    EXIT_ANNOTATION_DRIFT,
    EXIT_CONNECTION_ERROR,
    EXIT_INVALID_BACKEND,
    EXIT_VIEW_NOT_FOUND,
    _render_check_report,
)
from semolina.codegen.annotation_check import (
    ABSENT,
    ROUTE_METADATA,
    STATUS_DRIFT,
    STATUS_MATCH,
    FieldCheckRow,
    ViewCheckReport,
)
from semolina.codegen.introspector import IntrospectedField, IntrospectedView
from semolina.codegen.probe import ROUTE_EXECUTE_SCHEMA
from semolina.engines.base import SemolinaConnectionError, SemolinaViewNotFoundError

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import Result

runner = CliRunner()


def make_mock_engine(views: list[IntrospectedView]) -> MagicMock:
    """
    Build a MagicMock engine whose introspect() returns views by view_name.

    Args:
        views: IntrospectedView objects to serve via introspect().

    Returns:
        MagicMock with introspect configured as a side_effect lookup.
    """
    engine = MagicMock()
    engine.introspect.side_effect = lambda view_name: next(
        v for v in views if v.view_name == view_name
    )
    return engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SALES_VIEW = IntrospectedView(
    view_name="my_schema.my_sales_view",
    class_name="MySalesView",
    fields=[
        IntrospectedField(name="revenue", field_type="metric", data_type="int"),
        IntrospectedField(name="country", field_type="dimension", data_type="str"),
        IntrospectedField(name="unit_price", field_type="fact", data_type="float"),
    ],
)

DESCRIBED_VIEW = IntrospectedView(
    view_name="my_schema.orders",
    class_name="Orders",
    fields=[
        IntrospectedField(
            name="revenue",
            field_type="metric",
            data_type="int",
            description="Total revenue",
        ),
        IntrospectedField(name="country", field_type="dimension", data_type="str", description=""),
    ],
)

TODO_VIEW = IntrospectedView(
    view_name="my_schema.geo",
    class_name="Geo",
    fields=[
        IntrospectedField(name="location", field_type="dimension", data_type="TODO: GEOGRAPHY"),
    ],
)

SECOND_VIEW = IntrospectedView(
    view_name="my_schema.customers",
    class_name="Customers",
    fields=[
        IntrospectedField(name="customer_id", field_type="dimension", data_type="int"),
    ],
)


# ---------------------------------------------------------------------------
# TestCLIBasicBehavior
# ---------------------------------------------------------------------------


class TestCLIBasicBehavior:
    """Basic CLI plumbing: help, missing args, version flag."""

    def test_help_shows_usage(self) -> None:
        """Help output includes --backend option and positional views argument."""
        result = runner.invoke(app, ["codegen", "--help"])
        assert result.exit_code == 0
        assert "--backend" in result.output
        # Typer uses the parameter name as metavar; 'views' appears in Usage line
        assert "views" in result.output.lower()

    def test_missing_backend_exits_error(self) -> None:
        """Omitting --backend causes a non-zero exit."""
        result = runner.invoke(app, ["codegen", "my_schema.my_view"])
        assert result.exit_code != 0

    def test_missing_view_exits_error(self) -> None:
        """Omitting view_names causes a non-zero exit."""
        result = runner.invoke(app, ["codegen", "--backend", "snowflake"])
        assert result.exit_code != 0

    def test_version_flag(self) -> None:
        """--version prints semolina and exits 0."""
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "semolina" in result.output


# ---------------------------------------------------------------------------
# TestReverseCodegenOutput
# ---------------------------------------------------------------------------


class TestReverseCodegenOutput:
    """Core: introspect view -> emit Python class."""

    def test_generates_python_class_for_view(self) -> None:
        """Output contains a SemanticView subclass for the introspected view."""
        mock_engine = make_mock_engine([SALES_VIEW])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app, ["codegen", "my_schema.my_sales_view", "--backend", "snowflake"]
            )
        assert result.exit_code == 0, result.output
        assert "class MySalesView(SemanticView" in result.output

    def test_view_parameter_uses_full_qualified_name(self) -> None:
        """Generated class contains the schema-qualified view name in view=."""
        mock_engine = make_mock_engine([SALES_VIEW])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app, ["codegen", "my_schema.my_sales_view", "--backend", "snowflake"]
            )
        assert result.exit_code == 0
        assert 'view="my_schema.my_sales_view"' in result.output

    def test_metric_field_emitted_correctly(self) -> None:
        """Metric field appears as `revenue = Metric[int | None]()` in output."""
        mock_engine = make_mock_engine([SALES_VIEW])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app, ["codegen", "my_schema.my_sales_view", "--backend", "snowflake"]
            )
        assert result.exit_code == 0
        assert "revenue = Metric[int | None]()" in result.output

    def test_dimension_field_emitted_correctly(self) -> None:
        """Dimension field appears as `country = Dimension[str]()` in output."""
        mock_engine = make_mock_engine([SALES_VIEW])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app, ["codegen", "my_schema.my_sales_view", "--backend", "snowflake"]
            )
        assert result.exit_code == 0
        assert "country = Dimension[str]()" in result.output

    def test_fact_field_emitted_correctly(self) -> None:
        """Fact field appears as `unit_price = Fact[float]()` in output."""
        mock_engine = make_mock_engine([SALES_VIEW])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app, ["codegen", "my_schema.my_sales_view", "--backend", "snowflake"]
            )
        assert result.exit_code == 0
        assert "unit_price = Fact[float]()" in result.output

    def test_imports_at_top(self) -> None:
        """Output starts with the standard semolina import line."""
        mock_engine = make_mock_engine([SALES_VIEW])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app, ["codegen", "my_schema.my_sales_view", "--backend", "snowflake"]
            )
        assert result.exit_code == 0
        assert "from semolina import Dimension, Fact, Metric, SemanticView" in result.output

    def test_field_with_description_emits_the_description_argument(self) -> None:
        """A field's description is written as ``description=``."""
        mock_engine = make_mock_engine([DESCRIBED_VIEW])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(app, ["codegen", "my_schema.orders", "--backend", "snowflake"])
        assert result.exit_code == 0
        assert 'description="Total revenue",' in result.output

    def test_field_without_description_keeps_the_one_line_form(self) -> None:
        """A field with an empty description gets no ``description=`` and no docstring."""
        mock_engine = make_mock_engine([DESCRIBED_VIEW])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(app, ["codegen", "my_schema.orders", "--backend", "snowflake"])
        assert result.exit_code == 0
        assert "    country = Dimension[str]()\n" in result.output
        assert '"""' not in result.output.split("class ", 1)[1]

    def test_todo_comment_for_unresolved_type(self) -> None:
        """Field with data_type starting 'TODO:' emits a # TODO: comment."""
        mock_engine = make_mock_engine([TODO_VIEW])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(app, ["codegen", "my_schema.geo", "--backend", "snowflake"])
        assert result.exit_code == 0
        assert "# TODO:" in result.output

    def test_output_to_stdout(self) -> None:
        """Python source goes to result.output (stdout); exit code is 0."""
        mock_engine = make_mock_engine([SALES_VIEW])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app, ["codegen", "my_schema.my_sales_view", "--backend", "snowflake"]
            )
        assert result.exit_code == 0
        assert len(result.output) > 0


# ---------------------------------------------------------------------------
# TestMultipleViews
# ---------------------------------------------------------------------------


class TestMultipleViews:
    """Multiple view names produce multiple class definitions."""

    def test_two_views_generate_two_classes(self) -> None:
        """Passing two view names emits two Python class definitions."""
        mock_engine = make_mock_engine([SALES_VIEW, SECOND_VIEW])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app,
                [
                    "codegen",
                    "my_schema.my_sales_view",
                    "my_schema.customers",
                    "--backend",
                    "snowflake",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "class MySalesView(SemanticView" in result.output
        assert "class Customers(SemanticView" in result.output

    def test_single_imports_section_for_multiple_views(self) -> None:
        """Only one semolina import line appears even when rendering two views."""
        mock_engine = make_mock_engine([SALES_VIEW, SECOND_VIEW])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app,
                [
                    "codegen",
                    "my_schema.my_sales_view",
                    "my_schema.customers",
                    "--backend",
                    "snowflake",
                ],
            )
        assert result.exit_code == 0
        assert result.output.count("from semolina import") == 1

    def test_a_view_named_twice_generates_one_class(self) -> None:
        """
        ``codegen sales sales`` is the same request as ``codegen sales``, not a collision.

        The two-views-one-class-name refusal tells the user to generate the views into
        separate files, which is no remedy for a view that was simply repeated -- in a shell
        loop, or a Makefile variable that listed it twice. The repeat is dropped, keeping the
        first occurrence's position, before anything is introspected.
        """
        mock_engine = make_mock_engine([SALES_VIEW, SECOND_VIEW])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app,
                [
                    "codegen",
                    "my_schema.my_sales_view",
                    "my_schema.customers",
                    "my_schema.my_sales_view",
                    "--backend",
                    "snowflake",
                ],
            )
        assert result.exit_code == 0, result.output
        classes = [
            line.split("(")[0].removeprefix("class ")
            for line in result.stdout.splitlines()
            if line.startswith("class ")
        ]
        assert classes == ["MySalesView", "Customers"]
        assert [c.args for c in mock_engine.introspect.call_args_list] == [
            ("my_schema.my_sales_view",),
            ("my_schema.customers",),
        ]


# ---------------------------------------------------------------------------
# TestValidityGate
# ---------------------------------------------------------------------------


def _diagnostics(result: Result) -> str:
    """
    Read stderr back as one unwrapped line, so a test asserts what a message says.

    Rich hard-wraps to the console width; collapsing whitespace makes the assertion
    independent of where the wrap fell.

    Args:
        result: The ``CliRunner`` result.

    Returns:
        Stderr with every run of whitespace collapsed to a single space.
    """
    return " ".join(result.stderr.split())


UNUSABLE_CLASS_NAMES = [
    pytest.param("2024Sales", id="leading-digit"),
    pytest.param("class", id="keyword"),
    pytest.param("Sales:\n    pass\n\nimport os\nclass X", id="statement-injection"),
]
"""
Class names an engine may hand back that no ``class`` statement can carry.

The built-in engines all derive the name with ``introspector.class_name_for``, which never
produces one of these. A ``--backend dotted.path.ClassName`` engine builds its own
``IntrospectedView``, though, and its ``class_name`` is written into the generated file as a
bare token -- the one value no escaper can make safe.
"""


class TestValidityGate:
    """
    ``codegen`` refuses a class name it cannot emit, and never emits source that won't parse.

    The DTO command has had these guards since it shipped; the model command had none, and
    exited 0 having written a module that fails on import (A24).
    """

    @pytest.mark.parametrize("class_name", UNUSABLE_CLASS_NAMES)
    def test_a_view_whose_class_name_is_unusable_exits_2_naming_it(self, class_name: str) -> None:
        """The view and the class name are both named, and nothing reaches stdout."""
        view = IntrospectedView(
            view_name="analytics.2024_sales",
            class_name=class_name,
            fields=[IntrospectedField(name="revenue", field_type="metric", data_type="int")],
        )
        mock_engine = make_mock_engine([view])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app, ["codegen", "analytics.2024_sales", "--backend", "custom.Engine"]
            )
        assert result.exit_code == EXIT_INVALID_BACKEND, result.output
        assert result.stdout == "", result.stdout
        assert "analytics.2024_sales" in _diagnostics(result), result.stderr
        assert "not a valid Python class name" in _diagnostics(result), result.stderr

    @pytest.mark.parametrize(
        ("view_name", "class_name"),
        [
            pytest.param("analytics.metric", "Metric", id="Metric"),
            pytest.param("analytics.semantic_view", "SemanticView", id="SemanticView"),
            pytest.param("analytics.any", "Any", id="Any"),
            pytest.param("analytics.datetime", "datetime", id="datetime"),
        ],
    )
    def test_a_class_name_that_shadows_a_module_import_exits_2_naming_the_view(
        self, view_name: str, class_name: str
    ) -> None:
        """
        A custom engine's class named after something the generated module imports is refused.

        ``class Metric(SemanticView, ...)`` rebinds the imported ``Metric`` for every class
        declared after it -- the module parses, exits 0, and then fails or misbehaves on
        import. The built-in engines never derive such a name (``class_name_for`` prefixes it
        with ``View``), so this is the backstop for a ``--backend dotted.path.ClassName``
        engine that picks its own. The names come from
        ``python_renderer.GENERATED_MODULE_NAMES``, the set the import block is built from.
        The remedy is the engine's to apply: splitting views into separate modules does not
        help, because a lone ``class Metric`` still shadows the import.
        """
        view = IntrospectedView(
            view_name=view_name,
            class_name=class_name,
            fields=[IntrospectedField(name="revenue", field_type="metric", data_type="int")],
        )
        mock_engine = make_mock_engine([view])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(app, ["codegen", view_name, "--backend", "custom.Engine"])
        assert result.exit_code == EXIT_INVALID_BACKEND, result.output
        assert result.stdout == "", result.stdout
        diagnostics = _diagnostics(result)
        assert view_name in diagnostics, result.stderr
        assert f"{class_name!r}" in diagnostics, result.stderr
        assert "generated module imports" in diagnostics, result.stderr
        assert "its own module" not in diagnostics, result.stderr
        assert "engine" in diagnostics, result.stderr

    @pytest.mark.parametrize(
        ("class_name", "binds_as"),
        [
            pytest.param("Ｍetric", "Metric", id="fullwidth-M"),
            pytest.param("Ａny", "Any", id="fullwidth-A"),
        ],
    )
    def test_a_class_name_that_shadows_an_import_after_nfkc_is_refused(
        self, class_name: str, binds_as: str
    ) -> None:
        """
        Python reads ``class Ｍetric`` as ``class Metric``, so the check compares the same way.

        Both spellings pass ``isidentifier()``; only the raw string differs from the import.
        """
        view = IntrospectedView(
            view_name="analytics.m",
            class_name=class_name,
            fields=[IntrospectedField(name="revenue", field_type="metric", data_type="int")],
        )
        mock_engine = make_mock_engine([view])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(app, ["codegen", "analytics.m", "--backend", "custom.Engine"])
        assert result.exit_code == EXIT_INVALID_BACKEND, result.output
        assert result.stdout == "", result.stdout
        assert "generated module imports" in _diagnostics(result), result.stderr
        assert binds_as in _diagnostics(result), result.stderr

    def test_two_class_names_equal_after_nfkc_exit_2_naming_both(self) -> None:
        """
        ``Ｓales`` and ``Sales`` are one class to Python, so the second would replace the first.

        A custom engine that keeps a quoted view's fullwidth spelling hands back exactly this.
        """
        views = [
            IntrospectedView(
                view_name=view_name,
                class_name=class_name,
                fields=[IntrospectedField(name="revenue", field_type="metric", data_type="int")],
            )
            for view_name, class_name in (('"ｓales"', "Ｓales"), ("sales", "Sales"))
        ]
        mock_engine = make_mock_engine(views)
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app, ["codegen", '"ｓales"', "sales", "--backend", "custom.Engine"]
            )
        assert result.exit_code == EXIT_INVALID_BACKEND, result.output
        assert result.stdout == "", result.stdout
        diagnostics = _diagnostics(result)
        assert '"ｓales"' in diagnostics, result.stderr
        assert "both generate a class" in diagnostics, result.stderr

    def test_two_views_folding_to_one_class_name_exit_2_naming_both(self) -> None:
        """
        ``a.sales`` and ``b.sales`` both become ``Sales``, and one module cannot carry two.

        Python does not report it: the second ``class Sales`` silently replaces the first, so
        the file imports cleanly and is missing a model. Both views are named so the user
        knows which pair to split.
        """
        from semolina.codegen.introspector import class_name_for

        views = [
            IntrospectedView(
                view_name=name,
                class_name=class_name_for(name),
                fields=[IntrospectedField(name="revenue", field_type="metric", data_type="int")],
            )
            for name in ("emea.sales", "apac.sales")
        ]
        mock_engine = make_mock_engine(views)
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app, ["codegen", "emea.sales", "apac.sales", "--backend", "snowflake"]
            )
        assert result.exit_code == EXIT_INVALID_BACKEND, result.output
        assert result.stdout == "", result.stdout
        diagnostics = _diagnostics(result)
        assert "emea.sales" in diagnostics, result.stderr
        assert "apac.sales" in diagnostics, result.stderr
        assert "'Sales'" in diagnostics, result.stderr

    def test_source_that_does_not_parse_exits_1_and_emits_nothing(self) -> None:
        """
        The rendered module is ``ast.parse``d before it is emitted.

        The renderer is patched to produce a broken module, standing in for any template or
        escaping defect the name checks did not anticipate. ruff is switched off so the parse
        check is what has to notice, not the formatter.
        """
        mock_engine = make_mock_engine([SALES_VIEW])
        with (
            patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine),
            patch(
                "semolina.codegen.python_renderer.render_views",
                return_value="class MySalesView(:\n    pass\n",
            ),
            patch("semolina.codegen.python_renderer.ruff_available", return_value=False),
        ):
            result = runner.invoke(
                app, ["codegen", "my_schema.my_sales_view", "--backend", "snowflake"]
            )
        assert result.exit_code == 1, result.output
        assert result.stdout == "", result.stdout
        assert "does not parse" in _diagnostics(result), result.stderr
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_a_ruff_failure_exits_1_with_ruffs_message(self) -> None:
        """
        Ruff installed but exiting non-zero is reported, not swallowed (GEN-02).

        Before, the formatter's failure returned the unformatted source with exit 0, so the
        one tool that might have noticed a broken module stayed silent.
        """
        failed = MagicMock(returncode=2, stdout="", stderr="error: Failed to parse models.py")
        mock_engine = make_mock_engine([SALES_VIEW])
        with (
            patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine),
            patch("semolina.codegen.python_renderer.ruff_available", return_value=True),
            patch("semolina.codegen.python_renderer.subprocess.run", return_value=failed),
        ):
            result = runner.invoke(
                app, ["codegen", "my_schema.my_sales_view", "--backend", "snowflake"]
            )
        assert result.exit_code == 1, result.output
        assert result.stdout == "", result.stdout
        assert "Failed to parse models.py" in _diagnostics(result), result.stderr
        assert result.exception is None or isinstance(result.exception, SystemExit)


# ---------------------------------------------------------------------------
# TestCredentialsNeverReachStderr / TestTheSharedExitTable
# ---------------------------------------------------------------------------

# The two commands reach `_resolve_backend` through different arguments. `codegen-dto --view`
# builds its query from names alone, so it gets to the backend without importing anything or
# touching a warehouse -- the same point `codegen` reaches with a view name.
COMMAND_PREFIXES = [
    pytest.param(["codegen", "analytics.sales"], id="codegen"),
    pytest.param(
        ["codegen-dto", "--view", "analytics.sales", "--metrics", "revenue"], id="codegen-dto"
    ),
]

SECRET = "hunter2-do-not-print"


def _isolate_snowflake_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Run from an empty directory with no ``SNOWFLAKE_*`` variable inherited.

    Args:
        tmp_path: The directory to run from.
        monkeypatch: pytest's monkeypatch fixture.
    """
    import os

    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("SNOWFLAKE_") or key == "SEMOLINA_ENV_FILE":
            monkeypatch.delenv(key)


class TestCredentialsNeverReachStderr:
    """
    A config ``ValidationError`` names fields, never their values (GEN-03, A25).

    pydantic's own message carries ``input_value={'user': ..., 'password': ...}`` -- the
    whole dict the settings were built from -- and the command printed it verbatim. A CI log
    is the last place a warehouse password should land.
    """

    @pytest.mark.parametrize("prefix", COMMAND_PREFIXES)
    def test_a_password_from_the_environment_is_not_printed(
        self, prefix: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``account`` is missing; the error names it and leaves the password out."""
        _isolate_snowflake_config(tmp_path, monkeypatch)
        monkeypatch.setenv("SNOWFLAKE_USER", "bob")
        monkeypatch.setenv("SNOWFLAKE_PASSWORD", SECRET)

        result = runner.invoke(app, [*prefix, "--backend", "snowflake"])

        assert result.exit_code == EXIT_INVALID_BACKEND, result.output
        assert SECRET not in result.output
        assert SECRET not in result.stderr
        assert "account" in _diagnostics(result), result.stderr

    @pytest.mark.parametrize("prefix", COMMAND_PREFIXES)
    def test_a_password_from_semolina_toml_is_not_printed(
        self, prefix: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same, with the password committed to ``.semolina.toml``."""
        _isolate_snowflake_config(tmp_path, monkeypatch)
        (tmp_path / ".semolina.toml").write_text(
            f'[connections.snowflake]\nuser = "bob"\npassword = "{SECRET}"\n'
        )

        result = runner.invoke(app, [*prefix, "--backend", "snowflake"])

        assert result.exit_code == EXIT_INVALID_BACKEND, result.output
        assert SECRET not in result.output
        assert SECRET not in result.stderr
        assert "account" in _diagnostics(result), result.stderr

    @pytest.mark.parametrize("prefix", COMMAND_PREFIXES)
    def test_a_custom_backend_validation_error_is_redacted(self, prefix: list[str]) -> None:
        """
        A ``--backend dotted.Class`` constructor raising a ``ValidationError`` is redacted too.

        The class is the user's, but the error is pydantic's, and its ``str()`` carries
        ``input_value=`` -- here, the settings dict holding the password. It goes through
        the same field-path-and-type summary the built-in backends use.
        """
        from codegen.fixtures.backends import FIXTURE_SECRET

        backend = "codegen.fixtures.backends.RaisesValidationError"
        result = runner.invoke(app, [*prefix, "--backend", backend])

        assert result.exit_code == EXIT_INVALID_BACKEND, result.output
        assert FIXTURE_SECRET not in result.output
        assert FIXTURE_SECRET not in result.stderr
        assert backend in _diagnostics(result), result.stderr
        assert "account" in _diagnostics(result), result.stderr

    def test_a_validation_error_from_introspect_is_redacted(self) -> None:
        """
        A ``ValidationError`` raised once the engine is in use is summarized, not quoted.

        It is a ``ValueError``, so the shared table reports it as 1 at the warehouse stage,
        and printed it with ``str()`` -- ``input_value=`` and the password with it. A custom
        engine that connects lazily and validates its settings then is the way to get there.
        """
        from codegen.fixtures.backends import FIXTURE_SECRET

        backend = "codegen.fixtures.backends.ValidatesOnIntrospect"
        result = runner.invoke(app, ["codegen", "analytics.sales", "--backend", backend])

        assert result.exit_code == 1, result.output
        assert FIXTURE_SECRET not in result.output
        assert FIXTURE_SECRET not in result.stderr
        assert "input_value" not in result.stderr
        assert "account" in _diagnostics(result), result.stderr

    def test_exit_for_redacts_a_validation_error_on_either_command(self) -> None:
        """``exit_for`` is the one reporter both commands use, so the redaction lives there."""
        from pydantic import ValidationError

        from codegen.fixtures.backends import FIXTURE_SECRET, RaisesValidationError
        from semolina.cli.codegen import STAGE_WAREHOUSE, exit_for

        with pytest.raises(ValidationError) as raised:
            RaisesValidationError()
        error = raised.value
        buffer = io.StringIO()
        console = Console(file=buffer, width=200, force_terminal=False, highlight=False)
        with patch("semolina.cli.codegen._stderr", console), pytest.raises(typer.Exit) as exited:
            exit_for(error, stage=STAGE_WAREHOUSE)
        assert exited.value.exit_code == 1
        assert FIXTURE_SECRET not in buffer.getvalue()
        assert "account (missing)" in buffer.getvalue(), buffer.getvalue()


class TestTheSharedExitTable:
    """
    Expected failures map to documented exit codes on both commands (GEN-04, A26).

    Each of these escaped ``codegen`` as a raw traceback with exit 1: a backend whose driver
    extra is missing, a ``--backend dotted.path.ClassName`` that cannot be built with no
    arguments, and a driver error ``Engine.introspect`` did not translate.
    """

    @pytest.mark.parametrize("prefix", COMMAND_PREFIXES)
    def test_a_missing_driver_extra_exits_2_naming_the_extra(
        self, prefix: list[str], tmp_path: Path
    ) -> None:
        """
        The ``ImportError`` adbc-poolhouse raises for an absent driver is an option error.

        Raised from ``create_engine`` because that is where it comes from in practice: the
        pool is built, and poolhouse loads the driver while doing it.
        """
        missing = ImportError(
            "ADBC driver 'adbc_driver_duckdb' not found. See: https://docs.adbc-drivers.org/"
        )
        with patch("semolina.config.create_engine", side_effect=missing):
            result = runner.invoke(
                app, [*prefix, "--backend", "duckdb", "--database", str(tmp_path / "x.db")]
            )

        assert result.exit_code == EXIT_INVALID_BACKEND, result.output
        assert "adbc_driver_duckdb" in _diagnostics(result), result.stderr
        assert "semolina[duckdb]" in _diagnostics(result), result.stderr
        assert result.exception is None or isinstance(result.exception, SystemExit)

    @pytest.mark.parametrize("prefix", COMMAND_PREFIXES)
    def test_a_missing_databricks_driver_points_at_dbc_install(self, prefix: list[str]) -> None:
        """
        The Databricks driver is not on PyPI, so the hint names ``dbc install``, not pip.

        The ``databricks`` extra installs only ``databricks-sql-connector``; following a
        ``pip install 'semolina[databricks]'`` hint left the user at the same exit 2.
        """
        missing = ImportError(
            "ADBC driver 'databricks' not found. See: https://docs.adbc-drivers.org/"
        )
        with (
            patch("semolina.config.warehouse_config", return_value=object()),
            patch("semolina.config.create_engine", side_effect=missing),
        ):
            result = runner.invoke(app, [*prefix, "--backend", "databricks"])

        assert result.exit_code == EXIT_INVALID_BACKEND, result.output
        diagnostics = _diagnostics(result)
        assert "dbc install" in diagnostics, result.stderr
        assert "semolina[databricks]" not in diagnostics, result.stderr

    @pytest.mark.parametrize(
        "backend",
        [
            pytest.param("codegen.fixtures.backends.NeedsArguments", id="needs-arguments"),
            pytest.param("codegen.fixtures.backends.RaisesInInit", id="raises-in-init"),
        ],
    )
    @pytest.mark.parametrize("prefix", COMMAND_PREFIXES)
    def test_a_dotted_backend_that_cannot_be_built_exits_2(
        self, prefix: list[str], backend: str
    ) -> None:
        """The class is named, with what its constructor said, and no traceback."""
        result = runner.invoke(app, [*prefix, "--backend", backend])

        assert result.exit_code == EXIT_INVALID_BACKEND, result.output
        assert backend in _diagnostics(result), result.stderr
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_an_untranslated_driver_error_while_introspecting_exits_6(self) -> None:
        """
        A raw ``adbc_driver_manager.Error`` from ``introspect()`` exits 6, as on codegen-dto.

        The built-in engines translate every driver error they raise; a ``--backend
        dotted.path.ClassName`` engine need not. ``codegen-dto`` maps the same exception at
        the same stage -- the warehouse describing what was asked for -- to 6, and one table
        means one answer.
        """
        import adbc_driver_manager

        from semolina.cli.dto_codegen import EXIT_PROBE_FAILED

        error = adbc_driver_manager.ProgrammingError(
            "relation does not exist",
            status_code=adbc_driver_manager.AdbcStatusCode.INVALID_ARGUMENT,
        )
        engine = MagicMock()
        engine.introspect.side_effect = error
        with patch("semolina.cli.codegen._resolve_backend", return_value=engine):
            result = runner.invoke(app, ["codegen", "analytics.sales", "--backend", "custom.E"])

        assert result.exit_code == EXIT_PROBE_FAILED, result.output
        assert "relation does not exist" in _diagnostics(result), result.stderr
        assert "analytics.sales" in _diagnostics(result), result.stderr
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_an_untranslated_driver_error_under_check_exits_6(self, tmp_path: Path) -> None:
        """``--check`` goes through the same table as generation."""
        import adbc_driver_manager

        from semolina.cli.dto_codegen import EXIT_PROBE_FAILED

        model = tmp_path / "models.py"
        model.write_text(
            "from semolina import Metric, SemanticView\n\n\n"
            'class Sales(SemanticView, view="analytics.sales"):\n'
            "    revenue = Metric[int | None]()\n"
        )
        error = adbc_driver_manager.OperationalError(
            "warehouse suspended",
            status_code=adbc_driver_manager.AdbcStatusCode.IO,
        )
        with (
            patch("semolina.cli.codegen._resolve_backend", return_value=MagicMock()),
            patch("semolina.codegen.annotation_check.check_view", side_effect=error),
        ):
            result = runner.invoke(
                app,
                [
                    "codegen",
                    "analytics.sales",
                    "--backend",
                    "custom.E",
                    "--check",
                    "--model",
                    str(model),
                ],
            )

        assert result.exit_code == EXIT_PROBE_FAILED, result.output
        assert "warehouse suspended" in _diagnostics(result), result.stderr
        assert result.exception is None or isinstance(result.exception, SystemExit)

    @pytest.mark.parametrize(
        ("make_error", "stage", "code"),
        [
            pytest.param(lambda: typer.BadParameter("x"), "setup", 2, id="bad-parameter"),
            pytest.param(lambda: ImportError("no driver"), "setup", 2, id="missing-extra"),
            pytest.param(
                lambda: SemolinaConnectionError("refused"), "setup", 4, id="setup-connection"
            ),
            pytest.param(
                lambda: SemolinaViewNotFoundError("gone"), "warehouse", 3, id="view-not-found"
            ),
            pytest.param(
                lambda: SemolinaConnectionError("refused"), "warehouse", 4, id="connection"
            ),
            pytest.param(lambda: RuntimeError("odd"), "warehouse", 1, id="runtime-error"),
            pytest.param(lambda: ValueError("unquotable"), "warehouse", 1, id="value-error"),
            pytest.param(lambda: KeyError("bug"), "warehouse", None, id="unlisted-propagates"),
            # typer's own control flow is not a failure: Exit and Abort subclass RuntimeError,
            # which the warehouse stage maps to 1, so a reordered check would turn Exit(0)
            # into a failing run.
            pytest.param(lambda: typer.Exit(0), "warehouse", None, id="typer-exit"),
            pytest.param(lambda: typer.Exit(0), "setup", None, id="typer-exit-at-setup"),
            pytest.param(lambda: typer.Abort(), "setup", None, id="typer-abort"),
            pytest.param(lambda: typer.Abort(), "warehouse", None, id="typer-abort-warehouse"),
            pytest.param(lambda: KeyError("bug"), "setup", None, id="unlisted-at-setup"),
            pytest.param(lambda: RuntimeError("odd"), "setup", None, id="runtime-at-setup"),
        ],
    )
    def test_the_table(self, make_error: Any, stage: str, code: int | None) -> None:
        """
        One function answers for both commands; anything it does not list propagates.

        A row missing here would be an exception one command reports and the other lets
        out as a traceback, which is the divergence GEN-04 removes. ``None`` is the table's
        "not mine" answer: a bug should still surface as one, not as a tidy exit 1.
        """
        from semolina.cli.codegen import exit_code_for

        assert exit_code_for(make_error(), stage=stage) == code

    @pytest.mark.parametrize("stage", ["setup", "warehouse"])
    def test_the_table_maps_driver_errors_by_stage(self, stage: str) -> None:
        """A driver error is a connection failure at setup and a failed description after."""
        import adbc_driver_manager

        from semolina.cli.codegen import exit_code_for

        error = adbc_driver_manager.InternalError(
            "x", status_code=adbc_driver_manager.AdbcStatusCode.INTERNAL
        )
        assert exit_code_for(error, stage=stage) == (4 if stage == "setup" else 6)


# ---------------------------------------------------------------------------
# TestBackendResolution
# ---------------------------------------------------------------------------


class TestBackendResolution:
    """Backend specifier parsing and validation."""

    def test_invalid_dotted_backend_exits_2(self) -> None:
        """A dotted path that cannot be imported causes exit code 2."""
        result = runner.invoke(app, ["codegen", "s.v", "--backend", "nonexistent.module.Class"])
        assert result.exit_code == 2

    @pytest.mark.parametrize(
        "command",
        [
            ["codegen", "sales_view"],
            ["codegen-dto", "--view", "sales_view", "--metrics", "revenue"],
        ],
        ids=["codegen", "codegen-dto-view"],
    )
    def test_a_dotted_backend_in_the_working_directory_imports(
        self, command: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A custom engine module beside you resolves without being installed, on both commands.

        ``codegen-dto`` appends the working directory to ``sys.path`` before importing a
        ``QUERY_PATH``; ``--backend`` imported without it, so the same module worked after a
        QUERY_PATH and failed with exit 2 under ``codegen`` or ``codegen-dto --view``.
        """
        module = "semolina_cwd_backend_probe"
        (tmp_path / f"{module}.py").write_text(
            "from semolina.exceptions import SemolinaConnectionError\n"
            "\n"
            "class LocalEngine:\n"
            "    def __init__(self):\n"
            "        raise SemolinaConnectionError('local engine was built')\n"
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "path", [p for p in sys.path if p not in ("", str(tmp_path))])
        monkeypatch.delitem(sys.modules, module, raising=False)

        result = runner.invoke(app, [*command, "--backend", f"{module}.LocalEngine"])

        # The engine was imported and built: its constructor's own error is what stops it.
        assert "local engine was built" in result.output, result.output
        assert result.exit_code == 4, result.output

    def test_unknown_simple_backend_exits_2(self) -> None:
        """A simple name with no dot (not snowflake/databricks) causes exit code 2."""
        result = runner.invoke(app, ["codegen", "s.v", "--backend", "mysql"])
        assert result.exit_code == 2

    def test_bad_parameter_via_mock_exits_2(self) -> None:
        """_resolve_backend raising BadParameter produces exit code 2."""
        with patch(
            "semolina.cli.codegen._resolve_backend",
            side_effect=typer.BadParameter("bad backend"),
        ):
            result = runner.invoke(app, ["codegen", "s.v", "--backend", "bad"])
        assert result.exit_code == 2

    def test_malformed_toml_exits_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A malformed .semolina.toml surfaces as a clean error (exit 2), not a raw traceback."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".semolina.toml").write_text("this is = = not valid toml\n")
        result = runner.invoke(app, ["codegen", "s.v", "--backend", "snowflake"])
        assert result.exit_code == 2

    def test_resolve_backend_malformed_toml_raises_bad_parameter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_resolve_backend wraps a TOML parse error in typer.BadParameter."""
        from semolina.cli.codegen import _resolve_backend

        monkeypatch.chdir(tmp_path)
        (tmp_path / ".semolina.toml").write_text("this is = = not valid toml\n")
        with pytest.raises(typer.BadParameter, match="semolina.toml"):
            _resolve_backend("snowflake")


# ---------------------------------------------------------------------------
# TestErrorHandling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Runtime errors from introspection are surfaced cleanly."""

    def test_introspect_runtime_error_exits_1(self) -> None:
        """RuntimeError from engine.introspect() causes exit code 1."""
        mock_engine = MagicMock()
        mock_engine.introspect.side_effect = RuntimeError("Unexpected error")
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app, ["codegen", "bad_schema.missing_view", "--backend", "snowflake"]
            )
        assert result.exit_code == 1

    def test_view_not_found_exits_3(self) -> None:
        """SemolinaViewNotFoundError from engine.introspect() causes exit code 3."""
        mock_engine = MagicMock()
        mock_engine.introspect.side_effect = SemolinaViewNotFoundError("View not found")
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app, ["codegen", "bad_schema.missing_view", "--backend", "snowflake"]
            )
        assert result.exit_code == 3

    def test_connection_error_exits_4(self) -> None:
        """SemolinaConnectionError from engine.introspect() causes exit code 4."""
        mock_engine = MagicMock()
        mock_engine.introspect.side_effect = SemolinaConnectionError("Connection refused")
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(app, ["codegen", "my_schema.my_view", "--backend", "snowflake"])
        assert result.exit_code == 4

    def test_databricks_view_not_found_exits_3(self) -> None:
        """SemolinaViewNotFoundError from a Databricks engine path causes exit code 3."""
        mock_engine = MagicMock()
        mock_engine.introspect.side_effect = SemolinaViewNotFoundError(
            "Databricks view not found or inaccessible: <DatabaseError>"
        )
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app, ["codegen", "main.analytics.missing_view", "--backend", "databricks"]
            )
        assert result.exit_code == 3

    def test_databricks_connection_error_exits_4(self) -> None:
        """SemolinaConnectionError from a Databricks engine path causes exit code 4."""
        mock_engine = MagicMock()
        mock_engine.introspect.side_effect = SemolinaConnectionError(
            "Databricks connection failed: <OperationalError>"
        )
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app, ["codegen", "main.analytics.my_view", "--backend", "databricks"]
            )
        assert result.exit_code == 4


# ---------------------------------------------------------------------------
# TestDuckDBBackend
# ---------------------------------------------------------------------------


class TestDuckDBBackend:
    """DuckDB backend resolution and --database option."""

    def test_duckdb_backend_with_database_option(self) -> None:
        """--backend duckdb --database test.db resolves DuckDBEngine."""
        mock_engine = make_mock_engine([SALES_VIEW])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app,
                [
                    "codegen",
                    "my_schema.my_sales_view",
                    "--backend",
                    "duckdb",
                    "--database",
                    "test.db",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "class MySalesView(SemanticView" in result.output

    def test_duckdb_backend_database_env_var(self) -> None:
        """--backend duckdb uses DUCKDB_DATABASE env var when --database not given."""
        mock_engine = make_mock_engine([SALES_VIEW])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app,
                ["codegen", "my_schema.my_sales_view", "--backend", "duckdb"],
                env={"DUCKDB_DATABASE": "/tmp/test.db"},
            )
        assert result.exit_code == 0, result.output

    def test_duckdb_backend_no_database_exits_error(self) -> None:
        """--backend duckdb without --database or env var exits with error."""
        result = runner.invoke(
            app,
            ["codegen", "my_schema.my_sales_view", "--backend", "duckdb"],
        )
        assert result.exit_code == EXIT_INVALID_BACKEND

    def test_duckdb_resolve_creates_engine_with_database(self) -> None:
        """_resolve_backend('duckdb', database='test.db') builds via create_engine."""
        with patch("semolina.config.create_engine") as mock_create_engine:
            mock_create_engine.return_value = MagicMock()
            from pathlib import Path

            from semolina.cli.codegen import _resolve_backend

            _resolve_backend("duckdb", database="test.db")
            expected = str(Path("test.db").expanduser().resolve(strict=False))
            mock_create_engine.assert_called_once()
            (config,) = mock_create_engine.call_args[0]
            assert config.database == expected

    def test_duckdb_view_not_found_exits_3(self) -> None:
        """SemolinaViewNotFoundError from DuckDB introspect exits 3."""
        mock_engine = MagicMock()
        mock_engine.introspect.side_effect = SemolinaViewNotFoundError("View not found")
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app,
                ["codegen", "missing_view", "--backend", "duckdb", "--database", "test.db"],
            )
        assert result.exit_code == EXIT_VIEW_NOT_FOUND

    def test_an_invalid_view_name_is_an_error_message_not_a_traceback(self) -> None:
        """quote_table_name's ValueError on a malformed name reaches the user as an error line."""
        mock_engine = MagicMock()
        mock_engine.introspect.side_effect = ValueError("View name '\"x' has an unbalanced quote")
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app,
                ["codegen", '"x', "--backend", "duckdb", "--database", "test.db"],
            )
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert "unbalanced quote" in result.output

    def test_duckdb_connection_error_exits_4(self) -> None:
        """SemolinaConnectionError from DuckDB exits 4."""
        mock_engine = MagicMock()
        mock_engine.introspect.side_effect = SemolinaConnectionError("Cannot open file")
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app,
                ["codegen", "orders", "--backend", "duckdb", "--database", "bad.db"],
            )
        assert result.exit_code == EXIT_CONNECTION_ERROR


# ---------------------------------------------------------------------------
# TestPathNormalization
# ---------------------------------------------------------------------------


class TestPathNormalization:
    """
    Tests for the ``_normalize_database_path`` helper in ``cli/codegen.py``.

    Pinned by CONTEXT.md decision D-LockedPathHandling: ``:memory:`` MUST pass
    through unchanged; empty strings MUST pass through unchanged (so DuckDB
    raises the consistent error); non-sentinel values MUST be expanded with
    ``expanduser`` and resolved with ``strict=False``. The same treatment
    applies whether the value arrives via ``--database`` or ``DUCKDB_DATABASE``.
    """

    def test_memory_sentinel_preserved(self) -> None:
        """``:memory:`` sentinel must pass through unchanged (CONTEXT.md guard)."""
        from semolina.cli.codegen import _normalize_database_path

        assert _normalize_database_path(":memory:") == ":memory:"

    def test_empty_string_passthrough(self) -> None:
        """
        Empty strings must pass through unchanged.

        The CONTEXT.md guard ``if database and database != ":memory:":``
        short-circuits on falsy input. Without this, ``Path("").resolve()``
        silently expands to the current working directory — masking a bug
        and producing an inconsistent error path. Leaving ``""`` unchanged
        lets DuckDB raise the same error it would for any invalid path.
        """
        from semolina.cli.codegen import _normalize_database_path

        assert _normalize_database_path("") == ""

    def test_tilde_expanded(self) -> None:
        """Leading ``~`` must expand to the user home directory."""
        from pathlib import Path

        from semolina.cli.codegen import _normalize_database_path

        result = _normalize_database_path("~/sales.duckdb")
        assert "~" not in result
        assert result.startswith(str(Path.home()))

    def test_relative_resolved_to_absolute(self) -> None:
        """Relative paths must resolve to absolute paths (non-strict)."""
        from pathlib import Path

        from semolina.cli.codegen import _normalize_database_path

        result = _normalize_database_path("./does_not_exist/sales.duckdb")
        assert Path(result).is_absolute()
        # strict=False: should NOT raise FileNotFoundError when target is missing

    def test_envvar_path_normalized(self) -> None:
        """
        Paths supplied via ``DUCKDB_DATABASE`` env-var must also be normalized.

        CONTEXT.md (Path Handling, locked): "Apply ``expanduser()`` +
        ``resolve(strict=False)`` to the ``--database`` value AND to the value
        of the ``DUCKDB_DATABASE`` env-var fallback. Same treatment for both
        sources." This test invokes the CLI with the env-var set and no
        ``--database`` flag, then asserts the expanded path is what reached
        the engine layer.
        """
        from pathlib import Path

        captured: dict[str, str] = {}

        def _fake_create_engine(config: object) -> MagicMock:
            captured["database"] = config.database  # type: ignore[attr-defined]
            engine = MagicMock()
            engine.introspect.side_effect = SystemExit(0)  # short-circuit codegen
            return engine

        # Patch the factory invoked inside _resolve_backend's duckdb branch.
        with patch("semolina.config.create_engine", _fake_create_engine):
            runner.invoke(
                app,
                ["codegen", "sales_view", "--backend", "duckdb"],
                env={"DUCKDB_DATABASE": "~/foo.duckdb"},
            )

        assert "database" in captured, (
            "create_engine was never called; env-var path did not reach _resolve_backend"
        )
        assert "~" not in captured["database"], (
            f"DUCKDB_DATABASE was not normalized: {captured['database']!r}"
        )
        assert captured["database"].startswith(str(Path.home())), (
            f"Expected expanded home path, got {captured['database']!r}"
        )


class TestRuffNotInstalledHint:
    """When ruff is absent, codegen still emits source plus a stderr hint."""

    def test_hint_printed_when_ruff_missing(self) -> None:
        """Source still goes to stdout; the codegen-lint hint goes to the stderr console."""
        mock_engine = make_mock_engine([SALES_VIEW])
        with (
            patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine),
            patch("semolina.codegen.python_renderer.ruff_available", return_value=False),
            patch("semolina.cli.codegen._stderr") as mock_stderr,
        ):
            result = runner.invoke(
                app, ["codegen", "my_schema.my_sales_view", "--backend", "snowflake"]
            )
        assert result.exit_code == 0, result.output
        # Generated source still reaches stdout, unformatted.
        assert "class MySalesView(SemanticView" in result.output
        # The hint goes to the stderr console — never to stdout, so piping stays clean.
        assert "codegen-lint" not in result.output
        mock_stderr.print.assert_called_once()
        hint = mock_stderr.print.call_args[0][0]
        assert "codegen-lint" in hint

    def test_no_hint_when_ruff_available(self) -> None:
        """When ruff is installed, codegen prints no hint."""
        mock_engine = make_mock_engine([SALES_VIEW])
        with (
            patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine),
            patch("semolina.codegen.python_renderer.ruff_available", return_value=True),
            patch("semolina.cli.codegen._stderr") as mock_stderr,
        ):
            result = runner.invoke(
                app, ["codegen", "my_schema.my_sales_view", "--backend", "snowflake"]
            )
        assert result.exit_code == 0, result.output
        assert "class MySalesView(SemanticView" in result.output
        mock_stderr.print.assert_not_called()


# ---------------------------------------------------------------------------
# TestAnnotationCheck
# ---------------------------------------------------------------------------


def _generate_model(db_path: Path, target: Path) -> str:
    """
    Generate a model for ``sales_view`` through the real CLI and write it to ``target``.

    Generating the fixture rather than hand-writing it proves the round trip and keeps the
    fixture from going stale the next time the renderer changes.

    Args:
        db_path: The file-backed DuckDB database.
        target: Where to write the generated source.

    Returns:
        The generated source.
    """
    result = runner.invoke(
        app, ["codegen", "sales_view", "--backend", "duckdb", "--database", str(db_path)]
    )
    assert result.exit_code == 0, result.output
    target.write_text(result.stdout, encoding="utf-8")
    return result.stdout


def _ruff_format_check(path: Path) -> subprocess.CompletedProcess[str]:
    """
    Run ``ruff format --check --isolated`` on a file, as a CI job would.

    ``--isolated`` because the generated module is formatted that way (GEN-07): with this
    repository's own ``pyproject.toml`` in play, the check would measure the repo's ruff
    settings rather than the generated file.

    Args:
        path: The file to check.

    Returns:
        The completed process; ``returncode`` 0 means ruff would change nothing.
    """
    return subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", "--isolated", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


class TestCodegenWritesToAFile:
    """
    ``--output`` on ``codegen``: the module ``> models.py`` produces, written safely.

    The shell redirect truncates ``models.py`` before codegen has done anything, so a run
    that fails leaves an empty file where a committed model was (A29). ``--output`` writes
    only after every view rendered, and atomically, so a failed run leaves it alone.
    """

    def test_stdout_passes_ruff_format_check(self, tmp_path: Path) -> None:
        """
        ``> models.py`` yields a file ``ruff format --check`` accepts (GEN-05, A27).

        ``typer.echo`` appended a newline to source that already ended in one, so the
        documented workflow produced a file the formatter wanted to change.
        """
        mock_engine = make_mock_engine([SALES_VIEW])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app, ["codegen", "my_schema.my_sales_view", "--backend", "snowflake"]
            )
        assert result.exit_code == 0, result.output
        redirected = tmp_path / "models.py"
        redirected.write_text(result.stdout, encoding="utf-8")
        check = _ruff_format_check(redirected)
        assert check.returncode == 0, check.stdout + check.stderr

    def test_output_writes_the_module_and_nothing_to_stdout(self, tmp_path: Path) -> None:
        """The file holds the module; stdout is empty; stderr says where it went."""
        target = tmp_path / "models.py"
        mock_engine = make_mock_engine([SALES_VIEW])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app,
                [
                    "codegen",
                    "my_schema.my_sales_view",
                    "--backend",
                    "snowflake",
                    "--output",
                    str(target),
                ],
            )
        assert result.exit_code == 0, result.output
        assert result.stdout == "", result.stdout
        assert "class MySalesView(SemanticView" in target.read_text()
        assert "Wrote:" in _diagnostics(result), result.stderr

    def test_stdout_and_output_are_byte_identical_and_ruff_clean(self, tmp_path: Path) -> None:
        """
        The two documented ways to land the module produce the same bytes (GEN-05).

        Bytes, not text: a trailing-newline difference is exactly what this pins, and a
        ``read_text`` comparison would hide a line-ending one as well.
        """
        target = tmp_path / "models.py"
        mock_engine = make_mock_engine([SALES_VIEW, SECOND_VIEW])
        views = ["my_schema.my_sales_view", "my_schema.customers"]
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            to_stdout = runner.invoke(app, ["codegen", *views, "--backend", "snowflake"])
            to_file = runner.invoke(
                app, ["codegen", *views, "--backend", "snowflake", "--output", str(target)]
            )
        assert to_stdout.exit_code == 0, to_stdout.output
        assert to_file.exit_code == 0, to_file.output
        assert target.read_bytes() == to_stdout.stdout_bytes
        check = _ruff_format_check(target)
        assert check.returncode == 0, check.stdout + check.stderr

    def test_a_failure_on_a_later_view_leaves_the_file_untouched(self, tmp_path: Path) -> None:
        """
        Nothing is written until every view has been introspected and rendered.

        The second view fails, after the first succeeded -- the point at which an
        implementation writing per view would already have replaced the committed file.
        """
        target = tmp_path / "models.py"
        target.write_text("# committed\n", encoding="utf-8")
        engine = MagicMock()
        engine.introspect.side_effect = [SALES_VIEW, SemolinaViewNotFoundError("customers")]
        with patch("semolina.cli.codegen._resolve_backend", return_value=engine):
            result = runner.invoke(
                app,
                [
                    "codegen",
                    "my_schema.my_sales_view",
                    "my_schema.customers",
                    "--backend",
                    "snowflake",
                    "--output",
                    str(target),
                ],
            )
        assert result.exit_code == EXIT_VIEW_NOT_FOUND, result.output
        assert target.read_text() == "# committed\n"

    def test_the_write_is_atomic(self, tmp_path: Path) -> None:
        """
        The module is written beside the target and renamed over it, never written in place.

        Driven by failing the rename. An in-place write would already have replaced the
        committed file by then; an atomic one leaves it, and leaves no temporary file behind.
        """
        target = tmp_path / "models.py"
        target.write_text("# committed\n", encoding="utf-8")
        mock_engine = make_mock_engine([SALES_VIEW])
        with (
            patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine),
            patch("os.replace", side_effect=OSError("disk full")),
        ):
            result = runner.invoke(
                app,
                [
                    "codegen",
                    "my_schema.my_sales_view",
                    "--backend",
                    "snowflake",
                    "--output",
                    str(target),
                ],
            )
        assert result.exit_code == 1, result.output
        assert target.read_text() == "# committed\n"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["models.py"]
        assert "disk full" in _diagnostics(result), result.stderr

    def test_an_unwritable_directory_falls_back_to_writing_in_place(self, tmp_path: Path) -> None:
        """
        A writable file in a directory that refuses new entries is still written.

        A locked checkout, or a single file bind-mounted into a container, lets you write
        ``models.py`` but not create the temporary file beside it. Simulated by refusing
        exclusive creation, since the suite runs as root and root ignores directory modes.
        """
        import pathlib

        target = tmp_path / "models.py"
        target.write_text("# committed\n", encoding="utf-8")
        real_open = pathlib.Path.open

        def refuse_new_entries(self: pathlib.Path, mode: str = "r", *args: Any, **kw: Any) -> Any:
            if "x" in mode:
                raise PermissionError(13, "Permission denied", str(self))
            return real_open(self, mode, *args, **kw)

        mock_engine = make_mock_engine([SALES_VIEW])
        with (
            patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine),
            patch.object(pathlib.Path, "open", refuse_new_entries),
        ):
            result = runner.invoke(
                app,
                [
                    "codegen",
                    "my_schema.my_sales_view",
                    "--backend",
                    "snowflake",
                    "--output",
                    str(target),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "class MySalesView(SemanticView" in target.read_text()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["models.py"]

    def test_an_unwritable_directory_and_no_file_still_exits_1(self, tmp_path: Path) -> None:
        """With no file to write in place, the refusal is reported rather than worked around."""
        import pathlib

        target = tmp_path / "models.py"
        real_open = pathlib.Path.open

        def refuse_new_entries(self: pathlib.Path, mode: str = "r", *args: Any, **kw: Any) -> Any:
            if "x" in mode:
                raise PermissionError(13, "Permission denied", str(self))
            return real_open(self, mode, *args, **kw)

        mock_engine = make_mock_engine([SALES_VIEW])
        with (
            patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine),
            patch.object(pathlib.Path, "open", refuse_new_entries),
        ):
            result = runner.invoke(
                app,
                [
                    "codegen",
                    "my_schema.my_sales_view",
                    "--backend",
                    "snowflake",
                    "--output",
                    str(target),
                ],
            )
        assert result.exit_code == 1, result.output
        assert not target.exists()
        assert "Permissiondenied" in "".join(result.stderr.split()), result.stderr

    def test_a_failed_in_place_write_exits_1(self, tmp_path: Path) -> None:
        """
        The in-place fallback failing too is a message and exit 1, not a traceback.

        The temporary file is refused for permission, so the write falls back to the target
        itself; that open then fails as a full disk would.
        """
        import pathlib

        target = tmp_path / "models.py"
        target.write_text("# committed\n", encoding="utf-8")
        real_open = pathlib.Path.open

        def refuse_every_write(self: pathlib.Path, mode: str = "r", *args: Any, **kw: Any) -> Any:
            if "x" in mode:
                raise PermissionError(13, "Permission denied", str(self))
            if "w" in mode:
                raise OSError(28, "No space left on device", str(self))
            return real_open(self, mode, *args, **kw)

        mock_engine = make_mock_engine([SALES_VIEW])
        with (
            patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine),
            patch.object(pathlib.Path, "open", refuse_every_write),
        ):
            result = runner.invoke(
                app,
                [
                    "codegen",
                    "my_schema.my_sales_view",
                    "--backend",
                    "snowflake",
                    "--output",
                    str(target),
                ],
            )
        assert result.exit_code == 1, result.output
        assert not isinstance(result.exception, OSError), result.exception
        assert "could not write" in _diagnostics(result), result.stderr
        assert "No space left on device" in _diagnostics(result), result.stderr
        assert target.read_text() == "# committed\n"

    @pytest.mark.parametrize(
        ("errno_", "message"),
        [
            (30, "Read-only file system"),
            (36, "File name too long"),
            (2, "No such file or directory"),
        ],
        ids=["read-only-mount", "name-too-long", "dangling-symlink"],
    )
    def test_any_other_failure_to_create_the_temporary_file_exits_1(
        self, tmp_path: Path, errno_: int, message: str
    ) -> None:
        """
        A temporary file refused for any reason but permission is exit 1, not a traceback.

        A read-only mount, a target name too long to take the temporary suffix, or a
        symlink into a missing directory all fail when the temporary file is created.
        Only a permission refusal falls back to writing in place: after, say, a full disk,
        an in-place write would truncate the committed file. So the file is left alone.
        """
        import pathlib

        target = tmp_path / "models.py"
        target.write_text("# committed\n", encoding="utf-8")
        real_open = pathlib.Path.open

        def refuse_new_entries(self: pathlib.Path, mode: str = "r", *args: Any, **kw: Any) -> Any:
            if "x" in mode:
                raise OSError(errno_, message, str(self))
            return real_open(self, mode, *args, **kw)

        mock_engine = make_mock_engine([SALES_VIEW])
        with (
            patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine),
            patch.object(pathlib.Path, "open", refuse_new_entries),
        ):
            result = runner.invoke(
                app,
                [
                    "codegen",
                    "my_schema.my_sales_view",
                    "--backend",
                    "snowflake",
                    "--output",
                    str(target),
                ],
            )
        assert result.exit_code == 1, result.output
        assert not isinstance(result.exception, OSError), result.exception
        assert target.read_text() == "# committed\n"
        assert message in _diagnostics(result), result.stderr

    @pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc/self/fd")
    def test_a_file_descriptor_path_is_written_in_place(self, tmp_path: Path) -> None:
        """
        ``-o /dev/stdout`` with stdout redirected to a file writes that file, not a new one.

        There ``/dev/stdout`` is a regular file, reached through ``/proc/self/fd/1``.
        Renaming over it would swap the inode under the shell's redirect, and anything
        written to stdout afterwards would go to the old, unlinked file. A path under
        ``/dev`` or ``/proc`` names an open stream, so it is written in place.
        """
        import os

        target = tmp_path / "out.py"
        target.write_text("", encoding="utf-8")
        fd = os.open(target, os.O_WRONLY)
        try:
            inode = os.fstat(fd).st_ino
            mock_engine = make_mock_engine([SALES_VIEW])
            with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
                result = runner.invoke(
                    app,
                    [
                        "codegen",
                        "my_schema.my_sales_view",
                        "--backend",
                        "snowflake",
                        "--output",
                        f"/proc/self/fd/{fd}",
                    ],
                )
        finally:
            os.close(fd)
        assert result.exit_code == 0, result.output
        assert target.stat().st_ino == inode
        assert "class MySalesView(SemanticView" in target.read_text()

    @pytest.mark.parametrize(
        ("path", "is_stream"),
        [
            pytest.param("/dev/stdout", True, id="dev-stdout"),
            pytest.param("/dev/stderr", True, id="dev-stderr"),
            pytest.param("/dev/fd/3", True, id="dev-fd"),
            pytest.param("/proc/self/fd/1", True, id="proc-self-fd"),
            pytest.param("/proc/1234/fd/1", True, id="proc-pid-fd"),
            pytest.param("/dev/shm/models.py", False, id="dev-shm"),
            pytest.param("/proc/sys/models.py", False, id="proc-other"),
        ],
    )
    def test_only_stream_paths_under_dev_and_proc_are_written_in_place(
        self, path: str, *, is_stream: bool
    ) -> None:
        """
        ``/dev/shm/models.py`` is an ordinary file and keeps the atomic route.

        Only the descriptor paths stand for an open stream; any other file under ``/dev``
        written in place could be truncated by a failed write.
        """
        from pathlib import PurePosixPath

        from semolina.cli.codegen import _names_an_open_stream

        assert _names_an_open_stream(PurePosixPath(path)) is is_stream

    def test_a_windows_dev_checkout_is_not_a_stream_path(self) -> None:
        """A ``C:/dev/app/models.py`` checkout is a common layout, not ``/dev``."""
        from pathlib import PureWindowsPath

        from semolina.cli.codegen import _names_an_open_stream

        assert not _names_an_open_stream(PureWindowsPath("C:/dev/app/models.py"))

    def test_a_symlinked_output_stays_a_symlink(self, tmp_path: Path) -> None:
        """
        ``--output link.py`` writes through the link, to the file it points at.

        Renaming a temporary file over the link itself replaces the link with a regular file,
        silently detaching it from the file it was there to point at -- a module kept in a
        shared location and linked into several packages, say.
        """
        real = tmp_path / "shared" / "models.py"
        real.parent.mkdir()
        real.write_text("# committed\n", encoding="utf-8")
        link = tmp_path / "models.py"
        link.symlink_to(real)
        mock_engine = make_mock_engine([SALES_VIEW])
        with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
            result = runner.invoke(
                app,
                [
                    "codegen",
                    "my_schema.my_sales_view",
                    "--backend",
                    "snowflake",
                    "--output",
                    str(link),
                ],
            )
        assert result.exit_code == 0, result.output
        assert link.is_symlink()
        assert "class MySalesView(SemanticView" in real.read_text()
        assert sorted(p.name for p in real.parent.iterdir()) == ["models.py"]
        assert sorted(p.name for p in tmp_path.iterdir()) == ["models.py", "shared"]

    def test_a_target_that_is_not_a_regular_file_is_written_in_place(self, tmp_path: Path) -> None:
        """
        A FIFO -- like ``-o /dev/stdout`` -- receives the module, and is not replaced.

        Renaming a temporary file over a device or a pipe is not a write to it: for
        ``/dev/stdout`` the temporary file cannot even be created beside it. Anything that
        exists and is not a regular file is written in place instead.

        Nothing here may block on an ``open()``. On Linux, opening one end of a FIFO blocks
        until the other end is opened, and this test used to unblock a reader thread with a
        blocking ``open("wb")`` guarded by ``reader.is_alive()``. If the reader read EOF and
        closed between that check and the open, the open waited for a reader that would
        never come, and hung the xdist worker (seen on the 3.11 CI job). So the read end is
        opened non-blocking before the command runs, which lets the command's own open for
        writing return at once, and a thread drains it with ``select`` timeouts until told
        to stop, so a module larger than the pipe buffer cannot wedge the write either.
        """
        import os
        import select
        import threading

        fifo = tmp_path / "out.fifo"
        os.mkfifo(fifo)
        received: list[bytes] = []
        stop = threading.Event()
        read_end = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)

        def drain_available() -> None:
            # Non-blocking: no writer yet, or no data yet, ends the pass rather than waiting.
            while True:
                try:
                    chunk = os.read(read_end, 65536)
                except BlockingIOError:
                    return
                if not chunk:
                    return
                received.append(chunk)

        def drain() -> None:
            while not stop.is_set():
                ready, _, _ = select.select([read_end], [], [], 0.05)
                if ready:
                    drain_available()
                    # A closed writer leaves the FIFO readable (at EOF) for good; pace the loop.
                    stop.wait(0.01)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        mock_engine = make_mock_engine([SALES_VIEW])
        try:
            with patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine):
                result = runner.invoke(
                    app,
                    [
                        "codegen",
                        "my_schema.my_sales_view",
                        "--backend",
                        "snowflake",
                        "--output",
                        str(fifo),
                    ],
                )
        finally:
            stop.set()
            reader.join(timeout=10)
            drain_available()
            os.close(read_end)
        assert not reader.is_alive()
        assert result.exit_code == 0, result.output
        assert fifo.is_fifo()
        assert b"class MySalesView(SemanticView" in b"".join(received)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["out.fifo"]

    def test_an_interrupted_write_leaves_no_temporary_file(self, tmp_path: Path) -> None:
        """
        Ctrl-C between writing the temporary file and renaming it cleans the file up.

        ``KeyboardInterrupt`` is not an ``OSError``, so a cleanup that only ran for the
        latter left a ``.models.py.<hex>.tmp`` beside the target on every interrupt.
        """
        target = tmp_path / "models.py"
        target.write_text("# committed\n", encoding="utf-8")
        mock_engine = make_mock_engine([SALES_VIEW])
        with (
            patch("semolina.cli.codegen._resolve_backend", return_value=mock_engine),
            patch("os.replace", side_effect=KeyboardInterrupt),
        ):
            result = runner.invoke(
                app,
                [
                    "codegen",
                    "my_schema.my_sales_view",
                    "--backend",
                    "snowflake",
                    "--output",
                    str(target),
                ],
            )
        assert result.exit_code != 0, result.output
        assert target.read_text() == "# committed\n"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["models.py"]

    def test_a_directory_that_does_not_exist_exits_2_before_connecting(
        self, tmp_path: Path
    ) -> None:
        """A mistyped destination is caught before the backend is resolved."""
        with patch("semolina.cli.codegen._resolve_backend") as resolve:
            result = runner.invoke(
                app,
                [
                    "codegen",
                    "my_schema.my_sales_view",
                    "--backend",
                    "snowflake",
                    "--output",
                    str(tmp_path / "nope" / "models.py"),
                ],
            )
        assert result.exit_code == EXIT_INVALID_BACKEND, result.output
        assert "does not exist" in _diagnostics(result), result.stderr
        resolve.assert_not_called()

    def test_an_output_that_is_a_directory_exits_2_before_connecting(self, tmp_path: Path) -> None:
        """
        ``--output`` naming an existing directory is an option error, not a write failure.

        Reported before the backend is resolved, like the missing-parent case above, so the
        mistake costs no warehouse round trip. The directory is left as it was.
        """
        with patch("semolina.cli.codegen._resolve_backend") as resolve:
            result = runner.invoke(
                app,
                [
                    "codegen",
                    "my_schema.my_sales_view",
                    "--backend",
                    "snowflake",
                    "--output",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == EXIT_INVALID_BACKEND, result.output
        assert "is a directory" in _diagnostics(result), result.stderr
        assert "Traceback" not in result.stderr
        assert list(tmp_path.iterdir()) == []
        resolve.assert_not_called()

    def test_check_reads_the_file_output_names(
        self, duckdb_file_backed_db: Path, tmp_path: Path
    ) -> None:
        """
        ``--check --output PATH`` checks the file ``--output PATH`` writes (GEN-06).

        The same pairing ``codegen-dto`` has: one option names the file both ways, so CI
        checks exactly the file generation would write. ``--model`` is no longer needed.
        """
        model = tmp_path / "models.py"
        _generate_model(duckdb_file_backed_db, model)

        result = runner.invoke(
            app,
            [
                "codegen",
                "sales_view",
                "--check",
                "--output",
                str(model),
                "--backend",
                "duckdb",
                "--database",
                str(duckdb_file_backed_db),
            ],
        )

        assert result.exit_code == 0, result.stderr
        assert result.stdout == ""
        assert ROUTE_EXECUTE_SCHEMA in result.stderr

    def test_check_with_both_model_and_output_exits_2(self, tmp_path: Path) -> None:
        """Two options naming the file to check is one too many; neither silently wins."""
        result = runner.invoke(
            app,
            [
                "codegen",
                "sales_view",
                "--check",
                "--output",
                str(tmp_path / "a.py"),
                "--model",
                str(tmp_path / "b.py"),
                "--backend",
                "duckdb",
                "--database",
                "x.db",
            ],
        )
        assert result.exit_code == EXIT_INVALID_BACKEND, result.output
        assert "--model" in _diagnostics(result), result.stderr


class TestAnnotationCheckOptionValidation:
    """``--check`` and ``--model`` are only meaningful together."""

    def test_check_without_model_exits_invalid_backend(self) -> None:
        result = runner.invoke(
            app, ["codegen", "sales_view", "--check", "--backend", "duckdb", "--database", "x.db"]
        )

        assert result.exit_code == EXIT_INVALID_BACKEND, result.output
        assert "--model" in result.stderr

    def test_model_without_check_exits_invalid_backend(self) -> None:
        result = runner.invoke(
            app,
            [
                "codegen",
                "sales_view",
                "--model",
                "models.py",
                "--backend",
                "duckdb",
                "--database",
                "x.db",
            ],
        )

        assert result.exit_code == EXIT_INVALID_BACKEND, result.output
        assert "--model" in result.stderr

    @pytest.mark.parametrize("flag", ["--model", "--output"])
    def test_a_missing_committed_file_exits_2_before_connecting(
        self, flag: str, tmp_path: Path
    ) -> None:
        """
        A ``--check`` with no file to read is an option error, as it is on ``codegen-dto``.

        It exited 1 here, from ``read_committed_model``, while the sibling exited 2 for the
        same mistake. Refused before the backend is resolved, so the database need not exist.
        """
        missing = tmp_path / "nope.py"

        with patch("semolina.cli.codegen._resolve_backend") as resolve:
            result = runner.invoke(
                app,
                ["codegen", "sales_view", "--check", flag, str(missing), "--backend", "duckdb"],
            )

        assert result.exit_code == EXIT_INVALID_BACKEND, result.output
        assert "nope.py" in _diagnostics(result), result.stderr
        assert "Traceback" not in result.stderr
        resolve.assert_not_called()

    def test_an_unparseable_committed_file_exits_1(self, tmp_path: Path) -> None:
        """
        A hand-edited model file that does not parse is a message and exit 1, not a traceback.

        Mirrors ``codegen-dto``'s twin: nothing was compared, so 5 (drift) would report a
        verdict never reached. The check fails before the engine is used, so a mock stands in.
        """
        model = tmp_path / "models.py"
        model.write_text(
            "from semolina import Metric, SemanticView\n\n\n"
            'class Sales(SemanticView, view="sales_view"\n'
            "    revenue = Metric[int | None]()\n"
        )
        engine = MagicMock()

        with patch("semolina.cli.codegen._resolve_backend", return_value=engine):
            result = runner.invoke(
                app,
                ["codegen", "sales_view", "--backend", "duckdb", "--check", "--model", str(model)],
            )

        assert result.exit_code == 1, result.output
        assert "Cannot parse model file" in _diagnostics(result), result.stderr
        # Rich may wrap inside the path, so the file is looked for with whitespace removed.
        assert str(model) in "".join(result.stderr.split()), result.stderr
        assert "Traceback" not in result.stderr
        assert result.exception is None or isinstance(result.exception, SystemExit)
        engine.introspect.assert_not_called()

    def test_an_unreadable_committed_file_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A model file that exists but cannot be read is the same message-and-exit-1.

        ``chmod 000`` does not stop root, so the read itself is made to fail with the
        ``PermissionError`` a real unreadable file raises, for that one path only.
        """
        import pathlib

        model = tmp_path / "models.py"
        model.write_text("from semolina import SemanticView\n")
        real_read_text = pathlib.Path.read_text

        def read_text(self: pathlib.Path, *args: Any, **kwargs: Any) -> str:
            if self == model:
                raise PermissionError(13, "Permission denied", str(self))
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "read_text", read_text)
        engine = MagicMock()

        with patch("semolina.cli.codegen._resolve_backend", return_value=engine):
            result = runner.invoke(
                app,
                ["codegen", "sales_view", "--backend", "duckdb", "--check", "--model", str(model)],
            )

        assert result.exit_code == 1, result.output
        assert "Cannot read model file" in _diagnostics(result), result.stderr
        assert "Permission denied" in _diagnostics(result), result.stderr
        assert "Traceback" not in result.stderr
        assert result.exception is None or isinstance(result.exception, SystemExit)
        engine.introspect.assert_not_called()

    def test_help_documents_the_drift_exit_code(self) -> None:
        result = runner.invoke(app, ["codegen", "--help"])

        assert result.exit_code == 0
        drift_lines = [line for line in result.output.splitlines() if "drift" in line]
        assert drift_lines, result.output
        assert any("5" in line for line in drift_lines)


def _render_to_text(report: ViewCheckReport, *, width: int = 200) -> str:
    """
    Render one report through the real ``_render_check_report`` and capture the text.

    The module-level ``_stderr`` console is swapped for a wide, file-backed one so the
    assertions below are about markup handling rather than about where rich chose to wrap.

    Args:
        report: The report to render.
        width: Console width; wide enough that no cell in these fixtures wraps.

    Returns:
        Everything the renderer wrote.
    """
    buffer = io.StringIO()
    console = Console(file=buffer, width=width, force_terminal=False, highlight=False)
    with patch("semolina.cli.codegen._stderr", console):
        _render_check_report(report)
    return buffer.getvalue()


class TestCheckReportPrintsViewDetail:
    """Drift in the view's own metadata has no row, so it is printed under the table."""

    def test_the_view_detail_is_printed(self) -> None:
        report = ViewCheckReport(
            view_name="v",
            rows=[],
            has_drift=True,
            detail="description: committed 'old', warehouse 'new'",
        )

        output = _render_to_text(report)

        assert "(view): description: committed 'old', warehouse 'new'" in output

    def test_no_view_detail_prints_nothing_for_the_view(self) -> None:
        report = ViewCheckReport(view_name="v", rows=[], has_drift=False)

        assert "(view)" not in _render_to_text(report)


class TestCheckReportIsNotParsedAsMarkup:
    """
    The report's cells are data, not style directives.

    The drift report carries warehouse- and model-supplied strings, and rich renders
    ``str`` cells with ``markup=True``. A report whose whole job is to show a difference
    must not have the difference eaten by a style tag, and a catalogue field name must not
    be able to crash the CLI out of its own exit codes.
    """

    def test_a_subscripted_annotation_survives_the_table(self) -> None:
        """``list[str]`` and ``list[int]`` must not both print as ``list``."""
        report = ViewCheckReport(
            view_name="v",
            rows=[
                FieldCheckRow(
                    name="payload",
                    committed="list[str] | None",
                    probed="list[int] | None",
                    route=ROUTE_EXECUTE_SCHEMA,
                    status=STATUS_DRIFT,
                )
            ],
            has_drift=True,
        )

        output = _render_to_text(report)

        assert "list[str] | None" in output
        assert "list[int] | None" in output

    def test_a_closing_tag_shaped_field_name_does_not_crash(self) -> None:
        """A quoted identifier from the catalogue is data, not a style directive."""
        report = ViewCheckReport(
            view_name="v",
            rows=[
                FieldCheckRow(
                    name="[/red]",
                    committed="str",
                    probed=ABSENT,
                    route=ROUTE_EXECUTE_SCHEMA,
                    status=STATUS_DRIFT,
                )
            ],
            has_drift=True,
        )

        output = _render_to_text(report)

        assert "[/red]" in output

    def test_the_status_cell_is_still_styled(self) -> None:
        """Bypassing markup must not cost the red/green the status column carries."""
        report = ViewCheckReport(
            view_name="v",
            rows=[
                FieldCheckRow(
                    name="ok",
                    committed="str",
                    probed="str",
                    route=ROUTE_EXECUTE_SCHEMA,
                    status=STATUS_MATCH,
                ),
                FieldCheckRow(
                    name="bad",
                    committed="str",
                    probed="int",
                    route=ROUTE_EXECUTE_SCHEMA,
                    status=STATUS_DRIFT,
                ),
            ],
            has_drift=True,
        )

        buffer = io.StringIO()
        # ``tests/conftest.py`` sets NO_COLOR=1 for the whole suite, which rich honours by
        # stripping colour while keeping every other attribute. This one test is about the
        # colour, so it opts back in explicitly rather than asserting on an empty channel.
        console = Console(
            file=buffer,
            width=200,
            force_terminal=True,
            color_system="truecolor",
            no_color=False,
            highlight=False,
        )
        with patch("semolina.cli.codegen._stderr", console):
            _render_check_report(report)
        rendered = buffer.getvalue()

        # The ANSI SGR codes for the two colours, not the literal tag text.
        assert "\x1b[32m" in rendered
        assert "\x1b[31m" in rendered
        assert "[green]" not in rendered
        assert "[red]" not in rendered

    def test_a_driver_error_in_the_fallback_note_is_printed_verbatim(self) -> None:
        """The note interpolates a driver message, which routinely contains brackets."""
        detail = 'Binder Error: Referenced column "x[0]" not found'
        report = ViewCheckReport(
            view_name="v",
            rows=[
                FieldCheckRow(
                    name="x",
                    committed="str",
                    probed="str",
                    route=ROUTE_METADATA,
                    status=STATUS_MATCH,
                )
            ],
            has_drift=False,
            probe_error=detail,
        )

        output = _render_to_text(report)

        assert "x[0]" in output
        assert "Note:" in output

    def test_a_bracketed_view_name_does_not_crash_the_absent_class_note(
        self, duckdb_file_backed_db: Path, tmp_path: Path
    ) -> None:
        """``_run_check``'s ``{view_name!r}`` note goes through the same parser."""
        model = tmp_path / "models.py"
        model.write_text(
            "from semolina import Dimension, SemanticView\n\n\n"
            'class Other(SemanticView, view="other_view"):\n'
            "    country = Dimension[str]()\n",
            encoding="utf-8",
        )

        result = runner.invoke(
            app,
            [
                "codegen",
                "[/red]missing_view",
                "--check",
                "--model",
                str(model),
                "--backend",
                "duckdb",
                "--database",
                str(duckdb_file_backed_db),
            ],
        )

        # Whatever the outcome, it is one of the CLI's own exit codes rather than a
        # MarkupError traceback, and the note names the view it could not find.
        assert result.exit_code in (EXIT_VIEW_NOT_FOUND, EXIT_ANNOTATION_DRIFT), result.stderr
        assert "MarkupError" not in result.stderr
        assert "[/red]missing_view" in result.stderr


class TestAnnotationCheckAgainstLiveDuckDB:
    """The end-to-end contract, over a real file-backed DuckDB semantic view."""

    @pytest.mark.parametrize("view_name", ['"sales_view', 'main."sales.view"'])
    def test_an_invalid_view_name_is_an_error_message_not_a_traceback(
        self, view_name: str, duckdb_file_backed_db: Path, tmp_path: Path
    ) -> None:
        """``quote_table_name``'s ValueError reaches a ``--check`` user as an error line."""
        model = tmp_path / "models.py"
        _generate_model(duckdb_file_backed_db, model)

        result = runner.invoke(
            app,
            [
                "codegen",
                view_name,
                "--check",
                "--model",
                str(model),
                "--backend",
                "duckdb",
                "--database",
                str(duckdb_file_backed_db),
            ],
        )

        assert result.exit_code == 1, result.stderr
        assert isinstance(result.exception, SystemExit)
        assert "Error:" in result.stderr

    def test_a_freshly_generated_model_exits_0_with_empty_stdout(
        self, duckdb_file_backed_db: Path, tmp_path: Path
    ) -> None:
        model = tmp_path / "models.py"
        _generate_model(duckdb_file_backed_db, model)

        result = runner.invoke(
            app,
            [
                "codegen",
                "sales_view",
                "--check",
                "--model",
                str(model),
                "--backend",
                "duckdb",
                "--database",
                str(duckdb_file_backed_db),
            ],
        )

        assert result.exit_code == 0, result.stderr
        assert result.stdout == ""

    def test_an_unread_statement_is_printed_as_a_warning(
        self, duckdb_file_backed_db: Path, tmp_path: Path
    ) -> None:
        """
        GEN-12: a field-like statement the reader cannot read is named on stderr.

        The field then reads as absent, which the report shows as drift; the warning is what
        says why, where before D9 it vanished without a word.
        """
        model = tmp_path / "models.py"
        source = _generate_model(duckdb_file_backed_db, model)
        lines = source.splitlines(keepends=True)
        class_line = next(i for i, line in enumerate(lines) if line.startswith("class "))
        lines.insert(class_line + 1, "    if True:\n        hand_added = Metric[int]()\n")
        model.write_text("".join(lines), encoding="utf-8")

        result = runner.invoke(
            app,
            [
                "codegen",
                "sales_view",
                "--check",
                "--output",
                str(model),
                "--backend",
                "duckdb",
                "--database",
                str(duckdb_file_backed_db),
            ],
        )

        assert result.exit_code == 0, result.stderr
        diagnostics = _diagnostics(result)
        assert "Warning:" in diagnostics, result.stderr
        assert "hand_added" in diagnostics, result.stderr

    def test_two_classes_for_one_view_are_each_checked_and_named(
        self, duckdb_file_backed_db: Path, tmp_path: Path
    ) -> None:
        """M9: every class declaring the view is checked, and each table names its class."""
        model = tmp_path / "models.py"
        source = _generate_model(duckdb_file_backed_db, model)
        class_source = source[source.index("class ") :]
        second = class_source.replace("class ", "class Old", 1)
        model.write_text(source + "\n\n" + second, encoding="utf-8")
        first_name = class_source.split("(", 1)[0].removeprefix("class ")

        result = runner.invoke(
            app,
            [
                "codegen",
                "sales_view",
                "--check",
                "--output",
                str(model),
                "--backend",
                "duckdb",
                "--database",
                str(duckdb_file_backed_db),
            ],
        )

        assert result.exit_code == 0, result.stderr
        diagnostics = _diagnostics(result)
        assert f"({first_name})" in diagnostics, result.stderr
        assert f"(Old{first_name})" in diagnostics, result.stderr

    def test_a_check_that_fell_back_to_metadata_exits_6(
        self, duckdb_file_backed_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A probe that failed is a check that was not performed, even with no drift (A33).

        The fallback compares the model with the catalogue -- the source that wrote it -- so
        it reports a match and exited 0, indistinguishable in CI from a probed pass. It now
        exits ``EXIT_PROBE_FAILED``, with the report and the note still printed.
        """
        from semolina.cli.codegen import EXIT_PROBE_FAILED

        model = tmp_path / "models.py"
        _generate_model(duckdb_file_backed_db, model)

        def refuse(cursor: object, sql: str, params: object) -> None:
            raise RuntimeError("probe unavailable in this test")

        monkeypatch.setattr("semolina.codegen.annotation_check.probe_schema", refuse)
        result = runner.invoke(
            app,
            [
                "codegen",
                "sales_view",
                "--check",
                "--output",
                str(model),
                "--backend",
                "duckdb",
                "--database",
                str(duckdb_file_backed_db),
            ],
        )

        assert result.exit_code == EXIT_PROBE_FAILED, result.stderr
        assert ROUTE_METADATA in result.stderr
        assert "probe unavailable" in _diagnostics(result), result.stderr

    def test_drift_on_a_probed_view_is_named_when_another_view_exits_6(
        self, duckdb_file_backed_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Exit 6 outranks 5, so drift a working probe found is said on stderr instead.

        One view probes and drifts, the other's probe fails. The run exits 6, and without
        the note a CI job that regenerates on 5 never learns the first view is stale.
        """
        from semolina.cli.codegen import EXIT_PROBE_FAILED

        model = tmp_path / "models.py"
        _generate_model(duckdb_file_backed_db, model)

        def report_for(engine: object, view_name: str, committed: object) -> ViewCheckReport:
            probed = view_name == "sales_view"
            return ViewCheckReport(
                view_name=view_name,
                rows=[
                    FieldCheckRow(
                        name="x",
                        committed="str",
                        probed="int" if probed else "str",
                        route=ROUTE_EXECUTE_SCHEMA if probed else ROUTE_METADATA,
                        status=STATUS_DRIFT if probed else STATUS_MATCH,
                    )
                ],
                has_drift=probed,
                probe_error=None if probed else "probe unavailable in this test",
            )

        monkeypatch.setattr("semolina.codegen.annotation_check.check_view", report_for)
        result = runner.invoke(
            app,
            [
                "codegen",
                "sales_view",
                "other_view",
                "--check",
                "--output",
                str(model),
                "--backend",
                "duckdb",
                "--database",
                str(duckdb_file_backed_db),
            ],
        )

        assert result.exit_code == EXIT_PROBE_FAILED, result.stderr
        diagnostics = _diagnostics(result)
        assert "drift" in diagnostics.lower(), result.stderr
        assert "'sales_view'" in diagnostics.split("probe unavailable")[-1], result.stderr

    def test_a_probed_run_with_an_unresolved_field_does_not_exit_6(
        self, duckdb_file_backed_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Exit 6 means the probe failed, not that one field fell back to metadata.

        A probe that succeeded can still carry no column for a field, and that row is
        compared against metadata. Keying exit 6 on any metadata row made such a ``--check``
        exit 6 on every run with ``probe_error`` empty -- a failure nothing the user could
        do would clear. The note says what did happen, rather than "the probe was
        unavailable".
        """
        from semolina.codegen import annotation_check

        model = tmp_path / "models.py"
        _generate_model(duckdb_file_backed_db, model)

        resolve = annotation_check._arrow_annotation

        def miss_revenue(dialect: Any, field: Any, schema: Any) -> Any:
            return None if field.name == "revenue" else resolve(dialect, field, schema)

        monkeypatch.setattr(annotation_check, "_arrow_annotation", miss_revenue)
        result = runner.invoke(
            app,
            [
                "codegen",
                "sales_view",
                "--check",
                "--output",
                str(model),
                "--backend",
                "duckdb",
                "--database",
                str(duckdb_file_backed_db),
            ],
        )

        assert result.exit_code == 0, result.stderr
        assert ROUTE_METADATA in result.stderr
        diagnostics = _diagnostics(result)
        assert "unavailable" not in diagnostics, result.stderr
        assert "revenue" in diagnostics, result.stderr

    def test_the_route_is_reported_on_a_clean_run(
        self, duckdb_file_backed_db: Path, tmp_path: Path
    ) -> None:
        """A green ``--check`` still says what it checked against."""
        model = tmp_path / "models.py"
        _generate_model(duckdb_file_backed_db, model)

        result = runner.invoke(
            app,
            [
                "codegen",
                "sales_view",
                "--check",
                "--model",
                str(model),
                "--backend",
                "duckdb",
                "--database",
                str(duckdb_file_backed_db),
            ],
        )

        assert result.exit_code == 0
        assert ROUTE_EXECUTE_SCHEMA in result.stderr

    def test_an_edited_annotation_exits_with_the_drift_code(
        self, duckdb_file_backed_db: Path, tmp_path: Path
    ) -> None:
        model = tmp_path / "models.py"
        source = _generate_model(duckdb_file_backed_db, model)
        model.write_text(
            source.replace(
                "revenue = Metric[decimal.Decimal | None]()", "revenue = Metric[int | None]()"
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            app,
            [
                "codegen",
                "sales_view",
                "--check",
                "--model",
                str(model),
                "--backend",
                "duckdb",
                "--database",
                str(duckdb_file_backed_db),
            ],
        )

        assert result.exit_code == EXIT_ANNOTATION_DRIFT, result.stderr
        assert result.stdout == ""
        # The table names the field and marks it. Asserted on short, unwrappable tokens:
        # rich wraps a long cell at the terminal width, so a longer substring is not a
        # stable assertion.
        assert "revenue" in result.stderr
        assert "drift" in result.stderr

    @pytest.mark.usefixtures("data_fetch_guard")
    def test_a_check_run_fetches_no_data_rows(
        self, duckdb_file_backed_db: Path, tmp_path: Path
    ) -> None:
        """
        ``--check`` runs "without executing a query for rows", at the CLI seam.

        The guard permits catalogue fetches (``DESCRIBE``/``SHOW``), which is what
        introspection is and what the generation path has always done, and refuses a fetch
        from anything else. The model fixture is written before the guard's scope matters
        because generation is inside it too.
        """
        model = tmp_path / "models.py"
        _generate_model(duckdb_file_backed_db, model)

        result = runner.invoke(
            app,
            [
                "codegen",
                "sales_view",
                "--check",
                "--model",
                str(model),
                "--backend",
                "duckdb",
                "--database",
                str(duckdb_file_backed_db),
            ],
        )

        assert result.exit_code == 0, result.stderr

    def test_a_view_with_no_class_in_the_model_is_named(
        self, duckdb_file_backed_db: Path, tmp_path: Path
    ) -> None:
        model = tmp_path / "models.py"
        model.write_text(
            "from semolina import Dimension, SemanticView\n\n\n"
            'class Other(SemanticView, view="other_view"):\n'
            "    country = Dimension[str]()\n",
            encoding="utf-8",
        )

        result = runner.invoke(
            app,
            [
                "codegen",
                "sales_view",
                "--check",
                "--model",
                str(model),
                "--backend",
                "duckdb",
                "--database",
                str(duckdb_file_backed_db),
            ],
        )

        assert result.exit_code == EXIT_ANNOTATION_DRIFT, result.stderr
        assert "sales_view" in result.stderr

    def test_the_generation_path_is_unchanged(self, duckdb_file_backed_db: Path) -> None:
        """Without ``--check``, model source still goes to stdout and the exit code is 0."""
        result = runner.invoke(
            app,
            [
                "codegen",
                "sales_view",
                "--backend",
                "duckdb",
                "--database",
                str(duckdb_file_backed_db),
            ],
        )

        assert result.exit_code == 0, result.stderr
        assert "class SalesView(SemanticView" in result.stdout
