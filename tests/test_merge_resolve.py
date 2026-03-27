"""Tests for scoped merge-conflict reapply."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from otto.git_ops import _anchor_candidate_ref
from otto.merge_resolve import scoped_reapply


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
    )
    return result


def _write_tasks(repo: Path, task_key: str) -> Path:
    tasks_path = repo / "tasks.yaml"
    tasks_path.write_text(yaml.dump({
        "tasks": [
            {
                "id": 1,
                "key": task_key,
                "prompt": "Resolve merge conflict",
                "status": "pending",
            },
        ],
    }))
    return tasks_path


def _make_config() -> dict[str, object]:
    return {
        "default_branch": "main",
        "verify_timeout": 30,
        "test_command": "python -c \"assert True\"",
        "coding_agent_settings": "project",
    }


def _show_file(repo: Path, rev: str, path: str) -> str:
    return _git(repo, "show", f"{rev}:{path}").stdout


@pytest.mark.asyncio
async def test_scoped_reapply_cherry_pick_succeeds(tmp_git_repo):
    task_key = "task-clean"
    tasks_path = _write_tasks(tmp_git_repo, task_key)
    config = _make_config()

    (tmp_git_repo / "app.txt").write_text("base\n")
    _git(tmp_git_repo, "add", "app.txt")
    _git(tmp_git_repo, "commit", "-m", "add app")
    base_sha = _git(tmp_git_repo, "rev-parse", "HEAD").stdout.strip()

    _git(tmp_git_repo, "checkout", "-b", "candidate-clean")
    (tmp_git_repo / "feature.txt").write_text("from candidate\n")
    _git(tmp_git_repo, "add", "feature.txt")
    _git(tmp_git_repo, "commit", "-m", "candidate change")
    candidate_sha = _git(tmp_git_repo, "rev-parse", "HEAD").stdout.strip()
    candidate_ref = _anchor_candidate_ref(tmp_git_repo, task_key, 1, candidate_sha)

    _git(tmp_git_repo, "checkout", "main")
    (tmp_git_repo / "main.txt").write_text("from main\n")
    _git(tmp_git_repo, "add", "main.txt")
    _git(tmp_git_repo, "commit", "-m", "main change")
    main_before = _git(tmp_git_repo, "rev-parse", "HEAD").stdout.strip()

    agent_mock = AsyncMock()
    with patch("otto.merge_resolve.run_agent_query", agent_mock):
        success, new_sha = await scoped_reapply(
            task_key=task_key,
            candidate_ref=candidate_ref,
            base_sha=base_sha,
            config=config,
            project_dir=tmp_git_repo,
            tasks_file=tasks_path,
        )

    assert success is True
    assert new_sha
    assert agent_mock.await_count == 0
    assert _git(tmp_git_repo, "branch", "--show-current").stdout.strip() == "main"
    assert _git(tmp_git_repo, "rev-parse", "HEAD").stdout.strip() == main_before
    assert _show_file(tmp_git_repo, new_sha, "feature.txt") == "from candidate\n"


@pytest.mark.asyncio
async def test_scoped_reapply_uses_agent_after_cherry_pick_conflict(tmp_git_repo):
    task_key = "task-conflict"
    tasks_path = _write_tasks(tmp_git_repo, task_key)
    config = _make_config()

    (tmp_git_repo / "app.txt").write_text("value=base\n")
    _git(tmp_git_repo, "add", "app.txt")
    _git(tmp_git_repo, "commit", "-m", "add app")
    base_sha = _git(tmp_git_repo, "rev-parse", "HEAD").stdout.strip()

    _git(tmp_git_repo, "checkout", "-b", "candidate-conflict")
    (tmp_git_repo / "app.txt").write_text("value=candidate\n")
    _git(tmp_git_repo, "add", "app.txt")
    _git(tmp_git_repo, "commit", "-m", "candidate change")
    candidate_sha = _git(tmp_git_repo, "rev-parse", "HEAD").stdout.strip()
    candidate_ref = _anchor_candidate_ref(tmp_git_repo, task_key, 1, candidate_sha)

    _git(tmp_git_repo, "checkout", "main")
    (tmp_git_repo / "app.txt").write_text("value=main\n")
    _git(tmp_git_repo, "add", "app.txt")
    _git(tmp_git_repo, "commit", "-m", "main change")
    main_before = _git(tmp_git_repo, "rev-parse", "HEAD").stdout.strip()

    async def fake_run_agent_query(prompt, options, **kwargs):
        assert options.permission_mode == "bypassPermissions"
        assert options.max_turns == 30
        assert options.effort == "low"
        assert options.model is None
        assert options.system_prompt["type"] == "preset"
        assert options.system_prompt["preset"] == "claude_code"
        assert "merge conflict resolver" in options.system_prompt["append"].lower()
        assert "Full patch to apply:" in prompt
        assert "diff --git" in prompt
        assert "@@" in prompt
        assert "value=candidate" in prompt
        (tmp_git_repo / "app.txt").write_text("value=resolved\n")
        return "", 0.0, SimpleNamespace(is_error=False)

    with patch("otto.merge_resolve.run_agent_query", side_effect=fake_run_agent_query):
        success, new_sha = await scoped_reapply(
            task_key=task_key,
            candidate_ref=candidate_ref,
            base_sha=base_sha,
            config=config,
            project_dir=tmp_git_repo,
            tasks_file=tasks_path,
        )

    assert success is True
    assert new_sha
    assert _git(tmp_git_repo, "branch", "--show-current").stdout.strip() == "main"
    assert _git(tmp_git_repo, "rev-parse", "HEAD").stdout.strip() == main_before
    assert _show_file(tmp_git_repo, new_sha, "app.txt") == "value=resolved\n"


@pytest.mark.asyncio
async def test_scoped_reapply_returns_false_when_agent_cannot_resolve(tmp_git_repo):
    task_key = "task-fail"
    tasks_path = _write_tasks(tmp_git_repo, task_key)
    config = _make_config()

    (tmp_git_repo / "app.txt").write_text("value=base\n")
    _git(tmp_git_repo, "add", "app.txt")
    _git(tmp_git_repo, "commit", "-m", "add app")
    base_sha = _git(tmp_git_repo, "rev-parse", "HEAD").stdout.strip()

    _git(tmp_git_repo, "checkout", "-b", "candidate-fail")
    (tmp_git_repo / "app.txt").write_text("value=candidate\n")
    _git(tmp_git_repo, "add", "app.txt")
    _git(tmp_git_repo, "commit", "-m", "candidate change")
    candidate_sha = _git(tmp_git_repo, "rev-parse", "HEAD").stdout.strip()
    candidate_ref = _anchor_candidate_ref(tmp_git_repo, task_key, 1, candidate_sha)

    _git(tmp_git_repo, "checkout", "main")
    (tmp_git_repo / "app.txt").write_text("value=main\n")
    _git(tmp_git_repo, "add", "app.txt")
    _git(tmp_git_repo, "commit", "-m", "main change")
    main_before = _git(tmp_git_repo, "rev-parse", "HEAD").stdout.strip()

    with patch(
        "otto.merge_resolve.run_agent_query",
        new=AsyncMock(return_value=("", 0.0, SimpleNamespace(is_error=True))),
    ):
        success, new_sha = await scoped_reapply(
            task_key=task_key,
            candidate_ref=candidate_ref,
            base_sha=base_sha,
            config=config,
            project_dir=tmp_git_repo,
            tasks_file=tasks_path,
        )

    assert (success, new_sha) == (False, "")
    assert _git(tmp_git_repo, "branch", "--show-current").stdout.strip() == "main"
    assert _git(tmp_git_repo, "rev-parse", "HEAD").stdout.strip() == main_before
    deleted = _git(tmp_git_repo, "rev-parse", "--verify", f"otto/_scoped_reapply_{task_key}", check=False)
    assert deleted.returncode != 0
