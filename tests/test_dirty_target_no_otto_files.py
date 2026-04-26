"""Regression tests for W11-CRITICAL-1: JobDialog must not require the
"I understand this dirty target" acknowledgement when the only "dirty"
state is Otto-owned runtime files (queue state, otto_logs/, .worktrees/,
watcher log) that Otto wrote itself between enqueues.

Two layers of defence:

1. ``otto.web.app._create_managed_project`` writes a ``.gitignore`` that
   covers every Otto runtime artifact (Option A — fix the source).
2. ``otto.mission_control.serializers.serialize_project`` only flags
   ``dirty`` for user-owned changes (Option B — defence-in-depth).

Both layers are exercised here so a future Otto runtime file added
without a corresponding ``.gitignore`` entry does NOT silently re-trigger
the dirty-target trap.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from otto.mission_control.serializers import serialize_project
from otto.setup_gitignore import OTTO_PATTERNS
from otto.web.app import _create_managed_project


def _git(cwd: Path, *args: str) -> str:
    """Run a git command in ``cwd``, return stdout. Raises on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _make_project(tmp_path: Path, name: str = "demo") -> Path:
    root = tmp_path / "managed-projects"
    return _create_managed_project(root, name)


# ---------------------------------------------------------------------------
# Option A — .gitignore source-of-truth coverage
# ---------------------------------------------------------------------------


def test_gitignore_covers_all_otto_runtime_files(tmp_path: Path):
    """The .gitignore written by _create_managed_project must include
    every pattern in setup_gitignore.OTTO_PATTERNS so future runtime
    files Otto adds are covered by a single source of truth."""
    project = _make_project(tmp_path)
    gitignore = (project / ".gitignore").read_text()
    missing = [pat for pat in OTTO_PATTERNS if pat not in gitignore]
    assert not missing, (
        f"_create_managed_project's .gitignore is missing Otto runtime "
        f"patterns: {missing}\n--- gitignore ---\n{gitignore}"
    )


def test_gitignore_keeps_tree_clean_after_otto_runtime_writes(tmp_path: Path):
    """After Otto touches its runtime files, ``git status --porcelain``
    must be empty. This is the closest unit-test equivalent of running
    a full ``otto build`` and observing W1-IMPORTANT-4 / W11-CRITICAL-1."""
    project = _make_project(tmp_path)
    # Simulate what Otto writes at runtime when a queue task is enqueued
    # and the watcher starts.
    runtime_files = [
        ".otto-queue.yml",
        ".otto-queue.yml.lock",
        ".otto-queue.lock",
        ".otto-queue-state.json",
        ".otto-queue-commands.jsonl",
        ".otto-queue-commands.jsonl.processing",
        ".otto-queue-commands.jsonl.lock",
        ".otto-queue-commands.acks.jsonl",
        ".watcher.log",
        "otto_logs/sessions/x.log",
        ".worktrees/task-1/scratch.txt",
    ]
    for rel in runtime_files:
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("runtime")
    porcelain = _git(project, "status", "--porcelain")
    assert porcelain == "", (
        "Otto runtime writes leaked into the working tree — "
        f"git status --porcelain is non-empty:\n{porcelain}"
    )


# ---------------------------------------------------------------------------
# Option B — serialize_project.dirty defence-in-depth
# ---------------------------------------------------------------------------


def test_serialize_project_dirty_false_for_otto_owned_untracked_only(tmp_path: Path):
    """Even if a future Otto runtime file slips past the .gitignore,
    serialize_project must not mark the project dirty. This is the
    promise the JobDialog dirty-target preflight relies on."""
    project = _make_project(tmp_path)
    # Replace .gitignore with one that does NOT cover Otto runtime
    # patterns, then commit so the user-empty .gitignore is the
    # baseline. This simulates "future Otto runtime file Otto added
    # without updating .gitignore" — defence-in-depth scenario.
    (project / ".gitignore").write_text("# baseline — no otto patterns\n")
    _git(project, "add", ".gitignore")
    _git(project, "commit", "-q", "-m", "test baseline gitignore")
    # Now write Otto runtime files that should be exempted.
    (project / ".otto-queue-state.json").write_text("{}")
    (project / "otto_logs").mkdir(exist_ok=True)
    (project / "otto_logs" / "narrative.log").write_text("hi")
    (project / ".worktrees").mkdir(exist_ok=True)
    (project / ".worktrees" / "x.txt").write_text("scratch")
    (project / ".watcher.log").write_text("…")
    porcelain = _git(project, "status", "--porcelain")
    # Sanity: only untracked entries, all of them Otto-owned.
    assert porcelain.strip(), "test setup error — files should be untracked"
    for line in porcelain.splitlines():
        assert line.startswith("?? "), f"unexpected non-untracked entry: {line!r}"
    project_payload = serialize_project(project)
    assert project_payload["dirty"] is False, (
        f"Otto-owned untracked files should not flag dirty.\n"
        f"git status:\n{porcelain}\nserialized: {project_payload}"
    )


def test_serialize_project_dirty_true_for_user_untracked_files(tmp_path: Path):
    """Sanity check: an actual user-created untracked file MUST still
    flag the project dirty — only Otto-owned paths get the exemption."""
    project = _make_project(tmp_path)
    (project / "user_notes.txt").write_text("my work")
    payload = serialize_project(project)
    assert payload["dirty"] is True


def test_serialize_project_dirty_true_for_user_modifications(tmp_path: Path):
    """Sanity check: user-modified tracked file flags the project dirty
    even if Otto runtime files are also present."""
    project = _make_project(tmp_path)
    readme = project / "README.md"
    readme.write_text(readme.read_text() + "\n\nuser edit\n")
    # Otto-owned untracked file present alongside, must not mask the user edit.
    (project / ".otto-queue-state.json").write_text("{}")
    payload = serialize_project(project)
    assert payload["dirty"] is True


# ---------------------------------------------------------------------------
# End-to-end (Option A + B): the original W11-CRITICAL-1 user flow
# ---------------------------------------------------------------------------


def test_enqueue_after_first_does_not_trip_dirty_preflight_due_to_otto_runtime_files(
    tmp_path: Path,
):
    """Reproduce W11-CRITICAL-1 at the service layer: after a managed
    project is created and Otto enqueues two queue tasks, the project
    must not be marked dirty (because Otto's ``.otto-queue*`` runtime
    files are gitignored), so the JobDialog never asks the user to
    acknowledge a dirtiness Otto created itself."""
    pytest.importorskip("yaml")  # enqueue depends on PyYAML
    from otto.queue.enqueue import enqueue_task

    project = _make_project(tmp_path)
    # First enqueue: triggers first_touch_bookkeeping; Otto creates
    # .otto-queue.yml, .otto-queue.lock, etc.
    enqueue_task(
        project,
        command="build",
        raw_args=[],
        intent="first task",
        after=[],
        explicit_as=None,
        resumable=True,
    )
    # Second enqueue: at this point Otto's own runtime files are present
    # in the working tree. The dirty-target preflight is what JobDialog
    # consults via serialize_project — it must not fire.
    enqueue_task(
        project,
        command="build",
        raw_args=[],
        intent="second task",
        after=[],
        explicit_as=None,
        resumable=True,
    )
    payload = serialize_project(project)
    porcelain = _git(project, "status", "--porcelain")
    assert payload["dirty"] is False, (
        "JobDialog would require the dirty-target acknowledgement after "
        "two clean enqueues — the W11-CRITICAL-1 trap.\n"
        f"git status --porcelain:\n{porcelain}\n"
        f"serialized project: {payload}"
    )
