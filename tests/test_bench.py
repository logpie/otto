"""Tests for otto/bench.py — data structures, serialization, comparison."""

import json
import tempfile
from pathlib import Path

import pytest
import yaml

from otto.bench import (
    BenchRun,
    BenchTask,
    TaskResult,
    compare_runs,
    cross_runner_report,
    filter_tasks,
    list_results,
    load_bench_task,
    load_results,
    load_suite,
    save_results,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_task():
    return BenchTask(
        name="add-search",
        repo="bookmarks",
        commit="abc123",
        difficulty="medium",
        spec="Add a search endpoint.",
        validator="validators/add-search/",
        timeout=300,
        tags=["api", "feature"],
    )


@pytest.fixture
def sample_result_pass():
    return TaskResult(
        passed=True, cost_usd=0.42, time_s=87.0, tokens=45000,
        retries=0, mutation_score=0.85,
    )


@pytest.fixture
def sample_result_fail():
    return TaskResult(
        passed=False, cost_usd=0.30, time_s=120.0, tokens=60000,
        retries=2, error="validator failed",
    )


@pytest.fixture
def sample_run(sample_result_pass, sample_result_fail):
    return BenchRun(
        run_id="20260317-120000-abc123",
        label="baseline",
        runner="otto",
        otto_commit="abc123",
        tasks={
            "add-search": sample_result_pass,
            "add-concurrent": sample_result_fail,
        },
        timestamp="2026-03-17T12:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# BenchTask
# ---------------------------------------------------------------------------

class TestBenchTask:
    def test_fields(self, sample_task):
        assert sample_task.name == "add-search"
        assert sample_task.repo == "bookmarks"
        assert sample_task.difficulty == "medium"
        assert sample_task.timeout == 300
        assert "api" in sample_task.tags

    def test_default_timeout(self):
        t = BenchTask(name="x", repo="r", commit="c", difficulty="easy",
                      spec="s", validator="v/")
        assert t.timeout == 300

    def test_default_tags(self):
        t = BenchTask(name="x", repo="r", commit="c", difficulty="easy",
                      spec="s", validator="v/")
        assert t.tags == []


# ---------------------------------------------------------------------------
# TaskResult
# ---------------------------------------------------------------------------

class TestTaskResult:
    def test_pass_result(self, sample_result_pass):
        assert sample_result_pass.passed is True
        assert sample_result_pass.cost_usd == 0.42

    def test_fail_result(self, sample_result_fail):
        assert sample_result_fail.passed is False
        assert sample_result_fail.error == "validator failed"

    def test_default_values(self):
        r = TaskResult(passed=True)
        assert r.cost_usd == 0.0
        assert r.tokens == 0
        assert r.error == ""


# ---------------------------------------------------------------------------
# BenchRun.summary
# ---------------------------------------------------------------------------

class TestBenchRunSummary:
    def test_summary_metrics(self, sample_run):
        s = sample_run.summary
        assert s["total"] == 2
        assert s["passed"] == 1
        assert s["success_rate"] == 0.5
        assert s["total_cost"] == 0.72
        assert s["cost_per_success"] == 0.72
        assert s["total_tokens"] == 105000

    def test_empty_run(self):
        run = BenchRun(run_id="x", label="x", runner="x", otto_commit="x", tasks={})
        s = run.summary
        assert s["success_rate"] == 0
        assert s["total_cost"] == 0

    def test_all_pass(self):
        run = BenchRun(
            run_id="x", label="x", runner="x", otto_commit="x",
            tasks={
                "a": TaskResult(passed=True, cost_usd=1.0, time_s=60, tokens=10000),
                "b": TaskResult(passed=True, cost_usd=2.0, time_s=90, tokens=20000),
            },
        )
        s = run.summary
        assert s["success_rate"] == 1.0
        assert s["passed"] == 2
        assert s["cost_per_success"] == 1.5

    def test_all_fail(self):
        run = BenchRun(
            run_id="x", label="x", runner="x", otto_commit="x",
            tasks={
                "a": TaskResult(passed=False, cost_usd=1.0),
                "b": TaskResult(passed=False, cost_usd=2.0),
            },
        )
        s = run.summary
        assert s["success_rate"] == 0.0
        assert s["cost_per_success"] == float("inf")

    def test_mutation_score_only_from_passing(self):
        run = BenchRun(
            run_id="x", label="x", runner="x", otto_commit="x",
            tasks={
                "a": TaskResult(passed=True, mutation_score=0.8),
                "b": TaskResult(passed=False, mutation_score=0.9),  # ignored
                "c": TaskResult(passed=True, mutation_score=0.6),
            },
        )
        s = run.summary
        assert s["mean_mutation_score"] == 0.7  # (0.8 + 0.6) / 2


# ---------------------------------------------------------------------------
# Suite / task loading from YAML
# ---------------------------------------------------------------------------

class TestLoadBenchTask:
    def test_load_from_yaml(self, tmp_path):
        task_file = tmp_path / "task.yaml"
        task_file.write_text(yaml.dump({
            "name": "add-flag",
            "repo": "bookmarks",
            "commit": "abc123",
            "difficulty": "easy",
            "spec": "Add a --verbose flag.",
            "validator": "validators/add-flag/",
            "timeout": 120,
            "tags": ["cli"],
        }))
        task = load_bench_task(task_file)
        assert task.name == "add-flag"
        assert task.timeout == 120
        assert task.tags == ["cli"]


class TestLoadSuite:
    def test_load_suite(self, tmp_path):
        # Create task files
        tasks_dir = tmp_path / "tasks" / "easy"
        tasks_dir.mkdir(parents=True)
        for name in ["task-a", "task-b"]:
            (tasks_dir / f"{name}.yaml").write_text(yaml.dump({
                "name": name,
                "repo": "r",
                "commit": "c",
                "spec": f"Spec for {name}",
                "validator": f"validators/{name}/",
            }))

        suite_file = tmp_path / "suite.yaml"
        suite_file.write_text(yaml.dump({
            "tasks": [
                "tasks/easy/task-a.yaml",
                "tasks/easy/task-b.yaml",
            ]
        }))

        tasks = load_suite(suite_file)
        assert len(tasks) == 2
        assert tasks[0].name == "task-a"

    def test_missing_task_file_skipped(self, tmp_path):
        suite_file = tmp_path / "suite.yaml"
        suite_file.write_text(yaml.dump({
            "tasks": ["nonexistent.yaml"]
        }))
        tasks = load_suite(suite_file)
        assert tasks == []


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

class TestFilterTasks:
    def test_filter_by_difficulty(self):
        tasks = [
            BenchTask(name="a", repo="r", commit="c", difficulty="easy",
                      spec="s", validator="v"),
            BenchTask(name="b", repo="r", commit="c", difficulty="hard",
                      spec="s", validator="v"),
        ]
        result = filter_tasks(tasks, difficulty="easy")
        assert len(result) == 1
        assert result[0].name == "a"

    def test_filter_by_name(self):
        tasks = [
            BenchTask(name="a", repo="r", commit="c", difficulty="easy",
                      spec="s", validator="v"),
            BenchTask(name="b", repo="r", commit="c", difficulty="easy",
                      spec="s", validator="v"),
        ]
        result = filter_tasks(tasks, names=["b"])
        assert len(result) == 1
        assert result[0].name == "b"

    def test_filter_by_tags(self):
        tasks = [
            BenchTask(name="a", repo="r", commit="c", difficulty="easy",
                      spec="s", validator="v", tags=["api"]),
            BenchTask(name="b", repo="r", commit="c", difficulty="easy",
                      spec="s", validator="v", tags=["cli"]),
        ]
        result = filter_tasks(tasks, tags=["api"])
        assert len(result) == 1
        assert result[0].name == "a"

    def test_no_filter(self):
        tasks = [
            BenchTask(name="a", repo="r", commit="c", difficulty="easy",
                      spec="s", validator="v"),
        ]
        assert filter_tasks(tasks) == tasks


# ---------------------------------------------------------------------------
# Results serialization
# ---------------------------------------------------------------------------

class TestResultsSerialization:
    def test_save_and_load(self, sample_run, tmp_path):
        results_dir = tmp_path / "results"
        out_path = save_results(sample_run, results_dir)
        assert out_path.exists()
        assert out_path.suffix == ".json"

        loaded = load_results(out_path)
        assert loaded.run_id == sample_run.run_id
        assert loaded.label == sample_run.label
        assert loaded.runner == sample_run.runner
        assert len(loaded.tasks) == 2
        assert loaded.tasks["add-search"].passed is True
        assert loaded.tasks["add-concurrent"].passed is False
        assert loaded.tasks["add-search"].cost_usd == 0.42

    def test_json_includes_summary(self, sample_run, tmp_path):
        results_dir = tmp_path / "results"
        out_path = save_results(sample_run, results_dir)
        data = json.loads(out_path.read_text())
        assert "summary" in data
        assert data["summary"]["total"] == 2

    def test_list_results(self, sample_run, tmp_path):
        results_dir = tmp_path / "results"
        save_results(sample_run, results_dir)

        # Create a second run
        run2 = BenchRun(
            run_id="20260317-130000-def456", label="after-tweak",
            runner="otto", otto_commit="def456",
            tasks={"a": TaskResult(passed=True)},
        )
        save_results(run2, results_dir)

        runs = list_results(results_dir)
        assert len(runs) == 2

    def test_list_results_empty(self, tmp_path):
        assert list_results(tmp_path / "nonexistent") == []


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

class TestComparison:
    def test_compare_basic(self):
        baseline = BenchRun(
            run_id="b", label="baseline", runner="otto", otto_commit="x",
            tasks={
                "a": TaskResult(passed=True, cost_usd=1.0, time_s=60, tokens=10000),
                "b": TaskResult(passed=False, cost_usd=0.5, time_s=30, tokens=5000),
            },
        )
        current = BenchRun(
            run_id="c", label="after-tweak", runner="otto", otto_commit="y",
            tasks={
                "a": TaskResult(passed=True, cost_usd=0.8, time_s=50, tokens=8000),
                "b": TaskResult(passed=True, cost_usd=0.6, time_s=40, tokens=7000),
            },
        )
        report = compare_runs(baseline, current)
        assert "baseline" in report
        assert "after-tweak" in report
        assert "Success rate" in report
        assert "New passes" in report
        assert "b" in report  # task b is a new pass

    def test_compare_regressions(self):
        baseline = BenchRun(
            run_id="b", label="before", runner="otto", otto_commit="x",
            tasks={"a": TaskResult(passed=True)},
        )
        current = BenchRun(
            run_id="c", label="after", runner="otto", otto_commit="y",
            tasks={"a": TaskResult(passed=False)},
        )
        report = compare_runs(baseline, current)
        assert "Regressions" in report
        assert "a" in report

    def test_compare_no_changes(self):
        run = BenchRun(
            run_id="x", label="same", runner="otto", otto_commit="x",
            tasks={"a": TaskResult(passed=True)},
        )
        report = compare_runs(run, run)
        assert "No task-level changes" in report


class TestCrossRunnerReport:
    def test_cross_runner(self):
        runs = [
            BenchRun(
                run_id="1", label="otto", runner="otto", otto_commit="x",
                tasks={"a": TaskResult(passed=True, cost_usd=1.0, tokens=10000)},
            ),
            BenchRun(
                run_id="2", label="bare", runner="bare-cc", otto_commit="x",
                tasks={"a": TaskResult(passed=False, cost_usd=0.5, tokens=5000)},
            ),
        ]
        report = cross_runner_report(runs)
        assert "otto" in report
        assert "bare-cc" in report
        assert "Success rate" in report

    def test_empty_runs(self):
        assert "No runs" in cross_runner_report([])
