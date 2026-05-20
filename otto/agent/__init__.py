"""Otto agent utilities — provider abstraction, event normalization, helpers."""

from __future__ import annotations

import asyncio
import os
import traceback
from pathlib import Path
from typing import Any, Callable

from otto.observability import iso_timestamp, write_crash_artifact
from otto.token_usage import TOKEN_USAGE_KEYS

# Re-export event types, dataclasses, and normalization helpers.
from otto.agent.events import (
    AgentOptions,
    AssistantMessage,
    ClaudeAgentOptions,
    ProviderEventMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    _TranscriptAccumulator,
    _json_safe,
    _normalize_block,
    _normalize_message,
    _raw_value,
    _structured_output_to_text,
    _truncate_for_agent_log,
    _usage_total_cost_usd,
    tool_use_summary,
)

# Re-export bash approval / process-kill safety helpers.
from otto.agent.bash_approval import (
    DEFAULT_DISALLOWED_BASH_TOOLS,
    _COMMAND_SPLIT_RE,
    _KILL_COMMAND_RE,
    _default_agent_hooks,
    _merge_disallowed_tools,
    _otto_can_use_tool_safety,
    _otto_pre_tool_safety_hook,
    _permission_allow,
    _permission_deny,
    _unsafe_bash_command_reason,
    _unsafe_kill_segment_reason,
)

# Re-export codex / openai-agents subprocess harness symbols.
from otto.agent.codex import (
    CODEX_APP_SERVER_DELTA_PROGRESS_CHARS,
    CODEX_APP_SERVER_DELTA_PROGRESS_INTERVAL_S,
    CODEX_APP_SERVER_RECONNECT_GRACE_S,
    CODEX_POST_RESULT_EXIT_GRACE_S,
    CODEX_PROVIDER_ERROR_OUTPUT_LIMIT_CHARS,
    CODEX_STDIO_LIMIT_BYTES,
    CODEX_TOOL_OUTPUT_LOG_LIMIT_CHARS,
    _OPENAI_AGENTS_IMPORT_ERROR_MESSAGE,
    _OpenAIAgent,
    _OpenAIAgentOutputSchema,
    _OpenAICompactionCapability,
    _OpenAIExecCommandTool,
    _OpenAIFilesystemCapability,
    _OpenAIManifest,
    _OpenAIModelSettings,
    _OpenAIRunConfig,
    _OpenAIRunner,
    _OpenAISandboxAgent,
    _OpenAISandboxRunConfig,
    _OpenAISetDefaultOpenAIKey,
    _OpenAIShellCapability,
    _OpenAIUnixLocalSandboxClient,
    _cancel_provider_wait_task,
    _cleanup_agent_processes,
    _codex_app_server_approval_policy,
    _codex_app_server_approval_result,
    _codex_app_server_collab_result_text,
    _codex_app_server_command,
    _codex_app_server_diff_summary,
    _codex_app_server_granted_permissions,
    _codex_app_server_normalize_item,
    _codex_app_server_output_schema,
    _codex_app_server_reconnect_grace_s,
    _codex_app_server_safe_filesystem_permissions,
    _codex_app_server_safe_permission_base,
    _codex_app_server_safe_permission_root,
    _codex_app_server_sandbox_mode,
    _codex_app_server_sandbox_policy,
    _codex_app_server_strict_schema,
    _codex_app_server_structured_output,
    _codex_app_server_structured_output_result,
    _codex_app_server_thread_params,
    _codex_app_server_turn_params,
    _codex_app_server_usage_dict,
    _codex_auth_credentials,
    _codex_collab_result_text,
    _codex_command,
    _codex_compat_prelude,
    _codex_prompt,
    _codex_reasoning_effort,
    _codex_tool_compat_prelude,
    _compact_codex_event_for_error,
    _configure_openai_shell_tools,
    _drain_cancelled_task,
    _is_path_under,
    _json_schema_python_type,
    _json_schema_type_matches,
    _openai_agents_agent,
    _openai_agents_auth_error,
    _openai_agents_capabilities,
    _openai_agents_instructions,
    _openai_agents_manifest,
    _openai_agents_model_settings,
    _openai_agents_normalize_item,
    _openai_agents_output_type,
    _openai_agents_reasoning_effort,
    _openai_agents_reasoning_text,
    _openai_agents_run_config,
    _openai_agents_sandbox_config,
    _openai_agents_text_from_message_raw,
    _openai_agents_tool_input,
    _openai_agents_tool_name,
    _openai_agents_tool_output_text,
    _openai_agents_trace_enabled,
    _openai_agents_trace_sensitive,
    _openai_agents_usage_dict,
    _query_codex,
    _query_codex_app_server,
    _query_openai_agents,
    _remember_agent_process,
    _safe_read,
    _signal_provider_process,
    _terminate_provider_process,
    _validate_json_schema_subset,
)

_SDK_IMPORT_ERROR_MESSAGE = ""

_CLAUDE_ENV_LOCK = asyncio.Lock()

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


def is_transient_provider_error(exc: BaseException) -> bool:
    """Return true for provider transport failures that are safe to retry upstream."""
    if not isinstance(exc, AgentCallError):
        return False
    haystack = " ".join(
        str(part or "")
        for part in (
            exc.reason,
            exc.last_provider_stderr,
            exc.traceback_text,
        )
    ).lower()
    return (
        "stream stalled after recoverable error" in haystack
        or "reconnecting..." in haystack
    )


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
        _append_narrative(f"━━━ Timed out after {timeout}s")
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
        _append_narrative("━━━ Agent run cancelled")
        _cleanup_agent_processes(project_dir, agent_state)
        raise
    except KeyboardInterrupt:
        _append_narrative("━━━ KeyboardInterrupt")
        _cleanup_agent_processes(project_dir, agent_state)
        raise
    except Exception as exc:
        log.exception("Agent crashed")
        _append_narrative(f"━━━ Agent crashed: {exc}")
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
