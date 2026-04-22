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

    async def wait(self) -> int:
        self.returncode = self._return_code
        return self._return_code

    def kill(self) -> None:
        pass


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
    assert isinstance(messages[1].content[0], ToolUseBlock)
    assert "ls -1" in messages[1].content[0].input["command"]
    assert messages[1].content[0].id == "item_1"
    assert isinstance(messages[2].content[0], ToolResultBlock)
    assert messages[2].content[0].content == "README.md\n"
    assert messages[2].content[0].tool_use_id == "item_1"
    assert messages[3].session_id == "thread-123"
    assert messages[3].usage == {"input_tokens": 10, "output_tokens": 3}
    assert process.stdin.buffer.decode("utf-8") == "List files"
    assert process.stdin.closed is True

    args = seen["args"]
    assert args[:3] == ("codex", "exec", "--json")
    assert "--dangerously-bypass-approvals-and-sandbox" in args
    assert "-C" in args
    assert str(tmp_path) in args


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
    assert len(text) < 60_000


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
