"""
Tests for the annotation-drift comparison core.

Record/replay contract: the DuckDB half of this module runs **live, in-process**, against
in-memory and file-backed DuckDB, and must never carry ``pytest.mark.adbc_cassette`` — the
same rule ``tests/unit/test_type_fidelity_duckdb.py``'s module docstring states and for the
same reason (``adbc_auto_patch`` lists ``adbc_driver_manager.dbapi``, which DuckDB routes
through, and ``adbc_dialect`` maps that module to the Databricks sqlglot dialect).

The Snowflake half reads the **committed recording** with ``pyarrow.ipc.open_file`` and feeds
its real Arrow schema through the comparison core. That is a deliberate narrowing of a
replayed Snowflake check: this repo has no Snowflake *introspection* cassette
(``tests/type_fidelity_probe.py`` says so verbatim), so a replayed end-to-end CLI ``--check``
on Snowflake is not runnable. The recording carries the result-schema half, which is exactly
what the comparison core consumes.

There is deliberately **no Databricks test**: the driver has no ``ExecuteSchema``, and its
zero-row wrapper, measured working against a live metric view on 2026-08-15, cannot be
re-checked from a cassette, because replay serves the schema whatever the driver would do.
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING, Any, Literal

import pytest
from duckdb_extension import load_semantic_views

from semolina.codegen.annotation_check import (
    ABSENT,
    ROUTE_METADATA,
    STATUS_DRIFT,
    STATUS_MATCH,
    _result_field_names,
    check_view,
)
from semolina.codegen.introspector import IntrospectedField, IntrospectedView
from semolina.codegen.model_reader import CommittedField, CommittedModel
from semolina.codegen.probe import ROUTE_EXECUTE_SCHEMA
from semolina.engines.base import Engine
from semolina.engines.sql import DatabricksDialect, DuckDBDialect, SnowflakeDialect, SQLDialect

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping
    from pathlib import Path

    import pyarrow
    from s_layer_semantic_formats import SLayerBlock

pytest.importorskip("adbc_driver_duckdb")

PROBE_VIEW = "type_fidelity_view"
"""The in-memory probe view: seven metrics, one dimension, no facts."""

DECIMAL_METRIC = "total_order_value"
"""``SUM(DECIMAL(10,2))``: the field whose probed type is ``decimal128(38, 2)``."""


@pytest.fixture
def probe_engine() -> Generator[Engine]:
    """
    Yield the type-fidelity probe's in-memory DuckDB engine, disposing it on teardown.

    Mirrors the fixture of the same name in ``tests/unit/test_type_fidelity_duckdb.py``,
    which owns the record/replay contract this module inherits.
    """
    from type_fidelity_probe import make_probe_engine

    engine = make_probe_engine()
    yield engine
    engine.dispose()


@pytest.fixture
def sales_engine(duckdb_file_backed_db: Path) -> Generator[Engine]:
    """
    Yield an engine on the file-backed ``sales_view``, which carries facts AND metrics.

    That combination is what ``DuckDBSQLBuilder.build_select_with_params`` refuses in one
    query, so this fixture is the one that exercises the two-probe merge.
    """
    from adbc_poolhouse import DuckDBConfig

    from semolina.config import create_engine

    engine = create_engine(DuckDBConfig(database=str(duckdb_file_backed_db), read_only=True))
    yield engine
    engine.dispose()


def committed_from_warehouse(engine: Engine, view_name: str) -> CommittedModel:
    """
    Build the committed model the generation path would write for a view, right now.

    Renders the view through the real generation path and reads the result back with the
    real parser, so the fixture cannot go stale by hand and the round trip is proven.

    Args:
        engine: A live engine.
        view_name: The view to generate for.

    Returns:
        The parsed committed model.
    """
    import tempfile
    from pathlib import Path as _Path

    from semolina.codegen.model_reader import read_committed_model
    from semolina.codegen.python_renderer import render_and_format

    source = render_and_format([engine.introspect(view_name)])
    with tempfile.TemporaryDirectory() as tmp:
        path = _Path(tmp) / "models.py"
        path.write_text(source, encoding="utf-8")
        return read_committed_model(path)[0]


def replace_annotation(model: CommittedModel, name: str, annotation: str) -> CommittedModel:
    """
    Return a copy of ``model`` with one field's annotation replaced.

    Args:
        model: The parsed committed model.
        name: The field to edit.
        annotation: The annotation to write in its place.

    Returns:
        A new model carrying the edited field.
    """
    fields = dict(model.fields)
    fields[name] = CommittedField(
        name=name,
        field_class=fields[name].field_class,
        annotation=annotation,
        source_name=fields[name].source_name,
    )
    return CommittedModel(class_name=model.class_name, view_name=model.view_name, fields=fields)


def row_named(report: Any, name: str) -> Any:
    """
    Return the report row for a field, failing the test if it is absent.

    Args:
        report: A ``ViewCheckReport``.
        name: The field name.

    Returns:
        The matching ``FieldCheckRow``.
    """
    for row in report.rows:
        if row.name == name:
            return row
    pytest.fail(f"No row for {name!r}; rows were {[r.name for r in report.rows]}")


# ---------------------------------------------------------------------------
# Live DuckDB
# ---------------------------------------------------------------------------


class TestLiveDuckDB:
    """The end-to-end comparison, against a real in-memory DuckDB semantic view."""

    def test_a_freshly_generated_model_reports_no_drift(self, probe_engine: Engine) -> None:
        """
        The worked example: generate, then check, on the same view.

        For this view the metadata route and the probe route agree on every field, so
        nothing drifts. That is a measurement, not an assumption — see
        :meth:`TestMetadataProbeDivergence.test_an_interval_column_drifts` for a live view
        where they do not.
        """
        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)

        report = check_view(probe_engine, PROBE_VIEW, committed)

        assert report.has_drift is False, [
            (r.name, r.committed, r.probed) for r in report.rows if r.status == STATUS_DRIFT
        ]
        assert {r.status for r in report.rows} == {STATUS_MATCH}

    def test_every_row_carries_the_route_that_produced_it(self, probe_engine: Engine) -> None:
        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)

        report = check_view(probe_engine, PROBE_VIEW, committed)

        assert {r.route for r in report.rows} == {ROUTE_EXECUTE_SCHEMA}

    def test_a_drifted_annotation_is_reported(self, probe_engine: Engine) -> None:
        committed = replace_annotation(
            committed_from_warehouse(probe_engine, PROBE_VIEW), DECIMAL_METRIC, "int | None"
        )

        report = check_view(probe_engine, PROBE_VIEW, committed)

        row = row_named(report, DECIMAL_METRIC)
        assert report.has_drift is True
        assert row.committed == "int | None"
        assert row.probed == "decimal.Decimal | None"
        assert row.status == STATUS_DRIFT

    def test_a_wrong_type_map_entry_is_caught_by_the_probe(
        self, probe_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``--check`` measures the result schema; it does not ask the type map a second time.

        The type map is made wrong for DECIMAL, and the committed model is generated through
        it, so the model says ``float``. The probe reads the driver's own Arrow type and must
        still say ``decimal.Decimal``. A probe that derived its answer from the type map would
        agree with the wrong model and report no drift, and ``--check`` could then never catch
        a type-map mistake, which is the job it exists to do.
        """
        import semolina.codegen.type_map as type_map

        real = type_map.duckdb_type_to_python

        def decimal_reads_as_float(sql_type: str) -> str | None:
            if sql_type.upper().startswith("DECIMAL"):
                return "float"
            return real(sql_type)

        monkeypatch.setattr(type_map, "duckdb_type_to_python", decimal_reads_as_float)
        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)

        report = check_view(probe_engine, PROBE_VIEW, committed)

        row = row_named(report, DECIMAL_METRIC)
        assert row.committed == "float | None"
        assert row.probed == "decimal.Decimal | None"
        assert row.status == STATUS_DRIFT

    def test_a_metric_gets_nullability_and_a_dimension_does_not(self, probe_engine: Engine) -> None:
        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)

        report = check_view(probe_engine, PROBE_VIEW, committed)

        assert row_named(report, DECIMAL_METRIC).probed == "decimal.Decimal | None"
        assert row_named(report, "region").probed == "str"

    def test_a_field_missing_from_the_committed_model_is_drift(self, probe_engine: Engine) -> None:
        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)
        fields = dict(committed.fields)
        del fields["region"]
        trimmed = CommittedModel(
            class_name=committed.class_name, view_name=committed.view_name, fields=fields
        )

        report = check_view(probe_engine, PROBE_VIEW, trimmed)

        row = row_named(report, "region")
        assert row.committed == ABSENT
        assert row.probed == "str"
        assert row.status == STATUS_DRIFT
        assert report.has_drift is True

    def test_a_field_absent_from_the_warehouse_is_drift(self, probe_engine: Engine) -> None:
        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)
        fields = dict(committed.fields)
        fields["ghost"] = CommittedField(
            name="ghost", field_class="Metric", annotation="int | None", source_name=None
        )
        widened = CommittedModel(
            class_name=committed.class_name, view_name=committed.view_name, fields=fields
        )

        report = check_view(probe_engine, PROBE_VIEW, widened)

        row = row_named(report, "ghost")
        assert row.committed == "int | None"
        assert row.probed == ABSENT
        assert row.status == STATUS_DRIFT
        assert report.has_drift is True

    def test_an_unmapped_type_compares_as_any(self, probe_engine: Engine) -> None:
        """``region_list`` is ``list(o.region)``; neither map has an answer, so both say Any."""
        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)

        report = check_view(probe_engine, PROBE_VIEW, committed)

        row = row_named(report, "region_list")
        assert row.committed == "Any | None"
        assert row.probed == "Any | None"
        assert row.status == STATUS_MATCH


