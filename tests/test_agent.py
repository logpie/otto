"""Tests for provider-aware agent execution."""

import asyncio
import json

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.agent import (
    AgentCallError,
    AgentOptions,
    AssistantMessage,
    CODEX_STDIO_LIMIT_BYTES,
    CODEX_TOOL_OUTPUT_LOG_LIMIT_CHARS,
    ClaudeAgentOptions,
    ProviderEventMessage,
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


class _HangingStdout(_FakeStdout):
    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        await asyncio.sleep(60)
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


class _HangingProcess(_FakeProcess):
    def __init__(self, lines: list[str], return_code: int = 0):
        super().__init__(lines, return_code)
        self.stdout = _HangingStdout(lines)
        self.terminated = False

    async def wait(self) -> int:
        if not self.terminated:
            await asyncio.sleep(60)
        self.returncode = self._return_code
        return self._return_code

    def terminate(self) -> None:
        self.terminated = True


def json_event(payload: dict) -> str:
    return json.dumps(payload) + "\n"


class _LongLivedFakeProcess(_FakeProcess):
    def __init__(self, lines: list[str], return_code: int = 0):
        super().__init__(lines, return_code)
        self.terminated = False

    async def wait(self) -> int:
        if not self.terminated:
            raise AssertionError("long-lived app-server must be terminated after a completed turn")
        self.returncode = self._return_code
        return self._return_code

    def terminate(self) -> None:
        self.terminated = True


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


class _PostResultHangingProcess:
    def __init__(self):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout([
            '{"type":"thread.started","thread_id":"thread-123"}\n',
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":3}}\n',
        ])
        self.returncode: int | None = None
        self.signals: list[str] = []

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode

    def terminate(self) -> None:
        self.signals.append("term")
        self.returncode = -15

    def kill(self) -> None:
        self.signals.append("kill")
        self.returncode = -9


async def _collect_query_messages(**kwargs):
    messages = []
    async for message in query(**kwargs):
        messages.append(message)
    return messages


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
async def test_codex_query_does_not_wait_forever_after_turn_completed(tmp_path, monkeypatch):
    process = _PostResultHangingProcess()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    messages = await asyncio.wait_for(
        _collect_query_messages(
            prompt="Finish",
            options=ClaudeAgentOptions(provider="codex", cwd=str(tmp_path)),
        ),
        timeout=1.0,
    )

    assert [type(message) for message in messages] == [ResultMessage]
    assert messages[0].session_id == "thread-123"
    assert process.signals == ["term"]


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
    assert seen["kwargs"]["env"] is None
    assert seen["kwargs"]["limit"] == CODEX_STDIO_LIMIT_BYTES
    assert CODEX_STDIO_LIMIT_BYTES >= 16 * 1024 * 1024


@pytest.mark.asyncio
async def test_codex_query_truncates_huge_command_output_for_logs(tmp_path, monkeypatch):
    huge_output = "x" * (CODEX_TOOL_OUTPUT_LOG_LIMIT_CHARS + 500)
    process = _FakeProcess([
        '{"type":"thread.started","thread_id":"thread-123"}\n',
        json_event({
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "/bin/zsh -lc \"cat dist/assets/app.js\"",
                "aggregated_output": huge_output,
            },
        }),
        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    messages = []
    async for message in query(
        prompt="Audit",
        options=ClaudeAgentOptions(provider="codex", cwd=str(tmp_path)),
    ):
        messages.append(message)

    block = messages[0].content[0]
    assert isinstance(block, ToolResultBlock)
    assert len(block.content) < len(huge_output)
    assert "Otto truncated 500 chars" in block.content


@pytest.mark.asyncio
async def test_codex_query_nonzero_exit_uses_compact_provider_error(tmp_path, monkeypatch):
    huge_output = "x" * 50_000
    process = _FakeProcess([
        json_event({
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "/bin/zsh -lc \"sed -n '1,220p' dist/assets/app.js\"",
                "aggregated_output": huge_output,
                "exit_code": 0,
                "status": "completed",
            },
        }),
    ], return_code=1)
    state: dict[str, object] = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    messages = []
    async for message in query(
        prompt="Audit",
        options=ClaudeAgentOptions(provider="codex", cwd=str(tmp_path)),
        state=state,
    ):
        messages.append(message)

    result = messages[-1]
    assert isinstance(result, ResultMessage)
    assert result.is_error is True
    assert result.result is not None
    assert len(result.result) < 3_000
    assert "command_execution" in result.result
    assert "Otto truncated" in result.result
    assert huge_output not in result.result
    assert len(str(state["provider_stderr"])) < 3_000


@pytest.mark.asyncio
async def test_codex_app_server_query_normalizes_thread_turn_events(tmp_path, monkeypatch):
    seen: dict[str, object] = {}
    process = _LongLivedFakeProcess([
        '{"id":0,"result":{"codexHome":"/tmp/codex"}}\n',
        '{"id":1,"result":{"thread":{"id":"thread-app","turns":[]}}}\n',
        '{"id":2,"result":{"turn":{"id":"turn-1","status":"inProgress","items":[]}}}\n',
        '{"method":"item/started","params":{"threadId":"thread-app","turnId":"turn-1","item":{"type":"commandExecution","id":"cmd-1","command":"pytest -q","cwd":"/tmp/project","status":"inProgress","aggregatedOutput":null,"exitCode":null,"durationMs":null,"commandActions":[],"source":"exec","processId":null}}}\n',
        '{"method":"item/commandExecution/outputDelta","params":{"threadId":"thread-app","turnId":"turn-1","itemId":"cmd-1","delta":"1 passed\\n"}}\n',
        '{"method":"item/completed","params":{"threadId":"thread-app","turnId":"turn-1","item":{"type":"commandExecution","id":"cmd-1","command":"pytest -q","cwd":"/tmp/project","status":"completed","aggregatedOutput":null,"exitCode":0,"durationMs":12,"commandActions":[],"source":"exec","processId":null}}}\n',
        '{"method":"turn/diff/updated","params":{"threadId":"thread-app","turnId":"turn-1","diff":"diff --git a/app.py b/app.py"}}\n',
        '{"method":"thread/tokenUsage/updated","params":{"threadId":"thread-app","turnId":"turn-1","tokenUsage":{"last":{"inputTokens":1,"outputTokens":1,"totalTokens":2},"total":{"inputTokens":10,"cachedInputTokens":2,"outputTokens":4,"reasoningOutputTokens":1,"totalTokens":15},"modelContextWindow":128000}}}\n',
        '{"method":"item/completed","params":{"threadId":"thread-app","turnId":"turn-1","item":{"type":"agentMessage","id":"msg-1","text":"Done."}}}\n',
        '{"method":"thread/status/changed","params":{"threadId":"thread-app","status":{"type":"idle"}}}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    state: dict[str, object] = {}
    messages = []
    async for message in query(
        prompt="Run tests",
        options=ClaudeAgentOptions(
            provider="codex-app-server",
            cwd=str(tmp_path),
            permission_mode="bypassPermissions",
            effort="low",
        ),
        state=state,
    ):
        messages.append(message)

    provider_events = [m for m in messages if isinstance(m, ProviderEventMessage)]
    flow_messages = [m for m in messages if not isinstance(m, ProviderEventMessage)]
    assert [type(m) for m in flow_messages] == [
        AssistantMessage,
        AssistantMessage,
        AssistantMessage,
        ResultMessage,
    ]
    assert [event.event for event in provider_events] == [
        "turn_acknowledged",
        "diff_updated",
        "token_usage_updated",
        "thread_status_changed",
    ]
    assert provider_events[1].data["changed_files"] == ["app.py"]
    assert provider_events[2].usage["total_tokens"] == 15
    assert isinstance(flow_messages[0].content[0], ToolUseBlock)
    assert flow_messages[0].content[0].name == "Bash"
    assert flow_messages[0].content[0].input["command"] == "pytest -q"
    assert isinstance(flow_messages[1].content[0], ToolResultBlock)
    assert flow_messages[1].content[0].content == "1 passed\n"
    assert isinstance(flow_messages[2].content[0], TextBlock)
    assert flow_messages[2].content[0].text == "Done."
    assert isinstance(flow_messages[3], ResultMessage)
    assert flow_messages[3].session_id == "thread-app"
    assert flow_messages[3].usage == {
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "output_tokens": 4,
        "reasoning_tokens": 1,
        "total_tokens": 15,
    }
    assert state["session_id"] == "thread-app"
    assert state["codex_app_server_thread_id"] == "thread-app"
    assert state["codex_app_server_diff"] == "diff --git a/app.py b/app.py"

    args = seen["args"]
    assert args[:4] == ("codex", "app-server", "--listen", "stdio://")
    assert seen["kwargs"]["limit"] == CODEX_STDIO_LIMIT_BYTES
    written = [
        json.loads(line)
        for line in process.stdin.buffer.decode("utf-8").splitlines()
    ]
    assert [line["method"] for line in written[:4]] == [
        "initialize",
        "initialized",
        "thread/start",
        "turn/start",
    ]
    assert written[2]["params"]["approvalPolicy"] == "never"
    assert written[2]["params"]["sandbox"] == "danger-full-access"
    assert written[3]["params"]["effort"] == "low"
    assert written[3]["params"]["input"][0]["text"] == "Run tests"
    assert seen["kwargs"]["env"] is None


@pytest.mark.asyncio
async def test_codex_app_server_reconnect_stall_fails_before_outer_timeout(
    tmp_path,
    monkeypatch,
):
    """A recoverable AppServer stream error must not burn the whole agent timeout."""

    process = _HangingProcess([
        '{"id":0,"result":{"codexHome":"/tmp/codex"}}\n',
        '{"id":1,"result":{"thread":{"id":"thread-app","turns":[]}}}\n',
        '{"id":2,"result":{"turn":{"id":"turn-1","status":"inProgress","items":[]}}}\n',
        json_event(
            {
                "method": "error",
                "params": {
                    "threadId": "thread-app",
                    "turnId": "turn-1",
                    "error": {
                        "message": "Reconnecting... 2/5",
                        "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": None}},
                    },
                    "willRetry": True,
                    "additionalDetails": (
                        "stream disconnected before completion: WebSocket protocol error"
                    ),
                },
            }
        ),
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setenv("OTTO_CODEX_APP_SERVER_RECONNECT_GRACE_S", "0.01")
    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    state: dict[str, object] = {}
    messages = []
    async for message in query(
        prompt="Run tests",
        options=ClaudeAgentOptions(
            provider="codex-app-server",
            cwd=str(tmp_path),
            permission_mode="bypassPermissions",
            effort="low",
        ),
        state=state,
    ):
        messages.append(message)

    provider_events = [m for m in messages if isinstance(m, ProviderEventMessage)]
    results = [m for m in messages if isinstance(m, ResultMessage)]
    assert provider_events[-1].event == "provider_error"
    assert provider_events[-1].status == "retrying"
    assert results[-1].is_error is True
    assert "stream stalled after recoverable error" in str(results[-1].result)
    assert "Reconnecting... 2/5" in str(results[-1].result)
    assert process.terminated is True


@pytest.mark.asyncio
async def test_codex_app_server_agent_message_deltas_emit_progress(
    tmp_path,
    monkeypatch,
):
    process = _LongLivedFakeProcess([
        '{"id":0,"result":{"codexHome":"/tmp/codex"}}\n',
        '{"id":1,"result":{"thread":{"id":"thread-app","turns":[]}}}\n',
        '{"id":2,"result":{"turn":{"id":"turn-1","status":"inProgress","items":[]}}}\n',
        json_event(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thread-app",
                    "turnId": "turn-1",
                    "itemId": "msg-1",
                    "delta": "Writing the compiled spec now.",
                },
            }
        ),
        '{"method":"item/completed","params":{"threadId":"thread-app","turnId":"turn-1","item":{"type":"agentMessage","id":"msg-1","text":"Writing the compiled spec now."}}}\n',
        '{"method":"thread/status/changed","params":{"threadId":"thread-app","status":{"type":"idle"}}}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    messages = []
    async for message in query(
        prompt="Run tests",
        options=ClaudeAgentOptions(provider="codex-app-server", cwd=str(tmp_path)),
    ):
        messages.append(message)

    progress_events = [
        message
        for message in messages
        if isinstance(message, ProviderEventMessage)
        and message.event == "agent_message_delta"
    ]
    assert progress_events
    assert progress_events[0].data["preview"] == "Writing the compiled spec now."
    assert progress_events[0].data["chars"] == len("Writing the compiled spec now.")


@pytest.mark.asyncio
async def test_run_agent_with_timeout_persists_codex_app_server_events_and_diff(tmp_path, monkeypatch):
    process = _LongLivedFakeProcess([
        '{"id":0,"result":{"codexHome":"/tmp/codex"}}\n',
        '{"id":1,"result":{"thread":{"id":"thread-app","turns":[]}}}\n',
        '{"id":2,"result":{"turn":{"id":"turn-1","status":"inProgress","items":[]}}}\n',
        '{"method":"turn/diff/updated","params":{"threadId":"thread-app","turnId":"turn-1","diff":"diff --git a/app.py b/app.py\\n+print(1)\\n"}}\n',
        '{"method":"thread/tokenUsage/updated","params":{"threadId":"thread-app","turnId":"turn-1","tokenUsage":{"last":{"inputTokens":10,"outputTokens":4,"totalTokens":14},"total":{"inputTokens":10,"outputTokens":4,"totalTokens":14}}}}\n',
        '{"method":"item/completed","params":{"threadId":"thread-app","turnId":"turn-1","item":{"type":"agentMessage","id":"msg-1","text":"Done."}}}\n',
        '{"method":"thread/status/changed","params":{"threadId":"thread-app","status":{"type":"idle"}}}\n',
    ])

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    log_dir = tmp_path / "logs"
    text, _cost, session_id, breakdown = await run_agent_with_timeout(
        "Run tests",
        AgentOptions(
            provider="codex-app-server",
            cwd=str(tmp_path),
            permission_mode="bypassPermissions",
        ),
        log_dir=log_dir,
        phase_name="SPEC_COMPILE",
        phase_label="compile",
        timeout=30,
        project_dir=tmp_path,
    )

    assert text == "Done."
    assert session_id == "thread-app"
    diff_path = Path(breakdown["provider_diff_path"])
    assert diff_path.exists()
    assert diff_path.read_text(encoding="utf-8").startswith("diff --git a/app.py")
    assert breakdown["provider_diff_summary"]["changed_files"] == ["app.py"]
    records = [
        json.loads(line)
        for line in (log_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    provider_events = [record for record in records if record.get("type") == "provider_event"]
    assert [record["event"] for record in provider_events] == [
        "turn_acknowledged",
        "diff_updated",
        "token_usage_updated",
        "thread_status_changed",
    ]
    assert provider_events[1]["data"]["changed_files"] == ["app.py"]
    assert provider_events[2]["usage"]["total_tokens"] == 14


@pytest.mark.asyncio
async def test_codex_app_server_uses_thread_resume_when_requested(tmp_path, monkeypatch):
    process = _FakeProcess([
        '{"id":0,"result":{"codexHome":"/tmp/codex"}}\n',
        '{"id":1,"result":{"thread":{"id":"thread-prior","turns":[]}}}\n',
        '{"id":2,"result":{"turn":{"id":"turn-2","status":"inProgress","items":[]}}}\n',
        '{"method":"item/completed","params":{"threadId":"thread-prior","turnId":"turn-2","item":{"type":"agentMessage","id":"msg-1","text":"Resumed."}}}\n',
        '{"method":"turn/completed","params":{"threadId":"thread-prior","turn":{"id":"turn-2","status":"completed","items":[],"error":null,"startedAt":1,"completedAt":2,"durationMs":1000}}}\n',
    ])

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    messages = []
    async for message in query(
        prompt="Continue",
        options=AgentOptions(
            provider="codex-app-server",
            cwd=str(tmp_path),
            resume="thread-prior",
        ),
    ):
        messages.append(message)

    assert isinstance(messages[-1], ResultMessage)
    assert messages[-1].session_id == "thread-prior"
    written = [
        json.loads(line)
        for line in process.stdin.buffer.decode("utf-8").splitlines()
    ]
    assert [line["method"] for line in written[:4]] == [
        "initialize",
        "initialized",
        "thread/resume",
        "turn/start",
    ]
    assert written[2]["params"]["threadId"] == "thread-prior"
    assert written[3]["params"]["threadId"] == "thread-prior"


@pytest.mark.asyncio
async def test_codex_app_server_passes_output_schema_and_structured_result(tmp_path, monkeypatch):
    process = _FakeProcess([
        '{"id":0,"result":{"codexHome":"/tmp/codex"}}\n',
        '{"id":1,"result":{"thread":{"id":"thread-app","turns":[]}}}\n',
        '{"id":2,"result":{"turn":{"id":"turn-1","status":"inProgress","items":[]}}}\n',
        '{"method":"item/completed","params":{"threadId":"thread-app","turnId":"turn-1","item":{"type":"agentMessage","id":"msg-1","text":"{\\"verdict\\": \\"PASS\\", \\"stories\\": 1}"}}}\n',
        '{"method":"turn/completed","params":{"threadId":"thread-app","turn":{"id":"turn-1","status":"completed","items":[],"error":null,"startedAt":1,"completedAt":2,"durationMs":1000}}}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    messages = []
    async for message in query(
        prompt="Return a verdict",
        options=ClaudeAgentOptions(
            provider="codex-app-server",
            cwd=str(tmp_path),
            output_format={
                "json_schema": {
                    "schema": {
                        "type": "object",
                        "properties": {"verdict": {"type": "string"}},
                        "required": ["verdict"],
                    }
                }
            },
        ),
    ):
        messages.append(message)

    result = messages[-1]
    assert isinstance(result, ResultMessage)
    assert result.structured_output == {"verdict": "PASS", "stories": 1}
    written = [
        json.loads(line)
        for line in process.stdin.buffer.decode("utf-8").splitlines()
    ]
    assert written[3]["params"]["outputSchema"]["required"] == ["verdict"]
    assert written[3]["params"]["outputSchema"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_codex_app_server_reports_structured_schema_errors(tmp_path, monkeypatch):
    process = _FakeProcess([
        '{"id":0,"result":{"codexHome":"/tmp/codex"}}\n',
        '{"id":1,"result":{"thread":{"id":"thread-app","turns":[]}}}\n',
        '{"id":2,"result":{"turn":{"id":"turn-1","status":"inProgress","items":[]}}}\n',
        '{"method":"item/completed","params":{"threadId":"thread-app","turnId":"turn-1","item":{"type":"agentMessage","id":"msg-1","text":"{\\"stories\\": 1}"}}}\n',
        '{"method":"turn/completed","params":{"threadId":"thread-app","turn":{"id":"turn-1","status":"completed","items":[],"error":null,"startedAt":1,"completedAt":2,"durationMs":1000}}}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    messages = []
    async for message in query(
        prompt="Return a verdict",
        options=ClaudeAgentOptions(
            provider="codex-app-server",
            cwd=str(tmp_path),
            output_format={
                "json_schema": {
                    "schema": {
                        "type": "object",
                        "properties": {"verdict": {"type": "string"}},
                        "required": ["verdict"],
                    }
                }
            },
        ),
    ):
        messages.append(message)

    result = messages[-1]
    assert isinstance(result, ResultMessage)
    assert result.structured_output == {"stories": 1}
    assert "missing required field" in result.structured_output_error


@pytest.mark.asyncio
async def test_codex_app_server_declines_unsafe_command_approval(tmp_path, monkeypatch):
    process = _FakeProcess([
        '{"id":0,"result":{"codexHome":"/tmp/codex"}}\n',
        '{"id":1,"result":{"thread":{"id":"thread-app","turns":[]}}}\n',
        '{"method":"item/commandExecution/requestApproval","id":99,"params":{"threadId":"thread-app","turnId":"turn-1","itemId":"cmd-1","command":"killall node"}}\n',
        '{"id":2,"result":{"turn":{"id":"turn-1","status":"inProgress","items":[]}}}\n',
        '{"method":"turn/completed","params":{"threadId":"thread-app","turn":{"id":"turn-1","status":"completed","items":[],"error":null,"startedAt":1,"completedAt":2,"durationMs":1000}}}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    async for _message in query(
        prompt="Try command",
        options=ClaudeAgentOptions(provider="codex-app-server", cwd=str(tmp_path)),
    ):
        pass

    written = [
        json.loads(line)
        for line in process.stdin.buffer.decode("utf-8").splitlines()
    ]
    approval_response = next(line for line in written if line.get("id") == 99)
    assert approval_response["result"] == {"decision": "decline"}


@pytest.mark.asyncio
async def test_codex_app_server_filters_permission_expansion_requests(tmp_path, monkeypatch):
    inside = tmp_path / "inside"
    outside = tmp_path.parent / "outside"
    process = _FakeProcess([
        '{"id":0,"result":{"codexHome":"/tmp/codex"}}\n',
        '{"id":1,"result":{"thread":{"id":"thread-app","turns":[]}}}\n',
        json_event({
            "method": "item/permissions/requestApproval",
            "id": 99,
            "params": {
                "threadId": "thread-app",
                "turnId": "turn-1",
                "itemId": "perm-1",
                "cwd": str(tmp_path),
                "permissions": {
                    "fileSystem": {
                        "read": [str(inside), str(outside)],
                        "write": [str(outside)],
                        "entries": [
                            {
                                "access": "write",
                                "path": {"type": "path", "path": str(inside)},
                            },
                            {
                                "access": "write",
                                "path": {"type": "path", "path": str(outside)},
                            },
                            {
                                "access": "read",
                                "path": {
                                    "type": "special",
                                    "value": {"kind": "root"},
                                },
                            },
                        ],
                    },
                    "network": {"enabled": True},
                },
            },
        }),
        '{"id":2,"result":{"turn":{"id":"turn-1","status":"inProgress","items":[]}}}\n',
        '{"method":"turn/completed","params":{"threadId":"thread-app","turn":{"id":"turn-1","status":"completed","items":[],"error":null,"startedAt":1,"completedAt":2,"durationMs":1000}}}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    async for _message in query(
        prompt="Request permissions",
        options=ClaudeAgentOptions(provider="codex-app-server", cwd=str(tmp_path)),
    ):
        pass

    written = [
        json.loads(line)
        for line in process.stdin.buffer.decode("utf-8").splitlines()
    ]
    approval_response = next(line for line in written if line.get("id") == 99)
    assert approval_response["result"] == {
        "permissions": {
            "fileSystem": {
                "read": [str(inside.resolve())],
                "entries": [
                    {
                        "access": "write",
                        "path": {"type": "path", "path": str(inside.resolve())},
                    }
                ],
            }
        },
        "scope": "turn",
        "strictAutoReview": False,
    }


@pytest.mark.asyncio
async def test_codex_app_server_returns_empty_permission_grants_for_outside_workspace(
    tmp_path,
    monkeypatch,
):
    outside = tmp_path.parent / "outside"
    process = _FakeProcess([
        '{"id":0,"result":{"codexHome":"/tmp/codex"}}\n',
        '{"id":1,"result":{"thread":{"id":"thread-app","turns":[]}}}\n',
        json_event({
            "method": "item/permissions/requestApproval",
            "id": 99,
            "params": {
                "threadId": "thread-app",
                "turnId": "turn-1",
                "itemId": "perm-1",
                "cwd": str(tmp_path.parent),
                "permissions": {
                    "fileSystem": {
                        "write": [str(outside)],
                        "entries": [
                            {
                                "access": "write",
                                "path": {"type": "path", "path": str(outside)},
                            }
                        ],
                    },
                    "network": {"enabled": True},
                },
            },
        }),
        '{"id":2,"result":{"turn":{"id":"turn-1","status":"inProgress","items":[]}}}\n',
        '{"method":"turn/completed","params":{"threadId":"thread-app","turn":{"id":"turn-1","status":"completed","items":[],"error":null,"startedAt":1,"completedAt":2,"durationMs":1000}}}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr("otto.agent.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    async for _message in query(
        prompt="Request permissions",
        options=ClaudeAgentOptions(provider="codex-app-server", cwd=str(tmp_path)),
    ):
        pass

    written = [
        json.loads(line)
        for line in process.stdin.buffer.decode("utf-8").splitlines()
    ]
    approval_response = next(line for line in written if line.get("id") == 99)
    assert approval_response["result"] == {
        "permissions": {},
        "scope": "turn",
        "strictAutoReview": False,
    }


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


def test_make_agent_options_phase_cli_overrides_beat_global_cli(tmp_path):
    config = {
        "provider": "claude",
        "model": "sonnet",
        "effort": "medium",
        "_cli_overrides": {
            "provider": "claude",
            "model": "haiku",
            "effort": "low",
            "agents": {
                "certifier": {"provider": "codex", "model": "gpt-5.4", "effort": "high"},
            },
        },
    }

    build_options = make_agent_options(tmp_path, config, agent_type="build")
    certifier_options = make_agent_options(tmp_path, config, agent_type="certifier")

    assert build_options.provider == "claude"
    assert build_options.model == "haiku"
    assert build_options.effort == "low"
    assert certifier_options.provider == "codex"
    assert certifier_options.model == "gpt-5.4"
    assert certifier_options.effort == "high"


def test_make_agent_options_uses_safe_claude_defaults_when_model_omitted(tmp_path):
    build_options = make_agent_options(tmp_path, {"provider": "claude"}, agent_type="build")
    certifier_options = make_agent_options(tmp_path, {"provider": "claude"}, agent_type="certifier")
    codex_options = make_agent_options(tmp_path, {"provider": "codex"}, agent_type="build")

    assert build_options.model == "sonnet"
    assert certifier_options.model == "haiku"
    assert codex_options.model is None


def test_make_agent_options_env_prefers_target_project_src(tmp_path, monkeypatch):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    stale_src = "/tmp/stale-project/src"
    monkeypatch.setenv("PYTHONPATH", stale_src)

    options = make_agent_options(tmp_path, {"provider": "codex"}, agent_type="build")

    assert options.env is not None
    pythonpath = options.env.get("PYTHONPATH", "").split(os.pathsep)
    assert pythonpath[:2] == [str(src_dir), stale_src]


def test_make_agent_options_sets_default_max_turns(tmp_path):
    options = make_agent_options(tmp_path, {})

    assert options.provider == "codex-app-server"
    assert options.max_turns == 200
    assert options.max_subagent_dispatches == 160


@pytest.mark.asyncio
async def test_query_defaults_to_codex_app_server(monkeypatch):
    async def fake_query(*, prompt, options=None, state=None):
        assert prompt == "test"
        yield ResultMessage(total_cost_usd=0.0, session_id="app-server-default")

    monkeypatch.setattr("otto.agent._query_codex_app_server", fake_query)

    messages = [message async for message in query(prompt="test", options=AgentOptions())]

    assert [message.session_id for message in messages] == ["app-server-default"]


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
        AgentOptions(provider="claude"),
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
        AgentOptions(provider="claude"),
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
        AgentOptions(provider="claude"),
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
            AgentOptions(provider="claude", max_subagent_dispatches=2),
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
    monkeypatch.setattr("otto.runs.lifecycle._cleanup_orphan_processes", fake_cleanup)

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
    from otto.runs.lifecycle import _cleanup_orphan_processes

    def fail_if_called(*args, **kwargs):
        raise AssertionError("stale process identity must not be signaled")

    monkeypatch.setattr("otto.runs.lifecycle.os.killpg", fail_if_called)

    _cleanup_orphan_processes(
        tmp_path,
        process_group_id=os.getpid(),
        process_start_time_ns=1,
    )


@pytest.mark.asyncio
async def test_openai_agents_query_streams_tools_usage_and_structured_output(tmp_path, monkeypatch):
    import otto.agent as agent_mod

    calls: dict[str, object] = {}
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    class FakeAgent:
        def __init__(self, **kwargs):
            calls["agent_kwargs"] = kwargs

    class FakeRunConfig:
        def __init__(self, **kwargs):
            calls["run_config_kwargs"] = kwargs
            self.kwargs = kwargs

    class FakeModelSettings:
        def __init__(self, **kwargs):
            calls["model_settings_kwargs"] = kwargs
            self.kwargs = kwargs

    class FakeManifest:
        def __init__(self, **kwargs):
            calls["manifest_kwargs"] = kwargs

    class FakeSandboxRunConfig:
        def __init__(self, **kwargs):
            calls["sandbox_kwargs"] = kwargs

    class FakeUnixLocalSandboxClient:
        pass

    class FakeStream:
        final_output = {"verdict": "PASS", "stories": 2}
        raw_responses = [
            SimpleNamespace(
                response_id="resp-123",
                usage=SimpleNamespace(
                    requests=1,
                    input_tokens=11,
                    input_tokens_details=SimpleNamespace(cached_tokens=3),
                    output_tokens=5,
                    output_tokens_details=SimpleNamespace(reasoning_tokens=2),
                    total_tokens=18,
                ),
            )
        ]

        @property
        def last_response_id(self):
            return "resp-123"

        def ensure_sandbox_cleanup_on_completion(self):
            calls["cleanup_registered"] = True

        async def stream_events(self):
            yield SimpleNamespace(
                type="run_item_stream_event",
                item=SimpleNamespace(
                    type="message_output_item",
                    raw_item=SimpleNamespace(
                        content=[SimpleNamespace(text="Planning\n")]
                    ),
                ),
            )
            yield SimpleNamespace(
                type="run_item_stream_event",
                item=SimpleNamespace(
                    type="tool_call_item",
                    raw_item=SimpleNamespace(
                        type="shell_call",
                        call_id="call-1",
                        name="exec_command",
                        arguments='{"cmd": "pytest -q"}',
                    ),
                ),
            )
            yield SimpleNamespace(
                type="run_item_stream_event",
                item=SimpleNamespace(
                    type="tool_call_output_item",
                    output={"output": "2 passed\n"},
                    raw_item=SimpleNamespace(call_id="call-1"),
                ),
            )

    class FakeRunner:
        @staticmethod
        def run_streamed(agent, input, **kwargs):
            calls["runner_agent"] = agent
            calls["runner_input"] = input
            calls["runner_kwargs"] = kwargs
            return FakeStream()

    monkeypatch.setattr(agent_mod, "_OpenAIRunner", FakeRunner)
    monkeypatch.setattr(agent_mod, "_OpenAISandboxAgent", FakeAgent)
    monkeypatch.setattr(agent_mod, "_OpenAIAgent", FakeAgent)
    monkeypatch.setattr(agent_mod, "_OpenAIRunConfig", FakeRunConfig)
    monkeypatch.setattr(agent_mod, "_OpenAIModelSettings", FakeModelSettings)
    monkeypatch.setattr(agent_mod, "_OpenAIManifest", FakeManifest)
    monkeypatch.setattr(agent_mod, "_OpenAISandboxRunConfig", FakeSandboxRunConfig)
    monkeypatch.setattr(agent_mod, "_OpenAIUnixLocalSandboxClient", FakeUnixLocalSandboxClient)
    monkeypatch.setattr(agent_mod, "_OpenAIFilesystemCapability", None)
    monkeypatch.setattr(agent_mod, "_OpenAIShellCapability", None)
    monkeypatch.setattr(agent_mod, "_OpenAICompactionCapability", None)

    state: dict[str, object] = {}
    text, cost, result = await run_agent_query(
        "Build the app",
        AgentOptions(
            provider="openai-agents",
            cwd=str(tmp_path),
            max_turns=12,
            effort="low",
            setting_sources=["project"],
            env={"OTTO_TEST_FLAG": "1"},
        ),
        capture_tool_output=True,
        state=state,
    )

    assert "Planning" in text
    assert "2 passed" in text
    assert cost == 0.0
    assert isinstance(result, ResultMessage)
    assert result.session_id == "resp-123"
    assert result.structured_output == {"verdict": "PASS", "stories": 2}
    assert result.usage == {
        "requests": 1,
        "input_tokens": 11,
        "cached_input_tokens": 3,
        "output_tokens": 5,
        "reasoning_tokens": 2,
        "total_tokens": 18,
    }
    assert state["session_id"] == "resp-123"
    assert calls["runner_input"] == "Build the app"
    assert calls["runner_kwargs"]["max_turns"] == 12
    assert calls["runner_kwargs"]["previous_response_id"] is None
    assert calls["cleanup_registered"] is True
    assert calls["run_config_kwargs"]["tracing_disabled"] is True
    assert calls["model_settings_kwargs"]["include_usage"] is True
    assert calls["model_settings_kwargs"]["parallel_tool_calls"] is True
    assert calls["model_settings_kwargs"]["reasoning"] == {"effort": "low"}
    assert calls["manifest_kwargs"]["root"] == str(tmp_path.resolve())
    assert calls["manifest_kwargs"]["environment"] == {"value": {"OTTO_TEST_FLAG": "1"}}


@pytest.mark.asyncio
async def test_openai_agents_missing_sdk_surfaces_install_hint(monkeypatch):
    import otto.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_OpenAIRunner", None)
    monkeypatch.setattr(agent_mod, "_OPENAI_AGENTS_IMPORT_ERROR_MESSAGE", "missing agents")

    with pytest.raises(RuntimeError, match="openai"):
        async for _message in query(
            prompt="test",
            options=AgentOptions(provider="openai-agents"),
        ):
            pass


@pytest.mark.asyncio
async def test_openai_agents_subscription_auth_gets_clear_hint(tmp_path, monkeypatch):
    import otto.agent as agent_mod

    auth_dir = tmp_path / ".codex"
    auth_dir.mkdir()
    (auth_dir / "auth.json").write_text(
        (
            '{"OPENAI_API_KEY": null, '
            '"tokens": {"access_token": "oauth-token", "refresh_token": "refresh"}}'
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)
    monkeypatch.setattr(agent_mod.Path, "home", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="Codex subscription login"):
        async for _message in query(
            prompt="test",
            options=AgentOptions(provider="openai-agents"),
        ):
            pass


@pytest.mark.asyncio
async def test_openai_agents_shell_tool_blocks_process_kill_commands(monkeypatch):
    import otto.agent as agent_mod

    class FakeExecCommandTool:
        def __init__(self, *, session, user=None):
            self.session = session
            self.user = user

        async def run(self, args):
            return f"ran {args.cmd}"

    monkeypatch.setattr(agent_mod, "_OpenAIExecCommandTool", FakeExecCommandTool)
    toolset = SimpleNamespace(
        exec_command=FakeExecCommandTool(session=object(), user="otto")
    )

    agent_mod._configure_openai_shell_tools(toolset)

    blocked = await toolset.exec_command.run(SimpleNamespace(cmd="killall node"))
    allowed = await toolset.exec_command.run(SimpleNamespace(cmd="echo ok"))

    assert blocked.startswith("Otto blocked a broad killall command")
    assert allowed == "ran echo ok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "backend_attr"),
    [
        ("claude", "_query_claude"),
        ("codex", "_query_codex"),
        ("codex-app-server", "_query_codex_app_server"),
        ("openai-agents", "_query_openai_agents"),
    ],
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
