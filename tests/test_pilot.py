"""Tests for otto.pilot — gate pilot context assembly, parsing, and integration."""

import json
from unittest.mock import patch

import pytest

from otto.context import PipelineContext, TaskResult
from otto.pilot import (
    PilotDecision,
    RetryStrategy,
    RoutedContext,
    assemble_pilot_context,
    invoke_pilot,
    parse_pilot_decision,
    _truncate,
)
from otto.planner import Batch, ExecutionPlan, TaskPlan
from otto.tasks import load_tasks


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def project_dir(tmp_path):
    """Create a minimal project dir with otto_logs structure."""
    logs = tmp_path / "otto_logs"
    logs.mkdir()
    return tmp_path


@pytest.fixture
def context():
    ctx = PipelineContext()
    ctx.add_learning("project uses ESM modules", source="task-1", kind="observed")
    ctx.add_learning("vitest is the test runner", source="task-2", kind="observed")
    return ctx


@pytest.fixture
def failed_result():
    return TaskResult(
        task_key="add-search",
        success=False,
        cost_usd=0.45,
        duration_s=120.0,
        error="test_search_works FAILED",
        error_code="qa_failed",
        diff_summary="Modified 2 files: search.py, test_search.py",
    )


@pytest.fixture
def passed_result():
    return TaskResult(
        task_key="add-crud",
        success=True,
        cost_usd=0.30,
        duration_s=90.0,
        diff_summary="Modified 3 files: api.py, models.py, test_api.py",
    )


@pytest.fixture
def remaining_plan():
    return ExecutionPlan(
        batches=[
            Batch(tasks=[TaskPlan(task_key="add-tags"), TaskPlan(task_key="add-ui")]),
        ],
    )


# ---------------------------------------------------------------------------
# parse_pilot_decision
# ---------------------------------------------------------------------------

class TestParsePilotDecision:
    def test_valid_json(self):
        raw = json.dumps({
            "failure_analysis": {"task-1": "wrong test framework"},
            "retry_strategies": {
                "task-1": {
                    "action": "retry_different",
                    "guidance": "use vitest not jest",
                    "reason": "environment issue",
                }
            },
            "routed_context": [
                {"target_task": "task-2", "context": "project uses ESM"}
            ],
            "skip_tasks": ["task-3"],
            "new_learnings": ["all tests use vitest"],
            "batches": [{"tasks": [{"task_key": "task-1"}, {"task_key": "task-2"}]}],
        })
        decision = parse_pilot_decision(raw)

        assert decision.failure_analysis == {"task-1": "wrong test framework"}
        assert "task-1" in decision.retry_strategies
        assert decision.retry_strategies["task-1"].action == "retry_different"
        assert decision.retry_strategies["task-1"].guidance == "use vitest not jest"
        assert len(decision.routed_context) == 1
        assert decision.routed_context[0].target_task == "task-2"
        assert decision.skip_tasks == ["task-3"]
        assert decision.new_learnings == ["all tests use vitest"]
        assert len(decision.batches) == 1

    def test_json_in_markdown_fence(self):
        raw = """Here's my analysis:

```json
{
  "failure_analysis": {"t1": "timeout"},
  "retry_strategies": {},
  "routed_context": [],
  "skip_tasks": [],
  "new_learnings": [],
  "batches": [{"tasks": [{"task_key": "t1"}]}]
}
```
"""
        decision = parse_pilot_decision(raw)
        assert decision.failure_analysis == {"t1": "timeout"}

    def test_missing_fields_use_defaults(self):
        raw = json.dumps({"batches": [{"tasks": [{"task_key": "t1"}]}]})
        decision = parse_pilot_decision(raw)

        assert decision.failure_analysis == {}
        assert decision.retry_strategies == {}
        assert decision.skip_tasks == []
        assert decision.routed_context == []
        assert decision.new_learnings == []
        assert len(decision.batches) == 1

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="No JSON object"):
            parse_pilot_decision("I don't know what to do")

    def test_invalid_json_raises(self):
        with pytest.raises((ValueError, json.JSONDecodeError)):
            parse_pilot_decision("{invalid json}")

    def test_extra_text_around_json(self):
        raw = """Let me analyze the failures.

{"failure_analysis": {"t1": "bug"}, "retry_strategies": {}, "routed_context": [], "skip_tasks": [], "new_learnings": [], "batches": []}

That's my recommendation."""
        decision = parse_pilot_decision(raw)
        assert decision.failure_analysis == {"t1": "bug"}

    def test_malformed_retry_strategy_skipped(self):
        raw = json.dumps({
            "retry_strategies": {
                "good": {"action": "retry", "guidance": "", "reason": ""},
                "bad": "not a dict",
            },
            "batches": [],
        })
        decision = parse_pilot_decision(raw)
        assert "good" in decision.retry_strategies
        assert "bad" not in decision.retry_strategies

    def test_malformed_routed_context_skipped(self):
        raw = json.dumps({
            "routed_context": [
                {"target_task": "t1", "context": "good"},
                {"missing_field": True},
                "not a dict",
            ],
            "batches": [],
        })
        decision = parse_pilot_decision(raw)
        assert len(decision.routed_context) == 1


