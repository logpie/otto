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
from otto.v5_clean_verify import (
    CleanOracleIssue,
    CleanOracleResult,
    CleanOracleStepResult,
    Scope,
    verify_from_clean_oracle,
)
from otto.v5_preflight_repair import OracleRepairResult, RepairPacket
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


def _clean_oracle_result(
    repo: Path,
    *,
    passed: bool,
    scope: Scope = "subtree",
    paths: list[str] | None = None,
    kind: str = "start_failed",
    message: str = "start failed",
) -> CleanOracleResult:
    step = CleanOracleStepResult(
        id="start",
        status="passed" if passed else "failed",
        return_code=0 if passed else 1,
        command_identity="python -m otto.cli clean-verify",
        command=["python", "-m", "otto.cli", "clean-verify"],
        cwd=str(repo),
        env={},
    )
    issue = CleanOracleIssue(
        kind=kind,
        severity="block",
        message=message,
        step_id=step.id,
        paths=list(paths or []),
        command_identity=step.command_identity,
        return_code=step.return_code,
    )
    return CleanOracleResult.from_parts(
        passed=passed,
        scope=scope,
        issues=[] if passed else [issue],
        steps=[step],
        artifact_path_refs=[],
        command=step.command,
        env=step.env,
        project_dir=repo,
        temp_dir=None,
    )


