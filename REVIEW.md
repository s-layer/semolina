# Review rules

What a reviewer (the review bot or a person) should look for in a pull request here.
CLAUDE.md says how to work in this repository; this file says what to check in a diff.

## Severity

Tag every finding with one of these:

- **Important**: blocks the merge. A wrong result, a crash, a security problem, a test
  that would pass on broken code, or a break of one of the checks below.
- **Nit**: worth fixing but does not block. Naming, clarity, a small missing test, docs.
- **Pre-existing**: a problem the diff did not cause but sits next to it. Label it so it
  is not mistaken for a regression. It does not block this PR.

Lead with the Important findings. If there are none, say so in one line.

## What to look for

- The SQL semolina sends: each warehouse dialect (Snowflake semantic views, Databricks
  metric views, DuckDB semantic views) gets SQL it accepts and that means what the
  Python query says.
- Types: basedpyright strict passes with no new `# type: ignore`, and the public API's
  types say what the code does.
- Docs: public API changes update the matching docs page, written with the
  semolina-docs-author skill, and docstring examples use `.. code-block:: python`.

## Diff-triggered checks

Apply a check only when its trigger matches the diff. Each one comes from a miss that
already happened, named under "Source".

- **When the diff touches AGENTS.md, CLAUDE.md, REVIEW.md or anything under `.claude/`**
  Check: Important unless the PR description lists every rule the change adds, loosens or
  removes. Either way, list those rule changes in the review, and review the rest of the
  diff against the base branch's copy of these files, not the PR's. Once the rule changes
  are listed and acknowledged, the finding no longer blocks.
  Source: the review bot reads the root AGENTS.md, CLAUDE.md and REVIEW.md from the PR's
  own checkout (checked 2026-11-19), so a PR can rewrite the rules it is reviewed against.
  `.claude/` is included as a precaution.
- **When the diff changes the SQL sent to Snowflake or Databricks**
  Check: the cassettes that record that SQL were re-recorded against the warehouse and
  committed, or the PR says why replay still proves the change. Replay shows semolina
  sends the same SQL; it cannot show the warehouse still answers it the same way.
  Source: the 2026-11 test review: replay cannot see warehouse drift. The re-record
  schedule is in MAINTAINER.md under "Recording warehouse cassettes".
- **When the diff adds or re-records a cassette**
  Check: no personal name, email address, account identifier, key or local home-directory
  path is committed in it.
  Source: semolina cassettes carried an email address until they were scrubbed on
  2026-10-26 (it is still in git history).
- **When the diff changes filter or expression compilation**
  Check: the change is covered on DuckDB by executing the compiled SQL, not only by
  comparing SQL text.
  Source: the 2026-11 test review: one property test in 3,011, with the filter compiler
  the best target.
- **When the diff adds a test that compares wall-clock times**
  Check: it is marked `timing` so CI runs it alone after the parallel pass, or it uses a
  deterministic signal instead.
  Source: TEST-04: the two cancel-timing tests flaked under `-n auto`.
- **When the diff fixes a bug**
  Check: a test that reproduces the bug and fails without the fix (next to the most
  similar existing tests, failing test committed first). When one fix has several cases,
  each case needs its own observed failure, not just the run.
  Source: the failing-test-first rule in CLAUDE.md.
- **When the diff adds or changes a test**
  Check: the assertions would fail if the behaviour were wrong. Flag `len(x) > 0`, a
  substring check standing in for a result, `pytest.raises(Exception)` where a specific
  error type exists, fixtures where different answers produce the same number, and checks
  on a field the fixture or generator never varies.
  Source: the 2026-11 test review, which found each of these in s-layer repos
  (`pytest.raises(Exception)` in arrowmodel, a one-row fixture in dbt-s-layer where
  revenue, paid revenue and average order value were all 10).
- **When the diff adds `pytest.skip`, `skipif`, `importorskip` or an `xfail`**
  Check: under CI a skip fails the run unless its reason is allowed in `tests/ci_skips.py`
  with why. A new skip needs CI to install what it needs, a `-m` deselect in the job that
  cannot run it, or an allowlist entry with a reason. A non-strict xfail is Important.
  Source: the 2026-11 test review: 31 dbt-open-sl tests and 8 dbt-s-layer engine tests
  skipped in every CI run while CI stayed green.
- **When the diff deletes, skips or loosens a test, or narrows what a generator produces**
  Check: Important unless the PR says why. A refactor carries its tests with it.
  Source: the coverage-forward refactoring rule (duckdb-semantic-views CLAUDE.md), which
  applies to every s-layer repo.
- **When the diff adds a retry, a rerun, a longer timeout or a sleep to make a flaky test
  pass**
  Check: Important. A flaky test is fixed, or deleted with the reason written down. It is
  never rerun into green. A timeout raised because a job really got slower is not this.
  Source: a maintainer rule for all s-layer repos, set 2026-10-19 after a flaky failure
  was rerun instead of fixed: never re-run CI hoping for a lucky green.
- **When the diff changes a `uses:` line under `.github/workflows/`**
  Check: `.github/workflows/actions.lock` changes in the same PR, and an action is
  referenced by a release tag, not a branch or a commit SHA. Local workflows (`uses:
  ./...`) carry no ref, and the reusable review-bot workflow
  (`anentropic/github-reviewbot/...@main`) run at a branch on purpose; none of these is a
  finding.
  Source: the GitHub Actions lockfile section in CLAUDE.md; a branch ref
  (`dtolnay/rust-toolchain@stable`) broke every job's setup in arrowmodel when the branch
  was force-pushed, and Dependabot only bumps tags.
