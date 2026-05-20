"""Preflight/oracle payload helpers and integration smoke preflight.

Extracted from ``otto/v5_runner.py`` to reduce the size of the top-level
runner module. The public surface is unchanged — ``otto.v5_runner``
re-exports every symbol defined here, so ``from otto.v5_runner import
_build_repair_packet`` and ``monkeypatch.setattr("otto.v5_runner.X",
...)`` keep working.

Patchability contract: this module never imports patched symbols
(``subprocess``, ``verify_from_clean_oracle``,
``run_oracle_repair_agent``) by name. It looks them up at call time
via ``_v5r.X`` so test-time monkeypatches on ``otto.v5_runner`` are
honored.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast


from otto.defaults import (
    DEFAULT_ORACLE_STAGE_TIMEOUT_S,
    DEFAULT_PORT_WAIT_S,
    DEFAULT_REPAIR_AGENT_TURNS,
    DEFAULT_REPAIR_AGENT_WALL_CLOCK_S,
    DEFAULT_REPAIR_ORACLE_INVOCATIONS,
)
from otto.journey_scope_policy import ExecutionScope
from otto.lead import LeadResult
from otto.safe_slug import safe_slug
from otto.v5_branching import (
    commit_integration_worktree,
    ensure_branch_exists,
)
from otto.v5_clean_verify import (
    CleanOracleIssue,
    CleanOracleResult,
    CleanOracleStepResult,
    Scope,
    build_clean_verify_oracle_command,
    cleanup_stale_declared_ports,
)
from otto.worktree import add_worktree
from otto.v5_preflight import (
    preflight_issues_from_clean_oracle,
)
from otto.observability import iso_timestamp
from otto.v5_preflight_repair import (
    RepairBudget,
    RepairPacket,
)

logger = logging.getLogger("otto.v5_runner")

# Lazy access to the parent module to keep patchability of
# ``subprocess``/``verify_from_clean_oracle``/``run_oracle_repair_agent``
# at ``otto.v5_runner.X``. Functions defined here MUST reference these
# via ``_v5r.X`` at call time — never bind them locally.
from otto import v5_runner as _v5r  # noqa: E402


def _checkout_issue_payload(
    *,
    project_dir: Path,
    branch: str,
    current_branch: str,
    context: str,
    kind: str,
    message: str,
    status_lines: Any,
    on_event: Any = None,
) -> dict[str, Any]:
    lines = [str(line) for line in (status_lines or []) if str(line).strip()]
    issue = {
        "kind": kind,
        "severity": "block",
        "message": (
            f"{message}\n"
            f"phase={context}; current_branch={current_branch}; target_branch={branch}\n"
            f"dirty_status:\n" + "\n".join(lines[:80])
        ).strip(),
        "phase": context,
        "current_branch": current_branch,
        "target_branch": branch,
        "paths": [_status_path(line) for line in lines if _status_path(line)],
        "diff_stat": _v5r._git_diff_stat(project_dir),
    }
    payload = {
        "check": "git_checkout_clean",
        "phase": context,
        "cwd": str(project_dir),
        "passed": False,
        "issues": [issue],
        "error": None,
    }
    _v5r._emit(on_event, {
        "event": "checkout_preflight_issue",
        "phase": context,
        "kind": kind,
        "message": issue["message"],
    })
    return payload

def _status_path(line: str) -> str:
    text = line[3:].strip() if len(line) > 3 else line.strip()
    if " -> " in text:
        text = text.split(" -> ", 1)[1].strip()
    return text

def _port_cleanup_payload(cleanup: Any, *, project_dir: Path) -> dict[str, Any]:
    still_bound = list(getattr(cleanup, "still_bound_ports", []) or [])
    process_details = {
        str(port): _process_details_for_pids(
            list((getattr(cleanup, "pids_after", {}) or {}).get(port, []))
            or list((getattr(cleanup, "pids_before", {}) or {}).get(port, []))
        )
        for port in still_bound
    }
    payload: dict[str, Any] = {
        "check": "startup_port_cleanup",
        "phase": "pipeline_start",
        "cwd": str(project_dir),
        "passed": not still_bound,
        "issues": [],
        "cleanup": {
            "killed_ports": list(getattr(cleanup, "killed_ports", []) or []),
            "freed_ports": list(getattr(cleanup, "freed_ports", []) or []),
            "still_bound_ports": still_bound,
            "killed_pids": getattr(cleanup, "killed_pids", {}) or {},
            "pids_before": getattr(cleanup, "pids_before", {}) or {},
            "pids_after": getattr(cleanup, "pids_after", {}) or {},
            "ports_without_owned_process": list(
                getattr(cleanup, "ports_without_owned_process", []) or []
            ),
            "processes": process_details,
        },
        "error": None,
    }
    if still_bound:
        payload["issues"] = [
            {
                "kind": "clean_deploy_port_busy",
                "severity": "block",
                "message": (
                    "Declared ports still bound after startup cleanup: "
                    + ", ".join(str(port) for port in still_bound)
                ),
                "ports": still_bound,
                "processes": process_details,
            }
        ]
    return payload

def _process_details_for_pids(pids: list[int]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    clean_pids: set[int] = set()
    for raw_pid in pids:
        try:
            pid_int = int(raw_pid)
        except (TypeError, ValueError):
            continue
        if pid_int > 0:
            clean_pids.add(pid_int)
    for pid in sorted(clean_pids):
        detail: dict[str, Any] = {"pid": pid, "binary": "", "cmdline": ""}
        try:
            import psutil

            proc = psutil.Process(pid)
            detail["binary"] = proc.exe() or proc.name() or ""
            detail["cmdline"] = " ".join(proc.cmdline()) or proc.name() or ""
        except Exception:  # noqa: BLE001
            try:
                proc = _v5r.subprocess.run(
                    ["ps", "-p", str(pid), "-o", "comm=", "-o", "command="],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                line = (proc.stdout or "").strip().splitlines()
                if proc.returncode == 0 and line:
                    parts = line[0].strip().split(None, 1)
                    detail["binary"] = parts[0] if parts else ""
                    detail["cmdline"] = parts[1] if len(parts) > 1 else line[0].strip()
            except (OSError, _v5r.subprocess.SubprocessError):
                pass
        details.append(detail)
    return details

def _mechanical_fail_fast_payload(
    *,
    payload: dict[str, Any],
    kind: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = dict(payload)
    issue = {
        "kind": kind,
        "severity": "block",
        "message": message,
        **dict(details or {}),
    }
    out["passed"] = False
    out["issues"] = [issue]
    out["repair"] = {
        "terminal_state": "escalated",
        "repair_phase": "mechanical_env_classifier",
        "summary": message,
        "mechanical_blocker": issue,
        "_written_at": iso_timestamp(),
    }
    return out

def _handle_mechanical_preflight_blocker(
    *,
    payload: dict[str, Any],
    project_dir: Path,
    on_event: Any = None,
) -> tuple[str, dict[str, Any]]:
    """Classify preflight blockers before spending an LLM repair turn."""
    check = str(payload.get("check") or "")
    issues = [issue for issue in (payload.get("issues") or []) if isinstance(issue, dict)]
    if check == "git_checkout_clean":
        status_lines: list[str] = []
        for issue in issues:
            for path in issue.get("paths") or []:
                status_lines.append(f" M {path}")
            message = str(issue.get("message") or "")
            status_lines.extend(_v5r._status_lines_from_detail(message))
        paths = _v5r._porcelain_paths(status_lines)
        if _v5r._dirty_paths_are_runner_committable(paths):
            ok, detail = _v5r._commit_runner_output_paths(
                worktree_path=project_dir,
                paths=paths,
                message="chore(otto): commit runner output before checkout",
            )
            _v5r._emit(on_event, {
                "event": "mechanical_blocker_committed",
                "kind": "dirty_from_otto_output",
                "paths": paths,
                "detail": detail,
            })
            if ok:
                return "retry", payload
            return "terminal", _mechanical_fail_fast_payload(
                payload=payload,
                kind="dirty_from_otto_output_commit_failed",
                message=detail,
                details={"paths": paths},
            )
        return "repair", payload

    if any(str(issue.get("kind") or "") == "clean_deploy_port_busy" for issue in issues):
        cleanup = payload.get("cleanup") if isinstance(payload.get("cleanup"), dict) else {}
        busy_ports = list(cleanup.get("still_bound_ports") or [])
        foreign_ports = list(cleanup.get("ports_without_owned_process") or busy_ports)
        processes = cleanup.get("processes") or {}
        if foreign_ports:
            message = (
                "Declared port(s) are still busy after killing Otto-owned processes: "
                + ", ".join(str(port) for port in foreign_ports)
            )
            return "terminal", _mechanical_fail_fast_payload(
                payload=payload,
                kind="clean_deploy_port_busy_foreign_process",
                message=message,
                details={"ports": foreign_ports, "processes": processes},
            )

    for issue in issues:
        kind = str(issue.get("kind") or "")
        message = str(issue.get("message") or "")
        lowered = f"{kind} {message}".lower()
        if (
            "missing_toolchain" in lowered
            or "missing toolchain" in lowered
            or "command not found" in lowered
            or "no such file or directory" in lowered and "tool" in lowered
        ):
            return "terminal", _mechanical_fail_fast_payload(
                payload=payload,
                kind="missing_toolchain_fail_fast",
                message=message or "required toolchain is missing",
                details={"issue_kind": kind},
            )
    return "repair", payload

def _handle_mechanical_merge_blocker(
    *,
    detail: str,
    project_dir: Path,
    child_task_id: str,
    on_event: Any = None,
) -> tuple[str, str]:
    status_lines = _v5r._status_lines_from_detail(detail)
    paths = _v5r._porcelain_paths(status_lines)
    if not _v5r._dirty_paths_are_runner_committable(paths):
        return "repair", detail
    ok, commit_detail = _v5r._commit_runner_output_paths(
        worktree_path=project_dir,
        paths=paths,
        message="chore(otto): commit runner output before upward merge",
    )
    _v5r._emit(on_event, {
        "event": "mechanical_blocker_committed",
        "kind": "dirty_from_otto_output",
        "task_id": child_task_id,
        "paths": paths,
        "detail": commit_detail,
    })
    if ok:
        return "retry", commit_detail
    return "terminal", commit_detail

async def _run_startup_port_cleanup_with_repair(
    *,
    project_dir: Path,
    session_dir: Path,
    config: dict[str, Any],
    on_event: Any = None,
) -> dict[str, Any]:
    from otto.v5_clean_verify import cleanup_stale_declared_ports

    def run_once() -> dict[str, Any]:
        cleanup = cleanup_stale_declared_ports(
            project_dir,
            logger_fn=lambda m: logger.info("preflight: %s", m),
        )
        payload = _port_cleanup_payload(cleanup, project_dir=project_dir)
        if payload["cleanup"]["killed_ports"] or payload["cleanup"]["still_bound_ports"]:
            _v5r._emit(on_event, {
                "event": "stale_ports_checked",
                "ports": payload["cleanup"],
                "passed": payload["passed"],
            })
        return payload

    first = run_once()
    if not _integration_smoke_blocks(first):
        return first

    action, classified = _handle_mechanical_preflight_blocker(
        payload=first,
        project_dir=project_dir,
        on_event=on_event,
    )
    if action == "terminal":
        return classified
    if action == "retry":
        second = run_once()
        if not _integration_smoke_blocks(second):
            return second
        first = second

    return await _run_preflight_payload_repair_session(
        initial_payload=first,
        run_once=run_once,
        project_dir=project_dir,
        worktree_path=project_dir,
        session_dir=session_dir,
        config=config,
        task_id="root",  # ROOT_TASK_ID — same constant as v5_runner.py
        repair_phase="startup_port_cleanup",
        event_prefix="startup_port_cleanup",
        integration_branch=None,
        verify_scope="subtree",
        on_event=on_event,
        integration_context={"cleanup": first.get("cleanup") or {}},
    )

def _read_text_artifact(path: Path, *, max_chars: int = 60000) -> dict[str, Any]:
    payload: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if not path.exists() or not path.is_file():
        return payload
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload
    payload["text"] = text[:max_chars]
    payload["truncated"] = len(text) > max_chars
    return payload

def _read_json_artifact(path: Path, *, max_chars: int = 120000) -> dict[str, Any]:
    payload = _read_text_artifact(path, max_chars=max_chars)
    text = payload.get("text")
    if not isinstance(text, str):
        return payload
    try:
        payload["json"] = json.loads(text)
    except json.JSONDecodeError as exc:
        payload["json_error"] = f"{type(exc).__name__}: {exc}"
    return payload

def _failing_ui_journey_count(latest_oracle_result: Any) -> int:
    """How many UI journeys the oracle reports failing.

    The smoke-pre repair agent fixes journeys in ONE shared sequential
    session; each journey = diagnose + product fix + a ~5min cold
    clean-verify oracle re-run. resume16n proved the agent CAN fix the
    genuine product gaps (journey 1 register/onboarding fail->pass via
    real commits) but the fixed ~1199s wall killed it mid-work on the
    remaining journeys — N cross-feature journeys do not fit one base
    budget. Returns 0 when there is no failing ui_journeys step (other
    repair phases are unaffected — they stay at the base budget).
    """
    if not isinstance(latest_oracle_result, dict):
        return 0
    for st in latest_oracle_result.get("steps") or []:
        if not isinstance(st, dict):
            continue
        sid = str(st.get("id") or st.get("command_identity") or "").lower()
        if "journey" not in sid or str(st.get("status") or "").lower() != "failed":
            continue
        reason = str(st.get("reason") or "")
        if "failed:" in reason:
            return len(
                [j for j in reason.split("failed:", 1)[1].split(",") if j.strip()]
            )
    return 0

def _repair_budget_from_config(
    config: dict[str, Any],
    *,
    prefix: str,
    default_agent_turns: int,
    default_oracle_invocations: int,
    default_wall_clock_s: float = DEFAULT_REPAIR_AGENT_WALL_CLOCK_S,
) -> RepairBudget:
    def number(key: str, default: float | None) -> float | None:
        raw = config.get(f"{prefix}_{key}", config.get(f"repair_{key}", default))
        if isinstance(raw, (int, float)):
            return float(raw)
        return default

    def integer(key: str, default: int | None) -> int | None:
        raw = config.get(f"{prefix}_{key}", config.get(f"repair_{key}", default))
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.strip().isdigit():
            return int(raw)
        return default

    return RepairBudget(
        wall_clock_s=float(number("wall_clock_s", default_wall_clock_s) or default_wall_clock_s),
        cost_usd=number("cost_usd", None),
        agent_turns=max(0, int(integer("agent_turns", default_agent_turns) or 0)),
        oracle_invocations=max(
            0,
            int(integer("oracle_invocations", default_oracle_invocations) or 0),
        ),
        idle_s=number("idle_s", None),
        diff_churn=integer("diff_churn", None),
        closeout_agent_turns=max(0, int(integer("closeout_agent_turns", 0) or 0)),
        provider_max_turns=integer(
            "provider_max_turns",
            int(config.get("max_turns_per_call") or 1),
        ),
    )

def _packet_attempt_history(packet_path: Path) -> list[dict[str, Any]]:
    if not packet_path.exists():
        return []
    try:
        packet = RepairPacket.load(packet_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    history = list(packet.attempt_history)
    events = packet.events()
    if events:
        history.append({
            "type": "prior_packet_events",
            "event_count": len(events),
            "events_tail": events[-20:],
        })
    return history

def _make_initial_oracle_payload(
    *,
    worktree: Path,
    scope: Scope,
    oracle_command: Any,
    issue_kind: str,
    issue_message: str,
    step_id: str,
    paths: list[str] | None = None,
) -> dict[str, Any]:
    step = CleanOracleStepResult(
        id=step_id,
        status="failed",
        return_code=1,
        command_identity=getattr(oracle_command, "command_identity", "clean-verify"),
        command=list(getattr(oracle_command, "command", []) or []),
        cwd=str(worktree),
        env=dict(getattr(oracle_command, "env", {}) or {}),
        reason=issue_message,
    )
    issue = CleanOracleIssue(
        kind=issue_kind,
        severity="block",
        message=issue_message,
        step_id=step_id,
        paths=list(paths or []),
        command_identity=step.command_identity,
        return_code=1,
    )
    result = CleanOracleResult.from_parts(
        passed=False,
        scope=scope,
        issues=[issue],
        steps=[step],
        artifact_path_refs=[],
        command=step.command,
        env=step.env,
        project_dir=worktree,
        temp_dir=None,
    )
    return result.to_jsonable()

def _merge_refusal_oracle_payload(
    *,
    worktree: Path,
    scope: Scope,
    oracle_command: Any,
    issue_kind: str,
    issue_message: str,
    step_id: str,
    paths: list[str] | None = None,
) -> dict[str, Any]:
    return _make_initial_oracle_payload(
        worktree=worktree,
        scope=scope,
        oracle_command=oracle_command,
        issue_kind=issue_kind,
        issue_message=issue_message,
        step_id=step_id,
        paths=paths,
    )

def _blocking_payload_issues(payload: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for issue in payload.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        if issue.get("severity") not in ("error", "block"):
            continue
        issues.append(issue)
    if issues:
        return issues
    if payload.get("passed") is False:
        return [
            {
                "kind": str(payload.get("error") or payload.get("check") or "preflight_failed"),
                "severity": "block",
                "message": str(payload.get("error") or "preflight failed without issue detail"),
            }
        ]
    return []

def _clean_oracle_payload_from_preflight_payload(
    *,
    payload: dict[str, Any],
    worktree: Path,
    scope: Scope,
    oracle_command: Any,
    step_id: str,
) -> dict[str, Any]:
    clean_oracle = payload.get("clean_oracle_result")
    if isinstance(clean_oracle, dict) and isinstance(clean_oracle.get("issues"), list):
        return clean_oracle

    issues: list[CleanOracleIssue] = []
    issue_payloads = _blocking_payload_issues(payload)
    for raw_issue in issue_payloads:
        raw_severity = str(raw_issue.get("severity") or "block")
        severity = raw_severity if raw_severity in {"warn", "error", "block"} else "block"
        ports: list[int] = []
        for raw_port in raw_issue.get("ports") or []:
            try:
                ports.append(int(raw_port))
            except (TypeError, ValueError):
                continue
        issues.append(
            CleanOracleIssue(
                kind=str(raw_issue.get("kind") or payload.get("check") or "preflight_failed"),
                severity=cast(Any, severity),
                message=str(raw_issue.get("message") or payload.get("error") or ""),
                step_id=str(raw_issue.get("step_id") or step_id),
                paths=[str(path) for path in (raw_issue.get("paths") or [])],
                ports=ports,
                command_identity=getattr(oracle_command, "command_identity", "clean-verify"),
                return_code=(
                    int(raw_issue["return_code"])
                    if isinstance(raw_issue.get("return_code"), int)
                    else 1
                ),
            )
        )

    message = "; ".join(issue.message for issue in issues) or str(
        payload.get("error") or "preflight failed"
    )
    step = CleanOracleStepResult(
        id=step_id,
        status="failed",
        return_code=1,
        command_identity=getattr(oracle_command, "command_identity", "clean-verify"),
        command=list(getattr(oracle_command, "command", []) or []),
        cwd=str(worktree),
        env=dict(getattr(oracle_command, "env", {}) or {}),
        reason=message,
    )
    result = CleanOracleResult.from_parts(
        passed=False,
        scope=scope,
        issues=issues,
        steps=[step],
        artifact_path_refs=[],
        command=step.command,
        env=step.env,
        project_dir=worktree,
        temp_dir=None,
    )
    return result.to_jsonable()

def _worktree_product_contract(
    *,
    worktree: Path,
    spec_path: Path | None = None,
) -> dict[str, Any]:
    contract: dict[str, Any] = {
        "worktree": str(worktree),
        "config": _read_json_artifact(worktree / "otto.yaml"),
        "charter": _read_text_artifact(worktree / "CHARTER.md"),
    }
    if spec_path is not None:
        contract["spec"] = _read_json_artifact(spec_path)
    else:
        contract["spec"] = _read_json_artifact(worktree / "spec.json")
    return contract

def _build_repair_packet(
    *,
    session_dir: Path,
    repair_slug: str,
    worktree_path: Path,
    task_id: str,
    phase: str,
    repair_phase: str,
    verify_scope: Scope,
    config: dict[str, Any],
    budget_prefix: str,
    default_agent_turns: int,
    default_oracle_invocations: int,
    latest_oracle_result: dict[str, Any] | Callable[[Any], dict[str, Any]],
    product_contract: dict[str, Any],
    integration_context: dict[str, Any],
    success_criteria: dict[str, Any],
    attempt_history_entry: dict[str, Any],
    expected_artifact_paths: list[str] | None = None,
    allowed_paths: list[str] | None = None,
    scope_policy: str = "unrestricted",
    branch: str | None = None,
    repair_unit_extra: dict[str, Any] | None = None,
    oracle_spec_path: Path | None = None,
    oracle_journey_scope: ExecutionScope = "subtree_integration",
    oracle_journey_artifact_dir: Path | None = None,
) -> RepairPacket:
    packet_dir = session_dir / "repair" / repair_slug
    packet_path = packet_dir / "repair_packet.json"
    packet_dir.mkdir(parents=True, exist_ok=True)
    link_path = packet_dir / "worktree"
    if not link_path.exists():
        try:
            link_path.symlink_to(worktree_path)
        except OSError as exc:
            logger.debug("could not symlink repair worktree %s: %s", link_path, exc)

    oracle_command = build_clean_verify_oracle_command(
        worktree_path=worktree_path,
        verify_scope=verify_scope,
        repair_packet_path=packet_path,
        spec_path=oracle_spec_path,
        journey_scope=oracle_journey_scope,
        journey_artifact_dir=oracle_journey_artifact_dir,
    )
    if callable(latest_oracle_result):
        latest_payload = latest_oracle_result(oracle_command)
    else:
        latest_payload = latest_oracle_result
    attempt_history = _packet_attempt_history(packet_path)
    attempt = dict(attempt_history_entry)
    attempt.setdefault("_written_at", iso_timestamp())
    attempt_history.append(attempt)
    current_branch = branch if branch is not None else _v5r._git_capture(worktree_path, ["branch", "--show-current"])
    head = _v5r._git_capture(worktree_path, ["rev-parse", "HEAD"])
    prior_agent_session_id = ""
    if packet_path.exists():
        try:
            prior_agent_session_id = RepairPacket.load(packet_path).agent_session_id
        except (OSError, json.JSONDecodeError, ValueError):
            prior_agent_session_id = ""
    packet = RepairPacket(
        repair_unit={
            "id": repair_slug,
            "worktree": str(worktree_path),
            "branch": current_branch,
            "task_id": task_id,
            "phase": phase,
            "repair_phase": repair_phase,
            "allowed_paths": list(allowed_paths or []),
            "scope_policy": scope_policy,
            **dict(repair_unit_extra or {}),
        },
        acceptance_oracle={
            "verify_scope": verify_scope,
            "command": oracle_command.command,
            "env": oracle_command.env,
            "timeout_s": int(
                config.get(f"{budget_prefix}_oracle_timeout_s")
                or DEFAULT_ORACLE_STAGE_TIMEOUT_S
            ),
            "expected_artifact_paths": list(expected_artifact_paths or []),
            "success_criteria": {
                "clean_deploy": True,
                "composite_gate": True,
                **dict(success_criteria),
            },
        },
        latest_oracle_result=latest_payload,
        product_contract=product_contract,
        integration_context=integration_context,
        attempt_history=attempt_history,
        current_state={
            "git_status": _git_status_short(worktree_path),
            "head": head,
            "pre_repair_head": head,
            "branch": current_branch,
        },
        budget=_repair_budget_from_config(
            config,
            prefix=budget_prefix,
            default_agent_turns=default_agent_turns,
            default_oracle_invocations=default_oracle_invocations,
            # Scale the wall budget with the number of failing UI journeys
            # so a cross-feature repair (each journey = fix + ~5min cold
            # oracle re-run) actually fits. Per-journey marginal cost is
            # ~20 min; absolute cap of 6000s (5 journeys' worth) bounds
            # the worst case. Floor is DEFAULT_REPAIR_AGENT_WALL_CLOCK_S
            # so the journey path never gets *less* than the base any
            # other repair phase would get. An explicit
            # {prefix}_wall_clock_s config override still wins. Not
            # gate-weakening: journeys must still genuinely pass, and
            # idle/cost/diff-churn budgets + agent escalation still
            # terminate a non-progressing repair.
            default_wall_clock_s=max(
                DEFAULT_REPAIR_AGENT_WALL_CLOCK_S,
                min(6000.0, 1200.0 * max(1, _failing_ui_journey_count(latest_payload))),
            ),
        ),
        packet_dir=packet_dir,
        agent_session_id=prior_agent_session_id,
    )
    packet.capture_scope_baseline()
    return packet

def _repair_result_payload(
    *,
    repair_phase: str,
    repair: Any,
    terminal_state: str,
    final_payload: dict[str, Any],
) -> dict[str, Any]:
    repaired = terminal_state == "continued"
    attempt_outcome = "repaired" if repaired else "escalated"
    return {
        "terminal_state": terminal_state,
        "repaired": repaired,
        "repair_phase": repair_phase,
        "oracle_verdict": getattr(repair, "verdict", ""),
        "summary": getattr(repair, "summary", ""),
        "cost_usd": float(getattr(repair, "cost_usd", 0.0) or 0.0),
        "agent_turns_used": int(getattr(repair, "agent_turns_used", 0) or 0),
        "oracle_invocations": int(getattr(repair, "oracle_invocations", 0) or 0),
        "packet_path": str(getattr(repair, "packet_path", "") or ""),
        "composite_gate": getattr(repair, "composite_gate", None),
        "escalation": getattr(repair, "escalation", None),
        "final_preflight_passed": not _integration_smoke_blocks(final_payload),
        "attempts": [
            {
                "action": "agent",
                "outcome": attempt_outcome,
                "repair_phase": repair_phase,
                "repair_packet": str(getattr(repair, "packet_path", "") or ""),
                "summary": getattr(repair, "summary", ""),
                "cost_usd": float(getattr(repair, "cost_usd", 0.0) or 0.0),
            }
        ],
    }

async def _run_preflight_payload_repair_session(
    *,
    initial_payload: dict[str, Any],
    run_once: Any,
    project_dir: Path,
    worktree_path: Path,
    session_dir: Path,
    config: dict[str, Any],
    task_id: str,
    repair_phase: str,
    event_prefix: str,
    integration_branch: str | None,
    verify_scope: Scope = "subtree",
    on_event: Any = None,
    integration_context: dict[str, Any] | None = None,
    allowed_paths: list[str] | None = None,
    scope_policy: str = "unrestricted",
    journey_scope: ExecutionScope = "subtree_integration",
) -> dict[str, Any]:
    action, classified = _handle_mechanical_preflight_blocker(
        payload=initial_payload,
        project_dir=project_dir,
        on_event=on_event,
    )
    if action == "terminal":
        return classified
    if action == "retry":
        retried = run_once()
        if not _integration_smoke_blocks(retried):
            return retried
        initial_payload = retried

    repair_slug = safe_slug(
        f"{task_id}-{repair_phase}-{initial_payload.get('phase') or initial_payload.get('check') or 'repair'}",
        max_len=64,
    )
    branch = _v5r._git_capture(worktree_path, ["branch", "--show-current"])
    packet = _build_repair_packet(
        session_dir=session_dir,
        repair_slug=repair_slug,
        worktree_path=worktree_path,
        task_id=task_id,
        phase="preflight",
        repair_phase=repair_phase,
        verify_scope=verify_scope,
        config=config,
        budget_prefix=f"{repair_phase}_repair",
        default_agent_turns=DEFAULT_REPAIR_AGENT_TURNS,
        default_oracle_invocations=DEFAULT_REPAIR_ORACLE_INVOCATIONS,
        latest_oracle_result=lambda oracle_command: _clean_oracle_payload_from_preflight_payload(
            payload=initial_payload,
            worktree=worktree_path,
            scope=verify_scope,
            oracle_command=oracle_command,
            step_id=str(initial_payload.get("check") or repair_phase),
        ),
        product_contract=_worktree_product_contract(worktree=worktree_path),
        integration_context={
            "integration_branch": integration_branch,
            "project_dir": str(project_dir),
            "initial_preflight": initial_payload,
            **dict(integration_context or {}),
        },
        success_criteria={
            "preflight_operation": initial_payload.get("check") or repair_phase,
        },
        attempt_history_entry={
            "type": "preflight_blocking_payload",
            "repair_phase": repair_phase,
            "payload": initial_payload,
            "git_status": _git_status_short(worktree_path),
        },
        allowed_paths=list(allowed_paths or []),
        scope_policy=scope_policy,
        branch=branch or integration_branch or "",
        oracle_spec_path=session_dir / "spec" / "spec.json",
        oracle_journey_scope=journey_scope,
        oracle_journey_artifact_dir=session_dir / "journeys" / safe_slug(repair_phase, max_len=48),
    )
    _v5r._emit(on_event, {
        "event": f"{event_prefix}_repair_start",
        "task_id": task_id,
        "repair_phase": repair_phase,
        "repair_packet": str(packet.packet_path),
    })

    async def commit_hook(_packet: RepairPacket, _oracle_result: Any) -> tuple[bool, str]:
        from otto.v5_branching import commit_integration_worktree

        changed_paths = _v5r._git_diff_name_only(worktree_path)
        scope_feedback = _v5r._allowed_paths_write_feedback(
            acting_task_id=task_id,
            changed_paths=changed_paths,
            allowed_paths=allowed_paths,
            scope_policy=scope_policy,
            operation=f"{repair_phase}_repair_commit",
        )
        if scope_feedback is not None:
            detail = _v5r._foundation_contract_write_block_detail(scope_feedback)
            _v5r._emit(on_event, {
                "event": f"{event_prefix}_repair_commit_failed",
                "task_id": task_id,
                "repair_phase": repair_phase,
                "worktree": str(worktree_path),
                "detail": detail,
                "structured_reason": scope_feedback,
            })
            return False, detail
        feedback = _v5r._foundation_contract_write_feedback(
            project_dir=project_dir,
            acting_task_id=task_id,
            parent_integration_branch=integration_branch or _branch_checked_out(worktree_path) or "main",
            changed_paths=changed_paths,
            operation=f"{repair_phase}_repair_commit",
        )
        if feedback is not None:
            detail = _v5r._foundation_contract_write_block_detail(feedback)
            _v5r._emit(on_event, {
                "event": f"{event_prefix}_repair_commit_failed",
                "task_id": task_id,
                "repair_phase": repair_phase,
                "worktree": str(worktree_path),
                "detail": detail,
                "structured_reason": feedback,
            })
            return False, detail
        commit_ok, commit_detail = commit_integration_worktree(
            worktree_path=worktree_path,
            task_id=f"{task_id}-{repair_phase}",
        )
        _v5r._emit(on_event, {
            "event": (
                f"{event_prefix}_repair_commit"
                if commit_ok
                else f"{event_prefix}_repair_commit_failed"
            ),
            "task_id": task_id,
            "repair_phase": repair_phase,
            "worktree": str(worktree_path),
            "detail": commit_detail,
        })
        return commit_ok, commit_detail

    repair = await _v5r.run_oracle_repair_agent(
        packet,
        config=config,
        commit_hook=commit_hook,
    )
    if repair.verdict == "pass":
        final_payload = run_once()
    else:
        final_payload = dict(initial_payload)
    terminal_state = (
        "continued"
        if repair.verdict == "pass" and not _integration_smoke_blocks(final_payload)
        else "escalated"
    )
    final_payload["repair"] = _repair_result_payload(
        repair_phase=repair_phase,
        repair=repair,
        terminal_state=terminal_state,
        final_payload=final_payload,
    )
    _v5r._emit(on_event, {
        "event": f"{event_prefix}_repair_done",
        "task_id": task_id,
        "terminal_state": terminal_state,
        "repair_phase": repair_phase,
        "repair_packet": getattr(repair, "packet_path", str(packet.packet_path)),
    })
    return final_payload

async def _checkout_v5_branch_clean_with_repair(
    *,
    project_dir: Path,
    branch: str,
    context: str,
    session_dir: Path,
    config: dict[str, Any],
    integration_branch: str | None,
    task_id: str,
    on_event: Any = None,
) -> dict[str, Any]:
    """Checkout a branch, repairing dirty/failed checkout states via agent."""

    def passed_payload() -> dict[str, Any]:
        return {
            "check": "git_checkout_clean",
            "phase": context,
            "cwd": str(project_dir),
            "passed": True,
            "issues": [],
            "error": None,
        }

    def run_once() -> dict[str, Any]:
        payload = _v5r._checkout_v5_branch_clean(
            project_dir=project_dir,
            branch=branch,
            context=context,
            on_event=on_event,
        )
        return payload or passed_payload()

    first = run_once()
    if not _integration_smoke_blocks(first):
        return first
    action, classified = _handle_mechanical_preflight_blocker(
        payload=first,
        project_dir=project_dir,
        on_event=on_event,
    )
    if action == "terminal":
        return classified
    if action == "retry":
        second = run_once()
        if not _integration_smoke_blocks(second):
            return second
        first = second
    return await _run_preflight_payload_repair_session(
        initial_payload=first,
        run_once=run_once,
        project_dir=project_dir,
        worktree_path=project_dir,
        session_dir=session_dir,
        config=config,
        task_id=task_id,
        repair_phase="checkout_clean",
        event_prefix="checkout",
        integration_branch=integration_branch,
        verify_scope="subtree",
        on_event=on_event,
        integration_context={
            "checkout": {
                "branch": branch,
                "context": context,
            }
        },
    )

def _preflight_issue_payload(issue: Any) -> dict[str, Any]:
    return {
        "kind": getattr(issue, "kind", "unknown"),
        "severity": getattr(issue, "severity", "warn"),
        "message": getattr(issue, "message", ""),
        "task_id": getattr(issue, "task_id", None),
        "paths": list(getattr(issue, "paths", None) or []),
    }

def _run_integration_smoke_preflight(
    *,
    worktree_path: Path,
    task_id: str,
    phase: str,
    journey_scope: ExecutionScope = "subtree_integration",
    spec_path: Path | None = None,
    journey_artifact_dir: Path | None = None,
    on_event: Any = None,
) -> dict[str, Any]:
    """Run clean-deploy smoke for an integration worktree and serialize it."""
    payload: dict[str, Any] = {
        "check": "smoke_clean_deploy",
        "phase": phase,
        "task_id": task_id,
        "cwd": str(worktree_path),
        "passed": True,
        "issues": [],
        "error": None,
    }
    try:
        logger.info(
            "preflight: running %s integration clean-deploy check in %s",
            phase,
            worktree_path,
        )
        # FOUNDATION-GATE CLEAN-BOOT PROBE scope (consistent-by-construction
        # with the foundation child's OWN verify_result). The probe exists to
        # catch the boot/build-clean class EARLY — does the MERGED foundation
        # scaffold install/build/start/serve clean before the ~50min feature
        # children absorb a broken env. The product ``behavior_journeys`` are
        # Feature scope: a passed foundation legitimately ships only shared
        # infra + stub pages, and its own verify_result classifies the
        # behavior journeys ``passed=False`` "not runnable at foundation
        # level — Feature A/B will implement the real pages" while STILL
        # passing the foundation. Running those journeys here against the
        # stub-only foundation is a category error — they can never pass, and
        # they burn the entire bounded foundation-clean-boot repair turn
        # re-implementing Feature scope (observed p0fix3: 8 commits, 1199s
        # timeout, architect re-entry, budget-terminal merge_blocked). Pass an
        # explicit EMPTY behavior-journey list for this phase only: it
        # short-circuits ``_load_clean_oracle_journeys`` BEFORE the schema_v4
        # ``>=1 behavior journey`` validator, so 0 behavior journeys run while
        # the clean_deploy/install/build/start/health step DAG — which is
        # independent of behavior_journeys and is the entire reason this probe
        # exists — stays FULLY gated (NOT gate-weakening; the p0fix2
        # npm-run-build rollup block would still fire here). Every other
        # phase keeps the default (None => the full compiled journeys).
        _probe_behavior_journeys: list[dict[str, Any]] | None = (
            [] if phase == "foundation_clean_boot" else None
        )
        clean_oracle_result = _v5r.verify_from_clean_oracle(
            worktree_path,
            scope="subtree",
            timeout_s=90,
            port_wait_s=DEFAULT_PORT_WAIT_S,
            logger_fn=lambda m: logger.info("preflight: %s", m),
            journey_scope=journey_scope,
            spec_path=spec_path,
            journey_artifact_dir=journey_artifact_dir,
            behavior_journeys=_probe_behavior_journeys,
        )
        payload["clean_oracle_result"] = clean_oracle_result.to_jsonable()
        smoke_issues = preflight_issues_from_clean_oracle(
            clean_oracle_result,
            surface="clean_deploy",
        )
        payload["issues"] = [_preflight_issue_payload(issue) for issue in smoke_issues]
        payload["passed"] = not smoke_issues
        for issue in smoke_issues:
            issue_payload = _preflight_issue_payload(issue)
            log_fn = (
                logger.error
                if issue_payload["severity"] in ("error", "block")
                else logger.warning
            )
            log_fn(
                "preflight %s [%s]: %s",
                issue_payload["kind"],
                issue_payload["severity"],
                issue_payload["message"],
            )
            _v5r._emit(on_event, {
                "event": "preflight_issue",
                "phase": phase,
                "worktree": str(worktree_path),
                "kind": issue_payload["kind"],
                "severity": issue_payload["severity"],
                "message": issue_payload["message"],
            })
    except Exception as exc:  # noqa: BLE001 - report to agent, do not crash runner
        payload["passed"] = False
        payload["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("integration %s smoke check raised: %s", phase, exc)
        payload["issues"] = [
            {
                "kind": "oracle_infra_error",
                "severity": "block",
                "message": payload["error"],
                "task_id": task_id,
            }
        ]
        _v5r._emit(on_event, {
            "event": "preflight_issue",
            "phase": phase,
            "worktree": str(worktree_path),
            "kind": "oracle_infra_error",
            "severity": "block",
            "message": payload["error"],
        })
    return payload

async def _run_integration_smoke_preflight_with_repair(
    *,
    project_dir: Path,
    worktree_path: Path,
    task_id: str,
    phase: str,
    session_dir: Path,
    config: dict[str, Any],
    integration_branch: str | None,
    journey_scope: ExecutionScope = "subtree_integration",
    allowed_paths: list[str] | None = None,
    scope_policy: str = "unrestricted",
    on_event: Any = None,
) -> dict[str, Any]:
    """Run clean-deploy smoke and repair blocking issues before continuing."""
    spec_path = session_dir / "spec" / "spec.json"
    journey_artifact_dir = session_dir / "journeys" / safe_slug(phase, max_len=48)

    def run_once() -> dict[str, Any]:
        return _v5r._run_integration_smoke_preflight(
            worktree_path=worktree_path,
            task_id=task_id,
            phase=phase,
            journey_scope=journey_scope,
            spec_path=spec_path,
            journey_artifact_dir=journey_artifact_dir,
            on_event=on_event,
        )

    first = run_once()
    if not _integration_smoke_blocks(first):
        return first

    return await _run_preflight_payload_repair_session(
        initial_payload=first,
        run_once=run_once,
        project_dir=project_dir,
        worktree_path=worktree_path,
        session_dir=session_dir,
        config=config,
        task_id=task_id,
        repair_phase="integration_smoke",
        event_prefix="integration_smoke",
        integration_branch=integration_branch,
        verify_scope="subtree",
        on_event=on_event,
        integration_context={
            "smoke_phase": phase,
        },
        allowed_paths=allowed_paths,
        scope_policy=scope_policy,
        journey_scope=journey_scope,
    )

def _integration_smoke_blocks(payload: dict[str, Any]) -> bool:
    if payload.get("error") and payload.get("passed") is False:
        return True
    issues = payload.get("issues") or []
    return any(
        isinstance(issue, dict)
        and issue.get("severity") in ("error", "block")
        for issue in issues
    )

def _preflight_repair_escalated(payload: dict[str, Any]) -> bool:
    repair = payload.get("repair")
    return isinstance(repair, dict) and repair.get("terminal_state") == "escalated"

def _preflight_blocked_result(
    *,
    task_id: str,
    preflight_result: dict[str, Any],
) -> LeadResult:
    reason = _preflight_blocking_summary(
        "Integration preflight repair escalated before agent dispatch",
        preflight_result,
    )
    return LeadResult(
        task_id=task_id,
        verdict="merge_blocked",
        verify_called=True,
        verify_result={
            "verdict": "merge_blocked",
            "summary": reason,
            "pre_integration_preflight": preflight_result,
        },
        failure_reason=reason,
    )

def _preflight_blocking_summary(prefix: str, payload: dict[str, Any]) -> str:
    messages = [
        str(issue.get("message") or issue.get("kind"))
        for issue in payload.get("issues", [])
        if isinstance(issue, dict)
        and issue.get("severity") in ("error", "block")
    ]
    return prefix + (": " + "; ".join(messages) if messages else "")

def _git_status_short(worktree_path: Path) -> str:
    try:
        proc = _v5r.subprocess.run(
            ["git", "status", "--short"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, _v5r.subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.rstrip("\n")

def _branch_checked_out(worktree_path: Path) -> str:
    try:
        proc = _v5r.subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, _v5r.subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""

def _integration_worktree_setup_payload(
    *,
    project_dir: Path,
    task_id: str,
    integration_branch: str,
    worktree_path: Path | None,
    detail: str,
) -> dict[str, Any]:
    actual_branch = _branch_checked_out(worktree_path) if worktree_path is not None else ""
    passed = bool(worktree_path and worktree_path.exists() and actual_branch == integration_branch)
    issue_kind = "integration_worktree_wrong_branch" if worktree_path and actual_branch else "integration_worktree_setup_failed"
    message = detail
    if worktree_path and actual_branch != integration_branch:
        message = (
            f"integration worktree {worktree_path} is on {actual_branch or 'unknown'}; "
            f"expected {integration_branch}"
        )
    return {
        "check": "integration_worktree_setup",
        "task_id": task_id,
        "cwd": str(worktree_path or project_dir),
        "integration_branch": integration_branch,
        "actual_branch": actual_branch,
        "passed": passed,
        "issues": [] if passed else [
            {
                "kind": issue_kind,
                "severity": "block",
                "message": message or "integration worktree setup failed",
            }
        ],
        "error": None if passed else (message or "integration worktree setup failed"),
    }

def _setup_integration_worktree_once(
    *,
    project_dir: Path,
    task_id: str,
    own_integration_branch: str,
    integration_session_dir: Path,
) -> tuple[Path | None, str]:
    from otto.v5_branching import child_worktree_path, ensure_branch_exists
    from otto.worktree import add_worktree

    existing = _v5r.subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(project_dir), capture_output=True, text=True,
    )
    existing_path: Path | None = None
    if existing.returncode == 0:
        block_path: str | None = None
        for line in existing.stdout.splitlines():
            if line.startswith("worktree "):
                block_path = line[len("worktree "):].strip()
            elif line.startswith("branch ") and block_path:
                if line.endswith(f"/{own_integration_branch}") or line.endswith(own_integration_branch):
                    existing_path = Path(block_path)
                    break

    if existing_path is not None and existing_path.exists():
        integration_worktree = existing_path
    else:
        ensure_branch_exists(project_dir, own_integration_branch, base_ref="main")
        integration_worktree = child_worktree_path(project_dir, f"integ-{task_id}")
        if not (integration_worktree.exists() and (integration_worktree / ".git").exists()):
            add_worktree(
                project_dir=project_dir,
                worktree_path=integration_worktree,
                branch=own_integration_branch,
            )

    if not integration_worktree.exists():
        return None, f"integration worktree path does not exist: {integration_worktree}"
    branch = _branch_checked_out(integration_worktree)
    if branch != own_integration_branch:
        return integration_worktree, (
            f"integration worktree {integration_worktree} is on "
            f"{branch or 'unknown'}; expected {own_integration_branch}"
        )

    link_path = integration_session_dir / "worktree"
    if not link_path.exists():
        try:
            link_path.symlink_to(integration_worktree)
        except OSError as exc:
            logger.warning("symlink worktree failed: %s", exc)
    return integration_worktree, ""

async def _prepare_integration_worktree_with_repair(
    *,
    project_dir: Path,
    task_id: str,
    integration_branch: str,
    integration_session_dir: Path,
    config: dict[str, Any],
    on_event: Any = None,
) -> tuple[Path | None, dict[str, Any]]:
    prepared_path: Path | None = None

    def run_once() -> dict[str, Any]:
        nonlocal prepared_path
        try:
            prepared_path, detail = _v5r._setup_integration_worktree_once(
                project_dir=project_dir,
                task_id=task_id,
                own_integration_branch=integration_branch,
                integration_session_dir=integration_session_dir,
            )
        except Exception as exc:  # noqa: BLE001
            prepared_path = None
            detail = f"{type(exc).__name__}: {exc}"
            logger.warning("integration worktree setup failed for %s: %s", task_id, exc)
        return _integration_worktree_setup_payload(
            project_dir=project_dir,
            task_id=task_id,
            integration_branch=integration_branch,
            worktree_path=prepared_path,
            detail=detail,
        )

    first = run_once()
    if not _integration_smoke_blocks(first):
        return prepared_path, first

    payload = await _run_preflight_payload_repair_session(
        initial_payload=first,
        run_once=run_once,
        project_dir=project_dir,
        worktree_path=project_dir,
        session_dir=integration_session_dir,
        config=config,
        task_id=task_id,
        repair_phase="integration_worktree_setup",
        event_prefix="integration_worktree_setup",
        integration_branch=integration_branch,
        verify_scope="subtree",
        on_event=on_event,
        integration_context={
            "integration_branch": integration_branch,
        },
    )
    return prepared_path, payload


