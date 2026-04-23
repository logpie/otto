"""Textual dashboard for `otto queue run`.

Read-only UI over queue.yml/state.json/queue manifests. The watcher remains
the sole writer of queue state; the only mutation path exposed here is
appending a cancel command to commands.jsonl.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.driver import Driver
from textual.drivers.linux_driver import LinuxDriver
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Log, SelectionList, Static

from otto import paths
from otto.manifest import queue_index_path_for
from otto.queue.runtime import task_display_status, watcher_alive as watcher_is_alive
from otto.queue.runner import Runner, RunnerConfig
from otto.queue.schema import QueueTask, append_command, load_queue, load_state
from otto.theme import error_console

logger = logging.getLogger("otto.queue.dashboard")

_NARRATIVE_TAIL_BYTES = 32 * 1024
_NARRATIVE_MAX_LINES = 5000
_CANCEL_DEDUPE_WINDOW_S = 2.0
_PHASE_BUILD = "BUILD"
_PHASE_CERTIFY = "CERTIFY"
_STATUS_CANCELABLE = {"running"}
_LOG_UNAVAILABLE_PLACEHOLDER = "<log file no longer available>"
_TERMINAL_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[NO]"
)


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _format_elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _format_cost(cost: float | None, *, pending: bool = False) -> str:
    if isinstance(cost, (int, float)):
        return f"${float(cost):.2f}"
    return "…" if pending else "-"


def _truncate(text: str, limit: int = 88) -> str:
    cleaned = " ".join(_strip_terminal_escapes(text).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)] + "…"


def _strip_terminal_escapes(text: str) -> str:
    return _TERMINAL_ESCAPE_RE.sub("", text or "")


def _render_dashboard_closed_notice(running_count: int) -> str | None:
    if running_count <= 0:
        return None
    task_label = "task" if running_count == 1 else "tasks"
    complete_label = "it completes" if running_count == 1 else "they complete"
    return "\n".join(
        [
            "Dashboard closed. Watcher continues running in foreground.",
            f"{running_count} {task_label} still running; reopen with `otto queue dashboard` while {complete_label}.",
            "Press Ctrl-C to interrupt (twice for immediate stop).",
        ]
    )


def _print_dashboard_closed_notice(running_count: int, *, stream: TextIO | None = None) -> bool:
    message = _render_dashboard_closed_notice(running_count)
    if message is None:
        return False
    print(message, file=stream or sys.stdout, flush=True)
    return True


def _safe_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _command_phase(task: QueueTask) -> str:
    if task.command_argv and task.command_argv[0] == "certify":
        return _PHASE_CERTIFY
    return _PHASE_BUILD


def infer_phase_from_narrative(lines: Sequence[str], *, default: str) -> str:
    """Infer BUILD vs CERTIFY from recent narrative lines."""
    for raw in reversed(lines):
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if (
            "— CERTIFY" in upper
            or "CERTIFY_ROUND" in upper
            or "CERTIFY STARTING" in upper
            or "CERT IN PROGRESS" in upper
            or "VERDICT" in upper
        ):
            return _PHASE_CERTIFY
        if "— BUILD" in upper or "BUILD STARTING" in upper or "RUN SUMMARY:" in upper:
            return _PHASE_BUILD
    return default


@dataclass(slots=True)
class _ManifestCacheEntry:
    mtime: float
    size: int
    data: dict[str, Any] | None


@dataclass(slots=True)
class _NarrativeCacheEntry:
    mtime: float
    size: int
    last_line: str
    phase: str


@dataclass(slots=True)
class TaskView:
    task: QueueTask
    status: str
    phase: str
    branch: str
    elapsed_s: float | None
    elapsed_display: str
    cost_usd: float | None
    cost_display: str
    event: str
    narrative_path: Path | None
    manifest_path: Path | None
    session_id: str | None
    state: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] | None = None

    @property
    def can_cancel(self) -> bool:
        return self.status in _STATUS_CANCELABLE


@dataclass(slots=True)
class QueueSnapshot:
    tasks: list[TaskView]
    watcher: dict[str, Any] | None
    watcher_alive: bool
    header_elapsed: str
    total_cost_usd: float
    running_count: int
    queued_count: int
    done_count: int
    interrupted_count: int
    total_count: int

    @property
    def by_id(self) -> dict[str, TaskView]:
        return {task.task.id: task for task in self.tasks}


class QueueModel:
    """Read-only queue/project snapshot loader with small file caches."""

    def __init__(self, project_dir: Path, *, launched_at: datetime | None = None) -> None:
        self.project_dir = project_dir
        self._queue_mtime_ns: int | None = None
        self._queue_cache: list[QueueTask] = []
        self._state_cache: dict[str, Any] = {"tasks": {}, "watcher": None}
        self._manifest_cache: dict[str, _ManifestCacheEntry] = {}
        self._narrative_cache: dict[Path, _NarrativeCacheEntry] = {}
        self._launched_at = launched_at or _now_utc()
        self.queue_warning: str | None = None
        self.state_warning: str | None = None

    def snapshot(self) -> QueueSnapshot:
        tasks = self._load_queue_cached()
        state = self._load_state_safe()
        state_tasks = state.get("tasks", {}) if isinstance(state.get("tasks"), dict) else {}
        watcher = state.get("watcher") if isinstance(state.get("watcher"), dict) else None
        watcher_live = watcher_is_alive(state)
        rows: list[TaskView] = []
        total_cost = 0.0

        for task in tasks:
            task_state = state_tasks.get(task.id, {}) if isinstance(state_tasks.get(task.id), dict) else {}
            status = task_display_status(task_state)
            manifest = self._read_manifest(task.id)
            narrative_path = self.resolve_narrative_path(task, task_state, manifest)
            manifest_path = self.resolve_manifest_path(task.id, task_state, manifest)
            session_id = self.resolve_session_id(manifest, narrative_path, manifest_path)
            phase_default = _command_phase(task)
            phase = phase_default
            event = "-"
            if narrative_path is not None:
                event, phase = self._narrative_summary(narrative_path, default_phase=phase_default)
            cost = self._task_cost(status, task_state, manifest)
            if cost is not None:
                total_cost += cost
            elapsed_s = self._task_elapsed_seconds(task_state, manifest)
            rows.append(
                TaskView(
                    task=task,
                    status=status,
                    phase=phase,
                    branch=task.branch or "-",
                    elapsed_s=elapsed_s,
                    elapsed_display=_format_elapsed(elapsed_s),
                    cost_usd=cost,
                    cost_display=_format_cost(cost, pending=status == "running"),
                    event=_truncate(event or "-", 96),
                    narrative_path=narrative_path,
                    manifest_path=manifest_path,
                    session_id=session_id,
                    state=task_state,
                    manifest=manifest,
                )
            )

        watcher_started = _parse_iso(watcher.get("started_at")) if watcher else None
        header_elapsed = _format_elapsed(
            (_now_utc() - watcher_started).total_seconds() if watcher_started else (_now_utc() - self._launched_at).total_seconds()
        )
        return QueueSnapshot(
            tasks=rows,
            watcher=watcher,
            watcher_alive=watcher_live,
            header_elapsed=header_elapsed,
            total_cost_usd=total_cost,
            running_count=sum(1 for row in rows if row.status == "running"),
            queued_count=sum(1 for row in rows if row.status == "queued"),
            done_count=sum(1 for row in rows if row.status == "done"),
            interrupted_count=sum(1 for row in rows if row.status == "interrupted"),
            total_count=len(rows),
        )

    def resolve_task(self, task_id: str) -> TaskView | None:
        return self.snapshot().by_id.get(task_id)

    def overview_banner(self) -> str | None:
        warnings = [warning for warning in (self.state_warning, self.queue_warning) if warning]
        if not warnings:
            return None
        return " | ".join(warnings)

    def resolve_narrative_path(
        self,
        task: QueueTask,
        task_state: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> Path | None:
        phase_dir = "certify" if _command_phase(task) == _PHASE_CERTIFY else "build"

        for worktree_dir in self._candidate_worktree_dirs(task, task_state):
            candidate = self._resolve_worktree_narrative_path(worktree_dir, phase_dir)
            if candidate is not None:
                return candidate

        if manifest:
            checkpoint_path = manifest.get("checkpoint_path")
            if isinstance(checkpoint_path, str) and checkpoint_path:
                session_dir = Path(checkpoint_path).expanduser().resolve(strict=False).parent
                candidate = session_dir / phase_dir / "narrative.log"
                if candidate.exists():
                    return candidate
            mirror_of = manifest.get("mirror_of")
            if isinstance(mirror_of, str) and mirror_of:
                manifest_dir = Path(mirror_of).expanduser().resolve(strict=False).parent
                candidate = manifest_dir / phase_dir / "narrative.log"
                if candidate.exists():
                    return candidate
            proof = manifest.get("proof_of_work_path")
            if isinstance(proof, str) and proof:
                proof_path = Path(proof).expanduser().resolve(strict=False)
                if phase_dir == "certify":
                    candidate = proof_path.parent / "narrative.log"
                else:
                    candidate = proof_path.parent.parent / phase_dir / "narrative.log"
                if candidate.exists():
                    return candidate
        return None

    def _candidate_worktree_dirs(
        self,
        task: QueueTask,
        task_state: dict[str, Any] | None = None,
    ) -> list[Path]:
        candidates: list[Path] = []
        worktree = task.worktree or ""
        if worktree:
            candidates.append(self.project_dir / worktree)
        child = (task_state or {}).get("child") if isinstance(task_state, dict) else None
        if isinstance(child, dict):
            cwd = child.get("cwd")
            if isinstance(cwd, str) and cwd:
                alt_worktree = Path(cwd)
                if alt_worktree not in candidates:
                    candidates.append(alt_worktree)
        return candidates

    @staticmethod
    def _resolve_worktree_narrative_path(worktree_dir: Path, phase_dir: str) -> Path | None:
        latest = paths.resolve_pointer(worktree_dir, paths.LATEST_POINTER)
        if latest is not None:
            candidate = latest / phase_dir / "narrative.log"
            if candidate.exists():
                return candidate

        sessions_root = paths.sessions_root(worktree_dir)
        if sessions_root.exists():
            candidates = list(sessions_root.glob(f"*/{phase_dir}/narrative.log"))
            if candidates:
                return max(
                    candidates,
                    key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
                )
        return None

    def _load_queue_cached(self) -> list[QueueTask]:
        queue_path = self.project_dir / ".otto-queue.yml"
        try:
            stat = queue_path.stat()
        except OSError:
            self._queue_cache = []
            self._queue_mtime_ns = None
            self.queue_warning = None
            return []
        if self._queue_mtime_ns == stat.st_mtime_ns:
            return self._queue_cache
        try:
            tasks = load_queue(self.project_dir)
        except Exception as exc:
            logger.warning("failed to load queue.yml for dashboard: %s", exc)
            self.queue_warning = self._load_warning_message(
                "queue.yml",
                exc,
                fallback="using last good cache",
            )
            return self._queue_cache
        self._queue_cache = tasks
        self._queue_mtime_ns = stat.st_mtime_ns
        self.queue_warning = None
        return tasks

    def _load_state_safe(self) -> dict[str, Any]:
        try:
            state = load_state(self.project_dir)
        except Exception as exc:
            logger.warning("failed to load queue state for dashboard: %s", exc)
            self.state_warning = self._load_warning_message(
                "state.json",
                exc,
                fallback="using last good cache",
            )
            return self._state_cache
        self._state_cache = state
        self.state_warning = None
        return state

    def resolve_manifest_path(
        self,
        task_id: str,
        task_state: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> Path | None:
        if manifest:
            mirror_of = manifest.get("mirror_of")
            if isinstance(mirror_of, str) and mirror_of:
                candidate = Path(mirror_of).expanduser().absolute()
                if candidate.exists():
                    return candidate
        if isinstance(task_state, dict):
            manifest_path = task_state.get("manifest_path")
            if isinstance(manifest_path, str) and manifest_path:
                candidate = Path(manifest_path).expanduser().absolute()
                if candidate.exists():
                    return candidate
        queue_manifest = queue_index_path_for(self.project_dir, task_id)
        if queue_manifest is not None and queue_manifest.exists():
            return queue_manifest.absolute()
        if manifest:
            checkpoint_path = manifest.get("checkpoint_path")
            if isinstance(checkpoint_path, str) and checkpoint_path:
                candidate = (
                    Path(checkpoint_path).expanduser().absolute().parent / "manifest.json"
                )
                if candidate.exists():
                    return candidate
        if isinstance(task_state, dict):
            manifest_path = task_state.get("manifest_path")
            if isinstance(manifest_path, str) and manifest_path:
                return Path(manifest_path).expanduser().absolute()
        return queue_manifest.absolute() if queue_manifest is not None else None

    def resolve_session_id(
        self,
        manifest: dict[str, Any] | None,
        narrative_path: Path | None,
        manifest_path: Path | None,
    ) -> str | None:
        if manifest:
            run_id = manifest.get("run_id")
            if isinstance(run_id, str) and run_id:
                return run_id
        for path in (narrative_path, manifest_path):
            session_id = self._session_id_from_path(path)
            if session_id is not None:
                return session_id
        return None

    @staticmethod
    def _load_warning_message(path_label: str, exc: Exception, *, fallback: str) -> str:
        error_kind = "parse error" if isinstance(exc, ValueError) else "read error"
        return f"⚠ {path_label} {error_kind} ({fallback})"

    @staticmethod
    def _session_id_from_path(path: Path | None) -> str | None:
        if path is None:
            return None
        parts = path.parts
        try:
            index = parts.index("sessions")
        except ValueError:
            return None
        if index + 1 >= len(parts):
            return None
        session_id = parts[index + 1]
        return session_id or None

    def _read_manifest(self, task_id: str) -> dict[str, Any] | None:
        path = queue_index_path_for(self.project_dir, task_id)
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            self._manifest_cache.pop(task_id, None)
            return None
        cached = self._manifest_cache.get(task_id)
        if cached is not None and cached.mtime == stat.st_mtime and cached.size == stat.st_size:
            return cached.data
        data = _safe_json(path)
        self._manifest_cache[task_id] = _ManifestCacheEntry(
            mtime=stat.st_mtime,
            size=stat.st_size,
            data=data,
        )
        return data

    def _narrative_summary(self, path: Path, *, default_phase: str) -> tuple[str, str]:
        try:
            stat = path.stat()
        except OSError:
            self._narrative_cache.pop(path, None)
            return "-", default_phase
        cached = self._narrative_cache.get(path)
        if cached is not None and cached.mtime == stat.st_mtime and cached.size == stat.st_size:
            return cached.last_line, cached.phase

        try:
            with path.open("rb") as handle:
                if stat.st_size > _NARRATIVE_TAIL_BYTES:
                    handle.seek(max(0, stat.st_size - _NARRATIVE_TAIL_BYTES))
                chunk = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return "-", default_phase

        lines = [_strip_terminal_escapes(line).strip() for line in chunk.splitlines() if line.strip()]
        last_line = lines[-1] if lines else "-"
        phase = infer_phase_from_narrative(
            lines,
            default=cached.phase if cached is not None else default_phase,
        )
        self._narrative_cache[path] = _NarrativeCacheEntry(
            mtime=stat.st_mtime,
            size=stat.st_size,
            last_line=last_line,
            phase=phase,
        )
        return last_line, phase

    def _task_cost(
        self,
        status: str,
        task_state: dict[str, Any],
        manifest: dict[str, Any] | None,
    ) -> float | None:
        if isinstance(task_state.get("cost_usd"), (int, float)):
            return float(task_state["cost_usd"])
        if status in {"done", "failed", "cancelled", "removed"} and manifest:
            cost = manifest.get("cost_usd")
            if isinstance(cost, (int, float)):
                return float(cost)
        return None

    def _task_elapsed_seconds(
        self,
        task_state: dict[str, Any],
        manifest: dict[str, Any] | None,
    ) -> float | None:
        if isinstance(task_state.get("duration_s"), (int, float)):
            return float(task_state["duration_s"])
        started_at = _parse_iso(task_state.get("started_at"))
        finished_at = _parse_iso(task_state.get("finished_at"))
        if started_at and finished_at:
            return max(0.0, (finished_at - started_at).total_seconds())
        if started_at:
            return max(0.0, (_now_utc() - started_at).total_seconds())
        if manifest and isinstance(manifest.get("duration_s"), (int, float)):
            return float(manifest["duration_s"])
        return None


class NarrativeTailer:
    """Polls a narrative.log and returns appended complete lines."""

    def __init__(self, path_resolver: Callable[[], Path | None]) -> None:
        self._path_resolver = path_resolver
        self._path: Path | None = None
        self._offset = 0
        self._pending = ""
        self._mtime: float | None = None

    def poll(self) -> tuple[bool, list[str]]:
        path = self._path_resolver()
        if path is None:
            if self._path is not None:
                self._reset(clear_path=True)
                return True, [_LOG_UNAVAILABLE_PLACEHOLDER]
            return False, []
        if self._path is None or path != self._path:
            self._path = path
            self._reset(clear_path=False)
            return self._read_new(clear=True)
        return self._read_new(clear=False)

    def _reset(self, *, clear_path: bool) -> None:
        if clear_path:
            self._path = None
        self._offset = 0
        self._pending = ""
        self._mtime = None

    def _read_new(self, *, clear: bool) -> tuple[bool, list[str]]:
        assert self._path is not None
        try:
            stat = self._path.stat()
        except OSError:
            self._reset(clear_path=True)
            return True, [_LOG_UNAVAILABLE_PLACEHOLDER]
        if stat.st_size < self._offset:
            self._reset(clear_path=False)
            clear = True
        if stat.st_size == self._offset and self._mtime == stat.st_mtime:
            return clear, []
        try:
            with self._path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except OSError:
            return clear, []
        self._offset = stat.st_size
        self._mtime = stat.st_mtime
        if not chunk:
            return clear, []
        text = self._pending + chunk.decode("utf-8", errors="replace")
        if text.endswith("\n"):
            lines = text.splitlines()
            self._pending = ""
        else:
            parts = text.splitlines()
            self._pending = parts.pop() if parts else text
            lines = parts
        return clear, [_strip_terminal_escapes(line) for line in lines]


class HelpModal(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
    ]

    def __init__(self, title: str, lines: Sequence[str]) -> None:
        super().__init__()
        self._title = title
        self._lines = list(lines)

    def compose(self) -> ComposeResult:
        body = "\n".join([self._title, "", *self._lines, "", "Press Esc or q to close."])
        with Container(id="help-modal"):
            yield Static(body, id="help-body")

    def action_close(self) -> None:
        self.dismiss(None)


class OttoLinuxDriver(LinuxDriver):
    """Skip focus-tracking enable when Otto leaves mouse support off."""

    def write(self, data: str) -> None:
        if not self._mouse and data == "\x1b[?1004h":
            return
        super().write(data)


class OverviewScreen(Screen[None]):
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("enter", "open_task", "Open", show=False),
        Binding("y", "yank_to_clipboard", "Yank", show=False),
        Binding("c", "cancel_task", "Cancel", show=False),
        Binding("?", "show_help", "Help", show=False),
        Binding("q", "quit_dashboard", "Quit", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._row_order: list[str] = []
        self._refresh_timer = None

    def _first_widget(self, selector: str, expected_type: type[Any]):
        matches = list(self.query(selector))
        if not matches:
            return None
        widget = matches[0]
        if not isinstance(widget, expected_type):
            return None
        return widget

    def compose(self) -> ComposeResult:
        yield Static(id="overview-banner")
        yield Static(id="overview-header")
        yield DataTable(id="overview-table")
        yield Static(
            "No tasks queued. Use `otto queue build|improve|certify <intent>` to add some, "
            "or wait for the watcher to pick them up.",
            id="empty-state",
        )
        yield Static(id="overview-status")

    def on_mount(self) -> None:
        table = self._first_widget("#overview-table", DataTable)
        if table is None:
            logger.warning("overview table missing during mount; skipping table initialization")
            self._refresh_timer = self.set_interval(0.5, self._refresh)
            return
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("ID", key="id")
        table.add_column("STATUS", key="status")
        table.add_column("PHASE", key="phase")
        table.add_column("BRANCH", key="branch")
        table.add_column("ELAPSED", key="elapsed")
        table.add_column("COST", key="cost")
        table.add_column("EVENT", key="event")
        self._refresh()
        self._refresh_timer = self.set_interval(0.5, self._refresh)

    def _refresh(self) -> None:
        snapshot = self.app.model.snapshot()
        self.app.update_snapshot(snapshot)
        banner = self._first_widget("#overview-banner", Static)
        if banner is not None:
            banner.update(Text(self.app.model.overview_banner() or ""))
        header = self._first_widget("#overview-header", Static)
        if header is not None:
            header.update(
            f"[bold]otto queue[/bold] · {self.app.header_mode_label} · "
            f"{snapshot.header_elapsed} · {_format_cost(snapshot.total_cost_usd)}"
            )
        footer = self._first_widget("#overview-status", Static)
        if footer is not None:
            footer.update(self.app.overview_footer(snapshot))
        self._sync_rows(snapshot.tasks)

    def _sync_rows(self, tasks: Sequence[TaskView]) -> None:
        table = self._first_widget("#overview-table", DataTable)
        empty_state = self._first_widget("#empty-state", Static)
        if table is None or empty_state is None:
            return
        wanted = [task.task.id for task in tasks]
        current = set(self._row_order)
        for task_id in list(self._row_order):
            if task_id not in wanted:
                table.remove_row(task_id)
        self._row_order = []
        for task in tasks:
            row_values = (
                task.task.id,
                task.status.upper(),
                task.phase,
                task.branch,
                task.elapsed_display,
                task.cost_display,
                task.event,
            )
            if task.task.id not in current:
                table.add_row(*row_values, key=task.task.id)
            else:
                table.update_cell(task.task.id, "id", row_values[0], update_width=True)
                table.update_cell(task.task.id, "status", row_values[1], update_width=True)
                table.update_cell(task.task.id, "phase", row_values[2], update_width=True)
                table.update_cell(task.task.id, "branch", row_values[3], update_width=True)
                table.update_cell(task.task.id, "elapsed", row_values[4], update_width=True)
                table.update_cell(task.task.id, "cost", row_values[5], update_width=True)
                table.update_cell(task.task.id, "event", row_values[6], update_width=True)
            self._row_order.append(task.task.id)
        if self._row_order:
            cursor_row = min(table.cursor_row, len(self._row_order) - 1)
            table.move_cursor(row=cursor_row, column=0, animate=False, scroll=True)
        is_empty = not self._row_order
        table.styles.display = "none" if is_empty else "block"
        empty_state.styles.display = "block" if is_empty else "none"

    def _selected_task_id(self) -> str | None:
        table = self._first_widget("#overview-table", DataTable)
        if table is None:
            return None
        if not self._row_order:
            return None
        row = min(max(table.cursor_row, 0), len(self._row_order) - 1)
        return self._row_order[row]

    def action_cursor_down(self) -> None:
        table = self._first_widget("#overview-table", DataTable)
        if table is not None:
            table.action_cursor_down()

    def action_cursor_up(self) -> None:
        table = self._first_widget("#overview-table", DataTable)
        if table is not None:
            table.action_cursor_up()

    def action_open_task(self) -> None:
        task_id = self._selected_task_id()
        if task_id is None:
            return
        self.app.push_screen(TaskDetailScreen(task_id))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        del event
        self.action_open_task()

    def action_cancel_task(self) -> None:
        task_id = self._selected_task_id()
        if task_id is None:
            return
        self.app.cancel_task(task_id)

    def action_yank_to_clipboard(self) -> None:
        task_id = self._selected_task_id()
        if task_id is None:
            return
        task = self.app.last_snapshot.by_id.get(task_id)
        if task is None:
            return
        payload = "\n".join(
            [
                "\t".join(
                    [
                        task.task.id,
                        task.status,
                        task.phase,
                        task.branch,
                        task.elapsed_display,
                        task.cost_display,
                        task.event,
                    ]
                ),
                "\t".join(
                    [
                        task.session_id or "-",
                        str(task.manifest_path) if task.manifest_path is not None else "-",
                    ]
                ),
            ]
        )
        if _copy_to_clipboard(payload):
            self.notify(f"copied {task.task.id} to clipboard")
            return
        self.notify("clipboard not available", severity="warning")

    def action_show_help(self) -> None:
        self.app.push_screen(
            HelpModal(
                "Overview bindings",
                [
                    "j / Down / ↓: move to next row",
                    "k / Up / ↑: move to previous row",
                    "Enter: open task detail",
                    "y: yank selected row to clipboard",
                    *([] if self.app.read_only else ["c: queue cancel for selected running task"]),
                    "?: open this help",
                    "Esc: close this help overlay",
                    self.app.quit_help_text,
                ],
            )
        )

    def action_quit_dashboard(self) -> None:
        self.app.request_shutdown()


class TaskDetailScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "back", "Back", show=False),
        Binding("q", "back", "Back", show=False),
        Binding("j", "scroll_down", "Down", show=False),
        Binding("k", "scroll_up", "Up", show=False),
        Binding("pagedown", "page_down", "Page Down", show=False, priority=True),
        Binding("pageup", "page_up", "Page Up", show=False, priority=True),
        Binding("end", "follow_tail", "Tail", show=False, priority=True),
        Binding("home", "top", "Top", show=False, priority=True),
        Binding("y", "yank_to_clipboard", "Yank", show=False),
        Binding("c", "cancel_task", "Cancel", show=False),
        Binding("?", "show_help", "Help", show=False),
    ]

    def __init__(self, task_id: str) -> None:
        super().__init__()
        self.task_id = task_id
        self._tailer = NarrativeTailer(self._resolve_narrative_path)
        self._follow = True

    def compose(self) -> ComposeResult:
        yield Static(id="detail-header")
        yield Static(id="detail-info")
        yield Log(id="detail-log", max_lines=_NARRATIVE_MAX_LINES, auto_scroll=True, highlight=True)
        yield Static("[esc/q back · y yank · j/k scroll · pgup/pgdn · c cancel · end follow]", id="detail-status")

    def on_mount(self) -> None:
        self.query_one("#detail-log", Log).focus()
        self._refresh()
        self.set_interval(0.25, self._refresh)

    def _resolve_narrative_path(self) -> Path | None:
        current = self.app.model.resolve_task(self.task_id)
        if current is None:
            return None
        return current.narrative_path

    def _refresh(self) -> None:
        snapshot = self.app.model.snapshot()
        self.app.update_snapshot(snapshot)
        current = snapshot.by_id.get(self.task_id)
        self._update_header(current)
        self._update_info(current)
        log = self.query_one("#detail-log", Log)
        clear, lines = self._tailer.poll()
        if clear:
            log.clear()
        for line in lines:
            log.write_line(line, scroll_end=self._follow)
        if self._follow and lines:
            log.auto_scroll = True
            log.scroll_end(animate=False)

    def _update_header(self, current: TaskView | None) -> None:
        header = self.query_one("#detail-header", Static)
        if current is None:
            header.update(f"[bold]otto queue ▸ {self.task_id}[/bold] · unavailable")
            return
        header.update(
            f"[bold]otto queue ▸ {self.task_id}[/bold] · "
            f"{current.phase} · {current.elapsed_display} · {current.cost_display}"
        )

    def _update_info(self, current: TaskView | None) -> None:
        info = self.query_one("#detail-info", Static)
        if current is None:
            info.update(Text("branch: -\nlog: -\nmanifest: -"))
            return
        lines = [
            f"branch: {current.branch}",
            f"log: {self._display_path(current.narrative_path)}",
            f"manifest: {self._display_path(current.manifest_path)}",
        ]
        failure_reason = current.state.get("failure_reason")
        if current.status == "failed" and isinstance(failure_reason, str) and failure_reason:
            lines.append(f"failure: {failure_reason}")
        info.update(Text("\n".join(lines), overflow="fold", no_wrap=False))

    @staticmethod
    def _display_path(value: Path | None) -> str:
        text = str(value) if value is not None else "-"
        if text.startswith("/private/tmp/"):
            text = text.removeprefix("/private")
        return text

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_scroll_down(self) -> None:
        self._follow = False
        log = self.query_one("#detail-log", Log)
        log.auto_scroll = False
        log.scroll_down(animate=False)

    def action_scroll_up(self) -> None:
        self._follow = False
        log = self.query_one("#detail-log", Log)
        log.auto_scroll = False
        log.scroll_up(animate=False)

    def action_page_down(self) -> None:
        self._follow = False
        log = self.query_one("#detail-log", Log)
        log.auto_scroll = False
        log.scroll_page_down(animate=False)

    def action_page_up(self) -> None:
        self._follow = False
        log = self.query_one("#detail-log", Log)
        log.auto_scroll = False
        log.scroll_page_up(animate=False)

    def action_follow_tail(self) -> None:
        self._follow = True
        log = self.query_one("#detail-log", Log)
        log.auto_scroll = True
        log.scroll_end(animate=False)

    def action_top(self) -> None:
        self._follow = False
        log = self.query_one("#detail-log", Log)
        log.auto_scroll = False
        log.scroll_home(animate=False)

    def action_cancel_task(self) -> None:
        self.app.cancel_task(self.task_id)

    def action_yank_to_clipboard(self) -> None:
        narrative_path = self._resolve_narrative_path()
        if narrative_path is None:
            self.notify("narrative log unavailable", severity="warning")
            return
        text = _read_text_file(narrative_path)
        if text is None:
            self.notify("narrative log unavailable", severity="warning")
            return
        if _copy_to_clipboard(text):
            self.notify(f"copied {self.task_id} to clipboard")
            return
        self.notify("clipboard not available", severity="warning")

    def action_show_help(self) -> None:
        self.app.push_screen(
            HelpModal(
                f"Task detail bindings ({self.task_id})",
                [
                    "Esc / q: return to overview",
                    "y: yank full narrative log to clipboard",
                    "j / k: scroll one line",
                    "PgUp / PgDn: scroll one page",
                    "Home: jump to top",
                    "End: jump to tail and resume follow",
                    "c: queue cancel for running task",
                ],
            )
        )


class QueueApp(App[int]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #overview-banner {
        padding: 0 1;
        color: yellow;
    }

    #overview-header, #detail-header {
        height: 1;
        padding: 0 1;
        color: cyan;
    }

    #overview-status, #detail-status {
        height: 1;
        padding: 0 1;
        color: green;
    }

    #detail-info {
        padding: 0 1;
        color: $text-muted;
    }

    #overview-table, #detail-log, #empty-state {
        height: 1fr;
    }

    #empty-state {
        display: none;
        padding: 1 4;
        color: $text-muted;
        content-align: center middle;
    }

    #help-modal {
        width: 72;
        max-width: 96;
        border: round cyan;
        background: $surface;
        padding: 1 2;
        align: center middle;
    }

    #help-body {
        width: 100%;
    }
    """

    def __init__(
        self,
        project_dir: Path,
        *,
        concurrent: int,
        cancel_callback: Callable[[str], None] | None = None,
        runner: Runner | None = None,
        dashboard_mouse: bool = False,
        read_only: bool = False,
    ) -> None:
        super().__init__()
        self.project_dir = project_dir
        self.concurrent = concurrent
        self.dashboard_mouse = dashboard_mouse
        self.read_only = read_only
        self.model = QueueModel(project_dir)
        self.cancel_callback = cancel_callback or (lambda task_id: _append_cancel_command(project_dir, task_id))
        self.runner = runner
        self.last_snapshot = QueueSnapshot([], None, False, "-", 0.0, 0, 0, 0, 0, 0)
        self._recent_cancel_requests: dict[str, float] = {}

    def get_driver_class(self) -> type[Driver]:
        driver_class = super().get_driver_class()
        if driver_class is LinuxDriver:
            return OttoLinuxDriver
        return driver_class

    async def on_mount(self) -> None:
        await self.push_screen(OverviewScreen())

    def request_shutdown(self) -> None:
        if not self.read_only and self.runner is not None and self.runner.shutdown_level is None:
            self.runner.shutdown_level = "graceful"
        self.exit(0)

    @property
    def quit_help_text(self) -> str:
        if self.read_only:
            return "q: quit dashboard viewer"
        return "q: hide dashboard; reopen with `otto queue dashboard`"

    @property
    def header_mode_label(self) -> str:
        if self.read_only:
            return "viewer"
        return f"concurrent={self.concurrent}"

    def overview_footer(self, snapshot: QueueSnapshot) -> str:
        counts = (
            f"{snapshot.running_count} running · {snapshot.queued_count} queued · "
            f"{snapshot.interrupted_count} interrupted · {snapshot.done_count} done · "
            f"{snapshot.total_count} total"
        )
        if self.read_only:
            watcher_text = "watcher live" if snapshot.watcher_alive else "watcher stopped"
            return f"{watcher_text} · {counts} · enter open · y yank · ? help · q quit"
        return (
            f"{counts} · enter open · y yank · c cancel · ? help · "
            "q hide; reopen with `otto queue dashboard`"
        )

    def update_snapshot(self, snapshot: QueueSnapshot) -> None:
        self.last_snapshot = snapshot
        running_ids = {task.task.id for task in snapshot.tasks if task.can_cancel}
        for task_id in list(self._recent_cancel_requests):
            if task_id not in running_ids:
                self._recent_cancel_requests.pop(task_id, None)

    def cancel_task(self, task_id: str) -> None:
        if self.read_only:
            self.notify("viewer is read-only", severity="warning")
            return
        current = self.model.resolve_task(task_id)
        if current is None:
            self.notify(f"task {task_id} not found", severity="error")
            return
        if not current.can_cancel:
            self.notify("task is not running, cannot cancel", severity="warning")
            return
        now = time.monotonic()
        last_sent = self._recent_cancel_requests.get(task_id)
        if last_sent is not None and (now - last_sent) < _CANCEL_DEDUPE_WINDOW_S:
            self.notify(f"cancel already sent for {task_id}", severity="information")
            return
        self.cancel_callback(task_id)
        self._recent_cancel_requests[task_id] = now
        self.notify(f"cancel queued for {task_id}", severity="information")


