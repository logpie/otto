"""Append-only state journal for the intent-to-product pipeline.

Step 3 of the unified intent-to-product plan. The journal is the single
source of truth for slice progress; it coexists with `otto/checkpoint.py`
(which still owns session-level metadata: intent, costs, agent session
ids, spec phase).

Layout
------

  <session>/spec-state.jsonl

Each line is a JSON object with shape:

  {"ts": "<ISO-8601>", "kind": "<event-kind>", ...payload}

Event kinds (mirror the design doc):

  slice.started           — build agent for slice <id> dispatched
  slice.check.started     — a Check began running on slice <id>
  slice.check.finished    — a Check completed on slice <id> (passed/failed)
  slice.attempt.failed    — slice's check round failed; retry counter ticks
  slice.merge.eligible    — slice cleared deps + freshness checks; queued
  slice.merge.started     — merge runner started landing this slice
  slice.merge.landed      — slice merged into target
  slice.blocked           — slice exhausted retries / merge repair budget
  audit.started           — final audit pass began
  audit.finished          — final audit pass produced verdict
  run.finished            — entire run reached a terminal state

Why a separate file from checkpoint.json?

  checkpoint.json is rewritten in place each time a phase transitions —
  the latest snapshot is what counts. The state journal is append-only
  per event so we can reconstruct the full timeline (including failed
  attempts) without losing intermediate state. Together they answer
  "what is the run doing right now?" (checkpoint) and "what has
  happened so far?" (journal).

Mid-merge recovery
------------------

If the host process dies mid-rebase, the worktree's `.git/REBASE_HEAD`
or `.git/MERGE_HEAD` will exist. `recover_mid_merge_state(worktree_dir)`
detects this, runs `git rebase --abort` / `git merge --abort` to clear
the intermediate state, and returns a `MidMergeRecovery` describing
what was found. Callers then emit a fresh `slice.merge.eligible` event
to restart the merge from a clean base.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal

logger = logging.getLogger("otto.spec_state")

JOURNAL_FILENAME = "spec-state.jsonl"


# Allowed event kinds. Keeping this as a tuple (not Enum) so JSON values
# are plain strings and tests can read them literally.
EVENT_KINDS: tuple[str, ...] = (
    "slice.started",
    "slice.check.started",
    "slice.check.finished",
    "slice.attempt.failed",
    "slice.merge.eligible",
    "slice.merge.started",
    "slice.merge.landed",
    "slice.blocked",
    "audit.started",
    "audit.finished",
    "run.finished",
    # v2.2 — amendments + scope events ----------------------------------
    "scope.warning",            # build agent attempted out-of-scope edit (informational)
    "amendment.requested",      # build agent (or user) requested a tier-3 amendment
    "amendment.applied",        # request passed all tier checks and persisted
    "amendment.rejected",       # request failed a tier rule
    "intent.lock.violated",     # tier-1 invariant violated (tampering signal)
)

EventKind = Literal[
    "slice.started",
    "slice.check.started",
    "slice.check.finished",
    "slice.attempt.failed",
    "slice.merge.eligible",
    "slice.merge.started",
    "slice.merge.landed",
    "slice.blocked",
    "audit.started",
    "audit.finished",
    "run.finished",
    "scope.warning",
    "amendment.requested",
    "amendment.applied",
    "amendment.rejected",
    "intent.lock.violated",
]


@dataclass(frozen=True)
class Event:
    """One append-only journal event.

    `event_id` is stable per-session and assigned at append time
    (`ev-NNNNNN` based on the journal's line count). Amendments
    reference these IDs in `Amendment.trigger_event_id`.
    """
    ts: str                                   # ISO-8601 UTC, e.g. 2026-05-03T12:34:56Z
    kind: str                                 # one of EVENT_KINDS
    event_id: str = ""                        # set at append time, never changes
    slice_id: str = ""                        # blank for audit.* and run.*
    check_id: str = ""                        # blank unless slice.check.*
    attempt: int = 0                          # 0 unless retry-aware event
    detail: str = ""                          # short human-readable detail
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Slice phases derived from the journal
# ---------------------------------------------------------------------------

# Slice lifecycle (DAG; events transition forward only):
#
#   PENDING                       (slice in spec, no events yet)
#   BUILDING   ← slice.started
#   CHECKING   ← slice.check.started
#   FAILED     ← slice.attempt.failed         (retry counter advances)
#   ELIGIBLE   ← slice.merge.eligible
#   MERGING    ← slice.merge.started
#   LANDED     ← slice.merge.landed
#   BLOCKED    ← slice.blocked                (terminal failure)
#
# `replay()` walks the journal and returns the final phase per slice.

PENDING = "pending"
BUILDING = "building"
CHECKING = "checking"
FAILED = "failed"
ELIGIBLE = "eligible"
MERGING = "merging"
LANDED = "landed"
BLOCKED = "blocked"

_PHASE_FOR_KIND: dict[str, str] = {
    "slice.started": BUILDING,
    "slice.check.started": CHECKING,
    "slice.check.finished": CHECKING,
    "slice.attempt.failed": FAILED,
    "slice.merge.eligible": ELIGIBLE,
    "slice.merge.started": MERGING,
    "slice.merge.landed": LANDED,
    "slice.blocked": BLOCKED,
}


@dataclass
class SliceState:
    slice_id: str
    phase: str = PENDING
    last_event_ts: str = ""
    attempts: int = 0
    last_failure: str = ""


@dataclass
class RunState:
    """Snapshot of run progress derived from the journal."""
    slices: dict[str, SliceState] = field(default_factory=dict)
    audit_started: bool = False
    audit_finished: bool = False
    audit_verdict: str = ""                   # "" | "passed" | "partial" | "blocked"
    run_finished: bool = False
    run_verdict: str = ""

    def slice_state(self, slice_id: str) -> SliceState:
        if slice_id not in self.slices:
            self.slices[slice_id] = SliceState(slice_id=slice_id)
        return self.slices[slice_id]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def journal_path(session_dir: Path) -> Path:
    return Path(session_dir) / JOURNAL_FILENAME


def _next_event_id(session_dir: Path) -> str:
    """Generate the next stable event id for this session.

    Format: ``ev-NNNNNN`` based on the current line count of
    `spec-state.jsonl`. New events get the next number; once written,
    an event's id is permanent. Amendments reference these via
    `Amendment.trigger_event_id`.
    """
    target = journal_path(session_dir)
    if not target.exists():
        return "ev-000001"
    try:
        with target.open("rb") as fh:
            count = sum(1 for _ in fh)
    except OSError:
        count = 0
    return f"ev-{count + 1:06d}"


def append_event(session_dir: Path, event: Event) -> Event:
    """Append one event to the session's `spec-state.jsonl`.

    Lines are written with a trailing `\\n` so a reader can split on
    newlines without sentinel. Caller is responsible for picking the
    `kind` from `EVENT_KINDS`; we validate to catch typos early.

    Returns the event with its assigned `event_id` populated. If the
    caller already supplied an event_id, that's preserved (used during
    replay reconstruction); otherwise a fresh stable id is generated.
    """
    if event.kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind {event.kind!r}; expected one of {EVENT_KINDS}")
    target = journal_path(session_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not event.event_id:
        event = dataclasses.replace(event, event_id=_next_event_id(session_dir))
    payload = dataclasses.asdict(event)
    line = json.dumps(payload, sort_keys=True) + "\n"
    with target.open("a", encoding="utf-8") as fh:
        fh.write(line)
    return event


def emit(
    session_dir: Path,
    kind: str,
    *,
    slice_id: str = "",
    check_id: str = "",
    attempt: int = 0,
    detail: str = "",
    **extra: Any,
) -> Event:
    """Convenience wrapper around `append_event` with `ts=_iso_now()`.

    Returns the persisted Event with `event_id` populated; amendment
    callers store this id in `Amendment.trigger_event_id`.
    """
    event = Event(
        ts=_iso_now(),
        kind=kind,
        slice_id=slice_id,
        check_id=check_id,
        attempt=attempt,
        detail=detail,
        extra=dict(extra),
    )
    return append_event(session_dir, event)


def find_event(session_dir: Path, event_id: str) -> Event | None:
    """Locate an event by its stable id. Returns None if not found.

    Used by the amendment API to verify trigger_event_id linkage.
    """
    if not event_id:
        return None
    for event in iter_events(session_dir):
        if event.event_id == event_id:
            return event
    return None


# ---------------------------------------------------------------------------
# Reading + replay
# ---------------------------------------------------------------------------


def iter_events(session_dir: Path) -> Iterator[Event]:
    target = journal_path(session_dir)
    if not target.exists():
        return
    with target.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("skipping malformed journal line in %s", target)
                continue
            yield Event(
                ts=str(payload.get("ts") or ""),
                kind=str(payload.get("kind") or ""),
                event_id=str(payload.get("event_id") or ""),
                slice_id=str(payload.get("slice_id") or ""),
                check_id=str(payload.get("check_id") or ""),
                attempt=int(payload.get("attempt") or 0),
                detail=str(payload.get("detail") or ""),
                extra=dict(payload.get("extra") or {}),
            )


def replay(session_dir: Path, slice_ids: Iterable[str] = ()) -> RunState:
    """Derive per-slice state by scanning the journal in order.

    Pre-seeds slice entries from `slice_ids` so the caller's spec slice
    set is reflected even if no events have fired yet (slices in PENDING
    show up in `state.slices`).
    """
    state = RunState()
    for sid in slice_ids:
        state.slice_state(sid)

    attempts_by_slice: dict[str, int] = defaultdict(int)

    for event in iter_events(session_dir):
        if event.kind in {"audit.started", "audit.finished", "run.finished"}:
            if event.kind == "audit.started":
                state.audit_started = True
            elif event.kind == "audit.finished":
                state.audit_finished = True
                verdict = str(event.extra.get("verdict") or "")
                if verdict:
                    state.audit_verdict = verdict
            elif event.kind == "run.finished":
                state.run_finished = True
                verdict = str(event.extra.get("verdict") or "")
                if verdict:
                    state.run_verdict = verdict
            continue

        if not event.slice_id:
            # Slice-scoped event without a slice_id is a programming error
            # but we don't drop it — surface in extra debugging if needed.
            continue

        slice_state = state.slice_state(event.slice_id)
        slice_state.last_event_ts = event.ts

        if event.kind == "slice.attempt.failed":
            attempts_by_slice[event.slice_id] += 1
            slice_state.attempts = attempts_by_slice[event.slice_id]
            slice_state.last_failure = event.detail

        new_phase = _PHASE_FOR_KIND.get(event.kind)
        if new_phase is not None:
            slice_state.phase = new_phase

    return state


# ---------------------------------------------------------------------------
# Mid-merge git intermediate-state recovery
# ---------------------------------------------------------------------------


@dataclass
class MidMergeRecovery:
    """Result of probing a worktree for stuck intermediate git state.

    `kind` is `""` for "nothing was stuck", or one of `rebase` / `merge` /
    `unmerged` describing what we cleaned up. `restart_required` is
    `True` whenever we ran any abort — callers should emit a fresh
    `slice.merge.eligible` event.
    """
    kind: str = ""
    restart_required: bool = False
    detail: str = ""


def _git(worktree_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a `git` invocation in `worktree_dir`. Returns the completed process."""
    return subprocess.run(
        ["git", *args],
        cwd=str(worktree_dir),
        capture_output=True,
        text=True,
        check=False,
    )


