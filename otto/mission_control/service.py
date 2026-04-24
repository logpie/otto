"""Shared Mission Control service for non-TUI clients."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from otto.config import resolve_intent_for_enqueue
from otto import paths
from otto.mission_control.actions import _otto_cli_argv
from otto.mission_control.actions import execute_action, execute_merge_all
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
from otto.queue.enqueue import enqueue_task
from otto.queue.runtime import task_display_status, watcher_alive
from otto.queue.schema import load_queue, load_state


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
        payload["watcher"] = self.watcher_status()
        return payload

    def detail(self, run_id: str, filters: MissionControlFilters | None = None) -> dict[str, Any]:
        return serialize_detail(self._detail_view(run_id, filters))

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

        selected_artifact_path = None
        if key == "e":
            index = 0 if artifact_index is None else artifact_index
            if index < 0 or index >= len(detail.artifacts):
                raise MissionControlServiceError("artifact index out of range", status_code=404)
            selected_artifact_path = str(self._validated_artifact_path(detail.artifacts[index].path))

        result = execute_action(
            detail.record,
            key,
            self.project_dir,
            selected_artifact_path=selected_artifact_path,
            selected_queue_task_ids=selected_queue_task_ids,
        )
        return serialize_action_result(result)

    def merge_all(self) -> dict[str, Any]:
        return serialize_action_result(execute_merge_all(self.project_dir))

    def watcher_status(self) -> dict[str, Any]:
        try:
            state = load_state(self.project_dir)
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
            status = task_display_status(raw if isinstance(raw, dict) else None)
            counts[status] = counts.get(status, 0) + 1
        watcher = state.get("watcher") if isinstance(state, dict) else None
        return {
            "alive": watcher_alive(state) if isinstance(state, dict) else False,
            "watcher": watcher if isinstance(watcher, dict) else None,
            "counts": counts,
        }

    def start_watcher(self, *, concurrent: int | None = None, exit_when_empty: bool = False) -> dict[str, Any]:
        status = self.watcher_status()
        if status["alive"]:
            return {"ok": True, "message": "watcher already running", "refresh": True, "watcher": status}
        argv = [
            *_otto_cli_argv("queue", "run", "--no-dashboard"),
            "--concurrent",
            str(max(1, int(concurrent or 3))),
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
        except OSError as exc:
            raise MissionControlServiceError(f"watcher failed to start: {exc}", status_code=500) from exc

        for _ in range(20):
            if proc.poll() is not None:
                tail = _tail_text(log_path)
                raise MissionControlServiceError(
                    f"watcher exited immediately with {proc.returncode}: {tail}",
                    status_code=500,
                )
            fresh = self.watcher_status()
            if fresh["alive"]:
                return {
                    "ok": True,
                    "message": "watcher started",
                    "refresh": True,
                    "watcher": fresh,
                    "log_path": str(log_path),
                    "pid": proc.pid,
                }
            time.sleep(0.1)
        return {
            "ok": True,
            "message": "watcher launch requested",
            "refresh": True,
            "watcher": self.watcher_status(),
            "log_path": str(log_path),
            "pid": proc.pid,
        }

    def stop_watcher(self) -> dict[str, Any]:
        status = self.watcher_status()
        watcher = status.get("watcher")
        if not status.get("alive") or not isinstance(watcher, dict):
            return {"ok": True, "message": "watcher is not running", "refresh": True, "watcher": status}
        pid = watcher.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            raise MissionControlServiceError("watcher pid unavailable", status_code=409)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return {"ok": True, "message": "watcher already stopped", "refresh": True, "watcher": self.watcher_status()}
        except PermissionError as exc:
            raise MissionControlServiceError(f"watcher stop denied: {exc}", status_code=403) from exc
        return {"ok": True, "message": "watcher stop requested", "refresh": True, "watcher": self.watcher_status()}

    def enqueue(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        command = command.strip().lower()
        explicit_as = _optional_str(payload.get("as") or payload.get("task_id"))
        after = _string_list(payload.get("after"))
        extra_args = _string_list(payload.get("extra_args"))

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

        return {
            "ok": True,
            "message": f"queued {result.task.id}",
            "task": asdict(result.task),
            "warnings": result.warnings,
            "refresh": True,
        }

    def _state(self, filters: MissionControlFilters | None) -> MissionControlState:
        return self.model.initial_state(filters=filters or MissionControlFilters())

    def _detail_view(self, run_id: str, filters: MissionControlFilters | None) -> DetailView:
        state = self._state(filters)
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
