"""Phase 2 smoke tests — hierarchy + bottom-up integration.

Tests Otto-side plumbing for hierarchical decomposition:
  - v5_runner's child scheduling respects depends_on.
  - All-children-resolved triggers integration phase.
  - Tree budget cap halts new dispatches.
  - Crashed child's verdict propagates as catastrophic; siblings continue.
  - Verdict aggregation honors severity-monotonicity.

These are pure-Python / async tests using stub Lead invocations. The actual
LLM-driven Lead behavior is covered by /tmp/sdk-smoke/test_queue_recursion.py
and /tmp/sdk-smoke/test_concurrency.py (already verified).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from otto.lead import LeadResult
from otto.queue.subtask import enqueue_subtask
from otto.queue.task_graph import (
    aggregate_verdict,
    get_task,
    record_task,
    set_decomposition,
    set_verdict,
)
from otto.v5_runner import (
    _DispatchLease,
    _build_child_summaries,
    _is_descendant_of,
    _process_children,
    run_v5_pipeline,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "otto_logs").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# Topology helpers
# ---------------------------------------------------------------------------


class TestTopologyHelpers:
    def test_is_descendant_of_simple(self, project: Path) -> None:
        record_task(project, task_id="root", intent="r")
        record_task(project, task_id="c1", intent="c1", parent_task_id="root")
        record_task(project, task_id="g1", intent="g1", parent_task_id="c1")
        assert _is_descendant_of(project, "c1", "root") is True
        assert _is_descendant_of(project, "g1", "c1") is True
        # Deep descendant: should walk up the chain.
        assert _is_descendant_of(project, "g1", "root") is True
        assert _is_descendant_of(project, "root", "c1") is False

    def test_build_child_summaries(self, project: Path) -> None:
        record_task(project, task_id="root", intent="r")
        record_task(project, task_id="c1", intent="c1 intent", parent_task_id="root")
        record_task(project, task_id="c2", intent="c2 intent", parent_task_id="root")
        results = {
            "c1": LeadResult(task_id="c1", verdict="pass", cost_usd=0.5, final_text="ok"),
            "c2": LeadResult(task_id="c2", verdict="partial", cost_usd=1.5, final_text="some failed"),
        }
        summaries = _build_child_summaries(project, "root", results)
        assert len(summaries) == 2
        c1 = next(s for s in summaries if s["task_id"] == "c1")
        assert c1["verdict"] == "pass"
        assert c1["cost_usd"] == 0.5
        c2 = next(s for s in summaries if s["task_id"] == "c2")
        assert c2["verdict"] == "partial"

    def test_build_child_summaries_reconstructs_decomposed_child(
        self, project: Path
    ) -> None:
        record_task(project, task_id="root", intent="r")
        record_task(project, task_id="c1", intent="decomposed child", parent_task_id="root")
        record_task(project, task_id="g1", intent="grandchild", parent_task_id="c1")
        set_decomposition(project, "c1", "emit")
        set_verdict(project, "c1", "pending_children")
        set_verdict(project, "g1", "pass")

        child_results = {
            "c1": LeadResult(
                task_id="c1",
                verdict="pending_children",
                cost_usd=0.5,
                final_text="planned grandchildren",
            ),
            "g1": LeadResult(task_id="g1", verdict="pass", cost_usd=0.2),
        }
        integration_results = {
            "c1": LeadResult(
                task_id="c1",
                verdict="partial",
                cost_usd=0.7,
                final_text="integrated grandchildren with one skipped item",
                verify_called=True,
                verify_result={
                    "verdict": "partial",
                    "intent_coverage": {
                        "built": ["grandchild feature"],
                        "partial": [],
                        "skipped": [{"feature": "export", "reason": "not in subtree"}],
                    },
                },
            )
        }

        summaries = _build_child_summaries(
            project,
            "root",
            child_results,
            integration_results,
        )

        c1 = next(s for s in summaries if s["task_id"] == "c1")
        assert c1["verdict"] == "partial"
        assert c1["reconstructed_from"] == "subtree_integration"
        assert c1["intent_coverage"]["skipped"][0]["feature"] == "export"


# ---------------------------------------------------------------------------
# Child processing (stubbed run_lead)
# ---------------------------------------------------------------------------


class TestProcessChildren:
    @pytest.mark.asyncio
    async def test_child_processing_respects_depends_on(self, project: Path) -> None:
        """Children with depends_on do not run until their dependencies complete."""
        record_task(project, task_id="root", intent="r")
        a = enqueue_subtask(
            project_dir=project,
            parent_task_id="root",
            parent_session_dir=project / "session",
            intent="A",
        )
        b = enqueue_subtask(
            project_dir=project,
            parent_task_id="root",
            parent_session_dir=project / "session",
            intent="B",
            depends_on=[a],
        )
        record_task(project, task_id=a, intent="A", parent_task_id="root")
        record_task(project, task_id=b, intent="B", parent_task_id="root")

        order: list[str] = []

        async def fake_run_lead(**kwargs: Any) -> LeadResult:
            tid = kwargs["task_id"]
            order.append(tid)
            await asyncio.sleep(0.01)  # ensure interleaving is possible
            return LeadResult(
                task_id=tid, verdict="pass", cost_usd=0.1, decomposition="inline"
            )

        with patch("otto.v5_runner.run_lead", new=fake_run_lead):
            child_results: dict[str, LeadResult] = {}
            integration_results: dict[str, LeadResult] = {}
            await _process_children(
                project_dir=project,
                parent_task_id="root",
                config={},
                max_parallel=3,
                tree_budget_usd=10.0,
                child_results=child_results,
                integration_results=integration_results,
            )
        # Both ran.
        assert set(child_results.keys()) == {a, b}
        # A ran before B (depends_on respected).
        assert order.index(a) < order.index(b)

    @pytest.mark.asyncio
    async def test_crashed_child_propagates_catastrophic_siblings_continue(
        self, project: Path
    ) -> None:
        """A child that raises sets verdict=catastrophic; sibling still runs."""
        record_task(project, task_id="root", intent="r")
        a = enqueue_subtask(
            project_dir=project,
            parent_task_id="root",
            parent_session_dir=project / "session",
            intent="A_will_crash",
        )
        b = enqueue_subtask(
            project_dir=project,
            parent_task_id="root",
            parent_session_dir=project / "session",
            intent="B_works",
        )
        record_task(project, task_id=a, intent="A", parent_task_id="root")
        record_task(project, task_id=b, intent="B", parent_task_id="root")

        async def fake_run_lead(**kwargs: Any) -> LeadResult:
            tid = kwargs["task_id"]
            if tid == a:
                raise RuntimeError("simulated crash")
            return LeadResult(task_id=tid, verdict="pass", cost_usd=0.1)

        with patch("otto.v5_runner.run_lead", new=fake_run_lead):
            child_results: dict[str, LeadResult] = {}
            integration_results: dict[str, LeadResult] = {}
            await _process_children(
                project_dir=project,
                parent_task_id="root",
                config={},
                max_parallel=3,
                tree_budget_usd=10.0,
                child_results=child_results,
                integration_results=integration_results,
            )
        # B ran successfully.
        assert b in child_results
        assert child_results[b].verdict == "pass"
        # A's crash recorded as catastrophic in task_graph (best-effort invariant).
        a_entry = get_task(project, a) or {}
        assert a_entry.get("verdict") == "catastrophic"

    @pytest.mark.asyncio
    async def test_tree_budget_cap_halts_new_dispatches(self, project: Path) -> None:
        """When cumulative cost exceeds tree_budget_usd, no new tasks dispatch."""
        record_task(project, task_id="root", intent="r")
        # Pre-populate root with enough cost to be over the cap.
        from otto.queue.task_graph import add_cost
        add_cost(project, "root", 50.0)

        a = enqueue_subtask(
            project_dir=project,
            parent_task_id="root",
            parent_session_dir=project / "session",
            intent="A",
        )
        record_task(project, task_id=a, intent="A", parent_task_id="root")

        dispatched: list[str] = []

        async def fake_run_lead(**kwargs: Any) -> LeadResult:
            dispatched.append(kwargs["task_id"])
            return LeadResult(task_id=kwargs["task_id"], verdict="pass", cost_usd=0.1)

        with patch("otto.v5_runner.run_lead", new=fake_run_lead):
            child_results: dict[str, LeadResult] = {}
            integration_results: dict[str, LeadResult] = {}
            await _process_children(
                project_dir=project,
                parent_task_id="root",
                config={},
                max_parallel=3,
                tree_budget_usd=10.0,  # already exceeded by root's pre-populated $50
                child_results=child_results,
                integration_results=integration_results,
            )
        # No children dispatched because cap was already hit.
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_shared_dispatch_lease_caps_concurrent_schedulers(
        self,
        project: Path,
    ) -> None:
        record_task(project, task_id="root", intent="r")
        task_ids = [
            enqueue_subtask(
                project_dir=project,
                parent_task_id="root",
                parent_session_dir=project / "session",
                intent=f"task {index}",
            )
            for index in range(6)
        ]
        for tid in task_ids:
            record_task(project, task_id=tid, intent=tid, parent_task_id="root")

        lock = asyncio.Lock()
        active: set[str] = set()
        max_seen = 0
        dispatch_counts: dict[str, int] = {tid: 0 for tid in task_ids}

        async def fake_run_child(**kwargs: Any) -> LeadResult:
            nonlocal max_seen
            tid = kwargs["entry"]["task_id"]
            async with lock:
                dispatch_counts[tid] += 1
                active.add(tid)
                max_seen = max(max_seen, len(active))
            await asyncio.sleep(0.02)
            set_verdict(project, tid, "pass")
            async with lock:
                active.remove(tid)
            return LeadResult(task_id=tid, verdict="pass", cost_usd=0.1)

        lease = _DispatchLease(max_parallel=3)
        child_results: dict[str, LeadResult] = {}
        integration_results: dict[str, LeadResult] = {}

        with patch("otto.v5_runner._run_child", new=fake_run_child):
            await asyncio.gather(
                _process_children(
                    project_dir=project,
                    parent_task_id="root",
                    config={},
                    max_parallel=3,
                    tree_budget_usd=10.0,
                    child_results=child_results,
                    integration_results=integration_results,
                    dispatch_lease=lease,
                ),
                _process_children(
                    project_dir=project,
                    parent_task_id="root",
                    config={},
                    max_parallel=3,
                    tree_budget_usd=10.0,
                    child_results=child_results,
                    integration_results=integration_results,
                    dispatch_lease=lease,
                ),
            )

        assert max_seen <= 3
        assert dispatch_counts == {tid: 1 for tid in task_ids}
        assert set(child_results) == set(task_ids)


# ---------------------------------------------------------------------------
# Aggregation + invariants
# ---------------------------------------------------------------------------


class TestVerdictPropagation:
    def test_parent_pending_children_resolves_to_worst_when_children_done(
        self, project: Path
    ) -> None:
        """Once children resolve, aggregate_verdict reflects the worst."""
        record_task(project, task_id="root", intent="r")
        record_task(project, task_id="c1", intent="c1", parent_task_id="root")
        record_task(project, task_id="c2", intent="c2", parent_task_id="root")
        set_verdict(project, "root", "pending_children")
        set_verdict(project, "c1", "pass")
        set_verdict(project, "c2", "merge_blocked")
        # Aggregate: max(pending_children, pass, merge_blocked) = merge_blocked.
        assert aggregate_verdict(project, "root") == "merge_blocked"

    def test_three_level_aggregation(self, project: Path) -> None:
        """Verdict propagates through 3 levels."""
        record_task(project, task_id="root", intent="r")
        record_task(project, task_id="c1", intent="c1", parent_task_id="root")
        record_task(project, task_id="g1", intent="g1", parent_task_id="c1")
        set_verdict(project, "root", "pass")
        set_verdict(project, "c1", "pass")
        set_verdict(project, "g1", "catastrophic")
        # c1's aggregate from g1 = catastrophic.
        assert aggregate_verdict(project, "c1") == "catastrophic"
        # root's aggregate from c1's children = ... but aggregate_verdict only
        # walks one level. The runner is responsible for recursive aggregation.
        # Check at least the immediate children's worst:
        assert aggregate_verdict(project, "root") == "pass"
        # Now if c1's verdict gets updated to its aggregate, root sees it.
        set_verdict(project, "c1", "catastrophic")
        assert aggregate_verdict(project, "root") == "catastrophic"


# ---------------------------------------------------------------------------
# v5_runner end-to-end with all calls stubbed
# ---------------------------------------------------------------------------


class TestRunV5PipelineStubbed:
    @pytest.mark.asyncio
    async def test_root_inline_no_children(self, project: Path) -> None:
        """Root inlines (begin_inline) → no children → root verdict from Lead."""

        async def fake_compile(**kwargs: Any) -> Any:
            from otto.spec_compile_flat import FlatSpec
            return FlatSpec(intent=kwargs["intent"], behavior_journeys=[])

        async def fake_run_lead(**kwargs: Any) -> LeadResult:
            tid = kwargs["task_id"]
            kind = kwargs.get("kind", "plan_or_inline")
            if kind == "integration":
                return LeadResult(task_id=tid, verdict="pass", cost_usd=0.05)
            # Inline run: root sets decomposition=inline and produces pass.
            set_decomposition(project, tid, "inline")
            return LeadResult(
                task_id=tid, verdict="pass", cost_usd=0.5,
                decomposition="inline", verify_called=True,
            )

        with patch("otto.v5_runner.compile_flat_spec", new=fake_compile), \
             patch("otto.v5_runner.run_lead", new=fake_run_lead):
            result = await run_v5_pipeline(
                project_dir=project,
                intent="trivial",
                config={},
                tree_budget_usd=10.0,
            )
        assert result.verdict == "pass"
        assert result.root_lead_result is not None
        assert result.root_lead_result.decomposition == "inline"
        # No integration step because no children.
        assert "root" not in result.integration_results

    @pytest.mark.asyncio
    async def test_root_with_two_children_aggregates(self, project: Path) -> None:
        """Root emits 2 children, integration runs, root verdict = integration verdict."""

        async def fake_compile(**kwargs: Any) -> Any:
            from otto.spec_compile_flat import FlatSpec
            return FlatSpec(intent=kwargs["intent"], behavior_journeys=[])

        async def fake_run_lead(**kwargs: Any) -> LeadResult:
            tid = kwargs["task_id"]
            kind = kwargs.get("kind", "plan_or_inline")
            if kind == "integration":
                return LeadResult(task_id=tid, verdict="partial", cost_usd=0.05)
            if tid == "root":
                # Root emits two children.
                a = enqueue_subtask(
                    project_dir=project, parent_task_id="root",
                    parent_session_dir=project / "session", intent="A",
                )
                b = enqueue_subtask(
                    project_dir=project, parent_task_id="root",
                    parent_session_dir=project / "session", intent="B",
                )
                record_task(project, task_id=a, intent="A", parent_task_id="root")
                record_task(project, task_id=b, intent="B", parent_task_id="root")
                set_decomposition(project, "root", "emit")
                return LeadResult(
                    task_id="root", verdict="pending_children", cost_usd=0.1,
                    decomposition="emit", emitted_subtask_ids=[a, b],
                )
            # Children run inline.
            set_decomposition(project, tid, "inline")
            return LeadResult(
                task_id=tid, verdict="pass", cost_usd=0.2,
                decomposition="inline", verify_called=True,
            )

        with patch("otto.v5_runner.compile_flat_spec", new=fake_compile), \
             patch("otto.v5_runner.run_lead", new=fake_run_lead):
            result = await run_v5_pipeline(
                project_dir=project,
                intent="multi-feature",
                config={},
                tree_budget_usd=10.0,
            )
        # Integration's verdict (partial) is the final root verdict.
        assert result.verdict == "partial"
        # Children both ran.
        assert len(result.child_results) == 2
        # Integration ran.
        assert "root" in result.integration_results
        assert result.integration_results["root"].verdict == "partial"
