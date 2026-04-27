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
    assert "queue.yml is malformed" in out


def test_merge_outside_git_repo_shows_clean_error(tmp_path: Path):
    code, out = _run(["merge", "--all", "--no-certify"], cwd=tmp_path)

    assert code == 2
    assert "Not a git repository" in out
    assert "Traceback" not in out


def test_merge_resume_option_is_not_registered(tmp_path: Path):
    repo = init_repo(tmp_path)

    code, out = _run(["merge", "--resume"], cwd=repo)

    assert code == 2
    assert "No such option" in out
    assert "deferred" not in out.lower()


def test_merge_summary_uses_warning_icon_for_merged_with_markers(
    tmp_path: Path,
    monkeypatch,
):
    repo = init_repo(tmp_path)
    seen: dict[str, object] = {}

    async def fake_run_merge(**kwargs):
        seen.update(kwargs)
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
    assert seen["all_done_queue_tasks"] is True
    assert seen["explicit_ids_or_branches"] is None
    assert "⚠ feature/conflict (merged_with_markers)" in out


def test_merge_allow_any_branch_flag_reaches_orchestrator(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path)
    seen: dict[str, object] = {}

    async def fake_run_merge(**kwargs):
        seen["explicit_ids_or_branches"] = kwargs["explicit_ids_or_branches"]
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
    assert seen["explicit_ids_or_branches"] == ["feature/random"]
    assert getattr(seen["options"], "allow_any_branch") is True


def test_merge_transactional_flag_reaches_orchestrator(tmp_path: Path, monkeypatch):
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

    code, out = _run(["merge", "feature/random", "--fast", "--transactional", "--allow-any-branch"], cwd=repo)

    assert code == 0, out
    assert getattr(seen["options"], "fast") is True
    assert getattr(seen["options"], "transactional") is True


def test_merge_verify_policy_reaches_orchestrator(tmp_path: Path, monkeypatch):
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

    code, out = _run(["merge", "--all", "--verify", "full"], cwd=repo)

    assert code == 0, out
    assert getattr(seen["options"], "verification_policy") == "full"
    assert getattr(seen["options"], "full_verify") is True


def test_merge_from_subdirectory_uses_repo_root(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path)
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    seen: dict[str, object] = {}

    async def fake_run_merge(**kwargs):
        seen["project_dir"] = kwargs["project_dir"]
        seen["explicit_ids_or_branches"] = kwargs["explicit_ids_or_branches"]
        return MergeRunResult(
            success=True,
            merge_id="merge-test",
            state=MergeState(merge_id="merge-test", target="main", outcomes=[]),
            note="ok",
        )

    monkeypatch.setattr("otto.merge.orchestrator.run_merge", fake_run_merge)

    code, out = _run(["merge", "feature/random", "--allow-any-branch", "--no-certify"], cwd=nested)

    assert code == 0, out
    assert seen["project_dir"] == repo.resolve()
    assert seen["explicit_ids_or_branches"] == ["feature/random"]


def test_merge_summary_lists_batch_pow_paths(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path)

    async def fake_run_merge(**kwargs):
        return MergeRunResult(
            success=True,
            merge_id="merge-test",
            state=MergeState(merge_id="merge-test", target="main", outcomes=[]),
            source_pow_paths=[
                {
                    "task_id": "add",
                    "branch": "build/add-2026-04-21",
                    "path": "/tmp/add-proof-of-work.html",
                },
                {
                    "task_id": "mul",
                    "branch": "build/mul-2026-04-21",
                    "path": "/tmp/mul-proof-of-work.html",
                },
            ],
            post_merge_pow_path="/tmp/post-merge-proof-of-work.html",
            note="ok",
        )

    monkeypatch.setattr("otto.merge.orchestrator.run_merge", fake_run_merge)

    code, out = _run(["merge", "--all"], cwd=repo)

    assert code == 0
    assert "PoWs from this batch:" in out
    assert "Per-task:" in out
    assert "add (build/add-2026-04-21):  /tmp/add-proof-of-work.html" in out
    assert "mul (build/mul-2026-04-21):  /tmp/mul-proof-of-work.html" in out
    assert "Post-merge: /tmp/post-merge-proof-of-work.html" in out
