# Persona Report

**Generated:** 2026-10-27 (0.9.1 documentation gate, pass 1 of 3)
**Audience:** Python web developers building analytics backends (advanced)
**Scenarios tested:** 4 (reused from `scenarios.yaml`: no pages were added or removed)
**Results:** 3 PASS, 1 PARTIAL, 0 FAIL

## Summary

For this persona 0.9.1 is about types: an uncast `SUM` over an integer column on DuckDB now
arrives as a `decimal.Decimal`. The tutorials cast in the view, say why, and their outputs
reproduce. The typed-results guide's `validate=True` table, refreshed this run, now says a
whole-number decimal converts to `int`, which is the way out for a DTO that declared
`revenue: int` before the upgrade. The friction left is in the warehouse-testing guide,
whose fixture doesn't cast and never says its values are decimals.

---

## Scenario S1: Query by name for an agent tool and a JSON endpoint

**Verdict:** PASS

Nothing on this path changed in 0.9.1; `how-to/query-by-name.rst` and the web-api error
mapping still match source.

---

## Scenario S2: Weekly revenue chart, Snowflake in production, DuckDB in development

**Verdict:** PASS

### Navigation Path

1. `how-to/queries.rst` > time grain: call, column name `ordered_at__week`, week start, and
   the DuckDB tab now says the pinned extra runs it (measured on DuckDB 2.0.0).
2. The metric's own type on DuckDB (`Decimal` for an uncast integer sum) is covered in
   `how-to/typed-results.rst` and `explanation/duckdb-vs-warehouse.rst`.

---

## Scenario S3: Serve labels, descriptions and formats from the model

**Verdict:** PASS

`how-to/models.rst` covers declaring and reading metadata; the codegen side now includes a
Databricks view's native `format`.

---

## Scenario S4: Nothing to a FastAPI service, then Snowflake

**Verdict:** PARTIAL

### Navigation Path

1. `tutorials/first-query.rst` > `tutorials/testing-queries.rst` >
   `tutorials/dashboard-api.rst`: the cast view gives `revenue: int`, and the note says
   Snowflake would deliver a `Decimal` and points to `howto-typed-results`.
2. `how-to/typed-results.rst` > Pass `validate=True`: a whole-number `decimal128` converts to
   `int`; the fast path refuses it with `SemolinaSchemaMismatchError` (measured both).
3. `how-to/warehouse-testing.rst`: the in-memory fixture's metric has no cast, so its values
   are `Decimal`. The page doesn't say so.

### Gap

- **Page:** `how-to/warehouse-testing.rst`, section "Write a test"
- **Issue:** A reader who carries the dashboard tutorial's `revenue: int` DTO into this
  fixture gets a schema mismatch the page never prepares them for. The page's own
  assertions pass, because `Decimal(1500) == 1500`.
- **Why:** Achievable with the typed-results guide (PARTIAL).
- **Suggested fix:** In `how-to/warehouse-testing.rst`, section "Write a test": say the
  values are `Decimal` (a `HUGEINT` sum), why the assertion still holds, and that casting
  the sum to `BIGINT` gives `int`, as the testing tutorial does.

---

## Revision Recommendations

### FAIL Issues

None.

### PARTIAL Issues (for project author approval)

| Scenario | Page | Gap | Suggested Fix |
|----------|------|-----|---------------|
| S4 | `how-to/warehouse-testing.rst` | Fixture values are `Decimal`, unstated | One paragraph after the first test. **Applied.** |
