# pyright: reportPrivateUsage=false
from __future__ import annotations

from pathlib import Path

from otto import v5_runner
from otto.lead import LeadResult
from otto.queue.subtask import enqueue_subtask, take_ready
from otto.queue.task_graph import (
    get_task,
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

    assert allowed is None
    assert blocked is not None
    assert blocked["violations"][0]["contract_path"] == "frontend/src/lib/routes.ts"
