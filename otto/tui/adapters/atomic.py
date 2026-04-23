"""Atomic run adapter for Mission Control."""

from __future__ import annotations

from pathlib import Path

from otto.tui.mission_control_actions import calculate_legal_actions
from otto.tui.mission_control_model import ArtifactRef, DetailModel, HistoryRow


class AtomicMissionControlAdapter:
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
        return calculate_legal_actions(record, overlay)

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
