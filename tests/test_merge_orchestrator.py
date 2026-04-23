"""Tests for otto/merge/orchestrator.py — Python-driven merge loop.

Strategy: most tests exercise the orchestrator without invoking real
agents (set provider=codex to short-circuit, or use --fast). Real-LLM
E2E lives in a separate test suite gated by an env var.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import otto.merge.orchestrator as orchestrator_module
from otto import paths
from otto.certifier.report import CertificationOutcome, CertificationReport
from otto.merge import conflict_agent
from otto.merge.orchestrator import (
    MergeOptions,
    MergeAlreadyRunning,
    MergeRunResult,
    _run_post_merge_verification,
    _graduate_merged_task_sessions,
    merge_lock,
    run_merge,
)
from otto.merge import git_ops
from otto.merge.state import MergeState, find_latest_merge_id, load_state
from otto.queue.schema import QueueTask, append_task
from otto.runs.history import append_history_snapshot, read_history_rows
from otto.runs.registry import load_live_record
from tests._helpers import init_repo


def _init_repo_with_gitattributes(tmp_path: Path) -> Path:
    repo = init_repo(tmp_path, commit_content="baseline\n", commit_msg="initial")
    # Install bookkeeping merge drivers (Phase 1.6 precondition)
    from otto.setup_gitattributes import install
    install(repo)
    subprocess.run(["git", "add", ".gitattributes"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "gitattrs"], cwd=repo, check=True)
    return repo


def _make_branch(repo: Path, name: str, file: str, content: str):
    subprocess.run(["git", "checkout", "-b", name], cwd=repo, capture_output=True, check=True)
    (repo / file).write_text(content)
    subprocess.run(["git", "add", "--", file], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"{name}: change"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, capture_output=True, check=True)


def _create_worktree(repo: Path, name: str) -> Path:
    worktree = repo / ".worktrees" / name
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", f"build/{name}", str(worktree)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return worktree


def _seed_queue_task(repo: Path, *, task_id: str, worktree: Path) -> None:
    append_task(repo, QueueTask(
        id=task_id,
        command_argv=["build", task_id],
        added_at="2026-04-21T00:00:00Z",
        branch=f"build/{task_id}",
        worktree=str(worktree.relative_to(repo)),
    ))


def _config_codex_provider() -> dict[str, Any]:
    """Provider=codex is allowed only in `--fast` mode for merge."""
    return {"provider": "codex", "default_branch": "main", "queue": {"bookkeeping_files": []}}


def _config_no_bookkeeping() -> dict[str, Any]:
    """Skip the .gitattributes precondition for tests that don't care."""
    return {"default_branch": "main", "queue": {"bookkeeping_files": []}}


# ---------- pre-flight checks ----------


def test_merge_refuses_when_not_on_target(tmp_path: Path):
    repo = _init_repo_with_gitattributes(tmp_path)
    _make_branch(repo, "feat-a", "a.txt", "A\n")
    subprocess.run(["git", "checkout", "feat-a"], cwd=repo, check=True, capture_output=True)
    result = asyncio.run(run_merge(
        project_dir=repo,
        config=_config_no_bookkeeping(),
        options=MergeOptions(target="main", no_certify=True, allow_any_branch=True),
        explicit_ids_or_branches=["feat-a"],
    ))
    assert result.success is False
    assert "must be on 'main'" in result.note


def test_merge_refuses_when_dirty(tmp_path: Path):
    repo = _init_repo_with_gitattributes(tmp_path)
    _make_branch(repo, "feat-a", "a.txt", "A\n")
    (repo / "dirty.txt").write_text("uncommitted\n")
    subprocess.run(["git", "add", "dirty.txt"], cwd=repo, check=True)
    result = asyncio.run(run_merge(
        project_dir=repo,
        config=_config_no_bookkeeping(),
        options=MergeOptions(target="main", no_certify=True, allow_any_branch=True),
        explicit_ids_or_branches=["feat-a"],
    ))
    assert result.success is False
    assert "clean" in result.note
    assert "dirty.txt" in result.note
    assert "Commit, stash, or clean" in result.note


def test_merge_refuses_unknown_branch(tmp_path: Path):
    repo = _init_repo_with_gitattributes(tmp_path)
    with pytest.raises(ValueError, match="unknown task id or branch"):
        asyncio.run(run_merge(
            project_dir=repo,
            config=_config_no_bookkeeping(),
            options=MergeOptions(target="main", no_certify=True),
            explicit_ids_or_branches=["does-not-exist"],
        ))


def test_merge_refuses_unmanaged_local_branch_by_default(tmp_path: Path):
    repo = _init_repo_with_gitattributes(tmp_path)
    _make_branch(repo, "feature/random", "a.txt", "A\n")

    with pytest.raises(ValueError, match="not a queue task or atomic-mode branch"):
        asyncio.run(run_merge(
            project_dir=repo,
            config=_config_no_bookkeeping(),
            options=MergeOptions(target="main", no_certify=True),
            explicit_ids_or_branches=["feature/random"],
        ))


