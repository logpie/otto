"""Tests for ``otto.resume`` — resume planner for the i2p stack.

Covers (per docs/i2p-resume-design.md §10):

* ``plan_resume`` correctly classifies a fixture session with mixed
  landed/pending Components.
* ``recover_mid_merge_state_for_project`` detects + cleans
  ``.git/REBASE_HEAD``.
* ``verify_spec_hash_matches`` raises on mismatch.

No LLM cost; no real subprocess calls beyond the git invocations
``recover_mid_merge_state`` already issues against a temp repo.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from otto.checkpoint import write_checkpoint
from otto.resume import (
    ResumeError,
    plan_resume,
    recover_mid_merge_state_for_project,
    verify_spec_hash_matches,
)
from otto.spec_compile import Component, Group, Spec, persist_spec
from otto.spec_state import emit


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@otto.local"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Otto Tester"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "commit.gpgsign", "false"],
        check=True,
    )
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _seed_session(tmp_path: Path) -> tuple[Path, Spec]:
    """Create a session dir with a tiny spec on disk. Returns (session_dir, spec)."""
    session_dir = tmp_path / "otto_logs" / "sessions" / "2026-05-04-120000-abc123"
    spec_dir = session_dir / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = Spec(
        intent="tiny webapp",
        groups=[
            Group(id="g-a", name="A"),
            Group(id="g-b", name="B"),
        ],
        components=[
            Component(id="c-x", name="Comp X"),
        ],
    )
    spec_path = spec_dir / "spec.json"
    persist_spec(spec, spec_path, allow_initial=True)
    return session_dir, spec


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# plan_resume — classification
# ---------------------------------------------------------------------------


def test_plan_resume_classifies_landed_pending(tmp_path: Path) -> None:
    """g-a is LANDED, g-b is FAILED (mid-flight), c-x untouched (PENDING)."""
    session_dir, spec = _seed_session(tmp_path)
    # g-a → built and landed.
    emit(session_dir, "group.started", group_id="g-a")
    emit(session_dir, "group.merge.eligible", group_id="g-a")
    emit(session_dir, "group.merge.landed", group_id="g-a")
    # g-b → started + failed; never landed.
    emit(session_dir, "group.started", group_id="g-b")
    emit(session_dir, "group.attempt.failed", group_id="g-b", detail="oops")
    # c-x has no events.

    plan = plan_resume(session_dir)

    assert "g-a" in plan.landed_components
    assert "g-b" in plan.pending_components
    assert "c-x" in plan.pending_components
    # No audit events → audit_finished is False.
    assert plan.audit_finished is False
    # spec hash is non-empty (sha256 hex)
    assert len(plan.spec_hash) == 64
    assert plan.session_id == session_dir.name
    assert plan.paused_session_dir == session_dir


def test_plan_resume_treats_redundant_as_landed(tmp_path: Path) -> None:
    """REDUNDANT (Pattern A: no diff) counts as landed — do not re-dispatch."""
    session_dir, _ = _seed_session(tmp_path)
    emit(session_dir, "group.started", group_id="g-a")
    emit(session_dir, "group.merge.eligible", group_id="g-a")
    emit(session_dir, "group.merge.redundant", group_id="g-a")

    plan = plan_resume(session_dir)
    assert "g-a" in plan.landed_components


def test_plan_resume_audit_finished_with_verdict(tmp_path: Path) -> None:
    """audit.finished + non-empty verdict short-circuits the audit phase."""
    session_dir, _ = _seed_session(tmp_path)
    emit(session_dir, "audit.started")
    emit(session_dir, "audit.finished", detail="all good", verdict="passed")

    plan = plan_resume(session_dir)
    assert plan.audit_finished is True
    assert plan.audit_verdict == "passed"


def test_plan_resume_blocked_components_not_in_either_set(tmp_path: Path) -> None:
    """BLOCKED is terminal — neither auto-rebuilt nor counted as landed."""
    session_dir, _ = _seed_session(tmp_path)
    emit(session_dir, "group.started", group_id="g-a")
    emit(session_dir, "group.blocked", group_id="g-a", detail="exhausted retries")

    plan = plan_resume(session_dir)
    assert "g-a" not in plan.landed_components
    assert "g-a" not in plan.pending_components


def test_plan_resume_reads_prior_cost_from_summary(tmp_path: Path) -> None:
    """prior_cost_usd is hydrated from summary.json for cost-carry."""
    session_dir, _ = _seed_session(tmp_path)
    summary = session_dir / "summary.json"
    summary.write_text(json.dumps({"cost_usd": 4.25, "duration_s": 120.0}))

    plan = plan_resume(session_dir)
    assert plan.prior_cost_usd == pytest.approx(4.25)
    assert plan.prior_wall_s == pytest.approx(120.0)


def test_plan_resume_missing_session_raises(tmp_path: Path) -> None:
    with pytest.raises(ResumeError, match="does not exist"):
        plan_resume(tmp_path / "nope")


def test_plan_resume_missing_spec_raises(tmp_path: Path) -> None:
    sess = tmp_path / "sess"
    sess.mkdir()
    with pytest.raises(ResumeError, match="no spec.json"):
        plan_resume(sess)


# ---------------------------------------------------------------------------
# verify_spec_hash_matches
# ---------------------------------------------------------------------------


def test_verify_spec_hash_matches_passes_on_identical_bytes(tmp_path: Path) -> None:
    session_dir, _ = _seed_session(tmp_path)
    plan = plan_resume(session_dir)
    # No mutation between plan_resume and verify → hashes match.
    verify_spec_hash_matches(plan, session_dir / "spec" / "spec.json")


def test_verify_spec_hash_matches_raises_on_mutation(tmp_path: Path) -> None:
    session_dir, _ = _seed_session(tmp_path)
    plan = plan_resume(session_dir)
    spec_path = session_dir / "spec" / "spec.json"
    spec_path.write_text(spec_path.read_text() + "\n# tampered\n")
    with pytest.raises(ResumeError, match="modified after the run paused"):
        verify_spec_hash_matches(plan, spec_path)


def test_plan_resume_uses_checkpoint_spec_hash_for_paused_drift(
    tmp_path: Path,
) -> None:
    """Edits made before invoking --resume must not become the baseline."""
    session_dir, _ = _seed_session(tmp_path)
    spec_path = session_dir / "spec" / "spec.json"
    original_hash = _sha256(spec_path)
    write_checkpoint(
        tmp_path,
        run_id=session_dir.name,
        command="build",
        status="paused",
        phase="i2p",
        spec_path=str(spec_path),
        spec_hash=original_hash,
    )

    payload = json.loads(spec_path.read_text(encoding="utf-8"))
    payload["intent"] = "tampered while paused"
    spec_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    plan = plan_resume(session_dir)
    assert plan.spec_hash == original_hash
    with pytest.raises(ResumeError, match="modified after the run paused"):
        verify_spec_hash_matches(plan, spec_path)


# ---------------------------------------------------------------------------
# recover_mid_merge_state_for_project
# ---------------------------------------------------------------------------


def test_recover_mid_merge_state_detects_rebase_head(tmp_path: Path) -> None:
    """A REBASE_HEAD file → cleaned=True, kind='rebase'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    # Manufacture a stuck-rebase state. We don't need a real conflict —
    # the existence of REBASE_HEAD is what recover_mid_merge_state probes.
    git_dir = repo / ".git"
    rebase_merge = git_dir / "rebase-merge"
    rebase_merge.mkdir()
    (rebase_merge / "head-name").write_text("refs/heads/main")
    (git_dir / "REBASE_HEAD").write_text("dummy")

    rec = recover_mid_merge_state_for_project(repo)
    assert rec.cleaned is True
    assert rec.kind == "rebase"


