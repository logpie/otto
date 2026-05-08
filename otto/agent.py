"""Otto agent utilities — provider abstraction, event normalization, helpers."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shlex
import signal
import traceback
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from otto.observability import iso_timestamp, write_crash_artifact
from otto.token_usage import TOKEN_USAGE_KEYS
_SDK_IMPORT_ERROR_MESSAGE = ""
_OPENAI_AGENTS_IMPORT_ERROR_MESSAGE = ""

CODEX_STDIO_LIMIT_BYTES = 16 * 1024 * 1024
CODEX_POST_RESULT_EXIT_GRACE_S = 0.25
CODEX_TOOL_OUTPUT_LOG_LIMIT_CHARS = 20_000
CODEX_PROVIDER_ERROR_OUTPUT_LIMIT_CHARS = 1_200
_CLAUDE_ENV_LOCK = asyncio.Lock()
DEFAULT_DISALLOWED_BASH_TOOLS = (
    "Bash(killall*)",
    "Bash(killall:*)",
    "Bash(pkill*)",
    "Bash(pkill:*)",
)

try:
    from claude_agent_sdk import ClaudeSDKClient as _SDKClaudeSDKClient
    from claude_agent_sdk import ClaudeAgentOptions as _SDKClaudeAgentOptions
    from claude_agent_sdk import query as _sdk_query
    from claude_agent_sdk.types import AssistantMessage as _SDKAssistantMessage
    from claude_agent_sdk.types import ResultMessage as _SDKResultMessage
    from claude_agent_sdk.types import TextBlock as _SDKTextBlock
    from claude_agent_sdk.types import HookMatcher as _SDKHookMatcher
    from claude_agent_sdk.types import PermissionResultAllow as _SDKPermissionResultAllow
    from claude_agent_sdk.types import PermissionResultDeny as _SDKPermissionResultDeny
    from claude_agent_sdk.types import ToolResultBlock as _SDKToolResultBlock
    from claude_agent_sdk.types import ToolUseBlock as _SDKToolUseBlock
except ImportError:
    import sys

    _SDK_IMPORT_ERROR_MESSAGE = str(sys.exc_info()[1] or "")
    _SDKClaudeSDKClient = None
    _SDKClaudeAgentOptions = None
    _sdk_query = None
    _SDKAssistantMessage = None
    _SDKResultMessage = None
    _SDKTextBlock = None
    _SDKHookMatcher = None
    _SDKPermissionResultAllow = None
    _SDKPermissionResultDeny = None
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

try:
    from agents import Agent as _OpenAIAgent
    from agents import AgentOutputSchema as _OpenAIAgentOutputSchema
    from agents import ModelSettings as _OpenAIModelSettings
    from agents import RunConfig as _OpenAIRunConfig
    from agents import Runner as _OpenAIRunner
    from agents import set_default_openai_key as _OpenAISetDefaultOpenAIKey
    from agents.sandbox import Manifest as _OpenAIManifest
    from agents.sandbox import SandboxAgent as _OpenAISandboxAgent
    from agents.sandbox import SandboxRunConfig as _OpenAISandboxRunConfig
    from agents.sandbox.capabilities import Compaction as _OpenAICompactionCapability
    from agents.sandbox.capabilities import Filesystem as _OpenAIFilesystemCapability
    from agents.sandbox.capabilities import Shell as _OpenAIShellCapability
    from agents.sandbox.capabilities.tools import ExecCommandTool as _OpenAIExecCommandTool
    from agents.sandbox.sandboxes import UnixLocalSandboxClient as _OpenAIUnixLocalSandboxClient
except ImportError:
    import sys

    _OPENAI_AGENTS_IMPORT_ERROR_MESSAGE = str(sys.exc_info()[1] or "")
    _OpenAIAgent = None
    _OpenAIAgentOutputSchema = None
    _OpenAIModelSettings = None
    _OpenAIRunConfig = None
    _OpenAIRunner = None
    _OpenAISetDefaultOpenAIKey = None
    _OpenAIManifest = None
    _OpenAISandboxAgent = None
    _OpenAISandboxRunConfig = None
    _OpenAICompactionCapability = None
    _OpenAIFilesystemCapability = None
    _OpenAIShellCapability = None
    _OpenAIExecCommandTool = None
    _OpenAIUnixLocalSandboxClient = None


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


def make_agent_options(
    project_dir: Path,
    config: dict[str, Any] | None = None,
    *,
    agent_type: str | None = None,
    **overrides: Any,
) -> AgentOptions:
    """Create standard agent options for a named otto agent.

    ``agent_type`` is one of ``"build" | "certifier" | "spec" | "fix"``.
    Per-agent provider/model/effort overrides (from ``otto.yaml``'s
    ``agents.<name>`` block) take precedence over the global values.
    When ``agent_type`` is ``None``, only global values are used.

    Pass keyword overrides for system_prompt, setting_sources, etc.
    """
    from otto.testing import _subprocess_env
    from otto.config import (
        agent_effort,
        agent_provider,
        effective_agent_model,
        get_max_rounds,
        get_max_turns_per_call,
    )
    opts = AgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        system_prompt={"type": "preset", "preset": "claude_code"},
        env=_subprocess_env(project_dir),
        setting_sources=["project"],
        **overrides,
    )
    cfg = config or {}
    if opts.max_turns is None:
        opts.max_turns = get_max_turns_per_call(cfg)
    if opts.max_subagent_dispatches is None:
        max_rounds = int(cfg.get("max_certify_rounds", get_max_rounds(cfg)))
        opts.max_subagent_dispatches = max(20, max_rounds * 20)
    if opts.debug_unredacted is None:
        opts.debug_unredacted = bool(cfg.get("debug_unredacted"))
    if opts.can_use_tool is None:
        opts.can_use_tool = _otto_can_use_tool_safety
    opts.disallowed_tools = _merge_disallowed_tools(opts.disallowed_tools)
    opts.provider = agent_provider(cfg, agent_type)
    model = effective_agent_model(cfg, agent_type)
    if model:
        opts.model = str(model)
    effort = agent_effort(cfg, agent_type)
    if effort:
        opts.effort = str(effort)
    return opts


def _merge_disallowed_tools(existing: list[str] | None) -> list[str]:
    merged: list[str] = []
    for item in [*(existing or []), *DEFAULT_DISALLOWED_BASH_TOOLS]:
        if item not in merged:
            merged.append(item)
    return merged


def _default_agent_hooks() -> dict[str, list[Any]]:
    if _SDKHookMatcher is not None:
        return {
            "PreToolUse": [
                _SDKHookMatcher(matcher="Bash", hooks=[_otto_pre_tool_safety_hook])
            ]
        }
    return {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [_otto_pre_tool_safety_hook],
            }
        ]
    }


async def _otto_can_use_tool_safety(
    tool_name: str,
    tool_input: dict[str, Any],
    _context: Any,
) -> Any:
    """Allow normal tools while denying process-kill commands that can escape a run."""
    if str(tool_name or "") != "Bash":
        return _permission_allow()
    reason = _unsafe_bash_command_reason(str((tool_input or {}).get("command") or ""))
    if reason:
        return _permission_deny(reason)
    return _permission_allow()


def _permission_allow() -> Any:
    if _SDKPermissionResultAllow is not None:
        return _SDKPermissionResultAllow()
    return {"behavior": "allow"}


def _permission_deny(reason: str) -> Any:
    if _SDKPermissionResultDeny is not None:
        return _SDKPermissionResultDeny(message=reason, interrupt=False)
    return {"behavior": "deny", "message": reason}


async def _otto_pre_tool_safety_hook(
    hook_input: dict[str, Any],
    _tool_use_id: str | None,
    _context: dict[str, Any],
) -> dict[str, Any]:
    """Block provider shell commands that can kill unrelated user processes."""
    if str(hook_input.get("hook_event_name") or "") != "PreToolUse":
        return {}
    if str(hook_input.get("tool_name") or "") != "Bash":
        return {}
    tool_input = hook_input.get("tool_input")
    if not isinstance(tool_input, dict):
        return {}
    reason = _unsafe_bash_command_reason(str(tool_input.get("command") or ""))
    if not reason:
        return {}
    return {
        "decision": "block",
        "reason": reason,
        "systemMessage": reason,
    }


_KILL_COMMAND_RE = re.compile(r"(?<![\w./-])(?:/bin/)?(kill|pkill|killall)(?![\w./-])")
_COMMAND_SPLIT_RE = re.compile(r"(?:&&|\|\||;|\n)")


def _unsafe_bash_command_reason(command: str) -> str:
    """Return a human-readable deny reason for process-kill commands.

    Provider agents cannot reliably know which local PIDs belong to Otto. Even
    an explicit numeric PID can target unrelated user processes discovered via
    lsof or ps, so direct shell kills are denied here. Otto-owned subprocesses
    should be stopped by Otto's managed process cleanup path instead.
    """
    text = str(command or "")
    lowered = text.lower()
    if not _KILL_COMMAND_RE.search(text):
        return ""
    if re.search(r"(?<![\w./-])(?:/bin/)?killall(?![\w./-])", lowered):
        return (
            "Otto blocked a broad killall command. Stop only the exact PID or "
            "process group started by this run."
        )
    if re.search(r"(?<![\w./-])(?:/bin/)?pkill(?![\w./-])", lowered):
        return (
            "Otto blocked a broad pkill command. Stop only the exact PID or "
            "process group started by this run."
        )
    for segment in _COMMAND_SPLIT_RE.split(text):
        reason = _unsafe_kill_segment_reason(segment)
        if reason:
            return reason
    return ""


def _unsafe_kill_segment_reason(segment: str) -> str:
    match = re.search(r"(?<![\w./-])(?:/bin/)?kill(?![\w./-])", segment)
    if not match:
        return ""
    kill_invocation = segment[match.start():].strip()
    try:
        tokens = shlex.split(kill_invocation, comments=False, posix=True)
    except ValueError:
        return "Otto blocked a malformed kill command."
    if not tokens:
        return ""
    return (
        "Otto blocked a direct kill command. Agents must stop only processes "
        "through Otto-managed cleanup, not by targeting local PIDs."
    )


class AgentCallError(Exception):
    """Raised when an agent call fails (timeout or crash).

    Carries the best-known ``session_id`` from streamed messages so callers
    can write a resumable checkpoint. Without this, a build timeout would
    blank the session_id and ``otto build --resume`` would start a fresh
    agent session instead of continuing the existing SDK conversation.
    """
    def __init__(
        self,
        reason: str,
        session_id: str = "",
        total_cost_usd: float | None = None,
        *,
        crash_path: str = "",
        traceback_text: str = "",
        last_events: list[dict[str, Any]] | None = None,
        last_provider_stderr: str = "",
    ):
        from otto.redaction import redact_text

        self.reason = redact_text(reason)
        self.session_id = session_id
        self.total_cost_usd = (
            float(total_cost_usd) if isinstance(total_cost_usd, (int, float)) else None
        )
        self.crash_path = crash_path
        self.traceback_text = traceback_text
        self.last_events = list(last_events or [])
        self.last_provider_stderr = last_provider_stderr
        self.last_activity = ""
        self.last_tool_name = ""
        self.last_tool_args_summary = ""
        self.last_story_id = ""
        self.last_operation_started_at = ""
        super().__init__(self.reason)


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


async def run_agent_with_timeout(
    prompt: str,
    options: AgentOptions,
    *,
    log_dir: Path,
    phase_name: str = "BUILD",
    phase_label: str | None = None,
    timeout: int | None,
    project_dir: Path,
    capture_tool_output: bool = False,
    on_terminal_event: Callable[[str], None] | None = None,
    on_heartbeat: Callable[[], None] | None = None,
    verbose: bool = False,
    strict_mode: bool = False,
) -> tuple[str, float, str, dict[str, Any]]:
    """Run an agent query with streaming session logs, timeout, and orphan cleanup.

    Returns (text, cost, session_id, breakdown_data) on success.
    Raises AgentCallError on timeout/crash.
    Always closes the session loggers and cleans up orphan processes on failure.

    Writes ``log_dir/messages.jsonl`` (lossless normalized SDK event stream)
    and ``log_dir/narrative.log`` (human-readable stream). A ``live.log``
    symlink -> ``narrative.log`` is also created for back-compat.
    """
    import asyncio
    import logging

    from otto.logstream import estimate_phase_costs, make_session_logger

    log = logging.getLogger("otto.agent")
    callbacks = make_session_logger(
        log_dir,
        phase_name=phase_name,
        phase_label=phase_label,
        stdout_callback=on_terminal_event,
        verbose=verbose,
        strict_mode=strict_mode,
        project_dir=project_dir,
        debug_unredacted=bool(getattr(options, "debug_unredacted", False) or False),
    )
    close_fh = callbacks.pop("_close")
    narrative = callbacks.pop("_narrative")
    jsonl_writer = callbacks.pop("_jsonl")
    # Mutable bag — streaming handlers update it so timeout/crash paths can
    # recover the last-known session_id for a resumable checkpoint.
    agent_state: dict[str, Any] = {
        "session_id": "",
        "child_session_ids": [],
        "total_cost_usd": None,
        "provider_stderr": "",
        "log_dir": str(log_dir),
    }

    def _append_narrative(line: str) -> None:
        """Append a terminal-error marker to narrative.log for human debugging."""
        from otto.redaction import redact_text

        try:
            with open(log_dir / "narrative.log", "a", encoding="utf-8") as fh:
                fh.write(redact_text(line) + "\n")
        except OSError:
            pass

    def _fmt_elapsed(elapsed_s: float) -> str:
        secs = max(0, int(elapsed_s))
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            mins, rem = divmod(secs, 60)
            return f"{mins}m {rem:02d}s"
        hours, rem = divmod(secs, 3600)
        mins, seconds = divmod(rem, 60)
        if mins:
            return f"{hours}h {mins:02d}m {seconds:02d}s"
        return f"{hours}h 00m {seconds:02d}s"

    def _write_crash_details(
        exc: BaseException,
        *,
        traceback_text: str = "",
    ) -> str:
        session_dir = log_dir.parent
        payload = {
            "occurred_at": iso_timestamp(),
            "phase": (phase_name or "").strip().lower() or "build",
            "exception_class": exc.__class__.__name__,
            "exception_message": str(exc),
            "traceback": traceback_text,
            "provider": _provider_name(options),
            "model": getattr(options, "model", None) or "",
            "agent_session_id": agent_state.get("session_id", ""),
            "last_n_events": jsonl_writer.last_records(20),
            "last_provider_stderr": agent_state.get("provider_stderr", "") or "",
        }
        crash_path = write_crash_artifact(session_dir, payload)
        _append_narrative(f"crash details: {crash_path}")
        return str(crash_path)

    def _attach_structured_result(
        breakdown_data: dict[str, Any],
        result_msg: ResultMessage | None,
    ) -> None:
        if result_msg is None:
            return
        structured_output = getattr(result_msg, "structured_output", None)
        if structured_output is not None:
            breakdown_data["structured_output"] = structured_output
        structured_error = getattr(result_msg, "structured_output_error", "") or ""
        if structured_error:
            breakdown_data["structured_output_error"] = structured_error

    def _persist_provider_artifacts(breakdown_data: dict[str, Any]) -> None:
        diff_text = agent_state.get("codex_app_server_diff")
        if not isinstance(diff_text, str) or not diff_text.strip():
            return
        try:
            diff_path = log_dir / "codex-app-server-diff.patch"
            diff_path.write_text(diff_text, encoding="utf-8")
        except OSError:
            return
        breakdown_data["provider_diff_path"] = str(diff_path)
        breakdown_data["provider_diff_summary"] = _codex_app_server_diff_summary(diff_text)

    heartbeat_task: asyncio.Task[None] | None = None
    heartbeat_error: BaseException | None = None
    if on_terminal_event is not None or on_heartbeat is not None:
        from otto.runs.registry import HEARTBEAT_INTERVAL_S

        async def _heartbeat(agent_task: asyncio.Task[tuple[str, float, ResultMessage]]) -> None:
            nonlocal heartbeat_error
            interval_s = HEARTBEAT_INTERVAL_S
            terminal_interval_s = 20.0
            last_terminal_heartbeat_at = narrative.last_terminal_event_monotonic()
            while True:
                await asyncio.sleep(interval_s)
                try:
                    if on_heartbeat is not None:
                        on_heartbeat()
                    if on_terminal_event is None:
                        continue
                    now = asyncio.get_running_loop().time()
                    if (now - narrative.last_terminal_event_monotonic()) < terminal_interval_s:
                        continue
                    if (now - last_terminal_heartbeat_at) < terminal_interval_s:
                        continue
                    narrative.write_heartbeat(_fmt_elapsed(narrative.phase_elapsed_seconds()))
                    last_terminal_heartbeat_at = narrative.last_terminal_event_monotonic()
                except BaseException as exc:  # pragma: no cover - exercised via agent_task cancellation path
                    heartbeat_error = exc
                    agent_task.cancel()
                    return

    try:
        agent_task = asyncio.create_task(
            run_agent_query(
                prompt,
                options,
                capture_tool_output=capture_tool_output,
                state=agent_state,
                **callbacks,
            )
        )
        if on_terminal_event is not None or on_heartbeat is not None:
            heartbeat_task = asyncio.create_task(_heartbeat(agent_task))
        try:
            text, cost, result_msg = await asyncio.wait_for(agent_task, timeout=timeout)
        except asyncio.CancelledError:
            if heartbeat_error is not None:
                raise heartbeat_error
            raise
        session_id = getattr(result_msg, "session_id", "") or agent_state.get("session_id", "")
        if getattr(result_msg, "is_error", False) is True:
            reason = getattr(result_msg, "result", None) or "agent returned an error result"
            if "max_turn" in str(reason).lower() or "max turn" in str(reason).lower():
                reason = "max_turns cap reached; raise --max-turns or check for agent loops"
            breakdown_data = {
                "round_timings": narrative.round_timings(),
                "build_duration_s": narrative.build_duration_or_none(),
                "recovered_tool_errors": 0,
                "child_session_ids": [],
                "last_activity": narrative.latest_activity(),
                "last_tool_name": narrative.latest_tool_name(),
                "last_tool_args_summary": narrative.latest_tool_args_summary(),
                "last_story_id": narrative.current_story_id(),
                "last_operation_started_at": narrative.last_operation_started_at(),
                "subagent_errors": [
                    item for item in jsonl_writer.last_records(40)
                    if item.get("type") == "subagent_error"
                ],
            }
            finalize_stats = narrative.finalize(None)
            breakdown_data["recovered_tool_errors"] = int(
                finalize_stats.get("recovered_tool_errors", 0)
            )
            _attach_structured_result(breakdown_data, result_msg)
            _persist_provider_artifacts(breakdown_data)
            err = AgentCallError(
                str(reason),
                session_id=session_id,
                total_cost_usd=float(cost or 0.0),
            )
            err.crash_path = _write_crash_details(err)
            err.last_events = jsonl_writer.last_records(20)
            err.last_provider_stderr = agent_state.get("provider_stderr", "") or ""
            raise err
        child_session_ids = [
            sid for sid in agent_state.get("child_session_ids", []) or []
            if sid and sid != session_id
        ]
        breakdown_data = {
            "round_timings": narrative.round_timings(),
            "build_duration_s": narrative.build_duration_or_none(),
            "recovered_tool_errors": 0,
            "child_session_ids": child_session_ids,
            "phase_usage": jsonl_writer.phase_breakdown(),
            "last_activity": narrative.latest_activity(),
            "last_tool_name": narrative.latest_tool_name(),
            "last_tool_args_summary": narrative.latest_tool_args_summary(),
            "last_story_id": narrative.current_story_id(),
            "last_operation_started_at": narrative.last_operation_started_at(),
            "subagent_errors": [
                item for item in jsonl_writer.last_records(40)
                if item.get("type") == "subagent_error"
            ],
        }
        phase = (phase_name or "").lower()
        finalize_breakdown: dict[str, dict[str, float | int]] | None = None
        if phase == "build":
            rounds = len(breakdown_data["round_timings"])
            if rounds > 0:
                certify_duration = sum(
                    end - start for start, end in breakdown_data["round_timings"]
                )
                build_duration = breakdown_data["build_duration_s"]
                if build_duration is None:
                    build_duration = max(narrative.elapsed_seconds() - certify_duration, 0.0)
                if build_duration is not None:
                    finalize_breakdown = {
                        "build": {"duration_s": build_duration},
                        "certify": {
                            "duration_s": certify_duration,
                            "rounds": rounds,
                        },
                    }
            else:
                finalize_breakdown = {"build": {"duration_s": narrative.elapsed_seconds()}}
        elif phase == "certify":
            rounds = len(breakdown_data["round_timings"]) or 1
            finalize_breakdown = {
                "certify": {
                    "duration_s": narrative.elapsed_seconds(),
                    "rounds": rounds,
                }
            }
        elif phase == "spec":
            finalize_breakdown = {
                "spec": {
                    "duration_s": narrative.elapsed_seconds(),
                    "cost_usd": float(cost or 0.0),
                }
            }
        if finalize_breakdown is not None:
            for usage_phase, usage in (breakdown_data.get("phase_usage") or {}).items():
                if usage_phase not in finalize_breakdown or not isinstance(usage, dict):
                    continue
                for key in TOKEN_USAGE_KEYS:
                    if isinstance(usage.get(key), (int, float)):
                        finalize_breakdown[usage_phase][key] = int(usage[key])
                if (
                    isinstance(usage.get("cost_usd"), (int, float))
                    and float(usage["cost_usd"]) > 0
                    and "cost_usd" not in finalize_breakdown[usage_phase]
                ):
                    finalize_breakdown[usage_phase]["cost_usd"] = float(usage["cost_usd"])
        if phase == "build" and finalize_breakdown is not None:
            estimated_costs = estimate_phase_costs(log_dir / "messages.jsonl", float(cost or 0.0))
            if estimated_costs:
                for phase_name, phase_costs in estimated_costs.items():
                    if phase_name in finalize_breakdown:
                        finalize_breakdown[phase_name].update(phase_costs)
        finalize_stats = narrative.finalize(finalize_breakdown)
        breakdown_data["recovered_tool_errors"] = int(
            finalize_stats.get("recovered_tool_errors", 0)
        )
        _attach_structured_result(breakdown_data, result_msg)
        _persist_provider_artifacts(breakdown_data)
        return text, cost, session_id, breakdown_data
    except AgentCallError as err:
        _cleanup_agent_processes(project_dir, agent_state)
        err.session_id = err.session_id or agent_state.get("session_id", "")
        if err.total_cost_usd is None and agent_state.get("total_cost_usd") is not None:
            err.total_cost_usd = float(agent_state.get("total_cost_usd"))
        if not err.crash_path:
            err.crash_path = _write_crash_details(err, traceback_text=err.traceback_text)
        err.last_events = jsonl_writer.last_records(20)
        err.last_provider_stderr = agent_state.get("provider_stderr", "") or ""
        err.last_activity = narrative.latest_activity()
        err.last_tool_name = narrative.latest_tool_name()
        err.last_tool_args_summary = narrative.latest_tool_args_summary()
        err.last_story_id = narrative.current_story_id()
        err.last_operation_started_at = narrative.last_operation_started_at()
        raise err
    except asyncio.TimeoutError:
        log.error("Agent timed out after %ds", timeout)
        _append_narrative(f"\u2501\u2501\u2501 Timed out after {timeout}s")
        _cleanup_agent_processes(project_dir, agent_state)
        err = AgentCallError(
            f"Timed out after {timeout}s",
            session_id=agent_state.get("session_id", ""),
            total_cost_usd=float(agent_state.get("total_cost_usd", 0.0) or 0.0),
        )
        err.crash_path = _write_crash_details(err)
        err.last_events = jsonl_writer.last_records(20)
        err.last_provider_stderr = agent_state.get("provider_stderr", "") or ""
        err.last_activity = narrative.latest_activity()
        err.last_tool_name = narrative.latest_tool_name()
        err.last_tool_args_summary = narrative.latest_tool_args_summary()
        err.last_story_id = narrative.current_story_id()
        err.last_operation_started_at = narrative.last_operation_started_at()
        raise err
    except asyncio.CancelledError:
        _append_narrative("\u2501\u2501\u2501 Agent run cancelled")
        _cleanup_agent_processes(project_dir, agent_state)
        raise
    except KeyboardInterrupt:
        _append_narrative("\u2501\u2501\u2501 KeyboardInterrupt")
        _cleanup_agent_processes(project_dir, agent_state)
        raise
    except Exception as exc:
        log.exception("Agent crashed")
        _append_narrative(f"\u2501\u2501\u2501 Agent crashed: {exc}")
        _cleanup_agent_processes(project_dir, agent_state)
        tb = traceback.format_exc()
        err = AgentCallError(
            f"Agent crashed: {exc}",
            session_id=agent_state.get("session_id", ""),
            total_cost_usd=float(agent_state.get("total_cost_usd", 0.0) or 0.0),
        )
        err.traceback_text = tb
        err.crash_path = _write_crash_details(err, traceback_text=tb)
        err.last_events = jsonl_writer.last_records(20)
        err.last_provider_stderr = agent_state.get("provider_stderr", "") or ""
        err.last_activity = narrative.latest_activity()
        err.last_tool_name = narrative.latest_tool_name()
        err.last_tool_args_summary = narrative.latest_tool_args_summary()
        err.last_story_id = narrative.current_story_id()
        err.last_operation_started_at = narrative.last_operation_started_at()
        raise err
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        close_fh()


def _provider_name(options: AgentOptions | None) -> str:
    from otto.config import CODEX_APP_SERVER_PROVIDER, normalize_provider

    raw_provider = getattr(options, "provider", None)
    try:
        provider = normalize_provider(raw_provider, default=CODEX_APP_SERVER_PROVIDER)
    except ValueError as exc:
        raise ValueError(f"Unsupported agent provider: {str(raw_provider).strip().lower()}") from exc
    return provider or CODEX_APP_SERVER_PROVIDER


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
    tool_compat = _codex_tool_compat_prelude(prompt)
    if tool_compat:
        parts.append(tool_compat)
    parts.append(prompt)
    return "\n\n".join(part for part in parts if part).strip()


def _codex_tool_compat_prelude(prompt: str) -> str:
    """Translate Otto's Claude-flavored tool names for Codex sessions."""
    lower = prompt.lower()
    if "agent tool" not in lower and "subagent" not in lower and "sub-agent" not in lower:
        return ""
    return (
        "Codex provider compatibility:\n"
        "- When these instructions say to use the Agent tool or subagents, "
        "use Codex's `spawn_agent` tool.\n"
        "- After spawning subagents, use the Codex wait tool to collect every "
        "subagent result before reporting."
    )


