from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from otto.audit import (
    AuditAgentInput,
    AuditAgentOutput,
    AuditBudget,
    AuditVerdict,
    WalkthroughResult,
    default_walkthrough_from_spec,
    run_audit,
)
from otto.build import BuildResult
from otto.checks import run_check
from otto.lead import LeadResult
from otto.merge_queue import MergeQueueResult
from otto.queue.subtask import enqueue_subtask, take_ready
from otto.queue.task_graph import (
    get_task,
    mark_reviewed_partial,
    record_task,
    set_verdict,
)
from otto.spec_compile import Group, RepoTestCheck, Spec, StructureDecisions
from otto.v5_runner import _build_decomp_runtime_context, _run_child


def _web_spec() -> Spec:
    return Spec(
        intent="webapp",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[Group(id="ui", name="UI", dependencies=[], owned_paths=[], feature_ids=[])],
    )


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


def test_synthesized_webapp_walkthrough_no_shape_is_not_success(tmp_path: Path) -> None:
    callable_ = default_walkthrough_from_spec(_web_spec())

    result = callable_(tmp_path, tmp_path / "walk", 60)

    assert result.succeeded is False
    assert result.artifacts
    assert "no runnable webapp shape" in result.detail


def test_run_audit_caps_pass_when_configured_walkthrough_fails(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    def failed_walkthrough(_project_dir: Path, log_dir: Path, _timeout_s: int) -> WalkthroughResult:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "walkthrough.log"
        log_path.write_text("no artifact\n", encoding="utf-8")
        return WalkthroughResult(
            succeeded=False,
            detail="configured walkthrough produced no product artifact",
            artifacts=[log_path],
        )

    async def passing_agent(input_: AuditAgentInput) -> AuditAgentOutput:
        assert input_.walkthrough_succeeded is False
        assert "no product artifact" in input_.walkthrough_detail
        return AuditAgentOutput(verdict=AuditVerdict.PASSED, narrative="agent passed")

    result = asyncio.run(
        run_audit(
            _web_spec(),
            project_dir=tmp_path,
            session_dir=session_dir,
            build_result=BuildResult(spec_session_dir=session_dir),
            merge_result=MergeQueueResult(),
            audit_agent=passing_agent,
            walkthrough=failed_walkthrough,
            budget=AuditBudget(audit_retries=0),
        )
    )

    assert result.verdict == AuditVerdict.PARTIAL
    assert any("walkthrough oracle failed" in reason for reason in result.verdict_cap_reasons)


def test_malformed_check_is_non_blocking_but_not_proof(tmp_path: Path) -> None:
    evidence = run_check(RepoTestCheck(command=(), timeout_s=10), project_dir=tmp_path)

    assert evidence.passed is True
    assert evidence.raw["malformed"] is True
    assert evidence.raw["malformed_check"] is True
    assert evidence.raw["evidence_quality"] == "malformed"
    assert evidence.raw["proof_usable"] is False


@pytest.mark.asyncio
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
