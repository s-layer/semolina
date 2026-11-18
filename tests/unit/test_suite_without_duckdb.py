"""
The test suite loads without ``duckdb`` installed.

``duckdb`` is an optional extra, not part of the ``dev`` group, so ``uv sync --dev`` followed
by ``pytest`` must give a run with skips rather than a conftest that fails to import. The
check runs pytest's collection in a child interpreter that blocks ``duckdb`` through
``sys.modules``, standing in for an environment without it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]

MODULES = [
    "unit/test_cli_packaging.py",
    "unit/test_extension_install_retry.py",
    "unit/test_async_cancel.py",
    "unit/codegen/test_dto_cli.py",
    "unit/codegen/test_codegen_e2e.py",
    "unit/codegen/test_annotation_check.py",
]
"""The root conftest's first collector, plus every module that imports the install helper."""


def test_collection_without_duckdb_loads_the_conftest() -> None:
    """Without ``duckdb``, the conftest and the helper's importers load; their tests skip."""
    code = (
        "import sys\n"
        "sys.modules['duckdb'] = None\n"
        "import pytest\n"
        "sys.exit(pytest.main(['-q', '--collect-only', '-p', 'no:xdist', '-p', 'no:cacheprovider',"
        " '--no-cov', *sys.argv[1:]]))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, *(str(TESTS / m) for m in MODULES)],
        capture_output=True,
        text=True,
        check=False,
        cwd=TESTS.parent,
        # The skips are the expected outcome here, so the CI skip check must not fail them.
        env={k: v for k, v in os.environ.items() if k != "CI"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_cli_packaging.py" in result.stdout
