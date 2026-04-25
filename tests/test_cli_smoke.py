from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner

from otto.cli import main
from tests._helpers import init_repo


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


def test_dashboard_uses_repo_root_from_nested_directory(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    nested = repo / "pkg" / "subdir"
    nested.mkdir(parents=True)
    seen: dict[str, object] = {}

    class FakeMissionControlApp:
        def __init__(self, project_dir: Path, *, dashboard_mouse: bool = False):
            seen["project_dir"] = project_dir
            seen["dashboard_mouse"] = dashboard_mouse

        def run(self, *, mouse: bool = False) -> int:
            seen["mouse"] = mouse
            return 0

    monkeypatch.setattr("otto.tui.mission_control.MissionControlApp", FakeMissionControlApp)

    saved_cwd = Path.cwd()
    os.chdir(nested)
    try:
        result = CliRunner().invoke(main, ["dashboard"], catch_exceptions=False)
    finally:
        os.chdir(saved_cwd)

    assert result.exit_code == 0
    assert seen == {"project_dir": repo.resolve(), "dashboard_mouse": False, "mouse": False}