class TestAnUntypedMetric:
    """
    ``Metric()`` is ``Metric[Any]()``, and ``Any | None`` is ``Any`` (GEN-09, A32).

    The reader records the documented shorthand as ``Any``; the probe's answer for a metric
    gets ``| None`` (Decision 2). For a column neither map types, both sides mean "anything",
    and the check reported drift on every run anyway.
    """

    def test_an_untyped_metric_on_an_untyped_column_is_not_drift(
        self, probe_engine: Engine
    ) -> None:
        """``region_list`` is unmapped, so codegen writes ``Any | None``; ``Metric()`` agrees."""
        committed = replace_annotation(
            committed_from_warehouse(probe_engine, PROBE_VIEW), "region_list", "Any"
        )

        report = check_view(probe_engine, PROBE_VIEW, committed)

        row = row_named(report, "region_list")
        assert row.committed == "Any"
        assert row.status == STATUS_MATCH
        assert report.has_drift is False

    def test_an_untyped_metric_on_a_typed_column_is_still_drift(self, probe_engine: Engine) -> None:
        """
        The fix equates two spellings of ``Any``; it does not make ``Any`` a wildcard.

        A guard rather than a red: it passed before the fix, and pins that the fix did not
        widen into "an untyped field never drifts", which would hide a column codegen can
        now type -- the stance ``codegen-dto --check`` takes for its own ``Any`` (D5).
        """
        committed = replace_annotation(
            committed_from_warehouse(probe_engine, PROBE_VIEW), DECIMAL_METRIC, "Any"
        )

        report = check_view(probe_engine, PROBE_VIEW, committed)

        assert row_named(report, DECIMAL_METRIC).status == STATUS_DRIFT


