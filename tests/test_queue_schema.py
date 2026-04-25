"""Tests for otto/queue/schema.py — Phase 2.1 file formats + atomic I/O."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
import yaml

from otto.queue.schema import (
    COMMANDS_FILE,
    QUEUE_FILE,
    QUEUE_SCHEMA_VERSION,
    STATE_FILE,
    STATE_SCHEMA_VERSION,
    QueueTask,
    append_command,
    append_command_ack,
    append_task,
    begin_command_drain,
    finish_command_drain,
    load_queue,
    load_state,
    write_state,
)


# ---------- queue.yml ----------


def test_load_queue_returns_empty_when_file_missing(tmp_path: Path):
    assert load_queue(tmp_path) == []


def test_append_then_load_round_trips(tmp_path: Path):
    t = QueueTask(
        id="add-csv-export",
        command_argv=["build", "add CSV export"],
        resolved_intent="add CSV export",
        added_at="2026-04-19T14:32:01Z",
    )
    append_task(tmp_path, t)
    loaded = load_queue(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].id == "add-csv-export"
    assert loaded[0].command_argv == ["build", "add CSV export"]
    assert loaded[0].resolved_intent == "add CSV export"
    assert loaded[0].resumable is True


def test_append_preserves_existing_tasks(tmp_path: Path):
    append_task(tmp_path, QueueTask(id="t1", command_argv=["build", "x"]))
    append_task(tmp_path, QueueTask(id="t2", command_argv=["improve", "bugs"]))
    loaded = load_queue(tmp_path)
    assert [t.id for t in loaded] == ["t1", "t2"]


def test_append_rejects_duplicate_id(tmp_path: Path):
    append_task(tmp_path, QueueTask(id="t1", command_argv=["build", "x"]))
    with pytest.raises(ValueError, match="already exists"):
        append_task(tmp_path, QueueTask(id="t1", command_argv=["build", "y"]))


def test_load_rejects_wrong_schema_version(tmp_path: Path):
    p = tmp_path / QUEUE_FILE
    p.write_text(yaml.dump({"schema_version": 99, "tasks": []}))
    with pytest.raises(ValueError, match="schema_version"):
        load_queue(tmp_path)


def test_load_rejects_non_mapping_root(tmp_path: Path):
    p = tmp_path / QUEUE_FILE
    p.write_text("- not\n- a\n- mapping\n")
    with pytest.raises(ValueError, match="expected mapping"):
        load_queue(tmp_path)


def test_load_rejects_malformed_yaml_with_value_error(tmp_path: Path):
    p = tmp_path / QUEUE_FILE
    p.write_text("schema_version: [\n")
    with pytest.raises(ValueError, match="queue.yml is malformed"):
        load_queue(tmp_path)


def test_load_rejects_malformed_task_entry(tmp_path: Path):
    p = tmp_path / QUEUE_FILE
    p.write_text(yaml.dump({
        "schema_version": QUEUE_SCHEMA_VERSION,
        "tasks": ["not-a-dict"],
    }))
    with pytest.raises(ValueError, match="must be a mapping"):
        load_queue(tmp_path)


def test_load_rejects_task_missing_id(tmp_path: Path):
    p = tmp_path / QUEUE_FILE
    p.write_text(yaml.dump({
        "schema_version": QUEUE_SCHEMA_VERSION,
        "tasks": [{"command_argv": ["build", "x"]}],
    }))
    with pytest.raises(ValueError, match="missing required field 'id'"):
        load_queue(tmp_path)


def test_load_rejects_task_missing_command_argv(tmp_path: Path):
    p = tmp_path / QUEUE_FILE
    p.write_text(yaml.dump({
        "schema_version": QUEUE_SCHEMA_VERSION,
        "tasks": [{"id": "t1"}],
    }))
    with pytest.raises(ValueError, match="missing required field 'command_argv'"):
        load_queue(tmp_path)


def test_concurrent_appends_dont_lose_tasks(tmp_path: Path):
    """Stress test: 10 threads each appending a task should produce 10 entries."""
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            append_task(tmp_path, QueueTask(id=f"task-{i}", command_argv=["build", str(i)]))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"errors during concurrent append: {errors!r}"
    loaded = load_queue(tmp_path)
    assert len(loaded) == 10
    assert {t.id for t in loaded} == {f"task-{i}" for i in range(10)}


def test_append_task_writes_atomically(tmp_path: Path):
    """No leftover .tmp files after append."""
    append_task(tmp_path, QueueTask(id="t1", command_argv=["build", "x"]))
    leftover = list(tmp_path.glob(".otto-queue.yml*.tmp"))
    assert leftover == []


# ---------- state.json ----------


def test_load_state_returns_empty_when_missing(tmp_path: Path):
    s = load_state(tmp_path)
    assert s["schema_version"] == STATE_SCHEMA_VERSION
    assert s["watcher"] is None
    assert s["tasks"] == {}


def test_write_then_load_state_round_trips(tmp_path: Path):
    s = {
        "schema_version": STATE_SCHEMA_VERSION,
        "watcher": {"pid": 12345, "pgid": 12345, "started_at": "2026-04-19T00:00:00Z", "heartbeat": "..."},
        "tasks": {
            "t1": {"status": "running", "child": {"pid": 999, "pgid": 999}},
        },
    }
    write_state(tmp_path, s)
    loaded = load_state(tmp_path)
    assert loaded["watcher"]["pid"] == 12345
    assert loaded["tasks"]["t1"]["status"] == "running"
    assert loaded["tasks"]["t1"]["child"]["pid"] == 999


def test_write_state_adds_schema_version_if_missing(tmp_path: Path):
    write_state(tmp_path, {"watcher": None, "tasks": {}})
    loaded = load_state(tmp_path)
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION


def test_load_state_rejects_wrong_schema_version(tmp_path: Path):
    p = tmp_path / STATE_FILE
    p.write_text(json.dumps({"schema_version": 99, "tasks": {}}))
    with pytest.raises(ValueError, match="schema_version"):
        load_state(tmp_path)


def test_load_state_rejects_invalid_json(tmp_path: Path):
    p = tmp_path / STATE_FILE
    p.write_text("not json {{")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_state(tmp_path)


# ---------- commands.jsonl ----------


def test_append_then_drain_returns_commands_in_order(tmp_path: Path):
    first = append_command(tmp_path, {"cmd": "cancel", "id": "t1"})
    second = append_command(tmp_path, {"cmd": "remove", "id": "t2"})
    drained = begin_command_drain(tmp_path)
    assert drained == [first, second]
    assert drained[0]["schema_version"] == 1
    assert drained[0]["command_id"].startswith("queue-cmd-")
    finish_command_drain(tmp_path)


def test_append_command_preserves_existing_command_id_and_drain_skips_acked(tmp_path: Path):
    row = append_command(tmp_path, {"cmd": "cancel", "id": "t1", "command_id": "cmd-known"})
    append_command_ack(tmp_path, row, writer_id="watcher", outcome="applied")

    assert begin_command_drain(tmp_path) == []
    finish_command_drain(tmp_path)


def test_drain_clears_file(tmp_path: Path):
    append_command(tmp_path, {"cmd": "cancel", "id": "t1"})
    begin_command_drain(tmp_path)
    finish_command_drain(tmp_path)
    # Subsequent drain returns nothing
    assert begin_command_drain(tmp_path) == []


def test_drain_skips_malformed_lines(tmp_path: Path):
    p = tmp_path / COMMANDS_FILE
    p.write_text("not json\n" + json.dumps({"cmd": "cancel", "id": "t1"}) + "\n" + "{broken\n")
    drained = begin_command_drain(tmp_path)
    assert len(drained) == 1
    assert drained[0]["cmd"] == "cancel"
    assert drained[0]["id"] == "t1"
    finish_command_drain(tmp_path)


def test_drain_returns_empty_when_file_missing(tmp_path: Path):
    assert begin_command_drain(tmp_path) == []


def test_drain_handles_leftover_processing_file(tmp_path: Path):
    """Simulate watcher crash mid-drain: .processing file exists."""
    proc_path = tmp_path / ".otto-queue-commands.jsonl.processing"
    proc_path.write_text(json.dumps({"cmd": "cancel", "id": "old"}) + "\n")
    # New commands arrive after crash
    append_command(tmp_path, {"cmd": "cancel", "id": "new"})
    drained = begin_command_drain(tmp_path)
    # Both old and new commands recovered
    ids = [d["id"] for d in drained]
    assert "old" in ids
    assert "new" in ids
    finish_command_drain(tmp_path)


def test_concurrent_appends_to_commands_dont_lose_lines(tmp_path: Path):
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            append_command(tmp_path, {"cmd": "cancel", "id": f"t-{i}"})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    drained = begin_command_drain(tmp_path)
    ids = sorted(d["id"] for d in drained)
    assert ids == sorted(f"t-{i}" for i in range(20))
    finish_command_drain(tmp_path)
