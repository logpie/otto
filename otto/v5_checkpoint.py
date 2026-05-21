"""Phase 3 — Event-sourced checkpoint for v5 runs.

Two artifacts per session:
  - `checkpoint.events.jsonl` (durable, append-only) — the source of
    truth. Every phase boundary writes one line.
  - `checkpoint.json` (validated snapshot/cache) — materialized view of
    the latest state, with `last_event_seq` + sha256 checksum so we
    can detect corruption or staleness.

On resume:
  - Read `checkpoint.json` and verify checksum.
  - If valid + `last_event_seq` matches the events.jsonl tail → use it.
  - If invalid / stale → rebuild from events.jsonl.
  - If events.jsonl is also missing → fall back to graph inference
    (the pre-Phase-3 path; never crash).

Per Codex Plan Gate R5: "make `checkpoint.events.jsonl` the durable
source and persist `checkpoint.json` as a validated snapshot/cache
with `last_event_seq` and checksum. On corrupt/stale snapshot, rebuild
from events, then cross-check graph and branch HEADs before resume."

Phase 3 of plan-checkpoint-resume-v2.md (Codex APPROVED at R5).

Scope of this MVP:
  - Module + helpers + tests (this file).
  - Wired at the runner's key phase-boundary `_emit(...)` sites
    (compile_done / decompose_done / child_done / integration_done).
  - `compute_resume_plan` in v5_resume_plan.py reads the checkpoint
    when present (richer info than graph alone) and falls back to
    graph inference when absent.

Not in this MVP (defer to a Phase 3b if needed):
  - Sub-phase markers within integration (smoke / repair / post_agent).
  - Cross-check against branch HEADs.
  - Replay-with-patches tooling (Tier 2E from the plan proposal).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

CHECKPOINT_EVENTS_FILENAME = "checkpoint.events.jsonl"
CHECKPOINT_SNAPSHOT_FILENAME = "checkpoint.json"
CHECKPOINT_SCHEMA_VERSION = 1


# Recognized event kinds. Unknown kinds are still recorded (forward
# compat) but don't affect snapshot materialization.
EventKind = Literal[
    "compile_started",
    "compile_done",
    "decompose_started",
    "decompose_done",
    "child_dispatched",
    "child_done",
    "integration_started",
    "integration_done",
    "run_terminated",
]


@dataclass
class CheckpointEvent:
    event_seq: int
    kind: str
    ts: str
    payload: dict[str, Any]


@dataclass
class ChildCheckpoint:
    task_id: str
    verdict: str | None = None
    completed_at: str | None = None
    cost_usd: float = 0.0
    dispatched_at: str | None = None


@dataclass
class CheckpointSnapshot:
    """Materialized view over checkpoint.events.jsonl. The persistent
    `checkpoint.json` carries this plus `last_event_seq` + checksum."""

    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    last_event_seq: int = -1
    last_event_ts: str | None = None

    # Phase tracking
    compile_done_at: str | None = None
    compile_journey_count: int | None = None
    decompose_done_at: str | None = None
    decompose_emitted: list[str] = field(default_factory=list)
    integration_started_at: str | None = None
    integration_done_at: str | None = None
    integration_verdict: str | None = None
    run_terminated_at: str | None = None
    run_verdict: str | None = None

    # Per-child state
    children: dict[str, ChildCheckpoint] = field(default_factory=dict)

    # Cumulative cost (sum of child + integration emissions where known)
    cost_usd: float = 0.0


# ============================================================
# Path helpers
# ============================================================


def checkpoint_events_path(session_dir: Path) -> Path:
    return session_dir / CHECKPOINT_EVENTS_FILENAME


def checkpoint_snapshot_path(session_dir: Path) -> Path:
    return session_dir / CHECKPOINT_SNAPSHOT_FILENAME


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ============================================================
# Append + atomic write helpers
# ============================================================


def _locked_append_line(path: Path, line: str) -> None:
    """fcntl-locked append. Crash-safe single-line write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            if not line.endswith("\n"):
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_write(path: Path, content: str) -> None:
    """tmp-file + rename atomic write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(path))


# ============================================================
# Public API
# ============================================================


def record_event(
    session_dir: Path,
    kind: str,
    payload: dict[str, Any] | None = None,
) -> CheckpointEvent:
    """Append one event to the session's `checkpoint.events.jsonl`.

    Caller-owned: this function does NOT update the snapshot. Call
    `materialize_snapshot()` (or hold one in memory and persist
    periodically) to keep `checkpoint.json` in sync.

    The seq number is derived from the existing events file under the
    lock — race-safe across concurrent writers."""
    events_path = checkpoint_events_path(session_dir)
    payload = payload or {}
    # Determine next seq (line count under lock).
    events_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = events_path.with_suffix(events_path.suffix + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            existing_lines = (
                events_path.read_text(encoding="utf-8").splitlines()
                if events_path.exists()
                else []
            )
        except OSError:
            existing_lines = []
        seq = len([line for line in existing_lines if line.strip()])
        event = CheckpointEvent(
            event_seq=seq,
            kind=kind,
            ts=_now_iso(),
            payload=dict(payload),
        )
        line = json.dumps({
            "event_seq": event.event_seq,
            "kind": event.kind,
            "ts": event.ts,
            "payload": event.payload,
        })
        with events_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        return event
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def read_events(session_dir: Path) -> list[CheckpointEvent]:
    """Read all events. Skips malformed lines (best-effort durability)."""
    path = checkpoint_events_path(session_dir)
    if not path.exists():
        return []
    out: list[CheckpointEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        try:
            out.append(CheckpointEvent(
                event_seq=int(d.get("event_seq", 0)),
                kind=str(d.get("kind", "")),
                ts=str(d.get("ts", "")),
                payload=dict(d.get("payload") or {}),
            ))
        except (TypeError, ValueError):
            continue
    # Sort by seq to handle out-of-order writes.
    out.sort(key=lambda e: e.event_seq)
    return out


def materialize_snapshot(events: list[CheckpointEvent]) -> CheckpointSnapshot:
    """Replay events into a CheckpointSnapshot. Pure function."""
    snap = CheckpointSnapshot()
    for ev in events:
        snap.last_event_seq = max(snap.last_event_seq, ev.event_seq)
        snap.last_event_ts = ev.ts
        kind = ev.kind
        p = ev.payload or {}

        if kind == "compile_done":
            snap.compile_done_at = ev.ts
            jc = p.get("journey_count")
            if isinstance(jc, int):
                snap.compile_journey_count = jc
        elif kind == "decompose_done":
            snap.decompose_done_at = ev.ts
            emitted = p.get("emitted_subtask_ids") or p.get("child_task_ids") or []
            if isinstance(emitted, list):
                snap.decompose_emitted = [str(t) for t in emitted]
                # Initialize child placeholders if not already present.
                for tid in snap.decompose_emitted:
                    snap.children.setdefault(tid, ChildCheckpoint(task_id=tid))
        elif kind == "child_dispatched":
            tid = str(p.get("task_id") or "")
            if tid:
                child = snap.children.setdefault(tid, ChildCheckpoint(task_id=tid))
                child.dispatched_at = ev.ts
        elif kind == "child_done":
            tid = str(p.get("task_id") or "")
            if tid:
                child = snap.children.setdefault(tid, ChildCheckpoint(task_id=tid))
                v = p.get("verdict")
                if isinstance(v, str):
                    child.verdict = v
                child.completed_at = ev.ts
                cost = p.get("cost_usd")
                if isinstance(cost, (int, float)):
                    child.cost_usd = float(cost)
                    snap.cost_usd += float(cost)
        elif kind == "integration_started":
            snap.integration_started_at = ev.ts
        elif kind == "integration_done":
            snap.integration_done_at = ev.ts
            v = p.get("verdict")
            if isinstance(v, str):
                snap.integration_verdict = v
            cost = p.get("cost_usd")
            if isinstance(cost, (int, float)):
                snap.cost_usd += float(cost)
        elif kind == "run_terminated":
            snap.run_terminated_at = ev.ts
            v = p.get("verdict")
            if isinstance(v, str):
                snap.run_verdict = v
        # Unknown kinds are silently recorded in events.jsonl but don't
        # affect the snapshot — forward-compat for new event types.
    return snap


def snapshot_to_dict(snap: CheckpointSnapshot) -> dict[str, Any]:
    """Stable JSON-ready dict for the snapshot. Sorted-key for
    deterministic checksum computation."""
    return {
        "schema_version": snap.schema_version,
        "last_event_seq": snap.last_event_seq,
        "last_event_ts": snap.last_event_ts,
        "compile_done_at": snap.compile_done_at,
        "compile_journey_count": snap.compile_journey_count,
        "decompose_done_at": snap.decompose_done_at,
        "decompose_emitted": list(snap.decompose_emitted),
        "integration_started_at": snap.integration_started_at,
        "integration_done_at": snap.integration_done_at,
        "integration_verdict": snap.integration_verdict,
        "run_terminated_at": snap.run_terminated_at,
        "run_verdict": snap.run_verdict,
        "cost_usd": snap.cost_usd,
        "children": {
            tid: {
                "task_id": c.task_id,
                "verdict": c.verdict,
                "completed_at": c.completed_at,
                "cost_usd": c.cost_usd,
                "dispatched_at": c.dispatched_at,
            }
            for tid, c in sorted(snap.children.items())
        },
    }


def _checksum(payload: dict[str, Any]) -> str:
    """Deterministic sha256 over the JSON-serialized payload."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def persist_snapshot(session_dir: Path, snap: CheckpointSnapshot) -> None:
    """Atomic-write `checkpoint.json` with the snapshot + checksum."""
    payload = snapshot_to_dict(snap)
    wrapper = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checksum": _checksum(payload),
        "snapshot": payload,
    }
    _atomic_write(
        checkpoint_snapshot_path(session_dir),
        json.dumps(wrapper, indent=2, sort_keys=True),
    )


