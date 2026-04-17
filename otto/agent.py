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
    structured_output: Any = None


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
    output_format: dict[str, Any] | None = None


# Backward-compatible name used throughout the codebase and tests.
ClaudeAgentOptions = AgentOptions


def make_agent_options(
    project_dir: Path,
    config: dict[str, Any] | None = None,
    **overrides: Any,
) -> AgentOptions:
    """Create standard agent options for build/certify agents.

    Sets bypassPermissions, project cwd, CC preset prompt, and model from config.
    Pass keyword overrides for system_prompt, setting_sources, etc.
    """
    opts = AgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        system_prompt={"type": "preset", "preset": "claude_code"},
        env=_subprocess_env(),
        setting_sources=["project"],
        **overrides,
    )
    model = (config or {}).get("model")
    if model:
        opts.model = str(model)
    return opts


class AgentCallError(Exception):
    """Raised when an agent call fails (timeout or crash)."""
    def __init__(self, reason: str, text: str = "", cost: float = 0.0):
        self.reason = reason
        self.text = text
        self.cost = cost
        super().__init__(reason)


async def run_agent_with_timeout(
    prompt: str,
    options: AgentOptions,
    *,
    log_path: Path,
    timeout: int,
    project_dir: Path,
    capture_tool_output: bool = False,
) -> tuple[str, float]:
    """Run an agent query with live logging, timeout, and orphan cleanup.

    Returns (text, cost) on success. Raises AgentCallError on timeout/crash.
    Always closes the live logger and cleans up orphan processes on failure.
    """
    import asyncio as _asyncio
    import logging as _logging

    _log = _logging.getLogger("otto.agent")
    callbacks = make_live_logger(log_path)
    _close = callbacks.pop("_close")
    try:
        text, cost, _ = await _asyncio.wait_for(
            run_agent_query(prompt, options,
                            capture_tool_output=capture_tool_output,
                            **callbacks),
            timeout=timeout,
        )
        return text, cost
    except _asyncio.TimeoutError:
        _log.error("Agent timed out after %ds", timeout)
        from otto.pipeline import _cleanup_orphan_processes
        _cleanup_orphan_processes(project_dir)
        raise AgentCallError(f"Timed out after {timeout}s")
    except KeyboardInterrupt:
        from otto.pipeline import _cleanup_orphan_processes
        _cleanup_orphan_processes(project_dir)
        raise
    except Exception as exc:
        _log.exception("Agent crashed")
        from otto.pipeline import _cleanup_orphan_processes
        _cleanup_orphan_processes(project_dir)
        raise AgentCallError(f"Agent crashed: {exc}")
    finally:
        _close()


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
        disallowed_tools=opts.disallowed_tools or [],
        output_format=opts.output_format,
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

    try:
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
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


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


def make_live_logger(log_path: Path) -> dict[str, Callable]:
    """Create callback functions that write agent activity to a live log file.

    Returns a dict with on_text, on_tool, on_tool_result keys suitable for
    passing to run_agent_query as **kwargs.

    The log is append-mode and flushed after each write, so it can be
    tailed in real time: tail -f otto_logs/certifier/live.log
    """
    import time as _time
    _start = _time.monotonic()
    _fh = open(log_path, "a")

    def _elapsed() -> str:
        secs = _time.monotonic() - _start
        return f"[{secs:6.1f}s]"

    def _on_tool(block: Any) -> None:
        name = getattr(block, "name", "?")
        summary = tool_use_summary(block)
        _fh.write(f"{_elapsed()} \u25cf {name}  {summary}\n")
        _fh.flush()

    def _on_tool_result(block: Any) -> None:
        content = str(getattr(block, "content", "") or "")
        is_error = getattr(block, "is_error", False)
        prefix = "\u2717 error" if is_error else "\u2190 result"
        # Truncate to first meaningful line
        first_line = content.split("\n")[0][:200] if content else "(empty)"
        _fh.write(f"{_elapsed()} {prefix}: {first_line}\n")
        _fh.flush()

    def _on_text(text: str) -> None:
        # Only log thinking blocks and short text (skip large agent output)
        if text.startswith("[thinking]"):
            _fh.write(f"{_elapsed()} \u2192 {text[:150]}\n")
            _fh.flush()

    def _close() -> None:
        _fh.close()

    return {
        "on_tool": _on_tool,
        "on_tool_result": _on_tool_result,
        "on_text": _on_text,
        "_close": _close,
    }


async def run_agent_query(
    prompt: str,
    options: ClaudeAgentOptions,
    *,
    on_text: Callable[[str], Any] | None = None,
    on_tool: Callable[[Any], Any] | None = None,
    on_tool_result: Callable[[Any], Any] | None = None,
    on_result: Callable[[Any], Any] | None = None,
    capture_tool_output: bool = False,
) -> tuple[str, float, Any]:
    """Run a provider query, dispatching normalized events to callbacks.

    If capture_tool_output=True, tool result content (including subagent output)
    is appended to the returned text. This is useful when the caller needs to
    parse structured markers from subagent output.
    """
    text_parts: list[str] = []
    cost = 0.0
    result_msg = None

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result_msg = message
            raw_cost = getattr(message, "total_cost_usd", None)
            if isinstance(raw_cost, (int, float)):
                cost += float(raw_cost)
            if on_result:
                on_result(message)
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    if capture_tool_output and block.content:
                        text_parts.append(block.content)
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
