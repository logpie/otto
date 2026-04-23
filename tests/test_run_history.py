from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path

from otto import paths
from otto.history import append_history_entry
from otto.runs.history import append_history_snapshot, read_history_rows


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

