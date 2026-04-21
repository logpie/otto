"""Session log writers: messages.jsonl + narrative.log.

Replaces the old live.log / agent.log / agent-raw.log trio. Both files
stream as messages arrive so users can `tail -f` during a run.

  messages.jsonl  — one JSON line per normalized SDK message. Lossless
                    event stream for machine consumers (jq, future
                    `otto replay`). Never truncated or filtered.
  narrative.log   — human-readable event stream. Per-event formatters
                    for tool use, tool result, bash output, subagent
                    result, thinking, certifier markers, git commits.

A `live.log` symlink pointing at narrative.log is also created for one
release so existing docs and muscle memory keep working.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from otto.agent import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    tool_use_summary,
)

# Certifier-marker lines we elevate in the narrative so humans can scan
# for them. Order matters — checked as startswith.
_CERTIFY_MARKERS = (
    "CERTIFY_ROUND:",
    "VERDICT:",
    "DIAGNOSIS:",
    "STORIES_TESTED:",
    "STORIES_PASSED:",
    "STORY_RESULT:",
    "METRIC_MET:",
    "METRIC_VALUE:",
)

_MAX_NARRATIVE_LINE = 280


def _iso_ts() -> str:
    t = time.time()
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))
    millis = int((t - int(t)) * 1000)
    return f"{base}.{millis:03d}Z"


def _truncate_at_word(text: str, limit: int = _MAX_NARRATIVE_LINE) -> str:
    """Truncate at word boundary, never mid-word."""
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    if cut <= 0:
        cut = limit
    return text[:cut] + "..."


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Serialize a normalized SDK block to a JSON-safe dict."""
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ThinkingBlock):
        return {"type": "thinking", "thinking": block.thinking}
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    return {"type": "unknown", "repr": str(block)[:500]}


class JsonlMessageWriter:
    """Append one JSON object per SDK message event.

    Opens in append mode and flushes after every write so crashes don't
    lose events and `tail -f | jq` works during a run.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._fh = open(path, "a", encoding="utf-8")
        self._start = time.monotonic()

    def write(self, message: Any) -> None:
        record: dict[str, Any] = {
            "ts": _iso_ts(),
            "elapsed_s": round(time.monotonic() - self._start, 3),
            "session_id": getattr(message, "session_id", "") or "",
        }
        if isinstance(message, ResultMessage):
            record["type"] = "result"
            record["subtype"] = message.subtype
            record["is_error"] = message.is_error
            if message.result:
                record["result"] = message.result
            if message.structured_output is not None:
                record["structured_output"] = message.structured_output
            if message.total_cost_usd is not None:
                record["cost_usd"] = message.total_cost_usd
            if message.usage is not None:
                record["usage"] = message.usage
        elif isinstance(message, AssistantMessage):
            record["type"] = "assistant"
            record["blocks"] = [_block_to_dict(b) for b in message.content]
        else:
            record["type"] = "unknown"
            record["repr"] = str(message)[:500]
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str))
        self._fh.write("\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


class NarrativeFormatter:
    """Human-readable streaming event log.

    One or more lines per event, prefixed with `[+M:SS]` elapsed clock.
    Tool calls, tool results, thinking, text, and certifier markers each
    render with a distinct leading glyph so the log scans quickly.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._fh = open(path, "a", encoding="utf-8")
        self._start = time.monotonic()

    def _stamp(self) -> str:
        secs = int(time.monotonic() - self._start)
        m, s = divmod(secs, 60)
        return f"[+{m}:{s:02d}]"

    def _write(self, line: str) -> None:
        if not line.endswith("\n"):
            line = line + "\n"
        self._fh.write(line)
        self._fh.flush()

    def write_message(self, message: Any) -> None:
        if isinstance(message, ResultMessage):
            self._write_result(message)
            return
        if not isinstance(message, AssistantMessage):
            return
        for block in message.content:
            self._write_block(block)

    def _write_block(self, block: Any) -> None:
        ts = self._stamp()
        if isinstance(block, ToolUseBlock):
            summary = tool_use_summary(block) or ""
            line = f"{ts} \u25cf {block.name} {summary}".rstrip()
            self._write(_truncate_at_word(line))
            return
        if isinstance(block, ToolResultBlock):
            self._write_tool_result(ts, block)
            return
        if isinstance(block, ThinkingBlock):
            text = (block.thinking or "").strip()
            if not text:
                return
            for para in text.split("\n"):
                para = para.strip()
                if para:
                    self._write(f"{ts} \u22ef {_truncate_at_word(para)}")
            return
        if isinstance(block, TextBlock):
            text = (block.text or "").strip()
            if not text:
                return
            for line in text.split("\n"):
                s = line.strip()
                if not s:
                    continue
                if _is_marker(s):
                    self._write(f"{ts}   {s}")
                elif s.startswith("[thinking]"):
                    self._write(f"{ts} \u22ef {_truncate_at_word(s[10:].strip())}")
                else:
                    self._write(f"{ts} \u25b8 {_truncate_at_word(s)}")

    def _write_tool_result(self, ts: str, block: ToolResultBlock) -> None:
        content = str(block.content or "")
        if block.is_error:
            first = (content.split("\n", 1)[0][:200]) if content else "(empty)"
            self._write(f"{ts} \u2717 error: {first}")
            return
        if not content:
            self._write(f"{ts} \u2190 (empty)")
            return
        # Elevate certifier markers embedded in tool/subagent output so a
        # scanner can pick PASS/FAIL/rounds at a glance.
        marker_lines = [line.strip() for line in content.split("\n")
                        if _is_marker(line.strip())]
        if marker_lines:
            for s in marker_lines:
                self._write(f"{ts}   {s}")
            return
        # Detect git-commit in Bash output, elevate to a distinct glyph.
        commit_line = _extract_commit_line(content)
        if commit_line:
            self._write(f"{ts} \u2713 {commit_line}")
            return
        first = content.split("\n", 1)[0][:220]
        line_count = content.count("\n") + 1
        suffix = f" ({line_count} lines)" if line_count > 2 else ""
        self._write(f"{ts} \u2190 {_truncate_at_word(first)}{suffix}")

    def _write_result(self, message: ResultMessage) -> None:
        ts = self._stamp()
        status = "ERROR" if message.is_error else message.subtype.upper()
        cost = f" ${message.total_cost_usd:.2f}" if message.total_cost_usd else ""
        self._write(f"{ts} \u2501\u2501\u2501 {status}{cost}")

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


