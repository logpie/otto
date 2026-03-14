"""Tests for otto.rubric module."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
import json
from otto.rubric import _gather_project_context, generate_rubric, _parse_rubric_output, parse_markdown_tasks


class TestGatherProjectContext:
    def test_includes_file_tree(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmp_path, capture_output=True)
        (tmp_path / "src.py").write_text("def hello(): pass")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add src"], cwd=tmp_path, capture_output=True)
        ctx = _gather_project_context(tmp_path)
        assert "src.py" in ctx

    def test_includes_source_content(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmp_path, capture_output=True)
        (tmp_path / "app.py").write_text("class MyApp:\n    pass\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add app"], cwd=tmp_path, capture_output=True)
        ctx = _gather_project_context(tmp_path)
        assert "class MyApp" in ctx

    def test_skips_lockfiles(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmp_path, capture_output=True)
        (tmp_path / "poetry.lock").write_text("hash123")
        (tmp_path / "app.py").write_text("x = 1")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add files"], cwd=tmp_path, capture_output=True)
        ctx = _gather_project_context(tmp_path)
        assert "hash123" not in ctx
        assert "x = 1" in ctx


class TestParseRubricOutput:
    def test_numbered_list(self):
        text = "1. criterion one\n2. criterion two\n3. criterion three"
        result = _parse_rubric_output(text)
        assert result == ["criterion one", "criterion two", "criterion three"]

    def test_bullet_list(self):
        text = "- criterion one\n- criterion two"
        result = _parse_rubric_output(text)
        assert result == ["criterion one", "criterion two"]

    def test_mixed_format(self):
        text = "1) first\n* second\n- third"
        result = _parse_rubric_output(text)
        assert result == ["first", "second", "third"]

    def test_skips_empty_lines(self):
        text = "1. first\n\n2. second\n\n"
        result = _parse_rubric_output(text)
        assert result == ["first", "second"]


class TestGenerateRubric:
    @patch("otto.rubric.subprocess.run")
    def test_returns_parsed_rubric(self, mock_run, tmp_path):
        def side_effect(*args, **kwargs):
            m = MagicMock()
            m.returncode = 0
            if args[0] == ["git", "ls-files"]:
                m.stdout = "app.py"
            else:
                m.stdout = "1. search is case-insensitive\n2. no matches returns empty list\n3. partial matches work"
            return m
        mock_run.side_effect = side_effect
        rubric = generate_rubric("Add search", tmp_path)
        assert len(rubric) == 3
        assert "case-insensitive" in rubric[0]

    @patch("otto.rubric.subprocess.run")
    def test_returns_empty_on_failure(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        rubric = generate_rubric("Add search", tmp_path)
        assert rubric == []


class TestParseMarkdownTasks:
    @patch("otto.rubric.subprocess.run")
    def test_extracts_tasks(self, mock_run, tmp_path):
        md_file = tmp_path / "features.md"
        md_file.write_text("# Search\nAdd search.\n\n# Tags\nAdd tags.\n")

        def side_effect(*args, **kwargs):
            m = MagicMock()
            m.returncode = 0
            if args[0] == ["git", "ls-files"]:
                m.stdout = "app.py"
            else:
                m.stdout = json.dumps([
                    {"prompt": "Add search method", "rubric": ["case-insensitive"], "context": "store.py"},
                    {"prompt": "Add tags support", "rubric": ["by_tag filters"], "context": ""},
                ])
            return m
        mock_run.side_effect = side_effect

        tasks = parse_markdown_tasks(md_file, tmp_path)
        assert len(tasks) == 2
        assert tasks[0]["prompt"] == "Add search method"
        assert tasks[0]["rubric"] == ["case-insensitive"]
        assert tasks[0]["context"] == "store.py"

    @patch("otto.rubric.subprocess.run")
    def test_raises_on_invalid_json(self, mock_run, tmp_path):
        md_file = tmp_path / "features.md"
        md_file.write_text("# Task\nDo something.\n")

        def side_effect(*args, **kwargs):
            m = MagicMock()
            m.returncode = 0
            if args[0] == ["git", "ls-files"]:
                m.stdout = ""
            else:
                m.stdout = "This is not JSON"
            return m
        mock_run.side_effect = side_effect

        import pytest as _pytest
        with _pytest.raises(ValueError, match="Failed to parse"):
            parse_markdown_tasks(md_file, tmp_path)

    @patch("otto.rubric.subprocess.run")
    def test_raises_on_empty_prompt(self, mock_run, tmp_path):
        md_file = tmp_path / "features.md"
        md_file.write_text("# Task\nDo something.\n")

        def side_effect(*args, **kwargs):
            m = MagicMock()
            m.returncode = 0
            if args[0] == ["git", "ls-files"]:
                m.stdout = ""
            else:
                m.stdout = json.dumps([{"prompt": "", "rubric": [], "context": ""}])
            return m
        mock_run.side_effect = side_effect

        import pytest as _pytest
        with _pytest.raises(ValueError, match="missing 'prompt'"):
            parse_markdown_tasks(md_file, tmp_path)
