"""Tests for provider-aware agent execution."""

import asyncio

import pytest

from otto.agent import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    query,
)


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

    async def wait(self) -> int:
        return self._return_code


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
