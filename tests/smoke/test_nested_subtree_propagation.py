"""0% LLM smoke regression for nested subtree propagation.

This pins the v6b/v6c failure shape: root emits a frontend parent, that
frontend parent emits grandchildren, the grandchildren merge into the
frontend integration branch, and the frontend subtree must then propagate
up to main. Passing inside the subtree is not enough.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, cast

import pytest

from otto.lead import LeadResult
from otto.queue.subtask import v5_pending_path
from otto.queue.task_graph import get_task, record_task, set_decomposition, set_verdict
from otto.v5_branching import child_branch_name, integration_branch_name
from otto.v5_runner import ROOT_TASK_ID, _process_children, _propagate_subtree_integration

from .conftest import git


pytestmark = pytest.mark.smoke


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _enqueue_task(
    repo: Path,
    *,
    task_id: str,
    parent_task_id: str,
    intent: str,
) -> None:
    parent_session_dir = repo / "otto_logs" / "sessions" / f"session-{parent_task_id}"
    parent_session_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "schema_version": 1,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "parent_session_dir": str(parent_session_dir),
        "intent": intent,
        "depends_on": [],
        "owned_paths": [],
        "action_ids": [],
        "integration_branch": integration_branch_name(parent_task_id),
        "review_state": "approved",
        "enqueued_at": _now_iso(),
    }
    pending = v5_pending_path(repo)
    pending.parent.mkdir(parents=True, exist_ok=True)
    with pending.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    record_task(
        repo,
        task_id=task_id,
        intent=intent,
        parent_task_id=parent_task_id,
        integration_branch=cast(str, entry["integration_branch"]),
    )


def _worktree(project_dir: Path, session_dir: Path) -> Path:
    linked = session_dir / "worktree"
    return linked.resolve() if linked.exists() else project_dir


class FakeLead:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.children = {
            "v5-frontend": ["v5-login", "v5-dashboard", "v5-settings"],
        }
        self.files = {
            "v5-login": {
                "frontend/src/pages/Login.tsx": "export function Login(){return 'login'}\n",
            },
            "v5-dashboard": {
                "frontend/src/pages/Dashboard.tsx": "export function Dashboard(){return 'dash'}\n",
            },
            "v5-settings": {
                "frontend/src/pages/Settings.tsx": "export function Settings(){return 'settings'}\n",
            },
        }
        self._emitted: set[str] = set()

    async def __call__(self, **kwargs: Any) -> LeadResult:
        task_id = kwargs["task_id"]
        if kwargs.get("kind") == "integration":
            record_task(
                self.repo,
                task_id=task_id,
                intent=kwargs["intent"],
                integration_branch=kwargs["integration_branch"],
            )
            marker = _worktree(kwargs["project_dir"], kwargs["session_dir"]) / "docs" / f"integration-{task_id}.md"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(f"integration marker for {task_id}\n", encoding="utf-8")
            set_verdict(self.repo, task_id, "pass", cost_usd=0.0)
            return LeadResult(task_id=task_id, verdict="pass", decomposition="inline")

        if task_id in self.children:
            if task_id not in self._emitted:
                for child_id in self.children[task_id]:
                    _enqueue_task(
                        self.repo,
                        task_id=child_id,
                        parent_task_id=task_id,
                        intent=f"Build {child_id}",
                    )
                self._emitted.add(task_id)
            set_decomposition(self.repo, task_id, "emit")
            set_verdict(self.repo, task_id, "pending_children", cost_usd=0.0)
            return LeadResult(
                task_id=task_id,
                verdict="pending_children",
                decomposition="emit",
                emitted_subtask_ids=list(self.children[task_id]),
            )

        root = _worktree(kwargs["project_dir"], kwargs["session_dir"])
        for rel_path, content in self.files[task_id].items():
            target = root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        set_decomposition(self.repo, task_id, "inline")
        set_verdict(self.repo, task_id, "pass", cost_usd=0.0)
        return LeadResult(task_id=task_id, verdict="pass", decomposition="inline")


def _assert_main_has(repo: Path, *, task_id: str, rel_path: str, content: str) -> None:
    tree = git(repo, "ls-tree", "-r", "--name-only", "main", "--", rel_path, check=True).stdout.splitlines()
    assert rel_path in tree
    assert git(repo, "show", f"main:{rel_path}", check=True).stdout == content

    branch = child_branch_name(task_id)
    file_commit = git(repo, "log", "-1", "--format=%H", branch, "--", rel_path, check=True).stdout.strip()
    assert file_commit
    assert git(repo, "merge-base", "--is-ancestor", file_commit, "main").returncode == 0


def test_subtree_propagation_self_merge_marks_merge_blocked(tmp_path: Path) -> None:
    record_task(
        tmp_path,
        task_id="v5-loop",
        intent="malformed parent loop",
        parent_task_id="v5-loop",
        integration_branch=integration_branch_name("v5-loop"),
    )

    ok, detail, source, target = _propagate_subtree_integration(
        project_dir=tmp_path,
        task_id="v5-loop",
    )

    assert ok is False
    assert source == target == integration_branch_name("v5-loop")
    assert "self-merge" in detail
    assert (get_task(tmp_path, "v5-loop") or {}).get("verdict") == "merge_blocked"


@pytest.mark.asyncio
async def test_root_frontend_grandchildren_reach_main(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """This test reproduces the v6c silent self-merge no-op bug.

    Without the fix, target collapses to source and the test fails.
    """
    record_task(git_repo, task_id=ROOT_TASK_ID, intent="root", parent_task_id=None)
    set_decomposition(git_repo, ROOT_TASK_ID, "emit")
    set_verdict(git_repo, ROOT_TASK_ID, "pending_children")
    _enqueue_task(
        git_repo,
        task_id="v5-frontend",
        parent_task_id=ROOT_TASK_ID,
        intent="Build frontend and decompose feature pages",
    )

    from otto import v5_runner

    async def fake_smoke_preflight(**_kwargs):
        return {"check": "smoke_clean_deploy", "passed": True, "issues": []}

    monkeypatch.setattr(v5_runner, "run_lead", FakeLead(git_repo))
    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight_with_repair", fake_smoke_preflight)

    child_results: dict[str, LeadResult] = {}
    integration_results: dict[str, LeadResult] = {}
    await _process_children(
        project_dir=git_repo,
        parent_task_id=ROOT_TASK_ID,
        config={"default_branch": "main"},
        max_parallel=1,
        tree_budget_usd=1.0,
        child_results=child_results,
        integration_results=integration_results,
    )

    for task_id, rel_path, content in [
        ("v5-login", "frontend/src/pages/Login.tsx", "export function Login(){return 'login'}\n"),
        ("v5-dashboard", "frontend/src/pages/Dashboard.tsx", "export function Dashboard(){return 'dash'}\n"),
        ("v5-settings", "frontend/src/pages/Settings.tsx", "export function Settings(){return 'settings'}\n"),
    ]:
        _assert_main_has(git_repo, task_id=task_id, rel_path=rel_path, content=content)
