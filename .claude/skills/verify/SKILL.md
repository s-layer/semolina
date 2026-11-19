---
name: verify
description: Prove a change to semolina works end to end. Use before saying a change is done, before committing, and before opening or updating a pull request.
---

# Verify a change

Run from the repository root:

```bash
export CI=true  # CI's skip gate: a skipped test fails the run
uv tool install prek  # once; skip if prek is already on PATH
prek run --all-files
just test > /tmp/semolina-test.log 2>&1; echo "test rc=$?"
just docs-build
```

It passes when every command exits 0 and every printed `rc=` is 0. `just test` syncs
`--extra all`, runs the suite in parallel without the `timing` tests, then the `timing`
tests alone, then the semolina-jaffle-shop tests with `-p ci_skips` (which fails on a
skip only when `CI` is set, hence the export).

`CI=true` turns on `tests/ci_skips.py`, which fails the run on any skip not allowed
there, as CI does. Without it a skipped test only shows in the summary, and a run that
skipped what you changed is not a pass.

Take each command's exit status from the command itself. In a pipeline `$?` is the last
command's status, so `cmd | tail` hides a failure. Redirect long output to a file, then
read the file.

## What this covers

- live DuckDB with the semantic_views extension
- Snowflake and Databricks by cassette replay
- the docs build with warnings as errors

## What it does not cover

- what Snowflake or Databricks answer today: cassettes replay the recorded answers, so
  re-record when warehouse behaviour matters
- the coverage floor and the clean-venv extras matrix, which CI checks
- Python versions other than the one you ran: CI runs a version matrix

## When it fails

- Read the first failure and fix its cause. Do not rerun hoping for a green, and do not
  skip, loosen or delete the failing test to get one.
- If the failure is real but outside the change, say so in the PR and in your report
  rather than working around it.
