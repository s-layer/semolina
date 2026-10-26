# Gap Report

**Generated:** 2026-10-26
**Source root:** src/
**Language:** python
**Mode:** update (29 hand-written `.rst` pages under `docs/src/`, excluding the
sphinx-autoapi-generated `docs/src/reference/api/`)
**Scope of this run:** changes since the `v0.8.0` tag (PRs #72–#80), ahead of the 0.9.0 release
**Total undocumented symbols:** 9 (2 worth narrative coverage, 7 reference-only)
**Potentially stale pages:** 29 (every page: `src/` was last touched after every doc page, so
the timestamp heuristic flags the whole set; the Author narrows this in the inventory)

## Undocumented Symbols

Public names added since v0.8.0 that no hand-written page names. All are covered by the
sphinx-autoapi reference.

### src/semolina/spec.py
- `predicate_from_spec` (function) — builds only a filter from data. **Worth one paragraph**
  in `how-to/query-by-name.rst`: a caller that already has a query and only takes a filter
  from outside needs it.
- `QuerySpec` (TypedDict) — the type of `build_query`'s data. **Worth naming** in
  `how-to/query-by-name.rst` for typing a tool handler's argument.
- `FilterSpec`, `FilterLeaf`, `AndFilter`, `OrFilter`, `NotFilter`, `OrderSpec`, `OrderObject`,
  `FilterOp` (type aliases / TypedDicts) — the parts of `QuerySpec`. Reference-only; the page
  documents the shapes as data.

### src/semolina/fields.py
- `Grain`, `GRAINS`, `DATE_GRAINS` (type alias / constants) — the grain names. The how-to
  lists the eight names; reference-only otherwise.
- `GrainedDimension` (class) — what `Dimension.grain()` returns. Reference-only.
- `declared_type` (function) — internal helper re-exported in the module's `__all__`; not
  reachable from a user task.

## Persistent (from the previous run)

- `SQLDialect` — extension point for third-party dialect authors; reference-only, unchanged.
- `_require`, `_claim_read`, `_connecting` — underscore-prefixed, internal by convention.
- `Dialect` — the previous report flagged it as having lost narrative coverage; it is now
  named in `how-to/backends.rst`, `how-to/codegen.rst`, `how-to/filtering.rst` and others.
  **Resolved.**
- `SemolinaGeneratedSourceError`, `SemolinaUnresolvableAnnotationError` — codegen-internal
  error classes surfaced only through CLI exit codes; reference-only.

## Potentially Stale Pages

29 doc pages are older than the latest source change (src: 2026-10-26 17:47). The source
changes since v0.8.0 touch: `semolina.spec` (new), `Dimension.grain()`, field and model
metadata arguments, Snowflake/Databricks/DuckDB introspection (synonyms, comments,
S_LAYER tags), `codegen --check` metadata comparison, Snowflake date/time literal inlining,
and DuckDB member types from `DESCRIBE SEMANTIC VIEW`.

- `docs/src/changelog/index.rst` (doc: 2026-10-26 17:33, source: 2026-10-26 17:47)
- `docs/src/how-to/codegen.rst` (doc: 2026-10-26 17:27, source: 2026-10-26 17:47)
- `docs/src/how-to/query-by-name.rst` (doc: 2026-10-26 11:34, source: 2026-10-26 17:47)
- `docs/src/how-to/queries.rst` (doc: 2026-10-26 12:29, source: 2026-10-26 17:47)
- `docs/src/how-to/filtering.rst` (doc: 2026-10-26 12:29, source: 2026-10-26 17:47)
- `docs/src/how-to/index.rst` (doc: 2026-10-26 11:10, source: 2026-10-26 17:47)
- `docs/src/explanation/type-fidelity.rst` (doc: 2026-10-26 10:59, source: 2026-10-26 17:47)
- `docs/src/how-to/models.rst` (doc: 2026-10-26, via #77/#80, source: 2026-10-26 17:47)
- `docs/src/reference/cli.rst` (doc: 2026-10-16 18:00, source: 2026-10-26 17:47)
- `docs/src/reference/config.rst` (doc: 2026-10-19 15:27, source: 2026-10-26 17:47)
- `docs/src/how-to/backends.rst` (doc: 2026-10-19 15:25, source: 2026-10-26 17:47)
- `docs/src/how-to/connection-pools.rst` (doc: 2026-10-19 15:15, source: 2026-10-26 17:47)
- `docs/src/how-to/warehouse-testing.rst` (doc: 2026-10-19 13:40, source: 2026-10-26 17:47)
- `docs/src/tutorials/installation.rst` (doc: 2026-10-19 15:21, source: 2026-10-26 17:47)
- `docs/src/tutorials/testing-queries.rst` (doc: 2026-10-19 13:40, source: 2026-10-26 17:47)
- `docs/src/how-to/codegen-credentials.rst`, `how-to/dto-codegen.rst`, `how-to/streaming.rst`,
  `how-to/typed-results.rst`, `how-to/web-api.rst`, `explanation/duckdb-vs-warehouse.rst`,
  `explanation/semantic-views.rst`, `index.rst`, `tutorials/dashboard-api.rst`,
  `tutorials/first-query.rst`, `tutorials/shaping-a-report.rst`,
  `tutorials/warehouse-models.rst` (doc: 2026-10-16 18:00, source: 2026-10-26 17:47)
- `docs/src/tutorials/index.rst` (doc: 2026-09-05), `explanation/index.rst` (doc: 2026-08-16),
  `reference/index.rst` (doc: 2026-05-10) (source: 2026-10-26 17:47)
