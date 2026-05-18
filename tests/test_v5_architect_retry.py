"""Tests for architect-retry-on-preflight-failure machinery.

When scaffold preflight blocks an architect's self-declared pass, the
runner clears the verdict + stores a retry_reason so the next dispatch
picks it back up with the failure context attached.

Building blocks (helpers + cap constant) are tested directly. The end-
to-end retry loop is also exercised via ``_process_children`` with
stubbed ``run_lead`` + ``verify_from_clean_oracle`` so we verify the
state-machine without burning $13/run on a live build.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from otto.lead import LeadResult
from otto.queue.subtask import enqueue_subtask
from otto.queue.task_graph import (
    clear_verdict_for_retry,
    get_retry_count,
    get_retry_reason,
    get_task,
    record_task,
    set_verdict,
)
from otto.v5_branching import integration_branch_name
from otto.v5_clean_verify import CleanOracleIssue, CleanOracleResult, CleanOracleStepResult
from otto.v5_runner import (
    MAX_ARCHITECT_RETRIES,
    _link_shared_install_dirs,
    _process_children,
)


def _oracle_result(*, passed: bool, kind: str = "contract_structural_invalid", message: str = "") -> CleanOracleResult:
    step = CleanOracleStepResult(
        id="scaffold",
        status="passed" if passed else "failed",
        return_code=0 if passed else 1,
        command_identity="deterministic scaffold check",
        command=["true"],
        cwd=".",
        env={},
    )
    issue = CleanOracleIssue(
        kind=kind,
        severity="block",
        message=message or kind,
        step_id=step.id,
        command_identity=step.command_identity,
        return_code=step.return_code,
    )
    return CleanOracleResult.from_parts(
        passed=passed,
        scope="scaffold",
        issues=[] if passed else [issue],
        steps=[step],
        artifact_path_refs=[],
        command=step.command,
        env={},
        project_dir=Path("."),
    )


def _seed_architect(project_dir: Path, task_id: str = "v5-arch") -> None:
    """Create an architect-shaped task with verdict=pass."""
    record_task(
        project_dir,
        task_id=task_id,
        intent="Architect a Linear-lite scaffold.",
        parent_task_id="root",
    )
    set_verdict(project_dir, task_id, "pass")


def _git(cwd: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )


def _init_git_repo(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    _git(project_dir, "init", "-q", "-b", "main")
    _git(project_dir, "config", "user.email", "test@example.invalid")
    _git(project_dir, "config", "user.name", "Test User")
    (project_dir / ".gitignore").write_text(".worktrees/\notto_logs/\n", encoding="utf-8")
    (project_dir / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(project_dir, "add", "-A")
    _git(project_dir, "commit", "-q", "-m", "init")
    root_integration = integration_branch_name("root")
    if root_integration != "main":
        _git(project_dir, "branch", root_integration, "main")


def test_clear_verdict_for_retry_resets_state(tmp_path: Path) -> None:
    _seed_architect(tmp_path)
    # Sanity precondition.
    from otto.queue.task_graph import get_task

    pre = get_task(tmp_path, "v5-arch") or {}
    assert pre.get("verdict") == "pass"
    assert pre.get("retry_count", 0) == 0
    assert get_retry_reason(tmp_path, "v5-arch") is None

    new_count = clear_verdict_for_retry(
        tmp_path, "v5-arch", "scaffold failed: tsc error"
    )

    post = get_task(tmp_path, "v5-arch") or {}
    assert post["verdict"] is None  # cleared, take_ready will re-pick-up
    assert post["completed_at"] is None
    assert post["retry_count"] == 1
    assert new_count == 1
    assert get_retry_reason(tmp_path, "v5-arch") == "scaffold failed: tsc error"
    assert get_retry_count(tmp_path, "v5-arch") == 1


def test_retry_count_increments_across_calls(tmp_path: Path) -> None:
    _seed_architect(tmp_path)
    assert clear_verdict_for_retry(tmp_path, "v5-arch", "fail 1") == 1
    # Simulate the agent finishing again (verdict back to pass) then
    # preflight invalidating it a second time.
    set_verdict(tmp_path, "v5-arch", "pass")
    assert clear_verdict_for_retry(tmp_path, "v5-arch", "fail 2") == 2
    set_verdict(tmp_path, "v5-arch", "pass")
    assert clear_verdict_for_retry(tmp_path, "v5-arch", "fail 3") == 3
    assert get_retry_count(tmp_path, "v5-arch") == 3
    # The most recent reason is what's stored.
    assert get_retry_reason(tmp_path, "v5-arch") == "fail 3"


def test_clear_verdict_unknown_task_returns_zero(tmp_path: Path) -> None:
    # Don't seed; task doesn't exist.
    assert clear_verdict_for_retry(tmp_path, "does-not-exist", "x") == 0


def test_get_retry_reason_returns_none_when_empty(tmp_path: Path) -> None:
    _seed_architect(tmp_path)
    assert get_retry_reason(tmp_path, "v5-arch") is None


def test_max_architect_retries_constant(tmp_path: Path) -> None:
    """Sanity: the cap is a small positive integer.

    Runner enforces ``retry_count < MAX_ARCHITECT_RETRIES`` before
    invalidating again, so the architect gets 1 original attempt + N
    retries = N+1 total. We don't want the cap to be 0 (no retries
    allowed) or unboundedly large.
    """
    assert isinstance(MAX_ARCHITECT_RETRIES, int)
    assert 1 <= MAX_ARCHITECT_RETRIES <= 5


def test_retry_state_independent_per_task(tmp_path: Path) -> None:
    """Two architect tasks (different subsystems) keep separate retry state."""
    record_task(tmp_path, task_id="v5-arch-fe", intent="Architect FE.", parent_task_id="root")
    record_task(tmp_path, task_id="v5-arch-be", intent="Architect BE.", parent_task_id="root")
    set_verdict(tmp_path, "v5-arch-fe", "pass")
    set_verdict(tmp_path, "v5-arch-be", "pass")

    assert clear_verdict_for_retry(tmp_path, "v5-arch-fe", "fe broken") == 1
    assert get_retry_count(tmp_path, "v5-arch-fe") == 1
    assert get_retry_count(tmp_path, "v5-arch-be") == 0
    assert get_retry_reason(tmp_path, "v5-arch-be") is None


# ---------------------------------------------------------------------------
# End-to-end: drive _process_children with stubbed run_lead + scaffold check.
# ---------------------------------------------------------------------------


def _setup_root_with_architect(project: Path, intent: str = "Architect a scaffold.") -> str:
    """Seed project_dir with a root task + one pending architect subtask."""
    _init_git_repo(project)
    (project / "otto_logs").mkdir(exist_ok=True)
    record_task(project, task_id="root", intent="root build", parent_task_id=None)
    arch_tid = enqueue_subtask(
        project_dir=project,
        parent_task_id="root",
        parent_session_dir=project / "session",
        intent=intent,
    )
    record_task(project, task_id=arch_tid, intent=intent, parent_task_id="root")
    return arch_tid


@pytest.mark.asyncio
async def test_architect_retries_then_succeeds(tmp_path: Path) -> None:
    """One preflight failure → invalidate + re-dispatch with retry_reason
    prepended → second attempt succeeds → final verdict pass."""
    arch_tid = _setup_root_with_architect(tmp_path)

    intents_seen: list[str] = []

    async def fake_run_lead(**kwargs: Any) -> LeadResult:
        tid = kwargs["task_id"]
        intents_seen.append(kwargs["intent"])
        set_verdict(tmp_path, tid, "pass")
        return LeadResult(
            task_id=tid, verdict="pass", cost_usd=0.5, decomposition="inline"
        )

    scaffold_calls = {"v": 0}

    def fake_verify(project_dir: Path, *_args: Any, **_kwargs: Any) -> CleanOracleResult:
        scaffold_calls["v"] += 1
        if scaffold_calls["v"] == 1:
            return _oracle_result(
                passed=False,
                message="contract routes contradict required IA: tsc error TS2339",
            )
        return _oracle_result(passed=True)

    events: list[dict[str, Any]] = []

    with patch("otto.v5_runner.run_lead", new=fake_run_lead), \
         patch("otto.v5_runner.verify_from_clean_oracle", new=fake_verify):
        await _process_children(
            project_dir=tmp_path,
            parent_task_id="root",
            config={},
            max_parallel=3,
            tree_budget_usd=10.0,
            child_results={},
            integration_results={},
            on_event=events.append,
        )

    # Architect was dispatched twice.
    assert len(intents_seen) == 2, f"expected 2 dispatches, got {len(intents_seen)}"

    # First dispatch: plain intent, no retry preamble.
    assert "RETRY" not in intents_seen[0]
    assert intents_seen[0].lstrip().startswith("Architect")

    # Second dispatch: prepended with retry preamble + failure detail.
    assert "RETRY" in intents_seen[1]
    assert "tsc error TS2339" in intents_seen[1]
    assert "Architect" in intents_seen[1]  # original intent preserved

    # Retry state recorded in task graph.
    assert get_retry_count(tmp_path, arch_tid) == 1
    assert get_retry_reason(tmp_path, arch_tid) is not None

    # `architect_retry` event emitted exactly once with the right details.
    retry_events = [e for e in events if e.get("event") == "architect_retry"]
    assert len(retry_events) == 1
    assert retry_events[0]["task_id"] == arch_tid
    assert retry_events[0]["retry_count"] == 1
    assert retry_events[0]["max_retries"] == MAX_ARCHITECT_RETRIES

    # Final architect verdict is pass (after the successful retry).
    final = get_task(tmp_path, arch_tid)
    assert final is not None
    assert final.get("verdict") == "pass"


@pytest.mark.asyncio
async def test_architect_retry_cap_exhausted(tmp_path: Path) -> None:
    """When scaffold preflight keeps failing, retries cap out and an
    ``architect_retry_exhausted`` event is emitted. The architect stays
    at pass (its self-verdict) but descendants would remain blocked
    upstream via filter_blocked_descendants."""
    arch_tid = _setup_root_with_architect(tmp_path)

    dispatch_count = {"v": 0}

    async def fake_run_lead(**kwargs: Any) -> LeadResult:
        dispatch_count["v"] += 1
        set_verdict(tmp_path, kwargs["task_id"], "pass")
        return LeadResult(
            task_id=kwargs["task_id"], verdict="pass", cost_usd=0.1, decomposition="inline"
        )

    # Scaffold structural-contract preflight: always fails.
    def fake_verify(project_dir: Path, *_args: Any, **_kwargs: Any) -> CleanOracleResult:
        return _oracle_result(
            passed=False,
            message="persistent structured contract failure",
        )

    events: list[dict[str, Any]] = []

    with patch("otto.v5_runner.run_lead", new=fake_run_lead), \
         patch("otto.v5_runner.verify_from_clean_oracle", new=fake_verify):
        await _process_children(
            project_dir=tmp_path,
            parent_task_id="root",
            config={},
            max_parallel=3,
            tree_budget_usd=10.0,
            child_results={},
            integration_results={},
            on_event=events.append,
        )

    # Architect dispatched exactly MAX_ARCHITECT_RETRIES + 1 times
    # (1 original attempt + N retries).
    assert dispatch_count["v"] == MAX_ARCHITECT_RETRIES + 1, (
        f"expected {MAX_ARCHITECT_RETRIES + 1} dispatches, got {dispatch_count['v']}"
    )
    assert get_retry_count(tmp_path, arch_tid) == MAX_ARCHITECT_RETRIES

    # Retry events: one per retry, then exactly one exhausted event.
    retry_events = [e for e in events if e.get("event") == "architect_retry"]
    exhausted = [e for e in events if e.get("event") == "architect_retry_exhausted"]
    assert len(retry_events) == MAX_ARCHITECT_RETRIES
    assert len(exhausted) == 1
    assert exhausted[0]["task_id"] == arch_tid

    # The architect's task itself stays at pass (its self-declared verdict).
    final = get_task(tmp_path, arch_tid)
    assert final is not None
    assert final.get("verdict") == "pass"


@pytest.mark.asyncio
async def test_toolchain_preflight_runs_after_architect_and_children_inherit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Architect preflight should install once, propagate, then leaves inherit."""
    (tmp_path / "CHARTER.md").write_text("# Test charter\n", encoding="utf-8")
    record_task(tmp_path, task_id="root", intent="root build", parent_task_id=None)
    arch_tid = enqueue_subtask(
        project_dir=tmp_path,
        parent_task_id="root",
        parent_session_dir=tmp_path / "session",
        intent="Architect frontend scaffold.",
    )
    feature_tid = enqueue_subtask(
        project_dir=tmp_path,
        parent_task_id="root",
        parent_session_dir=tmp_path / "session",
        intent="Build dashboard page.",
        depends_on=[arch_tid],
    )
    record_task(tmp_path, task_id=arch_tid, intent="Architect frontend scaffold.", parent_task_id="root")
    record_task(
        tmp_path,
        task_id=feature_tid,
        intent="Build dashboard page.",
        parent_task_id="root",
        depends_on=[arch_tid],
    )

    inherited: list[bool] = []

    async def fake_run_child(**kwargs: Any) -> LeadResult:
        tid = kwargs["entry"]["task_id"]
        if tid == arch_tid:
            arch_worktree = tmp_path / ".worktrees" / arch_tid / "frontend"
            arch_worktree.mkdir(parents=True)
            (arch_worktree / "package.json").write_text('{"scripts":{"build":"vite build"}}\n')
            set_verdict(tmp_path, tid, "pass")
            return LeadResult(task_id=tid, verdict="pass", cost_usd=0.1)

        child_worktree = tmp_path / ".worktrees" / feature_tid
        child_worktree.mkdir(parents=True)
        linked = _link_shared_install_dirs(tmp_path, child_worktree, feature_tid)
        inherited.append(
            linked == 1
            and (child_worktree / "frontend" / "node_modules").is_symlink()
        )
        set_verdict(tmp_path, tid, "pass")
        return LeadResult(task_id=tid, verdict="pass", cost_usd=0.1)

    def fake_toolchain(worktree_dir: Path, **_kwargs: Any):
        from otto.v5_clean_verify import ToolchainPreflightResult

        node_modules = worktree_dir / "frontend" / "node_modules"
        node_modules.mkdir(parents=True)
        return ToolchainPreflightResult(
            passed=True,
            worktree=str(worktree_dir),
            _written_at="2026-05-14T00:00:00Z",
            manifest_counts={"package_json": 1, "pyproject": 0},
        )

    events: list[dict[str, Any]] = []
    monkeypatch.setattr("otto.v5_runner._run_child", fake_run_child)
    monkeypatch.setattr("otto.v5_runner.verify_from_clean_oracle", lambda *_a, **_k: _oracle_result(passed=True))
    monkeypatch.setattr("otto.v5_clean_verify.preflight_shared_toolchains", fake_toolchain)

    await _process_children(
        project_dir=tmp_path,
        parent_task_id="root",
        config={},
        max_parallel=3,
        tree_budget_usd=10.0,
        child_results={},
        integration_results={},
        on_event=events.append,
    )

    assert inherited == [True]
    propagated = tmp_path / "frontend" / "node_modules"
    assert propagated.is_symlink()
    assert any(e.get("event") == "toolchain_preflight_done" for e in events)
    assert any(
        e.get("event") == "install_dirs_propagated" and e.get("count") == 1
        for e in events
    )
