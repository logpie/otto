"""Queue adapter for Mission Control."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from otto import paths
from otto.manifest import queue_index_path_for
from otto.queue.runtime import (
    IN_FLIGHT_STATUSES,
    INTERRUPTED_STATUS,
    RESUMABLE_QUEUE_STATUSES,
    checkpoint_path_for_task,
    task_display_status,
    task_resume_block_reason,
)
from otto.queue.schema import load_queue, load_state
from otto.runs.registry import make_run_record, writer_identity_gone_or_stale
from otto.runs.schema import RunRecord
from otto.runs.schema import is_terminal_status
from otto.mission_control.actions import ActionExecutingAdapter, make_action
from otto.mission_control.adapters.common import (
    artifact_ref_for_path,
    expanded_artifact_paths,
    supplemental_session_artifact_paths,
)
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
        session_dir = str(record.artifacts.get("session_dir") or "").strip()
        extra_log_paths = [str(path).strip() for path in record.artifacts.get("extra_log_paths") or [] if str(path).strip()]

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
        seen_extra_paths = {artifact.path for artifact in items}
        for index, path in enumerate([*supplemental_session_artifact_paths(session_dir), *extra_log_paths], start=1):
            if path.endswith("watcher.log"):
                if path not in seen_extra_paths:
                    seen_extra_paths.add(path)
                    items.append(ArtifactRef.from_path("watcher log", path, kind="log"))
            else:
                for expanded_path in expanded_artifact_paths(path):
                    if expanded_path in seen_extra_paths:
                        continue
                    seen_extra_paths.add(expanded_path)
                    items.append(artifact_ref_for_path(expanded_path, fallback_label=f"extra {index}"))
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
        task_present = _queue_task_present(Path(record.project_dir), queue_task_id)
        spec_review_pending, spec_review_reason = _spec_review_pending(record)
        resume_enabled, resume_reason = _queue_resume_action_state(
            Path(record.project_dir),
            queue_task_id,
            record.status,
            checkpoint_path,
        )
        if spec_review_pending:
            resume_enabled = False
            resume_reason = "approve the spec or request changes first"
        cleanup_enabled = (
            task_present
            and (
                record.status == "queued"
                or stale_overlay
                or (is_terminal_status(record.status) and writer_identity_gone_or_stale(record.writer))
            )
        )
        cleanup_reason = (
            None
            if cleanup_enabled
            else "queue task already cleaned up"
            if queue_task_id and not task_present
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
                "a",
                "approve spec",
                enabled=spec_review_pending,
                reason=None if spec_review_pending else spec_review_reason,
                preview=f"would approve spec and resume {task_id}",
            ),
            make_action(
                "g",
                "request spec changes",
                enabled=spec_review_pending,
                reason=None if spec_review_pending else spec_review_reason,
                preview=f"would regenerate spec for {task_id}",
            ),
            make_action(
                "r",
                "resume from checkpoint",
                enabled=resume_enabled,
                reason=resume_reason,
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
                preview="would shell `otto merge --fast --transactional --no-certify --all`",
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
        candidate = Path(worktree).expanduser()
        if not candidate.is_absolute():
            candidate = Path(record.project_dir) / candidate
        return str(candidate.resolve(strict=False))
    try:
        for task in load_queue(Path(record.project_dir)):
            task_id = str(record.identity.get("queue_task_id") or "")
            if task.id == task_id and task.worktree:
                return str((Path(record.project_dir) / task.worktree).resolve(strict=False))
    except Exception:
        return None
    return None


def _queue_task_present(project_dir: Path, task_id: str) -> bool:
    if not task_id:
        return False
    try:
        return any(task.id == task_id for task in load_queue(project_dir))
    except Exception:
        return False


def _queue_resume_action_state(
    project_dir: Path,
    task_id: str,
    status: str,
    checkpoint_path: str,
) -> tuple[bool, str | None]:
    if not task_id:
        return False, "queue task id unknown"
    if status not in RESUMABLE_QUEUE_STATUSES:
        return False, "run is not checkpoint-resumable"
    if not checkpoint_path or not Path(checkpoint_path).exists():
        return False, "checkpoint missing"
    try:
        tasks = load_queue(project_dir)
        state = load_state(project_dir)
    except Exception as exc:
        return False, f"queue state unavailable: {exc}"
    task = next((task for task in tasks if task.id == task_id), None)
    if task is None:
        return False, "queue task definition missing"
    task_state = state.get("tasks", {}).get(task_id, {"status": status})
    current_status = task_display_status(task_state)
    if current_status not in RESUMABLE_QUEUE_STATUSES:
        return False, f"current task state is {current_status}"
    reason = task_resume_block_reason(project_dir, task, task_state)
    if reason is not None:
        return False, reason
    return True, None


def _spec_review_pending(record) -> tuple[bool, str | None]:
    if record.status != "paused":
        return False, "run is not paused for spec review"
    checkpoint_path = str(record.artifacts.get("checkpoint_path") or "").strip()
    if not checkpoint_path:
        return False, "checkpoint missing"
    try:
        checkpoint = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        return False, f"checkpoint unreadable: {exc}"
    if not isinstance(checkpoint, dict):
        return False, "checkpoint is malformed"
    if str(checkpoint.get("phase") or "") != "spec_review":
        return False, "run is not waiting at the spec review gate"
    spec_path = str(checkpoint.get("spec_path") or "").strip()
    if not spec_path or not Path(spec_path).exists():
        return False, "spec file missing"
    return True, None


def _checkpoint_spec_path(checkpoint_path: Path | None) -> str | None:
    if checkpoint_path is None:
        return None
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(checkpoint, dict):
        return None
    spec_path = str(checkpoint.get("spec_path") or "").strip()
    if spec_path and Path(spec_path).exists():
        return spec_path
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
    # W3-IMPORTANT-5: prefer build/narrative.log; fall back to improve/ for
    # `otto improve` queue tasks whose stream now lives under improve/.
    primary_log_path = None
    if session_run_id:
        build_log = paths.build_dir(worktree_path, session_run_id) / "narrative.log"
        improve_log = paths.improve_dir(worktree_path, session_run_id) / "narrative.log"
        if build_log.exists():
            primary_log_path = build_log
        elif improve_log.exists():
            primary_log_path = improve_log
        else:
            primary_log_path = build_log
    extra_log_paths: list[str] = []
    if status in {"failed", "cancelled", INTERRUPTED_STATUS} and not (primary_log_path and primary_log_path.exists()):
        for candidate in (paths.logs_dir(project_dir) / "web" / "watcher.log", paths.queue_dir(project_dir) / "watcher.log"):
            if candidate.exists():
                extra_log_paths.append(str(candidate.resolve(strict=False)))
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
        # W3-IMPORTANT-7: keep the user-facing display name free of internal
        # mode flags. The Recent Activity feed renders display_name + " started"
        # — the prior "<task>: legacy queue mode" leaked an internal flag into
        # operator-facing copy and read like a deprecation warning. The
        # compatibility detail is still surfaced via `compatibility_warning` in
        # identity (used by the side-panel "compat:" line in detail_panel_renderer
        # and as a UI badge), so nothing is lost — only the leak is fixed.
        display_name=task.id,
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
        intent={
            "summary": task.resolved_intent or task.id,
            "intent_path": None,
            "spec_path": _checkpoint_spec_path(checkpoint_path) or task.spec_file_path,
        },
        artifacts={
            "session_dir": str(session_dir),
            "manifest_path": str(child_manifest.resolve(strict=False)) if child_manifest and child_manifest.exists() else None,
            "checkpoint_path": str(checkpoint_path.resolve(strict=False)) if checkpoint_path is not None else None,
            "summary_path": str(paths.session_summary(worktree_path, session_run_id).resolve(strict=False)) if session_run_id else None,
            "primary_log_path": str(primary_log_path.resolve(strict=False)) if primary_log_path and primary_log_path.exists() else None,
            "extra_log_paths": extra_log_paths,
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
