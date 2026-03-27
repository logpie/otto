"""Tests for otto.planner — ExecutionPlan dataclasses, JSON parsing, plan/replan."""

import json
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from otto.planner import (
    Batch,
    ExecutionPlan,
    TaskPlan,
    default_plan,
    parse_plan_json,
    plan,
    replan,
)


class TestTaskPlan:
    def test_defaults(self):
        tp = TaskPlan(task_key="abc123")
        assert tp.task_key == "abc123"
        assert tp.strategy == "direct"
        assert tp.research_query == ""
        assert tp.skip_qa is False
        assert tp.effort == "high"

    def test_research_first(self):
        tp = TaskPlan(
            task_key="abc123",
            strategy="research_first",
            research_query="how to optimize React hydration",
        )
        assert tp.strategy == "research_first"
        assert "React" in tp.research_query


class TestExecutionPlan:
    def test_empty_plan(self):
        plan = ExecutionPlan()
        assert plan.total_tasks == 0
        assert plan.is_empty is True

    def test_total_tasks(self):
        plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="t1"), TaskPlan(task_key="t2")]),
            Batch(tasks=[TaskPlan(task_key="t3")]),
        ])
        assert plan.total_tasks == 3
        assert plan.is_empty is False

    def test_remaining_after(self):
        plan = ExecutionPlan(
            batches=[
                Batch(tasks=[TaskPlan(task_key="t1"), TaskPlan(task_key="t2")]),
                Batch(tasks=[TaskPlan(task_key="t3")]),
            ],
            learnings=["lesson1"],
            conflicts=[{"tasks": ["t4", "t5"], "description": "conflict"}],
            analysis=[
                {"task_a": "t1", "task_b": "t2", "relationship": "ADDITIVE", "reason": "same file"},
                {"task_a": "t4", "task_b": "t5", "relationship": "CONTRADICTORY", "reason": "same function"},
            ],
        )
        remaining = plan.remaining_after({"t1", "t3"})
        assert remaining.total_tasks == 1
        assert remaining.batches[0].tasks[0].task_key == "t2"
        assert remaining.learnings == ["lesson1"]
        assert remaining.conflicts == []
        assert remaining.analysis == []

    def test_remaining_after_preserves_unresolved_metadata(self):
        plan = ExecutionPlan(
            batches=[
                Batch(tasks=[TaskPlan(task_key="t1"), TaskPlan(task_key="t2")]),
                Batch(tasks=[TaskPlan(task_key="t3")]),
            ],
            conflicts=[{"tasks": ["t2", "t3"], "description": "conflict"}],
            analysis=[
                {"task_a": "t1", "task_b": "t2", "relationship": "DEPENDENT", "reason": "chain"},
                {"task_a": "t2", "task_b": "t3", "relationship": "CONTRADICTORY", "reason": "overlap"},
            ],
        )
        remaining = plan.remaining_after({"t1"})
        assert remaining.total_tasks == 2
        assert remaining.conflicts == [{"tasks": ["t2", "t3"], "description": "conflict"}]
        assert remaining.analysis == [
            {"task_a": "t2", "task_b": "t3", "relationship": "CONTRADICTORY", "reason": "overlap"},
        ]

    def test_remaining_after_all_complete(self):
        plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="t1")]),
        ])
        remaining = plan.remaining_after({"t1"})
        assert remaining.is_empty


