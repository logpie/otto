"""Tests for pilot display functions — verify tool calls and results render correctly.

These tests exercise _print_pilot_tool_call and _print_pilot_tool_result with
synthetic block objects that mimic what the Agent SDK streams. Captures stdout
and verifies the output contains expected content.
"""

import io
import json
import sys
from dataclasses import dataclass, field
from typing import Any

import pytest

from otto.pilot import (
    _print_pilot_tool_call,
    _print_pilot_tool_result,
    _Spinner,
    _active_spinner,
)


# ---------------------------------------------------------------------------
# Fake SDK block types for testing
# ---------------------------------------------------------------------------

@dataclass
class FakeToolUseBlock:
    name: str
    input: dict[str, Any] | None = None

    @property
    def type(self):
        return "tool_use"


@dataclass
class FakeToolResultBlock:
    tool_use_id: str = "test-123"
    content: str | list[dict[str, Any]] | None = None
    is_error: bool = False

    @property
    def type(self):
        return "tool_result"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def capture_output(func, *args, **kwargs) -> str:
    """Capture stdout from a function call."""
    old = sys.stdout
    sys.stdout = buf = io.StringIO()
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout = old
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Tool call display tests
# ---------------------------------------------------------------------------

class TestPilotToolCallDisplay:
    """Test _print_pilot_tool_call with various tool types."""

    def test_primary_tool_shows_bold_with_separator(self):
        block = FakeToolUseBlock(name="mcp__otto-pilot__run_coding_agent",
                                  input={"task_key": "abc123def456"})
        output = capture_output(_print_pilot_tool_call, block)
        assert "Coding" in output
        assert "abc123de" in output  # truncated key
        assert "─" in output  # separator

    def test_primary_tool_with_hint(self):
        block = FakeToolUseBlock(name="mcp__otto-pilot__run_coding_agent",
                                  input={"task_key": "abc123", "hint": "fix the import"})
        output = capture_output(_print_pilot_tool_call, block)
        assert "hint" in output
        assert "fix the import" in output

    def test_noise_tool_suppressed(self):
        block = FakeToolUseBlock(name="mcp__otto-pilot__save_run_state",
                                  input={"phase": "test"})
        output = capture_output(_print_pilot_tool_call, block)
        assert output.strip() == ""  # completely suppressed

    def test_toolsearch_suppressed(self):
        block = FakeToolUseBlock(name="ToolSearch", input={})
        output = capture_output(_print_pilot_tool_call, block)
        assert output.strip() == ""

    def test_secondary_tool_dimmed(self):
        block = FakeToolUseBlock(name="mcp__otto-pilot__get_run_state", input={})
        output = capture_output(_print_pilot_tool_call, block)
        assert "Loading task state" in output
        # Should NOT have separator line
        assert "─" * 50 not in output

    def test_unknown_tool_shows_name(self):
        block = FakeToolUseBlock(name="mcp__otto-pilot__some_new_tool", input={})
        output = capture_output(_print_pilot_tool_call, block)
        assert "some_new_tool" in output


# ---------------------------------------------------------------------------
# Tool result display tests
# ---------------------------------------------------------------------------

