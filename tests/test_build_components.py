"""Tests for A1b.3 — Component dispatch in run_build.

Components run alongside Groups in the same dispatch loop, but produce
ComponentResult (no Feature verdict). Component dependencies may target
Groups or other Components; the merge queue will resolve cross-deps via
the unified id namespace.

Coverage:
- run_build dispatches a Component alongside a Group, both pass.
- Component status reflects check pass/fail.
- A Component whose deps are unsatisfied stays BLOCKED (dep blocked).
- A Component with deps on a Group runs only after the Group passes.
- ready_components returns components whose deps are met.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from otto.build import (
    BuildAgentInput,
    BuildAgentOutput,
    BuildBudget,
    ComponentStatus,
    GroupStatus,
    ready_components,
    run_build,
)
from otto.spec_compile import (
    Component,
    RepoTestCheck,
    Group,
    Spec,
    StructureDecisions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(
    *,
    groups: list[Group] | None = None,
    components: list[Component] | None = None,
    shared_paths: list[str] | None = None,
) -> Spec:
    return Spec(
        intent="test intent",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=groups or [],
        components=components or [],
        shared_paths=shared_paths or [],
    )


def _init_git(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", ".gitkeep"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init", "--no-verify"], cwd=repo, check=True)


def _passing() -> RepoTestCheck:
    return RepoTestCheck(command=("python", "-c", "print('ok')"), timeout_s=10)


def _failing() -> RepoTestCheck:
    return RepoTestCheck(command=("python", "-c", "import sys; sys.exit(1)"), timeout_s=10)


# ---------------------------------------------------------------------------
# ready_components
# ---------------------------------------------------------------------------


def test_ready_components_no_deps() -> None:
    spec = _spec(
        components=[
            Component(id="ws-hub", name="WS hub"),
            Component(id="search", name="Search index", dependencies=["ws-hub"]),
        ]
    )
    assert [c.id for c in ready_components(spec, completed_ids=set())] == ["ws-hub"]


def test_ready_components_with_satisfied_dep() -> None:
    spec = _spec(
        components=[
            Component(id="ws-hub", name="WS hub"),
            Component(id="search", name="Search index", dependencies=["ws-hub"]),
        ]
    )
    ready = ready_components(spec, completed_ids={"ws-hub"})
    assert [c.id for c in ready] == ["search"]


def test_ready_components_can_depend_on_group() -> None:
    spec = _spec(
        groups=[Group(id="g1", name="grp")],
        components=[Component(id="c1", name="C1", dependencies=["g1"])],
    )
    # Before group lands: not ready.
    assert ready_components(spec, completed_ids=set()) == []
    # After group lands: ready.
    ready = ready_components(spec, completed_ids={"g1"})
    assert [c.id for c in ready] == ["c1"]


# ---------------------------------------------------------------------------
# run_build dispatches Components
# ---------------------------------------------------------------------------


def test_run_build_dispatches_group_and_component(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    spec = _spec(
        groups=[Group(id="g1", name="group", checks=[_passing()])],
        components=[Component(id="ws-hub", name="WS hub", checks=[_passing()])],
    )

    seen: list[str] = []

    async def fake_agent(inp: BuildAgentInput) -> BuildAgentOutput:
        seen.append(inp.group.id)
        return BuildAgentOutput(succeeded=True, cost_usd=0.01)

    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=fake_agent,
        )
    )

    assert result.all_passing  # group passed
    assert result.passing_ids == ["g1"]
    assert len(result.component_results) == 1
    cr = result.component_results[0]
    assert cr.component_id == "ws-hub"
    assert cr.status == ComponentStatus.PASSING
    assert "ws-hub" in seen and "g1" in seen
    # Both Group and Component all-passing helpers report True.
    assert result.all_components_passing
    assert result.passing_component_ids == ["ws-hub"]


def test_run_build_component_blocks_on_failing_check(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    spec = _spec(
        components=[Component(id="bad", name="bad", checks=[_failing()])],
    )

    async def fake_agent(_inp: BuildAgentInput) -> BuildAgentOutput:
        return BuildAgentOutput(succeeded=True)

    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=fake_agent,
            budget=BuildBudget(per_slice_retries_hard_cap=4),
        )
    )

    assert len(result.component_results) == 1
    cr = result.component_results[0]
    assert cr.component_id == "bad"
    assert cr.status == ComponentStatus.BLOCKED
    assert result.blocked_component_ids == ["bad"]
    assert not result.all_components_passing


def test_run_build_component_runs_after_group_dep(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    spec = _spec(
        groups=[Group(id="g1", name="grp", checks=[_passing()])],
        components=[
            Component(
                id="c1",
                name="C1",
                dependencies=["g1"],
                checks=[_passing()],
            )
        ],
    )

    order: list[str] = []

    async def fake_agent(inp: BuildAgentInput) -> BuildAgentOutput:
        order.append(inp.group.id)
        return BuildAgentOutput(succeeded=True)

    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=fake_agent,
        )
    )

    assert order == ["g1", "c1"]  # Group runs first, then Component.
    assert result.passing_ids == ["g1"]
    assert result.passing_component_ids == ["c1"]


def test_run_build_component_blocked_when_dep_blocks(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    spec = _spec(
        groups=[Group(id="g1", name="grp", checks=[_failing()])],
        components=[
            Component(
                id="c1",
                name="C1",
                dependencies=["g1"],
                checks=[_passing()],
            )
        ],
    )

    async def fake_agent(_inp: BuildAgentInput) -> BuildAgentOutput:
        return BuildAgentOutput(succeeded=True)

    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=fake_agent,
            budget=BuildBudget(per_slice_cost_usd=0.0, total_cost_usd=0.0),
        )
    )

    by_id = {r.group_id: r for r in result.group_results}
    assert by_id["g1"].status == GroupStatus.BLOCKED
    assert len(result.component_results) == 1
    cr = result.component_results[0]
    assert cr.status == ComponentStatus.BLOCKED
    assert "dep blocked" in cr.failure_narrative
    assert cr.attempts == 0  # never ran


def test_run_build_component_dep_pending_kept_blocked(tmp_path: Path) -> None:
    """A Component with deps not satisfied (e.g. dep id refers to a
    nonexistent unit) ends up BLOCKED with attempts==0."""
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    spec = _spec(
        components=[
            Component(
                id="c1",
                name="C1",
                dependencies=["nonexistent"],
                checks=[_passing()],
            )
        ],
    )

    async def fake_agent(_inp: BuildAgentInput) -> BuildAgentOutput:
        return BuildAgentOutput(succeeded=True)

    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=fake_agent,
        )
    )

    assert len(result.component_results) == 1
    cr = result.component_results[0]
    assert cr.status == ComponentStatus.BLOCKED
    assert cr.attempts == 0
