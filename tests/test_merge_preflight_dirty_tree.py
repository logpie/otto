"""Regression tests for W5-CRITICAL-1: the merge action preflight ignored
untracked files in the project root and silently merged anyway.

Live W5 rerun (``bench-results/web-as-user/2026-04-26-060606-9cd966/W5/``)
showed the symptom:

  1. Project has untracked ``DIRTY_FILE.txt`` (real user file, not Otto-owned).
  2. Operator clicks Merge from the web UI.
  3. Server returns HTTP 200 with ``"all clean merges, cert skipped per --fast"``.
  4. ``ping.py`` actually lands on main despite the dirty tree.

The bug lived in ``otto.config.repo_preflight_issues`` which only ran
``git diff --quiet`` (tracked files only) and explicitly ignored
untracked state. This test file pins the new contract: untracked
non-Otto files in the project root MUST block the merge action with a
409 + structured ``message``/``dirty_files``, while Otto-owned untracked
files (``.otto-queue*``, ``otto_logs/``, ``.worktrees/``,
``.watcher.log``) MUST NOT block (the W11-CRITICAL-1 invariant).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from otto.config import repo_preflight_issues
from otto.queue.schema import QueueTask, append_task, write_state as write_queue_state
from otto.runs.registry import make_run_record, write_record
from otto.web.app import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "merge@test.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Merge Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)


def _seed_ready_task(repo: Path) -> None:
    """Create a ``done`` queue task with a real branch + worktree so the
    web action surface mounts a legit merge target."""
    subprocess.run(["git", "checkout", "-q", "-b", "build/ready-task"], cwd=repo, check=True)
    (repo / "ready.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "ready.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "ready"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    append_task(
        repo,
        QueueTask(
            id="ready-task",
            command_argv=["build", "ready task"],
            added_at="2026-04-26T00:00:00Z",
            resolved_intent="ready task",
            branch="build/ready-task",
            worktree=".worktrees/ready-task",
        ),
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "ready-task": {
                    "status": "done",
                    "attempt_run_id": "run-ready",
                    "stories_passed": 1,
                    "stories_tested": 1,
                },
            },
        },
    )
    record = make_run_record(
        project_dir=repo,
        run_id="run-ready",
        domain="queue",
        run_type="queue",
        command="build ready task",
        display_name="ready-task",
        status="done",
        cwd=repo,
        identity={"queue_task_id": "ready-task"},
        git={"branch": "build/ready-task"},
        intent={"summary": "ready task"},
        adapter_key="queue.attempt",
    )
    write_record(repo, record)


# ---------------------------------------------------------------------------
# Unit-level: repo_preflight_issues
# ---------------------------------------------------------------------------


def test_preflight_flags_user_untracked_file_in_project_root(tmp_path: Path) -> None:
    """The exact W5 repro: untracked ``DIRTY_FILE.txt`` MUST surface in
    the new ``untracked`` preflight category (which the merge preflight
    consumes; build/improve preflights ignore it by design)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "DIRTY_FILE.txt").write_text("from the W5 bench harness\n", encoding="utf-8")

    issues = repo_preflight_issues(repo)

    assert any("untracked" in msg.lower() for msg in issues["untracked"]), (
        f"untracked user file must produce an 'untracked' issue; got {issues!r}"
    )
    assert "DIRTY_FILE.txt" in issues["dirty_files"]
    # build/improve preflight (``dirty``) tolerates untracked-only state
    # so we don't break test repos with .gitattributes or other
    # uncommitted-but-not-modified files.
    assert issues["dirty"] == []


