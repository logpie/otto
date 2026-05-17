from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from otto.journey_scope_policy import (
    APPLICABILITY_POLICY,
    EXECUTION_SCOPES,
    VERIFICATION_LEVELS,
    validate_policy_exhaustive,
)
from otto.lead import LeadResult
from otto.queue.task_graph import set_decomposition, set_verdict
from otto.spec_compile_flat import FlatSpec
from otto.v5_runner import ROOT_TASK_ID
from tests.test_v5_decomposed_child_lands_in_main import (
    _enqueue_fixed_task,
    _git,
    _init_repo,
)


def test_execution_scope_policy_covers_every_cell() -> None:
    validate_policy_exhaustive()

    assert set(APPLICABILITY_POLICY) == {
        (scope, level)
        for scope in EXECUTION_SCOPES
        for level in VERIFICATION_LEVELS
    }
    assert APPLICABILITY_POLICY[("root_integration", "ui")] == "run"
    assert APPLICABILITY_POLICY[("subtree_integration", "ui")] == "defer"
    assert APPLICABILITY_POLICY[("leaf", "api")] == "run"


def test_scope_level_applicability_has_no_ad_hoc_conditionals() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    policy_path = repo_root / "otto" / "journey_scope_policy.py"
    offenders: list[str] = []
    for path in sorted((repo_root / "otto").rglob("*.py")):
        if path == policy_path or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and "execution_scope" in ast.unparse(node.test):
                offenders.append(f"{path.relative_to(repo_root)}:{node.lineno}")

    assert offenders == []


@pytest.mark.asyncio
async def test_depth_three_graph_has_single_root_integration_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from otto import v5_runner

    repo = tmp_path / "repo"
    _init_repo(repo)
    scopes: list[tuple[str, str, str]] = []

    async def fake_compile_flat_spec(**_kwargs: Any) -> FlatSpec:
        return FlatSpec(intent="depth three", behavior_journeys=[])

    async def fake_run_lead(**kwargs: Any) -> LeadResult:
        task_id = str(kwargs["task_id"])
        kind = str(kwargs.get("kind") or "plan_or_inline")
        scope = str(kwargs.get("execution_scope") or "")
        scopes.append((task_id, kind, scope))
        if task_id == ROOT_TASK_ID and kind != "integration":
            _enqueue_fixed_task(
                repo,
                task_id="child",
                parent_task_id=ROOT_TASK_ID,
                intent="Build child",
            )
            set_decomposition(repo, ROOT_TASK_ID, "emit")
            set_verdict(repo, ROOT_TASK_ID, "pending_children")
            return LeadResult(
                task_id=ROOT_TASK_ID,
                verdict="pending_children",
                decomposition="emit",
                emitted_subtask_ids=["child"],
            )
        if task_id == "child" and kind != "integration":
            _enqueue_fixed_task(
                repo,
                task_id="grandchild",
                parent_task_id="child",
                intent="Build grandchild",
            )
            set_decomposition(repo, "child", "emit")
            set_verdict(repo, "child", "pending_children")
            return LeadResult(
                task_id="child",
                verdict="pending_children",
                decomposition="emit",
                emitted_subtask_ids=["grandchild"],
            )
        set_decomposition(repo, task_id, "inline")
        set_verdict(repo, task_id, "pass")
        return LeadResult(
            task_id=task_id,
            verdict="pass",
            decomposition="inline",
            verify_called=True,
            verify_result={"verdict": "pass", "summary": "ok"},
        )

    async def fake_smoke_preflight(**_kwargs: Any) -> dict[str, Any]:
        return {"check": "smoke_clean_deploy", "passed": True, "issues": []}

    monkeypatch.setattr(v5_runner, "compile_flat_spec", fake_compile_flat_spec)
    monkeypatch.setattr(v5_runner, "run_lead", fake_run_lead)
    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight_with_repair", fake_smoke_preflight)

    result = await v5_runner.run_v5_pipeline(
        project_dir=repo,
        intent="build depth three",
        config={"default_branch": "main"},
        max_parallel=1,
    )

    assert result.verdict == "pass"
    assert [scope for _task, _kind, scope in scopes].count("root_integration") == 1
    assert ("child", "integration", "subtree_integration") in scopes
    assert _git(repo, "branch", "--show-current").stdout.strip() == "main"
