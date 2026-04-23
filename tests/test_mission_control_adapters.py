from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from otto import paths
from otto.merge.state import MergeState, write_state
from otto.queue.schema import QueueTask, append_task, write_state as write_queue_state
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


def test_queue_adapter_disables_cancel_without_task_id_and_cleanup_while_writer_alive(tmp_path: Path, monkeypatch) -> None:
    record = make_run_record(
        project_dir=tmp_path,
        run_id="queue-run",
        domain="queue",
        run_type="queue",
        command="queue",
        display_name="queue task",
        status="done",
        cwd=tmp_path,
        identity={"queue_task_id": None, "merge_id": None, "parent_run_id": None},
        adapter_key="queue.attempt",
    )
    active = make_run_record(
        project_dir=tmp_path,
        run_id="queue-active",
        domain="queue",
        run_type="queue",
        command="queue",
        display_name="queue active",
        status="running",
        cwd=tmp_path,
        identity={"queue_task_id": None, "merge_id": None, "parent_run_id": None},
        adapter_key="queue.attempt",
    )
    monkeypatch.setattr("otto.tui.adapters.queue.writer_identity_gone_or_stale", lambda writer: False)

    adapter = adapter_for_key("queue.attempt")
    done_actions = {action.key: action for action in adapter.legal_actions(record, None)}
    active_actions = {action.key: action for action in adapter.legal_actions(active, None)}

    assert active_actions["c"].enabled is False
    assert active_actions["c"].reason == "queue task id unknown"
    assert done_actions["x"].enabled is False
    assert done_actions["x"].reason == "writer still alive — wait for finalization"


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


def test_queue_adapter_owns_legacy_record_and_overlay_compat(tmp_path: Path) -> None:
    append_task(
        tmp_path,
        QueueTask(
            id="legacy-task",
            command_argv=["build", "legacy task"],
            added_at="2026-04-23T12:00:00Z",
            resolved_intent="legacy queue task",
            branch="build/legacy-task",
            worktree=".worktrees/legacy-task",
        ),
    )
    write_queue_state(
        tmp_path,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "legacy-task": {
                    "status": "queued",
                    "started_at": "2026-04-23T12:00:00Z",
                }
            },
        },
    )

    adapter = adapter_for_key("queue.attempt")
    records = adapter.legacy_records(
        tmp_path,
        datetime(2026, 4, 23, 12, 1, tzinfo=timezone.utc),
        [],
    )

    assert [record.identity["queue_task_id"] for record in records] == ["legacy-task"]
    assert records[0].identity["compatibility_warning"] == "legacy queue mode"
    assert adapter.live_overlay(records[0], StaleOverlay("stale", "STALE", "writer unavailable", False)) is None


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


def test_atomic_and_merge_cleanup_wait_for_writer_finalization(tmp_path: Path, monkeypatch) -> None:
    atomic = make_run_record(
        project_dir=tmp_path,
        run_id="atomic-run",
        domain="atomic",
        run_type="build",
        command="build",
        display_name="atomic",
        status="failed",
        cwd=tmp_path,
        adapter_key="atomic.build",
    )
    merge = make_run_record(
        project_dir=tmp_path,
        run_id="merge-run",
        domain="merge",
        run_type="merge",
        command="merge",
        display_name="merge",
        status="failed",
        cwd=tmp_path,
        identity={"merge_id": "merge-run", "queue_task_id": None, "parent_run_id": None},
        adapter_key="merge.run",
    )
    monkeypatch.setattr("otto.tui.adapters.atomic.writer_identity_gone_or_stale", lambda writer: False)
    monkeypatch.setattr("otto.tui.adapters.merge.writer_identity_gone_or_stale", lambda writer: False)

    atomic_actions = {action.key: action for action in adapter_for_key("atomic.build").legal_actions(atomic, None)}
    merge_actions = {action.key: action for action in adapter_for_key("merge.run").legal_actions(merge, None)}

    assert atomic_actions["x"].enabled is False
    assert atomic_actions["x"].reason == "writer still alive — wait for finalization"
    assert merge_actions["x"].enabled is False
    assert merge_actions["x"].reason == "writer still alive — wait for finalization"