def test_recover_mid_merge_state_clean_repo_returns_no_op(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    rec = recover_mid_merge_state_for_project(repo)
    assert rec.cleaned is False
    assert rec.kind == ""


# ---------------------------------------------------------------------------
# Round-3 audit gap 3 — A6/A7 composition with resume
# ---------------------------------------------------------------------------


def test_plan_resume_picks_up_paused_by_user(tmp_path: Path) -> None:
    """A7: a session with a trailing `run.paused_by_user` event surfaces
    on `ResumePlan.paused_by_user`. Resume of an explicitly resumed
    session does not (the resume event clears the flag)."""
    session_dir, _ = _seed_session(tmp_path)
    emit(session_dir, "run.paused_by_user", detail="operator clicked Pause")

    plan = plan_resume(session_dir)
    assert plan.paused_by_user is True

    # After a `run.resumed_by_user`, the flag clears.
    emit(session_dir, "run.resumed_by_user", detail="operator clicked Resume")
    plan2 = plan_resume(session_dir)
    assert plan2.paused_by_user is False


def test_plan_resume_picks_up_prior_invalidations(tmp_path: Path) -> None:
    """A6: trailing `group.invalidated_by_spec_edit` events with no
    terminal phase event after them surface on
    `ResumePlan.prior_invalidated_group_ids`. A subsequent
    `group.merge.landed` clears that Group from the set."""
    session_dir, _ = _seed_session(tmp_path)
    emit(session_dir, "group.started", group_id="g-a")
    emit(session_dir, "group.invalidated_by_spec_edit", group_id="g-a",
         detail="feature_ids changed")
    emit(session_dir, "group.invalidated_by_spec_edit", group_id="g-b",
         detail="feature_ids changed")

    plan = plan_resume(session_dir)
    assert plan.prior_invalidated_group_ids == frozenset({"g-a", "g-b"})

    # Once g-a lands, it falls out of the set; g-b stays.
    emit(session_dir, "group.merge.landed", group_id="g-a", detail="abcd1234")
    plan2 = plan_resume(session_dir)
    assert plan2.prior_invalidated_group_ids == frozenset({"g-b"})


def test_plan_resume_invalidations_default_empty_when_clean(
    tmp_path: Path,
) -> None:
    """No invalidation events → empty frozenset, paused flag False."""
    session_dir, _ = _seed_session(tmp_path)
    emit(session_dir, "group.started", group_id="g-a")
    emit(session_dir, "group.merge.landed", group_id="g-a", detail="ff00")

    plan = plan_resume(session_dir)
    assert plan.prior_invalidated_group_ids == frozenset()
    assert plan.paused_by_user is False
