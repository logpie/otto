"""Shared Mission Control service for non-TUI clients."""

from __future__ import annotations

import json
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
from otto.queue.enqueue import enqueue_task
from otto.queue.runtime import IN_FLIGHT_STATUSES, task_display_status, watcher_alive
from otto.queue.runner import child_is_alive
from otto.queue.schema import load_queue, load_state as load_queue_state


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
        return payload

    def detail(self, run_id: str, filters: MissionControlFilters | None = None) -> dict[str, Any]:
        detail = self._detail_view(run_id, filters)
        payload = serialize_detail(detail)
        payload["review_packet"] = _review_packet(self.project_dir, detail)
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
            _ensure_merge_unblocked(self.project_dir)

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
        _ensure_merge_unblocked(self.project_dir)
        return serialize_action_result(execute_merge_all(self.project_dir))

    def watcher_status(self) -> dict[str, Any]:
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
        health = watcher_health(self.project_dir, state if isinstance(state, dict) else {})
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
        merged_by_branch = _merged_branch_index(self.project_dir)
        target = _merge_target(self.project_dir)
        preflight = _merge_preflight(self.project_dir)

        items: list[dict[str, Any]] = []
        ready_tasks: list[Any] = []
        counts = {"ready": 0, "merged": 0, "blocked": 0, "total": 0}
        for task in tasks:
            raw_state = task_states.get(task.id) if isinstance(task_states, dict) else None
            queue_status = _queue_display_status(raw_state if isinstance(raw_state, dict) else None, state)
            branch = str(task.branch or "").strip()
            merge_info = merged_by_branch.get(branch)
            if merge_info is not None:
                landing_state = "merged"
                label = "Merged"
                counts["merged"] += 1
            elif queue_status == "done" and branch:
                landing_state = "ready"
                label = "Ready to merge"
                counts["ready"] += 1
                ready_tasks.append(task)
            else:
                landing_state = "blocked"
                label = _blocked_landing_label(queue_status, branch)
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
            changed_files = _branch_changed_files(self.project_dir, branch, target)
            item["changed_file_count"] = len(changed_files)
            item["changed_files"] = changed_files[:8]
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
        raw_health = status.get("health")
        health = raw_health if isinstance(raw_health, dict) else {}
        pid = health.get("blocking_pid")
        if not health and (not isinstance(pid, int) or pid <= 0) and isinstance(watcher, dict):
            pid = watcher.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return {"ok": True, "message": "watcher is not running", "refresh": True, "watcher": status}
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return {"ok": True, "message": "watcher already stopped", "refresh": True, "watcher": self.watcher_status()}
        except PermissionError as exc:
            raise MissionControlServiceError(f"watcher stop denied: {exc}", status_code=403) from exc
        message = "stale watcher stop requested" if health.get("state") == "stale" else "watcher stop requested"
        return {"ok": True, "message": message, "refresh": True, "watcher": self.watcher_status()}

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
    target = str(record.git.get("target_branch") or _merge_target(project_dir))
    branch = _optional_str(record.git.get("branch"))
    changed_files = _branch_changed_files(project_dir, branch, target)
    return {
        "headline": _review_headline(record, display_status),
        "status": display_status,
        "summary": _optional_str(record.intent.get("summary")) or record.display_name or record.run_id,
        "next_action": _suggested_next_action(display_status, detail.legal_actions, detail.overlay),
        "certification": _certification_summary(record),
        "changes": {
            "branch": branch,
            "target": target,
            "file_count": len(changed_files),
            "files": changed_files[:12],
            "truncated": len(changed_files) > 12,
            "diff_command": f"git diff {target}...{branch}" if branch and branch != target else None,
        },
        "evidence": [serialize_artifact(artifact, index) for index, artifact in enumerate(detail.artifacts)],
        "failure": _failure_summary(record, detail.overlay),
    }


def _review_headline(record: Any, display_status: str) -> str:
    if display_status == "done":
        return "Ready for review"
    if display_status == "failed":
        return "Failed; review evidence and requeue or remove"
    if display_status == "stale":
        return "Stale; stop or remove the orphaned work"
    if display_status in {"running", "starting", "queued"}:
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
                "label": action.label,
                "action_key": action.key,
                "enabled": True,
                "reason": action.preview,
            }
    for action in actions:
        if action.enabled:
            return {
                "label": action.label,
                "action_key": action.key,
                "enabled": True,
                "reason": action.preview,
            }
    reason = overlay.reason if overlay is not None else "No safe action is currently enabled."
    return {"label": "No action", "action_key": None, "enabled": False, "reason": reason}


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


def _merged_branch_index(project_dir: Path) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for state_path in sorted(paths.merge_dir(project_dir).glob("*/state.json")):
        try:
            state = load_merge_state(project_dir, state_path.parent.name)
        except Exception:
            continue
        for outcome in state.outcomes:
            if outcome.status not in {"merged", "conflict_resolved"}:
                continue
            merged[outcome.branch] = {
                "merge_id": state.merge_id,
                "status": outcome.status,
                "merge_run_status": state.status,
            }
    return merged


def _branch_changed_files(project_dir: Path, branch: str | None, target: str) -> list[str]:
    branch = str(branch or "").strip()
    target = str(target or "").strip()
    if not branch or not target or branch == target:
        return []
    try:
        return sorted(git_ops.files_in_branch_diff(project_dir, branch, target))
    except Exception:
        return []


def _landing_collisions(project_dir: Path, ready_tasks: list[Any], target: str) -> list[dict[str, Any]]:
    if len(ready_tasks) < 2:
        return []
    files_by_id: dict[str, set[str]] = {}
    for task in ready_tasks:
        branch = str(getattr(task, "branch", "") or "").strip()
        if not branch:
            continue
        try:
            files_by_id[task.id] = set(git_ops.files_in_branch_diff(project_dir, branch, target))
        except Exception:
            files_by_id[task.id] = set()
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