class TestTheRoleAndTheColumnAreComparedToo:
    """
    An annotation is not the only thing a committed model can get wrong about a field.

    ``CommittedField`` already carries ``field_class`` and ``source_name``; both change the
    SQL the model builds. A role change puts the field in the wrong ``semantic_view()``
    clause, and a stale ``source=`` selects a column that no longer exists. Neither shows up
    as an annotation difference, so neither is caught by comparing annotations alone.
    """

    def test_a_metric_committed_as_a_dimension_is_drift(self, probe_engine: Engine) -> None:
        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)
        fields = dict(committed.fields)
        original = fields[DECIMAL_METRIC]
        fields[DECIMAL_METRIC] = CommittedField(
            name=original.name,
            # The annotation is left exactly as generated, so the only difference is the
            # role. Annotation-only comparison cannot see this.
            field_class="Dimension",
            annotation=original.annotation,
            source_name=original.source_name,
        )
        rerolled = CommittedModel(
            class_name=committed.class_name, view_name=committed.view_name, fields=fields
        )

        report = check_view(probe_engine, PROBE_VIEW, rerolled)

        row = row_named(report, DECIMAL_METRIC)
        assert row.status == STATUS_DRIFT
        assert "Dimension" in row.detail and "Metric" in row.detail
        assert report.has_drift is True

    def test_a_stale_source_override_is_drift(self, probe_engine: Engine) -> None:
        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)
        fields = dict(committed.fields)
        original = fields["region"]
        fields["region"] = CommittedField(
            name=original.name,
            field_class=original.field_class,
            annotation=original.annotation,
            source_name="RENAMED_LAST_QUARTER",
        )
        stale = CommittedModel(
            class_name=committed.class_name, view_name=committed.view_name, fields=fields
        )

        report = check_view(probe_engine, PROBE_VIEW, stale)

        row = row_named(report, "region")
        assert row.status == STATUS_DRIFT
        assert "RENAMED_LAST_QUARTER" in row.detail
        assert report.has_drift is True

    def test_an_explicit_source_that_matches_the_default_is_not_drift(
        self, probe_engine: Engine
    ) -> None:
        """
        The comparison is on the *resolved* column name, not on the raw override.

        ``docs/src/how-to/codegen.rst`` documents ``source=`` as something you may add by
        hand, and the warehouse reports ``source_name=None`` for a column whose name already
        round-trips. Comparing the raw overrides would report drift for a model that builds
        byte-identical SQL, which would make ``--check`` fight a documented workflow.
        """
        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)
        fields = dict(committed.fields)
        original = fields["region"]
        assert original.source_name is None, "fixture assumption: `region` needs no override"
        fields["region"] = CommittedField(
            name=original.name,
            field_class=original.field_class,
            annotation=original.annotation,
            source_name=probe_engine.dialect.normalize_identifier("region"),
        )
        explicit = CommittedModel(
            class_name=committed.class_name, view_name=committed.view_name, fields=fields
        )

        report = check_view(probe_engine, PROBE_VIEW, explicit)

        assert row_named(report, "region").status == STATUS_MATCH

    def test_a_renamed_attribute_reading_the_same_column_matches_and_says_so(
        self, probe_engine: Engine
    ) -> None:
        """
        M5: ``sales_region = Dimension[str](source="region")`` answers for ``region``.

        The row is named after the warehouse field and matches, and its detail names the
        attribute the model declares, so the report still shows what the file says.
        """
        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)
        fields = {k: v for k, v in committed.fields.items() if k != "region"}
        original = committed.fields["region"]
        fields["sales_region"] = CommittedField(
            name="sales_region",
            field_class=original.field_class,
            annotation=original.annotation,
            source_name="region",
        )
        renamed = CommittedModel(
            class_name=committed.class_name, view_name=committed.view_name, fields=fields
        )

        report = check_view(probe_engine, PROBE_VIEW, renamed)

        row = row_named(report, "region")
        assert row.status == STATUS_MATCH
        assert "sales_region" in row.detail
        assert "sales_region" not in {r.name for r in report.rows}
        assert report.has_drift is False

    def test_a_renamed_attribute_reading_another_column_is_still_drift(
        self, probe_engine: Engine
    ) -> None:
        """A rename is matched by its column, so a rename with no ``source=`` matches nothing."""
        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)
        fields = {k: v for k, v in committed.fields.items() if k != "region"}
        original = committed.fields["region"]
        fields["sales_region"] = CommittedField(
            name="sales_region",
            field_class=original.field_class,
            annotation=original.annotation,
            source_name=None,
        )
        renamed = CommittedModel(
            class_name=committed.class_name, view_name=committed.view_name, fields=fields
        )

        report = check_view(probe_engine, PROBE_VIEW, renamed)

        assert row_named(report, "region").committed == ABSENT
        assert row_named(report, "sales_region").status == STATUS_DRIFT

    def test_two_renames_of_one_column_match_only_the_first(self, probe_engine: Engine) -> None:
        """One warehouse field answers for one attribute; the second rename stays its own row."""
        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)
        fields = {k: v for k, v in committed.fields.items() if k != "region"}
        original = committed.fields["region"]
        for name in ("sales_region", "other_region"):
            fields[name] = CommittedField(
                name=name,
                field_class=original.field_class,
                annotation=original.annotation,
                source_name="region",
            )
        renamed = CommittedModel(
            class_name=committed.class_name, view_name=committed.view_name, fields=fields
        )

        report = check_view(probe_engine, PROBE_VIEW, renamed)

        assert "sales_region" in row_named(report, "region").detail
        assert "other_region" not in row_named(report, "region").detail
        assert "other_region" in {r.name for r in report.rows}

    def test_a_rename_of_a_column_declared_by_name_is_not_paired(
        self, probe_engine: Engine
    ) -> None:
        """
        ``region`` declared under its own name wins; the rename is not read as it.

        The rename comes first in the file, so it is seen before ``region`` is matched by
        name: only the check against the declared names keeps it from claiming the column.
        """
        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)
        original = committed.fields["region"]
        fields = {
            "sales_region": CommittedField(
                name="sales_region",
                field_class=original.field_class,
                annotation=original.annotation,
                source_name="region",
            ),
            **committed.fields,
        }
        renamed = CommittedModel(
            class_name=committed.class_name, view_name=committed.view_name, fields=fields
        )

        report = check_view(probe_engine, PROBE_VIEW, renamed)

        assert row_named(report, "region").status == STATUS_MATCH
        assert "sales_region" not in row_named(report, "region").detail
        assert "sales_region" in {r.name for r in report.rows}

    def test_a_matching_row_carries_no_detail(self, probe_engine: Engine) -> None:
        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)

        report = check_view(probe_engine, PROBE_VIEW, committed)

        assert {r.detail for r in report.rows} == {""}


