"""Startup repair for durable atomic run history."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from otto import paths
from otto.history import command_family
from otto.runs.history import append_history_snapshot, build_terminal_snapshot, read_history_rows

_TERMINAL_ATOMIC_SUMMARY_STATUSES = {"completed", "failed", "interrupted", "cancelled"}
_TERMINAL_MANIFEST_EXIT_STATUSES = {"success", "failure"}


def repair_atomic_history(project_dir: Path) -> None:
    seen = {
        str(row.get("dedupe_key") or "")
        for row in read_history_rows(paths.history_jsonl(project_dir))
        if isinstance(row, dict)
    }
    sessions_root = paths.sessions_root(project_dir)
    if not sessions_root.exists():
        return

    for summary_path in sorted(sessions_root.glob("*/summary.json")):
        summary = _read_json(summary_path)
        if not isinstance(summary, dict):
            continue
        run_id = str(summary.get("run_id") or summary_path.parent.name).strip()
        if not run_id:
            continue
        dedupe_key = f"terminal_snapshot:{run_id}"
        if dedupe_key in seen:
            continue
        command = str(summary.get("command") or "").strip()
        run_type = command_family(command)
        if run_type not in {"build", "improve", "certify"}:
            continue

        session_dir = paths.session_dir(project_dir, run_id)
        manifest = _read_json(session_dir / "manifest.json")
        if not _is_proved_terminal_atomic_summary(summary, manifest):
            continue
        checkpoint = _read_json(paths.session_checkpoint(project_dir, run_id))
        terminal_snapshot = _terminal_snapshot_status(summary, manifest)
        if terminal_snapshot is None:
            continue
        status, terminal_outcome = terminal_snapshot
        append_history_snapshot(
            project_dir,
            build_terminal_snapshot(
                run_id=run_id,
                domain="atomic",
                run_type=run_type,
                command=command,
                intent_meta={
                    "summary": str(summary.get("intent") or "")[:200],
                    "intent_path": str(paths.session_intent(project_dir, run_id)),
                    "spec_path": str(session_dir / "spec.md") if (session_dir / "spec.md").exists() else None,
                },
                status=status,
                terminal_outcome=terminal_outcome,
                timing={
                    "started_at": _string_or_none(
                        manifest.get("started_at")
                        or checkpoint.get("started_at")
                        or checkpoint.get("session_started_at")
                    ),
                    "finished_at": _string_or_none(
                        manifest.get("finished_at")
                        or summary.get("completed_at")
                        or checkpoint.get("updated_at")
                    ),
                    "timestamp": _string_or_none(
                        manifest.get("finished_at")
                        or summary.get("completed_at")
                        or checkpoint.get("updated_at")
                    ),
                    "duration_s": _float_or_zero(summary.get("duration_s") or manifest.get("duration_s")),
                },
                metrics={"cost_usd": _float_or_zero(summary.get("cost_usd") or manifest.get("cost_usd"))},
                git={
                    "branch": _string_or_none(summary.get("branch") or manifest.get("branch")),
                    "worktree": None,
                },
                source={"resumable": run_type != "certify"},
                artifacts=_atomic_artifacts(project_dir, run_id, primary_phase=_primary_phase_for_run_type(run_type)),
                extra_fields=_summary_extra_fields(summary, checkpoint, run_type=run_type),
            ),
            strict=True,
        )
        seen.add(dedupe_key)


def _atomic_artifacts(project_dir: Path, run_id: str, *, primary_phase: str) -> dict[str, Any]:
    session_dir = paths.session_dir(project_dir, run_id)
    return {
        "session_dir": str(session_dir),
        "manifest_path": str(session_dir / "manifest.json"),
        "checkpoint_path": str(paths.session_checkpoint(project_dir, run_id)),
        "summary_path": str(paths.session_summary(project_dir, run_id)),
        "primary_log_path": str(session_dir / primary_phase / "narrative.log"),
        "extra_log_paths": [],
    }


def _primary_phase_for_run_type(run_type: str) -> str:
    if run_type == "improve":
        return "improve"
    if run_type == "certify":
        return "certify"
    return "build"


def _summary_extra_fields(
    summary: dict[str, Any],
    checkpoint: dict[str, Any],
    *,
    run_type: str,
) -> dict[str, Any]:
    stories_passed = _int_or_zero(summary.get("stories_passed"))
    stories_tested = _int_or_zero(summary.get("stories_tested"))
    certifier_mode = str(checkpoint.get("certifier_mode") or summary.get("certifier_mode") or "")
    certify_cost = (
        _float_or_zero(summary.get("cost_usd"))
        if run_type == "certify"
        else _float_or_zero(
            ((summary.get("breakdown") or {}).get("certify") or {}).get("cost_usd")
        )
    )
    return {
        "passed": bool(summary.get("passed")),
        "certifier_mode": certifier_mode,
        "mode": certifier_mode,
        "stories_passed": stories_passed,
        "stories_tested": stories_tested,
        "passed_count": stories_passed,
        "failed_count": max(stories_tested - stories_passed, 0),
        "warn_count": 0,
        "certify_rounds": _int_or_zero(summary.get("rounds")),
        "certifier_cost_usd": certify_cost,
    }


def _is_proved_terminal_atomic_summary(summary: dict[str, Any], manifest: dict[str, Any]) -> bool:
    summary_status = str(summary.get("status") or "").strip().lower()
    if summary_status not in _TERMINAL_ATOMIC_SUMMARY_STATUSES:
        return False
    command = str(summary.get("command") or "").strip()
    if command_family(command) not in {"build", "improve", "certify"}:
        return False
    return _valid_iso_timestamp(summary.get("completed_at")) or _manifest_has_terminal_fields(manifest)


def _terminal_snapshot_status(
    summary: dict[str, Any],
    manifest: dict[str, Any],
) -> tuple[str, str] | None:
    summary_status = str(summary.get("status") or "").strip().lower()
    if summary_status == "completed":
        passed = summary.get("passed")
        if isinstance(passed, bool):
            return ("done", "success") if passed else ("failed", "failure")
        manifest_exit_status = str(manifest.get("exit_status") or "").strip().lower()
        if manifest_exit_status == "success":
            return "done", "success"
        if manifest_exit_status == "failure":
            return "failed", "failure"
        return None
    if summary_status == "failed":
        return "failed", "failure"
    if summary_status == "interrupted":
        return "interrupted", "interrupted"
    if summary_status == "cancelled":
        return "cancelled", "cancelled"
    return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _string_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _manifest_has_terminal_fields(manifest: dict[str, Any]) -> bool:
    finished_at = manifest.get("finished_at")
    exit_status = str(manifest.get("exit_status") or "").strip().lower()
    return _valid_iso_timestamp(finished_at) and exit_status in _TERMINAL_MANIFEST_EXIT_STATUSES


def _valid_iso_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return True


def _int_or_zero(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _float_or_zero(value: Any) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