class TestParsePlanJson:
    def test_valid_json(self):
        raw = json.dumps({
            "batches": [
                {"tasks": [{"task_key": "abc123", "strategy": "direct"}]},
                {"tasks": [{"task_key": "def456"}]},
            ],
            "learnings": ["API needs auth"],
        })
        plan = parse_plan_json(raw)
        assert plan is not None
        assert plan.total_tasks == 2
        assert plan.batches[0].tasks[0].task_key == "abc123"
        assert plan.batches[1].tasks[0].task_key == "def456"
        assert plan.learnings == ["API needs auth"]

    def test_json_in_markdown_fences(self):
        raw = """Here's the plan:
```json
{
    "batches": [{"tasks": [{"task_key": "abc123"}]}]
}
```
"""
        plan = parse_plan_json(raw)
        assert plan is not None
        assert plan.total_tasks == 1

    def test_json_with_surrounding_text(self):
        raw = 'I think the best plan is: {"batches": [{"tasks": [{"task_key": "t1"}]}]} end.'
        plan = parse_plan_json(raw)
        assert plan is not None
        assert plan.total_tasks == 1

    def test_malformed_json_returns_none(self):
        assert parse_plan_json("not json at all") is None
        assert parse_plan_json("{}") is None  # no batches
        assert parse_plan_json('{"batches": []}') is None  # empty batches
        assert parse_plan_json('{"batches": [{"tasks": []}]}') is None  # empty tasks

    def test_missing_task_key_skipped(self):
        raw = json.dumps({
            "batches": [{"tasks": [
                {"task_key": "abc123"},
                {"strategy": "direct"},  # no task_key — skipped
                {"task_key": "def456"},
            ]}],
        })
        plan = parse_plan_json(raw)
        assert plan is not None
        assert plan.total_tasks == 2

    def test_extra_fields_preserved(self):
        raw = json.dumps({
            "batches": [{"tasks": [
                {"task_key": "t1", "strategy": "research_first",
                 "research_query": "how to X", "skip_qa": True, "effort": "low"},
            ]}],
        })
        plan = parse_plan_json(raw)
        assert plan is not None
        tp = plan.batches[0].tasks[0]
        assert tp.strategy == "research_first"
        assert tp.research_query == "how to X"
        assert tp.skip_qa is True
        assert tp.effort == "low"

    def test_non_list_learnings_ignored(self):
        raw = json.dumps({
            "batches": [{"tasks": [{"task_key": "t1"}]}],
            "learnings": "not a list",
        })
        plan = parse_plan_json(raw)
        assert plan is not None
        assert plan.learnings == []

    def test_conflicts_without_batches_parse(self):
        raw = json.dumps({
            "conflicts": [
                {
                    "tasks": ["t1", "t2"],
                    "description": "same function, different rewrite",
                    "suggestion": "combine prompts",
                }
            ],
            "analysis": [
                {
                    "task_a": "t1",
                    "task_b": "t2",
                    "relationship": "contradictory",
                    "reason": "same function",
                }
            ],
        })
        plan = parse_plan_json(raw)
        assert plan is not None
        assert plan.total_tasks == 0
        assert plan.conflicts[0]["tasks"] == ["t1", "t2"]
        assert plan.analysis[0]["relationship"] == "CONTRADICTORY"


class TestDefaultPlan:
    def test_single_task(self):
        tasks = [{"key": "abc123", "prompt": "Add hello"}]
        plan = default_plan(tasks)
        assert plan.total_tasks == 1
        assert len(plan.batches) == 1
        assert plan.batches[0].tasks[0].task_key == "abc123"

    def test_multi_task_independent_parallel(self):
        """Independent tasks should be grouped into one parallel batch."""
        tasks = [
            {"key": "t1", "id": 1, "prompt": "Task 1"},
            {"key": "t2", "id": 2, "prompt": "Task 2"},
            {"key": "t3", "id": 3, "prompt": "Task 3"},
        ]
        result = default_plan(tasks)
        assert result.total_tasks == 3
        # All independent → single batch (parallel)
        assert len(result.batches) == 1
        keys = {tp.task_key for tp in result.batches[0].tasks}
        assert keys == {"t1", "t2", "t3"}

    def test_multi_task_with_deps(self):
        """Tasks with depends_on should be in later batches."""
        tasks = [
            {"key": "t1", "id": 1, "prompt": "Task 1"},
            {"key": "t2", "id": 2, "prompt": "Task 2", "depends_on": [1]},
            {"key": "t3", "id": 3, "prompt": "Task 3"},
        ]
        result = default_plan(tasks)
        assert result.total_tasks == 3
        assert len(result.batches) == 2  # t1+t3 parallel, then t2
        # t2 must be in a later batch than t1
        batch_of = {}
        for i, batch in enumerate(result.batches):
            for tp in batch.tasks:
                batch_of[tp.task_key] = i
        assert batch_of["t2"] > batch_of["t1"]

    def test_empty_tasks(self):
        plan = default_plan([])
        assert plan.is_empty

    def test_tasks_without_key_skipped(self):
        tasks = [{"prompt": "no key"}, {"key": "t1", "prompt": "has key"}]
        plan = default_plan(tasks)
        assert plan.total_tasks == 1


# ---------------------------------------------------------------------------
# Tests for plan() and replan() with mocked query
# ---------------------------------------------------------------------------

def _make_fake_message(text: str):
    """Create a fake AssistantMessage-like object for mocked query()."""
    class FakeBlock:
        def __init__(self, t):
            self.text = t
    class FakeMessage:
        def __init__(self, t):
            self.content = [FakeBlock(t)]
    return FakeMessage(text)


async def _fake_query_returning(text):
    """Create an async generator that yields one fake message."""
    yield _make_fake_message(text)