def test_merge_allows_unmanaged_local_branch_with_escape_hatch(tmp_path: Path):
    repo = _init_repo_with_gitattributes(tmp_path)
    _make_branch(repo, "feature/random", "a.txt", "A\n")

    result = asyncio.run(run_merge(
        project_dir=repo,
        config=_config_no_bookkeeping(),
        options=MergeOptions(target="main", no_certify=True, allow_any_branch=True),
        explicit_ids_or_branches=["feature/random"],
    ))

    assert result.success is True
    assert (repo / "a.txt").exists()


# ---------- clean merges (no agent) ----------


def test_clean_merge_two_independent_branches(tmp_path: Path):
    repo = _init_repo_with_gitattributes(tmp_path)
    _make_branch(repo, "feat-a", "a.txt", "A\n")
    _make_branch(repo, "feat-b", "b.txt", "B\n")
    result = asyncio.run(run_merge(
        project_dir=repo,
        config=_config_no_bookkeeping(),
        options=MergeOptions(target="main", no_certify=True, allow_any_branch=True),
        explicit_ids_or_branches=["feat-a", "feat-b"],
    ))
    assert result.success, f"expected success, got: {result.note}"
    assert len(result.state.outcomes) == 2
    assert all(o.status == "merged" for o in result.state.outcomes)
    assert (repo / "a.txt").exists()
    assert (repo / "b.txt").exists()


def test_clean_merge_single_branch(tmp_path: Path):
    repo = _init_repo_with_gitattributes(tmp_path)
    _make_branch(repo, "feat-x", "x.txt", "X\n")
    result = asyncio.run(run_merge(
        project_dir=repo,
        config=_config_no_bookkeeping(),
        options=MergeOptions(target="main", no_certify=True, allow_any_branch=True),
        explicit_ids_or_branches=["feat-x"],
    ))
    assert result.success
    assert (repo / "x.txt").exists()


# ---------- conflict + --fast (bail without agent) ----------


def test_fast_mode_bails_on_conflict(tmp_path: Path):
    repo = _init_repo_with_gitattributes(tmp_path)
    _make_branch(repo, "feat-a", "f.txt", "A's content\n")
    _make_branch(repo, "feat-b", "f.txt", "B's content\n")
    result = asyncio.run(run_merge(
        project_dir=repo,
        config=_config_no_bookkeeping(),
        options=MergeOptions(target="main", no_certify=True, fast=True, allow_any_branch=True),
        explicit_ids_or_branches=["feat-a", "feat-b"],
    ))
    assert result.success is False
    assert "--fast" in result.note or "fast" in result.note
    # First branch merged cleanly; second hit conflict
    statuses = [o.status for o in result.state.outcomes]
    assert "merged" in statuses
    assert "agent_giveup" in statuses
    # Working tree should be left dirty for manual resolution.
    assert git_ops.merge_in_progress(repo)
    git_ops.merge_abort(repo)  # cleanup


# ---------- provider=codex rejects conflict agent ----------


def test_conflict_with_codex_provider_refused(tmp_path: Path):
    """Codex provider must refuse before merging any branch unless `--fast` is used."""
    repo = _init_repo_with_gitattributes(tmp_path)
    _make_branch(repo, "feat-a", "f.txt", "A\n")
    _make_branch(repo, "feat-b", "f.txt", "B\n")
    original_head = git_ops.head_sha(repo)
    result = asyncio.run(run_merge(
        project_dir=repo,
        config=_config_codex_provider(),
        options=MergeOptions(target="main", no_certify=True, allow_any_branch=True),
        explicit_ids_or_branches=["feat-a", "feat-b"],
    ))
    assert result.success is False
    assert "claude" in result.note.lower()
    assert "--fast" in result.note
    assert result.merge_id == ""
    assert result.state.outcomes == []
    assert git_ops.head_sha(repo) == original_head
    assert git_ops.merge_in_progress(repo) is False


def test_codex_provider_allowed_in_fast_mode(tmp_path: Path):
    repo = _init_repo_with_gitattributes(tmp_path)
    _make_branch(repo, "feat-a", "f.txt", "A\n")
    _make_branch(repo, "feat-b", "f.txt", "B\n")
    result = asyncio.run(run_merge(
        project_dir=repo,
        config=_config_codex_provider(),
        options=MergeOptions(target="main", no_certify=True, fast=True, allow_any_branch=True),
        explicit_ids_or_branches=["feat-a", "feat-b"],
    ))
    assert result.success is False
    assert "fast" in result.note.lower()
    assert [o.status for o in result.state.outcomes] == ["merged", "agent_giveup"]
    assert git_ops.merge_in_progress(repo)
    git_ops.merge_abort(repo)


