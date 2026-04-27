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

import pytest

from otto.agent import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from otto.logstream import (
    JsonlMessageWriter,
    NarrativeFormatter,
    _parse_story_result_marker,
    browser_efficiency_outlier,
    estimate_phase_costs,
    make_session_logger,
    normalize_phase_breakdown,
    summarize_browser_efficiency,
)
from otto.redaction import _TOKEN_PATTERNS


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

    def test_result_message_redacts_secret_text(self, tmp_path):
        path = tmp_path / "messages.jsonl"
        w = JsonlMessageWriter(path)
        w.write(ResultMessage(
            subtype="success",
            is_error=False,
            result="OPENAI_API_KEY=sk-secretvalue1234567890",
        ))
        w.close()

        rec = json.loads(path.read_text().strip())
        assert "sk-secretvalue1234567890" not in rec["result"]
        assert "[REDACTED:OPENAI_API_KEY]" in rec["result"]

    @pytest.mark.parametrize(
        ("secret_text", "expected"),
        [
            ("OPENAI_API_KEY=sk-secretvalue1234567890", "[REDACTED:OPENAI_API_KEY]"),
            ("token sk-ant-abcdefghijklmnopqrstuvwxyz", "sk-ant-REDACTED"),
            ("token ghp_abcdefghijklmnopqrstuvwxyz1234", "ghp_REDACTED"),
            ("token github_pat_abcdefghijklmnopqrstuvwxyz_1234", "github_pat_REDACTED"),
            ("token gho_abcdefghijklmnopqrstuvwxyz1234", "gho_REDACTED"),
            ("token ghs_abcdefghijklmnopqrstuvwxyz1234", "ghs_REDACTED"),
            ("token ghu_abcdefghijklmnopqrstuvwxyz1234", "ghu_REDACTED"),
            ("token AIzaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "AIzaREDACTED"),
            ("token sk-abcdefghijklmnopqrstuvwxyz123456", "sk-REDACTED"),
        ],
    )
    def test_result_message_redacts_all_supported_secret_patterns(self, tmp_path, secret_text, expected):
        path = tmp_path / "messages.jsonl"
        w = JsonlMessageWriter(path)
        w.write(ResultMessage(subtype="success", is_error=False, result=secret_text))
        w.close()

        rec = json.loads(path.read_text().strip())
        assert expected in rec["result"]
        for pattern, _replacement in _TOKEN_PATTERNS:
            match = pattern.search(secret_text)
            if match:
                assert match.group(0) not in rec["result"]

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

    def test_assistant_message_records_usage(self, tmp_path):
        path = tmp_path / "messages.jsonl"
        w = JsonlMessageWriter(path)
        w.write(AssistantMessage(
            content=[TextBlock(text="hello")],
            usage={"input_tokens": 12, "output_tokens": 34},
        ))
        w.close()

        rec = json.loads(path.read_text().strip())
        assert rec["type"] == "assistant"
        assert rec["usage"] == {"input_tokens": 12, "output_tokens": 34}

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

        assert _strip_ts(path.read_text().strip()) == "\u2014 BUILD starting \u2014"

    def test_start_emits_spec_banner(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path, phase_name="SPEC")
        f.start()
        f.close()

        assert _strip_ts(path.read_text().strip()) == "\u2014 SPEC starting \u2014"

    def test_display_phase_label_keeps_logical_summary_phase(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path, phase_name="CERTIFY", phase_label="CERTIFY ROUND 2")
        f._start = time.monotonic() - 15.0
        f.start()
        f.write_message(ResultMessage(
            subtype="success", is_error=False, session_id="x",
            usage={"input_tokens": 12, "output_tokens": 3},
        ))
        f.close()

        lines = [_strip_ts(line) for line in path.read_text().splitlines()]
        assert lines[0] == "\u2014 CERTIFY ROUND 2 starting \u2014"
        assert lines[1] == "\u2014 CERTIFY ROUND 2 complete \u2014"
        assert "RUN SUMMARY: certify=0:15" in lines[2]
        assert "CERTIFY ROUND 2" not in lines[2]

    def test_terminal_callback_fires_for_phase_banners(self, tmp_path):
        path = tmp_path / "narrative.log"
        seen: list[str] = []
        f = NarrativeFormatter(path, stdout_callback=seen.append)
        f.start()
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(
                name="Agent",
                input={"prompt": "Please certify.\n## Verdict Format\nVERDICT: PASS|FAIL"},
                id="cert-1",
            ),
            ToolResultBlock(
                tool_use_id="cert-1",
                content="STORIES_TESTED: 1\nSTORIES_PASSED: 1\nVERDICT: PASS\n",
            ),
        ]))
        f.close()

        assert seen[0] == "[dim]  \u2014 BUILD starting \u2014[/dim]"
        assert seen[1] == "[dim]  \u2014 BUILD complete; starting verification \u2014[/dim]"
        assert seen[2] == "[dim]  \u2014 CERTIFY ROUND 1 \u2014[/dim]"
        assert seen[3] == "[dim]  \u2014 CERTIFY ROUND 1 \u2192 PASS (1/1) \u2014[/dim]"
        assert all("[+" not in event for event in seen)

    def test_story_result_marker_prefers_structured_summary_field(self):
        story = _parse_story_result_marker(
            "STORY_RESULT: smoke | PASS | claim=Health endpoint responds | "
            "observed_result=Returned 200 OK | summary=Health check passed"
        )

        assert story is not None
        assert story["summary"] == "Health check passed"

    def test_terminal_callback_ignores_quiet_events(self, tmp_path):
        path = tmp_path / "narrative.log"
        seen: list[str] = []
        f = NarrativeFormatter(path, stdout_callback=seen.append)
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(name="Read", input={"file_path": "/tmp/x.py"}, id="read-1"),
            ToolResultBlock(tool_use_id="read-1", content="1\tprint('hi')\n"),
            ThinkingBlock(thinking="let me inspect this"),
            TextBlock(text="Now let me check the next file."),
        ]))
        f.close()

        assert seen == []

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
            "\u2014 BUILD starting \u2014",
            "\u2014 BUILD complete; starting verification \u2014",
            "\u2014 CERTIFY ROUND 1 \u2014",
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
            ToolUseBlock(name="WebFetch", input={"url": "https://example.com"}, id="web-1"),
            ToolResultBlock(content="EPERM: permission denied", tool_use_id="web-1", is_error=True),
        ]))
        f.close()

        line = _strip_ts(path.read_text().splitlines()[-1])
        assert line.startswith("\u26a0 tool WebFetch retry:")
        assert "EPERM" in line

    def test_tool_result_error_dropped_when_next_retry_succeeds(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(name="WebFetch", input={"url": "https://example.com"}, id="web-1"),
        ]))
        f.write_message(AssistantMessage(content=[
            ToolResultBlock(content="Invalid URL", tool_use_id="web-1", is_error=True),
        ]))
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(name="WebFetch", input={"url": "https://example.com"}, id="web-2"),
        ]))
        f.write_message(AssistantMessage(content=[
            ToolResultBlock(content="200 OK", tool_use_id="web-2", is_error=False),
        ]))
        f.write_message(ResultMessage(subtype="success", is_error=False, session_id="x", total_cost_usd=0.1))
        f.close()

        content = path.read_text()
        assert "\u26a0 tool WebFetch retry:" not in content
        assert "Notes: worked around" not in content

    def test_tool_result_error_dropped_when_different_tool_succeeds_next(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(name="Bash", input={"command": "npm test"}, id="bash-1"),
        ]))
        f.write_message(AssistantMessage(content=[
            ToolResultBlock(content="Exit code 1", tool_use_id="bash-1", is_error=True),
        ]))
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(name="Read", input={"file_path": "src/App.jsx"}, id="read-1"),
        ]))
        f.write_message(AssistantMessage(content=[
            ToolResultBlock(content="1\tconst x = 1\n", tool_use_id="read-1", is_error=False),
        ]))
        f.write_message(ResultMessage(subtype="success", is_error=False, session_id="x", total_cost_usd=0.1))
        f.close()

        content = path.read_text()
        assert "\u26a0 tool Bash retry:" not in content
        assert "Notes: worked around" not in content

    def test_tool_result_error_strips_xml_wrapper_and_truncates(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(name="Write", input={"file_path": "src/App.jsx"}, id="write-1"),
            ToolResultBlock(
                content=(
                    "<tool_use_error>File has not been read yet. "
                    "Read it first before writing to it. Extra trailing detail.</tool_use_error>"
                ),
                tool_use_id="write-1",
                is_error=True,
            ),
        ]))
        f.close()

        line = _strip_ts(path.read_text().splitlines()[-1])
        assert "<tool_use_error>" not in line
        assert len(line) < 120
        assert line.endswith("...")

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

    def test_tool_result_redacts_obvious_secrets(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            ToolResultBlock(
                content=(
                    "OPENAI_API_KEY=sk-testsecret1234567890\n"
                    "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz123456\n"
                ),
                is_error=False,
            ),
        ]))
        f.close()

        content = path.read_text()
        assert "sk-testsecret1234567890" not in content
        assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in content
        assert "[REDACTED:OPENAI_API_KEY]" in content

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

        lines = [_strip_ts(line) for line in path.read_text().splitlines()]
        # Each thinking paragraph rendered with the ⋯ glyph.
        assert any(line.startswith("\u22ef first line") for line in lines)
        assert any(line.startswith("\u22ef second line") for line in lines)

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
        """Git-commit Bash output lines are demoted below final success."""
        path = tmp_path / "narrative.log"
        repo = tmp_path / "repo"
        repo.mkdir()
        import subprocess as _sp
        _sp.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
        _sp.run(["git", "checkout", "-b", "worktree-i2p"], cwd=repo, check=True, capture_output=True)
        f = NarrativeFormatter(path, project_dir=repo)
        commit_output = (
            "[main abc1234] Add kanban board\n"
            " 3 files changed, 42 insertions(+)\n"
        )
        f.write_message(AssistantMessage(content=[
            ToolResultBlock(content=commit_output, is_error=False),
        ]))
        f.close()

        line = path.read_text()
        assert "\u2022 committed to worktree-i2p:" in line
        assert "abc1234" in line
        assert "commit abc1234" not in line
        assert "Add kanban board" in line

    def test_story_results_stream_to_terminal_callback(self, tmp_path):
        path = tmp_path / "narrative.log"
        seen: list[str] = []
        f = NarrativeFormatter(path, stdout_callback=seen.append)
        f.start()
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(
                name="Agent",
                input={"prompt": "Please certify.\n## Verdict Format\nVERDICT: PASS|FAIL"},
                id="cert-1",
            ),
            ToolResultBlock(
                tool_use_id="cert-1",
                content=(
                    "STORY_RESULT: cols | PASS | Board renders 3 columns\n"
                    "STORY_RESULT: persist | FAIL | Cards persist to localStorage after being added\n"
                    "DIAGNOSIS: localStorage.setItem not called in addCard handler\n"
                    "VERDICT: FAIL\n"
                ),
            ),
        ]))
        f.close()

        assert any("✓ Board renders 3 columns" in line for line in seen)
        assert any(
            "✗ Cards persist to localStorage after being added — localStorage.setItem not called" in line
            for line in seen
        )

    def test_heartbeat_uses_latest_activity_label(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(name="Write", input={"file_path": "src/App.jsx"}, id="w1"),
        ]))
        f.write_heartbeat("1m 20s")
        f.close()

        assert "⋯ building… (1m 20s) · writing src/App.jsx" in path.read_text()

    def test_heartbeat_uses_verifying_label_inside_certify_round(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f.start()
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(
                name="Agent",
                input={"prompt": "Please certify.\n## Verdict Format\nVERDICT: PASS|FAIL"},
                id="cert-1",
            ),
        ]))
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(name="Bash", input={"command": "node test.js 2"}, id="bash-1"),
        ]))
        f.write_heartbeat("25s")
        f.close()

        assert "⋯ verifying… (25s) · running node test.js 2" in path.read_text()

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
        assert "RUN SUMMARY: build=1:00, certify=0:40 (2 rounds), total=$1.23 1:40" in lines[0]
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
            "RUN SUMMARY: build=$0.49 2:40, certify=$0.41 1:51 (2 rounds), "
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
        assert "RUN SUMMARY: build=2:40, certify=1:51 (2 rounds), total=$0.98 4:31" in summary
        assert "build=$" not in summary
        assert "certify=$" not in summary

    def test_finalize_with_estimated_phase_costs_emits_tilde_prefixed_summary(self, tmp_path):
        path = tmp_path / "narrative.log"
        f = NarrativeFormatter(path)
        f._start = time.monotonic() - 271.0
        f.write_message(ResultMessage(
            subtype="success", is_error=False, session_id="x",
            total_cost_usd=0.98,
        ))
        f.finalize({
            "build": {"duration_s": 138.0, "cost_usd": 0.49, "estimated": True},
            "certify": {
                "duration_s": 111.0,
                "cost_usd": 0.41,
                "estimated": True,
                "rounds": 2,
            },
        })
        f.close()

        summary = _strip_ts(path.read_text().splitlines()[0])
        assert (
            "RUN SUMMARY: build=~$0.49 2:40, certify=~$0.41 1:51 (2 rounds), "
            "total=$0.98 4:31"
        ) in summary

    def test_finalize_reassigns_non_certify_time_from_synthetic_transcript(
        self,
        tmp_path,
        monkeypatch,
    ):
        path = tmp_path / "narrative.log"
        now = [1000.0]

        monkeypatch.setattr(time, "monotonic", lambda: now[0])

        f = NarrativeFormatter(path)
        f.start()

        now[0] = 1005.0
        f.write_message(AssistantMessage(content=[TextBlock(text="initial build work")]))

        now[0] = 1010.0
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(
                name="Agent",
                input={"prompt": "Please certify.\n## Verdict Format\nVERDICT: PASS|FAIL"},
                id="cert-1",
            ),
        ]))

        now[0] = 1030.0
        f.write_message(AssistantMessage(content=[
            ToolResultBlock(
                tool_use_id="cert-1",
                content="STORIES_TESTED: 1\nSTORIES_PASSED: 0\nVERDICT: FAIL\n",
            ),
        ]))

        now[0] = 1045.0
        f.write_message(AssistantMessage(content=[TextBlock(text="fix/build work between rounds")]))

        now[0] = 1050.0
        f.write_message(AssistantMessage(content=[
            ToolUseBlock(
                name="Agent",
                input={"prompt": "Please certify.\n## Verdict Format\nVERDICT: PASS|FAIL"},
                id="cert-2",
            ),
        ]))

        now[0] = 1070.0
        f.write_message(AssistantMessage(content=[
            ToolResultBlock(
                tool_use_id="cert-2",
                content="STORIES_TESTED: 1\nSTORIES_PASSED: 1\nVERDICT: PASS\n",
            ),
        ]))

        now[0] = 1084.0
        f.write_message(ResultMessage(
            subtype="success",
            is_error=False,
            session_id="x",
            total_cost_usd=1.00,
        ))
        f.close()

        lines = [_strip_ts(line) for line in path.read_text().splitlines()]
        assert "RUN SUMMARY: build=0:44, certify=0:40 (2 rounds), total=$1.00 1:24" in lines[-2]

    def test_normalize_phase_breakdown_sums_to_total(self):
        normalized = normalize_phase_breakdown(
            1084.127,
            {
                "build": {"duration_s": 155.255},
                "certify": {"duration_s": 769.748, "rounds": 2},
            },
            primary_phase="build",
        )

        assert normalized is not None
        total = sum(float(entry["duration_s"]) for entry in normalized.values())
        assert abs(total - 1084.127) < 0.01
        assert abs(float(normalized["build"]["duration_s"]) - 314.379) < 0.01

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
        assert lines[0] == "\u2014 CERTIFY complete \u2014"
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
        assert lines[0] == "\u2014 SPEC starting \u2014"
        assert lines[1] == "\u2014 SPEC complete \u2014"
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

        records = [
            json.loads(line)
            for line in (tmp_path / "messages.jsonl").read_text().splitlines()
            if line.strip()
        ]
        assert records[0]["type"] == "phase_start"
        assert records[1]["type"] == "unknown"
        assert records[-1]["type"] == "phase_end"

    def test_emits_phase_boundary_events(self, tmp_path):
        cbs = make_session_logger(tmp_path)
        try:
            cbs["on_message"](AssistantMessage(content=[TextBlock(text="hello")], usage={"output_tokens": 10}))
            cbs["on_message"](AssistantMessage(content=[
                ToolUseBlock(
                    name="Agent",
                    id="cert-1",
                    input={"prompt": "Please certify.\n## Verdict Format\nVERDICT: PASS|FAIL"},
                )
            ], usage={"output_tokens": 5}))
            cbs["on_message"](UserMessage(content=[
                ToolResultBlock(tool_use_id="cert-1", content="done", is_error=False)
            ], usage={"output_tokens": 7}))
        finally:
            cbs["_close"]()

        records = [
            json.loads(line)
            for line in (tmp_path / "messages.jsonl").read_text().splitlines()
            if line.strip()
        ]
        phase_events = [rec for rec in records if rec.get("type") in {"phase_start", "phase_end"}]
        assert [event["type"] for event in phase_events] == [
            "phase_start",
            "phase_end",
            "phase_start",
            "phase_end",
            "phase_start",
            "phase_end",
        ]
        assert [event["phase"] for event in phase_events] == [
            "build",
            "build",
            "certify",
            "certify",
            "build",
            "build",
        ]

    def test_phase_usage_tracks_cached_input_tokens(self, tmp_path):
        cbs = make_session_logger(tmp_path)
        try:
            cbs["on_message"](
                AssistantMessage(
                    content=[TextBlock(text="hello")],
                    usage={
                        "input_tokens": 100,
                        "cached_input_tokens": 80,
                        "output_tokens": 10,
                    },
                )
            )
        finally:
            cbs["_close"]()

        records = [
            json.loads(line)
            for line in (tmp_path / "messages.jsonl").read_text().splitlines()
            if line.strip()
        ]
        phase_end = [rec for rec in records if rec.get("type") == "phase_end"][-1]
        assert phase_end["usage"]["input_tokens"] == 100
        assert phase_end["usage"]["cached_input_tokens"] == 80
        assert phase_end["usage"]["output_tokens"] == 10

    def test_phase_usage_preserves_total_only_token_usage(self, tmp_path):
        cbs = make_session_logger(tmp_path)
        try:
            cbs["on_message"](
                AssistantMessage(
                    content=[TextBlock(text="hello")],
                    usage={"total_tokens": 12345},
                )
            )
        finally:
            cbs["_close"]()

        records = [
            json.loads(line)
            for line in (tmp_path / "messages.jsonl").read_text().splitlines()
            if line.strip()
        ]
        phase_end = [rec for rec in records if rec.get("type") == "phase_end"][-1]
        assert phase_end["usage"]["total_tokens"] == 12345

    def test_phase_usage_cumulative_tokens_ignore_cost_reset(self, tmp_path):
        cbs = make_session_logger(tmp_path)
        try:
            cbs["on_message"](
                AssistantMessage(
                    content=[TextBlock(text="first")],
                    usage={"input_tokens": 100, "output_tokens": 10, "total_cost_usd": 0.20},
                )
            )
            cbs["on_message"](
                AssistantMessage(
                    content=[TextBlock(text="second")],
                    usage={"input_tokens": 150, "output_tokens": 15},
                )
            )
        finally:
            cbs["_close"]()

        records = [
            json.loads(line)
            for line in (tmp_path / "messages.jsonl").read_text().splitlines()
            if line.strip()
        ]
        phase_end = [rec for rec in records if rec.get("type") == "phase_end"][-1]
        assert phase_end["usage"]["input_tokens"] == 150
        assert phase_end["usage"]["output_tokens"] == 15
        assert phase_end["usage"]["cost_usd"] == 0.2

    def test_phase_usage_does_not_count_duplicate_snapshots_twice(self, tmp_path):
        cbs = make_session_logger(tmp_path)
        try:
            usage = {"input_tokens": 100, "output_tokens": 10}
            cbs["on_message"](AssistantMessage(content=[TextBlock(text="first")], usage=usage))
            cbs["on_message"](AssistantMessage(content=[TextBlock(text="same")], usage=usage))
        finally:
            cbs["_close"]()

        records = [
            json.loads(line)
            for line in (tmp_path / "messages.jsonl").read_text().splitlines()
            if line.strip()
        ]
        phase_end = [rec for rec in records if rec.get("type") == "phase_end"][-1]
        assert phase_end["usage"]["input_tokens"] == 100
        assert phase_end["usage"]["output_tokens"] == 10

    def test_single_phase_result_usage_overrides_streamed_snapshots(self, tmp_path):
        cbs = make_session_logger(tmp_path)
        try:
            cbs["on_message"](
                AssistantMessage(
                    content=[TextBlock(text="streamed")],
                    usage={"input_tokens": 100, "cache_read_input_tokens": 500, "output_tokens": 10},
                )
            )
            cbs["on_message"](
                ResultMessage(
                    subtype="success",
                    is_error=False,
                    total_cost_usd=0.42,
                    usage={"input_tokens": 20, "cache_read_input_tokens": 200, "output_tokens": 5},
                )
            )
        finally:
            cbs["_close"]()

        records = [
            json.loads(line)
            for line in (tmp_path / "messages.jsonl").read_text().splitlines()
            if line.strip()
        ]
        phase_end = [rec for rec in records if rec.get("type") == "phase_end"][-1]
        assert phase_end["usage"]["input_tokens"] == 20
        assert phase_end["usage"]["cache_read_input_tokens"] == 200
        assert phase_end["usage"]["output_tokens"] == 5
        assert phase_end["usage"]["cost_usd"] == 0.42

    def test_subagent_error_event_is_written(self, tmp_path):
        cbs = make_session_logger(tmp_path)
        try:
            cbs["on_message"](AssistantMessage(content=[
                ToolUseBlock(
                    name="Agent",
                    id="agent-1",
                    input={"prompt": "**Story: auth**\n## Verdict Format\nVERDICT: PASS|FAIL"},
                )
            ]))
            cbs["on_message"](UserMessage(content=[
                ToolResultBlock(tool_use_id="agent-1", content="Timed out after 30s", is_error=True)
            ]))
        finally:
            cbs["_close"]()

        records = [
            json.loads(line)
            for line in (tmp_path / "messages.jsonl").read_text().splitlines()
            if line.strip()
        ]
        subagent_errors = [rec for rec in records if rec.get("type") == "subagent_error"]
        assert len(subagent_errors) == 1
        assert subagent_errors[0]["story_id"] == "auth"
        assert subagent_errors[0]["reason"] == "timeout"
        assert subagent_errors[0]["final_effect_on_verdict"] == "FAIL"


