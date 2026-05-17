from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from otto.lead import LeadResult
from otto.queue.task_graph import set_decomposition, set_verdict
from otto.spec_compile_flat import FlatSpec
from otto.v5_runner import ROOT_TASK_ID
from tests.test_v5_decomposed_child_lands_in_main import (
    _assert_file_reachable_from_main,
    _enqueue_fixed_task,
    _git,
    _init_repo,
    _lead_worktree,
    _write_file,
)


@pytest.mark.asyncio
async def test_four_root_children_reach_main_before_root_integration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from otto import v5_runner

    repo = tmp_path / "repo"
    _init_repo(repo)
    child_files = {
        "v5-architect": {"CHARTER.md": "# CHARTER\n\nArchitecture\n"},
        "v5-api": {"api/main.py": "def app():\n    return 'ok'\n"},
        "v5-web": {"frontend/src/App.tsx": "export const App = 'ok';\n"},
        "v5-docs": {"docs/runbook.md": "Runbook\n"},
    }
    integration_seen: dict[str, str] = {}

    async def fake_compile_flat_spec(**_kwargs: Any) -> FlatSpec:
        return FlatSpec(intent="build four surfaces", behavior_journeys=[])

    async def fake_run_lead(**kwargs: Any) -> LeadResult:
        task_id = kwargs["task_id"]
        if task_id == ROOT_TASK_ID and kwargs.get("kind") != "integration":
            for child_id in child_files:
                _enqueue_fixed_task(
                    repo,
                    task_id=child_id,
                    parent_task_id=ROOT_TASK_ID,
                    intent=f"Build {child_id}",
                )
            set_decomposition(repo, ROOT_TASK_ID, "emit")
            set_verdict(repo, ROOT_TASK_ID, "pending_children")
            return LeadResult(
                task_id=ROOT_TASK_ID,
                verdict="pending_children",
                decomposition="emit",
                emitted_subtask_ids=list(child_files),
            )
        if kwargs.get("kind") == "integration":
            assert _git(repo, "branch", "--show-current").stdout.strip() == "main"
            for files in child_files.values():
                for rel_path in files:
                    integration_seen[rel_path] = (repo / rel_path).read_text(encoding="utf-8")
            assert {s["task_id"] for s in kwargs["child_summaries"]} == set(child_files)
            set_verdict(repo, ROOT_TASK_ID, "pass")
            return LeadResult(
                task_id=ROOT_TASK_ID,
                verdict="pass",
                verify_called=True,
                verify_result={"verdict": "pass", "summary": "all children visible"},
            )

        worktree = _lead_worktree(kwargs["project_dir"], kwargs["session_dir"])
        for rel_path, content in child_files[task_id].items():
            _write_file(worktree, rel_path, content)
        set_decomposition(repo, task_id, "inline")
        set_verdict(repo, task_id, "pass")
        return LeadResult(task_id=task_id, verdict="pass", decomposition="inline")

    async def fake_smoke_preflight(**_kwargs):
        return {"check": "smoke_clean_deploy", "passed": True, "issues": []}

    monkeypatch.setattr(v5_runner, "compile_flat_spec", fake_compile_flat_spec)
    monkeypatch.setattr(v5_runner, "run_lead", fake_run_lead)
    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight_with_repair", fake_smoke_preflight)

    result = await v5_runner.run_v5_pipeline(
        project_dir=repo,
        intent="build four surfaces",
        config={"default_branch": "main"},
        max_parallel=4,
    )

    assert result.verdict == "pass"
    assert integration_seen == {
        rel_path: content
        for files in child_files.values()
        for rel_path, content in files.items()
    }
    for task_id, files in child_files.items():
        for rel_path, content in files.items():
            _assert_file_reachable_from_main(
                repo,
                task_id=task_id,
                rel_path=rel_path,
                expected_content=content,
            )
