from __future__ import annotations

import json
import multiprocessing as mp
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from otto import paths
from otto.cli import _new_run_id
from otto.queue.schema import append_command, append_command_ack, begin_command_drain, finish_command_drain
from otto.runs.registry import (
    allocate_run_id,
    garbage_collect_live_records,
    make_run_record,
    read_live_records,
    RunPublisher,
    write_record,
)


def _allocate_ids(project_dir: str, queue: mp.Queue, count: int) -> None:
    for _ in range(count):
        queue.put(allocate_run_id(Path(project_dir)))


def test_allocate_run_id_multiprocess_race(tmp_path: Path) -> None:
    queue: mp.Queue[str] = mp.get_context("spawn").Queue()
    procs = [
        mp.get_context("spawn").Process(target=_allocate_ids, args=(str(tmp_path), queue, 10))
        for _ in range(4)
    ]
    for proc in procs:
        proc.start()
    ids = [queue.get(timeout=5) for _ in range(40)]
    for proc in procs:
        proc.join(timeout=5)
        assert proc.exitcode == 0
    assert len(ids) == len(set(ids))


def test_write_and_gc_live_record_appends_tombstone(tmp_path: Path) -> None:
    run_id = allocate_run_id(tmp_path)
    record = make_run_record(
        project_dir=tmp_path,
        run_id=run_id,
        domain="atomic",
        run_type="build",
        command="build",
        display_name="build: test",
        status="done",
        cwd=tmp_path,
    )
    record.timing["finished_at"] = "2026-04-20T00:00:00Z"
    write_record(tmp_path, record)

    removed = garbage_collect_live_records(
        tmp_path,
        terminal_retention_s=1.0,
        now=datetime(2026, 4, 23, tzinfo=timezone.utc),
    )
    assert removed == [run_id]
    tombstones = paths.run_gc_tombstones_jsonl(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(tombstones) == 1
    row = json.loads(tombstones[0])
    assert row["run_id"] == run_id
    assert row["status"] == "done"


def test_read_live_records_skips_malformed_rows(tmp_path: Path) -> None:
    good_id = allocate_run_id(tmp_path)
    write_record(
        tmp_path,
        make_run_record(
            project_dir=tmp_path,
            run_id=good_id,
            domain="atomic",
            run_type="build",
            command="build",
            display_name="build: ok",
            status="running",
            cwd=tmp_path,
        ),
    )
    bad_path = paths.live_run_path(tmp_path, "broken")
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    bad_path.write_text("{not json}\n", encoding="utf-8")
    records = read_live_records(tmp_path)
    assert [record.run_id for record in records] == [good_id]


def test_queue_unacked_command_replays_until_acked(tmp_path: Path) -> None:
    cmd = {
        "schema_version": 1,
        "command_id": "cmd-1",
        "run_id": "run-1",
        "kind": "cancel",
        "requested_at": "2026-04-23T00:00:00Z",
    }
    append_command(tmp_path, cmd)
    first = begin_command_drain(tmp_path)
    assert [item["command_id"] for item in first] == ["cmd-1"]
    second = begin_command_drain(tmp_path)
    assert [item["command_id"] for item in second] == ["cmd-1"]
    append_command_ack(tmp_path, cmd, writer_id="queue:test")
    third = begin_command_drain(tmp_path)
    assert third == []
    finish_command_drain(tmp_path)


def test_new_run_id_prefers_otto_run_id_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OTTO_RUN_ID", "2026-04-23-010101-abc123")
    assert _new_run_id(tmp_path) == "2026-04-23-010101-abc123"


def test_run_publisher_ignores_updates_after_finalize(tmp_path: Path) -> None:
    run_id = allocate_run_id(tmp_path)
    publisher = RunPublisher(
        tmp_path,
        make_run_record(
            project_dir=tmp_path,
            run_id=run_id,
            domain="atomic",
            run_type="build",
            command="build",
            display_name="build: test",
            status="running",
            cwd=tmp_path,
        ),
    )
    publisher.__enter__()
    publisher.finalize(status="done", terminal_outcome="success")
    publisher.update({"status": "running", "last_event": "stale heartbeat"})

    record = json.loads(paths.live_run_path(tmp_path, run_id).read_text())
    assert record["status"] == "done"
    assert record["terminal_outcome"] == "success"