def _openai_agents_instructions(options: AgentOptions) -> str | None:
    parts: list[str] = []
    if isinstance(options.system_prompt, str) and options.system_prompt.strip():
        parts.append(options.system_prompt.strip())
    compat = _codex_compat_prelude(options)
    if compat:
        parts.append(compat)
    parts.append(
        "Otto provider compatibility:\n"
        "- Use the provided filesystem, shell, and patch tools to work inside "
        "the current project directory.\n"
        "- Prefer apply_patch for text edits and shell commands for verification.\n"
        "- Do not use process-wide kill commands; stop only processes you started "
        "through the active tool session."
    )
    return "\n\n".join(part for part in parts if part).strip() or None


def _openai_agents_model_settings(options: AgentOptions) -> Any:
    if _OpenAIModelSettings is None:
        return None
    kwargs: dict[str, Any] = {
        "include_usage": True,
        "parallel_tool_calls": True,
        "truncation": "auto",
    }
    effort = _openai_agents_reasoning_effort(options.effort)
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    return _OpenAIModelSettings(**kwargs)


def _openai_agents_reasoning_effort(effort: str | None) -> str | None:
    value = str(effort or "").strip().lower()
    if not value:
        return None
    if value == "max":
        return "high"
    if value in {"low", "medium", "high"}:
        return value
    return None


