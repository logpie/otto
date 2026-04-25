"""Tests for provider-aware agent execution."""

import asyncio

import os

import pytest

from otto.agent import (
    AgentCallError,
    AgentOptions,
    AssistantMessage,
    CODEX_STDIO_LIMIT_BYTES,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    make_agent_options,
    query,
    run_agent_query,
    run_agent_with_timeout,
    _terminate_provider_process,
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

    async def wait(self) -> int:
        self.returncode = self._return_code
        return self._return_code

    def kill(self) -> None:
        pass


class _SlowWaitProcess:
    def __init__(self):
        self.returncode: int | None = None
        self.signals: list[str] = []

    async def wait(self) -> int:
        await asyncio.sleep(60)
        return self.returncode or 0

    def terminate(self) -> None:
        self.signals.append("term")

    def kill(self) -> None:
        self.signals.append("kill")
        self.returncode = -9


@pytest.mark.asyncio
async def test_provider_process_cleanup_kills_again_when_wait_is_cancelled():
    process = _SlowWaitProcess()

    task = asyncio.create_task(_terminate_provider_process(process, grace_s=30.0))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.signals == ["term", "kill"]


@pytest.mark.asyncio
async def test_codex_query_normalizes_json_events(tmp_path, monkeypatch):
    seen: dict[str, object] = {}
    process = _FakeProcess([
        '{"type":"thread.started","thread_id":"thread-123"}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":"Planning..."}}\n',
        '{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"/bin/zsh -lc \\"ls -1\\""}}\n',
        '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"/bin/zsh -lc \\"ls -1\\"","aggregated_output":"README.md\\n"}}\n',
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

    assert [type(m) for m in messages] == [AssistantMessage, AssistantMessage, AssistantMessage, ResultMessage]
    assert isinstance(messages[0].content[0], TextBlock)
    assert messages[0].content[0].text == "Planning..."
    assert messages[0].session_id == "thread-123"
    assert isinstance(messages[1].content[0], ToolUseBlock)
    assert "ls -1" in messages[1].content[0].input["command"]
    assert messages[1].content[0].id == "item_1"
    assert messages[1].session_id == "thread-123"
    assert isinstance(messages[2].content[0], ToolResultBlock)
    assert messages[2].content[0].content == "README.md\n"
    assert messages[2].content[0].tool_use_id == "item_1"
    assert messages[2].session_id == "thread-123"
    assert messages[3].session_id == "thread-123"
    assert messages[3].usage == {"input_tokens": 10, "output_tokens": 3}
    assert process.stdin.buffer.decode("utf-8") == "List files"
    assert process.stdin.closed is True

    args = seen["args"]
    assert args[:3] == ("codex", "exec", "--json")
    assert "--dangerously-bypass-approvals-and-sandbox" in args
    assert "-C" in args
    assert str(tmp_path) in args
    assert seen["kwargs"]["limit"] == CODEX_STDIO_LIMIT_BYTES
    assert CODEX_STDIO_LIMIT_BYTES >= 16 * 1024 * 1024


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
    from otto.agent import _codex_command

    command = _codex_command(ClaudeAgentOptions(
        provider="codex",
        resume="thread-123",
        cwd="/tmp/project",
        model="gpt-5.4",
        permission_mode="bypassPermissions",
    ))

    assert command[:4] == ["codex", "exec", "resume", "--json"]
    assert "--dangerously-bypass-approvals-and-sandbox" in command
    assert "--color" not in command
    assert "-C" not in command
    assert command[-2:] == ["thread-123", "-"]


def test_codex_command_passes_reasoning_effort():
    from otto.agent import _codex_command

    command = _codex_command(ClaudeAgentOptions(
        provider="codex",
        cwd="/tmp/project",
        effort="low",
    ))

    assert "-c" in command
    assert 'model_reasoning_effort="low"' in command


def test_codex_command_maps_max_effort_to_xhigh():
    from otto.agent import _codex_command

    command = _codex_command(ClaudeAgentOptions(
        provider="codex",
        cwd="/tmp/project",
        effort="max",
    ))

    assert 'model_reasoning_effort="xhigh"' in command


def test_codex_prompt_adds_agent_tool_compatibility_guidance():
    from otto.agent import _codex_prompt

    prompt = _codex_prompt(
        "Dispatch a certifier agent using the Agent tool.",
        ClaudeAgentOptions(provider="codex"),
    )

    assert "Codex provider compatibility:" in prompt
    assert "spawn_agent" in prompt
    assert "wait tool" in prompt


@pytest.mark.asyncio
async def test_codex_query_normalizes_collab_subagent_events(tmp_path, monkeypatch):
    process = _FakeProcess([
        '{"type":"thread.started","thread_id":"thread-parent"}\n',
        '{"type":"turn.started"}\n',
        '{"type":"item.started","item":{"id":"item_0","type":"collab_tool_call","tool":"spawn_agent","sender_thread_id":"thread-parent","receiver_thread_ids":[],"prompt":"Run certifier story.","agents_states":{},"status":"in_progress"}}\n',
        '{"type":"item.completed","item":{"id":"item_0","type":"collab_tool_call","tool":"spawn_agent","sender_thread_id":"thread-parent","receiver_thread_ids":["thread-child"],"prompt":"Run certifier story.","agents_states":{"thread-child":{"status":"pending_init","message":null}},"status":"completed"}}\n',
        '{"type":"item.started","item":{"id":"item_1","type":"collab_tool_call","tool":"wait","sender_thread_id":"thread-parent","receiver_thread_ids":["thread-child"],"prompt":null,"agents_states":{},"status":"in_progress"}}\n',
        '{"type":"item.completed","item":{"id":"item_1","type":"collab_tool_call","tool":"wait","sender_thread_id":"thread-parent","receiver_thread_ids":["thread-child"],"prompt":null,"agents_states":{"thread-child":{"status":"completed","message":"STORY_RESULT: smoke | PASS | ok\\nVERDICT: PASS"}},"status":"completed"}}\n',
        '{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"Certifier done."}}\n',
        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":3}}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    state: dict[str, object] = {}
    messages = []
    async for message in query(
        prompt="Certify",
        options=ClaudeAgentOptions(
            provider="codex",
            cwd=str(tmp_path),
            permission_mode="bypassPermissions",
        ),
        state=state,
    ):
        messages.append(message)

    tool_use = messages[0].content[0]
    assert isinstance(tool_use, ToolUseBlock)
    assert tool_use.name == "Agent"
    assert tool_use.id == "item_0"
    assert tool_use.input["subagent_type"] == "codex"
    assert tool_use.input["prompt"] == "Run certifier story."

    tool_result = messages[1].content[0]
    assert isinstance(tool_result, ToolResultBlock)
    assert tool_result.tool_use_id == "item_0"
    assert tool_result.content == "STORY_RESULT: smoke | PASS | ok\nVERDICT: PASS"
    assert messages[1].session_id == "thread-child"

    assert isinstance(messages[2].content[0], TextBlock)
    assert messages[2].content[0].text == "Certifier done."
    assert isinstance(messages[3], ResultMessage)
    assert messages[3].session_id == "thread-parent"
    assert state["codex_child_session_ids"] == ["thread-child"]


@pytest.mark.asyncio
async def test_run_agent_query_tracks_codex_child_sessions(tmp_path, monkeypatch):
    process = _FakeProcess([
        '{"type":"thread.started","thread_id":"thread-parent"}\n',
        '{"type":"item.started","item":{"id":"item_0","type":"collab_tool_call","tool":"spawn_agent","sender_thread_id":"thread-parent","receiver_thread_ids":[],"prompt":"Run certifier story.","agents_states":{},"status":"in_progress"}}\n',
        '{"type":"item.completed","item":{"id":"item_0","type":"collab_tool_call","tool":"spawn_agent","sender_thread_id":"thread-parent","receiver_thread_ids":["thread-child"],"prompt":"Run certifier story.","agents_states":{"thread-child":{"status":"pending_init","message":null}},"status":"completed"}}\n',
        '{"type":"item.completed","item":{"id":"item_1","type":"collab_tool_call","tool":"wait","sender_thread_id":"thread-parent","receiver_thread_ids":["thread-child"],"prompt":null,"agents_states":{"thread-child":{"status":"completed","message":"STORY_RESULT: smoke | PASS | ok\\nVERDICT: PASS"}},"status":"completed"}}\n',
        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":3}}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    state: dict[str, object] = {}
    text, _cost, result = await run_agent_query(
        "Certify",
        AgentOptions(provider="codex", cwd=str(tmp_path)),
        capture_tool_output=True,
        state=state,
    )

    assert "STORY_RESULT: smoke | PASS | ok" in text
    assert isinstance(result, ResultMessage)
    assert result.session_id == "thread-parent"
    assert state["child_session_ids"] == ["thread-child", "thread-parent"]


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
async def test_run_agent_query_dedupes_marker_block_when_final_assistant_repeats_it(monkeypatch):
    final_summary = (
        "All tests pass. Final certification result below.\n\n"
        "CERTIFY_ROUND: 1\n"
        "STORIES_TESTED: 1\n"
        "STORIES_PASSED: 0\n"
        "STORY_RESULT: smoke | FAIL | first attempt failed\n"
        "VERDICT: FAIL\n"
        "DIAGNOSIS: first attempt failed\n\n"
        "CERTIFY_ROUND: 2\n"
        "STORIES_TESTED: 1\n"
        "STORIES_PASSED: 1\n"
        "STORY_RESULT: smoke | PASS | fixed\n"
        "VERDICT: PASS\n"
        "DIAGNOSIS: null\n"
    )

    async def fake_query(*, prompt, options=None, state=None):
        yield AssistantMessage(content=[
            ToolResultBlock(
                content=(
                    "STORIES_TESTED: 1\n"
                    "STORIES_PASSED: 1\n"
                    "STORY_RESULT: smoke | PASS | fixed\n"
                    "VERDICT: PASS\n"
                    "DIAGNOSIS: null\n"
                ),
                tool_use_id="t1",
            ),
        ])
        yield AssistantMessage(content=[TextBlock(text=final_summary)])
        yield ResultMessage(total_cost_usd=0.1)

    monkeypatch.setattr("otto.agent._query_claude", fake_query)

    text, _cost, _result = await run_agent_query(
        "test",
        AgentOptions(),
        capture_tool_output=False,
    )

    parsed = parse_certifier_markers(text)
    assert [round_data["round"] for round_data in parsed.certify_rounds] == [1, 2]
    assert parsed.verdict_pass is True
    assert [story["story_id"] for story in parsed.stories] == ["smoke"]


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
async def test_run_agent_with_timeout_cleans_up_on_cancelled_error(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    async def fake_run_agent_query(*args, **kwargs):
        del args
        state = kwargs["state"]
        state["process_group_id"] = 424242
        state["process_start_time_ns"] = 123456789
        raise asyncio.CancelledError()

    def fake_cleanup(project_dir, *, process_group_id=None, process_start_time_ns=None):
        captured["project_dir"] = project_dir
        captured["process_group_id"] = process_group_id
        captured["process_start_time_ns"] = process_start_time_ns

    monkeypatch.setattr("otto.agent.run_agent_query", fake_run_agent_query)
    monkeypatch.setattr("otto.pipeline._cleanup_orphan_processes", fake_cleanup)

    with pytest.raises(asyncio.CancelledError):
        await run_agent_with_timeout(
            "test",
            AgentOptions(),
            log_dir=tmp_path,
            timeout=30,
            project_dir=tmp_path,
        )

    assert captured == {
        "project_dir": tmp_path,
        "process_group_id": 424242,
        "process_start_time_ns": 123456789,
    }


def test_cleanup_orphan_processes_skips_reused_process_group(tmp_path, monkeypatch):
    from otto.pipeline import _cleanup_orphan_processes

    def fail_if_called(*args, **kwargs):
        raise AssertionError("stale process identity must not be signaled")

    monkeypatch.setattr("otto.pipeline.os.killpg", fail_if_called)

    _cleanup_orphan_processes(
        tmp_path,
        process_group_id=os.getpid(),
        process_start_time_ns=1,
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
