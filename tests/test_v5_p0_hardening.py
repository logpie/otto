from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from otto.lead import LeadResult, _canonicalize_verdict_payload, run_lead
from otto.queue.task_graph import get_task, read_graph, record_task, set_verdict
from otto.spec_compile_flat import StructuredSpecValidationError, compile_flat_spec
from otto.v5_clean_verify import CleanOracleIssue, CleanOracleResult, CleanOracleStepResult, Scope
from otto.v5_preflight import check_scaffold_compiles, smoke_clean_deploy
from otto.v5_preflight_repair import OracleRepairResult, RepairPacket
from otto.v5_runner import _run_child


def _pass_verdict_payload() -> dict[str, Any]:
    return {
        "verdict": "pass",
        "summary": "verified",
        "journeys": [{"id": "smoke", "passed": True, "detail": "ran smoke"}],
        "evidence": ["pytest tests/smoke -q"],
    }


def _clean_oracle_result(
    tmp_path: Path,
    *,
    passed: bool,
    scope: Scope,
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


@pytest.mark.asyncio
async def test_unverified_child_runs_verify_repair_before_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from otto import v5_runner

    project = tmp_path / "repo"
    project.mkdir()
    parent_session = project / "otto_logs" / "sessions" / "parent"
    (parent_session / "spec").mkdir(parents=True)
    (parent_session / "spec" / "spec.json").write_text(
        json.dumps({"schema_version": 3, "behavior_journeys": []}),
        encoding="utf-8",
    )
    task_id = "v5-child"
    record_task(project, task_id="root", intent="root")
    record_task(
        project,
        task_id=task_id,
        intent="Build the child feature",
        parent_task_id="root",
        integration_branch="i2p/integ/root",
    )
    child_worktree = project / ".worktrees" / task_id
    child_worktree.mkdir(parents=True)

    lead_calls: list[dict[str, Any]] = []
    merge_calls: list[str] = []

    async def fake_run_lead(**kwargs: Any) -> LeadResult:
        lead_calls.append(kwargs)
        set_verdict(project, task_id, "unverified", cost_usd=0.1)
        return LeadResult(
            task_id=task_id,
            verdict="unverified",
            cost_usd=0.1,
            decomposition="inline",
            verify_called=True,
            verify_result={"verdict": "unverified", "summary": "no journey proof"},
        )

    def fake_merge_child_into_integration(
        *,
        project_dir: Path,
        child_task_id: str,
        parent_integration_branch: str,
    ) -> tuple[bool, str]:
        del project_dir, parent_integration_branch
        merge_calls.append(child_task_id)
        return True, "merged"

    monkeypatch.setattr(v5_runner, "run_lead", fake_run_lead)
    async def fake_smoke_preflight(**_kwargs: Any) -> dict[str, Any]:
        return {"check": "smoke_clean_deploy", "passed": True, "issues": []}

    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight_with_repair", fake_smoke_preflight)
    repair_packets: list[RepairPacket] = []

    async def fake_oracle_repair_agent(packet: RepairPacket, **_kwargs: Any) -> OracleRepairResult:
        repair_packets.append(packet)
        (child_worktree / "fixed.txt").write_text("verified\n", encoding="utf-8")
        verdict_path = Path(packet.repair_unit["canonical_verdict_path"])
        verdict_path.parent.mkdir(parents=True, exist_ok=True)
        verdict_path.write_text(json.dumps(_pass_verdict_payload()), encoding="utf-8")
        return OracleRepairResult(
            verdict="pass",
            summary="child verify repair passed",
            cost_usd=0.1,
            packet_path=str(packet.packet_path),
            composite_gate={"passed": True},
        )

    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", fake_oracle_repair_agent)
    monkeypatch.setattr(
        "otto.v5_branching.setup_child_worktree",
        lambda **_kwargs: child_worktree,
    )
    monkeypatch.setattr(
        "otto.v5_branching.commit_worktree",
        lambda **_kwargs: (True, "committed"),
    )
    monkeypatch.setattr(
        "otto.v5_branching.merge_child_into_integration",
        fake_merge_child_into_integration,
    )
    monkeypatch.setattr(
        v5_runner,
        "_record_and_check_integration_union",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(v5_runner, "_git_capture", lambda *_args, **_kwargs: "abc123")

    result = await _run_child(
        project_dir=project,
        entry={
            "task_id": task_id,
            "intent": "Build the child feature",
            "parent_session_dir": str(parent_session),
            "integration_branch": "i2p/integ/root",
        },
        config={},
        max_parallel=1,
    )

    assert result.verdict == "pass"
    assert len(lead_calls) == 1
    assert len(repair_packets) == 1
    assert repair_packets[0].repair_unit["phase"] == "child_verify"
    assert merge_calls == [task_id]
    assert (get_task(project, task_id) or {}).get("verdict") == "pass"


@pytest.mark.parametrize(
    "payload",
    [
        {"success": True},
        {"passed": True},
        {"ok": True},
        {"status": "passed"},
        {"result": "success"},
        {"outcome": "ok"},
        {"state": "done"},
        {"verdict": "pass", "summary": "looks good"},
        {"verdict": "pass", "summary": "empty journeys", "journeys": []},
    ],
)
def test_vague_success_payload_without_evidence_is_unverified(
    payload: dict[str, Any],
) -> None:
    canonical = _canonicalize_verdict_payload(payload)

    assert canonical is not None
    assert canonical["verdict"] == "unverified"
    assert canonical.get("journeys") == []


def test_evidence_bearing_pass_payload_stays_pass() -> None:
    canonical = _canonicalize_verdict_payload(_pass_verdict_payload())

    assert canonical is not None
    assert canonical["verdict"] == "pass"


@pytest.mark.asyncio
async def test_runner_verification_plan_exception_preserves_agent_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0c contract: when the otto-internal validator crashes, the agent's
    'pass' is PRESERVED (LAND-then-annotate) instead of silently
    downgraded to 'unverified'. The crash becomes an advisory finding —
    a validator bug isn't a build defect. See commit 2ff6addc3."""

    async def fake_agent(
        _prompt: str,
        _options: Any,
        *,
        log_dir: Path,
        phase_name: str,
        phase_label: str,
        timeout: int,
        project_dir: Path,
    ) -> tuple[str, float, str, dict[str, Any]]:
        del phase_name, phase_label, timeout, project_dir
        session_dir = log_dir.parent
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "verdict.json").write_text(
            json.dumps(_pass_verdict_payload()),
            encoding="utf-8",
        )
        return "done", 0.0, "agent-session", {}

    def explode_validate(**_kwargs: Any) -> Any:
        raise RuntimeError("verification matrix crashed")

    monkeypatch.setattr("otto.agent.make_agent_options", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_agent)
    monkeypatch.setattr("otto.mcp_tools.create_otto_mcp_server", lambda **_k: object())
    monkeypatch.setattr("otto.v5_verification_plan.validate_lead_verdict", explode_validate)

    session_dir = tmp_path / "session"
    result = await run_lead(
        task_id="leaf",
        intent="build leaf",
        project_dir=tmp_path,
        session_dir=session_dir,
        integration_branch="main",
        config={},
    )

    assert result.verdict == "pass"
    assert (read_graph(tmp_path)["tasks"]["leaf"]).get("verdict") == "pass"
    summary = json.loads((session_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["verdict"] == "pass"


@pytest.mark.asyncio
async def test_compile_flat_spec_raises_instead_of_writing_empty_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def invalid_compile(
        _prompt: str,
        _options: Any,
        log_dir: Path,
        _project_dir: Path,
    ) -> str:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "messages.jsonl").write_text("", encoding="utf-8")
        return "not json"

    monkeypatch.setattr(
        "otto.spec_compile_flat.make_agent_options",
        lambda *_a, **_k: SimpleNamespace(
            max_turns=1,
            provider="claude",
            model="claude-test",
        ),
    )
    monkeypatch.setattr("otto.spec_compile_flat._run_compile", invalid_compile)

    session_dir = tmp_path / "otto_logs" / "sessions" / "compile"
    with pytest.raises(StructuredSpecValidationError, match="valid JSON shape"):
        await compile_flat_spec(
            project_dir=tmp_path,
            session_dir=session_dir,
            intent="build an issue tracker",
            config={"spec_compile_no_cache": True},
            max_retries=1,
        )

    assert not (session_dir / "spec" / "spec.json").exists()


def test_scaffold_unknown_verify_kind_blocks_for_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_verify_from_clean(*_args: Any, **_kwargs: Any) -> CleanOracleResult:
        return _clean_oracle_result(
            tmp_path,
            passed=False,
            scope="scaffold",
            kind=cast(Any, "new_unknown_failure"),
            message="new provider failure shape",
        )

    monkeypatch.setattr("otto.v5_clean_verify.verify_from_clean_oracle", fake_verify_from_clean)

    issues = check_scaffold_compiles(tmp_path, architect_task_id="v5-arch")

    assert len(issues) == 1
    assert issues[0].severity == "block"
    assert issues[0].task_id == "v5-arch"
    assert "new_unknown_failure" in issues[0].message


def test_scaffold_copy_failure_blocks_for_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_verify_from_clean(*_args: Any, **_kwargs: Any) -> CleanOracleResult:
        return _clean_oracle_result(
            tmp_path,
            passed=False,
            scope="scaffold",
            kind="copy_failed",
            message="could not copy repo",
        )

    monkeypatch.setattr("otto.v5_clean_verify.verify_from_clean_oracle", fake_verify_from_clean)

    issues = check_scaffold_compiles(tmp_path, architect_task_id="v5-arch")

    assert len(issues) == 1
    assert issues[0].severity == "block"
    assert issues[0].task_id == "v5-arch"
    assert "could not copy repo" in issues[0].message


def test_clean_deploy_unknown_verify_kind_blocks_for_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "start.sh").write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")

    def fake_verify_from_clean(*_args: Any, **_kwargs: Any) -> CleanOracleResult:
        return _clean_oracle_result(
            tmp_path,
            passed=False,
            scope="subtree",
            kind=cast(Any, "surprise_runtime_kind"),
            message="runtime verifier failed oddly",
        )

    monkeypatch.setattr("otto.v5_clean_verify.verify_from_clean_oracle", fake_verify_from_clean)

    issues = smoke_clean_deploy(tmp_path)

    assert len(issues) == 1
    assert issues[0].severity == "block"
    assert issues[0].kind == "clean_deploy_smoke_error"
    assert "surprise_runtime_kind" in issues[0].message
