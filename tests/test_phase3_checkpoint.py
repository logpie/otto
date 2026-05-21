"""Phase 3 tests (plan-checkpoint-resume-v2.md): event-sourced
checkpoint with materialized snapshot + corruption recovery.

Codex Plan Gate APPROVED at R5; the model is:
  - checkpoint.events.jsonl = durable source of truth
  - checkpoint.json = validated snapshot/cache with checksum + last_event_seq
  - corrupt/stale snapshot → rebuild from events
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from otto.v5_checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointSnapshot,
    ChildCheckpoint,
    checkpoint_events_path,
    checkpoint_snapshot_path,
    find_latest_checkpoint_session,
    get_or_rebuild_snapshot,
    load_snapshot,
    materialize_snapshot,
    persist_snapshot,
    read_events,
    record_and_persist,
    record_event,
    snapshot_to_dict,
)


# --- Event recording -----------------------------------------------


def test_record_event_writes_to_events_jsonl(tmp_path: Path):
    ev = record_event(tmp_path, "compile_done", {"journey_count": 5})
    assert ev.event_seq == 0
    assert ev.kind == "compile_done"
    assert ev.ts
    path = checkpoint_events_path(tmp_path)
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event_seq"] == 0
    assert payload["kind"] == "compile_done"
    assert payload["payload"] == {"journey_count": 5}


def test_record_event_increments_seq(tmp_path: Path):
    for i in range(3):
        ev = record_event(tmp_path, f"kind_{i}", {"i": i})
        assert ev.event_seq == i


def test_read_events_returns_sorted_list(tmp_path: Path):
    record_event(tmp_path, "compile_done")
    record_event(tmp_path, "decompose_done")
    record_event(tmp_path, "integration_done")
    events = read_events(tmp_path)
    assert len(events) == 3
    assert [e.event_seq for e in events] == [0, 1, 2]
    assert [e.kind for e in events] == [
        "compile_done", "decompose_done", "integration_done"
    ]


def test_read_events_skips_malformed_lines(tmp_path: Path):
    """Best-effort durability: a malformed event line shouldn't break
    the whole read."""
    record_event(tmp_path, "compile_done")
    # Corrupt the file with a bad line
    path = checkpoint_events_path(tmp_path)
    with path.open("a", encoding="utf-8") as f:
        f.write("not valid json\n")
    record_event(tmp_path, "decompose_done")
    events = read_events(tmp_path)
    # 2 valid events read; 1 malformed skipped
    assert len(events) == 2
    assert events[0].kind == "compile_done"
    assert events[1].kind == "decompose_done"


# --- Snapshot materialization -------------------------------------


def test_materialize_snapshot_tracks_phase_progression(tmp_path: Path):
    record_event(tmp_path, "compile_started")
    record_event(tmp_path, "compile_done", {"journey_count": 5})
    record_event(tmp_path, "decompose_done",
                 {"emitted_subtask_ids": ["v5-a", "v5-b", "v5-c"]})
    record_event(tmp_path, "child_dispatched", {"task_id": "v5-a"})
    record_event(tmp_path, "child_done",
                 {"task_id": "v5-a", "verdict": "pass", "cost_usd": 5.0})
    snap = materialize_snapshot(read_events(tmp_path))
    assert snap.last_event_seq == 4
    assert snap.compile_done_at
    assert snap.compile_journey_count == 5
    assert snap.decompose_done_at
    assert snap.decompose_emitted == ["v5-a", "v5-b", "v5-c"]
    assert len(snap.children) == 3
    assert snap.children["v5-a"].verdict == "pass"
    assert snap.children["v5-a"].dispatched_at
    assert snap.children["v5-a"].completed_at
    assert snap.cost_usd == 5.0


def test_materialize_snapshot_handles_full_run(tmp_path: Path):
    record_event(tmp_path, "compile_done", {"journey_count": 3})
    record_event(tmp_path, "decompose_done",
                 {"emitted_subtask_ids": ["v5-x", "v5-y"]})
    record_event(tmp_path, "child_done",
                 {"task_id": "v5-x", "verdict": "pass", "cost_usd": 2.5})
    record_event(tmp_path, "child_done",
                 {"task_id": "v5-y", "verdict": "partial", "cost_usd": 3.0})
    record_event(tmp_path, "integration_started")
    record_event(tmp_path, "integration_done",
                 {"verdict": "partial", "cost_usd": 4.0})
    record_event(tmp_path, "run_terminated", {"verdict": "partial"})
    snap = materialize_snapshot(read_events(tmp_path))
    assert snap.integration_started_at
    assert snap.integration_done_at
    assert snap.integration_verdict == "partial"
    assert snap.run_terminated_at
    assert snap.run_verdict == "partial"
    assert snap.cost_usd == 2.5 + 3.0 + 4.0


def test_materialize_handles_unknown_event_kind(tmp_path: Path):
    """Forward-compat: unknown event kinds are stored in events.jsonl
    but don't crash snapshot materialization."""
    record_event(tmp_path, "compile_done", {"journey_count": 1})
    record_event(tmp_path, "future_unknown_kind", {"foo": "bar"})
    snap = materialize_snapshot(read_events(tmp_path))
    # Last seq still tracked even for unknown kinds
    assert snap.last_event_seq == 1
    # Compile state still tracked
    assert snap.compile_journey_count == 1


