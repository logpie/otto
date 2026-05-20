"""Smoke test: `otto queue run` enqueues a task whose argv runs `otto run`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

OTTO_ROOT = Path(__file__).resolve().parents[1]


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Minimal git project that the queue accepts."""
    proj = tmp_path / "p"
    proj.mkdir()
    _run(["git", "init", "-q"], proj)
    (proj / "otto.yaml").write_text("")
    (proj / "intent.md").write_text("# x\n")
    return proj


def _otto(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return _run(
        ["uv", "run", "--project", str(OTTO_ROOT), "--extra", "dev",
         "python", "-m", "otto.cli", *args],
        cwd=cwd,
    )


def test_queue_run_enqueues_argv(project: Path) -> None:
    result = _otto(["queue", "run", "build a tiny CLI", "--as", "tiny"], project)
    assert result.returncode == 0, result.stderr or result.stdout

    queue_file = project / ".otto-queue.yml"
    assert queue_file.exists()
    data = yaml.safe_load(queue_file.read_text())
    tasks = data["tasks"]
    assert len(tasks) == 1
    t = tasks[0]
    assert t["id"] == "tiny"
    assert t["command_argv"][0] == "run"
    assert "build a tiny CLI" in t["command_argv"]
    assert "--tier" in t["command_argv"]
    assert t["resumable"] is False  # run-resume deferred


def test_queue_run_with_explicit_tier(project: Path) -> None:
    result = _otto(
        ["queue", "run", "multi-subsystem chat", "--tier", "modular", "--as", "chat"],
        project,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    data = yaml.safe_load((project / ".otto-queue.yml").read_text())
    argv = data["tasks"][0]["command_argv"]
    tier_idx = argv.index("--tier")
    assert argv[tier_idx + 1] == "modular"


def test_queue_run_passthrough_extra_args(project: Path) -> None:
    result = _otto(
        ["queue", "run", "intent here", "--as", "x", "--",
         "--max-parallel", "2", "--tree-budget-usd", "5"],
        project,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    argv = yaml.safe_load((project / ".otto-queue.yml").read_text())["tasks"][0]["command_argv"]
    assert "--max-parallel" in argv
    assert argv[argv.index("--max-parallel") + 1] == "2"
    assert "--tree-budget-usd" in argv
