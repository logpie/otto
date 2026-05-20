"""SDK event parsing, normalization, and narrative formatting helpers.

This module owns provider-agnostic dataclasses (TextBlock/ToolUseBlock/...),
SDK-message normalization, the transcript accumulator that collects marker
lines, and one-line tool-use summaries used by the narrative log.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    from claude_agent_sdk.types import AssistantMessage as _SDKAssistantMessage
    from claude_agent_sdk.types import ResultMessage as _SDKResultMessage
    from claude_agent_sdk.types import TextBlock as _SDKTextBlock
    from claude_agent_sdk.types import ToolResultBlock as _SDKToolResultBlock
    from claude_agent_sdk.types import ToolUseBlock as _SDKToolUseBlock
except ImportError:
    _SDKAssistantMessage = None
    _SDKResultMessage = None
    _SDKTextBlock = None
    _SDKToolResultBlock = None
    _SDKToolUseBlock = None

try:
    from claude_agent_sdk.types import UserMessage as _SDKUserMessage
except (ImportError, AttributeError):
    _SDKUserMessage = None

try:
    from claude_agent_sdk.types import ThinkingBlock as _SDKThinkingBlock
except (ImportError, AttributeError):
    _SDKThinkingBlock = None


@dataclass
class TextBlock:
    text: str = ""


@dataclass
class ThinkingBlock:
    thinking: str = ""


@dataclass
class ToolUseBlock:
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    id: str | None = None


@dataclass
class ToolResultBlock:
    content: str = ""
    tool_use_id: str | None = None
    is_error: bool = False


@dataclass
class AssistantMessage:
    content: list[Any] = field(default_factory=list)
    session_id: str = ""
    usage: dict[str, Any] | None = None


@dataclass
class UserMessage:
    """Tool-result-only messages returning tool outputs to the model.

    The SDK tags these as "user" because tool_result blocks are passed
    back as user input on the next turn. Kept separate from
    AssistantMessage so messages.jsonl can record them with the correct
    ``type: "user"`` tag.
    """
    content: list[Any] = field(default_factory=list)
    session_id: str = ""
    usage: dict[str, Any] | None = None


@dataclass
class ResultMessage:
    subtype: str = "success"
    is_error: bool = False
    session_id: str = ""
    result: str | None = None
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    structured_output: Any = None
    structured_output_error: str = ""


@dataclass
class ProviderEventMessage:
    """Durable provider-side state that is not assistant prose or tool IO."""

    event: str
    provider: str
    session_id: str = ""
    method: str = ""
    turn_id: str = ""
    item_id: str = ""
    status: str = ""
    usage: dict[str, Any] | None = None
    data: dict[str, Any] | None = None


@dataclass
class AgentOptions:
    permission_mode: str | None = None
    cwd: str | None = None
    model: str | None = None
    resume: str | None = None
    max_turns: int | None = None
    system_prompt: str | dict[str, Any] | None = None
    mcp_servers: dict[str, Any] | None = None
    env: dict[str, str] | None = None
    setting_sources: list[str] | None = None
    effort: str | None = None
    agents: dict[str, Any] | None = None
    max_buffer_size: int | None = None
    provider: str | None = None
    disallowed_tools: list[str] | None = None
    hooks: dict[str, list[Any]] | None = None
    can_use_tool: Callable[..., Any] | None = None
    output_format: dict[str, Any] | None = None
    max_subagent_dispatches: int | None = None
    debug_unredacted: bool | None = None


# Backward-compatible name used throughout the codebase and tests.
ClaudeAgentOptions = AgentOptions


class _TranscriptAccumulator:
    """Keep structured markers plus bounded transcript tails."""

    def __init__(self, *, keep_tool_output: bool) -> None:
        self._assistant_parts: deque[str] = deque()
        self._assistant_chars = 0
        self._assistant_limit = 32_000
        self._tool_parts: deque[str] = deque()
        self._tool_chars = 0
        self._tool_limit = 16_000
        self._keep_tool_output = keep_tool_output
        self._marker_lines: list[str] = []
        self._carry = ""

    def add_assistant_text(self, text: str) -> None:
        text_to_store = self._strip_redundant_marker_recap(text)
        self._append(
            self._assistant_parts,
            "_assistant_chars",
            self._assistant_limit,
            text_to_store,
        )
        self._collect_markers(text_to_store)

    def add_tool_output(self, text: str) -> None:
        self._collect_markers(text)
        if self._keep_tool_output:
            self._append(self._tool_parts, "_tool_chars", self._tool_limit, text)

    def finalize_text(self) -> str:
        self._flush_carry()
        parts = [*self._assistant_parts]
        assistant_text = "\n\n".join(part for part in self._assistant_parts if part)
        assistant_has_markers = any(
            line.startswith(
                (
                    "CERTIFY_ROUND:",
                    "STORIES_TESTED:",
                    "STORIES_PASSED:",
                    "STORY_RESULT:",
                    "VERDICT:",
                    "DIAGNOSIS:",
                    "METRIC_VALUE:",
                    "METRIC_MET:",
                )
            )
            for line in assistant_text.splitlines()
        )
        if self._keep_tool_output:
            retained_lines = {
                line.strip()
                for part in [*self._assistant_parts, *self._tool_parts]
                for line in part.splitlines()
                if line.strip()
            }
            missing_marker_lines = [
                line for line in self._marker_lines if line not in retained_lines
            ]
            if missing_marker_lines:
                parts.append("\n".join(missing_marker_lines))
            parts.extend(self._tool_parts)
        elif self._marker_lines and not assistant_has_markers:
            parts.append("\n".join(self._marker_lines))
        return "\n\n".join(part for part in parts if part)

    def _append(
        self,
        bucket: deque[str],
        count_attr: str,
        limit: int,
        text: str,
    ) -> None:
        if not text:
            return
        setattr(self, count_attr, getattr(self, count_attr) + len(text))
        bucket.append(text)
        while bucket and getattr(self, count_attr) > limit:
            removed = bucket.popleft()
            setattr(self, count_attr, getattr(self, count_attr) - len(removed))

    def _strip_redundant_marker_recap(self, text: str) -> str:
        """Drop duplicated marker blocks from closing recap prose.

        Improve/build runs capture certifier marker lines from subagent tool
        output so they can be parsed later. If the parent agent then echoes the
        same `CERTIFY_ROUND` block in a closing assistant summary, parsing the
        combined transcript sees `1, 2, 1, 2` and trips the non-monotonic guard
        even though the underlying certifier output was valid.

        Keep the prose, but strip contiguous recap blocks only when we've
        already seen round markers earlier in the transcript. A recap block
        starts at `CERTIFY_ROUND:` and runs until the next blank line or end of
        input, so newly added marker lines inside that block are removed
        without having to enumerate them here.
        """
        if (
            "CERTIFY_ROUND:" not in text
            or not any(line.startswith("CERTIFY_ROUND:") for line in self._marker_lines)
        ):
            return text

        filtered_lines: list[str] = []
        skipping_marker_block = False
        for line in text.splitlines():
            stripped = line.strip()
            if skipping_marker_block:
                if not stripped:
                    skipping_marker_block = False
                continue
            if stripped.startswith("CERTIFY_ROUND:"):
                skipping_marker_block = True
                continue
            filtered_lines.append(line)

        return "\n".join(filtered_lines)

    def _collect_markers(self, fragment: str) -> None:
        if not fragment:
            return
        from otto.markers import _STORY_RESULT_RE, _VERDICT_RE

        separator = "\n" if self._carry and not fragment.startswith(("\n", "\r")) else ""
        combined = self._carry + separator + fragment
        lines = combined.splitlines(keepends=True)
        self._carry = ""
        in_coverage_block = False
        for raw_line in lines:
            if not raw_line.endswith(("\n", "\r")):
                self._carry = raw_line
                continue
            stripped = raw_line.strip()
            if not stripped or stripped.startswith(">"):
                in_coverage_block = False
                continue
            if stripped.startswith(("COVERAGE_OBSERVED:", "COVERAGE_GAPS:")):
                self._marker_lines.append(stripped)
                in_coverage_block = True
                continue
            if in_coverage_block and stripped.startswith("- "):
                self._marker_lines.append(stripped)
                continue
            in_coverage_block = False
            if (
                stripped.startswith(
                    (
                        "CERTIFY_ROUND:",
                        "STORIES_TESTED:",
                        "STORIES_PASSED:",
                        "DIAGNOSIS:",
                        "METRIC_VALUE:",
                        "METRIC_MET:",
                    )
                )
                or _STORY_RESULT_RE.match(stripped)
                or _VERDICT_RE.match(stripped)
            ):
                self._marker_lines.append(stripped)

    def _flush_carry(self) -> None:
        if not self._carry:
            return
        self._collect_markers(self._carry + "\n")
        self._carry = ""


def _normalize_block(block: Any) -> Any | None:
    if isinstance(block, TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock):
        return block
    if _SDKTextBlock and isinstance(block, _SDKTextBlock):
        return TextBlock(text=getattr(block, "text", "") or "")
    if _SDKThinkingBlock and isinstance(block, _SDKThinkingBlock):
        return ThinkingBlock(thinking=getattr(block, "thinking", "") or "")
    if _SDKToolUseBlock and isinstance(block, _SDKToolUseBlock):
        return ToolUseBlock(
            name=getattr(block, "name", "") or "",
            input=dict(getattr(block, "input", None) or {}),
            id=getattr(block, "id", None),
        )
    if _SDKToolResultBlock and isinstance(block, _SDKToolResultBlock):
        return ToolResultBlock(
            content=str(getattr(block, "content", "") or ""),
            tool_use_id=getattr(block, "tool_use_id", None),
            is_error=bool(getattr(block, "is_error", False)),
        )

    if hasattr(block, "text"):
        return TextBlock(text=str(getattr(block, "text", "") or ""))
    if hasattr(block, "thinking"):
        return ThinkingBlock(thinking=str(getattr(block, "thinking", "") or ""))
    if hasattr(block, "name") and hasattr(block, "input"):
        return ToolUseBlock(
            name=str(getattr(block, "name", "") or ""),
            input=dict(getattr(block, "input", None) or {}),
            id=getattr(block, "id", None),
        )
    if hasattr(block, "content") and hasattr(block, "tool_use_id"):
        return ToolResultBlock(
            content=str(getattr(block, "content", "") or ""),
            tool_use_id=getattr(block, "tool_use_id", None),
            is_error=bool(getattr(block, "is_error", False)),
        )
    return None


def _normalize_message(message: Any) -> Any | None:
    if isinstance(message, ResultMessage):
        return message
    if _SDKResultMessage and isinstance(message, _SDKResultMessage):
        return ResultMessage(
            subtype=str(getattr(message, "subtype", "success") or "success"),
            is_error=bool(getattr(message, "is_error", False)),
            session_id=str(getattr(message, "session_id", "") or ""),
            result=getattr(message, "result", None),
            total_cost_usd=getattr(message, "total_cost_usd", None),
            usage=getattr(message, "usage", None),
            structured_output=getattr(message, "structured_output", None),
        )
    if isinstance(message, UserMessage):
        return message
    if isinstance(message, AssistantMessage):
        return message

    session_id = str(getattr(message, "session_id", "") or "")

    # SDK UserMessage — tool_result-only payload returned to the model.
    if _SDKUserMessage and isinstance(message, _SDKUserMessage):
        content = []
        raw_content = getattr(message, "content", []) or []
        # SDK UserMessage.content may be a bare string — wrap as TextBlock.
        if isinstance(raw_content, str):
            if raw_content:
                content.append(TextBlock(text=raw_content))
        else:
            for block in raw_content:
                normalized = _normalize_block(block)
                if normalized is not None:
                    content.append(normalized)
        return UserMessage(
            content=content,
            session_id=session_id,
            usage=getattr(message, "usage", None),
        )

    if (_SDKAssistantMessage and isinstance(message, _SDKAssistantMessage)) or hasattr(message, "content"):
        content = []
        raw_content = getattr(message, "content", []) or []
        if isinstance(raw_content, str):
            if raw_content:
                content.append(TextBlock(text=raw_content))
        else:
            for block in raw_content:
                normalized = _normalize_block(block)
                if normalized is not None:
                    content.append(normalized)
        # If the message contains ONLY tool_result blocks (no text /
        # thinking / tool_use), it is semantically a user turn — tool
        # outputs fed back into the model. Tag as UserMessage so
        # messages.jsonl records type="user" correctly.
        if content and all(isinstance(b, ToolResultBlock) for b in content):
            return UserMessage(
                content=content,
                session_id=session_id,
                usage=getattr(message, "usage", None),
            )
        return AssistantMessage(
            content=content,
            session_id=session_id,
            usage=getattr(message, "usage", None),
        )
    return None


def _usage_total_cost_usd(message: Any) -> float | None:
    usage = getattr(message, "usage", None)
    if isinstance(usage, dict):
        raw = usage.get("total_cost_usd")
        if isinstance(raw, (int, float)):
            return float(raw)
    raw = getattr(usage, "total_cost_usd", None)
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def _raw_value(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        try:
            return _json_safe(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return _json_safe(vars(value))
    return str(value)


def _structured_output_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    safe = _json_safe(value)
    if isinstance(safe, str):
        return safe
    return json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True)


def _truncate_for_agent_log(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit].rstrip() + f"\n[... Otto truncated {omitted} chars ...]"


def tool_use_summary(block) -> str:
    """One-line summary of a tool use block for logging."""
    import re

    def _unwrap_shell_command(cmd: str) -> str:
        patterns = [
            r"^/bin/(?:zsh|bash|sh)\s+-lc\s+'(?P<body>.*)'$",
            r'^/bin/(?:zsh|bash|sh)\s+-lc\s+"(?P<body>.*)"$',
        ]
        for pattern in patterns:
            match = re.match(pattern, cmd, re.DOTALL)
            if match:
                return match.group("body")
        return cmd

    def _collapse(s: str) -> str:
        # Collapse embedded newlines (e.g. HEREDOC bodies) so the
        # narrative's single-line writer doesn't get multi-row output.
        return s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")

    inputs = block.input or {}
    name = block.name
    if name == "Read":
        return _collapse(inputs.get("file_path", ""))
    if name == "Glob":
        return _collapse(inputs.get("pattern") or inputs.get("path", ""))
    if name == "Grep":
        return _collapse(inputs.get("pattern", ""))
    if name in ("Edit", "Write"):
        return _collapse(inputs.get("file_path", ""))
    if name == "Bash":
        cmd = _unwrap_shell_command(inputs.get("command", ""))
        cmd = _collapse(cmd)
        if len(cmd) <= 120:
            return cmd
        cut = cmd.rfind(" ", 0, 120)
        if cut <= 0:
            cut = 120
        return cmd[:cut] + "..."
    if name == "Agent":
        subagent_type = str(inputs.get("subagent_type", "") or "").strip()
        prompt = _collapse(str(inputs.get("prompt", "") or "")).strip()
        preview = prompt[:80]
        if len(prompt) > 80:
            preview = preview.rstrip() + "..."
        if subagent_type:
            return f'subagent={subagent_type} "{preview}"'
        return f'"{preview}"' if preview else ""
    return ""