def test_preflight_clean_when_only_otto_owned_untracked_files(tmp_path: Path) -> None:
    """W11-CRITICAL-1 invariant must hold: Otto's own runtime files
    (queue state, otto_logs/, worktrees/, watcher log) MUST NOT be
    classified as user-dirty by the merge preflight."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".otto-queue-state.json").write_text("{}", encoding="utf-8")
    (repo / ".otto-queue-commands.jsonl").write_text("", encoding="utf-8")
    (repo / "otto_logs").mkdir()
    (repo / "otto_logs" / "session.log").write_text("hi", encoding="utf-8")
    (repo / ".worktrees").mkdir()
    (repo / ".worktrees" / "task-1").mkdir()
    (repo / ".worktrees" / "task-1" / "scratch.txt").write_text("", encoding="utf-8")
    (repo / ".watcher.log").write_text("", encoding="utf-8")
    (repo / ".playwright-cli").mkdir()
    (repo / ".playwright-cli" / "page.yml").write_text("browser artifact\n", encoding="utf-8")
    (repo / "__audit_home_body__.html").write_text("<h1>audit artifact</h1>", encoding="utf-8")

    issues = repo_preflight_issues(repo)

    assert issues["dirty"] == [], (
        f"Otto-owned untracked files should not flag dirty; got {issues['dirty']!r}"
    )
    assert issues["untracked"] == [], (
        f"Otto-owned untracked files should not flag untracked; got {issues['untracked']!r}"
    )
    assert issues["dirty_files"] == []
    assert issues["blocking"] == []


def test_preflight_respects_gitignore_for_user_paths(tmp_path: Path) -> None:
    """Files matched by the project's .gitignore (e.g. node_modules) must
    not surface as user-untracked. Otherwise every project with build
    artifacts would block its first merge."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".gitignore").write_text("ignored_dir/\nbuild_artifact.txt\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "gitignore"], cwd=repo, check=True)
    (repo / "ignored_dir").mkdir()
    (repo / "ignored_dir" / "x.txt").write_text("", encoding="utf-8")
    (repo / "build_artifact.txt").write_text("", encoding="utf-8")

    issues = repo_preflight_issues(repo)

    assert issues["dirty"] == [], f"gitignored files must not flag dirty; got {issues!r}"
    assert issues["untracked"] == [], f"gitignored files must not flag untracked; got {issues!r}"
    assert issues["dirty_files"] == []


def test_preflight_flags_user_untracked_alongside_otto_owned(tmp_path: Path) -> None:
    """Mixed case: Otto-owned files PLUS a real user file. The real one
    must still surface in the new ``untracked`` category; the Otto-owned
    ones must not pollute the list."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".otto-queue-state.json").write_text("{}", encoding="utf-8")
    (repo / "user_notes.md").write_text("", encoding="utf-8")

    issues = repo_preflight_issues(repo)

    assert issues["dirty_files"] == ["user_notes.md"]
    assert any("user_notes.md" in msg for msg in issues["untracked"])
    assert issues["dirty"] == []


def test_preflight_flags_tracked_file_modification(tmp_path: Path) -> None:
    """Sanity: existing behaviour for tracked-file modifications must not
    regress as part of the W5 untracked-detection fix."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("# repo\n\nlocal change\n", encoding="utf-8")

    issues = repo_preflight_issues(repo)

    assert "working tree has unstaged changes" in issues["dirty"]
    assert "README.md" in issues["dirty_files"]


def test_preflight_flags_staged_changes_in_root(tmp_path: Path) -> None:
    """Sanity: ``git add`` of a new file (so it's in the index, not just
    untracked) must still trip the dirty-tree refusal."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "new_file.txt").write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "new_file.txt"], cwd=repo, check=True)

    issues = repo_preflight_issues(repo)

    assert "index has staged but uncommitted changes" in issues["dirty"]
    assert "new_file.txt" in issues["dirty_files"]


# ---------------------------------------------------------------------------
# Service-level: landing payload surfaces dirty-tree blockers
# ---------------------------------------------------------------------------
#
# Phase C.1e deleted the per-run ``POST /api/runs/{id}/actions/merge``
# route; merge is now driven by the global ``/api/actions/merge-all``
# route + the landing payload's ``merge_blocked``/``dirty_files``
# channel. Tests that exercised the deleted per-run route were removed
# in W8-A.


def test_merge_blocks_lists_user_untracked_in_dirty_files(tmp_path: Path) -> None:
    """Verifies the ``dirty_files`` channel (not just the message text)
    surfaces the untracked file. The web client uses ``dirty_files`` /
    ``merge_blockers`` from the landing payload to pre-disable the merge
    button via ``mergeBlockedText`` — same producer, same channel."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_ready_task(repo)
    (repo / "DIRTY_FILE.txt").write_text("hi\n", encoding="utf-8")

    state = TestClient(create_app(repo)).get("/api/state").json()
    landing = state["landing"]

    assert landing["merge_blocked"] is True
    assert "DIRTY_FILE.txt" in landing["dirty_files"], (
        f"untracked file must surface in landing.dirty_files; got "
        f"{landing['dirty_files']!r}"
    )
    assert any("untracked" in msg.lower() for msg in landing["merge_blockers"])
