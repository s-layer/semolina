# Persona Report

**Generated:** 2026-10-26 (0.9.0 documentation gate, pass 2 of 3)
**Audience:** Python web developers building analytics backends (advanced)
**Scenarios tested:** 4 (regenerated this run; see `scenarios.yaml`)
**Results:** 4 PASS, 0 PARTIAL, 0 FAIL
**Pass 1 results:** 2 PASS, 1 PARTIAL, 1 FAIL

## Summary

The 0.9.0 surface reads well from this seat. Query-by-name is reachable from the queries
guide's "See also" and the how-to index, and its schema and error examples are complete.
The time-grain section answers the questions a chart needs (column name, type, week start)
and now says the pinned DuckDB extra can't run a grain. Pass 1 failed S1 because the web API
guide told me to send every `SemolinaError` to a 500, which would have turned a client's
misspelt field name into a server error. That and the S3 gap (no way shown to read metadata
back) are fixed.

---

## Scenario S1: Expose a view to an LLM tool and a JSON endpoint by field name, bad input as 4xx

**Verdict:** PASS (pass 1: FAIL)

### Navigation Path

1. Started at: `index.rst`
   - Found: "Build queries" card.
   - Followed: card to `how-to/queries.rst`.
2. Navigated to: `how-to/queries.rst` > See also
   - Found: "build the same queries, grains included, from field names".
   - Followed: link to `how-to/query-by-name.rst`.
3. Navigated to: `how-to/query-by-name.rst`
   - Found: `build_query()` with a full dict example and its attribute equivalent; sort
     terms; filter trees and the op table; ISO date strings; `SemolinaSpecError` with `path`
     and message; `query_spec_schema()` as a tool input schema with a `run_tool` handler;
     `QuerySpec` for typing the argument; `predicate_from_spec()` for filter-only input.
   - Found (pass 2): "In a web handler, answer it with a 400 ... catch it before any
     `except SemolinaError` clause" with a link to the web API error section.
   - Followed: that link.
4. Navigated to: `how-to/web-api.rst` > Catch Semolina's own errors in one place
   - Pass 1: the page said every `SemolinaError` is "not the client's fault", mapped "Any
     other `SemolinaError`" to 500, and put `except SemolinaError` before the 400 clause. A
     `SemolinaSpecError` from a bad request body therefore landed on 500 with "Internal
     error". Contradicted the query-by-name page.
   - Pass 2: `SemolinaSpecError` has its own 400 row, the catch order says it goes before
     `except SemolinaError`, and the code block has the clause.

### Gap Analysis (pass 1, resolved)

**Where:** `how-to/web-api.rst` > Catch Semolina's own errors in one place
**What:** No row or clause for `SemolinaSpecError`; the documented catch order sent it to 500.
**Impact:** Following the guide misreports client mistakes as server errors and hides the
message the tool is supposed to return.
**Fix applied:** 400 row, ordering rule, `except SemolinaSpecError` in the example, and a
link from query-by-name.

---

## Scenario S2: Weekly revenue chart, Snowflake in production, DuckDB in development

**Verdict:** PASS

### Navigation Path

1. Started at: `index.rst` > "Build queries" card > `how-to/queries.rst` > Group by a time grain
   - Found: `.grain("week")` example with Snowflake, Databricks and DuckDB SQL tabs; the
     result column `ordered_at__week`; `date` for day and coarser, `datetime` below that;
     weeks start on Monday regardless of `WEEK_START`; filter the base field, not the grain.
   - Found: the DuckDB tab says grains need `semantic_views` 0.16.0 on DuckDB 2.0 and that
     the `semolina[duckdb]` extra's DuckDB 1.5.6 can't run them; `to_sql("duckdb")` still
     renders. I know not to promise this locally on the pinned extra.
2. Followed: "Filter queries" link to `how-to/filtering.rst` > How values reach the warehouse
   - Found: Snowflake inlines date, timestamp and time values as literals; an aware `time`
     raises on Snowflake.

---

## Scenario S3: Serve labels, descriptions and formats from the model

**Verdict:** PASS (pass 1: PARTIAL)

### Navigation Path

1. Started at: `index.rst` > "Define models" card > `how-to/models.rst` > Describe your fields
   - Found: the four keyword arguments and the model's `description=`, with validation rules.
   - Pass 1: nothing showed how to read the values back; "They're there for whatever reads
     your models" left me guessing at attribute names.
   - Pass 2: a dict comprehension over `Orders.metrics()` reading `label`, `description`,
     `synonyms` and `semantic_format` (via `model_dump`), and what an unset argument reads as.
2. Same page > Where the warehouse holds them
   - Found: per-backend table, regenerate rather than edit, `--check` link.

### Observation (not a verdict change)

The page shows how to declare a view-level `description=` but not how to read it back.
The fields cover this scenario's goal, so no fix is requested here; see the orchestrator's
note on the model's description accessor.

---

## Scenario S4: Zero to FastAPI `GET /revenue`, then Snowflake via `.semolina.toml`

**Verdict:** PASS

### Navigation Path

1. `index.rst` > tutorials 1–4 (`installation`, `first-query`, `shaping-a-report`,
   `dashboard-api`): install commands are quoted, the tutorial database chain is linked from
   installation, the endpoint runs with a DTO.
2. `how-to/backends.rst` (Snowflake tab) and `reference/config.rst`: the `[connections.default]`
   fields; `how-to/web-api.rst` for async lifespan and status codes.
   - Nothing in the 0.9.0 changes touched this path; the earlier PARTIALs on it are fixed.

---

## Revision Recommendations

### FAIL Issues (trigger revision)

None after pass 2. Pass 1 FAIL (S1, `how-to/web-api.rst`) was revised and now passes.

### PARTIAL Issues (for project author approval)

None outstanding. Pass 1 PARTIAL (S3, `how-to/models.rst` > Describe your fields: show how to
read metadata back from a model's fields) was approved and applied.