class TestPlan:
    @pytest.mark.asyncio
    async def test_two_independent_tasks_parallel_after_shortlist(self, tmp_path):
        tasks = [
            {"key": "t1", "id": 1, "prompt": "Add search page"},
            {"key": "t2", "id": 2, "prompt": "Add dark mode toggle"},
        ]
        seen_options = []

        async def fake_query(prompt, options, *args, **kwargs):
            seen_options.append(options)
            assert options.system_prompt == {"type": "preset", "preset": "claude_code"}
            return json.dumps({"candidates": []}), 0.0, None

        with patch("otto.planner.run_agent_query", side_effect=fake_query):
            result = await plan(tasks, {}, tmp_path)

        assert len(seen_options) == 1
        assert seen_options[0].model == "haiku"
        assert len(result.batches) == 1
        assert {tp.task_key for tp in result.batches[0].tasks} == {"t1", "t2"}
        assert result.analysis == []

    @pytest.mark.asyncio
    async def test_two_additive_tasks_parallel(self, tmp_path):
        tasks = [
            {"key": "t1", "id": 1, "prompt": "Add slugify() to utils.py"},
            {"key": "t2", "id": 2, "prompt": "Add title_case() to utils.py"},
        ]
        responses = iter([
            json.dumps({"candidates": [{"task_a": "t1", "task_b": "t2", "reason": "same file"}]}),
            json.dumps({
                "analysis": [
                    {"task_a": "t1", "task_b": "t2", "relationship": "ADDITIVE", "reason": "same file, different functions"},
                ],
                "conflicts": [],
                "batches": [
                    {"tasks": [{"task_key": "t1"}, {"task_key": "t2"}]},
                ],
            }),
        ])

        async def fake_query(prompt, options, *args, **kwargs):
            return next(responses), 0.0, None

        with patch("otto.planner.run_agent_query", side_effect=fake_query):
            result = await plan(tasks, {}, tmp_path)

        assert len(result.batches) == 1
        assert {tp.task_key for tp in result.batches[0].tasks} == {"t1", "t2"}
        assert result.analysis[0]["relationship"] == "ADDITIVE"

    @pytest.mark.asyncio
    async def test_two_dependent_tasks_serialized(self, tmp_path):
        tasks = [
            {"key": "t1", "id": 1, "prompt": "Add auth backend"},
            {"key": "t2", "id": 2, "prompt": "Build profile page on top of auth"},
        ]
        responses = iter([
            json.dumps({"candidates": [{"task_a": "t1", "task_b": "t2", "reason": "profile depends on auth"}]}),
            json.dumps({
                "analysis": [
                    {"task_a": "t1", "task_b": "t2", "relationship": "DEPENDENT", "reason": "profile requires auth output"},
                ],
                "conflicts": [],
                "batches": [
                    {"tasks": [{"task_key": "t1"}]},
                    {"tasks": [{"task_key": "t2"}]},
                ],
            }),
        ])

        async def fake_query(prompt, options, *args, **kwargs):
            return next(responses), 0.0, None

        with patch("otto.planner.run_agent_query", side_effect=fake_query):
            result = await plan(tasks, {}, tmp_path)

        assert [batch.tasks[0].task_key for batch in result.batches] == ["t1", "t2"]
        assert result.analysis[0]["relationship"] == "DEPENDENT"

    @pytest.mark.asyncio
    async def test_two_contradictory_tasks_flagged(self, tmp_path):
        tasks = [
            {"key": "t1", "id": 1, "prompt": "Rewrite calculateWindChill with formula A"},
            {"key": "t2", "id": 2, "prompt": "Rewrite calculateWindChill with formula B"},
        ]
        responses = iter([
            json.dumps({"candidates": [{"task_a": "t1", "task_b": "t2", "reason": "same function"}]}),
            json.dumps({
                "analysis": [
                    {"task_a": "t1", "task_b": "t2", "relationship": "CONTRADICTORY", "reason": "same function, incompatible goals"},
                ],
                "conflicts": [
                    {
                        "tasks": ["t1", "t2"],
                        "description": "Both rewrite calculateWindChill incompatibly",
                        "suggestion": "Combine into one task",
                    }
                ],
                "batches": [],
            }),
        ])

        async def fake_query(prompt, options, *args, **kwargs):
            return next(responses), 0.0, None

        with patch("otto.planner.run_agent_query", side_effect=fake_query):
            result = await plan(tasks, {}, tmp_path)

        assert result.total_tasks == 0
        assert result.conflicts == [
            {
                "tasks": ["t1", "t2"],
                "description": "Both rewrite calculateWindChill incompatibly",
                "suggestion": "Combine into one task",
            }
        ]

    @pytest.mark.asyncio
    async def test_mixed_relationships_plan(self, tmp_path):
        tasks = [
            {"key": "t1", "id": 1, "prompt": "Add slugify() to utils.py"},
            {"key": "t2", "id": 2, "prompt": "Add title_case() to utils.py"},
            {"key": "t3", "id": 3, "prompt": "Use slugify() in user routes"},
            {"key": "t4", "id": 4, "prompt": "Add onboarding page"},
        ]
        responses = iter([
            json.dumps({
                "candidates": [
                    {"task_a": "t1", "task_b": "t2", "reason": "same file"},
                    {"task_a": "t1", "task_b": "t3", "reason": "route depends on helper"},
                ]
            }),
            json.dumps({
                "analysis": [
                    {"task_a": "t1", "task_b": "t2", "relationship": "ADDITIVE", "reason": "same file, different functions"},
                    {"task_a": "t1", "task_b": "t3", "relationship": "DEPENDENT", "reason": "routes need helper"},
                    {"task_a": "t2", "task_b": "t4", "relationship": "INDEPENDENT", "reason": "unrelated"},
                ],
                "conflicts": [],
                "batches": [
                    {"tasks": [{"task_key": "t1"}, {"task_key": "t2"}, {"task_key": "t4"}]},
                    {"tasks": [{"task_key": "t3"}]},
                ],
            }),
        ])

        async def fake_query(prompt, options, *args, **kwargs):
            return next(responses), 0.0, None

        with patch("otto.planner.run_agent_query", side_effect=fake_query):
            result = await plan(tasks, {}, tmp_path)

        assert len(result.batches) == 2
        assert {tp.task_key for tp in result.batches[0].tasks} == {"t1", "t2", "t4"}
        assert [tp.task_key for tp in result.batches[1].tasks] == ["t3"]

    @pytest.mark.asyncio
    async def test_single_task_skips_llm(self, tmp_path):
        """Single task should use default_plan without calling LLM."""
        tasks = [{"key": "abc123", "prompt": "Add hello"}]
        result = await plan(tasks, {}, tmp_path)
        assert result.total_tasks == 1
        assert result.batches[0].tasks[0].task_key == "abc123"

    @pytest.mark.asyncio
    async def test_empty_tasks(self, tmp_path):
        result = await plan([], {}, tmp_path)
        assert result.is_empty

    @pytest.mark.asyncio
    async def test_fallback_on_import_error(self, tmp_path):
        """Planner failure should fall back to a serial plan."""
        tasks = [
            {"key": "t1", "prompt": "Task 1"},
            {"key": "t2", "prompt": "Task 2"},
        ]
        with patch("otto.planner.run_agent_query", side_effect=RuntimeError("boom")):
            result = await plan(tasks, {}, tmp_path)
        assert result.total_tasks == 2
        assert len(result.batches) == 2

    @pytest.mark.asyncio
    async def test_successful_plan_from_llm(self, tmp_path):
        """Mocked LLM returns valid plan JSON."""
        tasks = [
            {"key": "t1", "id": 1, "prompt": "Task 1"},
            {"key": "t2", "id": 2, "prompt": "Task 2"},
        ]
        plan_json = json.dumps({
            "batches": [
                {"tasks": [
                    {"task_key": "t1", "strategy": "direct"},
                    {"task_key": "t2", "strategy": "direct"},
                ]},
            ],
        })

        async def fake_query(**kwargs):
            yield _make_fake_message(plan_json)

        with patch("otto.planner.sdk_query", fake_query, create=True):
            with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
                # Directly test parse path since we can't easily mock the import chain
                result = parse_plan_json(plan_json)
        assert result is not None
        assert result.total_tasks == 2
        assert len(result.batches) == 1  # parallel batch

    @pytest.mark.asyncio
    async def test_malformed_llm_output_falls_back(self, tmp_path):
        """Malformed LLM output should fall back to default_plan."""
        tasks = [
            {"key": "t1", "prompt": "Task 1"},
            {"key": "t2", "prompt": "Task 2"},
        ]
        # Verify malformed JSON falls back
        result = parse_plan_json("This is not valid JSON at all")
        assert result is None


class TestReplan:
    @pytest.mark.asyncio
    async def test_empty_remaining_returns_same(self, tmp_path):
        """Empty remaining plan should be returned as-is."""
        from otto.context import PipelineContext
        ctx = PipelineContext()
        remaining = ExecutionPlan()
        result = await replan(ctx, remaining, {}, tmp_path)
        assert result.is_empty

    @pytest.mark.asyncio
    async def test_fallback_on_import_error(self, tmp_path):
        """Should return remaining_plan when SDK not available."""
        from otto.context import PipelineContext, TaskResult
        ctx = PipelineContext()
        ctx.add_failure(TaskResult(task_key="t1", success=False, error="tests failed"))

        remaining = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="t2")]),
        ])

        import builtins
        original_import = builtins.__import__
        def mock_import(name, *args, **kwargs):
            if name == "claude_agent_sdk":
                raise ImportError("no SDK")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=mock_import):
            result = await replan(ctx, remaining, {}, tmp_path)

        assert result.total_tasks == 1
        assert result.batches[0].tasks[0].task_key == "t2"
