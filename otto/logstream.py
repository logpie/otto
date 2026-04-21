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
import re
import time
from pathlib import Path
from typing import Any

from otto.agent import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
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

# Heading markers we use to detect subagent prompts flooding into a
# TextBlock (the SDK sometimes echoes full prompt bodies into assistant
# text). Collapsed to a single summary line.
_PROMPT_FLOOD_HEADING_RE = re.compile(r"^##\s+\S", re.MULTILINE)
_READ_OUTPUT_PREFIX_RE = re.compile(r"^\d+\t")
_PLACEHOLDER_RE = re.compile(r"<[a-zA-Z_]")
_WRITE_EDIT_BOILERPLATE_RE = re.compile(
    r"\s*\(file state is current in your context[^)]*\)\s*$"
)


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


def _coerce_usage(usage: Any) -> Any:
    """Best-effort coerce ``usage`` to a JSON-safe dict.

    Returns a dict if coercion succeeds, otherwise the raw value (which
    json.dumps(default=str) will fall back to stringifying). This keeps
    the writer resilient to SDK versions that ship a typed object
    instead of a plain mapping.
    """
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage
    try:
        return dict(usage)
    except (TypeError, ValueError):
        return usage


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
                record["usage"] = _coerce_usage(message.usage)
        elif isinstance(message, UserMessage):
            record["type"] = "user"
            record["blocks"] = [_block_to_dict(b) for b in message.content]
            if message.usage is not None:
                record["usage"] = _coerce_usage(message.usage)
        elif isinstance(message, AssistantMessage):
            record["type"] = "assistant"
            record["blocks"] = [_block_to_dict(b) for b in message.content]
            if message.usage is not None:
                record["usage"] = _coerce_usage(message.usage)
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

    One or more lines per event, prefixed with `[+M:SS]` elapsed clock
    (or `[+H:MM:SS]` past an hour). Tool calls, tool results, thinking,
    text, and certifier markers each render with a distinct leading
    glyph so the log scans quickly.
    """

    # Glyphs — referenced from multiple methods.
    _GLYPH_MARKER = "\u2726"     # ✦ elevated certifier marker
    _GLYPH_SUBAGENT = "\u21d0"   # ⇐ subagent tool result
    _GLYPH_SUMMARY = "\u220e"    # ∎ closing summary text
    _GLYPH_PHASE = "\u2501" * 3  # ━━━ phase begin/end banner

    def __init__(self, path: Path, *, phase_name: str = "BUILD") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._fh = open(path, "a", encoding="utf-8")
        self._start = time.monotonic()
        self._phase_name = (phase_name or "BUILD").upper()
        # tool_use_id -> tool name, so ToolResultBlock renderers can
        # tailor output (Glob=>"(N files)", Read=>"(N lines)", Agent=>
        # subagent glyph).
        self._tool_by_id: dict[str, str] = {}
        # Counter for subagent dispatches, used to label certify rounds
        # (each Agent-tool dispatch that looks like a certifier prompt
        # begins a new "round"). Tracked separately from total Agent
        # dispatches in case other subagents are dispatched.
        self._agent_dispatch_count: int = 0
        self._agent_round_by_id: dict[str, int] = {}
        self._round_start_elapsed_by_id: dict[str, float] = {}
        self._round_timings: list[tuple[float, float]] = []
        self._build_duration: float | None = None
        self._phase_started = False
        self._phase_complete_written = False
        self._summary_written = False
        self._pending_result: ResultMessage | None = None

    def start(self) -> None:
        if self._phase_started:
            return
        self._phase_started = True
        self._write(
            f"{self._stamp()} {self._GLYPH_PHASE} "
            f"{self._phase_name} starting {self._GLYPH_PHASE}"
        )

    def _elapsed_seconds(self) -> float:
        return time.monotonic() - self._start

    def _elapsed_fmt(self) -> str:
        secs = int(self._elapsed_seconds())
        return _format_elapsed_seconds(secs)

    def _phase_complete_line(self) -> str:
        if self._phase_name == "BUILD":
            return (
                f"{self._stamp()} {self._GLYPH_PHASE} BUILD complete — "
                f"handing off to certifier {self._GLYPH_PHASE}"
            )
        return (
            f"{self._stamp()} {self._GLYPH_PHASE} {self._phase_name} complete "
            f"{self._GLYPH_PHASE}"
        )

    def _write_phase_complete(self) -> None:
        if self._phase_complete_written:
            return
        self._phase_complete_written = True
        self._write(self._phase_complete_line())

    def _summary_label(self) -> str:
        return self._phase_name.lower()

    def round_timings(self) -> list[tuple[float, float]]:
        return list(self._round_timings)

    def build_duration_or_none(self) -> float | None:
        return self._build_duration

    def elapsed_seconds(self) -> float:
        return self._elapsed_seconds()

    def _fallback_breakdown(self, total_elapsed: float) -> dict[str, dict[str, float | int]]:
        primary_label = self._summary_label()
        build_duration = self._build_duration
        if build_duration is None:
            build_duration = total_elapsed
        breakdown: dict[str, dict[str, float | int]] = {
            primary_label: {"duration_s": build_duration},
        }
        if self._round_timings and self._phase_name == "BUILD":
            certify_duration = sum(end - start for start, end in self._round_timings)
            breakdown["certify"] = {
                "duration_s": certify_duration,
                "rounds": len(self._round_timings),
            }
        return breakdown

    def _summary_line(
        self,
        total_elapsed: float,
        breakdown: dict[str, dict[str, Any]] | None,
        total_cost_usd: float | None,
    ) -> str:
        if breakdown is None:
            breakdown = self._fallback_breakdown(total_elapsed)

        parts: list[str] = []
        for phase in ("spec", "build", "certify"):
            phase_data = breakdown.get(phase)
            if not phase_data:
                continue
            segment = f"{phase}="
            cost_usd = phase_data.get("cost_usd")
            if isinstance(cost_usd, int | float):
                if phase_data.get("estimated") is True:
                    segment += "~"
                segment += f"${float(cost_usd):.2f} "
            segment += _format_elapsed_seconds(float(phase_data.get("duration_s", 0.0)))
            rounds = phase_data.get("rounds")
            if phase == "certify" and isinstance(rounds, int):
                segment += f" ({rounds} round{'s' if rounds != 1 else ''})"
            parts.append(segment)

        total_segment = "total="
        if isinstance(total_cost_usd, int | float):
            total_segment += f"${float(total_cost_usd):.2f} "
        total_segment += _format_elapsed_seconds(total_elapsed)
        parts.append(total_segment)
        return (
            f"{self._stamp()} {self._GLYPH_PHASE} RUN SUMMARY: "
            f"{', '.join(parts)} {self._GLYPH_PHASE}"
        )

    def _stamp(self) -> str:
        return f"[+{self._elapsed_fmt()}]"

    def _write(self, line: str) -> None:
        if not line.endswith("\n"):
            line = line + "\n"
        self._fh.write(line)
        self._fh.flush()

    def write_message(self, message: Any) -> None:
        if isinstance(message, ResultMessage):
            self._write_result(message)
            return
        if not isinstance(message, (AssistantMessage, UserMessage)):
            return
        for block in message.content:
            self._write_block(block)

    def _write_block(self, block: Any) -> None:
        ts = self._stamp()
        if isinstance(block, ToolUseBlock):
            # Remember the tool name so tool_result renderers can look
            # up the originator by tool_use_id.
            if block.id:
                self._tool_by_id[block.id] = block.name or ""
            # Certify-phase banners — Agent dispatches with a
            # certifier-shaped prompt open a new round.
            if block.name == "Agent" and _looks_like_certifier_prompt(block.input):
                round_start_elapsed = self._elapsed_seconds()
                if self._agent_dispatch_count == 0:
                    self._build_duration = round_start_elapsed
                    if self._phase_name == "BUILD":
                        self._write_phase_complete()
                self._agent_dispatch_count += 1
                round_n = self._agent_dispatch_count
                if block.id:
                    self._agent_round_by_id[block.id] = round_n
                    self._round_start_elapsed_by_id[block.id] = round_start_elapsed
                self._write(
                    f"{ts} {self._GLYPH_PHASE} CERTIFY ROUND {round_n} "
                    f"starting {self._GLYPH_PHASE}"
                )
                return
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
            # Prompt flood detector: if the text looks like a full
            # subagent system prompt, collapse to a one-liner instead of
            # streaming every line.
            flood_summary = _prompt_flood_summary(text)
            if flood_summary is not None:
                self._write(f"{ts} \u25b8 {flood_summary}")
                return
            text_glyph = (
                self._GLYPH_SUMMARY
                if self._agent_dispatch_count >= 1 and _looks_like_closing_summary(text)
                else "\u25b8"
            )
            # Suppress redundant final-summary re-emission: when the
            # agent's closing text contains CERTIFY_ROUND marker blocks
            # from already-completed rounds, we've already streamed those
            # markers live via subagent results. Render just the prose;
            # drop the re-emitted markers.
            certify_round_count = text.count("CERTIFY_ROUND:")
            if (self._agent_dispatch_count >= 1
                    and certify_round_count >= 1
                    and certify_round_count <= self._agent_dispatch_count):
                prose_lines = [
                    l.strip() for l in text.split("\n")
                    if l.strip() and not _is_marker(l.strip())
                ]
                for s in prose_lines:
                    self._write(f"{ts} {text_glyph} {_truncate_at_word(s)}")
                return
            for line in text.split("\n"):
                s = line.strip()
                if not s:
                    continue
                if _is_marker(s):
                    self._write(f"{ts} {self._GLYPH_MARKER} {s}")
                else:
                    self._write(f"{ts} {text_glyph} {_truncate_at_word(s)}")

    def _write_tool_result(self, ts: str, block: ToolResultBlock) -> None:
        raw_content = block.content
        tool_name = self._tool_by_id.get(block.tool_use_id or "", "")

        # content may be a list-of-dicts payload (subagent results are
        # returned as [{"type":"text","text":"..."}]). Extract the
        # concatenated text before any further processing.
        subagent_text = _maybe_extract_subagent_text(raw_content)

        if subagent_text is not None:
            content = subagent_text
            is_subagent = True
        else:
            content = str(raw_content or "")
            is_subagent = tool_name == "Agent"

        if block.is_error:
            first = (content.split("\n", 1)[0][:200]) if content else "(empty)"
            self._write(f"{ts} \u2717 error: {first}")
            return
        if not content:
            self._write(f"{ts} \u2190 (empty)")
            return

        # Strip Write/Edit boilerplate trailer before any downstream
        # rendering so it doesn't leak into the narrative.
        content = _WRITE_EDIT_BOILERPLATE_RE.sub("", content)
        if not content:
            self._write(f"{ts} \u2190 (empty)")
            return

        # Elevate certifier markers embedded in tool/subagent output.
        # For subagent results, also preserve the non-marker prose so the
        # human gets the summary (e.g. "All 5 tests pass") alongside the
        # parsed STORY_RESULT / VERDICT lines.
        marker_lines = [line.strip() for line in content.split("\n")
                        if _is_marker(line.strip())]
        if marker_lines:
            if is_subagent:
                prose = "\n".join(
                    l for l in content.split("\n")
                    if l.strip() and not _is_marker(l.strip())
                ).strip()
                if prose:
                    flat = " ".join(prose.split())
                    self._write(
                        f"{ts} {self._GLYPH_SUBAGENT} [subagent]: "
                        f"{_truncate_at_word(flat)}"
                    )
            for s in marker_lines:
                self._write(f"{ts} {self._GLYPH_MARKER} {s}")
            # Round-end banner when we can correlate to a round start.
            if is_subagent and block.tool_use_id:
                round_n = self._agent_round_by_id.get(block.tool_use_id)
                if round_n is not None:
                    round_start = self._round_start_elapsed_by_id.pop(block.tool_use_id, None)
                    if round_start is not None:
                        self._round_timings.append((round_start, self._elapsed_seconds()))
                    verdict, passed, tested = _summarize_round(marker_lines)
                    stats = f" ({passed}/{tested})" if tested else ""
                    self._write(
                        f"{ts} {self._GLYPH_PHASE} CERTIFY ROUND {round_n} "
                        f"→ {verdict}{stats} {self._GLYPH_PHASE}"
                    )
            return

        # Detect git-commit in Bash output, elevate to a distinct glyph.
        commit_line = _extract_commit_line(content)
        if commit_line:
            self._write(f"{ts} \u2713 {commit_line}")
            return

        # Subagent result — dedicated glyph + word-safe truncation.
        if is_subagent:
            flat = " ".join(content.split())
            self._write(f"{ts} {self._GLYPH_SUBAGENT} [subagent]: {_truncate_at_word(flat)}")
            return

        # Glob — show just "(N files)".
        if tool_name == "Glob":
            lines = [l for l in content.split("\n") if l.strip()]
            self._write(f"{ts} \u2190 ({len(lines)} files)")
            return

        # Read — the SDK returns `N\t<content>` per line. If the first
        # line is just a line-number + very short content, collapse to
        # `(N lines)` with no preview.
        if tool_name == "Read":
            total_lines = content.count("\n") + (0 if content.endswith("\n") else 1)
            first_line = content.split("\n", 1)[0]
            # Strip any `\d+\t` prefix for preview.
            stripped_first = _READ_OUTPUT_PREFIX_RE.sub("", first_line, count=1)
            # If there's nothing meaningful after the tab, just count lines.
            if not stripped_first.strip() or len(stripped_first.strip()) <= 4:
                self._write(f"{ts} \u2190 ({total_lines} lines)")
                return
            self._write(f"{ts} \u2190 {_truncate_at_word(stripped_first)} ({total_lines} lines)")
            return

        # Default rendering: scan forward for the first meaningful line
        # (skip leading blank lines, lone punctuation like `}` or `)`, and
        # one-char noise) so multi-line bash output doesn't show `} (8 lines)`.
        # Word-safe truncation; suffix line-count if multi-line.
        first = _first_meaningful_line(content)
        line_count = content.count("\n") + 1
        suffix = f" ({line_count} lines)" if line_count > 2 else ""
        if not first:
            self._write(f"{ts} \u2190 ({line_count} lines)")
            return
        self._write(f"{ts} \u2190 {_truncate_at_word(first)}{suffix}")

    def _write_result(self, message: ResultMessage) -> None:
        self._pending_result = message

    def finalize(self, breakdown: dict[str, dict[str, float | int]] | None = None) -> None:
        if self._summary_written:
            return
        self._summary_written = True

        message = self._pending_result
        if message is None:
            return

        ts = self._stamp()
        total_elapsed = self._elapsed_seconds()
        if self._phase_name != "BUILD":
            self._write_phase_complete()
        self._write(self._summary_line(total_elapsed, breakdown, message.total_cost_usd))
        status = "ERROR" if message.is_error else message.subtype.upper()
        cost = f" ${message.total_cost_usd:.2f}" if message.total_cost_usd else ""
        duration = f" in {self._elapsed_fmt()}"
        self._write(f"{ts} \u2501\u2501\u2501 {status}{cost}{duration}")

    def close(self) -> None:
        self.finalize()
        try:
            self._fh.close()
        except OSError:
            pass


def _is_marker(line: str) -> bool:
    if not any(line.startswith(m) for m in _CERTIFY_MARKERS):
        return False
    # Skip template placeholders like `STORIES_TESTED: <number>` — they
    # appear in prompt examples, not real runs.
    if _PLACEHOLDER_RE.search(line):
        return False
    return True


def _format_elapsed_seconds(elapsed_s: float) -> str:
    secs = max(0, int(elapsed_s))
    if secs >= 3600:
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}"
    m, s = divmod(secs, 60)
    return f"{m}:{s:02d}"


def _looks_like_closing_summary(text: str) -> bool:
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if any(line.startswith("## ") for line in lines):
        return True

    bullet_run = 0
    for line in lines:
        if line.startswith("- "):
            bullet_run += 1
            if bullet_run >= 3:
                return True
        else:
            bullet_run = 0
    return False


def _prompt_flood_summary(text: str) -> str | None:
    """If ``text`` looks like a subagent prompt body, return a one-line
    summary. Otherwise return None.

    Heuristic: at least 3 markdown `## ` headings, OR a `<spec source=`
    tag, OR the literal `## Verdict Format` string.
    """
    heading_count = len(_PROMPT_FLOOD_HEADING_RE.findall(text))
    has_spec_tag = "<spec source=" in text
    has_verdict_header = "## Verdict Format" in text
    if heading_count < 3 and not has_spec_tag and not has_verdict_header:
        return None

    line_count = text.count("\n") + 1
    first_heading = ""
    for raw in text.split("\n"):
        s = raw.strip()
        if s.startswith("## "):
            first_heading = s[3:].strip()
            break
    if first_heading:
        return f"[subagent prompt: {line_count} lines, {first_heading}]"
    return f"[subagent prompt: {line_count} lines]"


def _maybe_extract_subagent_text(content: Any) -> str | None:
    """If ``content`` is a list of ``{"type":"text","text":"..."}`` dicts
    (common subagent result shape), concatenate the text values. Also
    handles the string repr of that list (``[{'type': 'text', ...}]``).

    Returns None if the content doesn't match the pattern.
    """
    parsed: Any = None
    if isinstance(content, list):
        parsed = content
    elif isinstance(content, str):
        stripped = content.lstrip()
        if not stripped.startswith("["):
            return None
        # Try JSON first (double-quoted). Subagent outputs are often
        # python-repr style with single quotes; fall through to ast.literal_eval.
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            import ast
            try:
                parsed = ast.literal_eval(stripped)
            except (ValueError, SyntaxError):
                return None
    else:
        return None

    if not isinstance(parsed, list) or not parsed:
        return None

    texts: list[str] = []
    for item in parsed:
        if not isinstance(item, dict):
            return None
        if item.get("type") != "text" or "text" not in item:
            return None
        texts.append(str(item.get("text", "")))
    return "\n".join(texts)


def _looks_like_certifier_prompt(tool_input: Any) -> bool:
    """Heuristic: does this Agent tool_input look like a certifier dispatch?

    Otto's certifier prompts always contain the literal ``## Verdict Format``
    heading — that uniquely identifies them. Bare ``STORY_RESULT`` mentions
    are too common (ordinary prose might mention it) so we don't key off that.
    """
    if not isinstance(tool_input, dict):
        return False
    prompt = tool_input.get("prompt") or ""
    if not isinstance(prompt, str):
        return False
    return "## Verdict Format" in prompt


def estimate_phase_costs(
    messages_jsonl: Path,
    total_cost_usd: float,
) -> dict[str, dict[str, Any]] | None:
    """Estimate build/certify cost split from assistant output-token share.

    Assistant messages are classified into build vs certify brackets by
    certifier-shaped Agent dispatches. Missing files, malformed JSONL,
    missing usage, or zero-token runs return ``None``.
    """
    if total_cost_usd <= 0 or not messages_jsonl.exists():
        return None

    build_tokens = 0
    certify_tokens = 0
    in_certify_round = False
    certifier_tool_use_id: str | None = None

    try:
        with messages_jsonl.open(encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("type") != "assistant":
                    continue

                blocks = rec.get("blocks")
                if not isinstance(blocks, list):
                    return None

                opens_certify = False
                closes_certify = False
                opening_tool_use_id: str | None = None
                for block in blocks:
                    if not isinstance(block, dict):
                        return None
                    block_type = block.get("type")
                    if (
                        block_type == "tool_use"
                        and block.get("name") == "Agent"
                        and _looks_like_certifier_prompt(block.get("input"))
                    ):
                        opens_certify = True
                        opening_tool_use_id = str(block.get("id", "") or "")
                    elif (
                        block_type == "tool_result"
                        and certifier_tool_use_id
                        and str(block.get("tool_use_id", "") or "") == certifier_tool_use_id
                    ):
                        closes_certify = True

                usage = rec.get("usage")
                output_tokens = 0
                if isinstance(usage, dict):
                    raw_tokens = usage.get("output_tokens")
                    if isinstance(raw_tokens, int | float):
                        output_tokens = max(int(raw_tokens), 0)

                if opens_certify or in_certify_round:
                    certify_tokens += output_tokens
                else:
                    build_tokens += output_tokens

                if opens_certify:
                    in_certify_round = True
                    certifier_tool_use_id = opening_tool_use_id
                if closes_certify:
                    in_certify_round = False
                    certifier_tool_use_id = None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None

    total_tokens = build_tokens + certify_tokens
    if total_tokens <= 0:
        return None

    estimated: dict[str, dict[str, Any]] = {}
    if build_tokens > 0:
        estimated["build"] = {
            "cost_usd": round(float(total_cost_usd) * (build_tokens / total_tokens), 4),
            "estimated": True,
        }
    if certify_tokens > 0:
        estimated["certify"] = {
            "cost_usd": round(float(total_cost_usd) * (certify_tokens / total_tokens), 4),
            "estimated": True,
        }
    return estimated or None


def _summarize_round(marker_lines: list[str]) -> tuple[str, int, int]:
    """Extract (verdict, passed_count, tested_count) from marker lines."""
    verdict = "?"
    passed = 0
    tested = 0
    for line in marker_lines:
        if line.startswith("VERDICT:"):
            v = line.split(":", 1)[1].strip().upper()
            if "PASS" in v:
                verdict = "PASS"
            elif "FAIL" in v:
                verdict = "FAIL"
            elif "WARN" in v:
                verdict = "WARN"
        elif line.startswith("STORIES_PASSED:"):
            try:
                passed = int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                pass
        elif line.startswith("STORIES_TESTED:"):
            try:
                tested = int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                pass
    return verdict, passed, tested


def _first_meaningful_line(content: str) -> str:
    """Return the first line with meaningful content.

    Skips blank lines and lines that are only whitespace + a single
    bracket/punctuation char (e.g. `}`, `)`, `]`, `{`, `(`, `[`).
    Those are typically tail-end of JSON or code-block closers with
    no information value on their own.

    Returns "" if no meaningful line found.
    """
    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if len(stripped) == 1 and stripped in "{}()[]":
            continue
        return stripped
    return ""


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


def make_session_logger(log_dir: Path, *, phase_name: str = "BUILD") -> dict[str, Any]:
    """Open messages.jsonl + narrative.log in ``log_dir`` and return the
    callback dict for run_agent_with_timeout / run_agent_query.

    Also maintains a ``live.log`` symlink -> ``narrative.log`` for one
    release so existing `tail -f …/live.log` habits keep working. If
    symlinks are unsupported, live.log is just absent (narrative.log is
    the canonical file).

    Returned callbacks:
      on_message  — receives every normalized SDK message (assistant/result).
      _close      — closes both writers; callers must invoke in `finally`.
      _narrative  — NarrativeFormatter for post-run timing inspection/finalize.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    jsonl = JsonlMessageWriter(log_dir / "messages.jsonl")
    narr = NarrativeFormatter(log_dir / "narrative.log", phase_name=phase_name)
    narr.start()

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

    return {"on_message": _on_message, "_close": _close, "_narrative": narr}
