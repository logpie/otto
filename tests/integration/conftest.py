from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

import pytest
from click.testing import CliRunner

from otto.agent import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from otto.cli import main


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
    )


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


class MockSDK:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.calls: list[dict[str, Any]] = []

    def install(
        self,
        factory: Callable[..., Any] | Iterable[Any],
    ) -> None:
        async def fake_query(*, prompt: str, options: Any) -> Any:
            self.calls.append({"prompt": prompt, "options": options})
            stream = factory(prompt=prompt, options=options) if callable(factory) else factory
            async for message in stream:
                yield message

        self._monkeypatch.setattr("otto.agent._sdk_query", fake_query)

    def install_messages(
        self,
        messages: Iterable[Any],
        *,
        on_call: Callable[[str, Any], None] | None = None,
    ) -> None:
        async def stream(*, prompt: str, options: Any):
            if on_call is not None:
                on_call(prompt, options)
            for message in messages:
                if isinstance(message, BaseException):
                    raise message
                yield message

        self.install(stream)


@pytest.fixture
def mock_sdk(monkeypatch: pytest.MonkeyPatch) -> MockSDK:
    sdk = MockSDK(monkeypatch)
    return sdk


@pytest.fixture
def tmp_otto_repo(tmp_git_repo: Path) -> Path:
    return tmp_git_repo


@pytest.fixture
def cli_in_repo() -> Callable[[Path, list[str], dict[str, str] | None], Any]:
    def run(repo: Path, args: list[str], env: dict[str, str] | None = None):
        runner = CliRunner()
        saved_cwd = os.getcwd()
        os.chdir(repo)
        try:
            return runner.invoke(main, args, catch_exceptions=False, env=env)
        finally:
            os.chdir(saved_cwd)

    return run


@pytest.fixture
def fake_subprocess_otto(tmp_path: Path) -> Callable[..., Path]:
    def make_fake(
        *,
        exit_code: int = 0,
        sleep_s: float = 0.05,
        write_manifest: bool = True,
        capture_path: Path | None = None,
    ) -> Path:
        script = tmp_path / f"fake_otto_{len(list(tmp_path.glob('fake_otto_*.py')))}.py"
        capture_literal = repr(str(capture_path)) if capture_path is not None else "None"
        script.write_text(
            f"""#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time

capture_path = {capture_literal}
if capture_path:
    pathlib.Path(capture_path).write_text(json.dumps({{
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "queue_task_id": os.environ.get("OTTO_QUEUE_TASK_ID"),
        "queue_project_dir": os.environ.get("OTTO_QUEUE_PROJECT_DIR"),
    }}))

time.sleep({sleep_s})

if {write_manifest!r}:
    task_id = os.environ.get("OTTO_QUEUE_TASK_ID", "")
    project_dir = os.environ.get("OTTO_QUEUE_PROJECT_DIR", "")
    if task_id and project_dir:
        manifest_dir = pathlib.Path(project_dir) / "otto_logs" / "queue" / task_id
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / "manifest.json").write_text(json.dumps({{
            "command": "build",
            "argv": sys.argv[1:],
            "queue_task_id": task_id,
            "run_id": "fake-run",
            "branch": None,
            "checkpoint_path": None,
            "proof_of_work_path": None,
            "cost_usd": 0.42,
            "duration_s": 0.1,
            "started_at": "2026-04-22T00:00:00Z",
            "finished_at": "2026-04-22T00:00:01Z",
            "head_sha": None,
            "resolved_intent": "test",
            "focus": None,
            "target": None,
            "exit_status": "success" if {exit_code} == 0 else "failure",
            "schema_version": 1,
            "extra": {{}},
        }}))

sys.exit({exit_code})
"""
        )
        script.chmod(0o755)
        return script

    return make_fake


def assistant_text(text: str, *, session_id: str = "sdk-session") -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], session_id=session_id)


def assistant_tool_use(
    name: str,
    input: dict[str, Any],
    *,
    tool_id: str = "tool-1",
    session_id: str = "sdk-session",
) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolUseBlock(name=name, input=input, id=tool_id)],
        session_id=session_id,
    )


def assistant_tool_result(
    content: str,
    *,
    tool_id: str = "tool-1",
    session_id: str = "sdk-session",
    is_error: bool = False,
) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolResultBlock(content=content, tool_use_id=tool_id, is_error=is_error)],
        session_id=session_id,
    )


def result_message(
    *,
    session_id: str = "sdk-session",
    is_error: bool = False,
    result: str | None = None,
    total_cost_usd: float = 0.25,
    usage: dict[str, Any] | None = None,
    structured_output: Any = None,
) -> ResultMessage:
    return ResultMessage(
        subtype="error" if is_error else "success",
        is_error=is_error,
        session_id=session_id,
        result=result,
        total_cost_usd=total_cost_usd,
        usage=usage or {"input_tokens": 123, "output_tokens": 45},
        structured_output=structured_output,
    )


def fast_pass_markers(
    *,
    round_number: int = 1,
    story_id: str = "smoke",
    summary: str = "happy path works",
) -> str:
    return (
        f"CERTIFY_ROUND: {round_number}\n"
        "STORIES_TESTED: 1\n"
        "STORIES_PASSED: 1\n"
        f"STORY_RESULT: {story_id} | PASS | {summary}\n"
        "VERDICT: PASS\n"
        "DIAGNOSIS: null\n"
    )


def thorough_pass_markers(
    *,
    round_number: int = 1,
    story_id: str = "smoke",
    summary: str = "happy path works",
) -> str:
    return (
        f"CERTIFY_ROUND: {round_number}\n"
        "STORIES_TESTED: 1\n"
        "STORIES_PASSED: 1\n"
        f"STORY_RESULT: {story_id} | PASS | {summary}\n"
        "COVERAGE_OBSERVED:\n"
        f"- Exercised {story_id}\n"
        "COVERAGE_GAPS:\n"
        "- None\n"
        "VERDICT: PASS\n"
        "DIAGNOSIS: null\n"
    )
