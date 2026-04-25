from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from otto import paths
from otto.history import append_history_entry
from otto.queue.schema import QueueTask, append_task, write_state
from otto.runs.registry import finalize_record, make_run_record, update_record, write_record
import otto.mission_control.model as mission_control_model
from otto.mission_control.model import MissionControlFilters, MissionControlModel


class _Clock:
    def __init__(self, when: datetime, monotonic: float = 0.0) -> None:
        self.when = when
        self.monotonic = monotonic

    def now(self) -> datetime:
        return self.when

    def tick(self, *, seconds: float) -> None:
        self.when += timedelta(seconds=seconds)
        self.monotonic += seconds


def _record(
    project_dir: Path,
    *,
    run_id: str,
    run_type: str,
    status: str,
    updated_at: str,
    heartbeat_seq: int = 1,
    queue_task_id: str | None = None,
    heartbeat_interval_s: float = 2.0,
) -> None:
    record = make_run_record(
        project_dir=project_dir,
        run_id=run_id,
        domain="queue" if run_type == "queue" else "merge" if run_type == "merge" else "atomic",
        run_type=run_type,
        command=run_type,
        display_name=f"{run_type}: {run_id}",
        status=status,
        cwd=project_dir,
        identity={"queue_task_id": queue_task_id, "merge_id": run_id if run_type == "merge" else None, "parent_run_id": None},
        artifacts={"primary_log_path": str(paths.session_dir(project_dir, run_id) / "build" / "narrative.log")},
        adapter_key="queue.attempt" if run_type == "queue" else "merge.run" if run_type == "merge" else f"atomic.{run_type}",
        last_event=f"{run_id} event",
    )
    write_record(project_dir, record)
    timing = {
        "started_at": updated_at,
        "updated_at": updated_at,
        "heartbeat_at": updated_at,
        "heartbeat_interval_s": heartbeat_interval_s,
        "heartbeat_seq": heartbeat_seq,
    }
    if status in {"done", "failed", "cancelled", "removed", "interrupted"}:
        timing["finished_at"] = updated_at
    update_record(project_dir, run_id, heartbeat=False, updates={"timing": timing, "status": status})


def test_live_runs_sort_filter_and_retention_rules(tmp_path: Path) -> None:
    now = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)
    clock = _Clock(now)
    _record(tmp_path, run_id="done-old", run_type="build", status="done", updated_at="2026-04-23T11:40:00Z")
    _record(tmp_path, run_id="queued-new", run_type="queue", status="queued", updated_at="2026-04-23T11:59:30Z", queue_task_id="q-new")
    _record(tmp_path, run_id="run-new", run_type="build", status="running", updated_at="2026-04-23T11:59:50Z")
    _record(tmp_path, run_id="interrupted-mid", run_type="merge", status="interrupted", updated_at="2026-04-23T11:59:20Z")
    _record(tmp_path, run_id="done-recent", run_type="build", status="done", updated_at="2026-04-23T11:58:00Z")

    model = MissionControlModel(tmp_path, now_fn=clock.now, monotonic_fn=lambda: clock.monotonic, process_probe=lambda writer: True)
    state = model.initial_state()

    assert [item.record.run_id for item in state.live_runs.items] == [
        "run-new",
        "queued-new",
        "interrupted-mid",
        "done-recent",
    ]

    state.filters.active_only = True
    state = model.refresh(state)
    assert [item.record.run_id for item in state.live_runs.items] == ["run-new", "queued-new"]

    state.filters.active_only = False
    state.filters.type_filter = "queue"
    state = model.refresh(state)
    assert [item.record.run_id for item in state.live_runs.items] == ["queued-new"]


def test_stale_overlay_derivation_uses_grace_window_and_dead_writer(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc))
    _record(tmp_path, run_id="lagger", run_type="build", status="running", updated_at="2026-04-23T12:00:00Z", heartbeat_seq=1)
    model = MissionControlModel(tmp_path, now_fn=clock.now, monotonic_fn=lambda: clock.monotonic, process_probe=lambda writer: True)
    state = model.initial_state()
    assert state.live_runs.items[0].overlay is None

    clock.tick(seconds=3)
    state = model.refresh(state)
    assert state.live_runs.items[0].overlay is None

    clock.tick(seconds=3)
    state = model.refresh(state)
    assert state.live_runs.items[0].overlay is not None
    assert state.live_runs.items[0].overlay.label == "LAGGING"

    update_record(
        tmp_path,
        "lagger",
        heartbeat=False,
        updates={
            "last_event": "still alive",
            "timing": {
                "updated_at": "2026-04-23T12:00:03Z",
                "heartbeat_at": "2026-04-23T12:00:03Z",
                "heartbeat_seq": 2,
            },
        },
    )
    state = model.refresh(state)
    assert state.live_runs.items[0].overlay is None

    clock.tick(seconds=10)
    model._process_probe = lambda writer: False
    state = model.refresh(state)
    assert state.live_runs.items[0].overlay is None

    clock.tick(seconds=6)
    state = model.refresh(state)
    assert state.live_runs.items[0].overlay is not None
    assert state.live_runs.items[0].overlay.label == "STALE"


