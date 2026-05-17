# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

import pytest

from otto import v5_runner
from otto.lead import LeadResult
from otto.queue.subtask import enqueue_subtask
from otto.queue.task_graph import get_task, read_graph, record_task, set_decomposition, set_verdict
from otto.v5_capability_inventory import check_route_registration_isolation
from otto.v5_clean_verify import CleanOracleResult, CleanOracleStepResult, ToolchainPreflightResult


def _clean_scaffold_result(project_dir: Path, *_args: Any, **_kwargs: Any) -> CleanOracleResult:
    step = CleanOracleStepResult(
        id="scaffold",
        status="passed",
        return_code=0,
        command_identity="deterministic scaffold check",
        command=["true"],
        cwd=str(project_dir),
        env={},
    )
    return CleanOracleResult.from_parts(
        passed=True,
        scope="scaffold",
        issues=[],
        steps=[step],
        artifact_path_refs=[],
        command=step.command,
        env={},
        project_dir=project_dir,
    )


def _ia(registry_path: str, extension_glob: str) -> dict[str, Any]:
    return {
        "entry_states": [{"id": "home", "route": "/", "expected": "Home"}],
        "routes": [
            {"id": "alpha", "path": "/alpha", "key_text": "Alpha"},
            {"id": "beta", "path": "/beta", "key_text": "Beta"},
        ],
        "nav_surfaces": [{"id": "nav", "must_link_routes": ["alpha", "beta"]}],
        "action_surfaces": [],
        "registration_isolation": {
            "policy": "file_local_auto_discovery",
            "shared_registry_files": [
                {
                    "path": registry_path,
                    "discovers": extension_glob,
                    "leaf_edit": False,
                }
            ],
            "leaf_extension_globs": [extension_glob],
        },
    }


def _write_charter(project_dir: Path, registry_path: str, extension_glob: str) -> None:
    (project_dir / "CHARTER.md").write_text(
        "# CHARTER\n\n"
        "## Information Architecture Contract\n\n"
        "```json\n"
        + json.dumps(_ia(registry_path, extension_glob), indent=2)
        + "\n```\n\n"
        "## Agent operating notes\n\n"
        "- Unit tests: `uv run pytest`.\n",
        encoding="utf-8",
    )


def _write_monolithic_scaffold(project_dir: Path) -> None:
    path = project_dir / "backend" / "main.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "from fastapi import FastAPI\n"
        "from backend.routes import alpha, beta\n\n"
        "app = FastAPI()\n"
        "app.include_router(alpha.router)\n"
        "app.include_router(beta.router)\n",
        encoding="utf-8",
    )
    _write_charter(project_dir, "backend/main.py", "backend/routers/*.py")


def _write_isolated_scaffold(project_dir: Path) -> None:
    path = project_dir / "backend" / "main.py"
    routers = project_dir / "backend" / "routers"
    routers.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "from importlib import import_module\n"
        "from pathlib import Path\n"
        "from fastapi import FastAPI\n\n"
        "app = FastAPI()\n"
        "for module_path in (Path(__file__).parent / 'routers').glob('*.py'):\n"
        "    module = import_module(f'backend.routers.{module_path.stem}')\n"
        "    app.include_router(module.router)\n",
        encoding="utf-8",
    )
    _write_charter(project_dir, "backend/main.py", "backend/routers/*.py")


def _enqueue(
    project_dir: Path,
    *,
    parent_task_id: str,
    intent: str,
    depends_on: list[str] | None = None,
    owned_paths: list[str] | None = None,
) -> str:
    task_id = enqueue_subtask(
        project_dir=project_dir,
        parent_task_id=parent_task_id,
        parent_session_dir=project_dir / "otto_logs" / "sessions" / parent_task_id,
        intent=intent,
        depends_on=depends_on,
        owned_paths=owned_paths,
    )
    record_task(
        project_dir,
        task_id=task_id,
        intent=intent,
        parent_task_id=parent_task_id,
        depends_on=depends_on or [],
        owned_paths=owned_paths or [],
    )
    return task_id


def _setup_decomposition(
    project_dir: Path,
    *,
    mode: Literal["monolithic", "isolated"],
) -> tuple[str, list[str]]:
    record_task(project_dir, task_id="root", intent="Build route isolation repro")
    architect_id = _enqueue(
        project_dir,
        parent_task_id="root",
        intent="Architect webapp scaffold with backend route registration.",
    )
    if mode == "monolithic":
        alpha_paths = beta_paths = ["backend/main.py"]
    else:
        alpha_paths = ["backend/routers/alpha.py"]
        beta_paths = ["backend/routers/beta.py"]
    leaf_ids = [
        _enqueue(
            project_dir,
            parent_task_id="root",
            intent="Build Alpha route /alpha.",
            depends_on=[architect_id],
            owned_paths=alpha_paths,
        ),
        _enqueue(
            project_dir,
            parent_task_id="root",
            intent="Build Beta route /beta.",
            depends_on=[architect_id],
            owned_paths=beta_paths,
        ),
    ]
    return architect_id, leaf_ids


