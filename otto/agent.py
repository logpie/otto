"""Otto agent utilities — provider abstraction, event normalization, helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import tempfile
import traceback
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from otto.costs import build_cost_payload, normalize_usage
from otto.observability import iso_timestamp, write_crash_artifact

_SDK_IMPORT_ERROR_MESSAGE = ""

try:
    from claude_agent_sdk import ClaudeAgentOptions as _SDKClaudeAgentOptions
    from claude_agent_sdk import query as _sdk_query
    from claude_agent_sdk.types import AssistantMessage as _SDKAssistantMessage
    from claude_agent_sdk.types import ResultMessage as _SDKResultMessage
    from claude_agent_sdk.types import TextBlock as _SDKTextBlock
    from claude_agent_sdk.types import ToolResultBlock as _SDKToolResultBlock
    from claude_agent_sdk.types import ToolUseBlock as _SDKToolUseBlock
except ImportError:
    import sys

    _SDK_IMPORT_ERROR_MESSAGE = str(sys.exc_info()[1] or "")
    _SDKClaudeAgentOptions = None
    _sdk_query = None
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
    max_subagent_dispatches: int | None = None
    debug_unredacted: bool | None = None


# Backward-compatible name used throughout the codebase and tests.
ClaudeAgentOptions = AgentOptions

_CODEX_LIFECYCLE_EVENT_TYPES = frozenset({
    "thread.started",
    "thread.updated",
    "thread.completed",
    "turn.started",
    "turn.updated",
    "turn.completed",
    "item.started",
    "item.updated",
    "item.completed",
})
_CODEX_LIFECYCLE_EVENT_PREFIXES = (
    "thread.",
    "turn.",
    "item.",
)


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
        agent_model,
        agent_provider,
        get_max_rounds,
        get_max_turns_per_call,
    )
    opts = AgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        system_prompt={"type": "preset", "preset": "claude_code"},
        env=_subprocess_env(),
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
    opts.provider = agent_provider(cfg, agent_type)
    model = agent_model(cfg, agent_type)
    if model:
        opts.model = str(model)
    effort = agent_effort(cfg, agent_type)
    if effort:
        opts.effort = str(effort)
    return opts


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
        elif self._marker_lines:
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

        Keep the prose, but strip marker lines only when we've already seen
        round markers earlier in the transcript.
        """
        if (
            "CERTIFY_ROUND:" not in text
            or not any(line.startswith("CERTIFY_ROUND:") for line in self._marker_lines)
        ):
            return text

        prose_lines = [
            line for line in text.splitlines()
            if line.strip() and not line.strip().startswith(
                (
                    "CERTIFY_ROUND:",
                    "STORIES_TESTED:",
                    "STORIES_PASSED:",
                    "DIAGNOSIS:",
                    "METRIC_VALUE:",
                    "METRIC_MET:",
                    "STORY_RESULT:",
                    "VERDICT:",
                )
            )
        ]
        return "\n".join(prose_lines)

    def _collect_markers(self, fragment: str) -> None:
        if not fragment:
            return
        from otto.markers import _STORY_RESULT_RE, _VERDICT_RE

        separator = "\n" if self._carry and not fragment.startswith(("\n", "\r")) else ""
        combined = self._carry + separator + fragment
        lines = combined.splitlines(keepends=True)
        self._carry = ""
        for raw_line in lines:
            if not raw_line.endswith(("\n", "\r")):
                self._carry = raw_line
                continue
            stripped = raw_line.strip()
            if not stripped or stripped.startswith(">"):
                continue
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
    timeout: int | None,
    project_dir: Path,
    capture_tool_output: bool = False,
    on_terminal_event: Callable[[str], None] | None = None,
    verbose: bool = False,
    strict_mode: bool = False,
) -> tuple[str, float | None, str, dict[str, Any]]:
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

    heartbeat_task: asyncio.Task[None] | None = None
    if on_terminal_event is not None:
        async def _heartbeat() -> None:
            interval_s = 20
            while True:
                await asyncio.sleep(interval_s)
                if (asyncio.get_running_loop().time()
                        - narrative.last_terminal_event_monotonic()) < interval_s:
                    continue
                narrative.write_heartbeat(_fmt_elapsed(narrative.phase_elapsed_seconds()))

        heartbeat_task = asyncio.create_task(_heartbeat())

    try:
        text, cost, result_msg = await asyncio.wait_for(
            run_agent_query(prompt, options,
                            capture_tool_output=capture_tool_output,
                            state=agent_state,
                            **callbacks),
            timeout=timeout,
        )
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
            err = AgentCallError(
                str(reason),
                session_id=session_id,
                total_cost_usd=cost,
            )
            err.crash_path = _write_crash_details(err)
            err.last_events = jsonl_writer.last_records(20)
            err.last_provider_stderr = agent_state.get("provider_stderr", "") or ""
            raise err
        child_session_ids = [
            sid for sid in agent_state.get("child_session_ids", []) or []
            if sid and sid != session_id
        ]
        cost_payload = build_cost_payload(
            provider=_provider_name(options),
            total_cost_usd=cost,
            usage=getattr(result_msg, "usage", None),
        )
        breakdown_data = {
            "round_timings": narrative.round_timings(),
            "build_duration_s": narrative.build_duration_or_none(),
            "recovered_tool_errors": 0,
            "child_session_ids": child_session_ids,
            "cost": cost_payload,
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
            finalize_breakdown = {"spec": {"duration_s": narrative.elapsed_seconds()}}
            if isinstance(cost, (int, float)):
                finalize_breakdown["spec"]["cost_usd"] = float(cost)
        if phase == "build" and finalize_breakdown is not None:
            estimated_costs = (
                estimate_phase_costs(log_dir / "messages.jsonl", float(cost))
                if isinstance(cost, (int, float))
                else None
            )
            if estimated_costs:
                for phase_name, phase_costs in estimated_costs.items():
                    if phase_name in finalize_breakdown:
                        finalize_breakdown[phase_name].update(phase_costs)
        finalize_stats = narrative.finalize(finalize_breakdown)
        breakdown_data["recovered_tool_errors"] = int(
            finalize_stats.get("recovered_tool_errors", 0)
        )
        return text, cost, session_id, breakdown_data
    except AgentCallError as err:
        from otto.pipeline import _cleanup_orphan_processes

        _cleanup_orphan_processes(
            project_dir,
            process_group_id=agent_state.get("process_group_id"),
        )
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
        from otto.pipeline import _cleanup_orphan_processes
        _cleanup_orphan_processes(
            project_dir,
            process_group_id=agent_state.get("process_group_id"),
        )
        err = AgentCallError(
            f"Timed out after {timeout}s",
            session_id=agent_state.get("session_id", ""),
            total_cost_usd=agent_state.get("total_cost_usd"),
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
    except KeyboardInterrupt:
        _append_narrative("\u2501\u2501\u2501 KeyboardInterrupt")
        from otto.pipeline import _cleanup_orphan_processes
        _cleanup_orphan_processes(
            project_dir,
            process_group_id=agent_state.get("process_group_id"),
        )
        raise
    except Exception as exc:
        log.exception("Agent crashed")
        _append_narrative(f"\u2501\u2501\u2501 Agent crashed: {exc}")
        from otto.pipeline import _cleanup_orphan_processes
        _cleanup_orphan_processes(
            project_dir,
            process_group_id=agent_state.get("process_group_id"),
        )
        tb = traceback.format_exc()
        err = AgentCallError(
            f"Agent crashed: {exc}",
            session_id=agent_state.get("session_id", ""),
            total_cost_usd=agent_state.get("total_cost_usd"),
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
    elif isinstance(options.system_prompt, dict):
        preset = str(options.system_prompt.get("preset", "") or "").strip()
        prompt_type = str(options.system_prompt.get("type", "") or "").strip()
        if not (prompt_type == "preset" and preset == "claude_code"):
            raise NotImplementedError(
                "codex provider does not support structured system_prompt presets "
                f"({options.system_prompt!r})"
            )
    compat = _codex_compat_prelude(options)
    if compat:
        parts.append(compat)
    parts.append(prompt)
    return "\n\n".join(part for part in parts if part).strip()


def _remember_session_id(state: dict[str, Any] | None, session_id: str) -> None:
    if state is None or not session_id:
        return
    state["session_id"] = session_id
    seen = state.setdefault("seen_session_ids", set())
    if isinstance(seen, set):
        seen.add(session_id)
        state["child_session_ids"] = sorted(seen)


def _codex_event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("params")
    if isinstance(payload, dict):
        return payload
    return event


def _codex_event_name(event: dict[str, Any]) -> str:
    raw = event.get("type") or event.get("method") or ""
    return str(raw or "").replace("/", ".").strip()


def _codex_event_item(event: dict[str, Any]) -> dict[str, Any]:
    payload = _codex_event_payload(event)
    item = payload.get("item")
    return item if isinstance(item, dict) else {}


def _codex_first_nonempty_str(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return ""


def _codex_string_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_codex_string_content(part) for part in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "content", "output_text", "summary_text", "reasoning_text", "value"):
            if key in value:
                text = _codex_string_content(value.get(key))
                if text:
                    return text
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            return str(value)
    if value is None:
        return ""
    return str(value)


def _codex_item_type(item: dict[str, Any], payload: dict[str, Any]) -> str:
    return str(
        item.get("type")
        or payload.get("item_type")
        or payload.get("itemType")
        or ""
    ).replace("/", ".").strip()


def _codex_item_id(item: dict[str, Any], payload: dict[str, Any]) -> str | None:
    raw = (
        item.get("id")
        or item.get("item_id")
        or item.get("tool_use_id")
        or item.get("call_id")
        or payload.get("call_id")
        or payload.get("tool_use_id")
        or payload.get("item_id")
    )
    return str(raw) if raw else None


def _codex_parse_input(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return {"input": raw}
        if isinstance(parsed, dict):
            return parsed
        return {"input": parsed}
    return {}


def _codex_tool_input(item: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    for candidate in (
        item.get("input"),
        item.get("arguments"),
        payload.get("input"),
        payload.get("arguments"),
    ):
        parsed = _codex_parse_input(candidate)
        if parsed:
            return parsed
    if isinstance(item.get("function"), dict):
        parsed = _codex_parse_input(item["function"].get("arguments"))
        if parsed:
            return parsed
    return {}


def _codex_tool_name(item: dict[str, Any], payload: dict[str, Any]) -> str:
    raw_name = _codex_first_nonempty_str(
        item.get("name"),
        item.get("tool_name"),
        item.get("toolName"),
        item.get("namespace"),
        payload.get("tool_name"),
        payload.get("toolName"),
        payload.get("namespace"),
    )
    item_type = _codex_item_type(item, payload)
    lowered = (raw_name or item_type).strip().lower()
    aliases = {
        "agent": "Agent",
        "collab_agent_spawn": "Agent",
        "spawn_agent": "Agent",
        "subagent": "Agent",
        "thread_spawn": "Agent",
        "bash": "Bash",
        "command_execution": "Bash",
        "exec_command": "Bash",
        "exec_command_begin": "Bash",
        "read": "Read",
        "read_file": "Read",
        "view": "Read",
        "write": "Write",
        "write_file": "Write",
        "edit": "Edit",
        "apply_patch": "Edit",
        "glob": "Glob",
        "list_files": "Glob",
        "grep": "Grep",
        "search": "Grep",
        "web_search": "WebFetch",
        "webfetch": "WebFetch",
        "open_page": "WebFetch",
        "find_in_page": "WebFetch",
        "view_image": "View",
    }
    if lowered in aliases:
        return aliases[lowered]
    return raw_name or item_type or "Tool"


def _codex_tool_result_content(item: dict[str, Any], payload: dict[str, Any]) -> str:
    for candidate in (
        item.get("aggregated_output"),
        item.get("output_text"),
        item.get("content"),
        item.get("output"),
        item.get("result"),
        payload.get("aggregated_output"),
        payload.get("output_text"),
        payload.get("content"),
        payload.get("output"),
        payload.get("result"),
        payload.get("structuredContent"),
        payload.get("structured_content"),
        payload.get("content_items"),
    ):
        text = _codex_string_content(candidate)
        if text:
            return text
    return ""


def _codex_tool_result_error(item: dict[str, Any], payload: dict[str, Any]) -> bool:
    raw = (
        item.get("is_error")
        or item.get("isError")
        or payload.get("is_error")
        or payload.get("isError")
    )
    return bool(raw)


def _codex_thinking_text(item: dict[str, Any], payload: dict[str, Any]) -> str:
    return _codex_first_nonempty_str(
        _codex_string_content(item.get("summary_text")),
        _codex_string_content(item.get("reasoning_text")),
        _codex_string_content(item.get("text")),
        _codex_string_content(item.get("content")),
        _codex_string_content(payload.get("summary_text")),
        _codex_string_content(payload.get("reasoning_text")),
        _codex_string_content(payload.get("text")),
        _codex_string_content(payload.get("delta")),
        _codex_string_content(payload.get("content")),
    )


def _codex_message_text(item: dict[str, Any], payload: dict[str, Any]) -> str:
    return _codex_first_nonempty_str(
        _codex_string_content(item.get("text")),
        _codex_string_content(item.get("delta")),
        _codex_string_content(item.get("content")),
        _codex_string_content(payload.get("text")),
        _codex_string_content(payload.get("delta")),
        _codex_string_content(payload.get("content")),
    )


def _codex_subagent_input(item: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    tool_input = _codex_tool_input(item, payload)
    if "prompt" not in tool_input:
        prompt = _codex_first_nonempty_str(
            _codex_string_content(item.get("prompt")),
            _codex_string_content(payload.get("prompt")),
        )
        if prompt:
            tool_input["prompt"] = prompt
    if "subagent_type" not in tool_input:
        subagent_type = _codex_first_nonempty_str(
            item.get("agent_type"),
            item.get("subagent_type"),
            payload.get("agent_type"),
            payload.get("subagent_type"),
            payload.get("new_agent_role"),
        )
        if subagent_type:
            tool_input["subagent_type"] = subagent_type
    return tool_input


def _codex_usage_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    payload = _codex_event_payload(event)
    for candidate in (
        payload.get("usage"),
        payload.get("token_usage"),
        event.get("usage"),
        event.get("token_usage"),
    ):
        usage = normalize_usage(candidate, provider="codex")
        if usage is not None:
            return usage
    return None


def _codex_usage_cost(usage: dict[str, Any] | None) -> float | None:
    if not isinstance(usage, dict):
        return None
    raw = usage.get("total_cost_usd")
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def _codex_is_lifecycle_event(event_type: str | None) -> bool:
    if not event_type:
        return False
    if event_type in _CODEX_LIFECYCLE_EVENT_TYPES:
        return True
    return any(event_type.startswith(prefix) for prefix in _CODEX_LIFECYCLE_EVENT_PREFIXES)


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
            usage=normalize_usage(getattr(message, "usage", None), provider="claude"),
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
            usage=normalize_usage(getattr(message, "usage", None), provider="claude"),
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
                usage=normalize_usage(getattr(message, "usage", None), provider="claude"),
            )
        return AssistantMessage(
            content=content,
            session_id=session_id,
            usage=normalize_usage(getattr(message, "usage", None), provider="claude"),
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
                state["process_group_id"] = pid
        return process

    os.environ.clear()
    os.environ.update(opts.env or {})
    try:
        if original_open_process is not None:
            _sdk_subprocess_cli.anyio.open_process = _open_process_with_session
        async for message in _sdk_query(prompt=prompt, options=sdk_options):
            normalized = _normalize_message(message)
            if normalized is not None:
                yield normalized
    finally:
        if original_open_process is not None:
            _sdk_subprocess_cli.anyio.open_process = original_open_process
        os.environ.clear()
        os.environ.update(saved_env)


def _codex_cli_config_args(options: AgentOptions) -> list[str]:
    args: list[str] = []
    if options.effort:
        effort = str(options.effort).strip().lower()
        effort = {"max": "xhigh"}.get(effort, effort)
        if effort not in {"low", "medium", "high", "xhigh"}:
            raise ValueError(f"Unsupported codex effort level: {options.effort!r}")
        # The installed Codex CLI exposes reasoning effort through config,
        # not a dedicated `exec` flag.
        args.extend(["-c", f"model_reasoning_effort={json.dumps(effort)}"])
    if options.max_turns is not None:
        args.extend(["-c", f"num_turns={int(options.max_turns)}"])
    if options.disallowed_tools:
        args.extend(["-c", f"disabled_tools={json.dumps(list(options.disallowed_tools))}"])
    if options.mcp_servers:
        raise NotImplementedError(
            "codex provider does not support per-call mcp_servers overrides via the installed CLI"
        )
    return args


def _codex_search_enabled(options: AgentOptions) -> bool:
    blocked = {str(tool).strip().lower() for tool in (options.disallowed_tools or []) if tool}
    return "webfetch" not in blocked and "web_fetch" not in blocked


def _codex_command(options: AgentOptions, *, output_schema_path: str | None = None) -> list[str]:
    command = ["codex"]
    if _codex_search_enabled(options):
        command.append("--search")
    command.extend(_codex_cli_config_args(options))
    command.append("exec")
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
    if output_schema_path:
        command.extend(["--output-schema", output_schema_path])
    if options.resume:
        command.append(options.resume)
    command.append("-")
    return command


async def _query_codex(
    *,
    prompt: str,
    options: AgentOptions | None = None,
    state: dict[str, Any] | None = None,
):
    opts = options or AgentOptions()
    env = dict(opts.env or {})
    logger = logging.getLogger("otto.agent")
    schema_path: str | None = None

    if opts.output_format is not None:
        fd, schema_path = tempfile.mkstemp(prefix="otto-codex-schema-", suffix=".json")
        os.close(fd)
        Path(schema_path).write_text(
            json.dumps(opts.output_format, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    try:
        process = await asyncio.create_subprocess_exec(
            *_codex_command(opts, output_schema_path=schema_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=opts.cwd or None,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "codex CLI not found in PATH; install it from https://github.com/openai/codex"
        ) from exc
    if state is not None:
        state["process_group_id"] = process.pid

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
    completion_usage: dict[str, Any] | None = None
    raw_lines: list[str] = []
    warned_unknown_events: set[str] = set()

    try:
        while True:
            raw_line = await stdout.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            raw_lines.append(line)
            if state is not None:
                state["provider_stderr"] = "\n".join(raw_lines[-50:])
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            payload = _codex_event_payload(event)
            event_type = _codex_event_name(event)

            if event_type == "thread.started":
                session_id = str(
                    payload.get("thread_id")
                    or payload.get("session_id")
                    or event.get("thread_id")
                    or ""
                )
                _remember_session_id(state, session_id)
                continue

            if event_type == "thread.completed":
                usage = _codex_usage_payload(event)
                if usage is not None:
                    completion_usage = usage
                continue

            item = _codex_event_item(event)
            item_type = _codex_item_type(item, payload)

            if event_type in {"agent_message", "agent_message_delta"}:
                text = _codex_message_text(item, payload)
                if text:
                    last_text = text
                    yield AssistantMessage(
                        content=[TextBlock(text=text)],
                        session_id=session_id,
                        usage=_codex_usage_payload(event),
                    )
                continue

            if item_type == "agent_message" and event_type == "item.completed":
                text = _codex_message_text(item, payload)
                if text:
                    last_text = text
                    yield AssistantMessage(
                        content=[TextBlock(text=text)],
                        session_id=session_id,
                        usage=_codex_usage_payload(event),
                    )
                continue

            if event_type in {
                "agent_reasoning",
                "agent_reasoning_delta",
                "agent_reasoning_raw_content",
                "agent_reasoning_raw_content_delta",
                "reasoning_content_delta",
            } or item_type in {"thinking", "reasoning"}:
                thinking = _codex_thinking_text(item, payload)
                if thinking:
                    yield AssistantMessage(
                        content=[TextBlock(text=f"[thinking] {thinking}")],
                        session_id=session_id,
                        usage=_codex_usage_payload(event),
                    )
                continue

            if event_type == "exec_command_begin" or item_type == "command_execution":
                # Repeated `git status` calls are typically Codex's own
                # repo-state probes. Otto just records streamed tool events.
                item_id = _codex_item_id(item, payload)
                command = _codex_first_nonempty_str(
                    _codex_string_content(item.get("command")),
                    _codex_string_content(payload.get("parsed_cmd")),
                    _codex_string_content(payload.get("command")),
                )
                if event_type in {"item.started", "exec_command_begin"}:
                    yield AssistantMessage(
                        content=[ToolUseBlock(name="Bash", input={"command": command}, id=item_id)],
                        session_id=session_id,
                        usage=_codex_usage_payload(event),
                    )
                    continue
                if event_type in {"item.completed", "exec_command_end"}:
                    output = _codex_tool_result_content(item, payload)
                    yield UserMessage(
                        content=[ToolResultBlock(content=output, tool_use_id=item_id)],
                        session_id=session_id,
                        usage=_codex_usage_payload(event),
                    )
                    continue

            if event_type in {"mcp_tool_call_begin", "dynamic_tool_call_request", "view_image_tool_call", "web_search_begin", "image_generation_begin", "collab_agent_spawn_begin"}:
                tool_payload = dict(payload)
                tool_payload.setdefault(
                    "tool_name",
                    event_type.removesuffix("_begin").removesuffix("_request"),
                )
                tool_name = _codex_tool_name(item, tool_payload)
                if tool_name == "Agent":
                    tool_input = _codex_subagent_input(item, tool_payload)
                else:
                    tool_input = _codex_tool_input(item, tool_payload)
                if tool_name and tool_name != "Bash":
                    yield AssistantMessage(
                        content=[ToolUseBlock(name=tool_name, input=tool_input, id=_codex_item_id(item, tool_payload))],
                        session_id=session_id,
                        usage=_codex_usage_payload(event),
                    )
                    continue

            if event_type in {"mcp_tool_call_end", "dynamic_tool_call_response", "web_search_end", "image_generation_end", "collab_agent_spawn_end"} and item_type in {"tool_result", "function_output", "tool_use", "function_call", "thread_spawn", "subagent"}:
                tool_payload = dict(payload)
                tool_payload.setdefault(
                    "tool_name",
                    event_type.removesuffix("_end").removesuffix("_response"),
                )
                tool_name = _codex_tool_name(item, tool_payload)
                if tool_name == "Agent" and event_type in {"item.completed", "collab_agent_spawn_end"}:
                    content = _codex_tool_result_content(item, tool_payload)
                    yield UserMessage(
                        content=[ToolResultBlock(content=content, tool_use_id=_codex_item_id(item, tool_payload), is_error=_codex_tool_result_error(item, tool_payload))],
                        session_id=session_id,
                        usage=_codex_usage_payload(event),
                    )
                    continue

            if event_type in {"mcp_tool_call_end", "dynamic_tool_call_response", "web_search_end", "image_generation_end", "collab_agent_spawn_end"}:
                tool_payload = dict(payload)
                tool_payload.setdefault(
                    "tool_name",
                    event_type.removesuffix("_end").removesuffix("_response"),
                )
                yield UserMessage(
                    content=[ToolResultBlock(
                        content=_codex_tool_result_content(item, tool_payload),
                        tool_use_id=_codex_item_id(item, tool_payload),
                        is_error=_codex_tool_result_error(item, tool_payload),
                    )],
                    session_id=session_id,
                    usage=_codex_usage_payload(event),
                )
                continue

            if item_type in {"tool_use", "function_call", "thread_spawn", "subagent"}:
                tool_name = _codex_tool_name(item, payload)
                tool_input = _codex_subagent_input(item, payload) if tool_name == "Agent" else _codex_tool_input(item, payload)
                if event_type == "item.started":
                    yield AssistantMessage(
                        content=[ToolUseBlock(name=tool_name, input=tool_input, id=_codex_item_id(item, payload))],
                        session_id=session_id,
                        usage=_codex_usage_payload(event),
                    )
                    continue
                if event_type == "item.completed":
                    content = _codex_tool_result_content(item, payload)
                    yield UserMessage(
                        content=[ToolResultBlock(content=content, tool_use_id=_codex_item_id(item, payload), is_error=_codex_tool_result_error(item, payload))],
                        session_id=session_id,
                        usage=_codex_usage_payload(event),
                    )
                    continue

            if item_type in {"tool_result", "function_output"}:
                yield UserMessage(
                    content=[ToolResultBlock(
                        content=_codex_tool_result_content(item, payload),
                        tool_use_id=_codex_item_id(item, payload),
                        is_error=_codex_tool_result_error(item, payload),
                    )],
                    session_id=session_id,
                    usage=_codex_usage_payload(event),
                )
                continue

            if event_type in {"turn.completed", "task_complete"}:
                saw_result = True
                usage = _codex_usage_payload(event) or completion_usage
                if usage is not None:
                    completion_usage = usage
                yield ResultMessage(
                    subtype="success",
                    is_error=False,
                    session_id=session_id,
                    result=last_text or None,
                    total_cost_usd=_codex_usage_cost(usage),
                    usage=usage,
                )
                continue

            if _codex_is_lifecycle_event(event_type):
                continue

            if event_type and event_type not in warned_unknown_events:
                warned_unknown_events.add(event_type)
                logger.warning("Unhandled codex event type: %s", event_type)

        return_code = await process.wait()
        if not saw_result and return_code == 0 and (completion_usage is not None or last_text):
            yield ResultMessage(
                subtype="success",
                is_error=False,
                session_id=session_id,
                result=last_text or None,
                total_cost_usd=_codex_usage_cost(completion_usage),
                usage=completion_usage,
            )
            saw_result = True
        if not saw_result or return_code != 0:
            error_lines = raw_lines[-20:]
            error_text = "\n".join(error_lines) or f"codex exited with code {return_code}"
            if state is not None:
                state["provider_stderr"] = error_text
            yield ResultMessage(
                subtype="error",
                is_error=True,
                session_id=session_id,
                result=error_text,
                total_cost_usd=None,
                usage=None,
            )
    finally:
        if process.returncode is None:
            try:
                if getattr(process, "pid", None):
                    os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
            await process.wait()
        if schema_path:
            try:
                Path(schema_path).unlink()
            except OSError:
                pass


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
    options: ClaudeAgentOptions,
    *,
    on_text: Callable[[str], Any] | None = None,
    on_tool: Callable[[Any], Any] | None = None,
    on_tool_result: Callable[[Any], Any] | None = None,
    on_result: Callable[[Any], Any] | None = None,
    on_message: Callable[[Any], Any] | None = None,
    capture_tool_output: bool = False,
    state: dict[str, Any] | None = None,
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
    """
    transcript = _TranscriptAccumulator(keep_tool_output=capture_tool_output)
    cost: float | None = None
    result_msg = None
    subagent_dispatches = 0
    max_subagent_dispatches = getattr(options, "max_subagent_dispatches", None)

    provider = _provider_name(options)
    query_kwargs: dict[str, Any] = {"prompt": prompt, "options": options}
    if state is not None:
        query_kwargs["state"] = state
    message_iter = query(**query_kwargs)

    async for message in message_iter:
        usage_cost = _usage_total_cost_usd(message)
        if usage_cost is not None:
            cost = max(cost, usage_cost) if cost is not None else usage_cost
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
                cost = max(cost, float(raw_cost)) if cost is not None else float(raw_cost)
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

    return transcript.finalize_text(), cost, result_msg
