"""W3-IMPORTANT-2 regression — improve always writes build-journal.md,
even when the agent-driven loop returns PASS on the first round.

In the live W3 dogfood, the improve session passed certify on round 1.
The session left an `improvement-report.md` behind but no
`build-journal.md` anywhere under `improve/`. CLAUDE.md documents
`improve/build-journal.md` as the round-by-round index. The agent-driven
build_agentic_v3 path never calls append_journal at all, and the
system-driven run_certify_fix_loop only journals on round transitions —
neither writes a journal when the loop bails on first-round PASS.

Fix: cli_improve writes a final journal row unconditionally after the
improve loop completes. Single-round → 1 entry; multi-round → N entries
already from the inner loop, plus this final marker.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from otto import paths
from otto.cli import main
from otto.pipeline import BuildResult


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """Plain git repo with otto.yaml — local copy of conftest fixture."""
    from otto.config import create_config

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@e.com"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=tmp_path, check=True,
    )
    create_config(tmp_path)
    subprocess.run(["git", "add", "otto.yaml"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "config"],
        cwd=tmp_path, capture_output=True, check=True,
    )
    return tmp_path


async def _fake_build_pass(intent, project_dir, config, **kwargs):
    """Stand-in for build_agentic_v3 that returns PASS in one round —
    the case that exposed W3-IMPORTANT-2 in the live W3 run."""
    return BuildResult(
        passed=True,
        build_id="run-w3i2",
        rounds=1,
        total_cost=1.23,
        tasks_passed=2,
        tasks_failed=0,
        journeys=[
            {"name": "story-1", "passed": True, "verdict": "PASS"},
            {"name": "story-2", "passed": True, "verdict": "PASS"},
        ],
    )


def test_improve_writes_build_journal_when_single_round_pass(
    tmp_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First-round PASS still leaves a journal under improve/."""
    (tmp_git_repo / "intent.md").write_text("a small product")
    monkeypatch.chdir(tmp_git_repo)

    with patch(
        "otto.cli_improve._create_improve_branch", return_value="improve/2026-04-23-abc"
    ), patch("otto.pipeline.build_agentic_v3", side_effect=_fake_build_pass):
        result = CliRunner().invoke(
            main,
            ["improve", "bugs", "edge cases", "--allow-dirty"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0, result.output

    # Locate the journal under any session that has an improve/ dir.
    sessions_root = paths.sessions_root(tmp_git_repo)
    journal_paths = list(sessions_root.glob("*/improve/build-journal.md"))
    assert journal_paths, (
        "expected build-journal.md under improve/ but found none. "
        f"sessions={list(sessions_root.glob('*/improve/*'))}"
    )

    # The journal should carry the table header + at least one row.
    journal_text = journal_paths[0].read_text()
    assert "# Build Journal" in journal_text, journal_text
    assert "| # | Time | Action | Result | Cost |" in journal_text
    # At least one entry below the header (the row marker uses "PASS" /
    # "FAIL"; we wrote PASS in fake_build_pass).
    body_rows = [
        line for line in journal_text.splitlines()
        if line.startswith("|") and "PASS" in line
    ]
    assert body_rows, f"expected at least one PASS row in journal: {journal_text}"


async def _fake_build_fail(intent, project_dir, config, **kwargs):
    return BuildResult(
        passed=False,
        build_id="run-w3i2-fail",
        rounds=2,
        total_cost=4.56,
        tasks_passed=1,
        tasks_failed=1,
        journeys=[
            {"name": "story-1", "passed": True, "verdict": "PASS"},
            {"name": "story-2", "passed": False, "verdict": "FAIL"},
        ],
    )


def test_improve_writes_build_journal_on_fail(
    tmp_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAIL outcomes also write a journal — the journal is the partial-
    progress handoff for debugging."""
    (tmp_git_repo / "intent.md").write_text("a small product")
    monkeypatch.chdir(tmp_git_repo)

    with patch(
        "otto.cli_improve._create_improve_branch", return_value="improve/2026-04-23-def"
    ), patch("otto.pipeline.build_agentic_v3", side_effect=_fake_build_fail):
        result = CliRunner().invoke(
            main,
            ["improve", "bugs", "edge cases", "--allow-dirty"],
            catch_exceptions=False,
        )

    # Result code is 1 on fail but the journal must still exist.
    assert result.exit_code in (0, 1), result.output

    sessions_root = paths.sessions_root(tmp_git_repo)
    journal_paths = list(sessions_root.glob("*/improve/build-journal.md"))
    assert journal_paths, "build-journal.md missing on FAIL outcome"
    text = journal_paths[0].read_text()
    assert "FAIL" in text, text
