"""The CI skip check in tests/ci_skips.py: a skip fails the run under CI=true, only there."""

from __future__ import annotations

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


def _run(pytester: pytest.Pytester) -> pytest.RunResult:
    (pytester.path / "test_suite.py").write_text(SUITE)
    return pytester.runpytest("-p", "ci_skips", "-rs")


def test_a_skip_fails_the_run_under_ci(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    result = _run(pytester)
    result.assert_outcomes(passed=1, skipped=1, xfailed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        ["*skipped in CI*", "*test_skips*no tool here*", "1 test(s) skipped under CI=true*"]
    )


def test_a_skip_passes_outside_ci(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CI", raising=False)
    result = _run(pytester)
    result.assert_outcomes(passed=1, skipped=1, xfailed=1)
    assert result.ret == pytest.ExitCode.OK
    result.stdout.no_fnmatch_line("*skipped in CI*")


def test_an_allowed_reason_passes_under_ci(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(ci_skips, "ALLOWED_CI_SKIPS", (("no tool", "for this test"),))
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.OK


def test_a_failing_run_stays_failed_under_ci(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    (pytester.path / "test_fails.py").write_text("def test_fails():\n    raise AssertionError\n")
    result = pytester.runpytest("-p", "ci_skips")
    assert result.ret == pytest.ExitCode.TESTS_FAILED


def test_a_collection_skip_fails_the_run_under_ci(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    (pytester.path / "test_needs_module.py").write_text(
        'import pytest\npytest.importorskip("no_such_module_here")\n'
    )
    result = pytester.runpytest("-p", "ci_skips")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*no_such_module_here*"])
