# Editor Report

**Generated:** 2026-10-27
**Files reviewed:** 4 refreshed pages (`how-to/typed-results.rst`, `how-to/models.rst`,
`how-to/codegen.rst`, `explanation/duckdb-vs-warehouse.rst`), all four passes, plus the two
pages that took a PARTIAL fix (`how-to/warehouse-testing.rst`, `how-to/backends.rst`);
terminology pass over all 29 hand-written `.rst` pages. `docs/src/reference/api/` excluded
(sphinx-autoapi generated).
**Files edited:** 0 beyond the Author's refresh
**Changes made:** 0
  - BLOCKING: 0
  - SUGGESTION: 0
  - NITPICK: 0

## Summary

The refreshed text was checked against the code it describes by running it: a scale-0
`decimal128` into an `int` field with `validate=True` (converts), a fractional one (raises
`ValidationError`), and the Databricks `format` mapping for each documented shape,
including the `ValueError` for a `currency` with no code. No type blur: the typed-results
addition is guidance inside a how-to, the duckdb-vs-warehouse change stays explanation. No
AI-pattern prose, no em dashes. No new public symbols are named in prose, so no links were
needed. `sphinx-build -W` (nitpicky) passes.

Every page #82–#84 edited is now within 100 characters a line, apart from literal code and
captured output (`queries.rst` line 208 is generated SQL; `typed-results.rst` line ~484 is
an error message).

## Not changed (noted)

- `how-to/typed-results.rst`, "When the DTO does not match": the quoted
  `SemolinaSchemaMismatchError` text ends "raising ValidationError where it cannot
  (decimal -> int)". That is the library's message verbatim (`src/semolina/dto.py`), so the
  doc keeps it. The message is imprecise for a whole-number decimal, which converts; worth a
  source fix in a later release.
- `changelog/index.rst` keeps its "Unreleased" heading; the version heading belongs to the
  release commit.

## Terminology Changes

No normalizations needed. `HUGEINT` is spelled the same on all 11 occurrences, and
"S-LAYER kind" / ``S_LAYER_*`` follow the existing `terminology.yaml` split.
