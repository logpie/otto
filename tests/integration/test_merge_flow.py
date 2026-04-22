from __future__ import annotations

from pathlib import Path

import pytest

from otto.merge import git_ops
from otto.merge.orchestrator import MergeOptions, run_merge

from .conftest import (
    assistant_text,
    assistant_tool_result,
    assistant_tool_use,
    commit_all,
    git,
    result_message,
)


@pytest.mark.asyncio
async def test_run_merge_resolves_real_git_conflict_without_conflict_agent_kwarg_crash(
    tmp_otto_repo: Path,
    mock_sdk,
) -> None:
    conflict_file = tmp_otto_repo / "shared.txt"
    conflict_file.write_text("base\n")
    commit_all(tmp_otto_repo, "add shared file")

    git(tmp_otto_repo, "checkout", "-b", "feat-a")
    conflict_file.write_text("left branch\n")
    commit_all(tmp_otto_repo, "left change")

    git(tmp_otto_repo, "checkout", "main")
    git(tmp_otto_repo, "checkout", "-b", "feat-b")
    conflict_file.write_text("right branch\n")
    commit_all(tmp_otto_repo, "right change")
    git(tmp_otto_repo, "checkout", "main")

    async def resolve_conflict(*, prompt: str, options):
        project_dir = Path(options.cwd)
        conflicted = project_dir / "shared.txt"
        assert "<<<<<<<" in conflicted.read_text()
        conflicted.write_text("resolved from feat-a and feat-b\n")
        yield assistant_tool_use("Read", {"file_path": "shared.txt"}, tool_id="read-1")
        yield assistant_tool_use(
            "Edit",
            {
                "file_path": "shared.txt",
                "new_string": "resolved from feat-a and feat-b\n",
            },
            tool_id="edit-1",
        )
        yield assistant_tool_result("Updated shared.txt to the merged content.", tool_id="edit-1")
        yield assistant_text("Resolved the accumulated conflict and left git history untouched.")
        yield result_message(total_cost_usd=0.19)

    mock_sdk.install(resolve_conflict)

    result = await run_merge(
        project_dir=tmp_otto_repo,
        config={
            "provider": "claude",
            "default_branch": "main",
            "queue": {"bookkeeping_files": []},
        },
        options=MergeOptions(
            target="main",
            no_certify=True,
            allow_any_branch=True,
        ),
        explicit_ids_or_branches=["feat-a", "feat-b"],
    )

    assert result.success is True, result.note
    assert "TypeError" not in result.note
    assert conflict_file.read_text() == "resolved from feat-a and feat-b\n"
    assert git_ops.merge_in_progress(tmp_otto_repo) is False
