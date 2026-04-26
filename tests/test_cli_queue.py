"""Tests for otto/cli_queue.py — Phase 2.3-2.6 CLI surface."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from click.testing import CliRunner

import otto.cli_queue as cli_queue_module
from otto.cli import main
from otto import paths
from otto.queue.schema import (
    COMMANDS_FILE,
    QUEUE_FILE,
    QueueTask,
    append_task,
    load_queue,
    load_state,
    write_state,
)
from otto.runs.history import append_history_snapshot, read_history_rows
from tests._helpers import init_repo


def _run(args: list[str], *, cwd: Path) -> tuple[int, str, str]:
    """Run otto CLI in `cwd`. Returns (exit_code, stdout, stderr)."""
    runner = CliRunner()
    saved_cwd = os.getcwd()
    os.chdir(cwd)
    try:
        result = runner.invoke(main, args, catch_exceptions=False)
    finally:
        os.chdir(saved_cwd)
    return result.exit_code, result.output, ""


def _fresh_iso_now() -> str:
    return cli_queue_module.time.strftime("%Y-%m-%dT%H:%M:%SZ", cli_queue_module.time.gmtime())


def _write_watcher_state(
    repo: Path,
    *,
    watcher: dict | None,
    tasks: dict[str, dict] | None = None,
) -> None:
    write_state(
        repo,
        {
            "watcher": watcher,
            "tasks": tasks or {},
        },
    )


def _read_queue_commands(repo: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (repo / COMMANDS_FILE).read_text().splitlines()
        if line.strip()
    ]


# ---------- enqueue commands ----------


def test_queue_build_appends_to_queue_yml(tmp_path: Path):
    repo = init_repo(tmp_path)
    # Capture date BEFORE the action so a midnight-rollover race can't make
    # the assertion compare against tomorrow's date.
    expected_date = cli_queue_module.time.strftime("%Y-%m-%d")
    code, out, _ = _run(["queue", "build", "add csv export"], cwd=repo)
    assert code == 0, out
    tasks = load_queue(repo)
    assert len(tasks) == 1
    assert tasks[0].command_argv == ["build", "add csv export"]
    assert tasks[0].resolved_intent == "add csv export"
    assert tasks[0].resumable is True
    assert tasks[0].id == "add-csv-export"
    assert tasks[0].branch == f"build/add-csv-export-{expected_date}"
    assert tasks[0].worktree == ".worktrees/add-csv-export"


def test_queue_build_from_subdirectory_uses_repo_root(tmp_path: Path):
    repo = init_repo(tmp_path)
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)

    code, out, _ = _run(["queue", "build", "add csv export"], cwd=nested)

    assert code == 0, out
    assert (repo / QUEUE_FILE).exists()
    assert not (nested / QUEUE_FILE).exists()
    assert load_queue(repo)[0].resolved_intent == "add csv export"


def test_queue_build_reports_malformed_otto_yaml_cleanly(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "otto.yaml").write_text("default_branch: [\n")

    code, out, _ = _run(["queue", "build", "add csv export"], cwd=repo)

    assert code == 2
    assert "otto.yaml" in out
    assert "Traceback" not in out


def test_queue_build_reports_malformed_queue_yml_cleanly(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / QUEUE_FILE).write_text(
        "schema_version: 1\n"
        "tasks:\n"
        "  - id: broken\n"
        "    added_at: 2026-04-21T00:00:00Z\n"
    )

    code, out, _ = _run(["queue", "build", "add csv export"], cwd=repo)

    assert code == 2
    assert "queue.yml is malformed" in out
    assert "command_argv" in out
    assert "Traceback" not in out


def test_queue_improve_reports_intent_resolution_error_cleanly(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "README.md").write_text("x" * 9000, encoding="utf-8")

    code, out, _ = _run(["queue", "improve", "bugs"], cwd=repo)

    assert code == 2
    assert "intent exceeds" in out
    assert "Traceback" not in out


def test_queue_certify_reports_intent_resolution_error_cleanly(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "README.md").write_text("x" * 9000, encoding="utf-8")

    code, out, _ = _run(["queue", "certify"], cwd=repo)

    assert code == 2
    assert "intent exceeds" in out
    assert "Traceback" not in out


def test_queue_certify_marked_not_resumable(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "intent.md").write_text("test product")
    code, out, _ = _run(["queue", "certify"], cwd=repo)
    assert code == 0, out
    tasks = load_queue(repo)
    assert tasks[0].resumable is False
    assert tasks[0].resolved_intent == "test product"


def test_queue_certify_explicit_intent_overrides_project_files(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "intent.md").write_text("from project files")
    code, out, _ = _run(["queue", "certify", "from cli"], cwd=repo)
    assert code == 0, out
    tasks = load_queue(repo)
    assert tasks[0].resolved_intent == "from cli"


def test_queue_improve_bugs(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "intent.md").write_text("a product")
    code, out, _ = _run(["queue", "improve", "bugs", "error handling"], cwd=repo)
    assert code == 0, out
    tasks = load_queue(repo)
    assert tasks[0].command_argv[:2] == ["improve", "bugs"]
    assert tasks[0].focus == "error handling"
    # W3-IMPORTANT-1: improve resolved_intent prefers focus over the
    # snapshot intent so the task board / agent context describes what
    # the user actually asked for, not the project's README.
    assert tasks[0].resolved_intent == "error handling"


def test_queue_improve_target_focus_not_set(tmp_path: Path):
    """For target subcommand, the arg goes to `target` not `focus`."""
    repo = init_repo(tmp_path)
    (repo / "intent.md").write_text("a product")
    code, out, _ = _run(["queue", "improve", "target", "latency < 100ms"], cwd=repo)
    assert code == 0, out
    tasks = load_queue(repo)
    assert tasks[0].target == "latency < 100ms"
    assert tasks[0].focus is None


def test_queue_improve_target_requires_goal(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "intent.md").write_text("a product")

    code, out, _ = _run(["queue", "improve", "target"], cwd=repo)

    assert code == 2
    assert "Missing argument 'GOAL'" in out
    assert load_queue(repo) == []


def test_queue_improve_target_requires_goal_before_passthrough(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "intent.md").write_text("a product")

    code, out, _ = _run(["queue", "improve", "target", "--rounds", "4"], cwd=repo)

    assert code == 2
    assert "Intent looks like a CLI flag" in out
    assert load_queue(repo) == []


def test_queue_build_rejects_resume_in_args(tmp_path: Path):
    repo = init_repo(tmp_path)
    code, out, _ = _run(["queue", "build", "test", "--resume"], cwd=repo)
    assert code == 2
    assert "--resume is not allowed" in out


def test_queue_build_rejects_flag_like_missing_intent_after_double_dash(tmp_path: Path):
    repo = init_repo(tmp_path)
    code, out, _ = _run(["queue", "build", "--as", "add", "--", "--fast"], cwd=repo)
    assert code == 2
    assert "looks like a CLI flag" in out
    assert '"add csv export" --as csv -- --fast --rounds 3' in out


def test_queue_build_accepts_real_intent_before_double_dash(tmp_path: Path):
    repo = init_repo(tmp_path)
    code, out, _ = _run(
        ["queue", "build", "real intent", "--as", "add", "--", "--fast"],
        cwd=repo,
    )
    assert code == 0, out
    tasks = load_queue(repo)
    assert tasks[0].id == "add"
    assert tasks[0].command_argv == ["build", "real intent", "--fast"]


def test_looks_like_flag_rejects_short_flag_like_intent():
    assert cli_queue_module._looks_like_flag("-foo") is True


def test_queue_build_allows_dash_inside_intent(tmp_path: Path):
    repo = init_repo(tmp_path)
    code, out, _ = _run(["queue", "build", "fix bug -1"], cwd=repo)
    assert code == 0, out
    tasks = load_queue(repo)
    assert tasks[0].resolved_intent == "fix bug -1"


def test_queue_build_explicit_as(tmp_path: Path):
    repo = init_repo(tmp_path)
    code, out, _ = _run(["queue", "build", "test", "--as", "my-id"], cwd=repo)
    assert code == 0, out
    tasks = load_queue(repo)
    assert tasks[0].id == "my-id"


def test_queue_build_explicit_as_rejects_reserved(tmp_path: Path):
    repo = init_repo(tmp_path)
    code, out, _ = _run(["queue", "build", "test", "--as", "ls"], cwd=repo)
    assert code == 2
    assert "reserved" in out


def test_queue_build_dedup_appends_suffix(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "same intent"], cwd=repo)
    _run(["queue", "build", "same intent"], cwd=repo)
    tasks = load_queue(repo)
    ids = [t.id for t in tasks]
    assert ids == ["same-intent", "same-intent-2"]
    assert tasks[0].branch != tasks[1].branch
    assert tasks[0].worktree != tasks[1].worktree


def test_queue_build_after_validates_existing(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "first"], cwd=repo)
    code, out, _ = _run(["queue", "build", "second", "--after", "first"], cwd=repo)
    assert code == 0, out
    tasks = load_queue(repo)
    assert tasks[1].after == ["first"]


def test_queue_build_after_rejects_unknown(tmp_path: Path):
    repo = init_repo(tmp_path)
    code, out, _ = _run(["queue", "build", "test", "--after", "nonexistent"], cwd=repo)
    assert code == 2
    assert "unknown task" in out


def test_queue_build_rejects_unknown_target_flag(tmp_path: Path):
    repo = init_repo(tmp_path)
    code, out, _ = _run(["queue", "build", "test", "--bogus-flag"], cwd=repo)
    assert code == 2
    assert "No such option: --bogus-flag" in out


def test_queue_improve_rejects_missing_option_value(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "intent.md").write_text("a product")
    code, out, _ = _run(["queue", "improve", "bugs", "--rounds"], cwd=repo)
    assert code == 2
    assert "Option '--rounds' requires an argument" in out


def test_queue_improve_accepts_valid_target_args(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "intent.md").write_text("a product")
    code, out, _ = _run(["queue", "improve", "bugs", "errors", "--rounds", "4"], cwd=repo)
    assert code == 0, out
    tasks = load_queue(repo)
    assert tasks[0].command_argv == ["improve", "bugs", "errors", "--rounds", "4"]


# ---------- ls / show ----------


def test_queue_ls_empty(tmp_path: Path):
    repo = init_repo(tmp_path)
    code, out, _ = _run(["queue", "ls"], cwd=repo)
    assert code == 0
    assert "Queue is empty" in out


def test_queue_ls_shows_tasks(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv export"], cwd=repo)
    _run(["queue", "build", "settings page"], cwd=repo)
    code, out, _ = _run(["queue", "ls"], cwd=repo)
    assert code == 0
    assert "csv-export" in out
    assert "settings-page" in out


def test_queue_ls_marks_interrupted_resume_ready(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv export"], cwd=repo)
    session_id = "2026-04-22-010203-abc123"
    paths.ensure_session_scaffold(repo / ".worktrees" / "csv-export", session_id)
    paths.session_checkpoint(repo / ".worktrees" / "csv-export", session_id).write_text(
        json.dumps({"status": "paused", "updated_at": "2026-04-22T01:02:03Z"})
    )
    _write_watcher_state(
        repo,
        watcher=None,
        tasks={
            "csv-export": {
                "status": "terminating",
                "terminal_status": "interrupted",
                "failure_reason": "interrupted by watcher shutdown; resume available",
            }
        },
    )

    code, out, _ = _run(["queue", "ls"], cwd=repo)

    assert code == 0
    assert "csv-export" in out
    assert "interrupted" in out
    assert "ready" in out


def test_queue_show_existing_task(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv export"], cwd=repo)
    code, out, _ = _run(["queue", "show", "csv-export"], cwd=repo)
    assert code == 0
    assert "csv-export" in out
    assert "queued" in out
    assert "Resumable: True" in out


def test_queue_show_reports_resume_checkpoint_for_interrupted_task(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv export"], cwd=repo)
    session_id = "2026-04-22-010203-abc123"
    paths.ensure_session_scaffold(repo / ".worktrees" / "csv-export", session_id)
    checkpoint_path = paths.session_checkpoint(repo / ".worktrees" / "csv-export", session_id)
    checkpoint_path.write_text(json.dumps({"status": "paused", "updated_at": "2026-04-22T01:02:03Z"}))
    _write_watcher_state(
        repo,
        watcher=None,
        tasks={
            "csv-export": {
                "status": "terminating",
                "terminal_status": "interrupted",
                "failure_reason": "interrupted by watcher shutdown; resume available",
            }
        },
    )

    code, out, _ = _run(["queue", "show", "csv-export"], cwd=repo)

    assert code == 0
    assert "Resume status:" in out
    assert "ready" in out
    assert str(checkpoint_path).replace("\n", "") in "".join(out.split())


def test_queue_show_reports_proof_of_work_html_path(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv export"], cwd=repo)
    pow_json = paths.certify_dir(repo, "run-queue-show") / "proof-of-work.json"
    pow_html = pow_json.with_name("proof-of-work.html")
    pow_html.parent.mkdir(parents=True, exist_ok=True)
    pow_json.write_text("{\"stories\": []}\n")
    pow_html.write_text("<html></html>\n")
    manifest_dir = repo / "otto_logs" / "queue" / "csv-export"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "manifest.json").write_text(json.dumps({
        "run_id": "run-queue-show",
        "proof_of_work_path": str(pow_json.resolve()),
    }, indent=2))
    _write_watcher_state(
        repo,
        watcher=None,
        tasks={"csv-export": {"status": "done"}},
    )

    code, out, _ = _run(["queue", "show", "csv-export"], cwd=repo)

    assert code == 0
    assert "Proof-of-work:" in out
    normalized = "".join(out.split())
    assert str(pow_html.resolve()).replace(" ", "") in normalized


def test_queue_show_unknown_task(tmp_path: Path):
    repo = init_repo(tmp_path)
    code, out, _ = _run(["queue", "show", "nonexistent"], cwd=repo)
    assert code == 2
    assert "No such task" in out


def test_queue_show_reports_malformed_queue_yml_cleanly(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / QUEUE_FILE).write_text(
        "schema_version: 1\n"
        "tasks:\n"
        "  - id: broken\n"
        "    added_at: 2026-04-21T00:00:00Z\n"
    )

    code, out, _ = _run(["queue", "show", "broken"], cwd=repo)

    assert code == 2
    assert "queue.yml is malformed" in out
    assert "command_argv" in out


# ---------- rm / cancel ----------


def test_queue_rm_without_watcher_removes_from_queue(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv"], cwd=repo)
    code, out, _ = _run(["queue", "rm", "csv"], cwd=repo)
    assert code == 0
    assert "Removed csv from queue." in out
    assert [task.id for task in load_queue(repo)] == []
    assert not (repo / COMMANDS_FILE).exists()


def test_queue_rm_with_watcher_running_appends_command(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv"], cwd=repo)
    now = _fresh_iso_now()
    _write_watcher_state(
        repo,
        watcher={
            "pid": os.getpid(),
            "pgid": os.getpid(),
            "started_at": now,
            "heartbeat": now,
        },
    )
    monkeypatch.setattr(cli_queue_module.os, "kill", lambda pid, sig: None)
    code, out, _ = _run(["queue", "rm", "csv"], cwd=repo)
    assert code == 0
    assert "Remove queued; watcher will apply within ~1s." in out
    assert [task.id for task in load_queue(repo)] == ["csv"]
    cmds = _read_queue_commands(repo)
    assert len(cmds) == 1
    assert cmds[0]["cmd"] == "remove"
    assert cmds[0]["id"] == "csv"
    assert cmds[0]["schema_version"] == 1
    assert cmds[0]["command_id"].startswith("queue-cmd-")


def test_queue_rm_reports_malformed_queue_yml_cleanly(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / QUEUE_FILE).write_text(
        "schema_version: 1\n"
        "tasks:\n"
        "  - id: csv\n"
        "    command_argv: build csv\n"
    )

    code, out, _ = _run(["queue", "rm", "csv"], cwd=repo)

    assert code == 2
    assert "queue.yml is malformed" in out
    assert "command_argv must be list[str]" in out


def test_queue_rm_refuses_finished_task_without_watcher(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv"], cwd=repo)
    _write_watcher_state(
        repo,
        watcher=None,
        tasks={"csv": {"status": "done"}},
    )

    code, out, _ = _run(["queue", "rm", "csv"], cwd=repo)

    assert code == 2
    assert "task csv is done" in out
    assert "otto queue cleanup csv" in out
    assert [task.id for task in load_queue(repo)] == ["csv"]


def test_queue_cleanup_without_watcher_prunes_terminal_task(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(
        repo,
        QueueTask(
            id="failed-task",
            command_argv=["build", "failed"],
            added_at="2026-04-25T00:00:00Z",
            branch="build/failed-task",
            worktree=".worktrees/failed-task",
        ),
    )
    _write_watcher_state(repo, watcher=None, tasks={"failed-task": {"status": "failed"}})

    code, out, _ = _run(["queue", "cleanup", "failed-task"], cwd=repo)

    assert code == 0, out
    assert "removed terminal task" in out
    assert [task.id for task in load_queue(repo)] == []
    assert "failed-task" not in load_state(repo)["tasks"]


def test_queue_cleanup_with_watcher_queues_cleanup_command(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path)
    append_task(
        repo,
        QueueTask(
            id="failed-task",
            command_argv=["build", "failed"],
            added_at="2026-04-25T00:00:00Z",
            branch="build/failed-task",
            worktree=".worktrees/failed-task",
        ),
    )
    now = _fresh_iso_now()
    _write_watcher_state(
        repo,
        watcher={
            "pid": os.getpid(),
            "pgid": os.getpid(),
            "started_at": now,
            "heartbeat": now,
        },
        tasks={"failed-task": {"status": "failed"}},
    )
    monkeypatch.setattr(cli_queue_module.os, "kill", lambda pid, sig: None)

    code, out, _ = _run(["queue", "cleanup", "failed-task"], cwd=repo)

    assert code == 0, out
    assert "Cleanup queued; watcher will remove 1 terminal task from the board." in out
    assert [task.id for task in load_queue(repo)] == ["failed-task"]
    cmds = [json.loads(line) for line in (repo / COMMANDS_FILE).read_text().splitlines() if line.strip()]
    assert cmds == [
        {
            "ts": cmds[0]["ts"],
            "cmd": "cleanup",
            "id": "failed-task",
            "schema_version": 1,
            "command_id": cmds[0]["command_id"],
        }
    ]
    assert cmds[0]["command_id"].startswith("queue-cmd-")


def test_queue_rm_without_watcher_refuses_non_queued_task(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv"], cwd=repo)
    _write_watcher_state(
        repo,
        watcher=None,
        tasks={"csv": {"status": "running"}},
    )

    code, out, _ = _run(["queue", "rm", "csv"], cwd=repo)

    assert code == 0
    assert "marked running, but the worker is not running" in out
    assert [task.id for task in load_queue(repo)] == ["csv"]


def test_queue_rm_refuses_interrupted_task_with_resume_or_cleanup_hint(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv"], cwd=repo)
    session_id = "2026-04-22-010203-abc123"
    paths.ensure_session_scaffold(repo / ".worktrees" / "csv", session_id)
    paths.session_checkpoint(repo / ".worktrees" / "csv", session_id).write_text(
        json.dumps({"status": "paused", "updated_at": "2026-04-22T01:02:03Z"})
    )
    _write_watcher_state(
        repo,
        watcher=None,
        tasks={"csv": {"status": "interrupted"}},
    )

    code, out, _ = _run(["queue", "rm", "csv"], cwd=repo)

    assert code == 2
    assert "otto queue resume csv" in out
    assert "queue cleanup csv" in out
    assert [task.id for task in load_queue(repo)] == ["csv"]


def test_queue_cleanup_preserves_session_artifacts_and_repairs_history(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path, subdir=None)
    task_id = "csv"
    run_id = "2026-04-23-170653-229eda"
    worktree = repo / ".worktrees" / task_id
    session_dir = paths.ensure_session_scaffold(worktree, run_id, phase="build")
    build_log = session_dir / "build" / "narrative.log"
    build_log.write_text("build log\n", encoding="utf-8")
    checkpoint = session_dir / "checkpoint.json"
    checkpoint.write_text("{}\n", encoding="utf-8")
    summary = session_dir / "summary.json"
    summary.write_text(json.dumps({"run_id": run_id, "command": "build"}, indent=2), encoding="utf-8")
    proof = session_dir / "certify" / "proof-of-work.json"
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text("{\"stories\": []}\n", encoding="utf-8")
    manifest = session_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "command": "build",
                "argv": ["build", "csv export"],
                "queue_task_id": task_id,
                "run_id": run_id,
                "branch": "build/csv",
                "checkpoint_path": str(checkpoint.resolve()),
                "proof_of_work_path": str(proof.resolve()),
                "cost_usd": 0.5,
                "duration_s": 2.0,
                "started_at": "2026-04-23T17:06:53Z",
                "finished_at": "2026-04-23T17:07:03Z",
                "head_sha": None,
                "resolved_intent": "csv export",
                "exit_status": "success",
                "schema_version": 1,
                "extra": {},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    queue_manifest = repo / "otto_logs" / "queue" / task_id / "manifest.json"
    queue_manifest.parent.mkdir(parents=True, exist_ok=True)
    queue_manifest.write_text(
        json.dumps(
            {
                **json.loads(manifest.read_text(encoding="utf-8")),
                "mirror_of": str(manifest.resolve()),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    append_task(
        repo,
        QueueTask(
            id=task_id,
            command_argv=["build", "csv export"],
            added_at="2026-04-23T17:06:00Z",
            branch="build/csv",
            worktree=".worktrees/csv",
        ),
    )
    _write_watcher_state(repo, watcher=None, tasks={task_id: {"status": "done"}})
    append_history_snapshot(
        repo,
        {
            "run_id": run_id,
            "status": "done",
            "terminal_outcome": "success",
            "session_dir": str(session_dir.resolve()),
            "manifest_path": str(manifest.resolve()),
            "summary_path": str(summary.resolve()),
            "checkpoint_path": str(checkpoint.resolve()),
            "primary_log_path": str(build_log.resolve()),
            "extra_log_paths": [str(proof.resolve())],
            "artifacts": {
                "session_dir": str(session_dir.resolve()),
                "manifest_path": str(manifest.resolve()),
                "summary_path": str(summary.resolve()),
                "checkpoint_path": str(checkpoint.resolve()),
                "primary_log_path": str(build_log.resolve()),
                "extra_log_paths": [str(proof.resolve())],
            },
        },
        strict=True,
    )
    _write_watcher_state(
        repo,
        watcher=None,
        tasks={task_id: {"status": "done"}},
    )

    def _fake_git_worktree_remove(project_dir: Path, wt_path: Path, *, force: bool):
        del project_dir, force
        shutil.rmtree(wt_path)
        return subprocess.CompletedProcess(["git", "worktree", "remove"], 0, "", "")

    monkeypatch.setattr(cli_queue_module, "_git_worktree_remove", _fake_git_worktree_remove)

    code, out, _ = _run(["queue", "cleanup", task_id], cwd=repo)

    dst_session = repo / "otto_logs" / "sessions" / run_id
    assert code == 0, out
    assert dst_session.exists()
    assert not worktree.exists()

    queue_manifest_data = json.loads(queue_manifest.read_text(encoding="utf-8"))
    assert queue_manifest_data["mirror_of"] == str((dst_session / "manifest.json").resolve())
    assert queue_manifest_data["checkpoint_path"] == str((dst_session / "checkpoint.json").resolve())
    assert queue_manifest_data["proof_of_work_path"] == str((dst_session / "certify" / "proof-of-work.json").resolve())

    rows = read_history_rows(paths.history_jsonl(repo))
    matching_rows = [
        row for row in rows
        if row.get("dedupe_key") == f"terminal_snapshot:{run_id}"
    ]
    assert len(matching_rows) == 1
    history_row = next(
        row for row in reversed(rows)
        if row.get("dedupe_key") == f"terminal_snapshot:{run_id}"
    )
    assert history_row["session_dir"] == str(dst_session.resolve())
    assert history_row["manifest_path"] == str((dst_session / "manifest.json").resolve())
    assert history_row["summary_path"] == str((dst_session / "summary.json").resolve())
    assert history_row["checkpoint_path"] == str((dst_session / "checkpoint.json").resolve())
    assert history_row["primary_log_path"] == str((dst_session / "build" / "narrative.log").resolve())
    assert history_row["extra_log_paths"] == [str((dst_session / "certify" / "proof-of-work.json").resolve())]
    assert history_row["artifacts"]["manifest_path"] == history_row["manifest_path"]
    assert history_row["artifacts"]["summary_path"] == history_row["summary_path"]
    assert history_row["artifacts"]["primary_log_path"] == history_row["primary_log_path"]

    for artifact_path in (
        history_row["session_dir"],
        history_row["manifest_path"],
        history_row["summary_path"],
        history_row["checkpoint_path"],
        history_row["primary_log_path"],
        *history_row["extra_log_paths"],
    ):
        assert Path(artifact_path).exists(), artifact_path


def test_queue_cleanup_rejects_running_explicit_task(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    append_task(
        repo,
        QueueTask(
            id="csv",
            command_argv=["build", "csv"],
            added_at="2026-04-23T17:06:00Z",
            branch="build/csv",
            worktree=".worktrees/csv",
        ),
    )
    _write_watcher_state(
        repo,
        watcher=None,
        tasks={"csv": {"status": "running"}},
    )

    code, out, _ = _run(["queue", "cleanup", "csv", "--force"], cwd=repo)

    assert code == 2
    assert "Only terminal tasks can be cleaned up" in out
    assert "csv is running" in out


def test_queue_cleanup_accepts_interrupted_explicit_task(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    append_task(
        repo,
        QueueTask(
            id="csv",
            command_argv=["build", "csv"],
            added_at="2026-04-23T17:06:00Z",
            branch="build/csv",
            worktree=".worktrees/csv",
        ),
    )
    _write_watcher_state(
        repo,
        watcher=None,
        tasks={"csv": {"status": "interrupted"}},
    )

    code, out, _ = _run(["queue", "cleanup", "csv", "--force"], cwd=repo)

    assert code == 0
    assert "removed terminal task csv from queue" in out
    assert "Done. Cleaned 1, skipped 0." in out


def test_queue_cancel_without_watcher_removes_queued_task(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv"], cwd=repo)
    code, out, _ = _run(["queue", "cancel", "csv"], cwd=repo)
    assert code == 0
    assert "was never started. Removed from queue." in out
    assert [task.id for task in load_queue(repo)] == []
    assert not (repo / COMMANDS_FILE).exists()


def test_queue_cancel_without_watcher_warns_for_running_task(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv"], cwd=repo)
    _write_watcher_state(
        repo,
        watcher=None,
        tasks={
            "csv": {
                "status": "running",
                "child": {"pid": 999, "pgid": 999},
            },
        },
    )
    code, out, _ = _run(["queue", "cancel", "csv"], cwd=repo)
    assert code == 0
    assert "is marked running, but the worker is not running." in out
    assert "otto queue run --concurrent N" in out
    assert [task.id for task in load_queue(repo)] == ["csv"]
    assert not (repo / COMMANDS_FILE).exists()


def test_queue_cancel_with_watcher_describes_queued_task(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv"], cwd=repo)
    now = _fresh_iso_now()
    _write_watcher_state(
        repo,
        watcher={
            "pid": os.getpid(),
            "pgid": os.getpid(),
            "started_at": now,
            "heartbeat": now,
        },
        tasks={"csv": {"status": "queued"}},
    )
    monkeypatch.setattr(cli_queue_module.os, "kill", lambda pid, sig: None)

    code, out, _ = _run(["queue", "cancel", "csv"], cwd=repo)

    assert code == 0
    assert "Cancel queued; watcher will remove from queue." in out
    assert [task.id for task in load_queue(repo)] == ["csv"]
    cmds = _read_queue_commands(repo)
    assert len(cmds) == 1
    assert cmds[0]["cmd"] == "cancel"
    assert cmds[0]["id"] == "csv"
    assert cmds[0]["schema_version"] == 1
    assert cmds[0]["command_id"].startswith("queue-cmd-")


def test_queue_cancel_with_watcher_describes_running_task(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv"], cwd=repo)
    now = _fresh_iso_now()
    _write_watcher_state(
        repo,
        watcher={
            "pid": os.getpid(),
            "pgid": os.getpid(),
            "started_at": now,
            "heartbeat": now,
        },
        tasks={"csv": {"status": "running"}},
    )
    monkeypatch.setattr(cli_queue_module.os, "kill", lambda pid, sig: None)

    code, out, _ = _run(["queue", "cancel", "csv"], cwd=repo)

    assert code == 0
    assert "Cancel queued; watcher will signal the task." in out
    assert [task.id for task in load_queue(repo)] == ["csv"]
    cmds = _read_queue_commands(repo)
    assert len(cmds) == 1
    assert cmds[0]["cmd"] == "cancel"
    assert cmds[0]["id"] == "csv"
    assert cmds[0]["schema_version"] == 1
    assert cmds[0]["command_id"].startswith("queue-cmd-")


def test_queue_cancel_with_watcher_reports_terminating_task(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv"], cwd=repo)
    now = _fresh_iso_now()
    _write_watcher_state(
        repo,
        watcher={
            "pid": os.getpid(),
            "pgid": os.getpid(),
            "started_at": now,
            "heartbeat": now,
        },
        tasks={"csv": {"status": "terminating"}},
    )
    monkeypatch.setattr(cli_queue_module.os, "kill", lambda pid, sig: None)

    code, out, _ = _run(["queue", "cancel", "csv"], cwd=repo)

    assert code == 0
    assert "Cancel already in progress." in out
    assert not (repo / COMMANDS_FILE).exists()


def test_queue_cancel_with_watcher_refuses_finished_task(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv"], cwd=repo)
    now = _fresh_iso_now()
    _write_watcher_state(
        repo,
        watcher={
            "pid": os.getpid(),
            "pgid": os.getpid(),
            "started_at": now,
            "heartbeat": now,
        },
        tasks={"csv": {"status": "done"}},
    )
    monkeypatch.setattr(cli_queue_module.os, "kill", lambda pid, sig: None)

    code, out, _ = _run(["queue", "cancel", "csv"], cwd=repo)

    assert code == 2
    assert "task csv is done; nothing to cancel." in out
    assert not (repo / COMMANDS_FILE).exists()


def test_queue_dashboard_no_active_watcher_errors(tmp_path: Path):
    repo = init_repo(tmp_path)

    code, out, _ = _run(["queue", "dashboard"], cwd=repo)

    assert code == 1
    assert "No active queue watcher found." in out
    assert "otto queue run --concurrent N" in out


def test_queue_dashboard_help_shows_examples(tmp_path: Path):
    repo = init_repo(tmp_path)

    code, out, _ = _run(["queue", "dashboard", "--help"], cwd=repo)

    assert code == 0
    assert "otto queue dashboard" in out
    assert "live queue dashboard" in out


def test_queue_run_rejects_zero_concurrency(tmp_path: Path):
    repo = init_repo(tmp_path)

    code, out, _ = _run(["queue", "run", "--concurrent", "0"], cwd=repo)

    assert code == 2
    assert "Invalid value for '--concurrent'" in out


def test_queue_ls_outside_git_repo_shows_clean_error(tmp_path: Path):
    code, out, _ = _run(["queue", "ls"], cwd=tmp_path)

    assert code == 2
    assert "Not a git repository" in out
    assert "Traceback" not in out


def test_queue_run_outside_git_repo_shows_clean_error(tmp_path: Path):
    code, out, _ = _run(["queue", "run", "--no-dashboard", "--exit-when-empty"], cwd=tmp_path)

    assert code == 2
    assert "Not a git repository" in out
    assert "Traceback" not in out


def test_queue_run_reports_malformed_otto_yaml_cleanly(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "otto.yaml").write_text("default_branch: [\n")

    code, out, _ = _run(["queue", "run", "--no-dashboard", "--exit-when-empty"], cwd=repo)

    assert code == 2
    assert "otto.yaml" in out
    assert "Traceback" not in out


def test_queue_resume_help_shows_examples(tmp_path: Path):
    repo = init_repo(tmp_path)

    code, out, _ = _run(["queue", "resume", "--help"], cwd=repo)

    assert code == 0
    assert "otto queue resume" in out
    assert "--select" in out
    assert "labels,due" in out


def test_queue_resume_defaults_to_resumable_interrupted_tasks(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "labels"], cwd=repo)
    _run(["queue", "certify", "release"], cwd=repo)
    session_id = "2026-04-22-010203-abc123"
    paths.ensure_session_scaffold(repo / ".worktrees" / "labels", session_id)
    paths.session_checkpoint(repo / ".worktrees" / "labels", session_id).write_text(
        json.dumps({"status": "paused", "updated_at": "2026-04-22T01:02:03Z"})
    )
    _write_watcher_state(
        repo,
        watcher=None,
        tasks={
            "labels": {
                "status": "terminating",
                "terminal_status": "interrupted",
                "failure_reason": "interrupted by watcher shutdown; resume available",
            },
            "release": {"status": "interrupted"},
        },
    )

    code, out, _ = _run(["queue", "resume"], cwd=repo)

    assert code == 0
    assert "Marked labels to resume" in out
    tasks = load_queue(repo)
    assert [task.id for task in tasks] == ["labels", "release"]
    cmds = _read_queue_commands(repo)
    assert len(cmds) == 1
    assert cmds[0]["cmd"] == "resume"
    assert cmds[0]["id"] == "labels"
    assert cmds[0]["schema_version"] == 1
    assert cmds[0]["command_id"].startswith("queue-cmd-")


def test_queue_resume_explicit_task_errors_without_checkpoint(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "labels"], cwd=repo)
    _write_watcher_state(
        repo,
        watcher=None,
        tasks={"labels": {"status": "interrupted"}},
    )

    code, out, _ = _run(["queue", "resume", "labels"], cwd=repo)

    assert code == 2
    assert "cannot be resumed from checkpoint" in out
    assert "checkpoint missing" in out


def test_queue_resume_explicit_failed_task_with_checkpoint(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "labels"], cwd=repo)
    session_id = "2026-04-22-010203-abc123"
    paths.ensure_session_scaffold(repo / ".worktrees" / "labels", session_id)
    paths.session_checkpoint(repo / ".worktrees" / "labels", session_id).write_text(
        json.dumps({"status": "in_progress", "updated_at": "2026-04-22T01:02:03Z"})
    )
    _write_watcher_state(
        repo,
        watcher=None,
        tasks={
            "labels": {
                "status": "failed",
                "failure_reason": "timed out after 1800s (limit 1800s)",
            }
        },
    )

    code, out, _ = _run(["queue", "resume", "labels"], cwd=repo)

    assert code == 0
    assert "Marked labels to resume" in out
    cmds = _read_queue_commands(repo)
    assert len(cmds) == 1
    assert cmds[0]["cmd"] == "resume"
    assert cmds[0]["id"] == "labels"


def test_queue_resume_explicit_failed_task_rejects_stale_checkpoint(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "labels"], cwd=repo)
    session_id = "2026-04-22-010203-abc123"
    paths.ensure_session_scaffold(repo / ".worktrees" / "labels", session_id)
    paths.session_checkpoint(repo / ".worktrees" / "labels", session_id).write_text(
        json.dumps({
            "status": "in_progress",
            "updated_at": "2026-04-22T01:02:03Z",
            "git_sha": "stale-sha",
        })
    )
    _write_watcher_state(
        repo,
        watcher=None,
        tasks={
            "labels": {
                "status": "failed",
                "failure_reason": "timed out after 1800s (limit 1800s)",
            }
        },
    )

    code, out, _ = _run(["queue", "resume", "labels"], cwd=repo)

    assert code == 2
    assert "cannot be resumed from checkpoint" in out
    assert "checkpoint is stale: git HEAD changed" in out
    assert not (repo / COMMANDS_FILE).exists()


def test_queue_resume_select_uses_picker(monkeypatch, tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "labels"], cwd=repo)
    _run(["queue", "build", "due"], cwd=repo)
    for task_id, session_id in [("labels", "2026-04-22-010203-abc123"), ("due", "2026-04-22-010204-def456")]:
        paths.ensure_session_scaffold(repo / ".worktrees" / task_id, session_id)
        paths.session_checkpoint(repo / ".worktrees" / task_id, session_id).write_text(
            json.dumps({"status": "paused", "updated_at": "2026-04-22T01:02:03Z"})
        )
    _write_watcher_state(
        repo,
        watcher=None,
        tasks={
            "labels": {"status": "interrupted"},
            "due": {"status": "interrupted"},
        },
    )
    monkeypatch.setattr(
        "otto.queue.dashboard.select_resume_tasks",
        lambda project_dir, tasks: ["due"],
    )

    code, out, _ = _run(["queue", "resume", "--select"], cwd=repo)

    assert code == 0
    assert "Marked due to resume" in out
    tasks = load_queue(repo)
    assert [task.id for task in tasks] == ["due", "labels"]
    cmds = _read_queue_commands(repo)
    assert len(cmds) == 1
    assert cmds[0]["cmd"] == "resume"
    assert cmds[0]["id"] == "due"
    assert cmds[0]["schema_version"] == 1
    assert cmds[0]["command_id"].startswith("queue-cmd-")


def test_queue_rm_rejects_unknown_task(tmp_path: Path):
    repo = init_repo(tmp_path)
    code, out, _ = _run(["queue", "rm", "nonexistent"], cwd=repo)
    assert code == 2
    assert "No such task" in out


# ---------- file format integrity ----------


def test_queue_yml_uses_schema_v1(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "test"], cwd=repo)
    import yaml
    raw = yaml.safe_load((repo / QUEUE_FILE).read_text())
    assert raw["schema_version"] == 1
    assert isinstance(raw["tasks"], list)


def test_resolve_otto_bin_fallback_returns_argv(monkeypatch, tmp_path: Path):
    fake_python = tmp_path / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text("")
    monkeypatch.setattr(cli_queue_module.sys, "executable", str(fake_python))
    assert cli_queue_module._resolve_otto_bin() == [str(fake_python), "-m", "otto.cli"]


def test_queue_run_help_shows_dashboard_mouse_and_exit_flags(tmp_path: Path):
    repo = init_repo(tmp_path)
    code, out, _ = _run(["queue", "run", "--help"], cwd=repo)
    help_text = " ".join(out.split()).replace("in- flight", "in-flight")
    assert code == 0
    assert "--dashboard-mouse" in help_text
    assert "loses terminal copy in most terminals" in help_text
    assert "--exit-when-empty" in help_text
    assert "Exit cleanly once the queue has no queued or in-flight tasks" in help_text
