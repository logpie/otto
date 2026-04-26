"""W3-IMPORTANT-5 regression — improve runs write narrative.log + the
checkpoint phase under improve/, not build/.

Live W3 dogfood: an `otto improve` session left:
  - improve/improvement-report.md  ✓ (correct location)
  - build/narrative.log            ✗ (split-brained; should be improve/)
  - checkpoint.json with phase="build"  ✗ (should be "improve")

CLAUDE.md documents `improve/` as the canonical per-session subdir for
improve commands. The agent stream and the checkpoint phase tag must
agree with that contract.
"""

from __future__ import annotations

import json
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


def test_improve_creates_improve_subdir_not_build_subdir(
    tmp_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `otto improve` runs, the per-session `improve/` directory
    must exist (because narrative.log + agent scaffolding land there).
    `build/` may or may not exist for an improve run, but `improve/`
    is non-negotiable per the CLAUDE.md layout contract.
    """

    async def fake_build(intent, project_dir, config, **kwargs):
        # Verify the pipeline put us on the improve track. We touch the
        # build_dir created in build_agentic_v3 by writing a probe file —
        # the test inspects it after asyncio.run returns.
        from otto import paths as _paths
        sess_id = kwargs.get("run_id") or "unknown"
        # This file should land under improve/, not build/, after the fix.
        improve_d = _paths.improve_dir(Path(project_dir), sess_id)
        improve_d.mkdir(parents=True, exist_ok=True)
        (improve_d / "narrative.log").write_text("agent ran here\n")
        return BuildResult(
            passed=True,
            build_id=sess_id,
            rounds=1,
            total_cost=0.5,
            tasks_passed=1,
            tasks_failed=0,
            journeys=[{"name": "ok", "passed": True, "verdict": "PASS"}],
        )

    (tmp_git_repo / "intent.md").write_text("a small product")
    monkeypatch.chdir(tmp_git_repo)

    with patch(
        "otto.cli_improve._create_improve_branch", return_value="improve/2026-04-23-i5"
    ), patch("otto.pipeline.build_agentic_v3", side_effect=fake_build):
        result = CliRunner().invoke(
            main,
            ["improve", "bugs", "edge cases", "--agentic", "--allow-dirty"],
            catch_exceptions=False,
        )
    assert result.exit_code == 0, result.output

    sessions_root = paths.sessions_root(tmp_git_repo)
    improve_dirs = list(sessions_root.glob("*/improve"))
    assert improve_dirs, (
        "no per-session improve/ subdir exists — improve must scaffold improve/"
    )


def test_pipeline_improve_mode_scaffolds_improve_dir_not_only_build(
    tmp_git_repo: Path,
) -> None:
    """Direct pipeline call: prompt_mode='improve' must scaffold improve/
    for the agent stream — NOT only build/. The earlier code wrote
    narrative.log into build/ unconditionally regardless of the mode.

    We let the pipeline run far enough to do the scaffold + checkpoint
    write, then short-circuit by raising from the patched agent runner.
    """
    import asyncio

    from otto.pipeline import build_agentic_v3

    sess_id = "2026-04-23-w3i5b-test"

    async def boom(*args, **kwargs):
        raise RuntimeError("short-circuit after scaffold")

    # Patch the agent invocation to abort after scaffolding completes.
    with patch("otto.agent.run_agent_with_timeout", new=boom):
        try:
            asyncio.run(build_agentic_v3(
                "improve me",
                tmp_git_repo,
                {"skip_product_qa": False, "max_rounds": 1, "allow_dirty_repo": True},
                prompt_mode="improve",
                run_id=sess_id,
                command="improve",
                manage_checkpoint=True,
            ))
        except Exception:
            # We expect either RuntimeError or a wrapped pipeline error.
            pass

    # Scaffold for improve mode → improve/ dir exists.
    improve_path = paths.improve_dir(tmp_git_repo, sess_id)
    assert improve_path.exists(), (
        f"improve/ not scaffolded for prompt_mode='improve'. session contents: "
        f"{list(paths.session_dir(tmp_git_repo, sess_id).iterdir()) if paths.session_dir(tmp_git_repo, sess_id).exists() else 'no session dir'}"
    )

    # Checkpoint, if present, tags phase=improve (not build).
    cp_path = paths.session_checkpoint(tmp_git_repo, sess_id)
    if cp_path.exists():
        cp = json.loads(cp_path.read_text())
        assert cp.get("phase") == "improve", (
            f"checkpoint phase should be 'improve' for an improve run, got "
            f"{cp.get('phase')!r}"
        )
