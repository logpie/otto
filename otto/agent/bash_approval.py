"""Bash tool approval, deny rules, and process-kill safety hooks.

Provider agents cannot reliably know which local PIDs belong to Otto. This
module owns the regexes and SDK hooks/can_use_tool callback that block
``kill``/``pkill``/``killall`` invocations before they execute.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

try:
    from claude_agent_sdk.types import HookMatcher as _SDKHookMatcher
    from claude_agent_sdk.types import PermissionResultAllow as _SDKPermissionResultAllow
    from claude_agent_sdk.types import PermissionResultDeny as _SDKPermissionResultDeny
except ImportError:
    _SDKHookMatcher = None
    _SDKPermissionResultAllow = None
    _SDKPermissionResultDeny = None


DEFAULT_DISALLOWED_BASH_TOOLS = (
    "Bash(killall*)",
    "Bash(killall:*)",
    "Bash(pkill*)",
    "Bash(pkill:*)",
)


_KILL_COMMAND_RE = re.compile(r"(?<![\w./-])(?:/bin/)?(kill|pkill|killall)(?![\w./-])")
_COMMAND_SPLIT_RE = re.compile(r"(?:&&|\|\||;|\n)")


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
