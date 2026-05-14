"""Regression: scaffold certification writes a pass verdict from a successful
build command, without invoking the full audit cycle.

Speed fix for the architect phase: the previous run spent ~10 minutes running
Playwright against an empty shell. The architect now uses scaffold
certification — a compile-only check.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from otto.mcp_tools import _run_scaffold_certification


def test_pass_on_zero_exit(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    session = tmp_path / "session"
    (session / "spec").mkdir(parents=True)

    result = asyncio.run(_run_scaffold_certification(
        project_dir=project,
        session_dir=session,
        build_command="true",
        summary="scaffold compiles",
    ))
    assert result.get("_err") is None
    assert result["verdict"] == "pass"
    assert result["scaffold"] is True
    assert result["summary"] == "scaffold compiles"

    # The verifier sentinel file lives where lead.py looks for it.
    written = json.loads((session / "verify" / "verify-result.json").read_text())
    assert written["verdict"] == "pass"


def test_unverified_on_nonzero_exit(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    session = tmp_path / "session"
    (session / "spec").mkdir(parents=True)

    result = asyncio.run(_run_scaffold_certification(
        project_dir=project,
        session_dir=session,
        build_command="false",
        summary="would have been pass",
    ))
    assert result["verdict"] == "unverified"
    assert "scaffold build failed" in result["summary"]


def test_rejects_empty_command(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    session = tmp_path / "session"
    (session / "spec").mkdir(parents=True)

    result = asyncio.run(_run_scaffold_certification(
        project_dir=project,
        session_dir=session,
        build_command="",
        summary="x",
    ))
    assert "_err" in result


def test_uses_worktree_symlink_as_cwd(tmp_path: Path) -> None:
    """When session_dir/worktree exists, the build command runs there, not in project_dir."""
    project = tmp_path / "p"
    project.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "marker.txt").write_text("hi\n")
    session = tmp_path / "session"
    (session / "spec").mkdir(parents=True)
    (session / "worktree").symlink_to(worktree)

    # Command checks for marker.txt in CWD — should pass only if CWD = worktree.
    result = asyncio.run(_run_scaffold_certification(
        project_dir=project,
        session_dir=session,
        build_command="test -f marker.txt && echo ok",
        summary="found",
    ))
    assert result["verdict"] == "pass"
    log = (session / "verify" / "scaffold-build.log").read_text()
    assert "ok" in log
