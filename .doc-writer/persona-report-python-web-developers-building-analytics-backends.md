# Persona Report

**Generated:** 2026-11-20 (0.10.0 documentation gate, update mode, pass 1)
**Audience:** Python web developers building analytics backends (advanced)
**Scenarios tested:** 5 (S1-S4 reused from `scenarios.yaml`; S5 added, because no scenario
reached `fetch_polars()` or a fact query)
**Results:** 4 PASS, 1 PARTIAL, 0 FAIL

## Summary

For this persona 0.10.0 brings three things: DuckDB models now carry labels and formats, so
local development matches production for the metadata endpoint (S3); `fetch_polars()`
works under polars 2 (S5); and a DuckDB query that selects facts with dimensions runs again
(S5). Every changed claim checks out against the source. I also ran each one on DuckDB
2.0.0 with `semantic_views` v0.19.0 and polars 2.1.0. The friction is on the fact side. The
models how-to still describes the old DuckDB route for every fact query. Three pages also
blame the extension for refusing facts with metrics, but since 0.18.0 the extension accepts
that combination and only Semolina refuses it.

Verification probes (run in the scratchpad, not in the repo):

- A tagged DuckDB metric (`WITH TAG (S_LAYER_LABEL = ..., S_LAYER_KIND = 'currency',
  S_LAYER_SPEC = '{"value": {"currency": "USD"}}')`) generated `label="Revenue (USD)"` and
  a currency `semantic_format=`. `--check` exited 0. A hand-added `label="Country"` on an
  untagged dimension became drift (`Detail: country: label: committed 'Country', warehouse
  None`, exit 5). This matches `how-to/codegen.rst` and the changelog.
- `.dimensions(O.country, O.price).to_sql(dialect="duckdb")` gave
  `semantic_view('orders_sv', dimensions := ['country', 'price'])` with no `DISTINCT`. A
  facts-only query still gave `SELECT DISTINCT ... facts := ['price']`. Facts with metrics
  raised `ValueError`. This matches `explanation/duckdb-vs-warehouse.rst`.
- `fetch_polars()` on polars 2.1.0 returned a `DataFrame` (2 x 2). The lock has
  adbc-poolhouse 1.8.1 and adbc-driver-manager 1.13.0.
- The raw extension (v0.19.0) answered `semantic_view(..., dimensions := ['price'],
  metrics := ['revenue'])` with rows. It refused `dimensions` with `facts` with `querying
  FACTS with DIMENSIONS is not supported`.

---

## Scenario S1: Query by name for an agent tool and a JSON endpoint

**Verdict:** PASS

`how-to/query-by-name.rst` and `how-to/web-api.rst` are unchanged since 0.9.1, so this
carries over.

---

## Scenario S2: Weekly revenue chart, Snowflake in production, DuckDB in development

**Verdict:** PASS

`how-to/queries.rst` > time grain is unchanged: it gives the call, `ordered_at__week`, the
week start, and says it runs on the pinned DuckDB. Nothing in 0.10.0 touches grains.

---

## Scenario S3: Serve labels, descriptions and formats from the model

**Verdict:** PASS

### Navigation Path

1. `index.rst` > "Define models" card > `how-to/models.rst`.
2. "Describe your fields": declare `label=`, `description=`, `synonyms=`,
   `semantic_format=`. The `field_info` snippet reads them back for an endpoint. Unchanged
   and clear.
3. "Where the warehouse holds them": the DuckDB row now reads `S_LAYER_LABEL` and
   `S_LAYER_KIND`/`S_LAYER_SPEC` instead of "(not read)". The new DuckDB paragraph says the
   tags come from `DESCRIBE SEMANTIC VIEW`'s `TAGS` row, need extension 0.17.0, match
   names case-insensitively and use the last part of a qualified name. All of this matches
   `engines/duckdb.py` (`_s_layer_tags`, `_tag_base_name`, `_TAGS_SINCE`).
4. The `howto-codegen-check` link leads to `how-to/codegen.rst` > "Read the Detail lines",
   which gives the DuckDB drift rule before and after 0.17.0. It matches
   `IntrospectedView.unread_metadata`.

The goal is met: the local DuckDB model now carries the same labels and formats the
Snowflake one does, so the metadata endpoint can be developed locally.

### Minor friction (non-blocking)

- None of the pages say which `semantic_views` release the pinned `semolina[duckdb]` gets,
  or how to check. Both 0.17.0 (tags) and 0.18.0 (fact routing) now matter.
  `how-to/backends.rst` also says `INSTALL` is a no-op once the extension is cached. So an
  older cached build stays in place, and its only symptom is a model with no labels.
