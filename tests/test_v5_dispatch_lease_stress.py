from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from otto.lead import LeadResult
from otto.queue.subtask import v5_pending_path
from otto.queue.task_graph import record_task, set_decomposition, set_verdict
from otto.v5_branching import integration_branch_name
from otto.v5_runner import _DispatchLease, _process_children


def _append_pending(project_dir: Path, task_id: str, parent_task_id: str) -> None:
    entry = {
        "schema_version": 1,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "parent_session_dir": str(project_dir / "session"),
        "intent": f"Build {task_id}",
        "depends_on": [],
        "owned_paths": [],
        "action_ids": [],
        "integration_branch": integration_branch_name(parent_task_id),
        "review_state": "approved",
        "enqueued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = v5_pending_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    record_task(project_dir, task_id=task_id, intent=entry["intent"], parent_task_id=parent_task_id)


@pytest.mark.asyncio
async def test_global_dispatch_lease_caps_nested_parallel_schedulers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record_task(tmp_path, task_id="root", intent="root")
    for tid in ("v5-root-a", "v5-decompose", "v5-root-b"):
        _append_pending(tmp_path, tid, "root")

    active: set[str] = set()
    lock = asyncio.Lock()
    max_seen = 0
    dispatch_counts: dict[str, int] = {}

    async def fake_run_child(**kwargs: Any) -> LeadResult:
        nonlocal max_seen
        tid = kwargs["entry"]["task_id"]
        async with lock:
            dispatch_counts[tid] = dispatch_counts.get(tid, 0) + 1
            active.add(tid)
            max_seen = max(max_seen, len(active))
        await asyncio.sleep(0.02)
        if tid == "v5-decompose":
            for grandchild in ("v5-grand-a", "v5-grand-b", "v5-grand-c"):
                _append_pending(tmp_path, grandchild, tid)
            set_decomposition(tmp_path, tid, "emit")
            set_verdict(tmp_path, tid, "pending_children")
            result = LeadResult(
                task_id=tid,
                verdict="pending_children",
                decomposition="emit",
                emitted_subtask_ids=["v5-grand-a", "v5-grand-b", "v5-grand-c"],
            )
        else:
            set_decomposition(tmp_path, tid, "inline")
            set_verdict(tmp_path, tid, "pass")
            result = LeadResult(task_id=tid, verdict="pass", decomposition="inline")
        async with lock:
            active.remove(tid)
        return result

    async def fake_run_integration(**kwargs: Any) -> LeadResult:
        task_id = kwargs["task_id"]
        set_verdict(tmp_path, task_id, "partial")
        result = LeadResult(task_id=task_id, verdict="partial", decomposition="inline")
        kwargs["integration_results"][task_id] = result
        return result

    lease = _DispatchLease(max_parallel=2)
    child_results: dict[str, LeadResult] = {}
    integration_results: dict[str, LeadResult] = {}
    monkeypatch.setattr("otto.v5_runner._run_child", fake_run_child)
    monkeypatch.setattr("otto.v5_runner._run_integration", fake_run_integration)

    await asyncio.gather(
        _process_children(
            project_dir=tmp_path,
            parent_task_id="root",
            config={},
            max_parallel=2,
            tree_budget_usd=10.0,
            child_results=child_results,
            integration_results=integration_results,
            dispatch_lease=lease,
        ),
        _process_children(
            project_dir=tmp_path,
            parent_task_id="root",
            config={},
            max_parallel=2,
            tree_budget_usd=10.0,
            child_results=child_results,
            integration_results=integration_results,
            dispatch_lease=lease,
        ),
    )

    expected = {
        "v5-root-a",
        "v5-decompose",
        "v5-root-b",
        "v5-grand-a",
        "v5-grand-b",
        "v5-grand-c",
    }
    assert max_seen <= 2
    assert dispatch_counts == {tid: 1 for tid in expected}
    assert expected.issubset(child_results)
    assert integration_results["v5-decompose"].verdict == "partial"
