"""Unit tests for otto.spec_state — Step 3 of the intent-to-product plan."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from otto.spec_state import (
    BLOCKED,
    BUILDING,
    CHECKING,
    ELIGIBLE,
    EVENT_KINDS,
    Event,
    FAILED,
    INVALIDATED,
    LANDED,
    MERGING,
    MidMergeRecovery,
    PENDING,
    append_event,
    aborted_group_ids,
    emit,
    is_run_paused_by_user,
    iter_events,
    journal_path,
    recover_mid_merge_state,
    replay,
)


# ---------------------------------------------------------------------------
# Round-trip per event kind
# ---------------------------------------------------------------------------


def test_append_and_iter_roundtrips_every_event_kind(tmp_path: Path) -> None:
    for kind in EVENT_KINDS:
        emit(
            tmp_path,
            kind,
            group_id="s1",
            check_id="c1" if kind.startswith("slice.check.") else "",
            attempt=2 if kind == "group.attempt.failed" else 0,
            detail=f"detail-{kind}",
            verdict="passed" if kind in {"audit.finished", "run.finished"} else None,
        )

    seen = list(iter_events(tmp_path))
    assert [e.kind for e in seen] == list(EVENT_KINDS)
    assert all(e.ts.endswith("Z") for e in seen)
    # extra fields round-trip
    by_kind = {e.kind: e for e in seen}
    assert by_kind["group.attempt.failed"].attempt == 2
    assert by_kind["audit.finished"].extra.get("verdict") == "passed"


def test_append_event_rejects_unknown_kind(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        append_event(
            tmp_path,
            Event(ts="2026-05-03T00:00:00Z", kind="not.real", group_id="s1"),
        )


def test_iter_events_skips_malformed_lines(tmp_path: Path) -> None:
    target = journal_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"ts": "2026-05-03T00:00:00Z", "kind": "group.started", "slice_id": "s1"})
        + "\n"
        + "this is not json\n"
        + json.dumps({"ts": "2026-05-03T00:01:00Z", "kind": "group.merge.landed", "slice_id": "s1"})
        + "\n",
        encoding="utf-8",
    )
    kinds = [e.kind for e in iter_events(tmp_path)]
    assert kinds == ["group.started", "group.merge.landed"]


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def test_replay_empty_journal_returns_pending_for_seeded_slices(tmp_path: Path) -> None:
    state = replay(tmp_path, group_ids=["a", "b"])
    assert state.group_state("a").phase == PENDING
    assert state.group_state("b").phase == PENDING
    assert not state.audit_started
    assert not state.run_finished


def test_replay_advances_phase_through_lifecycle(tmp_path: Path) -> None:
    emit(tmp_path, "group.started", group_id="s1")
    emit(tmp_path, "group.check.started", group_id="s1", check_id="c1")
    emit(tmp_path, "group.check.finished", group_id="s1", check_id="c1", detail="passed")
    emit(tmp_path, "group.merge.eligible", group_id="s1")
    emit(tmp_path, "group.merge.started", group_id="s1")
    emit(tmp_path, "group.merge.landed", group_id="s1")

    state = replay(tmp_path)
    assert state.group_state("s1").phase == LANDED


def test_replay_handles_concurrent_slices_in_different_phases(tmp_path: Path) -> None:
    emit(tmp_path, "group.started", group_id="a")
    emit(tmp_path, "group.started", group_id="b")
    emit(tmp_path, "group.check.started", group_id="a", check_id="ac")
    emit(tmp_path, "group.merge.eligible", group_id="b")
    emit(tmp_path, "group.attempt.failed", group_id="a", detail="check timed out")

    state = replay(tmp_path)
    assert state.group_state("a").phase == FAILED
    assert state.group_state("a").attempts == 1
    assert state.group_state("a").last_failure == "check timed out"
    assert state.group_state("b").phase == ELIGIBLE


def test_replay_marks_blocked_when_slice_blocks(tmp_path: Path) -> None:
    emit(tmp_path, "group.started", group_id="x")
    for i in range(3):
        emit(tmp_path, "group.attempt.failed", group_id="x", detail=f"attempt {i}")
    emit(tmp_path, "group.blocked", group_id="x", detail="retries exhausted")

    state = replay(tmp_path)
    assert state.group_state("x").phase == BLOCKED
    assert state.group_state("x").attempts == 3


def test_replay_tracks_audit_and_run_verdicts(tmp_path: Path) -> None:
    emit(tmp_path, "audit.started")
    emit(tmp_path, "audit.finished", verdict="passed")
    emit(tmp_path, "run.finished", verdict="passed")

    state = replay(tmp_path)
    assert state.audit_started
    assert state.audit_finished
    assert state.audit_verdict == "passed"
    assert state.run_finished
    assert state.run_verdict == "passed"


def test_replay_resume_with_one_slice_per_phase(tmp_path: Path) -> None:
    """Mid-run snapshot: every slice in a different phase, replay reflects all of them."""
    # building
    emit(tmp_path, "group.started", group_id="build")
    # checking
    emit(tmp_path, "group.started", group_id="check")
    emit(tmp_path, "group.check.started", group_id="check", check_id="c1")
    # failed (one retry)
    emit(tmp_path, "group.started", group_id="fail")
    emit(tmp_path, "group.attempt.failed", group_id="fail", detail="bad")
    # eligible
    emit(tmp_path, "group.started", group_id="elig")
    emit(tmp_path, "group.merge.eligible", group_id="elig")
    # merging
    emit(tmp_path, "group.started", group_id="merge")
    emit(tmp_path, "group.merge.eligible", group_id="merge")
    emit(tmp_path, "group.merge.started", group_id="merge")
    # landed
    emit(tmp_path, "group.started", group_id="land")
    emit(tmp_path, "group.merge.eligible", group_id="land")
    emit(tmp_path, "group.merge.started", group_id="land")
    emit(tmp_path, "group.merge.landed", group_id="land")

    state = replay(tmp_path)
    assert state.group_state("build").phase == BUILDING
    assert state.group_state("check").phase == CHECKING
    assert state.group_state("fail").phase == FAILED
    assert state.group_state("fail").attempts == 1
    assert state.group_state("elig").phase == ELIGIBLE
    assert state.group_state("merge").phase == MERGING
    assert state.group_state("land").phase == LANDED


# ---------------------------------------------------------------------------
# Joint A6 (mid-build spec edit) + A7 (operator pause/abort) coexistence
# ---------------------------------------------------------------------------
#
# A6 and A7 emit different event kinds into the same spec-state.jsonl
# journal. They don't share state, but the journal must compose them
# correctly: a single run can be paused, have a group aborted, accept
# a spec edit that invalidates another group, and then resume — and
# replay() must derive a sane RunState from the merged stream.
#
# Round-3 audit gap-2: no test exercised both event families in one
# journal. This test does.


def test_replay_composes_a6_spec_edit_and_a7_pause_abort_in_same_journal(
    tmp_path: Path,
) -> None:
    # Both groups start building.
    emit(tmp_path, "group.started", group_id="G1")
    emit(tmp_path, "group.started", group_id="G2")

    # A7: operator pauses the run, aborts G1.
    emit(tmp_path, "run.paused_by_user", detail="operator paused")
    emit(tmp_path, "group.aborted_by_user", group_id="G1", detail="operator abort")

    # A6: while paused, operator edits the spec; G2's contributions
    # change so the runner emits an invalidation event for G2.
    emit(tmp_path, "spec.edited", detail="user trimmed feature F2")
    emit(
        tmp_path,
        "group.invalidated_by_spec_edit",
        group_id="G2",
        detail="F2 dropped",
    )

    # A7: operator resumes the run.
    emit(tmp_path, "run.resumed_by_user", detail="operator resumed")

    # All five non-trivial events plus the two `group.started` should be
    # preserved, in order, with no exceptions raised during replay.
    kinds = [e.kind for e in iter_events(tmp_path)]
    assert kinds == [
        "group.started",
        "group.started",
        "run.paused_by_user",
        "group.aborted_by_user",
        "spec.edited",
        "group.invalidated_by_spec_edit",
        "run.resumed_by_user",
    ]

    state = replay(tmp_path, group_ids=["G1", "G2"])
    # A7 — group abort should land G1 in BLOCKED (operator-terminal).
    assert state.group_state("G1").phase == BLOCKED
    # A6 — spec-edit invalidation should land G2 in INVALIDATED so the
    # runner re-dispatches it under the new spec.
    assert state.group_state("G2").phase == INVALIDATED
    # A7 — pause then resume: pause flag should be cleared.
    assert is_run_paused_by_user(tmp_path) is False
    # A7 — the side-channel set used by the merge queue should still
    # see G1 as aborted (sticky for the rest of the run).
    assert "G1" in aborted_group_ids(tmp_path)
    # Run isn't finished — no run.finished event was emitted.
    assert not state.run_finished


def test_replay_a7_pause_without_resume_leaves_run_paused(tmp_path: Path) -> None:
    # Sanity check that the pause predicate is honestly derived from the
    # journal in the joint scenario above; if Resume were never emitted,
    # the run should still register as paused.
    emit(tmp_path, "group.started", group_id="G1")
    emit(tmp_path, "run.paused_by_user", detail="operator paused")
    emit(tmp_path, "group.aborted_by_user", group_id="G1", detail="operator abort")
    emit(tmp_path, "spec.edited", detail="user trimmed feature F2")

    state = replay(tmp_path, group_ids=["G1"])
    assert state.group_state("G1").phase == BLOCKED
    assert is_run_paused_by_user(tmp_path) is True


# ---------------------------------------------------------------------------
# Mid-merge git recovery
# ---------------------------------------------------------------------------


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@otto.local"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Otto Tester"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "commit.gpgsign", "false"], check=True)
    (path / "README.md").write_text("a\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def test_recover_mid_merge_state_returns_empty_for_clean_worktree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    recovery = recover_mid_merge_state(tmp_path)
    assert recovery.kind == ""
    assert not recovery.restart_required


def test_recover_mid_merge_state_aborts_a_stuck_merge(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    # Fabricate a stuck merge state by writing MERGE_HEAD directly. We do
    # not rely on a real merge conflict — the recovery path must trigger
    # off the marker file.
    git_dir = tmp_path / ".git"
    head_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (git_dir / "MERGE_HEAD").write_text(head_sha + "\n")
    (git_dir / "MERGE_MSG").write_text("simulated merge\n")

    recovery = recover_mid_merge_state(tmp_path)
    assert recovery.kind == "merge"
    assert recovery.restart_required
    # Marker should be gone after the abort
    assert not (git_dir / "MERGE_HEAD").exists()


def test_recover_mid_merge_state_aborts_a_stuck_rebase(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    git_dir = tmp_path / ".git"
    rebase_dir = git_dir / "rebase-merge"
    rebase_dir.mkdir()
    head_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (rebase_dir / "head-name").write_text("refs/heads/main\n")
    (rebase_dir / "onto").write_text(head_sha + "\n")
    (rebase_dir / "orig-head").write_text(head_sha + "\n")
    (git_dir / "REBASE_HEAD").write_text(head_sha + "\n")

    recovery = recover_mid_merge_state(tmp_path)
    assert recovery.kind == "rebase"
    assert recovery.restart_required


def test_recover_mid_merge_state_handles_non_git_dir(tmp_path: Path) -> None:
    # No .git/ — recovery should be a no-op.
    recovery = recover_mid_merge_state(tmp_path)
    assert isinstance(recovery, MidMergeRecovery)
    assert recovery.kind == ""


# ---------------------------------------------------------------------------
# Round-3 audit gaps 1 + 5 — phase mapping + Event.feature_id field
# ---------------------------------------------------------------------------


def test_replay_marks_aborted_group_as_blocked(tmp_path: Path) -> None:
    """Round-3 gap 1: `group.aborted_by_user` must transition the
    Group's phase to BLOCKED. Before the fix, the kind had no entry in
    `_PHASE_FOR_KIND`, so an aborted Group's phase stayed at whatever
    it was last (typically BUILDING) and replay()-derived RunState
    misclassified it.
    """
    emit(tmp_path, "group.started", group_id="g-a")
    emit(
        tmp_path, "group.aborted_by_user", group_id="g-a",
        detail="operator clicked Abort",
    )

    state = replay(tmp_path)
    assert state.group_state("g-a").phase == BLOCKED


def test_replay_ignores_run_scoped_events_without_changing_phase(
    tmp_path: Path,
) -> None:
    """Run-scoped events (run.paused_by_user, spec.review_pending, etc.)
    must not transition any single Group's phase.
    """
    emit(tmp_path, "group.started", group_id="g-a")
    # Sprinkle every documented run-scoped event between Group events.
    emit(tmp_path, "run.paused_by_user", detail="operator paused")
    emit(tmp_path, "run.resumed_by_user", detail="operator resumed")
    emit(tmp_path, "spec.review.opened")
    emit(tmp_path, "spec.review_pending")
    emit(tmp_path, "spec.review_approved")
    emit(tmp_path, "spec.edited")
    emit(tmp_path, "spec.approved")
    emit(tmp_path, "spec.regenerated")

    state = replay(tmp_path)
    # Group is still BUILDING; the run-scoped events did not move it.
    assert state.group_state("g-a").phase == BUILDING


def test_event_feature_id_round_trips_through_emit_and_replay(
    tmp_path: Path,
) -> None:
    """Round-3 gap 5: Event.feature_id is optional but persists across
    emit + iter_events. Existing replay() consumers ignore the field.
    """
    emit(
        tmp_path,
        "group.check.finished",
        group_id="g-a",
        check_id="c1",
        detail="passed",
        feature_id="md-render",
    )
    events = list(iter_events(tmp_path))
    assert len(events) == 1
    assert events[0].feature_id == "md-render"

    # Default empty for events without explicit feature_id.
    emit(tmp_path, "group.merge.landed", group_id="g-a", detail="abcd")
    events = list(iter_events(tmp_path))
    landed = [e for e in events if e.kind == "group.merge.landed"]
    assert landed[0].feature_id == ""
