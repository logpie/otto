from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from otto import v5_runner
from otto.audit_loop import (
    FailingFeature,
    RepairAttempt,
    features_to_repair,
    repair_failing_features,
)
from otto.build import BuildAgentInput, BuildAgentOutput, GroupStatus, run_build
from otto.lead import LeadResult
from otto.spec_compile import Feature, Group, RepoTestCheck, Spec
from otto.v5_clean_verify import CleanVerifyResult
from otto.v5_context_slicer import ChildScope, write_context_slice_for_child
from otto.v5_preflight import check_scaffold_compiles
from otto.v5_preflight_repair import (
    AgentRepairRequest,
    AgentRepairResult,
    OracleRepairResult,
    PreflightRepairController,
)


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


def _log_events(session_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (session_dir / "preflight-repair.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _spec_by_group(feature_to_group: dict[str, str]) -> Spec:
    groups = [
        Group(id=group_id, name=group_id.title())
        for group_id in dict.fromkeys(feature_to_group.values())
    ]
    return Spec(
        intent="x",
        groups=groups,
        features=[
            Feature(id=feature_id, name=feature_id, group_id=group_id)
            for feature_id, group_id in feature_to_group.items()
        ],
    )


def _verdict(feature_id: str, verdict: str = "partial") -> dict[str, Any]:
    return {"feature_id": feature_id, "verdict": verdict, "detail": "broken"}


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


def _passing_check() -> RepoTestCheck:
    return RepoTestCheck(command=("python", "-c", "print('ok')"), timeout_s=10)


def test_scaffold_missing_required_runtime_blocks_for_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_verify_from_clean(*_args: Any, **_kwargs: Any) -> CleanVerifyResult:
        return CleanVerifyResult(
            passed=False,
            scope="scaffold",
            failure_kind="no_npm",
            failure_message="npm not on PATH",
        )

    monkeypatch.setattr("otto.v5_clean_verify.verify_from_clean", fake_verify_from_clean)

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

    def fake_verify_from_clean(*_args: Any, **kwargs: Any) -> CleanVerifyResult:
        timeouts.append(int(kwargs["timeout_s"]))
        if len(timeouts) == 1:
            return CleanVerifyResult(
                passed=False,
                scope="scaffold",
                failure_kind="build_timeout",
                failure_message="build timed out",
            )
        return CleanVerifyResult(passed=True, scope="scaffold")

    monkeypatch.setattr("otto.v5_clean_verify.verify_from_clean", fake_verify_from_clean)

    assert check_scaffold_compiles(tmp_path, timeout_s=5) == []
    assert timeouts == [5, 65]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "message", "detail"),
    [
        ("filename_too_long", "File name too long: generated label", {"renamed": []}),
        ("clean_deploy_script_valid_failed", "start.sh is not executable", {"chmod_x": []}),
    ],
)
async def test_preflight_autofix_noop_falls_back_to_agent(
    tmp_path: Path,
    kind: str,
    message: str,
    detail: dict[str, Any],
) -> None:
    session_dir = tmp_path / "session"
    worktree = tmp_path / "repo"
    session_dir.mkdir()
    worktree.mkdir()
    requests: list[AgentRepairRequest] = []
    runs = 0

    def run_preflight() -> dict[str, Any]:
        nonlocal runs
        runs += 1
        if requests:
            return _passing_payload()
        return _blocking_payload(kind, message)

    async def repair(request: AgentRepairRequest) -> AgentRepairResult:
        requests.append(request)
        return AgentRepairResult(ok=True, summary="agent fixed deterministic no-op")

    controller = PreflightRepairController(
        session_dir=session_dir,
        worktree_path=worktree,
        filename_repair=lambda *_args: detail,
        chmod_repair=lambda *_args: detail,
        agent_repair=repair,
    )

    result = await controller.repair_until_clean(run_preflight)

    assert result.terminal_state == "continued"
    assert runs == 2
    assert len(requests) == 1
    events = _log_events(session_dir)
    assert any(event.get("outcome") == "no_op_agent_fallback" for event in events)
    assert not any(
        event.get("action") == "auto_fix" and event.get("outcome") == "repaired"
        for event in events
    )


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

    cleanup_result_type = getattr(v5_clean_verify, "PortCleanupResult")
    calls = 0
    repairs: list[AgentRepairRequest] = []

    def fake_cleanup(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return cleanup_result_type(killed_ports=[19001], still_bound_ports=[19001])
        return cleanup_result_type()

    async def fake_repair(
        *,
        request: AgentRepairRequest,
        task_id: str,
        project_dir: Path,
        config: dict[str, Any],
        integration_branch: str | None,
        on_event: Any = None,
    ) -> AgentRepairResult:
        del task_id, project_dir, config, integration_branch, on_event
        repairs.append(request)
        return AgentRepairResult(ok=True, summary="made start.sh avoid busy port")

    monkeypatch.setattr("otto.v5_clean_verify.cleanup_stale_declared_ports", fake_cleanup)
    monkeypatch.setattr(v5_runner, "_run_preflight_repair_agent", fake_repair)

    payload = await v5_runner._run_startup_port_cleanup_with_repair(
        project_dir=tmp_path,
        session_dir=tmp_path / "session",
        config={},
    )

    assert payload["passed"] is True
    assert calls == 2
    assert [request.failure_kind for request in repairs] == ["port_busy"]


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

    async def fake_repair(**_kwargs: Any) -> AgentRepairResult:
        return AgentRepairResult(ok=False, summary="worktree setup still broken")

    monkeypatch.setattr(v5_runner, "_setup_integration_worktree_once", lambda **_kwargs: (None, "boom"))
    monkeypatch.setattr(v5_runner, "_run_preflight_repair_agent", fake_repair)
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

    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", fake_oracle_repair_agent)
    monkeypatch.setattr(v5_runner, "smoke_clean_deploy", lambda *_args, **_kwargs: [])

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


def test_audit_selection_gives_each_failing_group_first_attempt_before_cap() -> None:
    spec = _spec_by_group({"f1": "g1", "f2": "g2", "f3": "g3"})

    selected = features_to_repair(
        spec,
        [_verdict("f1"), _verdict("f2"), _verdict("f3")],
        max_attempts_per_run=1,
    )

    assert [feature.feature_id for feature in selected] == ["f1", "f2", "f3"]


def test_audit_loop_reserves_reaudit_budget_for_first_fix() -> None:
    spec = _spec_by_group({"f1": "g1"})
    fix_calls: list[str] = []
    audit_calls: list[list[str]] = []

    async def fix_agent(failing: FailingFeature, group: Group) -> RepairAttempt:
        fix_calls.append(f"{group.id}:{failing.feature_id}")
        return RepairAttempt(
            feature_id=failing.feature_id,
            group_id=group.id,
            attempt_number=1,
            succeeded=True,
        )

    async def re_audit(feature_ids: list[str]) -> list[dict[str, Any]]:
        audit_calls.append(list(feature_ids))
        return [_verdict(feature_id, "passed") for feature_id in feature_ids]

    result = asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[_verdict("f1")],
            fix_agent=fix_agent,
            re_audit=re_audit,
            max_audit_passes=1,
            audit_passes_so_far=1,
        )
    )

    assert fix_calls == ["g1:f1"]
    assert audit_calls == [["f1"]]
    assert result.audit_passes_run == 2


