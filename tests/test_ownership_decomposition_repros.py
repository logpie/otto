# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from otto import cli, v5_branching, v5_clean_verify, v5_runner
from otto.lead import LeadResult
from otto.queue.subtask import v5_pending_path
from otto.queue.task_graph import get_task, read_graph, record_task, set_verdict, update_task_metadata
from otto.v5_runner import ROOT_TASK_ID


pytestmark = pytest.mark.integration


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
    (repo / "CHARTER.md").write_text("# Ownership repro fixture\n", encoding="utf-8")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A", check=True)
    _git(repo, "commit", "-q", "-m", "init", check=True)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _enqueue(
    repo: Path,
    *,
    task_id: str,
    parent_task_id: str,
    depends_on: list[str] | None = None,
    owned_paths: list[str] | None = None,
    intent: str | None = None,
    integration_branch: str = "main",
) -> None:
    pending = v5_pending_path(repo)
    pending.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "schema_version": 1,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "parent_session_dir": str(repo / "otto_logs" / "sessions" / f"session-{parent_task_id}"),
        "intent": intent or task_id,
        "depends_on": list(depends_on or []),
        "owned_paths": list(owned_paths or []),
        "action_ids": [],
        "integration_branch": integration_branch,
        "review_state": "approved",
        "enqueued_at": _now_iso(),
    }
    with pending.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    record_task(
        repo,
        task_id=task_id,
        parent_task_id=parent_task_id,
        intent=str(entry["intent"]),
        integration_branch=integration_branch,
        depends_on=list(depends_on or []),
        owned_paths=list(owned_paths or []),
    )


def _write_session_spec(session_dir: Path) -> None:
    spec_path = session_dir / "spec" / "spec.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        json.dumps({"routes": [], "features": [], "behavior_journeys": []}, indent=2) + "\n",
        encoding="utf-8",
    )


class _CleanPass:
    passed = True
    issues: list[Any] = []

    def to_jsonable(self) -> dict[str, Any]:
        return {"passed": True, "issues": []}


