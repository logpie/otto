"""Tests for otto/merge_queue.py — eligibility-gated FIFO merge.

Coverage:
- eligible_candidates: FIFO within deps, exclusion of landed/blocked
- run_merge_queue: happy path (slice + cross-slice checks pass → LANDED),
  cross-slice failure with no agent → BLOCKED, cross-slice failure with
  agent that fixes → LANDED via repair, agent that always fails →
  BLOCKED via repair retries exhausted, agent crash recovery
- Integration commit semantics: idempotent re-run, no-op if no changes
- Integration with build.py: passing_group_ids feeds eligible_candidates
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from otto.build import (
    BuildAgentInput,
    BuildAgentOutput,
    BuildResult,
    GroupResult,
    GroupStatus,
    run_build,
)
from otto.merge_queue import (
    MergeBudget,
    MergeStatus,
    eligible_candidates,
    passing_group_ids,
    run_merge_queue,
)
from otto.spec_compile import (
    RepoTestCheck,
    Group,
    Spec,
    StateInvariant,
    StructureDecisions,
)


def _spec(slices: list[Group], cross_checks=None) -> Spec:
    return Spec(
        intent="test intent",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=slices,
        cross_group_checks=cross_checks or [],
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


def _ensure_main(repo: Path) -> None:
    current = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if current != "main":
        subprocess.run(["git", "branch", "-m", current, "main"], cwd=repo, check=True)


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
            Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[]),
            Group(id="s2", name="y", dependencies=["s1"], owned_paths=[], feature_ids=[], checks=[]),
            Group(id="s3", name="z", dependencies=["s1"], owned_paths=[], feature_ids=[], checks=[]),
        ]
    )
    eligible = eligible_candidates(
        spec, passing_ids={"s1", "s2", "s3"}, landed_ids={"s1"}
    )
    assert [s.id for s in eligible] == ["s2", "s3"]


def test_eligible_candidates_excludes_already_landed() -> None:
    spec = _spec(
        [
            Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[]),
        ]
    )
    eligible = eligible_candidates(
        spec, passing_ids={"s1"}, landed_ids={"s1"}
    )
    assert eligible == []


def test_eligible_candidates_excludes_blocked() -> None:
    spec = _spec(
        [
            Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[]),
        ]
    )
    eligible = eligible_candidates(
        spec, passing_ids={"s1"}, landed_ids=set(), blocked_ids={"s1"}
    )
    assert eligible == []


def test_eligible_candidates_holds_back_when_dep_unlanded() -> None:
    spec = _spec(
        [
            Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[]),
            Group(id="s2", name="y", dependencies=["s1"], owned_paths=[], feature_ids=[], checks=[]),
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
            Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[]),
            Group(id="s2", name="y", dependencies=[], owned_paths=[], feature_ids=[], checks=[]),
        ]
    )
    eligible = eligible_candidates(spec, passing_ids={"s1", "s2"}, landed_ids=set())
    assert [s.id for s in eligible] == ["s1", "s2"]


# ---------------------------------------------------------------------------
# passing_group_ids
# ---------------------------------------------------------------------------


def test_passing_slice_ids_extracts_passing_only(tmp_path: Path) -> None:
    build_result = BuildResult(
        spec_session_dir=tmp_path,
        group_results=[
            GroupResult(group_id="a", status=GroupStatus.PASSING, attempts=1, branch="x", worktree=tmp_path),
            GroupResult(group_id="b", status=GroupStatus.BLOCKED, attempts=3, branch="y", worktree=tmp_path),
            GroupResult(group_id="c", status=GroupStatus.PASSING, attempts=2, branch="z", worktree=tmp_path),
        ],
    )
    assert passing_group_ids(build_result) == ["a", "c"]


def test_passing_group_ids_latest_result_supersedes_older_pass(tmp_path: Path) -> None:
    build_result = BuildResult(
        spec_session_dir=tmp_path,
        group_results=[
            GroupResult(group_id="a", status=GroupStatus.PASSING, attempts=1, branch="old", worktree=tmp_path),
            GroupResult(group_id="a", status=GroupStatus.BLOCKED, attempts=2, branch="new", worktree=tmp_path),
        ],
    )
    assert passing_group_ids(build_result) == []


# ---------------------------------------------------------------------------
# run_merge_queue — happy path
# ---------------------------------------------------------------------------


def test_run_merge_queue_lands_single_slice_when_checks_pass(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = _spec(
        [
            Group(
                id="s1", name="hello", dependencies=[], owned_paths=[], feature_ids=[],
                checks=[_passing_check()],
            ),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(
                group_id="s1", status=GroupStatus.PASSING, attempts=1,
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


def test_build_and_merge_use_active_branch_in_linked_worktree(tmp_path: Path) -> None:
    """Queue task worktrees must merge into their task branch, not `main`.

    `main` often remains checked out in the parent project worktree. A hard
    coded merge target of `main` therefore fails in the linked task worktree
    with "already used by worktree".
    """
    parent = tmp_path / "parent"
    task = tmp_path / "task"
    parent.mkdir()
    _init_git(parent)
    _ensure_main(parent)
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "build/task", str(task), "main"],
        cwd=parent,
        check=True,
    )

    session_dir = task / "_session"
    session_dir.mkdir()

    async def writing_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        (input_.worktree / "alpha.txt").write_text("alpha\n", encoding="utf-8")
        return BuildAgentOutput(succeeded=True, cost_usd=0.0)

    spec = _spec(
        [
            Group(
                id="alpha",
                name="Alpha",
                owned_paths=["alpha.txt"],
                feature_ids=["write alpha"],
                checks=[_passing_check()],
            )
        ]
    )

    build_result = asyncio.run(
        run_build(
            spec,
            project_dir=task,
            session_dir=session_dir,
            build_agent=writing_agent,
        )
    )
    assert build_result.all_passing
    assert build_result.base_branch == "build/task"

    merge_result = asyncio.run(
        run_merge_queue(
            spec,
            build_result,
            project_dir=task,
            session_dir=session_dir,
            build_agent=None,
        )
    )

    assert merge_result.landed_ids == ["alpha"]
    assert merge_result.blocked_ids == []
    assert (task / "alpha.txt").read_text(encoding="utf-8") == "alpha\n"
    current = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=task,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert current == "build/task"


def test_run_merge_queue_uses_latest_passing_branch_for_superseded_group(tmp_path: Path) -> None:
    _init_git(tmp_path)
    subprocess.run(["git", "checkout", "-b", "old"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "chosen.txt").write_text("old", encoding="utf-8")
    subprocess.run(["git", "add", "chosen.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "old", "--no-verify"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "new"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "chosen.txt").write_text("new", encoding="utf-8")
    subprocess.run(["git", "add", "chosen.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "new", "--no-verify"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = _spec(
        [
            Group(
                id="s1",
                name="hello",
                dependencies=[],
                owned_paths=[],
                feature_ids=[],
                checks=[_passing_check()],
            ),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(group_id="s1", status=GroupStatus.PASSING, attempts=1, branch="old", worktree=tmp_path),
            GroupResult(group_id="s1", status=GroupStatus.PASSING, attempts=2, branch="new", worktree=tmp_path),
        ],
    )

    result = asyncio.run(
        run_merge_queue(spec, build_result, project_dir=tmp_path, session_dir=session_dir)
    )

    assert result.landed_ids == ["s1"]
    assert (tmp_path / "chosen.txt").read_text(encoding="utf-8") == "new"


def test_run_merge_queue_lands_in_dep_order(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = _spec(
        [
            Group(id="s1", name="a", dependencies=[], owned_paths=[], feature_ids=[], checks=[_passing_check()]),
            Group(id="s2", name="b", dependencies=["s1"], owned_paths=[], feature_ids=[], checks=[_passing_check()]),
            Group(id="s3", name="c", dependencies=["s2"], owned_paths=[], feature_ids=[], checks=[_passing_check()]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(group_id="s1", status=GroupStatus.PASSING, attempts=1, branch="b1", worktree=tmp_path),
            GroupResult(group_id="s2", status=GroupStatus.PASSING, attempts=1, branch="b2", worktree=tmp_path),
            GroupResult(group_id="s3", status=GroupStatus.PASSING, attempts=1, branch="b3", worktree=tmp_path),
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
            Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[_passing_check()]),
        ],
        cross_checks=[_passing_state_invariant("exists('marker.txt')")],
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(group_id="s1", status=GroupStatus.PASSING, attempts=1, branch="b1", worktree=tmp_path),
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
            Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[_passing_check()]),
        ],
        cross_checks=[_passing_state_invariant("exists('does-not-exist.txt')")],
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(group_id="s1", status=GroupStatus.PASSING, attempts=1, branch="b1", worktree=tmp_path),
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
            Group(id="s1", name="x", dependencies=[], owned_paths=["marker.txt"], feature_ids=[], checks=[_passing_check()]),
        ],
        cross_checks=[_passing_state_invariant("exists('marker.txt')")],
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(group_id="s1", status=GroupStatus.PASSING, attempts=1, branch="b1", worktree=tmp_path),
        ],
    )

    seen_configs: list[dict] = []

    async def repair_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        seen_configs.append(dict(input_.config))
        # Repair by creating the missing marker.
        (input_.worktree / "marker.txt").write_text("ok", encoding="utf-8")
        return BuildAgentOutput(succeeded=True, cost_usd=0.05)

    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            build_agent=repair_agent,
            config={"provider": "codex", "_cli_overrides": {"provider": "codex"}},
        )
    )
    assert result.landed_ids == ["s1"]
    assert result.results[0].repair_attempts == 1
    assert result.results[0].cost_usd > 0
    assert seen_configs == [
        {"provider": "codex", "_cli_overrides": {"provider": "codex"}}
    ]


def test_run_merge_queue_blocks_when_repair_retries_exhausted(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = _spec(
        [
            Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[_passing_check()]),
        ],
        cross_checks=[_passing_state_invariant("False")],  # always fails
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(group_id="s1", status=GroupStatus.PASSING, attempts=1, branch="b1", worktree=tmp_path),
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
            Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[_passing_check()]),
        ],
        cross_checks=[_passing_state_invariant("exists('marker.txt')")],
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(group_id="s1", status=GroupStatus.PASSING, attempts=1, branch="b1", worktree=tmp_path),
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
            Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[_passing_check()]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(group_id="s1", status=GroupStatus.PASSING, attempts=1, branch="b1", worktree=tmp_path),
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
            Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[_passing_check()]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(group_id="s1", status=GroupStatus.PASSING, attempts=1, branch="b1", worktree=tmp_path),
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
    # Group still LANDED (degenerate case).
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
            Group(id="s1", name="x", dependencies=[], owned_paths=["slice-work.txt"],
                  feature_ids=["write slice-work"], checks=[_passing_check()]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(group_id="s1", status=GroupStatus.PASSING, attempts=1,
                        branch=branch, worktree=tmp_path),
        ],
    )
    # Use the same branch_for_group formula as the production default.
    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            branch_for_group=lambda s: branch,
        )
    )
    assert result.landed_ids == ["s1"]
    # Verify a real merge commit was created (two parents).
    head_parents = subprocess.run(
        ["git", "log", "-1", "--pretty=format:%P"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip().split()
    assert len(head_parents) == 2, f"expected merge commit (2 parents), got {head_parents}"
    # Group's file is now on main.
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
            Group(id="s1", name="x", dependencies=[],
                  owned_paths=["expected.txt"],  # had declared work
                  feature_ids=["write expected.txt"],
                  checks=[_passing_check()]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(group_id="s1", status=GroupStatus.PASSING, attempts=1,
                        branch=branch, worktree=tmp_path),
        ],
    )
    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            branch_for_group=lambda s: branch,
        )
    )
    # Group declared work but produced no diff — REDUNDANT, surfaced
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
            Group(id="s1", name="x", dependencies=[], owned_paths=["shared.txt"],
                  feature_ids=["edit shared.txt"], checks=[_passing_check()]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(group_id="s1", status=GroupStatus.PASSING, attempts=1,
                        branch="i2p/_session/s1", worktree=tmp_path),
        ],
    )
    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            branch_for_group=lambda s: "i2p/_session/s1",
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
            Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[]),
            Group(id="s2", name="y", dependencies=["s1"], owned_paths=[], feature_ids=[], checks=[]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=tmp_path,
        group_results=[
            GroupResult(group_id="s1", status=GroupStatus.PASSING, attempts=1, branch="b1", worktree=tmp_path),
            GroupResult(group_id="s2", status=GroupStatus.BLOCKED, attempts=3, branch="b2", worktree=tmp_path),
        ],
    )
    eligible = eligible_candidates(
        spec, passing_ids=passing_group_ids(build_result), landed_ids=set()
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
    # Group branch off main~1, modify shared.txt to B → will conflict.
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
            Group(id="s1", name="x", dependencies=[], owned_paths=["shared.txt"],
                  feature_ids=["edit shared"], checks=[_passing_check()]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(group_id="s1", status=GroupStatus.PASSING, attempts=1,
                        branch="i2p/_session/s1", worktree=tmp_path),
        ],
    )

    # Track which branch and mode the repair agent saw.
    seen_branches: list[str] = []
    seen_merge_repair_modes: list[bool] = []

    async def repair_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        # Record what branch is checked out at the moment the agent runs.
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=input_.worktree, capture_output=True, text=True, check=True,
        )
        seen_branches.append(proc.stdout.strip())
        seen_merge_repair_modes.append(input_.merge_repair)
        # "Repair" by aligning shared.txt with main.
        (input_.worktree / "shared.txt").write_text("A", encoding="utf-8")
        return BuildAgentOutput(succeeded=True, cost_usd=0.01)

    asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            build_agent=repair_agent,
            branch_for_group=lambda s: "i2p/_session/s1",
        )
    )
    # B1 assertion: when the agent runs, it MUST be on the slice's branch,
    # NOT on base_branch.
    assert seen_branches, "repair agent should have been called at least once"
    assert all(b == "i2p/_session/s1" for b in seen_branches), (
        f"repair must run on slice branch; saw {seen_branches}"
    )
    assert all(seen_merge_repair_modes), "repair agent should receive merge_repair mode"


def test_merge_repair_blocks_out_of_scope_changes(tmp_path: Path) -> None:
    """A12: merge repair is hard-scoped after the agent returns."""
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    (tmp_path / "owned.txt").write_text("main\n", encoding="utf-8")
    (tmp_path / "peer.txt").write_text("main\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "owned.txt", "peer.txt"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "main", "--no-verify"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    branch = "i2p/_session/s1"
    subprocess.run(["git", "checkout", "-b", branch, "main~1"],
                   cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "owned.txt").write_text("branch\n", encoding="utf-8")
    subprocess.run(["git", "add", "owned.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "i2p(s1): conflicting owned", "--no-verify"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    spec = _spec(
        [
            Group(
                id="s1",
                name="owner",
                dependencies=[],
                owned_paths=["owned.txt"],
                feature_ids=["f1"],
                checks=[_passing_check()],
            ),
            Group(
                id="s2",
                name="peer",
                dependencies=[],
                owned_paths=["peer.txt"],
                feature_ids=["f2"],
                checks=[],
            ),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(
                group_id="s1",
                status=GroupStatus.PASSING,
                attempts=1,
                branch=branch,
                worktree=tmp_path,
            ),
        ],
    )

    async def overreaching_repair(input_: BuildAgentInput) -> BuildAgentOutput:
        (input_.worktree / "owned.txt").write_text("main\n", encoding="utf-8")
        (input_.worktree / "peer.txt").write_text("overreach\n", encoding="utf-8")
        return BuildAgentOutput(succeeded=True, cost_usd=0.01)

    result = asyncio.run(
        run_merge_queue(
            spec,
            build_result,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=overreaching_repair,
            branch_for_group=lambda s: branch,
            budget=MergeBudget(per_slice_repair_retries=1),
        )
    )

    assert result.landed_ids == []
    assert result.blocked_ids == ["s1"]
    assert "merge repair scope violation" in result.results[0].failure_narrative
    assert "peer.txt" in result.results[0].failure_narrative


# ---------------------------------------------------------------------------
# V2: merge-first-then-verify — pre-merge state shouldn't be checked
# ---------------------------------------------------------------------------


def test_merge_passes_check_against_post_merge_state(tmp_path: Path) -> None:
    """V2: a slice's check must be evaluated against the POST-merge
    integrated state, not against base_branch alone. The slice's
    deliverables only exist on `base + this_slice` after merge — running
    the check pre-merge would fail spuriously for any slice whose check
    tests its own contribution (the bug observed in P1: home_page's
    'Templates exist' check passed at build time on the slice branch
    but failed at merge time when run on bare base_branch).
    """
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    branch = "i2p/_session/templates_slice"
    # Set up the slice branch with templates/ (its deliverable).
    subprocess.run(["git", "checkout", "-b", branch], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    subprocess.run(["git", "add", "templates"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "templates", "--no-verify"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)
    # Templates do NOT exist on main pre-merge.
    assert not (tmp_path / "templates").exists()

    # Group's check: a state invariant requiring templates/ to exist.
    spec = _spec(
        [
            Group(id="templates_slice", name="x", dependencies=[],
                  owned_paths=["templates/*"],
                  feature_ids=["render index"],
                  checks=[_passing_state_invariant("exists('templates')")]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(group_id="templates_slice", status=GroupStatus.PASSING,
                        attempts=1, branch=branch, worktree=tmp_path),
        ],
    )
    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            branch_for_group=lambda s: branch,
        )
    )
    # V2: with merge-first-then-verify, the check runs after the slice
    # is merged — templates exist on main — and the slice LANDS.
    assert result.landed_ids == ["templates_slice"], (
        f"slice with own-deliverable check should land via merge-first; "
        f"got blocked={result.blocked_ids}"
    )
    # Verify templates are now on main.
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, capture_output=True)
    assert (tmp_path / "templates" / "index.html").exists()


def test_merge_rolls_back_when_post_merge_check_fails(tmp_path: Path) -> None:
    """V2: if post-merge slice checks fail (somehow the merged state is
    bad), rollback the merge so base_branch isn't corrupted with a bad
    slice. The slice is then BLOCKED.
    """
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    branch = "i2p/_session/bad_slice"
    subprocess.run(["git", "checkout", "-b", branch], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "broken.txt").write_text("broken", encoding="utf-8")
    subprocess.run(["git", "add", "broken.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "bad", "--no-verify"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)
    main_head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip()

    spec = _spec(
        [
            Group(id="bad_slice", name="x", dependencies=[],
                  owned_paths=["broken.txt"],
                  feature_ids=["create broken.txt"],
                  # Check that will FAIL post-merge.
                  checks=[_passing_state_invariant("exists('not-there.txt')")]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(group_id="bad_slice", status=GroupStatus.PASSING,
                        attempts=1, branch=branch, worktree=tmp_path),
        ],
    )
    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            branch_for_group=lambda s: branch,
        )
    )
    assert result.blocked_ids == ["bad_slice"]
    # main HEAD must be unchanged — rollback worked.
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, capture_output=True)
    main_head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert main_head_before == main_head_after, (
        f"failed merge must roll back; main moved {main_head_before} → {main_head_after}"
    )


# ---------------------------------------------------------------------------
# V6: dirty-worktree resilience — checkout cleans before switching branches
# ---------------------------------------------------------------------------


def test_merge_handles_dirty_worktree_from_prior_check(tmp_path: Path) -> None:
    """V6: post-merge checks (V2) can leave runtime artifacts modified
    in the worktree (e.g., a Flask app's instance/db.sqlite3). The
    next slice's `_merge_group_branch` MUST hard-reset before its
    checkout, otherwise git refuses with 'Your local changes would
    be overwritten by checkout' and the slice is spuriously BLOCKED.
    Observed in the P1 e2e re-run: 2 slices blocked back-to-back
    with `instance/db.sqlite3` git error.
    """
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    # main has runtime.bin tracked at version 1.
    (tmp_path / "runtime.bin").write_text("v1", encoding="utf-8")
    subprocess.run(["git", "add", "runtime.bin"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "v1", "--no-verify"],
                   cwd=tmp_path, check=True, capture_output=True)
    # Group branch off main; modifies a DIFFERENT file (no merge conflict).
    branch = "i2p/_session/clean_slice"
    subprocess.run(["git", "checkout", "-b", branch], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "feature.txt").write_text("feature", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "feature", "--no-verify"],
                   cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)
    # Simulate a post-merge check having mutated runtime.bin in the workdir
    # without committing — the exact P1 symptom.
    (tmp_path / "runtime.bin").write_text("v-mutated-by-check", encoding="utf-8")
    (tmp_path / "transient.tmp").write_text("untracked transient", encoding="utf-8")
    status_dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout
    assert "runtime.bin" in status_dirty, "test setup: workdir should be dirty"

    spec = _spec(
        [
            Group(id="clean_slice", name="x", dependencies=[],
                  owned_paths=["feature.txt"], feature_ids=["add feature"],
                  checks=[_passing_check()]),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(group_id="clean_slice", status=GroupStatus.PASSING,
                        attempts=1, branch=branch, worktree=tmp_path),
        ],
    )
    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            branch_for_group=lambda s: branch,
        )
    )
    # V6: merge should succeed despite the dirty worktree at start.
    assert result.landed_ids == ["clean_slice"], (
        f"merge with dirty worktree should reset+clean before checkout; "
        f"got blocked={result.blocked_ids}, results[0]={result.results[0]}"
    )


def test_merge_uses_base_worktree_when_slice_branch_is_linked_worktree(
    tmp_path: Path,
) -> None:
    """Linked slice worktrees cannot check out main while main is checked out.

    The integration merge must happen in the project/base worktree, while the
    slice worktree stays on the slice branch for possible repair.
    """
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    branch = "i2p/_session/linked_slice"
    slice_worktree = tmp_path / ".worktrees" / "linked_slice"
    slice_worktree.parent.mkdir()
    subprocess.run(
        ["git", "worktree", "add", "-qb", branch, str(slice_worktree), "main"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (slice_worktree / "feature.txt").write_text("feature", encoding="utf-8")
    subprocess.run(
        ["git", "add", "feature.txt"],
        cwd=slice_worktree,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "feature", "--no-verify"],
        cwd=slice_worktree,
        check=True,
        capture_output=True,
    )

    main_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert main_branch == "main"

    spec = _spec(
        [
            Group(
                id="linked_slice",
                name="linked",
                dependencies=[],
                owned_paths=["feature.txt"],
                feature_ids=["add feature"],
                checks=[_passing_check()],
            ),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(
                group_id="linked_slice",
                status=GroupStatus.PASSING,
                attempts=1,
                branch=branch,
                worktree=slice_worktree,
            ),
        ],
    )

    result = asyncio.run(
        run_merge_queue(
            spec,
            build_result,
            project_dir=tmp_path,
            session_dir=session_dir,
            branch_for_group=lambda _s: branch,
        )
    )

    assert result.landed_ids == ["linked_slice"], result.results[0]
    assert (tmp_path / "feature.txt").read_text(encoding="utf-8") == "feature"
    slice_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=slice_worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert slice_branch == branch


def test_run_merge_queue_resume_skip_seeds_landed_ids(tmp_path: Path) -> None:
    """Resume must not re-merge units already landed by the prior attempt.

    A skipped dependency should count as landed for eligibility, allowing
    downstream work to merge without calling the skipped branch.
    """
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    branch = "i2p/_session/downstream"
    subprocess.run(["git", "checkout", "-b", branch], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "downstream.txt").write_text("ok", encoding="utf-8")
    subprocess.run(["git", "add", "downstream.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "downstream", "--no-verify"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    spec = _spec(
        [
            Group(id="already_landed", name="a", dependencies=[], owned_paths=[], feature_ids=[]),
            Group(
                id="downstream",
                name="b",
                dependencies=["already_landed"],
                owned_paths=["downstream.txt"],
                feature_ids=["add downstream"],
                checks=[_passing_check()],
            ),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(
                group_id="already_landed",
                status=GroupStatus.PASSING,
                attempts=0,
                branch="",
                worktree=tmp_path,
                failure_narrative="resume: skipped",
            ),
            GroupResult(
                group_id="downstream",
                status=GroupStatus.PASSING,
                attempts=1,
                branch=branch,
                worktree=tmp_path,
            ),
        ],
    )

    result = asyncio.run(
        run_merge_queue(
            spec,
            build_result,
            project_dir=tmp_path,
            session_dir=session_dir,
            branch_for_group=lambda s: "" if s.id == "already_landed" else branch,
            skip_components={"already_landed"},
        )
    )

    assert result.landed_ids == ["already_landed", "downstream"]
    assert [r.group_id for r in result.results] == ["downstream"]
