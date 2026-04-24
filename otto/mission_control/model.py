"""Pure Mission Control model over the run registry and history."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from otto import paths
from otto.history import command_family, history_run_id, normalize_command_label
from otto.runs.history import load_project_history_rows
from otto.runs.registry import HEARTBEAT_INTERVAL_S, load_live_record, read_live_records, writer_identity_matches_live_process
from otto.runs.schema import RunRecord, is_terminal_status
from otto.mission_control.actions import ActionResult, ActionState

PaneName = Literal["live", "history", "detail"]
TypeFilter = Literal["all", "build", "improve", "certify", "merge", "queue"]
OutcomeFilter = Literal["all", "success", "failed", "interrupted", "cancelled", "removed", "other"]

_TERMINAL_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[NO]"
)
_LOG_UNAVAILABLE_PLACEHOLDER = "<log file no longer available>"
_TYPE_FILTERS: tuple[TypeFilter, ...] = ("all", "build", "improve", "certify", "merge", "queue")
_OUTCOME_FILTERS: tuple[OutcomeFilter, ...] = ("all", "success", "failed", "interrupted", "cancelled", "removed")
_DEFAULT_LIVE_RECORDS_LOADER = read_live_records
logger = logging.getLogger("otto.mission_control.model")
_WARNED_UNKNOWN_HISTORY_OUTCOMES: set[str] = set()
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

    @classmethod
    def from_path(cls, label: str, path: str, *, kind: str = "file") -> "ArtifactRef":
        candidate = Path(path)
        return cls(label=label, path=path, kind=kind, exists=candidate.exists())


@dataclass(slots=True)
class DetailModel:
    title: str
    summary_lines: list[str]


class MissionControlAdapter(Protocol):
    def row_label(self, record: RunRecord) -> str: ...
    def history_summary(self, history_row: "HistoryRow") -> str: ...
    def artifacts(self, record: RunRecord) -> list[ArtifactRef]: ...
    def legal_actions(self, record: RunRecord, overlay: "StaleOverlay | None") -> list[ActionState]: ...
    def execute(
        self,
        record: RunRecord,
        action_kind: str,
        project_dir: Path,
        *,
        selected_artifact_path: str | None = None,
        selected_queue_task_ids: list[str] | None = None,
        post_result: Callable[[ActionResult], None] | None = None,
    ) -> ActionResult: ...
    def detail_panel_renderer(self, record: RunRecord) -> DetailModel: ...
    def legacy_records(self, project_dir: Path, now: datetime, live_records: list[RunRecord]) -> list[RunRecord]: ...
    def live_overlay(self, record: RunRecord, overlay: "StaleOverlay | None") -> "StaleOverlay | None": ...


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
    session_dir: str | None
    intent_path: str | None
    spec_path: str | None
    manifest_path: str | None
    summary_path: str | None
    checkpoint_path: str | None
    primary_log_path: str | None
    extra_log_paths: list[str]
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
    selected_run_ids: set[str]
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


@dataclass(slots=True)
class LiveRegistryCacheStats:
    directory_checks: int = 0
    snapshot_hits: int = 0
    file_stats: int = 0
    record_hits: int = 0
    record_misses: int = 0


@dataclass(slots=True)
class _CachedLiveRecord:
    mtime_ns: int
    size: int
    record: RunRecord


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
        queue_compat: bool = False,
        now_fn: Callable[[], datetime] | None = None,
        monotonic_fn: Callable[[], float] | None = None,
        process_probe: Callable[[dict[str, Any]], bool] | None = None,
        live_records_loader: Callable[[Path], list[RunRecord]] | None = None,
    ) -> None:
        self.project_dir = Path(project_dir)
        self.history_page_size = history_page_size
        self.queue_compat = queue_compat
        self._now_fn = now_fn or _utc_now
        self._monotonic_fn = monotonic_fn or time.monotonic
        self._process_probe = process_probe or _writer_process_matches
        self._live_records_loader = live_records_loader or read_live_records
        self._seen_live_ids: set[str] = set()
        self._seen_live_ids_bootstrapped = False
        self._stale_trackers: dict[str, _StaleTracker] = {}
        self._last_poll_monotonic: float | None = None
        self._last_poll_wall: datetime | None = None
        self._suspend_started_monotonic: float | None = None
        self._live_registry_dir_mtime_ns: int | None = None
        self._live_registry_cache_ready = False
        self._live_registry_snapshot: list[RunRecord] = []
        self._live_registry_entries: dict[str, _CachedLiveRecord] = {}
        self._live_registry_cache_stats = LiveRegistryCacheStats()

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
                selected_run_ids=set(),
                focus=focus,
                filters=filters or MissionControlFilters(),
                last_event_banner=None,
            )
        )

    def refresh(self, previous_state: MissionControlState | None = None) -> MissionControlState:
        filters = replace(previous_state.filters) if previous_state is not None else MissionControlFilters()
        focus = previous_state.focus if previous_state is not None else "live"
        selection = replace(previous_state.selection) if previous_state is not None else SelectionState()
        selected_run_ids = set(previous_state.selected_run_ids) if previous_state is not None else set()

        now = self._now_fn()
        monotonic_now = self._monotonic_fn()
        self._detect_suspend(now, monotonic_now)

        live_records = self._load_live_records(now)
        live_items = self._build_live_items(live_records, filters, now, monotonic_now)
        live_view = LiveRunsView(
            items=live_items,
            total_count=len(live_items),
            active_count=sum(1 for item in live_items if _live_item_is_active(item)),
            refresh_interval_s=0.5 if any(_live_item_is_active(item) for item in live_items) else 1.5,
        )

        history_rows = self._dedupe_history_rows(load_project_history_rows(self.project_dir))
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
        selected_run_ids = self._preserve_selected_run_ids(selected_run_ids, live_view)
        banner = self._banner_for_new_runs(live_records)

        self._last_poll_monotonic = monotonic_now
        self._last_poll_wall = now

        return MissionControlState(
            live_runs=live_view,
            history_page=history_view,
            selection=selection,
            selected_run_ids=selected_run_ids,
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

    def live_registry_cache_stats(self) -> LiveRegistryCacheStats:
        return replace(self._live_registry_cache_stats)

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

    def _load_live_records(self, now: datetime) -> list[RunRecord]:
        records = (
            self._cached_live_records()
            if self._live_records_loader is _DEFAULT_LIVE_RECORDS_LOADER
            else list(self._live_records_loader(self.project_dir))
        )
        if not self.queue_compat:
            return records
        merged = list(records)
        from otto.mission_control.adapters import all_adapters

        for adapter in all_adapters():
            merged.extend(adapter.legacy_records(self.project_dir, now, records))
        return merged

    def _cached_live_records(self) -> list[RunRecord]:
        live_dir = paths.live_runs_dir(self.project_dir)
        self._live_registry_cache_stats.directory_checks += 1
        try:
            dir_stat = live_dir.stat()
        except OSError:
            self._live_registry_dir_mtime_ns = None
            self._live_registry_cache_ready = True
            self._live_registry_snapshot = []
            self._live_registry_entries.clear()
            return []

        if self._live_registry_cache_ready and dir_stat.st_mtime_ns == self._live_registry_dir_mtime_ns:
            self._live_registry_cache_stats.snapshot_hits += 1
            return list(self._live_registry_snapshot)

        records: list[RunRecord] = []
        next_entries: dict[str, _CachedLiveRecord] = {}
        for path in sorted(live_dir.glob("*.json")):
            try:
                stat = path.stat()
            except OSError:
                continue
            self._live_registry_cache_stats.file_stats += 1
            cache_key = str(path)
            cached = self._live_registry_entries.get(cache_key)
            if cached is not None and cached.mtime_ns == stat.st_mtime_ns and cached.size == stat.st_size:
                self._live_registry_cache_stats.record_hits += 1
                record = cached.record
            else:
                try:
                    record = load_live_record(self.project_dir, path.stem)
                except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                    continue
                self._live_registry_cache_stats.record_misses += 1
            records.append(record)
            next_entries[cache_key] = _CachedLiveRecord(
                mtime_ns=stat.st_mtime_ns,
                size=stat.st_size,
                record=record,
            )

        self._live_registry_dir_mtime_ns = dir_stat.st_mtime_ns
        self._live_registry_cache_ready = True
        self._live_registry_snapshot = records
        self._live_registry_entries = next_entries
        return list(records)

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
            if filters.type_filter != "all" and record.run_type != filters.type_filter:
                continue
            adapter = self._adapter_for_key(record.adapter_key or _adapter_key_for_record(record))
            row_label = adapter.row_label(record)
            if filters.query and not _live_record_matches_query(record, row_label, filters.query):
                continue
            overlay = adapter.live_overlay(record, self._derive_overlay(record, now, monotonic_now))
            if filters.active_only and not _status_is_effectively_active(record.status, overlay):
                continue
            elapsed_s = _elapsed_seconds(record, now, overlay)
            cost_usd = _coerce_float(record.metrics.get("cost_usd"))
            token_usage = _record_token_usage(record)
            effectively_active = _status_is_effectively_active(record.status, overlay)
            items.append(
                LiveRunItem(
                    record=record,
                    overlay=overlay,
                    display_id=str(record.identity.get("queue_task_id") or record.run_id),
                    branch_task=_branch_or_task(record),
                    elapsed_s=elapsed_s,
                    elapsed_display=_format_elapsed(elapsed_s),
                    cost_usd=cost_usd,
                    cost_display=_format_usage(cost_usd, token_usage, pending=effectively_active),
                    event=_live_event(record, overlay),
                    row_label=row_label,
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
                    cost_display=_format_usage(row.cost_usd, _history_token_usage(row)),
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
        rows = self._dedupe_history_rows(load_project_history_rows(self.project_dir))
        for row in rows:
            if row.run_id == run_id:
                return row
        return None

    def _preserve_selected_run_ids(self, selected_run_ids: set[str], live_view: LiveRunsView) -> set[str]:
        live_ids = {item.record.run_id for item in live_view.items}
        return {run_id for run_id in selected_run_ids if run_id in live_ids}

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

        heartbeat_interval_s = max(_coerce_float(record.timing.get("heartbeat_interval_s")) or HEARTBEAT_INTERVAL_S, 0.1)
        stale_threshold_s = max(3.0 * heartbeat_interval_s, 15.0)
        heartbeat_seq = int(record.timing.get("heartbeat_seq") or 0)
        writer_identity = _writer_identity(record.writer)
        heartbeat_at = _parse_iso(record.timing.get("heartbeat_at"))
        wall_age_s = max(0.0, (now - heartbeat_at).total_seconds()) if heartbeat_at is not None else 0.0
        writer_alive: bool | None = None
        tracker = self._stale_trackers.get(record.run_id)
        progressed = tracker is None or heartbeat_seq > tracker.heartbeat_seq or writer_identity != tracker.writer_identity
        if progressed:
            tracker = _StaleTracker(heartbeat_seq, writer_identity, monotonic_now)
            self._stale_trackers[record.run_id] = tracker
            if wall_age_s >= stale_threshold_s:
                writer_alive = self._process_probe(record.writer)
                if not writer_alive:
                    return StaleOverlay("stale", "STALE", "heartbeat stalled and writer identity is gone", False)
                if wall_age_s > heartbeat_interval_s:
                    return StaleOverlay("lagging", "LAGGING", "heartbeat overdue but writer still alive", True)
            return None

        writer_alive = self._process_probe(record.writer)
        grace_active = (
            self._suspend_started_monotonic is not None
            and (monotonic_now - self._suspend_started_monotonic) < stale_threshold_s
        )
        if grace_active and wall_age_s > heartbeat_interval_s and writer_alive:
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
        from otto.mission_control.adapters import adapter_for_key

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
        resumable=bool(raw.get("resumable", False)),
        session_dir=_history_artifact_path(raw, "session_dir"),
        intent_path=_string_or_none((raw.get("intent") or {}).get("intent_path")) if isinstance(raw.get("intent"), dict) else _string_or_none(raw.get("intent_path")),
        spec_path=_string_or_none((raw.get("intent") or {}).get("spec_path")) if isinstance(raw.get("intent"), dict) else _string_or_none(raw.get("spec_path")),
        manifest_path=_history_artifact_path(raw, "manifest_path"),
        summary_path=_history_artifact_path(raw, "summary_path"),
        checkpoint_path=_history_artifact_path(raw, "checkpoint_path"),
        primary_log_path=_history_artifact_path(raw, "primary_log_path"),
        extra_log_paths=_history_extra_log_paths(raw),
        dedupe_key=_string_or_none(raw.get("dedupe_key")) or f"terminal_snapshot:{run_id}",
        history_kind=_string_or_none(raw.get("history_kind")) or "terminal_snapshot",
        adapter_key=_adapter_key_for_history(domain=domain, run_type=run_type),
        raw=dict(raw),
    )


def _live_item_is_active(item: LiveRunItem) -> bool:
    return _status_is_effectively_active(item.record.status, item.overlay)


def _status_is_effectively_active(status: str | None, overlay: StaleOverlay | None) -> bool:
    if is_terminal_status(status):
        return False
    if overlay is not None and overlay.level == "stale":
        return False
    return True


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
        source=_history_source(row),
        timing={
            "started_at": row.started_at,
            "updated_at": row.finished_at or row.timestamp,
            "heartbeat_at": row.finished_at or row.timestamp,
            "finished_at": row.finished_at or row.timestamp,
            "duration_s": row.duration_s,
            "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
            "heartbeat_seq": 0,
        },
        git={"branch": row.branch, "worktree": row.worktree, "target_branch": None, "head_sha": None},
        intent={"summary": row.intent, "intent_path": row.intent_path, "spec_path": row.spec_path},
        artifacts={
            "session_dir": row.session_dir,
            "manifest_path": row.manifest_path,
            "checkpoint_path": row.checkpoint_path,
            "summary_path": row.summary_path,
            "primary_log_path": row.primary_log_path,
            "extra_log_paths": list(row.extra_log_paths),
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


def _history_source(row: HistoryRow) -> dict[str, Any]:
    source: dict[str, Any] = {"argv": [], "resumable": row.resumable, "invoked_via": "history"}
    raw_source = row.raw.get("source")
    if isinstance(raw_source, dict):
        source.update(raw_source)
    raw_argv = row.raw.get("argv")
    if isinstance(raw_argv, list) and raw_argv:
        source["argv"] = [str(part) for part in raw_argv]
    if not source.get("argv"):
        manifest = _read_history_manifest(row.manifest_path)
        argv = manifest.get("argv") if isinstance(manifest, dict) else None
        if isinstance(argv, list) and argv:
            source["argv"] = [str(part) for part in argv]
    return source


def _read_history_manifest(path_value: Any) -> dict[str, Any]:
    path = _string_or_none(path_value)
    if not path:
        return {}
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


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


def _live_record_matches_query(record: RunRecord, row_label: str, query: str) -> bool:
    needle = query.strip().lower()
    if not needle:
        return True
    argv = record.source.get("argv")
    argv_text = " ".join(str(part) for part in argv) if isinstance(argv, list) else ""
    haystack = " ".join(
        str(part)
        for part in (
            record.run_id,
            record.domain,
            record.run_type,
            record.command,
            record.display_name,
            record.status,
            record.terminal_outcome or "",
            record.identity.get("queue_task_id") or "",
            record.identity.get("merge_id") or "",
            record.git.get("branch") or "",
            record.git.get("worktree") or "",
            record.intent.get("summary") or "",
            row_label,
            record.last_event,
            argv_text,
        )
        if part
    ).lower()
    return needle in haystack


def _history_artifact_path(raw: dict[str, Any], key: str) -> str | None:
    artifacts = raw.get("artifacts")
    if isinstance(artifacts, dict):
        value = _string_or_none(artifacts.get(key))
        if value:
            return value
    return _string_or_none(raw.get(key))


def _history_extra_log_paths(raw: dict[str, Any]) -> list[str]:
    artifacts = raw.get("artifacts")
    if isinstance(artifacts, dict):
        value = artifacts.get("extra_log_paths")
        if isinstance(value, list):
            return [str(path).strip() for path in value if str(path).strip()]
    value = raw.get("extra_log_paths")
    if isinstance(value, list):
        return [str(path).strip() for path in value if str(path).strip()]
    return []


def _history_outcome(row: HistoryRow) -> OutcomeFilter:
    outcome = (row.terminal_outcome or row.status or "").lower()
    if outcome in {"success", "done"}:
        return "success"
    if outcome in {"failure", "failed"}:
        return "failed"
    if outcome == "interrupted":
        return "interrupted"
    if outcome == "removed":
        return "removed"
    if outcome == "cancelled":
        return "cancelled"
    if outcome not in _WARNED_UNKNOWN_HISTORY_OUTCOMES:
        logger.warning("unknown history outcome for %s: %s", row.run_id, outcome or "<empty>")
        _WARNED_UNKNOWN_HISTORY_OUTCOMES.add(outcome)
    return "other"


def _elapsed_seconds(record: RunRecord, now: datetime, overlay: StaleOverlay | None = None) -> float | None:
    if overlay is not None and overlay.level == "stale":
        return None
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


def _live_event(record: RunRecord, overlay: StaleOverlay | None) -> str:
    if overlay is not None and overlay.level == "stale":
        return _truncate(overlay.reason, 96)
    return _truncate(_strip_terminal_escapes(record.last_event or "-"), 96)


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
    return writer_identity_matches_live_process(writer)


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


def _format_usage(cost: float | None, token_usage: dict[str, int] | None = None, *, pending: bool = False) -> str:
    if isinstance(cost, (int, float)) and float(cost) > 0:
        return f"${float(cost):.2f}"
    if token_usage:
        input_tokens = int(token_usage.get("input_tokens", 0) or 0)
        output_tokens = int(token_usage.get("output_tokens", 0) or 0)
        if input_tokens or output_tokens:
            return f"{_format_compact_number(input_tokens)} in / {_format_compact_number(output_tokens)} out"
    if isinstance(cost, (int, float)):
        return "$0.00"
    return "…" if pending else "-"


def _format_compact_number(value: int | float) -> str:
    amount = float(value)
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"{amount / 1_000:.1f}K"
    return str(int(amount))


def _record_token_usage(record: RunRecord) -> dict[str, int]:
    usage = _token_usage_from_mapping(record.metrics)
    if usage:
        return usage
    return _token_usage_from_summary_paths(
        _summary_path_candidates(
            record.artifacts.get("summary_path"),
            record.artifacts.get("extra_log_paths"),
            record.last_event,
        ),
        base_dir=Path(record.project_dir),
    )


def _history_token_usage(row: HistoryRow) -> dict[str, int]:
    usage = _token_usage_from_mapping(row.raw)
    if usage:
        return usage
    return _token_usage_from_summary_paths(
        _summary_path_candidates(row.summary_path, row.extra_log_paths, row.raw.get("last_event")),
        base_dir=None,
    )


def _token_usage_from_mapping(mapping: Any) -> dict[str, int]:
    if not isinstance(mapping, dict):
        return {}
    raw_usage = mapping.get("token_usage")
    if isinstance(raw_usage, dict):
        mapping = {**mapping, **raw_usage}
    totals = {
        "input_tokens": _coerce_int(mapping.get("input_tokens")),
        "cached_input_tokens": _coerce_int(mapping.get("cached_input_tokens")),
        "output_tokens": _coerce_int(mapping.get("output_tokens")),
    }
    if any(totals.values()):
        return totals
    breakdown = mapping.get("breakdown")
    if not isinstance(breakdown, dict):
        return {}
    for phase in breakdown.values():
        if not isinstance(phase, dict):
            continue
        for key in totals:
            totals[key] += _coerce_int(phase.get(key))
    return totals if any(totals.values()) else {}


def _token_usage_from_summary_paths(paths: list[Path], *, base_dir: Path | None) -> dict[str, int]:
    for path in paths:
        candidate = path
        if not candidate.is_absolute() and base_dir is not None:
            candidate = base_dir / candidate
        try:
            summary = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        usage = _token_usage_from_mapping(summary)
        if usage:
            return usage
    return {}


def _summary_path_candidates(*values: Any) -> list[Path]:
    candidates: list[Path] = []
    for value in values:
        if isinstance(value, (list, tuple)):
            for item in value:
                _append_summary_path_candidate(candidates, item)
        else:
            _append_summary_path_candidate(candidates, value)
    return candidates


def _append_summary_path_candidate(candidates: list[Path], value: Any) -> None:
    text = _string_or_none(value)
    if not text:
        return
    if text.endswith("summary.json"):
        candidates.append(Path(text).expanduser())
        return
    match = re.search(r"(\S*otto_logs/sessions/\S+?)/certify/proof-of-work\.html", text)
    if match:
        candidates.append(Path(match.group(1)).expanduser() / "summary.json")


def _coerce_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


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