@pytest.mark.asyncio
async def test_shared_foundation_contracts_block_feature_dispatch_after_architect_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "CHARTER.md").write_text(
        "# Ownership repro fixture\n\n"
        "Foundation contracts:\n"
        "- backend/auth.py\n"
        "- frontend/src/lib/ws.ts\n",
        encoding="utf-8",
    )
    record_task(repo, task_id=ROOT_TASK_ID, intent="root", parent_task_id=None)
    record_task(
        repo,
        task_id="architect",
        parent_task_id=ROOT_TASK_ID,
        intent="Architect scaffold",
        integration_branch="main",
        owned_paths=["backend/", "frontend/"],
    )
    set_verdict(repo, "architect", "pass")
    update_task_metadata(
        repo,
        "architect",
        foundation_contracts=[
            {"path": "backend/auth.py", "owner_task_id": "architect", "check": "semantic"},
            {"path": "frontend/src/lib/ws.ts", "owner_task_id": "architect", "check": "semantic"},
        ],
    )
    _enqueue(
        repo,
        task_id="feature-auth",
        parent_task_id=ROOT_TASK_ID,
        depends_on=["architect"],
        owned_paths=["backend/routers/auth.py", "frontend/src/features/auth/"],
        intent="Feature auth imports foundation contracts",
    )
    _enqueue(
        repo,
        task_id="feature-realtime",
        parent_task_id=ROOT_TASK_ID,
        depends_on=["architect"],
        owned_paths=["frontend/src/features/comments/"],
        intent="Feature realtime imports websocket foundation contract",
    )

    dispatched: list[str] = []
    events: list[dict[str, Any]] = []

    async def fake_run_child(**kwargs: Any) -> LeadResult:
        entry = kwargs["entry"]
        task_id = str(entry["task_id"])
        dispatched.append(task_id)
        set_verdict(repo, task_id, "pass")
        return LeadResult(task_id=task_id, verdict="pass", decomposition="inline", verify_called=True)

    monkeypatch.setattr(v5_runner, "verify_from_clean_oracle", lambda *_args, **_kwargs: _CleanPass())
    monkeypatch.setattr(v5_runner, "_run_child", fake_run_child)
    monkeypatch.setattr(v5_runner, "_verify_child_branches_reached_parent", lambda **_kwargs: None)
    monkeypatch.setattr(v5_runner, "_propagate_install_dirs_from_architect", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(v5_runner, "_write_toolchain_preflight_log", lambda **_kwargs: repo / "toolchain.json")

    class _ToolchainPass:
        passed = True
        commands: list[Any] = []
        failure_messages: list[str] = []

        def to_jsonable(self) -> dict[str, Any]:
            return {"passed": True, "commands": []}

    import otto.v5_capability_inventory as capability_inventory

    monkeypatch.setattr(v5_clean_verify, "preflight_shared_toolchains", lambda *_args, **_kwargs: _ToolchainPass())
    monkeypatch.setattr(capability_inventory, "check_route_registration_isolation", lambda *_args, **_kwargs: None)

    await v5_runner._process_children(
        project_dir=repo,
        parent_task_id=ROOT_TASK_ID,
        config={},
        max_parallel=4,
        tree_budget_usd=100.0,
        child_results={},
        integration_results={},
        on_event=events.append,
    )

    structured_kinds = [
        event.get("structured_reason", {}).get("kind")
        for event in events
        if isinstance(event.get("structured_reason"), dict)
    ]
    assert {
        "structured_kinds": structured_kinds,
        "dispatched": dispatched,
    } == {
        "structured_kinds": ["shared_foundation_not_isolated"],
        "dispatched": [],
    }


@pytest.mark.asyncio
async def test_merge_conflict_repair_does_not_expand_leaf_scope_into_clean_deploy_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "branch", "i2p/root/integration", "main", check=True)
    record_task(repo, task_id=ROOT_TASK_ID, intent="root", parent_task_id=None)
    record_task(
        repo,
        task_id="leaf",
        parent_task_id=ROOT_TASK_ID,
        intent="Leaf owns a feature slice",
        integration_branch="i2p/root/integration",
        owned_paths=["frontend/src/features/issues/"],
    )
    set_verdict(repo, "leaf", "pass")
    session_dir = repo / "otto_logs" / "sessions" / "session-leaf"
    _write_session_spec(session_dir)
    merge_calls = 0
    smoke_calls: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    def fake_merge(**_kwargs: Any) -> tuple[bool, str]:
        nonlocal merge_calls
        merge_calls += 1
        if merge_calls == 1:
            return False, "CONFLICT (add/add): Merge conflict in backend/auth.py"
        return True, "merged after scoped conflict repair"

    async def fake_conflict_repair(**_kwargs: Any) -> tuple[bool, str]:
        return True, "resolved backend/auth.py only"

    async def fake_smoke(**kwargs: Any) -> dict[str, Any]:
        smoke_calls.append(kwargs)
        return {
            "passed": False,
            "issues": [
                {
                    "kind": "clean_deploy_failed",
                    "severity": "block",
                    "path": "frontend/vite.config.ts",
                    "message": "shared Vite config binds the wrong host",
                }
            ],
            "repair": {
                "terminal_state": "escalated",
                "attempted_paths": ["frontend/vite.config.ts"],
            },
        }

    monkeypatch.setattr(v5_branching, "commit_worktree", lambda **_kwargs: (True, "committed"))
    monkeypatch.setattr(v5_branching, "merge_child_into_integration", fake_merge)
    monkeypatch.setattr(v5_runner, "_repair_child_merge_conflict_once", fake_conflict_repair)
    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight_with_repair", fake_smoke)

    result = LeadResult(task_id="leaf", verdict="pass", decomposition="inline", verify_called=True)
    await v5_runner._merge_child_branch(
        project_dir=repo,
        child_task_id="leaf",
        child_worktree=repo,
        child_session_dir=session_dir,
        parent_integration_branch="i2p/root/integration",
        result=result,
        config={},
        on_event=events.append,
    )

    repair_need_events = [
        event
        for event in events
        if event.get("event") in {"foundation_repair_needed", "integration_repair_needed"}
    ]
    assert smoke_calls == []
    assert repair_need_events, events
    assert result.verdict != "merge_blocked"


def test_clean_verify_cli_uses_explicit_repair_worktree_env_instead_of_ambient_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_dir = tmp_path / "main"
    repair_worktree = tmp_path / "repair-worktree"
    main_dir.mkdir()
    repair_worktree.mkdir()
    captured: dict[str, Path] = {}

    def fake_verify(project_dir: Path, **_kwargs: Any) -> _CleanPass:
        captured["project_dir"] = project_dir
        return _CleanPass()

    monkeypatch.chdir(main_dir)
    monkeypatch.setenv("OTTO_CLEAN_VERIFY_WORKTREE", str(repair_worktree))
    monkeypatch.setattr(v5_clean_verify, "verify_from_clean_oracle", fake_verify)

    callback = getattr(cli.clean_verify_command, "callback", cli.clean_verify_command)
    callback(
        json_output=True,
        verify_scope="subtree",
        repair_packet=None,
        spec_path=None,
        journey_scope=None,
        journey_artifact_dir=None,
    )

    assert captured["project_dir"] == repair_worktree