class TestFactsAndMetricsAreTwoProbes:
    """``sales_view`` carries a fact and two metrics, which one query cannot select."""

    def test_the_builder_still_refuses_the_combined_query(self, sales_engine: Engine) -> None:
        """
        The reason two probes exist, asserted rather than assumed.

        If this ever stops raising, the two-probe split becomes dead weight and should be
        revisited — so the guard fails loudly instead of silently over-engineering.
        """
        from semolina.codegen.annotation_check import (
            _canonical_model,
        )

        view = sales_engine.introspect("sales_view")
        model = _canonical_model(view)
        builder = sales_engine.dialect.create_builder()
        query = (
            model.query()
            .metrics(*(getattr(model, f.name) for f in view.fields if f.field_type == "metric"))
            .dimensions(*(getattr(model, f.name) for f in view.fields if f.field_type != "metric"))
        )

        with pytest.raises(ValueError, match="facts and metrics"):
            builder.build_select_with_params(query)

    def test_check_view_completes_and_probes_both_groups(self, sales_engine: Engine) -> None:
        committed = committed_from_warehouse(sales_engine, "sales_view")

        report = check_view(sales_engine, "sales_view", committed)

        assert report.has_drift is False, [
            (r.name, r.committed, r.probed) for r in report.rows if r.status == STATUS_DRIFT
        ]
        # The fact comes from the second probe, the metrics from the first.
        assert row_named(report, "unit_price").probed == "int"
        # SUM over an INTEGER column is a HUGEINT, which arrives as decimal128(38, 0).
        assert row_named(report, "revenue").probed == "decimal.Decimal | None"
        assert {r.route for r in report.rows} == {ROUTE_EXECUTE_SCHEMA}

    def test_each_group_reports_the_route_that_answered_it(
        self, sales_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The route labels the row, not the last query the loop happened to run.

        DuckDB needs two probes for this view, and a driver is free to answer one and refuse
        the other — which is exactly the Snowflake shape, where a parameter-free query gets
        ``ExecuteSchema`` and a parameterized one does not. A single carried-forward route
        makes the report claim something about a query that never touched the row.
        """
        from semolina.codegen.probe import ROUTE_ZERO_ROW, ProbeResult
        from semolina.codegen.probe import probe_schema as real_probe_schema

        calls = {"n": 0}

        def alternating(cursor: Any, sql: str, params: list[Any]) -> ProbeResult:
            probed = real_probe_schema(cursor, sql, params)
            calls["n"] += 1
            # First group answered honestly; second group relabelled as the fallback route.
            route = ROUTE_EXECUTE_SCHEMA if calls["n"] == 1 else ROUTE_ZERO_ROW
            return ProbeResult(schema=probed.schema, route=route)

        monkeypatch.setattr("semolina.codegen.annotation_check.probe_schema", alternating)
        committed = committed_from_warehouse(sales_engine, "sales_view")

        report = check_view(sales_engine, "sales_view", committed)

        assert calls["n"] == 2, "fixture assumption: this view needs two probes"
        # The metrics came from the first probe and the fact from the second.
        assert row_named(report, "revenue").route == ROUTE_EXECUTE_SCHEMA
        assert row_named(report, "unit_price").route == ROUTE_ZERO_ROW


class TestADuplicateColumnNameIsNotAnAbsentOne:
    """
    ``pyarrow.Schema.get_field_index`` answers ``-1`` for *missing* and for *ambiguous*.

    A result schema carrying two columns of the same name is not a schema that lacks the
    column, but the ``index >= 0`` test cannot tell them apart — so the field silently took
    the metadata route and the CLI printed "the result-schema probe was unavailable", which
    is not what happened.
    """

    def test_two_columns_of_the_same_name_report_as_ambiguous_not_absent(self) -> None:
        import pyarrow as pa

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[IntrospectedField(name="country", field_type="dimension", data_type="str")],
        )
        schema = pa.schema([("COUNTRY", pa.string()), ("COUNTRY", pa.int64())])
        engine = _RecordedEngine(view, schema)
        committed = CommittedModel(
            class_name="SalesView",
            view_name="sales_view",
            fields={
                "country": CommittedField(
                    name="country", field_class="Dimension", annotation="str", source_name=None
                )
            },
        )

        report = check_view(engine, "sales_view", committed)

        row = row_named(report, "country")
        assert row.route == ROUTE_EXECUTE_SCHEMA, "the probe answered; it was not unavailable"
        assert row.probed == "Any"
        assert "ambiguous" in row.detail
        assert report.probe_error is None

    def test_a_genuinely_absent_column_still_takes_the_metadata_route(self) -> None:
        import pyarrow as pa

        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[IntrospectedField(name="country", field_type="dimension", data_type="str")],
        )
        schema = pa.schema([("SOMETHING_ELSE", pa.string())])
        engine = _RecordedEngine(view, schema)

        report = check_view(engine, "sales_view", None)

        row = row_named(report, "country")
        assert row.route == ROUTE_METADATA
        assert row.probed == "str"


class TestTheCandidateNamesMatchWhatTheWarehouseReturns:
    """
    A candidate list that never matches sends the field down the metadata route in silence.

    ``_arrow_annotation`` is first-match-wins over these names, so a field whose real result
    column is absent from the list reads as "the result-schema probe was unavailable" in the
    ``--check`` report -- a true statement about the wrong thing. Since phase 53 every
    builder aliases each selected column to its field name, so that name has to lead the
    list on every dialect and for every role.
    """

    @pytest.mark.parametrize(
        "dialect",
        [
            pytest.param(SnowflakeDialect(), id="snowflake"),
            pytest.param(DatabricksDialect(), id="databricks"),
            pytest.param(DuckDBDialect(), id="duckdb"),
        ],
    )
    @pytest.mark.parametrize("field_type", ["metric", "dimension", "fact"])
    def test_the_field_name_is_the_first_candidate(
        self, dialect: SQLDialect, field_type: Literal["metric", "dimension", "fact"]
    ) -> None:
        """
        ``revenue`` comes back as ``revenue`` on every backend, so it is tried first.

        Measured in the cassettes 53-04 re-recorded live: Snowflake and Databricks return
        ``revenue`` and ``country``, and DuckDB always did.
        """
        field = IntrospectedField(name="revenue", field_type=field_type, data_type=None)

        assert _result_field_names(dialect, field)[0] == "revenue"

    def test_the_pre_53_databricks_metric_spelling_is_no_longer_offered(self) -> None:
        """
        ``measure(revenue)`` came from ``metric_result_column_name``, deleted with the recordings.

        Databricks returned that spelling only for a metric selected without an alias, which
        no builder emits since phase 53 (D2-5). Offering it would let a stale schema bind a
        field to a column today's query never returns.
        """
        field = IntrospectedField(name="revenue", field_type="metric", data_type=None)

        assert "measure(revenue)" not in _result_field_names(DatabricksDialect(), field)

    def test_the_snowflake_and_duckdb_metric_candidates_are_undisturbed(self) -> None:
        """
        The SELECT-clause spellings stay as fallbacks behind the field name.

        ``AGG("REVENUE")`` is ``wrap_metric``'s spelling, not a result-column rule, and a
        committed pre-53 Snowflake schema still carries it; DuckDB's ``semantic_view()``
        returns the bare field name.
        """
        field = IntrospectedField(name="revenue", field_type="metric", data_type=None)

        assert 'AGG("REVENUE")' in _result_field_names(SnowflakeDialect(), field)
        assert "revenue" in _result_field_names(DuckDBDialect(), field)


class TestAnUnprobedRowSaysSo:
    """A field no probe examined must not borrow a probe's route."""

    def test_a_committed_only_field_is_labelled_not_probed(self, probe_engine: Engine) -> None:
        from semolina.codegen.annotation_check import ROUTE_NOT_PROBED

        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)
        fields = dict(committed.fields)
        fields["ghost"] = CommittedField(
            name="ghost", field_class="Metric", annotation="int | None", source_name=None
        )
        widened = CommittedModel(
            class_name=committed.class_name, view_name=committed.view_name, fields=fields
        )

        report = check_view(probe_engine, PROBE_VIEW, widened)

        row = row_named(report, "ghost")
        assert row.probed == ABSENT
        assert row.route == ROUTE_NOT_PROBED
        assert row.route != ROUTE_EXECUTE_SCHEMA


class TestMetadataProbeDivergence:
    """Where the two routes disagree, ``--check`` surfaces it rather than hiding it."""

    @pytest.fixture
    def interval_engine(self, tmp_path_factory: pytest.TempPathFactory) -> Generator[Engine]:
        """
        Yield an engine on a semantic view carrying an ``INTERVAL`` fact.

        ``_DUCKDB_TYPE_MAP['INTERVAL']`` still says ``datetime.timedelta`` and is known
        wrong; the Arrow map answers ``None`` for
        ``month_day_nano_interval`` deliberately rather than reproduce it. This view is where
        that disagreement becomes a live, runnable ``--check`` result.
        """
        import duckdb
        from adbc_poolhouse import DuckDBConfig

        from semolina.config import create_engine

        db_path = tmp_path_factory.mktemp("interval_fixture") / "interval.db"
        conn = duckdb.connect(database=str(db_path))
        try:
            load_semantic_views(conn)
            conn.execute("CREATE TABLE spans (id INTEGER, span INTERVAL, region VARCHAR)")
            conn.execute("INSERT INTO spans VALUES (1, INTERVAL 3 DAY, 'US')")
            conn.execute(
                "CREATE SEMANTIC VIEW span_view AS "
                "TABLES (t AS spans PRIMARY KEY (id)) "
                "FACTS (t.span AS span) "
                "DIMENSIONS (t.region AS region)"
            )
        finally:
            conn.close()

        engine = create_engine(DuckDBConfig(database=str(db_path), read_only=True))
        yield engine
        engine.dispose()

    def test_an_interval_column_drifts(self, interval_engine: Engine) -> None:
        committed = committed_from_warehouse(interval_engine, "span_view")

        report = check_view(interval_engine, "span_view", committed)

        row = row_named(report, "span")
        assert row.committed == "datetime.timedelta"
        assert row.probed == "Any"
        assert row.status == STATUS_DRIFT
        assert report.has_drift is True


