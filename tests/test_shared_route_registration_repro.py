# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from otto import v5_runner
from otto.lead import LeadResult
from otto.queue.task_graph import get_task, record_task, set_verdict
from otto.v5_branching import (
    ensure_initial_commit,
    integration_branch_name,
    setup_child_worktree,
)
from otto.v5_clean_verify import verify_from_clean_oracle
from otto.v5_preflight_repair import (
    OracleRepairResult,
    RepairPacket,
    append_repair_packet_oracle_event,
    run_oracle_repair_agent as real_run_oracle_repair_agent,
)


pytestmark = pytest.mark.integration

ROUTE_FILE = "app/main.py"
PARENT_ID = "v5-shared-routes"
EXPECTED_LEAF_ROUTES = ["/a", "/b", "/c"]
BASE_ROUTES = ["/"]
ROUTE_ORDER = {route: index for index, route in enumerate([*BASE_ROUTES, *EXPECTED_LEAF_ROUTES])}


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


def _render_route_file(routes: list[str]) -> str:
    ordered = sorted(dict.fromkeys(routes), key=lambda route: ROUTE_ORDER.get(route, 999))
    route_lines = "\n".join(
        f'register("{route}", endpoint("{route.strip("/") or "home"}"))'
        for route in ordered
    )
    return f'''from __future__ import annotations

from collections.abc import Callable

ROUTES: list[tuple[str, Callable[[], str]]] = []


def endpoint(name: str) -> Callable[[], str]:
    def handler() -> str:
        return name

    return handler


def register(path: str, handler: Callable[[], str]) -> None:
    ROUTES.append((path, handler))


# ROUTES START
{route_lines}
# ROUTES END
'''


def _route_region(text: str) -> str:
    match = re.search(r"# ROUTES START\n(?P<region>.*?)# ROUTES END", text, flags=re.DOTALL)
    assert match is not None, text
    return match.group("region")


def _routes_from_text(text: str, *, leaf_only: bool = False) -> list[str]:
    routes = re.findall(r'register\("([^"]+)"', text)
    if leaf_only:
        routes = [route for route in routes if route in EXPECTED_LEAF_ROUTES]
    return routes


def _route_counts(text: str) -> dict[str, int]:
    return {route: _routes_from_text(text).count(route) for route in [*BASE_ROUTES, *EXPECTED_LEAF_ROUTES]}


def _write_routes(worktree: Path, routes: list[str]) -> None:
    path = worktree / ROUTE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_route_file(routes), encoding="utf-8")


def _show(repo: Path, ref: str, path: str) -> str:
    return _git(repo, "show", f"{ref}:{path}", check=True).stdout


def _init_route_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main", check=True)
    _git(repo, "config", "user.email", "test@example.invalid", check=True)
    _git(repo, "config", "user.name", "Test User", check=True)
    (repo / ".gitignore").write_text(".worktrees/\notto_logs/\n.otto/\n", encoding="utf-8")
    (repo / "CHARTER.md").write_text("# Shared route fixture\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"shared-route-fixture\"\nversion = \"0.0.0\"\n",
        encoding="utf-8",
    )
    _write_routes(repo, BASE_ROUTES)
    assert ensure_initial_commit(repo)