def load_snapshot(session_dir: Path) -> CheckpointSnapshot | None:
    """Load and validate `checkpoint.json`. Returns None if missing,
    corrupt, or stale relative to events.jsonl. Caller falls back to
    `materialize_snapshot(read_events(session_dir))` in that case."""
    path = checkpoint_snapshot_path(session_dir)
    if not path.exists():
        return None
    try:
        wrapper = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(wrapper, dict):
        return None
    snapshot_dict = wrapper.get("snapshot")
    stored_checksum = wrapper.get("checksum")
    if not isinstance(snapshot_dict, dict) or not isinstance(stored_checksum, str):
        return None
    if _checksum(snapshot_dict) != stored_checksum:
        return None  # corrupt
    # Stale-check: load events, compare last_event_seq.
    events = read_events(session_dir)
    if events:
        actual_last_seq = max(e.event_seq for e in events)
        if snapshot_dict.get("last_event_seq") != actual_last_seq:
            return None  # stale; caller should rebuild
    # Reconstruct CheckpointSnapshot
    return _snapshot_from_dict(snapshot_dict)


def _snapshot_from_dict(d: dict[str, Any]) -> CheckpointSnapshot:
    snap = CheckpointSnapshot(
        schema_version=int(d.get("schema_version", CHECKPOINT_SCHEMA_VERSION)),
        last_event_seq=int(d.get("last_event_seq", -1)),
        last_event_ts=d.get("last_event_ts"),
        compile_done_at=d.get("compile_done_at"),
        compile_journey_count=d.get("compile_journey_count"),
        decompose_done_at=d.get("decompose_done_at"),
        decompose_emitted=list(d.get("decompose_emitted") or []),
        integration_started_at=d.get("integration_started_at"),
        integration_done_at=d.get("integration_done_at"),
        integration_verdict=d.get("integration_verdict"),
        run_terminated_at=d.get("run_terminated_at"),
        run_verdict=d.get("run_verdict"),
        cost_usd=float(d.get("cost_usd") or 0.0),
    )
    for tid, cd in (d.get("children") or {}).items():
        if isinstance(cd, dict):
            snap.children[tid] = ChildCheckpoint(
                task_id=str(cd.get("task_id") or tid),
                verdict=cd.get("verdict"),
                completed_at=cd.get("completed_at"),
                cost_usd=float(cd.get("cost_usd") or 0.0),
                dispatched_at=cd.get("dispatched_at"),
            )
    return snap


