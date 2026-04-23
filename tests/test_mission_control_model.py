from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from otto import paths
from otto.history import append_history_entry
from otto.runs.registry import finalize_record, make_run_record, update_record, write_record
from otto.tui.mission_control_model import MissionControlFilters, MissionControlModel


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
    assert state.live_runs.items[0].overlay is not None
    assert state.live_runs.items[0].overlay.label == "LAGGING"

    update_record(tmp_path, "lagger", heartbeat=True, updates={"last_event": "still alive"})
    state = model.refresh(state)
    assert state.live_runs.items[0].overlay is None

    clock.tick(seconds=40)
    model._process_probe = lambda writer: False
    state = model.refresh(state)
    assert state.live_runs.items[0].overlay is not None
    assert state.live_runs.items[0].overlay.label == "LAGGING"

    clock.tick(seconds=16)
    state = model.refresh(state)
    assert state.live_runs.items[0].overlay is not None
    assert state.live_runs.items[0].overlay.label == "STALE"


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
