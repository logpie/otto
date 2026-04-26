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
from otto.merge.state import BranchOutcome, MergeState, write_state as write_merge_state
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


def _post_merge(client: TestClient, run_id: str = "run-ready") -> tuple[int, dict]:
    response = client.post(f"/api/runs/{run_id}/actions/merge", json={})
    return response.status_code, response.json()


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
# Service-level: HTTP merge action returns 409 with structured detail
# ---------------------------------------------------------------------------


def test_merge_blocks_when_user_untracked_file_in_root(tmp_path: Path) -> None:
    """Live W5-CRITICAL-1 repro at the HTTP layer: untracked
    ``DIRTY_FILE.txt`` MUST cause ``POST /api/runs/{id}/actions/merge``
    to return 409 with the file in the message + ``dirty_files``."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_ready_task(repo)
    (repo / "DIRTY_FILE.txt").write_text("user work\n", encoding="utf-8")

    status, body = _post_merge(TestClient(create_app(repo)))

    assert status == 409, f"expected dirty-tree 409; got {status} body={body!r}"
    message = str(body.get("message") or "")
    assert "DIRTY_FILE.txt" in message, f"file must be in message; got {message!r}"
    assert "untracked" in message.lower() or "uncommitted" in message.lower(), (
        f"message must explain the dirty-tree block; got {message!r}"
    )


def test_merge_allows_when_only_otto_owned_untracked(tmp_path: Path, monkeypatch) -> None:
    """The merge preflight must not refuse purely on Otto's runtime
    files (W11-CRITICAL-1 invariant). We stub ``execute_action`` so the
    test doesn't actually shell out to the merge orchestrator — the
    contract under test is that the preflight does NOT raise 409."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_ready_task(repo)
    (repo / ".otto-queue-state.json").write_text("{}", encoding="utf-8")
    (repo / ".watcher.log").write_text("", encoding="utf-8")

    captured: list[str] = []
    def _fake_execute(record, kind, project_dir, **kwargs):
        captured.append(kind)
        from otto.mission_control.actions import ActionResult
        return ActionResult(ok=True, message="merged", refresh=True)

    monkeypatch.setattr("otto.mission_control.service.execute_action", _fake_execute)

    status, body = _post_merge(TestClient(create_app(repo)))

    assert status == 200, f"otto-owned-only must not block merge; got {status} body={body!r}"
    assert captured == ["m"], "merge action must reach execute_action"


def test_merge_blocks_when_tracked_file_modified(tmp_path: Path) -> None:
    """Existing behaviour: modifying a committed file blocks the merge."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_ready_task(repo)
    (repo / "README.md").write_text("# repo\n\nlocal edit\n", encoding="utf-8")

    status, body = _post_merge(TestClient(create_app(repo)))

    assert status == 409
    message = str(body.get("message") or "")
    assert "README.md" in message
    assert "unstaged" in message.lower()


def test_merge_blocks_when_staged_changes_in_root(tmp_path: Path) -> None:
    """Existing behaviour: a staged-but-uncommitted new file blocks the
    merge — the regression fix must not loosen this for the
    untracked-detection code path."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_ready_task(repo)
    (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.txt"], cwd=repo, check=True)

    status, body = _post_merge(TestClient(create_app(repo)))

    assert status == 409
    message = str(body.get("message") or "")
    assert "staged.txt" in message
    assert "staged" in message.lower()


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


def test_already_merged_check_still_precedes_dirty_tree_block(tmp_path: Path) -> None:
    """Defensive: if the branch is *already* merged, we still want the
    "Already merged into main" 409 (more actionable than "dirty tree").
    The order in service.execute is: already_merged → SHA freshness →
    dirty-tree. This test pins that ordering survives the W5 fix."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_ready_task(repo)
    (repo / "DIRTY_FILE.txt").write_text("user work\n", encoding="utf-8")
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-already",
            started_at="2026-04-26T00:00:00Z",
            finished_at="2026-04-26T00:01:00Z",
            target="main",
            status="done",
            terminal_outcome="success",
            branches_in_order=["build/ready-task"],
            outcomes=[BranchOutcome(branch="build/ready-task", status="merged")],
        ),
    )

    status, body = _post_merge(TestClient(create_app(repo)))

    # Either 409 is acceptable but the message MUST be the
    # already-merged one — that's the more actionable error and the
    # ordering the existing test_web_merge_action_reports_already_merged_before_dirty_repo
    # test pins for tracked-file dirt.
    assert status == 409
    assert body.get("message") == "Already merged into main."
