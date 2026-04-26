"""W3-IMPORTANT-4 regression — improvement-report Stories line splits
PASS / WARN / FAIL instead of conflating WARN observations into the
single PASS ratio.

Live W3 dogfood: the certifier narrative said `PASS (3/5)` (3 truly
passed, 2 were WARN-level observations). The improvement-report said
`Stories: 5/5` because WARN stories carry `passed=True` and the prior
denominator counted everything truthy. Operators reading the report
saw a green 5/5 that contradicted the certifier output one paragraph
earlier.

Fix: split the count by verdict — PASS / WARN / FAIL — so the report
matches what the certifier actually returned.
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


async def _fake_build_pass_with_warns(intent, project_dir, config, **kwargs):
    """Mirrors the live W3 outcome: 2 PASS + 3 WARN + 0 FAIL.

    Note: WARN stories report `passed=True` so they don't tank the run,
    but they are not "met requirements" — they're observations.
    """
    return BuildResult(
        passed=True,
        build_id="run-w3i4",
        rounds=1,
        total_cost=2.34,
        tasks_passed=5,  # legacy aggregate (PASS+WARN)
        tasks_failed=0,
        journeys=[
            {"name": "empty/None handled", "passed": True, "verdict": "PASS"},
            {"name": "all 4 tests pass", "passed": True, "verdict": "PASS"},
            {"name": "whitespace-only edge case", "passed": True, "verdict": "WARN"},
            {"name": "falsy catch overly broad", "passed": True, "verdict": "WARN"},
            {"name": "no type validation", "passed": True, "verdict": "WARN"},
        ],
    )


def test_report_splits_pass_warn_fail_in_stories_line(
    tmp_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_git_repo / "intent.md").write_text("a small product")
    monkeypatch.chdir(tmp_git_repo)

    with patch(
        "otto.cli_improve._create_improve_branch", return_value="improve/2026-04-23-x"
    ), patch("otto.pipeline.build_agentic_v3", side_effect=_fake_build_pass_with_warns):
        result = CliRunner().invoke(
            main, ["improve", "bugs", "edge cases", "--allow-dirty"], catch_exceptions=False,
        )

    assert result.exit_code == 0, result.output

    # Locate improvement-report.md.
    sessions_root = paths.sessions_root(tmp_git_repo)
    reports = list(sessions_root.glob("*/improve/improvement-report.md"))
    assert reports, f"improvement-report.md missing under {sessions_root}"
    report = reports[0].read_text()

    # Must NOT show the misleading "5/5" line.
    assert "Stories:** 5/5" not in report, (
        f"the conflated 5/5 line resurfaced — report says:\n{report}"
    )
    # Must call out the split.
    assert "2 PASS" in report, report
    assert "3 WARN" in report, report
    assert "0 FAIL" in report, report


async def _fake_build_all_pass(intent, project_dir, config, **kwargs):
    return BuildResult(
        passed=True, build_id="run-all-pass", rounds=1, total_cost=1.0,
        tasks_passed=2, tasks_failed=0,
        journeys=[
            {"name": "a", "passed": True, "verdict": "PASS"},
            {"name": "b", "passed": True, "verdict": "PASS"},
        ],
    )


def test_report_split_when_no_warns(tmp_git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The split format applies even when WARN count is zero — keeps the
    report shape stable for downstream parsing."""
    (tmp_git_repo / "intent.md").write_text("a small product")
    monkeypatch.chdir(tmp_git_repo)

    with patch(
        "otto.cli_improve._create_improve_branch", return_value="improve/2026-04-23-y"
    ), patch("otto.pipeline.build_agentic_v3", side_effect=_fake_build_all_pass):
        result = CliRunner().invoke(
            main, ["improve", "bugs", "edge cases", "--allow-dirty"], catch_exceptions=False,
        )

    assert result.exit_code == 0
    sessions_root = paths.sessions_root(tmp_git_repo)
    report = next(sessions_root.glob("*/improve/improvement-report.md")).read_text()
    assert "2 PASS" in report
    assert "0 WARN" in report
    assert "0 FAIL" in report
