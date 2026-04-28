from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from otto import paths
from otto.merge.state import BranchOutcome, MergeState, write_state as write_merge_state
from otto.mission_control.actions import ActionResult
from otto.queue.schema import QueueTask, append_task, write_state as write_queue_state
from otto.runs.history import append_history_snapshot, build_terminal_snapshot
from otto.runs.registry import make_run_record, write_record

from tests._web_mc_helpers import (
    _append_queue_task,
    _client,
    _create_branch_file,
    _init_repo,
    _set_origin_head,
    _write_empty_queue_state,
)


def test_web_state_exposes_landing_queue_status(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _create_branch_file(repo, "build/ready-task", "ready.txt")
    _append_queue_task(repo, "ready-task", command_argv=["build", "ready task"], resolved_intent="ready task")
    _append_queue_task(repo, "merged-task", command_argv=["build", "merged task"], resolved_intent="merged task")
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "ready-task": {
                    "status": "done",
                    "attempt_run_id": "run-ready",
                    "stories_passed": 2,
                    "stories_tested": 2,
                },
                "merged-task": {
                    "status": "done",
                    "attempt_run_id": "run-merged",
                    "stories_passed": 1,
                    "stories_tested": 1,
                },
            },
        },
    )
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-merged",
            started_at="2026-04-24T00:00:00Z",
            finished_at="2026-04-24T00:01:00Z",
            target="main",
            status="done",
            terminal_outcome="success",
            branches_in_order=["build/merged-task"],
            outcomes=[BranchOutcome(branch="build/merged-task", status="merged")],
        ),
    )

    state = _client(repo).get("/api/state").json()

    assert state["landing"]["counts"] == {"ready": 1, "merged": 1, "blocked": 0, "reviewed": 0, "total": 2}
    by_id = {item["task_id"]: item for item in state["landing"]["items"]}
    assert by_id["ready-task"]["landing_state"] == "ready"
    assert by_id["ready-task"]["label"] == "Ready to land"
    assert by_id["ready-task"]["run_id"] == "run-ready"
    assert by_id["ready-task"]["stories_passed"] == 2
    assert by_id["merged-task"]["landing_state"] == "merged"
    assert by_id["merged-task"]["label"] == "Landed"
    assert by_id["merged-task"]["merge_id"] == "merge-merged"

    detail = _client(repo).get("/api/runs/run-merged").json()
    actions = {action["key"]: action for action in detail["legal_actions"]}
    assert detail["landing_state"] == "merged"
    assert detail["review_packet"]["headline"] == "Already merged into main"
    assert detail["review_packet"]["readiness"]["state"] == "merged"
    assert detail["review_packet"]["checks"][-1]["detail"] == "Task is already landed."
    assert detail["review_packet"]["next_action"]["enabled"] is False
    assert actions["m"]["enabled"] is False
    assert actions["m"]["reason"] == "Already merged into main."

def test_web_landed_task_uses_merge_state_diff_after_source_branch_deleted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "checkout", "-q", "-b", "build/merged-task"], cwd=repo, check=True)
    (repo / "merged.txt").write_text("merged\n", encoding="utf-8")
    subprocess.run(["git", "add", "merged.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add merged task"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    subprocess.run(["git", "merge", "--no-ff", "-m", "land merged task", "build/merged-task"], cwd=repo, check=True)
    merge_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "branch", "-D", "build/merged-task"], cwd=repo, check=True)
    append_task(
        repo,
        QueueTask(
            id="merged-task",
            command_argv=["build", "merged task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="merged task",
            branch="build/merged-task",
            worktree=".worktrees/merged-task",
        ),
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "merged-task": {
                    "status": "done",
                    "attempt_run_id": "run-merged",
                    "stories_passed": 1,
                    "stories_tested": 1,
                },
            },
        },
    )
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-merged",
            started_at="2026-04-24T00:00:00Z",
            finished_at="2026-04-24T00:01:00Z",
            target="main",
            status="done",
            terminal_outcome="success",
            branches_in_order=["build/merged-task"],
            outcomes=[BranchOutcome(branch="build/merged-task", status="merged", merge_commit=merge_commit)],
        ),
    )

    client = _client(repo)
    item = client.get("/api/state").json()["landing"]["items"][0]
    packet = client.get("/api/runs/run-merged").json()["review_packet"]
    checks = {check["key"]: check for check in packet["checks"]}

    assert item["landing_state"] == "merged"
    assert item["diff_error"] is None
    assert item["changed_file_count"] == 1
    assert item["changed_files"] == ["merged.txt"]
    assert packet["readiness"]["state"] == "merged"
    assert packet["changes"]["diff_error"] is None
    assert packet["changes"]["diff_command"].startswith("git diff ")
    assert packet["changes"]["files"] == ["merged.txt"]
    assert checks["changes"]["status"] == "pass"
    assert checks["changes"]["detail"] == "1 file landed into main."

    diff = client.get("/api/runs/run-merged/diff").json()
    assert diff["file_count"] == 1
    assert diff["files"] == ["merged.txt"]
    assert "+merged" in diff["text"]

