"""Regression coverage for spec compile timeout re-entry."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from otto.agent import AgentCallError
from otto.audit import AuditVerdict
from otto.budget import RunBudget
from otto.cli import main
from otto.runner import run_pipeline
from otto.spec_compile import (
    BrowserJourney,
    Group,
    Spec,
    SpecValidationError,
    StructureDecisions,
    compile_spec,
    spec_to_dict,
)


def _valid_webapp_spec() -> Spec:
    return Spec(
        intent="a bookmark manager",
        project_kind="webapp",
        structure=StructureDecisions(
            payload={
                "routes": [{"path": "/", "component": "Home", "key_text": "Bookmarks"}],
                "components": [{"name": "Home", "key_text": "Bookmarks"}],
            }
        ),
        groups=[
            Group(
                id="shell",
                name="App shell",
                feature_ids=["show saved bookmarks"],
                dependencies=[],
                owned_paths=[],
                checks=[
                    BrowserJourney(
                        command=("pytest", "tests/browser/test_shell.py"),
                        evidence_globs=("evidence/shell/*.png",),
                    ),
                ],
            ),
        ],
        done_means=["The home route shows saved bookmarks."],
    )


def _agent_text_for_spec(spec: Spec) -> str:
    return f"<spec_json>{json.dumps(spec_to_dict(spec))}</spec_json>"


class _FixedBudget:
    def __init__(self, seconds: int) -> None:
        self.seconds = seconds

    def exhausted(self) -> bool:
        return False

    def remaining(self) -> float:
        return float(self.seconds)

    def elapsed(self) -> float:
        return 0.0

    def for_call(self) -> int:
        return self.seconds


@pytest.mark.asyncio
async def test_compile_spec_retries_timeout_with_extended_budget_clamped_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    run_dir = project_dir / "otto_logs" / "sessions" / "run-timeout-success" / "spec"
    calls: list[dict[str, Any]] = []

    async def timeout_then_success(*_args: object, **kwargs: Any):
        calls.append({"timeout": kwargs["timeout"], "log_dir": Path(kwargs["log_dir"])})
        if len(calls) == 1:
            raise AgentCallError("Timed out after 600s", session_id="slow-1")
        return _agent_text_for_spec(_valid_webapp_spec()), 0.0, "slow-2", {}

    monkeypatch.setattr("otto.agent.run_agent_with_timeout", timeout_then_success)

    spec = await compile_spec(
        "build a bookmark manager",
        project_dir=project_dir,
        run_dir=run_dir,
        config={"provider": "codex-app-server", "spec_timeout": 600},
        project_kind="webapp",
        budget=_FixedBudget(900),
    )

    assert spec.intent == "a bookmark manager"
    assert [call["timeout"] for call in calls] == [600, 900]
    assert [call["log_dir"].name for call in calls] == [
        "compile-agent",
        "compile-agent-retry-02",
    ]


@pytest.mark.asyncio
async def test_compile_spec_timeout_caps_shrink_with_elapsed_run_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    run_dir = project_dir / "otto_logs" / "sessions" / "run-timeout-shrinks" / "spec"
    budget = RunBudget(total=700.0, start=time.monotonic())
    calls: list[int] = []

    async def timeout_then_success(*_args: object, **kwargs: Any):
        calls.append(int(kwargs["timeout"]))
        if len(calls) == 1:
            budget.start = time.monotonic() - 250.0
            raise AgentCallError("Timed out after 600s", session_id="slow-1")
        return _agent_text_for_spec(_valid_webapp_spec()), 0.0, "slow-2", {}

    monkeypatch.setattr("otto.agent.run_agent_with_timeout", timeout_then_success)

    spec = await compile_spec(
        "build a bookmark manager",
        project_dir=project_dir,
        run_dir=run_dir,
        config={"provider": "codex-app-server", "spec_timeout": 600},
        project_kind="webapp",
        budget=budget,
    )

    assert spec.intent == "a bookmark manager"
    assert calls[0] == 600
    assert 0 < calls[1] < calls[0]


@pytest.mark.asyncio
async def test_compile_spec_timeout_exhaustion_is_structured_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    run_dir = project_dir / "otto_logs" / "sessions" / "run-timeout-exhausted" / "spec"
    timeouts: list[int] = []

    async def always_timeout(*_args: object, **kwargs: Any):
        timeout = int(kwargs["timeout"])
        timeouts.append(timeout)
        raise AgentCallError(f"Timed out after {timeout}s", session_id=f"slow-{len(timeouts)}")

    monkeypatch.setattr("otto.agent.run_agent_with_timeout", always_timeout)

    with pytest.raises(SpecValidationError) as excinfo:
        await compile_spec(
            "build a large capstone app",
            project_dir=project_dir,
            run_dir=run_dir,
            config={"provider": "codex-app-server", "spec_timeout": 600},
            project_kind="webapp",
        )

    reason = getattr(excinfo.value, "structured_reason", None)
    assert isinstance(reason, dict)
    assert reason["kind"] == "spec_compile_timeout_exhausted"
    assert reason["attempts"] == 3
    assert reason["per_attempt_timeouts"] == [600, 1200, 1800]
    assert reason["elapsed_s"] >= 0
    assert isinstance(reason["_written_at"], str) and reason["_written_at"]
    assert timeouts == [600, 1200, 1800]


@pytest.mark.asyncio
async def test_compile_spec_budget_exhausted_before_call_is_structured_zero_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    run_dir = project_dir / "otto_logs" / "sessions" / "run-budget-exhausted" / "spec"
    calls: list[int] = []

    async def should_not_run(*_args: object, **kwargs: Any):
        calls.append(int(kwargs["timeout"]))
        return _agent_text_for_spec(_valid_webapp_spec()), 0.0, "unexpected", {}

    monkeypatch.setattr("otto.agent.run_agent_with_timeout", should_not_run)

    with pytest.raises(SpecValidationError) as excinfo:
        await compile_spec(
            "build a bookmark manager",
            project_dir=project_dir,
            run_dir=run_dir,
            config={"provider": "codex-app-server", "spec_timeout": 600},
            project_kind="webapp",
            budget=RunBudget(total=1.0, start=time.monotonic() - 2.0),
        )

    reason = getattr(excinfo.value, "structured_reason", None)
    assert isinstance(reason, dict)
    assert reason["kind"] == "spec_compile_budget_exhausted"
    assert reason["attempts"] == 0
    assert reason["per_attempt_timeouts"] == []
    assert calls == []


@pytest.mark.asyncio
async def test_compile_spec_non_timeout_agent_call_error_still_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    run_dir = project_dir / "otto_logs" / "sessions" / "run-budget-exhausted" / "spec"

    async def budget_exhausted(*_args: object, **_kwargs: Any):
        raise AgentCallError("max_turns cap reached; raise --max-turns")

    monkeypatch.setattr("otto.agent.run_agent_with_timeout", budget_exhausted)

    with pytest.raises(AgentCallError, match="max_turns cap reached"):
        await compile_spec(
            "build a bookmark manager",
            project_dir=project_dir,
            run_dir=run_dir,
            config={"provider": "codex-app-server", "spec_timeout": 600},
            project_kind="webapp",
        )


@pytest.mark.asyncio
async def test_compile_spec_embedded_tool_timeout_error_still_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    run_dir = project_dir / "otto_logs" / "sessions" / "run-tool-timeout" / "spec"

    async def tool_timeout_crash(*_args: object, **_kwargs: Any):
        raise AgentCallError("Agent crashed: tool timed out after 30s")

    monkeypatch.setattr("otto.agent.run_agent_with_timeout", tool_timeout_crash)

    with pytest.raises(AgentCallError, match="tool timed out after 30s"):
        await compile_spec(
            "build a bookmark manager",
            project_dir=project_dir,
            run_dir=run_dir,
            config={"provider": "codex-app-server", "spec_timeout": 600},
            project_kind="webapp",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events",
    [
        ("transient", "timeout", "success"),
        ("timeout", "transient", "success"),
    ],
)
async def test_compile_spec_transient_timeout_interleavings_share_three_attempt_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: tuple[str, str, str],
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    run_dir = project_dir / "otto_logs" / "sessions" / ("run-" + "-".join(events)) / "spec"
    calls: list[int] = []

    async def interleaved_agent(*_args: object, **kwargs: Any):
        calls.append(int(kwargs["timeout"]))
        event = events[len(calls) - 1]
        if event == "transient":
            raise AgentCallError(
                "codex app-server stream stalled after recoverable error: "
                "Reconnecting... 2/5. No provider events arrived for 120s.",
                last_provider_stderr="Reconnecting... 2/5",
            )
        if event == "timeout":
            raise AgentCallError(f"Timed out after {kwargs['timeout']}s")
        return _agent_text_for_spec(_valid_webapp_spec()), 0.0, "ok", {}

    monkeypatch.setattr("otto.agent.run_agent_with_timeout", interleaved_agent)

    spec = await compile_spec(
        "build a bookmark manager",
        project_dir=project_dir,
        run_dir=run_dir,
        config={"provider": "codex-app-server", "spec_timeout": 600},
        project_kind="webapp",
    )

    assert spec.intent == "a bookmark manager"
    assert len(calls) == 3


def _init_project(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@otto.local"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Otto Tester"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "commit.gpgsign", "false"], check=True)
    (path / "README.md").write_text("test project\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _run_cli(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> tuple[int, str]:
    runner = CliRunner()
    saved_cwd = os.getcwd()
    os.chdir(cwd)
    try:
        result = runner.invoke(main, args, catch_exceptions=False, env=env or {})
    finally:
        os.chdir(saved_cwd)
    return result.exit_code, result.output


def test_cli_run_records_structured_compile_timeout_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_project(tmp_path)

    async def fail_compile(*_args: object, **_kwargs: Any):
        exc = SpecValidationError("spec compile timeout exhausted after 3 attempts")
        exc.structured_reason = {
            "kind": "spec_compile_timeout_exhausted",
            "attempts": 3,
            "per_attempt_timeouts": [600, 1200, 1800],
            "elapsed_s": 3600.0,
            "_written_at": "2026-05-16T08:22:38Z",
        }
        raise exc

    monkeypatch.setattr("otto.cli_run.compile_spec", fail_compile)

    env = {"OTTO_RUN_ID": "2026-05-16-082238-timeout"}
    code, out = _run_cli(["run", "--no-build", "build it"], cwd=tmp_path, env=env)

    assert code == 1
    assert "Spec compile failed" in out
    assert "catastrophic" not in out.lower()
    state_path = tmp_path / "otto_logs" / "sessions" / env["OTTO_RUN_ID"] / "spec-state.jsonl"
    rows = [json.loads(line) for line in state_path.read_text(encoding="utf-8").splitlines()]
    terminal = rows[-1]
    assert terminal["kind"] == "run.finished"
    assert terminal["detail"] == "spec compile timeout exhausted after 3 attempts"
    assert terminal["extra"]["verdict"] == "blocked"
    assert terminal["extra"]["structured_reason"]["kind"] == "spec_compile_timeout_exhausted"


@pytest.mark.asyncio
async def test_run_pipeline_compile_timeout_records_blocked_terminal_without_uncaught(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    session_dir = project_dir / "otto_logs" / "sessions" / "run-runner-timeout"
    session_dir.mkdir(parents=True)

    async def fail_compile(*_args: object, **_kwargs: Any):
        exc = SpecValidationError("spec compile timeout exhausted after 3 attempts")
        exc.structured_reason = {
            "kind": "spec_compile_timeout_exhausted",
            "attempts": 3,
            "per_attempt_timeouts": [600, 1200, 1800],
            "elapsed_s": 3600.0,
            "_written_at": "2026-05-16T08:22:38Z",
        }
        raise exc

    async def unused_agent(*_args: object, **_kwargs: Any):
        raise AssertionError("pipeline should stop during compile")

    monkeypatch.setattr("otto.runner.compile_spec", fail_compile)

    result = await run_pipeline(
        "build it",
        project_dir,
        session_dir,
        project_kind="webapp",
        config={"provider": "codex-app-server", "spec_timeout": 600},
        build_agent=unused_agent,
        audit_agent=unused_agent,
        spec=None,
    )

    assert result.verdict == AuditVerdict.BLOCKED
    assert result.halted_reason.startswith("spec_compile_failed:")
    state_path = session_dir / "spec-state.jsonl"
    rows = [json.loads(line) for line in state_path.read_text(encoding="utf-8").splitlines()]
    terminal = rows[-1]
    assert terminal["kind"] == "run.finished"
    assert terminal["extra"]["verdict"] == "blocked"
    assert terminal["extra"]["structured_reason"]["kind"] == "spec_compile_timeout_exhausted"