# --- Snapshot persistence + checksum -----------------------------


def test_persist_and_load_snapshot_roundtrip(tmp_path: Path):
    record_event(tmp_path, "compile_done", {"journey_count": 7})
    record_event(tmp_path, "decompose_done", {"emitted_subtask_ids": ["v5-q"]})
    snap_in = materialize_snapshot(read_events(tmp_path))
    persist_snapshot(tmp_path, snap_in)
    snap_out = load_snapshot(tmp_path)
    assert snap_out is not None
    assert snap_out.compile_journey_count == 7
    assert snap_out.decompose_emitted == ["v5-q"]
    assert snap_out.last_event_seq == snap_in.last_event_seq


def test_load_snapshot_returns_none_when_missing(tmp_path: Path):
    assert load_snapshot(tmp_path) is None


def test_load_snapshot_returns_none_when_checksum_corrupt(tmp_path: Path):
    record_event(tmp_path, "compile_done", {"journey_count": 1})
    snap = materialize_snapshot(read_events(tmp_path))
    persist_snapshot(tmp_path, snap)
    # Corrupt: change snapshot but keep old checksum
    path = checkpoint_snapshot_path(tmp_path)
    wrapper = json.loads(path.read_text(encoding="utf-8"))
    wrapper["snapshot"]["compile_journey_count"] = 999
    path.write_text(json.dumps(wrapper), encoding="utf-8")
    # load_snapshot detects mismatch → returns None
    assert load_snapshot(tmp_path) is None


def test_load_snapshot_returns_none_when_stale(tmp_path: Path):
    """If events.jsonl has more entries than the snapshot's
    last_event_seq, the snapshot is stale → return None to trigger
    rebuild."""
    record_event(tmp_path, "compile_done")
    snap = materialize_snapshot(read_events(tmp_path))
    persist_snapshot(tmp_path, snap)
    # Append another event WITHOUT updating snapshot
    record_event(tmp_path, "decompose_done")
    # load_snapshot detects stale → returns None
    assert load_snapshot(tmp_path) is None


def test_get_or_rebuild_snapshot_falls_back_to_events(tmp_path: Path):
    """If snapshot is missing/corrupt/stale, get_or_rebuild rebuilds
    from events.jsonl."""
    record_event(tmp_path, "compile_done", {"journey_count": 4})
    record_event(tmp_path, "decompose_done", {"emitted_subtask_ids": ["v5-z"]})
    # No persist call — snapshot doesn't exist.
    snap = get_or_rebuild_snapshot(tmp_path)
    assert snap.compile_journey_count == 4
    assert snap.decompose_emitted == ["v5-z"]


# --- The combined recorder ----------------------------------------


def test_record_and_persist_keeps_snapshot_synced(tmp_path: Path):
    record_and_persist(tmp_path, "compile_done", {"journey_count": 6})
    record_and_persist(tmp_path, "decompose_done", {"emitted_subtask_ids": ["v5-a"]})
    # Both events appended AND snapshot updated.
    snap = load_snapshot(tmp_path)
    assert snap is not None  # not stale, not corrupt
    assert snap.compile_journey_count == 6
    assert snap.decompose_emitted == ["v5-a"]
    assert snap.last_event_seq == 1


# --- Cross-session project helper ----------------------------------


def test_find_latest_checkpoint_session(tmp_path: Path):
    sessions = tmp_path / "otto_logs" / "sessions"
    # Older session with checkpoint
    older = sessions / "2026-05-20-100000-aaaa"
    older.mkdir(parents=True)
    record_event(older, "compile_done")
    # Newer session with checkpoint
    newer = sessions / "2026-05-20-120000-bbbb"
    newer.mkdir(parents=True)
    record_event(newer, "compile_done")
    # Session without checkpoint
    no_ckpt = sessions / "2026-05-20-110000-cccc"
    no_ckpt.mkdir(parents=True)
    # Find latest
    result = find_latest_checkpoint_session(tmp_path)
    assert result == newer


def test_find_latest_checkpoint_returns_none_when_no_sessions(tmp_path: Path):
    assert find_latest_checkpoint_session(tmp_path) is None


# --- Checksum stability + forward-compat ---------------------------


def test_snapshot_dict_is_deterministic(tmp_path: Path):
    """Same events → identical snapshot dict (no nondeterminism in
    serialization). Critical for the checksum guard."""
    record_event(tmp_path, "compile_done", {"journey_count": 5})
    record_event(tmp_path, "child_done", {"task_id": "v5-a", "verdict": "pass", "cost_usd": 1.0})
    record_event(tmp_path, "child_done", {"task_id": "v5-b", "verdict": "partial", "cost_usd": 2.0})
    snap = materialize_snapshot(read_events(tmp_path))
    d1 = snapshot_to_dict(snap)
    d2 = snapshot_to_dict(snap)
    assert d1 == d2
    # JSON-serialized form is also stable.
    j1 = json.dumps(d1, sort_keys=True)
    j2 = json.dumps(d2, sort_keys=True)
    assert j1 == j2


def test_snapshot_schema_version_stable():
    """Schema version is exposed for consumers to gate compat."""
    assert CHECKPOINT_SCHEMA_VERSION == 1
    snap = CheckpointSnapshot()
    assert snap.schema_version == 1
