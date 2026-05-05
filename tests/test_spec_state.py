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
    LANDED,
    MERGING,
    MidMergeRecovery,
    PENDING,
    append_event,
    emit,
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
