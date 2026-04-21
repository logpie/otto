"""Tests for otto/cli_merge.py."""

from __future__ import annotations

import os
from pathlib import Path

from click.testing import CliRunner

from otto.cli import main
from otto.merge.orchestrator import MergeRunResult
from otto.merge.state import BranchOutcome, MergeState
from tests._helpers import init_repo


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
    repo = init_repo(tmp_path)
    (repo / ".otto-queue.yml").write_text("schema_version: [\n")

    code, out = _run(["merge", "--all", "--no-certify"], cwd=repo)

    assert code == 2
    assert "no branches to merge" in out


def test_merge_summary_uses_warning_icon_for_merged_with_markers(
    tmp_path: Path,
    monkeypatch,
):
    repo = init_repo(tmp_path)

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


def test_merge_allow_any_branch_flag_reaches_orchestrator(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path)
    seen: dict[str, object] = {}

    async def fake_run_merge(**kwargs):
        seen["options"] = kwargs["options"]
        return MergeRunResult(
            success=True,
            merge_id="merge-test",
            state=MergeState(merge_id="merge-test", target="main", outcomes=[]),
            note="ok",
        )

    monkeypatch.setattr("otto.merge.orchestrator.run_merge", fake_run_merge)

    code, _out = _run(["merge", "feature/random", "--allow-any-branch", "--no-certify"], cwd=repo)

    assert code == 0
    assert getattr(seen["options"], "allow_any_branch") is True
