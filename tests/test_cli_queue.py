"""Tests for otto/cli_queue.py — Phase 2.3-2.6 CLI surface."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

import otto.cli_queue as cli_queue_module
from otto.cli import main
from otto.queue.schema import (
    COMMANDS_FILE,
    QUEUE_FILE,
    load_queue,
)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=repo, check=True)
    return repo


def _run(args: list[str], *, cwd: Path) -> tuple[int, str, str]:
    """Run otto CLI in `cwd`. Returns (exit_code, stdout, stderr)."""
    runner = CliRunner()
    saved_cwd = os.getcwd()
    os.chdir(cwd)
    try:
        result = runner.invoke(main, args, catch_exceptions=False)
    finally:
        os.chdir(saved_cwd)
    return result.exit_code, result.output, ""


# ---------- enqueue commands ----------


def test_queue_build_appends_to_queue_yml(tmp_path: Path):
    repo = _init_repo(tmp_path)
    code, out, _ = _run(["queue", "build", "add csv export"], cwd=repo)
    assert code == 0, out
    tasks = load_queue(repo)
    assert len(tasks) == 1
    assert tasks[0].command_argv == ["build", "add csv export"]
    assert tasks[0].resolved_intent == "add csv export"
    assert tasks[0].resumable is True
    assert tasks[0].id == "add-csv-export"
    assert tasks[0].branch == "build/add-csv-export-" + cli_queue_module.time.strftime("%Y-%m-%d")
    assert tasks[0].worktree == ".worktrees/add-csv-export"


def test_queue_certify_marked_not_resumable(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "intent.md").write_text("test product")
    code, out, _ = _run(["queue", "certify"], cwd=repo)
    assert code == 0, out
    tasks = load_queue(repo)
    assert tasks[0].resumable is False
    assert tasks[0].resolved_intent == "test product"


def test_queue_certify_explicit_intent_overrides_project_files(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "intent.md").write_text("from project files")
    code, out, _ = _run(["queue", "certify", "from cli"], cwd=repo)
    assert code == 0, out
    tasks = load_queue(repo)
    assert tasks[0].resolved_intent == "from cli"


def test_queue_improve_bugs(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "intent.md").write_text("a product")
    code, out, _ = _run(["queue", "improve", "bugs", "error handling"], cwd=repo)
    assert code == 0, out
    tasks = load_queue(repo)
    assert tasks[0].command_argv[:2] == ["improve", "bugs"]
    assert tasks[0].focus == "error handling"
    assert tasks[0].resolved_intent == "a product"


def test_queue_improve_target_focus_not_set(tmp_path: Path):
    """For target subcommand, the arg goes to `target` not `focus`."""
    repo = _init_repo(tmp_path)
    (repo / "intent.md").write_text("a product")
    code, out, _ = _run(["queue", "improve", "target", "latency < 100ms"], cwd=repo)
    assert code == 0, out
    tasks = load_queue(repo)
    assert tasks[0].target == "latency < 100ms"
    assert tasks[0].focus is None


def test_queue_build_rejects_resume_in_args(tmp_path: Path):
    repo = _init_repo(tmp_path)
    code, out, _ = _run(["queue", "build", "test", "--resume"], cwd=repo)
    assert code == 2
    assert "--resume is not allowed" in out


def test_queue_build_explicit_as(tmp_path: Path):
    repo = _init_repo(tmp_path)
    code, out, _ = _run(["queue", "build", "test", "--as", "my-id"], cwd=repo)
    assert code == 0, out
    tasks = load_queue(repo)
    assert tasks[0].id == "my-id"


def test_queue_build_explicit_as_rejects_reserved(tmp_path: Path):
    repo = _init_repo(tmp_path)
    code, out, _ = _run(["queue", "build", "test", "--as", "ls"], cwd=repo)
    assert code == 2
    assert "reserved" in out


def test_queue_build_dedup_appends_suffix(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _run(["queue", "build", "same intent"], cwd=repo)
    _run(["queue", "build", "same intent"], cwd=repo)
    tasks = load_queue(repo)
    ids = [t.id for t in tasks]
    assert ids == ["same-intent", "same-intent-2"]
    assert tasks[0].branch != tasks[1].branch
    assert tasks[0].worktree != tasks[1].worktree


def test_queue_build_after_validates_existing(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _run(["queue", "build", "first"], cwd=repo)
    code, out, _ = _run(["queue", "build", "second", "--after", "first"], cwd=repo)
    assert code == 0, out
    tasks = load_queue(repo)
    assert tasks[1].after == ["first"]


def test_queue_build_after_rejects_unknown(tmp_path: Path):
    repo = _init_repo(tmp_path)
    code, out, _ = _run(["queue", "build", "test", "--after", "nonexistent"], cwd=repo)
    assert code == 2
    assert "unknown task" in out


def test_queue_build_rejects_unknown_target_flag(tmp_path: Path):
    repo = _init_repo(tmp_path)
    code, out, _ = _run(["queue", "build", "test", "--bogus-flag"], cwd=repo)
    assert code == 2
    assert "No such option: --bogus-flag" in out


def test_queue_improve_rejects_missing_option_value(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "intent.md").write_text("a product")
    code, out, _ = _run(["queue", "improve", "bugs", "--rounds"], cwd=repo)
    assert code == 2
    assert "Option '--rounds' requires an argument" in out


def test_queue_improve_accepts_valid_target_args(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "intent.md").write_text("a product")
    code, out, _ = _run(["queue", "improve", "bugs", "errors", "--rounds", "4"], cwd=repo)
    assert code == 0, out
    tasks = load_queue(repo)
    assert tasks[0].command_argv == ["improve", "bugs", "errors", "--rounds", "4"]


# ---------- ls / show ----------


def test_queue_ls_empty(tmp_path: Path):
    repo = _init_repo(tmp_path)
    code, out, _ = _run(["queue", "ls"], cwd=repo)
    assert code == 0
    assert "Queue is empty" in out


def test_queue_ls_shows_tasks(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _run(["queue", "build", "csv export"], cwd=repo)
    _run(["queue", "build", "settings page"], cwd=repo)
    code, out, _ = _run(["queue", "ls"], cwd=repo)
    assert code == 0
    assert "csv-export" in out
    assert "settings-page" in out


def test_queue_show_existing_task(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _run(["queue", "build", "csv export"], cwd=repo)
    code, out, _ = _run(["queue", "show", "csv-export"], cwd=repo)
    assert code == 0
    assert "csv-export" in out
    assert "queued" in out
    assert "Resumable: True" in out


def test_queue_show_unknown_task(tmp_path: Path):
    repo = _init_repo(tmp_path)
    code, out, _ = _run(["queue", "show", "nonexistent"], cwd=repo)
    assert code == 2
    assert "No such task" in out


# ---------- rm / cancel ----------


def test_queue_rm_appends_command(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _run(["queue", "build", "csv"], cwd=repo)
    code, out, _ = _run(["queue", "rm", "csv"], cwd=repo)
    assert code == 0
    cmds_path = repo / COMMANDS_FILE
    assert cmds_path.exists()
    cmds = [json.loads(line) for line in cmds_path.read_text().splitlines() if line.strip()]
    assert len(cmds) == 1
    assert cmds[0]["cmd"] == "remove"
    assert cmds[0]["id"] == "csv"


def test_queue_cancel_appends_command(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _run(["queue", "build", "csv"], cwd=repo)
    code, out, _ = _run(["queue", "cancel", "csv"], cwd=repo)
    assert code == 0
    cmds_path = repo / COMMANDS_FILE
    cmds = [json.loads(line) for line in cmds_path.read_text().splitlines() if line.strip()]
    assert cmds[0]["cmd"] == "cancel"


def test_queue_rm_rejects_unknown_task(tmp_path: Path):
    repo = _init_repo(tmp_path)
    code, out, _ = _run(["queue", "rm", "nonexistent"], cwd=repo)
    assert code == 2
    assert "No such task" in out


# ---------- file format integrity ----------


def test_queue_yml_uses_schema_v1(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _run(["queue", "build", "test"], cwd=repo)
    import yaml
    raw = yaml.safe_load((repo / QUEUE_FILE).read_text())
    assert raw["schema_version"] == 1
    assert isinstance(raw["tasks"], list)


def test_resolve_otto_bin_fallback_returns_argv(monkeypatch, tmp_path: Path):
    fake_python = tmp_path / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text("")
    monkeypatch.setattr(cli_queue_module.sys, "executable", str(fake_python))
    assert cli_queue_module._resolve_otto_bin() == [str(fake_python), "-m", "otto.cli"]
