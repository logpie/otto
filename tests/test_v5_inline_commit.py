from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from otto.lead import LeadResult
from otto.queue.task_graph import set_decomposition, set_verdict
from otto.spec_compile_flat import FlatSpec
from otto.v5_runner import ROOT_TASK_ID, run_v5_pipeline


def _git(cwd: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and cp.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd}\n"
            f"stdout:\n{cp.stdout}\nstderr:\n{cp.stderr}"
        )
    return cp


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main", check=True)
    _git(repo, "config", "user.email", "test@example.invalid", check=True)
    _git(repo, "config", "user.name", "Test User", check=True)
    (repo / "README.md").write_text("initial repo\n", encoding="utf-8")
    _git(repo, "add", "README.md", check=True)
    _git(repo, "commit", "-q", "-m", "init", check=True)


@pytest.mark.asyncio
async def test_root_inline_build_is_committed_to_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from otto import v5_runner

    repo = tmp_path / "repo"
    _init_repo(repo)

    async def fake_compile_flat_spec(**kwargs: Any) -> FlatSpec:
        return FlatSpec(intent=kwargs["intent"], project_kind="cli")

    async def fake_run_lead(**kwargs: Any) -> LeadResult:
        project_dir = kwargs["project_dir"]
        (project_dir / "csv_to_json.py").write_text(
            "print('csv to json')\n",
            encoding="utf-8",
        )
        log_dir = project_dir / "otto_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "runtime.log").write_text("runtime noise\n", encoding="utf-8")
        set_decomposition(project_dir, ROOT_TASK_ID, "inline")
        set_verdict(project_dir, ROOT_TASK_ID, "pass", cost_usd=0.1)
        return LeadResult(
            task_id=ROOT_TASK_ID,
            verdict="pass",
            cost_usd=0.1,
            decomposition="inline",
            verify_called=True,
            verify_result={"verdict": "pass", "summary": "inline passed"},
        )

    monkeypatch.setattr(v5_runner, "compile_flat_spec", fake_compile_flat_spec)
    monkeypatch.setattr(v5_runner, "run_lead", fake_run_lead)

    result = await run_v5_pipeline(
        project_dir=repo,
        intent="build csv cli",
        config={"default_branch": "main", "provider": "fake"},
        max_parallel=1,
        tree_budget_usd=1.0,
    )

    assert result.verdict == "pass"
    assert (
        _git(repo, "log", "-1", "--format=%s", check=True).stdout.strip()
        == "v5 inline build"
    )
    assert (
        _git(repo, "show", "main:csv_to_json.py", check=True).stdout
        == "print('csv to json')\n"
    )
    assert _git(repo, "ls-tree", "-r", "--name-only", "main", "--", "otto_logs").stdout == ""
