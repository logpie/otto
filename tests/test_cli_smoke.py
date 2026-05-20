from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner
from rich.console import Console

from otto.cli import _print_config_banner, main
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


def test_python_module_cli_merge_command_removed() -> None:
    """Phase C.4 deleted the legacy ``otto merge`` CLI; invoking it must
    surface Click's ``No such command`` error (exit code 2).
    """
    result = subprocess.run(
        [sys.executable, "-m", "otto.cli", "merge", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "No such command 'merge'" in (result.stderr + result.stdout)


def test_config_banner_uses_effective_provider_safe_default(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "otto.yaml"
    config_path.write_text("provider: claude\n", encoding="utf-8")
    config = {
        "provider": "claude",
        "certifier_mode": "fast",
        "run_budget_seconds": 300,
        "max_certify_rounds": 2,
        "max_turns_per_call": 50,
    }
    monkeypatch.setattr("otto.cli._runtime_model_name", lambda _provider: "claude-opus-4-7")
    console = Console(record=True, width=120)

    _print_config_banner(console, config, {}, config_path, primary_agent_type="build")

    output = console.export_text()
    assert "sonnet" in output
    assert "claude-opus-4-7" not in output


