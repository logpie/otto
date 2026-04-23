from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from otto import paths
from otto.runs.registry import make_run_record, update_record, write_record
from otto.tui.mission_control_model import MissionControlModel


def _write_running_record(project_dir: Path, *, run_id: str) -> None:
    log_path = paths.build_dir(project_dir, run_id) / "narrative.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(f"{run_id} log\n", encoding="utf-8")
    record = make_run_record(
        project_dir=project_dir,
        run_id=run_id,
        domain="atomic",
        run_type="build",
        command="build",
        display_name=f"build: {run_id}",
        status="running",
        cwd=project_dir,
        artifacts={"primary_log_path": str(log_path)},
        adapter_key="atomic.build",
        last_event=f"{run_id} event",
    )
    write_record(project_dir, record)
    update_record(
        project_dir,
        run_id,
        heartbeat=False,
        updates={
            "timing": {
                "started_at": "2026-04-23T12:00:00Z",
                "updated_at": "2026-04-23T12:00:00Z",
                "heartbeat_at": "2026-04-23T12:00:00Z",
                "heartbeat_interval_s": 2.0,
                "heartbeat_seq": 1,
            },
            "status": "running",
        },
    )


def test_mission_control_refresh_uses_live_registry_mtime_cache(tmp_path: Path) -> None:
    for index in range(20):
        _write_running_record(tmp_path, run_id=f"build-{index:02d}")

    model = MissionControlModel(
        tmp_path,
        now_fn=lambda: datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc),
        process_probe=lambda writer: True,
    )

    state = model.initial_state()
    cold_stats = model.live_registry_cache_stats()
    assert len(state.live_runs.items) == 20
    assert cold_stats.record_misses == 20
    assert cold_stats.file_stats == 20
    assert cold_stats.snapshot_hits == 0

    model.refresh(state)
    warm_stats = model.live_registry_cache_stats()
    assert warm_stats.snapshot_hits >= 1
    assert warm_stats.file_stats == cold_stats.file_stats
    assert warm_stats.record_misses == cold_stats.record_misses


def test_mission_control_refresh_hot_path_stays_under_150ms_for_20_runs(tmp_path: Path) -> None:
    for index in range(20):
        _write_running_record(tmp_path, run_id=f"build-{index:02d}")

    model = MissionControlModel(
        tmp_path,
        now_fn=lambda: datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc),
        process_probe=lambda writer: True,
    )
    state = model.initial_state()

    start = time.perf_counter()
    for _ in range(10):
        state = model.refresh(state)
    elapsed_ms = (time.perf_counter() - start) * 1000 / 10

    assert len(state.live_runs.items) == 20
    assert elapsed_ms < 150
