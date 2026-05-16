from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from otto import v5_runner
from otto.lead import LeadResult
from otto.queue.task_graph import get_retry_count, get_task, record_task, set_verdict
from otto.v5_clean_verify import CleanOracleIssue, CleanOracleResult, CleanOracleStepResult, Scope
from otto.v5_preflight import PreflightIssue
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
    (repo / "CHARTER.md").write_text("# Charter\n\n", encoding="utf-8")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")


def _oracle_result(
    *,
    passed: bool,
    scope: Scope = "subtree",
    kind: str = "clean_deploy_start_failed",
    message: str = "start failed",
    paths: list[str] | None = None,
) -> CleanOracleResult:
    step = CleanOracleStepResult(
        id="start",
        status="passed" if passed else "failed",
        return_code=0 if passed else 1,
        command_identity="python -m otto.cli clean-verify",
        command=["python", "-m", "otto.cli", "clean-verify"],
        cwd=".",
        env={},
    )
    issue = CleanOracleIssue(
        kind=kind,
        severity="block",
        message=message,
        step_id=step.id,
        paths=paths or [],
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
        project_dir=Path("."),
        temp_dir=None,
    )


@pytest.mark.asyncio
async def test_child_verify_repair_uses_packet_journal_and_blocks_unreviewed_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    record_task(repo, task_id="v5-child", intent="Build child", parent_task_id="root")
    child_session_dir = tmp_path / "session"
    (child_session_dir / "spec").mkdir(parents=True)
    (child_session_dir / "spec" / "spec.json").write_text(
        json.dumps({"routes": ["/"], "features": [{"id": "f1"}]}),
        encoding="utf-8",
    )
    (repo / "app.py").write_text("print('partial')\n", encoding="utf-8")

    async def legacy_lead(**_kwargs: Any) -> LeadResult:
        raise AssertionError("child verify repair must not start a legacy Lead session")

    packets: list[RepairPacket] = []

    async def fake_repair(packet: RepairPacket, **_kwargs: Any) -> OracleRepairResult:
        packets.append(packet)
        return OracleRepairResult(
            verdict="pass",
            summary="clean oracle passed but child verdict stayed partial",
            composite_gate={"passed": True},
            packet_path=str(packet.packet_path),
        )

    monkeypatch.setattr(v5_runner, "_run_lead_with_fallback", legacy_lead)
    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", fake_repair)

    result = await v5_runner._ensure_child_merge_ready(
        project_dir=repo,
        child_task_id="v5-child",
        child_worktree=repo,
        child_session_dir=child_session_dir,
        parent_integration_branch="main",
        original_intent="Build child",
        result=LeadResult(
            task_id="v5-child",
            verdict="partial",
            cost_usd=0.2,
            verify_called=True,
            verify_result={"verdict": "partial", "summary": "missing one journey"},
        ),
        config={"max_turns_per_call": 1},
        max_parallel=1,
        run_started_at=None,
        spec_path=child_session_dir / "spec" / "spec.json",
    )

    assert len(packets) == 1
    packet = packets[0]
    assert packet.repair_unit["phase"] == "child_verify"
    assert packet.repair_unit["worktree"] == str(repo)
    assert packet.acceptance_oracle["verify_scope"] == "subtree"
    assert "clean-verify" in " ".join(packet.acceptance_oracle["command"])
    assert "--repair-packet" in packet.acceptance_oracle["command"]
    assert packet.acceptance_oracle["success_criteria"]["composite_gate"] is True
    assert packet.acceptance_oracle["success_criteria"]["child_merge_gate"] == "pass_or_reviewed_partial"
    assert packet.product_contract["spec"]["path"] == str(child_session_dir / "spec" / "spec.json")
    assert packet.integration_context["child_verdict"]["verdict"] == "partial"
    assert "app.py" in packet.integration_context["child_diff"]["name_only"]
    assert packet.attempt_history[0]["type"] == "pre_repair_verdict"
    assert packet.packet_dir.is_relative_to(child_session_dir / "repair")
    assert result.verdict == "merge_blocked"
    assert result.verify_result is not None
    assert result.verify_result["repair_packet"] == str(packet.packet_path)
    child_task = get_task(repo, "v5-child")
    assert child_task is not None
    assert child_task["merge_blocked_origin"] == "verification"