def test_conflict_agent_cleans_new_untracked_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _init_repo_with_gitattributes(tmp_path)
    _make_branch(repo, "feat-a", "f.txt", "A\n")
    _make_branch(repo, "feat-b", "f.txt", "B\n")

    async def fake_run_agent_with_timeout(*args, **kwargs):
        # Resolve markers in conflict files, but also create a stray
        # untracked file that the validator should clean up.
        for f in repo.glob("f.txt"):
            f.write_text("resolved\n")
        (repo / "extra.txt").write_text("stray\n")
        return ("done", 0.0, "", {})

    with patch("otto.agent.run_agent_with_timeout", side_effect=fake_run_agent_with_timeout):
        result = asyncio.run(run_merge(
            project_dir=repo,
            config=_config_no_bookkeeping(),
            options=MergeOptions(target="main", no_certify=True, allow_any_branch=True),
            explicit_ids_or_branches=["feat-a", "feat-b"],
        ))

    assert result.success is True
    assert not (repo / "extra.txt").exists()


def test_consolidated_merge_captures_merge_aware_diff_with_both_sides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    repo = _init_repo_with_gitattributes(tmp_path)
    _make_branch(repo, "feat-a", "f.txt", "A\n")
    _make_branch(repo, "feat-b", "f.txt", "B\n")

    captured: dict[str, str] = {}

    async def fake_resolve_all_conflicts(*, project_dir: Path, config: dict[str, Any], ctx, **kwargs):
        captured["diff"] = ctx.conflict_diff
        return conflict_agent.ConflictResolutionAttempt(
            success=False,
            note="stop after capturing diff",
        )

    monkeypatch.setattr(conflict_agent, "resolve_all_conflicts", fake_resolve_all_conflicts)

    result = asyncio.run(run_merge(
        project_dir=repo,
        config=_config_no_bookkeeping(),
        options=MergeOptions(target="main", no_certify=True, allow_any_branch=True),
        explicit_ids_or_branches=["feat-a", "feat-b"],
    ))

    assert result.success is False
    assert "<<<<<<<" in captured["diff"]
    assert ">>>>>>>" in captured["diff"]


