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
import time

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
    def test_start_emits_build_banner(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.start()
        f.close()

        assert _strip_ts(path.read_text().strip()) == "\u2501\u2501\u2501 BUILD starting \u2501\u2501\u2501"

    def test_start_emits_spec_banner(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path, phase_name="SPEC")
        f.start()
        f.close()

        assert _strip_ts(path.read_text().strip()) == "\u2501\u2501\u2501 SPEC starting \u2501\u2501\u2501"

    def test_first_certifier_dispatch_emits_build_handoff_then_round_start(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.start()
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(
                name="Agent",
                input={"prompt": "Please certify this.\n## Verdict Format\nVERDICT: PASS|FAIL"},
                id="cert-1",
            ),
        ]))
        f.close()

        lines = [_strip_ts(line) for line in path.read_text().splitlines()]
        assert lines == [
            "\u2501\u2501\u2501 BUILD starting \u2501\u2501\u2501",
            "\u2501\u2501\u2501 BUILD complete — handing off to certifier \u2501\u2501\u2501",
            "\u2501\u2501\u2501 CERTIFY ROUND 1 starting \u2501\u2501\u2501",
        ]

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

        content = path.read_text()
        # All three markers present as standalone lines, each carrying
        # the ✦ marker glyph for human scanners.
        assert "\u2726 STORY_RESULT: s1 | PASS | welcome page loads" in content
        assert "\u2726 STORY_RESULT: s2 | FAIL | add-to-cart 500s" in content
        assert "\u2726 VERDICT: FAIL" in content

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
        # Terminal marker carries a duration suffix: "in 0:00" etc.
        assert re.search(r" in \d+:\d{2}", content)

    def test_write_result_emits_run_summary_before_success(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f._start = time.monotonic() - 100.2
        f._build_duration = 30.0
        f._round_timings = [(30.0, 50.0), (60.0, 80.0)]
        f.write_message(ResultMessage(
            subtype="success", is_error=False, session_id="x",
            total_cost_usd=1.23,
        ))
        f.close()

        lines = [_strip_ts(line) for line in path.read_text().splitlines()]
        assert "RUN SUMMARY: build=0:30, certify=0:40 (2 rounds), total=$1.23 1:40" in lines[0]
        assert "SUCCESS $1.23 in 1:40" in lines[1]

    def test_finalize_with_phase_costs_emits_cost_annotated_summary(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f._start = time.monotonic() - 271.0
        f.write_message(ResultMessage(
            subtype="success", is_error=False, session_id="x",
            total_cost_usd=0.98,
        ))
        f.finalize({
            "build": {"duration_s": 138.0, "cost_usd": 0.49},
            "certify": {"duration_s": 111.0, "cost_usd": 0.41, "rounds": 2},
        })
        f.close()

        lines = [_strip_ts(line) for line in path.read_text().splitlines()]
        assert (
            "RUN SUMMARY: build=$0.49 2:18, certify=$0.41 1:51 (2 rounds), "
            "total=$0.98 4:31"
        ) in lines[0]
        assert "SUCCESS $0.98 in 4:31" in lines[1]

    def test_finalize_without_phase_costs_omits_cost_annotations(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f._start = time.monotonic() - 271.0
        f.write_message(ResultMessage(
            subtype="success", is_error=False, session_id="x",
            total_cost_usd=0.98,
        ))
        f.finalize({
            "build": {"duration_s": 138.0},
            "certify": {"duration_s": 111.0, "rounds": 2},
        })
        f.close()

        summary = _strip_ts(path.read_text().splitlines()[0])
        assert "RUN SUMMARY: build=2:18, certify=1:51 (2 rounds), total=$0.98 4:31" in summary
        assert "build=$" not in summary
        assert "certify=$" not in summary

    def test_finalize_no_qa_shape_omits_certify_entry(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f._start = time.monotonic() - 65.2
        f.write_message(ResultMessage(
            subtype="success", is_error=False, session_id="x",
            total_cost_usd=0.55,
        ))
        f.finalize({"build": {"duration_s": 65.2}})
        f.close()

        summary = _strip_ts(path.read_text().splitlines()[0])
        assert "RUN SUMMARY: build=1:05, total=$0.55 1:05" in summary
        assert "certify=" not in summary

    def test_finalize_standalone_certify_shape(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path, phase_name="CERTIFY")
        f._start = time.monotonic() - 15.1
        f.write_message(ResultMessage(
            subtype="success", is_error=False, session_id="x",
            total_cost_usd=0.08,
        ))
        f.finalize({"certify": {"duration_s": 15.0, "rounds": 1}})
        f.close()

        lines = [_strip_ts(line) for line in path.read_text().splitlines()]
        assert lines[0] == "\u2501\u2501\u2501 CERTIFY complete \u2501\u2501\u2501"
        assert "RUN SUMMARY: certify=0:15 (1 round), total=$0.08 0:15" in lines[1]
        assert "SUCCESS $0.08 in 0:15" in lines[2]

    def test_write_result_without_certify_omits_certify_summary(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f._start = time.monotonic() - 65.2
        f.write_message(ResultMessage(
            subtype="success", is_error=False, session_id="x",
        ))
        f.close()

        summary = _strip_ts(path.read_text().splitlines()[0])
        assert "RUN SUMMARY: build=1:05, total=1:05" in summary
        assert "certify=" not in summary

    def test_spec_phase_emits_complete_before_summary(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path, phase_name="SPEC")
        f.start()
        f.write_message(ResultMessage(
            subtype="success", is_error=False, session_id="x",
        ))
        f.close()

        lines = [_strip_ts(line) for line in path.read_text().splitlines()]
        assert lines[0] == "\u2501\u2501\u2501 SPEC starting \u2501\u2501\u2501"
        assert lines[1] == "\u2501\u2501\u2501 SPEC complete \u2501\u2501\u2501"
        assert "RUN SUMMARY: spec=" in lines[2]
        assert "SUCCESS" in lines[3]

    def test_agent_tool_use_renders_subagent_summary(self, tmp_path):
        """Agent tool_use renders subagent=<type> plus prompt preview."""
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(
                name="Agent",
                input={
                    "subagent_type": "general-purpose",
                    "prompt": "Run the kanban certifier and report STORY_RESULT lines for each story.",
                },
                id="t-agent-1",
            ),
        ]))
        f.close()

        line = _strip_ts(path.read_text().strip())
        assert line.startswith("\u25cf Agent ")
        assert "subagent=general-purpose" in line
        assert "Run the kanban certifier" in line

    def test_subagent_result_extracts_text_from_json_list(self, tmp_path):
        """Subagent tool_result content is a JSON list of text dicts —
        extract and render with the subagent glyph."""
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(name="Agent", input={"prompt": "test"}, id="t-s1"),
        ]))
        # Subagent returned a canonical list-of-text payload.
        payload = json.dumps([
            {"type": "text", "text": "First line of result."},
            {"type": "text", "text": "Second fragment."},
        ])
        f.write_message(AssistantMessage(content=[
            ToolResultBlock(content=payload, tool_use_id="t-s1"),
        ]))
        f.close()

        content = path.read_text()
        assert "\u21d0 [subagent]:" in content
        assert "First line of result" in content
        assert "Second fragment" in content

    def test_heredoc_bash_command_is_single_line(self, tmp_path):
        """Embedded newlines in a Bash command must collapse to one row."""
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(
                name="Bash",
                input={"command": "git commit -m \"$(cat <<'EOF'\nMulti-line\nheredoc body\nEOF\n)\""},
                id="t-heredoc",
            ),
        ]))
        f.close()

        # Only ONE narrative line (plus optional trailing newline) — no
        # HEREDOC body breaking the format.
        stripped = path.read_text().strip()
        assert "\n" not in stripped

    def test_read_result_strips_line_number_prefix(self, tmp_path):
        """Read returns `N\\t<content>` per line — narrative shows "(N lines)"
        or strips the prefix."""
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(name="Read", input={"file_path": "/tmp/x.json"}, id="t-r1"),
        ]))
        f.write_message(AssistantMessage(content=[
            ToolResultBlock(content="1\t{\n2\t  \"key\": \"value\"\n3\t}\n",
                            tool_use_id="t-r1"),
        ]))
        f.close()

        content = path.read_text()
        # No raw `1\t{` substring leaking to the log.
        assert "1\t{" not in content
        # Either the collapsed "(N lines)" or a stripped preview.
        # First line is very short ("{"), so we collapse to a count.
        assert "lines)" in content

    def test_glob_result_shows_file_count(self, tmp_path):
        """Glob content is a newline-separated path list — narrative shows
        `(N files)` rather than "first path (N lines)"."""
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(name="Glob", input={"pattern": "**/*.py"}, id="t-g1"),
        ]))
        f.write_message(AssistantMessage(content=[
            ToolResultBlock(
                content="otto/agent.py\notto/cli.py\notto/logstream.py\n",
                tool_use_id="t-g1",
            ),
        ]))
        f.close()

        content = path.read_text()
        assert "(3 files)" in content
        assert "(3 lines)" not in content

    def test_write_result_strips_boilerplate_parenthetical(self, tmp_path):
        """Write/Edit results often trail a `(file state is current in your
        context — no need to Read it back)` boilerplate that must not leak
        into the narrative."""
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(name="Write", input={"file_path": "/tmp/x.py"}, id="t-w1"),
        ]))
        f.write_message(AssistantMessage(content=[
            ToolResultBlock(
                content="File created successfully. (file state is current in your context — no need to Read it back)",
                tool_use_id="t-w1",
            ),
        ]))
        f.close()

        content = path.read_text()
        assert "file state is current" not in content
        assert "File created successfully" in content

    def test_marker_uses_glyph(self, tmp_path):
        """Marker lines use the ✦ glyph for alignment with other rows."""
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            TextBlock(text="Some prose.\nSTORIES_TESTED: 5\nMore prose.\n"),
        ]))
        f.close()

        content = path.read_text()
        assert "\u2726 STORIES_TESTED: 5" in content

    def test_placeholder_markers_are_not_elevated(self, tmp_path):
        """`STORIES_TESTED: <number>` is a prompt template placeholder
        (not a real value) — must NOT be treated as a marker."""
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            TextBlock(text="Example format: STORIES_TESTED: <number>\n"),
        ]))
        f.close()

        content = path.read_text()
        # Should be rendered as ordinary text (▸), not elevated marker (✦).
        assert "\u2726 STORIES_TESTED:" not in content
        assert "\u25b8" in content

    def test_prompt_flood_is_collapsed(self, tmp_path):
        """Large prompt-like TextBlock (3+ ## headings) collapses to a
        single summary line instead of flooding the log with every line."""
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        flood = (
            "## Role\n"
            "You are a tester.\n"
            "## Inputs\n"
            "The spec is…\n"
            "## Verdict Format\n"
            "Emit VERDICT: PASS or FAIL.\n"
        )
        f.write_message(AssistantMessage(content=[TextBlock(text=flood)]))
        f.close()

        content = path.read_text().strip()
        lines = content.splitlines()
        # A single summary line — not one per prompt row.
        assert len(lines) == 1
        assert "subagent prompt" in lines[0]

    def test_closing_summary_text_uses_summary_glyph(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(
                name="Agent",
                input={"prompt": "Please certify.\n## Verdict Format\nVERDICT: PASS|FAIL"},
                id="cert-1",
            ),
            TextBlock(text="## Summary\n- shipped\n- verified\n- closed"),
        ]))
        f.close()

        content = path.read_text()
        assert "\u220e ## Summary" in content
        assert "\u220e - shipped" in content

    def test_inline_agent_prose_still_uses_standard_glyph(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            TextBlock(text="Now dispatching the certifier."),
        ]))
        f.close()

        assert "\u25b8 Now dispatching the certifier." in path.read_text()

    def test_session_id_propagated_onto_assistant_message(self, tmp_path):
        """messages.jsonl records session_id on AssistantMessage entries,
        not only ResultMessage."""
        from otto.agent import _normalize_message

        class _FakeSDKMessage:
            def __init__(self):
                self.content = [TextBlock(text="hi")]
                self.session_id = "sess-abc"

        normalized = _normalize_message(_FakeSDKMessage())
        assert isinstance(normalized, AssistantMessage)
        assert normalized.session_id == "sess-abc"

        path = tmp_path / "messages.jsonl"
        w = JsonlMessageWriter(path)
        w.write(normalized)
        w.close()
        rec = json.loads(path.read_text().strip())
        assert rec["session_id"] == "sess-abc"
        assert rec["type"] == "assistant"

    def test_tool_result_only_message_tagged_as_user(self, tmp_path):
        """A message containing ONLY ToolResultBlocks is semantically a
        user turn (tool outputs fed back) — jsonl type must be 'user'."""
        from otto.agent import UserMessage, _normalize_message

        class _FakeSDKMessage:
            def __init__(self):
                self.content = [ToolResultBlock(content="ok", tool_use_id="t1")]
                self.session_id = "sess-xyz"

        normalized = _normalize_message(_FakeSDKMessage())
        assert isinstance(normalized, UserMessage)
        assert normalized.session_id == "sess-xyz"

        path = tmp_path / "messages.jsonl"
        w = JsonlMessageWriter(path)
        w.write(normalized)
        w.close()
        rec = json.loads(path.read_text().strip())
        assert rec["type"] == "user"
        assert rec["session_id"] == "sess-xyz"

    def test_clock_format_past_one_hour(self, tmp_path):
        """At t >= 3600s, elapsed clock uses H:MM:SS, not M:SS."""
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        # Rewind the start baseline by 65 minutes.
        f._start = f._start - 65 * 60
        f.write_message(AssistantMessage(content=[TextBlock(text="late")]))
        f.close()

        content = path.read_text()
        assert re.search(r"\[\+1:0[5-9]:\d{2}\]", content)


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