class TestEstimatePhaseCosts:
    def test_attributes_cost_by_output_token_share(self, tmp_path):
        path = tmp_path / "messages.jsonl"
        records = [
            {
                "type": "assistant",
                "blocks": [{"type": "text", "text": "build"}],
                "usage": {"output_tokens": 40},
            },
            {
                "type": "assistant",
                "blocks": [{
                    "type": "tool_use",
                    "id": "cert-1",
                    "name": "Agent",
                    "input": {"prompt": "hello\n## Verdict Format\nbye"},
                }],
                "usage": {"output_tokens": 10},
            },
            {
                "type": "assistant",
                "blocks": [{"type": "text", "text": "inside certify"}],
                "usage": {"output_tokens": 20},
            },
            {
                "type": "assistant",
                "blocks": [{
                    "type": "tool_result",
                    "tool_use_id": "cert-1",
                    "content": "done",
                    "is_error": False,
                }],
                "usage": {"output_tokens": 30},
            },
            {
                "type": "assistant",
                "blocks": [{"type": "text", "text": "wrap up"}],
                "usage": {"output_tokens": 50},
            },
        ]
        path.write_text("\n".join(json.dumps(rec) for rec in records) + "\n")

        estimated = estimate_phase_costs(path, 1.5)

        assert estimated == {
            "build": {"cost_usd": 0.9, "estimated": True},
            "certify": {"cost_usd": 0.6, "estimated": True},
        }

    def test_no_agent_dispatches_attribute_all_cost_to_build(self, tmp_path):
        path = tmp_path / "messages.jsonl"
        records = [
            {
                "type": "assistant",
                "blocks": [{"type": "text", "text": "build"}],
                "usage": {"output_tokens": 30},
            },
            {
                "type": "assistant",
                "blocks": [{"type": "text", "text": "more build"}],
                "usage": {"output_tokens": 70},
            },
        ]
        path.write_text("\n".join(json.dumps(rec) for rec in records) + "\n")

        estimated = estimate_phase_costs(path, 2.0)

        assert estimated == {
            "build": {"cost_usd": 2.0, "estimated": True},
        }

    def test_malformed_jsonl_returns_none(self, tmp_path):
        path = tmp_path / "messages.jsonl"
        path.write_text("{not json}\n")

        assert estimate_phase_costs(path, 1.0) is None

    def test_counts_user_tool_result_usage_inside_certify_round(self, tmp_path):
        path = tmp_path / "messages.jsonl"
        records = [
            {
                "type": "assistant",
                "blocks": [{
                    "type": "tool_use",
                    "id": "cert-1",
                    "name": "Agent",
                    "input": {"prompt": "hello\n## Verdict Format\nbye"},
                }],
                "usage": {"output_tokens": 10},
            },
            {
                "type": "user",
                "blocks": [{
                    "type": "tool_result",
                    "tool_use_id": "cert-1",
                    "content": "certifier output",
                    "is_error": False,
                }],
                "usage": {"output_tokens": 90},
            },
        ]
        path.write_text("\n".join(json.dumps(rec) for rec in records) + "\n")

        estimated = estimate_phase_costs(path, 2.0)

        assert estimated == {
            "certify": {"cost_usd": 2.0, "estimated": True},
        }


