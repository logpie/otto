"""Mission Control action capability calculation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from otto.queue.runtime import INTERRUPTED_STATUS
from otto.runs.schema import is_terminal_status

if TYPE_CHECKING:
    from otto.tui.mission_control_model import StaleOverlay
    from otto.runs.schema import RunRecord


@dataclass(slots=True)
class ActionState:
    key: str
    label: str
    enabled: bool
    reason: str | None
    preview: str


def calculate_legal_actions(record: "RunRecord", overlay: "StaleOverlay | None") -> list[ActionState]:
    actions = [
        _cancel_action(record, overlay),
        _resume_action(record, overlay),
        _retry_action(record),
        _cleanup_action(record),
        _merge_action(record),
        _open_logs_action(record),
        _open_file_action(record),
    ]
    return [action for action in actions if action is not None]


def _cancel_action(record: "RunRecord", overlay: "StaleOverlay | None") -> ActionState | None:
    if is_terminal_status(record.status):
        return ActionState("c", "cancel", False, "run already terminal", _cancel_preview(record))
    if overlay is not None and overlay.level == "stale":
        return ActionState("c", "cancel", False, "writer unavailable (stale overlay)", _cancel_preview(record))
    return ActionState("c", "cancel", True, None, _cancel_preview(record))


def _resume_action(record: "RunRecord", overlay: "StaleOverlay | None") -> ActionState:
    del overlay
    if record.domain == "merge":
        return ActionState("r", "resume", False, "merge --resume is deferred", "would shell `otto merge --resume` once supported")
    if record.run_type == "certify":
        return ActionState("r", "resume", False, "standalone certify has no resume path", "no resume path exists")
    if record.status not in {INTERRUPTED_STATUS, "paused"}:
        return ActionState("r", "resume", False, "run is not interrupted", _resume_preview(record))
    checkpoint_path = str(record.artifacts.get("checkpoint_path") or "").strip()
    if not checkpoint_path:
        return ActionState("r", "resume", False, "checkpoint missing", _resume_preview(record))
    if not Path(checkpoint_path).exists():
        return ActionState("r", "resume", False, "checkpoint missing", _resume_preview(record))
    return ActionState("r", "resume", True, None, _resume_preview(record))


def _retry_action(record: "RunRecord") -> ActionState:
    argv = record.source.get("argv")
    if not isinstance(argv, list) or not argv:
        return ActionState("R", "retry", False, "original argv unavailable", "cannot reconstruct original command")
    if not str(record.cwd or "").strip():
        return ActionState("R", "retry", False, "cwd missing", "cannot re-run without a working directory")
    label = "requeue" if record.domain == "queue" else "retry"
    return ActionState("R", label, is_terminal_status(record.status), None if is_terminal_status(record.status) else "run is still active", _retry_preview(record))


def _cleanup_action(record: "RunRecord") -> ActionState:
    if record.domain == "queue" and record.status == "queued":
        return ActionState("x", "remove", True, None, f"would shell `otto queue rm {record.identity.get('queue_task_id') or record.run_id}`")
    if not is_terminal_status(record.status):
        return ActionState("x", "cleanup", False, "run is still active", _cleanup_preview(record))
    return ActionState("x", "cleanup", True, None, _cleanup_preview(record))


def _merge_action(record: "RunRecord") -> ActionState | None:
    if record.domain != "queue":
        return None
    task_id = str(record.identity.get("queue_task_id") or "").strip()
    if not task_id:
        return ActionState("m", "merge selected", False, "queue task id missing", "cannot target queue merge")
    if record.status != "done":
        return ActionState("m", "merge selected", False, "only done queue rows can be merged", f"would shell `otto merge {task_id}`")
    return ActionState("m", "merge selected", True, None, f"would shell `otto merge {task_id}`")


def _open_logs_action(record: "RunRecord") -> ActionState:
    primary_log = str(record.artifacts.get("primary_log_path") or "").strip()
    if not primary_log:
        return ActionState("o", "open logs", False, "no log path available", "no logs to cycle")
    return ActionState("o", "open logs", True, None, "would cycle available log views")


def _open_file_action(record: "RunRecord") -> ActionState:
    if not any(str(record.artifacts.get(key) or "").strip() for key in ("manifest_path", "summary_path", "checkpoint_path")):
        return ActionState("e", "open file", False, "no selectable artifact", "would shell `$EDITOR <artifact>`")
    return ActionState("e", "open file", True, None, "would shell `$EDITOR <selected artifact>`")


def _cancel_preview(record: "RunRecord") -> str:
    if record.domain == "queue":
        task_id = record.identity.get("queue_task_id") or record.run_id
        return f"would append queue cancel for {task_id}"
    if record.domain == "merge":
        return f"would append merge cancel for {record.run_id}"
    return f"would append session cancel for {record.run_id}"


def _resume_preview(record: "RunRecord") -> str:
    if record.domain == "queue":
        task_id = record.identity.get("queue_task_id") or record.run_id
        return f"would shell `otto queue resume {task_id}`"
    if record.run_type == "improve":
        return f"would shell `otto improve --resume` from {record.cwd}"
    return f"would shell `otto {record.run_type} --resume` from {record.cwd}"


def _retry_preview(record: "RunRecord") -> str:
    argv = " ".join(str(part) for part in (record.source.get("argv") or []))
    if record.domain == "queue":
        return f"would reconstruct queue task from `{argv}`"
    return f"would re-run `{argv}` from {record.cwd}"


def _cleanup_preview(record: "RunRecord") -> str:
    if record.domain == "queue":
        task_id = record.identity.get("queue_task_id") or record.run_id
        return f"would shell queue cleanup for {task_id}"
    return f"would clean terminal artifacts for {record.run_id}"
