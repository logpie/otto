from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from otto import v5_runner
from otto import v5_runner
from otto.lead import LeadResult
from otto.v5_clean_verify import CleanOracleIssue, CleanOracleResult, CleanOracleStepResult, Scope
from otto.v5_context_slicer import ChildScope, write_context_slice_for_child
from otto.v5_preflight import check_scaffold_compiles
from otto.v5_preflight_repair import OracleRepairResult, RepairPacket


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text(".worktrees/\notto_logs/\n.otto/\n", encoding="utf-8")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")


def _blocking_payload(kind: str, message: str) -> dict[str, Any]:
    return {
        "check": "smoke_clean_deploy",
        "passed": False,
        "issues": [{"kind": kind, "severity": "block", "message": message}],
    }


def _passing_payload() -> dict[str, Any]:
    return {"check": "smoke_clean_deploy", "passed": True, "issues": []}


def _ia_spec() -> dict[str, Any]:
    return {
        "schema_version": 3,
        "intent_claims": [{"id": "claim.issue", "text": "Create issue"}],
        "core_entities": [
            {
                "id": "issue",
                "name": "Issue",
                "primary_actions": [
                    {"id": "issue.create", "verb": "create", "intent_claim_ids": ["claim.issue"]}
                ],
            }
        ],
    }


def _charter() -> str:
    return (
        "# CHARTER\n\n"
        "## Information Architecture Contract\n\n"
        "```json\n"
        + json.dumps({"action_surfaces": [{"id": "issue.create"}]})
        + "\n```\n"
    )


def _clean_oracle_result(
    tmp_path: Path,
    *,
    passed: bool,
    scope: Scope = "scaffold",
    kind: str = "build_failed",
    message: str = "failed",
) -> CleanOracleResult:
    step = CleanOracleStepResult(
        id="check",
        status="passed" if passed else "failed",
        return_code=0 if passed else 1,
        command_identity="python -m otto.cli clean-verify",
        command=["python", "-m", "otto.cli", "clean-verify"],
        cwd=str(tmp_path),
        env={},
    )
    issue = CleanOracleIssue(
        kind=kind,
        severity="block",
        message=message,
        step_id=step.id,
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
        project_dir=tmp_path,
        temp_dir=None,
    )


def test_scaffold_missing_required_runtime_blocks_for_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_verify_from_clean(*_args: Any, **_kwargs: Any) -> CleanOracleResult:
        return _clean_oracle_result(
            tmp_path,
            passed=False,
            scope="scaffold",
            kind="no_npm",
            message="npm not on PATH",
        )

    monkeypatch.setattr("otto.v5_clean_verify.verify_from_clean_oracle", fake_verify_from_clean)

    issues = check_scaffold_compiles(tmp_path, architect_task_id="v5-arch")

    assert len(issues) == 1
    assert issues[0].severity == "block"
    assert issues[0].kind == "scaffold_compile_failed"
    assert issues[0].task_id == "v5-arch"


def test_scaffold_timeout_retries_once_with_larger_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[int] = []

    def fake_verify_from_clean(*_args: Any, **kwargs: Any) -> CleanOracleResult:
        timeouts.append(int(kwargs["timeout_s"]))
        if len(timeouts) == 1:
            return _clean_oracle_result(
                tmp_path,
                passed=False,
                scope="scaffold",
                kind="build_timeout",
                message="build timed out",
            )
        return _clean_oracle_result(tmp_path, passed=True, scope="scaffold")

    monkeypatch.setattr("otto.v5_clean_verify.verify_from_clean_oracle", fake_verify_from_clean)

    assert check_scaffold_compiles(tmp_path, timeout_s=5) == []
    assert timeouts == [5, 65]


def test_stale_port_cleanup_reports_still_bound_after_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from otto import v5_clean_verify

    (tmp_path / "CHARTER.md").write_text("- app: 127.0.0.1:19001\n", encoding="utf-8")
    killed: list[int] = []
    monkeypatch.setattr(v5_clean_verify, "_pids_for_port", lambda _port: [101])
    monkeypatch.setattr(v5_clean_verify, "_is_otto_owned_process", lambda *_args: True)
    monkeypatch.setattr(v5_clean_verify, "_terminate_pid", lambda pid: killed.append(pid))
    monkeypatch.setattr(v5_clean_verify, "_check_ports_free", lambda _ports: [19001])

    result = v5_clean_verify.cleanup_stale_declared_ports(tmp_path)

    assert result == [19001]
    assert result.killed_ports == [19001]
    assert result.freed_ports == []
    assert result.still_bound_ports == [19001]
    assert killed == [101]