@pytest.mark.asyncio
async def test_merge_conflict_repair_packet_carries_three_way_context_and_no_old_adapter(
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

    packets: list[RepairPacket] = []

    async def fake_repair(packet: RepairPacket, **kwargs: Any) -> OracleRepairResult:
        packets.append(packet)
        assert packet.repair_unit["phase"] == "merge"
        assert packet.repair_unit["allowed_paths"] == ["shared.txt"]
        assert packet.repair_unit["scope_policy"] == "allowed_paths"
        conflict = packet.integration_context["conflict_packet"]
        assert conflict["unmerged_paths"] == ["shared.txt"]
        assert conflict["conflicts"][0]["base"] == "base\n"
        assert conflict["conflicts"][0]["ours"] == "parent\n"
        assert conflict["conflicts"][0]["theirs"] == "child\n"
        assert packet.integration_context["merge_refs"]["base_ref"]
        assert packet.integration_context["merge_refs"]["ours_ref"] == parent_branch
        assert packet.integration_context["merge_refs"]["theirs_ref"] == child_branch
        assert packet.integration_context["merge_safety"]["forbid_whole_side_checkout"] is True
        assert packet.acceptance_oracle["success_criteria"]["merge_retry"] is True
        assert packet.acceptance_oracle["success_criteria"]["clean_deploy"] is True

        _git(child_worktree, "merge", "--no-ff", "--no-commit", parent_branch)
        (child_worktree / "shared.txt").write_text("parent\nchild\n", encoding="utf-8")
        _git(child_worktree, "add", "shared.txt")
        ok, detail = await kwargs["commit_hook"](packet, _oracle_result(passed=True))
        assert ok, detail
        return OracleRepairResult(verdict="pass", summary="resolved", packet_path=str(packet.packet_path))

    async def fake_smoke_preflight(**_kwargs: Any) -> dict[str, Any]:
        return {"check": "smoke_clean_deploy", "passed": True, "issues": []}

    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", fake_repair)
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
    assert len(packets) == 1
    assert _git(repo, "show", f"{parent_branch}:shared.txt").stdout == "parent\nchild\n"


@pytest.mark.asyncio
async def test_scaffold_oracle_failure_repairs_with_packet_without_architect_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    record_task(repo, task_id="v5-arch", intent="Architect scaffold")
    set_verdict(repo, "v5-arch", "pass")

    monkeypatch.setattr(
        v5_runner,
        "check_scaffold_compiles",
        lambda *_args, **_kwargs: [
            PreflightIssue(
                kind="scaffold_compile_failed",
                severity="block",
                message="legacy compile failed",
                task_id="v5-arch",
            )
        ],
        raising=False,
    )
    monkeypatch.setattr(
        v5_runner,
        "verify_from_clean_oracle",
        lambda *_args, **_kwargs: _oracle_result(
            passed=False,
            scope="scaffold",
            kind="build_failed",
            message="scaffold build failed",
            paths=["package.json"],
        ),
        raising=False,
    )
    packets: list[RepairPacket] = []

    async def fake_repair(packet: RepairPacket, **_kwargs: Any) -> OracleRepairResult:
        packets.append(packet)
        return OracleRepairResult(
            verdict="merge_blocked",
            summary="scaffold still fails",
            escalation={"reason": "budget_exhausted"},
            packet_path=str(packet.packet_path),
        )

    events: list[dict[str, Any]] = []
    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", fake_repair)

    await v5_runner._process_children(
        project_dir=repo,
        parent_task_id="root",
        config={"max_turns_per_call": 1},
        max_parallel=1,
        tree_budget_usd=10.0,
        child_results={},
        integration_results={},
        on_event=events.append,
    )

    assert len(packets) == 1
    packet = packets[0]
    assert packet.repair_unit["phase"] == "scaffold"
    assert packet.repair_unit["task_id"] == "v5-arch"
    assert packet.acceptance_oracle["verify_scope"] == "scaffold"
    assert "clean-verify" in " ".join(packet.acceptance_oracle["command"])
    assert "--repair-packet" in packet.acceptance_oracle["command"]
    assert packet.acceptance_oracle["success_criteria"]["scaffold_scope"] is True
    assert not any(event.get("event") == "architect_retry" for event in events)
    arch_task = get_task(repo, "v5-arch")
    assert arch_task is not None
    assert arch_task["verdict"] == "merge_blocked"
    assert arch_task["merge_blocked_origin"] == "scaffold"


@pytest.mark.asyncio
async def test_scaffold_contract_structural_invalid_reenters_architect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    record_task(repo, task_id="v5-arch", intent="Architect scaffold")
    set_verdict(repo, "v5-arch", "pass")

    monkeypatch.setattr(
        v5_runner,
        "check_scaffold_compiles",
        lambda *_args, **_kwargs: [],
        raising=False,
    )
    monkeypatch.setattr(
        v5_runner,
        "verify_from_clean_oracle",
        lambda *_args, **_kwargs: _oracle_result(
            passed=False,
            scope="scaffold",
            kind="contract_structural_invalid",
            message="contract routes contradict required IA",
        ),
        raising=False,
    )

    async def fake_repair(_packet: RepairPacket, **_kwargs: Any) -> OracleRepairResult:
        raise AssertionError("structural contract invalidity must re-enter architect, not repair scaffold")

    events: list[dict[str, Any]] = []
    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", fake_repair)

    await v5_runner._process_children(
        project_dir=repo,
        parent_task_id="root",
        config={"max_turns_per_call": 1},
        max_parallel=1,
        tree_budget_usd=10.0,
        child_results={},
        integration_results={},
        on_event=events.append,
    )

    assert get_retry_count(repo, "v5-arch") == 1
    arch_task = get_task(repo, "v5-arch")
    assert arch_task is not None
    assert arch_task["verdict"] is None
    assert any(event.get("event") == "architect_retry" for event in events)
