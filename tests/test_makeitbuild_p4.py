from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.mcp_tools import submit_subtask_for_lead
from otto.v5_runner import _root_only_decomposition_enabled, run_v5_pipeline


def _git(project_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=project_dir,
        text=True,
        capture_output=True,
        check=True,
    )


def _init_repo(project_dir: Path) -> None:
    _git(project_dir, "init")
    _git(project_dir, "config", "user.email", "otto@example.invalid")
    _git(project_dir, "config", "user.name", "Otto Test")
    (project_dir / "README.md").write_text("# app\n", encoding="utf-8")
    _git(project_dir, "add", "README.md")
    _git(project_dir, "commit", "-m", "initial")


def test_non_root_submit_subtask_is_rejected_inline_only(tmp_path: Path) -> None:
    payload = submit_subtask_for_lead(
        project_dir=tmp_path,
        session_dir=tmp_path / "session",
        task_id="child",
        intent="split again",
    )

    assert payload["kind"] == "inline_only_at_depth"
    assert payload["decomposition"] == "inline_only"


def test_root_only_decomposition_cap_applies_to_modular_tier() -> None:
    assert _root_only_decomposition_enabled({"v5_tier": "modular"}) is True
    assert _root_only_decomposition_enabled({"v5_tier": "modular", "v5_allow_recursive_decomposition": True}) is False
    assert _root_only_decomposition_enabled({}) is False


@pytest.mark.asyncio
async def test_run_budget_seconds_is_hard_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)

    async def slow_compile(**_kwargs: object) -> object:
        await asyncio.sleep(2.0)
        return SimpleNamespace(behavior_journeys=[], lint_warnings=[])

    monkeypatch.setattr("otto.v5_runner.compile_flat_spec", slow_compile)
    start = time.monotonic()

    result = await run_v5_pipeline(
        project_dir=tmp_path,
        intent="build app",
        config={
            "run_budget_seconds": 0.2,
            "spec_compile_timeout_s": 5,
            "declared_ports": [],
        },
    )

    elapsed = time.monotonic() - start
    assert result.verdict == "merge_blocked"
    assert "run_budget_seconds exceeded during spec_compile" in result.failure_reason
    assert elapsed < 1.5
