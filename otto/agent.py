"""Otto agent utilities — provider abstraction, event normalization, helpers."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    from claude_agent_sdk import ClaudeAgentOptions as _SDKClaudeAgentOptions
    from claude_agent_sdk import query as _sdk_query
    from claude_agent_sdk.types import AssistantMessage as _SDKAssistantMessage
    from claude_agent_sdk.types import ResultMessage as _SDKResultMessage
    from claude_agent_sdk.types import TextBlock as _SDKTextBlock
    from claude_agent_sdk.types import ToolResultBlock as _SDKToolResultBlock
    from claude_agent_sdk.types import ToolUseBlock as _SDKToolUseBlock
except ImportError:
    _SDKClaudeAgentOptions = None
    _sdk_query = None
    _SDKAssistantMessage = None
    _SDKResultMessage = None
    _SDKTextBlock = None
    _SDKToolResultBlock = None
    _SDKToolUseBlock = None

try:
    from claude_agent_sdk.types import ThinkingBlock as _SDKThinkingBlock
except (ImportError, AttributeError):
    _SDKThinkingBlock = None

try:
    from claude_agent_sdk.types import AgentDefinition  # noqa: F401
except (ImportError, AttributeError):
    AgentDefinition = None  # type: ignore[assignment,misc]

from otto.testing import _subprocess_env  # noqa: F401


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


@dataclass
class ResultMessage:
    subtype: str = "success"
    is_error: bool = False
    session_id: str = ""
    result: str | None = None
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None


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


# Backward-compatible name used throughout the codebase and tests.
ClaudeAgentOptions = AgentOptions


def normalize_usage(usage: Any) -> dict[str, int]:
    """Normalize provider usage payloads into integer token counters."""
    if not isinstance(usage, dict):
        return {}
    normalized: dict[str, int] = {}
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, int):
            normalized[key] = value
        elif isinstance(value, float):
            normalized[key] = int(value)
    return normalized


def merge_usage(*usages: dict[str, int] | None) -> dict[str, int]:
    """Sum usage dicts by key."""
    merged: dict[str, int] = {}
    for usage in usages:
        if not usage:
            continue
        for key, value in usage.items():
            merged[key] = merged.get(key, 0) + int(value)
    return merged


def _provider_name(options: AgentOptions | None) -> str:
    provider = (getattr(options, "provider", None) or "claude").strip().lower()
    if provider not in {"claude", "codex"}:
        raise ValueError(f"Unsupported agent provider: {provider}")
    return provider


def _safe_read(path: Path, max_chars: int = 40_000) -> str | None:
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    return text[:max_chars]


def _codex_compat_prelude(options: AgentOptions) -> str:
    """Map CLAUDE.md-style settings into Codex prompts.

    Claude Code natively loads CLAUDE.md via setting_sources. Codex does not,
    so preserve Otto's current repo/user instruction behavior by prepending the
    requested files to the prompt when running through the Codex CLI.
    """
    blocks: list[str] = []
    sources = set(options.setting_sources or [])
    cwd = Path(options.cwd or os.getcwd())

    if "project" in sources:
        project_claude = _safe_read(cwd / "CLAUDE.md")
        if project_claude:
            blocks.append(
                "Project instructions from CLAUDE.md:\n"
                f"{project_claude}"
            )

    if "user" in sources:
        user_claude = _safe_read(Path.home() / ".claude" / "CLAUDE.md")
        if user_claude:
            blocks.append(
                "User instructions from ~/.claude/CLAUDE.md:\n"
                f"{user_claude}"
            )

    if not blocks:
        return ""
    return "\n\n".join(blocks).strip()


def _codex_prompt(prompt: str, options: AgentOptions) -> str:
    parts: list[str] = []
    if isinstance(options.system_prompt, str) and options.system_prompt.strip():
        parts.append(options.system_prompt.strip())
    compat = _codex_compat_prelude(options)
    if compat:
        parts.append(compat)
    parts.append(prompt)
    return "\n\n".join(part for part in parts if part).strip()


def _sdk_options(options: AgentOptions | None) -> Any:
    if _SDKClaudeAgentOptions is None:
        return options
    opts = options or AgentOptions()
    return _SDKClaudeAgentOptions(
        permission_mode=opts.permission_mode,
        cwd=opts.cwd,
        model=opts.model,
        resume=opts.resume,
        max_turns=opts.max_turns,
        system_prompt=opts.system_prompt,
        mcp_servers=opts.mcp_servers,
        env=opts.env,
        setting_sources=opts.setting_sources,
        effort=opts.effort,
        agents=opts.agents,
        max_buffer_size=opts.max_buffer_size,
    )


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
    if hasattr(block, "content"):
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
        )
    if hasattr(message, "session_id") and hasattr(message, "is_error"):
        return ResultMessage(
            subtype=str(getattr(message, "subtype", "success") or "success"),
            is_error=bool(getattr(message, "is_error", False)),
            session_id=str(getattr(message, "session_id", "") or ""),
            result=getattr(message, "result", None),
            total_cost_usd=getattr(message, "total_cost_usd", None),
            usage=getattr(message, "usage", None),
        )
    if isinstance(message, AssistantMessage):
        return message
    if (_SDKAssistantMessage and isinstance(message, _SDKAssistantMessage)) or hasattr(message, "content"):
        content = []
        for block in getattr(message, "content", []) or []:
            normalized = _normalize_block(block)
            if normalized is not None:
                content.append(normalized)
        return AssistantMessage(content=content)
    return None


async def _query_claude(*, prompt: str, options: AgentOptions | None = None):
    if _sdk_query is None:
        yield ResultMessage()
        return

    async for message in _sdk_query(prompt=prompt, options=_sdk_options(options)):
        normalized = _normalize_message(message)
        if normalized is not None:
            yield normalized


def _codex_command(options: AgentOptions) -> list[str]:
    command = ["codex", "exec"]
    if options.resume:
        command.extend(["resume", "--json"])
    else:
        command.extend(["--json"])
    if options.permission_mode == "bypassPermissions":
        command.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        command.append("--full-auto")
    if options.model:
        command.extend(["-m", options.model])
    if options.cwd and not options.resume:
        command.extend(["-C", options.cwd])
    if options.resume:
        command.append(options.resume)
    command.append("-")
    return command


async def _query_codex(*, prompt: str, options: AgentOptions | None = None):
    opts = options or AgentOptions()
    env = dict(os.environ)
    if opts.env:
        env.update(opts.env)

    process = await asyncio.create_subprocess_exec(
        *_codex_command(opts),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=opts.cwd or None,
        env=env,
    )

    final_prompt = _codex_prompt(prompt, opts)
    stdout = process.stdout
    stdin = process.stdin
    assert stdout is not None
    assert stdin is not None

    stdin.write(final_prompt.encode("utf-8"))
    await stdin.drain()
    stdin.close()

    session_id = ""
    last_text = ""
    saw_result = False
    raw_lines: list[str] = []

    while True:
        raw_line = await stdout.readline()
        if not raw_line:
            break
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        raw_lines.append(line)
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        if event_type == "thread.started":
            session_id = str(event.get("thread_id", "") or "")
            continue

        item = event.get("item") or {}
        item_type = item.get("type")
        if item_type == "agent_message" and event_type == "item.completed":
            text = str(item.get("text", "") or "")
            if text:
                last_text = text
                yield AssistantMessage(content=[TextBlock(text=text)])
            continue

        if item_type == "command_execution":
            item_id = str(item.get("id", "") or "") or None
            command = str(item.get("command", "") or "")
            if event_type == "item.started":
                yield AssistantMessage(content=[ToolUseBlock(name="Bash", input={"command": command}, id=item_id)])
                continue
            if event_type == "item.completed":
                output = str(item.get("aggregated_output", "") or "")
                yield AssistantMessage(content=[ToolResultBlock(content=output, tool_use_id=item_id)])
                continue

        if event_type == "turn.completed":
            saw_result = True
            yield ResultMessage(
                subtype="success",
                is_error=False,
                session_id=session_id,
                result=last_text or None,
                total_cost_usd=0.0,
                usage=event.get("usage"),
            )

    return_code = await process.wait()
    if not saw_result or return_code != 0:
        error_lines = raw_lines[-20:]
        error_text = "\n".join(error_lines) or f"codex exited with code {return_code}"
        yield ResultMessage(
            subtype="error",
            is_error=True,
            session_id=session_id,
            result=error_text,
            total_cost_usd=0.0,
            usage=None,
        )


async def query(*, prompt: str, options: AgentOptions | None = None):
    """Run an agent query against the configured provider."""
    provider = _provider_name(options)
    if provider == "codex":
        async for message in _query_codex(prompt=prompt, options=options):
            yield message
        return

    async for message in _query_claude(prompt=prompt, options=options):
        yield message


def tool_use_summary(block) -> str:
    """One-line summary of a tool use block for logging."""
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

    inputs = block.input or {}
    name = block.name
    if name in ("Read", "Glob", "Grep"):
        return inputs.get("file_path") or inputs.get("path") or inputs.get("pattern") or ""
    if name in ("Edit", "Write"):
        return inputs.get("file_path") or ""
    if name == "Bash":
        cmd = _unwrap_shell_command(inputs.get("command") or "")
        if len(cmd) <= 120:
            return cmd
        cut = cmd.rfind(" ", 0, 120)
        if cut <= 0:
            cut = 120
        return cmd[:cut] + "..."
    return ""


async def run_agent_query(
    prompt: str,
    options: ClaudeAgentOptions,
    *,
    on_text: Callable[[str], Any] | None = None,
    on_tool: Callable[[Any], Any] | None = None,
    on_tool_result: Callable[[Any], Any] | None = None,
    on_result: Callable[[Any], Any] | None = None,
) -> tuple[str, float, Any]:
    """Run a provider query, dispatching normalized events to callbacks."""
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
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    if on_tool_result:
                        on_tool_result(block)
                elif isinstance(block, ThinkingBlock):
                    thinking = getattr(block, "thinking", "")
                    if thinking and on_text:
                        on_text(f"[thinking] {thinking}")
                elif isinstance(block, TextBlock) and block.text:
                    text_parts.append(block.text)
                    if on_text:
                        on_text(block.text)
                elif isinstance(block, ToolUseBlock):
                    if on_tool:
                        on_tool(block)

    return "".join(text_parts), cost, result_msg