# ---------------------------------------------------------------------------
# assemble_pilot_context
# ---------------------------------------------------------------------------

class TestAssemblePilotContext:
    def test_includes_failed_task_error(self, project_dir, context, failed_result, remaining_plan):
        # Create verify log for the failed task
        task_dir = project_dir / "otto_logs" / "add-search"
        task_dir.mkdir(parents=True)
        (task_dir / "attempt-1-verify.log").write_text(
            "FAIL test_search_works\nAssertionError: expected 200 got 404"
        )

        result = assemble_pilot_context(
            batch_results=[failed_result],
            remaining_plan=remaining_plan,
            context=context,
            project_dir=project_dir,
            pending_by_key={
                "add-tags": {"key": "add-tags", "prompt": "Add tag system"},
                "add-ui": {"key": "add-ui", "prompt": "Build web UI"},
            },
        )

        assert "FAILED" in result
        assert "expected 200 got 404" in result
        assert "add-search" in result

    def test_includes_passed_task_summary(self, project_dir, context, passed_result, remaining_plan):
        task_dir = project_dir / "otto_logs" / "add-crud"
        task_dir.mkdir(parents=True)
        (task_dir / "task-summary.json").write_text(json.dumps({
            "phases": {"coding": {"cost": 0.25, "duration": 60}},
        }))

        result = assemble_pilot_context(
            batch_results=[passed_result],
            remaining_plan=remaining_plan,
            context=context,
            project_dir=project_dir,
            pending_by_key={},
        )

        assert "PASSED" in result
        assert "add-crud" in result

    def test_includes_learnings(self, project_dir, context, remaining_plan):
        result = assemble_pilot_context(
            batch_results=[],
            remaining_plan=remaining_plan,
            context=context,
            project_dir=project_dir,
            pending_by_key={},
        )

        assert "ESM modules" in result
        assert "vitest" in result

    def test_includes_remaining_tasks(self, project_dir, context, remaining_plan):
        result = assemble_pilot_context(
            batch_results=[],
            remaining_plan=remaining_plan,
            context=context,
            project_dir=project_dir,
            pending_by_key={
                "add-tags": {"key": "add-tags", "prompt": "Add tag system"},
                "add-ui": {"key": "add-ui", "prompt": "Build web UI"},
            },
        )

        assert "add-tags" in result
        assert "Add tag system" in result
        assert "add-ui" in result

    def test_includes_architecture_if_present(self, project_dir, context, remaining_plan):
        arch_dir = project_dir / "otto_arch"
        arch_dir.mkdir()
        (arch_dir / "architecture.md").write_text("Backend: FastAPI + SQLite")

        result = assemble_pilot_context(
            batch_results=[],
            remaining_plan=remaining_plan,
            context=context,
            project_dir=project_dir,
            pending_by_key={},
        )

        assert "FastAPI" in result

    def test_includes_qa_verdict(self, project_dir, context, failed_result, remaining_plan):
        task_dir = project_dir / "otto_logs" / "add-search"
        task_dir.mkdir(parents=True)
        (task_dir / "qa-verdict.json").write_text(json.dumps({
            "must_passed": False,
            "must_items": [{"criterion": "search returns results", "status": "fail"}],
        }))

        result = assemble_pilot_context(
            batch_results=[failed_result],
            remaining_plan=remaining_plan,
            context=context,
            project_dir=project_dir,
            pending_by_key={},
        )

        assert "search returns results" in result
        assert "must_passed" in result

    def test_uses_latest_verify_log_by_numeric_attempt(self, project_dir, context, failed_result, remaining_plan):
        task_dir = project_dir / "otto_logs" / "add-search"
        task_dir.mkdir(parents=True)
        (task_dir / "attempt-2-verify.log").write_text("older failure")
        (task_dir / "attempt-10-verify.log").write_text("latest failure")

        result = assemble_pilot_context(
            batch_results=[failed_result],
            remaining_plan=remaining_plan,
            context=context,
            project_dir=project_dir,
            pending_by_key={},
        )

        assert "latest failure" in result
        assert "older failure" not in result

    def test_includes_task_result_qa_report_for_failed_task(self, project_dir, context, failed_result, remaining_plan):
        failed_result.qa_report = "Batch QA found a missing migration and broken API contract."

        result = assemble_pilot_context(
            batch_results=[failed_result],
            remaining_plan=remaining_plan,
            context=context,
            project_dir=project_dir,
            pending_by_key={},
        )

        assert "QA report:" in result
        assert "missing migration" in result

    def test_marks_rolled_back_tasks_separately(self, project_dir, context, remaining_plan):
        rolled_back = TaskResult(
            task_key="add-search",
            success=False,
            cost_usd=0.45,
            duration_s=120.0,
            error="batch rolled back after batch QA failure",
            error_code="batch_qa_rolled_back",
        )

        result = assemble_pilot_context(
            batch_results=[rolled_back],
            remaining_plan=remaining_plan,
            context=context,
            project_dir=project_dir,
            pending_by_key={},
        )

        assert "Status: ROLLED_BACK" in result


