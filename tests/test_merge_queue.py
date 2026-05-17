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
from typing import Any

from otto.build import (
    BuildAgentInput,
    BuildAgentOutput,
    BuildResult,
    ContractDelta,
    GroupResult,
    GroupStatus,
    run_build,
)
from otto.merge_queue import (
    MergeBudget,
    MergeStatus,
    _commit_integration,
    _merge_group_branch,
    eligible_candidates,
    passing_group_ids,
    run_merge_queue,
)
from otto.spec_compile import (
    BrowserJourney,
    RepoTestCheck,
    Group,
    Spec,
    StateInvariant,
    StructureDecisions,
)
from otto.spec_state import iter_events


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


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _passing_check() -> RepoTestCheck:
    return RepoTestCheck(command=("python", "-c", "print('ok')"), timeout_s=10)


def _failing_check() -> RepoTestCheck:
    return RepoTestCheck(command=("python", "-c", "import sys; sys.exit(1)"), timeout_s=10)


def _failing_browser_check() -> BrowserJourney:
    return BrowserJourney(
        command=("python", "-c", "import sys; sys.exit(1)"),
        evidence_globs=(),
        timeout_s=10,
    )


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


def test_degraded_group_is_merge_candidate_but_not_passing_id(tmp_path: Path) -> None:
    build_result = BuildResult(
        spec_session_dir=tmp_path,
        group_results=[
            GroupResult(group_id="a", status=GroupStatus.DEGRADED, attempts=1, branch="x", worktree=tmp_path),
        ],
    )

    assert passing_group_ids(build_result) == []
    assert build_result.merge_candidate_ids == ["a"]


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


