"""Integration tests for Otto v2 features.

Tests components working together with real git repos, real file I/O,
and real task management. Mocks the agent SDK query() to avoid calling
the real Claude API.

Phases tested:
1. File-plan dependency injection (architect -> parse -> runner inject)
2. Holistic testgen (multi-task test generation)
3. Pilot (prompt building, MCP script generation)
4. Cross-task review (edge cases)
5. End-to-end: architect file-plan -> dependency injection -> tasks.yaml update
"""

import ast
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from otto._agent_stub import ResultMessage as StubResultMessage
from otto.architect import parse_file_plan, load_design_context
from otto.config import create_config, load_config, git_meta_dir
from otto.tasks import add_task, load_tasks, update_task, add_tasks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _commit_file(repo: Path, filename: str, content: str, msg: str = "add file") -> str:
    """Write, stage, and commit a file. Returns the commit SHA."""
    path = repo / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(["git", "add", filename], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=repo, capture_output=True, check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _write_file_plan(repo: Path, yaml_content: str) -> Path:
    """Write a file-plan.md with the given YAML content into otto_arch/."""
    arch_dir = repo / "otto_arch"
    arch_dir.mkdir(parents=True, exist_ok=True)
    file_plan = arch_dir / "file-plan.md"
    file_plan.write_text(f"# File Plan\n\n```yaml\n{yaml_content}```\n")
    return file_plan


async def _stub_query(*, prompt, options=None):
    """Async generator that mimics the agent stub: yields one empty ResultMessage."""
    yield StubResultMessage()


def _make_fake_result(session_id="test-session"):
    """Create a fake ResultMessage-like object for mocking."""
    result = MagicMock()
    result.session_id = session_id
    result.is_error = False
    result.subtype = "success"
    result.total_cost_usd = 0.0
    return result


# ===========================================================================
# Phase 1: File-plan dependency injection
# ===========================================================================

class TestFilePlanDependencyInjection:
    """Integration: architect produces file-plan.md -> parse_file_plan() reads it ->
    runner injects deps into tasks.yaml."""

    def test_full_flow_three_tasks_with_shared_files(self, tmp_git_repo):
        """Create 3 tasks and a file-plan that recommends deps because tasks
        share predicted files. Verify parse_file_plan returns correct deps
        and that injection updates tasks.yaml."""
        # Create tasks with the add_task API (real file I/O + locking)
        tasks_path = tmp_git_repo / "tasks.yaml"
        t1 = add_task(tasks_path, "Add user model")
        t2 = add_task(tasks_path, "Add user CLI")
        t3 = add_task(tasks_path, "Add user API")

        # Architect produces file-plan.md with overlap
        _write_file_plan(tmp_git_repo, (
            "tasks:\n"
            f"  - id: {t1['id']}\n"
            "    predicted_files: [models/user.py]\n"
            f"  - id: {t2['id']}\n"
            "    predicted_files: [models/user.py, cli.py]\n"
            f"  - id: {t3['id']}\n"
            "    predicted_files: [api.py, models/user.py]\n"
            "recommended_dependencies:\n"
            f"  - from: {t2['id']}\n"
            f"    depends_on: {t1['id']}\n"
            "    reason: both modify models/user.py\n"
            f"  - from: {t3['id']}\n"
            f"    depends_on: {t1['id']}\n"
            "    reason: both modify models/user.py\n"
        ))

        # Parse the file plan
        deps = parse_file_plan(tmp_git_repo)
        assert len(deps) == 2
        assert (t2["id"], t1["id"]) in deps
        assert (t3["id"], t1["id"]) in deps

        # Simulate runner's dependency injection logic (from run_all)
        tasks = load_tasks(tasks_path)
        pending_by_id = {t["id"]: t for t in tasks}
        injected = 0
        for dep_id, on_id in deps:
            task = pending_by_id.get(dep_id)
            if task:
                existing_deps = list(task.get("depends_on") or [])
                if on_id not in existing_deps:
                    existing_deps.append(on_id)
                    update_task(tasks_path, task["key"], depends_on=existing_deps)
                    injected += 1

        assert injected == 2

        # Verify tasks.yaml was updated
        final_tasks = load_tasks(tasks_path)
        tasks_by_id = {t["id"]: t for t in final_tasks}
        assert tasks_by_id[t2["id"]].get("depends_on") == [t1["id"]]
        assert tasks_by_id[t3["id"]].get("depends_on") == [t1["id"]]
        assert tasks_by_id[t1["id"]].get("depends_on") is None

    def test_injection_skips_already_declared_deps(self, tmp_git_repo):
        """If a task already has a depends_on for the recommended dep, skip it."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        t1 = add_task(tasks_path, "Task A")
        t2 = add_task(tasks_path, "Task B")

        # Pre-declare dependency
        update_task(tasks_path, t2["key"], depends_on=[t1["id"]])

        _write_file_plan(tmp_git_repo, (
            "recommended_dependencies:\n"
            f"  - from: {t2['id']}\n"
            f"    depends_on: {t1['id']}\n"
            "    reason: shared file\n"
        ))

        deps = parse_file_plan(tmp_git_repo)
        assert deps == [(t2["id"], t1["id"])]

        # Injection should skip (already declared)
        tasks = load_tasks(tasks_path)
        pending_by_id = {t["id"]: t for t in tasks}
        injected = 0
        for dep_id, on_id in deps:
            task = pending_by_id.get(dep_id)
            if task:
                existing_deps = list(task.get("depends_on") or [])
                if on_id not in existing_deps:
                    existing_deps.append(on_id)
                    update_task(tasks_path, task["key"], depends_on=existing_deps)
                    injected += 1

        assert injected == 0

    def test_injection_with_nonexistent_task_id(self, tmp_git_repo):
        """If file-plan references a task ID that doesn't exist, skip gracefully."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        t1 = add_task(tasks_path, "Only task")

        _write_file_plan(tmp_git_repo, (
            "recommended_dependencies:\n"
            "  - from: 999\n"
            f"    depends_on: {t1['id']}\n"
            "    reason: phantom task\n"
        ))

        deps = parse_file_plan(tmp_git_repo)
        assert deps == [(999, t1["id"])]

        # Injection skips task 999 (not in pending)
        tasks = load_tasks(tasks_path)
        pending_by_id = {t["id"]: t for t in tasks}
        injected = 0
        for dep_id, on_id in deps:
            task = pending_by_id.get(dep_id)
            if task:
                existing_deps = list(task.get("depends_on") or [])
                if on_id not in existing_deps:
                    existing_deps.append(on_id)
                    update_task(tasks_path, task["key"], depends_on=existing_deps)
                    injected += 1

        assert injected == 0

    def test_file_plan_with_chain_dependency(self, tmp_git_repo):
        """3->2->1 chain: verify the full chain is parsed and injected."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        t1 = add_task(tasks_path, "Foundation")
        t2 = add_task(tasks_path, "Middle layer")
        t3 = add_task(tasks_path, "Top layer")

        _write_file_plan(tmp_git_repo, (
            "recommended_dependencies:\n"
            f"  - from: {t2['id']}\n"
            f"    depends_on: {t1['id']}\n"
            "    reason: chain\n"
            f"  - from: {t3['id']}\n"
            f"    depends_on: {t2['id']}\n"
            "    reason: chain\n"
        ))

        deps = parse_file_plan(tmp_git_repo)

        tasks = load_tasks(tasks_path)
        pending_by_id = {t["id"]: t for t in tasks}
        for dep_id, on_id in deps:
            task = pending_by_id.get(dep_id)
            if task:
                existing_deps = list(task.get("depends_on") or [])
                if on_id not in existing_deps:
                    existing_deps.append(on_id)
                    update_task(tasks_path, task["key"], depends_on=existing_deps)

        final = load_tasks(tasks_path)
        by_id = {t["id"]: t for t in final}
        assert by_id[t2["id"]]["depends_on"] == [t1["id"]]
        assert by_id[t3["id"]]["depends_on"] == [t2["id"]]
        assert by_id[t1["id"]].get("depends_on") is None


# ===========================================================================
# Phase 2: Holistic testgen
# ===========================================================================

class TestHolisticTestgen:
    """Integration: run_holistic_testgen creates temp dirs, calls mocked agent,
    handles empty response, cleans up."""

    @pytest.mark.asyncio
    @patch("otto.testgen.query", new=_stub_query)
    async def test_holistic_testgen_with_stub_returns_empty(self, tmp_git_repo):
        """With the mocked agent (yields empty ResultMessage), holistic testgen
        should return empty results (no test files generated)."""
        from otto.testgen import run_holistic_testgen, build_blackbox_context

        tasks = [
            {"id": 1, "key": "aaa111bbb222", "prompt": "Add search", "rubric": ["search works"]},
            {"id": 2, "key": "ccc333ddd444", "prompt": "Add filter", "rubric": ["filter works"]},
        ]

        ctx = build_blackbox_context(tmp_git_repo, task_hint="search filter")
        results = await run_holistic_testgen(tasks, tmp_git_repo, ctx, quiet=True)

        # Stub produces no files, so all results should be None
        assert results["aaa111bbb222"] is None
        assert results["ccc333ddd444"] is None

    @pytest.mark.asyncio
    @patch("otto.testgen.query", new=_stub_query)
    async def test_holistic_testgen_creates_valid_test_dir_structure(self, tmp_git_repo):
        """Verify that the temp dir is cleaned up even on empty results."""
        from otto.testgen import run_holistic_testgen, build_blackbox_context
        import tempfile

        tasks = [
            {"id": 1, "key": "test111key22", "prompt": "Task 1", "rubric": ["criteria"]},
        ]

        ctx = build_blackbox_context(tmp_git_repo)

        # Track temp directories before and after
        pre_temps = set(Path(tempfile.gettempdir()).glob("otto_holistic_testgen_*"))
        results = await run_holistic_testgen(tasks, tmp_git_repo, ctx, quiet=True)
        post_temps = set(Path(tempfile.gettempdir()).glob("otto_holistic_testgen_*"))

        # Temp dir should be cleaned up
        new_dirs = post_temps - pre_temps
        assert len(new_dirs) == 0, "Temp directory was not cleaned up"

    @pytest.mark.asyncio
    @patch("otto.testgen.query", new=_stub_query)
    async def test_holistic_testgen_with_design_context(self, tmp_git_repo):
        """When otto_arch/ exists with design docs, holistic testgen should
        include them in the prompt (via load_design_context)."""
        from otto.testgen import run_holistic_testgen, build_blackbox_context

        # Create arch dir with testgen-relevant files
        arch_dir = tmp_git_repo / "otto_arch"
        arch_dir.mkdir()
        (arch_dir / "test-patterns.md").write_text("# Test Patterns\nUse pytest fixtures.")
        (arch_dir / "data-model.md").write_text("# Data Model\nJSON format.")
        (arch_dir / "conventions.md").write_text("# Conventions\nSnake case.")

        tasks = [
            {"id": 1, "key": "designctx123", "prompt": "Add feature", "rubric": ["it works"]},
        ]
        ctx = build_blackbox_context(tmp_git_repo)

        # This should not raise -- design context is loaded inside run_holistic_testgen
        results = await run_holistic_testgen(tasks, tmp_git_repo, ctx, quiet=True)
        assert isinstance(results, dict)
        assert "designctx123" in results


# ===========================================================================
# Phase 3: Pilot
# ===========================================================================

class TestPilotPromptBuilder:
    """Integration: _build_pilot_prompt produces valid prompts with all task info."""

    def test_prompt_contains_all_task_info(self, tmp_git_repo):
        from otto.pilot import _build_pilot_prompt

        tasks = [
            {"id": 1, "key": "aaa111", "prompt": "Implement search feature",
             "rubric": ["search is case-insensitive", "search returns results"],
             "depends_on": []},
            {"id": 2, "key": "bbb222", "prompt": "Implement filter feature",
             "rubric": ["filter by date works"],
             "depends_on": [1]},
            {"id": 3, "key": "ccc333", "prompt": "Add no-rubric task"},
        ]

        config = {
            "max_retries": 3,
            "test_command": "pytest",
            "max_parallel": 2,
            "default_branch": "main",
            "verify_timeout": 300,
        }

        prompt = _build_pilot_prompt(tasks, config, tmp_git_repo)

        # All task IDs present
        assert "#1" in prompt
        assert "#2" in prompt
        assert "#3" in prompt

        # Task keys present
        assert "aaa111" in prompt
        assert "bbb222" in prompt
        assert "ccc333" in prompt

        # Dependencies shown
        assert "depends_on: [1]" in prompt

        # Rubric counts
        assert "2 rubric items" in prompt
        assert "1 rubric items" in prompt
        assert "no rubric" in prompt

        # Config values
        assert "max_retries=3" in prompt
        assert "test_command=pytest" in prompt

        # Strategy guidance
        assert "run_holistic_testgen" in prompt
        assert "run_verify" in prompt
        assert "merge_task" in prompt

    def test_prompt_includes_design_context_when_available(self, tmp_git_repo):
        from otto.pilot import _build_pilot_prompt

        # Create architect docs
        arch_dir = tmp_git_repo / "otto_arch"
        arch_dir.mkdir()
        (arch_dir / "codebase.md").write_text("# Codebase\nModule map here.")
        (arch_dir / "task-decisions.md").write_text("# Decisions\nUse click for CLI.")
        (arch_dir / "file-plan.md").write_text("# File Plan\nTask 1 modifies cli.py.")

        tasks = [{"id": 1, "key": "abc123", "prompt": "Do something"}]
        config = {"max_retries": 3, "test_command": None, "max_parallel": 3,
                  "default_branch": "main", "verify_timeout": 300}

        prompt = _build_pilot_prompt(tasks, config, tmp_git_repo)

        assert "ARCHITECT DOCS" in prompt
        assert "Module map" in prompt
        assert "Use click" in prompt

    def test_prompt_handles_empty_task_list(self, tmp_git_repo):
        from otto.pilot import _build_pilot_prompt

        config = {"max_retries": 3, "test_command": "pytest", "max_parallel": 3,
                  "default_branch": "main", "verify_timeout": 300}

        prompt = _build_pilot_prompt([], config, tmp_git_repo)
        assert "PENDING TASKS" in prompt
        # Should not crash, just have empty task list


class TestMcpServerScript:
    """Integration: _build_mcp_server_script produces syntactically valid Python."""

    def test_script_is_valid_python(self, tmp_git_repo):
        from otto.pilot import _build_mcp_server_script

        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": []}, default_flow_style=False))

        config = {
            "max_retries": 3,
            "test_command": "pytest",
            "default_branch": "main",
            "verify_timeout": 300,
        }

        script = _build_mcp_server_script(config, tasks_path, tmp_git_repo)

        # Must parse without syntax errors
        tree = ast.parse(script)
        assert tree is not None

        # Must contain expected tool functions
        func_names = {
            node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        expected_tools = {
            "get_run_state", "run_holistic_testgen", "run_per_task_testgen",
            "run_coding_agent", "run_coding_agents", "run_verify",
            "read_verify_output", "merge_task", "update_task_status",
            "run_integration_gate_tool", "run_architect_tool",
            "abort_task", "save_run_state", "finish_run",
        }
        assert expected_tools.issubset(func_names), (
            f"Missing tools: {expected_tools - func_names}"
        )

    def test_script_embeds_correct_paths(self, tmp_git_repo):
        from otto.pilot import _build_mcp_server_script

        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": []}, default_flow_style=False))

        config = {"max_retries": 5, "default_branch": "develop", "verify_timeout": 120}

        script = _build_mcp_server_script(config, tasks_path, tmp_git_repo)

        # Paths should be embedded
        assert str(tasks_path) in script
        assert str(tmp_git_repo) in script
        # Config values should be embedded
        assert '"max_retries": 5' in script or "'max_retries': 5" in script
        assert '"default_branch": "develop"' in script or "'default_branch': 'develop'" in script


class TestRunPiloted:
    """Integration: run_piloted can start and handle the mocked agent's response."""

    @pytest.mark.asyncio
    async def test_piloted_with_no_pending_tasks(self, tmp_git_repo):
        """run_piloted should exit early with 0 when there are no pending tasks."""
        from otto.pilot import run_piloted

        create_config(tmp_git_repo)
        _commit_file(tmp_git_repo, "otto.yaml",
                     (tmp_git_repo / "otto.yaml").read_text(), "add otto config")

        config = load_config(tmp_git_repo / "otto.yaml")
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": []}, default_flow_style=False))

        exit_code = await run_piloted(config, tasks_path, tmp_git_repo)
        assert exit_code == 0

    @pytest.mark.asyncio
    @patch("otto.pilot.query", new=_stub_query)
    async def test_piloted_with_stub_agent(self, tmp_git_repo):
        """run_piloted with mocked agent (no real Claude) should complete.
        The stub yields a single empty ResultMessage, so the pilot finishes quickly."""
        from otto.pilot import run_piloted

        create_config(tmp_git_repo)
        _commit_file(tmp_git_repo, "otto.yaml",
                     (tmp_git_repo / "otto.yaml").read_text(), "add otto config")

        config = load_config(tmp_git_repo / "otto.yaml")
        config["no_architect"] = True  # Skip architect to speed up
        config["test_command"] = "true"

        tasks_path = tmp_git_repo / "tasks.yaml"
        add_task(tasks_path, "Test task", rubric=["it works"])

        exit_code = await run_piloted(config, tasks_path, tmp_git_repo)
        # Stub agent does nothing, so task stays pending -> pilot reports failure
        # The exact exit code depends on stub behavior, but it should not crash
        assert exit_code in (0, 1)


# ===========================================================================
# Phase 4: Cross-task review
# ===========================================================================

class TestCrossTaskReview:
    """Integration: _review_cross_task_changes handles edge cases correctly."""

    @pytest.mark.asyncio
    @patch("otto.runner.query", new=_stub_query)
    async def test_no_otto_commits_returns_false(self, tmp_git_repo):
        """When there are no otto commits in the repo, review should return False."""
        from otto.runner import _review_cross_task_changes

        config = {"test_command": "true", "verify_timeout": 300, "default_branch": "main"}
        tasks = [
            {"id": 1, "prompt": "Task 1"},
            {"id": 2, "prompt": "Task 2"},
        ]

        result = await _review_cross_task_changes(tasks, tmp_git_repo, config)
        assert result is False

    @pytest.mark.asyncio
    @patch("otto.runner.query", new=_stub_query)
    async def test_empty_diff_returns_false(self, tmp_git_repo):
        """When otto commits exist but the stub agent makes no changes,
        review should return False."""
        from otto.runner import _review_cross_task_changes

        _commit_file(tmp_git_repo, "dummy.txt", "content", "otto: dummy commit")

        config = {"test_command": "true", "verify_timeout": 300, "default_branch": "main"}
        tasks = [
            {"id": 1, "prompt": "Task 1"},
            {"id": 2, "prompt": "Task 2"},
        ]

        result = await _review_cross_task_changes(tasks, tmp_git_repo, config)
        assert result is False

    @pytest.mark.asyncio
    @patch("otto.runner.query", new=_stub_query)
    async def test_with_real_otto_commits(self, tmp_git_repo):
        """When multiple otto commits exist with changes, the function should
        find the first otto commit, compute diff, and call the agent."""
        from otto.runner import _review_cross_task_changes

        # Create multiple otto commits
        _commit_file(tmp_git_repo, "feature_a.py", "def a(): pass", "otto: add feature A (#1)")
        _commit_file(tmp_git_repo, "feature_b.py", "def b(): pass", "otto: add feature B (#2)")

        config = {"test_command": None, "verify_timeout": 300, "default_branch": "main"}
        tasks = [
            {"id": 1, "prompt": "Add feature A"},
            {"id": 2, "prompt": "Add feature B"},
        ]

        # Stub agent does nothing -> no edits -> returns False
        result = await _review_cross_task_changes(tasks, tmp_git_repo, config)
        assert result is False


# ===========================================================================
# Phase 5: End-to-end integration
# ===========================================================================

class TestEndToEndFilePlanInjection:
    """End-to-end: create tasks -> write file-plan -> inject deps -> verify tasks.yaml."""

    def test_file_plan_injection_updates_tasks_yaml_via_task_api(self, tmp_git_repo):
        """Full flow using the real task API: add_tasks with batch, write file-plan,
        parse, inject, and verify the final state in tasks.yaml."""
        tasks_path = tmp_git_repo / "tasks.yaml"

        # Add tasks with the batch API
        batch = [
            {"prompt": "Create data models", "rubric": ["models work"]},
            {"prompt": "Create CLI", "rubric": ["cli works"]},
            {"prompt": "Create API", "rubric": ["api works"]},
        ]
        created = add_tasks(tasks_path, batch)
        assert len(created) == 3
        id1, id2, id3 = created[0]["id"], created[1]["id"], created[2]["id"]

        # Architect produces file-plan saying CLI and API both depend on models
        _write_file_plan(tmp_git_repo, (
            "tasks:\n"
            f"  - id: {id1}\n"
            "    predicted_files: [models.py]\n"
            f"  - id: {id2}\n"
            "    predicted_files: [models.py, cli.py]\n"
            f"  - id: {id3}\n"
            "    predicted_files: [models.py, api.py]\n"
            "recommended_dependencies:\n"
            f"  - from: {id2}\n"
            f"    depends_on: {id1}\n"
            "    reason: both modify models.py\n"
            f"  - from: {id3}\n"
            f"    depends_on: {id1}\n"
            "    reason: both modify models.py\n"
        ))

        # Parse + inject (mirrors run_all logic)
        deps = parse_file_plan(tmp_git_repo)
        tasks = load_tasks(tasks_path)
        pending_by_id = {t["id"]: t for t in tasks}
        for dep_id, on_id in deps:
            task = pending_by_id.get(dep_id)
            if task:
                existing = list(task.get("depends_on") or [])
                if on_id not in existing:
                    existing.append(on_id)
                    update_task(tasks_path, task["key"], depends_on=existing)

        # Verify final state
        final = load_tasks(tasks_path)
        by_id = {t["id"]: t for t in final}

        # Task 1 (models) has no deps
        assert by_id[id1].get("depends_on") is None
        # Task 2 (CLI) depends on task 1
        assert by_id[id2]["depends_on"] == [id1]
        # Task 3 (API) depends on task 1
        assert by_id[id3]["depends_on"] == [id1]
        # All still pending
        assert all(by_id[tid]["status"] == "pending" for tid in [id1, id2, id3])

    def test_design_context_flows_to_roles_after_architect(self, tmp_git_repo):
        """After architect creates otto_arch/, the design context is available
        for coding, testgen, and pilot roles via load_design_context."""
        # Simulate architect output
        arch_dir = tmp_git_repo / "otto_arch"
        arch_dir.mkdir()
        (arch_dir / "codebase.md").write_text("# Codebase\nMain app is app.py.")
        (arch_dir / "conventions.md").write_text("# Conventions\nPEP8 strictly.")
        (arch_dir / "data-model.md").write_text("# Data Model\nSQLite backend.")
        (arch_dir / "interfaces.md").write_text("# Interfaces\ndef get(id): ...")
        (arch_dir / "test-patterns.md").write_text("# Tests\nUse pytest + tmp_path.")
        (arch_dir / "task-decisions.md").write_text("# Decisions\nClick for CLI.")
        (arch_dir / "gotchas.md").write_text("# Gotchas\n- Don't modify __init__.py")
        (arch_dir / "file-plan.md").write_text("# File Plan\nAll tasks use app.py")

        # Coding role gets conventions, data-model, interfaces, task-decisions, gotchas
        coding_ctx = load_design_context(tmp_git_repo, "coding")
        assert "PEP8" in coding_ctx
        assert "SQLite" in coding_ctx
        assert "def get(id)" in coding_ctx
        assert "Click for CLI" in coding_ctx
        assert "Don't modify __init__.py" in coding_ctx

        # Testgen role gets test-patterns, data-model, conventions
        testgen_ctx = load_design_context(tmp_git_repo, "testgen")
        assert "pytest + tmp_path" in testgen_ctx
        assert "SQLite" in testgen_ctx
        assert "PEP8" in testgen_ctx

        # Pilot role gets codebase, task-decisions, file-plan
        pilot_ctx = load_design_context(tmp_git_repo, "pilot")
        assert "Main app is app.py" in pilot_ctx
        assert "Click for CLI" in pilot_ctx
        assert "All tasks use app.py" in pilot_ctx


class TestReconciliationWithFilePlan:
    """Integration: reconciliation detects hidden dependencies not in file-plan."""

    def test_reconciliation_detects_overlapping_changed_files(self, tmp_git_repo):
        """Two tasks that both modify the same file should get a depends_on added
        during reconciliation, even if the file-plan didn't predict it."""
        from otto.runner import _reconcile_dependencies

        tasks_path = tmp_git_repo / "tasks.yaml"
        t1 = add_task(tasks_path, "Add logging")
        t2 = add_task(tasks_path, "Add metrics")

        # Simulate both tasks having passed and changed the same file
        update_task(tasks_path, t1["key"], status="passed",
                    changed_files=["app.py", "utils.py"])
        update_task(tasks_path, t2["key"], status="passed",
                    changed_files=["app.py", "metrics.py"])

        # Create a minimal source file for import graph analysis
        (tmp_git_repo / "app.py").write_text("# main app\n")
        (tmp_git_repo / "utils.py").write_text("# utils\n")
        (tmp_git_repo / "metrics.py").write_text("# metrics\n")
        subprocess.run(["git", "add", "."], cwd=tmp_git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add files"], cwd=tmp_git_repo, capture_output=True)

        # Run reconciliation
        ripple_risks = _reconcile_dependencies(tasks_path, tmp_git_repo)

        # Check that depends_on was updated for the later task
        final = load_tasks(tasks_path)
        by_id = {t["id"]: t for t in final}
        t2_deps = by_id[t2["id"]].get("depends_on") or []
        assert t1["id"] in t2_deps


class TestTopologicalOrderWithInjectedDeps:
    """Integration: verify that injected deps create correct topological ordering."""

    def test_topological_sort_respects_injected_deps(self, tmp_git_repo):
        """After injecting deps from file-plan, the topological sort should
        produce a valid execution order."""
        import graphlib

        tasks_path = tmp_git_repo / "tasks.yaml"
        t1 = add_task(tasks_path, "Foundation task")
        t2 = add_task(tasks_path, "Depends on foundation")
        t3 = add_task(tasks_path, "Also depends on foundation")

        # Inject deps
        update_task(tasks_path, t2["key"], depends_on=[t1["id"]])
        update_task(tasks_path, t3["key"], depends_on=[t1["id"]])

        # Build topological sorter (mirrors run_all logic)
        tasks = load_tasks(tasks_path)
        pending = [t for t in tasks if t["status"] == "pending"]
        pending_ids = {t["id"] for t in pending}

        ts = graphlib.TopologicalSorter()
        for t in pending:
            deps = t.get("depends_on") or []
            pending_deps = [d for d in deps if d in pending_ids]
            ts.add(t["id"], *pending_deps)

        ts.prepare()
        order = []
        while ts.is_active():
            ready = list(ts.get_ready())
            order.append(set(ready))
            for r in ready:
                ts.done(r)

        # First batch should be just task 1 (no deps)
        assert order[0] == {t1["id"]}
        # Second batch should be tasks 2 and 3 (can run in parallel)
        assert order[1] == {t2["id"], t3["id"]}

    def test_chain_dependency_produces_serial_order(self, tmp_git_repo):
        """3->2->1 chain should produce strictly serial execution."""
        import graphlib

        tasks_path = tmp_git_repo / "tasks.yaml"
        t1 = add_task(tasks_path, "Step 1")
        t2 = add_task(tasks_path, "Step 2")
        t3 = add_task(tasks_path, "Step 3")

        update_task(tasks_path, t2["key"], depends_on=[t1["id"]])
        update_task(tasks_path, t3["key"], depends_on=[t2["id"]])

        tasks = load_tasks(tasks_path)
        pending = [t for t in tasks if t["status"] == "pending"]
        pending_ids = {t["id"] for t in pending}

        ts = graphlib.TopologicalSorter()
        for t in pending:
            deps = t.get("depends_on") or []
            pending_deps = [d for d in deps if d in pending_ids]
            ts.add(t["id"], *pending_deps)

        ts.prepare()
        order = []
        while ts.is_active():
            ready = list(ts.get_ready())
            order.append(set(ready))
            for r in ready:
                ts.done(r)

        # Strictly serial: each batch has exactly 1 task
        assert len(order) == 3
        assert order[0] == {t1["id"]}
        assert order[1] == {t2["id"]}
        assert order[2] == {t3["id"]}


class TestGitMetaDirIntegration:
    """Integration: verify git_meta_dir works correctly with tmp repos."""

    def test_git_meta_dir_returns_dot_git(self, tmp_git_repo):
        """For a normal repo, git_meta_dir returns .git directory."""
        meta = git_meta_dir(tmp_git_repo)
        assert meta == tmp_git_repo / ".git"
        assert meta.is_dir()

    def test_testgen_storage_under_git_meta(self, tmp_git_repo):
        """Testgen files should be stored under <git-meta>/otto/testgen/<key>/."""
        meta = git_meta_dir(tmp_git_repo)
        testgen_dir = meta / "otto" / "testgen" / "abc123"
        testgen_dir.mkdir(parents=True)
        test_file = testgen_dir / "test_otto_abc123.py"
        test_file.write_text("def test_example(): assert True\n")

        # File should be outside the working tree (in .git/)
        assert ".git" in str(test_file)
        assert test_file.exists()
        assert test_file.read_text() == "def test_example(): assert True\n"
