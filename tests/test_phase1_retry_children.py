"""Phase 1 tests (plan-checkpoint-resume-v2.md): `otto v5 retry-children`
broken-state recovery — validation gate, dependency cascade, atomic
mutation, rollback safety.

These tests use minimal in-process state (no real otto runs) — the
focus is on the transaction + safety semantics. Real E2E is the
iTracker validation that follows Phase 0+1 shipping.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from otto.queue.subtask import rewrite_pending_for_retry, read_pending
from otto.queue.task_graph import (
    clear_task_for_retry,
    entry_is_satisfactory_terminal,
    get_task,
    read_graph,
)
from otto.v5_retry import (
    RetryPlan,
    ValidationFailure,
    execute_plan,
    validate_and_plan,
)


# --- helpers ----------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    """Init a git repo so otto's path utilities accept this as a project."""
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-m", "init", "-q"],
        cwd=str(path), check=True
    )


def _seed_graph(project_dir: Path, tasks: dict) -> None:
    """Write a minimal task graph for tests."""
    cross = project_dir / "otto_logs" / "cross-sessions"
    cross.mkdir(parents=True, exist_ok=True)
    graph = {"schema_version": 1, "tasks": tasks}
    (cross / "task_graph.json").write_text(
        json.dumps(graph, indent=2), encoding="utf-8"
    )


def _make_branch(project_dir: Path, branch: str) -> None:
    """Create a branch on the project repo."""
    subprocess.run(
        ["git", "branch", branch],
        cwd=str(project_dir), check=True, capture_output=True
    )


def _make_worktree(project_dir: Path, task_id: str, branch: str) -> Path:
    """Create a worktree at .worktrees/<task_id> on the given branch."""
    wt = project_dir / ".worktrees" / task_id
    wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", str(wt), branch],
        cwd=str(project_dir), check=True, capture_output=True
    )
    return wt


def _make_session_for_task(project_dir: Path, task_id: str, worktree: Path) -> Path:
    """Create a session dir with a `worktree` symlink pointing at the task's
    worktree."""
    sessions = project_dir / "otto_logs" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    sdir = sessions / f"2026-05-20-000000-{task_id[-6:]}"
    sdir.mkdir()
    (sdir / "worktree").symlink_to(str(worktree))
    return sdir


# --- entry_is_satisfactory_terminal stays correct ---------------------


def test_clear_task_for_retry_clears_blocker_metadata(tmp_path: Path):
    """clear_task_for_retry must clear merge_blocked_* + annotation_* +
    landed_with_annotation so entry_is_satisfactory_terminal correctly
    treats the retried task as not-yet-terminal."""
    _init_git_repo(tmp_path)
    _seed_graph(tmp_path, {
        "v5-blocked": {
            "id": "v5-blocked",
            "verdict": "merge_blocked",
            "completed_at": "2026-05-20T00:00:00Z",
            "merge_blocked_origin": "verification",
            "merge_blocked_reason": "child verify oracle failed",
            "merge_blocked_structured_reason": {"kind": "oracle_fail"},
            "failure_reason": "oracle",
            "annotation_origin": "verification",
            "landed_with_annotation": True,
        }
    })
    count = clear_task_for_retry(tmp_path, "v5-blocked", "test")
    assert count == 1
    t = get_task(tmp_path, "v5-blocked") or {}
    assert t.get("verdict") is None
    assert t.get("completed_at") is None
    assert t.get("merge_blocked_reason") is None
    assert t.get("merge_blocked_structured_reason") is None
    assert t.get("merge_blocked_origin") is None
    assert t.get("failure_reason") is None
    assert t.get("annotation_origin") is None
    assert t.get("landed_with_annotation") is None
    assert t.get("review_state") is None
    assert t.get("retry_reason") == "test"
    assert t.get("retry_count") == 1


# --- rewrite_pending_for_retry --------------------------------------


