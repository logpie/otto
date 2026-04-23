"""Queue adapter for Mission Control."""

from __future__ import annotations

import json
from pathlib import Path

from otto.manifest import queue_index_path_for
from otto.queue.schema import load_queue
from otto.tui.mission_control_actions import calculate_legal_actions
from otto.tui.mission_control_model import ArtifactRef, DetailModel, HistoryRow


class QueueMissionControlAdapter:
    def row_label(self, record) -> str:
        task_id = str(record.identity.get("queue_task_id") or record.run_id)
        summary = str(record.intent.get("summary") or "").strip()
        return f"{task_id}: {summary}".strip(": ")

    def history_summary(self, history_row: HistoryRow) -> str:
        return history_row.intent or history_row.queue_task_id or history_row.branch or history_row.run_id

    def artifacts(self, record) -> list[ArtifactRef]:
        items: list[ArtifactRef] = []
        worktree = _queue_worktree(record)
        intent_path = str(record.intent.get("intent_path") or "").strip()
        spec_path = str(record.intent.get("spec_path") or "").strip()
        manifest_path = str(record.artifacts.get("manifest_path") or "").strip()
        checkpoint_path = str(record.artifacts.get("checkpoint_path") or "").strip()
        summary_path = str(record.artifacts.get("summary_path") or "").strip()
        primary_log = str(record.artifacts.get("primary_log_path") or "").strip()

        if intent_path:
            items.append(_artifact("intent", intent_path))
        if spec_path:
            items.append(_artifact("spec", spec_path))
        queue_task_id = str(record.identity.get("queue_task_id") or "").strip()
        if queue_task_id:
            queue_manifest = queue_index_path_for(Path(record.project_dir), queue_task_id)
            if queue_manifest is not None:
                items.append(_artifact("queue manifest", str(queue_manifest.resolve(strict=False))))
        if manifest_path:
            items.append(_artifact("manifest", manifest_path))
        if summary_path:
            items.append(_artifact("summary", summary_path))
        if checkpoint_path:
            items.append(_artifact("checkpoint", checkpoint_path))
        if primary_log:
            items.append(_artifact("primary log", primary_log, kind="log"))
        if worktree:
            items.append(_artifact("worktree", worktree))
        return items

    def legal_actions(self, record, overlay):
        return calculate_legal_actions(record, overlay)

    def detail_panel_renderer(self, record) -> DetailModel:
        task_id = str(record.identity.get("queue_task_id") or record.run_id)
        summary = str(record.intent.get("summary") or "").strip() or task_id
        lines = [
            f"task: {task_id}",
            f"branch: {record.git.get('branch') or '-'}",
            f"worktree: {_queue_worktree(record) or '-'}",
            f"child run: {record.identity.get('child_run_id') or record.identity.get('expected_child_run_id') or '-'}",
        ]
        warning = str(record.identity.get("compatibility_warning") or "").strip()
        if warning:
            lines.append(f"compat: {warning}")
        manifest_path = str(record.artifacts.get("manifest_path") or "").strip()
        if manifest_path:
            try:
                manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            except Exception:
                manifest = None
            if isinstance(manifest, dict):
                lines.append(f"manifest run_id: {manifest.get('run_id') or '-'}")
        return DetailModel(title=f"queue: {task_id}", summary_lines=lines)


def _queue_worktree(record) -> str | None:
    worktree = str(record.git.get("worktree") or "").strip()
    if worktree:
        return worktree
    try:
        for task in load_queue(Path(record.project_dir)):
            task_id = str(record.identity.get("queue_task_id") or "")
            if task.id == task_id and task.worktree:
                return str((Path(record.project_dir) / task.worktree).resolve(strict=False))
    except Exception:
        return None
    return None


def _artifact(label: str, path: str, *, kind: str = "file") -> ArtifactRef:
    candidate = Path(path)
    return ArtifactRef(label=label, path=path, kind=kind, exists=candidate.exists())
