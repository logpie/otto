# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from otto import v5_branching, v5_runner
from otto.lead import LeadResult
from otto.queue.subtask import take_ready
from otto.queue.task_graph import get_task, read_graph, record_task, set_verdict, update_task_metadata
from otto.v5_runner import ROOT_TASK_ID


def _git(cwd: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main", check=True)
    _git(repo, "config", "user.email", "test@example.invalid", check=True)
    _git(repo, "config", "user.name", "Test User", check=True)
    (repo / ".gitignore").write_text(".worktrees/\notto_logs/\n.otto/\n", encoding="utf-8")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A", check=True)
    _git(repo, "commit", "-q", "-m", "init", check=True)
    _git(repo, "branch", "i2p/root/integration", "main", check=True)


def _write_session_spec(session_dir: Path) -> None:
    spec_path = session_dir / "spec" / "spec.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps({"routes": [], "features": []}) + "\n", encoding="utf-8")


def _record_leaf(repo: Path, *, owned_paths: list[str] | None = None) -> Path:
    record_task(repo, task_id=ROOT_TASK_ID, intent="root", parent_task_id=None)
    record_task(
        repo,
        task_id="leaf",
        parent_task_id=ROOT_TASK_ID,
        intent="leaf",
        integration_branch="i2p/root/integration",
        owned_paths=owned_paths or ["frontend/src/features/issues/"],
    )
    set_verdict(repo, "leaf", "pass")
    session_dir = repo / "otto_logs" / "sessions" / "session-leaf"
    _write_session_spec(session_dir)
    return session_dir


def _record_foundation_contract(repo: Path) -> None:
    record_task(
        repo,
        task_id="foundation",
        parent_task_id=ROOT_TASK_ID,
        intent="foundation",
        integration_branch="i2p/root/integration",
        owned_paths=["frontend/src/lib/ws.ts"],
        task_role="foundation",
    )
    update_task_metadata(
        repo,
        ROOT_TASK_ID,
        foundation_contracts=[
            {
                "path": "frontend/src/lib/ws.ts",
                "owner_task_id": "foundation",
                "check": "semantic",
            }
        ],
    )


def _blocking_smoke(path: str) -> dict[str, Any]:
    return {
        "passed": False,
        "issues": [
            {
                "kind": "clean_deploy_failed",
                "severity": "block",
                "path": path,
                "message": f"{path} breaks clean deploy",
            }
        ],
    }


def _repair_tasks(repo: Path) -> list[tuple[str, dict[str, Any]]]:
    return [
        (task_id, task)
        for task_id, task in (read_graph(repo).get("tasks") or {}).items()
        if task.get("repair_route") == "integration_smoke_repair"
    ]


@pytest.mark.asyncio
async def test_direct_conflict_repair_routes_out_of_scope_smoke_to_runnable_repair_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    session_dir = _record_leaf(repo)
    merge_calls = 0
    detection_calls: list[dict[str, Any]] = []
    repair_loop_calls: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    def fake_merge(**_kwargs: Any) -> tuple[bool, str]:
        nonlocal merge_calls
        merge_calls += 1
        if merge_calls == 1:
            return False, "CONFLICT (add/add): Merge conflict in backend/auth.py"
        return True, "merged"

    async def fake_conflict_repair(**_kwargs: Any) -> tuple[bool, str]:
        return True, "resolved scoped conflict"

    def fake_detection(**kwargs: Any) -> dict[str, Any]:
        detection_calls.append(kwargs)
        return _blocking_smoke("frontend/vite.config.ts")

    async def fake_repair_loop(**kwargs: Any) -> dict[str, Any]:
        repair_loop_calls.append(kwargs)
        return {"passed": True, "issues": []}

    monkeypatch.setattr(v5_branching, "commit_worktree", lambda **_kwargs: (True, "committed"))
    monkeypatch.setattr(v5_branching, "merge_child_into_integration", fake_merge)
    monkeypatch.setattr(v5_runner, "_repair_child_merge_conflict_once", fake_conflict_repair)
    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight", fake_detection)
    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight_with_repair", fake_repair_loop)

    result = LeadResult(task_id="leaf", verdict="pass", decomposition="inline", verify_called=True)
    await v5_runner._merge_child_branch(
        project_dir=repo,
        child_task_id="leaf",
        child_worktree=repo,
        child_session_dir=session_dir,
        parent_integration_branch="i2p/root/integration",
        result=result,
        config={},
        on_event=events.append,
    )

    assert detection_calls
    assert repair_loop_calls == []
    assert [event["event"] for event in events if event.get("event") == "integration_repair_needed"]
    repair_task_id, repair_task = _repair_tasks(repo)[0]
    assert repair_task["owned_paths"] == ["frontend/vite.config.ts"]
    assert {entry["task_id"] for entry in take_ready(repo, completed_task_ids=set(), in_flight_task_ids=set())} == {
        repair_task_id
    }
    leaf = get_task(repo, "leaf") or {}
    assert leaf["blocked_on_task_id"] == repair_task_id
    assert leaf["blocked_pending_contract_amendment"] is True
    assert leaf["verdict"] is None
    assert result.verdict == "pass"


@pytest.mark.asyncio
async def test_stale_target_retry_routes_out_of_scope_smoke_without_repair_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    session_dir = _record_leaf(repo)
    detection_calls: list[dict[str, Any]] = []
    repair_loop_calls: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    async def fake_stale_repair(**_kwargs: Any) -> tuple[bool, str, dict[str, Any]]:
        return True, "stale target repaired", {"kind": "stale_target"}

    def fake_detection(**kwargs: Any) -> dict[str, Any]:
        detection_calls.append(kwargs)
        return _blocking_smoke("frontend/vite.config.ts")

    async def fake_repair_loop(**kwargs: Any) -> dict[str, Any]:
        repair_loop_calls.append(kwargs)
        return {"passed": True, "issues": []}

    monkeypatch.setattr(v5_branching, "merge_child_into_integration", lambda **_kwargs: (True, "merged"))
    monkeypatch.setattr(v5_runner, "_repair_child_stale_target_gate_once", fake_stale_repair)
    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight", fake_detection)
    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight_with_repair", fake_repair_loop)

    result = LeadResult(task_id="leaf", verdict="pass", decomposition="inline", verify_called=True)
    retry = await v5_runner._repair_stale_target_and_retry_merge(
        project_dir=repo,
        child_task_id="leaf",
        child_worktree=repo,
        child_session_dir=session_dir,
        parent_integration_branch="i2p/root/integration",
        result=result,
        config={},
        detail="stale target",
        prior_repair_detail="conflict repair",
        origin="stale_target_merge_gate",
        terminal_phase="merge",
        source_branch="main",
        run_smoke_preflight=True,
        on_event=events.append,
    )

    assert retry.terminal_recorded is True
    assert detection_calls
    assert repair_loop_calls == []
    assert [event["event"] for event in events if event.get("event") == "integration_repair_needed"]
    repair_task_id, repair_task = _repair_tasks(repo)[0]
    assert repair_task["owned_paths"] == ["frontend/vite.config.ts"]
    assert {entry["task_id"] for entry in take_ready(repo, completed_task_ids=set(), in_flight_task_ids=set())} == {
        repair_task_id
    }
    leaf = get_task(repo, "leaf") or {}
    assert leaf["blocked_on_task_id"] == repair_task_id
    assert leaf["verdict"] is None
    assert result.verdict == "pass"


@pytest.mark.asyncio
async def test_out_of_scope_foundation_smoke_failure_routes_to_foundation_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    session_dir = _record_leaf(repo)
    _record_foundation_contract(repo)
    merge_calls = 0
    repair_loop_calls: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    def fake_merge(**_kwargs: Any) -> tuple[bool, str]:
        nonlocal merge_calls
        merge_calls += 1
        if merge_calls == 1:
            return False, "CONFLICT (content): Merge conflict in frontend/src/lib/ws.ts"
        return True, "merged"

    async def fake_conflict_repair(**_kwargs: Any) -> tuple[bool, str]:
        return True, "resolved foundation conflict"

    async def fake_repair_loop(**kwargs: Any) -> dict[str, Any]:
        repair_loop_calls.append(kwargs)
        return {"passed": True, "issues": []}

    monkeypatch.setattr(v5_branching, "commit_worktree", lambda **_kwargs: (True, "committed"))
    monkeypatch.setattr(v5_branching, "merge_child_into_integration", fake_merge)
    monkeypatch.setattr(v5_runner, "_repair_child_merge_conflict_once", fake_conflict_repair)
    monkeypatch.setattr(
        v5_runner,
        "_run_integration_smoke_preflight",
        lambda **_kwargs: _blocking_smoke("frontend/src/lib/ws.ts"),
    )
    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight_with_repair", fake_repair_loop)

    result = LeadResult(task_id="leaf", verdict="pass", decomposition="inline", verify_called=True)
    await v5_runner._merge_child_branch(
        project_dir=repo,
        child_task_id="leaf",
        child_worktree=repo,
        child_session_dir=session_dir,
        parent_integration_branch="i2p/root/integration",
        result=result,
        config={},
        on_event=events.append,
    )

    assert repair_loop_calls == []
    assert [event["event"] for event in events if event.get("event") == "foundation_repair_needed"]
    repair_tasks = [
        (task_id, task)
        for task_id, task in (read_graph(repo).get("tasks") or {}).items()
        if task.get("repair_route") == "foundation_contract_amendment"
        and task.get("task_role") == "contract_amendment"
    ]
    assert len(repair_tasks) == 1
    repair_task_id, repair_task = repair_tasks[0]
    assert repair_task["owned_paths"] == ["frontend/src/lib/ws.ts"]
    assert repair_task["contract_amendment"]["owner_task_id"] == "foundation"
    assert {entry["task_id"] for entry in take_ready(repo, completed_task_ids=set(), in_flight_task_ids=set())} == {
        repair_task_id
    }
    leaf = get_task(repo, "leaf") or {}
    assert leaf["blocked_on_task_id"] == repair_task_id
    assert leaf["verdict"] is None
    assert result.verdict == "pass"


@pytest.mark.asyncio
async def test_in_scope_smoke_failure_still_uses_existing_scoped_repair_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    session_dir = _record_leaf(repo)
    merge_calls = 0
    detection_calls: list[dict[str, Any]] = []
    repair_loop_calls: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    def fake_merge(**_kwargs: Any) -> tuple[bool, str]:
        nonlocal merge_calls
        merge_calls += 1
        if merge_calls == 1:
            return False, "CONFLICT (content): Merge conflict in frontend/src/features/issues/view.tsx"
        return True, "merged"

    async def fake_conflict_repair(**_kwargs: Any) -> tuple[bool, str]:
        return True, "resolved feature conflict"

    def fake_detection(**kwargs: Any) -> dict[str, Any]:
        detection_calls.append(kwargs)
        return _blocking_smoke("frontend/src/features/issues/view.tsx")

    async def fake_repair_loop(**kwargs: Any) -> dict[str, Any]:
        repair_loop_calls.append(kwargs)
        return {"passed": True, "issues": []}

    monkeypatch.setattr(v5_branching, "commit_worktree", lambda **_kwargs: (True, "committed"))
    monkeypatch.setattr(v5_branching, "merge_child_into_integration", fake_merge)
    monkeypatch.setattr(v5_runner, "_repair_child_merge_conflict_once", fake_conflict_repair)
    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight", fake_detection)
    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight_with_repair", fake_repair_loop)

    result = LeadResult(task_id="leaf", verdict="pass", decomposition="inline", verify_called=True)
    await v5_runner._merge_child_branch(
        project_dir=repo,
        child_task_id="leaf",
        child_worktree=repo,
        child_session_dir=session_dir,
        parent_integration_branch="i2p/root/integration",
        result=result,
        config={},
        on_event=events.append,
    )

    assert detection_calls
    assert repair_loop_calls
    assert _repair_tasks(repo) == []
    assert not [event for event in events if event.get("event") == "integration_repair_needed"]
    assert (get_task(repo, "leaf") or {})["verdict"] == "pass"
