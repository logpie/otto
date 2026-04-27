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
import shlex
import subprocess
import time
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable
from pathlib import Path

from rich.markup import escape as rich_escape

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
from otto.markers import _STORY_RESULT_RE, _VERDICT_RE, _parse_story_result_fields
from otto.redaction import redact_text
from otto.token_usage import (
    TOKEN_USAGE_KEYS,
    add_token_usage,
    empty_token_usage,
    normalize_token_usage,
    token_total,
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
_TERMINAL_STAMP_RE = re.compile(r"^\[\+\d+:\d{2}(?::\d{2})?\]\s+")
_TOOL_USE_ERROR_TAG_RE = re.compile(r"</?tool_use_error>")
_SHELL_NOISE_SPLIT_RE = re.compile(r"\s*(?:\||&&|;|>|<)\s*")
_AGENT_BROWSER_SESSION_RE = re.compile(r"(?:^|\s)--session\s+([^\s]+)")
_DISPATCH_STORY_SECTION_RE = re.compile(r"(?is)\*\*Story:\s*([^\n*]+?)\*\*\s*(.*?)(?=\n\*\*Story:|\Z)")
_SESSION_NAME_HINT_RE = re.compile(r"session name\s+[\"']?([a-zA-Z0-9._-]+)[\"']?", re.IGNORECASE)
_WORD_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
_GENERIC_BROWSER_SESSIONS = {"anonymous", "default", "main"}
_COMMON_TEXT_TOKENS = {
    "agent",
    "agent-browser",
    "all",
    "and",
    "bash",
    "browser",
    "button",
    "card",
    "cards",
    "capture",
    "click",
    "commands",
    "description",
    "end",
    "eval",
    "for",
    "http",
    "https",
    "localstorage",
    "open",
    "page",
    "pass",
    "prompt",
    "reload",
    "report",
    "result",
    "run",
    "screenshot",
    "session",
    "snapshot",
    "stories",
    "story",
    "test",
    "timeout",
    "tool",
    "type",
    "use",
    "verify",
    "with",
}


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


def _block_to_dict(block: Any, *, redact: bool = True) -> dict[str, Any]:
    """Serialize a normalized SDK block to a JSON-safe dict."""
    if isinstance(block, TextBlock):
        return {"type": "text", "text": _maybe_redact_text(block.text, redact=redact)}
    if isinstance(block, ThinkingBlock):
        return {"type": "thinking", "thinking": _maybe_redact_text(block.thinking, redact=redact)}
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": _redact_obj(block.input, redact=redact),
        }
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": _maybe_redact_text(block.content, redact=redact),
            "is_error": block.is_error,
        }
    return {"type": "unknown", "repr": str(block)[:500]}


def _maybe_redact_text(value: str, *, redact: bool) -> str:
    return redact_text(value) if redact else value


def _redact_obj(value: Any, *, redact: bool = True) -> Any:
    if isinstance(value, str):
        return _maybe_redact_text(value, redact=redact)
    if isinstance(value, list):
        return [_redact_obj(item, redact=redact) for item in value]
    if isinstance(value, dict):
        return {key: _redact_obj(item, redact=redact) for key, item in value.items()}
    return value


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


def normalize_phase_breakdown(
    total_elapsed: float,
    breakdown: dict[str, dict[str, Any]] | None,
    *,
    primary_phase: str | None = None,
) -> dict[str, dict[str, Any]] | None:
    """Return a copy of ``breakdown`` whose durations sum to ``total_elapsed``.

    Build/improve transcripts can alternate between build and certify multiple
    times. Some legacy paths only captured the pre-first-certify build span,
    which omits later fix/build work. This normalizer assigns all remaining
    non-certify wall-clock time to the primary phase so phase totals stay
    consistent with the session total.
    """
    if breakdown is None:
        return None

    normalized = {
        str(phase): dict(data)
        for phase, data in breakdown.items()
        if isinstance(data, dict)
    }
    if not normalized:
        return None

    total_elapsed = max(float(total_elapsed), 0.0)
    duration_entries = {
        phase: float(data["duration_s"])
        for phase, data in normalized.items()
        if isinstance(data.get("duration_s"), int | float)
    }
    if not duration_entries:
        return normalized

    if primary_phase is None or primary_phase not in normalized:
        for candidate in ("build", "spec", "certify"):
            if candidate in normalized:
                primary_phase = candidate
                break
        else:
            primary_phase = next(iter(normalized))

    other_total = sum(
        duration
        for phase, duration in duration_entries.items()
        if phase != primary_phase
    )
    if other_total <= total_elapsed:
        normalized.setdefault(primary_phase, {})
        normalized[primary_phase]["duration_s"] = max(total_elapsed - other_total, 0.0)
    else:
        total_phase_duration = sum(duration_entries.values())
        scale = (total_elapsed / total_phase_duration) if total_phase_duration > 0 else 0.0
        for phase, duration in duration_entries.items():
            normalized[phase]["duration_s"] = max(duration * scale, 0.0)

    normalized_total = sum(
        float(data.get("duration_s", 0.0))
        for data in normalized.values()
        if isinstance(data.get("duration_s"), int | float)
    )
    if abs(normalized_total - total_elapsed) > 0.01:
        raise AssertionError(
            f"phase breakdown must sum to total duration: {normalized_total:.3f}s != {total_elapsed:.3f}s"
        )
    return normalized


