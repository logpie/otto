from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
HISTORY_PATH = REPO_ROOT / "otto_logs" / "cross-sessions" / "history.jsonl"
LIVE_DIR = REPO_ROOT / "otto_logs" / "cross-sessions" / "runs" / "live"
PHASE_LOG_PATH = REPO_ROOT / "otto_logs" / "cross-sessions" / "mission-control-pilot-phases.jsonl"


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


def test_realistic_session_writes_expected_terminal_history() -> None:
    rows = [
        row
        for row in _read_jsonl(HISTORY_PATH)
        if row.get("history_kind", "terminal_snapshot") == "terminal_snapshot"
    ]
    assert len(rows) >= 3

    build_rows = [row for row in rows if row.get("domain") == "atomic" and row.get("run_type") == "build"]
    queue_rows = [row for row in rows if row.get("domain") == "queue"]
    merge_rows = [row for row in rows if row.get("domain") == "merge" and row.get("run_type") == "merge"]

    assert len(build_rows) == 1
    assert len(queue_rows) >= 2
    assert len(merge_rows) == 1

    assert build_rows[0]["terminal_outcome"] in {"success", "cancelled"}
    assert merge_rows[0]["terminal_outcome"] == "success"
    queue_outcomes = [row["terminal_outcome"] for row in queue_rows]
    assert "cancelled" in queue_outcomes
    assert "success" in queue_outcomes
    assert sum(1 for row in rows if row["terminal_outcome"] == "cancelled") >= 1
    assert sum(1 for row in rows if row["terminal_outcome"] == "success") >= 2

    cancelled_row = next(row for row in queue_rows if row["terminal_outcome"] == "cancelled")
    assert cancelled_row["terminal_outcome"] != "failure"

    for row in rows:
        assert row["schema_version"] == 2
        assert row["dedupe_key"] == f"terminal_snapshot:{row['run_id']}"
        for key in ("manifest_path", "summary_path", "primary_log_path"):
            value = row.get(key)
            assert value, f"missing {key} for {row['run_id']}"
            assert Path(value).exists(), f"missing {key}: {value}"


def test_live_records_are_gced_or_terminal_only() -> None:
    if not LIVE_DIR.exists():
        return

    for path in sorted(LIVE_DIR.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record.get("status") in {"done", "failed", "cancelled", "removed"}, path.name


def test_mission_control_phase_log_captures_expected_stages() -> None:
    phases = _read_jsonl(PHASE_LOG_PATH)
    assert phases, "missing Mission Control phase log"

    phase_by_name = {phase["phase"]: phase for phase in phases}
    for required in (
        "build-running",
        "history-pre-merge",
        "history-cancelled-detail",
        "merge-complete",
    ):
        assert required in phase_by_name, f"missing phase snapshot: {required}"
    assert "build-done" in phase_by_name or "build-cancelled" in phase_by_name

    build_running = phase_by_name["build-running"]
    assert build_running["focus"] == "detail"
    assert any(row["domain"] == "atomic" and row["status"] == "running" for row in build_running["live_rows"])
    assert sum(1 for row in build_running["live_rows"] if row["domain"] == "queue" and row["status"] in {"queued", "starting", "running"}) >= 2
    assert len(build_running["detail"]["log_paths"]) >= 2

    history_pre_merge = phase_by_name["history-pre-merge"]
    outcomes = [row["terminal_outcome"] for row in history_pre_merge["history_rows"]]
    assert "cancelled" in outcomes
    assert "success" in outcomes

    cancelled_detail = phase_by_name["history-cancelled-detail"]
    assert cancelled_detail["focus"] == "detail"
    assert cancelled_detail["detail"]["status"] == "cancelled"

    merge_complete = phase_by_name["merge-complete"]
    assert any(row["domain"] == "merge" and row["terminal_outcome"] == "success" for row in merge_complete["history_rows"])
