"""Tests for ``otto.runner._make_layer2_fix_agent`` (Tick 60 W3-C follow-up).

The bridge adapts a ``BuildAgentCallable`` (input-shape contract from
``otto.build``) to the ``FixAgentCallable`` contract used by
``otto.audit_loop.repair_failing_features``. Earlier this was a stub
that always returned ``succeeded=False``; the wiring landed in this
tick. These tests pin:

* the BuildAgentInput passed to the build agent has
  ``feature_id`` set to the failing feature's id (so the build prompt
  emits the "FIX ONLY THIS FEATURE" preamble);
* a build agent reporting ``succeeded=True`` produces a successful
  RepairAttempt;
* a build agent that raises is caught and recorded as a failed
  RepairAttempt — Layer 2 must never crash the run;
* multiple failing features each receive their own dispatch with the
  right narrowing.

No real LLM, no subprocess.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from otto.audit_loop import FailingFeature, RepairAttempt
from otto.build import BuildAgentInput, BuildAgentOutput
from otto.runner import _make_layer2_fix_agent
from otto.spec_compile import Feature, Group, Spec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _spec_with_two_features() -> Spec:
    group = Group(id="g1", name="G1")
    f1 = Feature(id="feat-a", name="Feature A", group_id="g1",
                 description="alpha")
    f2 = Feature(id="feat-b", name="Feature B", group_id="g1",
                 description="beta")
    return Spec(intent="x", groups=[group], features=[f1, f2])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_layer2_passes_feature_id_to_build_agent(tmp_path: Path) -> None:
    """The bridge must thread failing.feature_id into BuildAgentInput."""
    spec = _spec_with_two_features()
    captured: dict[str, Any] = {}

    async def recording_build_agent(agent_input: BuildAgentInput) -> BuildAgentOutput:
        captured["feature_id"] = agent_input.feature_id
        captured["related_feature_ids"] = agent_input.related_feature_ids
        captured["group_id"] = agent_input.group.id
        captured["last_failure_narrative"] = agent_input.last_failure_narrative
        captured["worktree"] = agent_input.worktree
        captured["project_dir"] = agent_input.project_dir
        return BuildAgentOutput(succeeded=True, cost_usd=0.42, wall_s=1.5,
                                detail="fixed it")

    bridge = _make_layer2_fix_agent(
        fix_agent=recording_build_agent,
        spec=spec,
        project_dir=tmp_path,
        session_dir=tmp_path / "sess",
        base_url=None,
    )

    failing = FailingFeature(
        feature_id="feat-a",
        verdict="partial",
        detail="login button does nothing",
    )
    group = spec.groups[0]
    attempt = asyncio.run(bridge(failing, group))

    assert captured["feature_id"] == "feat-a"
    assert captured["related_feature_ids"] == ("feat-a",)
    assert captured["group_id"] == "g1"
    assert captured["last_failure_narrative"] == "login button does nothing"
    assert captured["project_dir"] == tmp_path
    assert captured["worktree"] == tmp_path
    assert isinstance(attempt, RepairAttempt)
    assert attempt.feature_id == "feat-a"
    assert attempt.group_id == "g1"
    assert attempt.succeeded is True
    assert attempt.cost_usd == pytest.approx(0.42)
    assert attempt.wall_s == pytest.approx(1.5)
    assert "fixed it" in attempt.detail


def test_layer2_uses_resume_session_and_persistent_log_dir(tmp_path: Path) -> None:
    """Layer 2 continuity must survive process restarts via repair logs."""
    spec = _spec_with_two_features()
    seen: list[tuple[str, Path | None, int]] = []

    async def recording_build_agent(agent_input: BuildAgentInput) -> BuildAgentOutput:
        seen.append((
            agent_input.agent_session_id,
            agent_input.log_dir,
            agent_input.attempt,
        ))
        return BuildAgentOutput(
            succeeded=True,
            cost_usd=0.0,
            wall_s=0.1,
            detail="fixed",
            session_id="thread-after-repair",
        )

    session_dir = tmp_path / "sess"
    bridge = _make_layer2_fix_agent(
        fix_agent=recording_build_agent,
        spec=spec,
        project_dir=tmp_path,
        session_dir=session_dir,
        base_url=None,
        resume_agent_sessions={"feat-a": "thread-before-repair"},
    )

    failing = FailingFeature(feature_id="feat-a", verdict="partial", detail="d")
    asyncio.run(bridge(failing, spec.groups[0]))
    asyncio.run(bridge(failing, spec.groups[0]))

    assert seen[0] == (
        "thread-before-repair",
        session_dir / "repair" / "feat-a" / "attempt-01",
        1,
    )
    assert seen[1] == (
        "thread-after-repair",
        session_dir / "repair" / "feat-a" / "attempt-02",
        2,
    )


def test_layer2_success_path(tmp_path: Path) -> None:
    """build_agent.succeeded=True → RepairAttempt.succeeded=True."""
    spec = _spec_with_two_features()

    async def succeeding(agent_input: BuildAgentInput) -> BuildAgentOutput:
        return BuildAgentOutput(succeeded=True, cost_usd=0.10, wall_s=2.0,
                                detail="done")

    bridge = _make_layer2_fix_agent(
        fix_agent=succeeding,
        spec=spec,
        project_dir=tmp_path,
        session_dir=tmp_path / "sess",
        base_url=None,
    )
    failing = FailingFeature(feature_id="feat-a", verdict="failed", detail="d")
    attempt = asyncio.run(bridge(failing, spec.groups[0]))
    assert attempt.succeeded is True
    assert attempt.cost_usd == pytest.approx(0.10)


def test_layer2_failure_path_records_exception(tmp_path: Path) -> None:
    """A build_agent that raises must surface as succeeded=False."""
    spec = _spec_with_two_features()

    async def crashing(agent_input: BuildAgentInput) -> BuildAgentOutput:
        raise RuntimeError("boom-from-fix-agent")

    bridge = _make_layer2_fix_agent(
        fix_agent=crashing,
        spec=spec,
        project_dir=tmp_path,
        session_dir=tmp_path / "sess",
        base_url=None,
    )
    failing = FailingFeature(feature_id="feat-a", verdict="failed", detail="d")
    attempt = asyncio.run(bridge(failing, spec.groups[0]))
    assert attempt.succeeded is False
    assert attempt.feature_id == "feat-a"
    assert attempt.group_id == "g1"
    assert "RuntimeError" in attempt.detail
    assert "boom-from-fix-agent" in attempt.detail
    # Wall time should still be recorded (even if zero-ish on fast crash).
    assert attempt.wall_s >= 0.0


def test_layer2_dispatches_each_feature_with_its_own_id(tmp_path: Path) -> None:
    """Multiple failing features → each dispatch carries its own feature_id."""
    spec = _spec_with_two_features()
    seen_ids: list[str] = []

    async def recording(agent_input: BuildAgentInput) -> BuildAgentOutput:
        seen_ids.append(agent_input.feature_id)
        return BuildAgentOutput(succeeded=False, cost_usd=0.0, wall_s=0.0,
                                detail=f"saw {agent_input.feature_id}")

    bridge = _make_layer2_fix_agent(
        fix_agent=recording,
        spec=spec,
        project_dir=tmp_path,
        session_dir=tmp_path / "sess",
        base_url=None,
    )
    group = spec.groups[0]
    f_a = FailingFeature(feature_id="feat-a", verdict="partial", detail="da")
    f_b = FailingFeature(feature_id="feat-b", verdict="partial", detail="db")

    a_attempt = asyncio.run(bridge(f_a, group))
    b_attempt = asyncio.run(bridge(f_b, group))

    assert seen_ids == ["feat-a", "feat-b"]
    assert a_attempt.feature_id == "feat-a"
    assert b_attempt.feature_id == "feat-b"
    assert a_attempt.succeeded is False
    assert b_attempt.succeeded is False


def test_layer2_prompt_narrowing_includes_feature_id(tmp_path: Path) -> None:
    """The build prompt must mention the failing feature when feature_id is set.

    This protects the load-bearing piece: feature_id only matters if it
    actually reaches the prompt. If the prompt template ever stops
    rendering the narrowing preamble, this test breaks loudly.
    """
    from otto.build import _build_agent_prompt

    spec = _spec_with_two_features()
    agent_input = BuildAgentInput(
        spec=spec,
        group=spec.groups[0],
        project_dir=tmp_path,
        worktree=tmp_path,
        branch="",
        attempt=1,
        last_failure_narrative="login button does nothing",
        feature_id="feat-a",
    )
    rendered = _build_agent_prompt(agent_input)
    assert "FIX ONLY THE FAILING FEATURE" not in rendered
    assert "Repair the failing acceptance cluster" in rendered
    assert "feat-a" in rendered
    assert "Feature A" in rendered
    assert "login button does nothing" in rendered
    assert "shared-contract edits are allowed" in rendered


def test_layer2_prompt_cluster_repair_does_not_claim_siblings_passed(tmp_path: Path) -> None:
    """Cluster repair should tell the agent to fix related failures together."""
    from otto.build import _build_agent_prompt

    spec = _spec_with_two_features()
    agent_input = BuildAgentInput(
        spec=spec,
        group=spec.groups[0],
        project_dir=tmp_path,
        worktree=tmp_path,
        branch="",
        attempt=1,
        last_failure_narrative="Both the form and filter controls are missing.",
        feature_id="feat-a",
        related_feature_ids=("feat-a", "feat-b"),
    )
    rendered = _build_agent_prompt(agent_input)
    assert "Repair the failing acceptance cluster" in rendered
    assert "feat-a" in rendered
    assert "feat-b" in rendered
    assert "already passed audit" not in rendered
    assert "do not treat failing sibling features as off-limits" in rendered


def test_layer2_prompt_no_narrowing_when_feature_id_empty(tmp_path: Path) -> None:
    """Default empty feature_id must NOT add the Layer 2 preamble (back-compat)."""
    from otto.build import _build_agent_prompt

    spec = _spec_with_two_features()
    agent_input = BuildAgentInput(
        spec=spec,
        group=spec.groups[0],
        project_dir=tmp_path,
        worktree=tmp_path,
        branch="",
        attempt=1,
    )
    rendered = _build_agent_prompt(agent_input)
    assert "FIX ONLY THE FAILING FEATURE" not in rendered
    assert "Repair the failing acceptance cluster" not in rendered