def _openai_agents_output_type(output_format: Any) -> Any:
    if output_format is None:
        return None
    if isinstance(output_format, type):
        return output_format
    if not isinstance(output_format, dict):
        return None

    schema = output_format.get("schema")
    if not isinstance(schema, dict):
        json_schema = output_format.get("json_schema")
        if isinstance(json_schema, dict):
            schema = json_schema.get("schema")
    if not isinstance(schema, dict):
        return None
    if str(schema.get("type") or "").lower() != "object":
        return None

    try:
        from pydantic import create_model
    except Exception:
        return None

    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return None
    required = {
        str(item)
        for item in (schema.get("required") or [])
        if isinstance(item, str)
    }
    fields: dict[str, tuple[Any, Any]] = {}
    for name, spec in properties.items():
        if not isinstance(name, str) or not name.isidentifier():
            continue
        field_type = _json_schema_python_type(spec if isinstance(spec, dict) else {})
        fields[name] = (field_type, ... if name in required else None)
    if not fields:
        return None
    model_name = str(
        output_format.get("name")
        or (output_format.get("json_schema") or {}).get("name")
        or schema.get("title")
        or "OttoStructuredOutput"
    )
    model_name = re.sub(r"\W+", "_", model_name).strip("_") or "OttoStructuredOutput"
    model = create_model(model_name, **fields)
    if _OpenAIAgentOutputSchema is None:
        return model
    return _OpenAIAgentOutputSchema(
        model,
        strict_json_schema=bool(output_format.get("strict", True)),
    )


def _json_schema_python_type(spec: dict[str, Any]) -> Any:
    typ = str(spec.get("type") or "").lower()
    if isinstance(spec.get("enum"), list):
        return str
    if typ == "string":
        return str
    if typ == "integer":
        return int
    if typ == "number":
        return float
    if typ == "boolean":
        return bool
    if typ == "array":
        return list[Any]
    if typ == "object":
        return dict[str, Any]
    return Any


