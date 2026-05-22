# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

from otto import v5_branching, v5_runner
from otto.lead import LeadResult
from otto.queue import task_graph
from otto.queue.subtask import enqueue_subtask, take_ready
from otto.queue.task_graph import (
    get_task,
    mark_contract_amendment_retry_in_progress,
    read_graph,
    record_task,
    set_contract_amendment_blocked,
    set_verdict,
    update_task_metadata,
)
from otto.v5_runner import ROOT_TASK_ID


def _record_root_with_contract(repo: Path) -> None:
    record_task(repo, task_id=ROOT_TASK_ID, intent="root", parent_task_id=None)
    record_task(
        repo,
        task_id="foundation",
        parent_task_id=ROOT_TASK_ID,
        intent="Foundation owner",
        integration_branch="i2p/root/integration",
        owned_paths=["frontend/src/lib/ws.ts"],
        task_role="foundation",
    )
    update_task_metadata(
        repo,
        ROOT_TASK_ID,
        foundation_contracts=[
            {
                "path": "frontend/src/lib/ws.ts",
                "owner_task_id": "foundation",
                "check": "semantic",
            }
        ],
    )


def _enqueue_known(
    repo: Path,
    *,
    task_id: str,
    parent_task_id: str = ROOT_TASK_ID,
    intent: str | None = None,
    owned_paths: list[str] | None = None,
    task_role: str = "feature",
) -> None:
    generated = enqueue_subtask(
        project_dir=repo,
        parent_task_id=parent_task_id,
        parent_session_dir=repo / "otto_logs" / "sessions" / parent_task_id,
        intent=intent or task_id,
        owned_paths=owned_paths or [],
        task_role=task_role,
        parent_integration_branch="i2p/root/integration",
    )
    pending = repo / "otto_logs" / "cross-sessions" / "v5_pending.jsonl"
    pending.write_text(
        pending.read_text(encoding="utf-8").replace(generated, task_id),
        encoding="utf-8",
    )
    record_task(
        repo,
        task_id=task_id,
        parent_task_id=parent_task_id,
        intent=intent or task_id,
        integration_branch="i2p/root/integration",
        owned_paths=owned_paths or [],
        task_role=task_role,  # type: ignore[arg-type]
    )


def test_take_ready_skips_blocked_on_unsatisfied_task(tmp_path: Path) -> None:
    _record_root_with_contract(tmp_path)
    _enqueue_known(tmp_path, task_id="leaf", owned_paths=["frontend/src/features/comments/"])
    _enqueue_known(
        tmp_path,
        task_id="amendment",
        owned_paths=["frontend/src/lib/ws.ts"],
        task_role="contract_amendment",
    )
    set_contract_amendment_blocked(tmp_path, "leaf", "amendment", reason="needs contract")

    ready = take_ready(tmp_path, completed_task_ids=set(), in_flight_task_ids=set())

    assert {entry["task_id"] for entry in ready} == {"amendment"}


def test_set_contract_amendment_blocked_clears_persisted_pass(tmp_path: Path) -> None:
    _record_root_with_contract(tmp_path)
    record_task(
        tmp_path,
        task_id="leaf",
        parent_task_id=ROOT_TASK_ID,
        intent="leaf",
        integration_branch="i2p/root/integration",
        owned_paths=["frontend/src/features/comments/"],
    )
    set_verdict(tmp_path, "leaf", "pass")

    set_contract_amendment_blocked(tmp_path, "leaf", "amendment", reason="needs contract")

    leaf = get_task(tmp_path, "leaf") or {}
    assert leaf["last_agent_verdict"] == "pass"
    assert leaf["verdict"] is None
    assert leaf["completed_at"] is None
    assert leaf["blocked_pending_contract_amendment"] is True
    assert leaf["blocked_on_task_id"] == "amendment"


