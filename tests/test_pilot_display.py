"""Tests for TaskDisplay — verify tool calls, QA results, and phase output render correctly."""

import io
import time
from typing import Any

import pytest
from rich.console import Console

from otto.display import TaskDisplay


class TestTaskDisplay:
    def test_start_stop_returns_elapsed(self):
        buf = io.StringIO()
        test_console = Console(file=buf, highlight=False, color_system=None)
        td = TaskDisplay(test_console)
        td.start()
        time.sleep(0.3)
        elapsed = td.stop()
        assert "s" in elapsed

    def test_stop_without_start(self):
        buf = io.StringIO()
        test_console = Console(file=buf, highlight=False, color_system=None)
        td = TaskDisplay(test_console)
        td._start_time = 0
        elapsed = td.stop()
        assert isinstance(elapsed, str)

    def test_phase_done_prints_permanently(self):
        buf = io.StringIO()
        test_console = Console(file=buf, highlight=False, color_system=None)
        td = TaskDisplay(test_console)
        td.update_phase("coding", "running")
        td.update_phase("coding", "done", time_s=10.0, cost=0.5)
        output = buf.getvalue()
        assert "coding" in output
        assert "10s" in output
        assert "$0.50" in output

    def test_coding_running_prints_start_line(self):
        buf = io.StringIO()
        test_console = Console(file=buf, highlight=False, color_system=None)
        td = TaskDisplay(test_console)
        td.update_phase("coding", "running", detail="bare CC")
        output = buf.getvalue()
        assert "coding" in output
        assert "bare CC" in output

    def test_tool_calls_print_permanently(self):
        buf = io.StringIO()
        test_console = Console(file=buf, highlight=False, color_system=None)
        td = TaskDisplay(test_console)
        td._current_phase = "coding"
        td.add_tool(name="Write", detail="/tmp/project/src/alerts.ts")
        td.add_tool(name="Edit", detail="/tmp/project/src/WeatherApp.tsx")
        output = buf.getvalue()
        assert "alerts.ts" in output
        assert "WeatherApp.tsx" in output
        assert "Write" in output
        assert "Edit" in output
        assert "src/alerts.ts" in td._coding_files
        assert "src/WeatherApp.tsx" in td._coding_files

    def test_tool_dedup_uses_shortened_relative_path(self):
        buf = io.StringIO()
        test_console = Console(file=buf, highlight=False, color_system=None)
        td = TaskDisplay(test_console)
        td._current_phase = "coding"
        td.add_tool(name="Write", detail="/tmp/project/src/app.py")
        td.add_tool(name="Write", detail="/tmp/project/tests/app.py")
        output = buf.getvalue()
        assert "src/app.py" in output
        assert "tests/app.py" in output
        assert td._coding_files == ["src/app.py", "tests/app.py"]

    def test_qa_findings_print_permanently(self):
        buf = io.StringIO()
        test_console = Console(file=buf, highlight=False, color_system=None)
        td = TaskDisplay(test_console)
        td._current_phase = "qa"
        td.update_phase("qa", "running")
        td.add_finding("### Spec 1: API endpoint")
        td.add_finding("**PASS** \u2014 URL verified")
        td.add_finding("### Spec 2: AQI display")
        td.add_finding("**PASS** \u2014 renders correctly")
        td.add_finding("QA VERDICT: PASS")
        output = buf.getvalue()
        assert "Spec 1" in output
        assert "Spec 2" in output
        assert "\u2713" in output
        assert td._qa_spec_count == 2
        assert td._qa_pass_count == 2

    def test_qa_summary_is_authoritative(self):
        buf = io.StringIO()
        test_console = Console(file=buf, highlight=False, color_system=None)
        td = TaskDisplay(test_console)
        td.update_phase("qa", "running")
        td.add_finding("**PASS** \u2014 URL verified")
        td.set_qa_summary(total=3, passed=2, failed=1)
        td.update_phase("qa", "done", time_s=3.0)
        output = buf.getvalue()
        assert "2/3 specs passed" in output
        assert td._qa_spec_count == 3
        assert td._qa_pass_count == 2

    def test_spec_and_qa_item_helpers_render(self):
        buf = io.StringIO()
        test_console = Console(file=buf, highlight=False, color_system=None)
        td = TaskDisplay(test_console)
        td.add_spec_item("[must] Adds feature flag")
        td.add_qa_item_result("\u2713 [must] Adds feature flag", passed=True)
        td.add_qa_item_result("\u2717 [must] Rejects invalid input", passed=False, evidence="raised ValueError")
        output = buf.getvalue()
        assert "[must] Adds feature flag" in output
        assert "Rejects invalid input" in output
        assert "raised ValueError" in output

    def test_visual_spec_and_should_observation_render_neutrally(self):
        buf = io.StringIO()
        test_console = Console(file=buf, highlight=False, color_system=None)
        td = TaskDisplay(test_console)
        td.add_spec_item("[must \u25c8] Layout matches mock")
        td.add_qa_item_result("[should \u25c8] Colors fit theme", passed=None, evidence="close to palette")
        output = buf.getvalue()
        assert "[must" in output
        assert "\u25c8" in output
        assert "Layout matches mock" in output
        assert "\u00b7" in output
        assert "Colors fit theme" in output
        assert "close to palette" in output

    def test_qa_tools_show_informative_labels(self):
        """QA tool calls show what's being tested, not generic categories."""
        buf = io.StringIO()
        test_console = Console(file=buf, highlight=False, color_system=None)
        td = TaskDisplay(test_console)
        td.update_phase("qa", "running")
        td.add_tool(name="Read", detail="/tmp/project/src/WindCompass.tsx")
        td.add_tool(name="Bash", detail="npx tsc --noEmit 2>&1 | head -40")
        td.add_tool(name="Bash", detail="node -e \"// Test the rotation math...\"")
        td.add_tool(name="Bash", detail="npx jest --testPathPattern=windCompass --no-coverage")
        td.add_tool(name="Bash", detail="npm run build")
        output = buf.getvalue()
        assert "WindCompass.tsx" in output
        assert "Checking types" in output
        assert "node -e" in output
        assert "jest" in output
        assert "Building project" in output

    def test_qa_labels_deduplicate_identical_calls_only(self):
        buf = io.StringIO()
        test_console = Console(file=buf, highlight=False, color_system=None)
        td = TaskDisplay(test_console)
        td.update_phase("qa", "running")
        td.add_tool(name="Read", detail="/tmp/project/src/WindCompass.tsx")
        td.add_tool(name="Read", detail="/tmp/project/tests/windCompass.test.tsx")
        td.add_tool(name="Read", detail="/tmp/project/tests/windCompass.test.tsx")
        output = buf.getvalue()
        assert "WindCompass.tsx" in output
        assert "windCompass.test.tsx" in output
        assert output.count("windCompass.test.tsx") == 1

    def test_qa_labels_reset_when_phase_restarts(self):
        buf = io.StringIO()
        test_console = Console(file=buf, highlight=False, color_system=None)
        td = TaskDisplay(test_console)
        td.update_phase("qa", "running")
        td.add_tool(name="Read", detail="/tmp/project/src/WindCompass.tsx")
        td.update_phase("qa", "done", time_s=1.0)
        td.update_phase("qa", "running")
        td.add_tool(name="Read", detail="/tmp/project/src/WindCompass.tsx")
        output = buf.getvalue()
        assert output.count("WindCompass.tsx") == 2

    def test_internal_files_excluded_from_coding(self):
        buf = io.StringIO()
        test_console = Console(file=buf, highlight=False, color_system=None)
        td = TaskDisplay(test_console)
        td._current_phase = "coding"
        td.add_tool(name="Write", detail="/tmp/p/src/api.ts")
        td.add_tool(name="Write", detail="/tmp/p/otto_arch/task-notes/abc123.md")
        assert "src/api.ts" in td._coding_files
        assert len(td._coding_files) == 1
