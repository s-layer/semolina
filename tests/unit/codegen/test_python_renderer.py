"""
Tests for Python code renderer.

Converts IntrospectedView objects into formatted, importable Python source.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from s_layer_semantic_formats import validate_block

from semolina.codegen.introspector import IntrospectedField, IntrospectedView

if TYPE_CHECKING:
    from pathlib import Path


def test_field_class_for_unrecognized_role_raises() -> None:
    """Unrecognized role string raises rather than defaulting to Dimension."""
    from semolina.codegen.python_renderer import _field_class_for

    with pytest.raises(ValueError, match="Unrecognized field role"):
        _field_class_for("widget")


class TestRenderViews:
    """Tests for render_views() function."""

    def test_single_view_metric_field(self) -> None:
        """Single view with one metric field renders Metric[int | None]() assignment."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="revenue", field_type="metric", data_type="int"),
            ],
        )
        source = render_views([view])
        assert "revenue = Metric[int | None]()" in source
        assert "from semolina import Dimension, Fact, Metric, SemanticView" in source

    def test_single_view_dimension_field(self) -> None:
        """Single view with one dimension field renders Dimension[str]() assignment."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="country", field_type="dimension", data_type="str"),
            ],
        )
        source = render_views([view])
        assert "country = Dimension[str]()" in source

    def test_single_view_fact_field(self) -> None:
        """Single view with one fact field renders Fact[float]() assignment."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="unit_price", field_type="fact", data_type="float"),
            ],
        )
        source = render_views([view])
        assert "unit_price = Fact[float]()" in source

    def test_field_with_description_emits_the_description_argument(self) -> None:
        """
        A description becomes ``description=``, so the model carries it at runtime.

        It used to be an attribute docstring, which only a reader of the source could see.
        """
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(
                    name="revenue",
                    field_type="metric",
                    data_type="int",
                    description="Total revenue",
                ),
            ],
        )
        source = render_views([view])
        assert (
            '    revenue = Metric[int | None](\n        description="Total revenue",\n    )\n'
        ) in source
        assert '"""' not in source
        namespace: dict[str, object] = {}
        exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102
        assert namespace["SalesView"].revenue.description == "Total revenue"  # pyright: ignore[reportAttributeAccessIssue]

    def test_field_without_description_no_docstring(self) -> None:
        """Field without description emits no docstring."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="revenue", field_type="metric", data_type="int"),
            ],
        )
        source = render_views([view])
        assert '"""' not in source

    def test_field_todo_data_type_emits_comment(self) -> None:
        """Field with data_type starting 'TODO:' emits a # comment and Dimension[Any]()."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(
                    name="geo",
                    field_type="dimension",
                    data_type='TODO: {"type": "GEOGRAPHY"}',
                ),
            ],
        )
        source = render_views([view])
        assert "# TODO:" in source
        assert "geo = Dimension[Any]()" in source
        # Comment must appear before the field assignment
        todo_idx = source.index("# TODO:")
        field_idx = source.index("geo = Dimension[Any]()")
        assert todo_idx < field_idx

    def test_field_todo_comment_with_newline_stays_single_line(self) -> None:
        """
        A TODO type containing a newline must not break the comment across lines.

        Warehouse type descriptors can be pretty-printed (e.g. a multi-line STRUCT
        definition). The renderer interpolates the descriptor into a ``# ...`` comment,
        so an embedded newline would push the rest onto a non-comment physical line and
        produce a SyntaxError. The comment must collapse to a single line.
        """
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(
                    name="geo",
                    field_type="dimension",
                    data_type='TODO: {\n  "type": "STRUCT"\n}',
                ),
            ],
        )
        source = render_views([view])
        # The interpolated comment must not contain a raw newline that would escape it.
        comment_line = next(line for line in source.splitlines() if line.lstrip().startswith("#"))
        assert "STRUCT" in comment_line
        # Every physical line after the imports must be a comment, an assignment,
        # a docstring, a class/decl, or blank — never an orphaned type-descriptor fragment.
        for line in source.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            assert not stripped.startswith('"type"'), f"comment leaked onto its own line: {line!r}"

    def test_none_data_type_emits_any_type(self) -> None:
        """Field with data_type=None emits FieldClass[Any]() and 'from typing import Any'."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="geo", field_type="dimension", data_type=None),
            ],
        )
        source = render_views([view])
        assert "geo = Dimension[Any]()" in source
        assert "from typing import Any" in source

    def test_source_name_set_emits_source_kwarg(self) -> None:
        """Field with source_name emits FieldClass[T](source='...')."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="orders_view",
            class_name="OrdersView",
            fields=[
                IntrospectedField(
                    name="order_id",
                    field_type="dimension",
                    data_type="str",
                    source_name="order_id",
                ),
            ],
        )
        source = render_views([view])
        assert 'order_id = Dimension[str](source="order_id")' in source

    def test_source_name_none_no_source_kwarg(self) -> None:
        """Field without source_name emits FieldClass[T]() without source= kwarg."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="orders_view",
            class_name="OrdersView",
            fields=[
                IntrospectedField(
                    name="order_id",
                    field_type="dimension",
                    data_type="str",
                    source_name=None,
                ),
            ],
        )
        source = render_views([view])
        assert "order_id = Dimension[str]()" in source
        assert "source=" not in source

    def test_datetime_date_type_imports_datetime(self) -> None:
        """Field with datetime.date data_type causes 'import datetime' to be emitted."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(
                    name="order_date", field_type="dimension", data_type="datetime.date"
                ),
            ],
        )
        source = render_views([view])
        assert "import datetime" in source

    def test_datetime_datetime_type_imports_datetime(self) -> None:
        """Field with datetime.datetime data_type causes 'import datetime' to be emitted."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(
                    name="created_at",
                    field_type="dimension",
                    data_type="datetime.datetime",
                ),
            ],
        )
        source = render_views([view])
        assert "import datetime" in source

    def test_no_datetime_fields_no_datetime_import(self) -> None:
        """No datetime fields → no 'import datetime' in output."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="revenue", field_type="metric", data_type="int"),
                IntrospectedField(name="country", field_type="dimension", data_type="str"),
            ],
        )
        source = render_views([view])
        assert "import datetime" not in source
        assert "from typing import Any" not in source

    def test_multiple_views_single_imports_section(self) -> None:
        """Multiple views produce a single shared imports section at the top."""
        from semolina.codegen.python_renderer import render_views

        views = [
            IntrospectedView(
                view_name="sales_view",
                class_name="SalesView",
                fields=[
                    IntrospectedField(name="revenue", field_type="metric", data_type="int"),
                ],
            ),
            IntrospectedView(
                view_name="orders_view",
                class_name="OrdersView",
                fields=[
                    IntrospectedField(name="order_count", field_type="metric", data_type="int"),
                ],
            ),
        ]
        source = render_views(views)
        # Only one imports line
        assert source.count("from semolina import Dimension, Fact, Metric, SemanticView") == 1
        # Both class definitions present
        assert "class SalesView(SemanticView" in source
        assert "class OrdersView(SemanticView" in source
        # Fields use typed subscripts
        assert "revenue = Metric[int | None]()" in source
        assert "order_count = Metric[int | None]()" in source

    def test_class_declaration_uses_full_view_name(self) -> None:
        """Class view= parameter uses the full original schema-qualified name."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="my_schema.sales_view",
            class_name="SalesView",
            fields=[],
        )
        source = render_views([view])
        assert 'view="my_schema.sales_view"' in source

    def test_class_declaration_format(self) -> None:
        """Class declaration uses correct SemanticView inheritance syntax."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="revenue", field_type="metric", data_type="int"),
            ],
        )
        source = render_views([view])
        assert 'class SalesView(SemanticView, view="sales_view"):' in source

    def test_imports_appear_before_classes(self) -> None:
        """The semolina import line appears before any class definition."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="revenue", field_type="metric", data_type="int"),
            ],
        )
        source = render_views([view])
        import_idx = source.index("from semolina import")
        class_idx = source.index("class SalesView")
        assert import_idx < class_idx

    def test_datetime_import_before_semolina_import(self) -> None:
        """'import datetime' appears before the semolina import line (stdlib before third-party)."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(
                    name="order_date", field_type="dimension", data_type="datetime.date"
                ),
            ],
        )
        source = render_views([view])
        semolina_idx = source.index("from semolina import")
        datetime_idx = source.index("import datetime")
        assert datetime_idx < semolina_idx

    def test_empty_views_list(self) -> None:
        """Empty views list returns a string with just the imports."""
        from semolina.codegen.python_renderer import render_views

        source = render_views([])
        assert "from semolina import Dimension, Fact, Metric, SemanticView" in source

    def test_datetime_across_multiple_views(self) -> None:
        """Datetime import triggered by field in any view across all views."""
        from semolina.codegen.python_renderer import render_views

        views = [
            IntrospectedView(
                view_name="sales_view",
                class_name="SalesView",
                fields=[
                    IntrospectedField(name="revenue", field_type="metric", data_type="int"),
                ],
            ),
            IntrospectedView(
                view_name="orders_view",
                class_name="OrdersView",
                fields=[
                    IntrospectedField(
                        name="order_date",
                        field_type="dimension",
                        data_type="datetime.date",
                    ),
                ],
            ),
        ]
        source = render_views(views)
        assert "import datetime" in source
        # Only one import datetime line
        assert source.count("import datetime") == 1


def _exec(source: str) -> dict[str, Any]:
    """Run generated source and return its namespace."""
    namespace: dict[str, Any] = {}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102
    return namespace


class TestFieldMetadata:
    """Label, synonyms and semantic format are written as arguments the model keeps."""

    _FORMAT = validate_block(
        {
            "value": {"kind": "currency", "currency": "USD"},
            "display": {"decimals": {"min": 0, "max": 2}, "compact": "auto"},
        },
        "metric",
    )

    def _view(self, **metadata: Any) -> IntrospectedView:
        return IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="revenue", field_type="metric", data_type="int", **metadata)
            ],
        )

    def test_every_argument_in_a_fixed_order(self) -> None:
        """``source``, ``label``, ``description``, ``synonyms``, ``semantic_format``."""
        from semolina.codegen.python_renderer import render_views

        source = render_views(
            [
                self._view(
                    source_name="Revenue",
                    label="Revenue (USD)",
                    description="Gross revenue",
                    synonyms=("sales", "turnover"),
                    semantic_format=self._FORMAT,
                )
            ]
        )
        assert (
            "    revenue = Metric[int | None](\n"
            '        source="Revenue",\n'
            '        label="Revenue (USD)",\n'
            '        description="Gross revenue",\n'
            '        synonyms=("sales", "turnover"),\n'
            "        semantic_format={\n"
            '            "display": {\n'
            '                "compact": "auto",\n'
            '                "decimals": [0, 2],\n'
        ) in source

    def test_the_model_reads_back_what_the_warehouse_reported(self) -> None:
        """Every value survives the trip through generated source."""
        from semolina.codegen.python_renderer import render_views

        source = render_views(
            [
                self._view(
                    label="Revenue (USD)",
                    description="Gross revenue",
                    synonyms=("sales",),
                    semantic_format=self._FORMAT,
                )
            ]
        )
        revenue = _exec(source)["SalesView"].revenue
        assert revenue.label == "Revenue (USD)"
        assert revenue.description == "Gross revenue"
        assert revenue.synonyms == ("sales",)
        assert revenue.semantic_format == self._FORMAT
        assert revenue.semantic_format.normal_form() == self._FORMAT.normal_form()

    def test_a_single_synonym_is_still_a_tuple(self) -> None:
        """``("sales",)``, not ``("sales")``, which is a string."""
        from semolina.codegen.python_renderer import render_views

        source = render_views([self._view(synonyms=("sales",))])
        assert 'synonyms=("sales",),' in source

    def test_source_alone_keeps_the_one_line_form(self) -> None:
        """A view without metadata renders as it always has."""
        from semolina.codegen.python_renderer import render_views

        source = render_views([self._view(source_name="Revenue")])
        assert '    revenue = Metric[int | None](source="Revenue")\n' in source

    def test_view_description(self) -> None:
        """The view's comment becomes the class's ``description=``."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[IntrospectedField(name="revenue", field_type="metric", data_type="int")],
            description="Gross sales by country",
        )
        source = render_views([view])
        assert (
            "class SalesView(\n"
            "    SemanticView,\n"
            '    view="sales_view",\n'
            '    description="Gross sales by country",\n'
            "):\n"
        ) in source
        assert _exec(source)["SalesView"]._description == "Gross sales by country"

    def test_the_output_is_ruff_formatted_already(self) -> None:
        """Generated metadata needs no reformatting, so a file without ruff reads the same."""
        from semolina.codegen.python_renderer import format_with_ruff, render_views, ruff_available

        if not ruff_available():
            pytest.skip("ruff is not installed")
        source = render_views(
            [
                self._view(
                    label="Revenue (USD)",
                    description="Gross revenue",
                    synonyms=("sales",),
                    semantic_format=self._FORMAT,
                )
            ]
        )
        assert format_with_ruff(source) == source


