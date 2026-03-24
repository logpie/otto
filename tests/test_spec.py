"""Tests for otto.spec module."""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from otto.spec import (
    async_generate_spec,
    generate_spec,
    _parse_spec_output,
    parse_markdown_tasks,
)


class TestParseSpecOutput:
    def test_numbered_list(self):
        text = "1. criterion one\n2. criterion two\n3. criterion three"
        result = _parse_spec_output(text)
        assert len(result) == 3
        assert result[0] == {"text": "criterion one", "binding": "must", "verifiable": True}
        assert result[2] == {"text": "criterion three", "binding": "must", "verifiable": True}

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

    def test_must_tag(self):
        text = "[must] rate-limited requests return HTTP 429"
        result = _parse_spec_output(text)
        assert len(result) == 1
        assert result[0]["text"] == "rate-limited requests return HTTP 429"
        assert result[0]["binding"] == "must"
        assert result[0]["verifiable"] is True

    def test_should_tag(self):
        text = "[should] prefer inline explanations"
        result = _parse_spec_output(text)
        assert len(result) == 1
        assert result[0]["text"] == "prefer inline explanations"
        assert result[0]["binding"] == "should"
        assert result[0]["verifiable"] is True  # [should] is verifiable by default

    def test_should_visual_tag(self):
        text = "[should ◈] colors match existing theme"
        result = _parse_spec_output(text)
        assert len(result) == 1
        assert result[0]["text"] == "colors match existing theme"
        assert result[0]["binding"] == "should"
        assert result[0]["verifiable"] is False  # ◈ = non-verifiable

    def test_must_visual_tag(self):
        text = "[must ◈] card renders in weather details area"
        result = _parse_spec_output(text)
        assert len(result) == 1
        assert result[0]["text"] == "card renders in weather details area"
        assert result[0]["binding"] == "must"
        assert result[0]["verifiable"] is False  # ◈ = non-verifiable

    def test_must_should_mixed(self):
        text = (
            "1. [must] returns 429 for rate-limited requests\n"
            "2. [should] include retry-after header\n"
            "3. plain criterion defaults to must"
        )
        result = _parse_spec_output(text)
        assert len(result) == 3
        assert result[0]["binding"] == "must"
        assert result[1]["binding"] == "should"
        assert result[2]["binding"] == "must"

    def test_backward_compat_verifiable(self):
        """[verifiable] still works, maps to binding=must."""
        text = "[verifiable] search is case-insensitive"
        result = _parse_spec_output(text)
        assert result[0]["binding"] == "must"
        assert result[0]["verifiable"] is True

    def test_backward_compat_visual(self):
        """[visual] still works, maps to binding=should."""
        text = "[visual] smooth transitions"
        result = _parse_spec_output(text)
        assert result[0]["binding"] == "should"
        assert result[0]["verifiable"] is False


class TestGenerateSpec:
    @patch("otto.spec.query")
    def test_returns_parsed_spec(self, mock_query, tmp_path):
        """Agent writes spec to a temp file, we parse it."""
        async def fake_query(*, prompt, options=None):
            assert "PROJECT FILES" not in prompt
            assert "Read only what you need" in prompt
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
        assert len(spec) == 3
        assert "case-insensitive" in spec[0]["text"]

    @patch("otto.spec.query")
    def test_returns_empty_on_failure(self, mock_query, tmp_path):
        async def fake_query(*, prompt, options=None):
            if False:
                yield None
            raise RuntimeError("agent failed")

        mock_query.side_effect = fake_query
        spec = generate_spec("Add search", tmp_path)
        assert spec == []

    @patch("otto.spec.query")
    def test_async_returns_structured_error_on_failure(self, mock_query, tmp_path):
        async def fake_query(*, prompt, options=None):
            if False:
                yield None
            raise RuntimeError("agent failed")

        mock_query.side_effect = fake_query

        spec, cost, error = asyncio.run(async_generate_spec("Add search", tmp_path))

        assert spec == []
        assert cost == 0.0
        assert error == "agent failed"


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

    @patch("otto.spec.query")
    def test_raises_on_agent_result_error(self, mock_query, tmp_path):
        md_file = tmp_path / "features.md"
        md_file.write_text("# Task\nDo something.\n")

        async def fake_query(*, prompt, options=None):
            from otto._agent_stub import ResultMessage
            yield ResultMessage(session_id="s1", is_error=True, result="agent exploded")

        mock_query.side_effect = fake_query

        with pytest.raises(ValueError, match="agent exploded"):
            parse_markdown_tasks(md_file, tmp_path)