def _py_compile_payload(repo: Path, *, bad_path: str) -> dict[str, Any]:
    (repo / "pyproject.toml").write_text("[project]\nname = \"smoke-scope-repro\"\n", encoding="utf-8")
    files = {
        "pkg/feature/view.py": "VALUE = 1\n",
        "pkg/foundation.py": "VALUE = 2\n",
        "pkg/other.py": "VALUE = 3\n",
    }
    files[bad_path] = "def broken(:\n    pass\n"
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    result = verify_from_clean_oracle(repo, scope="scaffold", timeout_s=30, journey_scope="leaf")
    assert result.passed is False
    assert result.issues
    assert result.issues[0].kind == "py_compile_failed"
    return {
        "passed": False,
        "issues": [
            {
                "kind": "clean_deploy_start_failed",
                "severity": "block",
                "message": result.issues[0].message,
                "paths": list(result.issues[0].paths),
            }
        ],
        "clean_oracle_result": result.to_jsonable(),
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
    retry = await v5_runner._repair_child_upward_merge_after_failure(
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


def test_integration_smoke_serialization_preserves_real_clean_oracle_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    monkeypatch.setattr(
        v5_runner,
        "verify_from_clean_oracle",
        lambda *_args, **_kwargs: _clean_oracle_result(
            repo,
            passed=False,
            paths=["frontend/src/lib/ws.ts"],
            message="ws contract broke startup",
        ),
    )

    payload = v5_runner._run_integration_smoke_preflight(
        worktree_path=repo,
        task_id="leaf",
        phase="child_merge_conflict_repair",
    )

    assert payload["passed"] is False
    assert payload["issues"][0]["paths"] == ["frontend/src/lib/ws.ts"]
    assert v5_runner._smoke_payload_paths(payload) == ["frontend/src/lib/ws.ts"]


def test_pathless_smoke_failure_terminalizes_without_empty_amendment(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _record_leaf(repo)
    result = LeadResult(task_id="leaf", verdict="pass", decomposition="inline", verify_called=True)
    payload = {
        "passed": False,
        "issues": [
            {
                "kind": "clean_deploy_smoke_error",
                "severity": "block",
                "message": "start failed without path",
            }
        ],
    }

    routed = v5_runner._route_out_of_scope_smoke_failure(
        project_dir=repo,
        child_task_id="leaf",
        child_worktree=repo,
        child_session_dir=repo / "otto_logs" / "sessions" / "session-leaf",
        parent_integration_branch="i2p/root/integration",
        source_branch="main",
        pre_merge_ref="HEAD",
        smoke_payload=payload,
        result=result,
    )

    assert routed is True
    assert _repair_tasks(repo) == []
    leaf = get_task(repo, "leaf") or {}
    assert leaf.get("blocked_on_task_id") in (None, "")
    assert leaf["verdict"] == "merge_blocked"
    structured = leaf["merge_blocked_structured_reason"]
    assert structured["kind"] == "integration_smoke_unrouteable"
    assert structured["repair_path"] == ""
    assert result.verdict == "merge_blocked"


def test_py_compile_multi_input_leaf_causal_path_stays_in_leaf_scope(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _record_leaf(repo, owned_paths=["pkg/feature/"])
    payload = _py_compile_payload(repo, bad_path="pkg/feature/view.py")
    assert v5_runner._smoke_payload_paths(payload) == ["pkg/feature/view.py"]

    result = LeadResult(task_id="leaf", verdict="pass", decomposition="inline", verify_called=True)
    routed = v5_runner._route_out_of_scope_smoke_failure(
        project_dir=repo,
        child_task_id="leaf",
        child_worktree=repo,
        child_session_dir=repo / "otto_logs" / "sessions" / "session-leaf",
        parent_integration_branch="i2p/root/integration",
        source_branch="main",
        pre_merge_ref="HEAD",
        smoke_payload=payload,
        result=result,
    )

    assert routed is False
    assert _repair_tasks(repo) == []
    assert result.verdict == "pass"


def test_py_compile_multi_input_foundation_causal_path_routes_to_owner_with_bound_path(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _record_leaf(repo, owned_paths=["pkg/feature/"])
    record_task(
        repo,
        task_id="foundation",
        parent_task_id=ROOT_TASK_ID,
        intent="foundation",
        integration_branch="i2p/root/integration",
        owned_paths=["pkg/foundation.py"],
        task_role="foundation",
    )
    update_task_metadata(
        repo,
        ROOT_TASK_ID,
        foundation_contracts=[
            {
                "path": "pkg/foundation.py",
                "owner_task_id": "foundation",
                "check": "py_compile",
            }
        ],
    )
    payload = _py_compile_payload(repo, bad_path="pkg/foundation.py")
    assert v5_runner._smoke_payload_paths(payload) == ["pkg/foundation.py"]

    result = LeadResult(task_id="leaf", verdict="pass", decomposition="inline", verify_called=True)
    routed = v5_runner._route_out_of_scope_smoke_failure(
        project_dir=repo,
        child_task_id="leaf",
        child_worktree=repo,
        child_session_dir=repo / "otto_logs" / "sessions" / "session-leaf",
        parent_integration_branch="i2p/root/integration",
        source_branch="main",
        pre_merge_ref="HEAD",
        smoke_payload=payload,
        result=result,
    )

    assert routed is True
    repair_tasks = [
        (task_id, task)
        for task_id, task in (read_graph(repo).get("tasks") or {}).items()
        if task.get("repair_route") == "foundation_contract_amendment"
        and task.get("task_role") == "contract_amendment"
    ]
    assert len(repair_tasks) == 1
    _repair_task_id, repair_task = repair_tasks[0]
    assert repair_task["owned_paths"] == ["pkg/foundation.py"]
    assert repair_task["contract_amendment"]["contract_paths"] == ["pkg/foundation.py"]
    assert repair_task["contract_amendment"]["owner_task_id"] == "foundation"


def test_py_compile_indeterminate_causal_path_terminalizes_unrouteable(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _record_leaf(repo, owned_paths=["pkg/feature/"])
    payload = {
        "passed": False,
        "issues": [
            {
                "kind": "clean_deploy_start_failed",
                "severity": "block",
                "message": "py_compile failed but did not identify a file",
                "paths": [],
            }
        ],
        "clean_oracle_result": _clean_oracle_result(
            repo,
            passed=False,
            paths=[],
            kind="py_compile_failed",
            message="py_compile failed but did not identify a file",
        ).to_jsonable(),
    }

    result = LeadResult(task_id="leaf", verdict="pass", decomposition="inline", verify_called=True)
    routed = v5_runner._route_out_of_scope_smoke_failure(
        project_dir=repo,
        child_task_id="leaf",
        child_worktree=repo,
        child_session_dir=repo / "otto_logs" / "sessions" / "session-leaf",
        parent_integration_branch="i2p/root/integration",
        source_branch="main",
        pre_merge_ref="HEAD",
        smoke_payload=payload,
        result=result,
    )

    assert routed is True
    assert _repair_tasks(repo) == []
    leaf = get_task(repo, "leaf") or {}
    assert leaf["verdict"] == "merge_blocked"
    structured = leaf["merge_blocked_structured_reason"]
    assert structured["kind"] == "integration_smoke_unrouteable"
    assert structured["repair_path"] == ""


@pytest.mark.asyncio
async def test_in_scope_smoke_repair_packet_and_commit_hook_enforce_owned_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "frontend/src/features/issues").mkdir(parents=True)
    (repo / "frontend/src/features/issues/view.tsx").write_text("old\n", encoding="utf-8")
    (repo / "frontend/src/lib").mkdir(parents=True)
    (repo / "frontend/src/lib/ws.ts").write_text("old\n", encoding="utf-8")
    _git(repo, "add", "-A", check=True)
    _git(repo, "commit", "-q", "-m", "seed app", check=True)
    _record_leaf(repo)
    packets: list[RepairPacket] = []
    commit_results: list[tuple[bool, str]] = []
    calls = 0

    def fake_smoke(**_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _blocking_smoke("frontend/src/features/issues/view.tsx")
        return {"passed": True, "issues": []}

    async def fake_repair(packet: RepairPacket, **kwargs: Any) -> OracleRepairResult:
        packets.append(packet)
        (repo / "frontend/src/lib/ws.ts").write_text("outside\n", encoding="utf-8")
        commit_hook = kwargs["commit_hook"]
        commit_results.append(
            await commit_hook(packet, _clean_oracle_result(repo, passed=True))
        )
        return OracleRepairResult(
            verdict="merge_blocked",
            summary=commit_results[-1][1],
            packet_path=str(packet.packet_path),
        )

    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight", fake_smoke)
    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", fake_repair)

    payload = await v5_runner._run_integration_smoke_preflight_with_repair(
        project_dir=repo,
        worktree_path=repo,
        task_id="leaf",
        phase="child_merge_conflict_repair",
        session_dir=repo / "otto_logs" / "sessions" / "session-leaf",
        config={},
        integration_branch="i2p/root/integration",
        allowed_paths=["frontend/src/features/issues/"],
        scope_policy="allowed_paths",
    )

    assert packets
    assert packets[0].repair_unit["allowed_paths"] == ["frontend/src/features/issues/"]
    assert packets[0].repair_unit["scope_policy"] == "allowed_paths"
    assert commit_results and commit_results[0][0] is False
    assert "allowed_paths_write_blocked" in commit_results[0][1]
    assert "frontend/src/lib/ws.ts" in commit_results[0][1]
    assert payload["repair"]["terminal_state"] == "escalated"
