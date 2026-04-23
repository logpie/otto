from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path
from unittest.mock import patch

from otto import paths
from otto.history import append_history_entry
from otto.merge.orchestrator import _append_merge_history
from otto.merge.state import MergeState
from otto.pipeline import _append_session_history
from otto.queue.runner import Runner, RunnerConfig
from otto.queue.schema import QueueTask
from otto.runs.history import append_history_snapshot, load_project_history_rows, read_history_rows


def _append_history(project_dir: str, index: int) -> None:
    append_history_snapshot(
        Path(project_dir),
        {
            "run_id": f"run-{index}",
            "status": "done",
            "terminal_outcome": "success",
        },
        strict=True,
    )


def test_append_history_snapshot_sets_dedupe_key(tmp_path: Path) -> None:
    row = append_history_snapshot(
        tmp_path,
        {"run_id": "run-1", "status": "done", "terminal_outcome": "success"},
        strict=True,
    )
    assert row["schema_version"] == 2
    assert row["dedupe_key"] == "terminal_snapshot:run-1"
    lines = paths.history_jsonl(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["run_id"] == "run-1"


def test_append_history_snapshot_is_strict_by_default(tmp_path: Path) -> None:
    try:
        append_history_snapshot(tmp_path, {"status": "done"}, strict=True)
    except ValueError as exc:
        assert "run_id" in str(exc)
    else:
        raise AssertionError("expected strict append to reject missing run_id")


def test_history_reader_skips_malformed_lines(tmp_path: Path) -> None:
    history_path = paths.history_jsonl(tmp_path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        '{"run_id":"good-1","schema_version":2,"history_kind":"terminal_snapshot"}\n'
        '{bad json}\n'
        '{"run_id":"good-2","schema_version":2,"history_kind":"terminal_snapshot"}\n',
        encoding="utf-8",
    )
    rows = read_history_rows(history_path)
    assert [row["run_id"] for row in rows] == ["good-1", "good-2"]


def test_append_history_entry_writes_v2_snapshot(tmp_path: Path) -> None:
    row = append_history_entry(
        tmp_path,
        {
            "run_id": "run-2",
            "command": "improve.bugs",
            "intent": "fix flaky auth tests",
            "passed": False,
        },
    )
    assert row["schema_version"] == 2
    assert row["command"] == "improve bugs"
    assert row["run_type"] == "improve"
    assert row["terminal_outcome"] == "failure"


def test_history_append_concurrent_processes(tmp_path: Path) -> None:
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_append_history, args=(str(tmp_path), index)) for index in range(6)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=5)
        assert proc.exitcode == 0
    rows = read_history_rows(paths.history_jsonl(tmp_path))
    assert sorted(row["run_id"] for row in rows) == [f"run-{index}" for index in range(6)]