class TestBrowserEfficiency:
    def test_counts_browser_calls_sessions_and_verbs(self, tmp_path):
        path = tmp_path / "messages.jsonl"
        records = [
            {
                "type": "assistant",
                "blocks": [{
                    "type": "tool_use",
                    "id": "agent-1",
                    "name": "Agent",
                    "input": {"prompt": "Verify story_id=first-experience and capture STORY_RESULT lines."},
                }],
            },
            {
                "type": "assistant",
                "blocks": [
                    {
                        "type": "tool_use",
                        "id": "bash-1",
                        "name": "Bash",
                        "input": {"command": "agent-browser --session main snapshot -i"},
                    },
                    {
                        "type": "tool_use",
                        "id": "bash-2",
                        "name": "Bash",
                        "input": {"command": "agent-browser --session main eval \"return !!document.querySelector('#app')\""},
                    },
                    {
                        "type": "tool_use",
                        "id": "bash-3",
                        "name": "Bash",
                        "input": {"command": "echo no-browser-here"},
                    },
                    {
                        "type": "tool_use",
                        "id": "agent-generic",
                        "name": "Agent",
                        "input": {"prompt": "Summarize the browser evidence and collect screenshots."},
                    },
                    {
                        "type": "tool_use",
                        "id": "bash-4",
                        "name": "Bash",
                        "input": {"command": "agent-browser click \"Add card\""},
                    },
                ],
            },
        ]
        path.write_text("\n".join(json.dumps(rec) for rec in records) + "\n")

        efficiency = summarize_browser_efficiency(
            path,
            certifier_mode="standard",
            story_ids=["first-experience", "crud-lifecycle"],
        )

        assert efficiency["total_browser_calls"] == 3
        assert efficiency["distinct_sessions"] == 2
        assert efficiency["verb_counts"] == {"click": 1, "eval": 1, "snapshot": 1}
        assert efficiency["calls_per_story"] == {
            "first-experience": 2,
            "crud-lifecycle": 0,
            "shared": 1,
        }

    def test_calls_per_story_uses_direct_attribution_and_shared_bucket(self, tmp_path):
        path = tmp_path / "messages.jsonl"
        records = [
            {
                "type": "assistant",
                "blocks": [{
                    "type": "tool_use",
                    "id": "agent-story-1",
                    "name": "Agent",
                    "input": {"description": "Run crud-lifecycle certifier story in the browser."},
                }],
            },
            {
                "type": "assistant",
                "blocks": [{
                    "type": "tool_use",
                    "id": "bash-1",
                    "name": "Bash",
                    "input": {"command": "agent-browser --session crud-user snapshot -i"},
                }],
            },
            {
                "type": "assistant",
                "blocks": [{
                    "type": "tool_use",
                    "id": "bash-1b",
                    "name": "Bash",
                    "input": {"command": "agent-browser --session crud-user eval \"return window.localStorage.length\""},
                }],
            },
            {
                "type": "assistant",
                "blocks": [{
                    "type": "tool_use",
                    "id": "agent-story-2",
                    "name": "Agent",
                    "input": {"prompt": "Now verify drag-drop behavior end to end."},
                }],
            },
            {
                "type": "assistant",
                "blocks": [{
                    "type": "tool_use",
                    "id": "bash-2",
                    "name": "Bash",
                    "input": {"command": "agent-browser --session dnd drag \"Card A\" \"Done\""},
                }],
            },
            {
                "type": "assistant",
                "blocks": [{
                    "type": "tool_use",
                    "id": "bash-3",
                    "name": "Bash",
                    "input": {"command": "agent-browser screenshot evidence/final.png"},
                }],
            },
        ]
        path.write_text("\n".join(json.dumps(rec) for rec in records) + "\n")

        efficiency = summarize_browser_efficiency(
            path,
            certifier_mode="standard",
            story_ids=["crud-lifecycle", "drag-drop"],
        )

        assert efficiency["calls_per_story"] == {
            "crud-lifecycle": 2,
            "drag-drop": 1,
            "shared": 1,
        }

    def test_main_agent_browser_work_stays_in_shared_bucket(self, tmp_path):
        path = tmp_path / "messages.jsonl"
        records = [
            {
                "type": "assistant",
                "blocks": [{
                    "type": "tool_use",
                    "id": "bash-1",
                    "name": "Bash",
                    "input": {"command": "agent-browser snapshot -i"},
                }],
            },
            {
                "type": "assistant",
                "blocks": [{
                    "type": "tool_use",
                    "id": "bash-2",
                    "name": "Bash",
                    "input": {"command": "agent-browser click \"Continue\""},
                }],
            },
            {
                "type": "assistant",
                "blocks": [{
                    "type": "tool_use",
                    "id": "bash-3",
                    "name": "Bash",
                    "input": {"command": "agent-browser eval \"return window.location.pathname\""},
                }],
            },
        ]
        path.write_text("\n".join(json.dumps(rec) for rec in records) + "\n")

        efficiency = summarize_browser_efficiency(
            path,
            certifier_mode="standard",
            story_ids=["first-experience", "crud-lifecycle"],
        )

        assert efficiency["calls_per_story"] == {
            "first-experience": 0,
            "crud-lifecycle": 0,
            "shared": 3,
        }

    def test_outlier_heuristic_thresholds_by_mode(self):
        assert browser_efficiency_outlier(
            certifier_mode="fast",
            total_browser_calls=21,
            distinct_sessions=1,
            story_count=1,
        ) == (
            True,
            "fast-mode outlier: calls per story 21.0 > 20.0.",
        )
        assert browser_efficiency_outlier(
            certifier_mode="standard",
            total_browser_calls=91,
            distinct_sessions=1,
            story_count=2,
        ) == (
            True,
            "standard-mode outlier: total browser calls 91 > 90; calls per story 45.5 > 40.0.",
        )
        assert browser_efficiency_outlier(
            certifier_mode="standard",
            total_browser_calls=10,
            distinct_sessions=3,
            story_count=2,
            isolated_story_count=1,
        ) == (False, "")
        assert browser_efficiency_outlier(
            certifier_mode="standard",
            total_browser_calls=10,
            distinct_sessions=4,
            story_count=2,
            isolated_story_count=1,
        ) == (
            True,
            "standard-mode outlier: distinct sessions 4 > 3.",
        )
        assert browser_efficiency_outlier(
            certifier_mode="thorough",
            total_browser_calls=170,
            distinct_sessions=4,
            story_count=2,
        ) == (
            True,
            "thorough-mode outlier: total browser calls 170 > 160; calls per story 85.0 > 80.0.",
        )
        assert browser_efficiency_outlier(
            certifier_mode="standard",
            total_browser_calls=167,
            distinct_sessions=5,
            story_count=6,
        ) == (False, "")
        assert browser_efficiency_outlier(
            certifier_mode="thorough",
            total_browser_calls=60,
            distinct_sessions=4,
            story_count=2,
        ) == (False, "")
        assert browser_efficiency_outlier(
            certifier_mode="thorough",
            total_browser_calls=60,
            distinct_sessions=5,
            story_count=2,
        ) == (
            True,
            "thorough-mode outlier: distinct sessions 5 > 4.",
        )
        assert browser_efficiency_outlier(
            certifier_mode="standard",
            total_browser_calls=60,
            distinct_sessions=3,
            story_count=2,
            isolated_story_count=1,
        ) == (False, "")
