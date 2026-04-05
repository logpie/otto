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


def test_parse_tagged_verdict_no_markers_fallback():
    story = UserStory(id="test", persona="user", title="Test", narrative="",
                      steps=[], critical=False)
    raw = "All steps passed successfully. The product works great."
    result = _parse_tagged_verdict(raw, story)
    assert result.passed is True  # inferred from prose


def test_parse_tagged_verdict_no_markers_fail():
    story = UserStory(id="test", persona="user", title="Test", narrative="",
                      steps=[], critical=False)
    raw = "Step 2 failed with a 500 error. The server crashed."
    result = _parse_tagged_verdict(raw, story)
    assert result.passed is False  # "fail" in text


# ---------------------------------------------------------------------------
# Scavenge stale workers
# ---------------------------------------------------------------------------


def test_scavenge_stale_workers(tmp_path):
    from otto.certifier.journey_agent import _scavenge_stale_workers

    workers_dir = tmp_path / ".otto-workers" / "stories"
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

    _scavenge_stale_workers(tmp_path, max_age_s=3600)

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

            result = await _run_story_in_subprocess(story, tmp_path, {}, story_dir)

    assert result.passed is False
    assert "crashed without output" in result.diagnosis


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

            result = await _run_story_in_subprocess(story, tmp_path, {}, story_dir)

    assert result.passed is True
    assert result.cost_usd == 0.5
