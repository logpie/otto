from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from otto.v5_clean_verify import (
    CleanOracleIssue,
    CleanOracleResult,
    CleanOracleStepResult,
    build_clean_verify_oracle_command,
    verify_from_clean_oracle,
)
from otto.v5_preflight_repair import (
    RepairBudget,
    RepairPacket,
    append_repair_packet_oracle_event,
    oracle_progress_reproducible,
    run_oracle_repair_agent,
)
from otto.agent import AgentCallError
from otto import v5_runner


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
    (repo / ".gitignore").write_text("otto_logs/\n.venv/\n", encoding="utf-8")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")


def _oracle_result(
    *,
    passed: bool,
    kind: str = "clean_deploy_start_failed",
    message: str = "start failed",
    ports: list[int] | None = None,
) -> CleanOracleResult:
    issue = CleanOracleIssue(
        kind=kind,
        severity="block",
        message=message,
        step_id="start",
        ports=ports or [],
        command_identity="bash start.sh",
        return_code=1,
    )
    steps = [
        CleanOracleStepResult(
            id="start",
            status="passed" if passed else "failed",
            return_code=0 if passed else 1,
            command_identity="bash start.sh",
            command=["bash", "start.sh"],
            cwd=".",
            env={"PATH": "/usr/bin"},
        )
    ]
    return CleanOracleResult.from_parts(
        passed=passed,
        scope="subtree",
        issues=[] if passed else [issue],
        steps=steps,
        artifact_path_refs=[],
        command=["python", "-m", "otto.cli", "clean-verify"],
        env={"PATH": "/usr/bin"},
        project_dir=Path("."),
        temp_dir=Path("/tmp/otto-clean-random"),
    )


def _packet(
    tmp_path: Path,
    repo: Path,
    *,
    unit_id: str = "unit",
    agent_session_id: str = "",
    budget: RepairBudget | None = None,
    allowed_paths: tuple[str, ...] = (),
    scope_policy: str = "unrestricted",
) -> RepairPacket:
    session_dir = tmp_path / "session"
    return RepairPacket(
        repair_unit={
            "id": unit_id,
            "worktree": str(repo),
            "branch": "main",
            "task_id": unit_id,
            "phase": "preflight",
            "allowed_paths": list(allowed_paths),
            "scope_policy": scope_policy,
        },
        acceptance_oracle={
            "verify_scope": "subtree",
            "command": [sys.executable, "-m", "otto.cli", "clean-verify"],
            "env": {},
            "timeout_s": 30,
        },
        latest_oracle_result=_oracle_result(passed=False).to_jsonable(),
        product_contract={},
        integration_context={},
        attempt_history=[],
        current_state={},
        budget=budget or RepairBudget(agent_turns=1, oracle_invocations=4),
        packet_dir=session_dir / "repair" / unit_id,
        agent_session_id=agent_session_id,
    )


