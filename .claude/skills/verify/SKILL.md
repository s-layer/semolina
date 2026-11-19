---
name: verify
description: Prove a change to semolina works end to end. Use before saying a change is done, before committing, and before opening or updating a pull request.
---

# Verify a change

Run from the repository root:

```bash
prek run --all-files
just test > /tmp/semolina-test.log 2>&1; echo "test rc=$?"
just docs-build
```

It passes when every command exits 0. `just test` syncs `--extra all`, runs the suite in
parallel without the `timing` tests, then the `timing` tests alone, then the
semolina-jaffle-shop tests with skips failing.

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

## When it fails

- Read the first failure and fix its cause. Do not rerun hoping for a green, and do not
  skip, loosen or delete the failing test to get one.
- If the failure is real but outside the change, say so in the PR and in your report
  rather than working around it.
