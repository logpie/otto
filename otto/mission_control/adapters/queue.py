"""Queue adapter for Mission Control."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from otto import paths
from otto.manifest import queue_index_path_for
from otto.queue.runtime import IN_FLIGHT_STATUSES, INTERRUPTED_STATUS, checkpoint_path_for_task, task_display_status
from otto.queue.schema import load_queue, load_state
from otto.runs.registry import make_run_record, writer_identity_gone_or_stale
from otto.runs.schema import RunRecord
from otto.runs.schema import is_terminal_status
from otto.mission_control.actions import ActionExecutingAdapter, make_action
from otto.mission_control.model import ArtifactRef, DetailModel, HistoryRow


class QueueMissionControlAdapter(ActionExecutingAdapter):
    def legacy_records(self, project_dir: Path, now: datetime, live_records: list[RunRecord]):
        try:
            tasks = load_queue(project_dir)
            state = load_state(project_dir)
        except Exception:
            return []

        task_states = state.get("tasks", {}) if isinstance(state, dict) else {}
        existing_task_ids = {
            str(record.identity.get("queue_task_id") or "").strip()
            for record in live_records
            if record.domain == "queue"
        }
        records = []
        for task in tasks:
            if task.id in existing_task_ids:
                continue
            task_state = task_states.get(task.id) if isinstance(task_states, dict) else None
            records.append(_legacy_queue_record(project_dir, task, task_state, now))
        return records

    def live_overlay(self, record, overlay):
        if str(record.identity.get("compatibility_warning") or "").strip() == "legacy queue mode":
            if record.status in IN_FLIGHT_STATUSES:
                return overlay
            return None
        return overlay

    def row_label(self, record) -> str:
        task_id = str(record.identity.get("queue_task_id") or record.run_id)
        summary = str(record.intent.get("summary") or "").strip()
        return f"{task_id}: {summary}".strip(": ")

    def history_summary(self, history_row: HistoryRow) -> str:
        return history_row.intent or history_row.queue_task_id or history_row.branch or history_row.run_id

    def artifacts(self, record) -> list[ArtifactRef]:
        items: list[ArtifactRef] = []
        worktree = _queue_worktree(record)
        intent_path = str(record.intent.get("intent_path") or "").strip()
        spec_path = str(record.intent.get("spec_path") or "").strip()
        manifest_path = str(record.artifacts.get("manifest_path") or "").strip()
        checkpoint_path = str(record.artifacts.get("checkpoint_path") or "").strip()
        summary_path = str(record.artifacts.get("summary_path") or "").strip()
        primary_log = str(record.artifacts.get("primary_log_path") or "").strip()

        if intent_path:
            items.append(ArtifactRef.from_path("intent", intent_path))
        if spec_path:
            items.append(ArtifactRef.from_path("spec", spec_path))
        queue_task_id = str(record.identity.get("queue_task_id") or "").strip()
        if queue_task_id:
            queue_manifest = queue_index_path_for(Path(record.project_dir), queue_task_id)
            if queue_manifest is not None:
                items.append(ArtifactRef.from_path("queue manifest", str(queue_manifest.resolve(strict=False))))
        if manifest_path:
            items.append(ArtifactRef.from_path("manifest", manifest_path))
        if summary_path:
            items.append(ArtifactRef.from_path("summary", summary_path))
        if checkpoint_path:
            items.append(ArtifactRef.from_path("checkpoint", checkpoint_path))
        if primary_log:
            items.append(ArtifactRef.from_path("primary log", primary_log, kind="log"))
            messages_path = Path(primary_log).with_name("messages.jsonl")
            if messages_path.exists():
                items.append(ArtifactRef.from_path("messages", str(messages_path), kind="log"))
        if worktree:
            items.append(ArtifactRef.from_path("worktree", worktree))
        return items

    def legal_actions(self, record, overlay):
        task_id = str(record.identity.get("queue_task_id") or record.run_id).strip()
        queue_task_id = str(record.identity.get("queue_task_id") or "").strip()
        warning = str(record.identity.get("compatibility_warning") or "").strip()
        checkpoint_path = str(record.artifacts.get("checkpoint_path") or "").strip()
        log_paths = [artifact.path for artifact in self.artifacts(record) if artifact.kind == "log"]
        has_artifact = bool(self.artifacts(record))
        argv = record.source.get("argv")
        argv_preview = " ".join(str(part) for part in (argv or []))
        legacy_logs_reason = "legacy queue mode has no registry-backed log view"
        legacy_artifacts_reason = "legacy queue mode has no registry-backed artifacts"
        stale_overlay = overlay is not None and overlay.level == "stale"
        cleanup_enabled = (
            record.status == "queued"
            or stale_overlay
            or (is_terminal_status(record.status) and writer_identity_gone_or_stale(record.writer))
        )
        cleanup_reason = (
            None
            if cleanup_enabled
            else "run is still active"
            if not is_terminal_status(record.status)
            else "writer still alive — wait for finalization"
        )
        return [
            make_action(
                "c",
                "cancel",
                enabled=(
                    bool(queue_task_id)
                    and not is_terminal_status(record.status)
                    and not (overlay is not None and overlay.level == "stale")
                ),
                reason=(
                    "run already terminal"
                    if is_terminal_status(record.status)
                    else "queue task id unknown"
                    if not queue_task_id
                    else "writer unavailable (stale overlay)"
                    if overlay is not None and overlay.level == "stale"
                    else None
                ),
                preview=f"would append queue cancel for {task_id}",
            ),
            make_action(
                "r",
                "resume",
                enabled=(
                    record.status in {INTERRUPTED_STATUS, "paused"}
                    and bool(checkpoint_path)
                    and Path(checkpoint_path).exists()
                ),
                reason=(
                    "run is not interrupted"
                    if record.status not in {INTERRUPTED_STATUS, "paused"}
                    else "checkpoint missing"
                    if not checkpoint_path or not Path(checkpoint_path).exists()
                    else None
                ),
                preview=f"would shell `otto queue resume {task_id}`",
            ),
            make_action(
                "R",
                "requeue",
                enabled=is_terminal_status(record.status) and isinstance(argv, list) and bool(argv) and bool(str(record.cwd or "").strip()),
                reason=(
                    "original argv unavailable"
                    if not isinstance(argv, list) or not argv
                    else "cwd missing"
                    if not str(record.cwd or "").strip()
                    else "run is still active"
                    if not is_terminal_status(record.status)
                    else None
                ),
                preview=(
                    "cannot reconstruct original command"
                    if not isinstance(argv, list) or not argv
                    else f"would reconstruct queue task from `{argv_preview}`"
                ),
            ),
            make_action(
                "x",
                "remove" if record.status == "queued" or stale_overlay else "cleanup",
                enabled=cleanup_enabled,
                reason=cleanup_reason,
                preview=(
                    f"would shell `otto queue rm {task_id}`"
                    if record.status == "queued" or stale_overlay
                    else f"would shell queue cleanup for {task_id}"
                ),
            ),
            make_action(
                "m",
                "merge selected",
                enabled=bool(task_id) and record.status == "done",
                reason=(
                    "queue task id missing"
                    if not task_id
                    else "only done queue rows can be merged"
                    if record.status != "done"
                    else None
                ),
                preview=(
                    "cannot target queue merge"
                    if not task_id
                    else f"would shell `otto merge --fast --no-certify {task_id}`"
                ),
            ),
            make_action(
                "M",
                "merge all",
                enabled=True,
                reason=None,
                preview="would shell `otto merge --fast --no-certify --all`",
            ),
            make_action(
                "o",
                "open logs",
                enabled=bool(log_paths) and warning != "legacy queue mode",
                reason=(
                    legacy_logs_reason
                    if warning == "legacy queue mode"
                    else None if log_paths else "no log path available"
                ),
                preview="would cycle available log views" if log_paths else "no logs to cycle",
            ),
            make_action(
                "e",
                "open file",
                enabled=has_artifact and warning != "legacy queue mode",
                reason=(
                    legacy_artifacts_reason
                    if warning == "legacy queue mode"
                    else None if has_artifact else "no selectable artifact"
                ),
                preview="would shell `$EDITOR <selected artifact>`",
            ),
        ]

    def detail_panel_renderer(self, record) -> DetailModel:
        task_id = str(record.identity.get("queue_task_id") or record.run_id)
        summary = str(record.intent.get("summary") or "").strip() or task_id
        lines = [
            f"task: {task_id}",
            f"intent: {summary}",
            f"branch: {record.git.get('branch') or '-'}",
            f"worktree: {_queue_worktree(record) or '-'}",
            f"child run: {record.identity.get('child_run_id') or record.identity.get('expected_child_run_id') or '-'}",
        ]
        warning = str(record.identity.get("compatibility_warning") or "").strip()
        if warning:
            lines.append(f"compat: {warning}")
        manifest_path = str(record.artifacts.get("manifest_path") or "").strip()
        if manifest_path:
            try:
                manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            except Exception:
                manifest = None
            if isinstance(manifest, dict):
                lines.append(f"manifest run_id: {manifest.get('run_id') or '-'}")
        return DetailModel(title=f"queue: {task_id}", summary_lines=lines)


def _queue_worktree(record) -> str | None:
    worktree = str(record.git.get("worktree") or "").strip()
    if worktree:
        return worktree
    try:
        for task in load_queue(Path(record.project_dir)):
            task_id = str(record.identity.get("queue_task_id") or "")
            if task.id == task_id and task.worktree:
                return str((Path(record.project_dir) / task.worktree).resolve(strict=False))
    except Exception:
        return None
    return None


def _legacy_queue_record(project_dir, task, task_state, now):
    state = task_state if isinstance(task_state, dict) else {}
    status = task_display_status(state)
    session_run_id = str(state.get("child_run_id") or state.get("attempt_run_id") or "").strip()
    worktree_path = (project_dir / task.worktree).resolve(strict=False) if task.worktree else project_dir.resolve(strict=False)
    session_root = paths.sessions_root(worktree_path)
    session_dir = paths.session_dir(worktree_path, session_run_id) if session_run_id else session_root
    checkpoint_path = checkpoint_path_for_task(project_dir, task)
    queue_manifest = queue_index_path_for(project_dir, task.id)
    child_manifest = session_dir / "manifest.json" if session_run_id else None
    primary_log_path = paths.build_dir(worktree_path, session_run_id) / "narrative.log" if session_run_id else None
    started_at = str(state.get("started_at") or task.added_at or now.strftime("%Y-%m-%dT%H:%M:%SZ")).strip()
    finished_at = str(state.get("finished_at") or "").strip() or None
    if not is_terminal_status(status):
        finished_at = None
    updated_at = finished_at or str(state.get("started_at") or "").strip() or started_at
    record = make_run_record(
        project_dir=project_dir,
        run_id=session_run_id or f"queue-compat:{task.id}",
        domain="queue",
        run_type="queue",
        command=" ".join(task.command_argv[:2]) if task.command_argv else "queue",
        display_name=f"{task.id}: legacy queue mode",
        status=status,
        cwd=worktree_path,
        identity={
            "queue_task_id": task.id,
            "merge_id": None,
            "parent_run_id": None,
            "child_run_id": session_run_id or None,
            "expected_child_run_id": str(state.get("attempt_run_id") or "").strip() or None,
            "compatibility_warning": "legacy queue mode",
        },
        source={
            "invoked_via": "queue",
            "argv": list(task.command_argv),
            "resumable": bool(task.resumable),
        },
        git={"branch": task.branch, "worktree": task.worktree, "target_branch": None, "head_sha": None},
        intent={"summary": task.resolved_intent or task.id, "intent_path": None, "spec_path": task.spec_file_path},
        artifacts={
            "session_dir": str(session_dir),
            "manifest_path": str(child_manifest.resolve(strict=False)) if child_manifest and child_manifest.exists() else None,
            "checkpoint_path": str(checkpoint_path.resolve(strict=False)) if checkpoint_path is not None else None,
            "summary_path": str(paths.session_summary(worktree_path, session_run_id).resolve(strict=False)) if session_run_id else None,
            "primary_log_path": str(primary_log_path.resolve(strict=False)) if primary_log_path and primary_log_path.exists() else None,
            "extra_log_paths": [],
            "queue_manifest_path": str(queue_manifest.resolve(strict=False)) if queue_manifest is not None else None,
        },
        metrics={
            "cost_usd": _coerce_float(state.get("cost_usd")),
            "stories_passed": _coerce_float(state.get("stories_passed")),
            "stories_tested": _coerce_float(state.get("stories_tested")),
        },
        adapter_key="queue.attempt",
        last_event=str(state.get("failure_reason") or "legacy queue mode"),
    )
    child = state.get("child") if isinstance(state.get("child"), dict) else {}
    writer: dict[str, object] = {}
    if isinstance(child, dict):
        writer = {
            "pid": child.get("pid"),
            "pgid": child.get("pgid"),
            "process_start_time_ns": child.get("start_time_ns"),
            "writer_id": f"queue:{task.id}",
        }
    record.writer = writer
    record.timing.update({
        "started_at": started_at,
        "updated_at": updated_at,
        "heartbeat_at": updated_at,
        "finished_at": finished_at,
        "duration_s": _coerce_float(state.get("duration_s")),
        # Legacy queue compatibility rows are stale snapshots with no live writer.
        "heartbeat_interval_s": 60.0,
        "heartbeat_seq": 0,
    })
    return record


def _coerce_float(value) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
