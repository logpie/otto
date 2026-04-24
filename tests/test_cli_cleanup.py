from __future__ import annotations

import os
from pathlib import Path

from click.testing import CliRunner

from otto.cli import main
from otto.runs.registry import cleanup_live_record, make_run_record, read_live_records, write_record
from otto import paths


def _run(args: list[str], *, cwd: Path) -> tuple[int, str]:
    runner = CliRunner()
    saved_cwd = os.getcwd()
    os.chdir(cwd)
    try:
        result = runner.invoke(main, args, catch_exceptions=False)
    finally:
        os.chdir(saved_cwd)
    return result.exit_code, result.output


def test_cleanup_cli_removes_terminal_live_record_and_writes_tombstone(tmp_path: Path, monkeypatch) -> None:
    record = make_run_record(
        project_dir=tmp_path,
        run_id="atomic-run",
        domain="atomic",
        run_type="build",
        command="build",
        display_name="build",
        status="failed",
        cwd=tmp_path,
        adapter_key="atomic.build",
    )
    write_record(tmp_path, record)
    monkeypatch.setattr("otto.runs.registry.writer_identity_gone_or_stale", lambda writer: True)

    code, out = _run(["cleanup", "atomic-run"], cwd=tmp_path)

    assert code == 0, out
    assert read_live_records(tmp_path) == []
    tombstones = paths.run_gc_tombstones_jsonl(tmp_path).read_text(encoding="utf-8")
    assert "atomic-run" in tombstones


def test_cleanup_live_record_removes_abandoned_non_terminal(tmp_path: Path, monkeypatch) -> None:
    record = make_run_record(
        project_dir=tmp_path,
        run_id="atomic-run",
        domain="atomic",
        run_type="build",
        command="build",
        display_name="build",
        status="running",
        cwd=tmp_path,
        adapter_key="atomic.build",
    )
    write_record(tmp_path, record)
    monkeypatch.setattr("otto.runs.registry.writer_identity_gone_or_stale", lambda writer: True)

    removed = cleanup_live_record(tmp_path, "atomic-run")

    assert removed.status == "running"
    assert read_live_records(tmp_path) == []
    tombstones = paths.run_gc_tombstones_jsonl(tmp_path).read_text(encoding="utf-8")
    assert "atomic-run" in tombstones


def test_cleanup_live_record_rejects_terminal_run_while_writer_alive(tmp_path: Path, monkeypatch) -> None:
    record = make_run_record(
        project_dir=tmp_path,
        run_id="atomic-run",
        domain="atomic",
        run_type="build",
        command="build",
        display_name="build",
        status="failed",
        cwd=tmp_path,
        adapter_key="atomic.build",
    )
    write_record(tmp_path, record)
    monkeypatch.setattr("otto.runs.registry.writer_identity_gone_or_stale", lambda writer: False)

    try:
        cleanup_live_record(tmp_path, "atomic-run")
    except ValueError as exc:
        assert str(exc) == "writer still alive — wait for finalization"
    else:
        raise AssertionError("expected ValueError for live writer")


def test_cleanup_live_record_rejects_non_terminal_run_while_writer_alive(tmp_path: Path, monkeypatch) -> None:
    record = make_run_record(
        project_dir=tmp_path,
        run_id="atomic-run",
        domain="atomic",
        run_type="build",
        command="build",
        display_name="build",
        status="running",
        cwd=tmp_path,
        adapter_key="atomic.build",
    )
    write_record(tmp_path, record)
    monkeypatch.setattr("otto.runs.registry.writer_identity_gone_or_stale", lambda writer: False)

    try:
        cleanup_live_record(tmp_path, "atomic-run")
    except ValueError as exc:
        assert str(exc) == "writer still alive — wait for finalization"
    else:
        raise AssertionError("expected ValueError for live writer")


def test_cleanup_cli_rejects_writer_alive(tmp_path: Path, monkeypatch) -> None:
    record = make_run_record(
        project_dir=tmp_path,
        run_id="atomic-run",
        domain="atomic",
        run_type="build",
        command="build",
        display_name="build",
        status="failed",
        cwd=tmp_path,
        adapter_key="atomic.build",
    )
    write_record(tmp_path, record)
    monkeypatch.setattr("otto.runs.registry.writer_identity_gone_or_stale", lambda writer: False)

    code, out = _run(["cleanup", "atomic-run"], cwd=tmp_path)

    assert code == 2
    assert "writer still alive — wait for finalization" in out