@pytest.mark.asyncio
async def test_startup_port_cleanup_still_bound_routes_to_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from otto import v5_clean_verify

    _init_repo(tmp_path)
    cleanup_result_type = getattr(v5_clean_verify, "PortCleanupResult")
    calls = 0
    repairs: list[RepairPacket] = []

    def fake_cleanup(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return cleanup_result_type(killed_ports=[19001], still_bound_ports=[19001])
        return cleanup_result_type()

    async def fake_repair(packet: RepairPacket, **_kwargs: Any) -> OracleRepairResult:
        repairs.append(packet)
        return OracleRepairResult(verdict="pass", summary="made start.sh avoid busy port")

    monkeypatch.setattr("otto.v5_clean_verify.cleanup_stale_declared_ports", fake_cleanup)
    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", fake_repair)

    payload = await v5_runner._run_startup_port_cleanup_with_repair(
        project_dir=tmp_path,
        session_dir=tmp_path / "session",
        config={},
    )

    assert payload["passed"] is True
    assert calls == 2
    assert [packet.repair_unit["repair_phase"] for packet in repairs] == [
        "startup_port_cleanup"
    ]
    issues = repairs[0].latest_oracle_result["issues"]
    assert issues[0]["kind"] == "clean_deploy_port_busy"


@pytest.mark.asyncio
async def test_integration_worktree_setup_failure_blocks_without_project_dir_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    integration_calls: list[dict[str, Any]] = []

    async def fake_run_lead(**kwargs: Any) -> LeadResult:
        if kwargs.get("kind") == "integration":
            integration_calls.append(kwargs)
        return LeadResult(task_id=kwargs["task_id"], verdict="catastrophic", cost_usd=0.0)

    repair_packets: list[RepairPacket] = []

    async def fake_repair(packet: RepairPacket, **_kwargs: Any) -> OracleRepairResult:
        repair_packets.append(packet)
        return OracleRepairResult(verdict="merge_blocked", summary="worktree setup still broken")

    monkeypatch.setattr(v5_runner, "_setup_integration_worktree_once", lambda **_kwargs: (None, "boom"))
    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", fake_repair)
    monkeypatch.setattr(v5_runner, "run_lead", fake_run_lead)

    result = await v5_runner._run_integration(
        project_dir=repo,
        task_id="v5-integrate",
        intent="integrate",
        config={"default_branch": "main"},
        child_results={},
        integration_results={},
    )

    assert result.verdict == "merge_blocked"
    assert integration_calls == []
    assert [packet.repair_unit["repair_phase"] for packet in repair_packets] == [
        "integration_worktree_setup"
    ]


@pytest.mark.asyncio
async def test_child_merge_conflict_dispatches_agent_then_gates_on_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "shared.txt")
    _git(repo, "commit", "-q", "-m", "base")
    parent_branch = "i2p/integ/parent"
    child_branch = "i2p/build/v5-child"
    _git(repo, "branch", parent_branch, "main")
    _git(repo, "checkout", "-q", parent_branch)
    (repo / "shared.txt").write_text("parent\n", encoding="utf-8")
    _git(repo, "commit", "-am", "parent")
    child_worktree = tmp_path / "child-wt"
    _git(repo, "worktree", "add", "-q", "-b", child_branch, str(child_worktree), "main")
    (child_worktree / "shared.txt").write_text("child\n", encoding="utf-8")
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    repair_seen: list[Path] = []

    async def fake_oracle_repair_agent(repair_packet: Any, **kwargs: Any) -> OracleRepairResult:
        packet = repair_packet
        worktree = Path(packet.repair_unit["worktree"])
        repair_seen.append(worktree)
        conflict_packet = json.loads((repo / ".otto" / "merge-conflicts" / "latest.json").read_text())
        assert conflict_packet["unmerged_paths"] == ["shared.txt"]
        assert conflict_packet["conflicts"][0]["ours"] == "parent\n"
        assert conflict_packet["conflicts"][0]["theirs"] == "child\n"
        _git(worktree, "merge", "--no-ff", "--no-commit", parent_branch)
        (worktree / "shared.txt").write_text("parent\nchild\n", encoding="utf-8")
        _git(worktree, "add", "shared.txt")
        ok, detail = await kwargs["commit_hook"](packet, packet.latest_oracle_result)
        assert ok, detail
        return OracleRepairResult(verdict="pass", summary="repaired", cost_usd=0.1)

    async def fake_smoke_preflight(**_kwargs: Any) -> dict[str, Any]:
        return {"check": "smoke_clean_deploy", "passed": True, "issues": []}

    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", fake_oracle_repair_agent)
    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight_with_repair", fake_smoke_preflight)

    result = LeadResult(task_id="v5-child", verdict="pass", cost_usd=0.1)
    await v5_runner._merge_child_branch(
        project_dir=repo,
        child_task_id="v5-child",
        child_worktree=child_worktree,
        child_session_dir=session_dir,
        parent_integration_branch=parent_branch,
        result=result,
        config={"default_branch": "main"},
    )

    assert result.verdict == "pass"
    assert repair_seen == [child_worktree]
    assert _git(repo, "show", f"{parent_branch}:shared.txt").stdout == "parent\nchild\n"


