# Persona Report

**Generated:** 2026-10-26 (0.9.0 documentation gate, pass 2 of 3)
**Audience:** Data engineers exposing semantic layers via APIs (intermediate)
**Scenarios tested:** 5 (regenerated this run; see `scenarios.yaml`)
**Results:** 5 PASS, 0 PARTIAL, 0 FAIL
**Pass 1 results:** 4 PASS, 1 PARTIAL, 0 FAIL

## Summary

The field-metadata story is complete for someone who maintains annotations in the
warehouse: the per-backend table in the models guide says where each value comes from, what
the codegen role must be allowed to read, and what error a refused tag lookup raises; the
codegen guide and CLI reference now say metadata drift counts as drift and which exit code
CI sees. DuckDB's limits (no tags read, no grains on the pinned extra) are stated plainly.
The one pass 1 friction was that the codegen guide's generated-output section never said
annotations become arguments; that is fixed.

---

## Scenario S1: Codegen from dbt-s-layer-tagged Snowflake views, with permissions

**Verdict:** PASS (pass 1: PARTIAL)

### Navigation Path

1. Started at: `index.rst` > tutorial 6 "Generate models" > `tutorials/warehouse-models.rst`
   - Followed: link to `how-to/codegen.rst`.
2. `how-to/codegen.rst` > Choose a backend
   - Found: Snowflake introspects with `SHOW COLUMNS IN VIEW`, then `DESCRIBE SEMANTIC VIEW`
     and one `TAG_REFERENCES` query.
3. `how-to/codegen.rst` > Understand the generated output
   - Pass 1: plain examples only; the link to the metadata section was only further down,
     under `--check`'s Detail lines.
   - Pass 2: a paragraph after the examples says comments, synonyms, display names and
     `S_LAYER_*` tags become `description=` / `synonyms=` / `label=` / `semantic_format=`,
     with a link.
   - Followed: link to `how-to/models.rst` > Describe your fields.
4. `how-to/models.rst` > Where the warehouse holds them
   - Found: Snowflake row (COMMENT, `S_LAYER_LABEL`, `WITH SYNONYMS`, `S_LAYER_KIND` +
     `S_LAYER_SPEC`); own-tags-only rule; invalid tags raise `ValueError` naming the member;
     the role needs to call `INFORMATION_SCHEMA.TAG_REFERENCES` in the view's database; a
     refusal raises `SemolinaIntrospectionError`, exit 6 per the codegen exit-code table.

### Gap Analysis (pass 1, resolved)

**Where:** `how-to/codegen.rst` > Understand the generated output
**What:** No mention that warehouse annotations become field arguments.
**Fix applied:** One paragraph plus a link to `howto-models-metadata`.

---

## Scenario S2: `--check` in CI for metadata drift, and drift after upgrading

**Verdict:** PASS

### Navigation Path

1. `how-to/codegen.rst` > Check a committed model / Read the Detail lines
   - Found: metadata drift prints a `Detail:` line, an example for synonyms, only what the
     backend records is compared, formats by meaning, synonyms as a set, view description
     as `Detail: (view)` and exit 5.
   - Interpret the exit code: 5 now covers annotation, role, column, metadata or the view
     description. Exit-code table: 1 for invalid `S_LAYER_*` tags, 6 for a refused tag lookup.
   - Edit the committed file by hand: metadata read as literals or string constants;
     anything else warned about and not compared.
2. Followed: Detail lines link to `how-to/models.rst` > metadata
   - Found: models from earlier releases drift wherever the warehouse records a comment or
     synonyms; regenerate once. Same note in the changelog's Unreleased entry.
3. `reference/cli.rst` > `--check`, Exit codes: consistent with the how-to.

---

## Scenario S3: Databricks only, metadata and limits

**Verdict:** PASS

### Navigation Path

1. `how-to/models.rst` > Where the warehouse holds them, Databricks row: column `comment`,
   `display_name`, `synonyms`, `S_LAYER_KIND`/`S_LAYER_SPEC` column tags; the role needs the
   catalog's `information_schema.column_tags`.
2. `how-to/codegen.rst` > Choose a backend: `DESCRIBE TABLE EXTENDED AS JSON`, then
   `information_schema.column_tags`. The `--check` on Databricks warning (unverified,
   VARIANT false positive) is still there and clear.

---

## Scenario S4: Local DuckDB on the `semolina[duckdb]` extra

**Verdict:** PASS

### Navigation Path

1. `how-to/models.rst`: DuckDB reads `COMMENT` and `WITH SYNONYMS`; label and format "(not
   read)", and a sentence says Semolina reads no `S_LAYER_*` tags on DuckDB, so add them by
   hand; `--check` leaves them alone.
2. `how-to/queries.rst` > Group by a time grain, DuckDB tab: grains need `semantic_views`
   0.16.0 on DuckDB 2.0; the extra pins 1.5.6, so a grain query doesn't run there, but
   `to_sql("duckdb")` renders it.

---

## Scenario S5: Query endpoint with a filter from the query string

**Verdict:** PASS

### Navigation Path

1. `index.rst` > tutorial 4 `tutorials/dashboard-api.rst`: complete FastAPI handler with an
   optional `country` filter, lifespan engine, status mapping.
2. `how-to/web-api.rst` > Catch Semolina's own errors in one place: status table, now with a
   `SemolinaSpecError` → 400 row for endpoints that take field names.

---

## Revision Recommendations

### FAIL Issues (trigger revision)

No revision needed. No scenario failed in either pass.

### PARTIAL Issues (for project author approval)

None outstanding. Pass 1 PARTIAL (S1, `how-to/codegen.rst` > Understand the generated
output: say that warehouse annotations become field arguments and link the metadata
section) was approved and applied.