def test_load_project_history_rows_limit_hint_preserves_cross_source_dedupe(tmp_path: Path) -> None:
    append_history_entry(
        tmp_path,
        {
            "run_id": "shared-run",
            "command": "build",
            "intent": "current snapshot",
            "passed": True,
            "status": "done",
            "terminal_outcome": "success",
            "timestamp": "2026-04-23T12:00:00Z",
        },
    )
    for index in range(1, 21):
        append_history_entry(
            tmp_path,
            {
                "run_id": f"run-{index}",
                "command": "build",
                "intent": f"intent {index}",
                "passed": True,
                "status": "done",
                "terminal_outcome": "success",
                "timestamp": f"2026-04-23T12:{index:02d}:00Z",
            },
        )

    legacy_path = tmp_path / "otto_logs" / "run-history.jsonl"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        "\n".join(
            [
                json.dumps({"build_id": "legacy-run", "timestamp": "2026-04-23T11:58:00Z", "intent": "legacy row"}),
                json.dumps({"build_id": "shared-run", "timestamp": "2026-04-23T11:59:00Z", "intent": "legacy duplicate"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rows = load_project_history_rows(tmp_path, limit_hint=5)

    shared = [row for row in rows if row.get("run_id") == "shared-run"]
    assert len(shared) == 1
    assert shared[0]["intent"] == "current snapshot"
    assert any(row.get("build_id") == "legacy-run" for row in rows)


def test_cli_history_loader_threads_limit_hint(tmp_path: Path) -> None:
    from otto.cli_logs import _load_history_entries

    with patch("otto.cli_logs.load_project_history_rows", return_value=[]) as loader:
        _load_history_entries(tmp_path, limit_hint=17)

    assert loader.call_args.kwargs["limit_hint"] == 17


def test_terminal_history_writers_emit_v2_snapshots_for_all_domains(tmp_path: Path) -> None:
    _append_session_history(
        tmp_path,
        run_id="build-run",
        command="build",
        certifier_mode="thorough",
        intent="build feature",
        stories=[],
        passed=True,
        duration_s=12.3,
        total_cost_usd=0.11,
        certifier_cost_usd=0.01,
        rounds=1,
        started_at="2026-04-23T12:00:00Z",
        finished_at="2026-04-23T12:00:12Z",
        branch="build/feature",
        spec_path=str(paths.session_dir(tmp_path, "build-run") / "spec.md"),
    )
    _append_session_history(
        tmp_path,
        run_id="improve-run",
        command="improve bugs",
        certifier_mode="fast",
        intent="fix regressions",
        stories=[],
        passed=False,
        duration_s=7.0,
        total_cost_usd=0.22,
        certifier_cost_usd=0.05,
        rounds=2,
        started_at="2026-04-23T12:10:00Z",
        finished_at="2026-04-23T12:10:07Z",
        branch="improve/bugs",
    )
    _append_session_history(
        tmp_path,
        run_id="certify-run",
        command="certify",
        certifier_mode="standard",
        intent="verify release",
        stories=[],
        passed=True,
        duration_s=3.4,
        total_cost_usd=0.33,
        certifier_cost_usd=0.33,
        rounds=1,
        started_at="2026-04-23T12:20:00Z",
        finished_at="2026-04-23T12:20:03Z",
        branch="main",
    )

    runner = Runner(tmp_path, RunnerConfig(), otto_bin="otto")
    task = QueueTask(
        id="task-1",
        command_argv=["build", "queued work"],
        resolved_intent="queued work",
        branch="build/task-1",
        worktree=".worktrees/task-1",
        resumable=True,
    )
    runner._append_queue_history_snapshot(
        task,
        {
            "attempt_run_id": "queue-run",
            "child_run_id": "queue-run",
            "started_at": "2026-04-23T12:30:00Z",
            "finished_at": "2026-04-23T12:30:09Z",
            "cost_usd": 0.44,
            "duration_s": 9.0,
        },
        run_id="queue-run",
        status="done",
        terminal_outcome="success",
    )

    _append_merge_history(
        tmp_path,
        MergeState(
            merge_id="merge-run",
            started_at="2026-04-23T12:40:00Z",
            finished_at="2026-04-23T12:40:15Z",
            status="failed",
            terminal_outcome="failure",
            target="main",
            branches_in_order=["feature/a", "feature/b"],
        ),
    )

    rows = read_history_rows(paths.history_jsonl(tmp_path))
    by_run_id = {row["run_id"]: row for row in rows}

    assert set(by_run_id) == {"build-run", "improve-run", "certify-run", "queue-run", "merge-run"}
    assert all(row["schema_version"] == 2 for row in rows)
    assert all(row["history_kind"] == "terminal_snapshot" for row in rows)
    assert all(row["dedupe_key"] == f"terminal_snapshot:{row['run_id']}" for row in rows)

    assert by_run_id["build-run"]["artifacts"]["primary_log_path"].endswith("/build-run/build/narrative.log")
    assert by_run_id["build-run"]["intent_path"].endswith("/build-run/intent.txt")
    assert by_run_id["build-run"]["spec_path"].endswith("/build-run/spec.md")
    assert by_run_id["improve-run"]["artifacts"]["primary_log_path"].endswith("/improve-run/improve/narrative.log")
    assert by_run_id["certify-run"]["mode"] == "standard"
    assert by_run_id["certify-run"]["artifacts"]["checkpoint_path"] is None
    assert by_run_id["queue-run"]["domain"] == "queue"
    assert by_run_id["queue-run"]["queue_task_id"] == "task-1"
    assert by_run_id["queue-run"]["artifacts"]["primary_log_path"].endswith("/queue-run/build/narrative.log")
    assert by_run_id["merge-run"]["domain"] == "merge"
    assert by_run_id["merge-run"]["artifacts"]["extra_log_paths"][0].endswith("/merge-run/state.json")
