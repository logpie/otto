"""Regression tests for W3-CRITICAL-1.

Live finding (``docs/mc-audit/live-findings.md``): the web JobDialog's
"improve" submission silently forks from ``main`` instead of iterating on
the just-completed build's branch. The improve worktree starts empty, the
agent re-creates files from scratch, and the resulting improve branch
collides with the build branch on the same files at merge time.

Fix: queue tasks now carry an optional ``base_ref`` snapshot which the
runner passes to ``git worktree add -b <branch> <wt> <base_ref>``. The
mission-control web layer fills ``base_ref`` from a ``prior_run_id``
posted by JobDialog. CLI ``otto queue improve …`` keeps the legacy
"branch from HEAD" behaviour unless a base ref is provided.

This file covers:

- ``test_improve_worktree_contains_prior_build_files`` — end-to-end:
  build commits a file, improve enqueued with ``prior_run_id`` produces a
  worktree containing that file BEFORE the agent runs.

- ``test_improve_branch_history_includes_prior_build_commits`` — the new
  improve branch's ``git log`` lists the prior build's commit as an
  ancestor (no parallel forks).

- ``test_improve_merge_does_not_collide_with_prior_build`` — merging
  the build branch then the improve branch into ``main`` doesn't conflict.

- ``test_improve_without_prior_run_id_falls_back_to_main`` — backwards
  compat: no ``prior_run_id`` ⇒ today's behaviour (worktree is empty).

- ``test_enqueue_improve_resolves_prior_run_branch_from_history`` — the
  Mission Control service enqueue path looks up the branch from history
  records when the operator posts a ``prior_run_id`` for a previously-
  completed run.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from otto.queue.enqueue import enqueue_task
from otto.queue.schema import load_queue
from otto.worktree import add_worktree
from tests._helpers import init_repo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=check)


def _commit_file_on_branch(
    repo: Path, *, branch: str, file_path: str, content: str, message: str
) -> str:
    """Create a branch (if missing), check it out, write a file, commit. Returns commit SHA."""
    result = _run_git(repo, "rev-parse", "--verify", branch, check=False)
    if result.returncode != 0:
        _run_git(repo, "checkout", "-b", branch)
    else:
        _run_git(repo, "checkout", branch)
    (repo / file_path).write_text(content, encoding="utf-8")
    _run_git(repo, "add", file_path)
    _run_git(repo, "commit", "-q", "-m", message)
    sha = _run_git(repo, "rev-parse", "HEAD").stdout.strip()
    # Park the project back on main so the worktree-from-branch test
    # exercises the "branch is unchecked-out elsewhere" flow.
    _run_git(repo, "checkout", "main")
    return sha


# ---------------------------------------------------------------------------
# Worktree-level tests (proving the base_ref plumbing works)
# ---------------------------------------------------------------------------


def test_improve_worktree_contains_prior_build_files(tmp_path: Path) -> None:
    """A worktree created with base_ref=<prior build branch> must inherit its files."""
    repo = init_repo(tmp_path)
    build_branch = "build/greet-2026-04-25"
    _commit_file_on_branch(
        repo,
        branch=build_branch,
        file_path="greet.py",
        content='def greet(name):\n    return f"Hello, {name}!"\n',
        message="feat: add greet",
    )

    wt_path = repo / ".worktrees" / "improve-greet"
    add_worktree(
        project_dir=repo,
        worktree_path=wt_path,
        branch="improve/greet-2026-04-25",
        base_ref=build_branch,
    )

    # The worktree must contain greet.py BEFORE any agent runs — otherwise
    # the agent re-creates the file from scratch and we collide at merge.
    assert (wt_path / "greet.py").exists(), (
        "improve worktree missing prior build's greet.py — base_ref didn't seed history"
    )
    assert (wt_path / "greet.py").read_text() == 'def greet(name):\n    return f"Hello, {name}!"\n'


def test_improve_branch_history_includes_prior_build_commits(tmp_path: Path) -> None:
    """git log on the improve branch must include the prior build's commit."""
    repo = init_repo(tmp_path)
    build_branch = "build/greet-2026-04-25"
    build_sha = _commit_file_on_branch(
        repo,
        branch=build_branch,
        file_path="greet.py",
        content="x",
        message="feat: greet",
    )

    wt_path = repo / ".worktrees" / "improve-greet"
    improve_branch = "improve/greet-2026-04-25"
    add_worktree(
        project_dir=repo,
        worktree_path=wt_path,
        branch=improve_branch,
        base_ref=build_branch,
    )

    log = _run_git(wt_path, "log", "--format=%H", improve_branch).stdout.strip().splitlines()
    assert build_sha in log, (
        f"improve branch's history does not contain prior build commit {build_sha}; "
        f"got log={log!r}"
    )


