"""Tests for otto/merge_queue.py — eligibility-gated FIFO merge.

Coverage:
- eligible_candidates: FIFO within deps, exclusion of landed/blocked
- run_merge_queue: happy path (slice + cross-slice checks pass → LANDED),
  cross-slice failure with no agent → BLOCKED, cross-slice failure with
  agent that fixes → LANDED via repair, agent that always fails →
  BLOCKED via repair retries exhausted, agent crash recovery
- Integration commit semantics: idempotent re-run, no-op if no changes
- Integration with build.py: passing_slice_ids feeds eligible_candidates
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from otto.build import (
    BuildAgentInput,
    BuildAgentOutput,
    BuildResult,
    SliceResult,
    SliceStatus,
)
from otto.merge_queue import (
    MergeBudget,
    MergeStatus,
    eligible_candidates,
    passing_slice_ids,
    run_merge_queue,
)
from otto.spec_compile import (
    RepoTestCheck,
    Slice,
    Spec,
    StateInvariant,
    StructureDecisions,
)


def _spec(slices: list[Slice], cross_checks=None) -> Spec:
    return Spec(
        intent="test intent",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        slices=slices,
        cross_slice_checks=cross_checks or [],
    )


def _init_git(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    # Mirror production: session dirs and otto_logs/ are ignored by git.
    (repo / ".gitignore").write_text("_session/\notto_logs/\n", encoding="utf-8")
    (repo / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", ".gitkeep", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init", "--no-verify"], cwd=repo, check=True)


def _passing_check() -> RepoTestCheck:
    return RepoTestCheck(command=("python", "-c", "print('ok')"), timeout_s=10)


def _failing_check() -> RepoTestCheck:
    return RepoTestCheck(command=("python", "-c", "import sys; sys.exit(1)"), timeout_s=10)


def _passing_state_invariant(predicate: str) -> StateInvariant:
    return StateInvariant(description="cross-slice", expression=predicate)


# ---------------------------------------------------------------------------
# eligible_candidates
# ---------------------------------------------------------------------------


def test_eligible_candidates_returns_passing_with_satisfied_deps() -> None:
    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[]),
            Slice(id="s2", title="y", deps=["s1"], owned_paths=[], tasks=[], checks=[]),
            Slice(id="s3", title="z", deps=["s1"], owned_paths=[], tasks=[], checks=[]),
        ]
    )
    eligible = eligible_candidates(
        spec, passing_ids={"s1", "s2", "s3"}, landed_ids={"s1"}
    )
    assert [s.id for s in eligible] == ["s2", "s3"]


def test_eligible_candidates_excludes_already_landed() -> None:
    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[]),
        ]
    )
    eligible = eligible_candidates(
        spec, passing_ids={"s1"}, landed_ids={"s1"}
    )
    assert eligible == []


def test_eligible_candidates_excludes_blocked() -> None:
    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[]),
        ]
    )
    eligible = eligible_candidates(
        spec, passing_ids={"s1"}, landed_ids=set(), blocked_ids={"s1"}
    )
    assert eligible == []


def test_eligible_candidates_holds_back_when_dep_unlanded() -> None:
    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[]),
            Slice(id="s2", title="y", deps=["s1"], owned_paths=[], tasks=[], checks=[]),
        ]
    )
    # s2 passing but s1 not landed → s2 not eligible
    eligible = eligible_candidates(
        spec, passing_ids={"s1", "s2"}, landed_ids=set()
    )
    assert [s.id for s in eligible] == ["s1"]


def test_eligible_candidates_fifo_within_eligible_per_spec_order() -> None:
    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[]),
            Slice(id="s2", title="y", deps=[], owned_paths=[], tasks=[], checks=[]),
        ]
    )
    eligible = eligible_candidates(spec, passing_ids={"s1", "s2"}, landed_ids=set())
    assert [s.id for s in eligible] == ["s1", "s2"]


# ---------------------------------------------------------------------------
# passing_slice_ids
# ---------------------------------------------------------------------------


def test_passing_slice_ids_extracts_passing_only(tmp_path: Path) -> None:
    build_result = BuildResult(
        spec_session_dir=tmp_path,
        slice_results=[
            SliceResult(slice_id="a", status=SliceStatus.PASSING, attempts=1, branch="x", worktree=tmp_path),
            SliceResult(slice_id="b", status=SliceStatus.BLOCKED, attempts=3, branch="y", worktree=tmp_path),
            SliceResult(slice_id="c", status=SliceStatus.PASSING, attempts=2, branch="z", worktree=tmp_path),
        ],
    )
    assert passing_slice_ids(build_result) == ["a", "c"]


# ---------------------------------------------------------------------------
# run_merge_queue — happy path
# ---------------------------------------------------------------------------


def test_run_merge_queue_lands_single_slice_when_checks_pass(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = _spec(
        [
            Slice(
                id="s1", title="hello", deps=[], owned_paths=[], tasks=[],
                checks=[_passing_check()],
            ),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        slice_results=[
            SliceResult(
                slice_id="s1", status=SliceStatus.PASSING, attempts=1,
                branch="i2p/x/s1", worktree=tmp_path,
            ),
        ],
    )
    result = asyncio.run(
        run_merge_queue(spec, build_result, project_dir=tmp_path, session_dir=session_dir)
    )
    assert result.landed_ids == ["s1"]
    assert result.blocked_ids == []
    assert result.results[0].status == MergeStatus.LANDED
    assert result.results[0].landed_commit  # short hash present


def test_run_merge_queue_lands_in_dep_order(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = _spec(
        [
            Slice(id="s1", title="a", deps=[], owned_paths=[], tasks=[], checks=[_passing_check()]),
            Slice(id="s2", title="b", deps=["s1"], owned_paths=[], tasks=[], checks=[_passing_check()]),
            Slice(id="s3", title="c", deps=["s2"], owned_paths=[], tasks=[], checks=[_passing_check()]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        slice_results=[
            SliceResult(slice_id="s1", status=SliceStatus.PASSING, attempts=1, branch="b1", worktree=tmp_path),
            SliceResult(slice_id="s2", status=SliceStatus.PASSING, attempts=1, branch="b2", worktree=tmp_path),
            SliceResult(slice_id="s3", status=SliceStatus.PASSING, attempts=1, branch="b3", worktree=tmp_path),
        ],
    )
    result = asyncio.run(
        run_merge_queue(spec, build_result, project_dir=tmp_path, session_dir=session_dir)
    )
    assert result.landed_ids == ["s1", "s2", "s3"]


def test_run_merge_queue_runs_cross_slice_checks(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    # Cross-slice invariant: marker.txt must exist
    (tmp_path / "marker.txt").write_text("ok", encoding="utf-8")
    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[_passing_check()]),
        ],
        cross_checks=[_passing_state_invariant("exists('marker.txt')")],
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        slice_results=[
            SliceResult(slice_id="s1", status=SliceStatus.PASSING, attempts=1, branch="b1", worktree=tmp_path),
        ],
    )
    result = asyncio.run(
        run_merge_queue(spec, build_result, project_dir=tmp_path, session_dir=session_dir)
    )
    assert result.landed_ids == ["s1"]
    assert len(result.results[0].cross_slice_evidence) == 1
    assert result.results[0].cross_slice_evidence[0].passed is True


# ---------------------------------------------------------------------------
# run_merge_queue — blocking
# ---------------------------------------------------------------------------


def test_run_merge_queue_blocks_on_cross_slice_failure_without_agent(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[_passing_check()]),
        ],
        cross_checks=[_passing_state_invariant("exists('does-not-exist.txt')")],
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        slice_results=[
            SliceResult(slice_id="s1", status=SliceStatus.PASSING, attempts=1, branch="b1", worktree=tmp_path),
        ],
    )
    result = asyncio.run(
        run_merge_queue(spec, build_result, project_dir=tmp_path, session_dir=session_dir)
    )
    assert result.landed_ids == []
    assert result.blocked_ids == ["s1"]
    assert "cross-slice" in result.results[0].failure_narrative
    assert "no build_agent" in result.results[0].failure_narrative


def test_run_merge_queue_repairs_via_agent_then_lands(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    # Cross-slice check: needs marker.txt to exist. Initially absent.
    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[], owned_paths=["marker.txt"], tasks=[], checks=[_passing_check()]),
        ],
        cross_checks=[_passing_state_invariant("exists('marker.txt')")],
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        slice_results=[
            SliceResult(slice_id="s1", status=SliceStatus.PASSING, attempts=1, branch="b1", worktree=tmp_path),
        ],
    )

    async def repair_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        # Repair by creating the missing marker.
        (input_.worktree / "marker.txt").write_text("ok", encoding="utf-8")
        return BuildAgentOutput(succeeded=True, cost_usd=0.05)

    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            build_agent=repair_agent,
        )
    )
    assert result.landed_ids == ["s1"]
    assert result.results[0].repair_attempts == 1
    assert result.results[0].cost_usd > 0


def test_run_merge_queue_blocks_when_repair_retries_exhausted(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[_passing_check()]),
        ],
        cross_checks=[_passing_state_invariant("False")],  # always fails
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        slice_results=[
            SliceResult(slice_id="s1", status=SliceStatus.PASSING, attempts=1, branch="b1", worktree=tmp_path),
        ],
    )

    async def useless_agent(_input: BuildAgentInput) -> BuildAgentOutput:
        return BuildAgentOutput(succeeded=True)

    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            build_agent=useless_agent,
            budget=MergeBudget(per_slice_repair_retries=2),
        )
    )
    assert result.blocked_ids == ["s1"]
    r = result.results[0]
    assert r.repair_attempts == 2
    assert "repair retries exhausted" in r.failure_narrative


def test_run_merge_queue_handles_agent_crash_during_repair(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[_passing_check()]),
        ],
        cross_checks=[_passing_state_invariant("exists('marker.txt')")],
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        slice_results=[
            SliceResult(slice_id="s1", status=SliceStatus.PASSING, attempts=1, branch="b1", worktree=tmp_path),
        ],
    )
    counter = {"n": 0}

    async def crash_then_fix(input_: BuildAgentInput) -> BuildAgentOutput:
        counter["n"] += 1
        if counter["n"] == 1:
            raise RuntimeError("boom")
        # On retry: actually fix it.
        (input_.worktree / "marker.txt").write_text("ok", encoding="utf-8")
        return BuildAgentOutput(succeeded=True)

    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            build_agent=crash_then_fix,
            budget=MergeBudget(per_slice_repair_retries=3),
        )
    )
    assert result.landed_ids == ["s1"]
    assert counter["n"] >= 2


# ---------------------------------------------------------------------------
# Integration commit semantics
# ---------------------------------------------------------------------------


def test_run_merge_queue_commits_pending_changes(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    # Simulate the build agent having created an uncommitted file.
    (tmp_path / "new-file.txt").write_text("from build", encoding="utf-8")
    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[_passing_check()]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        slice_results=[
            SliceResult(slice_id="s1", status=SliceStatus.PASSING, attempts=1, branch="b1", worktree=tmp_path),
        ],
    )
    result = asyncio.run(
        run_merge_queue(spec, build_result, project_dir=tmp_path, session_dir=session_dir)
    )
    assert result.landed_ids == ["s1"]
    # Verify the new-file.txt is committed.
    log = subprocess.run(
        ["git", "log", "-1", "--name-only", "--pretty=format:%s"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    assert "i2p(s1): land slice" in log.stdout
    assert "new-file.txt" in log.stdout


def test_run_merge_queue_no_op_commit_when_no_changes(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[_passing_check()]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        slice_results=[
            SliceResult(slice_id="s1", status=SliceStatus.PASSING, attempts=1, branch="b1", worktree=tmp_path),
        ],
    )
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    result = asyncio.run(
        run_merge_queue(spec, build_result, project_dir=tmp_path, session_dir=session_dir)
    )
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    # No changes → no new commit.
    assert head_before == head_after
    # Slice still LANDED (degenerate case).
    assert result.landed_ids == ["s1"]


# ---------------------------------------------------------------------------
# Pattern D — real per-slice branches and real merges
# ---------------------------------------------------------------------------


def test_run_merge_queue_real_merge_when_slice_branch_exists(tmp_path: Path) -> None:
    """Pattern D: when the slice's branch exists in git, merge_queue does
    a real `git merge --no-ff` instead of `git add && git commit` in the
    shared worktree. The integration commit is a true merge commit
    (two parents) traceable to the slice branch.
    """
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    branch = "i2p/_session/s1"
    # Set up the slice branch with its own commit, then return to main.
    subprocess.run(["git", "checkout", "-b", branch], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "slice-work.txt").write_text("from slice", encoding="utf-8")
    subprocess.run(["git", "add", "slice-work.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "i2p(s1): build slice", "--no-verify"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[], owned_paths=["slice-work.txt"],
                  tasks=["write slice-work"], checks=[_passing_check()]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        slice_results=[
            SliceResult(slice_id="s1", status=SliceStatus.PASSING, attempts=1,
                        branch=branch, worktree=tmp_path),
        ],
    )
    # Use the same branch_for_slice formula as the production default.
    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            branch_for_slice=lambda s: branch,
        )
    )
    assert result.landed_ids == ["s1"]
    # Verify a real merge commit was created (two parents).
    head_parents = subprocess.run(
        ["git", "log", "-1", "--pretty=format:%P"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip().split()
    assert len(head_parents) == 2, f"expected merge commit (2 parents), got {head_parents}"
    # Slice's file is now on main.
    assert (tmp_path / "slice-work.txt").exists()


def test_run_merge_queue_real_merge_redundant_when_branch_empty(tmp_path: Path) -> None:
    """Pattern D: a slice branch that exists but has no commits beyond
    base_branch reports REDUNDANT (slice produced no diff).
    """
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    branch = "i2p/_session/s1"
    # Create slice branch but make NO commits on it.
    subprocess.run(["git", "checkout", "-b", branch], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[],
                  owned_paths=["expected.txt"],  # had declared work
                  tasks=["write expected.txt"],
                  checks=[_passing_check()]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        slice_results=[
            SliceResult(slice_id="s1", status=SliceStatus.PASSING, attempts=1,
                        branch=branch, worktree=tmp_path),
        ],
    )
    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            branch_for_slice=lambda s: branch,
        )
    )
    # Slice declared work but produced no diff — REDUNDANT, surfaced
    # as the over-reach diagnostic. Counts as landed for dep flow.
    assert "s1" in result.landed_ids
    assert "s1" in result.redundant_ids
    assert result.results[0].status == MergeStatus.REDUNDANT


def test_run_merge_queue_real_merge_blocks_on_conflict(tmp_path: Path) -> None:
    """Pattern D: a slice branch that conflicts with main on merge
    BLOCKS without an agent (real merge errors are surfaced, not hidden).
    """
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    # main: file with content A
    (tmp_path / "shared.txt").write_text("A", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "main A", "--no-verify"],
        cwd=tmp_path, check=True, capture_output=True,
    )

    # slice branch off an EARLIER state, modify shared.txt to B.
    subprocess.run(["git", "checkout", "-b", "i2p/_session/s1", "main~1"],
                   cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "shared.txt").write_text("B", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "i2p(s1): conflicting B", "--no-verify"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[], owned_paths=["shared.txt"],
                  tasks=["edit shared.txt"], checks=[_passing_check()]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        slice_results=[
            SliceResult(slice_id="s1", status=SliceStatus.PASSING, attempts=1,
                        branch="i2p/_session/s1", worktree=tmp_path),
        ],
    )
    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            branch_for_slice=lambda s: "i2p/_session/s1",
        )
    )
    assert result.blocked_ids == ["s1"]
    assert "merge conflict" in result.results[0].failure_narrative.lower()
    # Verify worktree is left clean (merge --abort ran).
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert status == "", f"worktree should be clean after aborted merge, got: {status}"


# ---------------------------------------------------------------------------
# Integration with build.py's BuildResult
# ---------------------------------------------------------------------------


def test_passing_slice_ids_drives_eligible_candidates(tmp_path: Path) -> None:
    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[]),
            Slice(id="s2", title="y", deps=["s1"], owned_paths=[], tasks=[], checks=[]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=tmp_path,
        slice_results=[
            SliceResult(slice_id="s1", status=SliceStatus.PASSING, attempts=1, branch="b1", worktree=tmp_path),
            SliceResult(slice_id="s2", status=SliceStatus.BLOCKED, attempts=3, branch="b2", worktree=tmp_path),
        ],
    )
    eligible = eligible_candidates(
        spec, passing_ids=passing_slice_ids(build_result), landed_ids=set()
    )
    assert [s.id for s in eligible] == ["s1"]


# ---------------------------------------------------------------------------
# B1: merge-conflict repair runs on slice branch, not base_branch
# ---------------------------------------------------------------------------


def test_merge_repair_runs_on_slice_branch_not_base(tmp_path: Path) -> None:
    """B1: when a merge conflicts and a build_agent is provided, the
    repair MUST happen on the slice's branch (so the next merge attempt
    sees the fix). Previously the repair edited base_branch, leaving
    the slice branch unchanged and producing the same conflict on the
    next loop iteration.
    """
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    # main: shared.txt = A
    (tmp_path / "shared.txt").write_text("A", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "main A", "--no-verify"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    # Slice branch off main~1, modify shared.txt to B → will conflict.
    subprocess.run(["git", "checkout", "-b", "i2p/_session/s1", "main~1"],
                   cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "shared.txt").write_text("B", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "i2p(s1): B", "--no-verify"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[], owned_paths=["shared.txt"],
                  tasks=["edit shared"], checks=[_passing_check()]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        slice_results=[
            SliceResult(slice_id="s1", status=SliceStatus.PASSING, attempts=1,
                        branch="i2p/_session/s1", worktree=tmp_path),
        ],
    )

    # Track which branch the repair agent saw.
    seen_branches: list[str] = []

    async def repair_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        # Record what branch is checked out at the moment the agent runs.
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=input_.worktree, capture_output=True, text=True, check=True,
        )
        seen_branches.append(proc.stdout.strip())
        # "Repair" by aligning shared.txt with main.
        (input_.worktree / "shared.txt").write_text("A", encoding="utf-8")
        return BuildAgentOutput(succeeded=True, cost_usd=0.01)

    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            build_agent=repair_agent,
            branch_for_slice=lambda s: "i2p/_session/s1",
        )
    )
    # B1 assertion: when the agent runs, it MUST be on the slice's branch,
    # NOT on base_branch.
    assert seen_branches, "repair agent should have been called at least once"
    assert all(b == "i2p/_session/s1" for b in seen_branches), (
        f"repair must run on slice branch; saw {seen_branches}"
    )