class TestMetricNullability:
    """
    Metric annotations are uniformly ``T | None``.

    The decoration is applied in ``_build_model_context`` and nowhere else. Applying it in
    a type map or an engine would put ``| None`` into ``IntrospectedField.data_type``,
    which the artifact generator and ``--check`` both read as the mapped annotation.
    """

    def test_metric_annotation_gains_none(self) -> None:
        """A metric field renders ``Metric[T | None]()``."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="revenue", field_type="metric", data_type="int"),
            ],
        )
        source = render_views([view])
        assert "revenue = Metric[int | None]()" in source, source

    def test_dimension_annotation_gains_no_none(self) -> None:
        """A dimension field is untouched — the nullability policy covers metrics only."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="country", field_type="dimension", data_type="str"),
            ],
        )
        source = render_views([view])
        assert "country = Dimension[str]()" in source, source
        assert "| None" not in source, source

    def test_fact_annotation_gains_no_none(self) -> None:
        """A fact field is untouched by the metric nullability stance."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="unit_price", field_type="fact", data_type="float"),
            ],
        )
        source = render_views([view])
        assert "unit_price = Fact[float]()" in source, source
        assert "| None" not in source, source

    def test_unmapped_metric_is_any_or_none(self) -> None:
        """An unmapped metric renders ``Metric[Any | None]()`` and still imports Any."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="blob", field_type="metric", data_type=None),
                IntrospectedField(name="geo", field_type="dimension", data_type=None),
            ],
        )
        source = render_views([view])
        assert "blob = Metric[Any | None]()" in source, source
        assert "geo = Dimension[Any]()" in source, source
        assert "from typing import Any" in source, source

    def test_source_kwarg_survives_nullability(self) -> None:
        """A nullable metric with a source= kwarg keeps both."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="orders_view",
            class_name="OrdersView",
            fields=[
                IntrospectedField(
                    name="revenue",
                    field_type="metric",
                    data_type="int",
                    source_name="Revenue",
                ),
            ],
        )
        source = render_views([view])
        assert 'revenue = Metric[int | None](source="Revenue")' in source, source


class TestImportEmission:
    """
    Imports are derived from the *resolved* annotations, not from the raw introspected type.

    The predecessor computed ``needs_datetime`` by exact membership of
    ``IntrospectedField.data_type`` in a frozenset of three literal strings, evaluated
    before ``_build_model_context`` ran. Appending ``| None`` to a metric annotation would
    have silently stopped that test matching, dropping ``import datetime`` from generated
    modules for datetime-typed metrics only — a NameError at import time in the user's
    model, with most of the suite still green.
    """

    def test_nullable_datetime_metric_still_imports_datetime(self) -> None:
        """A ``datetime.datetime`` metric emits ``import datetime`` despite the ``| None``."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(
                    name="last_seen",
                    field_type="metric",
                    data_type="datetime.datetime",
                ),
            ],
        )
        source = render_views([view])
        assert "import datetime" in source, source
        assert "last_seen = Metric[datetime.datetime | None]()" in source, source

    def test_decimal_annotation_imports_decimal(self) -> None:
        """A ``decimal.Decimal`` annotation emits ``import decimal``."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(
                    name="revenue",
                    field_type="metric",
                    data_type="decimal.Decimal",
                ),
            ],
        )
        source = render_views([view])
        assert "import decimal" in source, source
        assert "revenue = Metric[decimal.Decimal | None]()" in source, source

    def test_no_decimal_fields_no_decimal_import(self) -> None:
        """No decimal-annotated field means no ``import decimal``."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="revenue", field_type="metric", data_type="int"),
            ],
        )
        source = render_views([view])
        assert "import decimal" not in source, source

    def test_stdlib_imports_are_sorted(self) -> None:
        """``import datetime`` precedes ``import decimal`` regardless of field order."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="revenue", field_type="metric", data_type="decimal.Decimal"),
                IntrospectedField(
                    name="last_seen", field_type="metric", data_type="datetime.datetime"
                ),
            ],
        )
        source = render_views([view])
        assert source.index("import datetime") < source.index("import decimal"), source

    def test_semolina_import_emitted_once_and_sorted(self) -> None:
        """Exactly one ``from semolina import`` line is emitted, with sorted names."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="revenue", field_type="metric", data_type="int"),
            ],
        )
        source = render_views([view])
        assert source.count("from semolina import") == 1, source
        assert "from semolina import Dimension, Fact, Metric, SemanticView" in source, source

    def test_jsonvalue_field_imports_jsonvalue(self) -> None:
        """
        A ``JsonValue`` annotation joins the single ``from semolina import`` line.

        One merged statement, not a second import: ruff's isort does not merge two separate
        ``from semolina import`` statements, and ruff ships as the optional ``codegen-lint``
        extra — so a second statement would ship duplicated-looking output for anyone
        without it.
        """
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="events_view",
            class_name="EventsView",
            fields=[
                IntrospectedField(
                    name="payload",
                    field_type="dimension",
                    data_type="JsonValue",
                    raw_type="VARIANT",
                ),
            ],
        )
        source = render_views([view])

        assert source.count("from semolina import") == 1, source
        assert "from semolina import Dimension, Fact, JsonValue, Metric, SemanticView" in source, (
            source
        )
        assert "payload = Dimension[JsonValue]()" in source, source

    def test_no_jsonvalue_field_no_jsonvalue_import(self) -> None:
        """A module with no VARIANT field does not import ``JsonValue``."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="revenue", field_type="metric", data_type="int"),
            ],
        )
        source = render_views([view])

        assert "JsonValue" not in source, source

    def test_render_views_is_deterministic(self) -> None:
        """Two renders of the same input return byte-identical source."""
        from semolina.codegen.python_renderer import render_views

        views = [
            IntrospectedView(
                view_name="sales_view",
                class_name="SalesView",
                fields=[
                    IntrospectedField(
                        name="revenue", field_type="metric", data_type="decimal.Decimal"
                    ),
                    IntrospectedField(
                        name="last_seen", field_type="metric", data_type="datetime.datetime"
                    ),
                    IntrospectedField(name="geo", field_type="dimension", data_type=None),
                ],
            ),
        ]
        assert render_views(views) == render_views(views)


class TestRawTypeComment:
    """
    The raw warehouse type survives into generated source once a type stops being a TODO.

    Without it, the ``TODO:`` comment would be the only channel carrying a warehouse type
    into emitted code, and it is skipped for mapped types — so annotating a DuckDB
    ``DECIMAL(38,2)`` as ``decimal.Decimal`` would have thrown away the precision and scale
    the user needs in order to reason about the column. ``IntrospectedField.raw_type``
    carries it instead, and the renderer emits it for any annotation that does not name the
    warehouse type it came from.
    """

    def test_lossy_annotation_emits_raw_type_comment(self) -> None:
        """A decimal.Decimal annotation keeps its DECIMAL(38,2) origin as a comment."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(
                    name="revenue",
                    field_type="metric",
                    data_type="decimal.Decimal",
                    raw_type="DECIMAL(38,2)",
                ),
            ],
        )
        source = render_views([view])
        assert "# DECIMAL(38,2)" in source, source
        comment_idx = source.index("# DECIMAL(38,2)")
        field_idx = source.index("revenue = Metric[decimal.Decimal | None]()")
        assert comment_idx < field_idx, source

    def test_faithful_annotation_emits_no_comment(self) -> None:
        """A str annotation for a VARCHAR column already names its origin, so no comment."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(
                    name="country",
                    field_type="dimension",
                    data_type="str",
                    raw_type="VARCHAR",
                ),
            ],
        )
        source = render_views([view])
        assert "#" not in source, source

    def test_todo_comment_is_unchanged(self) -> None:
        """An unmapped field still emits the existing ``# TODO: <raw>`` text verbatim."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(
                    name="geo",
                    field_type="dimension",
                    data_type='TODO: {"type": "GEOGRAPHY"}',
                    raw_type='{"type": "GEOGRAPHY"}',
                ),
            ],
        )
        source = render_views([view])
        assert '# TODO: {"type": "GEOGRAPHY"}' in source, source
        assert source.count("#") == 1, source

    def test_raw_type_with_newline_stays_single_line(self) -> None:
        """
        A pretty-printed warehouse descriptor collapses to one physical comment line.

        Snowflake's ``data_type`` is a JSON blob and can arrive pretty-printed. A comment
        interpolating a raw newline would push the remainder onto a non-comment line and
        make the generated module a SyntaxError — or, worse, let a crafted catalogue entry
        put arbitrary text onto a fresh line of a file the user then executes.
        """
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(
                    name="revenue",
                    field_type="metric",
                    data_type="decimal.Decimal",
                    raw_type='{\n  "type": "FIXED",\n  "scale": 2\n}',
                ),
            ],
        )
        source = render_views([view])
        comment_lines = [line for line in source.splitlines() if line.strip().startswith("#")]
        assert len(comment_lines) == 1, source
        assert "FIXED" in comment_lines[0], source

    def test_raw_type_is_optional(self) -> None:
        """IntrospectedField still constructs with no raw_type argument."""
        field = IntrospectedField(name="revenue", field_type="metric", data_type="int")
        assert field.raw_type is None

    def test_lossy_annotation_without_raw_type_emits_no_comment(self) -> None:
        """An engine that supplies no raw_type produces no comment rather than a broken one."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(
                    name="revenue",
                    field_type="metric",
                    data_type="decimal.Decimal",
                ),
            ],
        )
        source = render_views([view])
        assert "#" not in source, source

    def test_lossy_base_type_emits_comment_for_faithful_looking_annotation(self) -> None:
        """
        A ``str``-annotated UUID column earns a comment: the annotation hides the type.

        The annotation names the measured value, so a DuckDB ``UUID`` is ``str``. That is
        correct and lossy at the same time, which is exactly the case the raw-type comment
        exists for.
        """
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(
                    name="order_id",
                    field_type="dimension",
                    data_type="str",
                    raw_type="UUID",
                ),
            ],
        )
        source = render_views([view])
        assert "# UUID" in source, source


def _string_constants(source: str) -> list[str]:
    """
    Parse ``source`` and return every string constant it contains, in source order.

    Parsing rather than substring-matching is the point: a payload that stayed safely
    inside its literal still *contains* the text ``import os``, so ``"import os" not in
    source`` proves nothing. Recovering the value through :func:`ast.parse` proves the
    generated module holds the payload as data.

    Args:
        source: Generated Python source.

    Returns:
        The values of every ``ast.Constant`` string node.

    Raises:
        SyntaxError: If the generated source does not parse, which is itself the failure.
    """
    import ast

    tree = ast.parse(source)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _executes_only_imports_and_class_definitions(source: str) -> bool:
    """
    Report whether a generated module's top level is only imports and class definitions.

    The shape assertion that matters: an injected payload has to become a *statement* to
    run at import time, and every statement this renderer legitimately emits is an
    ``import``, a ``from ... import``, or a ``class``.

    Args:
        source: Generated Python source.

    Returns:
        True when nothing else appears at module level.
    """
    import ast

    return all(
        isinstance(node, ast.Import | ast.ImportFrom | ast.ClassDef)
        for node in ast.parse(source).body
    )


class TestWarehouseMetadataCannotInjectPython:
    """
    Everything the warehouse supplies is written into the generated file as a *literal*.

    ``docs/src/how-to/codegen.rst`` documents the workflow as redirecting this output to a
    file the user then imports, so a value that closes its own literal is module-level code
    execution rather than a formatting bug. ``source_name``, ``view_name`` and
    ``description`` all come from catalogue metadata.
    """

    def test_a_source_name_cannot_close_its_own_literal(self) -> None:
        from semolina.codegen.python_renderer import render_views

        payload = 'A"); import os; os.system("id") #'
        view = IntrospectedView(
            view_name="v",
            class_name="V",
            fields=[
                IntrospectedField(
                    name="c", field_type="dimension", data_type="str", source_name=payload
                )
            ],
        )

        source = render_views([view])

        assert _executes_only_imports_and_class_definitions(source)
        assert payload in _string_constants(source)

    def test_a_source_name_with_a_backslash_round_trips(self) -> None:
        r"""A literal ``\n`` in a warehouse name must stay two characters, not a newline."""
        from semolina.codegen.python_renderer import render_views

        payload = 'back\\slash and "quote"'
        view = IntrospectedView(
            view_name="v",
            class_name="V",
            fields=[
                IntrospectedField(
                    name="c", field_type="dimension", data_type="str", source_name=payload
                )
            ],
        )

        source = render_views([view])

        assert payload in _string_constants(source)

    def test_a_view_name_cannot_close_its_own_literal(self) -> None:
        from semolina.codegen.python_renderer import render_views

        payload = 'v"); import os; os.system("id") #'
        view = IntrospectedView(
            view_name=payload,
            class_name="V",
            fields=[IntrospectedField(name="c", field_type="dimension", data_type="str")],
        )

        source = render_views([view])

        assert _executes_only_imports_and_class_definitions(source)
        assert payload in _string_constants(source)

    def test_a_description_cannot_close_its_own_docstring(self) -> None:
        """A column COMMENT is warehouse metadata too, and it lands in a docstring."""
        from semolina.codegen.python_renderer import render_views

        payload = 'ends the docstring """ ; import os; os.system("id") #'
        view = IntrospectedView(
            view_name="v",
            class_name="V",
            fields=[
                IntrospectedField(
                    name="c", field_type="dimension", data_type="str", description=payload
                )
            ],
        )

        source = render_views([view])

        assert _executes_only_imports_and_class_definitions(source)
        assert payload in _string_constants(source)

    def test_metadata_strings_cannot_close_their_literals(self) -> None:
        """Label, synonyms, the view comment and a format extension are all literals."""
        from semolina.codegen.python_renderer import render_views

        payload = 'A"); import os; os.system("id") #'
        block = validate_block({"value": {"kind": "number"}, "x-acme": {"note": payload}}, "metric")
        view = IntrospectedView(
            view_name="v",
            class_name="V",
            fields=[
                IntrospectedField(
                    name="c",
                    field_type="metric",
                    data_type="int",
                    label=payload,
                    synonyms=(payload, "plain"),
                    semantic_format=block,
                )
            ],
            description=payload,
        )

        source = render_views([view])

        # Executing the module proves it: an escaped literal reads back as the exact string,
        # and one that closed early would have run its code or failed to compile.
        assert _string_constants(source).count(payload) == 4
        c = _exec(source)["V"].c
        assert c.label == payload
        assert c.synonyms == (payload, "plain")
        assert c.semantic_format == block
        assert _exec(source)["V"]._description == payload

    def test_a_multi_line_description_round_trips(self) -> None:
        """A newline is escaped inside the literal and comes back exactly."""
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name="v",
            class_name="V",
            fields=[
                IntrospectedField(
                    name="c",
                    field_type="dimension",
                    data_type="str",
                    description="first line\nsecond line",
                )
            ],
        )

        source = render_views([view])

        assert 'description="first line\\nsecond line",' in source
        assert "first line\nsecond line" in _string_constants(source)


