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


def test_dashboard_alias_opens_web_from_nested_directory(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    nested = repo / "pkg" / "subdir"
    nested.mkdir(parents=True)
    seen: dict[str, object] = {}

    def fake_create_app(project_dir: Path, **kwargs):
        seen["project_dir"] = project_dir
        seen["kwargs"] = kwargs
        return object()

    def fake_uvicorn_run(app, **kwargs):
        seen["app"] = app
        seen["uvicorn"] = kwargs

    monkeypatch.setattr("otto.web.app.create_app", fake_create_app)
    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)

    saved_cwd = Path.cwd()
    os.chdir(nested)
    try:
        result = CliRunner().invoke(main, ["dashboard", "--no-open"], catch_exceptions=False)
    finally:
        os.chdir(saved_cwd)

    assert result.exit_code == 0
    assert seen["project_dir"] == nested
    assert seen["uvicorn"]["host"] == "127.0.0.1"
    assert seen["uvicorn"]["port"] == 8765
    assert "`otto dashboard` is deprecated" in result.output
