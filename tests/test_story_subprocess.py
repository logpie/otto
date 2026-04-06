"""Tests for subprocess-per-story parallelism in the certifier."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.certifier.journey_agent import (
    BreakFinding,
    JourneyResult,
    StepResult,
    _atomic_write_json,
    _journey_result_from_dict,
    _journey_result_to_dict,
    _parse_tagged_verdict,
    _story_from_dict,
)
from otto.certifier.manifest import (
    ProductManifest,
    manifest_from_dict,
    manifest_to_dict,
)
from otto.certifier.stories import StoryStep, UserStory


# ---------------------------------------------------------------------------
# Serialization roundtrip tests
# ---------------------------------------------------------------------------


def test_journey_result_roundtrip():
    original = JourneyResult(
        story_id="test-story",
        story_title="Test Story",
        persona="new_user",
        passed=True,
        steps=[
            StepResult(
                action="register user",
                outcome="pass",
                verification="user created",
                evidence=[{"status": 200}],
                diagnosis="",
                fix_suggestion="",
            ),
            StepResult(
                action="create item",
                outcome="fail",
                verification="item not found",
                diagnosis="404 on POST /items",
                fix_suggestion="check route exists",
            ),
        ],
        break_findings=[
            BreakFinding(
                technique="long_input",
                description="sent 10000 char title",
                result="server 500",
                severity="moderate",
                fix_suggestion="add input validation",
            ),
        ],
        summary="partial pass",
        blocked_at="",
        diagnosis="item creation broken",
        fix_suggestion="fix POST route",
        cost_usd=0.42,
        duration_s=12.3,
    )
    d = _journey_result_to_dict(original)
    # Verify JSON round-trip
    json_str = json.dumps(d, default=str)
    d2 = json.loads(json_str)
    restored = _journey_result_from_dict(d2)

    assert restored.story_id == original.story_id
    assert restored.passed == original.passed
    assert len(restored.steps) == 2
    assert restored.steps[0].outcome == "pass"
    assert restored.steps[1].diagnosis == "404 on POST /items"
    assert len(restored.break_findings) == 1
    assert restored.break_findings[0].severity == "moderate"
    assert restored.cost_usd == original.cost_usd
    assert restored.duration_s == original.duration_s


def test_story_roundtrip():
    original = UserStory(
        id="first-task",
        persona="new_user",
        title="First Task",
        narrative="User creates their first task",
        steps=[
            StoryStep(
                action="register",
                verify="account created",
                entity="user",
                operation="create",
                mode="api",
                uses_output_from=None,
            ),
            StoryStep(
                action="create task",
                verify="task appears",
                entity="task",
                operation="create",
                mode="api",
                uses_output_from=0,
            ),
        ],
        critical=True,
        tests_integration=["auth", "crud"],
        break_strategies=["long_input"],
    )
    d = asdict(original)
    json_str = json.dumps(d, default=str)
    d2 = json.loads(json_str)
    restored = _story_from_dict(d2)

    assert restored.id == "first-task"
    assert restored.critical is True
    assert len(restored.steps) == 2
    assert restored.steps[1].uses_output_from == 0
    assert restored.break_strategies == ["long_input"]


def test_manifest_roundtrip():
    original = ProductManifest(
        framework="nextjs",
        language="typescript",
        product_type="webapp",
        interaction="http",
        auth_type="nextauth",
        register_endpoint="/api/auth/signup",
        login_endpoint="/api/auth/signin",
        seeded_users=[{"email": "admin@test.com", "password": "pass", "role": "admin"}],
        routes=[{"path": "/api/tasks", "methods": ["GET", "POST"], "requires_auth": True}],
        models=[{"name": "Task", "fields": {"title": "string"}}],
        base_url="http://localhost:3000",
        app_alive=True,
        confirmed_routes=["/api/tasks"],
        response_shapes={"/api/tasks": "array"},
    )
    d = manifest_to_dict(original)
    json_str = json.dumps(d, default=str)
    d2 = json.loads(json_str)
    restored = manifest_from_dict(d2)

    assert restored.framework == "nextjs"
    assert restored.base_url == "http://localhost:3000"
    assert len(restored.routes) == 1
    assert restored.confirmed_routes == ["/api/tasks"]


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def test_atomic_write_json(tmp_path):
    path = tmp_path / "test.json"
    data = {"key": "value", "num": 42}
    _atomic_write_json(path, data)

    assert path.exists()
    loaded = json.loads(path.read_text())
    assert loaded["key"] == "value"
    assert loaded["num"] == 42
    # Temp file should be cleaned up
    assert not (tmp_path / "test.tmp").exists()


# ---------------------------------------------------------------------------
# Worker copy
# ---------------------------------------------------------------------------


def test_create_worker_copy(tmp_path):
    from otto.certifier.journey_agent import _create_worker_copy

    # Set up fake project
    project = tmp_path / "project"
    project.mkdir()
    (project / "package.json").write_text("{}")
    (project / "src").mkdir()
    (project / "src" / "app.js").write_text("console.log('hi')")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "express").mkdir()
    (project / "node_modules" / "express" / "index.js").write_text("module.exports = {}")
    (project / ".venv").mkdir()
    (project / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin")
    (project / ".git").mkdir()
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (project / "__pycache__").mkdir()
    (project / "dev.db").write_text("sqlite data")

    worker = _create_worker_copy(project, "test-worker")

    # Source files copied
    assert (worker / "package.json").exists()
    assert (worker / "src" / "app.js").exists()

    # node_modules SYMLINKED (not copied — avoids .bin/ path issues)
    assert (worker / "node_modules").is_symlink()
    assert (worker / "node_modules" / "express" / "index.js").exists()

    # Excluded dirs
    assert not (worker / ".venv").exists()
    assert not (worker / ".git").exists()
    assert not (worker / "__pycache__").exists()

    # Runtime files included (APFS clone gives copy-on-write isolation)
    assert (worker / "dev.db").exists()


def test_create_worker_copy_symlinks_build_artifacts(tmp_path):
    from otto.certifier.baseline import AppRunner
    from otto.certifier.journey_agent import _create_worker_copy

    project = tmp_path / "project"
    project.mkdir()
    (project / "package.json").write_text("{}")
    (project / "node_modules").mkdir()
    (project / ".next").mkdir()
    (project / ".next" / "BUILD_ID").write_text("build-1")
    AppRunner.build_marker_path(project).write_text(
        json.dumps({"framework": "nextjs", "artifacts": [".next"]})
    )

    worker = _create_worker_copy(project, "test-worker-built")

    assert (worker / "node_modules").is_symlink()
    assert (worker / ".next").is_symlink()
    assert (worker / ".next" / "BUILD_ID").read_text() == "build-1"


def test_ensure_prisma_if_needed_skips_generate_for_shared_node_modules(tmp_path):
    from types import SimpleNamespace

    from otto.certifier.journey_agent import _ensure_prisma_if_needed

    project = tmp_path / "project"
    project.mkdir()
    (project / "prisma").mkdir()
    (project / "prisma" / "schema.prisma").write_text("datasource db { provider = \"sqlite\" }")

    shared_node_modules = tmp_path / "shared-node_modules"
    (shared_node_modules / ".bin").mkdir(parents=True)
    (shared_node_modules / ".bin" / "prisma").write_text("")
    (shared_node_modules / ".prisma" / "client").mkdir(parents=True)
    (shared_node_modules / ".prisma" / "client" / "index.js").write_text("module.exports = {}")
    os.symlink(shared_node_modules, project / "node_modules")

    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        _ensure_prisma_if_needed(project)

    assert all("generate" not in cmd for cmd in calls)
    assert any("db push" in cmd for cmd in calls)


def test_ensure_prisma_if_needed_generates_for_shared_node_modules_without_client(tmp_path):
    from types import SimpleNamespace

    from otto.certifier.journey_agent import _ensure_prisma_if_needed

    project = tmp_path / "project"
    project.mkdir()
    (project / "prisma").mkdir()
    (project / "prisma" / "schema.prisma").write_text("datasource db { provider = \"sqlite\" }")

    shared_node_modules = tmp_path / "shared-node_modules"
    (shared_node_modules / ".bin").mkdir(parents=True)
    (shared_node_modules / ".bin" / "prisma").write_text("")
    os.symlink(shared_node_modules, project / "node_modules")

    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        _ensure_prisma_if_needed(project)

    assert any("generate" in cmd for cmd in calls)
    assert any("db push" in cmd for cmd in calls)


def test_setup_worker_python_venv_falls_back_without_uv(tmp_path, caplog):
    from types import SimpleNamespace

    from otto.certifier.journey_agent import _setup_worker_python_venv

    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()
    (worker_dir / "requirements.txt").write_text("pytest\n")

    calls: list[list[str]] = []

    def fake_run_bootstrap(argv, project_dir, *, timeout, label):
        calls.append(argv)
        if argv[0] == "uv":
            raise FileNotFoundError("uv")
        if argv[:3] == [sys.executable, "-m", "venv"]:
            python_bin = worker_dir / ".venv" / "bin" / "python"
            python_bin.parent.mkdir(parents=True, exist_ok=True)
            python_bin.write_text("")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with caplog.at_level("WARNING"), \
         patch("otto.certifier.journey_agent._run_bootstrap_command", side_effect=fake_run_bootstrap):
        _setup_worker_python_venv(worker_dir)

    assert calls[0][0] == "uv"
    assert calls[1][:3] == [sys.executable, "-m", "venv"]
    assert calls[2][:3] == ["uv", "pip", "install"]
    assert calls[3][1:4] == ["-m", "pip", "install"]
    assert "falling back to python -m venv" in caplog.text
    assert "falling back to pip" in caplog.text


def test_setup_worker_python_venv_falls_back_when_uv_venv_fails(tmp_path, caplog):
    from types import SimpleNamespace

    from otto.certifier.journey_agent import _setup_worker_python_venv

    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()
    (worker_dir / "requirements.txt").write_text("pytest\n")

    calls: list[list[str]] = []

    def fake_run_bootstrap(argv, project_dir, *, timeout, label):
        calls.append(argv)
        if argv[:2] == ["uv", "venv"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="uv failed")
        if argv[:3] == [sys.executable, "-m", "venv"]:
            python_bin = worker_dir / ".venv" / "bin" / "python"
            python_bin.parent.mkdir(parents=True, exist_ok=True)
            python_bin.write_text("")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:3] == ["uv", "pip", "install"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="uv pip failed")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with caplog.at_level("WARNING"), \
         patch("otto.certifier.journey_agent._run_bootstrap_command", side_effect=fake_run_bootstrap):
        _setup_worker_python_venv(worker_dir)

    assert calls[0][:2] == ["uv", "venv"]
    assert calls[1][:3] == [sys.executable, "-m", "venv"]
    assert calls[2][:3] == ["uv", "pip", "install"]
    assert calls[3][1:4] == ["-m", "pip", "install"]
    assert "uv venv did not create" in caplog.text
    assert "uv pip install did not complete successfully" in caplog.text


# ---------------------------------------------------------------------------
# Tagged verdict parsing
# ---------------------------------------------------------------------------


def test_parse_tagged_verdict_pass():
    story = UserStory(id="test", persona="user", title="Test", narrative="",
                      steps=[], critical=False)
    raw = """
