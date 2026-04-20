"""Tests for otto/cli_merge.py."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from click.testing import CliRunner

from otto.cli import main
from otto.merge.orchestrator import MergeRunResult
from otto.merge.state import BranchOutcome, MergeState


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=repo, check=True)
    return repo


def _run(args: list[str], *, cwd: Path) -> tuple[int, str]:
    runner = CliRunner()
    saved_cwd = os.getcwd()
    os.chdir(cwd)
    try:
        result = runner.invoke(main, args, catch_exceptions=False)
    finally:
        os.chdir(saved_cwd)
    return result.exit_code, result.output


def test_merge_all_with_malformed_queue_yml_returns_cli_error(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / ".otto-queue.yml").write_text("schema_version: [\n")

    code, out = _run(["merge", "--all", "--no-certify"], cwd=repo)

    assert code == 2
    assert "no branches to merge" in out


def test_merge_summary_uses_warning_icon_for_merged_with_markers(
    tmp_path: Path,
    monkeypatch,
):
    repo = _init_repo(tmp_path)

    async def fake_run_merge(**kwargs):
        return MergeRunResult(
            success=True,
            merge_id="merge-test",
            state=MergeState(
                merge_id="merge-test",
                target="main",
                outcomes=[
                    BranchOutcome(
                        branch="feature/conflict",
                        status="merged_with_markers",
                        note="markers retained for consolidated resolution",
                    ),
                ],
            ),
            note="ok",
        )

    monkeypatch.setattr("otto.merge.orchestrator.run_merge", fake_run_merge)

    code, out = _run(["merge", "--all", "--no-certify"], cwd=repo)

    assert code == 0
    assert "⚠ feature/conflict (merged_with_markers)" in out
