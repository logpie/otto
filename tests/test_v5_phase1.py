"""Phase 1 smoke tests for Otto v5.

These are the gates for Phase 1 per plan-v5 §13/§14. Each test verifies one
invariant. If any fails, Phase 1 doesn't ship.

These tests are PURE-PYTHON (no live LLM calls); the SDK-level smoke tests in
``/tmp/sdk-smoke/`` cover the live-Lead behavior. These cover the Otto-side
plumbing: task_graph, subtask queue, spec_compile_flat lint, verdict
computation rules.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from otto.queue.task_graph import (
    aggregate_verdict,
    all_children_resolved,
    add_cost,
    children_of,
    get_task,
    read_graph,
    record_task,
    set_decomposition,
    set_verdict,
    tree_total_cost,
)
from otto.queue.subtask import (
    enqueue_subtask,
    read_pending,
    take_ready,
    v5_pending_path,
)
from otto.spec_compile_flat import FlatSpec, lint_journey, lint_spec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Empty project dir."""
    (tmp_path / "otto_logs").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# task_graph.py
# ---------------------------------------------------------------------------


class TestTaskGraph:
    def test_empty_graph_initially(self, project: Path) -> None:
        graph = read_graph(project)
        assert graph["schema_version"] == 1
        assert graph["tasks"] == {}

    def test_record_task_creates_entry(self, project: Path) -> None:
        record_task(project, task_id="t1", intent="build foo")
        entry = get_task(project, "t1")
        assert entry is not None
        assert entry["intent"] == "build foo"
        assert entry["parent_task_id"] is None
        assert entry["decomposition"] == "unknown"
        assert entry["verdict"] is None

    def test_parent_child_link_recorded(self, project: Path) -> None:
        record_task(project, task_id="root", intent="root intent")
        record_task(project, task_id="child", intent="child intent", parent_task_id="root")
        assert children_of(project, "root") == ["child"]
        child_entry = get_task(project, "child")
        assert child_entry is not None
        assert child_entry["parent_task_id"] == "root"

    def test_set_decomposition_persists(self, project: Path) -> None:
        record_task(project, task_id="t1", intent="x")
        set_decomposition(project, "t1", "inline")
        assert (get_task(project, "t1") or {})["decomposition"] == "inline"
        set_decomposition(project, "t1", "emit")
        assert (get_task(project, "t1") or {})["decomposition"] == "emit"

    def test_set_verdict_persists(self, project: Path) -> None:
        record_task(project, task_id="t1", intent="x")
        set_verdict(project, "t1", "pass", cost_usd=0.42)
        entry = get_task(project, "t1") or {}
        assert entry["verdict"] == "pass"
        assert entry["cost_usd"] == 0.42
        assert entry["completed_at"] is not None

    def test_add_cost_accumulates(self, project: Path) -> None:
        record_task(project, task_id="t1", intent="x")
        add_cost(project, "t1", 0.10)
        add_cost(project, "t1", 0.30)
        assert (get_task(project, "t1") or {})["cost_usd"] == pytest.approx(0.40)

    def test_all_children_resolved_false_until_all_terminal(self, project: Path) -> None:
        record_task(project, task_id="root", intent="r")
        record_task(project, task_id="c1", intent="c1", parent_task_id="root")
        record_task(project, task_id="c2", intent="c2", parent_task_id="root")
        assert all_children_resolved(project, "root") is False
        set_verdict(project, "c1", "pass")
        assert all_children_resolved(project, "root") is False
        set_verdict(project, "c2", "partial")
        assert all_children_resolved(project, "root") is True

    def test_aggregate_verdict_worst_wins(self, project: Path) -> None:
        record_task(project, task_id="root", intent="r")
        record_task(project, task_id="c1", intent="c1", parent_task_id="root")
        record_task(project, task_id="c2", intent="c2", parent_task_id="root")
        set_verdict(project, "c1", "pass")
        set_verdict(project, "c2", "partial")
        # Root has no own verdict; aggregate from children.
        assert aggregate_verdict(project, "root") == "partial"
        # Now make a child catastrophic; root aggregates to catastrophic.
        set_verdict(project, "c2", "catastrophic")
        assert aggregate_verdict(project, "root") == "catastrophic"

    def test_aggregate_verdict_parent_pass_with_child_partial_propagates(
        self, project: Path
    ) -> None:
        """Parent verdict NEVER more optimistic than worst child (philosophy invariant)."""
        record_task(project, task_id="root", intent="r")
        record_task(project, task_id="c1", intent="c1", parent_task_id="root")
        set_verdict(project, "root", "pass")
        set_verdict(project, "c1", "partial")
        # Root claimed pass, but child is partial → aggregate must be partial.
        assert aggregate_verdict(project, "root") == "partial"

    def test_tree_total_cost_walks_subtree(self, project: Path) -> None:
        record_task(project, task_id="root", intent="r")
        record_task(project, task_id="c1", intent="c1", parent_task_id="root")
        record_task(project, task_id="g1", intent="g1", parent_task_id="c1")
        add_cost(project, "root", 1.00)
        add_cost(project, "c1", 2.00)
        add_cost(project, "g1", 3.00)
        assert tree_total_cost(project, "root") == pytest.approx(6.00)
        assert tree_total_cost(project, "c1") == pytest.approx(5.00)
        assert tree_total_cost(project, "g1") == pytest.approx(3.00)


# ---------------------------------------------------------------------------
# subtask.py
# ---------------------------------------------------------------------------


