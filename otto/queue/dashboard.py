"""Textual dashboard for `otto queue run`.

Read-only UI over queue.yml/state.json/queue manifests. The watcher remains
the sole writer of queue state; the only mutation path exposed here is
appending a cancel command to commands.jsonl.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, RichLog, Static

from otto import paths
from otto.manifest import queue_index_path_for
from otto.queue.runner import Runner, RunnerConfig
from otto.queue.schema import QueueTask, append_command, load_queue, load_state

logger = logging.getLogger("otto.queue.dashboard")

_NARRATIVE_TAIL_BYTES = 32 * 1024
_NARRATIVE_MAX_LINES = 5000
_PHASE_BUILD = "BUILD"
_PHASE_CERTIFY = "CERTIFY"
_STATUS_CANCELABLE = {"running"}


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
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)] + "…"


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
        if "CERTIFY ROUND" in line or "CERTIFY_ROUND:" in line:
            return _PHASE_CERTIFY
        if line.startswith("[+") and "VERDICT:" in line:
            return _PHASE_CERTIFY
        if "BUILD starting" in line or "RUN SUMMARY:" in line:
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
    state: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] | None = None

    @property
    def can_cancel(self) -> bool:
        return self.status in _STATUS_CANCELABLE


@dataclass(slots=True)
class QueueSnapshot:
    tasks: list[TaskView]
    watcher: dict[str, Any] | None
    header_elapsed: str
    total_cost_usd: float
    running_count: int
    queued_count: int
    done_count: int
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
        self._manifest_cache: dict[str, _ManifestCacheEntry] = {}
        self._narrative_cache: dict[Path, _NarrativeCacheEntry] = {}
        self._launched_at = launched_at or _now_utc()

    def snapshot(self) -> QueueSnapshot:
        tasks = self._load_queue_cached()
        state = self._load_state_safe()
        state_tasks = state.get("tasks", {}) if isinstance(state.get("tasks"), dict) else {}
        watcher = state.get("watcher") if isinstance(state.get("watcher"), dict) else None
        rows: list[TaskView] = []
        total_cost = 0.0

        for task in tasks:
            task_state = state_tasks.get(task.id, {}) if isinstance(state_tasks.get(task.id), dict) else {}
            status = str(task_state.get("status") or "queued")
            manifest = self._read_manifest(task.id) if status != "running" else None
            narrative_path = self.resolve_narrative_path(task, task_state, manifest)
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
            header_elapsed=header_elapsed,
            total_cost_usd=total_cost,
            running_count=sum(1 for row in rows if row.status == "running"),
            queued_count=sum(1 for row in rows if row.status == "queued"),
            done_count=sum(1 for row in rows if row.status == "done"),
            total_count=len(rows),
        )

    def resolve_task(self, task_id: str) -> TaskView | None:
        return self.snapshot().by_id.get(task_id)

    def resolve_narrative_path(
        self,
        task: QueueTask,
        task_state: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> Path | None:
        phase_dir = "certify" if _command_phase(task) == _PHASE_CERTIFY else "build"

        if manifest:
            mirror_of = manifest.get("mirror_of")
            if isinstance(mirror_of, str) and mirror_of:
                manifest_dir = Path(mirror_of).expanduser().resolve(strict=False).parent
                candidate = manifest_dir / phase_dir / "narrative.log"
                if candidate.exists():
                    return candidate
            checkpoint_path = manifest.get("checkpoint_path")
            if isinstance(checkpoint_path, str) and checkpoint_path:
                session_dir = Path(checkpoint_path).expanduser().resolve(strict=False).parent
                candidate = session_dir / phase_dir / "narrative.log"
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

        worktree = task.worktree or ""
        if not worktree:
            return None
        worktree_dir = self.project_dir / worktree
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

        child = (task_state or {}).get("child") if isinstance(task_state, dict) else None
        if isinstance(child, dict):
            cwd = child.get("cwd")
            if isinstance(cwd, str) and cwd:
                alt_worktree = Path(cwd)
                latest = paths.resolve_pointer(alt_worktree, paths.LATEST_POINTER)
                if latest is not None:
                    candidate = latest / phase_dir / "narrative.log"
                    if candidate.exists():
                        return candidate
        return None

    def _load_queue_cached(self) -> list[QueueTask]:
        queue_path = self.project_dir / ".otto-queue.yml"
        try:
            stat = queue_path.stat()
        except OSError:
            self._queue_cache = []
            self._queue_mtime_ns = None
            return []
        if self._queue_mtime_ns == stat.st_mtime_ns:
            return self._queue_cache
        try:
            tasks = load_queue(self.project_dir)
        except Exception as exc:
            logger.warning("failed to load queue.yml for dashboard: %s", exc)
            return self._queue_cache
        self._queue_cache = tasks
        self._queue_mtime_ns = stat.st_mtime_ns
        return tasks

    def _load_state_safe(self) -> dict[str, Any]:
        try:
            return load_state(self.project_dir)
        except Exception as exc:
            logger.warning("failed to load queue state for dashboard: %s", exc)
            return {"tasks": {}, "watcher": None}

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

        lines = [line.strip() for line in chunk.splitlines() if line.strip()]
        last_line = lines[-1] if lines else "-"
        phase = infer_phase_from_narrative(lines, default=default_phase)
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
            return False, []
        if self._path is None or path != self._path:
            self._path = path
            self._offset = 0
            self._pending = ""
            self._mtime = None
            return self._read_new(clear=True)
        return self._read_new(clear=False)

    def _read_new(self, *, clear: bool) -> tuple[bool, list[str]]:
        assert self._path is not None
        try:
            stat = self._path.stat()
        except OSError:
            return clear, []
        if stat.st_size < self._offset:
            self._offset = 0
            self._pending = ""
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
        return clear, lines


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


class OverviewScreen(Screen[None]):
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("enter", "open_task", "Open", show=False),
        Binding("c", "cancel_task", "Cancel", show=False),
        Binding("?", "show_help", "Help", show=False),
        Binding("q", "quit_dashboard", "Quit", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._row_order: list[str] = []
        self._refresh_timer = None

    def compose(self) -> ComposeResult:
        yield Static(id="overview-header")
        yield DataTable(id="overview-table")
        yield Static(id="overview-status")

    def on_mount(self) -> None:
        table = self.query_one("#overview-table", DataTable)
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
        self.app.last_snapshot = snapshot
        header = self.query_one("#overview-header", Static)
        header.update(
            f"[bold]otto queue[/bold] · concurrent={self.app.concurrent} · "
            f"{snapshot.header_elapsed} · {_format_cost(snapshot.total_cost_usd)}"
        )
        footer = self.query_one("#overview-status", Static)
        footer.update(
            f"{snapshot.running_count} running · {snapshot.queued_count} queued · "
            f"{snapshot.done_count} done · {snapshot.total_count} total · "
            "enter open · c cancel · ? help · q quit"
        )
        self._sync_rows(snapshot.tasks)

    def _sync_rows(self, tasks: Sequence[TaskView]) -> None:
        table = self.query_one("#overview-table", DataTable)
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

    def _selected_task_id(self) -> str | None:
        table = self.query_one("#overview-table", DataTable)
        if not self._row_order:
            return None
        row = min(max(table.cursor_row, 0), len(self._row_order) - 1)
        return self._row_order[row]

    def action_cursor_down(self) -> None:
        self.query_one("#overview-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#overview-table", DataTable).action_cursor_up()

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

    def action_show_help(self) -> None:
        self.app.push_screen(
            HelpModal(
                "Overview bindings",
                [
                    "j / Down: move to next row",
                    "k / Up: move to previous row",
                    "Enter: open task detail",
                    "c: queue cancel for selected running task",
                    "q: quit dashboard and stop watcher",
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
        Binding("pagedown", "page_down", "Page Down", show=False),
        Binding("pageup", "page_up", "Page Up", show=False),
        Binding("end", "follow_tail", "Tail", show=False),
        Binding("home", "top", "Top", show=False),
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
        yield RichLog(id="detail-log", max_lines=_NARRATIVE_MAX_LINES, markup=True, highlight=True)
        yield Static("[esc/q back · j/k scroll · pgup/pgdn · c cancel · end follow]", id="detail-status")

    def on_mount(self) -> None:
        self.query_one("#detail-log", RichLog).focus()
        self._refresh()
        self.set_interval(0.25, self._refresh)

    def _resolve_narrative_path(self) -> Path | None:
        current = self.app.model.resolve_task(self.task_id)
        if current is None:
            return None
        return current.narrative_path

    def _refresh(self) -> None:
        current = self.app.model.resolve_task(self.task_id)
        self._update_header(current)
        log = self.query_one("#detail-log", RichLog)
        clear, lines = self._tailer.poll()
        if clear:
            log.clear()
        for line in lines:
            log.write(Text(line), scroll_end=self._follow)
        if self._follow and lines:
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

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_scroll_down(self) -> None:
        self._follow = False
        self.query_one("#detail-log", RichLog).scroll_down(animate=False)

    def action_scroll_up(self) -> None:
        self._follow = False
        self.query_one("#detail-log", RichLog).scroll_up(animate=False)

    def action_page_down(self) -> None:
        self._follow = False
        self.query_one("#detail-log", RichLog).scroll_page_down(animate=False)

    def action_page_up(self) -> None:
        self._follow = False
        self.query_one("#detail-log", RichLog).scroll_page_up(animate=False)

    def action_follow_tail(self) -> None:
        self._follow = True
        self.query_one("#detail-log", RichLog).scroll_end(animate=False)

    def action_top(self) -> None:
        self._follow = False
        self.query_one("#detail-log", RichLog).scroll_home(animate=False)

    def action_cancel_task(self) -> None:
        self.app.cancel_task(self.task_id)

    def action_show_help(self) -> None:
        self.app.push_screen(
            HelpModal(
                f"Task detail bindings ({self.task_id})",
                [
                    "Esc / q: return to overview",
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

    #overview-table, #detail-log {
        height: 1fr;
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
    ) -> None:
        super().__init__()
        self.project_dir = project_dir
        self.concurrent = concurrent
        self.model = QueueModel(project_dir)
        self.cancel_callback = cancel_callback or (lambda task_id: _append_cancel_command(project_dir, task_id))
        self.runner = runner
        self.last_snapshot = QueueSnapshot([], None, "-", 0.0, 0, 0, 0, 0)

    async def on_mount(self) -> None:
        await self.push_screen(OverviewScreen())

    def request_shutdown(self) -> None:
        if self.runner is not None and self.runner.shutdown_level is None:
            self.runner.shutdown_level = "graceful"
        self.exit(0)

    def cancel_task(self, task_id: str) -> None:
        current = self.model.resolve_task(task_id)
        if current is None:
            self.notify(f"task {task_id} not found", severity="error")
            return
        if not current.can_cancel:
            self.notify("task is not running, cannot cancel", severity="warning")
            return
        self.cancel_callback(task_id)
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


async def _run_dashboard_async(app: QueueApp, runner: Runner, *, quiet: bool) -> int:
    runner_task = asyncio.create_task(runner.run_async())
    app_task = asyncio.create_task(app.run_async())

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
    runner_config: RunnerConfig,
    otto_bin: list[str] | str,
) -> int:
    """Run Textual UI + watcher on the same asyncio loop."""
    from otto.cli_queue import _install_runner_logging

    _install_runner_logging(project_dir, quiet=True)
    runner = Runner(project_dir, runner_config, otto_bin=otto_bin)
    app = QueueApp(project_dir, concurrent=concurrent, runner=runner)
    return asyncio.run(_run_dashboard_async(app, runner, quiet=quiet))