def test_stale_live_runs_are_not_counted_as_active(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc))
    _record(tmp_path, run_id="abandoned-merge", run_type="merge", status="running", updated_at="2026-04-23T12:00:00Z")
    model = MissionControlModel(tmp_path, now_fn=clock.now, monotonic_fn=lambda: clock.monotonic, process_probe=lambda writer: False)

    state = model.initial_state()
    clock.tick(seconds=16)
    state = model.refresh(state)

    assert state.live_runs.items[0].overlay is not None
    assert state.live_runs.items[0].overlay.label == "STALE"
    assert state.live_runs.items[0].elapsed_display == "-"
    assert state.live_runs.items[0].cost_display == "-"
    assert state.live_runs.items[0].event == "heartbeat stalled and writer identity is gone"
    assert state.live_runs.active_count == 0
    assert state.live_runs.refresh_interval_s == 1.5

    state.filters.active_only = True
    state = model.refresh(state)
    assert state.live_runs.items == []


def test_history_pagination_and_dedup(tmp_path: Path) -> None:
    for index in range(55):
        append_history_entry(
            tmp_path,
            {
                "run_id": f"run-{index}",
                "command": "build",
                "intent": f"intent {index}",
                "passed": True,
                "status": "done",
                "terminal_outcome": "success",
                "timestamp": f"2026-04-23T12:{index:02d}:00Z",
            },
        )
    append_history_entry(
        tmp_path,
        {
            "run_id": "run-10",
            "command": "build",
            "intent": "replacement",
            "passed": False,
            "status": "failed",
            "terminal_outcome": "failure",
            "timestamp": "2026-04-23T13:00:00Z",
        },
    )

    model = MissionControlModel(tmp_path)
    state = model.initial_state()

    assert state.history_page.total_rows == 55
    assert state.history_page.total_pages == 2
    assert state.history_page.items[0].row.run_id == "run-10"
    assert state.history_page.items[0].summary == "replacement"

    state.filters.history_page = 1
    state = model.refresh(state)
    assert len(state.history_page.items) == 5


def test_history_merges_v1_v2_and_archived_sources_before_pagination(tmp_path: Path) -> None:
    append_history_entry(
        tmp_path,
        {
            "run_id": "shared-run",
            "command": "build",
            "intent": "new snapshot wins",
            "passed": True,
            "status": "done",
            "terminal_outcome": "success",
            "timestamp": "2026-04-23T12:03:00Z",
        },
    )
    append_history_entry(
        tmp_path,
        {
            "run_id": "new-run",
            "command": "improve bugs",
            "intent": "new timeline row",
            "passed": True,
            "status": "done",
            "terminal_outcome": "success",
            "timestamp": "2026-04-23T12:04:00Z",
        },
    )

    legacy_path = tmp_path / "otto_logs" / "run-history.jsonl"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        "\n".join(
            [
                '{"build_id":"shared-run","timestamp":"2026-04-23T12:01:00Z","intent":"legacy duplicate"}',
                '{"build_id":"legacy-run","timestamp":"2026-04-23T12:02:00Z","intent":"legacy row"}',
            ]
        )
        + "\n"
    )

    archive_dir = tmp_path / "otto_logs.pre-restructure.2026-04-22T230000Z"
    archive_dir.mkdir()
    (archive_dir / paths.LEGACY_RUN_HISTORY).write_text(
        '{"build_id":"archived-run","timestamp":"2026-04-23T12:00:00Z","intent":"archived row"}\n'
    )

    model = MissionControlModel(tmp_path)
    state = model.initial_state()

    assert state.history_page.total_rows == 4
    assert [item.row.run_id for item in state.history_page.items] == [
        "new-run",
        "shared-run",
        "legacy-run",
        "archived-run",
    ]
    assert state.history_page.items[1].summary == "new snapshot wins"