class TestPilotToolResultDisplay:
    """Test _print_pilot_tool_result with various result formats."""

    def test_single_task_success(self):
        result = json.dumps({
            "success": True, "status": "passed",
            "cost_usd": 0.37, "error": None,
            "diff": "", "verify_output": "",
        })
        block = FakeToolResultBlock(content=result)
        output = capture_output(_print_pilot_tool_result, block)
        assert "passed" in output
        assert "$0.37" in output

    def test_single_task_failure_with_error(self):
        result = json.dumps({
            "success": False, "status": "failed",
            "cost_usd": 0.40, "error": "max retries exhausted",
            "diff": "", "verify_output": "",
        })
        block = FakeToolResultBlock(content=result)
        output = capture_output(_print_pilot_tool_result, block)
        assert "failed" in output
        assert "max retries" in output

    def test_single_task_with_diff(self):
        diff_text = (
            "  taskflow/cli.py\\n"
            "    @@ -10,3 +10,15 @@\\n"
            "    \\033[32m+def search(ctx, query):\\033[0m\\n"
            "    \\033[32m+    store = ctx.obj['store']\\033[0m\\n"
            "  1 file changed, 12 insertions(+)"
        )
        result = json.dumps({
            "success": True, "status": "passed",
            "cost_usd": 0.35, "error": None,
            "diff": diff_text, "verify_output": "",
        })
        block = FakeToolResultBlock(content=result)
        output = capture_output(_print_pilot_tool_result, block)
        assert "passed" in output
        assert "cli.py" in output
        assert "search" in output

    def test_single_task_failure_with_verify_output(self):
        verify = (
            "FAILED tests/test_otto_abc.py::test_search - AssertionError\\n"
            "FAILED tests/test_otto_abc.py::test_case_insensitive - exit code 1\\n"
            "2 failed, 5 passed in 1.2s"
        )
        result = json.dumps({
            "success": False, "status": "failed",
            "cost_usd": 0.40, "error": "max retries exhausted",
            "diff": "", "verify_output": verify,
        })
        block = FakeToolResultBlock(content=result)
        output = capture_output(_print_pilot_tool_result, block)
        assert "FAILED" in output
        assert "test_search" in output

    def test_multi_task_results(self):
        result = json.dumps({
            "abc123": {"success": True, "status": "passed"},
            "def456": {"success": False, "error": "merge conflict"},
        })
        block = FakeToolResultBlock(content=result)
        output = capture_output(_print_pilot_tool_result, block)
        assert "abc123" in output
        assert "def456" in output
        assert "merge conflict" in output

    def test_task_state_list(self):
        result = json.dumps([
            {"id": 1, "status": "pending", "depends_on": [],
             "spec_count": 10, "prompt": "Add search command"},
            {"id": 2, "status": "pending", "depends_on": [1],
             "spec_count": 13, "prompt": "Add tag/untag commands"},
        ])
        block = FakeToolResultBlock(content=result)
        output = capture_output(_print_pilot_tool_result, block)
        assert "#1" in output
        assert "#2" in output
        assert "search" in output.lower()
        assert "#1" in output  # dep reference

    def test_ok_response_suppressed(self):
        result = json.dumps({"ok": True})
        block = FakeToolResultBlock(content=result)
        output = capture_output(_print_pilot_tool_result, block)
        assert output.strip() == ""

    def test_done_response_suppressed(self):
        result = json.dumps({"done": True, "summary": "all passed"})
        block = FakeToolResultBlock(content=result)
        output = capture_output(_print_pilot_tool_result, block)
        assert output.strip() == ""

    def test_error_result_shows_red(self):
        block = FakeToolResultBlock(content="Something went wrong", is_error=True)
        output = capture_output(_print_pilot_tool_result, block)
        assert "Something went wrong" in output

    def test_empty_content_suppressed(self):
        block = FakeToolResultBlock(content="")
        output = capture_output(_print_pilot_tool_result, block)
        assert output.strip() == ""

    def test_none_content_suppressed(self):
        block = FakeToolResultBlock(content=None)
        output = capture_output(_print_pilot_tool_result, block)
        assert output.strip() == ""

    def test_list_content_blocks(self):
        """ToolResultBlock.content can be list[dict] per SDK spec."""
        result = json.dumps({"success": True, "status": "passed", "cost_usd": 0.5})
        block = FakeToolResultBlock(content=[{"type": "text", "text": result}])
        output = capture_output(_print_pilot_tool_result, block)
        assert "passed" in output
        assert "$0.50" in output

    def test_long_content_truncated(self):
        block = FakeToolResultBlock(content="x" * 500)
        output = capture_output(_print_pilot_tool_result, block)
        assert "..." in output

    def test_single_task_result_with_tool_key(self):
        """Side-channel single task result includes 'tool' key — should parse correctly."""
        result = json.dumps({
            "tool": "run_coding_agent",
            "success": True, "status": "passed",
            "cost_usd": 0.42, "error": None,
            "diff": "", "verify_output": "",
        })
        block = FakeToolResultBlock(content=result)
        output = capture_output(_print_pilot_tool_result, block)
        assert "passed" in output
        assert "$0.42" in output


# ---------------------------------------------------------------------------
# Spinner tests
# ---------------------------------------------------------------------------

class TestSpinner:
    def test_start_stop_returns_elapsed(self):
        s = _Spinner("test")
        s.start()
        import time
        time.sleep(0.3)
        elapsed = s.stop()
        # Should be a string like "0s" or "1s"
        assert "s" in elapsed

    def test_stop_without_start(self):
        s = _Spinner("test")
        s._start_time = 0  # prevent division issues
        elapsed = s.stop()
        assert isinstance(elapsed, str)