class TestInvokePilot:
    @pytest.mark.asyncio
    async def test_prompt_distinguishes_terminal_failed_from_remaining(self, project_dir):
        captured_prompt = {}

        async def fake_run_agent_query(prompt, options):
            captured_prompt["text"] = prompt
            return (json.dumps({"batches": []}), 0.0, None)

        with patch("otto.pilot.run_agent_query", side_effect=fake_run_agent_query):
            await invoke_pilot(
                batch_results=[],
                remaining_plan=ExecutionPlan(
                    batches=[Batch(tasks=[TaskPlan(task_key="task-b"), TaskPlan(task_key="task-c")])]
                ),
                context=PipelineContext(),
                config={},
                project_dir=project_dir,
                batch_failed_keys={"task-a"},
                batch_rolled_back_keys={"task-c"},
                pending_by_key={},
            )

        prompt = captured_prompt["text"]
        assert "PERMANENTLY FAILED TASK KEYS (terminal, do not include in batches): task-a" in prompt
        assert "ROLLED_BACK TASK KEYS (still remaining, should be re-batched): task-c" in prompt
        assert "REMAINING TASK KEYS (the only keys batches may contain): task-b, task-c" in prompt
        assert "Do NOT include permanently failed tasks in batches" in prompt


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------

class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("hello", 100) == "hello"

    def test_long_text_keeps_end(self):
        text = "A" * 100 + "END"
        result = _truncate(text, 50)
        assert result.endswith("END")
        assert "truncated" in result
        assert len(result) < len(text)


# ---------------------------------------------------------------------------
# Integration: _apply_pilot_decision
# ---------------------------------------------------------------------------

class TestApplyPilotDecision:
    def test_applies_routed_context(self, project_dir):
        from otto.orchestrator import _apply_pilot_decision

        context = PipelineContext()
        decision = PilotDecision(
            failure_analysis={},
            retry_strategies={},
            skip_tasks=[],
            routed_context=[
                RoutedContext(target_task="task-b", context="project uses vitest"),
            ],
            new_learnings=["all tests require vitest"],
            batches=[],
        )

        tasks_file = project_dir / "tasks.yaml"
        tasks_file.write_text("tasks: []\n")
        (project_dir / "otto_logs").mkdir(exist_ok=True)

        result = _apply_pilot_decision(
            decision,
            ExecutionPlan(batches=[]),
            [],
            context,
            {},
            project_dir,
            tasks_file,
        )

        assert result is True
        # Routed context should be in learnings
        routed = [l for l in context.learnings if l.source == "gate_pilot" and "task-b" in l.text]
        assert len(routed) == 1
        assert "vitest" in routed[0].text

        # New learnings should be inferred (not injected into agents)
        inferred = [l for l in context.learnings if l.kind == "inferred"]
        assert len(inferred) == 1
        assert "vitest" in inferred[0].text

    def test_applies_retry_guidance(self, project_dir):
        from otto.orchestrator import _apply_pilot_decision

        context = PipelineContext()
        decision = PilotDecision(
            failure_analysis={"task-a": "used jest instead of vitest"},
            retry_strategies={
                "task-a": RetryStrategy(
                    action="retry_different",
                    guidance="use vitest not jest",
                    reason="environment issue",
                ),
            },
            skip_tasks=[],
            routed_context=[],
            new_learnings=[],
            batches=[],
        )

        tasks_file = project_dir / "tasks.yaml"
        tasks_file.write_text("tasks: []\n")
        (project_dir / "otto_logs").mkdir(exist_ok=True)

        _apply_pilot_decision(
            decision, ExecutionPlan(batches=[]), [], context, {}, project_dir, tasks_file,
        )

        # Retry guidance should be an observed learning
        guidance = [l for l in context.observed_learnings if "retry guidance" in l.text]
        assert len(guidance) == 1
        assert "vitest" in guidance[0].text

    def test_skip_marks_task_failed_not_skipped(self, project_dir):
        from otto.orchestrator import _apply_pilot_decision

        context = PipelineContext()
        decision = PilotDecision(
            failure_analysis={"task-b": "depends on a permanently failed migration task"},
            retry_strategies={},
            skip_tasks=["task-b"],
            routed_context=[],
            new_learnings=[],
            batches=[],
        )

        tasks_file = project_dir / "tasks.yaml"
        tasks_file.write_text(
            "tasks:\n"
            "  - id: 1\n"
            "    key: task-b\n"
            "    prompt: dependent task\n"
            "    status: pending\n"
        )
        (project_dir / "otto_logs").mkdir(exist_ok=True)

        _apply_pilot_decision(
            decision, ExecutionPlan(batches=[]), [], context, {}, project_dir, tasks_file,
        )

        persisted = {task["key"]: task for task in load_tasks(tasks_file)}
        assert persisted["task-b"]["status"] == "failed"
        assert "pilot:" in persisted["task-b"]["error"]
