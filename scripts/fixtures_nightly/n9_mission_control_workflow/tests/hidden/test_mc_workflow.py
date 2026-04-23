from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
HISTORY_PATH = REPO_ROOT / "otto_logs" / "cross-sessions" / "history.jsonl"
LIVE_DIR = REPO_ROOT / "otto_logs" / "cross-sessions" / "runs" / "live"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def test_mission_control_workflow_writes_coherent_terminal_history() -> None:
    rows = [
        row
        for row in _read_jsonl(HISTORY_PATH)
        if row.get("history_kind", "terminal_snapshot") == "terminal_snapshot"
    ]
    assert len(rows) >= 4

    required_keys = {
        "schema_version",
        "run_id",
        "domain",
        "run_type",
        "command",
        "terminal_outcome",
        "intent_path",
        "manifest_path",
        "summary_path",
        "primary_log_path",
        "artifacts",
        "dedupe_key",
    }
    for row in rows:
        assert required_keys.issubset(row)
        assert row["schema_version"] == 2
        assert row["dedupe_key"] == f"terminal_snapshot:{row['run_id']}"
        assert not (row.get("terminal_outcome") is None and row.get("status") == "running")

    atomic_builds = [row for row in rows if row.get("domain") == "atomic" and row.get("run_type") == "build"]
    assert atomic_builds
    cancelled_build = atomic_builds[0]
    assert cancelled_build["terminal_outcome"] == "cancelled"

    queue_rows = [row for row in rows if row.get("domain") == "queue"]
    assert len(queue_rows) >= 2
    assert all(row["terminal_outcome"] in {"success", "failure", "cancelled"} for row in queue_rows)

    merge_rows = [row for row in rows if row.get("domain") == "merge" and row.get("run_type") == "merge"]
    assert merge_rows
    assert merge_rows[0]["terminal_outcome"] in {"success", "failure", "cancelled"}

    for row in rows:
        for key in ("manifest_path", "summary_path", "primary_log_path"):
            value = row.get(key)
            if value:
                assert Path(value).exists(), f"missing {key}: {value}"


def test_live_registry_records_are_not_backed_by_live_writers() -> None:
    if not LIVE_DIR.exists():
        raise AssertionError("live registry directory missing")

    for path in sorted(LIVE_DIR.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        pid = record.get("writer", {}).get("pid")
        if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        raise AssertionError(f"writer for {record.get('run_id')} is still alive: pid={pid}")
