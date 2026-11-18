"""
A test that skips in CI did not run, so CI must not pass because of it.

Locally a skip is fine: an optional dependency or tool that is not installed.
Under CI (GitHub Actions sets ``CI=true``) every skip fails the run, unless its
reason matches a pattern in ``ALLOWED_CI_SKIPS``, each listed with why it may skip.
An ``xfail(run=False)`` test never runs either, so it counts as a skip; other
xfails ran and do not. Tests a CI job deliberately leaves out are deselected
(``-m``, ``--ignore``), not skipped, so they never reach this check.

Loaded from tests/conftest.py through ``pytest_plugins``, and with ``-p ci_skips``
for the semolina-jaffle-shop suite, which has its own rootdir.
"""

from __future__ import annotations

import os
import re

import pytest

# (pattern searched in the skip reason, why CI may skip it)
ALLOWED_CI_SKIPS: tuple[tuple[str, str], ...] = (
    (
        r"below the [\d.]+s floor these tests need to cancel",
        # tests/unit/test_async_cancel.py: on a runner fast enough that the heaviest
        # aggregate finishes under the floor, there is no running query to cancel.
        # The deterministic cancellation tests in the same file run on every runner.
        "runner too fast to time a live cancel",
    ),
)


def _in_ci() -> bool:
    return os.environ.get("CI", "").strip().lower() not in ("", "0", "false", "no")


def _not_run_reason(report: pytest.TestReport | pytest.CollectReport) -> str | None:
    """Why the report's test did not run, or None if it ran."""
    if not report.skipped:
        return None
    wasxfail: str | None = getattr(report, "wasxfail", None)
    if wasxfail is not None:
        return wasxfail if wasxfail.startswith("[NOTRUN]") else None
    longrepr = report.longrepr
    if isinstance(longrepr, tuple):
        return str(longrepr[2]).removeprefix("Skipped: ")
    return str(longrepr)


class _NotRunRecorder:
    """
    Collects the tests that did not run from the report hooks.

    Reading the hooks rather than the terminal reporter keeps the check working
    with ``-p no:terminal``; under xdist the controller gets its workers' reports.
    """

    def __init__(self) -> None:
        self.found: list[tuple[str, str]] = []

    def _record(self, report: pytest.TestReport | pytest.CollectReport) -> None:
        reason = _not_run_reason(report)
        if reason is not None and not any(
            re.search(pattern, reason) for pattern, _ in ALLOWED_CI_SKIPS
        ):
            self.found.append((report.nodeid, reason))

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self._record(report)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        self._record(report)


_RECORDER = pytest.StashKey[_NotRunRecorder]()


def pytest_configure(config: pytest.Config) -> None:
    if _in_ci():
        recorder = _NotRunRecorder()
        config.stash[_RECORDER] = recorder
        config.pluginmanager.register(recorder, "ci_skips_recorder")


def _unexpected_skips(config: pytest.Config) -> list[tuple[str, str]]:
    recorder = config.stash.get(_RECORDER, None)
    return recorder.found if recorder is not None else []


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    # NO_TESTS_COLLECTED too: a module skipped at import collects nothing.
    # Any other status (failures, interrupts, usage errors) is left as it is.
    passing = (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED)
    if exitstatus in passing and _unexpected_skips(session.config):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    skips = _unexpected_skips(terminalreporter.config)
    if not skips:
        return
    terminalreporter.section("skipped in CI", sep="=", red=True, bold=True)
    for nodeid, reason in skips:
        terminalreporter.line(f"{nodeid}: {reason}")
    terminalreporter.line(
        f"{len(skips)} test(s) did not run under CI, which counts as a failure. "
        "Make them run in CI, deselect them with -m, or allow the reason in "
        "ALLOWED_CI_SKIPS (tests/ci_skips.py)."
    )
