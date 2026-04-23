"""Pure Mission Control model over the run registry and history."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from otto import paths
from otto.history import command_family, history_run_id, normalize_command_label
from otto.runs.history import read_history_rows
from otto.runs.registry import read_live_records
from otto.runs.schema import RunRecord, is_terminal_status
from otto.tui.mission_control_actions import ActionState

PaneName = Literal["live", "history", "detail"]
TypeFilter = Literal["all", "build", "improve", "certify", "merge", "queue"]
OutcomeFilter = Literal["all", "success", "failed", "interrupted", "cancelled"]

_TERMINAL_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[NO]"
)
_LOG_UNAVAILABLE_PLACEHOLDER = "<log file no longer available>"
_TYPE_FILTERS: tuple[TypeFilter, ...] = ("all", "build", "improve", "certify", "merge", "queue")
_OUTCOME_FILTERS: tuple[OutcomeFilter, ...] = ("all", "success", "failed", "interrupted", "cancelled")
_STATUS_PRIORITY = {
    "running": 0,
    "starting": 0,
    "terminating": 0,
    "queued": 1,
    "paused": 2,
    "interrupted": 3,
    "done": 4,
    "failed": 4,
    "cancelled": 4,
    "removed": 4,
}


@dataclass(slots=True)
class ArtifactRef:
    label: str
    path: str
    kind: str = "file"
    exists: bool = True


@dataclass(slots=True)
class DetailModel:
    title: str
    summary_lines: list[str]


class MissionControlAdapter(Protocol):
    def row_label(self, record: RunRecord) -> str: ...
    def history_summary(self, history_row: "HistoryRow") -> str: ...
    def artifacts(self, record: RunRecord) -> list[ArtifactRef]: ...
    def legal_actions(self, record: RunRecord, overlay: "StaleOverlay | None") -> list[ActionState]: ...
    def detail_panel_renderer(self, record: RunRecord) -> DetailModel: ...


@dataclass(slots=True)
class StaleOverlay:
    level: Literal["lagging", "stale"]
    label: str
    reason: str
    writer_alive: bool


@dataclass(slots=True)
class HistoryRow:
    run_id: str
    domain: str
    run_type: str
    command: str
    status: str
    terminal_outcome: str | None
    timestamp: str
    started_at: str | None
    finished_at: str | None
    queue_task_id: str | None
    merge_id: str | None
    intent: str
    branch: str | None
    worktree: str | None
    cost_usd: float | None
    duration_s: float | None
    resumable: bool
    manifest_path: str | None
    summary_path: str | None
    checkpoint_path: str | None
    primary_log_path: str | None
    dedupe_key: str
    history_kind: str
    adapter_key: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LiveRunItem:
    record: RunRecord
    overlay: StaleOverlay | None
    display_id: str
    branch_task: str
    elapsed_s: float | None
    elapsed_display: str
    cost_usd: float | None
    cost_display: str
    event: str
    row_label: str


@dataclass(slots=True)
class HistoryItem:
    row: HistoryRow
    completed_at_display: str
    outcome_display: str
    duration_display: str
    cost_display: str
    summary: str


@dataclass(slots=True)
class LiveRunsView:
    items: list[LiveRunItem]
    total_count: int
    active_count: int
    refresh_interval_s: float


@dataclass(slots=True)
class HistoryView:
    items: list[HistoryItem]
    page: int
    page_size: int
    total_rows: int
    total_pages: int


@dataclass(slots=True)
class SelectionState:
    run_id: str | None = None
    origin_pane: PaneName = "live"
    artifact_index: int = 0
    log_index: int = 0


@dataclass(slots=True)
class MissionControlFilters:
    active_only: bool = False
    type_filter: TypeFilter = "all"
    outcome_filter: OutcomeFilter = "all"
    query: str = ""
    history_page: int = 0


@dataclass(slots=True)
class MissionControlState:
    live_runs: LiveRunsView
    history_page: HistoryView
    selection: SelectionState
    focus: PaneName
    filters: MissionControlFilters
    last_event_banner: str | None = None


@dataclass(slots=True)
class DetailView:
    run_id: str
    source: Literal["live", "history"]
    record: RunRecord
    overlay: StaleOverlay | None
    detail: DetailModel
    artifacts: list[ArtifactRef]
    log_paths: list[str]
    selected_log_index: int
    selected_log_path: str | None
    legal_actions: list[ActionState]


@dataclass(slots=True)
class TailResult:
    clear: bool
    lines: list[str]


@dataclass(slots=True)
class _StaleTracker:
    heartbeat_seq: int
    writer_identity: tuple[Any, ...]
    last_progress_monotonic: float


class LogTailer:
    """Poll a log file and return appended complete lines."""

    def __init__(self, path_resolver: Callable[[], Path | None]) -> None:
        self._path_resolver = path_resolver
        self._path: Path | None = None
        self._offset = 0
        self._pending = ""
        self._mtime: float | None = None

    def poll(self) -> TailResult:
        path = self._path_resolver()
        if path is None:
            if self._path is not None:
                self._reset(clear_path=True)
                return TailResult(True, [_LOG_UNAVAILABLE_PLACEHOLDER])
            return TailResult(False, [])
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

    def _read_new(self, *, clear: bool) -> TailResult:
        assert self._path is not None
        try:
            stat = self._path.stat()
        except OSError:
            self._reset(clear_path=True)
            return TailResult(True, [_LOG_UNAVAILABLE_PLACEHOLDER])
        if stat.st_size < self._offset:
            self._reset(clear_path=False)
            clear = True
        if stat.st_size == self._offset and self._mtime == stat.st_mtime:
            return TailResult(clear, [])
        try:
            with self._path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except OSError:
            return TailResult(clear, [])
        self._offset = stat.st_size
        self._mtime = stat.st_mtime
        if not chunk:
            return TailResult(clear, [])
        text = self._pending + chunk.decode("utf-8", errors="replace")
        if text.endswith("\n"):
            lines = text.splitlines()
            self._pending = ""
        else:
            parts = text.splitlines()
            self._pending = parts.pop() if parts else text
            lines = parts
        return TailResult(clear, [_strip_terminal_escapes(line) for line in lines])


class MissionControlModel:
    """Read-only Mission Control model over the live registry and history."""

    def __init__(
        self,
        project_dir: Path,
        *,
        history_page_size: int = 50,
        now_fn: Callable[[], datetime] | None = None,
        monotonic_fn: Callable[[], float] | None = None,
        process_probe: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        self.project_dir = Path(project_dir)
        self.history_page_size = history_page_size
        self._now_fn = now_fn or _utc_now
        self._monotonic_fn = monotonic_fn or time.monotonic
        self._process_probe = process_probe or _writer_process_matches
        self._seen_live_ids: set[str] = set()
        self._seen_live_ids_bootstrapped = False
        self._stale_trackers: dict[str, _StaleTracker] = {}
        self._last_poll_monotonic: float | None = None
        self._last_poll_wall: datetime | None = None
        self._suspend_started_monotonic: float | None = None

    def initial_state(
        self,
        *,
        filters: MissionControlFilters | None = None,
        focus: PaneName = "live",
    ) -> MissionControlState:
        return self.refresh(
            previous_state=MissionControlState(
                live_runs=LiveRunsView([], 0, 0, 1.5),
                history_page=HistoryView([], 0, self.history_page_size, 0, 0),
                selection=SelectionState(),
                focus=focus,
                filters=filters or MissionControlFilters(),
                last_event_banner=None,
            )
        )

    def refresh(self, previous_state: MissionControlState | None = None) -> MissionControlState:
        filters = replace(previous_state.filters) if previous_state is not None else MissionControlFilters()
        focus = previous_state.focus if previous_state is not None else "live"
        selection = replace(previous_state.selection) if previous_state is not None else SelectionState()

        now = self._now_fn()
        monotonic_now = self._monotonic_fn()
        self._detect_suspend(now, monotonic_now)

        live_records = read_live_records(self.project_dir)
        live_items = self._build_live_items(live_records, filters, now, monotonic_now)
        live_view = LiveRunsView(
            items=live_items,
            total_count=len(live_items),
            active_count=sum(1 for item in live_items if not is_terminal_status(item.record.status)),
            refresh_interval_s=0.5 if any(not is_terminal_status(record.status) for record in live_records) else 1.5,
        )

        history_rows = self._dedupe_history_rows(read_history_rows(paths.history_jsonl(self.project_dir)))
        history_items = self._build_history_items(history_rows, filters)
        total_rows = len(history_items)
        total_pages = max(1, (total_rows + self.history_page_size - 1) // self.history_page_size) if total_rows else 1
        page = min(max(filters.history_page, 0), max(0, total_pages - 1))
        filters.history_page = page
        start = page * self.history_page_size
        history_view = HistoryView(
            items=history_items[start:start + self.history_page_size],
            page=page,
            page_size=self.history_page_size,
            total_rows=total_rows,
            total_pages=total_pages,
        )

        selection = self._preserve_selection(selection, live_view, history_view, history_rows)
        banner = self._banner_for_new_runs(live_records)

        self._last_poll_monotonic = monotonic_now
        self._last_poll_wall = now

        return MissionControlState(
            live_runs=live_view,
            history_page=history_view,
            selection=selection,
            focus=focus,
            filters=filters,
            last_event_banner=banner,
        )

    def detail_view(self, state: MissionControlState) -> DetailView | None:
        selected_record, overlay, source = self.selected_record(state)
        if selected_record is None:
            return None
        adapter = self._adapter_for_key(selected_record.adapter_key or _adapter_key_for_record(selected_record))
        artifacts = adapter.artifacts(selected_record)
        log_paths = [artifact.path for artifact in artifacts if artifact.kind == "log"]
        selected_log_index = min(max(state.selection.log_index, 0), max(0, len(log_paths) - 1)) if log_paths else 0
        legal_actions = adapter.legal_actions(selected_record, overlay)
        return DetailView(
            run_id=selected_record.run_id,
            source=source,
            record=selected_record,
            overlay=overlay,
            detail=adapter.detail_panel_renderer(selected_record),
            artifacts=artifacts,
            log_paths=log_paths,
            selected_log_index=selected_log_index,
            selected_log_path=log_paths[selected_log_index] if log_paths else None,
            legal_actions=legal_actions,
        )

    def selected_record(self, state: MissionControlState) -> tuple[RunRecord | None, StaleOverlay | None, Literal["live", "history"]]:
        if not state.selection.run_id:
            return None, None, "live"
        for item in state.live_runs.items:
            if item.record.run_id == state.selection.run_id:
                return item.record, item.overlay, "live"
        row = self._find_history_row(state, state.selection.run_id)
        if row is None:
            return None, None, "live"
        return _history_row_to_record(self.project_dir, row), None, "history"

    def log_tailer(self, state: MissionControlState) -> LogTailer:
        return LogTailer(lambda: self._selected_log_path(state))

    def cycle_type_filter(self, state: MissionControlState) -> MissionControlState:
        idx = _TYPE_FILTERS.index(state.filters.type_filter)
        state.filters.type_filter = _TYPE_FILTERS[(idx + 1) % len(_TYPE_FILTERS)]
        state.filters.history_page = 0
        return state

    def cycle_outcome_filter(self, state: MissionControlState) -> MissionControlState:
        idx = _OUTCOME_FILTERS.index(state.filters.outcome_filter)
        state.filters.outcome_filter = _OUTCOME_FILTERS[(idx + 1) % len(_OUTCOME_FILTERS)]
        state.filters.history_page = 0
        return state

    def next_history_page(self, state: MissionControlState) -> MissionControlState:
        state.filters.history_page += 1
        return state

    def previous_history_page(self, state: MissionControlState) -> MissionControlState:
        state.filters.history_page = max(0, state.filters.history_page - 1)
        return state

    def _selected_log_path(self, state: MissionControlState) -> Path | None:
        detail = self.detail_view(state)
        if detail is None or detail.selected_log_path is None:
            return None
        return Path(detail.selected_log_path)

    def _build_live_items(
        self,
        records: list[RunRecord],
        filters: MissionControlFilters,
        now: datetime,
        monotonic_now: float,
    ) -> list[LiveRunItem]:
        items: list[LiveRunItem] = []
        cutoff = now.timestamp() - 300.0
        for record in records:
            retention_time = _parse_iso(record.timing.get("finished_at") or record.timing.get("updated_at"))
            if is_terminal_status(record.status) and retention_time is not None and retention_time.timestamp() < cutoff:
                continue
            if filters.active_only and is_terminal_status(record.status):
                continue
            if filters.type_filter != "all" and record.run_type != filters.type_filter:
                continue
            overlay = self._derive_overlay(record, now, monotonic_now)
            adapter = self._adapter_for_key(record.adapter_key or _adapter_key_for_record(record))
            elapsed_s = _elapsed_seconds(record, now)
            cost_usd = _coerce_float(record.metrics.get("cost_usd"))
            items.append(
                LiveRunItem(
                    record=record,
                    overlay=overlay,
                    display_id=str(record.identity.get("queue_task_id") or record.run_id),
                    branch_task=_branch_or_task(record),
                    elapsed_s=elapsed_s,
                    elapsed_display=_format_elapsed(elapsed_s),
                    cost_usd=cost_usd,
                    cost_display=_format_cost(cost_usd, pending=record.status in {"running", "starting", "terminating"}),
                    event=_truncate(_strip_terminal_escapes(record.last_event or "-"), 96),
                    row_label=adapter.row_label(record),
                )
            )
        items.sort(key=self._live_sort_key)
        return items

    def _build_history_items(self, rows: list[HistoryRow], filters: MissionControlFilters) -> list[HistoryItem]:
        items: list[HistoryItem] = []
        for row in rows:
            if filters.type_filter != "all" and row.run_type != filters.type_filter:
                continue
            outcome = _history_outcome(row)
            if filters.outcome_filter != "all" and outcome != filters.outcome_filter:
                continue
            if filters.query and not _history_matches_query(row, filters.query):
                continue
            adapter = self._adapter_for_key(row.adapter_key)
            items.append(
                HistoryItem(
                    row=row,
                    completed_at_display=_short_timestamp(row.finished_at or row.timestamp),
                    outcome_display=(row.terminal_outcome or row.status or "-").upper(),
                    duration_display=_format_elapsed(row.duration_s),
                    cost_display=_format_cost(row.cost_usd),
                    summary=adapter.history_summary(row),
                )
            )
        items.sort(key=lambda item: -(_parse_iso(item.row.finished_at or item.row.timestamp) or _epoch()).timestamp())
        return items

    def _dedupe_history_rows(self, raw_rows: list[dict[str, Any]]) -> list[HistoryRow]:
        best_by_dedupe: dict[str, HistoryRow] = {}
        for raw in raw_rows:
            row = _normalize_history_row(raw)
            if row is None:
                continue
            current = best_by_dedupe.get(row.dedupe_key)
            if current is None or _history_preference_key(row) > _history_preference_key(current):
                best_by_dedupe[row.dedupe_key] = row
        best_by_run_id: dict[str, HistoryRow] = {}
        for row in best_by_dedupe.values():
            current = best_by_run_id.get(row.run_id)
            if current is None or _run_history_preference_key(row) > _run_history_preference_key(current):
                best_by_run_id[row.run_id] = row
        return list(best_by_run_id.values())

    def _preserve_selection(
        self,
        selection: SelectionState,
        live_view: LiveRunsView,
        history_view: HistoryView,
        all_history_rows: list[HistoryRow],
    ) -> SelectionState:
        selected_run_id = selection.run_id
        if selected_run_id:
            if any(item.record.run_id == selected_run_id for item in live_view.items):
                return selection
            if any(row.run_id == selected_run_id for row in all_history_rows):
                selection.origin_pane = "history"
                return selection
        if live_view.items:
            selection.run_id = live_view.items[0].record.run_id
            selection.origin_pane = "live"
            selection.artifact_index = 0
            selection.log_index = 0
            return selection
        if history_view.items:
            selection.run_id = history_view.items[0].row.run_id
            selection.origin_pane = "history"
            selection.artifact_index = 0
            selection.log_index = 0
            return selection
        return selection

    def _find_history_row(self, state: MissionControlState, run_id: str) -> HistoryRow | None:
        for item in state.history_page.items:
            if item.row.run_id == run_id:
                return item.row
        rows = self._dedupe_history_rows(read_history_rows(paths.history_jsonl(self.project_dir)))
        for row in rows:
            if row.run_id == run_id:
                return row
        return None

    def _live_sort_key(self, item: LiveRunItem) -> tuple[int, int, float]:
        updated_at = _parse_iso(item.record.timing.get("updated_at")) or _epoch()
        return (
            1 if is_terminal_status(item.record.status) else 0,
            _STATUS_PRIORITY.get(item.record.status, 9),
            -updated_at.timestamp(),
        )

    def _derive_overlay(
        self,
        record: RunRecord,
        now: datetime,
        monotonic_now: float,
    ) -> StaleOverlay | None:
        if is_terminal_status(record.status):
            self._stale_trackers.pop(record.run_id, None)
            return None

        heartbeat_interval_s = max(_coerce_float(record.timing.get("heartbeat_interval_s")) or 2.0, 0.1)
        stale_threshold_s = max(3.0 * heartbeat_interval_s, 15.0)
        heartbeat_seq = int(record.timing.get("heartbeat_seq") or 0)
        writer_identity = _writer_identity(record.writer)
        tracker = self._stale_trackers.get(record.run_id)
        progressed = tracker is None or heartbeat_seq > tracker.heartbeat_seq or writer_identity != tracker.writer_identity
        if progressed:
            tracker = _StaleTracker(heartbeat_seq, writer_identity, monotonic_now)
            self._stale_trackers[record.run_id] = tracker
            return None

        heartbeat_at = _parse_iso(record.timing.get("heartbeat_at"))
        wall_age_s = max(0.0, (now - heartbeat_at).total_seconds()) if heartbeat_at is not None else 0.0
        writer_alive = self._process_probe(record.writer)
        grace_active = (
            self._suspend_started_monotonic is not None
            and (monotonic_now - self._suspend_started_monotonic) < stale_threshold_s
        )
        if grace_active and wall_age_s > heartbeat_interval_s:
            return StaleOverlay("lagging", "LAGGING", "reader grace window after suspend/clock jump", writer_alive)
        if wall_age_s > heartbeat_interval_s and writer_alive:
            return StaleOverlay("lagging", "LAGGING", "heartbeat overdue but writer still alive", True)
        if (monotonic_now - tracker.last_progress_monotonic) >= stale_threshold_s and not writer_alive:
            return StaleOverlay("stale", "STALE", "heartbeat stalled and writer identity is gone", False)
        return None

    def _detect_suspend(self, now: datetime, monotonic_now: float) -> None:
        if self._last_poll_monotonic is None or self._last_poll_wall is None:
            return
        monotonic_delta = monotonic_now - self._last_poll_monotonic
        wall_delta = (now - self._last_poll_wall).total_seconds()
        if monotonic_delta > 30.0 or abs(wall_delta - monotonic_delta) > 30.0:
            self._suspend_started_monotonic = monotonic_now

    def _banner_for_new_runs(self, live_records: list[RunRecord]) -> str | None:
        live_ids = {record.run_id for record in live_records if not is_terminal_status(record.status)}
        if not self._seen_live_ids_bootstrapped:
            self._seen_live_ids = live_ids
            self._seen_live_ids_bootstrapped = True
            return None
        new_ids = live_ids - self._seen_live_ids
        self._seen_live_ids = live_ids
        return "new run detected" if new_ids else None

    def _adapter_for_key(self, adapter_key: str) -> MissionControlAdapter:
        from otto.tui.adapters import adapter_for_key

        return adapter_for_key(adapter_key)


def _normalize_history_row(raw: dict[str, Any]) -> HistoryRow | None:
    if not isinstance(raw, dict):
        return None
    run_id = history_run_id(raw)
    if not run_id:
        return None
    command = normalize_command_label(raw.get("command"))
    run_type = str(raw.get("run_type") or command_family(command) or "build")
    domain = str(raw.get("domain") or ("merge" if run_type == "merge" else "queue" if run_type == "queue" else "atomic"))
    status = str(raw.get("status") or ("done" if raw.get("passed") else "failed"))
    finished_at = _string_or_none(raw.get("finished_at") or raw.get("timestamp"))
    timestamp = _string_or_none(raw.get("timestamp")) or finished_at or _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    return HistoryRow(
        run_id=run_id,
        domain=domain,
        run_type=run_type,
        command=command,
        status=status,
        terminal_outcome=_string_or_none(raw.get("terminal_outcome")),
        timestamp=timestamp,
        started_at=_string_or_none(raw.get("started_at")),
        finished_at=finished_at,
        queue_task_id=_string_or_none(raw.get("queue_task_id")),
        merge_id=_string_or_none(raw.get("merge_id")),
        intent=_string_or_none(raw.get("intent")) or "",
        branch=_string_or_none(raw.get("branch")),
        worktree=_string_or_none(raw.get("worktree")),
        cost_usd=_coerce_float(raw.get("cost_usd")),
        duration_s=_coerce_float(raw.get("duration_s")),
        resumable=bool(raw.get("resumable", True)),
        manifest_path=_string_or_none(raw.get("manifest_path")),
        summary_path=_string_or_none(raw.get("summary_path")),
        checkpoint_path=_string_or_none(raw.get("checkpoint_path")),
        primary_log_path=_string_or_none(raw.get("primary_log_path")),
        dedupe_key=_string_or_none(raw.get("dedupe_key")) or f"terminal_snapshot:{run_id}",
        history_kind=_string_or_none(raw.get("history_kind")) or "terminal_snapshot",
        adapter_key=_adapter_key_for_history(domain=domain, run_type=run_type),
        raw=dict(raw),
    )


def _history_row_to_record(project_dir: Path, row: HistoryRow) -> RunRecord:
    return RunRecord(
        run_id=row.run_id,
        domain=row.domain,
        run_type=row.run_type,
        command=row.command,
        display_name=row.intent or row.run_id,
        status=row.status,
        terminal_outcome=row.terminal_outcome,
        project_dir=str(project_dir.resolve(strict=False)),
        cwd=str(project_dir.resolve(strict=False)),
        writer={},
        identity={
            "queue_task_id": row.queue_task_id,
            "merge_id": row.merge_id,
            "parent_run_id": None,
        },
        source={"argv": [], "resumable": row.resumable, "invoked_via": "history"},
        timing={
            "started_at": row.started_at,
            "updated_at": row.finished_at or row.timestamp,
            "heartbeat_at": row.finished_at or row.timestamp,
            "finished_at": row.finished_at or row.timestamp,
            "duration_s": row.duration_s,
            "heartbeat_interval_s": 2.0,
            "heartbeat_seq": 0,
        },
        git={"branch": row.branch, "worktree": row.worktree, "target_branch": None, "head_sha": None},
        intent={"summary": row.intent, "intent_path": None, "spec_path": None},
        artifacts={
            "session_dir": str(project_dir.resolve(strict=False)),
            "manifest_path": row.manifest_path,
            "checkpoint_path": row.checkpoint_path,
            "summary_path": row.summary_path,
            "primary_log_path": row.primary_log_path,
            "extra_log_paths": [],
        },
        metrics={"cost_usd": row.cost_usd},
        adapter_key=row.adapter_key,
        last_event=row.terminal_outcome or row.status,
    )


def _history_preference_key(row: HistoryRow) -> tuple[int, float]:
    ts = (_parse_iso(row.finished_at or row.timestamp) or _epoch()).timestamp()
    return (1 if row.history_kind == "terminal_snapshot" else 0, ts)


def _run_history_preference_key(row: HistoryRow) -> tuple[int, int, float]:
    ts = (_parse_iso(row.finished_at or row.timestamp) or _epoch()).timestamp()
    return (
        1 if row.history_kind == "terminal_snapshot" else 0,
        1 if row.dedupe_key.startswith("terminal_snapshot:") else 0,
        ts,
    )


def _history_matches_query(row: HistoryRow, query: str) -> bool:
    needle = query.strip().lower()
    if not needle:
        return True
    haystack = " ".join(
        part
        for part in (
            row.intent,
            row.branch or "",
            row.queue_task_id or "",
            row.run_id,
        )
        if part
    ).lower()
    return needle in haystack


def _history_outcome(row: HistoryRow) -> OutcomeFilter:
    outcome = (row.terminal_outcome or row.status or "").lower()
    if outcome in {"success", "done"}:
        return "success"
    if outcome in {"failure", "failed"}:
        return "failed"
    if outcome == "interrupted":
        return "interrupted"
    return "cancelled"


def _elapsed_seconds(record: RunRecord, now: datetime) -> float | None:
    duration = _coerce_float(record.timing.get("duration_s"))
    if duration is not None and is_terminal_status(record.status):
        return duration
    started_at = _parse_iso(record.timing.get("started_at"))
    finished_at = _parse_iso(record.timing.get("finished_at"))
    if started_at is None:
        return duration
    if finished_at is not None:
        return max(0.0, (finished_at - started_at).total_seconds())
    return max(0.0, (now - started_at).total_seconds())


def _adapter_key_for_record(record: RunRecord) -> str:
    if record.domain == "queue":
        return "queue.attempt"
    if record.domain == "merge":
        return "merge.run"
    return f"atomic.{record.run_type or 'build'}"


def _adapter_key_for_history(*, domain: str, run_type: str) -> str:
    if domain == "queue" or run_type == "queue":
        return "queue.attempt"
    if domain == "merge" or run_type == "merge":
        return "merge.run"
    return f"atomic.{run_type or 'build'}"


def _branch_or_task(record: RunRecord) -> str:
    return str(
        record.git.get("branch")
        or record.identity.get("queue_task_id")
        or record.git.get("target_branch")
        or "-"
    )


def _writer_identity(writer: dict[str, Any]) -> tuple[Any, ...]:
    return (
        writer.get("pid"),
        writer.get("process_start_time_ns"),
        writer.get("boot_id"),
        writer.get("writer_id"),
    )


def _writer_process_matches(writer: dict[str, Any]) -> bool:
    pid = writer.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    expected_start_ns = writer.get("process_start_time_ns")
    expected_boot_id = str(writer.get("boot_id") or "").strip()
    if expected_boot_id:
        current_boot = _boot_id()
        if current_boot and current_boot != expected_boot_id:
            return False
    try:
        import psutil

        proc = psutil.Process(pid)
        actual_start_ns = int(proc.create_time() * 1_000_000_000)
        if isinstance(expected_start_ns, int) and abs(actual_start_ns - expected_start_ns) > 5_000_000_000:
            return False
        return proc.is_running()
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _truncate(text: str, limit: int = 88) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)] + "…"


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


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _epoch() -> datetime:
    return datetime.fromtimestamp(0, tz=timezone.utc)


def _short_timestamp(value: str | None) -> str:
    parsed = _parse_iso(value)
    if parsed is None:
        return "-"
    return parsed.strftime("%Y-%m-%d %H:%M")


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _strip_terminal_escapes(text: str) -> str:
    return _TERMINAL_ESCAPE_RE.sub("", text or "")
