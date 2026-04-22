"""Tests for provider-aware agent execution."""


import pytest

from otto.agent import (
    AgentCallError,
    AgentOptions,
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    _codex_command,
    make_agent_options,
    query,
    run_agent_query,
    run_agent_with_timeout,
)
from otto.markers import parse_certifier_markers


class _FakeStdout:
    def __init__(self, lines: list[str]):
        self._lines = [line.encode("utf-8") for line in lines]

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""


class _FakeStdin:
    def __init__(self):
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, lines: list[str], return_code: int = 0):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)
        self._return_code = return_code
        self.returncode: int | None = None
        self.pid = 999999999

    async def wait(self) -> int:
        self.returncode = self._return_code
        return self._return_code

    def kill(self) -> None:
        pass


@pytest.mark.asyncio
async def test_codex_query_normalizes_full_json_event_set(tmp_path, monkeypatch):
    seen: dict[str, object] = {}
    process = _FakeProcess([
        '{"type":"thread.started","thread_id":"thread-123"}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":"Planning..."}}\n',
        '{"type":"item.completed","item":{"type":"thinking","text":"Need context first"}}\n',
        '{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"/bin/zsh -lc \\"ls -1\\""}}\n',
        '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"/bin/zsh -lc \\"ls -1\\"","aggregated_output":"README.md\\n"}}\n',
        '{"type":"item.started","item":{"id":"read-1","type":"tool_use","name":"read_file","input":{"file_path":"README.md"}}}\n',
        '{"type":"item.completed","item":{"id":"read-1","type":"tool_result","tool_use_id":"read-1","content":"# hello"}}\n',
        '{"type":"item.started","item":{"call_id":"web-1","type":"function_call","name":"web_search","arguments":"{\\"query\\": \\"docs\\"}"}}\n',
        '{"type":"item.completed","item":{"call_id":"web-1","type":"function_output","output":"Found docs"}}\n',
        '{"type":"item.started","item":{"id":"agent-1","type":"thread_spawn","agent_type":"worker","prompt":"Investigate the flaky test"}}\n',
        '{"type":"item.completed","item":{"id":"agent-1","type":"thread_spawn","output":"Subagent done"}}\n',
        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":3}}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    messages = []
    async for message in query(
        prompt="List files",
        options=ClaudeAgentOptions(
            provider="codex",
            cwd=str(tmp_path),
            permission_mode="bypassPermissions",
        ),
    ):
        messages.append(message)

    assert [type(m) for m in messages] == [
        AssistantMessage,
        AssistantMessage,
        AssistantMessage,
        UserMessage,
        AssistantMessage,
        UserMessage,
        AssistantMessage,
        UserMessage,
        AssistantMessage,
        UserMessage,
        ResultMessage,
    ]
    assert isinstance(messages[0].content[0], TextBlock)
    assert messages[0].content[0].text == "Planning..."
    assert messages[1].content[0].text == "[thinking] Need context first"
    assert isinstance(messages[2].content[0], ToolUseBlock)
    assert "ls -1" in messages[2].content[0].input["command"]
    assert messages[2].content[0].id == "item_1"
    assert isinstance(messages[3].content[0], ToolResultBlock)
    assert messages[3].content[0].content == "README.md\n"
    assert messages[3].content[0].tool_use_id == "item_1"
    assert messages[4].content[0].name == "Read"
    assert messages[4].content[0].input["file_path"] == "README.md"
    assert messages[5].content[0].content == "# hello"
    assert messages[6].content[0].name == "WebFetch"
    assert messages[6].content[0].input["query"] == "docs"
    assert messages[7].content[0].content == "Found docs"
    assert messages[8].content[0].name == "Agent"
    assert messages[8].content[0].input["subagent_type"] == "worker"
    assert "Investigate the flaky test" in messages[8].content[0].input["prompt"]
    assert messages[9].content[0].content == "Subagent done"
    assert messages[10].session_id == "thread-123"
    assert messages[10].usage == {
        "input_tokens": 10,
        "output_tokens": 3,
        "provider": "codex",
    }
    assert messages[10].total_cost_usd is None
    assert process.stdin.buffer.decode("utf-8") == "List files"
    assert process.stdin.closed is True

    args = seen["args"]
    assert args[:4] == ("codex", "--search", "exec", "--json")
    assert "--dangerously-bypass-approvals-and-sandbox" in args
    assert "-C" in args
    assert str(tmp_path) in args


@pytest.mark.asyncio
async def test_codex_query_captures_session_id_on_thread_started(tmp_path, monkeypatch):
    process = _FakeProcess([
        '{"type":"thread.started","thread_id":"thread-789"}\n',
    ], return_code=1)

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    state: dict[str, object] = {}
    messages = []
    async for message in query(
        prompt="List files",
        options=ClaudeAgentOptions(provider="codex", cwd=str(tmp_path)),
        state=state,
    ):
        messages.append(message)

    assert state["session_id"] == "thread-789"
    assert state["child_session_ids"] == ["thread-789"]
    assert messages[-1].session_id == "thread-789"
    assert messages[-1].is_error is True


@pytest.mark.asyncio
async def test_codex_query_warns_on_unknown_event_type(tmp_path, monkeypatch, caplog):
    process = _FakeProcess([
        '{"type":"thread.started","thread_id":"thread-999"}\n',
        '{"type":"totally.unknown","payload":"x"}\n',
        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    with caplog.at_level("WARNING", logger="otto.agent"):
        async for _message in query(
            prompt="List files",
            options=ClaudeAgentOptions(provider="codex", cwd=str(tmp_path)),
        ):
            pass

    assert "Unhandled codex event type: totally.unknown" in caplog.text


@pytest.mark.asyncio
async def test_codex_query_ignores_lifecycle_event_types_without_warning(tmp_path, monkeypatch, caplog):
    process = _FakeProcess([
        '{"type":"thread.started","thread_id":"thread-321"}\n',
        '{"type":"turn.started","turn_id":"turn-1"}\n',
        '{"type":"item.started","item":{"id":"item-1","type":"message_progress"}}\n',
        '{"type":"item.updated","item":{"id":"item-1","type":"message_progress","delta":"working"}}\n',
        '{"type":"item.completed","item":{"id":"item-1","type":"message_progress"}}\n',
        '{"type":"thread.completed","thread_id":"thread-321"}\n',
        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    with caplog.at_level("WARNING", logger="otto.agent"):
        async for _message in query(
            prompt="List files",
            options=ClaudeAgentOptions(provider="codex", cwd=str(tmp_path)),
        ):
            pass

    assert "Unhandled codex event type" not in caplog.text


@pytest.mark.asyncio
async def test_codex_query_uses_thread_completed_usage_when_turn_result_is_missing(tmp_path, monkeypatch):
    process = _FakeProcess([
        '{"type":"thread.started","thread_id":"thread-654"}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":"Done."}}\n',
        '{"type":"thread.completed","usage":{"input_tokens":9,"output_tokens":4}}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    messages = []
    async for message in query(
        prompt="List files",
        options=ClaudeAgentOptions(provider="codex", cwd=str(tmp_path)),
    ):
        messages.append(message)

    assert isinstance(messages[-1], ResultMessage)
    assert messages[-1].usage == {
        "input_tokens": 9,
        "output_tokens": 4,
        "provider": "codex",
    }
    assert messages[-1].result == "Done."


@pytest.mark.asyncio
async def test_codex_query_prepends_project_claude_md(tmp_path, monkeypatch):
    (tmp_path / "CLAUDE.md").write_text("Prefer focused tests.\n")
    process = _FakeProcess([
        '{"type":"thread.started","thread_id":"thread-456"}\n',
        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    async for _message in query(
        prompt="Fix the bug",
        options=ClaudeAgentOptions(
            provider="codex",
            cwd=str(tmp_path),
            setting_sources=["project"],
        ),
    ):
        pass

    sent_prompt = process.stdin.buffer.decode("utf-8")
    assert "Project instructions from CLAUDE.md:" in sent_prompt
    assert "Prefer focused tests." in sent_prompt
    assert sent_prompt.endswith("Fix the bug")


def test_codex_resume_command_uses_resume_subcommand_shape():
    command = _codex_command(ClaudeAgentOptions(
        provider="codex",
        resume="thread-123",
        cwd="/tmp/project",
        model="gpt-5.4",
        permission_mode="bypassPermissions",
    ))

    assert command[:5] == ["codex", "--search", "exec", "resume", "--json"]
    assert "--dangerously-bypass-approvals-and-sandbox" in command
    assert "--color" not in command
    assert "-C" not in command
    assert command[-2:] == ["thread-123", "-"]


def test_codex_command_maps_supported_options_and_webfetch():
    command = _codex_command(ClaudeAgentOptions(
        provider="codex",
        cwd="/tmp/project",
        permission_mode="bypassPermissions",
        effort="high",
        max_turns=7,
        disallowed_tools=["Read", "Write"],
    ))

    assert command[:3] == ["codex", "--search", "-c"]
    assert 'model_reasoning_effort="high"' in command
    assert "num_turns=7" in command
    assert 'disabled_tools=["Read", "Write"]' in command


def test_codex_command_omits_search_when_webfetch_is_disallowed():
    command = _codex_command(ClaudeAgentOptions(
        provider="codex",
        disallowed_tools=["WebFetch"],
    ))

    assert "--search" not in command


def test_codex_command_rejects_unsupported_mcp_servers():
    with pytest.raises(NotImplementedError, match="mcp_servers"):
        _codex_command(ClaudeAgentOptions(
            provider="codex",
            mcp_servers={"github": {"command": ["mcp-github"]}},
        ))


def test_make_agent_options_cli_overrides_beat_per_agent_yaml(tmp_path):
    config = {
        "provider": "claude",
        "model": "sonnet",
        "effort": "medium",
        "agents": {
            "build": {"provider": "codex", "model": "gpt-5.3", "effort": "high"},
        },
        "_cli_overrides": {
            "provider": "claude",
            "model": "haiku",
            "effort": "low",
        },
    }

    options = make_agent_options(tmp_path, config, agent_type="build")

    assert options.provider == "claude"
    assert options.model == "haiku"
    assert options.effort == "low"


def test_make_agent_options_sets_default_max_turns(tmp_path):
    options = make_agent_options(tmp_path, {})

    assert options.max_turns == 200
    assert options.max_subagent_dispatches == 160


@pytest.mark.asyncio
async def test_run_agent_query_streams_markers_without_retaining_full_tool_blob(monkeypatch):
    huge_blob = "x" * 100_000

    async def fake_query(*, prompt, options=None, state=None):
        yield AssistantMessage(content=[
            TextBlock(text="Planning"),
            ToolResultBlock(
                content=f"STORY_RESULT: smoke | PASS | ok\nVERDICT: PASS\n{huge_blob}",
                tool_use_id="t1",
            ),
        ])
        yield ResultMessage(total_cost_usd=0.1)

    monkeypatch.setattr("otto.agent._query_claude", fake_query)

    text, _cost, _result = await run_agent_query(
        "test",
        AgentOptions(),
        capture_tool_output=True,
    )

    assert "STORY_RESULT: smoke | PASS | ok" in text
    assert "VERDICT: PASS" in text
    assert huge_blob not in text
    assert len(text) < 60_000


@pytest.mark.asyncio
async def test_codex_missing_binary_surfaces_provider_specific_hint(tmp_path, monkeypatch):
    async def missing_codex(*args, **kwargs):
        raise FileNotFoundError("codex")

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", missing_codex)

    with pytest.raises(RuntimeError, match="codex CLI not found in PATH"):
        async for _message in query(
            prompt="List files",
            options=ClaudeAgentOptions(provider="codex", cwd=str(tmp_path)),
        ):
            pass


@pytest.mark.asyncio
async def test_run_agent_query_strips_duplicate_certify_round_recap(monkeypatch):
    round_report = (
        "CERTIFY_ROUND: 1\n"
        "STORIES_TESTED: 1\n"
        "STORIES_PASSED: 0\n"
        "STORY_RESULT: smoke | FAIL | first round failed\n"
        "VERDICT: FAIL\n"
        "DIAGNOSIS: first round failed\n"
        "\n"
        "CERTIFY_ROUND: 2\n"
        "STORIES_TESTED: 1\n"
        "STORIES_PASSED: 1\n"
        "STORY_RESULT: smoke | PASS | second round passed\n"
        "VERDICT: PASS\n"
        "DIAGNOSIS: fixed\n"
    )

    async def fake_query(*, prompt, options=None, state=None):
        yield AssistantMessage(content=[
            ToolResultBlock(content=round_report, tool_use_id="t1"),
        ])
        yield AssistantMessage(content=[
            TextBlock(text=f"Certifier passed. Here are the results:\n\n{round_report}"),
        ])
        yield ResultMessage(total_cost_usd=0.1)

    monkeypatch.setattr("otto.agent._query_claude", fake_query)

    text, _cost, _result = await run_agent_query(
        "test",
        AgentOptions(),
        capture_tool_output=True,
    )

    parsed = parse_certifier_markers(text)
    assert [round_data["round"] for round_data in parsed.certify_rounds] == [1, 2]
    assert parsed.verdict_pass is True
    assert [story["story_id"] for story in parsed.stories] == ["smoke"]
    assert "Certifier passed. Here are the results:" in text


@pytest.mark.asyncio
async def test_run_agent_query_limits_subagent_dispatches(monkeypatch):
    async def fake_query(*, prompt, options=None, state=None):
        for idx in range(3):
            yield AssistantMessage(content=[ToolUseBlock(name="Agent", input={"prompt": f"round {idx}"})])
        yield ResultMessage(total_cost_usd=0.1)

    monkeypatch.setattr("otto.agent._query_claude", fake_query)

    with pytest.raises(AgentCallError, match="max_subagent dispatch cap reached"):
        await run_agent_query(
            "test",
            AgentOptions(max_subagent_dispatches=2),
        )


@pytest.mark.asyncio
async def test_run_agent_with_timeout_raises_on_error_result(tmp_path, monkeypatch):
    result = ResultMessage(
        subtype="error",
        is_error=True,
        session_id="sid-1",
        result="provider-side failure",
    )

    async def fake_run_agent_query(*args, **kwargs):
        on_message = kwargs.get("on_message")
        if on_message is not None:
            on_message(result)
        return "", 0.0, result

    monkeypatch.setattr("otto.agent.run_agent_query", fake_run_agent_query)

    with pytest.raises(AgentCallError, match="provider-side failure"):
        await run_agent_with_timeout(
            "test",
            AgentOptions(),
            log_dir=tmp_path,
            timeout=30,
            project_dir=tmp_path,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "backend_attr"),
    [("claude", "_query_claude"), ("codex", "_query_codex")],
)
async def test_run_agent_with_timeout_supports_debug_unredacted_for_all_providers(
    tmp_path,
    monkeypatch,
    provider,
    backend_attr,
):
    session_id = f"{provider}-session"
    assistant_text = f"{provider} raw channel ok"

    async def fake_provider_query(*, prompt, options=None, state=None):
        assert prompt == "test"
        assert options is not None
        yield AssistantMessage(
            content=[TextBlock(text=assistant_text)],
            session_id=session_id,
        )
        yield ResultMessage(
            subtype="success",
            is_error=False,
            session_id=session_id,
            result=assistant_text,
            total_cost_usd=0.25,
            usage={"total_cost_usd": 0.25},
        )

    monkeypatch.setattr(f"otto.agent.{backend_attr}", fake_provider_query)

    text, cost, returned_session_id, _breakdown = await run_agent_with_timeout(
        "test",
        AgentOptions(provider=provider, debug_unredacted=True),
        log_dir=tmp_path / "build",
        timeout=30,
        project_dir=tmp_path,
    )

    assert text == assistant_text
    assert cost == 0.25
    assert returned_session_id == session_id
    assert (tmp_path / "build" / "messages.jsonl").exists()
    assert (tmp_path / "raw" / "messages.jsonl").exists()
    assert assistant_text in (tmp_path / "raw" / "messages.jsonl").read_text(encoding="utf-8")
