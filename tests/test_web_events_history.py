from __future__ import annotations

import json
from pathlib import Path

from otto.mission_control.events import append_event, events_path
from otto.runs.history import append_history_snapshot, build_terminal_snapshot

from tests._web_mc_helpers import _client, _init_repo


def test_web_records_queue_events_and_exposes_operator_timeline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    client = _client(repo)
    response = client.post(
        "/api/queue/build",
        json={
            "intent": "add saved searches",
            "as": "saved-searches",
            "extra_args": ["--provider", "codex", "--effort", "high"],
        },
    )

    assert response.status_code == 200
    state = client.get("/api/state").json()
    event = state["events"]["items"][0]
    assert state["events"]["total_count"] == 1
    assert event["kind"] == "queue.build"
    assert event["severity"] == "success"
    assert event["task_id"] == "saved-searches"
    assert event["message"] == "queued saved-searches"

    endpoint = client.get("/api/events?limit=1").json()
    assert endpoint["items"] == state["events"]["items"][:1]
    assert endpoint["path"].endswith("otto_logs/mission-control/events.jsonl")

def test_web_events_endpoint_reports_malformed_rows_without_breaking_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    path = events_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('not-json\n{"schema_version":[],"message":"old event"}\n', encoding="utf-8")
    append_event(repo, kind="watcher.stop.skipped", message="watcher is not running")

    state = _client(repo).get("/api/state").json()

    assert state["events"]["malformed_count"] == 1
    assert state["events"]["total_count"] == 2
    assert state["events"]["items"][0]["message"] == "watcher is not running"

def test_web_events_tail_preserves_boundary_aligned_rows(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setattr("otto.mission_control.events.MAX_EVENT_TAIL_BYTES", 120)
    path = events_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    first = json.dumps({"schema_version": 1, "event_id": "a", "message": "first", "kind": "test", "severity": "info"})
    second = json.dumps({"schema_version": 1, "event_id": "b", "message": "second", "kind": "test", "severity": "info"})
    path.write_text(first + "\n" + second + "\n", encoding="utf-8")
    monkeypatch.setattr("otto.mission_control.events.MAX_EVENT_TAIL_BYTES", len(second) + 1)

    events = _client(repo).get("/api/events").json()

    assert events["truncated"] is True
    assert events["items"][0]["message"] == "second"
    assert events["total_count"] == 1

def test_web_history_detail_recovers_provider_from_manifest_argv(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    manifest_path = repo / "otto_logs" / "queue" / "hello-web" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"argv": ["build", "hello", "--provider", "codex", "--effort", "high"]}),
        encoding="utf-8",
    )
    append_history_snapshot(
        repo,
        build_terminal_snapshot(
            run_id="run-history",
            domain="queue",
            run_type="queue",
            command="build hello",
            intent_meta={"summary": "hello"},
            status="done",
            terminal_outcome="success",
            timing={"finished_at": "2026-04-24T00:00:00Z"},
            metrics={"cost_usd": 0.0},
            artifacts={"manifest_path": str(manifest_path)},
            source={"resumable": True},
            identity={"queue_task_id": "hello-web"},
        ),
    )

    detail = _client(repo).get("/api/runs/run-history").json()

    assert detail["provider"] == "codex"
    assert detail["reasoning_effort"] == "high"

def test_web_history_usage_reads_merge_summary_extra_artifact(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    summary_path = repo / "otto_logs" / "sessions" / "merge-cert" / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {
                "breakdown": {
                    "certify": {
                        "input_tokens": 2048,
                        "cached_input_tokens": 1024,
                        "output_tokens": 300,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    append_history_snapshot(
        repo,
        build_terminal_snapshot(
            run_id="merge-run",
            domain="merge",
            run_type="merge",
            command="merge",
            intent_meta={"summary": "merge 1 branch"},
            status="done",
            terminal_outcome="success",
            timing={"finished_at": "2026-04-24T00:00:00Z"},
            artifacts={"extra_log_paths": [str(summary_path)]},
            identity={"merge_id": "merge-run"},
        ),
    )

    state = _client(repo).get("/api/state?type=merge").json()

    assert state["history"]["items"][0]["cost_display"] == "2.3K tokens"

def test_web_project_stats_include_claude_cache_token_fields(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    summary_path = repo / "otto_logs" / "sessions" / "claude-build" / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {
                "breakdown": {
                    "build": {
                        "input_tokens": 51,
                        "cache_creation_input_tokens": 84864,
                        "cache_read_input_tokens": 2434281,
                        "output_tokens": 25347,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    append_history_snapshot(
        repo,
        build_terminal_snapshot(
            run_id="claude-build",
            domain="queue",
            run_type="queue",
            command="build",
            intent_meta={"summary": "build with claude"},
            status="done",
            terminal_outcome="success",
            timing={"finished_at": "2026-04-24T00:00:00Z"},
            artifacts={"summary_path": str(summary_path)},
            identity={"queue_task_id": "claude-build"},
        ),
    )

    state = _client(repo).get("/api/state").json()

    usage = state["history"]["items"][0]["token_usage"]
    assert usage["cache_creation_input_tokens"] == 84864
    assert usage["cache_read_input_tokens"] == 2434281
    assert state["project_stats"]["total_tokens"] == 2544543
    assert state["project_stats"]["token_display"] == "110.3K fresh + 2.4M cached · 97% hit"