def test_rewrite_pending_for_retry_resets_and_supersedes(tmp_path: Path):
    """The pending rewrite resets the latest entry per task_id and
    marks older entries as superseded."""
    _init_git_repo(tmp_path)
    pending_path = tmp_path / "otto_logs" / "cross-sessions" / "v5_pending.jsonl"
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    # Three entries: two for v5-A (older + latest), one for v5-B
    entries = [
        {"task_id": "v5-A", "verdict": "merge_blocked",
         "review_state": "cancelled", "ts": "2026-05-20T00:00:00Z"},
        {"task_id": "v5-A", "verdict": "merge_blocked",
         "review_state": "cancelled", "ts": "2026-05-20T01:00:00Z"},
        {"task_id": "v5-B", "verdict": "pass",
         "review_state": "approved", "ts": "2026-05-20T00:30:00Z"},
    ]
    pending_path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )

    summary = rewrite_pending_for_retry(tmp_path, ["v5-A"])
    assert summary["rewritten"] == ["v5-A"]
    assert summary["missing"] == []
    assert summary["superseded_count"] == 1

    # Latest v5-A entry is now runnable; older one is superseded; v5-B untouched.
    lines = pending_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    # Older v5-A
    assert parsed[0]["task_id"] == "v5-A"
    assert parsed[0]["superseded"] is True
    # Newer v5-A (rewritten)
    assert parsed[1]["task_id"] == "v5-A"
    assert parsed[1]["verdict"] is None
    assert parsed[1]["review_state"] == "approved"
    assert parsed[1]["retry_count"] == 1
    assert parsed[1].get("superseded") is False
    # v5-B untouched
    assert parsed[2]["task_id"] == "v5-B"
    assert parsed[2]["verdict"] == "pass"
    assert "superseded" not in parsed[2] or not parsed[2]["superseded"]


def test_read_pending_excludes_superseded(tmp_path: Path):
    """`read_pending` must filter out `superseded=True` entries."""
    _init_git_repo(tmp_path)
    pending_path = tmp_path / "otto_logs" / "cross-sessions" / "v5_pending.jsonl"
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        {"task_id": "v5-old", "superseded": True},
        {"task_id": "v5-active", "verdict": None, "review_state": "approved"},
    ]
    pending_path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )
    out = read_pending(tmp_path)
    assert len(out) == 1
    assert out[0]["task_id"] == "v5-active"


def test_rewrite_pending_for_retry_handles_missing_entry(tmp_path: Path):
    """If a task_id has no pending entry, the rewrite reports it in
    `missing` and does NOT synthesize (caller responsibility)."""
    _init_git_repo(tmp_path)
    pending_path = tmp_path / "otto_logs" / "cross-sessions" / "v5_pending.jsonl"
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text("", encoding="utf-8")
    summary = rewrite_pending_for_retry(tmp_path, ["v5-nonexistent"])
    assert summary["rewritten"] == []
    assert summary["missing"] == ["v5-nonexistent"]


# --- validate_and_plan: validation gate ------------------------------


def test_validate_refuses_nonexistent_task(tmp_path: Path):
    _init_git_repo(tmp_path)
    _seed_graph(tmp_path, {})
    plan = validate_and_plan(
        project_dir=tmp_path, task_ids=["v5-fake"],
        cascade_dependents=False, allow_continue_dirty=False, force_pass=False,
    )
    assert not plan.ok
    assert any(f.task_id == "v5-fake" and "not found" in f.reason
               for f in plan.failures)


def test_validate_refuses_foundation_task(tmp_path: Path):
    _init_git_repo(tmp_path)
    _seed_graph(tmp_path, {
        "v5-foundation": {
            "id": "v5-foundation",
            "verdict": "pass",
            "task_role": "foundation",
        },
    })
    plan = validate_and_plan(
        project_dir=tmp_path, task_ids=["v5-foundation"],
        cascade_dependents=False, allow_continue_dirty=False, force_pass=True,
    )
    assert not plan.ok
    assert any("foundation" in f.reason for f in plan.failures)


def test_validate_refuses_non_leaf(tmp_path: Path):
    _init_git_repo(tmp_path)
    _seed_graph(tmp_path, {
        "v5-parent": {
            "id": "v5-parent",
            "verdict": "partial",
            "child_task_ids": ["v5-c1"],
        },
        "v5-c1": {
            "id": "v5-c1",
            "verdict": "pass",
            "parent_task_id": "v5-parent",
        },
    })
    plan = validate_and_plan(
        project_dir=tmp_path, task_ids=["v5-parent"],
        cascade_dependents=False, allow_continue_dirty=False, force_pass=True,
    )
    assert not plan.ok
    assert any("children" in f.reason or "leaves" in f.reason
               for f in plan.failures)


def test_validate_refuses_pass_without_force(tmp_path: Path):
    """A pass-verdict child should refuse retry unless --force."""
    _init_git_repo(tmp_path)
    _seed_graph(tmp_path, {
        "v5-pass": {
            "id": "v5-pass",
            "verdict": "pass",
        },
    })
    plan = validate_and_plan(
        project_dir=tmp_path, task_ids=["v5-pass"],
        cascade_dependents=False, allow_continue_dirty=False, force_pass=False,
    )
    assert not plan.ok
    assert any("--force" in f.reason or "pass" in f.reason
               for f in plan.failures)


