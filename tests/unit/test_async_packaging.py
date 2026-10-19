"""
Packaging contract for the optional async surface.

Two claims, which fail for different reasons:

- The ``all`` extra reaches ``async``. Every CI test job syncs with ``--extra all``, so an
  ``all`` that dropped ``async`` would stop the async suite running in CI while it still
  passed locally. The version floors are not restated here: ``pyproject.toml`` is their one
  home, with the reasoning for each written beside it.
- ``import semolina`` does not import ``anyio``. That must hold for a plain
  ``pip install semolina``, but anyio *is* installed in this dev venv, so the check runs in a
  child interpreter and looks at that process's ``sys.modules``. CI's ``packaging-smoke``
  job makes the matching claim against a real base install.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_packaging_all_extra_includes_async() -> None:
    """The ``all`` extra reaches ``async``, so CI's ``--extra all`` runs the async suite."""
    with PYPROJECT.open("rb") as fh:
        pyproject: dict[str, Any] = tomllib.load(fh)
    extras = pyproject["project"]["optional-dependencies"]

    assert any("async" in requirement for requirement in extras["all"]), extras["all"]


def test_packaging_importing_semolina_does_not_import_anyio() -> None:
    """
    ``import semolina`` leaves anyio unimported, so a base install stays clean.

    adbc-poolhouse resolves its async entry points lazily (PEP 562) precisely to keep the
    sync path anyio-free; a module-level ``from adbc_poolhouse import create_async_pool`` in
    Semolina would defeat that.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import semolina, sys; print('anyio' in sys.modules)"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", (
        f"importing semolina pulled anyio into sys.modules: {result.stdout!r}"
    )


def test_create_async_engine_without_anyio_names_semolinas_extra() -> None:
    """
    Without anyio, ``create_async_engine`` says to install ``semolina[async]``.

    adbc-poolhouse's own message names ``adbc-poolhouse[async]``, which would send the reader
    to the wrong package. anyio is installed here, so a child interpreter blocks it through
    ``sys.modules`` to stand in for a base install.
    """
    code = (
        "import sys\n"
        "sys.modules['anyio'] = None\n"
        "from adbc_poolhouse import DuckDBConfig\n"
        "from semolina.config import create_async_engine\n"
        "try:\n"
        "    create_async_engine(DuckDBConfig(database=':memory:'))\n"
        "except ImportError as exc:\n"
        "    print(exc)\n"
        "else:\n"
        "    print('no ImportError')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert "semolina[async]" in result.stdout, result.stdout
    assert "adbc-poolhouse[async]" not in result.stdout, result.stdout
