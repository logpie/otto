# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from otto import spec_state, v5_branching, v5_runner
from otto.lead import LeadResult
from otto.queue.task_graph import (
    get_task,
    read_graph,
    record_task,
    set_verdict,
    update_task_metadata,
)
from otto.v5_branching import child_branch_name, integration_branch_name, setup_child_worktree
from otto.v5_preflight_repair import OracleRepairResult, RepairPacket
from otto.v5_runner import ROOT_TASK_ID


pytestmark = pytest.mark.integration

ROUTE_FILE = "app/routes.txt"


def _git(cwd: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main", check=True)
    _git(repo, "config", "user.email", "test@example.invalid", check=True)
    _git(repo, "config", "user.name", "Test User", check=True)
    (repo / ".gitignore").write_text(".worktrees/\notto_logs/\n.otto/\n", encoding="utf-8")
    (repo / "CHARTER.md").write_text("# Concurrent seam fixture\n", encoding="utf-8")
    (repo / ROUTE_FILE).parent.mkdir(parents=True, exist_ok=True)
    (repo / ROUTE_FILE).write_text("base\n", encoding="utf-8")
    (repo / "src").mkdir()
    _git(repo, "add", "-A", check=True)
    _git(repo, "commit", "-q", "-m", "init", check=True)


def _write_child_session(session_dir: Path) -> None:
    (session_dir / "spec").mkdir(parents=True, exist_ok=True)
    (session_dir / "spec" / "spec.json").write_text(
        json.dumps({"routes": [], "features": [], "behavior_journeys": []}, indent=2) + "\n",
        encoding="utf-8",
    )


def _show(repo: Path, ref: str, path: str) -> str:
    return _git(repo, "show", f"{ref}:{path}", check=True).stdout


def _branch_contains(repo: Path, branch: str, ancestor: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", ancestor, branch).returncode == 0


def _routes_from_text(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip().startswith("route-")]


def _render_routes(routes: list[str]) -> str:
    ordered = ["base", *sorted(dict.fromkeys(routes))]
    return "\n".join(ordered) + "\n"


def _write_route(worktree: Path, route: str) -> None:
    (worktree / ROUTE_FILE).parent.mkdir(parents=True, exist_ok=True)
    (worktree / ROUTE_FILE).write_text(_render_routes([route]), encoding="utf-8")


def _write_leaf_output(worktree: Path, rel_path: str, content: str) -> None:
    path = worktree / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def _passing_smoke_preflight(**_kwargs: Any) -> dict[str, Any]:
    return {"check": "smoke_clean_deploy", "passed": True, "issues": []}


def _session_for(repo: Path, task_id: str) -> Path:
    session_dir = repo / "otto_logs" / "sessions" / f"session-{task_id}"
    _write_child_session(session_dir)
    return session_dir


async def _merge_leaf(
    *,
    repo: Path,
    task_id: str,
    parent_task_id: str,
    parent_integration_branch: str,
    rel_path: str,
    content: str,
    events: list[dict[str, Any]],
) -> None:
    record_task(
        repo,
        task_id=task_id,
        intent=f"Write {rel_path}",
        parent_task_id=parent_task_id,
        integration_branch=parent_integration_branch,
        owned_paths=[rel_path],
    )
    set_verdict(repo, task_id, "pass")
    worktree = setup_child_worktree(
        project_dir=repo,
        child_task_id=task_id,
        parent_integration_branch=parent_integration_branch,
    )
    _write_leaf_output(worktree, rel_path, content)
    result = LeadResult(
        task_id=task_id,
        verdict="pass",
        decomposition="inline",
        verify_called=True,
        verify_result={"verdict": "pass", "summary": "leaf fixture passed"},
    )
    await v5_runner._merge_child_branch(
        project_dir=repo,
        child_task_id=task_id,
        child_worktree=worktree,
        child_session_dir=_session_for(repo, task_id),
        parent_integration_branch=parent_integration_branch,
        result=result,
        config={"default_branch": "main"},
        on_event=events.append,
    )


async def _run_integration_and_propagate(
    *,
    repo: Path,
    task_id: str,
    child_results: dict[str, LeadResult],
    integration_results: dict[str, LeadResult],
    events: list[dict[str, Any]],
    barrier: threading.Barrier,
) -> tuple[bool, str, str, str]:
    result = await v5_runner._run_integration(
        project_dir=repo,
        task_id=task_id,
        intent=f"Integrate {task_id}",
        config={"default_branch": "main"},
        child_results=child_results,
        integration_results=integration_results,
        on_event=events.append,
    )
    assert result.verdict == "pass"

    def propagate() -> tuple[bool, str, str, str]:
        barrier.wait(timeout=10)
        return v5_runner._propagate_subtree_integration(project_dir=repo, task_id=task_id)

    return await asyncio.to_thread(propagate)


@pytest.mark.asyncio
async def test_depth3_dual_subtree_propagation_uses_owning_worktrees_and_blocks_only_failed_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    record_task(repo, task_id=ROOT_TASK_ID, intent="root", parent_task_id=None)

    top_tasks = {"subtree-a": "src/a/leaf.txt", "subtree-b": "src/b/leaf.txt"}
    child_results: dict[str, LeadResult] = {}
    integration_results: dict[str, LeadResult] = {}
    events: list[dict[str, Any]] = []
    lead_branches: dict[str, str] = {}

    async def fake_run_lead(**kwargs: Any) -> LeadResult:
        task_id = str(kwargs["task_id"])
        session_dir = Path(kwargs["session_dir"])
        linked = session_dir / "worktree"
        worktree = linked.resolve() if linked.exists() else repo
        lead_branches[task_id] = _git(worktree, "branch", "--show-current", check=True).stdout.strip()
        return LeadResult(
            task_id=task_id,
            verdict="pass",
            cost_usd=0.01,
            decomposition="inline",
            verify_called=True,
            verify_result={"verdict": "pass", "summary": f"{task_id} integration passed"},
        )

    async def blocked_repair(packet: RepairPacket, **_kwargs: Any) -> OracleRepairResult:
        return OracleRepairResult(
            verdict="merge_blocked",
            summary=f"{packet.repair_unit['task_id']} remains blocked",
            cost_usd=0.01,
            agent_turns_used=1,
            oracle_invocations=1,
            packet_path=str(packet.packet_path),
            escalation={"reason": "deterministic blocked slice", "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        )

    monkeypatch.setattr(v5_runner, "run_lead", fake_run_lead)
    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight_with_repair", _passing_smoke_preflight)
    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", blocked_repair)

    for top_id, rel_path in top_tasks.items():
        mid_id = f"{top_id}-mid"
        leaf_id = f"{top_id}-leaf"
        top_integration = integration_branch_name(top_id)
        mid_integration = integration_branch_name(mid_id)
        _git(repo, "branch", top_integration, "main", check=True)
        _git(repo, "branch", mid_integration, top_integration, check=True)
        record_task(
            repo,
            task_id=top_id,
            intent=f"Top {top_id}",
            parent_task_id=ROOT_TASK_ID,
            integration_branch="main",
        )
        record_task(
            repo,
            task_id=mid_id,
            intent=f"Mid {top_id}",
            parent_task_id=top_id,
            integration_branch=top_integration,
        )
        await _merge_leaf(
            repo=repo,
            task_id=leaf_id,
            parent_task_id=mid_id,
            parent_integration_branch=mid_integration,
            rel_path=rel_path,
            content=f"{top_id} landed\n",
            events=events,
        )
        child_results[leaf_id] = LeadResult(
            task_id=leaf_id,
            verdict="pass",
            decomposition="inline",
            verify_called=True,
            verify_result={"verdict": "pass"},
        )
        mid_result = await v5_runner._run_integration(
            project_dir=repo,
            task_id=mid_id,
            intent=f"Integrate {mid_id}",
            config={"default_branch": "main"},
            child_results=child_results,
            integration_results=integration_results,
            on_event=events.append,
        )
        assert mid_result.verdict == "pass"
        ok, detail, source, target = v5_runner._propagate_subtree_integration(
            project_dir=repo,
            task_id=mid_id,
        )
        assert ok, detail
        assert source == mid_integration
        assert target == top_integration

    barrier = threading.Barrier(2)
    propagation_results = await asyncio.gather(*[
        _run_integration_and_propagate(
            repo=repo,
            task_id=task_id,
            child_results=child_results,
            integration_results=integration_results,
            events=events,
            barrier=barrier,
        )
        for task_id in top_tasks
    ])
    assert all(ok for ok, _detail, _source, _target in propagation_results), propagation_results

    main_a = _show(repo, "main", "src/a/leaf.txt")
    main_b = _show(repo, "main", "src/b/leaf.txt")
    assert main_a == "subtree-a landed\n"
    assert main_b == "subtree-b landed\n"
    assert lead_branches["subtree-a"] == integration_branch_name("subtree-a")
    assert lead_branches["subtree-b"] == integration_branch_name("subtree-b")
    assert not _branch_contains(repo, integration_branch_name("subtree-a"), child_branch_name("subtree-b-leaf"))
    assert not _branch_contains(repo, integration_branch_name("subtree-b"), child_branch_name("subtree-a-leaf"))

    blocked_id = "subtree-b-blocked"
    blocked_wt = setup_child_worktree(
        project_dir=repo,
        child_task_id=blocked_id,
        parent_integration_branch=integration_branch_name("subtree-b"),
    )
    record_task(
        repo,
        task_id=blocked_id,
        intent="Blocked B-only slice",
        parent_task_id="subtree-b",
        integration_branch=integration_branch_name("subtree-b"),
    )
    set_verdict(repo, blocked_id, "pass")
    _write_leaf_output(blocked_wt, "src/b/blocked.txt", "must not land\n")
    b_owner = v5_runner._worktree_for_branch(repo, integration_branch_name("subtree-b"))
    (b_owner / "CHARTER.md").write_text("# Dirty B integration owner\n", encoding="utf-8")
    blocked_result = LeadResult(
        task_id=blocked_id,
        verdict="pass",
        decomposition="inline",
        verify_called=True,
        verify_result={"verdict": "pass", "summary": "blocked slice initially passed"},
    )
    await v5_runner._merge_child_branch(
        project_dir=repo,
        child_task_id=blocked_id,
        child_worktree=blocked_wt,
        child_session_dir=_session_for(repo, blocked_id),
        parent_integration_branch=integration_branch_name("subtree-b"),
        result=blocked_result,
        config={"default_branch": "main"},
        on_event=events.append,
    )
    blocked_task = get_task(repo, blocked_id) or {}
    assert blocked_task.get("verdict") == "merge_blocked"
    assert isinstance(blocked_task.get("merge_blocked_structured_reason"), dict), blocked_task
    assert _git(repo, "cat-file", "-e", "main:src/b/blocked.txt").returncode != 0
    assert _show(repo, "main", "src/a/leaf.txt") == "subtree-a landed\n"


class _ConcurrentRouteRepair:
    def __init__(self, repo: Path, expected_routes: dict[str, str]) -> None:
        self.repo = repo
        self.expected_routes = expected_routes
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._lossy_conflict_consumed = False
        self.pause_after_conflict_repair: dict[str, tuple[threading.Event, threading.Event]] = {}

    async def __call__(
        self,
        packet: RepairPacket,
        *,
        commit_hook: Any = None,
        **_kwargs: Any,
    ) -> OracleRepairResult:
        task_id = str(packet.repair_unit["task_id"])
        worktree = Path(str(packet.repair_unit["worktree"]))
        gate = packet.integration_context.get("upward_merge_gate_feedback")
        is_union_guard = isinstance(gate, dict) and gate.get("kind") == "integration_union_incomplete"
        self.calls.append({
            "task_id": task_id,
            "repair_phase": packet.repair_unit.get("repair_phase"),
            "is_union_guard": is_union_guard,
            "latest_issue_kinds": [
                issue.get("kind")
                for issue in (packet.latest_oracle_result.get("issues") or [])
                if isinstance(issue, dict)
            ],
        })

        if is_union_guard:
            feedback = gate
            target_branch = str(packet.integration_context["parent_integration_branch"])
            merge_target = _git(
                worktree,
                "merge",
                "-s",
                "ours",
                "--no-ff",
                "--no-edit",
                target_branch,
            )
            if merge_target.returncode != 0 and "Already up to date" not in (
                merge_target.stdout + merge_target.stderr
            ):
                raise AssertionError(merge_target.stderr or merge_target.stdout)
            final_text = str((feedback.get("final_text_by_path") or {}).get(ROUTE_FILE) or "")
            routes = _routes_from_text(final_text)
            for item in feedback.get("missing") or []:
                if isinstance(item, dict) and item.get("line"):
                    routes.extend(_routes_from_text(str(item["line"])))
            (worktree / ROUTE_FILE).write_text(_render_routes(routes), encoding="utf-8")
        else:
            refs = packet.integration_context["merge_refs"]
            target_branch = str(refs["ours_ref"])
            _git(worktree, "merge", "-s", "ours", "--no-ff", "--no-edit", target_branch, check=True)
            target_routes = _routes_from_text(_show(self.repo, target_branch, ROUTE_FILE))
            own_route = self.expected_routes[task_id]
            with self._lock:
                make_lossy = bool(target_routes) and not self._lossy_conflict_consumed
                if make_lossy:
                    self._lossy_conflict_consumed = True
            routes = [own_route] if make_lossy else [*target_routes, own_route]
            (worktree / ROUTE_FILE).write_text(_render_routes(routes), encoding="utf-8")

        if commit_hook is not None:
            ok, detail = await commit_hook(packet, packet.latest_oracle_result)
            assert ok, detail
        if not is_union_guard and task_id in self.pause_after_conflict_repair:
            repaired_event, release_event = self.pause_after_conflict_repair[task_id]
            repaired_event.set()
            assert release_event.wait(timeout=10), f"timed out waiting to release {task_id}"
        return OracleRepairResult(
            verdict="pass",
            summary="deterministic route repair",
            cost_usd=0.01,
            agent_turns_used=1,
            oracle_invocations=1,
            packet_path=str(packet.packet_path),
            composite_gate={"passed": True, "reasons": []},
        )


def test_five_way_concurrent_merge_serializes_and_reenters_union_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    parent_id = "v5-five-way-parent"
    parent_branch = integration_branch_name(parent_id)
    _git(repo, "branch", parent_branch, "main", check=True)
    record_task(repo, task_id=ROOT_TASK_ID, intent="root", parent_task_id=None)
    record_task(
        repo,
        task_id=parent_id,
        intent="Integrate five concurrent route leaves",
        parent_task_id=ROOT_TASK_ID,
        integration_branch=parent_branch,
    )

    child_ids = [f"v5-route-{idx}" for idx in range(5)]
    expected_routes = {child_id: f"route-{idx}" for idx, child_id in enumerate(child_ids)}
    child_worktrees: dict[str, Path] = {}
    child_sessions: dict[str, Path] = {}
    for child_id, route in expected_routes.items():
        record_task(
            repo,
            task_id=child_id,
            intent=f"Register {route}",
            parent_task_id=parent_id,
            integration_branch=parent_branch,
            owned_paths=[ROUTE_FILE],
        )
        set_verdict(repo, child_id, "pass")
        worktree = setup_child_worktree(
            project_dir=repo,
            child_task_id=child_id,
            parent_integration_branch=parent_branch,
        )
        _write_route(worktree, route)
        child_worktrees[child_id] = worktree
        child_sessions[child_id] = _session_for(repo, child_id)

    repair = _ConcurrentRouteRepair(repo, expected_routes)
    route2_repaired = threading.Event()
    release_route2_retry = threading.Event()
    repair.pause_after_conflict_repair["v5-route-2"] = (
        route2_repaired,
        release_route2_retry,
    )
    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", repair)
    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight_with_repair", _passing_smoke_preflight)

    original_merge = v5_branching._merge_branch_into_worktree
    active = 0
    max_active = 0
    critical_entries: list[dict[str, str]] = []
    active_lock = threading.Lock()

    def instrumented_merge(**kwargs: Any) -> tuple[bool, str]:
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
            critical_entries.append({
                "branch": str(kwargs["branch"]),
                "target": str(kwargs["parent_integration_branch"]),
            })
        try:
            return original_merge(**kwargs)
        finally:
            with active_lock:
                active -= 1

    monkeypatch.setattr(v5_branching, "_merge_branch_into_worktree", instrumented_merge)

    start_barrier = threading.Barrier(len(child_ids))
    start_events = {child_id: threading.Event() for child_id in child_ids}
    events: list[dict[str, Any]] = []
    events_lock = threading.Lock()
    events_condition = threading.Condition(events_lock)

    def on_event(event: dict[str, Any]) -> None:
        with events_condition:
            events.append(dict(event))
            events_condition.notify_all()

    def wait_for_event(child_id: str, event_name: str) -> None:
        deadline = time.monotonic() + 10
        with events_condition:
            while not any(
                event.get("event") == event_name and event.get("task_id") == child_id
                for event in events
            ):
                remaining = deadline - time.monotonic()
                assert remaining > 0, {
                    "waiting_for": {"task_id": child_id, "event": event_name},
                    "events": list(events),
                    "repair_calls": list(repair.calls),
                }
                events_condition.wait(timeout=remaining)

    def merge_child(child_id: str) -> None:
        start_barrier.wait(timeout=10)
        assert start_events[child_id].wait(timeout=10), f"timed out starting {child_id}"
        result = LeadResult(
            task_id=child_id,
            verdict="pass",
            decomposition="inline",
            verify_called=True,
            verify_result={"verdict": "pass", "summary": "leaf fixture passed"},
        )
        asyncio.run(
            v5_runner._merge_child_branch(
                project_dir=repo,
                child_task_id=child_id,
                child_worktree=child_worktrees[child_id],
                child_session_dir=child_sessions[child_id],
                parent_integration_branch=parent_branch,
                result=result,
                config={"default_branch": "main"},
                on_event=on_event,
            )
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(child_ids)) as pool:
        futures = [pool.submit(merge_child, child_id) for child_id in child_ids]
        start_events["v5-route-0"].set()
        wait_for_event("v5-route-0", "merged")
        start_events["v5-route-1"].set()
        wait_for_event("v5-route-1", "integration_union_incomplete")
        start_events["v5-route-2"].set()
        assert route2_repaired.wait(timeout=10), {
            "waiting_for": "v5-route-2 conflict repair to pause before retry",
            "events": events,
            "repair_calls": repair.calls,
        }
        start_events["v5-route-3"].set()
        start_events["v5-route-4"].set()
        release_route2_retry.set()
        for future in futures:
            future.result(timeout=30)

    final_routes = _routes_from_text(_show(repo, parent_branch, ROUTE_FILE))
    assert final_routes == sorted(expected_routes.values()), {
        "final_routes": final_routes,
        "expected_routes": sorted(expected_routes.values()),
        "events": events,
        "repair_calls": repair.calls,
    }
    assert max_active == 1, {
        "max_active": max_active,
        "critical_entries": critical_entries,
    }
    union_events = [event for event in events if event.get("event") == "integration_union_incomplete"]
    assert union_events, {"events": events, "repair_calls": repair.calls}
    assert all(
        isinstance(event.get("structured_reason"), dict)
        and event["structured_reason"].get("kind") == "integration_union_incomplete"
        for event in union_events
    )
    assert any(call["is_union_guard"] for call in repair.calls), repair.calls

    parent_task = get_task(repo, parent_id) or {}
    union_state = parent_task.get("integration_union_guard") or {}
    touched_children = {
        item.get("child_task_id")
        for item in union_state.get("touches") or []
        if isinstance(item, dict) and item.get("path") == ROUTE_FILE
    }
    assert touched_children == set(child_ids), {
        "touched_children": sorted(touched_children),
        "expected": child_ids,
        "union_state": union_state,
    }


def test_task_graph_and_spec_state_concurrent_terminal_writes_are_complete_and_ordered(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    record_task(repo, task_id=ROOT_TASK_ID, intent="root", parent_task_id=None)

    child_count = 32
    child_ids = [f"child-{idx:02d}" for idx in range(child_count)]
    expected_verdicts = {
        child_id: ("merge_blocked" if idx % 3 == 0 else "pass")
        for idx, child_id in enumerate(child_ids)
    }
    start_barrier = threading.Barrier(child_count)
    errors: list[str] = []
    errors_lock = threading.Lock()

    def writer(child_id: str) -> None:
        verdict = expected_verdicts[child_id]
        try:
            record_task(
                repo,
                task_id=child_id,
                intent=f"Concurrent child {child_id}",
                parent_task_id=ROOT_TASK_ID,
                integration_branch=integration_branch_name(ROOT_TASK_ID),
            )
            spec_state.emit(session_dir, "group.started", group_id=child_id, detail="started")
            start_barrier.wait(timeout=10)
            structured_reason = {
                "kind": "terminal_verdict",
                "child_id": child_id,
                "verdict": verdict,
                "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            spec_state.emit(
                session_dir,
                "group.blocked" if verdict == "merge_blocked" else "group.merge.landed",
                group_id=child_id,
                detail=f"{child_id} {verdict}",
                verdict=verdict,
                structured_reason=structured_reason,
            )
            set_verdict(repo, child_id, verdict)
            update_task_metadata(
                repo,
                child_id,
                terminal_structured_reason=structured_reason,
                **(
                    {
                        "merge_blocked_reason": f"{child_id} blocked",
                        "merge_blocked_structured_reason": structured_reason,
                    }
                    if verdict == "merge_blocked"
                    else {}
                ),
            )
        except Exception as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(f"{child_id}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=writer, args=(child_id,)) for child_id in child_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []

    graph = read_graph(repo)
    tasks = graph["tasks"]
    assert set(child_ids).issubset(tasks)
    for child_id, verdict in expected_verdicts.items():
        task = tasks[child_id]
        assert task.get("verdict") == verdict
        if verdict == "merge_blocked":
            reason = task.get("merge_blocked_structured_reason")
            assert isinstance(reason, dict) and reason.get("child_id") == child_id

    journal_path = session_dir / "spec-state.jsonl"
    raw_lines = journal_path.read_text(encoding="utf-8").splitlines()
    rows: list[dict[str, Any]] = []
    for line_no, raw in enumerate(raw_lines, start=1):
        try:
            rows.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise AssertionError(f"torn spec-state line {line_no}: {raw!r}") from exc
    assert len(rows) == child_count * 2

    terminal_rows = [
        row
        for row in rows
        if row.get("kind") in {"group.blocked", "group.merge.landed"}
    ]
    terminal_by_child: dict[str, dict[str, Any]] = {}
    for row in terminal_rows:
        child_id = str(row.get("group_id") or "")
        assert child_id not in terminal_by_child, {
            "duplicate_terminal_row": child_id,
            "rows": terminal_rows,
        }
        terminal_by_child[child_id] = row
    assert set(terminal_by_child) == set(child_ids)
    for child_id, verdict in expected_verdicts.items():
        row = terminal_by_child[child_id]
        assert row.get("extra", {}).get("verdict") == verdict
        reason = row.get("extra", {}).get("structured_reason")
        assert isinstance(reason, dict) and reason.get("child_id") == child_id

    event_ids = [str(row.get("event_id") or "") for row in rows]
    expected_event_ids = [f"ev-{idx:06d}" for idx in range(1, len(rows) + 1)]
    assert event_ids == expected_event_ids, {
        "reason": "spec-state append must not use stale line-count reads under concurrent writers",
        "event_ids": event_ids,
        "expected": expected_event_ids,
    }