def test_queue_compat_synthesizes_legacy_queue_rows_and_disables_registry_actions(tmp_path: Path) -> None:
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
    write_state(
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

    model = MissionControlModel(
        tmp_path,
        queue_compat=True,
        now_fn=lambda: datetime(2026, 4, 23, 12, 1, tzinfo=timezone.utc),
    )
    state = model.initial_state(filters=MissionControlFilters(type_filter="queue"))
    detail = model.detail_view(state)

    assert [item.display_id for item in state.live_runs.items] == ["legacy-task"]
    assert detail is not None
    assert detail.record.identity["compatibility_warning"] == "legacy queue mode"
    actions = {action.key: action for action in detail.legal_actions}
    assert actions["c"].enabled is True
    assert actions["o"].enabled is False
    assert actions["o"].reason == "legacy queue mode has no registry-backed log view"
    assert actions["e"].enabled is False
    assert actions["e"].reason == "legacy queue mode has no registry-backed artifacts"


def test_queue_compat_uses_model_snapshot_for_legacy_dedupe(tmp_path: Path, monkeypatch) -> None:
    append_task(
        tmp_path,
        QueueTask(
            id="live-task",
            command_argv=["build", "live task"],
            added_at="2026-04-23T12:00:00Z",
            resolved_intent="live queue task",
            branch="build/live-task",
            worktree=".worktrees/live-task",
        ),
    )
    write_state(
        tmp_path,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "live-task": {
                    "status": "queued",
                    "started_at": "2026-04-23T12:00:00Z",
                }
            },
        },
    )

    live_record = make_run_record(
        project_dir=tmp_path,
        run_id="run-live-task",
        domain="queue",
        run_type="queue",
        command="build live",
        display_name="live-task",
        status="queued",
        cwd=tmp_path,
        identity={"queue_task_id": "live-task", "merge_id": None, "parent_run_id": None},
        intent={"summary": "live queue task"},
        adapter_key="queue.attempt",
    )
    monkeypatch.setattr(
        "otto.mission_control.model.read_live_records",
        lambda project_dir: [live_record] if project_dir == tmp_path else [],
    )

    model = MissionControlModel(
        tmp_path,
        queue_compat=True,
        now_fn=lambda: datetime(2026, 4, 23, 12, 1, tzinfo=timezone.utc),
    )
    state = model.initial_state(filters=MissionControlFilters(type_filter="queue"))

    assert [item.record.run_id for item in state.live_runs.items] == ["run-live-task"]
    assert all(item.record.identity.get("compatibility_warning") != "legacy queue mode" for item in state.live_runs.items)


def test_detail_view_uses_adapter_artifact_ordering(tmp_path: Path) -> None:
    run_id = "artifact-run"
    session_dir = paths.session_dir(tmp_path, run_id)
    paths.build_dir(tmp_path, run_id).mkdir(parents=True, exist_ok=True)
    paths.session_intent(tmp_path, run_id).write_text("intent")
    (session_dir / "spec.md").write_text("spec")
    (session_dir / "manifest.json").write_text("{}")
    paths.session_summary(tmp_path, run_id).write_text("{}")
    paths.session_checkpoint(tmp_path, run_id).write_text("{}")
    (paths.build_dir(tmp_path, run_id) / "narrative.log").write_text("log")
    record = make_run_record(
        project_dir=tmp_path,
        run_id=run_id,
        domain="atomic",
        run_type="build",
        command="build",
        display_name="build",
        status="running",
        cwd=tmp_path,
        intent={"summary": "artifact test", "intent_path": str(paths.session_intent(tmp_path, run_id)), "spec_path": str(session_dir / "spec.md")},
        artifacts={
            "session_dir": str(session_dir),
            "manifest_path": str(session_dir / "manifest.json"),
            "checkpoint_path": str(paths.session_checkpoint(tmp_path, run_id)),
            "summary_path": str(paths.session_summary(tmp_path, run_id)),
            "primary_log_path": str(paths.build_dir(tmp_path, run_id) / "narrative.log"),
            "extra_log_paths": [],
        },
        adapter_key="atomic.build",
    )
    write_record(tmp_path, record)

    model = MissionControlModel(tmp_path, process_probe=lambda writer: True)
    state = model.initial_state()
    detail = model.detail_view(state)

    assert detail is not None
    assert [artifact.label for artifact in detail.artifacts] == [
        "intent",
        "spec",
        "manifest",
        "summary",
        "checkpoint",
        "primary log",
    ]


