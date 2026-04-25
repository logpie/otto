"""Shared Mission Control service for non-TUI clients."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from otto.config import load_config
from otto.config import repo_preflight_issues
from otto.config import resolve_intent_for_enqueue
from otto import paths
from otto.merge import git_ops
from otto.merge.state import load_state as load_merge_state
from otto.mission_control.actions import _otto_cli_argv
from otto.mission_control.actions import execute_action, execute_merge_all
from otto.mission_control.events import append_event
from otto.mission_control.events import events_status
from otto.mission_control.model import (
    DetailView,
    MissionControlFilters,
    MissionControlModel,
    MissionControlState,
    SelectionState,
)
from otto.mission_control.serializers import (
    serialize_action_result,
    serialize_artifact,
    serialize_detail,
    serialize_state,
)
from otto.mission_control.runtime import runtime_status as build_runtime_status
from otto.mission_control.runtime import watcher_health
from otto.mission_control.supervisor import record_watcher_launch
from otto.mission_control.supervisor import record_watcher_stop
from otto.mission_control.supervisor import read_supervisor
from otto.queue.enqueue import enqueue_task
from otto.queue.runtime import IN_FLIGHT_STATUSES, task_display_status, watcher_alive
from otto.queue.runner import child_is_alive
from otto.queue.schema import load_queue, load_state as load_queue_state

LOGGER = logging.getLogger(__name__)
REVIEW_IN_PROGRESS_STATUSES = {"queued", "starting", "running", "terminating"}


class MissionControlServiceError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class LogReadResult:
    path: str | None
    offset: int
    next_offset: int
    text: str
    exists: bool


class MissionControlService:
    """Client-neutral Mission Control operations."""

    def __init__(self, project_dir: Path, *, queue_compat: bool = True) -> None:
        self.project_dir = Path(project_dir).resolve(strict=False)
        self.model = MissionControlModel(self.project_dir, queue_compat=queue_compat)

    def state(self, filters: MissionControlFilters | None = None) -> dict[str, Any]:
        state = self._state(filters)
        payload = serialize_state(self.project_dir, state)
        watcher = self.watcher_status()
        landing = self.landing_status()
        payload["watcher"] = watcher
        payload["landing"] = landing
        payload["runtime"] = self.runtime_status(watcher=watcher, landing=landing)
        payload["events"] = self.events(limit=50)
        return payload

    def detail(self, run_id: str, filters: MissionControlFilters | None = None) -> dict[str, Any]:
        detail = self._detail_view(run_id, filters)
        payload = serialize_detail(detail)
        payload["review_packet"] = _review_packet(self.project_dir, detail)
        _apply_landing_context(self.project_dir, payload, detail)
        return payload

    def logs(
        self,
        run_id: str,
        *,
        log_index: int = 0,
        offset: int = 0,
        limit_bytes: int = 128_000,
        filters: MissionControlFilters | None = None,
    ) -> dict[str, Any]:
        detail = self._detail_view(run_id, filters)
        if not detail.log_paths:
            return asdict(LogReadResult(None, offset, offset, "", False))
        index = min(max(log_index, 0), len(detail.log_paths) - 1)
        path = self._validated_artifact_path(detail.log_paths[index])
        return asdict(self._read_file_slice(path, offset=max(0, offset), limit_bytes=limit_bytes))

    def artifacts(self, run_id: str, filters: MissionControlFilters | None = None) -> dict[str, Any]:
        detail = self._detail_view(run_id, filters)
        return {
            "run_id": detail.run_id,
            "artifacts": [serialize_artifact(artifact, index) for index, artifact in enumerate(detail.artifacts)],
        }

    def artifact_content(
        self,
        run_id: str,
        artifact_index: int,
        *,
        filters: MissionControlFilters | None = None,
        limit_bytes: int = 256_000,
    ) -> dict[str, Any]:
        detail = self._detail_view(run_id, filters)
        if artifact_index < 0 or artifact_index >= len(detail.artifacts):
            raise MissionControlServiceError("artifact index out of range", status_code=404)
        artifact = detail.artifacts[artifact_index]
        path = self._validated_artifact_path(artifact.path)
        if path.is_dir():
            raise MissionControlServiceError("artifact is a directory", status_code=400)
        read = self._read_file_slice(path, offset=0, limit_bytes=limit_bytes)
        return {
            "artifact": serialize_artifact(artifact, artifact_index),
            "content": read.text,
            "truncated": read.next_offset < (path.stat().st_size if path.exists() else read.next_offset),
        }

    def diff(
        self,
        run_id: str,
        *,
        filters: MissionControlFilters | None = None,
        limit_chars: int = 240_000,
    ) -> dict[str, Any]:
        detail = self._detail_view(run_id, filters)
        target = _review_target(self.project_dir, detail.record)
        branch = _optional_str(detail.record.git.get("branch"))
        diff = _branch_diff(self.project_dir, branch, target)
        text = ""
        truncated = False
        command = f"git diff {target}...{branch}" if branch and branch != target else None
        if branch and target and branch != target and diff["error"] is None:
            text_result = _branch_diff_text(self.project_dir, branch, target)
            if text_result["error"] is not None:
                diff = {**diff, "error": text_result["error"]}
            else:
                text = str(text_result["text"])
                if len(text) > limit_chars:
                    text = text[:limit_chars]
                    truncated = True
        return {
            "run_id": detail.run_id,
            "branch": branch,
            "target": target,
            "command": command,
            "files": diff["files"],
            "file_count": len(diff["files"]),
            "text": text,
            "error": diff["error"],
            "truncated": truncated,
        }

    def execute(
        self,
        run_id: str,
        action: str,
        *,
        selected_queue_task_ids: list[str] | None = None,
        artifact_index: int | None = None,
        filters: MissionControlFilters | None = None,
    ) -> dict[str, Any]:
        key = _action_key(action)
        detail = self._detail_view(run_id, filters)
        legal = {item.key: item for item in detail.legal_actions}
        if key not in legal:
            raise MissionControlServiceError("action unavailable", status_code=404)
        if not legal[key].enabled:
            reason = legal[key].reason or "action disabled"
            raise MissionControlServiceError(reason, status_code=409)
        if key == "m":
            merge_info = _detail_merge_info(self.project_dir, detail)
            if merge_info is not None:
                target = str(merge_info.get("target") or _merge_target(self.project_dir))
                raise MissionControlServiceError(f"Already merged into {target}.", status_code=409)
            _ensure_merge_unblocked(self.project_dir)

        selected_artifact_path = None
        if key == "e":
            index = 0 if artifact_index is None else artifact_index
            if index < 0 or index >= len(detail.artifacts):
                raise MissionControlServiceError("artifact index out of range", status_code=404)
            selected_artifact_path = str(self._validated_artifact_path(detail.artifacts[index].path))

        event_action = _event_action_name(key, label=legal[key].label, domain=detail.record.domain)
        result = execute_action(
            detail.record,
            key,
            self.project_dir,
            selected_artifact_path=selected_artifact_path,
            selected_queue_task_ids=selected_queue_task_ids,
            post_result=lambda item: self._record_async_action_result(
                kind=f"run.{event_action}.completed",
                result=item,
                run_id=detail.run_id,
                task_id=_optional_str(detail.record.identity.get("queue_task_id")),
                details={"action": event_action, "display_status": detail.record.status},
            ),
        )
        payload = serialize_action_result(result)
        self._record_event(
            kind=f"run.{event_action}",
            severity=_event_severity(payload),
            message=payload.get("message") or f"{event_action} requested",
            run_id=detail.run_id,
            task_id=_optional_str(detail.record.identity.get("queue_task_id")),
            details={
                "ok": payload.get("ok"),
                "action": event_action,
                "display_status": detail.record.status,
            },
        )
        return payload

    def merge_all(self) -> dict[str, Any]:
        _ensure_merge_unblocked(self.project_dir)
        payload = serialize_action_result(
            execute_merge_all(
                self.project_dir,
                post_result=lambda item: self._record_async_action_result(
                    kind="merge.all.completed",
                    result=item,
                    details={"action": "merge-all"},
                ),
            )
        )
        self._record_event(
            kind="merge.all",
            severity=_event_severity(payload),
            message=payload.get("message") or "merge ready tasks requested",
            details={"ok": payload.get("ok")},
        )
        return payload

    def watcher_status(self, *, probe_lock: bool = True) -> dict[str, Any]:
        try:
            state = load_queue_state(self.project_dir)
        except Exception:
            state = {"watcher": None, "tasks": {}}
        try:
            tasks = load_queue(self.project_dir)
        except Exception:
            tasks = []
        task_states = state.get("tasks", {}) if isinstance(state, dict) else {}
        counts = {
            "queued": 0,
            "starting": 0,
            "running": 0,
            "terminating": 0,
            "interrupted": 0,
            "done": 0,
            "failed": 0,
            "cancelled": 0,
            "removed": 0,
        }
        for task in tasks:
            raw = task_states.get(task.id) if isinstance(task_states, dict) else None
            status = _queue_display_status(raw if isinstance(raw, dict) else None, state)
            counts[status] = counts.get(status, 0) + 1
        watcher = state.get("watcher") if isinstance(state, dict) else None
        health = watcher_health(self.project_dir, state if isinstance(state, dict) else {}, probe_lock=probe_lock)
        return {
            "alive": health["state"] == "running",
            "watcher": watcher if isinstance(watcher, dict) else None,
            "counts": counts,
            "health": health,
        }

    def landing_status(self) -> dict[str, Any]:
        try:
            tasks = load_queue(self.project_dir)
        except Exception:
            tasks = []
        try:
            state = load_queue_state(self.project_dir)
        except Exception:
            state = {"tasks": {}}
        task_states = state.get("tasks", {}) if isinstance(state, dict) else {}
        target = _merge_target(self.project_dir)
        merged_by_branch = _merged_branch_index(self.project_dir, target)
        preflight = _merge_preflight(self.project_dir)

        items: list[dict[str, Any]] = []
        ready_tasks: list[Any] = []
        counts = {"ready": 0, "merged": 0, "blocked": 0, "total": 0}
        for task in tasks:
            raw_state = task_states.get(task.id) if isinstance(task_states, dict) else None
            queue_status = _queue_display_status(raw_state if isinstance(raw_state, dict) else None, state)
            branch = str(task.branch or "").strip()
            merge_info = merged_by_branch.get(branch)
            diff = (
                {"files": [], "error": None}
                if merge_info is not None or queue_status in {"queued", "starting", "running", "terminating"}
                else _branch_diff(self.project_dir, branch, target)
            )
            if merge_info is not None:
                landing_state = "merged"
                label = "Landed"
                counts["merged"] += 1
            elif queue_status == "done" and branch and diff["error"] is None:
                landing_state = "ready"
                label = "Ready to land"
                counts["ready"] += 1
                ready_tasks.append(task)
            else:
                landing_state = "blocked"
                label = "Review blocked" if queue_status == "done" and diff["error"] else _blocked_landing_label(queue_status, branch)
                counts["blocked"] += 1

            counts["total"] += 1
            item = {
                "task_id": task.id,
                "run_id": _task_run_id(raw_state),
                "branch": branch or None,
                "worktree": task.worktree,
                "summary": task.resolved_intent or _task_intent(task.command_argv),
                "queue_status": queue_status,
                "landing_state": landing_state,
                "label": label,
                "merge_id": merge_info.get("merge_id") if merge_info else None,
                "merge_status": merge_info.get("status") if merge_info else None,
                "merge_run_status": merge_info.get("merge_run_status") if merge_info else None,
                "duration_s": _number_from_mapping(raw_state, "duration_s"),
                "cost_usd": _number_from_mapping(raw_state, "cost_usd"),
                "stories_passed": _number_from_mapping(raw_state, "stories_passed"),
                "stories_tested": _number_from_mapping(raw_state, "stories_tested"),
            }
            item["changed_file_count"] = len(diff["files"])
            item["changed_files"] = diff["files"][:8]
            item["diff_error"] = diff["error"]
            items.append(item)

        return {
            "target": target,
            "items": items,
            "counts": counts,
            "collisions": _landing_collisions(self.project_dir, ready_tasks, target),
            **preflight,
        }

    def runtime_status(
        self,
        *,
        watcher: dict[str, Any] | None = None,
        landing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return build_runtime_status(
            self.project_dir,
            watcher=watcher or self.watcher_status(),
            landing=landing or self.landing_status(),
        )

    def events(self, *, limit: int = 80) -> dict[str, Any]:
        return events_status(self.project_dir, limit=limit)

    def start_watcher(self, *, concurrent: int | None = None, exit_when_empty: bool = False) -> dict[str, Any]:
        status = self.watcher_status()
        if status["alive"]:
            payload = {"ok": True, "message": "watcher already running", "refresh": True, "watcher": status}
            self._record_event(
                kind="watcher.start.skipped",
                severity="info",
                message=payload["message"],
                details={"state": status.get("health", {}).get("state")},
            )
            return payload
        health = status.get("health") if isinstance(status.get("health"), dict) else {}
        if health.get("state") != "stopped":
            message = str(health.get("next_action") or "Stop the stale watcher before starting another one.")
            self._record_event(
                kind="watcher.start.blocked",
                severity="warning",
                message=message,
                details={"state": health.get("state"), "blocking_pid": health.get("blocking_pid")},
            )
            raise MissionControlServiceError(message, status_code=409)
        concurrent_value = max(1, int(concurrent or 3))
        argv = [
            *_otto_cli_argv("queue", "run", "--no-dashboard"),
            "--concurrent",
            str(concurrent_value),
        ]
        if exit_when_empty:
            argv.append("--exit-when-empty")
        log_path = paths.logs_dir(self.project_dir) / "web" / "watcher.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["OTTO_NO_TUI"] = "1"
        try:
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\n[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] starting {' '.join(argv)}\n")
                proc = subprocess.Popen(
                    argv,
                    cwd=str(self.project_dir),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=env,
                    start_new_session=True,
                )
                supervisor = self._record_watcher_launch(
                    watcher_pid=proc.pid,
                    argv=argv,
                    log_path=log_path,
                    concurrent=concurrent_value,
                    exit_when_empty=exit_when_empty,
                )
        except OSError as exc:
            self._record_event(
                kind="watcher.start.failed",
                severity="error",
                message=f"watcher failed to start: {exc}",
                details={"argv": argv, "log_path": str(log_path)},
            )
            raise MissionControlServiceError(f"watcher failed to start: {exc}", status_code=500) from exc

        for _ in range(20):
            if proc.poll() is not None:
                tail = _tail_text(log_path)
                self._record_event(
                    kind="watcher.start.failed",
                    severity="error",
                    message=f"watcher exited immediately with {proc.returncode}",
                    details={"pid": proc.pid, "returncode": proc.returncode, "tail": tail, "log_path": str(log_path)},
                )
                raise MissionControlServiceError(
                    f"watcher exited immediately with {proc.returncode}: {tail}",
                    status_code=500,
                )
            fresh = self.watcher_status(probe_lock=False)
            if fresh["alive"]:
                payload = {
                    "ok": True,
                    "message": "watcher started",
                    "refresh": True,
                    "watcher": fresh,
                    "log_path": str(log_path),
                    "pid": proc.pid,
                    "supervisor": supervisor,
                }
                self._record_event(
                    kind="watcher.started",
                    severity="success",
                    message=payload["message"],
                    details={"pid": proc.pid, "concurrent": concurrent_value, "log_path": str(log_path)},
                )
                return payload
            time.sleep(0.1)
        payload = {
            "ok": True,
            "message": "watcher launch requested",
            "refresh": True,
            "watcher": self.watcher_status(),
            "log_path": str(log_path),
            "pid": proc.pid,
            "supervisor": supervisor,
        }
        self._record_event(
            kind="watcher.launch.requested",
            severity="info",
            message=payload["message"],
            details={"pid": proc.pid, "concurrent": concurrent_value, "log_path": str(log_path)},
        )
        return payload

    def stop_watcher(self) -> dict[str, Any]:
        status = self.watcher_status()
        watcher = status.get("watcher")
        raw_health = status.get("health")
        health = raw_health if isinstance(raw_health, dict) else {}
        pid = health.get("blocking_pid")
        if not health and (not isinstance(pid, int) or pid <= 0) and isinstance(watcher, dict):
            pid = watcher.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            payload = {"ok": True, "message": "watcher is not running", "refresh": True, "watcher": status}
            self._record_event(
                kind="watcher.stop.skipped",
                severity="info",
                message=payload["message"],
                details={"state": health.get("state")},
            )
            return payload
        identity_issue = _watcher_stop_identity_issue(self.project_dir, pid, health)
        if identity_issue is not None:
            self._record_event(
                kind="watcher.stop.blocked",
                severity="error",
                message=identity_issue,
                details={"pid": pid, "state": health.get("state")},
            )
            raise MissionControlServiceError(identity_issue, status_code=409)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            payload = {"ok": True, "message": "watcher already stopped", "refresh": True, "watcher": self.watcher_status()}
            self._record_event(
                kind="watcher.stop.skipped",
                severity="info",
                message=payload["message"],
                details={"pid": pid, "state": health.get("state")},
            )
            return payload
        except PermissionError as exc:
            self._record_event(
                kind="watcher.stop.failed",
                severity="error",
                message=f"watcher stop denied: {exc}",
                details={"pid": pid, "state": health.get("state")},
            )
            raise MissionControlServiceError(f"watcher stop denied: {exc}", status_code=403) from exc
        message = "stale watcher stop requested" if health.get("state") == "stale" else "watcher stop requested"
        supervisor = self._record_watcher_stop(target_pid=pid, reason=message)
        payload = {"ok": True, "message": message, "refresh": True, "watcher": self.watcher_status()}
        if supervisor is not None:
            payload["supervisor"] = supervisor
        self._record_event(
            kind="watcher.stop.requested",
            severity="warning" if health.get("state") == "stale" else "info",
            message=message,
            details={"pid": pid, "state": health.get("state")},
        )
        return payload

    def enqueue(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        command = command.strip().lower()
        explicit_as = _optional_str(payload.get("as") or payload.get("task_id"))
        after = _string_list(payload.get("after"))
        extra_args = _string_list(payload.get("extra_args"))

        try:
            if command == "build":
                intent = _required_str(payload.get("intent"), "intent")
                result = enqueue_task(
                    self.project_dir,
                    command="build",
                    raw_args=[intent, *extra_args],
                    intent=intent,
                    explicit_intent=intent,
                    after=after,
                    explicit_as=explicit_as,
                    resumable=True,
                )
            elif command == "improve":
                subcommand = _required_str(payload.get("subcommand"), "subcommand")
                if subcommand not in {"bugs", "feature", "target"}:
                    raise MissionControlServiceError("unsupported improve subcommand", status_code=400)
                focus_or_goal = _optional_str(payload.get("focus") or payload.get("goal"))
                raw_args = [subcommand]
                if focus_or_goal:
                    raw_args.append(focus_or_goal)
                raw_args.extend(extra_args)
                snapshot_intent = resolve_intent_for_enqueue(self.project_dir)
                result = enqueue_task(
                    self.project_dir,
                    command="improve",
                    raw_args=raw_args,
                    intent=snapshot_intent,
                    explicit_intent=focus_or_goal,
                    after=after,
                    explicit_as=explicit_as,
                    resumable=True,
                    focus=focus_or_goal if subcommand in {"bugs", "feature"} else None,
                    target=focus_or_goal if subcommand == "target" else None,
                )
            elif command == "certify":
                intent = _optional_str(payload.get("intent"))
                resolved = resolve_intent_for_enqueue(self.project_dir, explicit=intent)
                raw_args = [intent] if intent else []
                raw_args.extend(extra_args)
                result = enqueue_task(
                    self.project_dir,
                    command="certify",
                    raw_args=raw_args,
                    intent=resolved,
                    explicit_intent=intent,
                    after=after,
                    explicit_as=explicit_as,
                    resumable=False,
                )
            else:
                raise MissionControlServiceError("unsupported queue command", status_code=404)
        except ValueError as exc:
            raise MissionControlServiceError(str(exc), status_code=400) from exc

        response = {
            "ok": True,
            "message": f"queued {result.task.id}",
            "task": asdict(result.task),
            "warnings": result.warnings,
            "refresh": True,
        }
        self._record_event(
            kind=f"queue.{command}",
            severity="warning" if result.warnings else "success",
            message=response["message"],
            task_id=result.task.id,
            details={
                "command": command,
                "branch": result.task.branch,
                "worktree": result.task.worktree,
                "after": result.task.after,
                "warnings": result.warnings,
            },
        )
        return response

    def _state(self, filters: MissionControlFilters | None) -> MissionControlState:
        return self.model.initial_state(filters=filters or MissionControlFilters())

    def _detail_view(self, run_id: str, filters: MissionControlFilters | None) -> DetailView:
        del filters
        state = self._state(MissionControlFilters())
        state.selection = SelectionState(run_id=run_id)
        detail = self.model.detail_view(state)
        if detail is None:
            raise MissionControlServiceError("run not found", status_code=404)
        return detail

    def _validated_artifact_path(self, path: str) -> Path:
        candidate = Path(path).expanduser().resolve(strict=False)
        root = self.project_dir.resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise MissionControlServiceError("artifact path is outside the project", status_code=403) from exc
        return candidate

    def _read_file_slice(self, path: Path, *, offset: int, limit_bytes: int) -> LogReadResult:
        if not path.exists() or not path.is_file():
            return LogReadResult(str(path), offset, offset, "", False)
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(max(1, limit_bytes))
            next_offset = handle.tell()
        return LogReadResult(
            path=str(path),
            offset=offset,
            next_offset=next_offset,
            text=chunk.decode("utf-8", errors="replace"),
            exists=True,
        )

    def _record_event(
        self,
        *,
        kind: str,
        message: str,
        severity: str = "info",
        run_id: str | None = None,
        task_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        try:
            append_event(
                self.project_dir,
                kind=kind,
                message=message,
                severity=severity,
                run_id=run_id,
                task_id=task_id,
                details=details,
            )
        except Exception as exc:
            LOGGER.warning("mission control event write failed: %s", exc)
            return

    def _record_async_action_result(
        self,
        *,
        kind: str,
        result: Any,
        run_id: str | None = None,
        task_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload = serialize_action_result(result)
        merged_details = dict(details or {})
        merged_details["ok"] = payload.get("ok")
        self._record_event(
            kind=kind,
            severity=_event_severity(payload),
            message=payload.get("message") or kind,
            run_id=run_id,
            task_id=task_id,
            details=merged_details,
        )

    def _record_watcher_launch(
        self,
        *,
        watcher_pid: int,
        argv: list[str],
        log_path: Path,
        concurrent: int,
        exit_when_empty: bool,
    ) -> dict[str, Any] | None:
        try:
            return record_watcher_launch(
                self.project_dir,
                watcher_pid=watcher_pid,
                argv=argv,
                log_path=log_path,
                concurrent=concurrent,
                exit_when_empty=exit_when_empty,
            )
        except Exception as exc:
            self._record_event(
                kind="supervisor.write.failed",
                severity="warning",
                message=f"watcher supervisor metadata was not written: {exc}",
            )
            return None

    def _record_watcher_stop(self, *, target_pid: int, reason: str) -> dict[str, Any] | None:
        try:
            return record_watcher_stop(self.project_dir, target_pid=target_pid, reason=reason)
        except Exception as exc:
            self._record_event(
                kind="supervisor.write.failed",
                severity="warning",
                message=f"watcher supervisor stop metadata was not written: {exc}",
            )
            return None


def filters_from_params(
    *,
    active_only: bool = False,
    type_filter: str = "all",
    outcome_filter: str = "all",
    query: str = "",
    history_page: int = 0,
) -> MissionControlFilters:
    if type_filter not in {"all", "build", "improve", "certify", "merge", "queue"}:
        raise MissionControlServiceError("invalid type filter", status_code=400)
    if outcome_filter not in {"all", "success", "failed", "interrupted", "cancelled", "removed", "other"}:
        raise MissionControlServiceError("invalid outcome filter", status_code=400)
    return MissionControlFilters(
        active_only=bool(active_only),
        type_filter=type_filter,  # type: ignore[arg-type]
        outcome_filter=outcome_filter,  # type: ignore[arg-type]
        query=str(query or ""),
        history_page=max(0, int(history_page or 0)),
    )


def _review_packet(project_dir: Path, detail: DetailView) -> dict[str, Any]:
    record = detail.record
    display_status = "stale" if detail.overlay is not None and detail.overlay.level == "stale" else record.status
    target = _review_target(project_dir, record)
    if record.domain == "merge":
        return _merge_review_packet(project_dir, detail, display_status=display_status, target=target)
    branch = _optional_str(record.git.get("branch"))
    merge_info = _detail_merge_info(project_dir, detail)
    merged = merge_info is not None
    in_progress = display_status in REVIEW_IN_PROGRESS_STATUSES
    diff = (
        {"files": [], "error": None}
        if merged or in_progress
        else _branch_diff(project_dir, branch, target)
    )
    changed_files = diff["files"]
    certification = _certification_summary(record)
    evidence = [serialize_artifact(artifact, index) for index, artifact in enumerate(detail.artifacts)]
    merge_preflight = _merge_preflight(project_dir)
    readiness = _review_readiness(
        display_status=display_status,
        merged=merged,
        branch=branch,
        diff_error=diff["error"],
        target=target,
        overlay=detail.overlay,
        merge_preflight=merge_preflight,
    )
    next_action = (
        {"label": "No action", "action_key": None, "enabled": False, "reason": f"Already merged into {target}."}
        if merged
        else _suggested_next_action(display_status, detail.legal_actions, detail.overlay)
    )
    if not merged and next_action.get("action_key") == "m" and readiness.get("state") != "ready":
        reason = (
            "Commit, stash, or revert local project changes before landing."
            if display_status == "done" and merge_preflight.get("merge_blocked")
            else "Resolve review blockers before landing."
        )
        next_action = {
            "label": "Land blocked",
            "action_key": None,
            "enabled": False,
            "reason": reason,
        }
    return {
        "headline": _review_packet_headline(record, display_status, merged=merged, readiness=readiness, target=target),
        "status": "merged" if merged else display_status,
        "summary": _optional_str(record.intent.get("summary")) or record.display_name or record.run_id,
        "readiness": readiness,
        "checks": _review_checks(
            display_status=display_status,
            merged=merged,
            branch=branch,
            target=target,
            diff=diff,
            certification=certification,
            evidence=evidence,
            readiness=readiness,
        ),
        "next_action": next_action,
        "certification": certification,
        "changes": {
            "branch": branch,
            "target": target,
            "merged": merged,
            "merge_id": merge_info.get("merge_id") if merge_info else None,
            "file_count": len(changed_files),
            "files": changed_files[:12],
            "truncated": len(changed_files) > 12,
            "diff_command": None if merged or in_progress else f"git diff {target}...{branch}" if branch and branch != target else None,
            "diff_error": diff["error"],
        },
        "evidence": evidence,
        "failure": _failure_summary(record, detail.overlay),
    }


def _merge_review_packet(project_dir: Path, detail: DetailView, *, display_status: str, target: str) -> dict[str, Any]:
    record = detail.record
    merge_id = _optional_str(record.identity.get("merge_id")) or record.run_id
    certification = _certification_summary(record)
    evidence = [serialize_artifact(artifact, index) for index, artifact in enumerate(detail.artifacts)]
    terminal_success = display_status == "done" or record.terminal_outcome == "success"
    needs_attention = display_status in {"failed", "cancelled", "interrupted", "stale"}
    if terminal_success:
        readiness = {
            "state": "merged",
            "label": f"Landed in {target}",
            "tone": "success",
            "blockers": [],
            "next_step": "Audit the landing record, artifacts, and final logs if needed.",
        }
        headline = f"Landed in {target}"
    elif display_status in REVIEW_IN_PROGRESS_STATUSES:
        readiness = {
            "state": "in_progress",
            "label": "Landing in progress",
            "tone": "info",
            "blockers": ["Wait for the landing run to finish."],
            "next_step": "Watch logs or wait for completion.",
        }
        headline = "Landing in progress"
    elif needs_attention:
        reason = detail.overlay.reason if detail.overlay is not None else f"Landing status is {display_status}."
        readiness = {
            "state": "needs_attention",
            "label": "Landing needs action",
            "tone": "danger",
            "blockers": [reason],
            "next_step": "Inspect merge logs and resolve the landing failure.",
        }
        headline = "Landing failed"
    else:
        readiness = {
            "state": "blocked",
            "label": "Landing audit",
            "tone": "warning",
            "blockers": [f"Landing status is {display_status or 'unknown'}."],
            "next_step": "Inspect merge logs before taking further action.",
        }
        headline = "Landing audit"
    return {
        "headline": headline,
        "status": "merged" if terminal_success else display_status,
        "summary": _optional_str(record.intent.get("summary")) or record.display_name or record.run_id,
        "readiness": readiness,
        "checks": _merge_review_checks(
            display_status=display_status,
            target=target,
            certification=certification,
            evidence=evidence,
            readiness=readiness,
        ),
        "next_action": {"label": "No action", "action_key": None, "enabled": False, "reason": "Landing runs are audit records."},
        "certification": certification,
        "changes": {
            "branch": _optional_str(record.git.get("branch")),
            "target": target,
            "merged": terminal_success,
            "merge_id": merge_id,
            "file_count": 0,
            "files": [],
            "truncated": False,
            "diff_command": None,
            "diff_error": None,
        },
        "evidence": evidence,
        "failure": _failure_summary(record, detail.overlay),
    }


def _review_target(project_dir: Path, record: Any) -> str:
    target = _optional_str(record.git.get("target_branch")) if hasattr(record, "git") else None
    if target:
        return target
    if getattr(record, "domain", None) == "merge":
        merge_id = _optional_str(record.identity.get("merge_id")) if hasattr(record, "identity") else None
        merge_id = merge_id or _optional_str(getattr(record, "run_id", None))
        if merge_id:
            try:
                state = load_merge_state(project_dir, merge_id)
            except Exception:
                state = None
            if state is not None:
                state_target = _optional_str(state.target)
                if state_target:
                    return state_target
    return _merge_target(project_dir)


def _merge_review_checks(
    *,
    display_status: str,
    target: str,
    certification: dict[str, Any],
    evidence: list[dict[str, Any]],
    readiness: dict[str, Any],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    terminal_success = readiness["state"] == "merged"
    if terminal_success:
        checks.append(_review_check("run", "Landing run", "pass", f"Landing completed into {target}."))
    elif display_status in REVIEW_IN_PROGRESS_STATUSES:
        checks.append(_review_check("run", "Landing run", "pending", "Landing is still in flight."))
    else:
        checks.append(_review_check("run", "Landing run", "fail", f"Landing status is {display_status or 'unknown'}."))

    stories_tested = _int_or_none(certification.get("stories_tested"))
    stories_passed = _int_or_none(certification.get("stories_passed"))
    if stories_tested and stories_passed is not None and stories_passed >= stories_tested:
        checks.append(_review_check("certification", "Post-landing certification", "pass", f"{stories_passed}/{stories_tested} stories passed."))
    elif stories_tested and stories_passed is not None:
        checks.append(_review_check("certification", "Post-landing certification", "fail", f"{stories_passed}/{stories_tested} stories passed."))
    else:
        checks.append(_review_check("certification", "Post-landing certification", "info", "No post-landing story count was recorded."))

    existing_evidence = [item for item in evidence if item.get("exists")]
    if existing_evidence:
        checks.append(_review_check("evidence", "Evidence", "pass", f"{len(existing_evidence)} artifact{'' if len(existing_evidence) == 1 else 's'} available."))
    else:
        checks.append(_review_check("evidence", "Evidence", "warn", "No readable landing artifacts are attached."))

    if terminal_success:
        checks.append(_review_check("landing", "Landing state", "pass", "No further landing action is needed."))
    else:
        detail = "; ".join(str(item) for item in readiness.get("blockers", []) if item) or "Landing is not complete."
        checks.append(_review_check("landing", "Landing state", "fail", detail))
    return checks


def _review_readiness(
    *,
    display_status: str,
    merged: bool,
    branch: str | None,
    diff_error: str | None,
    target: str,
    overlay: Any,
    merge_preflight: dict[str, Any],
) -> dict[str, Any]:
    blockers: list[str] = []
    if merged:
        return {
            "state": "merged",
            "label": f"Landed in {target}",
            "tone": "success",
            "blockers": blockers,
            "next_step": "No merge action is needed.",
        }
    if display_status in REVIEW_IN_PROGRESS_STATUSES:
        label = {
            "queued": "Queued",
            "starting": "Starting",
            "running": "Running",
            "terminating": "Stopping",
        }.get(display_status, "In progress")
        next_step = (
            "Start the watcher when you want this queued task to run."
            if display_status == "queued"
            else "Watch logs or wait for completion."
        )
        return {
            "state": "in_progress",
            "label": label,
            "tone": "info",
            "blockers": ["Wait for the task to finish before review."],
            "next_step": next_step,
        }
    if display_status == "done":
        if not branch:
            blockers.append("No source branch was recorded for this task.")
        if diff_error:
            blockers.append(f"Changed files could not be inspected: {diff_error}")
        if merge_preflight.get("merge_blocked"):
            blockers.append(_merge_preflight_review_blocker(merge_preflight))
        if blockers:
            return {
                "state": "blocked",
                "label": "Review blocked",
                "tone": "danger",
                "blockers": blockers,
                "next_step": "Fix the branch or repository state, then refresh.",
            }
        return {
            "state": "ready",
            "label": f"Ready to land in {target}",
            "tone": "success",
            "blockers": blockers,
            "next_step": "Review evidence and land the task.",
        }
    if display_status in {"failed", "cancelled", "interrupted", "stale"}:
        reason = overlay.reason if overlay is not None else f"Run status is {display_status}."
        return {
            "state": "needs_attention",
            "label": "Needs action",
            "tone": "warning" if display_status in {"interrupted", "stale"} else "danger",
            "blockers": [reason],
            "next_step": "Inspect failure evidence and retry, resume, requeue, or remove.",
        }
    return {
        "state": "blocked",
        "label": "Not ready",
        "tone": "warning",
        "blockers": [f"Run status is {display_status or 'unknown'}."],
        "next_step": "Inspect the run before taking action.",
    }


def _review_checks(
    *,
    display_status: str,
    merged: bool,
    branch: str | None,
    target: str,
    diff: dict[str, Any],
    certification: dict[str, Any],
    evidence: list[dict[str, Any]],
    readiness: dict[str, Any],
) -> list[dict[str, Any]]:
    changed_files = list(diff.get("files") or [])
    diff_error = _optional_str(diff.get("error"))
    existing_evidence = [item for item in evidence if item.get("exists")]
    missing_evidence = [item for item in evidence if not item.get("exists")]
    stories_tested = _int_or_none(certification.get("stories_tested"))
    stories_passed = _int_or_none(certification.get("stories_passed"))

    checks: list[dict[str, Any]] = []
    if merged:
        checks.append(_review_check("run", "Run finished", "pass", f"Already landed in {target}."))
    elif display_status == "done":
        checks.append(_review_check("run", "Run finished", "pass", "Task completed and is ready for human review."))
    elif display_status in REVIEW_IN_PROGRESS_STATUSES:
        run_label = "Waiting to start" if display_status == "queued" else "Run in progress"
        run_detail = (
            "The watcher has not started this queued task yet."
            if display_status == "queued"
            else "Task is still in flight."
        )
        checks.append(_review_check("run", run_label, "pending", run_detail))
    else:
        checks.append(_review_check("run", "Run finished", "fail", f"Run status is {display_status or 'unknown'}."))

    if display_status in REVIEW_IN_PROGRESS_STATUSES:
        checks.append(_review_check("certification", "Certification", "pending", "Certification is pending until the task finishes."))
    elif stories_tested and stories_passed is not None and stories_passed >= stories_tested:
        checks.append(_review_check("certification", "Certification", "pass", f"{stories_passed}/{stories_tested} stories passed."))
    elif stories_tested and stories_passed is not None:
        checks.append(_review_check("certification", "Certification", "fail", f"{stories_passed}/{stories_tested} stories passed."))
    else:
        checks.append(_review_check("certification", "Certification", "warn", "No story pass count was recorded. Inspect artifacts before landing."))

    if display_status in REVIEW_IN_PROGRESS_STATUSES:
        checks.append(_review_check("changes", "Changed files", "pending", "Changed files are available after the task creates its branch."))
    elif diff_error:
        checks.append(_review_check("changes", "Changed files", "fail", diff_error))
    elif changed_files:
        checks.append(_review_check("changes", "Changed files", "pass", f"{len(changed_files)} file{'' if len(changed_files) == 1 else 's'} changed on {branch}."))
    elif merged:
        checks.append(_review_check("changes", "Changed files", "info", "No unlanded diff remains."))
    else:
        checks.append(_review_check("changes", "Changed files", "warn", "No changed files were detected. Confirm the task produced the expected artifact."))

    if display_status in REVIEW_IN_PROGRESS_STATUSES and not existing_evidence:
        checks.append(_review_check("evidence", "Evidence", "pending", "Evidence is available after the task writes artifacts."))
    elif existing_evidence and not missing_evidence:
        checks.append(_review_check("evidence", "Evidence", "pass", f"{len(existing_evidence)} artifact{'' if len(existing_evidence) == 1 else 's'} available."))
    elif existing_evidence:
        checks.append(
            _review_check(
                "evidence",
                "Evidence",
                "warn",
                f"{len(existing_evidence)} available, {len(missing_evidence)} missing.",
            )
        )
    else:
        checks.append(
            _review_check(
                "evidence",
                "Evidence",
                "warn",
                "No readable artifacts are attached; use stories and changed files as proof before landing.",
            )
        )

    if readiness["state"] == "ready":
        checks.append(_review_check("landing", "Landing action", "pass", f"Safe to land into {target}."))
    elif readiness["state"] == "merged":
        checks.append(_review_check("landing", "Landing action", "pass", "Task is already landed."))
    elif readiness["state"] == "in_progress":
        checks.append(_review_check("landing", "Landing action", "pending", "Landing is disabled until the task completes."))
    else:
        detail = "; ".join(str(item) for item in readiness.get("blockers", []) if item) or "Landing is disabled."
        checks.append(_review_check("landing", "Landing action", "fail", detail))

    return checks


def _review_check(key: str, label: str, status: str, detail: str) -> dict[str, str]:
    return {"key": key, "label": label, "status": status, "detail": detail}


def _review_packet_headline(
    record: Any,
    display_status: str,
    *,
    merged: bool,
    readiness: dict[str, Any],
    target: str,
) -> str:
    if merged:
        return f"Already merged into {target}"
    if readiness.get("state") == "blocked" and display_status == "done":
        blockers = [str(item) for item in readiness.get("blockers", []) if item]
        if any(item.startswith("Repository has local changes") for item in blockers):
            return "Repository cleanup required before landing"
        return "Review blocked before landing"
    return _review_headline(record, display_status)


def _review_headline(record: Any, display_status: str) -> str:
    if display_status == "done":
        return "Ready for review"
    if display_status == "failed":
        return "Failed; review evidence and requeue or remove"
    if display_status == "stale":
        return "Stale; stop or remove the orphaned work"
    if display_status == "queued":
        return "Waiting for watcher"
    if display_status in REVIEW_IN_PROGRESS_STATUSES:
        return "In progress"
    if display_status == "interrupted":
        return "Interrupted; resume or requeue"
    summary = _optional_str(record.intent.get("summary")) if hasattr(record, "intent") else None
    return summary or str(display_status or "Run detail")


def _suggested_next_action(
    display_status: str,
    actions: list[Any],
    overlay: Any,
) -> dict[str, Any]:
    if display_status == "queued":
        return {
            "label": "Start watcher",
            "action_key": None,
            "enabled": False,
            "reason": "Use Start watcher in the sidebar to run queued work.",
        }
    by_key = {action.key: action for action in actions}
    preferred = {
        "failed": ["R", "x"],
        "interrupted": ["r", "R", "x"],
        "stale": ["x", "c"],
        "done": ["m", "x"],
        "running": ["c"],
        "starting": ["c"],
        "queued": ["x"],
    }.get(display_status, [])
    for key in preferred:
        action = by_key.get(key)
        if action is not None and action.enabled:
            return {
                "label": _review_action_label(action.key, action.label),
                "action_key": action.key,
                "enabled": True,
                "reason": action.preview,
            }
    for action in actions:
        if action.enabled:
            return {
                "label": _review_action_label(action.key, action.label),
                "action_key": action.key,
                "enabled": True,
                "reason": action.preview,
            }
    reason = overlay.reason if overlay is not None else "No safe action is currently enabled."
    return {"label": "No action", "action_key": None, "enabled": False, "reason": reason}


def _review_action_label(key: str, label: str) -> str:
    return "Land selected" if key == "m" else label


def _apply_landing_context(project_dir: Path, payload: dict[str, Any], detail: DetailView) -> None:
    merge_info = _detail_merge_info(project_dir, detail)
    if merge_info is None:
        payload["landing_state"] = None
        return
    target = str(merge_info.get("target") or _merge_target(project_dir))
    payload["landing_state"] = "merged"
    payload["merge_info"] = merge_info
    for action in payload.get("legal_actions", []):
        if isinstance(action, dict) and action.get("key") == "m":
            action["enabled"] = False
            action["reason"] = f"Already merged into {target}."
            action["preview"] = f"Already merged into {target}."


def _detail_merge_info(project_dir: Path, detail: DetailView) -> dict[str, Any] | None:
    branch = _optional_str(detail.record.git.get("branch"))
    if not branch:
        return None
    target = str(detail.record.git.get("target_branch") or _merge_target(project_dir))
    info = _merged_branch_index(project_dir, target).get(branch)
    if info is None:
        return None
    return {**info, "target": target}


def _certification_summary(record: Any) -> dict[str, Any]:
    summary = _summary_for_record(record)
    metrics = getattr(record, "metrics", {}) if isinstance(getattr(record, "metrics", {}), dict) else {}
    stories_tested = _int_or_none(metrics.get("stories_tested"))
    stories_passed = _int_or_none(metrics.get("stories_passed"))
    if isinstance(summary, dict):
        stories_tested = stories_tested if stories_tested is not None else _int_or_none(summary.get("stories_tested"))
        stories_passed = stories_passed if stories_passed is not None else _int_or_none(summary.get("stories_passed"))
        if stories_tested is None:
            stories_tested = _int_or_none(summary.get("stories_total_count"))
    return {
        "stories_passed": stories_passed,
        "stories_tested": stories_tested,
        "passed": (
            stories_passed is not None
            and stories_tested is not None
            and stories_tested > 0
            and stories_passed >= stories_tested
        ),
        "summary_path": _optional_str(getattr(record, "artifacts", {}).get("summary_path")),
    }


def _summary_for_record(record: Any) -> dict[str, Any] | None:
    artifacts = getattr(record, "artifacts", {}) if isinstance(getattr(record, "artifacts", {}), dict) else {}
    candidates = [
        artifacts.get("summary_path"),
        Path(str(artifacts.get("session_dir"))) / "summary.json" if artifacts.get("session_dir") else None,
    ]
    for candidate in candidates:
        text = _optional_str(candidate)
        if not text:
            continue
        value = _read_json_object(Path(text).expanduser())
        if value is not None:
            return value
    return None


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _failure_summary(record: Any, overlay: Any) -> dict[str, Any] | None:
    status = str(getattr(record, "status", "") or "")
    overlay_reason = overlay.reason if overlay is not None else None
    last_event = _optional_str(getattr(record, "last_event", None))
    if status not in {"failed", "cancelled", "interrupted"} and not overlay_reason:
        return None
    return {
        "reason": overlay_reason or last_event or status,
        "last_event": last_event,
    }


def _action_key(action: str) -> str:
    mapping = {
        "cancel": "c",
        "resume": "r",
        "retry": "R",
        "requeue": "R",
        "cleanup": "x",
        "remove": "x",
        "merge": "m",
        "open": "e",
    }
    return mapping.get(action, action)


def _event_action_name(key: str, *, label: str | None = None, domain: str | None = None) -> str:
    if key == "R":
        normalized = str(label or "").strip().lower()
        if normalized == "retry" or domain in {"atomic", "merge"}:
            return "retry"
        return "requeue"
    return {
        "c": "cancel",
        "r": "resume",
        "x": "cleanup",
        "m": "merge",
        "e": "open",
    }.get(key, key)


def _event_severity(payload: dict[str, Any]) -> str:
    if payload.get("ok") is False:
        return "error"
    value = str(payload.get("severity") or "").strip().lower()
    if value == "information":
        return "info"
    if value in {"error", "warning", "info", "success"}:
        return value
    return "success"


def _watcher_stop_identity_issue(project_dir: Path, pid: int, health: dict[str, Any]) -> str | None:
    lock_pid = _int_or_none(health.get("lock_pid"))
    if lock_pid == pid:
        return None
    supervisor, supervisor_error = read_supervisor(project_dir)
    supervisor_pid = _int_or_none(supervisor.get("watcher_pid") if supervisor else None)
    if supervisor_pid == pid:
        return None
    if supervisor_error:
        return f"Refusing to stop watcher pid {pid}; supervisor metadata is unreadable: {supervisor_error}"
    return (
        f"Refusing to stop pid {pid}; Mission Control could not verify that it owns the watcher. "
        "Use a terminal if this process must be stopped manually."
    )


def _merge_target(project_dir: Path) -> str:
    try:
        cfg = load_config(project_dir / "otto.yaml")
    except Exception:
        cfg = {}
    return str(cfg.get("default_branch") or "main")


def _merge_preflight(project_dir: Path) -> dict[str, Any]:
    try:
        issues = repo_preflight_issues(project_dir)
    except Exception as exc:
        return {
            "merge_blocked": True,
            "merge_blockers": [f"merge preflight failed: {exc}"],
            "dirty_files": [],
        }
    blockers = [*issues.get("blocking", []), *issues.get("dirty", [])]
    return {
        "merge_blocked": bool(blockers),
        "merge_blockers": blockers,
        "dirty_files": list(issues.get("dirty_files", []) or []),
    }


def _ensure_merge_unblocked(project_dir: Path) -> None:
    preflight = _merge_preflight(project_dir)
    if not preflight["merge_blocked"]:
        return
    blockers = "; ".join(preflight["merge_blockers"]) or "repository is not merge-ready"
    dirty_files = list(preflight.get("dirty_files", []) or [])
    suffix = ""
    if dirty_files:
        suffix = f" Affected paths: {', '.join(dirty_files[:5])}"
        if len(dirty_files) > 5:
            suffix += f", ... (+{len(dirty_files) - 5} more)"
        suffix += "."
    raise MissionControlServiceError(
        f"Merge blocked by local repository state: {blockers}.{suffix} "
        "Commit, stash, or revert these project changes before merging.",
        status_code=409,
    )


def _merge_preflight_review_blocker(preflight: dict[str, Any]) -> str:
    dirty_files = list(preflight.get("dirty_files", []) or [])
    if dirty_files:
        preview = ", ".join(str(path) for path in dirty_files[:3])
        if len(dirty_files) > 3:
            preview += f", ... (+{len(dirty_files) - 3} more)"
        return f"Repository has local changes: {preview}."
    blockers = [str(item) for item in preflight.get("merge_blockers", []) or [] if item]
    if blockers:
        return f"Repository is not ready to land: {'; '.join(blockers)}."
    return "Repository is not ready to land."


def _merged_branch_index(project_dir: Path, target: str) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for state_path in sorted(paths.merge_dir(project_dir).glob("*/state.json")):
        try:
            state = load_merge_state(project_dir, state_path.parent.name)
        except Exception:
            continue
        if str(state.target or "") != target:
            continue
        for outcome in state.outcomes:
            if outcome.status not in {"merged", "conflict_resolved"}:
                continue
            if outcome.merge_commit and not _merge_commit_reachable(project_dir, outcome.merge_commit, target):
                continue
            merged[outcome.branch] = {
                "merge_id": state.merge_id,
                "status": outcome.status,
                "merge_run_status": state.status,
            }
    return merged


def _merge_commit_reachable(project_dir: Path, merge_commit: str, target: str) -> bool:
    result = git_ops.run_git(project_dir, "merge-base", "--is-ancestor", merge_commit, target)
    return result.returncode == 0


def _branch_diff(project_dir: Path, branch: str | None, target: str) -> dict[str, Any]:
    branch = str(branch or "").strip()
    target = str(target or "").strip()
    if not branch or not target or branch == target:
        return {"files": [], "error": None}
    target_ref = _git_diff_ref(project_dir, target)
    branch_ref = _git_diff_ref(project_dir, branch)
    result = git_ops.run_git(project_dir, "diff", "--name-only", f"{target_ref}...{branch_ref}")
    if not result.ok:
        detail = (result.stderr or result.stdout or f"git diff exited {result.returncode}").strip()
        return {"files": [], "error": detail}
    return {"files": sorted(line for line in result.stdout.splitlines() if line), "error": None}


def _branch_diff_text(project_dir: Path, branch: str | None, target: str) -> dict[str, Any]:
    branch = str(branch or "").strip()
    target = str(target or "").strip()
    if not branch or not target or branch == target:
        return {"text": "", "error": None}
    target_ref = _git_diff_ref(project_dir, target)
    branch_ref = _git_diff_ref(project_dir, branch)
    result = git_ops.run_git(project_dir, "diff", "--no-ext-diff", "--no-color", f"{target_ref}...{branch_ref}")
    if not result.ok:
        detail = (result.stderr or result.stdout or f"git diff exited {result.returncode}").strip()
        return {"text": "", "error": detail}
    return {"text": result.stdout, "error": None}


def _git_diff_ref(project_dir: Path, ref: str) -> str:
    if git_ops.run_git(project_dir, "rev-parse", "--verify", "--quiet", ref).ok:
        return ref
    remote_ref = f"origin/{ref}"
    if git_ops.run_git(project_dir, "rev-parse", "--verify", "--quiet", remote_ref).ok:
        return remote_ref
    return ref


def _branch_changed_files(project_dir: Path, branch: str | None, target: str) -> list[str]:
    return list(_branch_diff(project_dir, branch, target)["files"])


def _landing_collisions(project_dir: Path, ready_tasks: list[Any], target: str) -> list[dict[str, Any]]:
    if len(ready_tasks) < 2:
        return []
    files_by_id: dict[str, set[str]] = {}
    for task in ready_tasks:
        branch = str(getattr(task, "branch", "") or "").strip()
        if not branch:
            continue
        files_by_id[task.id] = set(_branch_diff(project_dir, branch, target)["files"])
    collisions: list[dict[str, Any]] = []
    ids = [task.id for task in ready_tasks if task.id in files_by_id]
    for index, left in enumerate(ids):
        for right in ids[index + 1:]:
            common = sorted(files_by_id[left] & files_by_id[right])
            if not common:
                continue
            collisions.append(
                {
                    "left": left,
                    "right": right,
                    "files": common[:6],
                    "file_count": len(common),
                }
            )
    return collisions


def _task_run_id(raw_state: Any) -> str | None:
    if isinstance(raw_state, dict):
        value = raw_state.get("attempt_run_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _task_intent(argv: Any) -> str | None:
    if isinstance(argv, list) and len(argv) > 1 and isinstance(argv[1], str):
        return argv[1]
    return None


def _queue_display_status(raw_state: dict[str, Any] | None, queue_state: dict[str, Any]) -> str:
    status = task_display_status(raw_state)
    if status not in IN_FLIGHT_STATUSES:
        return status
    if watcher_alive(queue_state):
        return status
    child = raw_state.get("child") if isinstance(raw_state, dict) else None
    if isinstance(child, dict) and child_is_alive(child):
        return status
    return "stale"


def _blocked_landing_label(queue_status: str, branch: str) -> str:
    if not branch:
        return "No branch"
    if queue_status == "queued":
        return "Queued"
    if queue_status in {"starting", "running", "terminating"}:
        return "In progress"
    if queue_status in {"failed", "cancelled", "interrupted", "stale"}:
        return "Needs attention"
    return "Not ready"


def _number_from_mapping(raw_state: Any, key: str) -> int | float | None:
    if not isinstance(raw_state, dict):
        return None
    value = raw_state.get(key)
    return value if isinstance(value, (int, float)) else None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return None


def _required_str(value: Any, field: str) -> str:
    text = _optional_str(value)
    if text is None:
        raise MissionControlServiceError(f"{field} is required", status_code=400)
    return text


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MissionControlServiceError("expected a list of strings", status_code=400)
    return [str(item) for item in value if str(item).strip()]


def _tail_text(path: Path, *, limit: int = 4000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace").strip()