def test_amendment_pass_clears_and_reenqueues_all_blocked_leaves(tmp_path: Path) -> None:
    _record_root_with_contract(tmp_path)
    _enqueue_known(tmp_path, task_id="leaf-a", owned_paths=["frontend/src/features/a/"])
    _enqueue_known(tmp_path, task_id="leaf-b", owned_paths=["frontend/src/features/b/"])
    _enqueue_known(
        tmp_path,
        task_id="amendment",
        owned_paths=["frontend/src/lib/ws.ts"],
        task_role="contract_amendment",
    )
    update_task_metadata(
        tmp_path,
        "amendment",
        contract_amendment={
            "contract_path": "frontend/src/lib/ws.ts",
            "owner_task_id": "foundation",
        },
    )
    for leaf_id in ("leaf-a", "leaf-b"):
        set_verdict(tmp_path, leaf_id, "pass")
        set_contract_amendment_blocked(
            tmp_path,
            leaf_id,
            "amendment",
            merge_context={
                "child_session_dir": str(tmp_path / "otto_logs" / "sessions" / leaf_id),
                "parent_integration_branch": "i2p/root/integration",
            },
        )
    set_verdict(tmp_path, "amendment", "pass")

    v5_runner._settle_contract_amendment_dependents(
        project_dir=tmp_path,
        amendment_id="amendment",
        amendment_result=LeadResult(task_id="amendment", verdict="pass", decomposition="inline"),
        completed={"leaf-a", "leaf-b"},
        child_results={"leaf-a": LeadResult(task_id="leaf-a", verdict="pass", decomposition="inline")},
    )

    graph_tasks = read_graph(tmp_path)["tasks"]
    assert graph_tasks["leaf-a"]["blocked_on_task_id"] is None
    assert graph_tasks["leaf-b"]["blocked_on_task_id"] is None
    assert graph_tasks["leaf-a"]["contract_amendment_retry_merge"] is True
    assert graph_tasks["leaf-b"]["contract_amendment_retry_merge"] is True
    ready_ids = [entry["task_id"] for entry in take_ready(tmp_path, completed_task_ids=set(), in_flight_task_ids=set())]
    assert "leaf-a" in ready_ids
    assert "leaf-b" in ready_ids


def test_amendment_fail_marks_blocked_leaves_merge_blocked(tmp_path: Path) -> None:
    _record_root_with_contract(tmp_path)
    for leaf_id in ("leaf-a", "leaf-b"):
        record_task(
            tmp_path,
            task_id=leaf_id,
            parent_task_id=ROOT_TASK_ID,
            intent=leaf_id,
            integration_branch="i2p/root/integration",
            owned_paths=[f"frontend/src/features/{leaf_id}/"],
        )
        set_verdict(tmp_path, leaf_id, "pass")
        set_contract_amendment_blocked(tmp_path, leaf_id, "amendment")
    record_task(
        tmp_path,
        task_id="amendment",
        parent_task_id=ROOT_TASK_ID,
        intent="amendment",
        integration_branch="i2p/root/integration",
        owned_paths=["frontend/src/lib/ws.ts"],
        task_role="contract_amendment",
    )
    set_verdict(tmp_path, "amendment", "merge_blocked")

    v5_runner._settle_contract_amendment_dependents(
        project_dir=tmp_path,
        amendment_id="amendment",
        amendment_result=LeadResult(
            task_id="amendment",
            verdict="merge_blocked",
            decomposition="inline",
            failure_reason="bounded exhaustion",
        ),
        completed=set(),
        child_results={},
    )

    for leaf_id in ("leaf-a", "leaf-b"):
        leaf = get_task(tmp_path, leaf_id) or {}
        assert leaf["verdict"] == "merge_blocked"
        assert leaf["merge_blocked_origin"] == "contract_amendment"
        assert leaf["merge_blocked_structured_reason"]["kind"] == "contract_amendment_failed"
        assert leaf["blocked_on_task_id"] is None


