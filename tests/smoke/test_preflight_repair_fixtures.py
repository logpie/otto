"""Smoke fixtures for the packet-native preflight repair path."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from otto import v5_clean_verify, v5_runner
from otto.lead import (
    _read_agent_verdict,
    _read_agent_verdict_with_rewrite,
    _verdict_failure_reason,
)
from otto.safe_slug import safe_slug


def _merge_raw_log_dir(session_dir: Path, group_id: str) -> Path:
    """Inline of the former otto.merge_queue helper — runner-owned raw merge log dir."""
    return session_dir / "merge" / safe_slug(group_id, max_len=48)

from otto.v5_clean_verify import CleanOracleIssue, CleanOracleResult, CleanOracleStepResult
from otto.v5_preflight_repair import OracleRepairResult, RepairBudget, RepairPacket, run_oracle_repair_agent


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
    (repo / ".gitignore").write_text("otto_logs/\n.otto/\n", encoding="utf-8")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init", "--no-verify")


def _oracle_result(repo: Path, *, passed: bool, kind: str = "start_failed") -> CleanOracleResult:
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
        message=f"{kind} remained",
        step_id=step.id,
        command_identity=step.command_identity,
        return_code=step.return_code,
    )
    return CleanOracleResult.from_parts(
        passed=passed,
        scope="subtree",
        issues=[] if passed else [issue],
        steps=[step],
        artifact_path_refs=[],
        command=step.command,
        env=step.env,
        project_dir=repo,
        temp_dir=None,
    )


def _packet(tmp_path: Path, repo: Path, *, budget: RepairBudget | None = None) -> RepairPacket:
    return RepairPacket(
        repair_unit={
            "id": "smoke-unit",
            "worktree": str(repo),
            "branch": "main",
            "task_id": "smoke",
            "phase": "preflight",
            "repair_phase": "integration_smoke",
            "allowed_paths": [],
            "scope_policy": "unrestricted",
        },
        acceptance_oracle={
            "verify_scope": "subtree",
            "command": ["python", "-m", "otto.cli", "clean-verify"],
            "env": {},
            "timeout_s": 30,
        },
        latest_oracle_result=_oracle_result(repo, passed=False).to_jsonable(),
        product_contract={},
        integration_context={},
        attempt_history=[],
        current_state={},
        budget=budget or RepairBudget(agent_turns=1, oracle_invocations=2),
        packet_dir=tmp_path / "session" / "repair" / "smoke-unit",
    )


@pytest.mark.asyncio
async def test_packet_repair_budget_escalates_with_journal(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    packet = _packet(tmp_path, repo, budget=RepairBudget(agent_turns=1, oracle_invocations=2))

    async def fake_agent(*_args: Any, **_kwargs: Any) -> tuple[str, float, str, dict[str, Any]]:
        return "still failing", 0.01, "sess-smoke", {}

    async def fake_oracle(_packet: RepairPacket) -> CleanOracleResult:
        return _oracle_result(repo, passed=False, kind="ports_not_listening")

    result = await run_oracle_repair_agent(
        packet,
        config={"max_turns_per_call": 1},
        agent_runner=fake_agent,
        oracle_runner=fake_oracle,
    )

    assert result.verdict == "merge_blocked"
    assert result.escalation is not None
    assert result.escalation["reason"] == "budget_exhausted"
    events = packet.events()
    assert [event["seq"] for event in events] == [1, 2, 3]
    assert events[-1]["event"]["reason"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_startup_port_cleanup_routes_to_packet_repair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    cleanup_result_type = getattr(v5_clean_verify, "PortCleanupResult")
    calls = 0
    packets: list[RepairPacket] = []

    def fake_cleanup(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return cleanup_result_type(killed_ports=[18080], still_bound_ports=[18080])
        return cleanup_result_type()

    async def fake_repair(packet: RepairPacket, **_kwargs: Any) -> OracleRepairResult:
        packets.append(packet)
        return OracleRepairResult(verdict="pass", summary="fixed", packet_path=str(packet.packet_path))

    monkeypatch.setattr("otto.v5_clean_verify.cleanup_stale_declared_ports", fake_cleanup)
    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", fake_repair)

    payload = await v5_runner._run_startup_port_cleanup_with_repair(
        project_dir=repo,
        session_dir=tmp_path / "session",
        config={"max_turns_per_call": 1},
    )

    assert payload["passed"] is True
    assert calls == 2
    assert packets[0].repair_unit["repair_phase"] == "startup_port_cleanup"
    assert packets[0].current_state["scope_baseline"] is not None


def test_safe_slug_handles_270_char_curl_verification_label(tmp_path: Path) -> None:
    label = "curl verification: " + (
        "GET http://127.0.0.1:3000/api/health?include=status&require=json "
        "assert response contains service status and no HTML fallback "
    ) * 3
    assert len(label) > 270

    slug = safe_slug(label, max_len=48)

    assert len(slug) <= 48
    assert re.fullmatch(r"[a-z0-9][a-z0-9._-]*", slug)
    assert "/" not in slug
    assert slug != safe_slug(label + "different", max_len=48)

    log_dir = _merge_raw_log_dir(tmp_path / "session", label)
    assert log_dir.parent.name == "merge"
    assert log_dir.name == slug


def test_noncanonical_success_verdict_maps_to_pass(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "verdict.json").write_text(
        json.dumps({
            "status": "success",
            "tests": {"unit": {"passed": True}, "typecheck": {"status": "passed"}},
            "deliverables": ["frontend/src/App.tsx"],
            "summary": "tests passed",
        }),
        encoding="utf-8",
    )

    called, payload = _read_agent_verdict(session_dir)

    assert called is True
    assert payload is not None
    assert payload["verdict"] == "pass"
    assert payload["summary"] == "tests passed"
    assert payload["canonicalized_from"]["status"] == "success"
    assert json.loads((session_dir / "verdict.json").read_text(encoding="utf-8"))["verdict"] == "pass"


def test_verdict_parser_downgrades_bare_aliases_and_reports_bad_existing_file(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "verdict.json").write_text(
        json.dumps({"status": "success"}),
        encoding="utf-8",
    )

    called, payload = _read_agent_verdict(session_dir)

    assert called is True
    assert payload is not None
    assert payload["verdict"] == "unverified"

    (session_dir / "verdict.json").write_text(
        json.dumps({"verdict": "passed", "summary": "provider alias"}),
        encoding="utf-8",
    )

    called, payload = _read_agent_verdict(session_dir)

    assert called is True
    assert payload is not None
    assert payload["verdict"] == "unverified"

    (session_dir / "verdict.json").write_text("{ not json", encoding="utf-8")
    reason = _verdict_failure_reason(session_dir, integration=False)

    assert "Agent wrote verdict.json" in reason
    assert "json_decode_error" in reason
    assert "Agent did not write verdict.json" not in reason


@pytest.mark.asyncio
async def test_unmappable_verdict_triggers_one_canonical_rewrite_retry(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "verdict.json").write_text(
        json.dumps({"status": "done-ish", "tests": {"unit": "unclear"}}),
        encoding="utf-8",
    )
    attempts: list[str] = []

    async def rewrite(context: dict[str, Any]) -> None:
        attempts.append(context["original_text"])
        (session_dir / "verdict.json").write_text(
            json.dumps({"verdict": "partial", "summary": "rewritten canonically"}),
            encoding="utf-8",
        )

    called, payload, retried = await _read_agent_verdict_with_rewrite(
        session_dir,
        rewrite_once=rewrite,
    )

    assert called is True
    assert retried is True
    assert len(attempts) == 1
    assert payload is not None
    assert payload["verdict"] == "partial"