def test_improve_merge_does_not_collide_with_prior_build(tmp_path: Path) -> None:
    """build + improve should land on main without conflict."""
    repo = init_repo(tmp_path)
    build_branch = "build/greet-2026-04-25"
    _commit_file_on_branch(
        repo,
        branch=build_branch,
        file_path="greet.py",
        content='def greet(name):\n    return f"Hello, {name}!"\n',
        message="feat: greet",
    )

    wt_path = repo / ".worktrees" / "improve-greet"
    improve_branch = "improve/greet-2026-04-25"
    add_worktree(
        project_dir=repo,
        worktree_path=wt_path,
        branch=improve_branch,
        base_ref=build_branch,
    )

    # Simulate the improve agent making an edit on top of the build commit.
    (wt_path / "greet.py").write_text(
        'def greet(name):\n    if not name:\n        return "Hello, world!"\n    return f"Hello, {name}!"\n',
        encoding="utf-8",
    )
    _run_git(wt_path, "add", "greet.py")
    _run_git(wt_path, "commit", "-q", "-m", "improve: handle empty name")

    # Merge build first.
    merge_build = _run_git(repo, "merge", "--no-edit", build_branch, check=False)
    assert merge_build.returncode == 0, (
        f"merge of build branch failed: {merge_build.stderr or merge_build.stdout}"
    )

    # Then merge improve. Without the fix this conflicts on greet.py
    # because the two branches are parallel forks of main.
    merge_improve = _run_git(repo, "merge", "--no-edit", improve_branch, check=False)
    assert merge_improve.returncode == 0, (
        f"merge of improve branch onto main collided — base_ref didn't link the two:\n"
        f"stdout={merge_improve.stdout!r}\nstderr={merge_improve.stderr!r}"
    )
    # And greet.py contains the improve agent's edit.
    assert "Hello, world!" in (repo / "greet.py").read_text()


def test_improve_without_prior_run_id_falls_back_to_main(tmp_path: Path) -> None:
    """Backwards compat: omitting base_ref keeps today's "fork from HEAD" behaviour.

    This isn't a desirable outcome (it's exactly W3-CRITICAL-1) but the
    backwards-compat guarantee matters for projects that have no prior
    runs to point at — the improve queue path must still work and create
    a worktree, just one that's empty / matches HEAD.
    """
    repo = init_repo(tmp_path)
    # Park a build branch with greet.py that we *don't* reference.
    _commit_file_on_branch(
        repo,
        branch="build/greet-2026-04-25",
        file_path="greet.py",
        content="x",
        message="feat: greet",
    )

    wt_path = repo / ".worktrees" / "improve-no-base"
    add_worktree(
        project_dir=repo,
        worktree_path=wt_path,
        branch="improve/no-base-2026-04-25",
        base_ref=None,
    )

    # Worktree must NOT contain greet.py because it was forked from main,
    # and main never saw the build commit. This documents the fallback.
    assert not (wt_path / "greet.py").exists(), (
        "expected the no-base-ref worktree to fork from main and miss greet.py — "
        "this proves the regression: improve from main doesn't see build's files"
    )


# ---------------------------------------------------------------------------
# Enqueue-level test (queue task carries the snapshot)
# ---------------------------------------------------------------------------


def test_enqueue_task_persists_base_ref(tmp_path: Path) -> None:
    """enqueue_task with base_ref writes it into queue.yml so the runner sees it."""
    repo = init_repo(tmp_path)
    result = enqueue_task(
        repo,
        command="improve",
        raw_args=["bugs", "tighten error handling"],
        intent="# project",
        after=[],
        explicit_as=None,
        resumable=True,
        focus="tighten error handling",
        base_ref="build/foo-2026-04-25",
    )
    assert result.task.base_ref == "build/foo-2026-04-25"

    # Round-trip via queue.yml — the runner reads it that way.
    queue = load_queue(repo)
    assert len(queue) == 1
    assert queue[0].base_ref == "build/foo-2026-04-25"


