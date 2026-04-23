"""Merge adapter for Mission Control."""

from __future__ import annotations

from pathlib import Path

from otto.merge.state import load_state
from otto.runs.schema import is_terminal_status
from otto.tui.mission_control_actions import make_action
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
        argv = record.source.get("argv")
        argv_preview = " ".join(str(part) for part in (argv or []))
        primary_log = str(record.artifacts.get("primary_log_path") or "").strip()
        state_path = str(record.artifacts.get("session_dir") or "").strip()
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
                "r",
                "resume",
                enabled=False,
                reason="merge --resume is deferred",
                preview="would shell `otto merge --resume` once supported",
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
                enabled=bool(state_path),
                reason=None if state_path else "no selectable artifact",
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


def _artifact(label: str, path: str, *, kind: str = "file") -> ArtifactRef:
    candidate = Path(path)
    return ArtifactRef(label=label, path=path, kind=kind, exists=candidate.exists())