class JsonlMessageWriter:
    """Append one JSON object per SDK message event.

    Opens in append mode and flushes after every write so crashes don't
    lose events and `tail -f | jq` works during a run.
    """

    def __init__(
        self,
        path: Path,
        *,
        phase_name: str = "BUILD",
        redact: bool = True,
        emit_phase_events: bool = False,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._fh = open(path, "a", encoding="utf-8")
        self._start = time.monotonic()
        self._redact = redact
        self._emit_phase_events = emit_phase_events
        self._phase_name = (phase_name or "BUILD").strip().lower()
        self._phase = self._phase_name if self._phase_name in {"build", "certify", "spec"} else "build"
        self._phase_started_monotonic = self._start
        self._phase_usage_current = _empty_usage()
        self._phase_usage_totals: dict[str, dict[str, float | int]] = {}
        self._last_usage_seen: dict[str, float | int] | None = None
        self._phase_switch_count = 0
        self._tool_by_id: dict[str, str] = {}
        self._agent_input_by_id: dict[str, dict[str, Any]] = {}
        self._subagent_retry_counts: dict[str, int] = {}
        self._last_records: list[dict[str, Any]] = []
        self._closed = False
        if self._emit_phase_events:
            self._write_phase_event("phase_start", self._phase)

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
                record["result"] = _maybe_redact_text(message.result, redact=self._redact)
            if message.structured_output is not None:
                record["structured_output"] = message.structured_output
            if message.total_cost_usd is not None:
                record["cost_usd"] = message.total_cost_usd
            if message.usage is not None:
                record["usage"] = _coerce_usage(message.usage)
        elif isinstance(message, UserMessage):
            record["type"] = "user"
            record["blocks"] = [_block_to_dict(b, redact=self._redact) for b in message.content]
            if message.usage is not None:
                record["usage"] = _coerce_usage(message.usage)
        elif isinstance(message, AssistantMessage):
            record["type"] = "assistant"
            record["blocks"] = [_block_to_dict(b, redact=self._redact) for b in message.content]
            if message.usage is not None:
                record["usage"] = _coerce_usage(message.usage)
        else:
            record["type"] = "unknown"
            record["repr"] = str(message)[:500]
        self._record_tool_metadata(message)
        self._record_usage(record)
        self._write_record(record)
        self._advance_phase(record)

    def emit_event(self, event: dict[str, Any]) -> None:
        record = {
            "ts": _iso_ts(),
            "elapsed_s": round(time.monotonic() - self._start, 3),
            **event,
        }
        self._write_record(record)

    def phase_breakdown(self, *, include_open: bool = True) -> dict[str, dict[str, float | int]]:
        data = deepcopy(self._phase_usage_totals)
        if include_open and not self._closed:
            current = data.setdefault(
                self._phase,
                {"duration_s": 0.0, **_empty_usage()},
            )
            current["duration_s"] = float(current.get("duration_s", 0.0)) + max(
                time.monotonic() - self._phase_started_monotonic,
                0.0,
            )
            add_token_usage(current, self._phase_usage_current)
            current["cost_usd"] = float(current.get("cost_usd", 0.0) or 0.0) + float(self._phase_usage_current.get("cost_usd", 0.0) or 0.0)
        return data

    def last_records(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        return deepcopy(self._last_records[-limit:])

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._emit_phase_events:
                self._end_phase(self._phase)
            self._fh.close()
        except OSError:
            pass

    def _write_record(self, record: dict[str, Any]) -> None:
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str))
        self._fh.write("\n")
        self._fh.flush()
        self._last_records.append(deepcopy(record))
        if len(self._last_records) > 40:
            self._last_records = self._last_records[-40:]

    def _write_phase_event(
        self,
        event_type: str,
        phase: str,
        *,
        duration_s: float | None = None,
        usage: dict[str, float | int] | None = None,
    ) -> None:
        event: dict[str, Any] = {"type": event_type, "phase": phase, "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        if duration_s is not None:
            event["duration_s"] = round(float(duration_s), 3)
        if usage is not None:
            clean_usage = normalize_token_usage(usage)
            cost_value = usage.get("cost_usd")
            if isinstance(cost_value, int | float):
                clean_usage["cost_usd"] = round(float(cost_value), 4)
            event["usage"] = clean_usage
        self._write_record(event)

    def _record_tool_metadata(self, message: Any) -> None:
        if not isinstance(message, (AssistantMessage, UserMessage)):
            return
        for block in getattr(message, "content", []) or []:
            if isinstance(block, ToolUseBlock) and block.id:
                self._tool_by_id[block.id] = block.name or ""
                if block.name == "Agent":
                    self._agent_input_by_id[block.id] = dict(block.input or {})

    def _usage_delta(self, usage: dict[str, Any] | None) -> dict[str, float | int]:
        if not isinstance(usage, dict):
            return _empty_usage()
        current: dict[str, float | int] = {
            **normalize_token_usage(usage),
            "cost_usd": float(
                usage.get(
                    "total_cost_usd",
                    usage.get("cost_usd", usage.get("estimated_cost_usd", 0.0)),
                )
                or 0.0
            ),
        }
        previous = self._last_usage_seen
        self._last_usage_seen = current
        if previous is None:
            return current
        monotonic_tokens = all(current[key] >= previous.get(key, 0) for key in TOKEN_USAGE_KEYS)
        if monotonic_tokens:
            delta = {key: (current[key] - previous.get(key, 0)) for key in TOKEN_USAGE_KEYS}
            previous_cost = float(previous.get("cost_usd", 0.0) or 0.0)
            current_cost = float(current.get("cost_usd", 0.0) or 0.0)
            delta["cost_usd"] = current_cost - previous_cost if current_cost >= previous_cost else current_cost
            return delta
        return current

    def _record_usage(self, record: dict[str, Any]) -> None:
        if (
            record.get("type") == "result"
            and self._phase_switch_count == 0
            and isinstance(record.get("usage"), dict)
        ):
            usage = {
                **normalize_token_usage(record["usage"]),
                "cost_usd": float(record.get("cost_usd") or 0.0),
            }
            self._phase_usage_current = usage
            self._last_usage_seen = usage
            return
        delta = self._usage_delta(record.get("usage"))
        add_token_usage(self._phase_usage_current, delta)
        self._phase_usage_current["cost_usd"] += float(delta.get("cost_usd", 0.0) or 0.0)

    def _end_phase(self, phase: str) -> None:
        duration_s = max(time.monotonic() - self._phase_started_monotonic, 0.0)
        usage = dict(self._phase_usage_current)
        if self._emit_phase_events:
            self._write_phase_event("phase_end", phase, duration_s=duration_s, usage=usage)
        totals = self._phase_usage_totals.setdefault(
            phase,
            {
                "duration_s": 0.0,
                "input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
            },
        )
        totals["duration_s"] = float(totals.get("duration_s", 0.0)) + duration_s
        add_token_usage(totals, usage)
        totals["cost_usd"] = float(totals.get("cost_usd", 0.0) or 0.0) + float(usage.get("cost_usd", 0.0) or 0.0)
        self._phase_usage_current = _empty_usage()

    def _start_phase(self, phase: str) -> None:
        self._phase = phase
        self._phase_started_monotonic = time.monotonic()
        self._phase_switch_count += 1
        if self._emit_phase_events:
            self._write_phase_event("phase_start", phase)

    def _advance_phase(self, record: dict[str, Any]) -> None:
        blocks = record.get("blocks")
        if not isinstance(blocks, list):
            return
        opens_certify = False
        closes_certify = False
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if (
                block.get("type") == "tool_use"
                and block.get("name") == "Agent"
                and _looks_like_certifier_prompt(block.get("input"))
            ):
                opens_certify = True
            elif (
                block.get("type") == "tool_result"
                and self._tool_by_id.get(str(block.get("tool_use_id") or ""), "") == "Agent"
                and not block.get("is_error")
                and self._phase == "certify"
            ):
                closes_certify = True
        if opens_certify and self._phase == "build":
            self._end_phase("build")
            self._start_phase("certify")
        elif closes_certify and self._phase == "certify" and self._phase_name == "build":
            self._end_phase("certify")
            self._start_phase("build")


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
    _GLYPH_PHASE = "\u2501" * 3  # ━━━ final summary banner
    _GLYPH_WARNING = "\u26a0"    # ⚠ recoverable tool warning

    def __init__(
        self,
        path: Path,
        *,
        phase_name: str = "BUILD",
        phase_label: str | None = None,
        stdout_callback: Callable[[str], None] | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        verbose: bool = False,
        strict_mode: bool = False,
        project_dir: Path | None = None,
        redact: bool = True,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._fh = open(path, "a", encoding="utf-8")
        self._start = time.monotonic()
        self._phase_name = (phase_name or "BUILD").upper()
        self._phase_label = (phase_label or self._phase_name).upper()
        self._stdout_callback = stdout_callback
        self._event_callback = event_callback
        self._verbose = verbose
        self._strict_mode = strict_mode
        self._project_dir = project_dir
        self._redact = redact
        # tool_use_id -> tool name, so ToolResultBlock renderers can
        # tailor output (Glob=>"(N files)", Read=>"(N lines)", Agent=>
        # subagent glyph).
        self._tool_by_id: dict[str, str] = {}
        self._tool_call_count: int = 0
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
        self._last_terminal_event_monotonic = self._start
        self._pending_tool_error: dict[str, Any] | None = None
        self._tool_error_chain_count = 0
        self._recovered_tool_errors = 0
        self._last_round_verdict: str | None = None
        self._current_phase_label = self._initial_phase_label()
        self._phase_started_monotonic = self._start
        self._latest_activity: str | None = None
        self._latest_activity_tool_name: str | None = None
        self._latest_tool_args_summary: str | None = None
        self._current_story_id: str | None = None
        self._last_operation_started_at: str | None = None
        self._streamed_story_keys: set[tuple[int, str]] = set()
        self._git_context_label_cache: str | None = None
        self._in_certify_round = False

    def start(self) -> None:
        if self._phase_started:
            return
        self._phase_started = True
        self._write_terminal_event(
            self._phase_banner(f"{self._phase_label} starting"),
            style="dim",
        )

    def _elapsed_seconds(self) -> float:
        return time.monotonic() - self._start

    def _initial_phase_label(self) -> str:
        if self._phase_name == "SPEC":
            return "specing"
        if self._phase_name == "CERTIFY":
            return "verifying"
        return "building"

    def _elapsed_fmt(self) -> str:
        secs = int(self._elapsed_seconds())
        return _format_elapsed_seconds(secs)

    def _phase_complete_line(self) -> str:
        if self._phase_name == "BUILD":
            return self._phase_banner("BUILD complete; starting verification")
        return self._phase_banner(f"{self._phase_label} complete")

    def _phase_banner(self, label: str) -> str:
        return f"{self._stamp()} \u2014 {label} \u2014"

    def _write_phase_complete(self) -> None:
        if self._phase_complete_written:
            return
        self._phase_complete_written = True
        self._write_terminal_event(self._phase_complete_line(), style="dim")

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
        breakdown = normalize_phase_breakdown(
            total_elapsed,
            breakdown,
            primary_phase=self._summary_label(),
        )

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
            if float(total_cost_usd) > 0:
                total_segment += f"${float(total_cost_usd):.2f} "
            else:
                tokens = _total_token_usage(breakdown)
                if tokens["input_tokens"] or tokens["output_tokens"]:
                    total_segment += (
                        f"{_format_compact_tokens(tokens['input_tokens'])} in/"
                        f"{_format_compact_tokens(tokens['output_tokens'])} out "
                    )
        total_segment += _format_elapsed_seconds(total_elapsed)
        parts.append(total_segment)
        return (
            f"{self._stamp()} {self._GLYPH_PHASE} RUN SUMMARY: "
            f"{', '.join(parts)} {self._GLYPH_PHASE}"
        )

    def _stamp(self) -> str:
        return f"[+{self._elapsed_fmt()}]"

    def _write(self, line: str) -> None:
        line = _maybe_redact_text(line, redact=self._redact)
        if not line.endswith("\n"):
            line = line + "\n"
        self._fh.write(line)
        self._fh.flush()

    def _emit_terminal(self, line: str, *, style: str | None = None) -> None:
        line = _maybe_redact_text(line, redact=self._redact)
        self._last_terminal_event_monotonic = time.monotonic()
        if self._stdout_callback is None:
            return
        rendered = f"  {_TERMINAL_STAMP_RE.sub('', line, count=1)}"
        escaped = rich_escape(rendered)
        if style:
            self._stdout_callback(f"[{style}]{escaped}[/{style}]")
            return
        self._stdout_callback(escaped)

    def _write_terminal_event(self, line: str, *, style: str | None = None) -> None:
        self._write(line)
        self._emit_terminal(line, style=style)

    def _set_phase_activity(self, label: str) -> None:
        if label == self._current_phase_label:
            return
        self._current_phase_label = label
        self._phase_started_monotonic = time.monotonic()

    def phase_elapsed_seconds(self) -> float:
        return time.monotonic() - self._phase_started_monotonic

    def latest_activity(self) -> str:
        return self._latest_activity or self._current_phase_label

    def latest_tool_name(self) -> str:
        return self._latest_activity_tool_name or ""

    def latest_tool_args_summary(self) -> str:
        return self._latest_tool_args_summary or ""

    def current_story_id(self) -> str:
        return self._current_story_id or ""

    def last_operation_started_at(self) -> str:
        return self._last_operation_started_at or ""

    def write_heartbeat(self, elapsed: str) -> None:
        label = self._current_phase_label
        tool_calls = self._tool_call_count
        noun = "call" if tool_calls == 1 else "calls"
        activity = f" \u00b7 {self._latest_activity}" if self._latest_activity else ""
        detailed = (
            f"{self._stamp()} \u22ef {label}\u2026 ({elapsed}){activity}"
            f" \u00b7 {tool_calls} tool {noun}"
        )
        terminal = (
            detailed if self._verbose
            else f"{self._stamp()} \u22ef {label}\u2026 ({elapsed}){activity}"
        )
        self._write(detailed)
        self._emit_terminal(terminal, style="dim")

    def last_terminal_event_monotonic(self) -> float:
        return self._last_terminal_event_monotonic

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
            self._tool_call_count += 1
            self._latest_activity = _tool_activity_label(block)
            self._latest_activity_tool_name = block.name or ""
            self._latest_tool_args_summary = tool_use_summary(block) or ""
            self._last_operation_started_at = _iso_ts()
            # Remember the tool name so tool_result renderers can look
            # up the originator by tool_use_id.
            if block.id:
                self._tool_by_id[block.id] = block.name or ""
                if block.name == "Agent":
                    self._current_story_id = _find_story_hint(block.input, self._story_ids_seen()) or self._extract_story_id_from_tool_input(block.input)
            # Certify-phase banners — Agent dispatches with a
            # certifier-shaped prompt open a new round.
            if block.name == "Agent" and _looks_like_certifier_prompt(block.input):
                round_start_elapsed = self._elapsed_seconds()
                if self._agent_dispatch_count == 0:
                    self._build_duration = round_start_elapsed
                    if self._phase_name == "BUILD":
                        self._write_phase_complete()
                self._agent_dispatch_count += 1
                self._in_certify_round = True
                self._set_phase_activity("verifying")
                self._latest_activity = "running verifier"
                self._latest_activity_tool_name = "Agent"
                round_n = self._agent_dispatch_count
                if self._strict_mode and round_n == 2 and self._last_round_verdict == "PASS":
                    self._write_terminal_event(
                        f"{ts} \u2713 round 1 passed \u2014 re-verifying for consistency (strict mode)",
                        style="dim",
                    )
                if block.id:
                    self._agent_round_by_id[block.id] = round_n
                    self._round_start_elapsed_by_id[block.id] = round_start_elapsed
                self._write_terminal_event(
                    self._phase_banner(f"CERTIFY ROUND {round_n}"),
                    style="dim",
                )
                return
            if self._phase_name == "BUILD" and not self._in_certify_round:
                self._set_phase_activity("building")
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
                    line.strip() for line in text.split("\n")
                    if line.strip() and not _is_marker(line.strip())
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
            self._buffer_tool_error(tool_name, content)
            return
        self._recover_tool_errors()
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
            diagnosis = ""
            for marker in marker_lines:
                if marker.startswith("DIAGNOSIS:"):
                    diagnosis = _parse_diagnosis_marker(marker)
                    break
            if is_subagent:
                prose = "\n".join(
                    line for line in content.split("\n")
                    if line.strip() and not _is_marker(line.strip())
                ).strip()
                if prose:
                    flat = " ".join(prose.split())
                    self._write(
                        f"{ts} {self._GLYPH_SUBAGENT} [subagent]: "
                        f"{_truncate_at_word(flat)}"
                    )
            round_n_for_stream = self._agent_round_by_id.get(block.tool_use_id or "", 0)
            self._stream_story_results(ts, marker_lines, diagnosis, round_n_for_stream)
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
                    self._last_round_verdict = verdict
                    self._in_certify_round = False
                    if self._phase_name == "BUILD":
                        self._set_phase_activity("building")
                    stats = f" ({passed}/{tested})" if tested else ""
                    self._write_terminal_event(
                        self._phase_banner(f"CERTIFY ROUND {round_n} \u2192 {verdict}{stats}"),
                        style="dim",
                    )
            return

        # Detect git-commit in Bash output, elevate to a distinct glyph.
        commit_line = _extract_commit_line(content)
        if commit_line:
            self._write_terminal_event(
                f"{ts} \u2022 committed to {self._git_context_label()}: {commit_line}",
                style="dim",
            )
            return

        # Subagent result — dedicated glyph + word-safe truncation.
        if is_subagent:
            flat = " ".join(content.split())
            self._write(f"{ts} {self._GLYPH_SUBAGENT} [subagent]: {_truncate_at_word(flat)}")
            return

        # Glob — show just "(N files)".
        if tool_name == "Glob":
            lines = [line for line in content.split("\n") if line.strip()]
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

    def _tool_error_reason(self, content: str) -> str:
        cleaned = _TOOL_USE_ERROR_TAG_RE.sub("", content or "")
        first = _first_meaningful_line(cleaned) or "(empty)"
        return _truncate_at_word(first.strip(), 80)

    def _buffer_tool_error(self, tool_name: str, content: str) -> None:
        pending = {
            "tool_name": tool_name or "unknown",
            "reason": self._tool_error_reason(content),
            "raw_reason": content or "",
            "story_id": self._current_story_id or "",
            "child_session_id": self._extract_child_session_id(content),
        }
        if self._pending_tool_error is not None:
            self._pending_tool_error["_final_effect_on_verdict"] = "FAIL"
            self._emit_tool_warning(self._pending_tool_error)
        self._pending_tool_error = pending
        self._tool_error_chain_count += 1

    def _recover_tool_errors(self) -> None:
        if self._tool_error_chain_count <= 0:
            return
        if self._pending_tool_error is not None and self._pending_tool_error.get("tool_name") == "Agent":
            self._pending_tool_error["_final_effect_on_verdict"] = "WARN"
            self._emit_tool_warning(self._pending_tool_error)
        self._recovered_tool_errors += self._tool_error_chain_count
        self._tool_error_chain_count = 0
        self._pending_tool_error = None

    def _emit_tool_warning(self, pending: dict[str, Any]) -> None:
        ts = self._stamp()
        tool_name = pending.get("tool_name", "unknown")
        reason = pending.get("reason", "(empty)")
        final_effect = str(pending.get("_final_effect_on_verdict") or "FAIL")
        if tool_name == "Agent" and self._event_callback is not None:
            self._event_callback(
                {
                    "type": "subagent_error",
                    "story_id": pending.get("story_id", ""),
                    "child_session_id": pending.get("child_session_id", ""),
                    "reason": self._classify_subagent_reason(pending.get("raw_reason", reason)),
                    "retry_count": int(self._tool_error_chain_count or 1),
                    "final_effect_on_verdict": final_effect,
                }
            )
        self._write_terminal_event(
            f"{ts} {self._GLYPH_WARNING} tool {tool_name} retry: {reason}",
            style="dim",
        )

    def _stream_story_results(
        self,
        ts: str,
        marker_lines: list[str],
        diagnosis: str,
        round_n: int,
    ) -> None:
        for line in marker_lines:
            story = _parse_story_result_marker(line)
            if story is None:
                continue
            story_key = (round_n, story["story_id"] or story["summary"])
            if story_key in self._streamed_story_keys:
                continue
            self._streamed_story_keys.add(story_key)
            glyph = "\u2713" if story["passed"] else "\u2717"
            rendered = story["summary"]
            if not story["passed"] and diagnosis:
                rendered += f" \u2014 {_truncate_at_word(diagnosis, 100)}"
            style = "success" if story["passed"] else "red"
            self._write_terminal_event(f"{ts} {glyph} {rendered}", style=style)

    def _git_context_label(self) -> str:
        if self._git_context_label_cache is not None:
            return self._git_context_label_cache
        label = "current branch"
        if self._project_dir is not None:
            for cmd in (
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                ["git", "branch", "--show-current"],
            ):
                try:
                    result = subprocess.run(
                        cmd,
                        cwd=self._project_dir,
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )
                except (OSError, subprocess.SubprocessError):
                    continue
                branch = result.stdout.strip()
                if result.returncode == 0 and branch:
                    label = branch
                    break
        self._git_context_label_cache = label
        return label

    def finalize(
        self,
        breakdown: dict[str, dict[str, float | int]] | None = None,
    ) -> dict[str, int]:
        if self._summary_written:
            return {"recovered_tool_errors": self._recovered_tool_errors}
        self._summary_written = True

        if self._pending_tool_error is not None:
            self._pending_tool_error["_final_effect_on_verdict"] = "FAIL"
            self._emit_tool_warning(self._pending_tool_error)
            self._pending_tool_error = None

        message = self._pending_result
        if message is None:
            return {"recovered_tool_errors": self._recovered_tool_errors}

        ts = self._stamp()
        total_elapsed = self._elapsed_seconds()
        if self._phase_name != "BUILD":
            self._write_phase_complete()
        self._write_terminal_event(self._summary_line(total_elapsed, breakdown, message.total_cost_usd))
        status = "ERROR" if message.is_error else message.subtype.upper()
        cost = f" ${message.total_cost_usd:.2f}" if message.total_cost_usd else ""
        duration = f" in {self._elapsed_fmt()}"
        self._write_terminal_event(f"{ts} \u2501\u2501\u2501 {status}{cost}{duration}")
        return {"recovered_tool_errors": self._recovered_tool_errors}

    def close(self) -> None:
        self.finalize()
        try:
            self._fh.close()
        except OSError:
            pass

    def _story_ids_seen(self) -> list[str]:
        story_ids = {
            story_id
            for _round, story_id in self._streamed_story_keys
            if story_id
        }
        return sorted(story_ids)

    def _extract_story_id_from_tool_input(self, tool_input: Any) -> str:
        if not isinstance(tool_input, dict):
            return ""
        for key in ("story_id", "name"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        prompt = str(tool_input.get("prompt") or tool_input.get("description") or "")
        match = re.search(r"\*\*Story:\s*([^\n*]+)", prompt)
        if match:
            return match.group(1).strip()
        return ""

    def _extract_child_session_id(self, content: str) -> str:
        if not content:
            return ""
        for pattern in (
            re.compile(r"session[_ -]?id[:=]\s*([a-zA-Z0-9._-]+)", re.IGNORECASE),
            re.compile(r"child[_ -]?session[_ -]?id[:=]\s*([a-zA-Z0-9._-]+)", re.IGNORECASE),
        ):
            match = pattern.search(content)
            if match:
                return match.group(1).strip()
        return ""

    def _classify_subagent_reason(self, text: str) -> str:
        lowered = str(text or "").lower()
        if "timeout" in lowered or "timed out" in lowered:
            return "timeout"
        if "crash" in lowered or "killed" in lowered:
            return "crash"
        return "tool-error"


def _is_marker(line: str) -> bool:
    if not any(line.startswith(m) for m in _CERTIFY_MARKERS):
        return False
    if line.startswith("VERDICT:") and not _VERDICT_RE.match(line):
        return False
    if line.startswith("STORY_RESULT:") and not _STORY_RESULT_RE.match(line):
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


def _format_compact_tokens(value: int | float) -> str:
    number = max(float(value or 0), 0.0)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M".rstrip("0").rstrip(".")
    if number >= 1_000:
        return f"{number / 1_000:.1f}K".rstrip("0").rstrip(".")
    return str(int(number))


def _total_token_usage(breakdown: dict[str, dict[str, Any]] | None) -> dict[str, int]:
    totals = empty_token_usage()
    for phase_data in (breakdown or {}).values():
        if not isinstance(phase_data, dict):
            continue
        add_token_usage(totals, phase_data)
    return {key: int(totals.get(key, 0) or 0) for key in TOKEN_USAGE_KEYS}


def _empty_usage() -> dict[str, float | int]:
    return {**empty_token_usage(), "cost_usd": 0.0}


def _usage_total(usage: dict[str, Any]) -> int:
    return token_total(usage)


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
                rec_type = rec.get("type")
                if rec_type not in {"assistant", "user"}:
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

                if opens_certify or closes_certify or in_certify_round:
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


def _iter_jsonl_records(messages_jsonl: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with messages_jsonl.open(encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if isinstance(rec, dict):
                records.append(rec)
    return records


def _story_match_aliases(story_id: str) -> set[str]:
    base = str(story_id or "").strip().lower()
    if not base:
        return set()
    aliases = {base}
    aliases.add(base.replace("_", "-"))
    aliases.add(base.replace("-", " "))
    aliases.add(base.replace("_", " "))
    return {alias for alias in aliases if alias}


def _normalize_match_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _keyword_tokens(value: str) -> set[str]:
    tokens = {
        token
        for token in _WORD_TOKEN_RE.findall(_normalize_match_text(value))
        if len(token) >= 3 and token not in _COMMON_TEXT_TOKENS and not token.isdigit()
    }
    return tokens


def _story_specs(
    story_ids: list[str],
    story_claims: dict[str, str] | None,
) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    for story_id in story_ids:
        claim = str((story_claims or {}).get(story_id) or "").strip()
        specs[story_id] = {
            "story_id": story_id,
            "claim": claim,
            "aliases": _story_match_aliases(story_id),
            "id_tokens": _keyword_tokens(story_id),
            "claim_tokens": _keyword_tokens(claim),
        }
    return specs


def _story_match_score(text: str, spec: dict[str, Any]) -> int:
    raw = str(text or "").strip().lower()
    if not raw:
        return 0
    normalized = _normalize_match_text(raw)
    text_tokens = _keyword_tokens(raw)
    score = 0
    for alias in spec["aliases"]:
        alias_norm = _normalize_match_text(alias)
        if alias in raw or (alias_norm and alias_norm in normalized):
            score += 8 if alias == spec["story_id"] else 5
    score += 3 * len(text_tokens & set(spec["id_tokens"]))
    score += len(text_tokens & set(spec["claim_tokens"]))
    return score


def _pick_best_story_from_scores(
    scores: dict[str, int],
    *,
    minimum_score: int = 1,
) -> str | None:
    ranked = sorted(
        ((int(score), story_id) for story_id, score in scores.items() if int(score) >= minimum_score),
        key=lambda item: (-item[0], item[1]),
    )
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


def _tool_input_text(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    parts: list[str] = []
    for key in ("description", "prompt", "summary", "task", "story_id", "name"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)


def _extract_dispatch_story_sections(
    dispatch_text: str,
    story_specs: dict[str, dict[str, Any]],
) -> dict[str, str]:
    sections: dict[str, str] = {}
    for match in _DISPATCH_STORY_SECTION_RE.finditer(dispatch_text or ""):
        heading = match.group(1).strip()
        section_text = match.group(2).strip()
        heading_scores = {
            story_id: _story_match_score(heading, spec)
            for story_id, spec in story_specs.items()
        }
        story_id = _pick_best_story_from_scores(heading_scores, minimum_score=3)
        if story_id and section_text:
            sections[story_id] = section_text
    return sections


def _extract_dispatch_session_hints(dispatch_text: str) -> set[str]:
    hints = {
        match.group(1).strip().strip("\"'").lower()
        for match in _AGENT_BROWSER_SESSION_RE.finditer(dispatch_text or "")
        if match.group(1).strip().strip("\"'")
    }
    hints.update(
        match.group(1).strip().strip("\"'").lower()
        for match in _SESSION_NAME_HINT_RE.finditer(dispatch_text or "")
        if match.group(1).strip().strip("\"'")
    )
    return {hint for hint in hints if hint}


def _find_story_hint(tool_input: Any, story_ids: list[str]) -> str | None:
    if not isinstance(tool_input, dict) or not story_ids:
        return None

    parts: list[str] = []
    for key in ("prompt", "description", "summary", "task", "story_id"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip().lower())
    if not parts:
        return None

    haystack = "\n".join(parts)
    normalized = re.sub(r"[-_]+", " ", haystack)
    for story_id in story_ids:
        aliases = _story_match_aliases(story_id)
        if any(alias in haystack or alias in normalized for alias in aliases):
            return story_id
    return None


def _extract_agent_browser_session(command: str) -> str:
    match = _AGENT_BROWSER_SESSION_RE.search(command)
    if not match:
        return "default"
    session = match.group(1).strip().strip("\"'")
    return session or "default"


def _extract_agent_browser_verb(command: str) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    try:
        browser_index = next(index for index, token in enumerate(tokens) if token == "agent-browser")
    except StopIteration:
        return "unknown"

    expect_value = False
    for token in tokens[browser_index + 1:]:
        if expect_value:
            expect_value = False
            continue
        if token.startswith("--"):
            expect_value = token in {"--session"}
            continue
        if token.startswith("-"):
            continue
        return token
    return "unknown"


def _story_bucket_counts(
    *,
    direct_counts: dict[str, int],
    shared_count: int,
    story_ids: list[str],
) -> dict[str, int]:
    buckets = {story_id: int(direct_counts.get(story_id, 0)) for story_id in story_ids}
    if shared_count <= 0:
        return buckets
    buckets["shared"] = shared_count
    return buckets


def _dispatch_story_scores(
    dispatch_text: str,
    story_specs: dict[str, dict[str, Any]],
) -> dict[str, int]:
    return {
        story_id: _story_match_score(dispatch_text, spec)
        for story_id, spec in story_specs.items()
    }


def _call_story_scores(
    *,
    browser_call: dict[str, Any],
    dispatch: dict[str, Any],
    story_specs: dict[str, dict[str, Any]],
) -> dict[str, int]:
    command = str(browser_call.get("command") or "")
    verb = _extract_agent_browser_verb(command)
    session = str(browser_call.get("session") or "").strip().lower()
    command_tokens = _keyword_tokens(command)
    scores: dict[str, int] = {}
    for story_id, spec in story_specs.items():
        section_text = str(dispatch.get("story_sections", {}).get(story_id) or "")
        if section_text:
            target_text = section_text
            score = _story_match_score(section_text, spec) + 4
        else:
            target_text = str(dispatch.get("text") or "")
            score = int(dispatch.get("story_scores", {}).get(story_id, 0))
        if section_text:
            score += 2
        if target_text:
            target_tokens = _keyword_tokens(target_text)
            if verb != "unknown" and re.search(rf"\b{re.escape(verb)}\b", _normalize_match_text(target_text)):
                score += 5 if section_text else 3
            score += len(command_tokens & target_tokens)
            if session and session not in _GENERIC_BROWSER_SESSIONS:
                if f"--session {session}" in target_text.lower():
                    score += 5
                elif session in _extract_dispatch_session_hints(target_text):
                    score += 4
        score += 2 * len(command_tokens & set(spec["claim_tokens"]))
        scores[story_id] = score
    return scores


def _browser_call_story_id(
    *,
    browser_call: dict[str, Any],
    dispatches: list[dict[str, Any]],
    story_specs: dict[str, dict[str, Any]],
) -> str | None:
    preceding = [dispatch for dispatch in dispatches if int(dispatch["index"]) < int(browser_call["index"])]
    if not preceding or not story_specs:
        return None

    session = str(browser_call.get("session") or "").strip().lower()
    session_specific = session and session not in _GENERIC_BROWSER_SESSIONS
    dispatch: dict[str, Any] | None = None
    if session_specific:
        session_dispatches = [item for item in preceding if session in set(item.get("session_hints", set()))]
        if session_dispatches:
            dispatch = session_dispatches[-1]
    generic_session_is_ambiguous = (not session_specific) and len(preceding) > 1
    if dispatch is None:
        if generic_session_is_ambiguous:
            return None
        dispatch = preceding[-1]

    scores = _call_story_scores(
        browser_call=browser_call,
        dispatch=dispatch,
        story_specs=story_specs,
    )
    story_id = _pick_best_story_from_scores(scores, minimum_score=4)
    if story_id:
        return story_id
    if session_specific:
        return dispatch.get("matched_story_id")
    if len(preceding) == 1:
        return dispatch.get("matched_story_id")
    return None


def browser_efficiency_outlier(
    *,
    certifier_mode: str,
    total_browser_calls: int,
    distinct_sessions: int,
    story_count: int,
    isolated_story_count: int = 0,
) -> tuple[bool, str]:
    story_count = max(int(story_count or 0), 0)
    total_browser_calls = max(int(total_browser_calls or 0), 0)
    distinct_sessions = max(int(distinct_sessions or 0), 0)
    isolated_story_count = max(int(isolated_story_count or 0), 0)
    if story_count <= 0:
        return False, ""

    avg_calls = float(total_browser_calls) / story_count
    reasons: list[str] = []
    if certifier_mode == "fast":
        if total_browser_calls > 30:
            reasons.append(f"total browser calls {total_browser_calls} > 30")
        if avg_calls > 20:
            reasons.append(f"calls per story {avg_calls:.1f} > 20.0")
        return (bool(reasons), f"fast-mode outlier: {'; '.join(reasons)}." if reasons else "")

    if certifier_mode == "thorough":
        if total_browser_calls > 80 * story_count:
            reasons.append(f"total browser calls {total_browser_calls} > {80 * story_count}")
        session_limit = max(4, story_count + 1)
        if distinct_sessions > session_limit:
            reasons.append(f"distinct sessions {distinct_sessions} > {session_limit}")
        if avg_calls > 80:
            reasons.append(f"calls per story {avg_calls:.1f} > 80.0")
        return (bool(reasons), f"thorough-mode outlier: {'; '.join(reasons)}." if reasons else "")

    if total_browser_calls > 45 * story_count:
        reasons.append(f"total browser calls {total_browser_calls} > {45 * story_count}")
    session_limit = max(3, story_count)
    if distinct_sessions > session_limit:
        reasons.append(f"distinct sessions {distinct_sessions} > {session_limit}")
    if avg_calls > 40:
        reasons.append(f"calls per story {avg_calls:.1f} > 40.0")
    return (bool(reasons), f"standard-mode outlier: {'; '.join(reasons)}." if reasons else "")


def summarize_browser_efficiency(
    messages_jsonl: Path,
    *,
    certifier_mode: str,
    story_ids: list[str] | None = None,
    story_claims: dict[str, str] | None = None,
) -> dict[str, Any]:
    story_ids = [str(story_id).strip() for story_id in (story_ids or []) if str(story_id).strip()]
    story_specs = _story_specs(story_ids, story_claims)
    empty = {
        "total_browser_calls": 0,
        "distinct_sessions": 0,
        "verb_counts": {},
        "calls_per_story": _story_bucket_counts(direct_counts={}, shared_count=0, story_ids=story_ids),
        "outlier": False,
        "outlier_reason": "",
    }
    if not messages_jsonl.exists():
        return empty

    try:
        records = _iter_jsonl_records(messages_jsonl)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return empty

    total_browser_calls = 0
    sessions: set[str] = set()
    verb_counts: Counter[str] = Counter()
    direct_counts: dict[str, int] = defaultdict(int)
    shared_count = 0
    story_sessions: dict[str, set[str]] = defaultdict(set)
    dispatches: list[dict[str, Any]] = []
    browser_calls: list[dict[str, Any]] = []
    tool_index = 0

    for rec in records:
        ts = str(rec.get("ts") or "")
        blocks = rec.get("blocks")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            tool_index += 1
            tool_name = str(block.get("name") or "")
            tool_input = block.get("input")

            if tool_name == "Agent":
                dispatch_text = _tool_input_text(tool_input)
                story_scores = _dispatch_story_scores(dispatch_text, story_specs)
                dispatches.append(
                    {
                        "index": tool_index,
                        "ts": ts,
                        "text": dispatch_text,
                        "session_hints": _extract_dispatch_session_hints(dispatch_text),
                        "story_sections": _extract_dispatch_story_sections(dispatch_text, story_specs),
                        "story_scores": story_scores,
                        "matched_story_id": _pick_best_story_from_scores(story_scores, minimum_score=3),
                    }
                )
                continue

            if tool_name != "Bash" or not isinstance(tool_input, dict):
                continue

            command = str(tool_input.get("command") or "")
            if "agent-browser" not in command:
                continue

            total_browser_calls += 1
            session = _extract_agent_browser_session(command)
            sessions.add(session)
            verb_counts[_extract_agent_browser_verb(command)] += 1
            browser_calls.append(
                {
                    "index": tool_index,
                    "browser_call_index": len(browser_calls),
                    "ts": ts,
                    "session": session,
                    "command": command,
                }
            )

    for browser_call in browser_calls:
        story_id = _browser_call_story_id(
            browser_call=browser_call,
            dispatches=dispatches,
            story_specs=story_specs,
        )
        if story_id:
            direct_counts[story_id] += 1
            story_sessions[story_id].add(str(browser_call["session"]))
        else:
            shared_count += 1

    calls_per_story = _story_bucket_counts(
        direct_counts=direct_counts,
        shared_count=shared_count,
        story_ids=story_ids,
    )
    isolated_story_count = sum(
        1
        for session_names in story_sessions.values()
        if any(name not in {"default", "main", "anonymous"} for name in session_names)
    )
    outlier, outlier_reason = browser_efficiency_outlier(
        certifier_mode=certifier_mode,
        total_browser_calls=total_browser_calls,
        distinct_sessions=len(sessions),
        story_count=len(story_ids),
        isolated_story_count=isolated_story_count,
    )
    return {
        "total_browser_calls": total_browser_calls,
        "distinct_sessions": len(sessions),
        "verb_counts": dict(sorted(verb_counts.items(), key=lambda item: (-item[1], item[0]))),
        "calls_per_story": calls_per_story,
        "outlier": outlier,
        "outlier_reason": outlier_reason,
    }


def _summarize_round(marker_lines: list[str]) -> tuple[str, int, int]:
    """Extract (verdict, passed_count, tested_count) from marker lines."""
    verdict = "?"
    passed = 0
    tested = 0
    for line in marker_lines:
        verdict_match = _VERDICT_RE.match(line)
        if verdict_match:
            verdict = verdict_match.group(1)
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
                    return f"{parts[-1]} \"{title[:120]}\""
    return None


def _parse_story_result_marker(line: str) -> dict[str, Any] | None:
    match = _STORY_RESULT_RE.match(line)
    if match is None:
        return None
    summary, fields = _parse_story_result_fields(match.group(3))
    summary = summary or fields.get("observed_result", "") or match.group(3).strip()
    verdict = match.group(2)
    return {
        "story_id": match.group(1).strip(),
        "passed": verdict in {"PASS", "WARN"},
        "summary": summary,
    }


def _parse_diagnosis_marker(line: str) -> str:
    if not line.startswith("DIAGNOSIS:"):
        return ""
    value = line.split(":", 1)[1].strip()
    if value.lower() == "null":
        return ""
    return value


def _tool_activity_label(block: ToolUseBlock) -> str | None:
    tool_name = block.name or ""
    tool_input = block.input if isinstance(block.input, dict) else {}

    if tool_name in {"Write", "Edit", "MultiEdit"}:
        path = tool_input.get("file_path") or tool_input.get("path")
        if isinstance(path, str) and path.strip():
            verb = "writing" if tool_name == "Write" else "editing"
            return f"{verb} {path.strip()}"

    if tool_name == "Read":
        path = tool_input.get("file_path") or tool_input.get("path")
        if isinstance(path, str) and path.strip():
            return f"reading {path.strip()}"

    if tool_name == "Glob":
        pattern = tool_input.get("pattern")
        if isinstance(pattern, str) and pattern.strip():
            return f"scanning {pattern.strip()}"

    if tool_name == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            cleaned = " ".join(command.strip().split())
            cleaned = _SHELL_NOISE_SPLIT_RE.split(cleaned, maxsplit=1)[0]
            return f"running {_truncate_at_word(cleaned, 40)}"

    if tool_name == "Agent" and _looks_like_certifier_prompt(tool_input):
        return "running verifier"

    return None


def make_session_logger(
    log_dir: Path,
    *,
    phase_name: str = "BUILD",
    phase_label: str | None = None,
    stdout_callback: Callable[[str], None] | None = None,
    verbose: bool = False,
    strict_mode: bool = False,
    project_dir: Path | None = None,
    debug_unredacted: bool = False,
) -> dict[str, Any]:
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
    jsonl = JsonlMessageWriter(
        log_dir / "messages.jsonl",
        phase_name=phase_name,
        redact=True,
        emit_phase_events=True,
    )
    raw_jsonl: JsonlMessageWriter | None = None
    raw_narr: NarrativeFormatter | None = None
    if debug_unredacted:
        raw_dir = log_dir.parent / "raw"
        raw_jsonl = JsonlMessageWriter(
            raw_dir / "messages.jsonl",
            phase_name=phase_name,
            redact=False,
            emit_phase_events=True,
        )
        raw_narr = NarrativeFormatter(
            raw_dir / "narrative.log",
            phase_name=phase_name,
            phase_label=phase_label,
            verbose=verbose,
            strict_mode=strict_mode,
            project_dir=project_dir,
            redact=False,
        )
    narr = NarrativeFormatter(
        log_dir / "narrative.log",
        phase_name=phase_name,
        phase_label=phase_label,
        stdout_callback=stdout_callback,
        event_callback=jsonl.emit_event,
        verbose=verbose,
        strict_mode=strict_mode,
        project_dir=project_dir,
    )
    narr.start()
    if raw_narr is not None:
        raw_narr.start()

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
        if raw_jsonl is not None:
            raw_jsonl.write(message)
        if raw_narr is not None:
            raw_narr.write_message(message)

    def _close() -> None:
        narr.close()
        jsonl.close()
        if raw_narr is not None:
            raw_narr.close()
        if raw_jsonl is not None:
            raw_jsonl.close()

    return {
        "on_message": _on_message,
        "_close": _close,
        "_narrative": narr,
        "_jsonl": jsonl,
        "_raw_jsonl": raw_jsonl,
        "_raw_narrative": raw_narr,
    }