class TestCheckParses:
    """``check_parses`` turns every way ``ast.parse`` refuses source into one error."""

    def test_a_nul_byte_is_a_generated_source_error(self) -> None:
        """Python 3.11 raises ``ValueError`` for a NUL, not ``SyntaxError`` as 3.12+ does."""
        from semolina.codegen.python_renderer import GeneratedSourceError, check_parses

        with pytest.raises(GeneratedSourceError, match="null bytes"):
            check_parses('x = "\x00"\n')

    def test_a_value_error_from_the_parser_is_a_generated_source_error(self) -> None:
        """Pinned on every Python, not only 3.11, by having the parser raise it."""
        from semolina.codegen.python_renderer import GeneratedSourceError, check_parses

        refusal = ValueError("source code string cannot contain null bytes")
        with (
            patch("semolina.codegen.python_renderer.ast.parse", side_effect=refusal),
            pytest.raises(GeneratedSourceError, match="null bytes"),
        ):
            check_parses("x = 1\n")


class TestFormatWithRuff:
    """Tests for format_with_ruff() function."""

    def test_valid_python_formatted(self) -> None:
        """format_with_ruff() runs the real formatter; ruff is a dev dependency."""
        from semolina.codegen.python_renderer import format_with_ruff

        assert format_with_ruff("x=1\n") == "x = 1\n"

    # GEN-02 inverted the three tests below. Each pinned a silent fallback -- a ruff that
    # could not run, or ran and failed, returned the source unformatted with no word to
    # anyone -- and the requirement is that the failure is reported instead. ruff being
    # *absent* is still a documented degrade (test_short_circuits_when_ruff_unavailable).

    def test_a_ruff_that_cannot_be_spawned_raises(self) -> None:
        """format_with_ruff() raises when ruff is installed but its process cannot start."""
        from semolina.codegen.python_renderer import GeneratedSourceError, format_with_ruff

        with (
            patch("subprocess.run", side_effect=FileNotFoundError("python not found")),
            pytest.raises(GeneratedSourceError, match="python not found"),
        ):
            format_with_ruff("x = 1\n")

    def test_a_failing_format_pass_raises_with_ruffs_stderr(self) -> None:
        """format_with_ruff() raises, carrying ruff's stderr, when ruff format exits non-zero."""
        from semolina.codegen.python_renderer import GeneratedSourceError, format_with_ruff

        mock_result = MagicMock()
        mock_result.returncode = 2
        mock_result.stdout = ""
        mock_result.stderr = "error: Failed to parse models.py:1:3"
        with (
            patch("subprocess.run", side_effect=[mock_result]),
            pytest.raises(GeneratedSourceError, match="Failed to parse models.py:1:3"),
        ):
            format_with_ruff("x = 1\n")

    def test_a_failing_isort_pass_raises_with_ruffs_stderr(self) -> None:
        """format_with_ruff() raises when the import-sort pass exits non-zero."""
        from semolina.codegen.python_renderer import GeneratedSourceError, format_with_ruff

        mock_format = MagicMock()
        mock_format.returncode = 0
        mock_format.stdout = "x = 1\n"
        mock_isort = MagicMock()
        mock_isort.returncode = 1
        mock_isort.stdout = ""
        mock_isort.stderr = "I001 unfixable"
        with (
            patch("subprocess.run", side_effect=[mock_format, mock_isort]),
            pytest.raises(GeneratedSourceError, match="I001 unfixable"),
        ):
            format_with_ruff("x=1\n")

    def test_short_circuits_when_ruff_unavailable(self) -> None:
        """format_with_ruff() returns source and spawns no subprocess when ruff is absent."""
        from semolina.codegen import python_renderer

        source = "x=1\n"
        with (
            patch.object(python_renderer, "ruff_available", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            result = python_renderer.format_with_ruff(source)
        assert result == source
        mock_run.assert_not_called()

    def test_the_working_directorys_ruff_config_does_not_apply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Formatting is the same from any working directory (GEN-07, A28).

        ruff resolved its settings from ``--stdin-filename models.py`` against the working
        directory, so a ``pyproject.toml`` there -- single quotes, a 40-column limit --
        changed the generated module, and regenerating from a subdirectory produced a diff.
        """
        from semolina.codegen.python_renderer import format_with_ruff

        source = 'very_long_name_number_one = {"key": "value", "other": "thing"}\n'
        plain = tmp_path / "plain"
        configured = tmp_path / "configured"
        plain.mkdir()
        configured.mkdir()
        (configured / "pyproject.toml").write_text(
            '[tool.ruff]\nline-length = 40\n\n[tool.ruff.format]\nquote-style = "single"\n'
        )

        monkeypatch.chdir(plain)
        from_plain = format_with_ruff(source)
        monkeypatch.chdir(configured)
        from_configured = format_with_ruff(source)

        assert from_configured == from_plain


class TestRuffAvailable:
    """Tests for ruff_available() helper."""

    def test_true_when_installed(self) -> None:
        """ruff_available() is True when importlib finds the ruff package."""
        from semolina.codegen.python_renderer import ruff_available

        with patch("importlib.util.find_spec", return_value=object()):
            assert ruff_available() is True

    def test_false_when_not_installed(self) -> None:
        """ruff_available() is False when the ruff package cannot be found."""
        from semolina.codegen.python_renderer import ruff_available

        with patch("importlib.util.find_spec", return_value=None):
            assert ruff_available() is False


class TestRenderAndFormat:
    """Tests for render_and_format() convenience wrapper."""

    def test_integration_ruff_available(self) -> None:
        """
        render_and_format() applies both ruff passes: the line wrap and the import sort.

        The field's long ``source=`` makes an over-long line, and the stdlib import needs a
        blank line before the first-party one, so the raw render differs from the formatted
        one on both counts. A render ruff would leave untouched could not show formatting
        happened at all.
        """
        from semolina.codegen.python_renderer import render_and_format

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(
                    name="order_timestamp",
                    field_type="dimension",
                    data_type="datetime.datetime",
                    source_name="ORDER TIMESTAMP IN THE WAREHOUSE LOCAL TIME ZONE",
                ),
                IntrospectedField(name="revenue", field_type="metric", data_type="int"),
            ],
        )

        assert render_and_format([view]) == (
            "import datetime\n"
            "\n"
            "from semolina import Dimension, Fact, Metric, SemanticView\n"
            "\n"
            "\n"
            'class SalesView(SemanticView, view="sales_view"):\n'
            "    order_timestamp = Dimension[datetime.datetime](\n"
            '        source="ORDER TIMESTAMP IN THE WAREHOUSE LOCAL TIME ZONE"\n'
            "    )\n"
            "    revenue = Metric[int | None]()\n"
        )

    def test_fallback_when_ruff_unavailable(self) -> None:
        """
        render_and_format() returns unformatted source if ruff is not installed.

        GEN-02: this used to simulate "unavailable" with a ``subprocess.run`` that could not
        start, which is now a reported failure. Absence is patched at ``ruff_available``,
        which is what the name always meant.
        """
        from semolina.codegen import python_renderer

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="revenue", field_type="metric", data_type="int"),
            ],
        )
        with (
            patch.object(python_renderer, "ruff_available", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            result = python_renderer.render_and_format([view])
        assert "SalesView" in result
        assert "revenue = Metric[int | None]()" in result
        mock_run.assert_not_called()

    @pytest.mark.parametrize(
        "class_name",
        [
            pytest.param("2024Sales", id="leading-digit"),
            pytest.param("class", id="keyword"),
            pytest.param("Sales Report", id="space"),
        ],
    )
    def test_a_render_that_does_not_parse_raises(self, class_name: str) -> None:
        """
        The rendered module is ``ast.parse``d, and a failure names the parser's complaint.

        Driven with a class name the CLI would have refused, because the renderer is a
        library boundary too: an ``IntrospectedView`` built by hand reaches it with no CLI in
        front. ruff is off so the parse check is what has to notice.
        """
        from semolina.codegen import python_renderer

        view = IntrospectedView(
            view_name="sales_view",
            class_name=class_name,
            fields=[IntrospectedField(name="revenue", field_type="metric", data_type="int")],
        )
        with (
            patch.object(python_renderer, "ruff_available", return_value=False),
            pytest.raises(python_renderer.GeneratedSourceError, match="does not parse"),
        ):
            python_renderer.render_and_format([view])


class TestClassNameFor:
    """``class_name_for`` always returns a name a generated ``class`` statement accepts."""

    @pytest.mark.parametrize(
        ("view_name", "expected"),
        [
            ("sales_view", "SalesView"),
            ("analytics.sales_view", "SalesView"),
            ('"Order Book"', "OrderBook"),
            ("2024_orders", "View2024Orders"),
            ("none", "ViewNone"),
            ("analytics.true", "ViewTrue"),
            ('"FALSE"', "ViewFalse"),
            ("café_sales", "CaféSales"),
            ("ventes_été", "VentesÉté"),
            ("売上", "売上"),
            ("analytics.売上_2024", "売上2024"),
            ("sales²", "Sales"),
        ],
    )
    def test_class_name(self, view_name: str, expected: str) -> None:
        from semolina.codegen.introspector import class_name_for

        assert class_name_for(view_name) == expected

    @pytest.mark.parametrize("view_name", ["none", "true", "false"])
    def test_a_keyword_view_name_renders_compilable_source(self, view_name: str) -> None:
        """``capitalize()`` turns these three into Python constants, which no class may be."""
        from semolina.codegen.introspector import class_name_for
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name=view_name,
            class_name=class_name_for(view_name),
            fields=[IntrospectedField(name="country", field_type="dimension", data_type="str")],
        )
        compile(render_views([view]), "<generated>", "exec")

    @pytest.mark.parametrize(
        ("view_name", "expected"),
        [
            pytest.param("analytics.metric", "ViewMetric", id="Metric"),
            pytest.param("fact", "ViewFact", id="Fact"),
            pytest.param("dimension", "ViewDimension", id="Dimension"),
            pytest.param("any", "ViewAny", id="Any"),
            pytest.param("semantic_view", "ViewSemanticView", id="SemanticView"),
            pytest.param('"json value"', "ViewJsonValue", id="JsonValue"),
        ],
    )
    def test_a_view_named_after_a_module_import_is_prefixed(
        self, view_name: str, expected: str
    ) -> None:
        """
        A class may not take a name the generated module imports, so it gets ``View`` too.

        ``analytics.metric`` capitalizes to ``Metric``, and ``class Metric(SemanticView)``
        rebinds the imported ``Metric`` for every class after it. The rule is the one
        keywords already follow, and the one field names follow with their trailing ``_``.
        """
        from semolina.codegen.introspector import class_name_for

        assert class_name_for(view_name) == expected

    def test_a_reserved_set_can_be_passed(self) -> None:
        """The DTO command names its own template's imports; the default is the model's."""
        from semolina.codegen.introspector import class_name_for

        assert class_name_for("pydantic_view", reserved=frozenset({"PydanticView"})) == (
            "ViewPydanticView"
        )
        assert class_name_for("metric", reserved=frozenset()) == "Metric"

    @pytest.mark.parametrize(
        ("view_name", "expected"),
        [
            pytest.param('"ｓales"', "Sales", id="fullwidth-s"),
            pytest.param('"ｍetric"', "ViewMetric", id="fullwidth-metric"),
            pytest.param('"ℌello"', "Hello", id="black-letter-H"),
        ],
    )
    def test_a_derived_name_is_nfkc_normalised(self, view_name: str, expected: str) -> None:
        """
        Python reads identifiers in NFKC form, so the derived name is the one Python binds.

        ``class Ｓales`` declares ``Sales``. Deriving ``Ｓales`` would let ``"ｓales"`` and
        ``sales`` pass the duplicate check as two names and then overwrite each other.
        """
        from semolina.codegen.introspector import class_name_for

        assert class_name_for(view_name) == expected

    @pytest.mark.parametrize("view_name", ["metric", "any", "semantic_view", "dimension"])
    def test_an_import_named_view_leaves_every_import_bound(self, view_name: str) -> None:
        """
        After the module runs, each name it imported still names the import.

        A class body sees the import while it runs, so the damage is to whatever comes after
        it: the next class's ``Dimension[Any]`` would name the class. Checking the module's
        globals catches the rebinding wherever it lands.
        """
        import typing

        import semolina
        from semolina.codegen.introspector import class_name_for
        from semolina.codegen.python_renderer import render_views

        view = IntrospectedView(
            view_name=view_name,
            class_name=class_name_for(view_name),
            fields=[
                IntrospectedField(name="country", field_type="dimension", data_type=None),
                IntrospectedField(name="revenue", field_type="metric", data_type="int"),
            ],
        )
        namespace: dict[str, object] = {}
        exec(compile(render_views([view]), "<generated>", "exec"), namespace)  # noqa: S102

        assert namespace["Any"] is typing.Any
        for name in ("SemanticView", "Metric", "Dimension", "Fact"):
            assert namespace[name] is getattr(semolina, name), name
        assert isinstance(namespace[class_name_for(view_name)], type)
