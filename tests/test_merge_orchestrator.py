"""Tests for otto/merge/orchestrator.py — Python-driven merge loop.

Strategy: most tests exercise the orchestrator without invoking real
agents (set provider=codex to short-circuit, or use --fast). Real-LLM
E2E lives in a separate test suite gated by an env var.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from otto.certifier.report import CertificationOutcome, CertificationReport
from otto.merge import conflict_agent
from otto.merge.orchestrator import (
    MergeOptions,
    _run_post_merge_verification,
    run_merge,
)
from otto.merge import git_ops
from otto.merge.state import MergeState, find_latest_merge_id, load_state
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
        options=MergeOptions(target="main", no_certify=True),
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
        options=MergeOptions(target="main", no_certify=True),
        explicit_ids_or_branches=["feat-a"],
    ))
    assert result.success is False
    assert "clean" in result.note


def test_merge_refuses_unknown_branch(tmp_path: Path):
    repo = _init_repo_with_gitattributes(tmp_path)
    with pytest.raises(ValueError, match="unknown task id or branch"):
        asyncio.run(run_merge(
            project_dir=repo,
            config=_config_no_bookkeeping(),
            options=MergeOptions(target="main", no_certify=True),
            explicit_ids_or_branches=["does-not-exist"],
        ))


# ---------- clean merges (no agent) ----------


def test_clean_merge_two_independent_branches(tmp_path: Path):
    repo = _init_repo_with_gitattributes(tmp_path)
    _make_branch(repo, "feat-a", "a.txt", "A\n")
    _make_branch(repo, "feat-b", "b.txt", "B\n")
    result = asyncio.run(run_merge(
        project_dir=repo,
        config=_config_no_bookkeeping(),
        options=MergeOptions(target="main", no_certify=True),
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
        options=MergeOptions(target="main", no_certify=True),
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
        options=MergeOptions(target="main", no_certify=True, fast=True),
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
        options=MergeOptions(target="main", no_certify=True),
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
        options=MergeOptions(target="main", no_certify=True, fast=True),
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
        return ("done", 0.0, "")

    with patch("otto.agent.run_agent_with_timeout", side_effect=fake_run_agent_with_timeout):
        result = asyncio.run(run_merge(
            project_dir=repo,
            config=_config_no_bookkeeping(),
            options=MergeOptions(target="main", no_certify=True),
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
        options=MergeOptions(target="main", no_certify=True),
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
        )

    monkeypatch.setattr(conflict_agent, "resolve_all_conflicts", fake_resolve_all_conflicts)

    result = asyncio.run(run_merge(
        project_dir=repo,
        config=_config_no_bookkeeping(),
        options=MergeOptions(target="main", no_certify=True),
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
        options=MergeOptions(target="main", no_certify=True),
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
        options=MergeOptions(target="main", no_certify=True),
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


def test_merge_state_persisted_to_disk(tmp_path: Path):
    repo = _init_repo_with_gitattributes(tmp_path)
    _make_branch(repo, "feat-a", "a.txt", "A\n")
    result = asyncio.run(run_merge(
        project_dir=repo,
        config=_config_no_bookkeeping(),
        options=MergeOptions(target="main", no_certify=True),
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
        options=MergeOptions(target="main", no_certify=True),
        explicit_ids_or_branches=["feat-a"],
    ))
    assert result1.success
    latest = find_latest_merge_id(repo)
    assert latest == result1.merge_id


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
        options=MergeOptions(target="main", no_certify=True),
        explicit_ids_or_branches=["feat-a", "feat-b"],
    ))
    assert result.success, f"union merge driver should make this auto-merge: {result.note}"
    final = (repo / "intent.md").read_text()
    assert "A's intent" in final
    assert "B's intent" in final
    assert "<<<<<<<" not in final
