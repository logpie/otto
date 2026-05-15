"""Deterministic fixtures for v6.5 preflight repair classes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from otto.lead import (
    _read_agent_verdict,
    _read_agent_verdict_with_rewrite,
    _verdict_failure_reason,
)
from otto.merge_queue import _merge_raw_log_dir
from otto.safe_slug import safe_slug
from otto import v5_clean_verify
from otto.v5_preflight_repair import (
    AgentRepairRequest,
    AgentRepairResult,
    PreflightRepairController,
)


pytestmark = pytest.mark.smoke


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


@pytest.mark.asyncio
async def test_port_busy_autofix_fires_and_continues(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    worktree = tmp_path / "repo"
    session_dir.mkdir()
    worktree.mkdir()
    calls: list[str] = []
    runs = 0

    def run_preflight() -> dict[str, Any]:
        nonlocal runs
        runs += 1
        if runs == 1:
            return _blocking_payload("clean_deploy_port_busy", "Declared ports [18080] already bound")
        return _passing_payload()

    def cleanup(_worktree: Path, issue: dict[str, Any]) -> dict[str, Any]:
        calls.append(issue["kind"])
        return {"killed_ports": [18080]}

    controller = PreflightRepairController(
        session_dir=session_dir,
        worktree_path=worktree,
        original_budget_usd=100.0,
        port_cleanup=cleanup,
    )

    result = await controller.repair_until_clean(run_preflight)

    assert result.terminal_state == "continued"
    assert calls == ["clean_deploy_port_busy"]
    assert runs == 2
    events = _log_events(session_dir)
    assert events[0]["event"] == "repair_attempt"
    assert events[0]["failure_kind"] == "port_busy"
    assert events[0]["action"] == "auto_fix"
    assert events[0]["outcome"] == "repaired"
    assert "_written_at" in events[0]


@pytest.mark.asyncio
async def test_port_busy_cleanup_noop_falls_back_to_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir = tmp_path / "session"
    worktree = tmp_path / "repo"
    session_dir.mkdir()
    worktree.mkdir()
    (worktree / "CHARTER.md").write_text("- app: 127.0.0.1:18080\n", encoding="utf-8")
    requests: list[AgentRepairRequest] = []
    killed: list[int] = []
    runs = 0

    monkeypatch.setattr(v5_clean_verify, "_pids_for_port", lambda _port: [4444])
    monkeypatch.setattr(v5_clean_verify, "_is_otto_owned_process", lambda *_args: False)
    monkeypatch.setattr(v5_clean_verify, "_terminate_pid", lambda pid: killed.append(pid))
    monkeypatch.setattr(v5_clean_verify, "_check_ports_free", lambda _ports: [18080])

    def run_preflight() -> dict[str, Any]:
        nonlocal runs
        runs += 1
        if requests:
            return _passing_payload()
        return _blocking_payload("clean_deploy_port_busy", "Declared ports [18080] already bound")

    async def repair(request: AgentRepairRequest) -> AgentRepairResult:
        requests.append(request)
        return AgentRepairResult(ok=True, cost_usd=0.4, summary="made start.sh pick a free port")

    controller = PreflightRepairController(
        session_dir=session_dir,
        worktree_path=worktree,
        original_budget_usd=100.0,
        agent_repair=repair,
    )

    result = await controller.repair_until_clean(run_preflight)

    assert result.terminal_state == "continued"
    assert killed == []
    assert runs == 2
    assert len(requests) == 1
    assert requests[0].failure_kind == "port_busy"
    assert "deterministic port cleanup" in requests[0].instruction.lower()
    assert "18080" in requests[0].instruction
    events = _log_events(session_dir)
    assert not any(
        event.get("action") == "auto_fix" and event.get("outcome") == "repaired"
        for event in events
    )
    assert any(
        event.get("action") == "auto_fix" and event.get("outcome") == "no_op_agent_fallback"
        for event in events
    )
    assert events[-1]["action"] == "agent"
    assert events[-1]["outcome"] == "repaired"


@pytest.mark.asyncio
async def test_filename_too_long_autofix_fires_and_continues(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    worktree = tmp_path / "repo"
    session_dir.mkdir()
    worktree.mkdir()
    calls: list[str] = []
    runs = 0
    label = "curl verification: " + "GET /api/health?include=everything " * 8

    def run_preflight() -> dict[str, Any]:
        nonlocal runs
        runs += 1
        if runs == 1:
            return _blocking_payload("filename_too_long", f"File name too long: {label}")
        return _passing_payload()

    def rename(_worktree: Path, issue: dict[str, Any]) -> dict[str, Any]:
        calls.append(issue["message"])
        return {"renamed": [{"from": label, "to": safe_slug(label)}]}

    controller = PreflightRepairController(
        session_dir=session_dir,
        worktree_path=worktree,
        original_budget_usd=100.0,
        filename_repair=rename,
    )

    result = await controller.repair_until_clean(run_preflight)

    assert result.terminal_state == "continued"
    assert len(calls) == 1
    assert _log_events(session_dir)[0]["failure_kind"] == "filename_too_long"


@pytest.mark.asyncio
async def test_non_executable_shell_script_autochmod_fires_and_continues(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    worktree = tmp_path / "repo"
    session_dir.mkdir()
    worktree.mkdir()
    script = worktree / "start.sh"
    script.write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")
    script.chmod(0o644)
    runs = 0

    def run_preflight() -> dict[str, Any]:
        nonlocal runs
        runs += 1
        if runs == 1:
            return _blocking_payload("clean_deploy_script_valid_failed", "start.sh is not executable")
        return _passing_payload()

    controller = PreflightRepairController(
        session_dir=session_dir,
        worktree_path=worktree,
        original_budget_usd=100.0,
    )

    result = await controller.repair_until_clean(run_preflight)

    assert result.terminal_state == "continued"
    assert script.stat().st_mode & 0o111
    assert _log_events(session_dir)[0]["failure_kind"] == "permission_chmod"


@pytest.mark.asyncio
async def test_non_autofix_failure_spawns_repair_agent_and_continues(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    worktree = tmp_path / "repo"
    session_dir.mkdir()
    worktree.mkdir()
    requests: list[AgentRepairRequest] = []
    runs = 0

    def run_preflight() -> dict[str, Any]:
        nonlocal runs
        runs += 1
        if runs == 1:
            return _blocking_payload(
                "scaffold_compile_failed",
                "frontend/src/App.tsx(12,7): error TS2304: Cannot find name 'Widget'.",
            )
        return _passing_payload()

    async def repair(request: AgentRepairRequest) -> AgentRepairResult:
        requests.append(request)
        return AgentRepairResult(ok=True, cost_usd=0.25, summary="fixed App.tsx")

    controller = PreflightRepairController(
        session_dir=session_dir,
        worktree_path=worktree,
        original_budget_usd=10.0,
        agent_repair=repair,
    )

    result = await controller.repair_until_clean(run_preflight)

    assert result.terminal_state == "continued"
    assert [request.failure_kind for request in requests] == ["scaffold_compile_failed"]
    assert requests[0].workspace_paths == ("frontend/src/App.tsx",)
    assert _log_events(session_dir)[0]["action"] == "agent"


@pytest.mark.asyncio
async def test_script_valid_failure_uses_agent_default_and_continues(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    worktree = tmp_path / "repo"
    session_dir.mkdir()
    worktree.mkdir()
    requests: list[AgentRepairRequest] = []
    runs = 0

    def run_preflight() -> dict[str, Any]:
        nonlocal runs
        runs += 1
        if runs == 1:
            return _blocking_payload("clean_deploy_script_valid_failed", "bash -n start.sh failed")
        return _passing_payload()

    async def repair(request: AgentRepairRequest) -> AgentRepairResult:
        requests.append(request)
        return AgentRepairResult(ok=True, cost_usd=0.1, summary="fixed start.sh")

    controller = PreflightRepairController(
        session_dir=session_dir,
        worktree_path=worktree,
        original_budget_usd=10.0,
        agent_repair=repair,
    )

    result = await controller.repair_until_clean(run_preflight)

    assert result.terminal_state == "continued"
    assert [request.failure_kind for request in requests] == ["clean_deploy_script_valid_failed"]
    assert requests[0].workspace_paths == ("start.sh",)


@pytest.mark.asyncio
async def test_repeated_issue_exits_by_budget_without_looping(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    worktree = tmp_path / "repo"
    session_dir.mkdir()
    worktree.mkdir()

    def run_preflight() -> dict[str, Any]:
        return _blocking_payload("clean_deploy_port_busy", "Declared ports [18080] already bound")

    controller = PreflightRepairController(
        session_dir=session_dir,
        worktree_path=worktree,
        original_budget_usd=100.0,
        port_cleanup=lambda *_args: {"killed_ports": [18080]},
    )

    result = await controller.repair_until_clean(run_preflight)

    assert result.terminal_state == "escalated"
    events = _log_events(session_dir)
    assert events[-1]["event"] == "repair_escalated"
    assert events[-1]["reason"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_progressing_preflight_repairs_do_not_hit_old_total_cap(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    worktree = tmp_path / "repo"
    session_dir.mkdir()
    worktree.mkdir()
    repairs: list[str] = []
    payloads = [
        _blocking_payload("clean_deploy_start_failed", "npm run build failed in frontend: TS2339"),
        _blocking_payload(
            "clean_deploy_ports_not_listening",
            "After clean-state deploy, ports [5173, 8000, 8001] did not bind within 102s. Listening: none.",
        ),
        _blocking_payload(
            "clean_deploy_port_busy",
            "Declared ports [8000, 8001] already bound (likely zombies from prior runs). Cannot run clean-deploy.",
        ),
        _blocking_payload(
            "clean_deploy_ports_not_listening",
            "After clean-state deploy, ports [5173] did not bind within 102s. Listening: [8000, 8001].",
        ),
        _passing_payload(),
    ]

    def run_preflight() -> dict[str, Any]:
        return payloads.pop(0)

    async def repair(request: AgentRepairRequest) -> AgentRepairResult:
        repairs.append(request.failure_kind)
        return AgentRepairResult(ok=True, summary=f"fixed {request.failure_kind}")

    controller = PreflightRepairController(
        session_dir=session_dir,
        worktree_path=worktree,
        agent_repair=repair,
        port_cleanup=lambda *_args: {"killed_ports": [8000], "bound_after": [], "repaired": True},
    )

    result = await controller.repair_until_clean(run_preflight)

    assert result.terminal_state == "continued"
    assert len(result.attempts) == 4
    assert repairs == [
        "clean_deploy_start_failed",
        "clean_deploy_ports_not_listening",
        "clean_deploy_ports_not_listening",
    ]
    events = _log_events(session_dir)
    assert not any(event.get("reason") == "total_attempt_cap" for event in events)

    no_progress_session = tmp_path / "no-progress-session"
    no_progress_session.mkdir()
    no_progress = PreflightRepairController(
        session_dir=no_progress_session,
        worktree_path=worktree,
        port_cleanup=lambda *_args: {"killed_ports": [18080], "bound_after": [], "repaired": True},
    )

    stuck = await no_progress.repair_until_clean(
        lambda: _blocking_payload("clean_deploy_port_busy", "Declared ports [18080] already bound")
    )

    assert stuck.terminal_state == "escalated"
    assert _log_events(no_progress_session)[-1]["reason"] == "budget_exhausted"


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
