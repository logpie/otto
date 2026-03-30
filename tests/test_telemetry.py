"""Tests for otto.telemetry — JSONL event writer and dual-write."""

import json

from otto.telemetry import (
    AgentToolCall,
    AllDone,
    BatchCompleted,
    PhaseCompleted,
    PlanCreated,
    ResearchComplete,
    TaskFailed,
    TaskMerged,
    TaskStarted,
    Telemetry,
    VerifyCompleted,
)


class TestEventDataclasses:
    def test_task_started(self):
        e = TaskStarted(task_key="abc", task_id=1, prompt="Add hello")
        assert e.event == "task_started"
        assert e.task_key == "abc"

    def test_task_merged(self):
        e = TaskMerged(task_key="abc", cost_usd=0.50, diff_summary="3 files")
        assert e.event == "task_merged"
        assert e.cost_usd == 0.50

    def test_task_failed(self):
        e = TaskFailed(task_key="abc", error="tests failed")
        assert e.event == "task_failed"
        assert e.error == "tests failed"

    def test_verify_completed(self):
        e = VerifyCompleted(task_key="abc", passed=True, duration_s=5.0)
        assert e.event == "verify_completed"
        assert e.passed is True

    def test_phase_completed(self):
        e = PhaseCompleted(task_key="abc", phase="qa", status="done", time_s=5.0, cost_usd=0.25)
        assert e.event == "phase_completed"
        assert e.phase == "qa"
        assert e.cost_usd == 0.25

    def test_agent_tool_call(self):
        e = AgentToolCall(task_key="abc", name="Read", detail="src/main.py")
        assert e.event == "agent_tool"

    def test_research_complete(self):
        e = ResearchComplete(task_key="abc", query="how to X", summary="found Y")
        assert e.event == "research_complete"

    def test_batch_completed(self):
        e = BatchCompleted(batch_index=0, tasks_passed=2, tasks_failed=1)
        assert e.event == "batch_completed"

    def test_plan_created(self):
        e = PlanCreated(total_batches=3, total_tasks=5)
        assert e.event == "plan_created"

    def test_all_done(self):
        e = AllDone(
            total_passed=3,
            total_failed=1,
            total_missing_or_interrupted=2,
            total_cost=2.50,
        )
        assert e.event == "all_done"
        assert e.total_missing_or_interrupted == 2


class TestTelemetry:
    def test_log_creates_file(self, tmp_path):
        t = Telemetry(tmp_path / "logs")
        t.log(TaskStarted(task_key="abc", task_id=1))
        assert t.events_path.exists()
        lines = t.events_path.read_text().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["event"] == "task_started"
        assert data["task_key"] == "abc"
        assert data["timestamp"] > 0

    def test_log_appends(self, tmp_path):
        t = Telemetry(tmp_path / "logs")
        t.log(TaskStarted(task_key="t1", task_id=1))
        t.log(PhaseCompleted(task_key="t1", phase="coding", status="done", time_s=12.0))
        t.log(AllDone(total_passed=1, total_failed=0, total_missing_or_interrupted=0, total_cost=0.50))
        lines = t.events_path.read_text().strip().splitlines()
        assert len(lines) == 3
        assert json.loads(lines[0])["event"] == "task_started"
        assert json.loads(lines[1])["event"] == "phase_completed"
        assert json.loads(lines[2])["event"] == "all_done"
        assert json.loads(lines[2])["total_missing_or_interrupted"] == 0

    def test_legacy_disabled_by_default(self, tmp_path):
        t = Telemetry(tmp_path / "logs")
        t.log(TaskStarted(task_key="t1", task_id=1))
        legacy = tmp_path / "logs" / "pilot_results.jsonl"
        assert not legacy.exists()

    def test_legacy_enabled(self, tmp_path):
        t = Telemetry(tmp_path / "logs")
        t.enable_legacy_write()
        t.log(TaskStarted(task_key="t1", task_id=1, prompt="hello"))
        legacy = tmp_path / "logs" / "pilot_results.jsonl"
        assert legacy.exists()
        lines = legacy.read_text().strip().splitlines()
        assert len(lines) >= 1
        data = json.loads(lines[0])
        assert data["tool"] == "progress"
        assert data["event"] == "phase"

    def test_legacy_task_merged(self, tmp_path):
        t = Telemetry(tmp_path / "logs")
        t.enable_legacy_write()
        t.log(TaskMerged(task_key="t1", cost_usd=0.50, diff_summary="2 files"))
        legacy = tmp_path / "logs" / "pilot_results.jsonl"
        lines = legacy.read_text().strip().splitlines()
        data = json.loads(lines[-1])
        assert data["tool"] == "run_task_with_qa"
        assert data["success"] is True
        assert data["cost_usd"] == 0.50

    def test_legacy_task_failed(self, tmp_path):
        t = Telemetry(tmp_path / "logs")
        t.enable_legacy_write()
        t.log(TaskFailed(task_key="t1", error="boom"))
        legacy = tmp_path / "logs" / "pilot_results.jsonl"
        lines = legacy.read_text().strip().splitlines()
        data = json.loads(lines[-1])
        assert data["tool"] == "run_task_with_qa"
        assert data["success"] is False
        assert data["error"] == "boom"

    def test_legacy_preserves_results_clears_live_state(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        # pilot_results.jsonl is append-only — should NOT be cleared
        results = log_dir / "pilot_results.jsonl"
        results.write_text("old data\n")
        # live-state.json is current snapshot — should be cleared
        live_state = log_dir / "live-state.json"
        live_state.write_text("{}")
        t = Telemetry(log_dir)
        t.enable_legacy_write()
        assert results.exists() and results.read_text() == "old data\n"
        assert not live_state.exists()

    def test_fire_and_forget(self, tmp_path):
        """Log should never raise, even with bad directory."""
        t = Telemetry(tmp_path / "logs")
        # Force a write error by making events_path a directory
        t._events_path.mkdir(parents=True)
        # Should not raise
        t.log(TaskStarted(task_key="t1", task_id=1))

    def test_events_path_property(self, tmp_path):
        t = Telemetry(tmp_path / "logs")
        assert t.events_path == tmp_path / "logs" / "events.jsonl"
