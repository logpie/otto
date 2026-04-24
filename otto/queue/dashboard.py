"""Compatibility wrappers for queue dashboard flows."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import SelectionList, Static

from otto.queue.runtime import watcher_alive as watcher_is_alive
from otto.queue.runner import Runner, RunnerConfig
from otto.queue.schema import QueueTask, load_state
from otto.theme import error_console
from otto.tui.mission_control import MissionControlApp
from otto.mission_control.model import MissionControlFilters


def _render_dashboard_closed_notice(running_count: int) -> str | None:
    if running_count <= 0:
        return None
    task_label = "task" if running_count == 1 else "tasks"
    complete_label = "it completes" if running_count == 1 else "they complete"
    return "\n".join(
        [
            "Dashboard closed. Watcher continues running in foreground.",
            f"{running_count} {task_label} still running; reopen with `otto queue dashboard` while {complete_label}.",
            "Press Ctrl-C to interrupt (twice for immediate stop).",
        ]
    )


def _print_dashboard_closed_notice(running_count: int, *, stream=None) -> bool:
    message = _render_dashboard_closed_notice(running_count)
    if message is None:
        return False
    print(message, file=stream or sys.stdout, flush=True)
    return True


class ResumeSelectionApp(App[list[str]]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #resume-header, #resume-footer {
        padding: 0 1;
    }

    #resume-header {
        color: cyan;
    }

    #resume-footer {
        color: green;
    }

    #resume-list {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("enter", "confirm", "Confirm", show=False),
        Binding("q", "cancel", "Cancel", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, tasks: Sequence[QueueTask]) -> None:
        super().__init__()
        self._tasks = list(tasks)

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold]Resume Interrupted Tasks[/bold]\n"
            "Space toggles a task. Enter confirms the selection.",
            id="resume-header",
        )
        yield SelectionList(*[(self._label(task), task.id, True) for task in self._tasks], id="resume-list")
        yield Static("space toggle · enter confirm · q cancel", id="resume-footer")

    def on_mount(self) -> None:
        self.query_one("#resume-list", SelectionList).focus()

    def action_confirm(self) -> None:
        self.exit(list(self.query_one("#resume-list", SelectionList).selected))

    def action_cancel(self) -> None:
        self.exit([])

    @staticmethod
    def _label(task: QueueTask) -> str:
        command = " ".join(task.command_argv[:3]).strip()
        return f"{task.id}  {command}"


def select_resume_tasks(project_dir: Path, tasks: Sequence[QueueTask]) -> list[str]:
    del project_dir
    if not sys.stdout.isatty() or os.environ.get("OTTO_NO_TUI"):
        error_console.print(
            "[error]`otto queue resume --select` requires an interactive terminal.[/error]\n"
            "  Pass task ids explicitly instead, for example: `otto queue resume labels,due`."
        )
        raise SystemExit(2)
    return list(ResumeSelectionApp(tasks).run())


async def _run_dashboard_async(app: MissionControlApp, runner: Runner, *, quiet: bool) -> int:
    runner_task = asyncio.create_task(runner.run_async())
    app_task = asyncio.create_task(app.run_async(mouse=app.dashboard_mouse))

    while True:
        done, _pending = await asyncio.wait({runner_task, app_task}, return_when=asyncio.FIRST_COMPLETED)
        if app_task in done:
            exc = app_task.exception()
            if exc is not None:
                from otto.cli_queue import _install_runner_logging
                from otto.display import console

                _install_runner_logging(app.project_dir, quiet=quiet)
                console.print("  [yellow]Dashboard crashed; continuing with prefixed stdout.[/yellow]")
                return await runner_task
            if not runner_task.done():
                runner.shutdown_level = runner.shutdown_level or "graceful"
                _print_dashboard_closed_notice(app.state.live_runs.active_count)
                return await runner_task
            return runner_task.result()

        if runner_task in done:
            exc = runner_task.exception()
            if exc is not None:
                if not app_task.done():
                    app.exit(1)
                    try:
                        await app_task
                    except Exception:
                        pass
                raise exc
            if not app_task.done():
                app.exit(runner_task.result())
                try:
                    await app_task
                except Exception:
                    pass
            return runner_task.result()


def _queue_filters() -> MissionControlFilters:
    return MissionControlFilters(type_filter="queue")


def run_dashboard(
    project_dir: Path,
    *,
    concurrent: int,
    quiet: bool,
    dashboard_mouse: bool = False,
    runner_config: RunnerConfig,
    otto_bin: list[str] | str,
) -> int:
    from otto.cli_queue import _install_runner_logging

    del concurrent  # kept for CLI compatibility while the wrapper delegates to Mission Control
    _install_runner_logging(project_dir, quiet=True)
    runner = Runner(project_dir, runner_config, otto_bin=otto_bin)
    app = MissionControlApp(
        project_dir,
        initial_filters=_queue_filters(),
        dashboard_mouse=dashboard_mouse,
        queue_compat=True,
    )
    return asyncio.run(_run_dashboard_async(app, runner, quiet=quiet))


def run_dashboard_viewer(
    project_dir: Path,
    *,
    dashboard_mouse: bool = False,
) -> int:
    state = load_state(project_dir)
    if not watcher_is_alive(state):
        error_console.print(
            "[error]No active queue watcher found.[/error]\n"
            "  Start one with `otto queue run --concurrent N`, then reopen with `otto queue dashboard`."
        )
        return 1
    app = MissionControlApp(
        project_dir,
        initial_filters=_queue_filters(),
        dashboard_mouse=dashboard_mouse,
        queue_compat=True,
    )
    return int(app.run(mouse=dashboard_mouse) or 0)


__all__ = [
    "ResumeSelectionApp",
    "_print_dashboard_closed_notice",
    "_render_dashboard_closed_notice",
    "run_dashboard",
    "run_dashboard_viewer",
    "select_resume_tasks",
]