def test_bound_contract_amendment_may_write_only_its_bound_contract(tmp_path: Path) -> None:
    _record_root_with_contract(tmp_path)
    update_task_metadata(
        tmp_path,
        ROOT_TASK_ID,
        foundation_contracts=[
            {
                "path": "frontend/src/lib/ws.ts",
                "owner_task_id": "foundation",
                "check": "semantic",
            },
            {
                "path": "frontend/src/lib/routes.ts",
                "owner_task_id": "routes-foundation",
                "check": "literal",
            },
        ],
    )
    record_task(
        tmp_path,
        task_id="amendment",
        parent_task_id=ROOT_TASK_ID,
        intent="amendment",
        integration_branch="i2p/root/integration",
        owned_paths=["frontend/src/lib/ws.ts"],
        task_role="contract_amendment",
    )
    update_task_metadata(
        tmp_path,
        "amendment",
        contract_amendment={
            "contract_path": "frontend/src/lib/ws.ts",
            "owner_task_id": "foundation",
        },
    )

    allowed = v5_runner._foundation_contract_write_feedback(
        project_dir=tmp_path,
        acting_task_id="amendment",
        parent_integration_branch="i2p/root/integration",
        changed_paths=["frontend/src/lib/ws.ts"],
        operation="test",
    )
    blocked = v5_runner._foundation_contract_write_feedback(
        project_dir=tmp_path,
        acting_task_id="amendment",
        parent_integration_branch="i2p/root/integration",
        changed_paths=["frontend/src/lib/routes.ts"],
        operation="test",
    )
    non_contract_blocked = v5_runner._foundation_contract_write_feedback(
        project_dir=tmp_path,
        acting_task_id="amendment",
        parent_integration_branch="i2p/root/integration",
        changed_paths=["frontend/src/features/comments/Panel.tsx"],
        operation="test",
    )

    assert allowed is None
    assert blocked is not None
    assert any(
        violation["contract_path"] == "frontend/src/lib/routes.ts"
        for violation in blocked["violations"]
    )
    assert non_contract_blocked is not None
    assert non_contract_blocked["violations"][0]["changed_paths"] == [
        "frontend/src/features/comments/Panel.tsx"
    ]