def recover_mid_merge_state(worktree_dir: Path) -> MidMergeRecovery:
    """Probe `worktree_dir` for stuck rebase / merge state and abort it cleanly.

    Detection rules:
      * `.git/REBASE_HEAD` (or `rebase-merge/` / `rebase-apply/` dirs) → `git rebase --abort`.
      * `.git/MERGE_HEAD` → `git merge --abort`.
      * Neither, but `git status --porcelain` shows files in `UU`/`AA`/`DD`
        → call `git reset --merge` to clear the index conflict state.

    The function never raises; it returns an empty `MidMergeRecovery`
    (kind=`""`, restart_required=False) when nothing was stuck, and
    surfaces aborts via the `kind` field.
    """
    git_dir = worktree_dir / ".git"
    if not git_dir.exists():
        return MidMergeRecovery()

    # Submodule / linked worktree: .git can be a file pointing at gitdir.
    if git_dir.is_file():
        try:
            content = git_dir.read_text(encoding="utf-8").strip()
        except OSError:
            return MidMergeRecovery()
        if content.startswith("gitdir:"):
            git_dir = Path(content.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = (worktree_dir / git_dir).resolve()

    rebase_head = git_dir / "REBASE_HEAD"
    rebase_merge = git_dir / "rebase-merge"
    rebase_apply = git_dir / "rebase-apply"
    merge_head = git_dir / "MERGE_HEAD"

    if rebase_head.exists() or rebase_merge.exists() or rebase_apply.exists():
        result = _git(worktree_dir, "rebase", "--abort")
        return MidMergeRecovery(
            kind="rebase",
            restart_required=True,
            detail=(result.stderr or result.stdout).strip(),
        )

    if merge_head.exists():
        result = _git(worktree_dir, "merge", "--abort")
        return MidMergeRecovery(
            kind="merge",
            restart_required=True,
            detail=(result.stderr or result.stdout).strip(),
        )

    # No HEAD pointer file but the index might still be in conflict.
    status = _git(worktree_dir, "status", "--porcelain")
    if status.returncode == 0 and status.stdout:
        for line in status.stdout.splitlines():
            if not line:
                continue
            code = line[:2]
            if code in {"UU", "AA", "DD", "AU", "UA", "UD", "DU"}:
                reset = _git(worktree_dir, "reset", "--merge")
                return MidMergeRecovery(
                    kind="unmerged",
                    restart_required=True,
                    detail=(reset.stderr or reset.stdout).strip(),
                )

    return MidMergeRecovery()
