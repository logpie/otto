from __future__ import annotations

import json
from pathlib import Path

from otto import paths
from otto.merge.state import MergeState, write_state
from otto.runs.registry import make_run_record
from otto.tui.mission_control_model import StaleOverlay
from otto.tui.adapters import adapter_for_key


def test_atomic_adapter_orders_artifacts_and_formats_summary(tmp_path: Path) -> None:
    run_id = "run-atomic"
    session_dir = paths.session_dir(tmp_path, run_id)
    paths.build_dir(tmp_path, run_id).mkdir(parents=True, exist_ok=True)
    paths.session_intent(tmp_path, run_id).write_text("intent")
    paths.session_summary(tmp_path, run_id).write_text("{}")
    paths.session_checkpoint(tmp_path, run_id).write_text("{}")
    (session_dir / "manifest.json").write_text("{}")
    (session_dir / "spec.md").write_text("# spec")
    (paths.build_dir(tmp_path, run_id) / "narrative.log").write_text("hello")
    (session_dir / "agent.log").write_text("secondary")

    record = make_run_record(
        project_dir=tmp_path,
        run_id=run_id,
        domain="atomic",
        run_type="build",
        command="build",
        display_name="build: export csv",
        status="running",
        cwd=tmp_path,
        intent={"summary": "export csv", "intent_path": str(paths.session_intent(tmp_path, run_id)), "spec_path": str(session_dir / "spec.md")},
        artifacts={
            "session_dir": str(session_dir),
            "manifest_path": str(session_dir / "manifest.json"),
            "checkpoint_path": str(paths.session_checkpoint(tmp_path, run_id)),
            "summary_path": str(paths.session_summary(tmp_path, run_id)),
            "primary_log_path": str(paths.build_dir(tmp_path, run_id) / "narrative.log"),
            "extra_log_paths": [str(session_dir / "agent.log")],
        },
        adapter_key="atomic.build",
    )

    adapter = adapter_for_key("atomic.build")
    labels = [artifact.label for artifact in adapter.artifacts(record)]
    actions = {action.key: action for action in adapter.legal_actions(record, None)}

    assert labels == ["intent", "spec", "manifest", "summary", "checkpoint", "primary log", "extra 1"]
    assert adapter.row_label(record) == "export csv"
    assert actions["r"].enabled is False
    assert actions["r"].reason == "run is not interrupted"
    assert actions["o"].enabled is True


def test_queue_adapter_includes_queue_manifest_and_merge_action_preview(tmp_path: Path) -> None:
    task_id = "queue-task"
    queue_manifest = tmp_path / "otto_logs" / "queue" / task_id / "manifest.json"
    queue_manifest.parent.mkdir(parents=True, exist_ok=True)
    queue_manifest.write_text(json.dumps({"run_id": "queue-run"}))

    record = make_run_record(
        project_dir=tmp_path,
        run_id="queue-run",
        domain="queue",
        run_type="queue",
        command="build thing",
        display_name="queue task",
        status="done",
        cwd=tmp_path,
        identity={"queue_task_id": task_id, "merge_id": None, "parent_run_id": None},
        intent={"summary": "build queued thing", "intent_path": None, "spec_path": None},
        git={"branch": "build/queued", "worktree": ".worktrees/queue-task"},
        artifacts={"manifest_path": str(queue_manifest), "primary_log_path": None},
        adapter_key="queue.attempt",
    )

    adapter = adapter_for_key("queue.attempt")
    artifacts = adapter.artifacts(record)
    actions = {action.key: action for action in adapter.legal_actions(record, None)}

    assert artifacts[0].label == "queue manifest"
    assert actions["m"].enabled is True
    assert "otto merge queue-task" in actions["m"].preview


def test_queue_adapter_legacy_mode_disables_registry_dependent_actions(tmp_path: Path) -> None:
    record = make_run_record(
        project_dir=tmp_path,
        run_id="queue-compat:legacy-task",
        domain="queue",
        run_type="queue",
        command="build legacy",
        display_name="legacy-task",
        status="queued",
        cwd=tmp_path,
        identity={
            "queue_task_id": "legacy-task",
            "merge_id": None,
            "parent_run_id": None,
            "compatibility_warning": "legacy queue mode",
        },
        source={"argv": ["build", "legacy task"], "resumable": True},
        adapter_key="queue.attempt",
    )

    adapter = adapter_for_key("queue.attempt")
    actions = {action.key: action for action in adapter.legal_actions(record, None)}

    assert actions["c"].enabled is True
    assert actions["o"].enabled is False
    assert actions["o"].reason == "legacy queue mode has no registry-backed log view"
    assert actions["e"].enabled is False
    assert actions["e"].reason == "legacy queue mode has no registry-backed artifacts"


def test_merge_adapter_renders_state_details(tmp_path: Path) -> None:
    merge_id = "merge-123"
    state = MergeState(
        merge_id=merge_id,
        started_at="2026-04-23T00:00:00Z",
        target="main",
        branches_in_order=["feature/a", "feature/b"],
        note="resolving conflicts",
    )
    write_state(tmp_path, state)
    merge_log = paths.merge_dir(tmp_path) / "merge.log"
    merge_log.parent.mkdir(parents=True, exist_ok=True)
    merge_log.write_text("merge started\n")

    record = make_run_record(
        project_dir=tmp_path,
        run_id=merge_id,
        domain="merge",
        run_type="merge",
        command="merge",
        display_name="merge",
        status="running",
        cwd=tmp_path,
        identity={"merge_id": merge_id, "queue_task_id": None, "parent_run_id": None},
        git={"target_branch": "main"},
        artifacts={"session_dir": str(paths.merge_dir(tmp_path) / merge_id), "primary_log_path": str(merge_log)},
        adapter_key="merge.run",
    )

    adapter = adapter_for_key("merge.run")
    detail = adapter.detail_panel_renderer(record)
    actions = {action.key: action for action in adapter.legal_actions(record, StaleOverlay("stale", "STALE", "writer unavailable", False))}

    assert "branches: 2" in detail.summary_lines
    assert "note: resolving conflicts" in detail.summary_lines
    assert actions["c"].enabled is False
    assert actions["c"].reason == "writer unavailable (stale overlay)"
    assert actions["r"].enabled is False
    assert actions["r"].reason == "merge --resume is deferred"