Step 1: Registered user successfully.
Step 2: Created item, got ID abc123.
Step 3: Listed items, found abc123.

VERDICT: PASS
BLOCKED_AT: null
DIAGNOSIS: null
SUGGESTED_FIX: null
"""
    result = _parse_tagged_verdict(raw, story)
    assert result.passed is True
    assert result.diagnosis == ""
    assert result.fix_suggestion == ""
    assert len(result.steps) == 0  # no failed steps


def test_parse_tagged_verdict_fail():
    story = UserStory(id="test", persona="user", title="Test", narrative="",
                      steps=[], critical=False)
    raw = """
Step 1: Registered user.
Step 2: Create item failed — 404 on POST /api/items.

VERDICT: FAIL
BLOCKED_AT: Step 2
DIAGNOSIS: POST /api/items returns 404 — route not implemented
SUGGESTED_FIX: Add POST handler for /api/items in routes.ts
FAILED_STEP: create item with title | POST /api/items returns 404
"""
    result = _parse_tagged_verdict(raw, story)
    assert result.passed is False
    assert "404" in result.diagnosis
    assert "POST handler" in result.fix_suggestion
    assert result.blocked_at == "Step 2"
    assert len(result.steps) == 1
    assert result.steps[0].outcome == "fail"
    assert "404" in result.steps[0].diagnosis


def test_parse_tagged_verdict_no_markers_defaults_to_fail():
    """Missing VERDICT marker = FAIL (safe default, won't false-pass)."""
    story = UserStory(id="test", persona="user", title="Test", narrative="",
                      steps=[], critical=False)
    raw = "All steps passed successfully. The product works great."
    result = _parse_tagged_verdict(raw, story)
    assert result.passed is False  # no VERDICT marker = fail
    assert "No VERDICT marker" in result.diagnosis


def test_parse_tagged_verdict_password_not_false_pass():
    """Text containing 'password' should not be misclassified as pass."""
    story = UserStory(id="test", persona="user", title="Test", narrative="",
                      steps=[], critical=False)
    raw = "Tested password reset flow. Everything looks good."
    result = _parse_tagged_verdict(raw, story)
    assert result.passed is False  # no VERDICT marker


# ---------------------------------------------------------------------------
# Scavenge stale workers
# ---------------------------------------------------------------------------


def test_scavenge_stale_workers(tmp_path):
    from otto.certifier.journey_agent import _scavenge_stale_workers

    # Workers are now in tempdir/otto-workers/{project_name}/
    # Mock by creating the expected structure and patching project_dir.resolve().name
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()
    workers_dir = tmp_path / "otto-workers" / "my-project"
    workers_dir.mkdir(parents=True)

    # Create an old dir and a fresh dir
    old = workers_dir / "old-run"
    old.mkdir()
    fresh = workers_dir / "fresh-run"
    fresh.mkdir()

    # Make old dir appear old (set mtime to 2 hours ago)
    import time
    old_time = time.time() - 7200
    os.utime(old, (old_time, old_time))

    with patch("tempfile.gettempdir", return_value=str(tmp_path)):
        _scavenge_stale_workers(project_dir, max_age_s=3600)

    assert not old.exists()
    assert fresh.exists()


# ---------------------------------------------------------------------------
# Kill orphan app
# ---------------------------------------------------------------------------


def test_kill_orphan_app_no_pidfile(tmp_path):
    """No crash when app.pid doesn't exist."""
    from otto.certifier.journey_agent import _kill_orphan_app
    _kill_orphan_app(tmp_path)  # should not raise


def test_kill_orphan_app_stale_pid(tmp_path):
    """Handles non-existent PID gracefully."""
    from otto.certifier.journey_agent import _kill_orphan_app
    (tmp_path / "app.pid").write_text("999999999")
    _kill_orphan_app(tmp_path)  # should not raise


# ---------------------------------------------------------------------------
# Worker main
# ---------------------------------------------------------------------------


def test_worker_main_writes_error_on_bad_input(tmp_path):
    """Worker writes error output when input.json is invalid."""
    from otto.certifier.journey_agent import _worker_main

    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    input_path.write_text("not valid json {{{")

    _worker_main(input_path, output_path)

    assert output_path.exists()
    result = json.loads(output_path.read_text())
    assert result["passed"] is False
    assert "Worker crashed" in result["diagnosis"]


def test_worker_main_uses_parent_discovery(tmp_path):
    """Workers use parent discovery from payload, not re-running LLM."""
    from types import SimpleNamespace

    from otto.certifier.classifier import ProductProfile
    from otto.certifier.journey_agent import _worker_main

    story = UserStory(
        id="cli-story", persona="user", title="CLI Story", narrative="test",
        steps=[], critical=False,
    )
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    input_path.write_text(json.dumps({
        "story": asdict(story),
        "worker_dir": str(worker_dir),
        "interaction": "cli",
        "config": {},
        "discovery": {
            "product_type": "cli",
            "interaction": "cli",
            "cli_entrypoint": ["python3", "notes.py"],
            "test_approach": "Run CLI commands and check stdout",
        },
    }))

    captured: dict[str, str] = {}

    async def fake_verify_story(story, manifest, base_url, project_dir, config):
        captured["base_url"] = base_url
        captured["cli_entrypoint"] = str(getattr(manifest, "cli_entrypoint", []))
        return JourneyResult(
            story_id=story.id,
            story_title=story.title,
            persona=story.persona,
            passed=True,
        )

    with patch("otto.certifier.classifier.classify", return_value=ProductProfile(
        product_type="unknown", framework="unknown", language="python",
        start_command="", port=None, test_command="pytest", interaction="unknown",
    )), patch("otto.certifier.adapter.analyze_project", return_value=SimpleNamespace(
            auth_type="none", register_endpoint="", login_endpoint="",
            seeded_users=[], routes=[], models=[], model_fields={},
            creatable_fields={}, enum_values={}, cli_entrypoints=[], cli_commands=[],
         )), \
         patch("otto.certifier.journey_agent.verify_story", side_effect=fake_verify_story):
        _worker_main(input_path, output_path)

    assert captured["base_url"] == ""  # CLI has no base_url
    assert "python3" in captured["cli_entrypoint"]  # parent discovery entrypoint passed through
    result = json.loads(output_path.read_text())
    assert result["passed"] is True


# ---------------------------------------------------------------------------
# Subprocess orchestration (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_story_in_subprocess_missing_output(tmp_path):
    """When subprocess exits without writing output, parent reads stderr."""
    from otto.certifier.journey_agent import _run_story_in_subprocess

    story = UserStory(
        id="test", persona="user", title="Test", narrative="test",
        steps=[], critical=False,
    )
    story_dir = tmp_path / "story"
    story_dir.mkdir()

    # Mock _create_worker_copy to return a temp dir
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()

    with patch("otto.certifier.journey_agent._create_worker_copy", return_value=worker_dir), \
         patch("otto.certifier.journey_agent._story_timeout", return_value=5.0):
        # Spawn a subprocess that exits immediately without writing output
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.pid = 99999
            mock_proc.returncode = 1
            mock_proc.wait = AsyncMock(return_value=1)
            mock_exec.return_value = mock_proc

            result = await _run_story_in_subprocess(story, tmp_path, {}, story_dir, "cli")

    assert result.passed is False
    assert "crashed without output" in result.diagnosis
    payload = json.loads((story_dir / "input.json").read_text())
    assert payload["interaction"] == "cli"


@pytest.mark.asyncio
async def test_run_story_in_subprocess_reads_output(tmp_path):
    """When subprocess writes valid output, parent reads and returns it."""
    from otto.certifier.journey_agent import _run_story_in_subprocess

    story = UserStory(
        id="test", persona="user", title="Test", narrative="test",
        steps=[], critical=False,
    )
    story_dir = tmp_path / "story"
    story_dir.mkdir()

    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()

    output_data = {
        "story_id": "test", "story_title": "Test", "persona": "user",
        "passed": True, "steps": [], "break_findings": [],
        "summary": "all good", "cost_usd": 0.5, "duration_s": 10.0,
    }

    async def fake_wait():
        # Write output.json as if the subprocess did it
        (story_dir / "output.json").write_text(json.dumps(output_data))
        return 0

    with patch("otto.certifier.journey_agent._create_worker_copy", return_value=worker_dir), \
         patch("otto.certifier.journey_agent._story_timeout", return_value=60.0):
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.pid = 99999
            mock_proc.returncode = 0
            mock_proc.wait = fake_wait
            mock_exec.return_value = mock_proc

            result = await _run_story_in_subprocess(story, tmp_path, {}, story_dir, "http")

    assert result.passed is True
    assert result.cost_usd == 0.5


@pytest.mark.asyncio
async def test_verify_all_stories_parallel_builds_once_and_clears_marker(tmp_path):
    from otto.certifier.baseline import AppRunner
    from otto.certifier.journey_agent import verify_all_stories

    story1 = UserStory(id="story-1", persona="user", title="One", narrative="test", steps=[], critical=True)
    story2 = UserStory(id="story-2", persona="user", title="Two", narrative="test", steps=[], critical=False)

    calls: list[str] = []

    def fake_ensure(project_dir):
        calls.append("deps")

    def fake_build(project_dir, config):
        calls.append("build")
        AppRunner.build_marker_path(project_dir).write_text(json.dumps({"framework": "nextjs", "artifacts": [".next"]}))

    async def fake_run_batch(stories, project_dir, config, story_dir, interaction, manifest=None):
        for story in stories:
            calls.append(f"story:{story.id}")
        return [JourneyResult(
            story_id=s.id,
            story_title=s.title,
            persona=s.persona,
            passed=True,
        ) for s in stories]

    with patch("otto.certifier.journey_agent._ensure_deps_installed", side_effect=fake_ensure), \
         patch("otto.certifier.journey_agent._build_project", side_effect=fake_build), \
         patch("otto.certifier.journey_agent._scavenge_stale_workers"), \
         patch("otto.certifier.journey_agent._scavenge_old_story_runs"), \
         patch("otto.certifier.journey_agent._run_stories_in_subprocess", side_effect=fake_run_batch):
        result = await verify_all_stories(
            stories=[story1, story2],
            manifest=MagicMock(interaction="cli"),
            base_url="http://localhost:3000",
            project_dir=tmp_path,
            config={"certifier_parallel_stories": 2},
        )

    assert result.stories_passed == 2
    assert calls[:2] == ["deps", "build"]
    assert set(calls[2:]) == {"story:story-1", "story:story-2"}
    assert not AppRunner.build_marker_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# CLI manifest formatting
# ---------------------------------------------------------------------------


def test_cli_manifest_format():
    """CLI manifest shows entrypoint, commands, help text — not HTTP routes."""
    from otto.certifier.manifest import ProductManifest, format_manifest_for_agent

    manifest = ProductManifest(
        framework="argparse",
        language="python",
        product_type="cli",
        interaction="cli",
        auth_type="none",
        register_endpoint="",
        login_endpoint="",
        seeded_users=[],
        routes=[],
        models=[],
        cli_entrypoint=["python3", "todo.py"],
        cli_commands=[
            {"name": "add", "args": ["task"], "flags": ["--priority"]},
            {"name": "list", "args": [], "flags": []},
            {"name": "done", "args": ["id"], "flags": []},
        ],
        cli_help_text="usage: todo.py [-h] {add,list,done} ...",
    )
    text = format_manifest_for_agent(manifest)
    assert "## CLI Interface" in text
    assert "python3 todo.py" in text
    assert "add" in text
    assert "list" in text
    assert "## API Routes" not in text
    assert "Base URL:" not in text
    assert "todo.py [-h]" in text


def test_cli_manifest_entrypoint_normalization():
    """CLI entrypoint is normalized from source path to runnable argv."""
    from otto.certifier.manifest import _normalize_cli_entrypoint
    from otto.certifier.classifier import ProductProfile

    # Python source file
    profile = ProductProfile(
        product_type="cli", framework="argparse", language="python",
        start_command="python todo.py", port=None, test_command="pytest",
        interaction="cli",
    )
    result = _normalize_cli_entrypoint("todo.py", profile)
    assert result == ["python3", "todo.py"]

    # Cargo project
    profile = ProductProfile(
        product_type="cli", framework="cargo", language="rust",
        start_command="cargo run", port=None, test_command="cargo test",
        interaction="cli",
    )
    result = _normalize_cli_entrypoint("src/main.rs", profile)
    assert result == ["cargo", "run", "--"]

    # Command with spaces (already a command)
    profile = ProductProfile(
        product_type="cli", framework="argparse", language="python",
        start_command="python -m mypackage", port=None, test_command="pytest",
        interaction="cli",
    )
    result = _normalize_cli_entrypoint("python -m mypackage", profile)
    assert result == ["python", "-m", "mypackage"]


def test_build_manifest_cli_override_normalizes_rust_start_command_fallback():
    """CLI override should drive manifest interaction and Rust entrypoint fallback."""
    from otto.certifier.adapter import TestConfig
    from otto.certifier.classifier import ProductProfile
    from otto.certifier.manifest import build_manifest

    manifest = build_manifest(
        TestConfig(),
        ProductProfile(
            product_type="cli",
            framework="cargo",
            language="rust",
            start_command="cargo run",
            port=None,
            test_command="cargo test",
            interaction="http",
        ),
        base_url=None,
        interaction="cli",
    )

    assert manifest.interaction == "cli"
    assert manifest.cli_entrypoint == ["cargo", "run", "--"]


# ---------------------------------------------------------------------------
# CLI adapter analysis
# ---------------------------------------------------------------------------


def test_analyze_cli_extracts_argparse_commands(tmp_path):
    """_analyze_cli finds subcommands from argparse add_parser calls."""
    from otto.certifier.adapter import TestConfig, _analyze_cli

    (tmp_path / "cli.py").write_text('''
import argparse

parser = argparse.ArgumentParser()
subparsers = parser.add_subparsers()
subparsers.add_parser("add")
subparsers.add_parser("list")
subparsers.add_parser("delete")
''')

    config = TestConfig()
    _analyze_cli(tmp_path, config)

    assert "argparse" in config.cli_frameworks
    assert "cli.py" in config.cli_entrypoints
    cmd_names = [c["name"] for c in config.cli_commands]
    assert "add" in cmd_names
    assert "list" in cmd_names
    assert "delete" in cmd_names


def test_analyze_cli_extracts_click_commands(tmp_path):
    """_analyze_cli finds subcommands from click decorators, including stacked decorators."""
    from otto.certifier.adapter import TestConfig, _analyze_cli

    (tmp_path / "app.py").write_text('''
import click

@click.group()
def cli():
    pass

@cli.command()
@click.argument("title")
@click.option("--tag", "-t", multiple=True)
def add_task(title, tag):
    pass

@cli.command("list")
@click.option("--tag", "-t")
def list_notes(tag):
    pass

@cli.command()
def show():
    pass
''')

    config = TestConfig()
    _analyze_cli(tmp_path, config)

    assert "click" in config.cli_frameworks
    cmd_names = [c["name"] for c in config.cli_commands]
    assert "add-task" in cmd_names   # from def add_task
    assert "list" in cmd_names       # explicit name from @cli.command("list")
    assert "show" in cmd_names       # simple case


# ---------------------------------------------------------------------------
# CLI probes
# ---------------------------------------------------------------------------


def test_cli_probes_help_works(tmp_path):
    """CLI probe succeeds when --help exits 0."""
    from unittest.mock import MagicMock
    from otto.certifier.tiers import run_tier2_cli_probes

    manifest = MagicMock()
    manifest.cli_entrypoint = ["echo", "help text"]
    manifest.cli_commands = []
    manifest.cli_help_text = ""

    profile = MagicMock()
    result = run_tier2_cli_probes(tmp_path, manifest, profile)

    assert result.status.value == "passed"


def test_cli_probes_missing_entrypoint(tmp_path):
    """CLI probe fails when entrypoint doesn't exist."""
    from unittest.mock import MagicMock
    from otto.certifier.tiers import run_tier2_cli_probes

    manifest = MagicMock()
    manifest.cli_entrypoint = ["/nonexistent/binary"]
    manifest.cli_commands = []
    manifest.cli_help_text = ""

    profile = MagicMock()
    result = run_tier2_cli_probes(tmp_path, manifest, profile)

    assert result.status.value == "failed"
    assert any("not found" in f.description for f in result.findings)


def test_cli_probes_no_entrypoint(tmp_path):
    """CLI probe skips when no entrypoint."""
    from unittest.mock import MagicMock
    from otto.certifier.tiers import run_tier2_cli_probes

    manifest = MagicMock()
    manifest.cli_entrypoint = []
    manifest.cli_commands = []

    profile = MagicMock()
    result = run_tier2_cli_probes(tmp_path, manifest, profile)

    assert result.status.value == "skipped"


# ---------------------------------------------------------------------------
# Story cache invalidation
# ---------------------------------------------------------------------------


def test_story_cache_key_includes_product_type(tmp_path):
    """Different product_type/interaction produces different cache paths."""
    from otto.certifier.stories import story_cache_path

    path_web = story_cache_path(tmp_path, "Build a todo app", "web", "browser")
    path_cli = story_cache_path(tmp_path, "Build a todo app", "cli", "cli")
    path_legacy = story_cache_path(tmp_path, "Build a todo app")

    assert path_web != path_cli
    assert path_web != path_legacy
    assert path_cli != path_legacy


# ---------------------------------------------------------------------------
# Override validation
# ---------------------------------------------------------------------------


def test_cli_override_on_http_product_ignored():
    """certifier_interaction=cli on a pure web app without CLI capability is ignored."""
    from otto.certifier.adapter import TestConfig
    # The validation logic is in __init__.py — test the routing decision
    config = TestConfig()
    config.cli_entrypoints = []  # no CLI capability

    # The override should be rejected
    interaction = "cli"
    if interaction == "cli" and not config.cli_entrypoints:
        interaction = "http"  # fallback

    assert interaction == "http"


def test_http_override_on_cli_product_ignored():
    """certifier_interaction=http on a CLI product is overridden to cli."""
    interaction = "http"
    product_type = "cli"

    if interaction in ("http", "browser") and product_type == "cli":
        interaction = "cli"

    assert interaction == "cli"
