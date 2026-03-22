"""Tests for otto.context — PipelineContext and TaskResult."""

from pathlib import Path

from otto.context import PipelineContext, TaskResult


class TestTaskResult:
    def test_defaults(self):
        r = TaskResult(task_key="abc123", success=True)
        assert r.task_key == "abc123"
        assert r.success is True
        assert r.commit_sha is None
        assert r.worktree is None
        assert r.cost_usd == 0.0
        assert r.error is None
        assert r.qa_report == ""
        assert r.diff_summary == ""
        assert r.duration_s == 0.0

    def test_full_result(self):
        r = TaskResult(
            task_key="abc123",
            success=False,
            commit_sha="deadbeef",
            worktree=Path("/tmp/wt"),
            cost_usd=1.23,
            error="tests failed",
            qa_report="QA VERDICT: FAIL",
            diff_summary="3 files changed",
            duration_s=42.5,
        )
        assert r.success is False
        assert r.commit_sha == "deadbeef"
        assert r.cost_usd == 1.23
        assert r.error == "tests failed"


class TestPipelineContext:
    def test_empty_context(self):
        ctx = PipelineContext()
        assert ctx.learnings == []
        assert ctx.results == {}
        assert ctx.total_cost == 0.0
        assert ctx.passed_count == 0
        assert ctx.failed_count == 0
        assert ctx.interrupted is False

    def test_add_research(self):
        ctx = PipelineContext()
        ctx.add_research("task1", "Found docs about API")
        assert ctx.get_research("task1") == "Found docs about API"
        assert ctx.get_research("nonexistent") is None

    def test_add_success(self):
        ctx = PipelineContext()
        r = TaskResult(task_key="t1", success=True, cost_usd=0.50)
        ctx.add_success(r)
        assert ctx.passed_count == 1
        assert ctx.failed_count == 0
        assert ctx.total_cost == 0.50
        assert ctx.results["t1"] is r

    def test_add_failure(self):
        ctx = PipelineContext()
        r = TaskResult(task_key="t1", success=False, cost_usd=0.30, error="boom")
        ctx.add_failure(r)
        assert ctx.passed_count == 0
        assert ctx.failed_count == 1
        assert ctx.total_cost == 0.30

    def test_mixed_results(self):
        ctx = PipelineContext()
        ctx.add_success(TaskResult(task_key="t1", success=True, cost_usd=0.50))
        ctx.add_failure(TaskResult(task_key="t2", success=False, cost_usd=0.30))
        ctx.add_success(TaskResult(task_key="t3", success=True, cost_usd=0.20))
        assert ctx.passed_count == 2
        assert ctx.failed_count == 1
        assert abs(ctx.total_cost - 1.0) < 0.001

    def test_pids_tracking(self):
        ctx = PipelineContext()
        ctx.pids.add(1234)
        ctx.pids.add(5678)
        assert 1234 in ctx.pids
        assert len(ctx.pids) == 2

    def test_learnings(self):
        ctx = PipelineContext()
        ctx.learnings.append("API requires auth header")
        ctx.learnings.append("Tests need CI=true")
        assert len(ctx.learnings) == 2

    def test_session_ids(self):
        ctx = PipelineContext()
        ctx.session_ids["t1"] = "sess-abc"
        assert ctx.session_ids["t1"] == "sess-abc"

    def test_zero_cost_not_tracked(self):
        ctx = PipelineContext()
        r = TaskResult(task_key="t1", success=True, cost_usd=0.0)
        ctx.add_success(r)
        assert ctx.total_cost == 0.0
        assert "t1" not in ctx.costs