def test_clean_oracle_runs_independent_steps_and_skips_dependents(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "a").mkdir(parents=True)
    (project / "b").mkdir()
    (project / "a" / "pyproject.toml").write_text("[project]\nname='a'\nversion='0'\n", encoding="utf-8")
    (project / "a" / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    (project / "b" / "pyproject.toml").write_text("[project]\nname='b'\nversion='0'\n", encoding="utf-8")
    (project / "b" / "good.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "start.sh").write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")
    (project / "start.sh").chmod(0o755)

    result = verify_from_clean_oracle(project, scope="subtree", timeout_s=5, port_wait_s=1)

    assert result.passed is False
    step_status = {step.id: step.status for step in result.steps}
    assert "py_compile:a" in step_status
    assert step_status["py_compile:a"] == "failed"
    assert step_status["py_compile:b"] == "passed"
    assert step_status["start"] == "skipped_due_to:py_compile:a"
    assert [issue.kind for issue in result.issues] == ["py_compile_failed"]


def test_clean_oracle_digest_ignores_temp_roots_and_timestamps(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n", encoding="utf-8")
    (project / "ok.py").write_text("print('ok')\n", encoding="utf-8")

    first = verify_from_clean_oracle(project, scope="scaffold", timeout_s=5)
    second = verify_from_clean_oracle(project, scope="scaffold", timeout_s=5)

    assert first.passed is True
    assert second.passed is True
    assert first.digest == second.digest
    digest = first.digest
    first._written_at = "2099-01-01T00:00:00Z"
    assert first.digest == digest


def test_clean_verify_cli_appends_packet_oracle_event(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    packet_path = tmp_path / "otto_logs" / "session" / "repair" / "unit" / "repair_packet.json"
    packet_path.parent.mkdir(parents=True)

    command = build_clean_verify_oracle_command(
        worktree_path=project,
        verify_scope="scaffold",
        repair_packet_path=packet_path,
    )
    proc = subprocess.run(
        command.command,
        cwd=project,
        env={**command.env, **dict(PATH=command.env.get("PATH", ""))},
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["passed"] is True
    events = [
        json.loads(line)
        for line in (packet_path.parent / "repair_packet.events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[0]["seq"] == 1
    assert events[0]["event"]["type"] == "oracle_run"
    assert events[0]["digest"] == payload["digest"]


def test_integration_smoke_exception_is_typed_blocking_oracle_infra(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("browser runner unavailable")

    monkeypatch.setattr(v5_runner, "verify_from_clean_oracle", boom)

    payload = v5_runner._run_integration_smoke_preflight(
        worktree_path=tmp_path,
        task_id="task",
        phase="pre_agent",
    )

    assert payload["passed"] is False
    assert payload["issues"][0]["kind"] == "oracle_infra_error"
    assert payload["issues"][0]["severity"] == "block"
    assert v5_runner._integration_smoke_blocks(payload) is True


@pytest.mark.asyncio
async def test_repair_packet_replay_resumes_same_agent_session(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    packet = _packet(tmp_path, repo, unit_id="unit-replay", agent_session_id="sess-1")
    packet.persist()
    packet.append_event("oracle_run", digest="preexisting", payload={"source": "agent"})
    resumes: list[str | None] = []

    async def fake_agent(prompt: str, options: Any, **kwargs: Any) -> tuple[str, float, str, dict[str, Any]]:
        del prompt, kwargs
        resumes.append(options.resume)
        return "still working", 0.1, "sess-1", {}

    async def fake_oracle(_packet: RepairPacket) -> CleanOracleResult:
        return _oracle_result(passed=False, kind="ports_not_listening", ports=[5173])

    result = await run_oracle_repair_agent(
        packet,
        config={"max_turns_per_call": 2},
        agent_runner=fake_agent,
        oracle_runner=fake_oracle,
    )

    assert resumes == ["sess-1"]
    assert result.agent_session_id == "sess-1"
    events = packet.events()
    assert events[0]["digest"] == "preexisting"
    assert all("seq" in event and "digest" in event for event in events)


@pytest.mark.asyncio
async def test_repair_budget_exits_with_structured_escalation_no_hidden_retries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    packet = _packet(
        tmp_path,
        repo,
        unit_id="unit-budget",
        budget=RepairBudget(agent_turns=2, oracle_invocations=6, closeout_agent_turns=0),
    )
    agent_calls = 0
    oracle_calls = 0

    async def fake_agent(prompt: str, options: Any, **kwargs: Any) -> tuple[str, float, str, dict[str, Any]]:
        nonlocal agent_calls
        del prompt, options, kwargs
        agent_calls += 1
        (repo / f"churn-{agent_calls}.txt").write_text(str(agent_calls), encoding="utf-8")
        return "churned", 0.01, "sess-budget", {}

    async def fake_oracle(_packet: RepairPacket) -> CleanOracleResult:
        nonlocal oracle_calls
        oracle_calls += 1
        if oracle_calls % 2:
            return _oracle_result(passed=False, kind="ports_not_listening", ports=[3000])
        return _oracle_result(passed=False, kind="start_failed")

    result = await run_oracle_repair_agent(
        packet,
        config={"max_turns_per_call": 1},
        agent_runner=fake_agent,
        oracle_runner=fake_oracle,
    )

    assert result.verdict == "merge_blocked"
    assert result.escalation is not None
    assert result.escalation["reason"] == "budget_exhausted"
    assert agent_calls == 2
    assert result.agent_turns_used == 2


@pytest.mark.asyncio
async def test_composite_gate_blocks_smoke_pass_with_dirty_markers_and_scope_violation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "allowed.txt").write_text("ok\n", encoding="utf-8")
    (repo / "outside.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    packet = _packet(
        tmp_path,
        repo,
        unit_id="unit-composite",
        budget=RepairBudget(agent_turns=1, oracle_invocations=3),
        allowed_paths=("allowed.txt",),
        scope_policy="allowed_paths",
    )
    packet.capture_scope_baseline()

    async def fake_agent(prompt: str, options: Any, **kwargs: Any) -> tuple[str, float, str, dict[str, Any]]:
        del prompt, options, kwargs
        (repo / "allowed.txt").write_text("<<<<<<< ours\nok\n=======\nother\n>>>>>>> theirs\n", encoding="utf-8")
        (repo / "outside.txt").write_text("outside change\n", encoding="utf-8")
        return "fixed smoke only", 0.01, "sess-composite", {}

    async def fake_oracle(_packet: RepairPacket) -> CleanOracleResult:
        return _oracle_result(passed=True)

    result = await run_oracle_repair_agent(
        packet,
        config={"max_turns_per_call": 1},
        agent_runner=fake_agent,
        oracle_runner=fake_oracle,
    )

    assert result.verdict == "merge_blocked"
    assert result.composite_gate is not None
    assert result.composite_gate["oracle_passed"] is True
    assert result.composite_gate["clean_worktree"] is False
    assert result.composite_gate["conflict_markers"] is False
    assert result.composite_gate["scope_ok"] is False


@pytest.mark.asyncio
async def test_composite_gate_blocks_scope_and_conflict_before_commit_hook_commits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "allowed.txt").write_text("ok\n", encoding="utf-8")
    (repo / "outside.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    pre_repair_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    packet = _packet(
        tmp_path,
        repo,
        unit_id="unit-precommit-gate",
        budget=RepairBudget(agent_turns=1, oracle_invocations=1),
        allowed_paths=("allowed.txt",),
        scope_policy="allowed_paths",
    )
    packet.capture_scope_baseline()

    async def fake_agent(prompt: str, options: Any, **kwargs: Any) -> tuple[str, float, str, dict[str, Any]]:
        del prompt, options, kwargs
        (repo / "allowed.txt").write_text(
            "<<<<<<< ours\nok\n=======\nother\n>>>>>>> theirs\n",
            encoding="utf-8",
        )
        (repo / "outside.txt").write_text("outside change\n", encoding="utf-8")
        return "fixed smoke only", 0.01, "sess-precommit", {}

    async def fake_oracle(_packet: RepairPacket) -> CleanOracleResult:
        return _oracle_result(passed=True)

    async def commit_hook(_packet: RepairPacket, _oracle_result: CleanOracleResult) -> tuple[bool, str]:
        _git(repo, "add", "-A")
        proc = _git(repo, "commit", "-q", "-m", "bad repair")
        return proc.returncode == 0, proc.stderr or proc.stdout

    result = await run_oracle_repair_agent(
        packet,
        config={"max_turns_per_call": 1},
        agent_runner=fake_agent,
        oracle_runner=fake_oracle,
        commit_hook=commit_hook,
    )

    assert result.verdict == "merge_blocked"
    assert result.composite_gate is not None
    assert result.composite_gate["scope_ok"] is False
    assert result.composite_gate["conflict_markers"] is False
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == pre_repair_head


@pytest.mark.asyncio
async def test_post_commit_gate_allows_committed_change_matching_owned_glob(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    packet = _packet(
        tmp_path,
        repo,
        unit_id="unit-postcommit-glob",
        budget=RepairBudget(agent_turns=0, oracle_invocations=0),
        allowed_paths=("src/features/foo/**",),
        scope_policy="allowed_paths",
    )
    packet.latest_oracle_result = _oracle_result(passed=True).to_jsonable()
    packet.capture_scope_baseline()

    async def forbidden_agent(prompt: str, options: Any, **kwargs: Any) -> tuple[str, float, str, dict[str, Any]]:
        del prompt, options, kwargs
        raise AssertionError("passing oracle should not dispatch a repair agent")

    async def commit_hook(_packet: RepairPacket, _oracle_result: CleanOracleResult) -> tuple[bool, str]:
        changed_path = repo / "src" / "features" / "foo" / "panel.tsx"
        changed_path.parent.mkdir(parents=True, exist_ok=True)
        changed_path.write_text("export const panel = 'ok';\n", encoding="utf-8")
        _git(repo, "add", "-A")
        proc = _git(repo, "commit", "-q", "-m", "repair foo panel")
        return proc.returncode == 0, proc.stderr or proc.stdout

    result = await run_oracle_repair_agent(
        packet,
        config={"max_turns_per_call": 1},
        agent_runner=forbidden_agent,
        oracle_runner=lambda _packet: _oracle_result(passed=True),
        commit_hook=commit_hook,
    )

    assert result.verdict == "pass"
    assert result.composite_gate is not None
    assert result.composite_gate["scope_ok"] is True
    assert result.composite_gate["changed_paths"] == ["src/features/foo/panel.tsx"]


@pytest.mark.asyncio
async def test_post_commit_gate_blocks_committed_change_outside_owned_glob(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    packet = _packet(
        tmp_path,
        repo,
        unit_id="unit-postcommit-outside-glob",
        budget=RepairBudget(agent_turns=0, oracle_invocations=0),
        allowed_paths=("src/features/foo/**",),
        scope_policy="allowed_paths",
    )
    packet.latest_oracle_result = _oracle_result(passed=True).to_jsonable()
    packet.capture_scope_baseline()

    async def forbidden_agent(prompt: str, options: Any, **kwargs: Any) -> tuple[str, float, str, dict[str, Any]]:
        del prompt, options, kwargs
        raise AssertionError("passing oracle should not dispatch a repair agent")

    async def commit_hook(_packet: RepairPacket, _oracle_result: CleanOracleResult) -> tuple[bool, str]:
        changed_path = repo / "src" / "other" / "x.tsx"
        changed_path.parent.mkdir(parents=True, exist_ok=True)
        changed_path.write_text("export const x = 'blocked';\n", encoding="utf-8")
        _git(repo, "add", "-A")
        proc = _git(repo, "commit", "-q", "-m", "repair outside scope")
        return proc.returncode == 0, proc.stderr or proc.stdout

    result = await run_oracle_repair_agent(
        packet,
        config={"max_turns_per_call": 1},
        agent_runner=forbidden_agent,
        oracle_runner=lambda _packet: _oracle_result(passed=True),
        commit_hook=commit_hook,
    )

    assert result.verdict == "merge_blocked"
    assert result.composite_gate is not None
    assert result.composite_gate["scope_ok"] is False
    assert result.composite_gate["scope_violations"] == ["src/other/x.tsx"]


@pytest.mark.asyncio
async def test_merge_conflict_scope_carves_in_conflicted_paths_but_blocks_unrelated(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "backend").mkdir()
    (repo / "backend" / "main.py").write_text("value = 'base'\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed shared backend")

    async def forbidden_agent(prompt: str, options: Any, **kwargs: Any) -> tuple[str, float, str, dict[str, Any]]:
        del prompt, options, kwargs
        raise AssertionError("passing clean-deploy should not dispatch an agent")

    allowed_packet = _packet(
        tmp_path,
        repo,
        unit_id="unit-conflicted-shared-allowed",
        budget=RepairBudget(agent_turns=0, oracle_invocations=0),
        allowed_paths=(),
        scope_policy="allowed_paths",
    )
    allowed_packet.repair_unit["phase"] = "merge"
    allowed_packet.repair_unit["repair_phase"] = "merge"
    allowed_packet.repair_unit["conflicted_paths"] = ["backend/main.py"]
    allowed_packet.integration_context["conflict_packet"] = {
        "unmerged_paths": ["backend/main.py"],
    }
    allowed_packet.latest_oracle_result = _oracle_result(passed=True).to_jsonable()

    async def commit_conflicted_path(
        _packet: RepairPacket,
        _oracle_result: CleanOracleResult,
    ) -> tuple[bool, str]:
        (repo / "backend" / "main.py").write_text("value = 'resolved'\n", encoding="utf-8")
        _git(repo, "add", "-A")
        proc = _git(repo, "commit", "-q", "-m", "resolve conflicted shared file")
        return proc.returncode == 0, proc.stderr or proc.stdout

    allowed = await run_oracle_repair_agent(
        allowed_packet,
        config={"max_turns_per_call": 1},
        agent_runner=forbidden_agent,
        oracle_runner=lambda _packet: _oracle_result(passed=True),
        commit_hook=commit_conflicted_path,
    )

    assert allowed.verdict == "pass"
    assert allowed.composite_gate is not None
    assert allowed.composite_gate["scope_ok"] is True
    assert allowed.composite_gate["scope_violations"] == []

    unrelated_packet = _packet(
        tmp_path,
        repo,
        unit_id="unit-conflicted-shared-unrelated-blocked",
        budget=RepairBudget(agent_turns=0, oracle_invocations=0),
        allowed_paths=(),
        scope_policy="allowed_paths",
    )
    unrelated_packet.repair_unit["phase"] = "merge"
    unrelated_packet.repair_unit["repair_phase"] = "merge"
    unrelated_packet.repair_unit["conflicted_paths"] = ["backend/main.py"]
    unrelated_packet.integration_context["conflict_packet"] = {
        "unmerged_paths": ["backend/main.py"],
    }
    unrelated_packet.latest_oracle_result = _oracle_result(passed=True).to_jsonable()

    async def commit_unrelated_path(
        _packet: RepairPacket,
        _oracle_result: CleanOracleResult,
    ) -> tuple[bool, str]:
        (repo / "backend" / "main.py").write_text("value = 'resolved again'\n", encoding="utf-8")
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "stray.md").write_text("unrelated\n", encoding="utf-8")
        _git(repo, "add", "-A")
        proc = _git(repo, "commit", "-q", "-m", "resolve plus unrelated file")
        return proc.returncode == 0, proc.stderr or proc.stdout

    blocked = await run_oracle_repair_agent(
        unrelated_packet,
        config={"max_turns_per_call": 1},
        agent_runner=forbidden_agent,
        oracle_runner=lambda _packet: _oracle_result(passed=True),
        commit_hook=commit_unrelated_path,
    )

    assert blocked.verdict == "merge_blocked"
    assert blocked.composite_gate is not None
    assert blocked.composite_gate["scope_ok"] is False
    assert blocked.composite_gate["scope_violations"] == ["docs/stray.md"]
    assert blocked.composite_gate["reasons"]
    assert blocked.escalation is not None
    assert blocked.escalation["composite_gate"]["reasons"]


def test_clean_verify_oracle_serialization_omits_secret_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_value = "sk-test-redaction-value-1234567890"
    monkeypatch.setenv("OPENAI_API_KEY", secret_value)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secretredactionvalue1234567890")
    monkeypatch.setenv("OTTO_CLEAN_VERIFY_WORKTREE", "/tmp/ambient-should-not-win")
    project = tmp_path / "project"
    project.mkdir()
    packet_path = tmp_path / "session" / "repair" / "unit-secret" / "repair_packet.json"
    command = build_clean_verify_oracle_command(
        worktree_path=project,
        verify_scope="scaffold",
        repair_packet_path=packet_path,
    )
    result = verify_from_clean_oracle(project, scope="scaffold", timeout_s=5)
    packet = _packet(tmp_path, project, unit_id="unit-secret")
    packet.acceptance_oracle["env"] = command.env
    packet.latest_oracle_result = result.to_jsonable()
    packet.persist()

    serialized = "\n".join([
        json.dumps(command.env, sort_keys=True),
        json.dumps(result.to_jsonable(), sort_keys=True),
        packet.packet_path.read_text(encoding="utf-8"),
    ])
    assert "OPENAI_API_KEY" not in serialized
    assert "GITHUB_TOKEN" not in serialized
    assert secret_value not in serialized
    assert "ghp_secretredactionvalue1234567890" not in serialized
    assert command.env["OTTO_CLEAN_VERIFY_WORKTREE"] == str(project.resolve(strict=False))


@pytest.mark.asyncio
async def test_agent_call_error_returns_structured_repair_escalation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    packet = _packet(
        tmp_path,
        repo,
        unit_id="unit-agent-error",
        budget=RepairBudget(agent_turns=2, oracle_invocations=1),
    )

    async def failing_agent(prompt: str, options: Any, **kwargs: Any) -> tuple[str, float, str, dict[str, Any]]:
        del prompt, options, kwargs
        raise AgentCallError(
            "provider crashed",
            session_id="sess-crashed",
            total_cost_usd=0.27,
            last_events=[{"type": "result", "summary": "partial"}],
        )

    result = await run_oracle_repair_agent(
        packet,
        config={"max_turns_per_call": 1},
        agent_runner=failing_agent,
        oracle_runner=lambda _packet: _oracle_result(passed=False),
    )

    assert result.verdict == "merge_blocked"
    assert result.agent_session_id == "sess-crashed"
    assert result.cost_usd == pytest.approx(0.27)
    assert result.escalation is not None
    assert result.escalation["reason"] == "agent_call_failed"
    events = packet.events()
    assert any(event["event"]["type"] == "agent_error" for event in events)


@pytest.mark.asyncio
async def test_repair_budget_replays_prior_usage_and_preserves_closeout_reserve(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    packet = _packet(
        tmp_path,
        repo,
        unit_id="unit-budget-replay",
        budget=RepairBudget(
            agent_turns=2,
            oracle_invocations=2,
            cost_usd=0.50,
            closeout_agent_turns=1,
        ),
    )
    packet.persist()
    packet.append_event("agent_turn", digest="old-agent", payload={"turn": 1, "cost_usd": 0.10})
    packet.append_event("oracle_run", digest="old-oracle", payload={"source": "controller", "passed": False})
    phases: list[str] = []

    async def fake_agent(prompt: str, options: Any, **kwargs: Any) -> tuple[str, float, str, dict[str, Any]]:
        del prompt, options
        phases.append(str(kwargs.get("phase_name") or ""))
        assert kwargs.get("phase_name") == "REPAIR_CLOSEOUT"
        return "closeout: still blocked", 0.03, "sess-closeout", {}

    async def fake_oracle(_packet: RepairPacket) -> CleanOracleResult:
        raise AssertionError("budget replay should block before spending another oracle")

    result = await run_oracle_repair_agent(
        packet,
        config={"max_turns_per_call": 1},
        agent_runner=fake_agent,
        oracle_runner=fake_oracle,
    )

    assert phases == ["REPAIR_CLOSEOUT"]
    assert result.verdict == "merge_blocked"
    assert result.agent_turns_used == 2
    assert result.oracle_invocations == 1
    assert result.cost_usd == pytest.approx(0.13)
    assert result.escalation is not None
    assert result.escalation["closeout_source"] == "agent_reserve"


@pytest.mark.asyncio
async def test_cost_exhaustion_writes_packet_escalation_without_closeout_agent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    packet = _packet(
        tmp_path,
        repo,
        unit_id="unit-cost-exhausted-no-closeout",
        budget=RepairBudget(
            agent_turns=2,
            oracle_invocations=2,
            cost_usd=0.10,
            closeout_agent_turns=1,
        ),
    )
    packet.persist()
    packet.append_event("agent_turn", digest="old-agent", payload={"turn": 1, "cost_usd": 0.10})

    async def forbidden_agent(prompt: str, options: Any, **kwargs: Any) -> tuple[str, float, str, dict[str, Any]]:
        del prompt, options, kwargs
        raise AssertionError("cost exhaustion must not spend a closeout agent turn")

    async def forbidden_oracle(_packet: RepairPacket) -> CleanOracleResult:
        raise AssertionError("cost exhaustion should block before spending another oracle")

    result = await run_oracle_repair_agent(
        packet,
        config={"max_turns_per_call": 1},
        agent_runner=forbidden_agent,
        oracle_runner=forbidden_oracle,
    )

    assert result.verdict == "merge_blocked"
    assert result.agent_turns_used == 1
    assert result.cost_usd == pytest.approx(0.10)
    assert result.escalation is not None
    assert result.escalation["reason"] == "cost_exhausted"
    assert result.escalation["closeout_source"] == "packet"
    assert not any(event["event"]["type"] == "closeout_agent_turn" for event in packet.events())


@pytest.mark.asyncio
async def test_turn_budget_exhaustion_can_use_single_closeout_agent_when_cost_remains(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    packet = _packet(
        tmp_path,
        repo,
        unit_id="unit-turn-exhausted-closeout",
        budget=RepairBudget(
            agent_turns=1,
            oracle_invocations=2,
            cost_usd=1.00,
            closeout_agent_turns=1,
        ),
    )
    phases: list[str] = []

    async def fake_agent(prompt: str, options: Any, **kwargs: Any) -> tuple[str, float, str, dict[str, Any]]:
        del prompt, options
        phases.append(str(kwargs.get("phase_name") or ""))
        return "closeout: turn budget exhausted", 0.05, "sess-turn-closeout", {}

    async def forbidden_oracle(_packet: RepairPacket) -> CleanOracleResult:
        raise AssertionError("turn exhaustion should close out before spending another oracle")

    result = await run_oracle_repair_agent(
        packet,
        config={"max_turns_per_call": 1},
        agent_runner=fake_agent,
        oracle_runner=forbidden_oracle,
    )

    assert phases == ["REPAIR_CLOSEOUT"]
    assert result.verdict == "merge_blocked"
    assert result.agent_turns_used == 1
    assert result.cost_usd == pytest.approx(0.05)
    assert result.escalation is not None
    assert result.escalation["reason"] == "budget_exhausted"
    assert result.escalation["closeout_source"] == "agent_reserve"


@pytest.mark.asyncio
async def test_agent_appended_passing_oracle_is_evaluated_before_controller_budget_escalation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    packet = _packet(
        tmp_path,
        repo,
        unit_id="unit-agent-oracle",
        budget=RepairBudget(agent_turns=1, oracle_invocations=0),
    )
    packet.persist()

    async def fake_agent(prompt: str, options: Any, **kwargs: Any) -> tuple[str, float, str, dict[str, Any]]:
        del prompt, options, kwargs
        append_repair_packet_oracle_event(packet.packet_path, _oracle_result(passed=True), source="agent")
        return "oracle passed", 0.01, "sess-oracle", {}

    async def forbidden_controller_oracle(_packet: RepairPacket) -> CleanOracleResult:
        raise AssertionError("controller oracle budget is exhausted; agent oracle should be used")

    result = await run_oracle_repair_agent(
        packet,
        config={"max_turns_per_call": 1},
        agent_runner=fake_agent,
        oracle_runner=forbidden_controller_oracle,
    )

    assert result.verdict == "pass"
    assert result.oracle_invocations == 1
    assert result.agent_session_id == "sess-oracle"


def test_flaky_oracle_alternating_domains_is_not_progress_without_reproducible_digest() -> None:
    previous = _oracle_result(passed=False, kind="ports_not_listening", ports=[5173])
    improved = _oracle_result(passed=False, kind="start_failed")
    reproduced = _oracle_result(passed=False, kind="ports_not_listening", ports=[5173])

    assert oracle_progress_reproducible(
        previous=previous,
        improved=improved,
        reproduced=reproduced,
        has_owned_path_diff=True,
    ) is False


@pytest.mark.asyncio
async def test_concurrent_repair_packets_are_isolated_per_unit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    packets = [
        _packet(tmp_path, repo, unit_id="parent-subtree", budget=RepairBudget(agent_turns=1, oracle_invocations=3)),
        _packet(tmp_path, repo, unit_id="child-subtree", budget=RepairBudget(agent_turns=1, oracle_invocations=3)),
    ]

    async def fake_agent(prompt: str, options: Any, **kwargs: Any) -> tuple[str, float, str, dict[str, Any]]:
        del prompt, options, kwargs
        await asyncio.sleep(0.01)
        return "no-op", 0.0, "sess-shared", {}

    async def fake_oracle(packet: RepairPacket) -> CleanOracleResult:
        return _oracle_result(
            passed=False,
            kind="ports_not_listening",
            ports=[5173, 8000],
            message=f"{packet.repair_unit['id']} ports not listening",
        )

    await asyncio.gather(*[
        run_oracle_repair_agent(
            packet,
            config={"max_turns_per_call": 1},
            agent_runner=fake_agent,
            oracle_runner=fake_oracle,
        )
        for packet in packets
    ])

    for packet in packets:
        events = packet.events()
        assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
        assert all(event["event"].get("repair_unit_id") == packet.repair_unit["id"] for event in events)
        assert packet.packet_path.exists()