def test_web_merge_action_rejects_already_merged_task(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    record = make_run_record(
        project_dir=repo,
        run_id="run-merged",
        domain="queue",
        run_type="queue",
        command="build merged",
        display_name="merged-task",
        status="done",
        cwd=repo,
        identity={"queue_task_id": "merged-task"},
        git={"branch": "build/merged-task"},
        intent={"summary": "merged"},
        adapter_key="queue.attempt",
    )
    write_record(repo, record)
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-merged",
            started_at="2026-04-24T00:00:00Z",
            finished_at="2026-04-24T00:01:00Z",
            target="main",
            status="done",
            terminal_outcome="success",
            branches_in_order=["build/merged-task"],
            outcomes=[BranchOutcome(branch="build/merged-task", status="merged")],
        ),
    )
    calls: list[str] = []
    monkeypatch.setattr("otto.mission_control.service.execute_action", lambda *args, **kwargs: calls.append("called"))
    monkeypatch.setattr(
        "otto.mission_control.service._merge_preflight",
        lambda project_dir: {"merge_blocked": False, "merge_blockers": [], "dirty_files": []},
    )

    response = _client(repo).post("/api/runs/run-merged/actions/merge", json={})

    assert response.status_code == 409
    assert response.json()["message"] == "Already merged into main."
    assert calls == []

def test_web_merge_action_reports_already_merged_before_dirty_repo(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    record = make_run_record(
        project_dir=repo,
        run_id="run-merged",
        domain="queue",
        run_type="queue",
        command="build merged",
        display_name="merged-task",
        status="done",
        cwd=repo,
        identity={"queue_task_id": "merged-task"},
        git={"branch": "build/merged-task"},
        intent={"summary": "merged"},
        adapter_key="queue.attempt",
    )
    write_record(repo, record)
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-merged",
            started_at="2026-04-24T00:00:00Z",
            finished_at="2026-04-24T00:01:00Z",
            target="main",
            status="done",
            terminal_outcome="success",
            branches_in_order=["build/merged-task"],
            outcomes=[BranchOutcome(branch="build/merged-task", status="merged")],
        ),
    )
    monkeypatch.setattr(
        "otto.mission_control.service._merge_preflight",
        lambda project_dir: {"merge_blocked": True, "merge_blockers": ["dirty"], "dirty_files": ["README.md"]},
    )

    response = _client(repo).post("/api/runs/run-merged/actions/merge", json={})

    assert response.status_code == 409
    assert response.json()["message"] == "Already merged into main."

def test_web_landing_ignores_merge_state_for_different_target(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _create_branch_file(repo, "build/ready-task", "ready.txt")
    append_task(
        repo,
        QueueTask(
            id="ready-task",
            command_argv=["build", "ready task"],
            added_at="2026-04-24T00:00:00Z",
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
            "tasks": {"ready-task": {"status": "done", "attempt_run_id": "run-ready"}},
        },
    )
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-other-target",
            started_at="2026-04-24T00:00:00Z",
            finished_at="2026-04-24T00:01:00Z",
            target="release",
            status="done",
            terminal_outcome="success",
            branches_in_order=["build/ready-task"],
            outcomes=[BranchOutcome(branch="build/ready-task", status="merged")],
        ),
    )

    state = _client(repo).get("/api/state").json()

    assert state["landing"]["target"] == "main"
    assert state["landing"]["counts"]["ready"] == 1
    assert state["landing"]["items"][0]["landing_state"] == "ready"

