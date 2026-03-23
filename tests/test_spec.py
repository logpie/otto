"""Tests for otto.spec module."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from otto.spec import generate_spec, _parse_spec_output, parse_markdown_tasks


class TestParseSpecOutput:
    def test_numbered_list(self):
        text = "1. criterion one\n2. criterion two\n3. criterion three"
        result = _parse_spec_output(text)
        assert len(result) == 3
        assert result[0] == {"text": "criterion one", "verifiable": True}
        assert result[2] == {"text": "criterion three", "verifiable": True}

    def test_bullet_list(self):
        text = "- criterion one\n- criterion two"
        result = _parse_spec_output(text)
        assert len(result) == 2
        assert result[0]["text"] == "criterion one"

    def test_mixed_format(self):
        text = "1) first\n* second\n- third"
        result = _parse_spec_output(text)
        assert len(result) == 3
        assert result[0]["text"] == "first"
        assert result[2]["text"] == "third"

    def test_skips_empty_lines(self):
        text = "1. first\n\n2. second\n\n"
        result = _parse_spec_output(text)
        assert len(result) == 2

    def test_verifiable_tag(self):
        text = "[verifiable] search is case-insensitive"
        result = _parse_spec_output(text)
        assert len(result) == 1
        assert result[0]["text"] == "search is case-insensitive"
        assert result[0]["verifiable"] is True
        assert "test_hint" not in result[0]  # hint parsing removed (C3)

    def test_visual_tag(self):
        text = "[visual] Apple Weather-style gradient backgrounds"
        result = _parse_spec_output(text)
        assert len(result) == 1
        assert result[0]["text"] == "Apple Weather-style gradient backgrounds"
        assert result[0]["verifiable"] is False
        assert "test_hint" not in result[0]

    def test_mixed_classified(self):
        text = (
            "1. [verifiable] latency <300ms\n"
            "2. [visual] smooth transitions\n"
            "3. plain criterion without tag"
        )
        result = _parse_spec_output(text)
        assert len(result) == 3
        assert result[0]["verifiable"] is True
        assert result[0]["text"] == "latency <300ms"
        assert "test_hint" not in result[0]  # hint parsing removed (C3)
        assert result[1]["verifiable"] is False
        assert result[2]["verifiable"] is True  # default


class TestGenerateSpec:
    @patch("otto.spec.build_project_map", return_value="PROJECT MAP")
    @patch("otto.spec.query")
    def test_returns_parsed_spec(self, mock_query, mock_project_map, tmp_path):
        """Agent writes spec to a temp file, we parse it."""
        async def fake_query(*, prompt, options=None):
            assert "PROJECT FILES (for context — do not prescribe file structure in specs):\nPROJECT MAP" in prompt
            # Extract the spec file path from the prompt
            import re
            match = re.search(r'(?:criteria|spec) to: (.+\.txt)', prompt)
            if match:
                spec_path = Path(match.group(1))
                spec_path.write_text(
                    "1. search is case-insensitive\n"
                    "2. no matches returns empty list\n"
                    "3. partial matches work\n"
                )
            from otto._agent_stub import ResultMessage
            yield ResultMessage(session_id="s1")

        mock_query.side_effect = fake_query
        spec = generate_spec("Add search", tmp_path)
        mock_project_map.assert_called_once_with(tmp_path)
        assert len(spec) == 3
        assert "case-insensitive" in spec[0]["text"]

    @patch("otto.spec.query")
    def test_returns_empty_on_failure(self, mock_query, tmp_path):
        async def fake_query(*, prompt, options=None):
            raise RuntimeError("agent failed")

        mock_query.side_effect = fake_query
        spec = generate_spec("Add search", tmp_path)
        assert spec == []


class TestParseMarkdownTasks:
    @patch("otto.spec.query")
    def test_extracts_tasks(self, mock_query, tmp_path):
        md_file = tmp_path / "features.md"
        md_file.write_text("# Search\nAdd search.\n\n# Tags\nAdd tags.\n")

        async def fake_query(*, prompt, options=None):
            import re
            match = re.search(r'JSON array to: (.+\.json)', prompt)
            if match:
                output_path = Path(match.group(1))
                output_path.write_text(json.dumps([
                    {"prompt": "Add search method", "spec": ["case-insensitive"]},
                    {"prompt": "Add tags support", "spec": ["by_tag filters"]},
                ]))
            from otto._agent_stub import ResultMessage
            yield ResultMessage(session_id="s1")

        mock_query.side_effect = fake_query

        tasks = parse_markdown_tasks(md_file, tmp_path)
        assert len(tasks) == 2
        assert tasks[0]["prompt"] == "Add search method"
        assert tasks[0]["spec"] == ["case-insensitive"]

    @patch("otto.spec.query")
    def test_markdown_parser_is_quiet(self, mock_query, tmp_path, capsys):
        md_file = tmp_path / "features.md"
        md_file.write_text("# Search\nAdd search.\n")

        async def fake_query(*, prompt, options=None):
            import re
            from dataclasses import dataclass

            @dataclass
            class FakeTextBlock:
                text: str

            @dataclass
            class FakeToolUseBlock:
                name: str
                input: dict

            @dataclass
            class FakeAssistantMessage:
                content: list

            match = re.search(r'JSON array to: (.+\.json)', prompt)
            if match:
                Path(match.group(1)).write_text(json.dumps([
                    {"prompt": "Add search method", "spec": ["case-insensitive"]},
                ]))
            yield FakeAssistantMessage(content=[
                FakeTextBlock("thinking out loud"),
                FakeToolUseBlock("Read", {"file_path": "src/app.py"}),
            ])
            from otto._agent_stub import ResultMessage
            yield ResultMessage(session_id="s1")

        mock_query.side_effect = fake_query

        tasks = parse_markdown_tasks(md_file, tmp_path)
        captured = capsys.readouterr()
        assert tasks[0]["prompt"] == "Add search method"
        assert captured.out == ""
        assert captured.err == ""

    @patch("otto.spec.query")
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