def test_selection_preservation_across_live_to_history_transition(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc))
    _record(tmp_path, run_id="transient", run_type="build", status="running", updated_at="2026-04-23T12:00:00Z")
    model = MissionControlModel(tmp_path, now_fn=clock.now, monotonic_fn=lambda: clock.monotonic, process_probe=lambda writer: True)
    state = model.initial_state()
    assert state.selection.run_id == "transient"
    assert state.selection.origin_pane == "live"

    finalize_record(tmp_path, "transient", status="done", terminal_outcome="success")
    append_history_entry(
        tmp_path,
        {
            "run_id": "transient",
            "command": "build",
            "intent": "transient",
            "passed": True,
            "status": "done",
            "terminal_outcome": "success",
            "timestamp": "2026-04-23T12:00:05Z",
        },
    )

    state = model.refresh(state)
    assert state.selection.run_id == "transient"

    update_record(
        tmp_path,
        "transient",
        heartbeat=False,
        updates={
            "timing": {
                "updated_at": "2026-04-23T11:40:00Z",
                "finished_at": "2026-04-23T11:40:00Z",
                "heartbeat_at": "2026-04-23T11:40:00Z",
            }
        },
    )
    clock.tick(seconds=1)
    state = model.refresh(state)

    assert state.selection.run_id == "transient"
    assert state.selection.origin_pane == "history"
    assert all(item.record.run_id != "transient" for item in state.live_runs.items)


def test_history_rows_default_missing_resumable_to_false(tmp_path: Path) -> None:
    history_path = paths.history_jsonl(tmp_path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "history_kind": "terminal_snapshot",
                "dedupe_key": "terminal_snapshot:legacy-run",
                "run_id": "legacy-run",
                "command": "build",
                "status": "done",
                "terminal_outcome": "success",
                "timestamp": "2026-04-23T12:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    model = MissionControlModel(tmp_path)
    state = model.initial_state()

    assert state.history_page.items[0].row.resumable is False


def test_history_outcome_removed_filters_correctly(tmp_path: Path) -> None:
    append_history_entry(
        tmp_path,
        {
            "run_id": "removed-run",
            "command": "build",
            "intent": "removed task",
            "status": "removed",
            "terminal_outcome": "removed",
            "passed": False,
            "timestamp": "2026-04-23T12:00:00Z",
        },
    )
    append_history_entry(
        tmp_path,
        {
            "run_id": "cancelled-run",
            "command": "build",
            "intent": "cancelled task",
            "status": "cancelled",
            "terminal_outcome": "cancelled",
            "passed": False,
            "timestamp": "2026-04-23T12:01:00Z",
        },
    )

    model = MissionControlModel(tmp_path)
    state = model.initial_state(filters=MissionControlFilters(outcome_filter="removed"))

    assert [item.row.run_id for item in state.history_page.items] == ["removed-run"]
    assert model.cycle_outcome_filter(state).filters.outcome_filter == "other"
    assert model.cycle_outcome_filter(state).filters.outcome_filter == "all"


def test_history_unknown_outcome_buckets_to_other_and_warns_once(caplog) -> None:
    mission_control_model._WARNED_UNKNOWN_HISTORY_OUTCOMES.clear()
    row = mission_control_model.HistoryRow(
        run_id="mystery-run",
        domain="atomic",
        run_type="build",
        command="build",
        status="mystery",
        terminal_outcome="mystery",
        timestamp="2026-04-23T12:00:00Z",
        started_at=None,
        finished_at=None,
        queue_task_id=None,
        merge_id=None,
        intent="mystery",
        branch=None,
        target_branch=None,
        head_sha=None,
        worktree=None,
        cost_usd=None,
        duration_s=None,
        resumable=False,
        session_dir=None,
        intent_path=None,
        spec_path=None,
        manifest_path=None,
        summary_path=None,
        checkpoint_path=None,
        primary_log_path=None,
        extra_log_paths=[],
        dedupe_key="terminal_snapshot:mystery-run",
        history_kind="terminal_snapshot",
        adapter_key="atomic.build",
    )

    with caplog.at_level("WARNING"):
        assert mission_control_model._history_outcome(row) == "other"
        assert mission_control_model._history_outcome(row) == "other"

    assert [record.message for record in caplog.records if "unknown history outcome" in record.message] == [
        "unknown history outcome for mystery-run: mystery"
    ]
