"""Tests for otto/cli_queue.py — Phase 2.3-2.6 CLI surface."""

from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

import otto.cli_queue as cli_queue_module
from otto.cli import main
from otto import paths
from otto.queue.schema import (
    COMMANDS_FILE,
    QUEUE_FILE,
    load_queue,
    write_state,
)
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
    assert tasks[0].resolved_intent == "a product"


def test_queue_improve_target_focus_not_set(tmp_path: Path):
    """For target subcommand, the arg goes to `target` not `focus`."""
    repo = init_repo(tmp_path)
    (repo / "intent.md").write_text("a product")
    code, out, _ = _run(["queue", "improve", "target", "latency < 100ms"], cwd=repo)
    assert code == 0, out
    tasks = load_queue(repo)
    assert tasks[0].target == "latency < 100ms"
    assert tasks[0].focus is None


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


def test_queue_show_existing_task(tmp_path: Path):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv export"], cwd=repo)
    code, out, _ = _run(["queue", "show", "csv-export"], cwd=repo)
    assert code == 0
    assert "csv-export" in out
    assert "queued" in out
    assert "Resumable: True" in out


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
            "pid": 4242,
            "pgid": 4242,
            "started_at": now,
            "heartbeat": now,
        },
    )
    monkeypatch.setattr(cli_queue_module.os, "kill", lambda pid, sig: None)
    code, out, _ = _run(["queue", "rm", "csv"], cwd=repo)
    assert code == 0
    assert "Remove queued; watcher will apply within ~1s." in out
    assert [task.id for task in load_queue(repo)] == ["csv"]
    cmds_path = repo / COMMANDS_FILE
    assert cmds_path.exists()
    cmds = [json.loads(line) for line in cmds_path.read_text().splitlines() if line.strip()]
    assert len(cmds) == 1
    assert cmds[0]["cmd"] == "remove"
    assert cmds[0]["id"] == "csv"


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
    assert "--break-lock --concurrent N" in out
    assert [task.id for task in load_queue(repo)] == ["csv"]
    assert not (repo / COMMANDS_FILE).exists()


def test_queue_cancel_with_watcher_describes_queued_task(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv"], cwd=repo)
    now = _fresh_iso_now()
    _write_watcher_state(
        repo,
        watcher={
            "pid": 4242,
            "pgid": 4242,
            "started_at": now,
            "heartbeat": now,
        },
        tasks={"csv": {"status": "queued"}},
    )
    monkeypatch.setattr(cli_queue_module.os, "kill", lambda pid, sig: None)

    code, out, _ = _run(["queue", "cancel", "csv"], cwd=repo)

    assert code == 0
    assert "Cancel queued; watcher will remove from queue." in out


def test_queue_cancel_with_watcher_describes_running_task(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv"], cwd=repo)
    now = _fresh_iso_now()
    _write_watcher_state(
        repo,
        watcher={
            "pid": 4242,
            "pgid": 4242,
            "started_at": now,
            "heartbeat": now,
        },
        tasks={"csv": {"status": "running"}},
    )
    monkeypatch.setattr(cli_queue_module.os, "kill", lambda pid, sig: None)

    code, out, _ = _run(["queue", "cancel", "csv"], cwd=repo)

    assert code == 0
    assert "Cancel queued; watcher will signal the task." in out


def test_queue_cancel_with_watcher_reports_terminating_task(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path)
    _run(["queue", "build", "csv"], cwd=repo)
    now = _fresh_iso_now()
    _write_watcher_state(
        repo,
        watcher={
            "pid": 4242,
            "pgid": 4242,
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
            "pid": 4242,
            "pgid": 4242,
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