def get_or_rebuild_snapshot(session_dir: Path) -> CheckpointSnapshot:
    """Single entry point for resume logic: try the snapshot, fall
    back to rebuilding from events. Always returns a snapshot (may be
    empty)."""
    snap = load_snapshot(session_dir)
    if snap is not None:
        return snap
    return materialize_snapshot(read_events(session_dir))


# ============================================================
# Convenience: combined record+persist for runner integration
# ============================================================


def record_and_persist(
    session_dir: Path,
    kind: str,
    payload: dict[str, Any] | None = None,
) -> CheckpointSnapshot:
    """Append an event AND atomically rewrite the snapshot. The
    runner's per-phase boundary code calls this so checkpoint.json
    always reflects the latest event.

    Returns the post-event snapshot (useful for telemetry/logging)."""
    record_event(session_dir, kind, payload)
    snap = materialize_snapshot(read_events(session_dir))
    try:
        persist_snapshot(session_dir, snap)
    except OSError:
        # Snapshot write failure is non-fatal — events.jsonl remains
        # authoritative; next call will retry the snapshot.
        pass
    return snap


# ============================================================
# Project-level helper: find the latest session with a checkpoint
# ============================================================


def find_latest_checkpoint_session(project_dir: Path) -> Path | None:
    """Locate the latest session dir under `otto_logs/sessions/` that
    has either a checkpoint.events.jsonl or a checkpoint.json. Used by
    resume planner to find checkpoint context without coupling to the
    runner's session layout."""
    sessions = project_dir / "otto_logs" / "sessions"
    if not sessions.exists():
        return None
    candidates: list[tuple[float, Path]] = []
    for sdir in sessions.iterdir():
        if not sdir.is_dir() or sdir.name.startswith("."):
            continue
        if (sdir / CHECKPOINT_EVENTS_FILENAME).exists() or (
            sdir / CHECKPOINT_SNAPSHOT_FILENAME
        ).exists():
            candidates.append((sdir.stat().st_mtime, sdir))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]