- The upgrade note for regenerating DuckDB models, "a model generated from a tagged view by
  an earlier release has neither argument, so `--check` reports it as drift", is only in
  the changelog. The matching upgrade paragraph at the end of
  `how-to/models.rst` > "Where the warehouse holds them" covers only `synonyms=` and the
  class `description=`.

---

## Scenario S4: Nothing to a FastAPI service, then Snowflake

**Verdict:** PASS (was PARTIAL at 0.9.1; that fix to `how-to/warehouse-testing.rst` is in
place)

`tutorials/installation.rst` > async extra now says `adbc-poolhouse[async]>=1.8.1`, "the
same floor as Semolina's base install". That is accurate (`pyproject.toml`).

### Minor friction (non-blocking)

The paragraph still explains only the older floors (1.6.4, 1.7.0) and never says why the
floor is 1.8.1. The reason, polars 2, is in `how-to/streaming.rst` and the changelog. The
page also never states the base install's floor on its own, so "the same floor as the
base install" points at something the page doesn't show.

---

## Scenario S5: Order-detail table (fact by dimensions) as a polars DataFrame under polars 2

**Verdict:** PARTIAL

### Navigation Path

1. `index.rst` > "Build queries" > `how-to/queries.rst` > "Select dimensions":
   `.dimensions()` takes `Dimension` or `Fact` fields. This answers how to select a fact.
2. `index.rst` > "Define models" > `how-to/models.rst` > "Fact fields": Snowflake and
   Databricks use `GROUP BY ALL`. The page then says: "DuckDB's `semantic_view()` takes
   facts in their own `facts :=` argument ... Semolina adds `SELECT DISTINCT`", and its
   DuckDB tab shows a facts-only query. **Friction:** my query has dimensions too, so
   `.to_sql(dialect="duckdb")` shows `dimensions := ['country', 'price']` with no
   `DISTINCT`, which this page doesn't describe. The page doesn't link to the explanation
   that does, and its "See also" doesn't list it.
3. I found the explanation by browsing the Explanation tab, not from a link:
   `explanation/duckdb-vs-warehouse.rst` > "Fact queries return the same rows". The new
   paragraph explains the `dimensions` route and why the rows match `GROUP BY ALL`. It is
   clear and correct. The next paragraph says "`semantic_view()` can't select facts and
   metrics together". **Friction:** the paragraph just before it says that from 0.18.0 a
   fact listed under `dimensions` is grouped on. The extension's 0.18.0 changelog adds that
   such a fact "can be queried with metrics", and v0.19.0 does answer that query. So what
   refuses it now is Semolina, not `semantic_view()`. An advanced reader who notices this
   can't tell whether the `ValueError` is permanent or a gap waiting to close.
4. `index.rst` > "Go further" > `how-to/streaming.rst` > "Fetch a polars DataFrame": the
   new paragraph says polars 1 and 2 both return a `DataFrame` and explains the floor.
   The decimal note now cites polars 2.1.0. Clear, and it matches `pyproject.toml`, the
   lock and my probe.
5. `changelog/index.rst` > 0.10.0: all three entries are accurate and link to the right
   pages.

The goal is achievable. The code works first time, and the explanation page answers the
rows question. The friction is that the how-to the reader lands on describes the wrong SQL
for this query, and that the facts-with-metrics limit is attributed to the wrong layer.

### Gap Analysis