@pytest.mark.asyncio
async def test_merge_retry_persists_durable_pass_only_after_real_merge_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_root_with_contract(tmp_path)
    _enqueue_known(tmp_path, task_id="leaf", owned_paths=["frontend/src/features/comments/"])
    record_task(
        tmp_path,
        task_id="amendment",
        parent_task_id=ROOT_TASK_ID,
        intent="amendment",
        integration_branch="i2p/root/integration",
        owned_paths=["frontend/src/lib/ws.ts"],
        task_role="contract_amendment",
    )
    set_verdict(tmp_path, "leaf", "pass")
    set_contract_amendment_blocked(
        tmp_path,
        "leaf",
        "amendment",
        merge_context={
            "child_session_dir": str(tmp_path / "otto_logs" / "sessions" / "leaf"),
            "parent_integration_branch": "i2p/root/integration",
        },
    )
    set_verdict(tmp_path, "amendment", "pass")
    v5_runner._settle_contract_amendment_dependents(
        project_dir=tmp_path,
        amendment_id="amendment",
        amendment_result=LeadResult(task_id="amendment", verdict="pass", decomposition="inline"),
        completed={"leaf"},
        child_results={"leaf": LeadResult(task_id="leaf", verdict="pass", decomposition="inline")},
    )
    ready = take_ready(tmp_path, completed_task_ids=set(), in_flight_task_ids=set())
    leaf_entry = next(entry for entry in ready if entry["task_id"] == "leaf")

    monkeypatch.setattr(v5_branching, "setup_child_worktree", lambda **_kwargs: tmp_path)
    monkeypatch.setattr(v5_branching, "commit_worktree", lambda **_kwargs: (True, "committed"))
    monkeypatch.setattr(v5_branching, "merge_child_into_integration", lambda **_kwargs: (True, "merged"))
    monkeypatch.setattr(v5_runner, "_git_capture", lambda *_args, **_kwargs: "pre-merge-ref")
    monkeypatch.setattr(v5_runner, "_git_diff_name_only", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(v5_runner, "_git_changed_paths_between_refs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(v5_runner, "_record_and_check_integration_union", lambda **_kwargs: None)

    result = await v5_runner._run_child(
        project_dir=tmp_path,
        entry=leaf_entry,
        config={},
        max_parallel=1,
        on_event=None,
    )

    leaf = get_task(tmp_path, "leaf") or {}
    assert result.verdict == "pass"
    assert leaf["verdict"] == "pass"
    assert leaf["completed_at"] is not None
    assert leaf["blocked_on_task_id"] is None
    assert leaf["contract_amendment_retry_merge"] is False
    assert "leaf" not in {
        entry["task_id"]
        for entry in take_ready(tmp_path, completed_task_ids=set(), in_flight_task_ids=set())
    }


@pytest.mark.asyncio
async def test_merge_retry_window_is_durably_non_ready_before_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_root_with_contract(tmp_path)
    _enqueue_known(tmp_path, task_id="leaf", owned_paths=["frontend/src/features/comments/"])
    record_task(
        tmp_path,
        task_id="amendment",
        parent_task_id=ROOT_TASK_ID,
        intent="amendment",
        integration_branch="i2p/root/integration",
        owned_paths=["frontend/src/lib/ws.ts"],
        task_role="contract_amendment",
    )
    set_verdict(tmp_path, "leaf", "pass")
    set_contract_amendment_blocked(
        tmp_path,
        "leaf",
        "amendment",
        merge_context={
            "child_session_dir": str(tmp_path / "otto_logs" / "sessions" / "leaf"),
            "parent_integration_branch": "i2p/root/integration",
        },
    )
    set_verdict(tmp_path, "amendment", "pass")
    v5_runner._settle_contract_amendment_dependents(
        project_dir=tmp_path,
        amendment_id="amendment",
        amendment_result=LeadResult(task_id="amendment", verdict="pass", decomposition="inline"),
        completed={"leaf"},
        child_results={"leaf": LeadResult(task_id="leaf", verdict="pass", decomposition="inline")},
    )
    leaf_entry = next(
        entry
        for entry in take_ready(tmp_path, completed_task_ids=set(), in_flight_task_ids=set())
        if entry["task_id"] == "leaf"
    )

    monkeypatch.setattr(v5_branching, "setup_child_worktree", lambda **_kwargs: tmp_path)

    async def observe_window(**_kwargs: object) -> None:
        leaf = get_task(tmp_path, "leaf") or {}
        assert leaf["verdict"] is None
        assert leaf["blocked_on_task_id"] is None
        assert leaf["contract_amendment_retry_merge"] is True
        assert leaf["contract_amendment_retry_in_progress"] is True
        ready_ids = {
            entry["task_id"]
            for entry in take_ready(tmp_path, completed_task_ids=set(), in_flight_task_ids=set())
        }
        assert "leaf" not in ready_ids

    monkeypatch.setattr(v5_runner, "_commit_child_for_integration", observe_window)

    result = await v5_runner._run_child(
        project_dir=tmp_path,
        entry=leaf_entry,
        config={},
        max_parallel=1,
        on_event=None,
    )

    leaf = get_task(tmp_path, "leaf") or {}
    assert result.verdict == "pass"
    assert leaf["verdict"] == "pass"
    assert leaf["contract_amendment_retry_merge"] is False
    assert leaf["contract_amendment_retry_in_progress"] is False


@pytest.mark.asyncio
async def test_live_long_running_merge_retry_heartbeat_prevents_false_reclaim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_root_with_contract(tmp_path)
    _enqueue_known(tmp_path, task_id="leaf", owned_paths=["frontend/src/features/comments/"])
    record_task(
        tmp_path,
        task_id="amendment",
        parent_task_id=ROOT_TASK_ID,
        intent="amendment",
        integration_branch="i2p/root/integration",
        owned_paths=["frontend/src/lib/ws.ts"],
        task_role="contract_amendment",
    )
    set_verdict(tmp_path, "leaf", "pass")
    set_contract_amendment_blocked(
        tmp_path,
        "leaf",
        "amendment",
        merge_context={
            "child_session_dir": str(tmp_path / "otto_logs" / "sessions" / "leaf"),
            "parent_integration_branch": "i2p/root/integration",
        },
    )
    set_verdict(tmp_path, "amendment", "pass")
    v5_runner._settle_contract_amendment_dependents(
        project_dir=tmp_path,
        amendment_id="amendment",
        amendment_result=LeadResult(task_id="amendment", verdict="pass", decomposition="inline"),
        completed={"leaf"},
        child_results={"leaf": LeadResult(task_id="leaf", verdict="pass", decomposition="inline")},
    )
    leaf_entry = next(
        entry
        for entry in take_ready(tmp_path, completed_task_ids=set(), in_flight_task_ids=set())
        if entry["task_id"] == "leaf"
    )

    clock = {"now": 1_700_000_000.0}

    def fake_now_iso() -> str:
        return task_graph.time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            task_graph.time.gmtime(clock["now"]),
        )

    monkeypatch.setattr(v5_branching, "setup_child_worktree", lambda **_kwargs: tmp_path)
    monkeypatch.setattr(task_graph, "_now_iso", fake_now_iso)
    monkeypatch.setattr(task_graph.time, "time", lambda: clock["now"])
    monkeypatch.setattr(v5_runner, "CONTRACT_AMENDMENT_RETRY_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    reclaim_attempts: list[bool] = []
    ready_snapshots: list[set[str]] = []

    async def long_running_merge(**_kwargs: object) -> None:
        initial_heartbeat = (get_task(tmp_path, "leaf") or {})[
            "contract_amendment_retry_heartbeat_at"
        ]
        clock["now"] += task_graph.CONTRACT_AMENDMENT_RETRY_STALE_SECONDS + 10
        for _ in range(20):
            await asyncio.sleep(0.01)
            if (get_task(tmp_path, "leaf") or {}).get(
                "contract_amendment_retry_heartbeat_at"
            ) != initial_heartbeat:
                break
        assert (get_task(tmp_path, "leaf") or {})[
            "contract_amendment_retry_heartbeat_at"
        ] != initial_heartbeat
        ready_snapshots.append(
            {
                entry["task_id"]
                for entry in take_ready(
                    tmp_path,
                    completed_task_ids=set(),
                    in_flight_task_ids=set(),
                )
            }
        )
        reclaim_attempts.append(
            mark_contract_amendment_retry_in_progress(
                tmp_path,
                "leaf",
                owner_id="second-runner",
            )
        )

    monkeypatch.setattr(v5_runner, "_commit_child_for_integration", long_running_merge)

    result = await v5_runner._run_child(
        project_dir=tmp_path,
        entry=leaf_entry,
        config={},
        max_parallel=1,
        on_event=None,
    )

    leaf = get_task(tmp_path, "leaf") or {}
    assert result.verdict == "pass"
    assert ready_snapshots == [set()]
    assert reclaim_attempts == [False]
    assert leaf["verdict"] == "pass"
    assert leaf["contract_amendment_retry_merge"] is False
    assert leaf["contract_amendment_retry_in_progress"] is False


@pytest.mark.asyncio
async def test_merge_retry_atomic_claim_allows_exactly_one_merger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_root_with_contract(tmp_path)
    _enqueue_known(tmp_path, task_id="leaf", owned_paths=["frontend/src/features/comments/"])
    record_task(
        tmp_path,
        task_id="amendment",
        parent_task_id=ROOT_TASK_ID,
        intent="amendment",
        integration_branch="i2p/root/integration",
        owned_paths=["frontend/src/lib/ws.ts"],
        task_role="contract_amendment",
    )
    set_verdict(tmp_path, "leaf", "pass")
    set_contract_amendment_blocked(
        tmp_path,
        "leaf",
        "amendment",
        merge_context={
            "child_session_dir": str(tmp_path / "otto_logs" / "sessions" / "leaf"),
            "parent_integration_branch": "i2p/root/integration",
        },
    )
    set_verdict(tmp_path, "amendment", "pass")
    v5_runner._settle_contract_amendment_dependents(
        project_dir=tmp_path,
        amendment_id="amendment",
        amendment_result=LeadResult(task_id="amendment", verdict="pass", decomposition="inline"),
        completed={"leaf"},
        child_results={"leaf": LeadResult(task_id="leaf", verdict="pass", decomposition="inline")},
    )
    leaf_entry = next(
        entry
        for entry in take_ready(tmp_path, completed_task_ids=set(), in_flight_task_ids=set())
        if entry["task_id"] == "leaf"
    )

    monkeypatch.setattr(v5_branching, "setup_child_worktree", lambda **_kwargs: tmp_path)
    merge_calls: list[str] = []
    loser_results: list[LeadResult] = []

    async def merge_once(**_kwargs: object) -> None:
        merge_calls.append("winner")
        loser_results.append(
            await v5_runner._run_child(
                project_dir=tmp_path,
                entry=leaf_entry,
                config={},
                max_parallel=1,
                on_event=None,
            )
        )

    monkeypatch.setattr(v5_runner, "_commit_child_for_integration", merge_once)

    winner = await v5_runner._run_child(
        project_dir=tmp_path,
        entry=leaf_entry,
        config={},
        max_parallel=1,
        on_event=None,
    )

    leaf = get_task(tmp_path, "leaf") or {}
    assert winner.verdict == "pass"
    assert [result.verdict for result in loser_results] == ["unverified"]
    assert loser_results[0].failure_reason == (
        "contract amendment merge-only retry is already claimed by another runner"
    )
    assert merge_calls == ["winner"]
    assert leaf["verdict"] == "pass"
    assert leaf["contract_amendment_retry_merge"] is False
    assert leaf["contract_amendment_retry_in_progress"] is False


@pytest.mark.asyncio
async def test_stale_merge_retry_is_recovered_or_bounded_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_root_with_contract(tmp_path)
    monkeypatch.setattr(v5_branching, "setup_child_worktree", lambda **_kwargs: tmp_path)
    recovered_merges: list[str] = []

    async def successful_merge(**kwargs: object) -> None:
        recovered_merges.append(str(kwargs["child_task_id"]))

    monkeypatch.setattr(v5_runner, "_commit_child_for_integration", successful_merge)

    for leaf_id in ("recover-leaf", "exhausted-leaf"):
        _enqueue_known(tmp_path, task_id=leaf_id, owned_paths=[f"frontend/src/features/{leaf_id}/"])
        record_task(
            tmp_path,
            task_id=f"amendment-{leaf_id}",
            parent_task_id=ROOT_TASK_ID,
            intent=f"amendment-{leaf_id}",
            integration_branch="i2p/root/integration",
            owned_paths=["frontend/src/lib/ws.ts"],
            task_role="contract_amendment",
        )
        set_verdict(tmp_path, leaf_id, "pass")
        set_contract_amendment_blocked(
            tmp_path,
            leaf_id,
            f"amendment-{leaf_id}",
            merge_context={
                "child_session_dir": str(tmp_path / "otto_logs" / "sessions" / leaf_id),
                "parent_integration_branch": "i2p/root/integration",
            },
        )
        set_verdict(tmp_path, f"amendment-{leaf_id}", "pass")
        v5_runner._settle_contract_amendment_dependents(
            project_dir=tmp_path,
            amendment_id=f"amendment-{leaf_id}",
            amendment_result=LeadResult(task_id=f"amendment-{leaf_id}", verdict="pass", decomposition="inline"),
            completed={leaf_id},
            child_results={leaf_id: LeadResult(task_id=leaf_id, verdict="pass", decomposition="inline")},
        )
        update_task_metadata(
            tmp_path,
            leaf_id,
            contract_amendment_retry_in_progress=True,
            contract_amendment_retry_owner="dead-owner",
            contract_amendment_retry_owner_pid=999999,
            contract_amendment_retry_owner_host=socket.gethostname(),
            contract_amendment_retry_merge_started_at="1970-01-01T00:00:00Z",
            contract_amendment_retry_heartbeat_at="1970-01-01T00:00:00Z",
            contract_amendment_retry_claim_count=1
            if leaf_id == "recover-leaf"
            else 2,
            contract_amendment_retry_max_claims=2,
        )

    ready_by_id = {
        entry["task_id"]: entry
        for entry in take_ready(tmp_path, completed_task_ids=set(), in_flight_task_ids=set())
    }
    assert "recover-leaf" in ready_by_id
    assert "exhausted-leaf" in ready_by_id
    assert (get_task(tmp_path, "recover-leaf") or {})["contract_amendment_retry_merge"] is True
    assert (get_task(tmp_path, "exhausted-leaf") or {})["contract_amendment_retry_merge"] is True

    recovered = await v5_runner._run_child(
        project_dir=tmp_path,
        entry=ready_by_id["recover-leaf"],
        config={},
        max_parallel=1,
        on_event=None,
    )
    exhausted = await v5_runner._run_child(
        project_dir=tmp_path,
        entry=ready_by_id["exhausted-leaf"],
        config={},
        max_parallel=1,
        on_event=None,
    )

    recovered_leaf = get_task(tmp_path, "recover-leaf") or {}
    exhausted_leaf = get_task(tmp_path, "exhausted-leaf") or {}
    assert recovered.verdict == "pass"
    assert recovered_leaf["verdict"] == "pass"
    assert recovered_leaf["contract_amendment_retry_claim_count"] == 2
    assert recovered_leaf["contract_amendment_retry_merge"] is False
    assert exhausted.verdict == "merge_blocked"
    assert exhausted_leaf["verdict"] == "merge_blocked"
    assert exhausted_leaf["merge_blocked_origin"] == "contract_amendment"
    assert exhausted_leaf["merge_blocked_structured_reason"]["kind"] == (
        "contract_amendment_retry_claims_exhausted"
    )
    assert exhausted_leaf["contract_amendment_retry_merge"] is False
    assert "exhausted-leaf" not in {
        entry["task_id"]
        for entry in take_ready(tmp_path, completed_task_ids=set(), in_flight_task_ids=set())
    }
    assert recovered_merges == ["recover-leaf"]


@pytest.mark.asyncio
async def test_amendment_crash_settles_blocked_leaves_merge_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_root_with_contract(tmp_path)
    _enqueue_known(
        tmp_path,
        task_id="amendment",
        owned_paths=["frontend/src/lib/ws.ts"],
        task_role="contract_amendment",
    )
    for leaf_id in ("leaf-a", "leaf-b"):
        record_task(
            tmp_path,
            task_id=leaf_id,
            parent_task_id=ROOT_TASK_ID,
            intent=leaf_id,
            integration_branch="i2p/root/integration",
            owned_paths=[f"frontend/src/features/{leaf_id}/"],
        )
        set_verdict(tmp_path, leaf_id, "pass")
        set_contract_amendment_blocked(tmp_path, leaf_id, "amendment")

    async def crash_amendment(**_kwargs: object) -> LeadResult:
        raise RuntimeError("amendment wrapper exploded")

    monkeypatch.setattr(v5_runner, "_run_child", crash_amendment)
    await v5_runner._process_children(
        project_dir=tmp_path,
        parent_task_id=ROOT_TASK_ID,
        config={},
        max_parallel=1,
        tree_budget_usd=100.0,
        child_results={},
        integration_results={},
        on_event=None,
    )

    for leaf_id in ("leaf-a", "leaf-b"):
        leaf = get_task(tmp_path, leaf_id) or {}
        assert leaf["verdict"] == "merge_blocked"
        assert leaf["merge_blocked_origin"] == "contract_amendment"
        assert leaf["merge_blocked_structured_reason"]["kind"] == "contract_amendment_failed"
        assert leaf["blocked_on_task_id"] is None


@pytest.mark.asyncio
async def test_futile_contract_amendments_are_bounded_to_honest_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_root_with_contract(tmp_path)
    record_task(
        tmp_path,
        task_id="leaf",
        parent_task_id=ROOT_TASK_ID,
        intent="leaf",
        integration_branch="i2p/root/integration",
        owned_paths=["frontend/src/features/comments/"],
    )
    update_task_metadata(
        tmp_path,
        "leaf",
        contract_amendment_attempts={
            "frontend/src/lib/ws.ts": v5_runner.MAX_CONTRACT_AMENDMENT_ATTEMPTS
        },
    )
    union_feedback = {
        "kind": "integration_union_incomplete",
        "message": "frontend/src/lib/ws.ts still missing required union",
        "paths": ["frontend/src/lib/ws.ts"],
        "missing": [{"path": "frontend/src/lib/ws.ts"}],
    }

    monkeypatch.setattr(v5_branching, "commit_worktree", lambda **_kwargs: (True, "committed"))
    monkeypatch.setattr(v5_branching, "merge_child_into_integration", lambda **_kwargs: (True, "merged"))
    monkeypatch.setattr(v5_runner, "_git_capture", lambda *_args, **_kwargs: "pre-merge-ref")
    monkeypatch.setattr(v5_runner, "_git_diff_name_only", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(v5_runner, "_git_changed_paths_between_refs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(v5_runner, "_record_and_check_integration_union", lambda **_kwargs: union_feedback)

    result = LeadResult(task_id="leaf", verdict="pass", decomposition="inline", verify_called=True)
    await v5_runner._merge_child_branch(
        project_dir=tmp_path,
        child_task_id="leaf",
        child_worktree=tmp_path,
        child_session_dir=tmp_path / "otto_logs" / "sessions" / "leaf",
        parent_integration_branch="i2p/root/integration",
        result=result,
        config={},
        on_event=None,
    )

    leaf = get_task(tmp_path, "leaf") or {}
    amendment_tasks = [
        task
        for task in (read_graph(tmp_path).get("tasks") or {}).values()
        if task.get("task_role") == "contract_amendment"
    ]
    assert amendment_tasks == []
    assert leaf["verdict"] == "merge_blocked"
    assert leaf["merge_blocked_origin"] == "contract_amendment"
    assert leaf["merge_blocked_structured_reason"]["kind"] == "contract_amendment_attempts_exhausted"
