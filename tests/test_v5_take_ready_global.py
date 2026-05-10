"""Regression: take_ready must skip globally-completed tasks across recursion.

The URL-shortener live run thrashed because _process_children's local
'completed' set didn't see siblings completed in earlier recursive calls,
so v5_pending entries got re-dispatched.
"""
from pathlib import Path

from otto.queue.subtask import enqueue_subtask, take_ready
from otto.queue.task_graph import record_task, set_verdict


def test_take_ready_skips_globally_completed(tmp_path: Path) -> None:
    project_dir = tmp_path
    parent_session = tmp_path / "session"
    parent_session.mkdir()

    # Enqueue 3 children — all ready.
    a = enqueue_subtask(project_dir=project_dir, parent_task_id="root",
                       parent_session_dir=parent_session, intent="task A")
    b = enqueue_subtask(project_dir=project_dir, parent_task_id="root",
                       parent_session_dir=parent_session, intent="task B")
    c = enqueue_subtask(project_dir=project_dir, parent_task_id="root",
                       parent_session_dir=parent_session, intent="task C")
    record_task(project_dir, task_id=a, intent="A", parent_task_id="root")
    record_task(project_dir, task_id=b, intent="B", parent_task_id="root")
    record_task(project_dir, task_id=c, intent="C", parent_task_id="root")

    # Local set says nothing is complete; pending file has all 3.
    ready = take_ready(project_dir, completed_task_ids=set(), in_flight_task_ids=set())
    assert {r["task_id"] for r in ready} == {a, b, c}

    # Mark A and B done in the GRAPH (not the local set).
    set_verdict(project_dir, a, "pass")
    set_verdict(project_dir, b, "pass")

    # New caller, fresh local set — should still skip A and B because the
    # graph says they're terminal.
    ready = take_ready(project_dir, completed_task_ids=set(), in_flight_task_ids=set())
    assert {r["task_id"] for r in ready} == {c}


def test_take_ready_respects_depends_on_via_graph(tmp_path: Path) -> None:
    project_dir = tmp_path
    parent_session = tmp_path / "session"
    parent_session.mkdir()

    a = enqueue_subtask(project_dir=project_dir, parent_task_id="root",
                       parent_session_dir=parent_session, intent="task A")
    # B depends on A.
    b = enqueue_subtask(project_dir=project_dir, parent_task_id="root",
                       parent_session_dir=parent_session, intent="task B",
                       depends_on=[a])
    record_task(project_dir, task_id=a, intent="A", parent_task_id="root")
    record_task(project_dir, task_id=b, intent="B", parent_task_id="root", depends_on=[a])

    # B not ready until A is graph-terminal.
    ready = take_ready(project_dir, completed_task_ids=set(), in_flight_task_ids=set())
    assert {r["task_id"] for r in ready} == {a}

    set_verdict(project_dir, a, "pass")
    ready = take_ready(project_dir, completed_task_ids=set(), in_flight_task_ids=set())
    assert {r["task_id"] for r in ready} == {b}
