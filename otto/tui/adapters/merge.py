"""Merge adapter for Mission Control."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from otto.merge.state import load_state
from otto.runs.registry import writer_identity_gone_or_stale
from otto.runs.schema import RunRecord
from otto.runs.schema import is_terminal_status
from otto.tui.mission_control_actions import ActionExecutingAdapter, make_action
from otto.tui.mission_control_model import ArtifactRef, DetailModel, HistoryRow


class MergeMissionControlAdapter(ActionExecutingAdapter):
    def legacy_records(self, project_dir: Path, now: datetime, live_records: list[RunRecord]):
        del project_dir, now, live_records
        return []

    def live_overlay(self, record, overlay):
        del record
        return overlay

    def row_label(self, record) -> str:
        return str(record.intent.get("summary") or record.display_name or record.run_id)

    def history_summary(self, history_row: HistoryRow) -> str:
        return history_row.intent or history_row.merge_id or history_row.run_id

    def artifacts(self, record) -> list[ArtifactRef]:
        items: list[ArtifactRef] = []
        primary_log = str(record.artifacts.get("primary_log_path") or "").strip()
        session_dir = str(record.artifacts.get("session_dir") or "").strip()
        state_path = str((Path(session_dir) / "state.json").resolve(strict=False)) if session_dir else ""
        extra_log_paths = [str(path).strip() for path in record.artifacts.get("extra_log_paths") or [] if str(path).strip()]
        if state_path:
            items.append(ArtifactRef.from_path("state", state_path))
        if primary_log:
            items.append(ArtifactRef.from_path("merge log", primary_log, kind="log"))
        extra_index = 1
        for path in extra_log_paths:
            if path == state_path:
                continue
            kind = "log" if path.endswith(".log") else "file"
            items.append(ArtifactRef.from_path(f"extra {extra_index}", path, kind=kind))
            extra_index += 1
        return items

    def legal_actions(self, record, overlay):
        argv = record.source.get("argv")
        argv_preview = " ".join(str(part) for part in (argv or []))
        log_paths = [artifact.path for artifact in self.artifacts(record) if artifact.kind == "log"]
        has_artifact = bool(self.artifacts(record))
        cleanup_enabled = is_terminal_status(record.status) and writer_identity_gone_or_stale(record.writer)
        return [
            make_action(
                "c",
                "cancel",
                enabled=not is_terminal_status(record.status) and not (overlay is not None and overlay.level == "stale"),
                reason=(
                    "run already terminal"
                    if is_terminal_status(record.status)
                    else "writer unavailable (stale overlay)"
                    if overlay is not None and overlay.level == "stale"
                    else None
                ),
                preview=f"would append merge cancel for {record.run_id}",
            ),
            make_action(
                "R",
                "retry",
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
                    else f"would re-run `{argv_preview}` from {record.cwd}"
                ),
            ),
            make_action(
                "x",
                "cleanup",
                enabled=cleanup_enabled,
                reason=(
                    None
                    if cleanup_enabled
                    else "run is still active"
                    if not is_terminal_status(record.status)
                    else "writer still alive — wait for finalization"
                ),
                preview=f"would clean terminal artifacts for {record.run_id}",
            ),
            make_action(
                "o",
                "open logs",
                enabled=bool(log_paths),
                reason=None if log_paths else "no log path available",
                preview="would cycle available log views" if log_paths else "no logs to cycle",
            ),
            make_action(
                "e",
                "open file",
                enabled=has_artifact,
                reason=None if has_artifact else "no selectable artifact",
                preview="would shell `$EDITOR <selected artifact>`",
            ),
        ]

    def detail_panel_renderer(self, record) -> DetailModel:
        merge_id = str(record.identity.get("merge_id") or record.run_id)
        lines = [
            f"merge id: {merge_id}",
            f"target: {record.git.get('target_branch') or '-'}",
            f"cwd: {record.cwd or '-'}",
        ]
        try:
            state = load_state(Path(record.project_dir), merge_id)
        except Exception:
            state = None
        if state is not None:
            lines.append(f"branches: {len(state.branches_in_order)}")
            if state.note:
                lines.append(f"note: {state.note}")
        return DetailModel(title=f"merge: {merge_id}", summary_lines=lines)