def _assert_no_shared_leaf_owned_files(project_dir: Path, leaf_ids: list[str]) -> None:
    graph = read_graph(project_dir)
    tasks = graph.get("tasks") or {}
    seen: dict[str, str] = {}
    for leaf_id in leaf_ids:
        task = tasks[leaf_id]
        for path in task.get("owned_paths") or []:
            previous = seen.setdefault(str(path), leaf_id)
            assert previous == leaf_id, f"{path} owned by both {previous} and {leaf_id}"


@pytest.mark.asyncio
async def test_monolithic_route_registry_reenters_architect_before_leaf_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    architect_id, leaf_ids = _setup_decomposition(tmp_path, mode="monolithic")
    dispatched: list[str] = []

    async def fake_run_child(**kwargs: Any) -> LeadResult:
        task_id = str(kwargs["entry"]["task_id"])
        dispatched.append(task_id)
        if task_id == architect_id:
            _write_monolithic_scaffold(tmp_path)
        elif task_id in leaf_ids:
            raise AssertionError("route leaves must not dispatch into known shared-registry collision")
        set_decomposition(tmp_path, task_id, "inline")
        set_verdict(tmp_path, task_id, "pass")
        return LeadResult(task_id=task_id, verdict="pass", decomposition="inline")

    monkeypatch.setattr(v5_runner, "_run_child", fake_run_child)
    monkeypatch.setattr(v5_runner, "verify_from_clean_oracle", _clean_scaffold_result)
    monkeypatch.setattr(v5_runner, "_verify_child_branches_reached_parent", lambda **_kwargs: None)
    monkeypatch.setattr(v5_runner, "_propagate_install_dirs_from_architect", lambda *_args: 0)
    monkeypatch.setattr(
        "otto.v5_clean_verify.preflight_shared_toolchains",
        lambda worktree, **_kwargs: ToolchainPreflightResult(
            passed=True,
            worktree=str(worktree),
            _written_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ),
    )
    events: list[dict[str, Any]] = []

    await v5_runner._process_children(
        project_dir=tmp_path,
        parent_task_id="root",
        config={},
        max_parallel=2,
        tree_budget_usd=10.0,
        child_results={},
        integration_results={},
        on_event=events.append,
    )

    feedback = check_route_registration_isolation(
        tmp_path,
        graph=read_graph(tmp_path),
        architect_task_id=architect_id,
    )
    assert feedback is not None
    assert feedback["kind"] == "shared_registry_not_isolated"
    assert feedback["shared_files"][0]["path"] == "backend/main.py"
    assert set(feedback["shared_files"][0]["task_ids"]) == set(leaf_ids)
    retry = next(event for event in events if event.get("event") == "architect_retry")
    assert retry["structured_reason"]["kind"] == "shared_registry_not_isolated"
    exhausted = next(event for event in events if event.get("event") == "architect_retry_exhausted")
    assert exhausted["structured_reason"]["kind"] == "shared_registry_not_isolated"
    assert all(task_id == architect_id for task_id in dispatched)
    arch_task = get_task(tmp_path, architect_id)
    assert arch_task is not None
    assert arch_task["merge_blocked_origin"] == "architect_contract"
    assert arch_task["merge_blocked_structured_reason"]["kind"] == "shared_registry_not_isolated"


@pytest.mark.asyncio
async def test_isolated_route_registration_contract_allows_parallel_leaves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    architect_id, leaf_ids = _setup_decomposition(tmp_path, mode="isolated")
    dispatched: list[str] = []

    async def fake_run_child(**kwargs: Any) -> LeadResult:
        task_id = str(kwargs["entry"]["task_id"])
        dispatched.append(task_id)
        if task_id == architect_id:
            _write_isolated_scaffold(tmp_path)
        elif task_id == leaf_ids[0]:
            (tmp_path / "backend" / "routers" / "alpha.py").write_text("router = object()\n", encoding="utf-8")
        elif task_id == leaf_ids[1]:
            (tmp_path / "backend" / "routers" / "beta.py").write_text("router = object()\n", encoding="utf-8")
        set_decomposition(tmp_path, task_id, "inline")
        set_verdict(tmp_path, task_id, "pass")
        return LeadResult(task_id=task_id, verdict="pass", decomposition="inline")

    monkeypatch.setattr(v5_runner, "_run_child", fake_run_child)
    monkeypatch.setattr(v5_runner, "verify_from_clean_oracle", _clean_scaffold_result)
    monkeypatch.setattr(v5_runner, "_verify_child_branches_reached_parent", lambda **_kwargs: None)
    monkeypatch.setattr(v5_runner, "_propagate_install_dirs_from_architect", lambda *_args: 0)
    monkeypatch.setattr(
        "otto.v5_clean_verify.preflight_shared_toolchains",
        lambda worktree, **_kwargs: ToolchainPreflightResult(
            passed=True,
            worktree=str(worktree),
            _written_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ),
    )
    events: list[dict[str, Any]] = []

    await v5_runner._process_children(
        project_dir=tmp_path,
        parent_task_id="root",
        config={},
        max_parallel=2,
        tree_budget_usd=10.0,
        child_results={},
        integration_results={},
        on_event=events.append,
    )

    assert check_route_registration_isolation(
        tmp_path,
        graph=read_graph(tmp_path),
        architect_task_id=architect_id,
    ) is None
    assert set(leaf_ids).issubset(dispatched)
    assert not any(event.get("event") == "architect_contract_invalid" for event in events)
    _assert_no_shared_leaf_owned_files(tmp_path, leaf_ids)
