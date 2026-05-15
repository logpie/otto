from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from otto import v5_runner
from otto.build import (
    BuildAgentInput,
    _build_agent_prompt,
    _failed_check_repair_narrative,
    _write_build_context_packet,
)
from otto.checks import Evidence
from otto.spec_compile import BrowserJourney, Feature, Group, Spec
from otto.v5_clean_verify import CleanOracleIssue, CleanOracleResult, CleanOracleStepResult, Scope
from otto.v5_preflight_repair import OracleRepairResult, RepairPacket


pytestmark = pytest.mark.smoke


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
    (repo / ".gitignore").write_text("otto_logs/\n.otto/\n.worktrees/\n", encoding="utf-8")
    (repo / "CHARTER.md").write_text("# CHARTER\n\n", encoding="utf-8")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init", "--no-verify")


def _oracle_result(
    *,
    passed: bool,
    scope: Scope = "subtree",
    kind: str = "start_failed",
    message: str = "start failed",
    paths: list[str] | None = None,
    artifact_refs: list[str] | None = None,
) -> CleanOracleResult:
    step = CleanOracleStepResult(
        id="start",
        status="passed" if passed else "failed",
        return_code=0 if passed else 1,
        command_identity="python -m otto.cli clean-verify",
        command=["python", "-m", "otto.cli", "clean-verify"],
        cwd=".",
        env={},
        artifact_paths=list(artifact_refs or []),
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
        artifact_path_refs=list(artifact_refs or []),
        command=step.command,
        env=step.env,
        project_dir=Path("."),
        temp_dir=None,
    )


def _blocking_payload(kind: str, message: str) -> dict[str, Any]:
    return {
        "check": "smoke_clean_deploy",
        "passed": False,
        "issues": [{"kind": kind, "severity": "block", "message": message}],
        "clean_oracle_result": _oracle_result(
            passed=False,
            kind=kind,
            message=message,
        ).to_jsonable(),
    }


def _passing_payload() -> dict[str, Any]:
    return {
        "check": "smoke_clean_deploy",
        "passed": True,
        "issues": [],
        "clean_oracle_result": _oracle_result(passed=True).to_jsonable(),
    }


def test_legacy_symptom_adapter_symbols_are_removed_from_production() -> None:
    source = inspect.getsource(__import__("otto.v5_preflight_repair").v5_preflight_repair)
    request_name = "Agent" + "RepairRequest"
    controller_name = "Preflight" + "RepairController"
    classifier_name = "classify_" + "preflight_issue"
    adapter_name = "_run_" + "preflight_repair_agent"
    forbidden = [
        f"class {request_name}",
        f"class {controller_name}",
        f"def {classifier_name}",
        "def " + "repair_" + "until_clean",
        f"def {adapter_name}",
        f"{request_name}(",
    ]
    for needle in forbidden:
        assert needle not in source
    assert adapter_name not in inspect.getsource(v5_runner)


