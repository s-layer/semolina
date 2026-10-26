# Editor Report

**Generated:** 2026-10-26
**Files reviewed:** 7 refreshed pages (`how-to/codegen.rst`, `reference/cli.rst`,
`how-to/queries.rst`, `changelog/index.rst`, `explanation/type-fidelity.rst`,
`how-to/query-by-name.rst`, `how-to/models.rst`), all four passes; terminology pass over all
29 hand-written `.rst` pages. `docs/src/reference/api/` excluded (sphinx-autoapi generated).
**Files edited:** 4 (pass 1), 1 more check on 4 revised pages (pass 2)
**Changes made:** 10
  - BLOCKING: 3
  - SUGGESTION: 3
  - NITPICK: 4

## Summary

The pages written in PRs #72–#80 and refreshed in this run are accurate against `src/`:
every grain SQL tab, the query-by-name examples and error messages, the metadata example
and its `TypeError` / `ValueError` claims were re-run against the current code and match.
Edits were two missing API links, two wording fixes where prose said a warehouse "reads" or
"records" something that is really Semolina's choice not to read it, one example that named
a model the page never defines, and line wrapping. No type blur, no AI-pattern prose beyond
that. `sphinx-build -W` (nitpicky) passes.

---

## docs/src/how-to/query-by-name.rst

### BLOCKING

| Section | Description | Fix |
|---------|-------------|-----|
| Write filters as data | First mention of ``SemolinaSpecError`` on the page was plain code; the linked mention came two sections later. | Linked to `semolina.exceptions.SemolinaSpecError`. |

### SUGGESTION

| Section | Description | Fix |
|---------|-------------|-----|
| Give a model the schema | The grains-by-name paragraph used ``Orders.ordered_at``, a model this page never defines (its model is ``Sales``), and said the schema lists grain names without saying a `date` field only gets `year`..`day` (verified against `query_spec_schema(Sales)`). | Rewritten on ``Sales.sold_on.grain("month")`` and states the date-only grain range. |

### NITPICK

| Section | Description | Fix |
|---------|-------------|-----|
| Report mistakes back to the caller | Line over 100 characters. | Rewrapped. |

---

## docs/src/how-to/models.rst

### BLOCKING

| Section | Description | Fix |
|---------|-------------|-----|
| Where the warehouse holds them | ``SemolinaIntrospectionError`` unlinked. | Linked to `semolina.exceptions.SemolinaIntrospectionError`. |

### SUGGESTION

| Section | Description | Fix |
|---------|-------------|-----|
| Where the warehouse holds them | Refreshed sentence said "DuckDB reads no S_LAYER tags"; it is Semolina that reads none on DuckDB (`DuckDBEngine.introspected_metadata` is `{"description", "synonyms"}`). | "Semolina reads no ``S_LAYER_*`` tags on DuckDB". |

### NITPICK

| Section | Description | Fix |
|---------|-------------|-----|
| Where the warehouse holds them | Line over 100 characters after the link. | Rewrapped. |

---

## docs/src/how-to/codegen.rst

### SUGGESTION

| Section | Description | Fix |
|---------|-------------|-----|
| Read the Detail lines | "DuckDB records no label or format" stated a warehouse property; the source fact is that Semolina does not read them there. Same distinction as in `models.rst`. | "Semolina reads no label or format on DuckDB". |

### NITPICK

| Section | Description | Fix |
|---------|-------------|-----|
| Read the Detail lines | Line over 100 characters. | Rewrapped the paragraph. |

---

## docs/src/reference/cli.rst

### NITPICK

| Section | Description | Fix |
|---------|-------------|-----|
| `--check` | Line over 100 characters after the refresh. | Rewrapped. |

---

## Pass 2 (revised pages: `how-to/web-api.rst`, `how-to/query-by-name.rst`, `how-to/models.rst`, `how-to/codegen.rst`)

### docs/src/how-to/models.rst

#### BLOCKING

| Section | Description | Fix |
|---------|-------------|-----|
| Describe your fields | New read-back paragraph named ``Orders.metrics()`` / ``Orders.dimensions()`` as plain code. | Linked to `semolina.models.SemanticView.metrics` / `.dimensions`. |

The other revised text (web-api `SemolinaSpecError` row, clause and ordering rule; the
query-by-name 400 sentence and anchor link; the codegen generated-output paragraph) needed
no edits: no AI patterns, symbols already linked, terms match `terminology.yaml`. The
read-back snippet and every new claim were run against the current source.

## Not changed (noted)

- `how-to/filtering.rst` has three lines over 100 characters from #76. Not a page changed in
  this run, so left alone.
- `changelog/index.rst` keeps plain inline code for symbols, as every earlier release entry
  does; no links added there.
- Exit-code tables keep the ``Code -- meaning`` double-hyphen form the existing rows use.

## Terminology Changes

No normalizations needed. Three project terms added to `terminology.yaml`:

| Term | Before | After | Authority |
|------|--------|-------|-----------|
| S-LAYER block | (new) | "S-LAYER block" for the format; ``S_LAYER_*`` only for tag names | `fields.py` docstrings, `introspector.py` tag constants |
| time grain | (new) | "time grain" / "grain"; ``.grain()`` in code | `Dimension.grain`, `Grain` in `fields.py` |
| field metadata | (new) | "metadata arguments" for ``label=``, ``description=``, ``synonyms=``, ``semantic_format=`` | `METADATA_ARGUMENTS` in `codegen/model_reader.py` |
