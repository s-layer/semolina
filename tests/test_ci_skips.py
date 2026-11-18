"""The CI skip check in tests/ci_skips.py: a skip fails the run under CI, only there."""

from __future__ import annotations

from pathlib import Path

import ci_skips
import pytest

pytest_plugins = ["pytester"]

SUITE = """
import pytest

def test_runs():
    pass

def test_skips():
    pytest.skip("no tool here")

@pytest.mark.xfail(strict=True, reason="known gap")
def test_expected_failure():
    raise AssertionError
"""

NOT_RUN_XFAIL = """
import pytest

@pytest.mark.xfail(run=False, reason="would hang")
def test_never_runs():
    raise AssertionError
"""


def _write(pytester: pytest.Pytester, name: str, source: str) -> None:
    (pytester.path / name).write_text(source)


def _run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    return pytester.runpytest("-p", "ci_skips", "-rs", *args)


def test_a_skip_fails_the_run_under_ci(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    _write(pytester, "test_suite.py", SUITE)
    result = _run(pytester)
    result.assert_outcomes(passed=1, skipped=1, xfailed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        ["*skipped in CI*", "*test_skips: no tool here", "1 test(s) did not run under CI*"]
    )


def test_the_check_holds_without_the_terminal_reporter(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    _write(pytester, "test_suite.py", SUITE)
    result = pytester.runpytest("-p", "ci_skips", "-p", "no:terminal")
    assert result.ret == pytest.ExitCode.TESTS_FAILED


@pytest.mark.parametrize("value", ["", "0", "false", "False", "no"])
def test_a_skip_passes_outside_ci(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("CI", value)
    _write(pytester, "test_suite.py", SUITE)
    result = _run(pytester)
    result.assert_outcomes(passed=1, skipped=1, xfailed=1)
    assert result.ret == pytest.ExitCode.OK
    result.stdout.no_fnmatch_line("*skipped in CI*")


def test_a_skip_passes_with_ci_unset(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CI", raising=False)
    _write(pytester, "test_suite.py", SUITE)
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.OK
    result.stdout.no_fnmatch_line("*skipped in CI*")


def test_an_allowed_reason_passes_under_ci(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    # Anchored: the pattern sees the bare reason, without pytest's "Skipped: " prefix.
    monkeypatch.setattr(ci_skips, "ALLOWED_CI_SKIPS", (("^no tool", "for this test"),))
    _write(pytester, "test_suite.py", SUITE)
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.OK
    result.stdout.no_fnmatch_line("*skipped in CI*")


def test_an_xfail_that_never_runs_fails_the_run_under_ci(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    _write(pytester, "test_not_run.py", NOT_RUN_XFAIL)
    result = _run(pytester)
    result.assert_outcomes(xfailed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*test_never_runs: [[]NOTRUN[]] would hang"])


def test_a_failing_run_with_a_skip_stays_failed_under_ci(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    _write(pytester, "test_suite.py", SUITE)
    _write(pytester, "test_fails.py", "def test_fails():\n    raise AssertionError\n")
    result = _run(pytester)
    result.assert_outcomes(passed=1, failed=1, skipped=1, xfailed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*skipped in CI*"])


def test_an_interrupted_run_is_not_replaced_under_ci(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    _write(pytester, "test_suite.py", SUITE)
    _write(
        pytester,
        "test_zz_interrupts.py",
        'import pytest\n\ndef test_interrupts():\n    pytest.exit("stopped")\n',
    )
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.INTERRUPTED


def test_a_collection_skip_fails_the_run_under_ci(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    _write(
        pytester,
        "test_needs_module.py",
        'import pytest\npytest.importorskip("no_such_module_here")\n',
    )
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*no_such_module_here*"])


def test_a_skip_on_an_xdist_worker_fails_the_run_under_ci(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CI runs the main suite with -n auto, so the controller must see worker skips.
    monkeypatch.setenv("CI", "true")
    # The workers are fresh processes and cannot import tests/ci_skips.py, so the
    # plugin goes in the pytester rootdir, loaded by its conftest there.
    _write(pytester, "ci_skips.py", Path(ci_skips.__file__).read_text())
    _write(pytester, "conftest.py", 'pytest_plugins = ["ci_skips"]\n')
    _write(pytester, "test_suite.py", SUITE)
    _write(
        pytester,
        "test_needs_module.py",
        'import pytest\npytest.importorskip("no_such_module_here")\n',
    )
    result = pytester.runpytest("-n", "2")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*skipped in CI*"])
    result.stdout.fnmatch_lines(["*test_skips: no tool here"])
    result.stdout.fnmatch_lines(["*no_such_module_here*"])