def test_semantic_foundation_contracts_do_not_require_literal_line_union_but_registries_do() -> None:
    state = {
        "schema_version": 1,
        "parent_integration_branch": "main",
        "foundation_contracts": {
            "frontend/src/lib/ws.ts": {
                "owner_task_id": "foundation",
                "check": "semantic",
                "required_exports": ["connect"],
            }
        },
        "touches": [
            {"child_task_id": "foundation", "path": "frontend/src/lib/ws.ts"},
            {"child_task_id": "feature-comments", "path": "frontend/src/lib/ws.ts"},
            {"child_task_id": "routes-a", "path": "frontend/src/routes.tsx"},
            {"child_task_id": "routes-b", "path": "frontend/src/routes.tsx"},
        ],
        "contributions": [
            {
                "child_task_id": "foundation",
                "path": "frontend/src/lib/ws.ts",
                "line": "export function connect(workspaceId: string) {",
                "line_hash": "semantic-connect-v1",
                "source_branch": "foundation",
                "base_ref": "base",
                "head_ref": "foundation",
            },
            {
                "child_task_id": "routes-a",
                "path": "frontend/src/routes.tsx",
                "line": "registerRoute('/issues', IssuesPage)",
                "line_hash": "route-issues",
                "source_branch": "routes-a",
                "base_ref": "base",
                "head_ref": "routes-a",
            },
        ],
    }
    final_text_by_path = {
        "frontend/src/lib/ws.ts": (
            "export function connect(workspaceId: string, token?: string) {\n"
            "  return openSocket(workspaceId, token)\n"
            "}\n"
            "export function disconnect() {}\n"
        ),
        "frontend/src/routes.tsx": "registerRoute('/workspaces', WorkspacesPage)\n",
    }

    missing = v5_runner._integration_union_missing_contributions(state, final_text_by_path)
    missing_by_path = {
        path: [item["line"] for item in missing if item["path"] == path]
        for path in {item["path"] for item in missing}
    }
    assert missing_by_path.get("frontend/src/lib/ws.ts", []) == []
    assert missing_by_path.get("frontend/src/routes.tsx") == ["registerRoute('/issues', IssuesPage)"]


@pytest.mark.asyncio
async def test_shared_contract_union_feedback_routes_to_foundation_owner_not_leaf_scope_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "branch", "i2p/root/integration", "main", check=True)
    record_task(repo, task_id=ROOT_TASK_ID, intent="root", parent_task_id=None)
    record_task(
        repo,
        task_id="foundation",
        parent_task_id=ROOT_TASK_ID,
        intent="Foundation owner",
        integration_branch="i2p/root/integration",
        owned_paths=["frontend/src/lib/ws.ts"],
    )
    update_task_metadata(
        repo,
        ROOT_TASK_ID,
        foundation_contracts=[
            {"path": "frontend/src/lib/ws.ts", "owner_task_id": "foundation", "check": "semantic"}
        ],
    )
    record_task(
        repo,
        task_id="leaf",
        parent_task_id=ROOT_TASK_ID,
        intent="Feature leaf uses shared websocket contract",
        integration_branch="i2p/root/integration",
        owned_paths=["frontend/src/features/comments/"],
    )
    set_verdict(repo, "leaf", "pass")
    session_dir = repo / "otto_logs" / "sessions" / "session-leaf"
    _write_session_spec(session_dir)
    child_repair_calls: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    def fake_union_feedback(**_kwargs: Any) -> dict[str, Any]:
        return {
            "kind": "integration_union_incomplete",
            "step_id": "integration_union_guard",
            "message": "frontend/src/lib/ws.ts missing compatible shared contract contribution",
            "paths": ["frontend/src/lib/ws.ts"],
            "missing": [
                {
                    "path": "frontend/src/lib/ws.ts",
                    "line": "export function connect(workspaceId: string) {",
                    "contributed_by": "foundation",
                    "source_branch": "foundation",
                }
            ],
            "child_task_id": "leaf",
            "parent_integration_branch": "i2p/root/integration",
            "integration_context": {
                "integration_union_guard": {
                    "paths": ["frontend/src/lib/ws.ts"],
                    "missing": [{"path": "frontend/src/lib/ws.ts"}],
                }
            },
        }

    async def fake_leaf_repair(**kwargs: Any) -> tuple[bool, str]:
        child_repair_calls.append(kwargs)
        return False, "scope gate rejected frontend/src/lib/ws.ts"

    monkeypatch.setattr(v5_branching, "commit_worktree", lambda **_kwargs: (True, "committed"))
    monkeypatch.setattr(v5_branching, "merge_child_into_integration", lambda **_kwargs: (True, "merged"))
    monkeypatch.setattr(v5_runner, "_record_and_check_integration_union", fake_union_feedback)
    monkeypatch.setattr(v5_runner, "_repair_child_upward_merge_gate_once", fake_leaf_repair)

    result = LeadResult(task_id="leaf", verdict="pass", decomposition="inline", verify_called=True)
    await v5_runner._merge_child_branch(
        project_dir=repo,
        child_task_id="leaf",
        child_worktree=repo,
        child_session_dir=session_dir,
        parent_integration_branch="i2p/root/integration",
        result=result,
        config={},
        on_event=events.append,
    )

    root_task = get_task(repo, ROOT_TASK_ID) or {}
    graph = read_graph(repo)
    routed_events = [
        event
        for event in events
        if event.get("event") in {"foundation_contract_amendment_repair", "foundation_repair_needed"}
    ]
    amendment_tasks = [
        task
        for task in (graph.get("tasks") or {}).values()
        if task.get("task_role") == "contract_amendment"
        or task.get("repair_route") == "foundation_contract_amendment"
    ]
    assert child_repair_calls == []
    assert routed_events or amendment_tasks or root_task.get("foundation_contract_repairs")