def test_validate_refuses_missing_branch(tmp_path: Path):
    _init_git_repo(tmp_path)
    _seed_graph(tmp_path, {
        "v5-merge-blocked": {
            "id": "v5-merge-blocked",
            "verdict": "merge_blocked",
        },
    })
    # Don't create the branch.
    plan = validate_and_plan(
        project_dir=tmp_path, task_ids=["v5-merge-blocked"],
        cascade_dependents=False, allow_continue_dirty=False, force_pass=False,
    )
    assert not plan.ok
    assert any("branch" in f.reason for f in plan.failures)


# --- happy path -----------------------------------------------------


def test_validate_happy_path_with_branch_and_worktree(tmp_path: Path):
    _init_git_repo(tmp_path)
    _seed_graph(tmp_path, {
        "v5-blocked": {
            "id": "v5-blocked",
            "verdict": "merge_blocked",
            "merge_blocked_origin": "verification",
        },
    })
    branch = "i2p/build/v5-blocked"
    _make_branch(tmp_path, branch)
    wt = _make_worktree(tmp_path, "v5-blocked", branch)
    _make_session_for_task(tmp_path, "v5-blocked", wt)

    plan = validate_and_plan(
        project_dir=tmp_path, task_ids=["v5-blocked"],
        cascade_dependents=False, allow_continue_dirty=False, force_pass=False,
    )
    assert plan.ok, [f.reason for f in plan.failures]
    assert "v5-blocked" in plan.targets
    assert plan.worktrees["v5-blocked"] == wt
    assert "v5-blocked" in plan.sessions_to_archive


# --- dependency cascade --------------------------------------------


def test_validate_refuses_without_cascade_when_dependents_stale(tmp_path: Path):
    """If a downstream `pass` task depends on a target being retried,
    refuse without --cascade-dependents."""
    _init_git_repo(tmp_path)
    _seed_graph(tmp_path, {
        "v5-upstream": {
            "id": "v5-upstream",
            "verdict": "merge_blocked",
        },
        "v5-downstream": {
            "id": "v5-downstream",
            "verdict": "pass",
            "depends_on": ["v5-upstream"],
        },
    })
    branch = "i2p/build/v5-upstream"
    _make_branch(tmp_path, branch)
    wt = _make_worktree(tmp_path, "v5-upstream", branch)
    _make_session_for_task(tmp_path, "v5-upstream", wt)

    plan = validate_and_plan(
        project_dir=tmp_path, task_ids=["v5-upstream"],
        cascade_dependents=False, allow_continue_dirty=False, force_pass=False,
    )
    assert not plan.ok
    assert any(
        "dependency-closure" in f.task_id or "stale" in f.reason
        for f in plan.failures
    )


# --- execute_plan: full transaction --------------------------------


def test_execute_plan_archives_session_and_resets_graph(tmp_path: Path):
    """execute_plan must:
       - archive the session dir
       - reset the task graph entry (clear verdict + blocker metadata)
       - rewrite the pending file
    """
    _init_git_repo(tmp_path)
    _seed_graph(tmp_path, {
        "v5-x": {
            "id": "v5-x",
            "verdict": "merge_blocked",
            "merge_blocked_origin": "verification",
            "merge_blocked_reason": "fake",
        },
    })
    branch = "i2p/build/v5-x"
    _make_branch(tmp_path, branch)
    wt = _make_worktree(tmp_path, "v5-x", branch)
    sdir = _make_session_for_task(tmp_path, "v5-x", wt)

    # Pre-seed a pending entry for v5-x so the rewrite can find it.
    pending_path = tmp_path / "otto_logs" / "cross-sessions" / "v5_pending.jsonl"
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "task_id": "v5-x",
        "verdict": "merge_blocked",
        "review_state": "cancelled",
    }
    pending_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    plan = validate_and_plan(
        project_dir=tmp_path, task_ids=["v5-x"],
        cascade_dependents=False, allow_continue_dirty=False, force_pass=False,
    )
    assert plan.ok, [f.reason for f in plan.failures]
    result = execute_plan(project_dir=tmp_path, plan=plan)
    assert result.error is None, result.error
    assert not result.rolled_back

    # Graph reset.
    t = get_task(tmp_path, "v5-x") or {}
    assert t.get("verdict") is None
    assert t.get("merge_blocked_origin") is None
    assert t.get("retry_count") == 1

    # Session archived.
    assert "v5-x" in result.archived
    assert result.archived["v5-x"].name.startswith(sdir.name + ".archived-")
    assert not sdir.exists()

    # Pending rewritten.
    out = read_pending(tmp_path)
    assert len(out) == 1
    assert out[0]["task_id"] == "v5-x"
    assert out[0]["verdict"] is None
    assert out[0]["review_state"] == "approved"
