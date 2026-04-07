import threading

import pytest

from otto.context import PipelineContext, TaskResult
from otto.cost_tracker import CostTracker


def test_basic_record_and_query():
    tracker = CostTracker()

    tracker.record(kind="coding", task_key="task-1", amount_usd=0.42)
    tracker.record(kind="spec", task_key="task-1", amount_usd=0.08)
    tracker.record(kind="qa", task_key="task-2", amount_usd=0.15)

    assert tracker.task_total("task-1") == pytest.approx(0.50)
    assert tracker.task_total("task-2") == pytest.approx(0.15)
    assert tracker.run_total() == pytest.approx(0.65)
    assert tracker.task_phase_breakdown("task-1") == {
        "coding": pytest.approx(0.42),
        "spec": pytest.approx(0.08),
    }

    snapshot = tracker.task_snapshot("task-1")
    assert snapshot["total_cost_usd"] == pytest.approx(0.50)
    assert snapshot["phase_costs"]["coding"] == pytest.approx(0.42)


def test_shared_allocation_batch_qa_split():
    tracker = CostTracker()

    tracker.record(
        kind="batch_qa",
        allocations={"task-a": 0.30, "task-b": 0.30, "task-c": 0.40},
    )

    assert tracker.run_total() == pytest.approx(1.0)
    assert tracker.task_total("task-a") == pytest.approx(0.30)
    assert tracker.task_total("task-b") == pytest.approx(0.30)
    assert tracker.task_total("task-c") == pytest.approx(0.40)
    assert tracker.task_phase_breakdown("task-c") == {"batch_qa": pytest.approx(0.40)}


def test_thread_safety():
    tracker = CostTracker()
    thread_count = 8
    writes_per_thread = 250
    amount = 0.01
    barrier = threading.Barrier(thread_count)

    def worker() -> None:
        barrier.wait()
        for _ in range(writes_per_thread):
            tracker.record(kind="coding", task_key="task-1", amount_usd=amount)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    expected = thread_count * writes_per_thread * amount
    assert tracker.task_total("task-1") == pytest.approx(expected)
    assert tracker.run_total() == pytest.approx(expected)


def test_retry_does_not_lose_prior_attempt_cost():
    tracker = CostTracker()

    tracker.record(kind="coding", task_key="task-1", amount_usd=0.25)
    tracker.record(kind="coding", task_key="task-1", amount_usd=0.35)
    tracker.record(kind="qa", task_key="task-1", amount_usd=0.10)

    assert tracker.task_total("task-1") == pytest.approx(0.70)
    assert tracker.task_phase_breakdown("task-1") == {
        "coding": pytest.approx(0.60),
        "qa": pytest.approx(0.10),
    }


def test_rollback_does_not_lose_cost():
    ctx = PipelineContext()

    ctx.costs.record(kind="coding", task_key="task-1", amount_usd=0.40)
    ctx.costs.record(kind="batch_qa", allocations={"task-1": 0.20, "task-2": 0.20})
    ctx.add_failure(TaskResult(task_key="task-1", success=False, error_code="batch_qa_rolled_back"))

    assert ctx.total_cost == pytest.approx(0.80)
    assert ctx.costs.task_total("task-1") == pytest.approx(0.60)
    assert ctx.costs.task_total("task-2") == pytest.approx(0.20)
