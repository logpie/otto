from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from otto.lead import LeadResult
from otto.queue.subtask import enqueue_subtask, take_ready
from otto.queue.task_graph import (
    get_task,
    mark_reviewed_partial,
    record_task,
    set_verdict,
)
from otto.v5_runner import _run_child


def test_non_pass_upstream_is_non_runnable_but_not_dependency_satisfied(
    tmp_path: Path,
) -> None:
    parent_session = tmp_path / "session"
    parent_session.mkdir()
    upstream = enqueue_subtask(
        project_dir=tmp_path,
        parent_task_id="root",
        parent_session_dir=parent_session,
        intent="upstream",
    )
    downstream = enqueue_subtask(
        project_dir=tmp_path,
        parent_task_id="root",
        parent_session_dir=parent_session,
        intent="downstream",
        depends_on=[upstream],
    )
    set_verdict(tmp_path, upstream, "catastrophic")

    ready = take_ready(tmp_path, completed_task_ids=set(), in_flight_task_ids=set())

    ready_ids = {entry["task_id"] for entry in ready}
    assert upstream not in ready_ids
    assert downstream not in ready_ids


def test_only_pass_or_reviewed_partial_satisfies_dependency(tmp_path: Path) -> None:
    parent_session = tmp_path / "session"
    parent_session.mkdir()
    upstream = enqueue_subtask(
        project_dir=tmp_path,
        parent_task_id="root",
        parent_session_dir=parent_session,
        intent="upstream",
    )
    downstream = enqueue_subtask(
        project_dir=tmp_path,
        parent_task_id="root",
        parent_session_dir=parent_session,
        intent="downstream",
        depends_on=[upstream],
    )
    set_verdict(tmp_path, upstream, "partial")

    assert {
        entry["task_id"]
        for entry in take_ready(tmp_path, completed_task_ids=set(), in_flight_task_ids=set())
    } == set()

    mark_reviewed_partial(tmp_path, upstream, reason="operator accepted partial")

    assert {
        entry["task_id"]
        for entry in take_ready(tmp_path, completed_task_ids=set(), in_flight_task_ids=set())
    } == {downstream}


def test_runtime_context_keeps_failed_dependency_out_of_ready_wave(tmp_path: Path) -> None:
    parent_session = tmp_path / "session"
    parent_session.mkdir()
    upstream = enqueue_subtask(
        project_dir=tmp_path,
        parent_task_id="root",
        parent_session_dir=parent_session,
        intent="upstream",
    )
    enqueue_subtask(
        project_dir=tmp_path,
        parent_task_id="root",
        parent_session_dir=parent_session,
        intent="downstream",
        depends_on=[upstream],
    )
    set_verdict(tmp_path, upstream, "merge_blocked")

    context = _build_decomp_runtime_context(
        project_dir=tmp_path,
        config={},
        max_parallel=1,
        run_started_at=None,
    )

    assert context["queue_state"]["ready"] == 0
    assert context["queue_state"]["waiting_on_deps"] == 1


async def test_child_worktree_setup_failure_blocks_before_lead_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from otto import v5_runner

    project = tmp_path / "repo"
    project.mkdir()
    parent_session = project / "otto_logs" / "sessions" / "parent"
    (parent_session / "spec").mkdir(parents=True)
    (parent_session / "spec" / "spec.json").write_text(
        json.dumps({"schema_version": 3, "behavior_journeys": []}),
        encoding="utf-8",
    )
    task_id = "v5-child"
    record_task(project, task_id=task_id, intent="child", parent_task_id="root")
    lead_calls: list[dict[str, Any]] = []

    async def fake_run_lead(**kwargs: Any) -> LeadResult:
        lead_calls.append(kwargs)
        return LeadResult(task_id=task_id, verdict="pass")

    def fail_setup(**_kwargs: Any) -> Path:
        raise RuntimeError("worktree setup exploded")

    monkeypatch.setattr(v5_runner, "run_lead", fake_run_lead)
    monkeypatch.setattr("otto.v5_branching.setup_child_worktree", fail_setup)

    result = await _run_child(
        project_dir=project,
        entry={
            "task_id": task_id,
            "intent": "Build the child feature",
            "parent_session_dir": str(parent_session),
            "integration_branch": "i2p/integ/root",
        },
        config={},
        max_parallel=1,
    )

    assert lead_calls == []
    assert result.verdict == "merge_blocked"
    assert "worktree setup exploded" in result.failure_reason
    assert (get_task(project, task_id) or {}).get("verdict") == "merge_blocked"


@pytest.mark.asyncio
async def test_child_missing_integration_branch_blocks_before_worktree_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from otto import v5_runner

    project = tmp_path / "repo"
    project.mkdir()
    parent_session = project / "otto_logs" / "sessions" / "parent"
    (parent_session / "spec").mkdir(parents=True)
    (parent_session / "spec" / "spec.json").write_text(
        json.dumps({"schema_version": 3, "behavior_journeys": []}),
        encoding="utf-8",
    )
    task_id = "v5-child"
    record_task(project, task_id=task_id, intent="child", parent_task_id="root")

    async def fake_run_lead(**_kwargs: Any) -> LeadResult:
        raise AssertionError("run_lead should not be dispatched without branch identity")

    def fail_if_called(**_kwargs: Any) -> Path:
        raise AssertionError("setup_child_worktree should not be called without branch identity")

    monkeypatch.setattr(v5_runner, "run_lead", fake_run_lead)
    monkeypatch.setattr("otto.v5_branching.setup_child_worktree", fail_if_called)

    result = await _run_child(
        project_dir=project,
        entry={
            "task_id": task_id,
            "intent": "Build the child feature",
            "parent_session_dir": str(parent_session),
        },
        config={},
        max_parallel=1,
    )

    assert result.verdict == "merge_blocked"
    assert "missing integration_branch" in result.failure_reason
    assert (get_task(project, task_id) or {}).get("verdict") == "merge_blocked"