def test_web_landing_ignores_unreachable_merge_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "checkout", "-q", "-b", "build/ready-task"], cwd=repo, check=True)
    (repo / "feature.txt").write_text("ready\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add feature"], cwd=repo, check=True)
    branch_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    append_task(
        repo,
        QueueTask(
            id="ready-task",
            command_argv=["build", "ready task"],
            added_at="2026-04-24T00:00:00Z",
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
            "tasks": {"ready-task": {"status": "done", "attempt_run_id": "run-ready"}},
        },
    )
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-unreachable",
            started_at="2026-04-24T00:00:00Z",
            finished_at="2026-04-24T00:01:00Z",
            target="main",
            status="done",
            terminal_outcome="success",
            branches_in_order=["build/ready-task"],
            outcomes=[BranchOutcome(branch="build/ready-task", status="merged", merge_commit=branch_sha)],
        ),
    )

    state = _client(repo).get("/api/state").json()

    assert state["landing"]["counts"]["ready"] == 1
    assert state["landing"]["counts"]["merged"] == 0
    assert state["landing"]["items"][0]["landing_state"] == "ready"

def test_web_landing_and_detail_show_review_packet_changed_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "checkout", "-q", "-b", "build/ready-task"], cwd=repo, check=True)
    (repo / "feature.txt").write_text("ready\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add feature"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    append_task(
        repo,
        QueueTask(
            id="ready-task",
            command_argv=["build", "ready task"],
            added_at="2026-04-24T00:00:00Z",
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

    client = _client(repo)
    state = client.get("/api/state").json()
    item = state["landing"]["items"][0]

    assert item["changed_file_count"] == 1
    assert item["changed_files"] == ["feature.txt"]

    detail = client.get("/api/runs/run-ready").json()
    packet = detail["review_packet"]
    assert packet["headline"] == "Ready for review"
    assert packet["readiness"] == {
        "state": "ready",
        "label": "Ready to land in main",
        "tone": "success",
        "blockers": [],
        "next_step": "Review evidence and land the task.",
    }
    checks = {check["key"]: check for check in packet["checks"]}
    assert checks["run"]["status"] == "pass"
    assert checks["certification"]["detail"] == "1/1 stories passed."
    assert checks["changes"]["status"] == "pass"
    assert checks["landing"]["detail"] == "Safe to land into main."
    assert packet["certification"]["stories_passed"] == 1
    assert packet["certification"]["stories_tested"] == 1
    assert packet["changes"]["files"] == ["feature.txt"]
    assert packet["changes"]["diff_command"] == "git diff main...build/ready-task"
    assert packet["next_action"]["label"] == "Land selected"
    assert packet["next_action"]["action_key"] == "m"

    diff = client.get("/api/runs/run-ready/diff").json()
    assert diff["command"] == "git diff main...build/ready-task"
    assert diff["files"] == ["feature.txt"]
    assert diff["file_count"] == 1
    assert diff["error"] is None
    assert "+ready" in diff["text"]

def test_web_landing_surfaces_diff_errors(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    append_task(
        repo,
        QueueTask(
            id="ready-task",
            command_argv=["build", "ready task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="ready task",
            branch="build/missing",
            worktree=".worktrees/ready-task",
        ),
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {"ready-task": {"status": "done", "attempt_run_id": "run-ready"}},
        },
    )
    client = _client(repo)
    state = client.get("/api/state").json()
    detail = client.get("/api/runs/run-ready").json()

    assert state["landing"]["counts"]["ready"] == 0
    assert state["landing"]["counts"]["blocked"] == 1
    assert state["landing"]["items"][0]["landing_state"] == "blocked"
    assert state["landing"]["items"][0]["label"] == "Review blocked"
    assert "build/missing" in state["landing"]["items"][0]["diff_error"]
    assert "build/missing" in detail["review_packet"]["changes"]["diff_error"]
    assert detail["review_packet"]["headline"] == "Review blocked before landing"
    assert detail["review_packet"]["readiness"]["state"] == "blocked"
    assert detail["review_packet"]["readiness"]["tone"] == "danger"
    assert detail["review_packet"]["next_action"] == {
        "label": "Land blocked",
        "action_key": None,
        "enabled": False,
        "reason": "Resolve review blockers before landing.",
    }
    checks = {check["key"]: check for check in detail["review_packet"]["checks"]}
    assert checks["changes"]["status"] == "fail"
    assert "build/missing" in checks["landing"]["detail"]

def test_web_landing_target_preserves_detected_branch_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _set_origin_head(repo, "fix/codex-provider-i2p")
    _create_branch_file(repo, "build/ready-task", "ready.txt")
    append_task(
        repo,
        QueueTask(
            id="ready-task",
            command_argv=["build", "ready task"],
            added_at="2026-04-24T00:00:00Z",
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

    state = _client(repo).get("/api/state").json()

    assert state["landing"]["target"] == "fix/codex-provider-i2p"
    assert state["landing"]["counts"]["ready"] == 1

def test_web_landing_blocks_merge_when_project_has_tracked_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _create_branch_file(repo, "build/ready-task", "ready.txt")
    append_task(
        repo,
        QueueTask(
            id="ready-task",
            command_argv=["build", "ready task"],
            added_at="2026-04-24T00:00:00Z",
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
    (repo / "README.md").write_text("# web\n\nlocal runtime state\n", encoding="utf-8")

    state = _client(repo).get("/api/state").json()

    assert state["landing"]["counts"]["ready"] == 1
    assert state["landing"]["merge_blocked"] is True
    assert "working tree has unstaged changes" in state["landing"]["merge_blockers"]
    assert state["landing"]["dirty_files"] == ["README.md"]

def test_web_review_packet_blocks_landing_when_project_has_tracked_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "checkout", "-q", "-b", "build/ready-task"], cwd=repo, check=True)
    (repo / "feature.txt").write_text("ready\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add feature"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    append_task(
        repo,
        QueueTask(
            id="ready-task",
            command_argv=["build", "ready task"],
            added_at="2026-04-24T00:00:00Z",
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
    (repo / "README.md").write_text("# web\n\nlocal runtime state\n", encoding="utf-8")

    detail = _client(repo).get("/api/runs/run-ready").json()
    packet = detail["review_packet"]
    checks = {check["key"]: check for check in packet["checks"]}

    assert packet["headline"] == "Repository cleanup required before landing"
    assert packet["readiness"]["state"] == "blocked"
    assert packet["readiness"]["tone"] == "danger"
    assert packet["next_action"] == {
        "label": "Land blocked",
        "action_key": None,
        "enabled": False,
        "reason": "Commit, stash, or revert local project changes before landing.",
    }
    assert "Repository has local changes: README.md." in packet["readiness"]["blockers"]
    assert checks["landing"]["status"] == "fail"
    assert "README.md" in checks["landing"]["detail"]

def test_web_merge_all_rejects_dirty_project_before_launch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("# web\n\nlocal runtime state\n", encoding="utf-8")

    response = _client(repo).post("/api/actions/merge-all", json={})

    assert response.status_code == 409
    assert "Merge blocked by local repository state" in response.json()["message"]
    assert "README.md" in response.json()["message"]

def test_web_runtime_issue_prefers_recovery_for_interrupted_merge(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setattr(
        "otto.mission_control.service._merge_preflight",
        lambda project_dir: {
            "merge_blocked": True,
            "merge_blockers": ["repository has unmerged paths: app.py", "repository has merge in progress"],
            "dirty_files": ["app.py"],
        },
    )

    state = _client(repo).get("/api/state").json()

    assert state["runtime"]["issues"][0]["label"] == "Landing recovery available"
    issue = next(item for item in state["runtime"]["issues"] if item["label"] == "Landing recovery available")
    assert "Recover landing" in issue["next_action"]

def test_web_merge_recovery_routes_record_actions(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    calls: list[str] = []

    def _fake_abort(project_dir):
        calls.append(f"abort:{project_dir}")
        return ActionResult(ok=True, message="aborted", refresh=True)

    def _fake_recover(project_dir, *, post_result=None):
        del post_result
        calls.append(f"recover:{project_dir}")
        return ActionResult(ok=True, message="recovery launched", refresh=True)

    monkeypatch.setattr("otto.mission_control.service.execute_merge_abort", _fake_abort)
    monkeypatch.setattr("otto.mission_control.service.execute_merge_recover", _fake_recover)
    client = _client(repo)

    abort = client.post("/api/actions/merge-abort", json={})
    recover = client.post("/api/actions/merge-recover", json={})

    assert abort.status_code == 200
    assert abort.json()["message"] == "aborted"
    assert recover.status_code == 200
    assert recover.json()["message"] == "recovery launched"
    assert calls == [f"abort:{repo.resolve()}", f"recover:{repo.resolve()}"]

def test_web_resolve_release_recovers_interrupted_merge(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    calls: list[str] = []

    monkeypatch.setattr(
        "otto.mission_control.service.MissionControlService.landing_status",
        lambda self: {
            "counts": {"ready": 1, "merged": 0, "blocked": 0, "total": 1},
            "items": [],
            "merge_blocked": True,
            "merge_blockers": ["repository has merge in progress"],
            "dirty_files": ["app.py"],
        },
    )
    monkeypatch.setattr(
        "otto.mission_control.service.execute_merge_recover",
        lambda project_dir, *, post_result=None: calls.append(str(project_dir)) or ActionResult(ok=True, message="recovery launched", refresh=True),
    )

    response = _client(repo).post("/api/actions/resolve-release", json={})

    assert response.status_code == 200
    assert response.json()["message"] == "recovery launched"
    assert calls == [str(repo.resolve())]

def test_web_resolve_release_cleans_superseded_failed_tasks(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    calls: list[list[str]] = []

    monkeypatch.setattr(
        "otto.mission_control.service.MissionControlService.landing_status",
        lambda self: {
            "counts": {"ready": 0, "merged": 1, "blocked": 1, "reviewed": 0, "total": 2},
            "items": [
                {
                    "task_id": "old-task",
                    "queue_status": "failed",
                    "landing_state": "blocked",
                    "summary": "Add CSV export",
                },
                {
                    "task_id": "redo-task",
                    "queue_status": "done",
                    "landing_state": "merged",
                    "summary": "Add CSV export",
                },
            ],
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
        },
    )
    monkeypatch.setattr(
        "otto.mission_control.service.execute_queue_cleanup",
        lambda project_dir, task_ids, *, post_result=None: calls.append(list(task_ids)) or ActionResult(ok=True, message="cleanup launched", refresh=True),
    )

    response = _client(repo).post("/api/actions/resolve-release", json={})

    assert response.status_code == 200
    assert response.json()["message"] == "cleanup launched"
    assert calls == [["old-task"]]

def test_web_landing_does_not_show_diff_errors_for_queued_future_branches(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _append_queue_task(repo, "queued-task", command_argv=["build", "queued task"], resolved_intent="queued task")
    _write_empty_queue_state(repo)

    state = _client(repo).get("/api/state").json()
    item = state["landing"]["items"][0]

    assert item["queue_status"] == "queued"
    assert item["diff_error"] is None
    assert item["changed_file_count"] == 0

def test_web_review_packet_does_not_diff_queued_future_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _append_queue_task(repo, "queued-task", command_argv=["build", "queued task"], resolved_intent="queued task")
    _write_empty_queue_state(repo)

    client = _client(repo)
    state = client.get("/api/state").json()
    run_id = state["live"]["items"][0]["run_id"]
    detail = client.get(f"/api/runs/{run_id}").json()
    packet = detail["review_packet"]
    checks = {check["key"]: check for check in packet["checks"]}

    assert detail["display_status"] == "queued"
    assert packet["headline"] == "Waiting for queue runner"
    assert packet["readiness"]["label"] == "Queued"
    assert packet["readiness"]["next_step"] == "Start the queue runner when you want this queued task to run."
    assert packet["next_action"]["label"] == "Start queue runner"
    assert packet["next_action"]["enabled"] is False
    assert packet["changes"]["diff_error"] is None
    assert packet["changes"]["diff_command"] is None
    assert packet["changes"]["file_count"] == 0
    assert packet["changes"]["files"] == []
    assert checks["changes"]["status"] == "pending"
    assert checks["certification"]["status"] == "pending"
    assert checks["evidence"]["status"] == "pending"
    verification_plan = detail["verification_plan"]
    assert verification_plan["scope"] == "queue/queue"
    assert verification_plan["policy"] == "smart"
    assert verification_plan["verification_level"] == "provisional"
    assert {check["id"]: check["status"] for check in verification_plan["checks"]}["certification"] == "pending"

def test_web_merge_run_review_packet_is_landing_audit_not_landable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    record = make_run_record(
        project_dir=repo,
        run_id="merge-audit",
        domain="merge",
        run_type="merge",
        command="merge",
        display_name="merge: 1 branch",
        status="done",
        cwd=repo,
        identity={"merge_id": "merge-audit"},
        git={"branch": "main", "target_branch": "main"},
        intent={"summary": "merge 1 branch"},
        adapter_key="merge.run",
    )
    record.terminal_outcome = "success"
    write_record(repo, record)

    detail = _client(repo).get("/api/runs/merge-audit").json()
    packet = detail["review_packet"]
    checks = {check["key"]: check for check in packet["checks"]}

    assert packet["headline"] == "Landed in main"
    assert packet["readiness"]["state"] == "merged"
    assert packet["readiness"]["next_step"] == "Audit the landing record, artifacts, and final logs if needed."
    assert packet["next_action"] == {
        "label": "No action",
        "action_key": None,
        "enabled": False,
        "reason": "Landing runs are audit records.",
    }
    assert packet["changes"]["file_count"] == 0
    assert packet["changes"]["diff_command"] is None
    assert checks["landing"]["detail"] == "No further landing action is needed."
    assert checks["certification"]["status"] == "info"

def test_web_merge_history_review_packet_uses_persisted_target(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-release",
            started_at="2026-04-24T00:00:00Z",
            finished_at="2026-04-24T00:01:00Z",
            target="release/1.0",
            target_head_before="abc123",
            status="done",
            terminal_outcome="success",
            branches_in_order=["build/release-task"],
            outcomes=[BranchOutcome(branch="build/release-task", status="merged")],
        ),
    )
    append_history_snapshot(
        repo,
        build_terminal_snapshot(
            run_id="merge-release",
            domain="merge",
            run_type="merge",
            command="merge",
            intent_meta={"summary": "merge release", "intent_path": None, "spec_path": None},
            status="done",
            terminal_outcome="success",
            identity={"merge_id": "merge-release"},
            artifacts={"session_dir": str(paths.merge_dir(repo) / "merge-release")},
        ),
    )

    packet = _client(repo).get("/api/runs/merge-release?type=merge").json()["review_packet"]

    assert packet["headline"] == "Landed in release/1.0"
    assert packet["changes"]["target"] == "release/1.0"

def test_web_merge_action_uses_fast_merge_and_reports_immediate_failure(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    record = make_run_record(
        project_dir=repo,
        run_id="queue-done",
        domain="queue",
        run_type="queue",
        command="build hello",
        display_name="hello-web",
        status="done",
        cwd=repo,
        identity={"queue_task_id": "hello-web"},
        git={"branch": "build/hello-web"},
        intent={"summary": "hello"},
        adapter_key="queue.attempt",
    )
    write_record(repo, record)
    calls: list[list[str]] = []

    class _FailedPopen:
        returncode = 1

        def __init__(self, argv, **kwargs) -> None:
            calls.append(list(argv))

        def poll(self):
            return self.returncode

        def communicate(self):
            return "", "merge failed"

    monkeypatch.setattr("otto.mission_control.actions.subprocess.Popen", _FailedPopen)
    monkeypatch.setattr(
        "otto.mission_control.service._merge_preflight",
        lambda project_dir: {"merge_blocked": False, "merge_blockers": [], "dirty_files": []},
    )

    client = _client(repo)
    response = client.post("/api/runs/queue-done/actions/merge", json={})

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "merge failed" in response.json()["message"]
    assert any(call[-5:] == ["merge", "--fast", "--verify", "smart", "hello-web"] for call in calls)
    event = client.get("/api/events").json()["items"][0]
    assert event["kind"] == "run.merge"
    assert event["severity"] == "error"
    assert event["run_id"] == "queue-done"

def test_web_merge_action_records_late_background_failure(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    record = make_run_record(
        project_dir=repo,
        run_id="queue-done",
        domain="queue",
        run_type="queue",
        command="build hello",
        display_name="hello-web",
        status="done",
        cwd=repo,
        identity={"queue_task_id": "hello-web"},
        git={"branch": "build/hello-web"},
        intent={"summary": "hello"},
        adapter_key="queue.attempt",
    )
    write_record(repo, record)
    completed = threading.Event()

    class _LateFailedPopen:
        pid = 23456
        returncode = None

        def __init__(self, argv, **kwargs) -> None:
            pass

        def poll(self):
            return None

        def communicate(self):
            self.returncode = 1
            completed.set()
            return "", "late merge failed"

    monkeypatch.setattr("otto.mission_control.actions.subprocess.Popen", _LateFailedPopen)
    monkeypatch.setattr(
        "otto.mission_control.service._merge_preflight",
        lambda project_dir: {"merge_blocked": False, "merge_blockers": [], "dirty_files": []},
    )

    client = _client(repo)
    response = client.post("/api/runs/queue-done/actions/merge", json={})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert completed.wait(2)
    events = client.get("/api/events").json()["items"]
    completion = next(event for event in events if event["kind"] == "run.merge.completed")
    assert completion["severity"] == "error"
    assert completion["message"] == "late merge failed"