class TestMetadataFallback:
    """A ``--check`` that could not probe must say so, not quietly pass."""

    def test_metadata_fallback_is_labelled_on_every_row(
        self, probe_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _refuse(cursor: Any, sql: str, params: list[Any]) -> Any:
            raise RuntimeError("probe unavailable in this test")

        monkeypatch.setattr("semolina.codegen.annotation_check.probe_schema", _refuse)
        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)

        report = check_view(probe_engine, PROBE_VIEW, committed)

        assert {r.route for r in report.rows} == {ROUTE_METADATA}
        assert report.probe_error is not None
        assert "probe unavailable" in report.probe_error

    def test_metadata_fallback_still_compares(
        self, probe_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Falling back is not the same as giving up: the comparison still runs."""

        def _refuse(cursor: Any, sql: str, params: list[Any]) -> Any:
            raise RuntimeError("probe unavailable in this test")

        monkeypatch.setattr("semolina.codegen.annotation_check.probe_schema", _refuse)
        committed = replace_annotation(
            committed_from_warehouse(probe_engine, PROBE_VIEW), DECIMAL_METRIC, "int | None"
        )

        report = check_view(probe_engine, PROBE_VIEW, committed)

        assert report.has_drift is True
        assert row_named(report, DECIMAL_METRIC).probed == "decimal.Decimal | None"

    def test_probe_setup_failing_falls_back_rather_than_raising(self) -> None:
        """
        The fallback covers *setting up* the probe, not only running it.

        ``_probe_view``'s ``except Exception`` carries the promise that "any probe failure is
        a fallback, not a crash". Building the runtime model is part of the probe, and a
        catalogue column named ``query`` makes ``SemanticViewMeta`` reject the name — which
        is a warehouse-shaped input, not a bug in the caller. Left outside the ``try`` it
        escapes ``check_view``, escapes ``_run_check``'s three narrow ``except`` clauses, and
        produces exactly the traceback the design says it is avoiding.
        """
        view = IntrospectedView(
            view_name="sales_view",
            class_name="SalesView",
            fields=[
                IntrospectedField(name="query", field_type="dimension", data_type="str"),
                IntrospectedField(name="country", field_type="dimension", data_type="str"),
            ],
        )
        engine = _RecordedEngine(view, _recorded_schema())
        committed = CommittedModel(
            class_name="SalesView",
            view_name="sales_view",
            fields={
                "query": CommittedField(
                    name="query", field_class="Dimension", annotation="str", source_name=None
                )
            },
        )

        report = check_view(engine, "sales_view", committed)

        assert {r.route for r in report.rows} == {ROUTE_METADATA}
        assert report.probe_error is not None
        assert "reserved" in report.probe_error
        assert row_named(report, "query").probed == "str"


class TestTheReportCarriesNoRowValues:
    """A ``--check`` report is likely to land in a CI log."""

    def test_no_report_field_holds_anything_but_names_types_and_routes(
        self, probe_engine: Engine
    ) -> None:
        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)
        annotations = {r.committed for r in check_view(probe_engine, PROBE_VIEW, committed).rows}

        # Every seeded value in the probe fixture, none of which may appear anywhere in the
        # report. `30.75`, `100.00` and `US`/`MX`/`CA` are the rows of PROBE_SEED_DML.
        report = check_view(probe_engine, PROBE_VIEW, committed)
        rendered = " ".join(
            f"{r.name} {r.committed} {r.probed} {r.route} {r.status}" for r in report.rows
        )
        for value in ("30.75", "12.50", "100.00", "'US'", "'MX'"):
            assert value not in rendered
        assert annotations  # the report is non-empty, so the assertion above is not vacuous

    @pytest.mark.usefixtures("data_fetch_guard")
    def test_check_view_fetches_no_data_rows(self, probe_engine: Engine) -> None:
        """
        With every fetch of a non-catalogue statement poisoned, ``check_view`` completes.

        "Without executing a query for rows", made runnable — and scoped to what
        it can mean. ``check_view`` calls ``engine.introspect()``, which fetches *catalogue*
        rows from ``DESCRIBE SEMANTIC VIEW`` exactly as ``semolina codegen`` already does.
        The guarantee is about the view's **data**: nothing ever fetches from the
        ``semantic_view(...)`` SELECT the probe resolves a schema for.
        """
        committed = committed_from_warehouse(probe_engine, PROBE_VIEW)

        assert check_view(probe_engine, PROBE_VIEW, committed).has_drift is False

    @pytest.mark.usefixtures("data_fetch_guard")
    def test_the_fetch_guard_is_not_vacuous(self, probe_engine: Engine) -> None:
        """Fetching from the probe's own query under the guard must fail."""
        from type_fidelity_probe import probe_sql_all

        sql, params = probe_sql_all()

        with probe_engine.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params or None)
            with pytest.raises(AssertionError, match="fetched data rows"):
                cursor.fetch_arrow_table()

    @pytest.mark.usefixtures("data_fetch_guard")
    def test_the_record_batch_guard_is_not_vacuous(self, probe_engine: Engine) -> None:
        """
        The reader ``fetch_record_batch`` hands back is guarded too.

        ``probe_schema``'s fallback branch is the only route Databricks can take and the one
        Snowflake takes with bound parameters, and it reaches rows through
        ``cursor.fetch_record_batch()`` — a method the guard did not wrap. The *call* has to
        succeed, because the fallback reads ``reader.schema`` off it; what must not succeed
        is pulling a batch out of the reader.
        """
        from type_fidelity_probe import probe_sql_all

        sql, params = probe_sql_all()

        with probe_engine.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params or None)
            reader = cursor.fetch_record_batch()
            try:
                assert reader.schema is not None, "the schema read must still work"
                with pytest.raises(AssertionError, match="fetched data rows"):
                    reader.read_all()
            finally:
                reader.close()

    @pytest.mark.usefixtures("data_fetch_guard")
    def test_the_zero_row_fallback_route_runs_under_the_guard(
        self, probe_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Drive ``probe_schema`` down its fallback branch, so the branch is exercised at all.

        Every other test in this module runs on DuckDB, which answers
        ``adbc_execute_schema`` — so without forcing the refusal the fallback is never under
        the guard on any backend.
        """
        import adbc_driver_manager
        import adbc_driver_manager.dbapi
        from type_fidelity_probe import probe_sql_all

        from semolina.codegen.probe import ROUTE_ZERO_ROW, probe_schema

        def refuse(self: Any, *args: Any, **kwargs: Any) -> Any:
            raise adbc_driver_manager.NotSupportedError("ExecuteSchema not implemented")

        cursor_cls: Any = adbc_driver_manager.dbapi.Cursor
        monkeypatch.setattr(cursor_cls, "adbc_execute_schema", refuse, raising=True)

        sql, params = probe_sql_all()
        with probe_engine.connect() as conn:
            cursor = conn.cursor()
            try:
                probed = probe_schema(cursor, sql, params)
            finally:
                cursor.close()

        assert probed.route == ROUTE_ZERO_ROW
        assert probed.schema.names


class TestCatalogueNamesReachSqlEscaped:
    """
    ``--check`` is the first path that puts the warehouse's own answer back into SQL.

    Before this mode, a ``source_name`` reaching ``DuckDBSQLBuilder`` came from a
    ``source="..."`` literal the user typed into their own model file.
    :func:`_canonical_model` takes it from ``DESCRIBE SELECT`` / ``SHOW COLUMNS`` output
    instead, which turns a quoting bug in the builder into a second-order injection with a
    real source. This test walks the shipped chain rather than the builder alone.
    """

    def test_a_catalogue_source_name_with_a_quote_stays_inside_its_literal(self) -> None:
        import re

        from semolina.codegen.annotation_check import _build_query, _canonical_model, _field_groups
        from semolina.engines.sql import DuckDBDialect, DuckDBSQLBuilder

        payload = "x') FROM read_csv('/etc/passwd') --"
        view = IntrospectedView(
            view_name="v",
            class_name="V",
            fields=[
                IntrospectedField(
                    name="country", field_type="dimension", data_type="str", source_name=payload
                )
            ],
        )

        builder = DuckDBSQLBuilder(DuckDBDialect())
        model = _canonical_model(view)
        groups = _field_groups(view, split_facts=True)
        sql, _params = builder.build_select_with_params(_build_query(model, groups[0]))

        assert sql == (
            'SELECT "x\') FROM read_csv(\'/etc/passwd\') --" AS "country"\n'
            "FROM semantic_view('v', "
            "dimensions := ['x'') FROM read_csv(''/etc/passwd'') --'])"
        )
        # The projection holds the name as a double-quoted identifier, which a single quote
        # cannot leave; the literals to check are on the FROM line.
        from_line = sql.split("\n")[1]
        # Strip the string literals — the pattern consumes a doubled `''` as content, which
        # is what the escaper emits — and the statement is the shape it would have had for a
        # well-behaved field name. The payload contributed no SQL of its own. Counting
        # substrings would not show this: the escaped payload still *contains* "FROM".
        assert re.sub(r"'(?:[^']|'')*'", "<literal>", from_line) == (
            "FROM semantic_view(<literal>, dimensions := [<literal>])"
        )


# ---------------------------------------------------------------------------
# Snowflake, from the committed recording
# ---------------------------------------------------------------------------

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
"""
The repository root, anchored on this file rather than on the current working directory.

``tests/integration/test_type_fidelity.py`` and ``tests/type_fidelity_probe.py`` both do
this already. A bare relative path made the whole Snowflake half of this module error out
whenever pytest was run from anywhere but the repo root.
"""

SNOWFLAKE_PROBE_CASSETTE = (
    REPO_ROOT
    / "tests/integration/cassettes/integration/test_type_fidelity"
    / "test_snowflake_probe_copied_cassette"
    / "adbc_driver_snowflake.dbapi"
)
"""The recorded ``sales_view`` query whose result schema this half reads."""

SNOWFLAKE_VIEW = IntrospectedView(
    view_name="sales_view",
    class_name="SalesView",
    fields=[
        IntrospectedField(
            name="revenue",
            field_type="metric",
            data_type="decimal.Decimal",
            raw_type='{"type": "FIXED", "scale": 0}',
        ),
        IntrospectedField(name="country", field_type="dimension", data_type="str"),
    ],
)
"""
The introspection half, derived from the DDL the cassette was recorded against.

``tests/integration/conftest.py`` creates ``revenue NUMBER, country VARCHAR``; a bare
``NUMBER`` is ``NUMBER(38,0)``, which Snowflake reports as ``{"type": "FIXED", "scale": 0}``
and 48-03 maps to ``decimal.Decimal``. Derived rather than read from a warehouse because
**no Snowflake introspection cassette exists** — the narrowing this module's docstring records.
"""


def _recorded_schema() -> pyarrow.Schema:
    """
    Read the Snowflake recording's real result schema.

    Returns:
        The recorded Arrow schema: ``revenue`` as ``decimal128(38, 0)`` and ``country`` as
        ``string``, each column named by its alias (ALIAS-01).
    """
    import pyarrow.ipc

    with pyarrow.ipc.open_file(SNOWFLAKE_PROBE_CASSETTE / "000_result.arrow") as reader:
        return reader.read_all().schema


class _RecordedCursor:
    """A cursor that answers ``adbc_execute_schema`` from the committed recording."""

    def __init__(self, schema: pyarrow.Schema) -> None:
        self._schema = schema

    def adbc_execute_schema(self, sql: str, params: list[Any]) -> pyarrow.Schema:
        return self._schema

    def close(self) -> None:
        return None


class _RecordedConnection:
    """A one-shot connection handing out :class:`_RecordedCursor`."""

    def __init__(self, schema: pyarrow.Schema) -> None:
        self._schema = schema

    def cursor(self) -> _RecordedCursor:
        return _RecordedCursor(self._schema)

    def __enter__(self) -> _RecordedConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _RecordedEngine(Engine):
    """
    A real :class:`~semolina.engines.base.Engine` whose connection replays the recording.

    Subclasses ``Engine`` rather than duck-typing it, which keeps the comparison core honest:
    it may only use the public engine surface. The dialect is a real ``SnowflakeDialect``, so
    the SQL that gets built and the result-column names that get resolved (``revenue``)
    come from the shipped dialect rather than from this test. There is no pool — nothing here
    opens a connection to anything.
    """

    def __init__(self, view: IntrospectedView, schema: pyarrow.Schema) -> None:
        from semolina.engines.sql import SnowflakeDialect

        super().__init__(pool=None, dialect=SnowflakeDialect())
        self._view = view
        self._schema = schema

    def introspect(self, view_name: str) -> IntrospectedView:
        return self._view

    def connect(self) -> _RecordedConnection:
        return _RecordedConnection(self._schema)


def _snowflake_committed(revenue_annotation: str) -> CommittedModel:
    """
    Build a hand-written committed model for the recorded Snowflake view.

    Args:
        revenue_annotation: The annotation to commit for the metric.

    Returns:
        The committed model.
    """
    return CommittedModel(
        class_name="SalesView",
        view_name="sales_view",
        fields={
            "revenue": CommittedField(
                name="revenue",
                field_class="Metric",
                annotation=revenue_annotation,
                source_name=None,
            ),
            "country": CommittedField(
                name="country", field_class="Dimension", annotation="str", source_name=None
            ),
        },
    )


class TestSnowflakeFromTheCommittedRecording:
    """The comparison core over Snowflake's real recorded Arrow types."""

    def test_the_recording_carries_the_types_this_test_relies_on(self) -> None:
        """Guard the fixture: if the recording changes, this says so first."""
        schema = _recorded_schema()

        assert str(schema.field("revenue").type) == "decimal128(38, 0)"
        assert str(schema.field("country").type) == "string"

    def test_a_matching_annotation_reports_no_drift(self) -> None:
        engine = _RecordedEngine(SNOWFLAKE_VIEW, _recorded_schema())

        report = check_view(engine, "sales_view", _snowflake_committed("decimal.Decimal | None"))

        assert report.has_drift is False, [
            (r.name, r.committed, r.probed) for r in report.rows if r.status == STATUS_DRIFT
        ]
        assert row_named(report, "revenue").probed == "decimal.Decimal | None"
        assert row_named(report, "country").probed == "str"
        assert {r.route for r in report.rows} == {ROUTE_EXECUTE_SCHEMA}

    def test_a_drifted_annotation_is_reported(self) -> None:
        engine = _RecordedEngine(SNOWFLAKE_VIEW, _recorded_schema())

        report = check_view(engine, "sales_view", _snowflake_committed("int | None"))

        assert report.has_drift is True
        row = row_named(report, "revenue")
        assert row.committed == "int | None"
        assert row.probed == "decimal.Decimal | None"


class TestMetadataIsComparedToo:
    """
    A committed field's ``description=``/``label=``/``synonyms=`` drift like its annotation.

    Only what the engine reads is compared (``Engine.introspected_metadata``), so a backend
    without a slot for something does not report it as lost.
    """

    @pytest.fixture
    def annotated_engine(self) -> Generator[Engine]:
        """A DuckDB engine with one view carrying a view comment, member comments, synonyms."""
        from adbc_poolhouse import DuckDBConfig

        from semolina.config import create_engine

        engine = create_engine(DuckDBConfig(database=":memory:", pool_size=1))
        with engine.connect() as conn:
            cur = conn.cursor()
            load_semantic_views(cur)
            cur.execute("CREATE TABLE sales_data (id INTEGER, country VARCHAR, amount DOUBLE)")
            cur.execute(
                """
                CREATE SEMANTIC VIEW annotated COMMENT = 'Sales by country' AS
                TABLES (s AS sales_data PRIMARY KEY (id))
                DIMENSIONS (s.country AS s.country WITH SYNONYMS = ('nation', 'land'))
                METRICS (
                    s.revenue AS SUM(s.amount)
                        COMMENT = 'Gross revenue'
                        WITH SYNONYMS = ('sales')
                )
                """
            )
            cur.close()
            conn.commit()
        yield engine
        engine.dispose()

    @staticmethod
    def with_metadata(model: CommittedModel, name: str, **metadata: object) -> CommittedModel:
        """Return ``model`` with one field's metadata arguments replaced by ``metadata``."""
        import dataclasses

        fields = dict(model.fields)
        fields[name] = dataclasses.replace(fields[name], metadata=metadata)
        return dataclasses.replace(model, fields=fields)

    def test_a_freshly_generated_model_round_trips_its_metadata(
        self, annotated_engine: Engine
    ) -> None:
        committed = committed_from_warehouse(annotated_engine, "annotated")

        assert committed.description == "Sales by country"
        assert committed.fields["revenue"].metadata == {
            "description": "Gross revenue",
            "synonyms": ("sales",),
        }
        report = check_view(annotated_engine, "annotated", committed)
        assert report.has_drift is False, report
        assert report.detail == ""

    def test_a_changed_description_is_drift(self, annotated_engine: Engine) -> None:
        committed = committed_from_warehouse(annotated_engine, "annotated")
        stale = self.with_metadata(
            committed, "revenue", description="Net revenue", synonyms=("sales",)
        )

        row = row_named(check_view(annotated_engine, "annotated", stale), "revenue")

        assert row.status == STATUS_DRIFT
        assert "description" in row.detail and "Net revenue" in row.detail

    def test_a_missing_synonym_is_drift(self, annotated_engine: Engine) -> None:
        committed = committed_from_warehouse(annotated_engine, "annotated")
        stale = self.with_metadata(committed, "country", synonyms=("nation",))

        row = row_named(check_view(annotated_engine, "annotated", stale), "country")

        assert row.status == STATUS_DRIFT
        assert "synonyms" in row.detail

    def test_reordered_synonyms_are_not_drift(self, annotated_engine: Engine) -> None:
        committed = committed_from_warehouse(annotated_engine, "annotated")
        reordered = self.with_metadata(committed, "country", synonyms=("land", "nation"))

        row = row_named(check_view(annotated_engine, "annotated", reordered), "country")

        assert row.status == STATUS_MATCH, row.detail

    def test_a_label_is_not_compared_where_the_backend_has_no_slot_for_one(
        self, annotated_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A view that cannot hold a label cannot have drifted from a hand-written ``label=``.

        DuckDB reads labels from tags, which ``semantic_views`` stores from 0.17.0, so a view
        on an earlier extension reports its labels unread.
        """
        import dataclasses

        engine_type = type(annotated_engine)
        introspect = engine_type.introspect

        def untagged(self: Engine, view_name: str) -> IntrospectedView:
            view = introspect(self, view_name)
            return dataclasses.replace(view, unread_metadata=frozenset({"label"}))

        monkeypatch.setattr(engine_type, "introspect", untagged)
        committed = committed_from_warehouse(annotated_engine, "annotated")
        labelled = self.with_metadata(
            committed, "country", label="Country", synonyms=("nation", "land")
        )

        row = row_named(check_view(annotated_engine, "annotated", labelled), "country")

        assert row.status == STATUS_MATCH, row.detail

    def test_a_label_is_compared_where_the_backend_reads_one(
        self, annotated_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import dataclasses

        engine_type = type(annotated_engine)
        introspect = engine_type.introspect

        def tagged(self: Engine, view_name: str) -> IntrospectedView:
            return dataclasses.replace(introspect(self, view_name), unread_metadata=frozenset())

        monkeypatch.setattr(engine_type, "introspect", tagged)
        monkeypatch.setattr(
            engine_type,
            "introspected_metadata",
            frozenset({"description", "label", "synonyms"}),
        )
        committed = committed_from_warehouse(annotated_engine, "annotated")
        labelled = self.with_metadata(
            committed, "country", label="Country", synonyms=("nation", "land")
        )

        row = row_named(check_view(annotated_engine, "annotated", labelled), "country")

        assert row.status == STATUS_DRIFT
        assert "label" in row.detail

    def test_a_freshly_generated_model_round_trips_its_label_and_format(
        self, annotated_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A label and a format written by codegen read back as no drift.

        The format goes out as its normal form through the renderer and comes back through
        the reader's ``literal_eval`` (a nested list stays a list) and ``parse_block``. A
        mismatch anywhere in that chain would show as drift on every freshly generated
        model, on every backend that reads formats.
        """
        import dataclasses

        from s_layer_semantic_formats import from_compact_json

        engine_type = type(annotated_engine)
        introspect = engine_type.introspect
        block = from_compact_json("category", '{"value":{"order":["DE","FR"]}}')

        def tagged(self: Engine, view_name: str) -> IntrospectedView:
            view = introspect(self, view_name)
            fields = [
                dataclasses.replace(f, label="Country", semantic_format=block)
                if f.name == "country"
                else f
                for f in view.fields
            ]
            return dataclasses.replace(view, fields=fields, unread_metadata=frozenset())

        monkeypatch.setattr(engine_type, "introspect", tagged)
        monkeypatch.setattr(
            engine_type,
            "introspected_metadata",
            frozenset({"description", "label", "synonyms", "semantic_format"}),
        )
        committed = committed_from_warehouse(annotated_engine, "annotated")

        assert committed.fields["country"].metadata["label"] == "Country"
        assert "semantic_format" in committed.fields["country"].metadata
        report = check_view(annotated_engine, "annotated", committed)
        assert report.has_drift is False, report
        assert row_named(report, "country").status == STATUS_MATCH

    def test_an_engine_that_reads_no_metadata_compares_none(
        self, annotated_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default for a custom engine: nothing it does not read can drift."""
        monkeypatch.setattr(type(annotated_engine), "introspected_metadata", frozenset())
        committed = committed_from_warehouse(annotated_engine, "annotated")
        bare = self.with_metadata(committed, "revenue")

        report = check_view(annotated_engine, "annotated", bare)

        assert report.has_drift is False, report

    def test_a_changed_view_description_is_drift_on_the_report(
        self, annotated_engine: Engine
    ) -> None:
        import dataclasses

        committed = committed_from_warehouse(annotated_engine, "annotated")
        stale = dataclasses.replace(committed, description="Old words")

        report = check_view(annotated_engine, "annotated", stale)

        assert {row.status for row in report.rows} == {STATUS_MATCH}
        assert report.has_drift is True
        assert "Old words" in report.detail and "Sales by country" in report.detail

    def test_a_model_generated_before_metadata_arguments_drifts(
        self, annotated_engine: Engine, tmp_path: pathlib.Path
    ) -> None:
        """
        The shape codegen wrote before these arguments: docstrings, no synonyms, no view text.

        It no longer describes the warehouse, so it drifts until it is regenerated; the
        docstring is not read as a description.
        """
        from semolina.codegen.model_reader import read_committed_model

        path = tmp_path / "models.py"
        path.write_text(
            "from semolina import Dimension, Metric, SemanticView\n"
            "\n"
            "\n"
            "class Annotated(SemanticView, view='annotated'):\n"
            "    country = Dimension[str]()\n"
            "    revenue = Metric[float | None]()\n"
            '    """Gross revenue"""\n',
            encoding="utf-8",
        )

        report = check_view(annotated_engine, "annotated", read_committed_model(path)[0])

        assert report.has_drift is True
        assert "Sales by country" in report.detail
        assert "synonyms" in row_named(report, "country").detail
        assert "description" in row_named(report, "revenue").detail

    def test_an_unreadable_argument_is_not_compared(
        self, annotated_engine: Engine, tmp_path: pathlib.Path
    ) -> None:
        """The reader warned that it could not evaluate it, so it cannot have drifted."""
        from semolina.codegen.model_reader import read_committed_model_file

        path = tmp_path / "models.py"
        path.write_text(
            "from semolina import Dimension, Metric, SemanticView\n"
            "\n"
            "\n"
            "class Annotated(SemanticView, view='annotated', description=make_description()):\n"
            "    country = Dimension[str](synonyms=make_synonyms())\n"
            "    revenue = Metric[float | None](\n"
            "        description=make_description(), synonyms=('sales',)\n"
            "    )\n",
            encoding="utf-8",
        )
        read = read_committed_model_file(path)

        report = check_view(annotated_engine, "annotated", read.models[0])

        assert len(read.warnings) == 3, read.warnings
        assert report.has_drift is False, report


class TestSemanticFormatIsComparedByNormalForm:
    """``semantic_format=`` against the block the warehouse's tags make."""

    WAREHOUSE_SPEC = '{"value":{"currency":"USD"}}'

    @staticmethod
    def drift(committed: Mapping[str, object], warehouse: SLayerBlock | None) -> str:
        from semolina.codegen.annotation_check import _metadata_drift

        field = IntrospectedField("revenue", "metric", "int", semantic_format=warehouse)
        committed_field = CommittedField(
            name="revenue",
            field_class="Metric",
            annotation="int | None",
            source_name=None,
            metadata=committed,
        )
        return _metadata_drift(frozenset({"semantic_format"}), field, committed_field)

    def warehouse(self) -> SLayerBlock:
        from s_layer_semantic_formats import from_compact_json

        return from_compact_json("currency", self.WAREHOUSE_SPEC)

    def test_the_same_block_spelled_without_defaults_matches(self) -> None:
        committed = {"semantic_format": {"value": {"kind": "currency", "currency": "USD"}}}
        assert self.drift(committed, self.warehouse()) == ""

    def test_the_normal_form_codegen_writes_matches(self) -> None:
        from s_layer_semantic_formats import from_compact_json

        normal = from_compact_json("currency", self.WAREHOUSE_SPEC).normal_form()
        assert self.drift({"semantic_format": normal}, self.warehouse()) == ""

    def test_a_different_block_is_drift(self) -> None:
        committed = {"semantic_format": {"value": {"kind": "currency", "currency": "EUR"}}}
        detail = self.drift(committed, self.warehouse())
        assert detail.startswith("semantic_format: committed ")
        assert "EUR" in detail and "USD" in detail

    def test_a_missing_format_against_a_tagged_member_is_drift(self) -> None:
        assert "committed None" in self.drift({}, self.warehouse())

    def test_a_format_on_an_untagged_member_is_drift(self) -> None:
        committed = {"semantic_format": {"value": {"kind": "count"}}}
        assert "warehouse None" in self.drift(committed, None)

    def test_neither_side_is_not_drift(self) -> None:
        assert self.drift({}, None) == ""

    def test_an_invalid_committed_block_is_drift_not_a_crash(self) -> None:
        committed = {"semantic_format": {"value": {"kind": "bogus"}}}
        assert "committed is not a valid block" in self.drift(committed, self.warehouse())

    @pytest.mark.parametrize("committed", ["currency", 3, ("currency",)])
    def test_a_committed_format_that_is_not_a_mapping_is_drift_not_a_crash(
        self, committed: object
    ) -> None:
        """The reader passes on whatever literal it found, not only a dict."""
        detail = self.drift({"semantic_format": committed}, self.warehouse())
        assert "committed is not a valid block" in detail
