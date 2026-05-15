from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from otto.lead import LeadResult
from otto.queue.task_graph import get_task, record_task, set_decomposition, set_verdict
from otto.spec_compile_flat import FlatSpec
from otto.v5_branching import child_branch_name
from otto.v5_runner import ROOT_TASK_ID
from tests.test_v5_decomposed_child_lands_in_main import _git, _init_repo


def _git_ok(repo: Path, *args: str) -> None:
    cp = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if cp.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed\n{cp.stdout}\n{cp.stderr}")


@pytest.mark.asyncio
async def test_root_integration_step0b_recovery_flips_final_verdict_to_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from otto import v5_runner

    repo = tmp_path / "repo"
    _init_repo(repo)
    child_id = "v5-blocked"
    child_branch = child_branch_name(child_id)
    events: list[dict[str, Any]] = []

    async def fake_compile_flat_spec(**_kwargs: Any) -> FlatSpec:
        return FlatSpec(intent="recover blocked child", behavior_journeys=[])

    async def fake_process_children(**kwargs: Any) -> None:
        project_dir = kwargs["project_dir"]
        child_results = kwargs["child_results"]
        record_task(project_dir, task_id=child_id, intent="blocked child", parent_task_id=ROOT_TASK_ID)
        _git_ok(project_dir, "checkout", "-q", "-b", child_branch, "main")
        (project_dir / "frontend" / "src").mkdir(parents=True)
        (project_dir / "frontend" / "src" / "Recovered.tsx").write_text(
            "export const Recovered = true;\n",
            encoding="utf-8",
        )
        _git_ok(project_dir, "add", "frontend/src/Recovered.tsx")
        _git_ok(project_dir, "commit", "-q", "-m", "child recovered work")
        _git_ok(project_dir, "checkout", "-q", "main")
        set_verdict(project_dir, child_id, "merge_blocked")
        child_results[child_id] = LeadResult(
            task_id=child_id,
            verdict="pass",
            final_text="agent self-verdict passed before merge failed",
        )

    async def fake_run_lead(**kwargs: Any) -> LeadResult:
        if kwargs.get("kind") == "integration":
            summary = kwargs["child_summaries"][0]
            assert summary["verdict"] == "merge_blocked"
            assert summary["build_branch"] == child_branch
            _git_ok(repo, "merge", "--no-ff", "--no-edit", child_branch)
            set_verdict(repo, ROOT_TASK_ID, "pass")
            return LeadResult(
                task_id=ROOT_TASK_ID,
                verdict="pass",
                verify_called=True,
                verify_result={"verdict": "pass", "summary": "Step 0b recovered"},
            )
        set_decomposition(repo, ROOT_TASK_ID, "emit")
        set_verdict(repo, ROOT_TASK_ID, "pending_children")
        return LeadResult(
            task_id=ROOT_TASK_ID,
            verdict="pending_children",
            decomposition="emit",
            emitted_subtask_ids=[child_id],
        )

    async def fake_smoke_preflight(**_kwargs: Any) -> dict[str, Any]:
        return {"check": "smoke_clean_deploy", "passed": True, "issues": []}

    monkeypatch.setattr(v5_runner, "compile_flat_spec", fake_compile_flat_spec)
    monkeypatch.setattr(v5_runner, "_process_children", fake_process_children)
    monkeypatch.setattr(v5_runner, "run_lead", fake_run_lead)
    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight_with_repair", fake_smoke_preflight)

    result = await v5_runner.run_v5_pipeline(
        project_dir=repo,
        intent="recover blocked child",
        config={"default_branch": "main"},
        on_event=events.append,
    )

    assert result.verdict == "pass"
    assert (get_task(repo, child_id) or {}).get("verdict") == "pass"
    assert (repo / "frontend" / "src" / "Recovered.tsx").read_text(encoding="utf-8")
    assert _git(repo, "merge-base", "--is-ancestor", child_branch, "main").returncode == 0
    assert any(e.get("event") == "child_recovery_reconciled" for e in events)