def test_audit_loop_raises_on_orphan_failing_feature() -> None:
    spec = Spec(
        intent="x",
        groups=[Group(id="g", name="G")],
        features=[Feature(id="orphan", name="orphan", group_id="")],
    )

    with pytest.raises(ValueError, match="without repair group"):
        features_to_repair(spec, [_verdict("orphan")], max_attempts_per_run=10)


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


def test_out_of_scope_write_blocks_until_amended_or_reverted(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "peer.txt").write_text("baseline\n", encoding="utf-8")
    _git(tmp_path, "add", "peer.txt")
    _git(tmp_path, "commit", "-q", "-m", "peer baseline")
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    attempts = 0

    async def overreaching_agent(agent_input: BuildAgentInput) -> BuildAgentOutput:
        nonlocal attempts
        attempts += 1
        (agent_input.worktree / "owned.txt").write_text("owned\n", encoding="utf-8")
        (agent_input.worktree / "peer.txt").write_text(f"tampered {attempts}\n", encoding="utf-8")
        return BuildAgentOutput(succeeded=True)

    spec = Spec(
        intent="x",
        groups=[
            Group(
                id="leaf",
                name="Leaf",
                owned_paths=["owned.txt"],
                checks=[_passing_check()],
            ),
            Group(id="peer", name="Peer", owned_paths=["peer.txt"], checks=[]),
        ],
    )

    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=overreaching_agent,
        )
    )
    leaf = next(group for group in result.group_results if group.group_id == "leaf")

    assert leaf.status == GroupStatus.FAILED_SCOPE
    assert "peer.txt" in leaf.scope_warnings
    assert "scope violation" in leaf.failure_narrative
    assert attempts >= 1
