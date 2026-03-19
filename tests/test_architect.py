"""Tests for otto.architect module."""

import json
from pathlib import Path

import pytest

from otto.architect import (
    _extract_conftest,
    _load_meta,
    _save_meta,
    append_gotcha,
    is_stale,
    load_design_context,
    feed_reconciliation_learnings,
    parse_file_plan,
)


@pytest.fixture
def arch_dir(tmp_path):
    """Create a minimal otto_arch/ directory."""
    d = tmp_path / "otto_arch"
    d.mkdir()
    (d / "codebase.md").write_text("# Codebase\nModule map here.")
    (d / "conventions.md").write_text("# Conventions\nSnake case everywhere.")
    (d / "data-model.md").write_text("# Data Model\nJSON list of dicts.")
    (d / "interfaces.md").write_text("# Interfaces\ndef add(x, y): ...")
    (d / "test-patterns.md").write_text("# Test Patterns\nUse pytest.\n```conftest.py\nimport pytest\n```")
    (d / "task-decisions.md").write_text("# Task Decisions\nTask #1: use click.")
    (d / "gotchas.md").write_text("# Gotchas\n")
    return tmp_path


class TestLoadDesignContext:
    def test_returns_empty_if_no_arch_dir(self, tmp_path):
        assert load_design_context(tmp_path, "coding") == ""

    def test_coding_role_gets_correct_files(self, arch_dir):
        ctx = load_design_context(arch_dir, "coding")
        assert "conventions.md" in ctx
        assert "data-model.md" in ctx
        assert "interfaces.md" in ctx
        assert "task-decisions.md" in ctx
        assert "gotchas.md" in ctx
        # Should NOT include test-patterns or codebase
        assert "Test Patterns" not in ctx
        assert "Module map" not in ctx

    def test_unknown_role_returns_empty(self, arch_dir):
        assert load_design_context(arch_dir, "unknown_role") == ""


class TestExtractConftest:
    def test_extracts_conftest_block(self):
        content = "# Test\n```conftest.py\nimport pytest\n\n@pytest.fixture\ndef db():\n    pass\n```\n"
        result = _extract_conftest(content)
        assert result is not None
        assert "import pytest" in result
        assert "@pytest.fixture" in result

    def test_extracts_python_conftest_block(self):
        content = "# Test\n```python conftest.py\nimport pytest\n```\n"
        result = _extract_conftest(content)
        assert result is not None
        assert "import pytest" in result

    def test_returns_none_when_no_block(self):
        content = "# Test\nNo conftest here.\n```python\nsome_code()\n```\n"
        assert _extract_conftest(content) is None


class TestAppendGotcha:
    def test_appends_to_existing(self, arch_dir):
        append_gotcha(arch_dir, "Don't use mutable defaults")
        gotchas = (arch_dir / "otto_arch" / "gotchas.md").read_text()
        assert "Don't use mutable defaults" in gotchas

    def test_increments_meta_counter(self, arch_dir):
        append_gotcha(arch_dir, "Warning 1")
        append_gotcha(arch_dir, "Warning 2")
        meta = _load_meta(arch_dir)
        assert meta["gotcha_count"] == 2

    def test_noop_when_no_arch_dir(self, tmp_path):
        # Should not raise
        append_gotcha(tmp_path, "some warning")


class TestIsStale:
    def test_fresh_is_not_stale(self, arch_dir):
        _save_meta(arch_dir, {"gotcha_count": 0})
        assert not is_stale(arch_dir)

    def test_stale_at_threshold(self, arch_dir):
        _save_meta(arch_dir, {"gotcha_count": 3})
        assert is_stale(arch_dir)

    def test_stale_above_threshold(self, arch_dir):
        _save_meta(arch_dir, {"gotcha_count": 5})
        assert is_stale(arch_dir)

    def test_not_stale_below_threshold(self, arch_dir):
        _save_meta(arch_dir, {"gotcha_count": 2})
        assert not is_stale(arch_dir)

    def test_no_meta_file_is_not_stale(self, arch_dir):
        # No .meta file exists
        assert not is_stale(arch_dir)


class TestFeedReconciliationLearnings:
    def test_feeds_warnings_to_gotchas(self, arch_dir):
        warnings = ["Task #1 changed store.py, cli.py imports it"]
        feed_reconciliation_learnings(arch_dir, warnings)
        gotchas = (arch_dir / "otto_arch" / "gotchas.md").read_text()
        assert "[reconciliation]" in gotchas
        assert "store.py" in gotchas

    def test_increments_counter(self, arch_dir):
        warnings = ["w1", "w2", "w3"]
        feed_reconciliation_learnings(arch_dir, warnings)
        meta = _load_meta(arch_dir)
        assert meta["gotcha_count"] == 3
        assert is_stale(arch_dir)


