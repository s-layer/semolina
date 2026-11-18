"""
A test that skips in CI did not run, so CI must not pass because of it.

Locally a skip is fine: an optional dependency or tool that is not installed.
Under CI (GitHub Actions sets ``CI=true``) every skip fails the run, unless its
reason matches a pattern in ``ALLOWED_CI_SKIPS``, each listed with why it may skip.
Tests a CI job deliberately leaves out are deselected (``-m``, ``--ignore``), not
skipped, so they never reach this check.

Loaded from tests/conftest.py through ``pytest_plugins`` (as ``ci_skips``: tests/ is
on sys.path, not a package).
"""

from __future__ import annotations

import os
import re

import pytest

# (pattern searched in the skip reason, why CI may skip it)
ALLOWED_CI_SKIPS: tuple[tuple[str, str], ...] = ()


def _reason(report: pytest.TestReport | pytest.CollectReport) -> str:
    longrepr = report.longrepr
    if isinstance(longrepr, tuple):
        return str(longrepr[2])
    return str(longrepr)


def _unexpected_skips(config: pytest.Config) -> list[tuple[str, str]]:
    if not os.environ.get("CI"):
        return []
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return []
    found: list[tuple[str, str]] = []
    for report in reporter.stats.get("skipped", []):
        reason = _reason(report)
        if not any(re.search(pattern, reason) for pattern, _ in ALLOWED_CI_SKIPS):
            found.append((report.nodeid, reason))
    return found


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    # NO_TESTS_COLLECTED too: a module skipped at import collects nothing.
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
        f"{len(skips)} test(s) skipped under CI=true, so they did not run. "
        "Make them run in CI, deselect them with -m, or allow the reason in "
        "ALLOWED_CI_SKIPS (tests/ci_skips.py)."
    )
