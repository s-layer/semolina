"""Tests for rebuilding a member's S-LAYER block from its tags."""

from __future__ import annotations

import pytest
from s_layer_semantic_formats import from_compact_json

from semolina.codegen.introspector import semantic_format_from_tags


class TestSemanticFormatFromTags:
    def test_no_kind_tag_is_no_format(self) -> None:
        assert semantic_format_from_tags("m", {}, "metric") is None

    def test_a_label_tag_alone_is_no_format(self) -> None:
        """The label is the field's own argument, never part of the block."""
        assert semantic_format_from_tags("m", {"S_LAYER_LABEL": "Revenue"}, "metric") is None

    def test_kind_alone_makes_a_block(self) -> None:
        block = semantic_format_from_tags("m", {"S_LAYER_KIND": "count"}, "metric")
        assert block == from_compact_json("count", None)

    def test_kind_and_spec_make_a_block(self) -> None:
        spec = '{"value":{"currency":"USD"}}'
        block = semantic_format_from_tags(
            "m", {"S_LAYER_KIND": "currency", "S_LAYER_SPEC": spec}, "metric"
        )
        assert block == from_compact_json("currency", spec)

    def test_the_block_carries_no_label(self) -> None:
        block = semantic_format_from_tags(
            "m", {"S_LAYER_KIND": "count", "S_LAYER_LABEL": "Orders"}, "metric"
        )
        assert block is not None
        assert block.label is None

    def test_a_spec_without_a_kind_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="'m' has an S_LAYER_SPEC tag but no S_LAYER_KIND"):
            semantic_format_from_tags("m", {"S_LAYER_SPEC": "{}"}, "metric")

    def test_a_kind_for_another_member_type_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="'country'"):
            semantic_format_from_tags("country", {"S_LAYER_KIND": "currency"}, "dimension")

    def test_an_unknown_kind_is_an_error(self) -> None:
        """Snowflake does not enforce a tag's allowed values on members, so check here."""
        with pytest.raises(ValueError, match="valid block"):
            semantic_format_from_tags("m", {"S_LAYER_KIND": "bogus"}, "metric")

    def test_a_spec_that_is_not_json_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="valid block"):
            semantic_format_from_tags(
                "m", {"S_LAYER_KIND": "count", "S_LAYER_SPEC": "{not json"}, "metric"
            )