class TestParseFilePlan:
    def test_parses_yaml_file_plan(self, arch_dir):
        (arch_dir / "otto_arch" / "file-plan.md").write_text(
            "tasks:\n"
            "  - id: 1\n"
            "    predicted_files: [cli.py]\n"
            "  - id: 2\n"
            "    predicted_files: [cli.py, store.py]\n"
            "recommended_dependencies:\n"
            "  - from: 2\n"
            "    depends_on: 1\n"
            "    reason: both modify cli.py\n"
        )
        deps = parse_file_plan(arch_dir)
        assert deps == [(2, 1)]

    def test_parses_fenced_yaml_block(self, arch_dir):
        (arch_dir / "otto_arch" / "file-plan.md").write_text(
            "# File Plan\n\n```yaml\n"
            "tasks:\n"
            "  - id: 1\n"
            "    predicted_files: [cli.py]\n"
            "recommended_dependencies:\n"
            "  - from: 3\n"
            "    depends_on: 1\n"
            "    reason: shared file\n"
            "```\n"
        )
        deps = parse_file_plan(arch_dir)
        assert deps == [(3, 1)]

    def test_returns_empty_if_no_file(self, tmp_path):
        assert parse_file_plan(tmp_path) == []

    def test_returns_empty_on_invalid_yaml(self, arch_dir):
        (arch_dir / "otto_arch" / "file-plan.md").write_text("not: [valid: yaml: :")
        assert parse_file_plan(arch_dir) == []

    def test_returns_empty_if_no_dependencies(self, arch_dir):
        (arch_dir / "otto_arch" / "file-plan.md").write_text(
            "tasks:\n"
            "  - id: 1\n"
            "    predicted_files: [cli.py]\n"
        )
        assert parse_file_plan(arch_dir) == []

    def test_multiple_dependencies(self, arch_dir):
        (arch_dir / "otto_arch" / "file-plan.md").write_text(
            "tasks:\n"
            "  - id: 1\n"
            "    predicted_files: [cli.py]\n"
            "  - id: 2\n"
            "    predicted_files: [cli.py]\n"
            "  - id: 3\n"
            "    predicted_files: [cli.py]\n"
            "recommended_dependencies:\n"
            "  - from: 2\n"
            "    depends_on: 1\n"
            "    reason: both modify cli.py\n"
            "  - from: 3\n"
            "    depends_on: 2\n"
            "    reason: both modify cli.py\n"
        )
        deps = parse_file_plan(arch_dir)
        assert (2, 1) in deps
        assert (3, 2) in deps
        # Auto-detection also adds (3,1) since tasks 1 and 3 share cli.py
        # and only (3,2) was explicitly declared, not (3,1)
        assert (3, 1) in deps

    def test_auto_detects_file_overlap(self, arch_dir):
        """Tasks sharing predicted files but no declared dep get auto-chained."""
        (arch_dir / "otto_arch" / "file-plan.md").write_text(
            "tasks:\n"
            "  - id: 1\n"
            "    predicted_files: [models.py]\n"
            "  - id: 2\n"
            "    predicted_files: [cli.py, models.py]\n"
            "  - id: 3\n"
            "    predicted_files: [cli.py]\n"
            "recommended_dependencies:\n"
            "  - from: 2\n"
            "    depends_on: 1\n"
            "    reason: both modify models.py\n"
        )
        deps = parse_file_plan(arch_dir)
        # 2→1 is explicit. 3→2 should be auto-injected (both have cli.py)
        assert (2, 1) in deps
        assert (3, 2) in deps

    def test_no_auto_dep_when_no_overlap(self, arch_dir):
        """Tasks with no shared files get no auto-injected deps."""
        (arch_dir / "otto_arch" / "file-plan.md").write_text(
            "tasks:\n"
            "  - id: 1\n"
            "    predicted_files: [models.py]\n"
            "  - id: 2\n"
            "    predicted_files: [cli.py]\n"
            "  - id: 3\n"
            "    predicted_files: [api.py]\n"
        )
        deps = parse_file_plan(arch_dir)
        assert deps == []  # no overlap, no deps

    def test_auto_dep_skips_already_connected(self, arch_dir):
        """Don't double-inject deps for pairs that already have one."""
        (arch_dir / "otto_arch" / "file-plan.md").write_text(
            "tasks:\n"
            "  - id: 1\n"
            "    predicted_files: [cli.py]\n"
            "  - id: 2\n"
            "    predicted_files: [cli.py]\n"
            "recommended_dependencies:\n"
            "  - from: 2\n"
            "    depends_on: 1\n"
            "    reason: both modify cli.py\n"
        )
        deps = parse_file_plan(arch_dir)
        assert deps == [(2, 1)]  # only the explicit one, no duplicate

    def test_handles_yaml_on_as_boolean_true(self, arch_dir):
        """YAML parses bare 'on:' as boolean True. parse_file_plan handles this."""
        (arch_dir / "otto_arch" / "file-plan.md").write_text(
            "recommended_dependencies:\n"
            "  - from: 2\n"
            "    on: 1\n"
            "    reason: both modify cli.py\n"
        )
        deps = parse_file_plan(arch_dir)
        assert deps == [(2, 1)]

    def test_ignores_non_int_ids(self, arch_dir):
        (arch_dir / "otto_arch" / "file-plan.md").write_text(
            "recommended_dependencies:\n"
            "  - from: foo\n"
            "    depends_on: 1\n"
            "    reason: test\n"
        )
        assert parse_file_plan(arch_dir) == []


class TestPilotRole:
    def test_pilot_role_gets_correct_files(self, arch_dir):
        (arch_dir / "otto_arch" / "file-plan.md").write_text("# File Plan\nno deps")
        ctx = load_design_context(arch_dir, "pilot")
        assert "Codebase" in ctx
        assert "Task Decisions" in ctx
        assert "File Plan" in ctx
        # Should NOT include test patterns or conventions
        assert "Test Patterns" not in ctx
        assert "Conventions" not in ctx


class TestMeta:
    def test_round_trip(self, arch_dir):
        _save_meta(arch_dir, {"gotcha_count": 7, "extra": "data"})
        meta = _load_meta(arch_dir)
        assert meta["gotcha_count"] == 7
        assert meta["extra"] == "data"

    def test_load_returns_empty_on_missing(self, tmp_path):
        assert _load_meta(tmp_path) == {}
