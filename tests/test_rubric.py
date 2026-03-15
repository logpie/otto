"""Tests for otto.rubric module."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from otto.rubric import generate_rubric, _parse_rubric_output, parse_markdown_tasks


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
    @patch("otto.rubric.query")
    def test_returns_parsed_rubric(self, mock_query, tmp_path):
        """Agent writes rubric to a temp file, we parse it."""
        async def fake_query(*, prompt, options=None):
            # Extract the rubric file path from the prompt
            import re
            match = re.search(r'criteria to: (.+\.txt)', prompt)
            if match:
                rubric_path = Path(match.group(1))
                rubric_path.write_text(
                    "1. search is case-insensitive\n"
                    "2. no matches returns empty list\n"
                    "3. partial matches work\n"
                )
            from otto._agent_stub import ResultMessage
            yield ResultMessage(session_id="s1")

        mock_query.side_effect = fake_query
        rubric = generate_rubric("Add search", tmp_path)
        assert len(rubric) == 3
        assert "case-insensitive" in rubric[0]

    @patch("otto.rubric.query")
    def test_returns_empty_on_failure(self, mock_query, tmp_path):
        async def fake_query(*, prompt, options=None):
            raise RuntimeError("agent failed")

        mock_query.side_effect = fake_query
        rubric = generate_rubric("Add search", tmp_path)
        assert rubric == []


class TestParseMarkdownTasks:
    @patch("otto.rubric.query")
    def test_extracts_tasks(self, mock_query, tmp_path):
        md_file = tmp_path / "features.md"
        md_file.write_text("# Search\nAdd search.\n\n# Tags\nAdd tags.\n")

        async def fake_query(*, prompt, options=None):
            import re
            match = re.search(r'JSON array to: (.+\.json)', prompt)
            if match:
                output_path = Path(match.group(1))
                output_path.write_text(json.dumps([
                    {"prompt": "Add search method", "rubric": ["case-insensitive"]},
                    {"prompt": "Add tags support", "rubric": ["by_tag filters"]},
                ]))
            from otto._agent_stub import ResultMessage
            yield ResultMessage(session_id="s1")

        mock_query.side_effect = fake_query

        tasks = parse_markdown_tasks(md_file, tmp_path)
        assert len(tasks) == 2
        assert tasks[0]["prompt"] == "Add search method"
        assert tasks[0]["rubric"] == ["case-insensitive"]

    @patch("otto.rubric.query")
    def test_raises_when_agent_doesnt_write_file(self, mock_query, tmp_path):
        md_file = tmp_path / "features.md"
        md_file.write_text("# Task\nDo something.\n")

        async def fake_query(*, prompt, options=None):
            from otto._agent_stub import ResultMessage
            yield ResultMessage(session_id="s1")

        mock_query.side_effect = fake_query

        import pytest as _pytest
        with _pytest.raises(ValueError, match="did not write"):
            parse_markdown_tasks(md_file, tmp_path)
