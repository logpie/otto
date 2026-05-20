"""Append-only state journal for the intent-to-product pipeline.

Step 3 of the unified intent-to-product plan. The journal is the single
source of truth for group progress; it coexists with `otto/checkpoint.py`
(which still owns session-level metadata: intent, costs, agent session
ids, spec phase).

Layout
------

  <session>/spec-state.jsonl

Each line is a JSON object with shape:

  {"ts": "<ISO-8601>", "kind": "<event-kind>", ...payload}

Event kinds (mirror the design doc):

  group.started           — build agent for group <id> dispatched
  group.check.started     — a Check began running on group <id>
  group.check.finished    — a Check completed on group <id> (passed/failed)
  group.attempt.failed    — group's check round failed; retry counter ticks
  group.repair.progress_extension — merge repair made progress; grant bounded retry
  group.merge.eligible    — group cleared deps + freshness checks; queued
  group.merge.started     — merge runner started landing this group
  group.merge.landed      — group merged into target
  group.blocked           — group exhausted retries / merge repair budget
  run.started             — entire run entered the pipeline
  audit.started           — final audit pass began
  audit.finished          — final audit pass produced verdict
  run.started             — entire run started
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
what was found. Callers then emit a fresh `group.merge.eligible` event
to restart the merge from a clean base.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import logging
import os
from otto.observability import iso_timestamp
from otto.paths import sidecar_lock_path
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
    "group.started",
    "group.execution.started",
    "group.execution.finished",
    "group.check.started",
    "group.check.finished",
    "group.check.feedback",     # authoritative check evidence fed to same repair thread
    "group.self_check.report",  # build agent's own bounded self-check summary
    "group.attempt.failed",
    "group.repair.progress_extension",  # repair failure changed; grant bounded retry
    "group.merge.eligible",
    "group.merge.started",
    "group.merge.landed",
    "group.merge.redundant",   # Pattern A: group produced no new diff (over-reach symptom)
    "group.blocked",
    "run.started",
    "audit.started",
    "audit.finished",
    "audit.attempt.finished",  # Pattern A: per-attempt audit verdict in retry loop
    "run.started",
    "run.finished",
    # v2.2 — amendments + scope events ----------------------------------
    "scope.warning",            # build agent attempted out-of-scope edit (informational)
    "scope.critical",           # reserved for hard safety scope crossings
    "contract.delta",           # build branch touched a shared contract surface
    "contract.delta.merge",     # merge will inspect branch contract deltas
    "amendment.requested",      # build agent (or user) requested a tier-3 amendment
    "amendment.applied",        # request passed all tier checks and persisted
    "amendment.rejected",       # request failed a tier rule
    "intent.lock.violated",     # tier-1 invariant violated (tampering signal)
    # A5 — spec-review surface (research §2.1) -------------------------
    "spec.review.opened",       # user opened the spec-review UI for this session
    "spec.edited",              # user posted edited markdown via /api/specs/.../edit
    "spec.approved",            # user approved the spec via /api/specs/.../approve
    "spec.regenerated",         # spec re-emitted by the compile agent (post-edit recompile)
    # A6 — mid-build spec edit invalidation ----------------------------
    "group.invalidated_by_spec_edit",  # group's spec contributions changed mid-build
    # A13 — review-gate (compile → review-gate → build) ----------------
    # `spec.review_pending` is the runner's signal "compile finished, gate is
    # holding the build phase". `spec.review_approved` is the resume signal
    # the runner polls for. Distinct from `spec.approved` so the existing
    # spec lifecycle state machine and the build-gate state machine stay
    # independent — approving the spec for a non-gated run already emits
    # `spec.approved`; the gate adds a separate "may proceed to build" signal.
    "spec.review_pending",      # runner paused after compile, waiting for approval
    "spec.review_approved",     # operator approved (build phase may proceed)
    # Seed stage lifecycle (RunView stage timeline; RUA W6-C fix) -------
    "seed.started",             # seed_fixtures() entered (one per Run)
    "seed.finished",            # seed_fixtures() returning; extra.succeeded=bool
    # A7 — user-initiated pause / resume / abort verbs (Mission Control) -
    "run.paused_by_user",       # operator clicked Pause; runner sleeps between phases
    "run.resumed_by_user",      # operator clicked Resume after a paused_by_user
    "group.aborted_by_user",    # operator aborted a single Group; build loop exits
)

EventKind = Literal[
    "group.started",
    "group.execution.started",
    "group.execution.finished",
    "group.check.started",
    "group.check.finished",
    "group.check.feedback",
    "group.self_check.report",
    "group.attempt.failed",
    "group.merge.eligible",
    "group.merge.started",
    "group.merge.landed",
    "group.merge.redundant",   # Pattern A — group produced no diff
    "group.blocked",
    "run.started",
    "audit.started",
    "audit.finished",
    "audit.attempt.finished",  # Pattern A — per-attempt audit verdict in retry loop
    "run.started",
    "run.finished",
    "scope.warning",
    "scope.critical",
    "contract.delta",
    "contract.delta.merge",
    "amendment.requested",
    "amendment.applied",
    "amendment.rejected",
    "intent.lock.violated",
    "spec.review.opened",
    "spec.edited",
    "spec.approved",
    "spec.regenerated",
    "group.invalidated_by_spec_edit",
    "spec.review_pending",
    "spec.review_approved",
    "seed.started",
    "seed.finished",
    "run.paused_by_user",
    "run.resumed_by_user",
    "group.aborted_by_user",
]


@dataclass(frozen=True)
class Event:
    """One append-only journal event.

    `event_id` is stable per-session and assigned at append time
    (`ev-NNNNNN` based on the journal's line count). Amendments
    reference these IDs in `Amendment.trigger_event_id`.

    `feature_id` (round-3 audit gap 5): optional per-Feature attribution
    for events that scope to a single Feature within a Group (e.g. an
    audit walkthrough action, a per-Feature check result). Mirrors
    ``otto.checks.Evidence.feature_id`` so journal events can join
    cleanly with evidence rows. Empty = "unattributed / Group-level".
    The field is optional and defaults to empty for back-compat with
    existing replay() consumers that ignore it.
    """
    ts: str                                   # ISO-8601 UTC, e.g. 2026-05-03T12:34:56Z
    kind: str                                 # one of EVENT_KINDS
    event_id: str = ""                        # set at append time, never changes
    group_id: str = ""                        # blank for audit.* and run.*
    check_id: str = ""                        # blank unless group.check.*
    attempt: int = 0                          # 0 unless retry-aware event
    detail: str = ""                          # short human-readable detail
    feature_id: str = ""                      # optional per-Feature attribution
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Group phases derived from the journal
# ---------------------------------------------------------------------------

# Group lifecycle (DAG; events transition forward only):
#
#   PENDING                       (group in spec, no events yet)
#   BUILDING   ← group.started
#   CHECKING   ← group.check.started
#   FAILED     ← group.attempt.failed         (retry counter advances)
#   ELIGIBLE   ← group.merge.eligible
#   MERGING    ← group.merge.started
#   LANDED     ← group.merge.landed
#   BLOCKED    ← group.blocked                (terminal failure)
#
# `replay()` walks the journal and returns the final phase per group.

PENDING = "pending"
BUILDING = "building"
CHECKING = "checking"
FAILED = "failed"
ELIGIBLE = "eligible"
MERGING = "merging"
LANDED = "landed"
REDUNDANT = "redundant"  # Pattern A: group's checks passed but no new commit
BLOCKED = "blocked"
INVALIDATED = "invalidated"  # A6: spec edit landed mid-build; group must re-dispatch

_PHASE_FOR_KIND: dict[str, str] = {
    "group.started": BUILDING,
    "group.check.started": CHECKING,
    "group.check.finished": CHECKING,
    "group.check.feedback": CHECKING,
    "group.attempt.failed": FAILED,
    "group.merge.eligible": ELIGIBLE,
    "group.merge.started": MERGING,
    "group.merge.landed": LANDED,
    "group.merge.redundant": REDUNDANT,
    "group.blocked": BLOCKED,
    "group.invalidated_by_spec_edit": INVALIDATED,
    # A7 — operator abort is terminal for the Group; treat like BLOCKED so
    # replay()-derived RunState matches the side-channel `aborted_group_ids()`
    # accounting (otherwise an aborted Group's phase stays at whatever it
    # was last — typically BUILDING — and `landed_components` /
    # `pending_components` classify it incorrectly on resume).
    "group.aborted_by_user": BLOCKED,
}

# Run-scoped events that are intentionally NOT phase-affecting. They mutate
# session-level state (lifecycle, review-gate, spec versions) but do NOT
# transition any single Group's phase. Listed here so future maintainers
# don't have to re-derive intent from absence; replay() simply ignores them.
# Mapping check is `_PHASE_FOR_KIND.get(event.kind)` → no entry → no-op.
_RUN_SCOPED_NO_PHASE_KINDS: frozenset[str] = frozenset({
    "run.paused_by_user",       # session pause flag (poll predicate, not phase)
    "run.resumed_by_user",      # clears pause flag (poll predicate, not phase)
    "run.started",              # session lifecycle marker, not a group phase
    "spec.review.opened",       # operator opened the review surface
    "spec.review_pending",      # A13 review-gate engaged
    "spec.review_approved",     # A13 review-gate cleared
    "spec.edited",              # spec markdown edited (Group invalidations
                                # come via separate `group.invalidated_by_spec_edit`)
    "spec.approved",            # lifecycle flip (draft → approved)
    "spec.regenerated",         # spec recompiled post-edit
})


@dataclass
class GroupState:
    group_id: str
    phase: str = PENDING
    last_event_ts: str = ""
    attempts: int = 0
    last_failure: str = ""


@dataclass
class RunState:
    """Snapshot of run progress derived from the journal."""
    groups: dict[str, GroupState] = field(default_factory=dict)
    audit_started: bool = False
    audit_finished: bool = False
    # Pattern G: count audit retries from journal so resume reports
    # "audit took N attempts" honestly. Each `audit.attempt.finished`
    # event increments this; the final `audit.finished` event sets
    # the terminal verdict. The two are independent — partial
    # verdicts in attempts don't change `audit_verdict`.
    audit_attempts: int = 0
    audit_verdict: str = ""                   # "" | "passed" | "partial" | "blocked"
    run_finished: bool = False
    run_verdict: str = ""
    # Pattern A reconciliation: when replay() is given a project_dir,
    # it cross-checks LANDED events against `git log`. Groups whose
    # claimed commit hash isn't in the actual git history land here.
    unreconciled_landed_ids: list[str] = field(default_factory=list)
    # Groups whose LANDED hash matches a real commit, BUT >1 group
    # claimed the same hash (only the first contributed; the rest are
    # bookkeeping echoes of an over-reaching group).
    duplicate_hash_landed_ids: list[str] = field(default_factory=list)

    def group_state(self, group_id: str) -> GroupState:
        if group_id not in self.groups:
            self.groups[group_id] = GroupState(group_id=group_id)
        return self.groups[group_id]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def journal_path(session_dir: Path) -> Path:
    return Path(session_dir) / JOURNAL_FILENAME


def _journal_lock_path(target: Path) -> Path:
    return sidecar_lock_path(target)


def _next_event_id_for_target(target: Path) -> str:
    if not target.exists():
        return "ev-000001"
    try:
        with target.open("rb") as fh:
            count = sum(1 for _ in fh)
    except OSError:
        count = 0
    return f"ev-{count + 1:06d}"


def _next_event_id(session_dir: Path) -> str:
    """Generate the next stable event id for this session.

    Format: ``ev-NNNNNN`` based on the current line count of
    `spec-state.jsonl`. New events get the next number; once written,
    an event's id is permanent. Amendments reference these via
    `Amendment.trigger_event_id`.
    """
    return _next_event_id_for_target(journal_path(session_dir))


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
    lock_fd = os.open(str(_journal_lock_path(target)), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if not event.event_id:
            event = dataclasses.replace(event, event_id=_next_event_id_for_target(target))
        payload = dataclasses.asdict(event)
        line = json.dumps(payload, sort_keys=True) + "\n"
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
    return event


def emit(
    session_dir: Path,
    kind: str,
    *,
    group_id: str = "",
    check_id: str = "",
    attempt: int = 0,
    detail: str = "",
    feature_id: str = "",
    **extra: Any,
) -> Event:
    """Convenience wrapper around `append_event` with `ts=iso_timestamp()`.

    Returns the persisted Event with `event_id` populated; amendment
    callers store this id in `Amendment.trigger_event_id`.

    `feature_id` is optional per-Feature attribution (see Event docstring).
    """
    event = Event(
        ts=iso_timestamp(),
        kind=kind,
        group_id=group_id,
        check_id=check_id,
        attempt=attempt,
        detail=detail,
        feature_id=feature_id,
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
# A7 — pause + abort flag predicates (Mission Control verbs)
# ---------------------------------------------------------------------------


def is_run_paused_by_user(session_dir: Path) -> bool:
    """Return True if the most-recent pause/resume event for this session
    is `run.paused_by_user` (i.e. the operator hit Pause and has not yet
    resumed).

    Implementation: scan the journal in order and remember the last
    `run.paused_by_user` / `run.resumed_by_user` we saw. If the trailing
    event is `run.paused_by_user`, the run is currently paused. If we
    saw a resume after the most recent pause (or never saw a pause at
    all), it is not paused.

    The runner polls this between phases to decide whether to sleep.
    """
    state: str = ""  # "" | "paused" | "resumed"
    for event in iter_events(session_dir):
        if event.kind == "run.paused_by_user":
            state = "paused"
        elif event.kind == "run.resumed_by_user":
            state = "resumed"
    return state == "paused"


def is_group_aborted_by_user(session_dir: Path, group_id: str) -> bool:
    """Return True if the journal contains a `group.aborted_by_user`
    event for `group_id`. Once aborted, the flag is sticky for the rest
    of the run — the build loop checks this before each retry attempt
    and bails out as BLOCKED with reason="aborted_by_user" when set.
    """
    if not group_id:
        return False
    target_id = str(group_id).strip()
    for event in iter_events(session_dir):
        if event.kind == "group.aborted_by_user" and event.group_id == target_id:
            return True
    return False


def aborted_group_ids(session_dir: Path) -> set[str]:
    """Return the set of all `group.aborted_by_user` ids in the journal.

    Used by the merge queue to skip aborted groups (treat as BLOCKED)
    so they never become merge candidates.
    """
    out: set[str] = set()
    for event in iter_events(session_dir):
        if event.kind == "group.aborted_by_user" and event.group_id:
            out.add(event.group_id)
    return out


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
                group_id=str(payload.get("group_id") or ""),
                check_id=str(payload.get("check_id") or ""),
                attempt=int(payload.get("attempt") or 0),
                detail=str(payload.get("detail") or ""),
                feature_id=str(payload.get("feature_id") or ""),
                extra=dict(payload.get("extra") or {}),
            )


def replay(
    session_dir: Path,
    group_ids: Iterable[str] = (),
    *,
    project_dir: Path | None = None,
) -> RunState:
    """Derive per-group state by scanning the journal in order.

    Pre-seeds group entries from `group_ids` so the caller's spec group
    set is reflected even if no events have fired yet (groups in PENDING
    show up in `state.groups`).

    Pattern A fix: when `project_dir` is provided, cross-checks every
    `LANDED` event's commit hash against `git log --oneline` on the
    project repo. If a group claims LANDED but its commit isn't in
    the actual git history, downgrade to a special UNRECONCILED phase
    in `state.unreconciled_landed_ids` (and leave the journal phase
    intact for backward compat). This catches the bookkeeping-vs-reality
    bug where multiple groups reported the same hash.
    """
    state = RunState()
    for sid in group_ids:
        state.group_state(sid)

    attempts_by_group: dict[str, int] = defaultdict(int)
    landed_events: list[tuple[str, str]] = []  # (group_id, commit_hash)

    for event in iter_events(session_dir):
        if event.kind == "group.merge.landed" and event.group_id and event.detail:
            landed_events.append((event.group_id, event.detail.strip().split()[0] if event.detail.strip() else ""))
        if event.kind in {"run.started", "audit.started", "audit.finished", "audit.attempt.finished", "run.finished"}:
            if event.kind == "run.started":
                continue
            if event.kind == "audit.started":
                state.audit_started = True
            elif event.kind == "audit.attempt.finished":
                # Pattern G: track retry count, but DO NOT touch
                # audit_verdict — only audit.finished sets the
                # terminal verdict. This event is journal-only for
                # observability; it does not change run state.
                state.audit_attempts += 1
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

        if not event.group_id:
            # Group-scoped event without a group_id is a programming error
            # but we don't drop it — surface in extra debugging if needed.
            continue

        group_state = state.group_state(event.group_id)
        group_state.last_event_ts = event.ts

        if event.kind == "group.attempt.failed":
            attempts_by_group[event.group_id] += 1
            group_state.attempts = attempts_by_group[event.group_id]
            group_state.last_failure = event.detail

        new_phase = _PHASE_FOR_KIND.get(event.kind)
        if new_phase is not None:
            group_state.phase = new_phase

    # Pattern A: cross-check LANDED against git history.
    if project_dir is not None and landed_events:
        try:
            log = subprocess.run(
                ["git", "log", "--all", "--format=%h %H"],
                cwd=str(project_dir), capture_output=True, text=True, check=False,
            )
            if log.returncode == 0:
                # Each line: short hash + full hash; both forms valid for matching.
                known_hashes: set[str] = set()
                for line in log.stdout.splitlines():
                    parts = line.split()
                    for p in parts:
                        if p:
                            known_hashes.add(p)
                # Detect duplicates: multiple groups claiming the same hash.
                hash_counts: dict[str, int] = defaultdict(int)
                for _sid, h in landed_events:
                    if h:
                        hash_counts[h] += 1
                for sid, h in landed_events:
                    if not h or h not in known_hashes:
                        state.unreconciled_landed_ids.append(sid)
                    elif hash_counts.get(h, 0) > 1:
                        # >1 group claims the same commit — only the first
                        # one possibly contributed; the rest are duplicates.
                        state.duplicate_hash_landed_ids.append(sid)
        except (FileNotFoundError, OSError):
            pass

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
    `group.merge.eligible` event.
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
