"""Codex / Codex-app-server / OpenAI-Agents subprocess harness.

This module owns provider-specific subprocess plumbing: building the codex
CLI command, projecting Otto's neutral AgentOptions into provider-shaped
params, normalizing provider events into the shared event dataclasses, and
managing subprocess lifecycle (start, signal, terminate).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import signal
from pathlib import Path
from typing import Any

from otto.agent.bash_approval import _unsafe_bash_command_reason
from otto.agent.events import (
    AgentOptions,
    AssistantMessage,
    ProviderEventMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    _json_safe,
    _raw_value,
    _structured_output_to_text,
    _truncate_for_agent_log,
)

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
    _OPENAI_AGENTS_IMPORT_ERROR_MESSAGE = ""
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


CODEX_STDIO_LIMIT_BYTES = 16 * 1024 * 1024
CODEX_POST_RESULT_EXIT_GRACE_S = 0.25
CODEX_APP_SERVER_RECONNECT_GRACE_S = 120.0
CODEX_APP_SERVER_DELTA_PROGRESS_INTERVAL_S = 2.0
CODEX_APP_SERVER_DELTA_PROGRESS_CHARS = 800
CODEX_TOOL_OUTPUT_LOG_LIMIT_CHARS = 20_000
CODEX_PROVIDER_ERROR_OUTPUT_LIMIT_CHARS = 1_200


def _codex_app_server_reconnect_grace_s() -> float:
    raw = os.environ.get("OTTO_CODEX_APP_SERVER_RECONNECT_GRACE_S")
    if raw is None:
        return CODEX_APP_SERVER_RECONNECT_GRACE_S
    try:
        value = float(raw)
    except ValueError:
        return CODEX_APP_SERVER_RECONNECT_GRACE_S
    return max(value, 0.001)


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
    agent_delta_progress: dict[str, tuple[float, int]] = {}
    emitted_agent_items: set[str] = set()
    emitted_collab_tool_ids: set[str] = set()
    child_tool_use_by_thread_id: dict[str, str] = {}
    reconnect_error: dict[str, Any] | None = None
    reconnect_deadline: float | None = None

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

    async def read_event(timeout_s: float | None = None) -> dict[str, Any] | None:
        while True:
            if timeout_s is None:
                raw_line = await stdout.readline()
            else:
                raw_line = await asyncio.wait_for(stdout.readline(), timeout=timeout_s)
            if not raw_line:
                return None
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
            if isinstance(event, dict):
                return event
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
            read_timeout: float | None = None
            if reconnect_deadline is not None:
                read_timeout = max(reconnect_deadline - asyncio.get_running_loop().time(), 0.001)
            try:
                event = await read_event(read_timeout)
            except asyncio.TimeoutError:
                error = reconnect_error or {}
                message = str(error.get("message") or "codex app-server stream stalled after reconnect")
                timeout_message = (
                    "codex app-server stream stalled after recoverable error: "
                    f"{message}. No provider events arrived for "
                    f"{_codex_app_server_reconnect_grace_s():.0f}s."
                )
                if state is not None:
                    state["provider_stderr"] = timeout_message
                yield ResultMessage(
                    subtype="error",
                    is_error=True,
                    session_id=thread_id,
                    result=timeout_message,
                    usage=last_usage,
                    total_cost_usd=0.0,
                )
                saw_result = True
                break
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
            if method != "error" and reconnect_deadline is not None:
                reconnect_error = None
                reconnect_deadline = None

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

            if method == "error":
                error = params.get("error") if isinstance(params.get("error"), dict) else {}
                message = str((error or {}).get("message") or "codex app-server error")
                will_retry = bool(params.get("willRetry"))
                yield provider_event(
                    "provider_error",
                    method=method,
                    params=params,
                    status="retrying" if will_retry else "failed",
                    data={
                        "message": message,
                        "will_retry": will_retry,
                        "additional_details": str(params.get("additionalDetails") or ""),
                    },
                )
                if will_retry:
                    reconnect_error = {
                        "message": message,
                        "additional_details": str(params.get("additionalDetails") or ""),
                    }
                    reconnect_deadline = (
                        asyncio.get_running_loop().time()
                        + _codex_app_server_reconnect_grace_s()
                    )
                    continue
                yield ResultMessage(
                    subtype="error",
                    is_error=True,
                    session_id=event_thread_id,
                    result=message,
                    total_cost_usd=0.0,
                    usage=last_usage,
                )
                saw_result = True
                break

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
                    buffered = agent_message_buffers[item_id]
                    now = asyncio.get_running_loop().time()
                    last_at, last_len = agent_delta_progress.get(item_id, (0.0, 0))
                    if (
                        len(buffered) - last_len >= CODEX_APP_SERVER_DELTA_PROGRESS_CHARS
                        or now - last_at >= CODEX_APP_SERVER_DELTA_PROGRESS_INTERVAL_S
                    ):
                        agent_delta_progress[item_id] = (now, len(buffered))
                        yield provider_event(
                            "agent_message_delta",
                            method=method,
                            params=params,
                            data={
                                "chars": len(buffered),
                                "preview": _truncate_for_agent_log(buffered[-240:], 240),
                            },
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
