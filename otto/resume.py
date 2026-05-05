"""Resume planner for the i2p stack.

Pure-derivation step: given a paused session directory, replays
``spec-state.jsonl`` through ``otto.spec_state.replay`` and produces a
read-only ``ResumePlan`` describing what the runner can skip.

Design contract (see ``docs/i2p-resume-design.md`` for the full picture):

* No new persisted journals — ``ResumePlan`` is derived from existing
  artifacts (spec-state.jsonl + summary.json + spec.json).
* ``plan_resume`` does NOT mutate disk. It is safe to call from the
  read-only inspection path.
* Spec-hash validation is split: ``plan_resume`` records the on-disk
  hash; ``verify_spec_hash_matches`` is the gate that the CLI calls
  with the freshly-loaded current spec. Splitting lets the CLI surface
  a friendly diff message before raising.

Mid-merge git recovery is exposed here as a thin re-export of
``otto.spec_state.recover_mid_merge_state`` plus a higher-level
``recover_mid_merge_state_for_project`` that probes the project's main
worktree (resume's first stop before re-dispatching anything).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from otto import paths as _paths
from otto.spec_compile import Spec, load_spec
from otto.spec_state import (
    BLOCKED,
    ELIGIBLE,
    LANDED,
    MidMergeRecovery,
    REDUNDANT,
    RunState,
    recover_mid_merge_state,
    replay,
)

logger = logging.getLogger("otto.resume")


SPEC_HASH_LENGTH = 16  # short prefix for human-readable error messages


class ResumeError(Exception):
    """Raised when a paused session cannot be resumed safely.

    The CLI catches this, prints the message, and either exits or
    proceeds depending on whether ``--force`` was passed.
    """


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResumePlan:
    """Read-only resume snapshot for one paused i2p session.

    All fields are populated by ``plan_resume`` from on-disk artifacts.
    The runner consumes this verbatim — it never mutates a ResumePlan.

    Field semantics:

    * ``landed_components``: Component / Group ids that the journal
      records as LANDED (or REDUNDANT — the over-reach pattern that
      still counts as "merged work, do not re-dispatch"). The runner
      passes this set into ``run_build`` so dispatch is skipped.
    * ``pending_components``: ids the journal saw at least one event
      for but no terminal event (BUILDING / CHECKING / FAILED /
      ELIGIBLE / MERGING / unreconciled-LANDED). These are "in-flight";
      resume re-dispatches after mid-merge git recovery. Naming kept
      as ``pending_components`` to match the user-facing CLI banner
      (we treat in-flight ids as "to do" from the operator's POV).
    * ``audit_finished``: ``True`` when the journal recorded
      ``audit.finished`` AND a non-empty verdict. Resume short-circuits
      the audit phase in this case.
    * ``prior_cost_usd`` / ``prior_wall_s``: pulled from
      ``summary.json`` (or ``checkpoint.json`` as a fallback). The
      runner adds these into the shared BuildBudget so the documented
      total cost cap counts both attempts honestly.
    * ``spec_hash``: sha256 of the on-disk ``spec.json``. Compared at
      resume time against the spec the runner is about to operate on.
    * ``halted_reason``: the prior run's ``run.finished`` detail field,
      surfaced in the CLI banner. Empty if the run was paused without a
      ``run.finished`` event.
    * ``unreconciled_landed_ids``: forwarded straight from the replay
      so the CLI can warn the operator. These are LANDED claims whose
      commit hash is missing from git history — almost certainly a
      crash between commit and journal flush.
    """

    session_id: str
    paused_session_dir: Path
    spec_hash: str
    landed_components: frozenset[str] = field(default_factory=frozenset)
    pending_components: frozenset[str] = field(default_factory=frozenset)
    audit_finished: bool = False
    audit_verdict: str = ""
    prior_cost_usd: float = 0.0
    prior_wall_s: float = 0.0
    halted_reason: str = ""
    unreconciled_landed_ids: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_paused_session(project_dir: Path) -> Path | None:
    """Dereference ``otto_logs/paused`` and return the session directory.

    Thin wrapper over ``paths.resolve_pointer``; centralizes the lookup
    so the CLI can call one entry point. Returns ``None`` when no
    paused pointer is set or the target is gone.
    """
    return _paths.resolve_pointer(project_dir, _paths.PAUSED_POINTER)


def plan_resume(
    session_dir: Path,
    *,
    project_dir: Path | None = None,
    component_ids: Iterable[str] | None = None,
) -> ResumePlan:
    """Build a ``ResumePlan`` from a paused session directory.

    Pure derivation: reads the spec, replays the journal, classifies
    Components, and pulls cost/wall accounting from the session
    summary. Does not mutate disk.

    Args:
        session_dir: ``otto_logs/sessions/<id>``.
        project_dir: Optional project root. When set, replay
            cross-checks LANDED journal events against ``git log``
            (Pattern A reconciliation) so resume treats lying-LANDED
            claims as in-flight.
        component_ids: Optional explicit list of Component / Group ids
            to seed the replay with (so a Component with zero events
            still shows up in ``pending_components``). When ``None``,
            pulled from the on-disk spec.

    Raises:
        ResumeError: when the session dir is missing required
            artifacts (no spec.json, no journal). The CLI surfaces
            the message verbatim.
    """
    session_dir = Path(session_dir)
    if not session_dir.exists():
        raise ResumeError(f"paused session dir does not exist: {session_dir}")

    spec_path = session_dir / "spec" / "spec.json"
    if not spec_path.exists():
        raise ResumeError(
            f"paused session has no spec.json (looked at {spec_path}); "
            "cannot resume — re-run fresh."
        )

    # Spec hash is computed against bytes on disk so we capture exactly
    # what the prior run worked from — independent of canonicalization.
    spec_hash = _hash_file(spec_path)

    # Pull the id set the runner cares about. The spec is the source of
    # truth for "what should this run produce"; the journal is the source
    # of truth for "what already got produced".
    if component_ids is None:
        try:
            spec = load_spec(spec_path)
        except Exception as exc:  # noqa: BLE001
            raise ResumeError(
                f"failed to load spec from {spec_path}: {type(exc).__name__}: {exc}"
            ) from exc
        ids = _all_unit_ids(spec)
    else:
        ids = frozenset(str(c) for c in component_ids)

    state = replay(session_dir, slice_ids=ids, project_dir=project_dir)
    landed, pending = _classify_components(state, ids)

    prior_cost, prior_wall, halted_reason = _read_prior_accounting(session_dir)

    session_id = session_dir.name

    return ResumePlan(
        session_id=session_id,
        paused_session_dir=session_dir,
        spec_hash=spec_hash,
        landed_components=frozenset(landed),
        pending_components=frozenset(pending),
        audit_finished=bool(
            state.audit_finished and state.audit_verdict
        ),
        audit_verdict=state.audit_verdict,
        prior_cost_usd=prior_cost,
        prior_wall_s=prior_wall,
        halted_reason=halted_reason,
        unreconciled_landed_ids=tuple(state.unreconciled_landed_ids),
    )


def verify_spec_hash_matches(plan: ResumePlan, current_spec_path: Path) -> None:
    """Raise ``ResumeError`` if the on-disk spec has drifted since pause.

    ``plan.spec_hash`` was captured at ``plan_resume`` time. This
    function re-hashes the spec the runner is about to operate on and
    compares. v1 policy: refuse on mismatch unless the caller has
    already decided ``--force`` is in effect (in which case the caller
    skips this call entirely).
    """
    current_hash = _hash_file(current_spec_path)
    if current_hash == plan.spec_hash:
        return
    raise ResumeError(
        f"Spec at {current_spec_path} was modified after the run paused "
        f"(hash changed: {plan.spec_hash[:SPEC_HASH_LENGTH]} → "
        f"{current_hash[:SPEC_HASH_LENGTH]}). Cannot surgically resume — "
        "Components built against the old spec may now reference "
        "nonexistent Features. Re-run fresh, or pass --force to resume "
        "against the new spec at your own risk."
    )


# ---------------------------------------------------------------------------
# Mid-merge recovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitRecoveryReport:
    """Aggregate result of probing a project for stuck git state."""

    cleaned: bool
    detail: str = ""
    kind: str = ""


def recover_mid_merge_state_for_project(project_dir: Path) -> GitRecoveryReport:
    """Higher-level mid-merge recovery: probe the project's main worktree.

    Wraps ``otto.spec_state.recover_mid_merge_state`` and returns a
    ``GitRecoveryReport`` so the CLI can render a single-line banner.
    Per-Component worktree recovery is a follow-up — v1 only cleans the
    project root (matches what the build phase actually uses).
    """
    rec: MidMergeRecovery = recover_mid_merge_state(project_dir)
    return GitRecoveryReport(
        cleaned=bool(rec.restart_required),
        detail=rec.detail,
        kind=rec.kind,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> str:
    """Return the sha256 hex digest of ``path``'s bytes."""
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _all_unit_ids(spec: Spec) -> frozenset[str]:
    """Collect every Group + Component id from a spec.

    Resume needs both: a Group is a Feature-bearing dispatch unit, a
    Component is a Feature-less infra unit, and the journal namespaces
    them in the same id space. ``replay()`` accepts the union and seeds
    a SliceState entry per id so PENDING ids surface in ``state.slices``
    even when no events have fired.
    """
    ids: set[str] = set()
    for group in (spec.groups or []):
        ids.add(group.id)
    for comp in (getattr(spec, "components", None) or []):
        ids.add(comp.id)
    return frozenset(ids)


def _classify_components(
    state: RunState, all_ids: Iterable[str]
) -> tuple[set[str], set[str]]:
    """Project a ``RunState`` onto (landed, pending) sets.

    Classification rules (from docs/i2p-resume-design.md §2):

    * LANDED + reconciled-with-git → ``landed``.
    * REDUNDANT (Pattern A: produced no diff but checks passed) →
      ``landed`` (already counted; do NOT re-dispatch).
    * BLOCKED → omitted from both sets — terminal failure.
      v1 does not auto-rebuild blocked Components; the operator can
      pass ``--force`` to retry.
    * Unreconciled LANDED (LANDED claim whose commit hash is missing
      from git) → ``pending`` (the journal lied; rebuild is safe).
    * Anything else (BUILDING, CHECKING, FAILED, ELIGIBLE, MERGING,
      PENDING) → ``pending``.
    """
    unreconciled = set(state.unreconciled_landed_ids)
    landed: set[str] = set()
    pending: set[str] = set()
    for cid in all_ids:
        slice_state = state.slices.get(cid)
        phase = slice_state.phase if slice_state is not None else ""
        if cid in unreconciled:
            pending.add(cid)
        elif phase == LANDED or phase == REDUNDANT:
            landed.add(cid)
        elif phase == BLOCKED:
            # Terminal failure — neither landed nor a candidate for
            # automatic re-dispatch.
            continue
        else:
            # PENDING / BUILDING / CHECKING / FAILED / ELIGIBLE / MERGING
            pending.add(cid)
    return landed, pending


def _read_prior_accounting(session_dir: Path) -> tuple[float, float, str]:
    """Pull (cost_usd, wall_s, halted_reason) from session_dir/summary.json.

    Falls back to ``checkpoint.json`` if summary is missing — both
    encode the same fields under different writers (lifecycle vs the
    legacy checkpoint module). Returns zeros + empty string on any read
    error; resume without prior cost is honest enough.
    """
    cost = 0.0
    wall = 0.0
    halted = ""

    summary_path = session_dir / "summary.json"
    if summary_path.exists():
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("resume: cannot read %s: %s", summary_path, exc)
        else:
            cost = float(data.get("cost_usd") or 0.0)
            wall = float(data.get("duration_s") or 0.0)
            halted = str(data.get("halted_reason") or "")

    # Checkpoint may have richer numbers if summary is missing or
    # the prior run halted before the lifecycle writer ran.
    checkpoint_path = session_dir / "checkpoint.json"
    if checkpoint_path.exists():
        try:
            data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("resume: cannot read %s: %s", checkpoint_path, exc)
        else:
            if cost == 0.0:
                cost = float(data.get("total_cost_so_far") or 0.0)
            if not halted:
                halted = str(data.get("halted_reason") or "")

    return cost, wall, halted


__all__ = [
    "GitRecoveryReport",
    "ResumeError",
    "ResumePlan",
    "plan_resume",
    "recover_mid_merge_state_for_project",
    "resolve_paused_session",
    "verify_spec_hash_matches",
]