**Where:** `how-to/models.rst` > "Fact fields" (the paragraph starting "On Snowflake and
Databricks, `Fact` and `Dimension` produce identical SQL") and "See also"
**What:** The paragraph describes `facts :=` plus `SELECT DISTINCT` as the DuckDB route
for every fact query. From 0.10.0, a fact selected with dimensions goes under
`dimensions :=` with no `DISTINCT`. The page has no link to
`explanation-duckdb-vs-warehouse`.
**Impact:** The SQL the reader inspects contradicts the page they are reading, and the page
offers no route to the explanation.
**Suggested Fix:** In `how-to/models.rst`, section "Fact fields": after "Semolina adds
`SELECT DISTINCT` to a fact query there", add "A fact selected beside a dimension is listed
under `dimensions :=` instead, which groups on it and returns the same rows; see
:ref:`explanation-duckdb-vs-warehouse`." Add that ref to the page's "See also".

**Where:** `explanation/duckdb-vs-warehouse.rst` > "Fact queries return the same rows"
(last paragraph); `how-to/models.rst` > "Fact fields" ("DuckDB can't select facts and
metrics in the same query"); `how-to/codegen.rst` > "Read the route on every row"
("DuckDB cannot select facts and metrics in a single `semantic_view()` call")
**What:** All three say the extension can't combine facts and metrics. From
`semantic_views` 0.18.0 it can, once the fact is listed under `dimensions`. I verified this
on v0.19.0. The `ValueError` comes from Semolina's own check in `engines/sql.py`, which
still refuses before the new routing runs.
**Impact:** The stated reason is wrong for the extension the pinned extra loads, so the
reader draws the wrong conclusion about where the limit lives.
**Suggested Fix:** In `explanation/duckdb-vs-warehouse.rst`, last paragraph of "Fact
queries return the same rows": say Semolina refuses facts with metrics on DuckDB, and drop
the claim that `semantic_view()` can't do it, or limit it to extensions before 0.18.0.
Apply the same wording to the two other sentences. Separately, for the maintainer, not the
docs: the refusal could be lifted on 0.18.0 and later by routing the facts under
`dimensions`, the way 0.10.0 already does for dimensions. If it stays, record it as a known
limitation.

---

## Revision Recommendations

### FAIL Issues (trigger revision)

None.

### PARTIAL Issues (for project author approval)

| Scenario | Page | Gap | Suggested Fix |
|----------|------|-----|---------------|
| S5 | `how-to/models.rst` > Fact fields, See also | Says the DuckDB route is `facts :=` + `DISTINCT` for every fact query; a fact with dimensions now goes under `dimensions :=`; no link to the explanation | One sentence after the `SELECT DISTINCT` sentence, plus `:ref:explanation-duckdb-vs-warehouse` in "See also" |
| S5 | `explanation/duckdb-vs-warehouse.rst`, `how-to/models.rst`, `how-to/codegen.rst` | Say `semantic_view()` can't combine facts and metrics; since 0.18.0 it can, and the refusal is Semolina's | Attribute the `ValueError` to Semolina, or limit the claim to extensions before 0.18.0; consider lifting the refusal or recording it as a limitation |
| S3 | `how-to/backends.rst` > The semantic_views extension | No way to tell which extension release is loaded, and a cached older build survives `INSTALL`; 0.17.0 and 0.18.0 now change behaviour | Add the check (`SELECT extension_version FROM duckdb_extensions() WHERE extension_name = 'semantic_views'`) and the refresh (`UPDATE EXTENSIONS` or `FORCE INSTALL semantic_views FROM community`); verify both before publishing. State the release the pinned DuckDB 2.0.0 gets (v0.19.0 was loaded here) |
| S3 | `how-to/models.rst` > end of "Where the warehouse holds them" | The note that DuckDB models from an earlier release need regenerating is only in the changelog | Extend the "Models generated by earlier releases" paragraph to cover `label=` and `semantic_format=` from a tagged DuckDB view |
| S4 | `tutorials/installation.rst` > async extra | States the 1.8.1 floor but explains only older floors | Add a clause: 1.8.1 brings `adbc-driver-manager` 1.13.0, which `fetch_polars()` needs under polars 2; link `howto-arrow-output` |

### Unverified risk (for the maintainer)

`DuckDBSQLBuilder` (`engines/sql.py`) moves facts under `dimensions :=` whenever a query
selects both, whatever the extension release. I could test only v0.19.0, the only build
cached here. If an extension before 0.18.0 refuses a fact under `dimensions`, then
fact-with-dimension queries worked on 0.9.1 and fail on 0.10.0 for anyone with an older
cached build. If so, the changelog entry needs a version note. This needs a test against a
0.17.x extension before the release.

### Pre-existing problems outside this release's changes

- `how-to/streaming.rst` > "Fetch a whole result in async code" shows only
  `await cursor.fetch_arrow_table()`. It never mentions `AsyncSemolinaCursor.fetch_polars()`
  or `fetch_df()`, which exist (`acursor.py`), so the new polars 2 paragraph reads as sync
  only.
- No page shows how to write an `S_LAYER_*` tag on a DuckDB member, or what `S_LAYER_SPEC`
  holds. In my probe, `'{"currency": "USD"}'` was rejected and
  `'{"value": {"currency": "USD"}}'` was accepted. Someone building a test fixture that
  should produce `label=`/`semantic_format=` has nothing to copy. This matters little to
  this persona when dbt-s-layer writes the tags, but a one-line `WITH TAG (...)` example in
  `how-to/warehouse-testing.rst` or `how-to/models.rst` would close it.
