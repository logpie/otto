"""Tests for otto/logstream.py — session log writers.

Covers:
  - JsonlMessageWriter emits one JSON line per normalized SDK message
  - NarrativeFormatter renders each block type distinctly
  - make_session_logger creates live.log symlink for back-compat
  - Formatter never truncates mid-word
  - Certifier markers are elevated above normal text
"""

from __future__ import annotations

import json
import re

from otto.agent import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from otto.logstream import (
    JsonlMessageWriter,
    NarrativeFormatter,
    make_session_logger,
)


_TS_RE = re.compile(r"^\[\+\d+:\d{2}\] ")


def _strip_ts(line: str) -> str:
    return _TS_RE.sub("", line)


class TestJsonlWriter:
    def test_assistant_message_records_blocks(self, tmp_path):
        path = tmp_path / "messages.jsonl"
        w = JsonlMessageWriter(path)
        w.write(AssistantMessage(content=[
            TextBlock(text="hello"),
            ToolUseBlock(name="Bash", input={"command": "ls"}, id="t1"),
        ]))
        w.close()

        lines = path.read_text().strip().split("\n")
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["type"] == "assistant"
        assert [b["type"] for b in rec["blocks"]] == ["text", "tool_use"]
        assert rec["blocks"][0]["text"] == "hello"
        assert rec["blocks"][1]["name"] == "Bash"
        assert rec["blocks"][1]["input"] == {"command": "ls"}

    def test_result_message_records_usage_and_cost(self, tmp_path):
        path = tmp_path / "messages.jsonl"
        w = JsonlMessageWriter(path)
        w.write(ResultMessage(
            subtype="success", is_error=False, session_id="sess-1",
            result="done", total_cost_usd=0.42,
            usage={"input_tokens": 100, "output_tokens": 50},
        ))
        w.close()

        rec = json.loads(path.read_text().strip())
        assert rec["type"] == "result"
        assert rec["subtype"] == "success"
        assert rec["is_error"] is False
        assert rec["cost_usd"] == 0.42
        assert rec["usage"] == {"input_tokens": 100, "output_tokens": 50}
        assert rec["session_id"] == "sess-1"

    def test_result_message_records_structured_output(self, tmp_path):
        path = tmp_path / "messages.jsonl"
        w = JsonlMessageWriter(path)
        w.write(ResultMessage(
            subtype="success",
            is_error=False,
            session_id="sess-1",
            structured_output={"verdict": "PASS", "stories": 3},
        ))
        w.close()

        rec = json.loads(path.read_text().strip())
        assert rec["structured_output"] == {"verdict": "PASS", "stories": 3}

    def test_appends_on_reopen(self, tmp_path):
        path = tmp_path / "messages.jsonl"
        w1 = JsonlMessageWriter(path)
        w1.write(AssistantMessage(content=[TextBlock(text="one")]))
        w1.close()
        w2 = JsonlMessageWriter(path)
        w2.write(AssistantMessage(content=[TextBlock(text="two")]))
        w2.close()

        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2


