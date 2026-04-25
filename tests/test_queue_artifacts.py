from __future__ import annotations

import json
from pathlib import Path

from otto import paths
from otto.manifest import queue_index_path_for
from otto.queue.artifacts import preserve_queue_session_artifacts
from otto.runs.history import append_history_snapshot, read_history_rows


def test_preserve_queue_session_artifacts_rewrites_top_level_intent_and_spec_paths(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "repo"
    worktree_path = project_dir / ".worktrees" / "task1"
    run_id = "run-123"
    src_session = paths.session_dir(worktree_path, run_id)
    src_session.mkdir(parents=True)
    intent_path = paths.session_intent(worktree_path, run_id)
    spec_path = paths.spec_dir(worktree_path, run_id) / "spec.md"
    manifest_path = src_session / "manifest.json"
    summary_path = src_session / "summary.json"
    primary_log_path = paths.build_dir(worktree_path, run_id) / "narrative.log"
    for path, text in (
        (intent_path, "intent"),
        (spec_path, "spec"),
        (manifest_path, json.dumps({"run_id": run_id})),
        (summary_path, "{}"),
        (primary_log_path, "log"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    queue_manifest = queue_index_path_for(project_dir, "task1")
    assert queue_manifest is not None
    queue_manifest.parent.mkdir(parents=True)
    queue_manifest.write_text(
        json.dumps({
            "run_id": run_id,
            "checkpoint_path": str(src_session / "checkpoint.json"),
            "proof_of_work_path": str(summary_path),
        }),
        encoding="utf-8",
    )
    append_history_snapshot(
        project_dir,
        {
            "run_id": run_id,
            "dedupe_key": f"terminal_snapshot:{run_id}",
            "domain": "queue",
            "run_type": "queue",
            "command": "build",
            "status": "done",
            "intent_path": str(intent_path),
            "spec_path": str(spec_path),
            "manifest_path": str(manifest_path),
            "summary_path": str(summary_path),
            "primary_log_path": str(primary_log_path),
            "artifacts": {
                "manifest_path": str(manifest_path),
                "summary_path": str(summary_path),
                "primary_log_path": str(primary_log_path),
            },
        },
    )

    result = preserve_queue_session_artifacts(
        project_dir,
        task_id="task1",
        worktree_path=worktree_path,
        strict=True,
    )

    assert result is not None
    row = read_history_rows(paths.history_jsonl(project_dir))[0]
    assert row["intent_path"] == str(paths.session_intent(project_dir, run_id).resolve())
    assert row["spec_path"] == str((paths.spec_dir(project_dir, run_id) / "spec.md").resolve())
    assert row["manifest_path"] == str((paths.session_dir(project_dir, run_id) / "manifest.json").resolve())
    assert row["artifacts"]["manifest_path"] == row["manifest_path"]
