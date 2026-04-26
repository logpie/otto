"""JSON serializers for Mission Control clients."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from otto.config import DEFAULTS
from otto.config import agent_effort
from otto.config import agent_model
from otto.config import agent_provider
from otto.config import load_config
from otto.config import resolve_certifier_mode
from otto.mission_control.actions import ActionResult, ActionState
from otto.mission_control.model import (
    ArtifactRef,
    DetailView,
    HistoryItem,
    LiveRunItem,
    MissionControlFilters,
    MissionControlState,
    StaleOverlay,
)
from otto.runs.schema import RunRecord


def serialize_project(project_dir: Path) -> dict[str, Any]:
    project_dir = Path(project_dir).resolve(strict=False)
    return {
        "path": str(project_dir),
        "name": project_dir.name,
        "branch": _git_output(project_dir, ["branch", "--show-current"]) or None,
        "dirty": bool(_git_output(project_dir, ["status", "--porcelain"])),
        "head_sha": _git_output(project_dir, ["rev-parse", "--short", "HEAD"]) or None,
        "defaults": _project_defaults(project_dir),
    }


def serialize_filters(filters: MissionControlFilters) -> dict[str, Any]:
    return {
        "active_only": filters.active_only,
        "type": filters.type_filter,
        "outcome": filters.outcome_filter,
        "query": filters.query,
        "history_page": filters.history_page,
        "history_page_size": filters.history_page_size,
    }


def serialize_state(project_dir: Path, state: MissionControlState) -> dict[str, Any]:
    return {
        "project": serialize_project(project_dir),
        "filters": serialize_filters(state.filters),
        "focus": state.focus,
        "selection": {
            "run_id": state.selection.run_id,
            "origin_pane": state.selection.origin_pane,
            "artifact_index": state.selection.artifact_index,
            "log_index": state.selection.log_index,
        },
        "selected_run_ids": sorted(state.selected_run_ids),
        "live": {
            "items": [serialize_live_item(item) for item in state.live_runs.items],
            "total_count": state.live_runs.total_count,
            "active_count": state.live_runs.active_count,
            "refresh_interval_s": state.live_runs.refresh_interval_s,
        },
        "history": {
            "items": [serialize_history_item(item) for item in state.history_page.items],
            "page": state.history_page.page,
            "page_size": state.history_page.page_size,
            "total_rows": state.history_page.total_rows,
            "total_pages": state.history_page.total_pages,
        },
        "banner": state.last_event_banner,
    }


def serialize_live_item(item: LiveRunItem) -> dict[str, Any]:
    record = item.record
    display_status = _display_status(record.status, item.overlay)
    return {
        **_record_summary(record),
        "display_status": display_status,
        "active": _is_effectively_active(record.status, item.overlay),
        "display_id": item.display_id,
        "branch_task": item.branch_task,
        "elapsed_s": item.elapsed_s,
        "elapsed_display": item.elapsed_display,
        "cost_usd": item.cost_usd,
        "cost_display": item.cost_display,
        "last_event": item.event,
        "row_label": item.row_label,
        "overlay": serialize_overlay(item.overlay),
    }


def serialize_history_item(item: HistoryItem) -> dict[str, Any]:
    row = item.row
    return {
        "run_id": row.run_id,
        "domain": row.domain,
        "run_type": row.run_type,
        "command": row.command,
        "status": row.status,
        "terminal_outcome": row.terminal_outcome,
        "queue_task_id": row.queue_task_id,
        "merge_id": row.merge_id,
        "branch": row.branch,
        "worktree": row.worktree,
        "summary": item.summary,
        "intent": row.intent,
        "completed_at_display": item.completed_at_display,
        "outcome_display": item.outcome_display,
        "duration_s": row.duration_s,
        "duration_display": item.duration_display,
        "cost_usd": row.cost_usd,
        "cost_display": item.cost_display,
        "resumable": row.resumable,
        "adapter_key": row.adapter_key,
    }


def serialize_detail(detail: DetailView) -> dict[str, Any]:
    record = detail.record
    return {
        **_record_summary(record),
        "display_status": _display_status(record.status, detail.overlay),
        "active": _is_effectively_active(record.status, detail.overlay),
        "source": detail.source,
        "title": detail.detail.title,
        "summary_lines": list(detail.detail.summary_lines),
        "overlay": serialize_overlay(detail.overlay),
        "artifacts": [serialize_artifact(artifact, index) for index, artifact in enumerate(detail.artifacts)],
        "log_paths": list(detail.log_paths),
        "selected_log_index": detail.selected_log_index,
        "selected_log_path": detail.selected_log_path,
        "legal_actions": [serialize_action_state(action) for action in detail.legal_actions],
        "record": record.to_dict(),
    }


def serialize_artifact(artifact: ArtifactRef, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "label": artifact.label,
        "path": artifact.path,
        "kind": artifact.kind,
        "exists": artifact.exists,
    }


def serialize_action_state(action: ActionState) -> dict[str, Any]:
    return {
        "key": action.key,
        "label": action.label,
        "enabled": action.enabled,
        "reason": action.reason,
        "preview": action.preview,
    }


def serialize_action_result(result: ActionResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "message": result.message,
        "severity": result.severity,
        "modal_title": result.modal_title,
        "modal_message": result.modal_message,
        "refresh": result.refresh,
        "clear_banner": result.clear_banner,
    }


def serialize_overlay(overlay: StaleOverlay | None) -> dict[str, Any] | None:
    if overlay is None:
        return None
    return {
        "level": overlay.level,
        "label": overlay.label,
        "reason": overlay.reason,
        "writer_alive": overlay.writer_alive,
    }


def _display_status(status: str | None, overlay: StaleOverlay | None) -> str:
    if overlay is not None and overlay.level == "stale":
        return "stale"
    return str(status or "")


def _is_effectively_active(status: str | None, overlay: StaleOverlay | None) -> bool:
    if str(status or "") in {"done", "failed", "cancelled", "removed", "interrupted"}:
        return False
    if overlay is not None and overlay.level == "stale":
        return False
    return True


def _record_summary(record: RunRecord) -> dict[str, Any]:
    argv = record.source.get("argv")
    provider = _first_string(
        record.source.get("provider"),
        record.source.get("agent_provider"),
        record.metrics.get("provider"),
        _argv_option(argv, "--provider"),
    )
    model = _first_string(
        record.source.get("model"),
        record.source.get("agent_model"),
        record.metrics.get("model"),
        _argv_option(argv, "--model"),
    )
    reasoning_effort = _first_string(
        record.source.get("reasoning_effort"),
        record.source.get("effort"),
        record.metrics.get("reasoning_effort"),
        _argv_option(argv, "--effort", "--reasoning-effort"),
    )
    return {
        "run_id": record.run_id,
        "domain": record.domain,
        "run_type": record.run_type,
        "command": record.command,
        "display_name": record.display_name,
        "status": record.status,
        "terminal_outcome": record.terminal_outcome,
        "project_dir": record.project_dir,
        "cwd": record.cwd,
        "queue_task_id": _first_string(record.identity.get("queue_task_id")),
        "merge_id": _first_string(record.identity.get("merge_id")),
        "branch": _first_string(record.git.get("branch")),
        "worktree": _first_string(record.git.get("worktree")),
        "provider": provider,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "adapter_key": record.adapter_key,
        "version": record.version,
    }


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _argv_option(argv: Any, *names: str) -> str | None:
    if not isinstance(argv, (list, tuple)):
        return None
    parts = [str(part) for part in argv]
    for index, part in enumerate(parts):
        for name in names:
            if part == name:
                if index + 1 >= len(parts):
                    continue
                value = parts[index + 1].strip()
                if value and not value.startswith("--"):
                    return value
            prefix = f"{name}="
            if part.startswith(prefix):
                value = part[len(prefix):].strip()
                if value:
                    return value
    return None


def _project_defaults(project_dir: Path) -> dict[str, Any]:
    config_path = project_dir / "otto.yaml"
    config_exists = config_path.exists()
    try:
        config = load_config(config_path)
        return {
            "provider": agent_provider(config),
            "model": agent_model(config),
            "reasoning_effort": agent_effort(config),
            "certifier_mode": resolve_certifier_mode(config),
            "skip_product_qa": bool(config.get("skip_product_qa")),
            "config_file_exists": config_exists,
            "config_error": None,
        }
    except Exception as exc:
        return {
            "provider": DEFAULTS["provider"],
            "model": DEFAULTS["model"],
            "reasoning_effort": DEFAULTS["effort"],
            "certifier_mode": DEFAULTS["certifier_mode"],
            "skip_product_qa": DEFAULTS["skip_product_qa"],
            "config_file_exists": config_exists,
            "config_error": str(exc),
        }


def _git_output(project_dir: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
