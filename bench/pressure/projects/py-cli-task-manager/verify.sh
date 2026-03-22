#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py <<'PY'
import datetime as dt
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile

failures = 0
ROOT = pathlib.Path(".")


def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")


def find_cli():
    candidates = [
        [sys.executable, "task_manager.py"],
        [sys.executable, "taskmanager.py"],
        [sys.executable, "cli.py"],
        [sys.executable, "-m", "task_manager"],
        [sys.executable, "-m", "taskmanager"],
        [sys.executable, "-m", "cli"],
    ]
    for cmd in candidates:
        target = ROOT / cmd[-1]
        if cmd[1:2] != ["-m"] and not target.exists():
            continue
        result = subprocess.run(cmd + ["--help"], capture_output=True, text=True)
        if result.returncode == 0:
            return cmd
    raise AssertionError("could not find CLI entry point")


CLI = find_cli()
db_fd, db_path = tempfile.mkstemp(prefix="taskmanager-", suffix=".db")
os.close(db_fd)
ENV = dict(os.environ, TASKMANAGER_DB=db_path)


def run_cli(*args, expect_ok=True):
    result = subprocess.run(CLI + list(args), capture_output=True, text=True, env=ENV)
    if expect_ok and result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result


def connect():
    return sqlite3.connect(db_path)


def check_help():
    result = run_cli("--help")
    text = (result.stdout + result.stderr).lower()
    for token in ("add", "list", "done", "delete", "search"):
        assert token in text


def check_add_and_schema():
    run_cli("add", "Verify persistence", "--priority", "high", "--due", "2030-01-01")
    with connect() as conn:
        columns = {row[1] for row in conn.execute("pragma table_info(tasks)")}
        rows = conn.execute("select title, priority, status, due_date from tasks").fetchall()
    assert {"id", "title", "notes", "priority", "status", "due_date", "created_at", "completed_at"} <= columns
    assert rows[0][:3] == ("Verify persistence", "high", "pending")
    assert rows[0][3] == "2030-01-01"


def check_done_updates_status():
    run_cli("done", "1")
    with connect() as conn:
        status, completed_at = conn.execute("select status, completed_at from tasks where id = 1").fetchone()
    assert status == "done"
    assert completed_at
    result = run_cli("list", "--status", "done")
    assert "Verify persistence" in result.stdout


def check_overdue_filter():
    overdue = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    run_cli("add", "Old task", "--due", overdue)
    result = run_cli("list", "--status", "overdue")
    assert "Old task" in result.stdout
    pending = run_cli("list", "--status", "pending")
    assert "Verify persistence" not in pending.stdout


def check_search_matches_notes():
    with connect() as conn:
        conn.execute("update tasks set notes = ? where id = 2", ("milk bread eggs",))
        conn.commit()
    result = run_cli("search", "bread")
    output = (result.stdout + result.stderr).lower()
    assert "old task" in output or "bread" in output


def check_delete_and_missing_delete():
    run_cli("delete", "2")
    with connect() as conn:
        remaining = conn.execute("select count(*) from tasks where id = 2").fetchone()[0]
    assert remaining == 0
    result = run_cli("delete", "9999", expect_ok=False)
    assert result.returncode != 0
    assert any(token in (result.stdout + result.stderr).lower() for token in ("not found", "no such", "missing"))


report("CLI advertises required subcommands", check_help)
report("add persists tasks with the required schema", check_add_and_schema)
report("done marks tasks complete in SQLite and list output", check_done_updates_status)
report("overdue listing reflects due-date status", check_overdue_filter)
report("search matches persisted task notes", check_search_matches_notes)
report("delete removes rows and surfaces missing-task errors", check_delete_and_missing_delete)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
