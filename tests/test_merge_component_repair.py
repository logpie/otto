"""A1c.3 — Components participate in the same conflict-repair flow as Groups.

Coverage:
- Component branches are dispatched through `run_merge_queue` via the
  `_component_as_merge_slice` adapter and land normally on a clean merge.
- A Component branch that hits a real `git merge --no-ff` conflict
  triggers the same repair flow as Groups: with a `build_agent`, the
  agent is re-invoked on the Component's own branch and the conflict is
  resolved within `Component.owned_paths`. Without an agent, the
  Component is BLOCKED — never silently landed.
- Repair is pinned to the Component's `owned_paths`: the agent receives
  a `BuildAgentInput.slice` adapter whose `owned_paths` come straight
  from `Component.owned_paths`. A repair attempt that touches files
  outside that set fails the standard scope-violation guard, so a
  Component conflict outside its owned_paths cannot silently rewrite
  unrelated files.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from otto.build import (
    BuildAgentInput,
    BuildAgentOutput,
    BuildResult,
    ComponentResult,
    ComponentStatus,
)
from otto.merge_queue import (
    MergeBudget,
    MergeStatus,
    _component_as_merge_slice,
    run_merge_queue,
)
from otto.spec_compile import (
    Component,
    RepoTestCheck,
    Spec,
    StructureDecisions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(components: list[Component]) -> Spec:
    return Spec(
        intent="test intent",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[],
        components=components,
    )


def _passing_check() -> RepoTestCheck:
    return RepoTestCheck(command=("python", "-c", "print('ok')"), timeout_s=10)


def _init_git(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("_session/\notto_logs/\n", encoding="utf-8")
    (repo / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", ".gitkeep", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init", "--no-verify"], cwd=repo, check=True)


def _build_result(
    *,
    session_dir: Path,
    component_id: str,
    branch: str,
    worktree: Path,
) -> BuildResult:
    return BuildResult(
        spec_session_dir=session_dir,
        group_results=[],
        component_results=[
            ComponentResult(
                component_id=component_id,
                status=ComponentStatus.PASSING,
                attempts=1,
                branch=branch,
                worktree=worktree,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Adapter — pure data, no git
# ---------------------------------------------------------------------------


def test_component_as_merge_slice_passes_through_owned_paths_and_deps() -> None:
    """The adapter must surface Component.owned_paths verbatim so the
    standard scope-violation guard can pin repair edits to the same set
    that Groups use (research §2.6 — Components dispatch like Groups)."""
    component = Component(
        id="c1",
        name="ws-hub",
        description="WebSocket fan-out",
        owned_paths=["server/ws/*", "shared/types.py"],
        dependencies=["g0"],
        checks=[_passing_check()],
    )
    adapted = _component_as_merge_slice(component)
    assert adapted.id == "c1"
    assert adapted.owned_paths == ["server/ws/*", "shared/types.py"]
    assert adapted.dependencies == ["g0"]
    # Description carried into a single synthetic task line so the build
    # agent has a concrete brief during repair.
    assert adapted.feature_ids == ["WebSocket fan-out"]
    assert len(adapted.checks) == 1


# ---------------------------------------------------------------------------
# Happy path — Component lands like a Group
# ---------------------------------------------------------------------------


def test_run_merge_queue_lands_passing_component(tmp_path: Path) -> None:
    """A Component reported PASSING by build.py must be processed by the
    merge queue exactly like a Group: dispatched, merged, and recorded
    in `landed_ids`."""
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    branch = "i2p/_session/c1"
    subprocess.run(["git", "checkout", "-b", branch], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "ws-hub.py").write_text("# component\n", encoding="utf-8")
    subprocess.run(["git", "add", "ws-hub.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "i2p(c1): build component", "--no-verify"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    spec = _spec(
        [
            Component(
                id="c1", name="ws-hub", description="WebSocket fan-out",
                owned_paths=["ws-hub.py"], dependencies=[],
                checks=[_passing_check()],
            ),
        ]
    )
    build_result = _build_result(
        session_dir=session_dir, component_id="c1", branch=branch, worktree=tmp_path,
    )
    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            branch_for_group=lambda s: branch,
        )
    )
    assert result.landed_ids == ["c1"]
    assert result.blocked_ids == []
    assert result.results[0].status == MergeStatus.LANDED


# ---------------------------------------------------------------------------
# A1c.3 — Component conflict on owned_paths triggers repair
# ---------------------------------------------------------------------------


def test_component_merge_conflict_on_owned_paths_triggers_repair(tmp_path: Path) -> None:
    """When a Component's branch conflicts with main on a file in
    `Component.owned_paths`, the same conflict-repair flow used for Group
    branches must engage. The build agent is re-invoked on the
    component's branch (not on base) and the conflict is resolved.
    Mirrors `test_merge_repair_runs_on_slice_branch_not_base` for the
    Group case — Components must reach the same outcome.
    """
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    # main: shared/state.py = "VERSION = 'A'"
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "state.py").write_text("VERSION = 'A'\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared/state.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "main A", "--no-verify"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    # Component branch off main~1, modify state.py to B (will conflict).
    branch = "i2p/_session/c1"
    subprocess.run(["git", "checkout", "-b", branch, "main~1"],
                   cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "shared").mkdir(exist_ok=True)
    (tmp_path / "shared" / "state.py").write_text("VERSION = 'B'\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared/state.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "i2p(c1): conflicting B", "--no-verify"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    spec = _spec(
        [
            Component(
                id="c1", name="state-store", description="shared state",
                owned_paths=["shared/state.py"],  # conflict path is owned
                dependencies=[],
                checks=[_passing_check()],
            ),
        ]
    )
    build_result = _build_result(
        session_dir=session_dir, component_id="c1", branch=branch, worktree=tmp_path,
    )

    seen_branches: list[str] = []
    seen_owned_paths: list[list[str]] = []

    async def repair_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        # Record the repair branch + the owned_paths the agent is pinned to —
        # both must come from the Component, not from a leaked Group.
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=input_.worktree, capture_output=True, text=True, check=True,
        )
        seen_branches.append(proc.stdout.strip())
        seen_owned_paths.append(list(input_.group.owned_paths))
        # Resolve the conflict by aligning with main.
        (input_.worktree / "shared" / "state.py").write_text("VERSION = 'A'\n", encoding="utf-8")
        return BuildAgentOutput(succeeded=True, cost_usd=0.02)

    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            build_agent=repair_agent,
            branch_for_group=lambda s: branch,
        )
    )
    # Repair fired and the Component landed.
    assert seen_branches, "repair agent should have been invoked at least once"
    assert all(b == branch for b in seen_branches), (
        f"Component repair must run on the component branch; saw {seen_branches}"
    )
    # Repair was scoped to Component.owned_paths.
    assert seen_owned_paths == [["shared/state.py"]] * len(seen_owned_paths)
    assert result.landed_ids == ["c1"]
    assert result.results[0].repair_attempts >= 1
    assert result.results[0].cost_usd > 0


# ---------------------------------------------------------------------------
# A1c.3 — Component conflict outside owned_paths is BLOCKED without an agent
# ---------------------------------------------------------------------------


def test_component_conflict_blocked_without_agent(tmp_path: Path) -> None:
    """When a Component's branch conflicts and no `build_agent` is
    supplied, the merge queue MUST surface BLOCKED rather than silently
    landing a marker-laden state. This is the same guarantee Groups
    already enforce — Components inherit it via the shared
    `_process_candidate` flow."""
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
    # Component branch off main~1, modify shared.txt to B → will conflict.
    branch = "i2p/_session/c1"
    subprocess.run(["git", "checkout", "-b", branch, "main~1"],
                   cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "shared.txt").write_text("B", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "i2p(c1): B", "--no-verify"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    spec = _spec(
        [
            Component(
                id="c1", name="state-store",
                # Component does NOT declare shared.txt in owned_paths —
                # the conflict path is outside its declared scope.
                owned_paths=["unrelated/area.py"],
                dependencies=[],
                checks=[_passing_check()],
            ),
        ]
    )
    build_result = _build_result(
        session_dir=session_dir, component_id="c1", branch=branch, worktree=tmp_path,
    )
    # No build_agent → must BLOCK on conflict.
    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            branch_for_group=lambda s: branch,
        )
    )
    assert result.blocked_ids == ["c1"]
    assert result.landed_ids == []
    narrative = result.results[0].failure_narrative.lower()
    assert "merge conflict" in narrative
    assert "no build_agent" in narrative
    # Worktree is left clean (merge --abort ran).
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert status == "", f"worktree should be clean after aborted merge, got: {status}"


# ---------------------------------------------------------------------------
# A1c.3 — Component repair retries are bounded just like Groups
# ---------------------------------------------------------------------------


def test_component_repair_blocks_when_retries_exhausted(tmp_path: Path) -> None:
    """A Component whose repair agent never actually fixes the merge
    must hit the same retry-exhaustion BLOCK as a Group, with
    `repair_attempts` reflecting the configured budget."""
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    (tmp_path / "shared.txt").write_text("A", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "main A", "--no-verify"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    branch = "i2p/_session/c1"
    subprocess.run(["git", "checkout", "-b", branch, "main~1"],
                   cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "shared.txt").write_text("B", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "i2p(c1): B", "--no-verify"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)

    spec = _spec(
        [
            Component(
                id="c1", name="state-store",
                owned_paths=["shared.txt"], dependencies=[],
                checks=[_passing_check()],
            ),
        ]
    )
    build_result = _build_result(
        session_dir=session_dir, component_id="c1", branch=branch, worktree=tmp_path,
    )

    async def useless_agent(_input: BuildAgentInput) -> BuildAgentOutput:
        # Pretends success but never actually edits the conflicting file.
        return BuildAgentOutput(succeeded=True)

    result = asyncio.run(
        run_merge_queue(
            spec, build_result, project_dir=tmp_path, session_dir=session_dir,
            build_agent=useless_agent,
            branch_for_group=lambda s: branch,
            budget=MergeBudget(per_slice_repair_retries=2),
        )
    )
    assert result.blocked_ids == ["c1"]
    assert result.results[0].repair_attempts == 2
    assert "exhausted" in result.results[0].failure_narrative.lower()