def test_run_merge_queue_blocks_degraded_slice_with_failed_behavior_check(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _ensure_main(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    subprocess.run(["git", "checkout", "-q", "-b", "b1"], cwd=tmp_path, check=True)
    (tmp_path / "feature.txt").write_text("best effort", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feature", "--no-verify"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=tmp_path, check=True)

    spec = _spec(
        [
            Group(
                id="s1",
                name="x",
                dependencies=[],
                owned_paths=["feature.txt"],
                feature_ids=[],
                checks=[_failing_browser_check()],
            ),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(
                group_id="s1",
                status=GroupStatus.DEGRADED,
                attempts=1,
                branch="b1",
                worktree=tmp_path,
            ),
        ],
        base_branch="main",
    )

    result = asyncio.run(
        run_merge_queue(spec, build_result, project_dir=tmp_path, session_dir=session_dir)
    )

    assert result.landed_ids == []
    assert result.results[0].group_recheck_evidence[0].passed is False
    assert "post-merge verification failed" in result.results[0].failure_narrative
    assert result.blocked_ids == ["s1"]
    assert result.results[0].status == MergeStatus.BLOCKED
    assert result.results[0].landed_commit == ""


def test_run_merge_queue_blocks_degraded_slice_on_structural_check_failure(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _ensure_main(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    subprocess.run(["git", "checkout", "-q", "-b", "b1"], cwd=tmp_path, check=True)
    (tmp_path / "feature.txt").write_text("best effort", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feature", "--no-verify"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=tmp_path, check=True)

    spec = _spec(
        [
            Group(
                id="s1",
                name="x",
                dependencies=[],
                owned_paths=["feature.txt"],
                feature_ids=[],
                checks=[_failing_check()],
            ),
        ]
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(
                group_id="s1",
                status=GroupStatus.DEGRADED,
                attempts=1,
                branch="b1",
                worktree=tmp_path,
            ),
        ],
        base_branch="main",
    )

    result = asyncio.run(
        run_merge_queue(spec, build_result, project_dir=tmp_path, session_dir=session_dir)
    )

    assert result.landed_ids == []
    assert result.blocked_ids == ["s1"]


def test_run_merge_queue_carries_build_blocked_ids_to_final_result(tmp_path: Path) -> None:
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = _spec([
        Group(id="s1", name="blocked at build", dependencies=[], owned_paths=[], feature_ids=[]),
    ])
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(
                group_id="s1",
                status=GroupStatus.BLOCKED,
                attempts=1,
                branch="i2p/x/s1",
                worktree=tmp_path,
                failure_narrative="build failed",
            ),
        ],
    )

    result = asyncio.run(
        run_merge_queue(spec, build_result, project_dir=tmp_path, session_dir=session_dir)
    )

    assert result.landed_ids == []
    assert result.blocked_ids == ["s1"]
    assert result.results == []


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

    async def writing_agent(agent_input: BuildAgentInput) -> BuildAgentOutput:
        (agent_input.worktree / "alpha.txt").write_text("alpha\n", encoding="utf-8")
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


def test_run_merge_queue_defers_future_owned_cross_group_check(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    first_branch = "i2p/_session/foundation"
    subprocess.run(["git", "checkout", "-b", first_branch], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "foundation.txt").write_text("foundation\n", encoding="utf-8")
    subprocess.run(["git", "add", "foundation.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "i2p(foundation): build", "--no-verify"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    final_branch = "i2p/_session/integration"
    subprocess.run(["git", "checkout", "-b", final_branch], cwd=tmp_path, check=True, capture_output=True)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "final_check.py").write_text(
        "from pathlib import Path\n"
        "assert Path('foundation.txt').exists()\n"
        "print('integrated ok')\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "tests/final_check.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "i2p(integration): build", "--no-verify"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    spec = _spec(
        [
            Group(
                id="foundation",
                name="Foundation",
                dependencies=[],
                owned_paths=["foundation.txt"],
                feature_ids=["write foundation"],
                checks=[_passing_check()],
            ),
            Group(
                id="integration",
                name="Integration",
                dependencies=["foundation"],
                owned_paths=["tests/final_check.py"],
                feature_ids=["write final check"],
                checks=[_passing_check()],
            ),
        ],
        cross_checks=[
            RepoTestCheck(command=("python", "tests/final_check.py"), timeout_s=10)
        ],
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(
                group_id="foundation",
                status=GroupStatus.PASSING,
                attempts=1,
                branch=first_branch,
                worktree=tmp_path,
            ),
            GroupResult(
                group_id="integration",
                status=GroupStatus.PASSING,
                attempts=1,
                branch=final_branch,
                worktree=tmp_path,
            ),
        ],
    )

    result = asyncio.run(
        run_merge_queue(spec, build_result, project_dir=tmp_path, session_dir=session_dir)
    )

    assert result.landed_ids == ["foundation", "integration"]
    assert result.blocked_ids == []
    assert result.results[0].cross_slice_evidence == []
    assert len(result.results[1].cross_slice_evidence) == 1
    assert result.results[1].cross_slice_evidence[0].passed is True


def test_run_merge_queue_defers_missing_unowned_cross_group_runner_until_complete(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    first_branch = "i2p/_session/foundation"
    subprocess.run(["git", "checkout", "-b", first_branch], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "foundation.txt").write_text("foundation\n", encoding="utf-8")
    subprocess.run(["git", "add", "foundation.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "i2p(foundation): build", "--no-verify"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    second_branch = "i2p/_session/feature"
    subprocess.run(["git", "checkout", "-b", second_branch], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "feature.txt").write_text("feature\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "i2p(feature): build", "--no-verify"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    spec = _spec(
        [
            Group(
                id="foundation",
                name="Foundation",
                dependencies=[],
                owned_paths=["foundation.txt"],
                feature_ids=["write foundation"],
                checks=[_passing_check()],
            ),
            Group(
                id="feature",
                name="Feature",
                dependencies=["foundation"],
                owned_paths=["feature.txt"],
                feature_ids=["write feature"],
                checks=[_passing_check()],
            ),
        ],
        cross_checks=[
            RepoTestCheck(command=("python", "tests/main_workflow.py"), timeout_s=10)
        ],
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(
                group_id="foundation",
                status=GroupStatus.PASSING,
                attempts=1,
                branch=first_branch,
                worktree=tmp_path,
            ),
            GroupResult(
                group_id="feature",
                status=GroupStatus.PASSING,
                attempts=1,
                branch=second_branch,
                worktree=tmp_path,
            ),
        ],
    )

    result = asyncio.run(
        run_merge_queue(spec, build_result, project_dir=tmp_path, session_dir=session_dir)
    )

    assert result.landed_ids == ["foundation"]
    assert result.blocked_ids == ["feature"]
    assert result.results[0].cross_slice_evidence == []
    assert len(result.results[1].cross_slice_evidence) == 1
    assert result.results[1].cross_slice_evidence[0].passed is False
    assert result.results[1].cross_slice_evidence[0].detail.startswith(
        "planned cross-group check artifact missing"
    )
    assert "tests/main_workflow.py" in result.results[1].failure_narrative
    assert (
        session_dir
        / "merge"
        / "feature"
        / "cross-attempt-00"
        / "000-MissingCrossGroupCheckArtifact.log"
    ).is_file()


def test_run_merge_queue_reselects_cross_group_checks_after_merge(
    tmp_path: Path,
) -> None:
    """Integrated checks wait for complete graph, even if runner landed early."""
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    first_branch = "i2p/_session/foundation"
    subprocess.run(["git", "checkout", "-b", first_branch], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "main_workflow.py").write_text(
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "tests/main_workflow.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "i2p(foundation): add failing runner", "--no-verify"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    second_branch = "i2p/_session/feature"
    subprocess.run(["git", "checkout", "-b", second_branch], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "feature.txt").write_text("feature\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "i2p(feature): build", "--no-verify"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    spec = _spec(
        [
            Group(
                id="foundation",
                name="Foundation",
                dependencies=[],
                owned_paths=["tests/main_workflow.py"],
                feature_ids=["write workflow check"],
                checks=[_passing_check()],
            ),
            Group(
                id="feature",
                name="Feature",
                dependencies=["foundation"],
                owned_paths=["feature.txt"],
                feature_ids=["write feature"],
                checks=[_passing_check()],
            ),
        ],
        cross_checks=[
            RepoTestCheck(command=("python", "tests/main_workflow.py"), timeout_s=10)
        ],
    )
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(
                group_id="foundation",
                status=GroupStatus.PASSING,
                attempts=1,
                branch=first_branch,
                worktree=tmp_path,
            ),
            GroupResult(
                group_id="feature",
                status=GroupStatus.PASSING,
                attempts=1,
                branch=second_branch,
                worktree=tmp_path,
            ),
        ],
    )

    result = asyncio.run(
        run_merge_queue(spec, build_result, project_dir=tmp_path, session_dir=session_dir)
    )

    assert result.landed_ids == ["foundation"]
    assert result.blocked_ids == ["feature"]
    assert result.results[0].cross_slice_evidence == []
    assert len(result.results[1].cross_slice_evidence) == 1
    assert result.results[1].cross_slice_evidence[0].passed is False
    assert "cross-slice" in result.results[1].failure_narrative


def test_run_merge_queue_does_not_treat_evidence_globs_as_missing_inputs(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    branch = "i2p/_session/s1"
    subprocess.run(["git", "checkout", "-b", branch], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "feature.txt").write_text("feature\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "i2p(s1): build", "--no-verify"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    spec = _spec(
        [
            Group(
                id="s1",
                name="Feature",
                dependencies=[],
                owned_paths=["feature.txt"],
                feature_ids=["Add feature marker file"],
                checks=[_passing_check()],
            ),
        ],
        cross_checks=[
            BrowserJourney(
                command=(
                    "python",
                    "-c",
                    "from pathlib import Path; p=Path('otto_artifacts/browser/full-workflow'); "
                    "p.mkdir(parents=True, exist_ok=True); (p/'step.png').write_bytes(b'png')",
                ),
                evidence_globs=("otto_artifacts/browser/full-workflow/*.png",),
                timeout_s=10,
            )
        ],
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

    result = asyncio.run(
        run_merge_queue(spec, build_result, project_dir=tmp_path, session_dir=session_dir)
    )

    assert result.landed_ids == ["s1"]
    assert result.results[0].cross_slice_evidence[0].passed is True
    assert not (
        session_dir
        / "merge"
        / "s1"
        / "cross-attempt-00"
        / "000-MissingCrossGroupCheckArtifact.log"
    ).exists()


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
            GroupResult(
                group_id="s1",
                status=GroupStatus.PASSING,
                attempts=1,
                branch="b1",
                worktree=tmp_path,
                contract_deltas=[
                    ContractDelta(
                        group_id="s1",
                        contract_id="shared-store",
                        owner_id="foundation",
                        paths=["src/lib/store.ts"],
                        invariants=["marker state remains compatible"],
                    )
                ],
            ),
        ],
    )

    seen_configs: list[dict[str, Any]] = []
    seen_timeouts: list[int | None] = []
    seen_context_packets: list[Path | None] = []
    seen_contract_deltas: list[tuple[ContractDelta, ...]] = []

    async def repair_agent(agent_input: BuildAgentInput) -> BuildAgentOutput:
        seen_configs.append(dict(agent_input.config))
        seen_timeouts.append(agent_input.timeout_s)
        seen_context_packets.append(agent_input.context_packet_path)
        seen_contract_deltas.append(agent_input.contract_deltas)
        # Repair by creating the missing marker.
        (agent_input.worktree / "marker.txt").write_text("ok", encoding="utf-8")
        return BuildAgentOutput(succeeded=True, cost_usd=0.05)

    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            build_agent=repair_agent,
            config={"provider": "codex", "_cli_overrides": {"provider": "codex"}},
            budget=MergeBudget(per_slice_repair_retries=1, per_slice_wall_s=17),
        )
    )
    assert result.landed_ids == ["s1"]
    assert result.results[0].repair_attempts == 1
    assert result.results[0].cost_usd > 0
    assert seen_configs == [
        {"provider": "codex", "_cli_overrides": {"provider": "codex"}}
    ]
    assert len(seen_timeouts) == 1
    assert seen_timeouts[0] is not None
    assert 1 <= seen_timeouts[0] <= 17
    assert seen_context_packets[0] is not None
    assert seen_context_packets[0].is_file()
    packet_text = seen_context_packets[0].read_text(encoding="utf-8")
    assert "behavior_journeys" in packet_text
    assert "contract_deltas" in packet_text
    assert seen_contract_deltas[0][0].contract_id == "shared-store"
    assert result.results[0].contract_deltas[0].contract_id == "shared-store"
    events = list(iter_events(session_dir))
    assert any(event.kind == "contract.delta.merge" for event in events)
    feedback = [event for event in events if event.kind == "group.check.feedback"]
    assert feedback
    assert feedback[0].group_id == "s1"
    assert feedback[0].extra["phase"] == "merge"
    assert "post-merge verification failed" in feedback[0].detail


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

    async def useless_agent(agent_input: BuildAgentInput) -> BuildAgentOutput:
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


def test_run_merge_queue_grants_bounded_extra_repair_on_new_failure(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _ensure_main(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    subprocess.run(["git", "checkout", "-b", "i2p/_session/s1"], cwd=tmp_path, check=True)
    (tmp_path / "feature.txt").write_text("feature\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "slice feature", "--no-verify"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True)

    check_script = """
from pathlib import Path
marker = Path("marker.txt")
if not marker.exists():
    raise AssertionError("Expected alpha marker")
if marker.read_text(encoding="utf-8").strip() != "beta":
    raise AssertionError("Expected beta marker")
"""
    spec = _spec(
        [
            Group(
                id="s1",
                name="x",
                dependencies=[],
                owned_paths=["feature.txt", "marker.txt"],
                feature_ids=["edit marker"],
                checks=[RepoTestCheck(command=("python", "-c", check_script), timeout_s=10)],
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
                branch="i2p/_session/s1",
                worktree=tmp_path,
            ),
        ],
    )
    calls = 0

    async def progressive_repair(agent_input: BuildAgentInput) -> BuildAgentOutput:
        nonlocal calls
        calls += 1
        value = "alpha" if calls == 1 else "beta"
        (agent_input.worktree / "marker.txt").write_text(f"{value}\n", encoding="utf-8")
        return BuildAgentOutput(succeeded=True)

    result = asyncio.run(
        run_merge_queue(
            spec,
            build_result,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=progressive_repair,
            budget=MergeBudget(
                per_slice_repair_retries=1,
                per_slice_progress_repair_extensions=1,
            ),
        )
    )

    assert result.landed_ids == ["s1"]
    assert calls == 2
    assert result.results[0].repair_attempts == 2
    events = list(iter_events(session_dir))
    assert any(event.kind == "group.repair.progress_extension" for event in events)
    assert (tmp_path / "marker.txt").read_text(encoding="utf-8") == "beta\n"


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

    async def crash_then_fix(agent_input: BuildAgentInput) -> BuildAgentOutput:
        counter["n"] += 1
        if counter["n"] == 1:
            raise RuntimeError("boom")
        # On retry: actually fix it.
        (agent_input.worktree / "marker.txt").write_text("ok", encoding="utf-8")
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


def test_run_merge_queue_preserves_local_virtualenv_during_cleanup(tmp_path: Path) -> None:
    _init_git(tmp_path)
    _ensure_main(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("local runtime\n", encoding="utf-8")
    branch = "i2p/_session/s1"
    subprocess.run(["git", "checkout", "-b", branch], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "slice-work.txt").write_text("from slice", encoding="utf-8")
    subprocess.run(["git", "add", "slice-work.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "slice work", "--no-verify"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    spec = _spec([
        Group(id="s1", name="x", dependencies=[], owned_paths=["slice-work.txt"], feature_ids=[], checks=[]),
    ])
    build_result = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(group_id="s1", status=GroupStatus.PASSING, attempts=1, branch=branch, worktree=tmp_path),
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

    assert result.landed_ids == ["s1"]
    assert venv_python.read_text(encoding="utf-8") == "local runtime\n"


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


def test_commit_integration_excludes_otto_runtime_evidence_from_product_commit(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _ensure_main(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('real change')\n", encoding="utf-8")
    artifact_dir = tmp_path / "otto_artifacts" / "browser"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "shot.png").write_bytes(b"png")
    (tmp_path / "__audit_home_body__.html").write_text("<main>audit</main>", encoding="utf-8")

    outcome = _commit_integration(_git, tmp_path, group_id="s1", branch="i2p/s1")

    assert outcome.status == MergeStatus.LANDED
    committed_paths = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert "src/app.py" in committed_paths
    assert not any(path.startswith("otto_artifacts/") for path in committed_paths)
    assert "__audit_home_body__.html" not in committed_paths


def test_merge_group_branch_blocks_missing_declared_branch_without_committing_dirty_state(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _ensure_main(tmp_path)
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (tmp_path / "unrelated.txt").write_text("must not be committed\n", encoding="utf-8")

    outcome = _merge_group_branch(
        _git,
        tmp_path,
        group_id="s1",
        branch="i2p/missing/s1",
        base_branch="main",
    )

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert outcome.status == MergeStatus.BLOCKED
    assert "does not exist" in outcome.detail
    assert head_after == head_before
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "unrelated.txt" in status


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
    assert "shared.txt" in result.results[0].failure_narrative
    # Verify worktree is left clean (merge --abort ran).
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert status == "", f"worktree should be clean after aborted merge, got: {status}"


def test_merge_repair_reproduces_conflict_markers_on_slice_branch(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _ensure_main(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    (tmp_path / "shared.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base shared", "--no-verify"], cwd=tmp_path, check=True)

    subprocess.run(["git", "checkout", "-b", "i2p/_session/s1"], cwd=tmp_path, check=True)
    (tmp_path / "shared.txt").write_text("slice\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "slice shared", "--no-verify"], cwd=tmp_path, check=True)

    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True)
    (tmp_path / "shared.txt").write_text("target\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "target shared", "--no-verify"], cwd=tmp_path, check=True)

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
    seen_unmerged: list[str] = []

    async def repair_agent(agent_input: BuildAgentInput) -> BuildAgentOutput:
        unmerged = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=agent_input.worktree,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        seen_unmerged.append(unmerged)
        assert "<<<<<<<" in (agent_input.worktree / "shared.txt").read_text(encoding="utf-8")
        assert "Unmerged paths: shared.txt" in agent_input.last_failure_narrative
        (agent_input.worktree / "shared.txt").write_text("target\nslice\n", encoding="utf-8")
        return BuildAgentOutput(succeeded=True, cost_usd=0.01)

    result = asyncio.run(
        run_merge_queue(
            spec,
            build_result,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=repair_agent,
            branch_for_group=lambda s: "i2p/_session/s1",
            budget=MergeBudget(per_slice_repair_retries=1),
        )
    )

    assert result.landed_ids == ["s1"]
    assert seen_unmerged == ["shared.txt"]
    assert (tmp_path / "shared.txt").read_text(encoding="utf-8") == "target\nslice\n"


def test_merge_repair_salvages_committable_edits_after_agent_error(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _ensure_main(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    (tmp_path / "shared.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base shared", "--no-verify"], cwd=tmp_path, check=True)

    subprocess.run(["git", "checkout", "-b", "i2p/_session/s1"], cwd=tmp_path, check=True)
    (tmp_path / "shared.txt").write_text("slice\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "slice shared", "--no-verify"], cwd=tmp_path, check=True)

    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True)
    (tmp_path / "shared.txt").write_text("target\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "target shared", "--no-verify"], cwd=tmp_path, check=True)

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
    calls = 0

    async def flaky_repair_agent(agent_input: BuildAgentInput) -> BuildAgentOutput:
        nonlocal calls
        calls += 1
        assert "<<<<<<<" in (agent_input.worktree / "shared.txt").read_text(encoding="utf-8")
        (agent_input.worktree / "shared.txt").write_text("target\nslice\n", encoding="utf-8")
        return BuildAgentOutput(succeeded=False, detail="provider stream ended after edits")

    result = asyncio.run(
        run_merge_queue(
            spec,
            build_result,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=flaky_repair_agent,
            branch_for_group=lambda s: "i2p/_session/s1",
            budget=MergeBudget(per_slice_repair_retries=1),
        )
    )

    assert calls == 1
    assert result.landed_ids == ["s1"]
    assert result.blocked_ids == []
    assert (tmp_path / "shared.txt").read_text(encoding="utf-8") == "target\nslice\n"


def test_merge_repair_handles_linked_slice_worktree_conflict(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _ensure_main(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    (tmp_path / "shared.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base shared", "--no-verify"], cwd=tmp_path, check=True)

    subprocess.run(["git", "branch", "i2p/_session/s1"], cwd=tmp_path, check=True)
    slice_worktree = tmp_path.parent / f"{tmp_path.name}-slice-worktree"
    subprocess.run(
        ["git", "worktree", "add", str(slice_worktree), "i2p/_session/s1"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (slice_worktree / "shared.txt").write_text("slice\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=slice_worktree, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "slice shared", "--no-verify"],
        cwd=slice_worktree,
        check=True,
    )

    (tmp_path / "shared.txt").write_text("target\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "target shared", "--no-verify"], cwd=tmp_path, check=True)

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
                        branch="i2p/_session/s1", worktree=slice_worktree),
        ],
    )
    seen_worktrees: list[Path] = []

    async def repair_agent(agent_input: BuildAgentInput) -> BuildAgentOutput:
        seen_worktrees.append(agent_input.worktree)
        assert agent_input.worktree == slice_worktree
        assert "<<<<<<<" in (agent_input.worktree / "shared.txt").read_text(encoding="utf-8")
        (agent_input.worktree / "shared.txt").write_text("target\nslice\n", encoding="utf-8")
        return BuildAgentOutput(succeeded=True)

    result = asyncio.run(
        run_merge_queue(
            spec,
            build_result,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=repair_agent,
            branch_for_group=lambda s: "i2p/_session/s1",
            budget=MergeBudget(per_slice_repair_retries=1),
        )
    )

    assert seen_worktrees == [slice_worktree]
    assert result.landed_ids == ["s1"]
    assert (tmp_path / "shared.txt").read_text(encoding="utf-8") == "target\nslice\n"


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

    async def repair_agent(agent_input: BuildAgentInput) -> BuildAgentOutput:
        # Record what branch is checked out at the moment the agent runs.
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=agent_input.worktree, capture_output=True, text=True, check=True,
        )
        seen_branches.append(proc.stdout.strip())
        seen_merge_repair_modes.append(agent_input.merge_repair)
        # "Repair" by aligning shared.txt with main.
        (agent_input.worktree / "shared.txt").write_text("A", encoding="utf-8")
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

    async def overreaching_repair(agent_input: BuildAgentInput) -> BuildAgentOutput:
        (agent_input.worktree / "owned.txt").write_text("main\n", encoding="utf-8")
        (agent_input.worktree / "peer.txt").write_text("overreach\n", encoding="utf-8")
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


def test_merge_repair_ignores_generated_playwright_artifact_conflicts(
    tmp_path: Path,
) -> None:
    """Generated Playwright reports must not make a real source repair fail."""
    _init_git(tmp_path)
    _ensure_main(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    app = tmp_path / "src" / "App.tsx"
    report = tmp_path / "test-results" / "playwright-report" / "index.html"
    app.parent.mkdir()
    report.parent.mkdir(parents=True)
    app.write_text("base app\n", encoding="utf-8")
    report.write_text("<html>base report</html>\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "src/App.tsx", "test-results/playwright-report/index.html"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "base app", "--no-verify"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    branch = "i2p/_session/s1"
    subprocess.run(["git", "checkout", "-b", branch], cwd=tmp_path, check=True, capture_output=True)
    app.write_text("slice app\n", encoding="utf-8")
    report.write_text("<html>slice generated report</html>\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "slice app", "--no-verify"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)
    app.write_text("main app\n", encoding="utf-8")
    report.write_text("<html>main generated report</html>\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "main app", "--no-verify"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    spec = _spec(
        [
            Group(
                id="s1",
                name="source",
                dependencies=[],
                owned_paths=["src/App.tsx"],
                feature_ids=["f1"],
                checks=[_passing_check()],
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

    async def repair(agent_input: BuildAgentInput) -> BuildAgentOutput:
        (agent_input.worktree / "src" / "App.tsx").write_text(
            "main app\nslice app\n",
            encoding="utf-8",
        )
        (agent_input.worktree / "test-results" / "playwright-report" / "index.html").write_text(
            "<html>latest generated report</html>\n",
            encoding="utf-8",
        )
        return BuildAgentOutput(succeeded=True, cost_usd=0.01)

    result = asyncio.run(
        run_merge_queue(
            spec,
            build_result,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=repair,
            branch_for_group=lambda s: branch,
            budget=MergeBudget(per_slice_repair_retries=1),
        )
    )

    assert result.landed_ids == ["s1"]
    assert result.blocked_ids == []
    tracked = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert "src/App.tsx" in tracked
    assert "test-results/playwright-report/index.html" not in tracked


def test_merge_repair_scope_ignores_preexisting_target_branch_changes(
    tmp_path: Path,
) -> None:
    """Target-branch edits present before the repair agent starts are not overreach."""
    _init_git(tmp_path)
    _ensure_main(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    (tmp_path / "owned.txt").write_text("base\n", encoding="utf-8")
    (tmp_path / "peer.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "owned.txt", "peer.txt"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "base files", "--no-verify"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    branch = "i2p/_session/s1"
    subprocess.run(
        ["git", "checkout", "-b", branch, "main"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "owned.txt").write_text("slice\n", encoding="utf-8")
    subprocess.run(["git", "add", "owned.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "i2p(s1): slice edit", "--no-verify"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "owned.txt").write_text("target\n", encoding="utf-8")
    (tmp_path / "peer.txt").write_text("landed peer\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "owned.txt", "peer.txt"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "target peer edit", "--no-verify"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

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

    async def resolving_repair(agent_input: BuildAgentInput) -> BuildAgentOutput:
        assert agent_input.merge_repair
        assert (agent_input.worktree / "peer.txt").read_text(encoding="utf-8") == "landed peer\n"
        owned = (agent_input.worktree / "owned.txt").read_text(encoding="utf-8")
        assert "<<<<<<<" in owned
        assert "target" in owned
        assert "slice" in owned
        (agent_input.worktree / "owned.txt").write_text("target\nslice\n", encoding="utf-8")
        return BuildAgentOutput(succeeded=True, cost_usd=0.01)

    result = asyncio.run(
        run_merge_queue(
            spec,
            build_result,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=resolving_repair,
            branch_for_group=lambda s: branch,
            budget=MergeBudget(per_slice_repair_retries=1),
        )
    )

    assert result.landed_ids == ["s1"]
    assert result.blocked_ids == []
    assert (tmp_path / "owned.txt").read_text(encoding="utf-8") == "target\nslice\n"
    assert (tmp_path / "peer.txt").read_text(encoding="utf-8") == "landed peer\n"


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
