"""Atomic run adapter for Mission Control."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from otto.queue.runtime import INTERRUPTED_STATUS
from otto.runs.registry import writer_identity_gone_or_stale
from otto.runs.schema import RunRecord
from otto.runs.schema import is_terminal_status
from otto.mission_control.actions import ActionExecutingAdapter, make_action
from otto.mission_control.model import ArtifactRef, DetailModel, HistoryRow


class AtomicMissionControlAdapter(ActionExecutingAdapter):
    def legacy_records(self, project_dir: Path, now: datetime, live_records: list[RunRecord]):
        del project_dir, now, live_records
        return []

    def live_overlay(self, record, overlay):
        del record
        return overlay

    def row_label(self, record) -> str:
        summary = str(record.intent.get("summary") or "").strip()
        return summary or record.display_name or record.run_id

    def history_summary(self, history_row: HistoryRow) -> str:
        return history_row.intent or history_row.branch or history_row.run_id

    def artifacts(self, record) -> list[ArtifactRef]:
        items: list[ArtifactRef] = []
        intent_path = str(record.intent.get("intent_path") or "").strip()
        spec_path = str(record.intent.get("spec_path") or "").strip()
        manifest_path = str(record.artifacts.get("manifest_path") or "").strip()
        summary_path = str(record.artifacts.get("summary_path") or "").strip()
        checkpoint_path = str(record.artifacts.get("checkpoint_path") or "").strip()
        primary_log = str(record.artifacts.get("primary_log_path") or "").strip()
        extra_log_paths = [str(path).strip() for path in record.artifacts.get("extra_log_paths") or [] if str(path).strip()]

        if intent_path:
            items.append(ArtifactRef.from_path("intent", intent_path))
        if spec_path:
            items.append(ArtifactRef.from_path("spec", spec_path))
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
        for index, path in enumerate(extra_log_paths, start=1):
            kind = "log" if path.endswith(".log") else "file"
            items.append(ArtifactRef.from_path(f"extra {index}", path, kind=kind))
        return items

    def legal_actions(self, record, overlay):
        checkpoint_path = str(record.artifacts.get("checkpoint_path") or "").strip()
        log_paths = [artifact.path for artifact in self.artifacts(record) if artifact.kind == "log"]
        has_artifact = bool(self.artifacts(record))
        argv = record.source.get("argv")
        argv_preview = " ".join(str(part) for part in (argv or []))
        stale_overlay = overlay is not None and overlay.level == "stale"
        cleanup_enabled = (is_terminal_status(record.status) or stale_overlay) and writer_identity_gone_or_stale(record.writer)
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
                preview=f"would append session cancel for {record.run_id}",
            ),
            make_action(
                "r",
                "resume",
                enabled=(
                    record.run_type != "certify"
                    and record.status in {INTERRUPTED_STATUS, "paused"}
                    and bool(checkpoint_path)
                    and Path(checkpoint_path).exists()
                ),
                reason=(
                    "standalone certify has no resume path"
                    if record.run_type == "certify"
                    else "run is not interrupted"
                    if record.status not in {INTERRUPTED_STATUS, "paused"}
                    else "checkpoint missing"
                    if not checkpoint_path or not Path(checkpoint_path).exists()
                    else None
                ),
                preview=(
                    f"would shell `otto improve --resume` from {record.cwd}"
                    if record.run_type == "improve"
                    else f"would shell `otto {record.run_type} --resume` from {record.cwd}"
                ),
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
                    if not is_terminal_status(record.status) and not stale_overlay
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
        summary = str(record.intent.get("summary") or "").strip() or record.display_name or record.run_id
        lines = [
            f"intent: {summary}",
            f"branch: {record.git.get('branch') or '-'}",
            f"cwd: {record.cwd or '-'}",
            f"resumable: {'yes' if bool(record.source.get('resumable')) else 'no'}",
        ]
        return DetailModel(title=f"{record.run_type}: {summary}", summary_lines=lines)