def _is_marker(line: str) -> bool:
    return any(line.startswith(m) for m in _CERTIFY_MARKERS)


def _extract_commit_line(content: str) -> str | None:
    """If a Bash output contains a git commit confirmation, pull the summary."""
    for line in content.split("\n"):
        s = line.strip()
        # Typical: "[branch abcd123] Add kanban board"
        if s.startswith("[") and "]" in s:
            rbr = s.index("]")
            head = s[1:rbr]
            parts = head.split()
            if len(parts) >= 2 and all(c in "0123456789abcdef" for c in parts[-1]) and len(parts[-1]) >= 7:
                title = s[rbr + 1:].strip()
                if title:
                    return f"commit {parts[-1]} \"{title[:120]}\""
    return None


def make_session_logger(log_dir: Path) -> dict[str, Callable]:
    """Open messages.jsonl + narrative.log in ``log_dir`` and return the
    callback dict for run_agent_with_timeout / run_agent_query.

    Also maintains a ``live.log`` symlink -> ``narrative.log`` for one
    release so existing `tail -f …/live.log` habits keep working. If
    symlinks are unsupported, live.log is just absent (narrative.log is
    the canonical file).

    Returned callbacks:
      on_message  — receives every normalized SDK message (assistant/result).
      _close      — closes both writers; callers must invoke in `finally`.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    jsonl = JsonlMessageWriter(log_dir / "messages.jsonl")
    narr = NarrativeFormatter(log_dir / "narrative.log")

    live = log_dir / "live.log"
    try:
        if live.is_symlink() or live.exists():
            live.unlink()
        os.symlink("narrative.log", live)
    except OSError:
        pass

    def _on_message(message: Any) -> None:
        jsonl.write(message)
        narr.write_message(message)

    def _close() -> None:
        jsonl.close()
        narr.close()

    return {"on_message": _on_message, "_close": _close}
