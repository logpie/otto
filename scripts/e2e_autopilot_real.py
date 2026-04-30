"""Real-project E2E checks for Mission Control Autopilot.

This script is intentionally opt-in because the Pilot scenario calls a real
LLM through the configured Otto provider.

Usage:
    OTTO_ALLOW_REAL_COST=1 .venv/bin/python scripts/e2e_autopilot_real.py \
      --project /Users/yuxuan/otto-projects/acme-expense-portal
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from real_cost_guard import require_real_cost_opt_in  # noqa: E402

from otto.mission_control.autopilot import AutopilotController, autopilot_events_path  # noqa: E402
from otto.mission_control.service import MissionControlService  # noqa: E402


def _log(message: str) -> None:
    print(f"[autopilot-e2e] {message}", flush=True)


def _fail(message: str) -> None:
    _log(f"FAIL: {message}")
    raise SystemExit(1)


def _check(condition: bool, message: str) -> None:
    if not condition:
        _fail(message)
    _log(f"OK: {message}")


def _snapshot_for_run(*, run_id: str, task_id: str, summary: str, status: str = "interrupted") -> dict[str, Any]:
    return {
        "watcher": {"health": {"state": "stopped"}, "counts": {"queued": 0, "running": 0}},
        "runtime": {
            "command_backlog": {"processing": 0},
            "supervisor": {"can_start": True, "can_stop": False},
        },
        "landing": {"merge_blocked": False, "counts": {"ready": 0}, "items": []},
        "live": {
            "items": [{
                "display_status": status,
                "run_id": run_id,
                "summary": summary,
                "queue_task_id": task_id,
            }]
        },
    }


def _landing_snapshot_for_interrupted_task(*, run_id: str, task_id: str, summary: str) -> dict[str, Any]:
    return {
        "watcher": {"health": {"state": "stopped"}, "counts": {"queued": 0, "running": 0}},
        "runtime": {
            "command_backlog": {"processing": 0},
            "supervisor": {"can_start": True, "can_stop": False},
        },
        "landing": {
            "merge_blocked": False,
            "counts": {"ready": 0, "blocked": 1},
            "items": [{
                "task_id": task_id,
                "run_id": run_id,
                "summary": summary,
                "build_config": {"command_family": "certify"},
                "queue_status": "interrupted",
                "landing_state": "blocked",
                "changed_file_count": 0,
            }],
        },
        "live": {"items": []},
    }


def _decision_events(project: Path, decision_id: str) -> list[dict[str, Any]]:
    path = autopilot_events_path(project)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        decision = details.get("decision") if isinstance(details.get("decision"), dict) else {}
        if row.get("decision_id") == decision_id or decision.get("id") == decision_id:
            rows.append(row)
    return rows


def _event_decision(row: dict[str, Any]) -> dict[str, Any]:
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    return details.get("decision") if isinstance(details.get("decision"), dict) else {}


def _cleanup_synthetic_events(project: Path, decision_ids: set[str]) -> None:
    path = autopilot_events_path(project)
    if not path.exists():
        return
    original = path.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    removed = 0
    for line in original:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        decision = _event_decision(row)
        decision_id = str(decision.get("id") or row.get("decision_id") or "")
        run_id = str(decision.get("run_id") or "")
        task_id = str(decision.get("task_id") or "")
        synthetic = (
            decision_id in decision_ids
            or run_id.startswith("autopilot-e2e-")
            or task_id.startswith("autopilot-e2e-")
            or str(row.get("message") or "") == "synthetic requeue"
        )
        if synthetic:
            removed += 1
            continue
        kept.append(line)
    if removed:
        path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        _log(f"cleaned {removed} synthetic Autopilot audit event{'' if removed == 1 else 's'}")


def check_blocked_recent_retry(controller: AutopilotController) -> None:
    """Verify a recent retry is presented as blocked review, not a fake CTA."""
    snapshot = _landing_snapshot_for_interrupted_task(
        run_id="autopilot-e2e-retried-run",
        task_id="autopilot-e2e-retried-task",
        summary="Synthetic interrupted certification for Autopilot retry-state testing.",
    )
    pending = controller.status(snapshot)["pending_decisions"]
    _check(len(pending) == 1 and pending[0]["action"] == "requeue", "fresh interrupted task proposes requeue")
    try:
        controller._append_audit("decision.executed", "success", "synthetic requeue", {"decision": pending[0]})

        status = controller.status(snapshot)
        _check(status["pending_decisions"] == [], "recently executed requeue is not actionable again")
        _check(status["decisions"][0]["status"] == "blocked", "recently executed requeue appears as blocked review")
        _check("already tried" in status["decisions"][0]["reason"].lower(), "blocked review explains recent retry")
    finally:
        _cleanup_synthetic_events(controller.project_dir, {str(pending[0]["id"])})


def check_real_pilot(project: Path, controller: AutopilotController) -> None:
    """Run the real Pilot agent on a real project with a synthetic failed-run snapshot."""
    run_id = "autopilot-e2e-pilot-run"
    task_id = "autopilot-e2e-pilot-task"
    summary = "Synthetic read-only certification was interrupted. Do not change code."
    snapshot = _snapshot_for_run(run_id=run_id, task_id=task_id, summary=summary)
    status = controller.status(snapshot)
    _check(status["pilot_agent"]["reasoning_effort"] == "low", "Pilot uses low reasoning effort")
    decision = next((item for item in status["pending_decisions"] if item.get("action") == "pilot_triage"), None)
    _check(decision is not None, "interrupted run asks Pilot for diagnosis")

    try:
        started_at = time.monotonic()
        approved = controller.approve(decision["id"], snapshot, MissionControlService(project))
        _check(approved["ok"] is True, "Pilot approval returns ok")
        _check(approved["status"] == "running", "Pilot approval returns immediately as running")
        _check(time.monotonic() - started_at < 2.0, "Ask Pilot is non-blocking for the UI")

        terminal: dict[str, Any] | None = None
        for _ in range(60):
            time.sleep(5)
            current = controller.status(snapshot)
            active = [item for item in current["pending_decisions"] if item.get("id") == decision["id"]]
            if active:
                item = active[0]
                if item.get("status") == "pending" and item.get("action") != "pilot_triage":
                    terminal = {"type": "proposed_action", "decision": item}
                    break
                continue
            terminal = {"type": "completed", "events": _decision_events(project, decision["id"])[-5:]}
            break

        _check(terminal is not None, "Pilot finished before timeout")
        terminal_json = json.dumps(terminal, sort_keys=True)
        _check("pilot.completed" in terminal_json or "pilot_plan" in terminal_json, "Pilot result includes a real plan")
        _log("Pilot terminal result:")
        print(json.dumps(terminal, indent=2, sort_keys=True, default=str))
    finally:
        controller._remove_pending_decision(decision["id"])
        _cleanup_synthetic_events(project, {str(decision["id"])})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path, help="Real Otto project directory")
    args = parser.parse_args(argv)

    try:
        require_real_cost_opt_in("Autopilot real-project E2E")
    except SystemExit as exc:
        return int(exc.code or 2)
    project = args.project.expanduser().resolve(strict=True)
    controller = AutopilotController(project)

    check_blocked_recent_retry(controller)
    check_real_pilot(project, controller)
    _log("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