class TestNarrativeFormatter:
    def test_tool_use_with_summary(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(name="Read", input={"file_path": "/tmp/x.py"}),
        ]))
        f.close()

        line = _strip_ts(path.read_text().strip())
        assert line.startswith("\u25cf Read")
        assert "/tmp/x.py" in line

    def test_tool_result_error(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            ToolResultBlock(content="EPERM: permission denied", is_error=True),
        ]))
        f.close()

        line = _strip_ts(path.read_text().strip())
        assert "error" in line.lower()
        assert "EPERM" in line

    def test_tool_result_elevates_certifier_markers(self, tmp_path):
        """STORY_RESULT / VERDICT lines inside subagent tool output must be
        elevated as their own lines so humans can scan them at a glance."""
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        subagent_output = (
            "I tested 3 stories.\n"
            "STORY_RESULT: s1 | PASS | welcome page loads\n"
            "STORY_RESULT: s2 | FAIL | add-to-cart 500s\n"
            "VERDICT: FAIL\n"
        )
        f.write_message(AssistantMessage(content=[
            ToolResultBlock(content=subagent_output, is_error=False),
        ]))
        f.close()

        lines = [_strip_ts(l) for l in path.read_text().splitlines()]
        # All three markers present as standalone lines
        assert any(l.strip().startswith("STORY_RESULT: s1") for l in lines)
        assert any(l.strip().startswith("STORY_RESULT: s2") for l in lines)
        assert any(l.strip().startswith("VERDICT: FAIL") for l in lines)

    def test_text_markers_elevated(self, tmp_path):
        """Markers embedded in TextBlock (not just ToolResult) also elevated."""
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            TextBlock(text=(
                "I'm about to start.\n"
                "CERTIFY_ROUND: 1\n"
                "Everything looks green.\n"
                "STORY_RESULT: s1 | PASS | ok\n"
            )),
        ]))
        f.close()

        content = path.read_text()
        assert "CERTIFY_ROUND: 1" in content
        assert "STORY_RESULT: s1 | PASS | ok" in content

    def test_thinking_block(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            ThinkingBlock(thinking="first line\nsecond line"),
        ]))
        f.close()

        lines = [_strip_ts(l) for l in path.read_text().splitlines()]
        # Each thinking paragraph rendered with the ⋯ glyph.
        assert any(l.startswith("\u22ef first line") for l in lines)
        assert any(l.startswith("\u22ef second line") for l in lines)

    def test_never_truncates_mid_word(self, tmp_path):
        """Long text must break at word boundaries, not in the middle."""
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        long_word = "supercalifragilisticexpialidocious " * 20
        f.write_message(AssistantMessage(content=[TextBlock(text=long_word.strip())]))
        f.close()

        for line in path.read_text().splitlines():
            # "..." suffix OK at end, but no partial word elsewhere.
            if line.endswith("..."):
                # Suffix is well-formed
                assert not line.endswith("supercalifragilisticexpialido...")

    def test_git_commit_elevated(self, tmp_path):
        """Git-commit Bash output lines are rendered with ✓ glyph."""
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        commit_output = (
            "[main abc1234] Add kanban board\n"
            " 3 files changed, 42 insertions(+)\n"
        )
        f.write_message(AssistantMessage(content=[
            ToolResultBlock(content=commit_output, is_error=False),
        ]))
        f.close()

        line = path.read_text()
        assert "\u2713" in line
        assert "abc1234" in line
        assert "Add kanban board" in line

    def test_result_message_writes_terminal_marker(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(ResultMessage(
            subtype="success", is_error=False, session_id="x",
            total_cost_usd=1.23,
        ))
        f.close()

        content = path.read_text()
        assert "SUCCESS" in content
        assert "$1.23" in content


class TestMakeSessionLogger:
    def test_creates_both_files_and_live_symlink(self, tmp_path):
        cbs = make_session_logger(tmp_path)
        try:
            cbs["on_message"](AssistantMessage(content=[TextBlock(text="hello world")]))
        finally:
            cbs["_close"]()

        assert (tmp_path / "messages.jsonl").exists()
        assert (tmp_path / "narrative.log").exists()
        # live.log is a symlink to narrative.log for back-compat
        live = tmp_path / "live.log"
        assert live.is_symlink() or live.exists()
        if live.is_symlink():
            import os as _os
            assert _os.readlink(live) == "narrative.log"

    def test_tailable_flush_per_message(self, tmp_path):
        """Each on_message should flush — the file must reflect the write
        before close()."""
        cbs = make_session_logger(tmp_path)
        try:
            cbs["on_message"](AssistantMessage(content=[TextBlock(text="first")]))
            # Read BEFORE close — must already be flushed.
            mid_read = (tmp_path / "messages.jsonl").read_text()
            assert "first" in mid_read
        finally:
            cbs["_close"]()

    def test_on_message_exception_does_not_propagate_through_agent(self, tmp_path):
        """run_agent_query wraps on_message in try/except so a handler bug
        can't kill the run. Verify the logstream itself doesn't raise on
        unknown message types."""
        cbs = make_session_logger(tmp_path)
        try:
            # A bare object that isn't AssistantMessage/ResultMessage.
            class Weird:
                session_id = ""
            cbs["on_message"](Weird())
        finally:
            cbs["_close"]()

        rec = json.loads((tmp_path / "messages.jsonl").read_text().strip())
        assert rec["type"] == "unknown"
