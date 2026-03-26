"""Otto agent utilities — shared SDK imports, query loop, and helpers.

Centralizes Claude Agent SDK boilerplate so callers don't repeat
the try/except import dance or the streaming message loop.
"""

from __future__ import annotations

from typing import Any, Callable

# ---------------------------------------------------------------------------
# SDK imports — single import point for the entire codebase
# ---------------------------------------------------------------------------

try:
    from claude_agent_sdk import ClaudeAgentOptions, query  # noqa: F401
    from claude_agent_sdk.types import (  # noqa: F401
        AssistantMessage,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
    )
except ImportError:
    from otto._agent_stub import ClaudeAgentOptions, query, ResultMessage  # noqa: F401
    AssistantMessage = None  # type: ignore[assignment,misc]
    TextBlock = None  # type: ignore[assignment,misc]
    ToolUseBlock = None  # type: ignore[assignment,misc]
    ToolResultBlock = None  # type: ignore[assignment,misc]

# Optional SDK types (may not exist in older versions)
try:
    from claude_agent_sdk.types import ThinkingBlock  # noqa: F401
except (ImportError, AttributeError):
    ThinkingBlock = None  # type: ignore[assignment,misc]

try:
    from claude_agent_sdk.types import AgentDefinition  # noqa: F401
except (ImportError, AttributeError):
    AgentDefinition = None  # type: ignore[assignment,misc]

# Re-export _subprocess_env so callers can import from one place.
# The canonical implementation lives in verify.py (used there for non-agent
# subprocess calls too).
from otto.verify import _subprocess_env  # noqa: F401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def tool_use_summary(block) -> str:
    """One-line summary of a tool use block for logging.

    Extracts the most useful identifier from each tool type:
    Read/Glob/Grep → file path or pattern, Edit/Write → file path,
    Bash → first 120 chars of command.
    """
    inputs = block.input or {}
    name = block.name
    if name in ("Read", "Glob", "Grep"):
        return inputs.get("file_path") or inputs.get("path") or inputs.get("pattern") or ""
    elif name in ("Edit", "Write"):
        return inputs.get("file_path") or ""
    elif name == "Bash":
        cmd = inputs.get("command") or ""
        if len(cmd) <= 120:
            return cmd
        cut = cmd.rfind(" ", 0, 120)
        if cut <= 0:
            cut = 120
        return cmd[:cut] + "..."
    return ""


# ---------------------------------------------------------------------------
# Shared query loop
# ---------------------------------------------------------------------------


async def run_agent_query(
    prompt: str,
    options: ClaudeAgentOptions,
    *,
    on_text: Callable[[str], Any] | None = None,
    on_tool: Callable[[Any], Any] | None = None,
    on_tool_result: Callable[[Any], Any] | None = None,
    on_result: Callable[[Any], Any] | None = None,
) -> tuple[str, float, Any]:
    """Run an Agent SDK query, dispatching events to optional callbacks.

    Handles the streaming message loop that every caller repeats:
    type-checking for ResultMessage vs AssistantMessage, iterating
    content blocks, extracting cost.

    Callbacks:
        on_text(text)           — called for each TextBlock
        on_tool(tool_use_block) — called for each ToolUseBlock
        on_tool_result(block)   — called for each ToolResultBlock
        on_result(result_msg)   — called once when ResultMessage arrives

    Returns (collected_text, cost_usd, result_message).
        collected_text: all TextBlock content concatenated (no separator)
        cost_usd: extracted from ResultMessage.total_cost_usd
        result_message: the raw ResultMessage (or duck-typed equivalent), or None
    """
    text_parts: list[str] = []
    cost = 0.0
    result_msg = None

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result_msg = message
            raw_cost = getattr(message, "total_cost_usd", None)
            if isinstance(raw_cost, (int, float)):
                cost = float(raw_cost)
            if on_result:
                on_result(message)
        elif hasattr(message, "session_id") and hasattr(message, "is_error"):
            # Duck-typed ResultMessage (SDK version compat)
            result_msg = message
            raw_cost = getattr(message, "total_cost_usd", None)
            if isinstance(raw_cost, (int, float)):
                cost = float(raw_cost)
            if on_result:
                on_result(message)
        elif AssistantMessage and isinstance(message, AssistantMessage):
            for block in message.content:
                if ToolResultBlock and isinstance(block, ToolResultBlock):
                    if on_tool_result:
                        on_tool_result(block)
                elif ThinkingBlock and isinstance(block, ThinkingBlock):
                    # Thinking blocks are logged but not collected as text
                    thinking = getattr(block, "thinking", "")
                    if thinking and on_text:
                        on_text(f"[thinking] {thinking}")
                elif TextBlock and isinstance(block, TextBlock) and block.text:
                    text_parts.append(block.text)
                    if on_text:
                        on_text(block.text)
                elif ToolUseBlock and isinstance(block, ToolUseBlock):
                    if on_tool:
                        on_tool(block)

    return "".join(text_parts), cost, result_msg
