"""Atomic run adapter for Mission Control."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from otto.queue.runtime import INTERRUPTED_STATUS
from otto.runs.schema import RunRecord
from otto.runs.schema import is_terminal_status
from otto.tui.mission_control_actions import ActionResult, execute_action, make_action
from otto.tui.mission_control_model import ArtifactRef, DetailModel, HistoryRow


class AtomicMissionControlAdapter:
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
            items.append(_artifact("intent", intent_path))
        if spec_path:
            items.append(_artifact("spec", spec_path))
        if manifest_path:
            items.append(_artifact("manifest", manifest_path))
        if summary_path:
            items.append(_artifact("summary", summary_path))
        if checkpoint_path:
            items.append(_artifact("checkpoint", checkpoint_path))
        if primary_log:
            items.append(_artifact("primary log", primary_log, kind="log"))
        for index, path in enumerate(extra_log_paths, start=1):
            kind = "log" if path.endswith(".log") else "file"
            items.append(_artifact(f"extra {index}", path, kind=kind))
        return items

    def legal_actions(self, record, overlay):
        checkpoint_path = str(record.artifacts.get("checkpoint_path") or "").strip()
        primary_log = str(record.artifacts.get("primary_log_path") or "").strip()
        has_artifact = any(
            str(record.artifacts.get(key) or "").strip()
            for key in ("manifest_path", "summary_path", "checkpoint_path")
        )
        argv = record.source.get("argv")
        argv_preview = " ".join(str(part) for part in (argv or []))
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
                enabled=is_terminal_status(record.status),
                reason=None if is_terminal_status(record.status) else "run is still active",
                preview=f"would clean terminal artifacts for {record.run_id}",
            ),
            make_action(
                "o",
                "open logs",
                enabled=bool(primary_log),
                reason=None if primary_log else "no log path available",
                preview="would cycle available log views" if primary_log else "no logs to cycle",
            ),
            make_action(
                "e",
                "open file",
                enabled=has_artifact,
                reason=None if has_artifact else "no selectable artifact",
                preview="would shell `$EDITOR <selected artifact>`",
            ),
        ]

    def execute(
        self,
        record: RunRecord,
        action_kind: str,
        project_dir: Path,
        *,
        selected_artifact_path: str | None = None,
        selected_queue_task_ids: list[str] | None = None,
    ) -> ActionResult:
        return execute_action(
            record,
            action_kind,
            project_dir,
            selected_artifact_path=selected_artifact_path,
            selected_queue_task_ids=selected_queue_task_ids,
        )

    def detail_panel_renderer(self, record) -> DetailModel:
        summary = str(record.intent.get("summary") or "").strip() or record.display_name or record.run_id
        lines = [
            f"intent: {summary}",
            f"branch: {record.git.get('branch') or '-'}",
            f"cwd: {record.cwd or '-'}",
            f"resumable: {'yes' if bool(record.source.get('resumable')) else 'no'}",
        ]
        return DetailModel(title=f"{record.run_type}: {summary}", summary_lines=lines)


def _artifact(label: str, path: str, *, kind: str = "file") -> ArtifactRef:
    candidate = Path(path)
    return ArtifactRef(label=label, path=path, kind=kind, exists=candidate.exists())
