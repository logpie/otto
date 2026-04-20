"""Tests for Phase 1.5 — OTTO_INTERNAL_QUEUE_RUNNER env bypass and scoping.

The bypass at cli.py allows the queue runner to spawn child otto processes
in worktree cwd without tripping the venv guard. The key property: the
bypass is ONE-LEVEL-DEEP — after otto.main() accepts it, the env var is
popped so any nested subprocess (Claude SDK, codex subprocess, etc.) does
NOT inherit it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from otto.cli import _check_venv_guard


# ---------- _check_venv_guard pure logic ----------


def test_guard_blocks_when_cwd_is_worktree_and_otto_isnt():
    should_block, msg = _check_venv_guard(
        cwd="/main/.worktrees/foo",
        otto_src="/main/.venv/lib/python/site-packages/otto",
        queue_runner_env=None,
    )
    assert should_block is True
    assert msg is not None
    assert "worktree" in msg
    assert ".venv/bin/otto" in msg


def test_guard_blocks_when_env_var_present_but_not_one():
    should_block, _ = _check_venv_guard(
        cwd="/main/.worktrees/foo",
        otto_src="/main/.venv/lib/python/site-packages/otto",
        queue_runner_env="0",  # any value other than "1" doesn't bypass
    )
    assert should_block is True


def test_guard_bypassed_when_queue_runner_env_is_one():
    should_block, msg = _check_venv_guard(
        cwd="/main/.worktrees/foo",
        otto_src="/main/.venv/lib/python/site-packages/otto",
        queue_runner_env="1",
    )
    assert should_block is False
    assert msg is None


def test_guard_does_not_block_when_otto_in_worktree_too():
    """Normal worktree usage: otto installed in worktree's own venv."""
    should_block, _ = _check_venv_guard(
        cwd="/main/.worktrees/foo",
        otto_src="/main/.worktrees/foo/.venv/lib/python/site-packages/otto",
        queue_runner_env=None,
    )
    assert should_block is False


def test_guard_does_not_block_when_cwd_outside_worktree():
    """Standard usage: cwd is the main project, otto from main's venv."""
    should_block, _ = _check_venv_guard(
        cwd="/main",
        otto_src="/main/.venv/lib/python/site-packages/otto",
        queue_runner_env=None,
    )
    assert should_block is False


# ---------- env scoping (one-level-deep) ----------


def test_env_var_popped_after_main_runs(tmp_path: Path):
    """After otto.cli.main() runs, OTTO_INTERNAL_QUEUE_RUNNER must be gone
    from os.environ so any subprocess otto spawns does NOT inherit it."""
    # Run a small Python script in a fresh subprocess that:
    #   1. sets OTTO_INTERNAL_QUEUE_RUNNER=1
    #   2. imports otto.cli, runs main with a no-op subcommand
    #   3. checks os.environ post-main
    py = str(Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python")
    helper_path = tmp_path / "helper.py"
    helper_path.write_text(
        "import os\n"
        "os.environ['OTTO_INTERNAL_QUEUE_RUNNER'] = '1'\n"
        "import otto.cli\n"
        "try:\n"
        "    otto.cli.main(['history'], standalone_mode=False)\n"
        "except SystemExit:\n"
        "    pass\n"
        "except Exception:\n"
        "    pass\n"
        "print('HAS_VAR=' + str('OTTO_INTERNAL_QUEUE_RUNNER' in os.environ))\n"
    )
    env = dict(os.environ)
    env["OTTO_INTERNAL_QUEUE_RUNNER"] = "1"
    result = subprocess.run(
        [py, str(helper_path)],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
    )
    assert "HAS_VAR=False" in result.stdout, (
        f"Env var was not popped after main() ran.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