def _write_child_session(child_session_dir: Path) -> None:
    (child_session_dir / "spec").mkdir(parents=True, exist_ok=True)
    (child_session_dir / "spec" / "spec.json").write_text(
        json.dumps(
            {
                "routes": EXPECTED_LEAF_ROUTES,
                "features": [],
                "behavior_journeys": [],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


@dataclass(frozen=True)
class RepairCall:
    task_id: str
    target_branch: str
    resolved_routes: list[str]
    packet_path: str


@dataclass(frozen=True)
class CaseEvidence:
    mode: str
    wall_time_s: float
    parent_branch: str
    final_region: str
    final_leaf_routes: list[str]
    route_counts: dict[str, int]
    child_verdicts: dict[str, str | None]
    child_reasons: dict[str, str | None]
    repair_calls: list[RepairCall]
    events: list[dict[str, Any]]
    repair_events: list[dict[str, Any]]

    @property
    def landed(self) -> bool:
        return all(verdict == "pass" for verdict in self.child_verdicts.values())

    def to_json(self) -> str:
        return json.dumps(
            {
                "mode": self.mode,
                "wall_time_s": self.wall_time_s,
                "parent_branch": self.parent_branch,
                "final_region": self.final_region,
                "final_leaf_routes": self.final_leaf_routes,
                "route_counts": self.route_counts,
                "child_verdicts": self.child_verdicts,
                "child_reasons": self.child_reasons,
                "repair_calls": [call.__dict__ for call in self.repair_calls],
                "events": self.events,
                "repair_events": self.repair_events,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )


class RouteRepairAgent:
    def __init__(self, mode: Literal["faithful", "lossy"]) -> None:
        self.mode = mode
        self.calls: list[RepairCall] = []
        self.packet_paths: list[Path] = []

    async def __call__(
        self,
        prompt: str,
        _options: Any,
        **_kwargs: Any,
    ) -> tuple[str, float, str, dict[str, Any]]:
        packet_path = _packet_path_from_prompt(prompt)
        packet = RepairPacket.load(packet_path)
        self.packet_paths.append(packet_path)
        worktree = Path(str(packet.repair_unit["worktree"]))
        target_branch = str(packet.integration_context["merge_refs"]["ours_ref"])

        merge = _git(worktree, "merge", "--no-ff", "--no-edit", target_branch)
        if merge.returncode == 0:
            merge_detail = "target merged without conflict"
        else:
            status = _git(worktree, "diff", "--name-only", "--diff-filter=U", check=True).stdout
            assert ROUTE_FILE in status.splitlines(), (
                f"expected {ROUTE_FILE} to be in a real conflict after merging "
                f"{target_branch} into {worktree}; stdout={merge.stdout!r}; stderr={merge.stderr!r}"
            )
            merge_detail = "target merge produced expected route conflict"

        conflict_packet = cast(dict[str, Any], packet.integration_context["conflict_packet"])
        conflicts = cast(list[dict[str, Any]], conflict_packet["conflicts"])
        routes: list[str] = []
        for conflict in conflicts:
            for side in ("base", "ours", "theirs"):
                routes.extend(_routes_from_text(str(conflict.get(side) or "")))
        routes = sorted(dict.fromkeys(routes), key=lambda route: ROUTE_ORDER.get(route, 999))

        if self.mode == "lossy" and "/c" in routes and "/a" in routes:
            routes = [route for route in routes if route != "/a"]

        _write_routes(worktree, routes)
        _git(worktree, "add", ROUTE_FILE, check=True)
        oracle = verify_from_clean_oracle(worktree, scope="subtree", timeout_s=10, port_wait_s=1)
        append_repair_packet_oracle_event(packet_path, oracle, source="agent")

        task_id = str(packet.repair_unit["task_id"])
        self.calls.append(
            RepairCall(
                task_id=task_id,
                target_branch=target_branch,
                resolved_routes=[route for route in routes if route in EXPECTED_LEAF_ROUTES],
                packet_path=str(packet_path),
            )
        )
        return (
            f"{merge_detail}; resolved {ROUTE_FILE} with {routes}",
            0.0,
            f"route-repair-{self.mode}-{len(self.calls)}",
            {"cost_usd": 0.0},
        )


def _packet_path_from_prompt(prompt: str) -> Path:
    match = re.search(r"Repair packet: (?P<path>.+?repair_packet\.json)", prompt)
    assert match is not None, prompt
    return Path(match.group("path").strip())


def _collect_repair_events(packet_paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for packet_path in packet_paths:
        packet = RepairPacket.load(packet_path)
        for row in packet.events():
            event = dict(row)
            event["packet_path"] = str(packet_path)
            rows.append(event)
    return rows


def _assert_gate_refusals_have_structured_reasons(evidence: CaseEvidence) -> None:
    refusals = [
        row
        for row in evidence.repair_events
        if isinstance(row.get("event"), dict)
        and row["event"].get("type") == "composite_gate"
        and row["event"].get("passed") is False
    ]
    missing = [
        row
        for row in refusals
        if not isinstance(row["event"].get("reasons"), list) or not row["event"]["reasons"]
    ]
    assert not missing, (
        "composite-gate refusal did not record structured reasons:\n"
        f"{evidence.to_json()}"
    )


def test_integration_union_guard_detects_missing_shared_added_line() -> None:
    route_a = 'register("/a", endpoint("a"))'
    route_b = 'register("/b", endpoint("b"))'
    state = v5_runner._integration_union_empty_state("i2p/integ/shared")
    state = v5_runner._merge_integration_union_state(
        state=state,
        child_task_id="child-a",
        source_branch="i2p/build/child-a",
        base_ref="base-a",
        head_ref="i2p/build/child-a",
        additions_by_path={ROUTE_FILE: [route_a]},
        touched_paths=[ROUTE_FILE],
    )

    assert (
        v5_runner._integration_union_missing_contributions(
            state,
            {ROUTE_FILE: route_a},
        )
        == []
    )

    state = v5_runner._merge_integration_union_state(
        state=state,
        child_task_id="child-b",
        source_branch="i2p/build/child-b",
        base_ref="base-b",
        head_ref="i2p/build/child-b",
        additions_by_path={ROUTE_FILE: [route_b]},
        touched_paths=[ROUTE_FILE],
    )
    assert (
        v5_runner._integration_union_missing_contributions(
            state,
            {ROUTE_FILE: f"{route_a}\n{route_b}\n"},
        )
        == []
    )

    missing = v5_runner._integration_union_missing_contributions(
        state,
        {ROUTE_FILE: f"{route_b}\n"},
    )
    assert missing == [
        {
            "path": ROUTE_FILE,
            "line": route_a,
            "line_hash": v5_runner._line_hash(route_a),
            "contributed_by": "child-a",
            "source_branch": "i2p/build/child-a",
            "base_ref": "base-a",
            "head_ref": "i2p/build/child-a",
        }
    ]
    feedback = v5_runner._integration_union_feedback(
        parent_integration_branch="i2p/integ/shared",
        child_task_id="child-b",
        source_branch="i2p/build/child-b",
        base_ref="base-b",
        post_merge_ref="post-b",
        missing=missing,
        final_text_by_path={ROUTE_FILE: f"{route_b}\n"},
    )
    assert feedback["kind"] == "integration_union_incomplete"
    assert feedback["missing"][0]["line"] == route_a
    assert feedback["missing"][0]["contributed_by"] == "child-a"
    assert route_a in feedback["message"]


async def _run_shared_route_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: Literal["faithful", "lossy"],
) -> CaseEvidence:
    started = time.monotonic()
    repo = tmp_path / f"repo-{mode}"
    _init_route_repo(repo)
    parent_branch = integration_branch_name(PARENT_ID)
    _git(repo, "branch", parent_branch, "main", check=True)

    record_task(repo, task_id="root", intent="root", parent_task_id=None)
    record_task(
        repo,
        task_id=PARENT_ID,
        intent="Integrate shared route leaves",
        parent_task_id="root",
        integration_branch=parent_branch,
    )

    child_worktrees: dict[str, Path] = {}
    child_sessions: dict[str, Path] = {}
    child_routes = {
        "v5-route-a": "/a",
        "v5-route-b": "/b",
        "v5-route-c": "/c",
    }
    for child_id, route in child_routes.items():
        record_task(
            repo,
            task_id=child_id,
            intent=f"Register route {route}",
            parent_task_id=PARENT_ID,
            integration_branch=parent_branch,
            owned_paths=[],
        )
        set_verdict(repo, child_id, "pass")
        child_worktree = setup_child_worktree(
            project_dir=repo,
            child_task_id=child_id,
            parent_integration_branch=parent_branch,
        )
        _write_routes(child_worktree, [*BASE_ROUTES, route])
        child_worktrees[child_id] = child_worktree
        child_session_dir = repo / "otto_logs" / "sessions" / f"session-{child_id}"
        _write_child_session(child_session_dir)
        child_sessions[child_id] = child_session_dir

    agent = RouteRepairAgent(mode)

    async def deterministic_repair(
        packet: RepairPacket,
        *,
        config: dict[str, Any],
        commit_hook: Any = None,
        **_kwargs: Any,
    ) -> OracleRepairResult:
        return await real_run_oracle_repair_agent(
            packet,
            config=config,
            agent_runner=agent,
            commit_hook=commit_hook,
        )

    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", deterministic_repair)

    events: list[dict[str, Any]] = []
    config: dict[str, Any] = {
        "default_branch": "main",
        "run_budget_seconds": 60,
        "max_turns_per_call": 1,
        "merge_repair_agent_turns": 2,
        "merge_repair_oracle_invocations": 4,
        "merge_repair_wall_clock_s": 60,
    }
    for child_id in child_routes:
        result = LeadResult(
            task_id=child_id,
            verdict="pass",
            decomposition="inline",
            verify_called=True,
            verify_result={"verdict": "pass", "summary": "leaf fixture passed"},
        )
        await v5_runner._merge_child_branch(
            project_dir=repo,
            child_task_id=child_id,
            child_worktree=child_worktrees[child_id],
            child_session_dir=child_sessions[child_id],
            parent_integration_branch=parent_branch,
            result=result,
            config=config,
            on_event=events.append,
        )

    final_text = _show(repo, parent_branch, ROUTE_FILE)
    child_verdicts = {
        child_id: cast(str | None, (get_task(repo, child_id) or {}).get("verdict"))
        for child_id in child_routes
    }
    child_reasons = {
        child_id: cast(str | None, (get_task(repo, child_id) or {}).get("merge_blocked_reason"))
        for child_id in child_routes
    }
    return CaseEvidence(
        mode=mode,
        wall_time_s=round(time.monotonic() - started, 3),
        parent_branch=parent_branch,
        final_region=_route_region(final_text).strip(),
        final_leaf_routes=_routes_from_text(final_text, leaf_only=True),
        route_counts=_route_counts(final_text),
        child_verdicts=child_verdicts,
        child_reasons=child_reasons,
        repair_calls=agent.calls,
        events=events,
        repair_events=_collect_repair_events(agent.packet_paths),
    )


@pytest.mark.asyncio
async def test_faithful_pairwise_route_repair_preserves_complete_union_and_lands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = await _run_shared_route_case(tmp_path, monkeypatch, mode="faithful")

    conflict_starts = [
        event["task_id"]
        for event in evidence.events
        if event.get("event") == "merge_conflict_repair_agent_start"
    ]
    assert conflict_starts == ["v5-route-b", "v5-route-c"], (
        "repro did not exercise the expected sequential pairwise conflict path:\n"
        f"{evidence.to_json()}"
    )
    assert [call.task_id for call in evidence.repair_calls] == ["v5-route-b", "v5-route-c"]
    assert evidence.final_leaf_routes == EXPECTED_LEAF_ROUTES, (
        "faithful pairwise conflict repair failed to preserve the complete route union:\n"
        f"{evidence.to_json()}"
    )
    assert all(evidence.route_counts[route] == 1 for route in EXPECTED_LEAF_ROUTES), (
        "faithful repair produced duplicate or missing route registrations:\n"
        f"{evidence.to_json()}"
    )
    assert evidence.landed, (
        "faithful pairwise conflict repair did not land all children:\n"
        f"{evidence.to_json()}"
    )
    _assert_gate_refusals_have_structured_reasons(evidence)


@pytest.mark.asyncio
async def test_lossy_pairwise_route_repair_cannot_silently_land_incomplete_union(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = await _run_shared_route_case(tmp_path, monkeypatch, mode="lossy")

    silently_landed_missing_union = evidence.landed and evidence.final_leaf_routes != EXPECTED_LEAF_ROUTES
    assert not silently_landed_missing_union, (
        "silent route-drop slipped through; the integration landed without the complete union:\n"
        f"{evidence.to_json()}"
    )
    reason_text = evidence.child_reasons.get("v5-route-c") or ""
    assert "integration union incomplete" in reason_text
    assert 'register("/a", endpoint("a"))' in reason_text
    assert "v5-route-a" in reason_text
    _assert_gate_refusals_have_structured_reasons(evidence)
