"""Tests for reading a Databricks metric view column's native ``format`` as an S-LAYER block."""

from __future__ import annotations

from typing import Any

import pytest
from s_layer_semantic_formats import validate_block

from semolina.codegen.introspector import semantic_format_from_databricks_format


class TestSemanticFormatFromDatabricksFormat:
    @pytest.mark.parametrize("native", [None, {}])
    def test_no_format_is_no_block(self, native: Any) -> None:
        assert semantic_format_from_databricks_format("m", native, "metric") is None

    @pytest.mark.parametrize(
        ("native", "expected"),
        [
            ({"number": {}}, {"value": {"kind": "number"}}),
            (
                {"currency": {"currency_code": "USD"}},
                {"value": {"kind": "currency", "currency": "USD"}},
            ),
            ({"percentage": {}}, {"value": {"kind": "percentage"}}),
            ({"byte": {}}, {"value": {"kind": "bytes"}}),
        ],
    )
    def test_each_numeric_type_maps_to_its_kind(
        self, native: dict[str, Any], expected: dict[str, Any]
    ) -> None:
        block = semantic_format_from_databricks_format("m", native, "metric")
        assert block == validate_block(expected, "metric")

    @pytest.mark.parametrize(
        ("native", "kind"),
        [({"date": {}}, "date"), ({"date_time": {}}, "datetime")],
    )
    def test_each_temporal_type_maps_to_its_kind(self, native: dict[str, Any], kind: str) -> None:
        block = semantic_format_from_databricks_format("d", native, "dimension")
        assert block == validate_block({"value": {"kind": kind}}, "dimension")

    def test_the_describe_shape_and_the_yaml_shape_read_the_same(self) -> None:
        """``DESCRIBE`` keys the options by type and upper-cases enums; the YAML does neither."""
        described = {
            "currency": {
                "currency_code": "EUR",
                "decimal_places": {"type": "EXACT", "places": 2},
                "abbreviation": "COMPACT",
            }
        }
        yaml = {
            "type": "currency",
            "currency_code": "EUR",
            "decimal_places": {"type": "exact", "places": 2},
            "abbreviation": "compact",
        }
        from_describe = semantic_format_from_databricks_format("m", described, "metric")
        assert from_describe == semantic_format_from_databricks_format("m", yaml, "metric")
        assert from_describe == validate_block(
            {
                "value": {"kind": "currency", "currency": "EUR"},
                "display": {"decimals": [2, 2], "compact": "auto"},
            },
            "metric",
        )

    @pytest.mark.parametrize(
        ("decimal_places", "expected"),
        [
            ({"type": "EXACT", "places": 3}, {"decimals": [3, 3]}),
            ({"type": "MAX", "places": 2}, {"decimals": [0, 2]}),
            ({"type": "ALL"}, {}),
        ],
    )
    def test_decimal_places(self, decimal_places: dict[str, Any], expected: dict[str, Any]) -> None:
        """``all`` (every digit the value has) has no slot, so it sets no decimals."""
        block = semantic_format_from_databricks_format(
            "m", {"number": {"decimal_places": decimal_places}}, "metric"
        )
        assert block == validate_block({"value": {"kind": "number"}, "display": expected}, "metric")

    @pytest.mark.parametrize(
        ("options", "expected"),
        [
            ({"abbreviation": "NONE"}, {"compact": "none"}),
            ({"abbreviation": "COMPACT"}, {"compact": "auto"}),
            ({"abbreviation": "SCIENTIFIC"}, {}),
            ({"hide_group_separator": True}, {"grouping": False}),
            ({"hide_group_separator": False}, {}),
        ],
    )
    def test_number_display_hints(self, options: dict[str, Any], expected: dict[str, Any]) -> None:
        block = semantic_format_from_databricks_format("m", {"number": options}, "metric")
        assert block == validate_block({"value": {"kind": "number"}, "display": expected}, "metric")

    @pytest.mark.parametrize(
        ("date_format", "style"),
        [
            ("YEAR_MONTH_DAY", "iso"),
            ("LOCALE_NUMBER_MONTH", "short"),
            ("LOCALE_SHORT_MONTH", "medium"),
            ("LOCALE_LONG_MONTH", "long"),
        ],
    )
    def test_date_format_is_the_date_style(self, date_format: str, style: str) -> None:
        block = semantic_format_from_databricks_format(
            "d", {"date_time": {"date_format": date_format, "time_format": "NO_TIME"}}, "dimension"
        )
        assert block == validate_block(
            {"value": {"kind": "datetime"}, "display": {"date_style": style}}, "dimension"
        )

    def test_a_date_format_with_no_style_slot_sets_none(self) -> None:
        block = semantic_format_from_databricks_format(
            "d", {"date": {"date_format": "YEAR_WEEK", "leading_zeros": True}}, "dimension"
        )
        assert block == validate_block({"value": {"kind": "date"}}, "dimension")

    @pytest.mark.parametrize(
        ("native", "member_type"),
        [
            ({"number": {}}, "dimension"),
            ({"currency": {"currency_code": "USD"}}, "dimension"),
            ({"date": {}}, "metric"),
            ({"date_time": {}}, "metric"),
        ],
    )
    def test_a_format_that_no_kind_of_this_member_takes_gives_no_block(
        self, native: dict[str, Any], member_type: Any
    ) -> None:
        """A numeric dimension or a date measure has no S-LAYER kind to carry its format."""
        assert semantic_format_from_databricks_format("m", native, member_type) is None

    def test_an_unknown_type_gives_no_block(self) -> None:
        """A format type added to Databricks after this release is skipped, not an error."""
        assert semantic_format_from_databricks_format("m", {"hologram": {}}, "metric") is None

    def test_a_currency_without_a_code_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="'revenue'"):
            semantic_format_from_databricks_format("revenue", {"currency": {}}, "metric")

    @pytest.mark.parametrize("native", ["currency", {"a": {}, "b": {}}, {"number": "x"}])
    def test_a_format_of_neither_shape_is_an_error(self, native: Any) -> None:
        with pytest.raises(ValueError, match="does not make a valid block"):
            semantic_format_from_databricks_format("m", native, "metric")