def _openai_agents_trace_enabled() -> bool:
    return str(os.environ.get("OTTO_OPENAI_AGENTS_TRACING", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _openai_agents_trace_sensitive() -> bool:
    return str(os.environ.get("OTTO_OPENAI_AGENTS_TRACE_SENSITIVE", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _openai_agents_run_config(options: AgentOptions) -> Any:
    if _OpenAIRunConfig is None:
        return None
    metadata = {
        "otto_provider": "openai-agents",
        "cwd": options.cwd or "",
    }
    if options.model:
        metadata["model"] = options.model
    kwargs: dict[str, Any] = {
        "model": options.model,
        "model_settings": _openai_agents_model_settings(options),
        "workflow_name": "otto.agent",
        "trace_metadata": metadata,
        "tracing_disabled": not _openai_agents_trace_enabled(),
        "trace_include_sensitive_data": _openai_agents_trace_sensitive(),
    }
    sandbox_config = _openai_agents_sandbox_config(options)
    if sandbox_config is not None:
        kwargs["sandbox"] = sandbox_config
    return _OpenAIRunConfig(**kwargs)


def _openai_agents_manifest(options: AgentOptions) -> Any:
    if _OpenAIManifest is None:
        return None
    root = str(Path(options.cwd or os.getcwd()).resolve())
    env = {
        str(key): str(value)
        for key, value in (options.env or {}).items()
        if key is not None and value is not None
    }
    kwargs: dict[str, Any] = {"root": root}
    if env:
        kwargs["environment"] = {"value": env}
    return _OpenAIManifest(**kwargs)


def _openai_agents_sandbox_config(options: AgentOptions) -> Any:
    if (
        _OpenAISandboxRunConfig is None
        or _OpenAIUnixLocalSandboxClient is None
        or _OpenAIManifest is None
    ):
        return None
    return _OpenAISandboxRunConfig(
        client=_OpenAIUnixLocalSandboxClient(),
        manifest=_openai_agents_manifest(options),
    )


def _openai_agents_capabilities() -> list[Any] | None:
    if (
        _OpenAIFilesystemCapability is None
        or _OpenAIShellCapability is None
        or _OpenAICompactionCapability is None
    ):
        return None

    capabilities: list[Any] = [_OpenAIFilesystemCapability(), _OpenAIShellCapability()]
    if _OpenAIExecCommandTool is not None:
        capabilities[1] = _OpenAIShellCapability(configure_tools=_configure_openai_shell_tools)
    capabilities.append(_OpenAICompactionCapability())
    return capabilities


def _configure_openai_shell_tools(toolset: Any) -> None:
    original = getattr(toolset, "exec_command", None)
    if original is None or _OpenAIExecCommandTool is None:
        return

    class OttoSafeExecCommandTool(_OpenAIExecCommandTool):  # type: ignore[misc, valid-type]
        async def run(self, args: Any) -> str:  # noqa: ANN401 - SDK-owned args model
            command = str(getattr(args, "cmd", "") or "")
            reason = _unsafe_bash_command_reason(command)
            if reason:
                return reason
            return await super().run(args)

    session = getattr(original, "session", None)
    if session is None:
        return
    toolset.exec_command = OttoSafeExecCommandTool(
        session=session,
        user=getattr(original, "user", None),
    )


def _openai_agents_agent(options: AgentOptions) -> Any:
    output_type = _openai_agents_output_type(options.output_format)
    agent_kwargs: dict[str, Any] = {
        "name": "otto-openai-agents",
        "instructions": _openai_agents_instructions(options),
        "model": options.model,
        "model_settings": _openai_agents_model_settings(options),
        "output_type": output_type,
    }
    if _OpenAISandboxAgent is not None and _openai_agents_sandbox_config(options) is not None:
        agent_kwargs["default_manifest"] = _openai_agents_manifest(options)
        capabilities = _openai_agents_capabilities()
        if capabilities is not None:
            agent_kwargs["capabilities"] = capabilities
        return _OpenAISandboxAgent(**agent_kwargs)
    if _OpenAIAgent is None:
        return None
    return _OpenAIAgent(**agent_kwargs)


def _openai_agents_auth_error() -> str | None:
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_ADMIN_KEY"):
        return None
    stored_api_key, has_subscription_tokens = _codex_auth_credentials()
    if stored_api_key:
        if _OpenAISetDefaultOpenAIKey is not None:
            _OpenAISetDefaultOpenAIKey(stored_api_key, use_for_tracing=False)
            return None
        os.environ["OPENAI_API_KEY"] = stored_api_key
        return None
    if has_subscription_tokens:
        return (
            "Codex subscription login was found in ~/.codex/auth.json, but the "
            "OpenAI Agents SDK uses API credentials for Responses API model calls. "
            "The subscription OAuth token is not an API key and lacks scopes such "
            "as api.responses.write. Use provider=codex for subscription-backed "
            "Codex CLI runs, or export an API-scoped OPENAI_API_KEY before using "
            "provider=openai-agents."
        )
    return (
        "OPENAI_API_KEY is not set. Export an API-scoped OPENAI_API_KEY before "
        "using provider=openai-agents, or use provider=codex for Codex CLI "
        "subscription-backed runs."
    )


def _codex_auth_credentials() -> tuple[str | None, bool]:
    auth_path = Path.home() / ".codex" / "auth.json"
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, False
    raw_key = data.get("OPENAI_API_KEY")
    api_key = str(raw_key).strip() if isinstance(raw_key, str) else ""
    tokens = data.get("tokens")
    has_subscription_tokens = isinstance(tokens, dict) and any(
        bool(tokens.get(key))
        for key in ("access_token", "id_token", "refresh_token")
    )
    return api_key or None, has_subscription_tokens


def _openai_agents_text_from_message_raw(raw_item: Any) -> str:
    content = _raw_value(raw_item, "content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        text = _raw_value(item, "text")
        if text is None:
            text = _raw_value(item, "content")
        if text is not None:
            parts.append(str(text))
    return "".join(parts)


def _openai_agents_tool_name(raw_item: Any, item: Any) -> str:
    name = _raw_value(item, "tool_name") or _raw_value(raw_item, "name")
    typ = str(_raw_value(raw_item, "type") or "").lower()
    if not name:
        if typ in {"shell_call", "local_shell_call"}:
            return "Bash"
        if typ == "apply_patch_call":
            return "Edit"
    if str(name) in {"exec_command", "shell", "local_shell"}:
        return "Bash"
    if str(name) == "apply_patch":
        return "Edit"
    return str(name or "")


def _openai_agents_tool_input(raw_item: Any) -> dict[str, Any]:
    for attr in ("input", "arguments", "args"):
        value = _raw_value(raw_item, attr)
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    command = _raw_value(raw_item, "command") or _raw_value(raw_item, "cmd")
    if command is not None:
        return {"command": str(command), "cmd": str(command)}
    action = _raw_value(raw_item, "action")
    if isinstance(action, dict):
        return dict(action)
    return {}


def _openai_agents_tool_output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    if hasattr(output, "model_dump"):
        try:
            output = output.model_dump()
        except Exception:
            pass
    if isinstance(output, dict):
        for key in ("output", "text", "content", "stdout"):
            value = output.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(_json_safe(output), ensure_ascii=False)
    return str(output)


def _openai_agents_reasoning_text(raw_item: Any) -> str:
    for attr in ("summary", "content", "text"):
        value = _raw_value(raw_item, attr)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list):
            parts = []
            for item in value:
                text = _raw_value(item, "text") or _raw_value(item, "content")
                if text:
                    parts.append(str(text))
            if parts:
                return "\n".join(parts)
    return ""


def _openai_agents_normalize_item(item: Any, session_id: str) -> Any | None:
    item_type = str(_raw_value(item, "type") or "")
    raw_item = _raw_value(item, "raw_item") or {}
    if item_type == "message_output_item":
        text = _openai_agents_text_from_message_raw(raw_item)
        if text:
            return AssistantMessage(content=[TextBlock(text=text)], session_id=session_id)
        return None
    if item_type == "tool_call_item":
        tool_name = _openai_agents_tool_name(raw_item, item)
        tool_input = _openai_agents_tool_input(raw_item)
        if tool_name == "Bash" and "command" not in tool_input and "cmd" in tool_input:
            tool_input["command"] = tool_input["cmd"]
        return AssistantMessage(
            content=[
                ToolUseBlock(
                    name=tool_name,
                    input=tool_input,
                    id=str(_raw_value(item, "call_id") or _raw_value(raw_item, "call_id") or _raw_value(raw_item, "id") or "") or None,
                )
            ],
            session_id=session_id,
        )
    if item_type == "tool_call_output_item":
        output = _openai_agents_tool_output_text(_raw_value(item, "output"))
        return AssistantMessage(
            content=[
                ToolResultBlock(
                    content=output,
                    tool_use_id=str(_raw_value(item, "call_id") or _raw_value(raw_item, "call_id") or _raw_value(raw_item, "id") or "") or None,
                )
            ],
            session_id=session_id,
        )
    if item_type == "reasoning_item":
        thinking = _openai_agents_reasoning_text(raw_item)
        if thinking:
            return AssistantMessage(content=[ThinkingBlock(thinking=thinking)], session_id=session_id)
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


def _openai_agents_usage_dict(result: Any) -> dict[str, Any] | None:
    totals: dict[str, int] = {
        "requests": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
    }
    seen = False
    for response in list(getattr(result, "raw_responses", []) or []):
        usage = getattr(response, "usage", None)
        if usage is None:
            continue
        seen = True
        totals["requests"] += int(getattr(usage, "requests", 0) or 0)
        totals["input_tokens"] += int(getattr(usage, "input_tokens", 0) or 0)
        totals["output_tokens"] += int(getattr(usage, "output_tokens", 0) or 0)
        totals["total_tokens"] += int(getattr(usage, "total_tokens", 0) or 0)
        input_details = getattr(usage, "input_tokens_details", None)
        totals["cached_input_tokens"] += int(getattr(input_details, "cached_tokens", 0) or 0)
        output_details = getattr(usage, "output_tokens_details", None)
        totals["reasoning_tokens"] += int(getattr(output_details, "reasoning_tokens", 0) or 0)
    if not seen:
        return None
    if not totals["total_tokens"]:
        totals["total_tokens"] = (
            totals["input_tokens"]
            + totals["output_tokens"]
            + totals["reasoning_tokens"]
        )
    return {key: value for key, value in totals.items() if value}


def _structured_output_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    safe = _json_safe(value)
    if isinstance(safe, str):
        return safe
    return json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True)


async def _query_openai_agents(
    *,
    prompt: str,
    options: AgentOptions | None = None,
    state: dict[str, Any] | None = None,
):
    auth_error = _openai_agents_auth_error()
    if auth_error:
        raise RuntimeError(auth_error)
    if _OpenAIRunner is None:
        detail = _OPENAI_AGENTS_IMPORT_ERROR_MESSAGE or "unknown import error"
        raise RuntimeError(
            "openai-agents provider requested but the Agents SDK is not importable: "
            f"{detail}; run `uv pip install -e .[openai]`"
        )
    opts = options or AgentOptions()
    agent = _openai_agents_agent(opts)
    if agent is None:
        raise RuntimeError("openai-agents provider requested but no SDK Agent class is available")

    run_config = _openai_agents_run_config(opts)
    stream = _OpenAIRunner.run_streamed(
        agent,
        prompt,
        max_turns=opts.max_turns,
        run_config=run_config,
        previous_response_id=opts.resume or None,
    )
    if hasattr(stream, "ensure_sandbox_cleanup_on_completion"):
        stream.ensure_sandbox_cleanup_on_completion()

    async for event in stream.stream_events():
        if str(_raw_value(event, "type") or "") != "run_item_stream_event":
            continue
        item = _raw_value(event, "item")
        if item is None:
            continue
        session_id = str(getattr(stream, "last_response_id", None) or "")
        if state is not None and session_id:
            state["session_id"] = session_id
        normalized = _openai_agents_normalize_item(item, session_id)
        if normalized is not None:
            yield normalized

    session_id = str(getattr(stream, "last_response_id", None) or "")
    if state is not None and session_id:
        state["session_id"] = session_id
    usage = _openai_agents_usage_dict(stream)
    structured = None
    final_output = getattr(stream, "final_output", None)
    if final_output is not None and not isinstance(final_output, str):
        structured = _json_safe(final_output)
    yield ResultMessage(
        subtype="success",
        is_error=False,
        session_id=session_id,
        result=_structured_output_to_text(final_output) if final_output is not None else None,
        total_cost_usd=None,
        usage=usage,
        structured_output=structured,
    )


def _sdk_options(options: AgentOptions | None) -> Any:
    if _SDKClaudeAgentOptions is None:
        return options
    opts = options or AgentOptions()
    permission_mode = opts.permission_mode
    if opts.can_use_tool is not None and permission_mode == "bypassPermissions":
        # Claude's bypass mode can execute tools without consulting the stdio
        # permission callback. Keep the callback on the control path so Otto's
        # process-kill guard applies to composed Bash commands, not just coarse
        # disallowedTools prefixes.
        permission_mode = "default"
    return _SDKClaudeAgentOptions(
        permission_mode=permission_mode,
        cwd=opts.cwd,
        model=opts.model,
        resume=opts.resume,
        max_turns=opts.max_turns,
        system_prompt=opts.system_prompt,
        mcp_servers=opts.mcp_servers,
        env=opts.env or {},
        setting_sources=opts.setting_sources,
        effort=opts.effort,
        agents=opts.agents,
        max_buffer_size=opts.max_buffer_size,
        disallowed_tools=opts.disallowed_tools or [],
        hooks=opts.hooks,
        can_use_tool=opts.can_use_tool,
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


async def _query_claude(
    *,
    prompt: str,
    options: AgentOptions | None = None,
    state: dict[str, Any] | None = None,
):
    if _sdk_query is None:
        detail = _SDK_IMPORT_ERROR_MESSAGE or "unknown import error"
        raise RuntimeError(
            "claude_agent_sdk not importable: "
            f"{detail}; run `uv pip install -e .[claude]`"
        )

    opts = options or AgentOptions()
    sdk_options = _sdk_options(opts)
    saved_env = dict(os.environ)

    try:
        import claude_agent_sdk._internal.transport.subprocess_cli as _sdk_subprocess_cli
    except Exception:  # pragma: no cover - SDK internals may move
        _sdk_subprocess_cli = None

    original_open_process = getattr(getattr(_sdk_subprocess_cli, "anyio", None), "open_process", None)

    async def _open_process_with_session(*args: Any, **kwargs: Any) -> Any:
        kwargs["start_new_session"] = True
        process = await original_open_process(*args, **kwargs)
        if state is not None:
            pid = getattr(process, "pid", None)
            if isinstance(pid, int):
                _remember_agent_process(state, pid)
        return process

    os.environ.clear()
    os.environ.update(opts.env or {})
    try:
        if original_open_process is not None:
            _sdk_subprocess_cli.anyio.open_process = _open_process_with_session
        if (opts.can_use_tool or opts.hooks) and _SDKClaudeSDKClient is not None:
            async with _SDKClaudeSDKClient(sdk_options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    normalized = _normalize_message(message)
                    if normalized is not None:
                        yield normalized
        else:
            async for message in _sdk_query(prompt=prompt, options=sdk_options):
                normalized = _normalize_message(message)
                if normalized is not None:
                    yield normalized
    finally:
        if original_open_process is not None:
            _sdk_subprocess_cli.anyio.open_process = original_open_process
        os.environ.clear()
        os.environ.update(saved_env)


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
    if options.effort:
        effort = _codex_reasoning_effort(options.effort)
        if effort:
            command.extend(["-c", f"model_reasoning_effort={json.dumps(effort)}"])
    if options.cwd and not options.resume:
        command.extend(["-C", options.cwd])
    if options.resume:
        command.append(options.resume)
    command.append("-")
    return command


def _codex_reasoning_effort(effort: str | None) -> str:
    value = str(effort or "").strip().lower()
    if value == "max":
        return "xhigh"
    return value


def _remember_agent_process(state: dict[str, Any], pid: int) -> None:
    state["process_group_id"] = pid
    try:
        import psutil

        state["process_start_time_ns"] = int(psutil.Process(pid).create_time() * 1_000_000_000)
    except Exception:
        state.pop("process_start_time_ns", None)


def _cleanup_agent_processes(project_dir: Path, agent_state: dict[str, Any]) -> None:
    from otto.runs.lifecycle import _cleanup_orphan_processes

    _cleanup_orphan_processes(
        project_dir,
        process_group_id=agent_state.get("process_group_id"),
        process_start_time_ns=agent_state.get("process_start_time_ns"),
    )


def _codex_collab_result_text(item: dict[str, Any], child_id: str | None = None) -> str:
    states = item.get("agents_states")
    if not isinstance(states, dict):
        return ""
    if child_id:
        child_state = states.get(child_id)
        if isinstance(child_state, dict):
            return str(child_state.get("message", "") or "")
        return ""
    messages: list[str] = []
    for child_state in states.values():
        if isinstance(child_state, dict):
            message = str(child_state.get("message", "") or "")
            if message:
                messages.append(message)
    return "\n\n".join(messages)


def _truncate_for_agent_log(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit].rstrip() + f"\n[... Otto truncated {omitted} chars ...]"


def _compact_codex_event_for_error(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "")
    item = event.get("item") or {}
    if not isinstance(item, dict):
        return _truncate_for_agent_log(json.dumps(event, sort_keys=True), 2_000)

    item_type = str(item.get("type") or "")
    item_id = str(item.get("id") or "")
    if item_type == "command_execution":
        command = _truncate_for_agent_log(str(item.get("command") or ""), 240)
        if event_type == "item.completed":
            output = _truncate_for_agent_log(
                str(item.get("aggregated_output") or ""),
                CODEX_PROVIDER_ERROR_OUTPUT_LIMIT_CHARS,
            )
            return (
                f"{event_type} command_execution id={item_id!r} "
                f"status={item.get('status')!r} exit_code={item.get('exit_code')!r} "
                f"command={command!r} output={output!r}"
            )
        return (
            f"{event_type} command_execution id={item_id!r} "
            f"status={item.get('status')!r} command={command!r}"
        )
    if item_type == "agent_message":
        text = _truncate_for_agent_log(
            str(item.get("text") or ""),
            CODEX_PROVIDER_ERROR_OUTPUT_LIMIT_CHARS,
        )
        return f"{event_type} agent_message text={text!r}"
    if item_type:
        return (
            f"{event_type} {item_type} id={item_id!r} "
            f"status={item.get('status')!r}"
        )
    return _truncate_for_agent_log(json.dumps(event, sort_keys=True), 2_000)


def _codex_app_server_command(_options: AgentOptions) -> list[str]:
    return ["codex", "app-server", "--listen", "stdio://"]


def _codex_app_server_approval_policy(options: AgentOptions) -> str:
    if options.permission_mode == "bypassPermissions":
        return "never"
    return "on-failure"


def _codex_app_server_sandbox_mode(options: AgentOptions) -> str:
    if options.permission_mode == "bypassPermissions":
        return "danger-full-access"
    return "workspace-write"


def _codex_app_server_sandbox_policy(options: AgentOptions) -> dict[str, Any]:
    if options.permission_mode == "bypassPermissions":
        return {"type": "dangerFullAccess"}
    return {
        "type": "workspaceWrite",
        "writableRoots": [str(Path(options.cwd or os.getcwd()).resolve())],
        "networkAccess": True,
        "excludeTmpdirEnvVar": False,
        "excludeSlashTmp": False,
    }


def _codex_app_server_thread_params(options: AgentOptions) -> dict[str, Any]:
    params: dict[str, Any] = {
        "cwd": str(Path(options.cwd or os.getcwd()).resolve()),
        "approvalPolicy": _codex_app_server_approval_policy(options),
        "sandbox": _codex_app_server_sandbox_mode(options),
        "serviceName": "otto",
    }
    if options.model:
        params["model"] = options.model
    if isinstance(options.system_prompt, str) and options.system_prompt.strip():
        params["baseInstructions"] = options.system_prompt.strip()
    return params


def _codex_app_server_turn_params(
    *,
    thread_id: str,
    prompt: str,
    options: AgentOptions,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "threadId": thread_id,
        "input": [{"type": "text", "text": prompt, "text_elements": []}],
        "cwd": str(Path(options.cwd or os.getcwd()).resolve()),
        "approvalPolicy": _codex_app_server_approval_policy(options),
        "sandboxPolicy": _codex_app_server_sandbox_policy(options),
    }
    if options.model:
        params["model"] = options.model
    effort = _codex_reasoning_effort(options.effort)
    if effort:
        params["effort"] = effort
    output_schema = _codex_app_server_output_schema(options.output_format)
    if output_schema is not None:
        params["outputSchema"] = output_schema
    return params


def _codex_app_server_output_schema(output_format: Any) -> dict[str, Any] | None:
    if not isinstance(output_format, dict):
        return None
    schema = output_format.get("schema")
    if not isinstance(schema, dict):
        json_schema = output_format.get("json_schema")
        if isinstance(json_schema, dict):
            schema = json_schema.get("schema")
    if not isinstance(schema, dict) and isinstance(output_format.get("type"), str):
        schema = output_format
    if not isinstance(schema, dict):
        return None
    return _codex_app_server_strict_schema(schema)


def _codex_app_server_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return an app-server-compatible JSON schema copy.

    Codex app-server forwards ``outputSchema`` into OpenAI structured output,
    which rejects object schemas that omit ``additionalProperties: false``.
    Otto's public `AgentOptions.output_format` remains provider-neutral; this
    adapter projects it into the stricter app-server dialect at the boundary.
    """
    projected = dict(schema)
    properties = projected.get("properties")
    if isinstance(properties, dict):
        projected["properties"] = {
            key: _codex_app_server_strict_schema(value)
            if isinstance(value, dict) else value
            for key, value in properties.items()
        }
        projected["additionalProperties"] = False
    items = projected.get("items")
    if isinstance(items, dict):
        projected["items"] = _codex_app_server_strict_schema(items)
    return projected


def _codex_app_server_usage_dict(token_usage: Any) -> dict[str, Any] | None:
    if not isinstance(token_usage, dict):
        return None
    breakdown = token_usage.get("total")
    if not isinstance(breakdown, dict):
        breakdown = token_usage.get("last")
    if not isinstance(breakdown, dict):
        breakdown = token_usage
    usage = {
        "input_tokens": int(breakdown.get("inputTokens") or breakdown.get("input_tokens") or 0),
        "cached_input_tokens": int(
            breakdown.get("cachedInputTokens") or breakdown.get("cached_input_tokens") or 0
        ),
        "output_tokens": int(breakdown.get("outputTokens") or breakdown.get("output_tokens") or 0),
        "reasoning_tokens": int(
            breakdown.get("reasoningOutputTokens")
            or breakdown.get("reasoning_tokens")
            or 0
        ),
        "total_tokens": int(breakdown.get("totalTokens") or breakdown.get("total_tokens") or 0),
    }
    if not usage["total_tokens"]:
        usage["total_tokens"] = (
            usage["input_tokens"]
            + usage["output_tokens"]
            + usage["reasoning_tokens"]
        )
    return {key: value for key, value in usage.items() if value}


def _is_path_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _codex_app_server_safe_permission_root(
    params: dict[str, Any],
    options: AgentOptions | None,
) -> Path:
    cwd = (options.cwd if options is not None else None) or params.get("cwd") or os.getcwd()
    return Path(str(cwd)).expanduser().resolve()


def _codex_app_server_safe_permission_base(
    params: dict[str, Any],
    *,
    root: Path,
) -> Path:
    cwd = params.get("cwd")
    if not isinstance(cwd, str):
        return root
    resolved = Path(cwd).expanduser().resolve()
    return resolved if _is_path_under(resolved, root) else root


def _codex_app_server_safe_filesystem_permissions(
    file_system: Any,
    *,
    root: Path,
    base: Path,
) -> dict[str, Any]:
    if not isinstance(file_system, dict):
        return {}

    granted: dict[str, Any] = {}
    for key in ("read", "write"):
        paths: list[str] = []
        raw_paths = file_system.get(key)
        if isinstance(raw_paths, list):
            for raw_path in raw_paths:
                path = Path(str(raw_path)).expanduser()
                if not path.is_absolute():
                    path = base / path
                resolved = path.resolve()
                if _is_path_under(resolved, root):
                    paths.append(str(resolved))
        if paths:
            granted[key] = paths

    entries: list[dict[str, Any]] = []
    raw_entries = file_system.get("entries")
    if isinstance(raw_entries, list):
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            path_spec = entry.get("path")
            if not isinstance(path_spec, dict):
                continue
            if path_spec.get("type") == "path":
                raw_path = path_spec.get("path")
                if not isinstance(raw_path, str):
                    continue
                path = Path(raw_path).expanduser()
                if not path.is_absolute():
                    path = base / path
                resolved = path.resolve()
                if _is_path_under(resolved, root):
                    safe_entry = dict(entry)
                    safe_entry["path"] = {"type": "path", "path": str(resolved)}
                    entries.append(safe_entry)
                continue
            if path_spec.get("type") == "special":
                value = path_spec.get("value")
                if isinstance(value, dict) and value.get("kind") == "project_roots":
                    entries.append(dict(entry))
    if entries:
        granted["entries"] = entries
        depth = file_system.get("globScanMaxDepth")
        if isinstance(depth, int) and depth > 0:
            granted["globScanMaxDepth"] = depth

    return granted


def _codex_app_server_granted_permissions(
    params: dict[str, Any],
    options: AgentOptions | None,
) -> dict[str, Any]:
    permissions = params.get("permissions")
    if not isinstance(permissions, dict):
        return {}
    root = _codex_app_server_safe_permission_root(params, options)
    base = _codex_app_server_safe_permission_base(params, root=root)
    granted: dict[str, Any] = {}
    file_system = _codex_app_server_safe_filesystem_permissions(
        permissions.get("fileSystem"),
        root=root,
        base=base,
    )
    if file_system:
        granted["fileSystem"] = file_system
    return granted


def _json_schema_type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True


def _validate_json_schema_subset(
    value: Any,
    schema: dict[str, Any],
    *,
    path: str = "$",
) -> list[str]:
    """Validate the JSON-schema subset Otto passes to app-server.

    This is intentionally small: it covers the schema features Otto emits
    for structured spec/audit results and provider regressions, while leaving
    full JSON Schema semantics to upstream providers and stage validators.
    """
    errors: list[str] = []
    raw_type = schema.get("type")
    expected_types = (
        [str(item) for item in raw_type]
        if isinstance(raw_type, list)
        else [str(raw_type)] if isinstance(raw_type, str) else []
    )
    if expected_types and not any(_json_schema_type_matches(value, kind) for kind in expected_types):
        errors.append(f"{path}: expected {'/'.join(expected_types)}, got {type(value).__name__}")
        return errors

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        errors.append(f"{path}: expected one of {enum!r}, got {value!r}")

    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    errors.append(f"{path}.{key}: missing required field")
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, child_schema in properties.items():
                if key in value and isinstance(child_schema, dict):
                    errors.extend(
                        _validate_json_schema_subset(
                            value[key],
                            child_schema,
                            path=f"{path}.{key}",
                        )
                    )
    elif isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(
                    _validate_json_schema_subset(
                        item,
                        item_schema,
                        path=f"{path}[{index}]",
                    )
                )
    return errors


def _codex_app_server_structured_output_result(
    text: str,
    options: AgentOptions,
) -> tuple[Any, str]:
    schema = _codex_app_server_output_schema(options.output_format)
    if schema is None:
        return None, ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"structured output was not valid JSON: {exc}"
    errors = _validate_json_schema_subset(parsed, schema)
    if errors:
        return parsed, "structured output failed schema validation: " + "; ".join(errors[:5])
    return parsed, ""


def _codex_app_server_structured_output(text: str, options: AgentOptions) -> Any:
    parsed, _error = _codex_app_server_structured_output_result(text, options)
    return parsed


def _codex_app_server_diff_summary(diff_text: str) -> dict[str, Any]:
    changed_files: list[str] = []
    for line in diff_text.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) >= 4:
            path = parts[3]
            changed_files.append(path[2:] if path.startswith("b/") else path)
    return {
        "diff_sha256": hashlib.sha256(diff_text.encode("utf-8")).hexdigest(),
        "diff_bytes": len(diff_text.encode("utf-8")),
        "changed_files": changed_files,
    }


def _codex_app_server_collab_result_text(item: dict[str, Any], child_id: str | None = None) -> str:
    states = item.get("agentsStates")
    if not isinstance(states, dict):
        states = item.get("agents_states")
    if not isinstance(states, dict):
        return ""
    if child_id:
        child_state = states.get(child_id)
        if isinstance(child_state, dict):
            return str(child_state.get("message", "") or "")
        return ""
    messages: list[str] = []
    for child_state in states.values():
        if isinstance(child_state, dict):
            message = str(child_state.get("message", "") or "")
            if message:
                messages.append(message)
    return "\n\n".join(messages)


def _codex_app_server_normalize_item(
    item: dict[str, Any],
    *,
    method: str,
    thread_id: str,
    command_outputs: dict[str, str],
    agent_message_buffers: dict[str, str],
    emitted_agent_items: set[str],
    emitted_collab_tool_ids: set[str],
    child_tool_use_by_thread_id: dict[str, str],
    state: dict[str, Any] | None,
) -> AssistantMessage | None:
    item_type = str(item.get("type") or "")
    item_id = str(item.get("id") or "") or None

    if item_type == "agentMessage":
        text = str(item.get("text") or "")
        if not text and item_id:
            text = agent_message_buffers.get(item_id, "")
        if text:
            if item_id:
                emitted_agent_items.add(item_id)
            return AssistantMessage(content=[TextBlock(text=text)], session_id=thread_id)
        return None

    if item_type == "plan":
        text = str(item.get("text") or "")
        if text:
            return AssistantMessage(content=[TextBlock(text=f"[plan] {text}")], session_id=thread_id)
        return None

    if item_type == "reasoning":
        parts = [
            str(part)
            for part in [*(item.get("summary") or []), *(item.get("content") or [])]
            if part
        ]
        if parts:
            return AssistantMessage(
                content=[ThinkingBlock(thinking="\n".join(parts))],
                session_id=thread_id,
            )
        return None

    if item_type == "commandExecution":
        command = str(item.get("command") or "")
        if method == "item/started":
            return AssistantMessage(
                content=[ToolUseBlock(name="Bash", input={"command": command}, id=item_id)],
                session_id=thread_id,
            )
        output = item.get("aggregatedOutput")
        if output is None and item_id:
            output = command_outputs.get(item_id, "")
        return AssistantMessage(
            content=[ToolResultBlock(content=str(output or ""), tool_use_id=item_id)],
            session_id=thread_id,
        )

    if item_type == "fileChange":
        changes = item.get("changes") or []
        if method == "item/started":
            return AssistantMessage(
                content=[ToolUseBlock(name="Edit", input={"changes": changes}, id=item_id)],
                session_id=thread_id,
            )
        return AssistantMessage(
            content=[
                ToolResultBlock(
                    content=json.dumps(
                        {
                            "status": item.get("status"),
                            "changes": changes,
                        },
                        ensure_ascii=False,
                    ),
                    tool_use_id=item_id,
                )
            ],
            session_id=thread_id,
        )

    if item_type == "collabAgentToolCall":
        tool = str(item.get("tool") or "")
        receiver_thread_ids = [
            str(child_id)
            for child_id in (item.get("receiverThreadIds") or item.get("receiver_thread_ids") or [])
            if child_id
        ]
        if state is not None and receiver_thread_ids:
            existing_children = set(state.get("codex_child_session_ids", []) or [])
            existing_children.update(receiver_thread_ids)
            state["codex_child_session_ids"] = sorted(existing_children)
        if tool == "spawnAgent" and method in {"item/started", "item/completed"}:
            if item_id and method == "item/completed":
                for child_id in receiver_thread_ids:
                    child_tool_use_by_thread_id[child_id] = item_id
            if item_id and item_id in emitted_collab_tool_ids:
                return None
            if item_id:
                emitted_collab_tool_ids.add(item_id)
            return AssistantMessage(
                content=[
                    ToolUseBlock(
                        name="Agent",
                        input={
                            "subagent_type": "codex-app-server",
                            "prompt": str(item.get("prompt") or ""),
                        },
                        id=item_id,
                    )
                ],
                session_id=thread_id,
            )
        if tool == "wait" and method == "item/completed":
            parts: list[ToolResultBlock] = []
            for child_id in receiver_thread_ids:
                content = _codex_app_server_collab_result_text(item, child_id)
                if content:
                    parts.append(
                        ToolResultBlock(
                            content=content,
                            tool_use_id=child_tool_use_by_thread_id.get(child_id) or item_id,
                        )
                    )
            if parts:
                return AssistantMessage(content=parts, session_id=thread_id)
    return None


def _codex_app_server_approval_result(
    method: str,
    params: dict[str, Any],
    options: AgentOptions | None = None,
) -> dict[str, Any] | None:
    if method == "item/commandExecution/requestApproval":
        reason = _unsafe_bash_command_reason(str(params.get("command") or ""))
        return {"decision": "decline" if reason else "accept"}
    if method == "execCommandApproval":
        command = str(params.get("command") or params.get("cmd") or "")
        reason = _unsafe_bash_command_reason(command)
        return {"decision": "denied" if reason else "approved"}
    if method == "item/fileChange/requestApproval":
        return {"decision": "accept"}
    if method == "applyPatchApproval":
        return {"decision": "approved"}
    if method == "item/permissions/requestApproval":
        return {
            "permissions": _codex_app_server_granted_permissions(params, options),
            "scope": "turn",
            "strictAutoReview": False,
        }
    return None


async def _query_codex_app_server(
    *,
    prompt: str,
    options: AgentOptions | None = None,
    state: dict[str, Any] | None = None,
):
    opts = options or AgentOptions()
    env = dict(opts.env) if opts.env is not None else None

    try:
        process = await asyncio.create_subprocess_exec(
            *_codex_app_server_command(opts),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=opts.cwd or None,
            env=env,
            limit=CODEX_STDIO_LIMIT_BYTES,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "codex CLI not found in PATH; install it from https://developers.openai.com/codex/cli"
        ) from exc

    if state is not None:
        pid = getattr(process, "pid", None)
        if isinstance(pid, int):
            _remember_agent_process(state, pid)

    stdout = process.stdout
    stdin = process.stdin
    assert stdout is not None
    assert stdin is not None

    request_id = 0
    raw_lines: list[str] = []
    last_text = ""
    last_usage: dict[str, Any] | None = None
    last_diff = ""
    command_outputs: dict[str, str] = {}
    agent_message_buffers: dict[str, str] = {}
    emitted_agent_items: set[str] = set()
    emitted_collab_tool_ids: set[str] = set()
    child_tool_use_by_thread_id: dict[str, str] = {}

    def provider_event(
        event_name: str,
        *,
        method: str = "",
        params: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        status: str = "",
        data: dict[str, Any] | None = None,
    ) -> ProviderEventMessage:
        event_params = params or {}
        fallback_session_id = state.get("session_id", "") if state is not None else ""
        return ProviderEventMessage(
            event=event_name,
            provider="codex-app-server",
            session_id=str(event_params.get("threadId") or fallback_session_id),
            method=method,
            turn_id=str(event_params.get("turnId") or ""),
            item_id=str(event_params.get("itemId") or ""),
            status=status,
            usage=usage,
            data=data,
        )

    async def send(payload: dict[str, Any]) -> None:
        stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await stdin.drain()

    async def send_request(method: str, params: Any) -> int:
        nonlocal request_id
        current = request_id
        request_id += 1
        await send({"method": method, "id": current, "params": params})
        return current

    async def send_result(current_id: Any, result: Any) -> None:
        await send({"id": current_id, "result": result})

    async def send_error(current_id: Any, message: str) -> None:
        await send({"id": current_id, "error": {"code": -32603, "message": message}})

    async def read_event() -> dict[str, Any] | None:
        while True:
            raw_line = await stdout.readline()
            if not raw_line:
                return None
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            raw_lines.append(line)
            if state is not None:
                state["provider_stderr"] = "\n".join(raw_lines[-50:])
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    async def handle_server_request(event: dict[str, Any]) -> bool:
        if "id" not in event or not isinstance(event.get("method"), str):
            return False
        method = str(event.get("method") or "")
        params = event.get("params")
        result = _codex_app_server_approval_result(
            method,
            params if isinstance(params, dict) else {},
            opts,
        )
        if result is None:
            await send_error(event.get("id"), f"Otto codex-app-server provider does not support {method}")
        else:
            await send_result(event.get("id"), result)
        return True

    async def read_response(expected_id: int) -> dict[str, Any] | None:
        while True:
            event = await read_event()
            if event is None:
                return None
            if await handle_server_request(event):
                continue
            if event.get("id") == expected_id:
                return event

    try:
        init_id = await send_request(
            "initialize",
            {"clientInfo": {"name": "otto", "title": "Otto", "version": "0.0.0"}},
        )
        init_response = await read_response(init_id)
        if init_response is None or init_response.get("error"):
            error = init_response.get("error") if isinstance(init_response, dict) else None
            message = (error or {}).get("message") if isinstance(error, dict) else None
            yield ResultMessage(subtype="error", is_error=True, result=message or "codex app-server initialize failed")
            return
        await send({"method": "initialized", "params": {}})

        if opts.resume:
            thread_params = {
                **_codex_app_server_thread_params(opts),
                "threadId": opts.resume,
            }
            thread_request_id = await send_request("thread/resume", thread_params)
        else:
            thread_request_id = await send_request(
                "thread/start",
                _codex_app_server_thread_params(opts),
            )
        thread_response = await read_response(thread_request_id)
        if thread_response is None or thread_response.get("error"):
            error = thread_response.get("error") if isinstance(thread_response, dict) else None
            message = (error or {}).get("message") if isinstance(error, dict) else None
            yield ResultMessage(subtype="error", is_error=True, result=message or "codex app-server thread start failed")
            return
        result = thread_response.get("result") if isinstance(thread_response, dict) else {}
        thread = (result or {}).get("thread") if isinstance(result, dict) else {}
        thread_id = str((thread or {}).get("id") or opts.resume or "")
        if not thread_id:
            yield ResultMessage(subtype="error", is_error=True, result="codex app-server did not return a thread id")
            return
        if state is not None:
            state["session_id"] = thread_id
            state["codex_app_server_thread_id"] = thread_id

        final_prompt = _codex_prompt(prompt, opts)
        turn_id = await send_request(
            "turn/start",
            _codex_app_server_turn_params(
                thread_id=thread_id,
                prompt=final_prompt,
                options=opts,
            ),
        )
        saw_result = False
        turn_seen = False
        while True:
            event = await read_event()
            if event is None:
                break
            if await handle_server_request(event):
                continue
            if event.get("id") == turn_id and event.get("error"):
                error = event.get("error")
                message = (error or {}).get("message") if isinstance(error, dict) else None
                yield ResultMessage(
                    subtype="error",
                    is_error=True,
                    session_id=thread_id,
                    result=message or "codex app-server turn start failed",
                    usage=last_usage,
                )
                saw_result = True
                break

            method = str(event.get("method") or "")
            params = event.get("params") if isinstance(event.get("params"), dict) else {}
            event_thread_id = str(params.get("threadId") or thread_id)

            if event.get("id") == turn_id and not event.get("error"):
                turn_seen = True
                yield provider_event(
                    "turn_acknowledged",
                    method="turn/start",
                    params={"threadId": thread_id},
                    data={"request_id": turn_id},
                )
                continue

            if method == "turn/started":
                turn_seen = True
                yield provider_event(
                    "turn_started",
                    method=method,
                    params=params,
                    status="started",
                )
                continue

            if method == "thread/tokenUsage/updated":
                usage = _codex_app_server_usage_dict(params.get("tokenUsage"))
                if usage:
                    last_usage = usage
                    yield provider_event(
                        "token_usage_updated",
                        method=method,
                        params=params,
                        usage=usage,
                    )
                continue

            if method == "turn/diff/updated":
                last_diff = str(params.get("diff") or "")
                if state is not None and last_diff:
                    state["codex_app_server_diff"] = last_diff
                if last_diff:
                    yield provider_event(
                        "diff_updated",
                        method=method,
                        params=params,
                        data=_codex_app_server_diff_summary(last_diff),
                    )
                continue

            if method == "item/agentMessage/delta":
                item_id = str(params.get("itemId") or "")
                if item_id:
                    agent_message_buffers[item_id] = (
                        agent_message_buffers.get(item_id, "")
                        + str(params.get("delta") or "")
                    )
                continue

            if method in {"item/commandExecution/outputDelta", "command/exec/outputDelta"}:
                item_id = str(params.get("itemId") or "")
                if item_id:
                    command_outputs[item_id] = (
                        command_outputs.get(item_id, "")
                        + str(params.get("delta") or "")
                    )
                continue

            if method in {"item/started", "item/completed"}:
                item = params.get("item")
                if isinstance(item, dict):
                    message = _codex_app_server_normalize_item(
                        item,
                        method=method,
                        thread_id=event_thread_id,
                        command_outputs=command_outputs,
                        agent_message_buffers=agent_message_buffers,
                        emitted_agent_items=emitted_agent_items,
                        emitted_collab_tool_ids=emitted_collab_tool_ids,
                        child_tool_use_by_thread_id=child_tool_use_by_thread_id,
                        state=state,
                    )
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock) and block.text:
                                last_text = block.text
                        yield message
                continue

            if method == "thread/status/changed":
                status = params.get("status")
                status_type = str((status or {}).get("type") or "") if isinstance(status, dict) else ""
                if status_type:
                    yield provider_event(
                        "thread_status_changed",
                        method=method,
                        params=params,
                        status=status_type,
                    )
                if status_type == "idle" and turn_seen and (last_text or last_usage):
                    structured_output, structured_error = _codex_app_server_structured_output_result(last_text, opts)
                    yield ResultMessage(
                        subtype="success",
                        is_error=False,
                        session_id=event_thread_id,
                        result=last_text or None,
                        total_cost_usd=0.0,
                        usage=last_usage,
                        structured_output=structured_output,
                        structured_output_error=structured_error,
                    )
                    saw_result = True
                    break
                continue

            if method == "turn/completed":
                for item_id, text in agent_message_buffers.items():
                    if item_id not in emitted_agent_items and text:
                        last_text = text
                        yield AssistantMessage(
                            content=[TextBlock(text=text)],
                            session_id=event_thread_id,
                        )
                turn = params.get("turn")
                status = str((turn or {}).get("status") or "") if isinstance(turn, dict) else ""
                error = (turn or {}).get("error") if isinstance(turn, dict) else None
                is_error = status == "failed"
                yield provider_event(
                    "turn_completed",
                    method=method,
                    params=params,
                    status=status or ("failed" if is_error else "completed"),
                )
                structured_output, structured_error = _codex_app_server_structured_output_result(last_text, opts)
                yield ResultMessage(
                    subtype="error" if is_error else "success",
                    is_error=is_error,
                    session_id=event_thread_id,
                    result=(
                        str((error or {}).get("message") or "codex app-server turn failed")
                        if is_error and isinstance(error, dict)
                        else last_text or None
                    ),
                    total_cost_usd=0.0,
                    usage=last_usage,
                    structured_output=structured_output,
                    structured_output_error=structured_error,
                )
                saw_result = True
                break

        if saw_result:
            return

        return_code = await process.wait()
        if not saw_result or return_code != 0:
            error_text = "\n".join(raw_lines[-20:]) or f"codex app-server exited with code {return_code}"
            if state is not None:
                state["provider_stderr"] = error_text
            yield ResultMessage(
                subtype="error",
                is_error=True,
                session_id=state.get("session_id", "") if state else "",
                result=error_text,
                total_cost_usd=0.0,
                usage=last_usage,
            )
    finally:
        await _terminate_provider_process(process)


async def _query_codex(
    *,
    prompt: str,
    options: AgentOptions | None = None,
    state: dict[str, Any] | None = None,
):
    opts = options or AgentOptions()
    env = dict(opts.env) if opts.env is not None else None

    try:
        process = await asyncio.create_subprocess_exec(
            *_codex_command(opts),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=opts.cwd or None,
            env=env,
            limit=CODEX_STDIO_LIMIT_BYTES,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "codex CLI not found in PATH; install it from https://github.com/openai/codex"
        ) from exc
    if state is not None:
        pid = getattr(process, "pid", None)
        if isinstance(pid, int):
            _remember_agent_process(state, pid)

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
    compact_event_lines: list[str] = []
    emitted_collab_tool_ids: set[str] = set()
    child_tool_use_by_thread_id: dict[str, str] = {}

    wait_task = asyncio.create_task(process.wait())
    try:
        while True:
            raw_line = await stdout.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                compact_event_lines.append(_truncate_for_agent_log(line, 2_000))
                if state is not None:
                    state["provider_stderr"] = "\n".join(compact_event_lines[-50:])
                continue
            compact_event_lines.append(_compact_codex_event_for_error(event))
            if state is not None:
                state["provider_stderr"] = "\n".join(compact_event_lines[-50:])

            event_type = event.get("type")
            if event_type == "thread.started":
                session_id = str(event.get("thread_id", "") or "")
                if state is not None and session_id:
                    state["session_id"] = session_id
                continue

            item = event.get("item") or {}
            item_type = item.get("type")
            if item_type == "agent_message" and event_type == "item.completed":
                text = str(item.get("text", "") or "")
                if text:
                    last_text = text
                    yield AssistantMessage(content=[TextBlock(text=text)], session_id=session_id)
                continue

            if item_type == "command_execution":
                item_id = str(item.get("id", "") or "") or None
                command = str(item.get("command", "") or "")
                if event_type == "item.started":
                    yield AssistantMessage(
                        content=[ToolUseBlock(name="Bash", input={"command": command}, id=item_id)],
                        session_id=session_id,
                    )
                    continue
                if event_type == "item.completed":
                    output = _truncate_for_agent_log(
                        str(item.get("aggregated_output", "") or ""),
                        CODEX_TOOL_OUTPUT_LOG_LIMIT_CHARS,
                    )
                    yield AssistantMessage(
                        content=[ToolResultBlock(content=output, tool_use_id=item_id)],
                        session_id=session_id,
                    )
                    continue

            if item_type == "collab_tool_call":
                item_id = str(item.get("id", "") or "") or None
                tool = str(item.get("tool", "") or "")
                receiver_thread_ids = [
                    str(child_id)
                    for child_id in (item.get("receiver_thread_ids") or [])
                    if child_id
                ]
                if state is not None and receiver_thread_ids:
                    existing_children = set(state.get("codex_child_session_ids", []) or [])
                    existing_children.update(receiver_thread_ids)
                    state["codex_child_session_ids"] = sorted(existing_children)

                if tool == "spawn_agent":
                    prompt_text = str(item.get("prompt", "") or "")
                    if item_id and event_type in {"item.started", "item.completed"}:
                        if item_id not in emitted_collab_tool_ids:
                            emitted_collab_tool_ids.add(item_id)
                            yield AssistantMessage(
                                content=[
                                    ToolUseBlock(
                                        name="Agent",
                                        input={
                                            "subagent_type": "codex",
                                            "prompt": prompt_text,
                                        },
                                        id=item_id,
                                    )
                                ],
                                session_id=session_id,
                            )
                    if item_id and event_type == "item.completed":
                        for child_id in receiver_thread_ids:
                            child_tool_use_by_thread_id[child_id] = item_id
                    continue

                if tool == "wait" and event_type == "item.completed":
                    for child_id in receiver_thread_ids:
                        content = _codex_collab_result_text(item, child_id)
                        if not content:
                            continue
                        yield AssistantMessage(
                            content=[
                                ToolResultBlock(
                                    content=content,
                                    tool_use_id=child_tool_use_by_thread_id.get(child_id) or item_id,
                                )
                            ],
                            session_id=child_id,
                        )
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
                break

        if saw_result:
            try:
                return_code = await asyncio.wait_for(
                    asyncio.shield(wait_task),
                    timeout=CODEX_POST_RESULT_EXIT_GRACE_S,
                )
            except asyncio.TimeoutError:
                return
            if return_code == 0:
                return
        else:
            return_code = await asyncio.shield(wait_task)

        if return_code != 0:
            error_lines = compact_event_lines[-20:]
            error_text = "\n".join(error_lines) or f"codex exited with code {return_code}"
            if state is not None:
                state["provider_stderr"] = error_text
            yield ResultMessage(
                subtype="error",
                is_error=True,
                session_id=session_id,
                result=error_text,
                total_cost_usd=0.0,
                usage=None,
            )
    finally:
        await _terminate_provider_process(process, wait_task=wait_task)


async def _terminate_provider_process(
    process: Any,
    *,
    grace_s: float = 2.0,
    wait_task: asyncio.Task[Any] | None = None,
) -> None:
    if getattr(process, "returncode", None) is not None:
        return
    wait_task = wait_task or asyncio.create_task(process.wait())

    _signal_provider_process(process, signal.SIGTERM)
    try:
        await asyncio.wait_for(asyncio.shield(wait_task), timeout=grace_s)
        return
    except asyncio.TimeoutError:
        _signal_provider_process(process, signal.SIGKILL)
        try:
            await asyncio.wait_for(asyncio.shield(wait_task), timeout=grace_s)
        except (asyncio.TimeoutError, ProcessLookupError):
            _cancel_provider_wait_task(wait_task)
            return
    except asyncio.CancelledError:
        _signal_provider_process(process, signal.SIGKILL)
        _cancel_provider_wait_task(wait_task)
        raise


def _cancel_provider_wait_task(wait_task: asyncio.Task[Any]) -> None:
    if wait_task.done():
        return
    wait_task.cancel()
    with contextlib.suppress(Exception):
        wait_task.get_loop().create_task(_drain_cancelled_task(wait_task))


async def _drain_cancelled_task(wait_task: asyncio.Task[Any]) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        await wait_task


def _signal_provider_process(process: Any, sig: signal.Signals) -> None:
    pid = getattr(process, "pid", None)
    if isinstance(pid, int):
        try:
            os.killpg(pid, sig)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    try:
        if sig == signal.SIGKILL:
            process.kill()
        else:
            process.terminate()
    except AttributeError:
        try:
            process.kill()
        except Exception:
            return
    except ProcessLookupError:
        return


async def query(
    *,
    prompt: str,
    options: AgentOptions | None = None,
    state: dict[str, Any] | None = None,
):
    """Run an agent query against the configured provider."""
    provider = _provider_name(options)
    if provider == "codex":
        async for message in _query_codex(prompt=prompt, options=options, state=state):
            yield message
        return
    if provider == "codex-app-server":
        async for message in _query_codex_app_server(prompt=prompt, options=options, state=state):
            yield message
        return
    if provider == "openai-agents":
        async for message in _query_openai_agents(prompt=prompt, options=options, state=state):
            yield message
        return

    async for message in _query_claude(prompt=prompt, options=options, state=state):
        yield message


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


async def run_agent_query(
    prompt: str,
    options: AgentOptions,
    *,
    on_text: Callable[[str], Any] | None = None,
    on_tool: Callable[[Any], Any] | None = None,
    on_tool_result: Callable[[Any], Any] | None = None,
    on_result: Callable[[Any], Any] | None = None,
    on_message: Callable[[Any], Any] | None = None,
    capture_tool_output: bool = False,
    state: dict[str, Any] | None = None,
    _raw_jsonl: Any | None = None,
    _raw_narrative: Any | None = None,
) -> tuple[str, float | None, Any]:
    """Run a provider query, dispatching normalized events to callbacks.

    If capture_tool_output=True, tool result content (including subagent output)
    is appended to the returned text. This is useful when the caller needs to
    parse structured markers from subagent output.

    If `on_message` is provided, it receives every normalized message before
    block-level dispatch. This is the hook session loggers use to stream
    both messages.jsonl and narrative.log.

    If `state` is provided, the function updates ``state["session_id"]`` as
    soon as a session_id is seen on any streamed message. This lets callers
    that cancel the task (e.g. on timeout) still recover the session_id for
    a resumable checkpoint.

    ``run_agent_with_timeout`` forwards a shared callback bag from the session
    logger. The private raw-log writers are consumed by ``on_message`` there,
    and are accepted here only so that debug-unredacted logging can pass
    through this layer unchanged for both providers.
    """
    _ = (_raw_jsonl, _raw_narrative)
    transcript = _TranscriptAccumulator(keep_tool_output=capture_tool_output)
    cost = 0.0
    result_msg = None
    subagent_dispatches = 0
    max_subagent_dispatches = getattr(options, "max_subagent_dispatches", None)

    query_kwargs: dict[str, Any] = {"prompt": prompt, "options": options}
    if state is not None:
        query_kwargs["state"] = state
    message_iter = query(**query_kwargs)

    async for message in message_iter:
        usage_cost = _usage_total_cost_usd(message)
        if usage_cost is not None:
            cost = max(cost, usage_cost)
            if state is not None:
                state["total_cost_usd"] = cost
        # Capture session_id eagerly. Every SDK message type carries it,
        # and we need it to build a resumable checkpoint even when the
        # stream is later cancelled (timeout) or crashes.
        if state is not None:
            sid = getattr(message, "session_id", "") or ""
            if sid:
                state["session_id"] = sid
                seen = state.setdefault("seen_session_ids", set())
                if isinstance(seen, set):
                    seen.add(sid)
                    state["child_session_ids"] = sorted(seen)

        if on_message is not None:
            try:
                on_message(message)
            except Exception:
                # Log writers must never kill the run.
                import logging
                logging.getLogger("otto.agent").exception("on_message handler failed")

        if isinstance(message, ResultMessage):
            result_msg = message
            raw_cost = getattr(message, "total_cost_usd", None)
            if isinstance(raw_cost, (int, float)):
                cost = max(cost, float(raw_cost))
                if state is not None:
                    state["total_cost_usd"] = cost
            if on_result:
                on_result(message)
        elif isinstance(message, (AssistantMessage, UserMessage)):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    if block.content:
                        transcript.add_tool_output(block.content)
                    if on_tool_result:
                        on_tool_result(block)
                elif isinstance(block, ThinkingBlock):
                    thinking = getattr(block, "thinking", "")
                    if thinking and on_text:
                        on_text(f"[thinking] {thinking}")
                elif isinstance(block, TextBlock) and block.text:
                    transcript.add_assistant_text(block.text)
                    if on_text:
                        on_text(block.text)
                elif isinstance(block, ToolUseBlock):
                    if block.name == "Agent":
                        subagent_dispatches += 1
                        if (
                            isinstance(max_subagent_dispatches, int)
                            and max_subagent_dispatches > 0
                            and subagent_dispatches > max_subagent_dispatches
                        ):
                            raise AgentCallError(
                                "max_subagent dispatch cap reached; check for agent loops",
                                session_id=(state or {}).get("session_id", ""),
                                total_cost_usd=cost,
                            )
                    if on_tool:
                        on_tool(block)

    if state is not None:
        legacy_ids = state.get("child_session_ids", []) or []
        app_server_ids = state.get("codex_child_session_ids", []) or []
        merged = {
            str(sid)
            for sid in [*legacy_ids, *app_server_ids]
            if str(sid or "").strip()
        }
        if merged:
            state["child_session_ids"] = sorted(merged)

    return transcript.finalize_text(), cost, result_msg
