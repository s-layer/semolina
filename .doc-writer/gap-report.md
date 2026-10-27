# Gap Report

**Generated:** 2026-10-27
**Source root:** src/
**Language:** python
**Mode:** update (29 hand-written `.rst` pages under `docs/src/`, excluding the
sphinx-autoapi-generated `docs/src/reference/api/`)
**Scope of this run:** changes since the `v0.9.0` tag (PRs #82, #83, #84), ahead of the 0.9.1
point release
**Total undocumented symbols:** 1 (reference-only)
**Potentially stale pages:** 17 (the 12 pages #82–#84 edited, plus 5 that describe the
surfaces they changed without having been edited)

## Undocumented Symbols

Public names added since v0.9.0 that no hand-written page names.

### src/semolina/codegen/introspector.py
- `semantic_format_from_databricks_format` (function) — added by #83 to `__all__` beside
  `semantic_format_from_tags`. It is the codegen helper `DatabricksEngine.introspect()`
  calls; no user task reaches it directly, and its behaviour (which native `format` maps to
  which `semantic_format`) is documented in `how-to/models.rst`. Reference-only, like
  `semantic_format_from_tags`.

No other export changed: #82 changed a pin and the DuckDB engine's internals, #84 a private
parser table in `engines/duckdb.py`.

## Persistent (from the previous run)

- `predicate_from_spec`, `QuerySpec` — covered in `how-to/query-by-name.rst` by the 0.9.0
  pass. **Resolved.**
- `FilterSpec`, `FilterLeaf`, `AndFilter`, `OrFilter`, `NotFilter`, `OrderSpec`,
  `OrderObject`, `FilterOp`, `Grain`, `GRAINS`, `DATE_GRAINS`, `GrainedDimension`,
  `declared_type`, `SQLDialect`, `SemolinaGeneratedSourceError`,
  `SemolinaUnresolvableAnnotationError` — reference-only, unchanged.
- `_require`, `_claim_read`, `_connecting` — underscore-prefixed, internal by convention.

## Potentially Stale Pages

`src/` was last touched 2026-10-27 07:30 (#84). The source changes since v0.9.0 are: the
`duckdb==2.0.0` pin (`semantic_views` 0.16.0, `HUGEINT` sums as `decimal.Decimal`, results
streamed during the fetch), the Databricks native `format` read into `semantic_format`, and
`DERIVED_METRIC` introspected as a metric on DuckDB.

Edited by #82–#84 (checked against source):

- `docs/src/changelog/index.rst` (doc: 2026-10-27 07:30, source: 2026-10-27 07:30)
- `docs/src/how-to/codegen.rst` (doc: 2026-10-26 23:16, source: 2026-10-27 07:30)
- `docs/src/how-to/models.rst` (doc: 2026-10-26 23:16, source: 2026-10-27 07:30)
- `docs/src/reference/cli.rst` (doc: 2026-10-26 23:16, source: 2026-10-27 07:30)
- `docs/src/explanation/duckdb-vs-warehouse.rst` (doc: 2026-10-26 21:19, source: 2026-10-27 07:30)
- `docs/src/explanation/type-fidelity.rst` (doc: 2026-10-26 21:19, source: 2026-10-27 07:30)
- `docs/src/how-to/dto-codegen.rst` (doc: 2026-10-26 21:19, source: 2026-10-27 07:30)
- `docs/src/how-to/queries.rst` (doc: 2026-10-26 21:19, source: 2026-10-27 07:30)
- `docs/src/how-to/web-api.rst` (doc: 2026-10-26 21:19, source: 2026-10-27 07:30)
- `docs/src/tutorials/dashboard-api.rst` (doc: 2026-10-26 21:19, source: 2026-10-27 07:30)
- `docs/src/tutorials/first-query.rst` (doc: 2026-10-26 21:19, source: 2026-10-27 07:30)
- `docs/src/tutorials/testing-queries.rst` (doc: 2026-10-26 21:19, source: 2026-10-27 07:30)

Not edited, but describing a changed surface (DuckDB result types, timeouts, DuckDB
introspection):

- `docs/src/how-to/typed-results.rst` (doc: 2026-10-16 18:00, source: 2026-10-27 07:30)
- `docs/src/how-to/streaming.rst` (doc: 2026-10-16 18:00, source: 2026-10-27 07:30)
- `docs/src/how-to/warehouse-testing.rst` (doc: 2026-10-19 13:40, source: 2026-10-27 07:30)
- `docs/src/how-to/backends.rst` (doc: 2026-10-19 15:25, source: 2026-10-27 07:30)
- `docs/src/tutorials/warehouse-models.rst` (doc: 2026-10-16 18:00, source: 2026-10-27 07:30)

The other 12 pages are older than `src/` too, but nothing they describe changed in
#82–#84, so they are not on the list.