def test_context_slice_resolver_replaces_ambiguous_full_fallback(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    child = tmp_path / "child"
    worktree = tmp_path / "worktree"
    parent_spec = parent / "spec" / "spec.json"
    child_spec = child / "spec" / "spec.json"
    charter = worktree / "CHARTER.md"
    parent_spec.parent.mkdir(parents=True)
    charter.parent.mkdir(parents=True)
    parent_spec.write_text(json.dumps(_ia_spec()), encoding="utf-8")
    charter.write_text(_charter(), encoding="utf-8")

    result = write_context_slice_for_child(
        project_dir=tmp_path,
        child_session_dir=child,
        child_scope=ChildScope(child_id="child", action_ids=["issue.make"]),
        parent_spec_path=parent_spec,
        full_charter_path=charter,
        child_spec_path=child_spec,
        scope_resolver=lambda _spec, scope, _reason: ChildScope(
            child_id=scope.child_id,
            action_ids=["issue.create"],
        ),
    )

    audit = json.loads((child / "context_slice.json").read_text(encoding="utf-8"))
    assert audit["fallback_to_full"] is False
    assert audit["scope_resolution"]["status"] == "resolved"
    assert result.spec["intent_claims"][0]["id"] == "claim.issue"


def test_context_slice_records_last_resort_full_context_without_resolver(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    child = tmp_path / "child"
    worktree = tmp_path / "worktree"
    parent_spec = parent / "spec" / "spec.json"
    child_spec = child / "spec" / "spec.json"
    charter = worktree / "CHARTER.md"
    parent_spec.parent.mkdir(parents=True)
    charter.parent.mkdir(parents=True)
    parent_spec.write_text(json.dumps(_ia_spec()), encoding="utf-8")
    charter.write_text(_charter(), encoding="utf-8")

    write_context_slice_for_child(
        project_dir=tmp_path,
        child_session_dir=child,
        child_scope=ChildScope(child_id="child", task_intent="polish shell"),
        parent_spec_path=parent_spec,
        full_charter_path=charter,
        child_spec_path=child_spec,
    )

    audit = json.loads((child / "context_slice.json").read_text(encoding="utf-8"))
    assert audit["fallback_to_full"] is True
    assert audit["fallback_last_resort"] is True
    assert audit["scope_resolution"]["status"] == "last_resort_full_context"


@pytest.mark.asyncio
async def test_context_scope_resolution_agent_returns_resolved_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, Any]] = []

    class Options:
        pass

    async def fake_agent(*_args: Any, **_kwargs: Any) -> tuple[str, float, str, dict[str, Any]]:
        return (
            'noise {"task_intent": "create issue", "owned_paths": ["src/issues.ts"], '
            '"action_ids": ["issue.create"]}',
            0.0,
            "scope-session",
            {},
        )

    monkeypatch.setattr("otto.agent.make_agent_options", lambda *_args, **_kwargs: Options())
    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_agent)

    payload = await v5_runner._resolve_child_scope_with_agent(
        project_dir=tmp_path,
        child_session_dir=tmp_path / "child",
        child_task_id="child",
        child_scope={"child_id": "child", "action_ids": ["issue.make"]},
        parent_spec_path=tmp_path / "spec.json",
        fallback_reason="explicit action ids not found: issue.make",
        config={},
        on_event=events.append,
    )

    assert payload == {
        "task_intent": "create issue",
        "owned_paths": ["src/issues.ts"],
        "action_ids": ["issue.create"],
    }
    assert events[-1]["event"] == "context_scope_resolution_agent_done"
    assert events[-1]["ok"] is True

