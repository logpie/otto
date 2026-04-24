from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from otto.merge.orchestrator import MergeOptions, run_merge
from otto.pipeline import build_agentic_v3
from otto.queue.runner import Runner, RunnerConfig


@pytest.mark.tui
@pytest.mark.asyncio
async def test_dashboard_startup_calls_registry_gc(tmp_path: Path, monkeypatch) -> None:
    from otto.tui.mission_control import MissionControlApp

    calls: list[Path] = []
    monkeypatch.setattr("otto.tui.mission_control.garbage_collect_live_records", lambda project_dir: calls.append(project_dir) or [])

    app = MissionControlApp(tmp_path)

    async with app.run_test() as pilot:
        await pilot.pause()

    assert calls == [tmp_path]


def test_queue_runner_startup_calls_registry_gc(tmp_path: Path, monkeypatch) -> None:
    calls: list[Path] = []
    monkeypatch.setattr("otto.queue.runner.garbage_collect_live_records", lambda project_dir: calls.append(project_dir) or [])
    monkeypatch.setattr("otto.queue.runner.acquire_lock", lambda project_dir: object())
    monkeypatch.setattr(Runner, "_install_signal_handlers", lambda self: None)
    monkeypatch.setattr(Runner, "_reconcile_on_startup", lambda self: None)
    monkeypatch.setattr(Runner, "_update_watcher_state", lambda self: None)

    runner = Runner(tmp_path, RunnerConfig(), otto_bin="otto")
    runner._begin_run()

    assert calls == [tmp_path]


@pytest.mark.asyncio
async def test_atomic_build_startup_calls_registry_gc(tmp_path: Path, monkeypatch) -> None:
    calls: list[Path] = []
    monkeypatch.setattr("otto.runs.registry.garbage_collect_live_records", lambda project_dir: calls.append(project_dir) or [])

    class _StopStartup(RuntimeError):
        pass

    monkeypatch.setattr("otto.config.ensure_safe_repo_state", lambda *args, **kwargs: (_ for _ in ()).throw(_StopStartup()))

    with pytest.raises(_StopStartup):
        await build_agentic_v3(
            "ship it",
            tmp_path,
            {},
            manage_checkpoint=False,
            record_intent=False,
            run_id="run-build-1",
        )

    assert calls == [tmp_path]


def test_merge_startup_calls_registry_gc(tmp_path: Path, monkeypatch) -> None:
    calls: list[Path] = []
    monkeypatch.setattr("otto.runs.registry.garbage_collect_live_records", lambda project_dir: calls.append(project_dir) or [])
    monkeypatch.setattr("otto.merge.orchestrator._repair_merge_history", lambda project_dir: None)
    monkeypatch.setattr("otto.merge.orchestrator.git_ops.current_branch", lambda project_dir: "feature/not-target")

    result = asyncio.run(
        run_merge(
            project_dir=tmp_path,
            config={},
            options=MergeOptions(target="main"),
        )
    )

    assert calls == [tmp_path]
    assert result.success is False
