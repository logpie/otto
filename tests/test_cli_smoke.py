from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_python_module_cli_help_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "otto.cli", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_python_module_cli_merge_help_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "otto.cli", "merge", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