@pytest.mark.asyncio
async def test_startup_port_cleanup_uses_repair_packet_without_compat_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from otto import v5_clean_verify

    _init_repo(tmp_path)
    cleanup_result_type = getattr(v5_clean_verify, "PortCleanupResult")
    calls = 0
    packets: list[RepairPacket] = []
    commit_hooks: list[Any] = []

    def fake_cleanup(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return cleanup_result_type(killed_ports=[19001], still_bound_ports=[19001])
        return cleanup_result_type()

    adapter_name = "_run_" + "preflight_repair_agent"

    async def forbidden_adapter(**_kwargs: Any) -> Any:
        raise AssertionError(f"startup cleanup must not use {adapter_name}")

    async def fake_repair(packet: RepairPacket, **kwargs: Any) -> OracleRepairResult:
        packets.append(packet)
        commit_hooks.append(kwargs.get("commit_hook"))
        return OracleRepairResult(verdict="pass", summary="fixed", packet_path=str(packet.packet_path))

    monkeypatch.setattr("otto.v5_clean_verify.cleanup_stale_declared_ports", fake_cleanup)
    monkeypatch.setattr(v5_runner, adapter_name, forbidden_adapter, raising=False)
    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", fake_repair)

    payload = await v5_runner._run_startup_port_cleanup_with_repair(
        project_dir=tmp_path,
        session_dir=tmp_path / "session",
        config={"max_turns_per_call": 1},
    )

    assert payload["passed"] is True
    assert calls == 2
    assert len(packets) == 1
    packet = packets[0]
    assert packet.repair_unit["repair_phase"] == "startup_port_cleanup"
    assert packet.repair_unit["phase"] == "preflight"
    assert packet.acceptance_oracle["verify_scope"] == "subtree"
    assert "clean-verify" in " ".join(packet.acceptance_oracle["command"])
    assert "--repair-packet" in packet.acceptance_oracle["command"]
    assert packet.current_state["scope_baseline"] is not None
    assert callable(commit_hooks[0])


@pytest.mark.asyncio
async def test_checkout_clean_uses_repair_packet_without_compat_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    calls = 0
    packets: list[RepairPacket] = []

    def fake_checkout(**_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _blocking_payload("git_checkout_dirty", "checkout blocked by dirty tree")
        return {"check": "git_checkout_clean", "passed": True, "issues": [], "error": None}

    adapter_name = "_run_" + "preflight_repair_agent"

    async def forbidden_adapter(**_kwargs: Any) -> Any:
        raise AssertionError(f"checkout repair must not use {adapter_name}")

    async def fake_repair(packet: RepairPacket, **kwargs: Any) -> OracleRepairResult:
        assert callable(kwargs.get("commit_hook"))
        packets.append(packet)
        return OracleRepairResult(verdict="pass", summary="fixed", packet_path=str(packet.packet_path))

    monkeypatch.setattr(v5_runner, "_checkout_v5_branch_clean", fake_checkout)
    monkeypatch.setattr(v5_runner, adapter_name, forbidden_adapter, raising=False)
    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", fake_repair)

    payload = await v5_runner._checkout_v5_branch_clean_with_repair(
        project_dir=tmp_path,
        branch="main",
        context="pre_integration",
        session_dir=tmp_path / "session",
        config={"max_turns_per_call": 1},
        integration_branch="main",
        task_id="v5-integrate",
    )

    assert payload["passed"] is True
    assert len(packets) == 1
    packet = packets[0]
    assert packet.repair_unit["repair_phase"] == "checkout_clean"
    assert packet.repair_unit["task_id"] == "v5-integrate"
    assert packet.integration_context["checkout"]["branch"] == "main"
    assert packet.acceptance_oracle["verify_scope"] == "subtree"


@pytest.mark.asyncio
async def test_integration_smoke_uses_typed_oracle_packet_without_compat_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    calls = 0
    packets: list[RepairPacket] = []

    def fake_smoke(**_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _blocking_payload("ports_not_listening", "typed ports failed")
        return _passing_payload()

    adapter_name = "_run_" + "preflight_repair_agent"

    async def forbidden_adapter(**_kwargs: Any) -> Any:
        raise AssertionError(f"integration smoke must not use {adapter_name}")

    async def fake_repair(packet: RepairPacket, **_kwargs: Any) -> OracleRepairResult:
        packets.append(packet)
        return OracleRepairResult(verdict="pass", summary="fixed", packet_path=str(packet.packet_path))

    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight", fake_smoke)
    monkeypatch.setattr(v5_runner, adapter_name, forbidden_adapter, raising=False)
    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", fake_repair)

    payload = await v5_runner._run_integration_smoke_preflight_with_repair(
        project_dir=tmp_path,
        worktree_path=tmp_path,
        task_id="v5-integrate",
        phase="pre_agent",
        session_dir=tmp_path / "session",
        config={"max_turns_per_call": 1},
        integration_branch="main",
    )

    assert payload["passed"] is True
    assert len(packets) == 1
    packet = packets[0]
    assert packet.repair_unit["repair_phase"] == "integration_smoke"
    assert packet.acceptance_oracle["verify_scope"] == "subtree"
    assert packet.latest_oracle_result["issues"][0]["kind"] == "ports_not_listening"
    assert "clean_deploy_ports_not_listening" not in json.dumps(packet.latest_oracle_result)


@pytest.mark.asyncio
async def test_integration_worktree_setup_uses_repair_packet_without_compat_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    calls = 0
    packets: list[RepairPacket] = []

    def fake_setup(**_kwargs: Any) -> tuple[Path | None, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return None, "worktree setup failed"
        return repo, ""

    adapter_name = "_run_" + "preflight_repair_agent"

    async def forbidden_adapter(**_kwargs: Any) -> Any:
        raise AssertionError(f"worktree setup must not use {adapter_name}")

    async def fake_repair(packet: RepairPacket, **kwargs: Any) -> OracleRepairResult:
        assert callable(kwargs.get("commit_hook"))
        packets.append(packet)
        return OracleRepairResult(verdict="pass", summary="fixed", packet_path=str(packet.packet_path))

    monkeypatch.setattr(v5_runner, "_setup_integration_worktree_once", fake_setup)
    monkeypatch.setattr(v5_runner, adapter_name, forbidden_adapter, raising=False)
    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", fake_repair)

    prepared, payload = await v5_runner._prepare_integration_worktree_with_repair(
        project_dir=repo,
        task_id="v5-integrate",
        integration_branch="main",
        integration_session_dir=session_dir,
        config={"max_turns_per_call": 1},
    )

    assert prepared == repo
    assert payload["passed"] is True
    assert len(packets) == 1
    packet = packets[0]
    assert packet.repair_unit["repair_phase"] == "integration_worktree_setup"
    assert packet.integration_context["integration_branch"] == "main"
    assert packet.acceptance_oracle["verify_scope"] == "subtree"


def test_layer2_prompt_repairs_acceptance_cluster_and_allows_contract_scope(
    tmp_path: Path,
) -> None:
    spec = Spec(
        intent="Build issue tracker",
        groups=[
            Group(
                id="issues",
                name="Issues",
                feature_ids=["feat-a", "feat-b"],
                owned_paths=["frontend/src/issues/**"],
            )
        ],
        features=[
            Feature(id="feat-a", name="Create issue", group_id="issues"),
            Feature(id="feat-b", name="Filter issue", group_id="issues"),
        ],
    )
    rendered = _build_agent_prompt(
        BuildAgentInput(
            spec=spec,
            group=spec.groups[0],
            project_dir=tmp_path,
            worktree=tmp_path,
            branch="",
            attempt=2,
            feature_id="feat-a",
            last_failure_narrative="Create issue fails; filter issue is also blocked.",
        )
    )

    assert "FIX ONLY THE FAILING FEATURE" not in rendered
    assert "Repair the failing acceptance cluster" in rendered
    assert "feat-a" in rendered
    assert "Create issue fails" in rendered
    assert "shared-contract edits" in rendered


def test_failed_check_narrative_keeps_full_artifact_paths_authoritative(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    raw_log = log_dir / "000-BrowserJourney.log"
    raw_log.write_text("error: scary excerpt must stay advisory\n", encoding="utf-8")
    artifact = tmp_path / "otto_artifacts" / "browser" / "flow" / "full-shot.png"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"png")
    narrative = _failed_check_repair_narrative(
        1,
        [
            (
                BrowserJourney(command=("python", "journey.py")),
                Evidence(
                    passed=False,
                    started_at="2026-05-15T00:00:00Z",
                    duration_s=1.0,
                    detail="browser journey failed",
                    artifacts=[artifact],
                    raw={},
                ),
            )
        ],
        log_dir,
    )

    assert str(artifact) in narrative
    assert str(raw_log) in narrative
    assert "advisory" in narrative.lower()
    assert "authoritative" in narrative.lower()


def test_failed_artifact_paths_are_written_to_build_context_packet(tmp_path: Path) -> None:
    artifact = tmp_path / "logs" / "000-BrowserJourney.log"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("full log\n", encoding="utf-8")
    spec = Spec(
        intent="build tracker",
        groups=[Group(id="issues", name="Issues")],
        features=[Feature(id="feat-a", name="Create issue", group_id="issues")],
    )
    packet_path = tmp_path / "context-packet.json"

    _write_build_context_packet(
        BuildAgentInput(
            spec=spec,
            group=spec.groups[0],
            project_dir=tmp_path,
            worktree=tmp_path,
            branch="",
            attempt=2,
            last_failure_narrative="advisory excerpt: error words are not verdicts",
            failure_artifact_paths=(artifact,),
        ),
        packet_path,
    )

    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["failure_artifact_paths"] == [str(artifact)]
