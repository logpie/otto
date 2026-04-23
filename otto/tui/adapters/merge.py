"""Merge adapter for Mission Control."""

from __future__ import annotations

from pathlib import Path

from otto.merge.state import load_state
from otto.tui.mission_control_actions import calculate_legal_actions
from otto.tui.mission_control_model import ArtifactRef, DetailModel, HistoryRow


class MergeMissionControlAdapter:
    def row_label(self, record) -> str:
        return str(record.intent.get("summary") or record.display_name or record.run_id)

    def history_summary(self, history_row: HistoryRow) -> str:
        return history_row.intent or history_row.merge_id or history_row.run_id

    def artifacts(self, record) -> list[ArtifactRef]:
        items: list[ArtifactRef] = []
        primary_log = str(record.artifacts.get("primary_log_path") or "").strip()
        session_dir = str(record.artifacts.get("session_dir") or "").strip()
        state_path = str((Path(session_dir) / "state.json").resolve(strict=False)) if session_dir else ""
        if state_path:
            items.append(_artifact("state", state_path))
        if primary_log:
            items.append(_artifact("merge log", primary_log, kind="log"))
        return items

    def legal_actions(self, record, overlay):
        return calculate_legal_actions(record, overlay)

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


def _artifact(label: str, path: str, *, kind: str = "file") -> ArtifactRef:
    candidate = Path(path)
    return ArtifactRef(label=label, path=path, kind=kind, exists=candidate.exists())
