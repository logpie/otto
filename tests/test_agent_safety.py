from __future__ import annotations

import asyncio

from otto.agent import (
    DEFAULT_DISALLOWED_BASH_TOOLS,
    _otto_can_use_tool_safety,
    _otto_pre_tool_safety_hook,
    _sdk_options,
    _unsafe_bash_command_reason,
    make_agent_options,
)


def test_agent_safety_blocks_broad_process_kills() -> None:
    assert "killall" in _unsafe_bash_command_reason("killall python3").lower()
    assert "pkill" in _unsafe_bash_command_reason("pkill -f app.py").lower()
    assert "direct kill" in _unsafe_bash_command_reason("kill -9 $(pgrep -f app.py)").lower()
    assert "direct kill" in _unsafe_bash_command_reason('do kill "$pid"; done').lower()
    assert "direct kill" in _unsafe_bash_command_reason(
        'kill $(ps aux | grep "[p]ython run.py" | awk \'{print $2}\')'
    ).lower()
    assert "direct kill" in _unsafe_bash_command_reason(
        "lsof -i :5000 | awk '{print $2}' | xargs kill -9"
    ).lower()
    assert "direct kill" in _unsafe_bash_command_reason(
        "ps aux | grep python | grep run.py | grep -v grep | awk '{print $2}' | xargs kill 2>/dev/null || true"
    ).lower()


def test_agent_safety_blocks_explicit_pid_kill() -> None:
    assert "direct kill" in _unsafe_bash_command_reason("kill 12345").lower()
    assert "direct kill" in _unsafe_bash_command_reason("kill -TERM 12345 2>/dev/null; sleep 1").lower()
    assert "direct kill" in _unsafe_bash_command_reason("/bin/kill -9 12345").lower()
    assert "direct kill" in _unsafe_bash_command_reason("command kill -9 12345").lower()


def test_default_agent_options_install_bash_safety_permission_callback(tmp_path) -> None:
    options = make_agent_options(tmp_path, {}, agent_type="certifier")

    assert options.can_use_tool is _otto_can_use_tool_safety
    for tool in DEFAULT_DISALLOWED_BASH_TOOLS:
        assert tool in (options.disallowed_tools or [])


def test_default_agent_safety_callback_reaches_sdk_options(tmp_path) -> None:
    options = make_agent_options(tmp_path, {}, agent_type="certifier")
    sdk_options = _sdk_options(options)

    assert sdk_options.can_use_tool is _otto_can_use_tool_safety
    assert sdk_options.permission_mode == "default"
    assert sdk_options.hooks is None


def test_bash_safety_callback_blocks_unsafe_command() -> None:
    result = asyncio.run(
        _otto_can_use_tool_safety("Bash", {"command": "killall python3"}, None)
    )

    assert getattr(result, "behavior", "") == "deny"
    assert "killall" in getattr(result, "message", "").lower()


def test_bash_safety_hook_shape_remains_supported() -> None:
    result = asyncio.run(
        _otto_pre_tool_safety_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "killall python3"},
            },
            None,
            {},
        )
    )

    assert result["decision"] == "block"
    assert "killall" in result["reason"].lower()