# ---------------------------------------------------------------------------
# MissionControlService enqueue test (the web's POST handler)
# ---------------------------------------------------------------------------


def test_mc_enqueue_improve_with_prior_run_id_resolves_branch(tmp_path: Path) -> None:
    """Enqueue improve via the service with prior_run_id — base_ref is set from history."""
    from otto.mission_control.service import MissionControlService
    from otto.runs.history import append_history_snapshot

    repo = init_repo(tmp_path)
    # intent.md so resolve_intent_for_enqueue doesn't blow up.
    (repo / "intent.md").write_text("# greet\n", encoding="utf-8")

    # Seed a history snapshot with branch + run_id so the service can
    # resolve `prior_run_id` to a branch.
    prior_run_id = "2026-04-25-120000-abcdef"
    append_history_snapshot(
        repo,
        {
            "history_kind": "terminal_snapshot",
            "run_id": prior_run_id,
            "build_id": prior_run_id,
            "command": "build",
            "domain": "queue",
            "run_type": "queue",
            "status": "success",
            "terminal_outcome": "success",
            "branch": "build/greet-2026-04-25",
            "git": {"branch": "build/greet-2026-04-25"},
            "summary": "build greet",
            "intent": "build greet",
            "duration_s": 30.0,
            "cost_usd": 0.05,
            "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )

    svc = MissionControlService(repo)
    response = svc.enqueue(
        "improve",
        {
            "subcommand": "bugs",
            "focus": "edge cases",
            "prior_run_id": prior_run_id,
        },
    )
    assert response["ok"] is True
    queue = load_queue(repo)
    assert len(queue) == 1
    assert queue[0].base_ref == "build/greet-2026-04-25", (
        f"service did not snapshot base_ref from history; got task={queue[0]!r}"
    )


def test_mc_enqueue_improve_without_prior_run_id_leaves_base_ref_unset(tmp_path: Path) -> None:
    """Backwards compat: improve without prior_run_id keeps base_ref None."""
    from otto.mission_control.service import MissionControlService

    repo = init_repo(tmp_path)
    (repo / "intent.md").write_text("# greet\n", encoding="utf-8")

    svc = MissionControlService(repo)
    response = svc.enqueue("improve", {"subcommand": "bugs", "focus": "x"})
    assert response["ok"] is True
    queue = load_queue(repo)
    assert queue[0].base_ref is None


def test_mc_enqueue_improve_with_unknown_prior_run_id_raises(tmp_path: Path) -> None:
    """Operator typos / stale ids must surface as a 4xx, not silently fall back to main."""
    from otto.mission_control.service import MissionControlService, MissionControlServiceError

    repo = init_repo(tmp_path)
    (repo / "intent.md").write_text("# greet\n", encoding="utf-8")
    svc = MissionControlService(repo)

    with pytest.raises(MissionControlServiceError) as exc_info:
        svc.enqueue("improve", {"subcommand": "bugs", "focus": "x", "prior_run_id": "no-such-run"})
    assert exc_info.value.status_code in (400, 404)
    # Nothing got enqueued.
    assert load_queue(repo) == []


# ---------------------------------------------------------------------------
# add_worktree edge cases (defensive)
# ---------------------------------------------------------------------------


def test_add_worktree_existing_branch_still_works_with_base_ref(tmp_path: Path) -> None:
    """If the target branch already exists, base_ref is silently ignored.

    Re-pointing an existing branch ref via ``git worktree add`` would be
    surprising. We keep the legacy "checkout existing branch" path.
    """
    repo = init_repo(tmp_path)
    build_branch = "build/greet-2026-04-25"
    _commit_file_on_branch(
        repo,
        branch=build_branch,
        file_path="greet.py",
        content="from build",
        message="feat: greet",
    )
    # Pre-create the improve branch from main (simulating a same-day re-run).
    _run_git(repo, "branch", "improve/greet-2026-04-25", "main")

    wt_path = repo / ".worktrees" / "improve-existing"
    add_worktree(
        project_dir=repo,
        worktree_path=wt_path,
        branch="improve/greet-2026-04-25",
        base_ref=build_branch,
    )
    # The pre-existing branch was rooted on main, so greet.py shouldn't be
    # there — proving we did NOT silently re-point the ref.
    assert not (wt_path / "greet.py").exists()