def test_consolidated_merge_upgrades_conflicted_branch_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    repo = _init_repo_with_gitattributes(tmp_path)
    (repo / "f1.txt").write_text("base-1\n")
    (repo / "f2.txt").write_text("base-2\n")
    subprocess.run(["git", "add", "f1.txt", "f2.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add conflict fixtures"], cwd=repo, check=True)

    subprocess.run(["git", "checkout", "-b", "feat-a"], cwd=repo, capture_output=True, check=True)
    (repo / "f1.txt").write_text("feat-a-1\n")
    (repo / "f2.txt").write_text("feat-a-2\n")
    subprocess.run(["git", "add", "f1.txt", "f2.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat-a: change both files"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, capture_output=True, check=True)

    subprocess.run(["git", "checkout", "-b", "feat-b"], cwd=repo, capture_output=True, check=True)
    (repo / "f1.txt").write_text("feat-b-1\n")
    subprocess.run(["git", "add", "f1.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat-b: conflict on f1"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, capture_output=True, check=True)

    subprocess.run(["git", "checkout", "-b", "feat-c"], cwd=repo, capture_output=True, check=True)
    (repo / "f2.txt").write_text("feat-c-2\n")
    subprocess.run(["git", "add", "f2.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat-c: conflict on f2"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, capture_output=True, check=True)

    async def fake_resolve_all_conflicts(*, project_dir: Path, config: dict[str, Any], ctx, **kwargs):
        assert ctx.conflict_files == ["f1.txt", "f2.txt"]
        (project_dir / "f1.txt").write_text("resolved-f1\n")
        (project_dir / "f2.txt").write_text("resolved-f2\n")
        return conflict_agent.ConflictResolutionAttempt(
            success=True,
            note="resolved for test",
            cost_usd=12.34,
            edited_files={"f1.txt", "f2.txt"},
        )

    monkeypatch.setattr(conflict_agent, "resolve_all_conflicts", fake_resolve_all_conflicts)

    result = asyncio.run(run_merge(
        project_dir=repo,
        config=_config_no_bookkeeping(),
        options=MergeOptions(target="main", no_certify=True, allow_any_branch=True),
        explicit_ids_or_branches=["feat-a", "feat-b", "feat-c"],
    ))

    assert result.success, result.note
    outcome_by_branch = {outcome.branch: outcome for outcome in result.state.outcomes}
    assert outcome_by_branch["feat-a"].status == "merged"
    assert outcome_by_branch["feat-b"].status == "conflict_resolved"
    assert outcome_by_branch["feat-c"].status == "conflict_resolved"
    assert "(consolidated)" not in outcome_by_branch
    assert "shared call" in outcome_by_branch["feat-b"].note
    assert "shared call" in outcome_by_branch["feat-c"].note

    persisted = load_state(repo, result.merge_id)
    persisted_by_branch = {outcome.branch: outcome for outcome in persisted.outcomes}
    assert persisted_by_branch["feat-b"].status == "conflict_resolved"
    assert persisted_by_branch["feat-c"].status == "conflict_resolved"
    assert "(consolidated)" not in persisted_by_branch


def test_conflict_resolved_merge_still_runs_cleanup_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    repo = _init_repo_with_gitattributes(tmp_path)
    (repo / "shared.txt").write_text("base\n")
    subprocess.run(["git", "add", "shared.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add shared fixture"], cwd=repo, check=True)

    subprocess.run(["git", "checkout", "-b", "feat-a"], cwd=repo, capture_output=True, check=True)
    (repo / "shared.txt").write_text("feat-a\n")
    subprocess.run(["git", "add", "shared.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat-a change"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, capture_output=True, check=True)

    subprocess.run(["git", "checkout", "-b", "feat-b"], cwd=repo, capture_output=True, check=True)
    (repo / "shared.txt").write_text("feat-b\n")
    subprocess.run(["git", "add", "shared.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat-b change"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, capture_output=True, check=True)

    append_task(
        repo,
        QueueTask(
            id="task-a",
            command_argv=["build", "task-a"],
            added_at="2026-04-23T00:00:00Z",
            branch="feat-a",
            worktree=".worktrees/task-a",
        ),
    )
    append_task(
        repo,
        QueueTask(
            id="task-b",
            command_argv=["build", "task-b"],
            added_at="2026-04-23T00:00:00Z",
            branch="feat-b",
            worktree=".worktrees/task-b",
        ),
    )
    lock_path = repo / ".otto-queue.yml.lock"
    if lock_path.exists():
        lock_path.unlink()
    subprocess.run(["git", "add", ".otto-queue.yml"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed queue tasks"], cwd=repo, check=True)

    async def fake_resolve_all_conflicts(*, project_dir: Path, config: dict[str, Any], ctx, **kwargs):
        del config, kwargs
        assert ctx.conflict_files == ["shared.txt"]
        (project_dir / "shared.txt").write_text("resolved\n")
        return conflict_agent.ConflictResolutionAttempt(
            success=True,
            note="resolved for cleanup test",
            cost_usd=1.23,
            edited_files={"shared.txt"},
        )

    cleanup_calls: list[dict[str, str]] = []

    def fake_graduate(project_dir: Path, queue_lookup: dict[str, str]) -> None:
        assert project_dir == repo
        cleanup_calls.append(dict(queue_lookup))

    monkeypatch.setattr(conflict_agent, "resolve_all_conflicts", fake_resolve_all_conflicts)
    monkeypatch.setattr(orchestrator_module, "_graduate_merged_task_sessions", fake_graduate)

    result = asyncio.run(run_merge(
        project_dir=repo,
        config=_config_no_bookkeeping(),
        options=MergeOptions(target="main", no_certify=True, cleanup_on_success=True),
        explicit_ids_or_branches=["task-a", "task-b"],
    ))

    assert result.success, result.note
    assert cleanup_calls == [{"feat-a": "task-a", "feat-b": "task-b"}]


# ---------- bookkeeping precondition ----------


def test_merge_requires_gitattributes_when_bookkeeping_enabled(tmp_path: Path):
    """If .gitattributes is missing required rules, merge hard-fails (unless opt-out)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "f.txt").write_text("baseline\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    _make_branch(repo, "feat-a", "a.txt", "A\n")
    # Note: no install_gitattributes called → precondition will fail
    cfg = {"default_branch": "main", "queue": {"bookkeeping_files": ["intent.md", "otto.yaml"]}}
    result = asyncio.run(run_merge(
        project_dir=repo,
        config=cfg,
        options=MergeOptions(target="main", no_certify=True, allow_any_branch=True),
        explicit_ids_or_branches=["feat-a"],
    ))
    assert result.success is False
    assert ".gitattributes" in result.note


def test_merge_skips_gitattributes_check_with_optout(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "f.txt").write_text("baseline\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    _make_branch(repo, "feat-a", "a.txt", "A\n")
    # Opt out via empty bookkeeping_files
    cfg = {"default_branch": "main", "queue": {"bookkeeping_files": []}}
    result = asyncio.run(run_merge(
        project_dir=repo,
        config=cfg,
        options=MergeOptions(target="main", no_certify=True, allow_any_branch=True),
        explicit_ids_or_branches=["feat-a"],
    ))
    assert result.success, f"expected success: {result.note}"


# ---------- state persistence ----------


def test_post_merge_verification_full_verify_preserves_merge_context_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        "otto.merge.orchestrator.collect_stories_from_branches",
        lambda **kwargs: [{"story_id": "story-a", "summary": "summary"}],
    )
    monkeypatch.setattr(
        "otto.merge.orchestrator.dedupe_stories",
        lambda stories: (stories, []),
    )
    monkeypatch.setattr(
        "otto.merge.orchestrator.git_ops.changed_files_between",
        lambda *args, **kwargs: ["app/csv.py"],
    )
    monkeypatch.setattr(
        "otto.merge.orchestrator.git_ops.head_sha",
        lambda *args, **kwargs: "new-head",
    )
    monkeypatch.setattr("otto.config.resolve_intent", lambda project_dir: "intent")

    async def fake_run_agentic_certifier(**kwargs):
        captured["merge_context"] = kwargs["merge_context"]
        return CertificationReport(
            outcome=CertificationOutcome.PASSED,
            story_results=[{"story_id": "story-a", "verdict": "PASS", "passed": True}],
            run_id="cert-1",
        )

    monkeypatch.setattr("otto.certifier.run_agentic_certifier", fake_run_agentic_certifier)

    state = MergeState(
        merge_id="merge-test",
        started_at="2026-04-20T00:00:00Z",
        target="main",
        target_head_before="old-head",
    )
    result = asyncio.run(_run_post_merge_verification(
        project_dir=tmp_path,
        config=_config_no_bookkeeping(),
        options=MergeOptions(target="main", full_verify=True),
        state=state,
        merge_id="merge-test",
        branches=["feat-a"],
        queue_lookup={},
        target_head_before="old-head",
    ))

    assert result.success is True
    assert captured["merge_context"] == {
        "target": "main",
        "diff_files": ["app/csv.py"],
        "allow_skip": False,
    }


def test_post_merge_verification_writes_merged_from_to_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    session_dir = tmp_path / "otto_logs" / "sessions" / "cert-merged"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "summary.json").write_text(json.dumps({
        "run_id": "cert-merged",
        "command": "certify",
        "verdict": "passed",
    }, indent=2))

    monkeypatch.setattr(
        "otto.merge.orchestrator.collect_stories_from_branches",
        lambda **kwargs: [{"story_id": "story-a", "summary": "summary"}],
    )
    monkeypatch.setattr(
        "otto.merge.orchestrator.dedupe_stories",
        lambda stories: (stories, []),
    )
    monkeypatch.setattr(
        "otto.merge.orchestrator.git_ops.changed_files_between",
        lambda *args, **kwargs: ["app/csv.py"],
    )
    monkeypatch.setattr(
        "otto.merge.orchestrator.git_ops.head_sha",
        lambda *args, **kwargs: "new-head",
    )
    monkeypatch.setattr("otto.config.resolve_intent", lambda project_dir: "intent")

    async def fake_run_agentic_certifier(**kwargs):
        return CertificationReport(
            outcome=CertificationOutcome.PASSED,
            story_results=[{"story_id": "story-a", "verdict": "PASS", "passed": True}],
            run_id="cert-merged",
        )

    monkeypatch.setattr("otto.certifier.run_agentic_certifier", fake_run_agentic_certifier)

    state = MergeState(
        merge_id="merge-test",
        started_at="2026-04-20T00:00:00Z",
        target="main",
        target_head_before="old-head",
    )
    result = asyncio.run(_run_post_merge_verification(
        project_dir=tmp_path,
        config=_config_no_bookkeeping(),
        options=MergeOptions(target="main"),
        state=state,
        merge_id="merge-test",
        branches=["build/add-2026-04-21", "feature/random"],
        queue_lookup={"build/add-2026-04-21": "add"},
        target_head_before="old-head",
    ))

    assert result.success is True
    summary = json.loads((session_dir / "summary.json").read_text())
    assert summary["merged_from"] == ["add", "feature/random"]


def test_merge_state_persisted_to_disk(tmp_path: Path):
    repo = _init_repo_with_gitattributes(tmp_path)
    _make_branch(repo, "feat-a", "a.txt", "A\n")
    result = asyncio.run(run_merge(
        project_dir=repo,
        config=_config_no_bookkeeping(),
        options=MergeOptions(target="main", no_certify=True, allow_any_branch=True),
        explicit_ids_or_branches=["feat-a"],
    ))
    assert result.success
    sp = repo / "otto_logs" / "merge" / result.merge_id / "state.json"
    assert sp.exists()
    data = json.loads(sp.read_text())
    assert data["target"] == "main"
    assert data["target_head_before"]
    assert data["branches_in_order"] == ["feat-a"]


def test_find_latest_merge_id(tmp_path: Path):
    repo = _init_repo_with_gitattributes(tmp_path)
    _make_branch(repo, "feat-a", "a.txt", "A\n")
    result1 = asyncio.run(run_merge(
        project_dir=repo,
        config=_config_no_bookkeeping(),
        options=MergeOptions(target="main", no_certify=True, allow_any_branch=True),
        explicit_ids_or_branches=["feat-a"],
    ))
    assert result1.success
    latest = find_latest_merge_id(repo)
    assert latest == result1.merge_id


# ---------- session graduation + merge lock ----------


def test_graduate_merged_task_session_end_to_end(tmp_path: Path):
    repo = init_repo(tmp_path)
    worktree = _create_worktree(repo, "task-1")
    _seed_queue_task(repo, task_id="task-1", worktree=worktree)

    run_id = "2026-04-21-010203-abcdef"
    src_session = worktree / "otto_logs" / "sessions" / run_id
    src_session.mkdir(parents=True, exist_ok=True)
    (src_session / "checkpoint.json").write_text("{}\n")
    build_dir = src_session / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "narrative.log").write_text("build log\n", encoding="utf-8")
    certify_dir = src_session / "certify"
    certify_dir.mkdir(parents=True, exist_ok=True)
    (certify_dir / "proof-of-work.json").write_text("{\"stories\": []}\n")
    (src_session / "summary.json").write_text(json.dumps({
        "run_id": run_id,
        "command": "build",
        "verdict": "passed",
    }, indent=2))
    canonical_manifest = {
        "command": "build",
        "argv": ["build", "task-1"],
        "queue_task_id": "task-1",
        "run_id": run_id,
        "branch": "build/task-1",
        "checkpoint_path": str((src_session / "checkpoint.json").resolve()),
        "proof_of_work_path": str((certify_dir / "proof-of-work.json").resolve()),
        "cost_usd": 1.0,
        "duration_s": 2.0,
        "started_at": "2026-04-21T00:00:00Z",
        "finished_at": "2026-04-21T00:01:00Z",
        "head_sha": "abc123",
        "resolved_intent": "test",
        "exit_status": "success",
        "schema_version": 1,
        "extra": {},
    }
    (src_session / "manifest.json").write_text(json.dumps(canonical_manifest, indent=2))

    queue_manifest = repo / "otto_logs" / "queue" / "task-1" / "manifest.json"
    queue_manifest.parent.mkdir(parents=True, exist_ok=True)
    queue_manifest.write_text(json.dumps({
        **canonical_manifest,
        "mirror_of": str((src_session / "manifest.json").resolve()),
    }, indent=2))
    append_history_snapshot(
        repo,
        {
            "run_id": run_id,
            "status": "done",
            "terminal_outcome": "success",
            "session_dir": str(src_session.resolve()),
            "manifest_path": str((src_session / "manifest.json").resolve()),
            "summary_path": str((src_session / "summary.json").resolve()),
            "checkpoint_path": str((src_session / "checkpoint.json").resolve()),
            "primary_log_path": str((src_session / "build" / "narrative.log").resolve()),
            "extra_log_paths": [str((src_session / "certify" / "proof-of-work.json").resolve())],
            "artifacts": {
                "session_dir": str(src_session.resolve()),
                "manifest_path": str((src_session / "manifest.json").resolve()),
                "summary_path": str((src_session / "summary.json").resolve()),
                "checkpoint_path": str((src_session / "checkpoint.json").resolve()),
                "primary_log_path": str((src_session / "build" / "narrative.log").resolve()),
                "extra_log_paths": [str((src_session / "certify" / "proof-of-work.json").resolve())],
            },
        },
        strict=True,
    )

    expected_merge_sha = git_ops.head_sha(repo)
    _graduate_merged_task_sessions(repo, {"build/task-1": "task-1"})

    dst_session = repo / "otto_logs" / "sessions" / run_id
    assert dst_session.exists()
    assert not src_session.exists()
    assert not worktree.exists()

    summary = json.loads((dst_session / "summary.json").read_text())
    assert summary["merge_commit_sha"] == expected_merge_sha
    assert summary["merged_at"].endswith("Z")

    manifest = json.loads((dst_session / "manifest.json").read_text())
    assert manifest["checkpoint_path"] == str((dst_session / "checkpoint.json").resolve())
    assert manifest["proof_of_work_path"] == str((dst_session / "certify" / "proof-of-work.json").resolve())
    assert manifest["extra"]["merge_commit_sha"] == expected_merge_sha
    assert manifest["extra"]["merged_at"].endswith("Z")

    mirror = json.loads(queue_manifest.read_text())
    assert mirror["mirror_of"] == str((dst_session / "manifest.json").resolve())
    assert mirror["checkpoint_path"] == manifest["checkpoint_path"]
    assert mirror["proof_of_work_path"] == manifest["proof_of_work_path"]
    assert mirror["extra"]["merge_commit_sha"] == expected_merge_sha

    history_row = next(
        row for row in reversed(read_history_rows(paths.history_jsonl(repo)))
        if row.get("dedupe_key") == f"terminal_snapshot:{run_id}"
    )
    assert history_row["session_dir"] == str(dst_session.resolve())
    assert history_row["manifest_path"] == str((dst_session / "manifest.json").resolve())
    assert history_row["summary_path"] == str((dst_session / "summary.json").resolve())
    assert history_row["checkpoint_path"] == str((dst_session / "checkpoint.json").resolve())
    assert history_row["primary_log_path"] == str((dst_session / "build" / "narrative.log").resolve())
    assert history_row["extra_log_paths"] == [str((dst_session / "certify" / "proof-of-work.json").resolve())]
    for artifact_path in (
        history_row["session_dir"],
        history_row["manifest_path"],
        history_row["summary_path"],
        history_row["checkpoint_path"],
        history_row["primary_log_path"],
        *history_row["extra_log_paths"],
    ):
        assert Path(artifact_path).exists(), artifact_path


def test_graduate_skips_collision_and_preserves_worktree(tmp_path: Path):
    repo = init_repo(tmp_path)
    worktree = _create_worktree(repo, "task-2")
    _seed_queue_task(repo, task_id="task-2", worktree=worktree)

    run_id = "2026-04-21-020304-bcd123"
    src_session = worktree / "otto_logs" / "sessions" / run_id
    src_session.mkdir(parents=True, exist_ok=True)
    (src_session / "manifest.json").write_text(json.dumps({
        "run_id": run_id,
        "queue_task_id": "task-2",
        "checkpoint_path": None,
        "proof_of_work_path": None,
        "extra": {},
    }, indent=2))
    (repo / "otto_logs" / "queue" / "task-2").mkdir(parents=True, exist_ok=True)
    (repo / "otto_logs" / "queue" / "task-2" / "manifest.json").write_text(json.dumps({
        "run_id": run_id,
        "queue_task_id": "task-2",
        "checkpoint_path": None,
        "proof_of_work_path": None,
        "extra": {},
        "mirror_of": str((src_session / "manifest.json").resolve()),
    }, indent=2))

    dst_session = repo / "otto_logs" / "sessions" / run_id
    dst_session.mkdir(parents=True, exist_ok=True)
    (dst_session / "sentinel.txt").write_text("keep\n")

    _graduate_merged_task_sessions(repo, {"build/task-2": "task-2"})

    assert src_session.exists()
    assert worktree.exists()
    assert (dst_session / "sentinel.txt").read_text() == "keep\n"

    subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=repo, check=True)


def test_merge_lock_refuses_second_holder(tmp_path: Path):
    repo = init_repo(tmp_path)
    script = """
from pathlib import Path
import sys
import time

from otto.merge.orchestrator import merge_lock

with merge_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    time.sleep(3)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(repo)],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "locked"
        with pytest.raises(MergeAlreadyRunning, match="another otto merge is in progress"):
            with merge_lock(repo):
                pass
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ---------- bookkeeping union driver E2E ----------


def test_intent_md_union_merge_no_conflict(tmp_path: Path):
    """Phase 1.6 .gitattributes union driver should auto-merge intent.md."""
    repo = _init_repo_with_gitattributes(tmp_path)  # already has gitattrs installed
    # Both branches add to intent.md
    subprocess.run(["git", "checkout", "-b", "feat-a"], cwd=repo, check=True, capture_output=True)
    (repo / "intent.md").write_text("# log\n\nA's intent\n")
    subprocess.run(["git", "add", "intent.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "a"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "feat-b"], cwd=repo, check=True, capture_output=True)
    (repo / "intent.md").write_text("# log\n\nB's intent\n")
    subprocess.run(["git", "add", "intent.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "b"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    # Use bookkeeping files config (default; gitattrs are installed)
    cfg = {"default_branch": "main", "queue": {"bookkeeping_files": ["intent.md", "otto.yaml"]}}
    result = asyncio.run(run_merge(
        project_dir=repo,
        config=cfg,
        options=MergeOptions(target="main", no_certify=True, allow_any_branch=True),
        explicit_ids_or_branches=["feat-a", "feat-b"],
    ))
    assert result.success, f"union merge driver should make this auto-merge: {result.note}"
    final = (repo / "intent.md").read_text()
    assert "A's intent" in final
    assert "B's intent" in final
    assert "<<<<<<<" not in final


def test_merge_preserves_interrupted_terminal_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("otto.runs.registry.garbage_collect_live_records", lambda project_dir: [])
    monkeypatch.setattr("otto.merge.orchestrator._repair_merge_history", lambda project_dir: None)
    monkeypatch.setattr("otto.config.agent_provider", lambda config: "claude")
    monkeypatch.setattr("otto.merge.orchestrator.git_ops.current_branch", lambda project_dir: "main")
    monkeypatch.setattr("otto.merge.orchestrator.git_ops.status_porcelain_entries", lambda project_dir: [])
    monkeypatch.setattr("otto.merge.orchestrator.git_ops.merge_in_progress", lambda project_dir: False)
    monkeypatch.setattr("otto.merge.orchestrator._resolve_branches", lambda *args, **kwargs: (["feature/a"], {}))
    monkeypatch.setattr("otto.merge.orchestrator.new_merge_id", lambda: "merge-interrupted")
    monkeypatch.setattr("otto.merge.orchestrator.git_ops.head_sha", lambda project_dir: "abc123")
    monkeypatch.setattr("otto.merge.orchestrator._drain_merge_cancel_commands", lambda *args, **kwargs: False)

    async def _fake_merge(**kwargs):
        state = kwargs["state"]
        state.status = "interrupted"
        state.terminal_outcome = "interrupted"
        state.finished_at = "2026-04-23T12:00:10Z"
        return MergeRunResult(
            success=False,
            merge_id=kwargs["merge_id"],
            state=state,
            note="paused for resume",
        )

    monkeypatch.setattr("otto.merge.orchestrator._run_consolidated_agentic_merge", _fake_merge)

    result = asyncio.run(
        run_merge(
            project_dir=tmp_path,
            config={},
            options=MergeOptions(target="main"),
            explicit_ids_or_branches=["feature/a"],
        )
    )

    record = load_live_record(tmp_path, "merge-interrupted")
    assert result.state.status == "interrupted"
    assert record.status == "interrupted"
    assert record.terminal_outcome == "interrupted"


def test_merge_startup_repairs_terminal_state_before_registry_gc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from otto.merge.orchestrator import _merge_artifacts
    from otto.merge.state import write_state
    from otto.runs.history import read_history_rows
    from otto.runs.registry import make_run_record, write_record

    merge_id = "merge-repair"
    state = MergeState(
        merge_id=merge_id,
        started_at="2026-04-23T12:00:00Z",
        finished_at="2026-04-23T12:00:05Z",
        target="main",
        target_head_before="abc123",
        status="cancelled",
        terminal_outcome="cancelled",
        note="cancelled by command",
        branches_in_order=["feature/a"],
    )
    write_state(tmp_path, state)
    stale_record = make_run_record(
        project_dir=tmp_path,
        run_id=merge_id,
        domain="merge",
        run_type="merge",
        command="merge",
        display_name="merge: 1 branch(es)",
        status="running",
        identity={"merge_id": merge_id},
        source={"resumable": False},
        git={"branch": "main", "worktree": None, "target_branch": "main", "head_sha": "abc123"},
        intent={"summary": "merge 1 branch(es)", "intent_path": str(tmp_path / "intent.md"), "spec_path": None},
        artifacts=_merge_artifacts(tmp_path, merge_id),
        adapter_key="merge.run",
        last_event="running",
    )
    write_record(tmp_path, stale_record)

    monkeypatch.setattr("otto.runs.registry.garbage_collect_live_records", lambda project_dir: [])
    monkeypatch.setattr("otto.merge.orchestrator.git_ops.current_branch", lambda project_dir: "feature/not-main")

    result = asyncio.run(
        run_merge(
            project_dir=tmp_path,
            config={},
            options=MergeOptions(target="main"),
        )
    )

    repaired = load_live_record(tmp_path, merge_id)
    history_rows = read_history_rows((tmp_path / "otto_logs" / "cross-sessions" / "history.jsonl"))
    history_row = next(row for row in history_rows if row["run_id"] == merge_id)

    assert result.success is False
    assert repaired.status == "cancelled"
    assert repaired.terminal_outcome == "cancelled"
    assert repaired.timing["finished_at"] == "2026-04-23T12:00:05Z"
    assert history_row["status"] == "cancelled"
    assert history_row["terminal_outcome"] == "cancelled"


def test_merge_stops_publisher_when_body_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakePublisher:
        def __init__(self) -> None:
            self.stopped = False

        def __enter__(self):
            return self

        def stop(self) -> None:
            self.stopped = True

        def finalize(self, **kwargs):
            raise AssertionError("finalize should not run")

    publisher = _FakePublisher()

    monkeypatch.setattr("otto.runs.registry.garbage_collect_live_records", lambda project_dir: [])
    monkeypatch.setattr("otto.merge.orchestrator._repair_merge_history", lambda project_dir: None)
    monkeypatch.setattr("otto.config.agent_provider", lambda config: "claude")
    monkeypatch.setattr("otto.merge.orchestrator.git_ops.current_branch", lambda project_dir: "main")
    monkeypatch.setattr("otto.merge.orchestrator.git_ops.status_porcelain_entries", lambda project_dir: [])
    monkeypatch.setattr("otto.merge.orchestrator.git_ops.merge_in_progress", lambda project_dir: False)
    monkeypatch.setattr("otto.merge.orchestrator._resolve_branches", lambda *args, **kwargs: (["feature/a"], {}))
    monkeypatch.setattr("otto.merge.orchestrator.new_merge_id", lambda: "merge-err")
    monkeypatch.setattr("otto.merge.orchestrator.git_ops.head_sha", lambda project_dir: "abc123")
    monkeypatch.setattr("otto.merge.orchestrator._drain_merge_cancel_commands", lambda *args, **kwargs: False)
    monkeypatch.setattr("otto.merge.orchestrator.publisher_for", lambda *args, **kwargs: publisher)

    async def _boom(**kwargs):
        raise RuntimeError("merge exploded")

    monkeypatch.setattr("otto.merge.orchestrator._run_consolidated_agentic_merge", _boom)

    with pytest.raises(RuntimeError, match="merge exploded"):
        asyncio.run(
            run_merge(
                project_dir=tmp_path,
                config={},
                options=MergeOptions(target="main"),
                explicit_ids_or_branches=["feature/a"],
            )
        )

    assert publisher.stopped is True
