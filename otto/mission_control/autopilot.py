"""Mission Control Autopilot recovery supervisor.

Autopilot is a bounded reconciler around the existing Otto queue, merge, and
Mission Control actions. It does not own normal build/certify execution. Its
job is to notice when the system is stuck, decide whether recovery is safe, and
either propose or execute a validated playbook.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, cast

from otto import paths
from otto.config import ConfigError, load_config
from otto.defaults import AUTOPILOT_RATE_LIMIT_WINDOW_S
from otto.mission_control.events import append_event
from otto.verification import normalize_verification_policy

SCHEMA_VERSION = 1
AUTOPILOT_MODES = ("off", "assisted", "full")
ACTIVE_DECISION_STATUSES = {"pending", "running"}
logger = logging.getLogger("otto.mission_control.autopilot")
DECISION_STATUSES = {*ACTIVE_DECISION_STATUSES, "executed", "blocked", "failed", "dismissed"}
SAFE_FULL_ACTIONS = {
    "start_watcher",
    "stop_watcher",
    "merge_recover",
    "merge_all",
    "rerun_merge_verification",
    "resolve_release",
    "pilot_triage",
    "requeue",
}
PILOT_ACTION_ALLOWLIST = {
    "start_watcher",
    "stop_watcher",
    "merge_recover",
    "merge_all",
    "rerun_merge_verification",
    "resolve_release",
    "requeue",
    "noop",
}
LARGE_LOG_BYTES = 250 * 1024 * 1024
LANDING_IN_PROGRESS_STATUSES = {"queued", "starting", "initializing", "running", "terminating"}


class AutopilotExecutor(Protocol):
    def start_watcher(self, *, concurrent: int | None = None, exit_when_empty: bool = False) -> dict[str, Any]: ...
    def stop_watcher(self) -> dict[str, Any]: ...
    def merge_recover(self) -> dict[str, Any]: ...
    def merge_all(self, *, verification_policy: str | None = "smart") -> dict[str, Any]: ...
    def rerun_merge_verification(self, merge_id: str, *, verification_policy: str | None = "smart") -> dict[str, Any]: ...
    def resolve_release_issues(self) -> dict[str, Any]: ...
    def execute(self, run_id: str, action: str, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class AutopilotPolicy:
    mode: str
    configured_mode: str
    max_actions_per_hour: int
    max_pilot_calls_per_hour: int
    allow_auto_land: bool
    verification_policy: str
    pilot_enabled: bool
    pilot_timeout_s: int


def autopilot_state_path(project_dir: Path) -> Path:
    return paths.logs_dir(project_dir) / "mission-control" / "autopilot.json"


def autopilot_events_path(project_dir: Path) -> Path:
    return paths.logs_dir(project_dir) / "mission-control" / "autopilot-events.jsonl"


class AutopilotController:
    def __init__(self, project_dir: Path) -> None:
        self.project_dir = Path(project_dir).resolve(strict=False)

    def status(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        state = self._read_state()
        policy = self._policy(state)
        incidents = self._classify(snapshot)
        pending = self._status_decisions(state, incidents, policy)
        budget = self._budget_status(policy)
        return self._status_payload(
            policy=policy,
            incidents=incidents,
            decisions=pending,
            budget=budget,
            state=state,
        )

    def set_mode(self, mode: str) -> dict[str, Any]:
        normalized = _normalize_mode(mode)
        state = self._read_state()
        state["mode"] = normalized
        state["updated_at"] = _utc_now()
        self._write_state(state)
        self._append_audit(
            "mode.changed",
            "info",
            f"Autopilot mode set to {normalized}",
            {"mode": normalized},
        )
        append_event(
            self.project_dir,
            kind="autopilot.mode",
            severity="info",
            message=f"Autopilot mode set to {normalized}",
            details={"mode": normalized},
        )
        return {"ok": True, "mode": normalized, "refresh": True}

    def emergency_stop(self) -> dict[str, Any]:
        state = self._read_state()
        state["mode"] = "off"
        state["pending_decisions"] = []
        state["updated_at"] = _utc_now()
        state["emergency_stop_at"] = _utc_now()
        self._write_state(state)
        self._append_audit("emergency_stop", "warning", "Autopilot emergency stop", {})
        append_event(
            self.project_dir,
            kind="autopilot.emergency_stop",
            severity="warning",
            message="Autopilot emergency stop",
        )
        return {"ok": True, "mode": "off", "refresh": True}

    def tick(self, snapshot: dict[str, Any], executor: AutopilotExecutor) -> dict[str, Any]:
        state = self._read_state()
        policy = self._policy(state)
        incidents = self._classify(snapshot)
        budget = self._budget_status(policy)

        if policy.mode == "off":
            return {
                "ok": True,
                "mode": policy.mode,
                "message": "Autopilot is off",
                "executed": [],
                "pending": [],
                "blocked": [],
                "refresh": True,
            }

        decisions = [self._decision_for_incident(incident, policy) for incident in incidents]
        decisions = [decision for decision in decisions if decision is not None]
        executed: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []

        for decision in decisions:
            if decision["status"] == "blocked":
                blocked.append(decision)
                continue
            if policy.mode == "assisted":
                pending.append(decision)
                continue
            if not self._can_execute_decision(decision, policy, budget):
                decision = {
                    **decision,
                    "status": "blocked",
                    "reason": self._blocked_reason(decision, policy, budget),
                }
                blocked.append(decision)
                continue
            result = self._execute_decision(decision, policy, executor)
            executed.append(result)
            budget = self._budget_status(policy)

        if policy.mode == "assisted":
            self._store_pending_decisions(pending, incidents)
        else:
            running = [item for item in executed if item.get("status") == "running"]
            self._store_pending_decisions(running, incidents)

        state = self._read_state()
        state["last_tick_at"] = _utc_now()
        state["updated_at"] = _utc_now()
        self._write_state(state)
        return {
            "ok": True,
            "mode": policy.mode,
            "message": _tick_message(policy.mode, executed, pending, blocked),
            "executed": executed,
            "pending": pending,
            "blocked": blocked,
            "refresh": True,
        }

    def approve(self, decision_id: str, snapshot: dict[str, Any], executor: AutopilotExecutor) -> dict[str, Any]:
        state = self._read_state()
        policy = self._policy(state)
        incidents = self._classify(snapshot)
        pending = self._status_decisions(state, incidents, policy)
        decision = next((item for item in pending if item.get("id") == decision_id), None)
        if decision is None:
            return {"ok": False, "message": "Autopilot decision is no longer pending", "refresh": True}
        if decision.get("status") != "pending":
            return {"ok": False, "message": "Autopilot decision is not executable", "refresh": True}
        result = self._execute_decision(decision, policy, executor, approved=True)
        remaining = [item for item in pending if item.get("id") != decision_id]
        if result.get("status") == "running":
            remaining.append(result)
        self._store_pending_decisions(remaining, incidents)
        nested_result = _dict(result.get("result"))
        ok = result.get("status") in {"executed", "running"} and not bool(nested_result.get("ok") is False)
        return {
            **result,
            "ok": ok,
            "message": nested_result.get("message") or result.get("reason") or result.get("action_label"),
            "refresh": True,
        }

    def _classify(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        incidents: list[dict[str, Any]] = []
        watcher = _dict(snapshot.get("watcher"))
        health = _dict(watcher.get("health"))
        counts = _dict(watcher.get("counts"))
        runtime = _dict(snapshot.get("runtime"))
        supervisor = _dict(runtime.get("supervisor"))
        command_backlog = _dict(runtime.get("command_backlog"))
        landing = _dict(snapshot.get("landing"))
        live = _dict(snapshot.get("live"))
        history = _dict(snapshot.get("history"))

        watcher_state = str(health.get("state") or "stopped")
        queued_count = _int(counts.get("queued"))
        if watcher_state == "stale":
            can_stop_stale = bool(supervisor.get("can_stop"))
            incidents.append(
                _incident(
                    "stale_watcher",
                    "warning",
                    "Queue runner is stale",
                    (
                        str(health.get("next_action") or "Stop stale queue runner before starting another one.")
                        if can_stop_stale
                        else "Queue runner heartbeat is stale, but Mission Control cannot verify ownership. Stop it manually."
                    ),
                    action="stop_watcher" if can_stop_stale else "human_required",
                    target=str(health.get("blocking_pid") or health.get("watcher_pid") or ""),
                )
            )
        if queued_count > 0 and watcher_state == "stopped":
            incidents.append(
                _incident(
                    "queued_without_runner",
                    "info",
                    "Queued work is waiting",
                    f"{queued_count} queued task{'' if queued_count == 1 else 's'} will not start until the queue runner is running.",
                    action="start_watcher",
                    target="queue",
                )
            )
        if _int(command_backlog.get("processing")) > 0 and watcher_state != "running":
            incidents.append(
                _incident(
                    "stuck_command_backlog",
                    "warning",
                    "Command drain is stuck",
                    "Queue commands are in processing but the queue runner is not running.",
                    action="start_watcher" if bool(supervisor.get("can_start")) else "stop_watcher",
                    target="commands",
                )
            )
        if bool(landing.get("merge_blocked")):
            blockers = [str(item) for item in (landing.get("merge_blockers") or []) if item]
            text = " ".join(blockers).lower()
            if "merge in progress" in text or "unmerged path" in text:
                incidents.append(
                    _incident(
                        "merge_recovery_needed",
                        "warning",
                        "Landing recovery is needed",
                        "; ".join(blockers[:3]) or "A previous merge was interrupted.",
                        action="merge_recover",
                        target="landing",
                    )
                )
            else:
                incidents.append(
                    _incident(
                        "landing_blocked",
                        "warning",
                        "Landing is blocked",
                        "; ".join(blockers[:3]) or "Repository state blocks landing.",
                        action="human_required",
                        target="landing",
                    )
                )
        for item in list(live.get("items") or [])[:8]:
            if not isinstance(item, dict):
                continue
            status = str(item.get("display_status") or item.get("status") or "").lower()
            if status not in {"failed", "interrupted", "cancelled"}:
                continue
            run_id = str(item.get("run_id") or "")
            incidents.append(
                _incident(
                    f"run_{status}",
                    "warning",
                    f"Run {status}",
                    str(item.get("summary") or item.get("row_label") or run_id or "A run needs recovery."),
                    action="pilot_triage",
                    target=run_id,
                    run_id=run_id,
                    task_id=str(item.get("queue_task_id") or "") or None,
                    needs_pilot=True,
                )
            )

        live_recovery_run_ids = {
            str(incident.get("run_id") or "")
            for incident in incidents
            if str(incident.get("run_id") or "")
        }
        for item in list(landing.get("items") or [])[:12]:
            if not isinstance(item, dict):
                continue
            if item.get("superseded"):
                continue
            run_id = str(item.get("run_id") or "").strip()
            if run_id and run_id in live_recovery_run_ids:
                continue
            status = str(item.get("queue_status") or "").strip().lower()
            landing_state = str(item.get("landing_state") or "").strip().lower()
            if status in LANDING_IN_PROGRESS_STATUSES:
                continue
            if status not in {"failed", "interrupted", "cancelled", "stale"} and landing_state != "blocked":
                continue
            task_id = str(item.get("task_id") or "").strip()
            action = _landing_attention_action(item)
            title = _landing_attention_title(status=status, action=action)
            detail = _landing_attention_detail(item, status=status, action=action)
            follow_up_actions = ["start_watcher"] if action == "requeue" and watcher_state == "stopped" else []
            incidents.append(
                _incident(
                    f"landing_{status or landing_state or 'attention'}",
                    "warning",
                    title,
                    detail,
                    action=action,
                    target=run_id or task_id or str(item.get("branch") or "landing"),
                    run_id=run_id or None,
                    task_id=task_id or None,
                    needs_pilot=action == "pilot_triage",
                    follow_up_actions=follow_up_actions,
                )
            )

        for item in list(history.get("items") or [])[:12]:
            incident = self._merge_history_incident(item)
            if incident is not None:
                incidents.append(incident)
                break

        for log_path in self._large_log_candidates(runtime):
            incidents.append(
                _incident(
                    "large_runtime_log",
                    "warning",
                    "Runtime log is very large",
                    f"{log_path} is larger than {_format_bytes(LARGE_LOG_BYTES)}. Stop the owner before it grows further.",
                    action="stop_watcher" if watcher_state in {"running", "stale"} else "human_required",
                    target=str(log_path),
                )
            )

        return _dedupe_incidents(incidents)

    def _merge_history_incident(self, item: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        if str(item.get("domain") or "").lower() != "merge" and str(item.get("run_type") or "").lower() != "merge":
            return None
        status = str(item.get("status") or item.get("display_status") or "").strip().lower()
        if status not in {"failed", "interrupted"}:
            return None
        merge_id = str(item.get("merge_id") or item.get("run_id") or "").strip()
        if not merge_id:
            return None
        try:
            from otto.merge.state import load_state

            state = load_state(self.project_dir, merge_id)
        except Exception:
            return _incident(
                "merge_history_failed",
                "warning",
                "Review failed merge",
                str(item.get("summary") or "A merge failed and needs review."),
                action="human_required",
                target=merge_id,
                run_id=merge_id,
            )
        landed_statuses = {"merged", "conflict_resolved"}
        landed = [outcome for outcome in state.outcomes if outcome.status in landed_statuses]
        unresolved = [outcome for outcome in state.outcomes if outcome.status not in landed_statuses]
        if (
            str(state.status or "").strip().lower() in LANDING_IN_PROGRESS_STATUSES
            and landed
            and not unresolved
            and _merge_live_record_stale(self.project_dir, merge_id)
        ):
            return _incident(
                "merge_verification_stale",
                "warning",
                "Rerun stalled merge verification",
                (
                    f"Merge {merge_id} has landed code, but its verification rerun "
                    "is marked running after the runner disappeared. Rerun verification "
                    "against the current merged code."
                ),
                action="rerun_merge_verification",
                target=merge_id,
                run_id=merge_id,
            )
        if state.cert_passed is False and landed and not unresolved:
            return _incident(
                "merge_verification_failed",
                "warning",
                "Rerun merge verification",
                (
                    f"Merge {merge_id} landed {len(landed)} branch"
                    f"{'' if len(landed) == 1 else 'es'}, but post-merge certification failed. "
                    "Rerun verification against the current merged code."
                ),
                action="rerun_merge_verification",
                target=merge_id,
                run_id=merge_id,
            )
        return _incident(
            "merge_history_failed",
            "warning",
            "Review failed merge",
            state.note or str(item.get("summary") or "A merge failed and needs review."),
            action="human_required",
            target=merge_id,
            run_id=merge_id,
        )

    def _decision_for_incident(self, incident: dict[str, Any], policy: AutopilotPolicy) -> dict[str, Any] | None:
        action = str(incident.get("action") or "human_required")
        status = "pending"
        reason = str(incident.get("detail") or "")
        if action == "human_required":
            status = "blocked"
        if action == "merge_all" and not policy.allow_auto_land:
            status = "blocked" if policy.mode == "full" else "pending"
            reason = "Auto-land is disabled by Autopilot policy."
        decision_id = _stable_id("decision", incident.get("id"), action)
        if status == "pending" and _uses_repeat_guard(action) and self._decision_executed_recently(decision_id):
            status = "blocked"
            reason = "Autopilot already tried this recovery action recently."
        decision = {
            "id": decision_id,
            "incident_id": incident.get("id"),
            "status": status,
            "action": action,
            "action_label": _action_label(action),
            "title": incident.get("title"),
            "reason": reason,
            "severity": incident.get("severity"),
            "target": incident.get("target"),
            "run_id": incident.get("run_id"),
            "task_id": incident.get("task_id"),
            "requires_pilot": bool(incident.get("needs_pilot")),
            "created_at": _utc_now(),
        }
        return _with_recovery_plan(decision, incident)

    def _execute_decision(
        self,
        decision: dict[str, Any],
        policy: AutopilotPolicy,
        executor: AutopilotExecutor,
        *,
        approved: bool = False,
    ) -> dict[str, Any]:
        action = str(decision.get("action") or "")
        if action == "pilot_triage":
            return self._execute_pilot_triage(decision, policy, approved=approved)
        try:
            if action == "start_watcher":
                payload = executor.start_watcher()
            elif action == "stop_watcher":
                payload = executor.stop_watcher()
            elif action == "merge_recover":
                payload = executor.merge_recover()
            elif action == "merge_all":
                payload = executor.merge_all(verification_policy=policy.verification_policy)
            elif action == "rerun_merge_verification":
                merge_id = str(decision.get("run_id") or decision.get("target") or "").strip()
                if not merge_id:
                    payload = {"ok": False, "message": "merge id missing for verification rerun"}
                else:
                    payload = executor.rerun_merge_verification(
                        merge_id,
                        verification_policy=policy.verification_policy,
                    )
            elif action == "resolve_release":
                payload = executor.resolve_release_issues()
            elif action == "requeue":
                run_id = str(decision.get("run_id") or "").strip()
                if not run_id:
                    payload = {"ok": False, "message": "run id missing for requeue"}
                else:
                    payload = self._execute_requeue_plan(decision, executor, run_id)
            else:
                payload = {"ok": False, "message": f"unsupported Autopilot action {action!r}"}
        except Exception as exc:  # pragma: no cover - defensive wrapper
            payload = {"ok": False, "message": str(exc)}
        status = "executed" if payload.get("ok") else "failed"
        result = {
            **decision,
            "status": status,
            "executed_at": _utc_now(),
            "approved": approved,
            "result": payload,
        }
        self._append_audit(
            f"decision.{status}",
            "success" if payload.get("ok") else "warning",
            str(payload.get("message") or _action_label(action)),
            {"decision": result},
        )
        append_event(
            self.project_dir,
            kind=f"autopilot.{status}",
            severity="success" if payload.get("ok") else "warning",
            message=str(payload.get("message") or _action_label(action)),
            run_id=str(decision.get("run_id") or "") or None,
            task_id=str(decision.get("task_id") or "") or None,
            details={"decision_id": decision.get("id"), "action": action, "approved": approved},
        )
        self._increment_counter("actions_executed" if payload.get("ok") else "actions_failed")
        return result

    def _execute_requeue_plan(
        self,
        decision: dict[str, Any],
        executor: AutopilotExecutor,
        run_id: str,
    ) -> dict[str, Any]:
        requeue_payload = executor.execute(run_id, "requeue")
        steps = [
            {
                "action": "requeue",
                "label": "Requeue task",
                "ok": bool(requeue_payload.get("ok")),
                "message": str(requeue_payload.get("message") or ""),
            }
        ]
        if not requeue_payload.get("ok"):
            return {
                **requeue_payload,
                "steps": steps,
                "message": str(requeue_payload.get("message") or "Requeue failed."),
            }
        if "start_watcher" not in _string_list(decision.get("chain_actions")):
            return {**requeue_payload, "steps": steps}

        try:
            watcher_payload = executor.start_watcher()
        except Exception as exc:  # pragma: no cover - defensive wrapper
            watcher_payload = {"ok": False, "message": str(exc)}
        steps.append(
            {
                "action": "start_watcher",
                "label": "Start queue runner",
                "ok": bool(watcher_payload.get("ok")),
                "message": str(watcher_payload.get("message") or ""),
            }
        )
        ok = bool(requeue_payload.get("ok")) and bool(watcher_payload.get("ok"))
        if ok:
            message = "Requeued task and started the queue runner."
        else:
            message = str(watcher_payload.get("message") or "Requeued task, but could not start the queue runner.")
        return {
            "ok": ok,
            "message": message,
            "refresh": True,
            "steps": steps,
            "requeue": requeue_payload,
            "start_watcher": watcher_payload,
        }

    def _execute_pilot_triage(
        self,
        decision: dict[str, Any],
        policy: AutopilotPolicy,
        *,
        approved: bool,
    ) -> dict[str, Any]:
        if not policy.pilot_enabled:
            result = {**decision, "status": "blocked", "reason": "Pilot agent is disabled by policy."}
            self._append_audit("pilot.blocked", "warning", "Pilot agent disabled", {"decision": decision})
            return result
        decision_id = str(decision.get("id") or "")
        if self._decision_is_running(decision_id):
            return {
                **decision,
                "status": "running",
                "approved": approved,
                "result": {"ok": True, "message": "Pilot is already diagnosing this task."},
            }
        running = {
            **decision,
            "status": "running",
            "approved": approved,
            "started_at": _utc_now(),
            "result": {"ok": True, "message": "Pilot diagnosis started."},
        }
        self._append_audit("pilot.started", "info", "Pilot diagnosis started", {"decision": running})
        append_event(
            self.project_dir,
            kind="autopilot.pilot.started",
            severity="info",
            message="Pilot diagnosis started",
            run_id=str(decision.get("run_id") or "") or None,
            task_id=str(decision.get("task_id") or "") or None,
            details={"decision_id": decision_id, "approved": approved},
        )
        self._start_pilot_background(running, policy, approved=approved)
        return running

    def _start_pilot_background(self, decision: dict[str, Any], policy: AutopilotPolicy, *, approved: bool) -> None:
        thread = threading.Thread(
            target=self._run_pilot_triage_background,
            args=(decision, policy, approved),
            name=f"otto-autopilot-pilot-{str(decision.get('id') or '')[:8]}",
            daemon=True,
        )
        thread.start()

    def _run_pilot_triage_background(self, decision: dict[str, Any], policy: AutopilotPolicy, approved: bool) -> None:
        decision_id = str(decision.get("id") or "")
        try:
            pilot_plan = self._call_pilot(decision, policy)
        except Exception as exc:
            result = {**decision, "status": "failed", "reason": f"Pilot failed: {exc}"}
            self._append_audit("pilot.failed", "warning", str(exc), {"decision": decision})
            self._increment_counter("actions_failed")
            self._remove_pending_decision(decision_id)
            return

        pilot_action = str(pilot_plan.get("action") or "noop")
        if pilot_action not in PILOT_ACTION_ALLOWLIST:
            result = {
                **decision,
                "status": "blocked",
                "reason": f"Pilot requested unsupported action {pilot_action!r}.",
                "pilot_plan": pilot_plan,
            }
            self._append_audit("pilot.blocked", "warning", str(result["reason"]), {"decision": decision, "pilot_plan": pilot_plan})
            self._remove_pending_decision(decision_id)
            return
        if pilot_action == "noop":
            result = {**decision, "status": "executed", "pilot_plan": pilot_plan, "result": {"ok": True, "message": "Pilot recommended no action."}}
            self._append_audit("pilot.noop", "info", "Pilot recommended no action", {"decision": decision, "pilot_plan": pilot_plan})
            self._increment_counter("actions_executed")
            self._remove_pending_decision(decision_id)
            return
        nested = {**decision, "action": pilot_action, "action_label": _action_label(pilot_action), "requires_pilot": False}
        if policy.mode != "full":
            suggestion = {
                **nested,
                "status": "pending",
                "reason": str(pilot_plan.get("reason") or f"Pilot recommends {_action_label(pilot_action)}."),
                "rationale": str(pilot_plan.get("reason") or ""),
                "pilot_plan": pilot_plan,
                "requires_pilot": False,
                "created_at": _utc_now(),
            }
            self._replace_pending_decision(decision_id, suggestion)
            self._append_audit(
                "pilot.action_proposed",
                "info",
                f"Pilot proposed {_action_label(pilot_action)}",
                {"decision": suggestion, "pilot_plan": pilot_plan},
            )
            append_event(
                self.project_dir,
                kind="autopilot.pilot.proposed",
                severity="info",
                message=f"Pilot proposed {_action_label(pilot_action)}",
                run_id=str(decision.get("run_id") or "") or None,
                task_id=str(decision.get("task_id") or "") or None,
                details={"decision_id": decision_id, "action": pilot_action},
            )
            return
        service_module = import_module("otto.mission_control.service")
        executor = cast(AutopilotExecutor, service_module.MissionControlService(self.project_dir))
        result = self._execute_decision(nested, policy, executor, approved=approved)
        result["pilot_plan"] = pilot_plan
        self._append_audit("pilot.action_selected", "info", f"Pilot selected {_action_label(pilot_action)}", {"decision": decision, "pilot_plan": pilot_plan, "result": result})
        self._remove_pending_decision(decision_id)

    def _call_pilot(self, decision: dict[str, Any], policy: AutopilotPolicy) -> dict[str, Any]:
        self._append_audit("pilot.requested", "info", "Pilot triage requested", {"decision": decision})
        plan = asyncio.run(self._call_pilot_async(decision, policy))
        self._append_audit("pilot.completed", "info", "Pilot triage completed", {"decision": decision, "pilot_plan": plan})
        return plan

    async def _call_pilot_async(self, decision: dict[str, Any], policy: AutopilotPolicy) -> dict[str, Any]:
        from otto.agent import make_agent_options, run_agent_with_timeout

        config = _load_config_best_effort(self.project_dir)
        prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "autopilot-pilot.md"
        template = prompt_path.read_text(encoding="utf-8")
        prompt = template.replace("{{DECISION_JSON}}", json.dumps(decision, indent=2, sort_keys=True, default=str))
        prompt = prompt.replace("{{ALLOWED_ACTIONS_JSON}}", json.dumps(sorted(PILOT_ACTION_ALLOWLIST), indent=2))
        options = make_agent_options(
            self.project_dir,
            config,
            agent_type="fix",
            max_turns=min(int(config.get("max_turns_per_call") or 40), 40),
            max_subagent_dispatches=0,
        )
        options.effort = _pilot_effort(config)
        text, _cost, _session, _breakdown = await run_agent_with_timeout(
            prompt,
            options,
            log_dir=paths.logs_dir(self.project_dir) / "mission-control" / "autopilot-pilot",
            phase_name="AUTOPILOT",
            timeout=min(policy.pilot_timeout_s, 180),
            project_dir=self.project_dir,
            capture_tool_output=False,
        )
        return _parse_pilot_json(text)

    def _can_execute_decision(
        self,
        decision: dict[str, Any],
        policy: AutopilotPolicy,
        budget: dict[str, Any],
    ) -> bool:
        action = str(decision.get("action") or "")
        if action not in SAFE_FULL_ACTIONS:
            return False
        if _uses_repeat_guard(action) and self._decision_executed_recently(str(decision.get("id") or "")):
            return False
        if _int(budget.get("remaining_actions")) <= 0:
            return False
        if bool(decision.get("requires_pilot")) and _int(budget.get("remaining_pilot_calls")) <= 0:
            return False
        if action == "merge_all" and not policy.allow_auto_land:
            return False
        return True

    def _blocked_reason(self, decision: dict[str, Any], policy: AutopilotPolicy, budget: dict[str, Any]) -> str:
        action = str(decision.get("action") or "")
        if action not in SAFE_FULL_ACTIONS:
            return f"Autopilot cannot execute {action or 'this action'} automatically."
        if _uses_repeat_guard(action) and self._decision_executed_recently(str(decision.get("id") or "")):
            return "Autopilot already tried this recovery action recently."
        if _int(budget.get("remaining_actions")) <= 0:
            return "Autopilot hourly action budget is exhausted."
        if bool(decision.get("requires_pilot")) and _int(budget.get("remaining_pilot_calls")) <= 0:
            return "Autopilot hourly Pilot-agent budget is exhausted."
        if action == "merge_all" and not policy.allow_auto_land:
            return "Auto-land is disabled by Autopilot policy."
        return "Autopilot policy blocked this action."

    def _policy(self, state: dict[str, Any]) -> AutopilotPolicy:
        config = _load_config_best_effort(self.project_dir)
        raw_config = config.get("autopilot")
        raw: dict[str, Any] = raw_config if isinstance(raw_config, dict) else {}
        configured_mode = _normalize_mode(raw.get("mode") if raw.get("mode") is not None else "assisted")
        mode = _normalize_mode(state.get("mode") if state.get("mode") is not None else configured_mode)
        max_actions = _bounded_int(raw.get("max_actions_per_hour"), 8, lower=0, upper=100)
        max_pilot = _bounded_int(raw.get("max_pilot_calls_per_hour"), 2, lower=0, upper=20)
        allow_auto_land_raw = raw.get("allow_auto_land")
        allow_auto_land = False if allow_auto_land_raw is None else bool(allow_auto_land_raw)
        try:
            verification_policy = normalize_verification_policy(str(raw.get("verification_policy") or "smart"))
        except ValueError:
            verification_policy = "smart"
        return AutopilotPolicy(
            mode=mode,
            configured_mode=configured_mode,
            max_actions_per_hour=max_actions,
            max_pilot_calls_per_hour=max_pilot,
            allow_auto_land=allow_auto_land,
            verification_policy=verification_policy,
            pilot_enabled=bool(raw.get("pilot_enabled", True)),
            pilot_timeout_s=_bounded_int(raw.get("pilot_timeout_s"), 300, lower=30, upper=1800),
        )

    def _budget_status(self, policy: AutopilotPolicy) -> dict[str, Any]:
        since = time.time() - AUTOPILOT_RATE_LIMIT_WINDOW_S
        action_count = 0
        pilot_count = 0
        for row in _read_audit_rows(autopilot_events_path(self.project_dir)):
            created = _parse_time(row.get("created_at"))
            if created is None or created < since:
                continue
            kind = str(row.get("kind") or "")
            if kind.startswith("decision.executed") or kind.startswith("decision.failed"):
                action_count += 1
            if kind.startswith("pilot.requested"):
                pilot_count += 1
        return {
            "window_seconds": AUTOPILOT_RATE_LIMIT_WINDOW_S,
            "max_actions_per_hour": policy.max_actions_per_hour,
            "actions_used": action_count,
            "remaining_actions": max(0, policy.max_actions_per_hour - action_count),
            "max_pilot_calls_per_hour": policy.max_pilot_calls_per_hour,
            "pilot_calls_used": pilot_count,
            "remaining_pilot_calls": max(0, policy.max_pilot_calls_per_hour - pilot_count),
        }

    def _status_payload(
        self,
        *,
        policy: AutopilotPolicy,
        incidents: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        budget: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        if policy.mode == "off":
            health = "off"
        elif any(item.get("severity") == "error" for item in incidents):
            health = "blocked"
        elif incidents:
            health = "attention"
        else:
            health = "idle"
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": policy.mode,
            "configured_mode": policy.configured_mode,
            "health": health,
            "enabled": policy.mode != "off",
            "next_tick_hint": _next_tick_hint(policy.mode, incidents, decisions),
            "policy": {
                "mode": policy.mode,
                "max_actions_per_hour": policy.max_actions_per_hour,
                "max_pilot_calls_per_hour": policy.max_pilot_calls_per_hour,
                "allow_auto_land": policy.allow_auto_land,
                "verification_policy": policy.verification_policy,
                "pilot_enabled": policy.pilot_enabled,
                "pilot_timeout_s": policy.pilot_timeout_s,
            },
            "pilot_agent": _pilot_agent_status(self.project_dir),
            "budgets": {
                **budget,
                "actions_used_last_hour": budget.get("actions_used", 0),
                "actions_limit_per_hour": budget.get("max_actions_per_hour", 0),
                "pilot_calls_used_last_hour": budget.get("pilot_calls_used", 0),
                "pilot_calls_limit_per_hour": budget.get("max_pilot_calls_per_hour", 0),
            },
            "counters": {
                "incidents_open": len(incidents),
                "decisions_pending": len([item for item in decisions if item.get("status") in ACTIVE_DECISION_STATUSES]),
                "actions_executed": _int(state.get("actions_executed")),
                "actions_failed": _int(state.get("actions_failed")),
            },
            "incidents": incidents,
            "decisions": decisions,
            "pending_decisions": [item for item in decisions if item.get("status") in ACTIVE_DECISION_STATUSES],
            "recent_events": _read_audit_rows(autopilot_events_path(self.project_dir), limit=12)[::-1],
            "last_tick_at": state.get("last_tick_at"),
            "state_path": str(autopilot_state_path(self.project_dir).resolve(strict=False)),
            "events_path": str(autopilot_events_path(self.project_dir).resolve(strict=False)),
        }

    def _read_state(self) -> dict[str, Any]:
        path = autopilot_state_path(self.project_dir)
        if not path.exists():
            return {"schema_version": SCHEMA_VERSION, "pending_decisions": []}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": SCHEMA_VERSION, "pending_decisions": []}
        if not isinstance(value, dict):
            return {"schema_version": SCHEMA_VERSION, "pending_decisions": []}
        value.setdefault("schema_version", SCHEMA_VERSION)
        value.setdefault("pending_decisions", [])
        return value

    def _write_state(self, state: dict[str, Any]) -> None:
        path = autopilot_state_path(self.project_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(state)
        payload["schema_version"] = SCHEMA_VERSION
        payload["updated_at"] = _utc_now()
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def _increment_counter(self, key: str) -> None:
        if key not in {"actions_executed", "actions_failed"}:
            return
        state = self._read_state()
        state[key] = _int(state.get(key)) + 1
        self._write_state(state)

    def _pending_decisions(self, state: dict[str, Any], incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        active_ids = {str(item.get("id") or "") for item in incidents}
        out: list[dict[str, Any]] = []
        for item in state.get("pending_decisions") or []:
            if not isinstance(item, dict):
                continue
            if item.get("status") not in DECISION_STATUSES:
                item["status"] = "pending"
            if str(item.get("incident_id") or "") in active_ids and item.get("status") in ACTIVE_DECISION_STATUSES:
                out.append(item)
        return out

    def _store_pending_decisions(self, decisions: list[dict[str, Any]], incidents: list[dict[str, Any]]) -> None:
        active_ids = {str(item.get("id") or "") for item in incidents}
        existing = self._pending_decisions(self._read_state(), incidents)
        by_id: dict[str, dict[str, Any]] = {
            str(item.get("id")): item
            for item in existing
            if str(item.get("incident_id") or "") in active_ids
        }
        for decision in decisions:
            if decision.get("status") not in ACTIVE_DECISION_STATUSES:
                continue
            by_id[str(decision.get("id"))] = decision
        state = self._read_state()
        state["pending_decisions"] = list(by_id.values())
        self._write_state(state)

    def _decision_is_running(self, decision_id: str) -> bool:
        if not decision_id:
            return False
        for item in self._read_state().get("pending_decisions") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "") == decision_id and item.get("status") == "running":
                return True
        return False

    def _remove_pending_decision(self, decision_id: str) -> None:
        if not decision_id:
            return
        state = self._read_state()
        state["pending_decisions"] = [
            item
            for item in state.get("pending_decisions") or []
            if not isinstance(item, dict) or str(item.get("id") or "") != decision_id
        ]
        self._write_state(state)

    def _replace_pending_decision(self, decision_id: str, replacement: dict[str, Any]) -> None:
        state = self._read_state()
        items = [
            item
            for item in state.get("pending_decisions") or []
            if not isinstance(item, dict) or str(item.get("id") or "") != decision_id
        ]
        if replacement.get("status") in ACTIVE_DECISION_STATUSES:
            replacement = {**replacement, "id": decision_id or str(replacement.get("id") or "")}
            items.append(replacement)
        state["pending_decisions"] = items
        self._write_state(state)

    def _append_audit(self, kind: str, severity: str, message: str, details: dict[str, Any]) -> None:
        path = autopilot_events_path(self.project_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "schema_version": SCHEMA_VERSION,
            "event_id": f"{time.time_ns()}-{os.getpid()}",
            "created_at": _utc_now(),
            "kind": kind,
            "severity": severity,
            "message": message,
            "details": details,
        }
        lock_path = paths.sidecar_lock_path(path)
        with lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _large_log_candidates(self, runtime: dict[str, Any]) -> list[Path]:
        candidates: list[Path] = []
        supervisor = _dict(runtime.get("supervisor"))
        for raw in (
            supervisor.get("watcher_log_path"),
            paths.logs_dir(self.project_dir) / "queue" / "watcher.log",
            paths.logs_dir(self.project_dir) / "web" / "watcher.log",
        ):
            if not raw:
                continue
            path = Path(str(raw)).expanduser()
            try:
                if path.exists() and path.stat().st_size >= LARGE_LOG_BYTES:
                    candidates.append(path)
            except OSError:
                continue
        seen: set[str] = set()
        unique: list[Path] = []
        for path in candidates:
            key = str(path.resolve(strict=False))
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)
        return unique

    def _status_decisions(
        self,
        state: dict[str, Any],
        incidents: list[dict[str, Any]],
        policy: AutopilotPolicy,
    ) -> list[dict[str, Any]]:
        active = self._pending_decisions(state, incidents)
        if policy.mode != "assisted":
            return active
        by_id = {str(item.get("id") or ""): item for item in active}
        for incident in incidents:
            decision = self._decision_for_incident(incident, policy)
            if decision is None:
                continue
            decision_id = str(decision.get("id") or "")
            existing = by_id.get(decision_id)
            if existing and existing.get("status") == "running":
                continue
            if existing and existing.get("status") == "pending" and existing.get("pilot_plan"):
                continue
            if existing and existing.get("status") == "pending":
                decision = {
                    **decision,
                    "created_at": existing.get("created_at") or decision.get("created_at"),
                }
            by_id[decision_id] = decision
        return list(by_id.values())

    def _decision_executed_recently(self, decision_id: str) -> bool:
        if not decision_id:
            return False
        for row in _read_audit_rows(autopilot_events_path(self.project_dir), limit=500):
            if str(row.get("kind") or "") != "decision.executed":
                continue
            details = _dict(row.get("details"))
            decision = _dict(details.get("decision"))
            if str(decision.get("id") or "") == decision_id:
                return True
        return False


def _incident(
    kind: str,
    severity: str,
    title: str,
    detail: str,
    *,
    action: str,
    target: str,
    run_id: str | None = None,
    task_id: str | None = None,
    needs_pilot: bool = False,
    follow_up_actions: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": _stable_id("incident", kind, target, run_id, task_id),
        "kind": kind,
        "severity": severity,
        "title": title,
        "detail": detail,
        "action": action,
        "target": target,
        "run_id": run_id,
        "task_id": task_id,
        "needs_pilot": needs_pilot,
        "follow_up_actions": list(follow_up_actions or []),
    }


def _stable_id(prefix: str, *parts: Any) -> str:
    text = "|".join(str(part or "") for part in parts)
    return f"{prefix}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"


def _dedupe_incidents(incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for incident in incidents:
        key = str(incident.get("id") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(incident)
    return out


def _landing_attention_action(item: dict[str, Any]) -> str:
    status = str(item.get("queue_status") or "").strip().lower()
    run_id = str(item.get("run_id") or "").strip()
    changed_count = _int(item.get("changed_file_count"))
    family = str(_dict(item.get("build_config")).get("command_family") or "").strip().lower()
    if run_id and status in {"interrupted", "cancelled"} and changed_count == 0:
        return "requeue"
    if run_id and status == "failed" and changed_count == 0 and family in {"certify", "build", "improve"}:
        return "requeue"
    return "pilot_triage" if run_id else "human_required"


def _landing_attention_title(*, status: str, action: str) -> str:
    if action == "requeue":
        if status == "interrupted":
            return "Requeue interrupted task"
        if status == "cancelled":
            return "Requeue cancelled task"
        return "Requeue failed task"
    if action == "pilot_triage":
        return "Diagnose task"
    return "Task needs review"


def _landing_attention_detail(item: dict[str, Any], *, status: str, action: str) -> str:
    raw_summary = str(item.get("summary") or item.get("task_id") or "This task").strip()
    summary = raw_summary.rstrip(". ")
    if action == "requeue":
        state = status or "blocked"
        state_phrase = {
            "failed": "failed",
            "interrupted": "was interrupted",
            "cancelled": "was cancelled",
        }.get(state, f"was {state}")
        return (
            f"{summary} {state_phrase} before verified changes were ready to land. "
            "Requeue it to get a fresh run."
        )
    if action == "pilot_triage":
        return f"{summary} needs a quick diagnosis before Otto chooses the recovery action."
    return f"{summary} needs manual review before Otto can continue."


def _with_recovery_plan(decision: dict[str, Any], incident: dict[str, Any]) -> dict[str, Any]:
    action = str(decision.get("action") or "")
    follow_up_actions = _string_list(incident.get("follow_up_actions"))
    includes_actions = [action, *[item for item in follow_up_actions if item != action]]
    if decision.get("status") != "pending":
        return {
            **decision,
            "includes_actions": includes_actions,
        }
    if action == "requeue" and "start_watcher" in follow_up_actions:
        title = _requeue_plan_title(str(incident.get("title") or ""))
        step_label = _requeue_step_label(str(incident.get("title") or ""))
        reason = str(decision.get("reason") or "").rstrip()
        plan_reason = (
            f"{reason} Otto will also start the queue runner so the retry actually begins."
            if reason
            else "Otto will requeue the interrupted task and start the queue runner so the retry actually begins."
        )
        return {
            **decision,
            "title": title,
            "action_label": "Recover task",
            "reason": plan_reason,
            "includes_actions": includes_actions,
            "chain_actions": follow_up_actions,
            "plan_steps": [
                {
                    "action": "requeue",
                    "label": step_label,
                    "status": "pending",
                    "detail": "Create a fresh queued run from the original task definition.",
                },
                {
                    "action": "start_watcher",
                    "label": "Start queue runner",
                    "status": "pending",
                    "detail": "Start queue processing so the retry does not sit paused.",
                },
                {
                    "action": "watch_retry",
                    "label": "Watch retry",
                    "status": "pending",
                    "detail": "Refresh state and replace the old attempt once the retry completes.",
                },
            ],
        }
    if action == "start_watcher":
        return {
            **decision,
            "includes_actions": includes_actions,
            "plan_steps": [
                {
                    "action": "start_watcher",
                    "label": "Start queue runner",
                    "status": "pending",
                    "detail": "Start queue processing for queued work.",
                }
            ],
        }
    if action == "rerun_merge_verification":
        return {
            **decision,
            "includes_actions": includes_actions,
            "plan_steps": [
                {
                    "action": "rerun_merge_verification",
                    "label": "Rerun merge verification",
                    "status": "pending",
                    "detail": "Run the post-merge certifier again against the already-landed code.",
                }
            ],
        }
    return {
        **decision,
        "includes_actions": includes_actions,
    }


def _requeue_plan_title(title: str) -> str:
    lowered = title.lower()
    if "interrupted" in lowered:
        return "Recover interrupted task"
    if "cancelled" in lowered:
        return "Recover cancelled task"
    if "failed" in lowered:
        return "Recover failed task"
    return "Recover task"


def _requeue_step_label(title: str) -> str:
    lowered = title.lower()
    if "interrupted" in lowered:
        return "Requeue interrupted task"
    if "cancelled" in lowered:
        return "Requeue cancelled task"
    if "failed" in lowered:
        return "Requeue failed task"
    return "Requeue task"


def _normalize_mode(value: Any) -> str:
    mode = str(value or "assisted").strip().lower()
    return mode if mode in AUTOPILOT_MODES else "assisted"


def _load_config_best_effort(project_dir: Path) -> dict[str, Any]:
    try:
        return load_config(Path(project_dir) / "otto.yaml")
    except (ConfigError, ValueError) as exc:
        logger.warning("autopilot ignoring unreadable config for %s: %s", project_dir, exc)
        return {}


def _pilot_agent_status(project_dir: Path) -> dict[str, Any]:
    from otto.config import agent_provider, effective_agent_model

    config = _load_config_best_effort(project_dir)
    return {
        "agent_type": "diagnostic",
        "provider": agent_provider(config, "fix"),
        "model": effective_agent_model(config, "fix") or "provider default",
        "reasoning_effort": _pilot_effort(config),
    }


def _pilot_effort(config: dict[str, Any]) -> str:
    raw_config = config.get("autopilot")
    autopilot: dict[str, Any] = raw_config if isinstance(raw_config, dict) else {}
    raw = str(autopilot.get("pilot_effort") or autopilot.get("pilot_reasoning_effort") or "").strip().lower()
    if raw in {"low", "medium"}:
        return raw
    if raw in {"high", "xhigh"}:
        return "medium"
    return "low"


def _read_audit_rows(path: Path, *, limit: int = 200) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines[-limit:]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _parse_time(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _parse_pilot_json(text: str) -> dict[str, Any]:
    stripped = str(text or "").strip()
    if not stripped:
        raise ValueError("Pilot returned no output")
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Pilot did not return a JSON object")
    value = json.loads(stripped[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("Pilot JSON must be an object")
    return value


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _merge_live_record_stale(project_dir: Path, merge_id: str) -> bool:
    try:
        from otto.runs.registry import load_live_record, writer_identity_gone_or_stale
        from otto.runs.schema import is_terminal_status

        record = load_live_record(project_dir, merge_id)
    except Exception as exc:
        logger.warning(
            "merge live-record load failed for %s (%s: %s); treating as stale",
            merge_id, type(exc).__name__, exc,
        )
        return True
    if is_terminal_status(record.status):
        return True
    try:
        return writer_identity_gone_or_stale(record.writer)
    except Exception as exc:
        logger.warning(
            "writer-identity check failed for %s (%s: %s); treating as stale",
            merge_id, type(exc).__name__, exc,
        )
        return True


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _bounded_int(value: Any, default: int, *, lower: int, upper: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(lower, min(upper, number))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _action_label(action: str) -> str:
    return {
        "start_watcher": "Start queue runner",
        "stop_watcher": "Stop stale queue runner",
        "merge_recover": "Recover landing",
        "merge_all": "Land ready work",
        "rerun_merge_verification": "Rerun merge verification",
        "resolve_release": "Resolve release issues",
        "pilot_triage": "Diagnose issue",
        "requeue": "Requeue task",
        "human_required": "Needs human review",
        "noop": "No action",
    }.get(action, action.replace("_", " ").strip().title() or "Autopilot action")


def _uses_repeat_guard(action: str) -> bool:
    return action not in {"start_watcher"}


def _tick_message(
    mode: str,
    executed: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    blocked: list[dict[str, Any]],
) -> str:
    if executed:
        return f"Autopilot executed {len(executed)} recovery action{'' if len(executed) == 1 else 's'}."
    if pending:
        return f"Autopilot found {len(pending)} suggested action{'' if len(pending) == 1 else 's'}."
    if blocked:
        return f"Autopilot found {len(blocked)} blocked issue{'' if len(blocked) == 1 else 's'}."
    return f"Autopilot {mode} found no recovery work."


def _next_tick_hint(mode: str, incidents: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> str:
    if mode == "off":
        return "Paused until you enable Ask first or Auto."
    running = len([item for item in decisions if item.get("status") == "running"])
    if running:
        return f"{running} Pilot diagnosis in progress."
    pending = len([item for item in decisions if item.get("status") == "pending"])
    if pending:
        return f"{pending} recovery decision{'' if pending == 1 else 's'} awaiting approval."
    if incidents:
        return f"Watching {len(incidents)} issue{'' if len(incidents) == 1 else 's'} and applying policy."
    return "Scanning for stuck runs, runner problems, merge issues, and landing blockers."


def _format_bytes(value: int) -> str:
    if value >= 1024 * 1024 * 1024:
        return f"{value / (1024 * 1024 * 1024):.1f} GB"
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.0f} MB"
    return f"{value} B"


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