def _append_cancel_command(project_dir: Path, task_id: str) -> None:
    append_command(
        project_dir,
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cmd": "cancel",
            "id": task_id,
        },
    )


def _read_text_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("failed to read %s for clipboard copy: %s", path, exc)
        return None


def _copy_to_clipboard(text: str) -> bool:
    encoded = text.encode()
    try:
        if sys.platform == "darwin":
            result = subprocess.run(["pbcopy"], input=encoded, check=False)
            return result.returncode == 0
        if sys.platform.startswith("linux"):
            if shutil.which("xclip"):
                result = subprocess.run(["xclip", "-selection", "clipboard"], input=encoded, check=False)
                return result.returncode == 0
            if shutil.which("wl-copy"):
                result = subprocess.run(["wl-copy"], input=encoded, check=False)
                return result.returncode == 0
            logger.warning("clipboard helper unavailable: neither xclip nor wl-copy found")
            return False
    except OSError as exc:
        logger.warning("clipboard copy failed: %s", exc)
        return False
    logger.warning("clipboard copy unsupported on platform %s", sys.platform)
    return False


async def _run_dashboard_async(app: QueueApp, runner: Runner, *, quiet: bool) -> int:
    runner_task = asyncio.create_task(runner.run_async())
    app_task = asyncio.create_task(app.run_async(mouse=app.dashboard_mouse))

    while True:
        done, _pending = await asyncio.wait(
            {runner_task, app_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if app_task in done:
            exc = app_task.exception()
            if exc is not None:
                logger.exception("dashboard crashed; falling back to prefixed stdout")
                from otto.cli_queue import _install_runner_logging
                from otto.display import console

                _install_runner_logging(app.project_dir, quiet=quiet)
                console.print("  [yellow]Dashboard crashed; continuing with prefixed stdout.[/yellow]")
                return await runner_task
            if not runner_task.done():
                runner.shutdown_level = runner.shutdown_level or "graceful"
                _print_dashboard_closed_notice(app.model.snapshot().running_count)
                return await runner_task
            return runner_task.result()

        if runner_task in done:
            exc = runner_task.exception()
            if exc is not None:
                if not app_task.done():
                    app.exit(1)
                    try:
                        await app_task
                    except Exception:
                        logger.exception("dashboard shutdown after runner failure")
                raise exc
            if not app_task.done():
                app.exit(runner_task.result())
                try:
                    await app_task
                except Exception:
                    logger.exception("dashboard shutdown after runner exit")
            return runner_task.result()


def run_dashboard(
    project_dir: Path,
    *,
    concurrent: int,
    quiet: bool,
    dashboard_mouse: bool = False,
    runner_config: RunnerConfig,
    otto_bin: list[str] | str,
) -> int:
    """Run Textual UI + watcher on the same asyncio loop."""
    from otto.cli_queue import _install_runner_logging

    _install_runner_logging(project_dir, quiet=True)
    runner = Runner(project_dir, runner_config, otto_bin=otto_bin)
    app = QueueApp(project_dir, concurrent=concurrent, runner=runner, dashboard_mouse=dashboard_mouse)
    return asyncio.run(_run_dashboard_async(app, runner, quiet=quiet))


class ResumeSelectionApp(App[list[str]]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #resume-header, #resume-footer {
        padding: 0 1;
    }

    #resume-header {
        color: cyan;
    }

    #resume-footer {
        color: green;
    }

    #resume-list {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("enter", "confirm", "Confirm", show=False),
        Binding("q", "cancel", "Cancel", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, tasks: Sequence[QueueTask]) -> None:
        super().__init__()
        self._tasks = list(tasks)

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold]Resume Interrupted Tasks[/bold]\n"
            "Space toggles a task. Enter confirms the selection.",
            id="resume-header",
        )
        yield SelectionList(
            *[
                (self._label(task), task.id, True)
                for task in self._tasks
            ],
            id="resume-list",
        )
        yield Static("space toggle · enter confirm · q cancel", id="resume-footer")

    def on_mount(self) -> None:
        self.query_one("#resume-list", SelectionList).focus()

    def action_confirm(self) -> None:
        selected = list(self.query_one("#resume-list", SelectionList).selected)
        self.exit(selected)

    def action_cancel(self) -> None:
        self.exit([])

    @staticmethod
    def _label(task: QueueTask) -> str:
        command = " ".join(task.command_argv[:3]).strip()
        return f"{task.id}  {command}"


def select_resume_tasks(project_dir: Path, tasks: Sequence[QueueTask]) -> list[str]:
    del project_dir
    if not sys.stdout.isatty() or os.environ.get("OTTO_NO_TUI"):
        error_console.print(
            "[error]`otto queue resume --select` requires an interactive terminal.[/error]\n"
            "  Pass task ids explicitly instead, for example: `otto queue resume labels,due`."
        )
        raise SystemExit(2)
    app = ResumeSelectionApp(tasks)
    return list(app.run())


def run_dashboard_viewer(
    project_dir: Path,
    *,
    dashboard_mouse: bool = False,
) -> int:
    """Run the read-only dashboard viewer attached to an active watcher."""
    snapshot = QueueModel(project_dir).snapshot()
    if not snapshot.watcher_alive:
        error_console.print(
            "[error]No active queue watcher found.[/error]\n"
            "  Start one with `otto queue run --concurrent N`, then reopen with `otto queue dashboard`."
        )
        return 1
    app = QueueApp(
        project_dir,
        concurrent=0,
        dashboard_mouse=dashboard_mouse,
        read_only=True,
    )
    return int(app.run(mouse=dashboard_mouse) or 0)