class TestSubtaskQueue:
    def test_enqueue_writes_pending_entry(self, project: Path) -> None:
        tid = enqueue_subtask(
            project_dir=project,
            parent_task_id="root",
            parent_session_dir=project / "session",
            intent="do thing",
        )
        assert tid.startswith("v5-")
        pending = read_pending(project)
        assert len(pending) == 1
        assert pending[0]["task_id"] == tid
        assert pending[0]["parent_task_id"] == "root"
        assert pending[0]["intent"] == "do thing"
        assert pending[0]["depends_on"] == []
        # Root's integration is the project's main branch — sibling tasks
        # don't get their own ref nesting under root.
        assert pending[0]["integration_branch"] == "main"

    def test_enqueue_records_depends_on(self, project: Path) -> None:
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
        pending = read_pending(project)
        b_entry = next(p for p in pending if p["task_id"] == b)
        assert b_entry["depends_on"] == [a]

    def test_enqueue_rejects_empty_intent(self, project: Path) -> None:
        with pytest.raises(ValueError):
            enqueue_subtask(
                project_dir=project,
                parent_task_id="root",
                parent_session_dir=project / "session",
                intent="   ",
            )

    def test_take_ready_respects_depends_on(self, project: Path) -> None:
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
        # A is ready (no deps); B is not (depends on A).
        ready = take_ready(project, completed_task_ids=set(), in_flight_task_ids=set())
        assert {r["task_id"] for r in ready} == {a}
        # Once A completes, B becomes ready.
        ready = take_ready(project, completed_task_ids={a}, in_flight_task_ids=set())
        assert {r["task_id"] for r in ready} == {b}

    def test_take_ready_skips_in_flight(self, project: Path) -> None:
        a = enqueue_subtask(
            project_dir=project,
            parent_task_id="root",
            parent_session_dir=project / "session",
            intent="A",
        )
        ready = take_ready(project, completed_task_ids=set(), in_flight_task_ids={a})
        assert ready == []


# ---------------------------------------------------------------------------
# spec_compile_flat.py — lint
# ---------------------------------------------------------------------------


class TestSpecCompileFlatLint:
    def test_user_language_passes(self) -> None:
        text = (
            "User clicks 'Add Transaction', enters $50 with category 'Food', and saves. "
            "The new transaction appears in the list."
        )
        assert lint_journey(text) == []

    def test_css_class_selector_rejected(self) -> None:
        text = "Click the element with class='add-btn' and verify class='txn-list'."
        warnings = lint_journey(text)
        assert any("CSS class selector" in w for w in warnings)

    def test_dom_id_rejected(self) -> None:
        text = "Find element with id='form-x' and submit."
        warnings = lint_journey(text)
        assert any("DOM id selector" in w for w in warnings)

    def test_data_testid_rejected(self) -> None:
        text = "Verify data-testid is present."
        warnings = lint_journey(text)
        assert any("data-testid" in w for w in warnings)

    def test_getbyrole_rejected(self) -> None:
        text = "Click via getByRole('button', {name: 'Save'})."
        warnings = lint_journey(text)
        assert any("getByRole" in w for w in warnings)

    def test_querySelector_rejected(self) -> None:
        text = "Use document.querySelector('#main') to find."
        warnings = lint_journey(text)
        assert any("querySelector" in w for w in warnings)

    def test_lint_spec_aggregates_per_journey(self) -> None:
        spec = FlatSpec(
            intent="x",
            behavior_journeys=[
                {"id": "good", "description": "User clicks Save."},
                {"id": "bad", "description": "Click element with class='x'."},
                {"id": "ugly", "description": "Use getByRole and data-testid."},
            ],
        )
        warnings = lint_spec(spec)
        # 'good' produces no warnings.
        assert not any("'good'" in w for w in warnings)
        assert any("'bad'" in w for w in warnings)
        # 'ugly' has TWO warnings (getByRole + data-testid).
        ugly_warnings = [w for w in warnings if "'ugly'" in w]
        assert len(ugly_warnings) == 2


# ---------------------------------------------------------------------------
# Atomicity / concurrency under filesystem stress
# ---------------------------------------------------------------------------


class TestAtomicWrites:
    def test_task_graph_write_is_atomic_on_concurrent_records(
        self, project: Path
    ) -> None:
        """fcntl-locked writes don't corrupt the JSON under interleaved updates."""
        import threading

        def writer(start: int, count: int) -> None:
            for i in range(start, start + count):
                record_task(project, task_id=f"t{i}", intent=f"intent-{i}")

        threads = [
            threading.Thread(target=writer, args=(0, 25)),
            threading.Thread(target=writer, args=(25, 25)),
            threading.Thread(target=writer, args=(50, 25)),
            threading.Thread(target=writer, args=(75, 25)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        graph = read_graph(project)
        assert len(graph["tasks"]) == 100
        for i in range(100):
            entry = graph["tasks"][f"t{i}"]
            assert entry["intent"] == f"intent-{i}"

    def test_subtask_enqueue_atomic_under_concurrent_enqueues(
        self, project: Path
    ) -> None:
        """v5_pending.jsonl appends are line-atomic under fcntl."""
        import threading

        ids: list[str] = []
        ids_lock = threading.Lock()

        def enqueuer(start: int, count: int) -> None:
            for i in range(start, start + count):
                tid = enqueue_subtask(
                    project_dir=project,
                    parent_task_id="root",
                    parent_session_dir=project / "session",
                    intent=f"task-{i}",
                )
                with ids_lock:
                    ids.append(tid)

        threads = [
            threading.Thread(target=enqueuer, args=(0, 10)),
            threading.Thread(target=enqueuer, args=(10, 10)),
            threading.Thread(target=enqueuer, args=(20, 10)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 30 should have been recorded with no JSON corruption.
        pending = read_pending(project)
        assert len(pending) == 30
        # Ids returned to the callers all appear in the file.
        pending_ids = {p["task_id"] for p in pending}
        assert pending_ids == set(ids)
