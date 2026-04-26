"""Otto CLI — `otto queue ...` command group (Phase 2.3-2.7).

Wrapper syntax: prepend `otto queue` to the otto command you'd already write.
    otto queue build "add csv export"                  # simple
    otto queue build "add csv export" --as csv         # explicit task id
    otto queue build "add csv export" -- --fast        # passthrough flags after --
    otto queue build "add csv export" --as csv -- --fast --rounds 3
    otto queue improve bugs "error handling" -- --rounds 3
    otto queue certify "release candidate" -- --thorough

Plus management verbs:
    otto queue ls
    otto queue show <id>
    otto queue rm <id>
    otto queue cancel <id>
    otto queue run --concurrent N

The CLI appends to queue.yml + commands.jsonl and may directly remove a
queued task from queue.yml when no watcher is running. The watcher
(`otto queue run`) is the SOLE writer of state.json.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import click
from rich.table import Table
import yaml

from otto import paths
from otto.display import CONTEXT_SETTINGS, console, rich_escape
from otto.queue.artifacts import preserve_queue_session_artifacts
from otto.theme import error_console
from otto.queue.runtime import (
    INTERRUPTED_STATUS,
    RESUMABLE_QUEUE_STATUSES,
    checkpoint_path_for_task,
    task_resume_allowed,
    task_resume_block_reason,
    task_display_status,
    watcher_alive,
)
from otto.runs.schema import TERMINAL_STATUSES


QUEUE_CLEANUP_STATUSES = set(TERMINAL_STATUSES)
QUEUE_ACTIVE_STATUSES = {"starting", "running", "terminating"}


def _git_worktree_remove(project_dir: Path, wt_path: Path, *, force: bool) -> subprocess.CompletedProcess:
    """Run `git worktree remove [-f] <path>`."""
    args = ["git", "worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(wt_path))
    return subprocess.run(args, cwd=project_dir, capture_output=True, text=True)


def _print_post_merge_preview(project_dir: Path, tasks, state) -> None:
    """Print file-overlap warnings between done-status branches.

    For each pair of done tasks, compute the intersection of files they
    each touched relative to default_branch. Overlapping files = collision
    risk at merge time.
    """
    from otto.merge import git_ops
    from otto.config import ConfigError, load_config
    try:
        cfg = load_config(project_dir / "otto.yaml")
    except (ConfigError, ValueError) as exc:
        error_console.print(f"[error]Failed to load config for post-merge preview: {rich_escape(str(exc))}[/error]")
        return
    target = str(cfg.get("default_branch", "main"))

    done_tasks = [
        t for t in tasks
        if state.get("tasks", {}).get(t.id, {}).get("status") == "done" and t.branch
    ]
    if len(done_tasks) < 2:
        console.print("\n  [dim]Post-merge preview: need 2+ done tasks to compare.[/dim]")
        return

    # Compute file sets per branch
    files_by_id: dict[str, set[str]] = {}
    for t in done_tasks:
        files_by_id[t.id] = set(git_ops.files_in_branch_diff(project_dir, t.branch, target))

    # Pairwise intersections
    overlaps: list[tuple[str, str, set[str]]] = []
    ids = [t.id for t in done_tasks]
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            common = files_by_id[ids[i]] & files_by_id[ids[j]]
            if common:
                overlaps.append((ids[i], ids[j], common))

    console.print("\n  [bold]Post-merge collision preview[/bold] "
                  f"(target: [info]{target}[/info])")
    if not overlaps:
        console.print("  [success]No file overlaps among done branches.[/success]")
        return
    for a, b, common in overlaps:
        files_str = ", ".join(sorted(common)[:5])
        if len(common) > 5:
            files_str += f" (+{len(common) - 5} more)"
        console.print(f"  [yellow]⚠[/yellow] [info]{a}[/info] vs [info]{b}[/info]: {files_str}")


# ---------- helpers ----------

def _project_dir() -> Path:
    from otto.config import ConfigError, resolve_project_dir

    try:
        return resolve_project_dir(Path.cwd())
    except ConfigError as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(2)


def _queue_manifest_pow_html(project_dir: Path, task_id: str, *, status: str) -> str:
    """Return the user-facing PoW HTML path string for a queue task."""
    manifest_path = paths.logs_dir(project_dir) / "queue" / task_id / "manifest.json"
    if not manifest_path.exists():
        return "(missing — see manifest for details)" if status == "done" else "(none yet)"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return "(missing — see manifest for details)" if status == "done" else "(none yet)"
    pow_json = manifest.get("proof_of_work_path")
    if not pow_json:
        return "(missing — see manifest for details)" if status == "done" else "(none yet)"
    pow_html = Path(str(pow_json)).with_name("proof-of-work.html")
    return str(pow_html.resolve()) if pow_html.exists() else "(missing — see manifest for details)"


def _install_runner_logging(project_dir: Path, *, quiet: bool) -> None:
    """Configure handlers for `otto.queue.runner` logger.

    - Always: file handler at otto_logs/queue/watcher.log (append mode,
      ISO-8601 timestamps, INFO level). Survives across watcher restarts.
    - Stdout: short one-line format "[HH:MM:SS] message" unless --quiet.
      INFO-level only; skips DEBUG noise.

    Idempotent: if handlers were already attached (e.g. from a prior call
    in the same process), they are removed first.
    """
    import logging
    runner_log = logging.getLogger("otto.queue.runner")
    # Avoid duplicate handlers if this is somehow called twice
    for h in list(runner_log.handlers):
        runner_log.removeHandler(h)
    runner_log.setLevel(logging.INFO)

    log_dir = paths.logs_dir(project_dir) / "queue"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_dir / "watcher.log", mode="a")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    ))
    runner_log.addHandler(file_handler)

    if not quiet:
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setLevel(logging.INFO)
        stdout_handler.setFormatter(logging.Formatter(
            "  [%(asctime)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        runner_log.addHandler(stdout_handler)


def _resolve_otto_bin() -> list[str]:
    # Test/E2E override: $OTTO_BIN may name an alternative executable. Used
    # by the e2e harness to point the watcher at a stubbed `otto`.
    override = os.environ.get("OTTO_BIN")
    if override:
        return [override]
    # Prefer the entry-point script next to the python executable
    py_dir = Path(sys.executable).parent
    candidate = py_dir / "otto"
    if candidate.exists():
        return [str(candidate)]
    # Fallback: re-invoke python with -m otto.cli
    return [sys.executable, "-m", "otto.cli"]


def _looks_like_flag(value: str) -> bool:
    """Return True if value looks like a forgotten CLI flag, not a real intent."""
    if not value:
        return True
    stripped = value.strip()
    if stripped.startswith("--"):
        return True
    if stripped.startswith("-") and len(stripped) > 1 and stripped[1].isalpha():
        return True
    return False


def _validate_target_args(command: click.Command, argv: list[str]) -> None:
    """Validate queued args against the target command signature without running it."""
    try:
        ctx = click.Context(command, info_name=command.name)
        command.parse_args(ctx, list(argv))
    except click.UsageError as exc:
        error_console.print(f"[error]{rich_escape(exc.format_message())}[/error]")
        sys.exit(2)
    finally:
        if "ctx" in locals():
            ctx.close()


def _watcher_is_alive(project_dir: Path, *, max_age_s: float = 10.0) -> bool:
    """Load state.json and return True iff the watcher heartbeat is fresh and live."""
    from otto.queue.schema import load_state

    try:
        state = load_state(project_dir)
    except Exception:
        return False
    return watcher_alive(state, max_age_s=max_age_s)


def _load_queue_or_exit(project_dir: Path):
    from otto.queue.schema import load_queue

    try:
        return load_queue(project_dir)
    except (ValueError, yaml.YAMLError) as exc:
        detail = str(exc)
        prefix = "queue.yml is malformed: "
        if detail.startswith(prefix):
            detail = detail[len(prefix):]
        error_console.print(
            f"[error]queue.yml is malformed: {rich_escape(detail)}[/error]"
        )
        sys.exit(2)


def _task_status(project_dir: Path, task_id: str) -> str:
    from otto.queue.schema import load_state

    state = load_state(project_dir)
    ts = state.get("tasks", {}).get(task_id, {"status": "queued"})
    return task_display_status(ts)


def _resume_status(project_dir: Path, task) -> tuple[str, str | None]:
    checkpoint_path = checkpoint_path_for_task(project_dir, task)
    if not task.resumable:
        return "n/a", None
    if checkpoint_path is None:
        return "no checkpoint", None
    reason = task_resume_block_reason(project_dir, task, {"status": INTERRUPTED_STATUS})
    if reason is not None:
        return reason, str(checkpoint_path)
    return "ready", str(checkpoint_path)


def _parse_resume_task_args(values: tuple[str, ...]) -> list[str]:
    task_ids: list[str] = []
    for value in values:
        for piece in value.split(","):
            task_id = piece.strip()
            if task_id:
                task_ids.append(task_id)
    seen: set[str] = set()
    ordered: list[str] = []
    for task_id in task_ids:
        if task_id in seen:
            continue
        seen.add(task_id)
        ordered.append(task_id)
    return ordered


def _print_added(task_id: str, project_dir: Path) -> None:
    from otto.queue.schema import load_state
    try:
        state = load_state(project_dir)
    except Exception:
        state = {}
    if watcher_alive(state):
        running = sum(
            1 for ts in state.get("tasks", {}).values()
            if ts.get("status") in {"initializing", "running"}
        )
        queued_before = sum(
            1 for tid, ts in state.get("tasks", {}).items()
            if ts.get("status") == "queued" and tid != task_id
        )
        console.print(
            f"  [success]Added[/success] [info]{task_id}[/info]. "
            f"Worker is running ({running} running, {queued_before} queued before this)."
        )
    else:
        console.print(
            f"  [success]Added[/success] [info]{task_id}[/info]. "
            f"Worker is not running. Start with: [info]otto queue run --concurrent N[/info]"
        )


# ---------- shared enqueue logic ----------

def _enqueue(
    *,
    command: str,
    raw_args: list[str],
    intent: str | None,
    after: list[str],
    explicit_as: str | None,
    resumable: bool,
    focus: str | None = None,
    target: str | None = None,
    explicit_intent: str | None = None,
) -> None:
    """The shared path for `otto queue build|improve|certify`."""
    from otto.config import ConfigError
    from otto.queue.enqueue import enqueue_task

    project_dir = _project_dir()
    try:
        result = enqueue_task(
            project_dir,
            command=command,
            raw_args=raw_args,
            intent=intent,
            after=after,
            explicit_as=explicit_as,
            resumable=resumable,
            focus=focus,
            target=target,
            explicit_intent=explicit_intent,
        )
    except (ConfigError, ValueError) as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(2)
    for warning in result.warnings:
        error_console.print(f"[yellow]warning: {rich_escape(warning)}[/yellow]")
    _print_added(result.task.id, project_dir)


# ---------- command group ----------

def register_queue_commands(main: click.Group) -> None:
    """Register `otto queue ...` on the main CLI group."""

    @main.group(context_settings=CONTEXT_SETTINGS)
    def queue():
        """Schedule otto build/improve/certify runs in parallel worktrees.

        Wrap any otto command with `otto queue` to defer execution:

        \b
            otto queue build "add csv export"                  # simple
            otto queue build "add csv export" --as csv         # explicit task id
            otto queue build "add csv export" -- --fast        # passthrough flags after --
            otto queue build "add csv export" --as csv -- --fast --rounds 3
            otto queue improve bugs "error handling" -- --rounds 3
            otto queue certify "release candidate" -- --thorough

        Then start the watcher to process queued tasks:

        \b
            otto queue run --concurrent 3
        """

    @queue.command(context_settings=CONTEXT_SETTINGS)
    def dashboard() -> None:
        """Deprecated queue TUI command."""
        _project_dir()
        error_console.print(
            "[error]`otto queue dashboard` has been removed. Use `otto web` for Mission Control.[/error]"
        )
        sys.exit(2)

    # ---- enqueue: build ----
    @queue.command(
        context_settings={**CONTEXT_SETTINGS, "ignore_unknown_options": True, "allow_extra_args": True},
        name="build",
    )
    @click.argument("intent", required=True)
    @click.option("--after", multiple=True, help="Task ID(s) this depends on")
    @click.option("--as", "explicit_as", default=None,
                  help="Explicit task ID (default: slug from intent)")
    @click.argument("extra_args", nargs=-1, type=click.UNPROCESSED)
    def queue_build(intent: str, after: tuple[str, ...], explicit_as: str | None,
                    extra_args: tuple[str, ...]) -> None:
        """Enqueue an `otto build` run.

        \b
            otto queue build "add csv export"                  # simple
            otto queue build "add csv export" --as csv         # explicit task id
            otto queue build "add csv export" -- --fast        # passthrough flags after --
            otto queue build "add csv export" --as csv -- --fast --rounds 3

        Intent must come before `--`. Anything after `--` is passed through
        to the inner `otto build` command.
        """
        _validate_target_args(main.commands["build"], [intent, *extra_args])
        _enqueue(
            command="build",
            raw_args=[intent, *extra_args],
            intent=intent,
            explicit_intent=intent,
            after=list(after),
            explicit_as=explicit_as,
            resumable=True,
        )

    # ---- enqueue: improve ----
    @queue.command(
        context_settings={**CONTEXT_SETTINGS, "ignore_unknown_options": True, "allow_extra_args": True},
        name="improve",
    )
    @click.argument("subcommand", type=click.Choice(["bugs", "feature", "target"]))
    @click.argument("focus_or_goal", required=False)
    @click.option("--after", multiple=True, help="Task ID(s) this depends on")
    @click.option("--as", "explicit_as", default=None, help="Explicit task ID")
    @click.argument("extra_args", nargs=-1, type=click.UNPROCESSED)
    def queue_improve(subcommand: str, focus_or_goal: str | None,
                      after: tuple[str, ...], explicit_as: str | None,
                      extra_args: tuple[str, ...]) -> None:
        """Enqueue an `otto improve <bugs|feature|target>` run.

        \b
            otto queue improve bugs "error handling"
            otto queue improve bugs "error handling" -- --rounds 3
            otto queue improve target "p95 latency under 100ms" -- --strict

        For subcommands with an explicit focus/goal, it must come before `--`.
        Anything after `--` is passed through to the inner `otto improve`.
        """
        from otto.config import ConfigError, resolve_intent_for_enqueue

        if subcommand == "target" and not str(focus_or_goal or "").strip():
            error_console.print("[error]Missing argument 'GOAL' for `otto queue improve target`.[/error]")
            sys.exit(2)
        try:
            snapshot_intent = resolve_intent_for_enqueue(_project_dir())
        except (ConfigError, ValueError) as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(2)
        # Build raw_args: [subcommand, focus_or_goal?, ...extra]
        raw = [subcommand]
        if focus_or_goal is not None:
            raw.append(focus_or_goal)
        raw.extend(extra_args)
        improve_group = main.commands["improve"]
        improve_command = improve_group.commands[subcommand]
        validation_argv = ([focus_or_goal] if focus_or_goal is not None else []) + list(extra_args)
        _validate_target_args(improve_command, validation_argv)

        focus = focus_or_goal if subcommand in ("bugs", "feature") else None
        target = focus_or_goal if subcommand == "target" else None

        _enqueue(
            command="improve",
            raw_args=raw,
            intent=snapshot_intent,
            explicit_intent=focus_or_goal,
            after=list(after),
            explicit_as=explicit_as,
            resumable=True,
            focus=focus,
            target=target,
        )

    # ---- enqueue: certify ----
    @queue.command(
        context_settings={**CONTEXT_SETTINGS, "ignore_unknown_options": True, "allow_extra_args": True},
        name="certify",
    )
    @click.argument("intent", required=False)
    @click.option("--after", multiple=True, help="Task ID(s) this depends on")
    @click.option("--as", "explicit_as", default=None, help="Explicit task ID")
    @click.argument("extra_args", nargs=-1, type=click.UNPROCESSED)
    def queue_certify(intent: str | None, after: tuple[str, ...],
                      explicit_as: str | None, extra_args: tuple[str, ...]) -> None:
        """Enqueue an `otto certify` run.

        \b
            otto queue certify
            otto queue certify "release candidate"
            otto queue certify "release candidate" -- --thorough

        If you pass an explicit intent, it must come before `--`. Anything
        after `--` is passed through to the inner `otto certify`.
        """
        from otto.config import ConfigError, resolve_intent_for_enqueue

        try:
            resolved = resolve_intent_for_enqueue(_project_dir(), explicit=intent)
        except (ConfigError, ValueError) as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(2)
        raw = [intent] if intent else []
        raw.extend(extra_args)
        _validate_target_args(main.commands["certify"], raw)
        _enqueue(
            command="certify",
            raw_args=raw,
            intent=resolved,
            explicit_intent=intent,
            after=list(after),
            explicit_as=explicit_as,
            resumable=False,  # certify has no --resume per cli.py:617
        )

    # ---- ls ----
    @queue.command(context_settings=CONTEXT_SETTINGS)
    @click.option("--all", "show_all", is_flag=True,
                  help="Include removed tasks (hidden by default)")
    @click.option("--post-merge-preview", is_flag=True,
                  help="Show file-overlap preview between done branches (collision risk)")
    def ls(show_all: bool, post_merge_preview: bool) -> None:
        """List tasks in the queue with their current state."""
        from otto.queue.schema import load_queue, load_state
        try:
            project_dir = _project_dir()
            tasks = load_queue(project_dir)
            state = load_state(project_dir)
        except Exception as exc:
            error_console.print(f"[error]Failed to load queue: {rich_escape(str(exc))}[/error]")
            sys.exit(1)
        if not tasks:
            console.print("  Queue is empty. Add tasks with `otto queue build|improve|certify ...`")
            return
        table = Table(show_header=True, header_style="bold")
        table.add_column("ID")
        table.add_column("STATUS")
        table.add_column("MODE")
        table.add_column("RESUME")
        table.add_column("COST", justify="right")
        table.add_column("DURATION", justify="right")
        table.add_column("BLOCKED-ON")
        any_shown = False
        for t in tasks:
            ts = state.get("tasks", {}).get(t.id, {"status": "queued"})
            status = task_display_status(ts)
            if status == "removed" and not show_all:
                continue
            any_shown = True
            mode = t.command_argv[0] if t.command_argv else "?"
            resume_status, _resume_path = _resume_status(project_dir, t)
            cost = ts.get("cost_usd")
            cost_s = f"${cost:.2f}" if isinstance(cost, (int, float)) else "—"
            dur = ts.get("duration_s")
            dur_s = f"{dur:.0f}s" if isinstance(dur, (int, float)) else "—"
            after_s = ", ".join(t.after) if t.after else "—"
            table.add_row(
                t.id,
                _color_status(status),
                mode,
                _color_resume_status(status, resume_status),
                cost_s,
                dur_s,
                after_s,
            )
        if not any_shown:
            console.print("  Queue is empty (all tasks are removed; use --all to show).")
            return
        console.print(table)
        if not watcher_alive(state):
            console.print("\n  [yellow]Worker is not running.[/yellow] "
                          "Start with: [info]otto queue run --concurrent N[/info]")

        if post_merge_preview:
            _print_post_merge_preview(project_dir, tasks, state)

    # ---- show ----
    @queue.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("task_id")
    def show(task_id: str) -> None:
        """Show full details of one task."""
        from otto.queue.schema import load_state
        project_dir = _project_dir()
        tasks = _load_queue_or_exit(project_dir)
        state = load_state(project_dir)
        task = next((t for t in tasks if t.id == task_id), None)
        if task is None:
            error_console.print(f"[error]No such task: {task_id!r}[/error]")
            sys.exit(2)
        ts = state.get("tasks", {}).get(task_id, {"status": "queued"})
        display_status = task_display_status(ts)
        console.print(f"\n  [bold]Task:[/bold] {task.id}")
        console.print(f"  [dim]Status:[/dim] {_color_status(display_status)}")
        console.print(f"  [dim]Command:[/dim] otto {' '.join(task.command_argv)}")
        if task.resolved_intent:
            console.print(f"  [dim]Intent:[/dim] {rich_escape(task.resolved_intent)}")
        if task.focus:
            console.print(f"  [dim]Focus:[/dim] {rich_escape(task.focus)}")
        if task.target:
            console.print(f"  [dim]Target:[/dim] {rich_escape(task.target)}")
        if task.after:
            console.print(f"  [dim]Depends on:[/dim] {', '.join(task.after)}")
        if task.branch:
            console.print(f"  [dim]Branch:[/dim] {task.branch}")
        if task.worktree:
            console.print(f"  [dim]Worktree:[/dim] {task.worktree}")
        console.print(f"  [dim]Resumable:[/dim] {task.resumable}")
        resume_status, checkpoint_path = _resume_status(project_dir, task)
        console.print(f"  [dim]Resume status:[/dim] {_color_resume_status(display_status, resume_status)}")
        if checkpoint_path:
            console.print(f"  [dim]Checkpoint:[/dim] {checkpoint_path}")
        console.print(f"  [dim]Added at:[/dim] {task.added_at}")
        if ts.get("started_at"):
            console.print(f"  [dim]Started at:[/dim] {ts['started_at']}")
        if ts.get("finished_at"):
            console.print(f"  [dim]Finished at:[/dim] {ts['finished_at']}")
        if isinstance(ts.get("cost_usd"), (int, float)):
            console.print(f"  [dim]Cost:[/dim] ${ts['cost_usd']:.2f}")
        if isinstance(ts.get("duration_s"), (int, float)):
            console.print(f"  [dim]Duration:[/dim] {ts['duration_s']:.1f}s")
        if ts.get("manifest_path"):
            console.print(f"  [dim]Manifest:[/dim] {ts['manifest_path']}")
        console.print(
            "  [dim]Proof-of-work:[/dim] "
            f"{_queue_manifest_pow_html(project_dir, task_id, status=ts.get('status', 'queued'))}"
        )
        if ts.get("failure_reason"):
            console.print(f"  [dim]Failure reason:[/dim] [red]{rich_escape(ts['failure_reason'])}[/red]")
        child = ts.get("child")
        if child:
            console.print(f"  [dim]Child PID:[/dim] {child.get('pid')}")
            console.print(f"  [dim]Child cwd:[/dim] {child.get('cwd')}")
        console.print()

    # ---- rm ----
    @queue.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("task_id")
    def rm(task_id: str) -> None:
        """Remove a task from the queue."""
        from otto.queue.schema import append_command, load_state, remove_task

        project_dir = _project_dir()
        tasks = _load_queue_or_exit(project_dir)
        task = next((t for t in tasks if t.id == task_id), None)
        if task is None:
            error_console.print(f"[error]No such task: {task_id!r}[/error]")
            sys.exit(2)
        task_state = load_state(project_dir).get("tasks", {}).get(task_id, {})
        status = task_display_status(task_state)
        if status in QUEUE_CLEANUP_STATUSES:
            action = (
                f"use `otto queue resume {task_id}` to continue it, or "
                f"`otto queue cleanup {task_id}` to discard its worktree"
                if status in RESUMABLE_QUEUE_STATUSES and task_resume_allowed(project_dir, task, task_state)
                else f"use `otto queue cleanup {task_id}` to clear finished tasks"
            )
            error_console.print(
                "[error]task "
                f"{task_id} is {status}; {action}."
                "[/error]"
            )
            sys.exit(2)
        if not _watcher_is_alive(project_dir):
            if status != "queued":
                if status in QUEUE_ACTIVE_STATUSES:
                    console.print(
                        f"  [yellow]Task [info]{task_id}[/info] is marked {status}, but the worker is "
                        "not running.[/yellow] Start the watcher with: "
                        "[info]otto queue run --concurrent N[/info] "
                        "for safe cleanup, or send SIGTERM to the child process group manually."
                    )
                    return
                error_console.print(
                    f"[error]task {task_id} is {status}; only queued tasks can be removed directly.[/error]"
                )
                sys.exit(2)
            if not remove_task(project_dir, task_id):
                error_console.print(f"[error]No such task: {task_id!r}[/error]")
                sys.exit(2)
            console.print(f"  Removed [info]{task_id}[/info] from queue.")
            return
        append_command(project_dir, {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cmd": "remove",
            "id": task_id,
        })
        console.print("  Remove queued; watcher will apply within ~1s.")

    # ---- cancel ----
    @queue.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("task_id")
    def cancel(task_id: str) -> None:
        """Signal a running task to stop (process group SIGTERM)."""
        from otto.queue.schema import append_command, load_state, remove_task

        project_dir = _project_dir()
        tasks = _load_queue_or_exit(project_dir)
        if not any(t.id == task_id for t in tasks):
            error_console.print(f"[error]No such task: {task_id!r}[/error]")
            sys.exit(2)
        if not _watcher_is_alive(project_dir):
            state = load_state(project_dir)
            ts = state.get("tasks", {}).get(task_id, {"status": "queued"})
            status = ts.get("status", "queued")
            if status == "queued":
                if not remove_task(project_dir, task_id):
                    error_console.print(f"[error]No such task: {task_id!r}[/error]")
                    sys.exit(2)
                console.print(
                    f"  Task [info]{task_id}[/info] was never started. Removed from queue."
                )
                return
            if status in {"running", "terminating"}:
                console.print(
                    f"  [yellow]Task [info]{task_id}[/info] is marked {status}, but the worker is "
                    "not running.[/yellow] Start the watcher with: "
                    "[info]otto queue run --concurrent N[/info] "
                    "for safe cleanup, or send SIGTERM to the child process group manually."
                )
                return
            console.print(
                f"  Task [info]{task_id}[/info] is already "
                f"{rich_escape(str(status))}. No queue change made."
            )
            return
        status = _task_status(project_dir, task_id)
        if status == "queued":
            append_command(project_dir, {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "cmd": "cancel",
                "id": task_id,
            })
            console.print("  Cancel queued; watcher will remove from queue.")
            return
        if status == "running":
            append_command(project_dir, {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "cmd": "cancel",
                "id": task_id,
            })
            console.print("  Cancel queued; watcher will signal the task.")
            return
        if status == "terminating":
            console.print("  Cancel already in progress.")
            return
        error_console.print(
            f"[error]task {task_id} is {status}; nothing to cancel.[/error]"
        )
        sys.exit(2)

    # ---- resume ----
    @queue.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("task_ids", nargs=-1)
    @click.option("--select", is_flag=True,
                  help="Pick checkpoint-resumable tasks from an interactive checkbox list")
    def resume(task_ids: tuple[str, ...], select: bool) -> None:
        """Resume interrupted or checkpointed failed queue tasks.

        \b
            otto queue resume                  # resume all tasks with a checkpoint
            otto queue resume labels           # resume one task
            otto queue resume labels,due       # resume multiple tasks
            otto queue resume --select         # removed; use explicit ids or `otto web`

        Resumed tasks move to the head of the queue and restart with `--resume`.
        """
        from otto.queue.schema import append_command, load_state, reorder_tasks

        project_dir = _project_dir()
        tasks = _load_queue_or_exit(project_dir)
        tasks_by_id = {task.id: task for task in tasks}
        state = load_state(project_dir)
        resumable_status_ids = [
            task.id
            for task in tasks
            if task_display_status(state.get("tasks", {}).get(task.id, {})) in RESUMABLE_QUEUE_STATUSES
        ]
        if not resumable_status_ids:
            console.print("  No checkpoint-resumable tasks are waiting to resume.")
            return
        resumable_ids = [
            task_id for task_id in resumable_status_ids
            if task_resume_allowed(project_dir, tasks_by_id[task_id], state.get("tasks", {}).get(task_id, {}))
        ]

        selected_ids = _parse_resume_task_args(task_ids)
        if select:
            error_console.print(
                "[error]`otto queue resume --select` has been removed with the TUI. "
                "Pass task IDs explicitly or use `otto web`.[/error]"
            )
            sys.exit(2)
        elif not selected_ids:
            selected_ids = list(resumable_ids)
            if not selected_ids:
                console.print("  Resume-eligible tasks exist, but none have a resumable checkpoint.")
                return

        missing = [task_id for task_id in selected_ids if task_id not in tasks_by_id]
        if missing:
            error_console.print(
                f"[error]Unknown task id(s): {rich_escape(', '.join(missing))}[/error]"
            )
            sys.exit(2)

        invalid_status = [
            task_id for task_id in selected_ids
            if task_display_status(state.get("tasks", {}).get(task_id, {})) not in RESUMABLE_QUEUE_STATUSES
        ]
        if invalid_status:
            error_console.print(
                "[error]Only interrupted, failed, cancelled, or paused tasks can be resumed.[/error]\n"
                f"  Not resume-eligible: {rich_escape(', '.join(invalid_status))}"
            )
            sys.exit(2)

        unavailable = {
            task_id: task_resume_block_reason(
                project_dir,
                tasks_by_id[task_id],
                state.get("tasks", {}).get(task_id, {}),
            )
            for task_id in selected_ids
        }
        unavailable = {task_id: reason for task_id, reason in unavailable.items() if reason}
        if unavailable:
            details = "; ".join(
                f"{task_id}: {reason}" for task_id, reason in unavailable.items()
            )
            error_console.print(
                "[error]Selected task(s) cannot be resumed from checkpoint.[/error]\n"
                f"  {rich_escape(details)}"
            )
            sys.exit(2)

        reorder_tasks(project_dir, selected_ids)
        for task_id in selected_ids:
            append_command(project_dir, {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "cmd": "resume",
                "id": task_id,
            })

        if _watcher_is_alive(project_dir):
            console.print(
                f"  Resuming [info]{', '.join(selected_ids)}[/info]; watcher will re-queue them within ~1s."
            )
            return
        console.print(
            f"  Marked [info]{', '.join(selected_ids)}[/info] to resume. "
            "Start the watcher with [info]otto queue run --concurrent N[/info]."
        )

    # ---- cleanup (Phase 6.4) ----
    @queue.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("task_ids", nargs=-1)
    @click.option("--done", "scope_done", is_flag=True,
                  help="Remove worktrees for tasks with status=done (default)")
    @click.option("--all", "scope_all", is_flag=True,
                  help="Also include failed/cancelled/removed/interrupted tasks")
    @click.option("--force", is_flag=True, help="Remove even if worktree is dirty")
    def cleanup(task_ids: tuple[str, ...], scope_done: bool, scope_all: bool, force: bool) -> None:
        """Explicitly remove worktrees for done/failed tasks.

        Branches and manifests are preserved. Only the working tree directory
        is removed (`git worktree remove <path>`). To remove the branch as
        well, use `git branch -d <branch>` afterward.

        \b
        Examples:
            otto queue cleanup --done       # remove done-task worktrees
            otto queue cleanup --all        # also failed/cancelled/removed/interrupted
            otto queue cleanup t1 t2        # specific tasks
        """
        from otto.queue.schema import append_command, load_queue, load_state, remove_task, write_state
        project_dir = _project_dir()
        try:
            tasks = load_queue(project_dir)
            state = load_state(project_dir)
        except Exception as exc:
            error_console.print(f"[error]Failed to load queue: {rich_escape(str(exc))}[/error]")
            sys.exit(1)

        # Decide which tasks to clean
        targets: list[Any] = []
        statuses_in_scope = {"done"}
        if scope_all:
            statuses_in_scope = QUEUE_CLEANUP_STATUSES
        if task_ids:
            for tid in task_ids:
                t = next((t for t in tasks if t.id == tid), None)
                if t is None:
                    error_console.print(f"[error]No such task: {tid!r}[/error]")
                    sys.exit(2)
                status = task_display_status(state.get("tasks", {}).get(t.id, {}))
                if status not in QUEUE_CLEANUP_STATUSES:
                    error_console.print(
                        "[error]Only terminal tasks can be cleaned up.[/error]\n"
                        f"  {rich_escape(t.id)} is {rich_escape(status)}; cancel or remove it first."
                    )
                    sys.exit(2)
                targets.append(t)
        else:
            # Default: tasks matching the scope filter
            for t in tasks:
                ts = state.get("tasks", {}).get(t.id, {})
                if ts.get("status") in statuses_in_scope:
                    targets.append(t)

        if not targets:
            console.print("  No worktrees to clean up.")
            return

        terminal_statuses = {"done", "failed", "cancelled", "removed", INTERRUPTED_STATUS}
        non_terminal = [
            t.id
            for t in targets
            if task_display_status(state.get("tasks", {}).get(t.id, {"status": "queued"})) not in terminal_statuses
        ]
        if non_terminal:
            error_console.print(
                "[error]cleanup only applies to terminal tasks; still active: "
                f"{rich_escape(', '.join(non_terminal))}[/error]"
            )
            sys.exit(2)

        if _watcher_is_alive(project_dir):
            for t in targets:
                append_command(project_dir, {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "cmd": "cleanup",
                    "id": t.id,
                })
            console.print(
                "  Cleanup queued; watcher will remove "
                f"{len(targets)} terminal task{'s' if len(targets) != 1 else ''} from the board."
            )
            return

        cleaned = 0
        skipped = 0
        for t in targets:
            removed_worktree = False
            if t.worktree:
                wt_path = project_dir / t.worktree
                if wt_path.exists():
                    try:
                        preserve_queue_session_artifacts(
                            project_dir,
                            task_id=t.id,
                            worktree_path=wt_path,
                            strict=False,
                        )
                    except Exception as exc:
                        console.print(
                            f"  [yellow]✗[/yellow] could not preserve queue artifacts for {t.worktree}: "
                            f"{rich_escape(str(exc))}"
                        )
                        skipped += 1
                        continue
                    r = _git_worktree_remove(project_dir, wt_path, force=force)
                    if r.returncode == 0:
                        console.print(f"  [success]✓[/success] removed [info]{t.worktree}[/info] (task: {t.id})")
                        removed_worktree = True
                    else:
                        console.print(f"  [yellow]✗[/yellow] could not remove {t.worktree}: "
                                      f"{rich_escape(r.stderr.strip())}")
                        skipped += 1
                        continue
            remove_task(project_dir, t.id)
            state.get("tasks", {}).pop(t.id, None)
            cleaned += 1
            if not removed_worktree:
                console.print(f"  [success]✓[/success] removed terminal task [info]{t.id}[/info] from queue.")
        write_state(project_dir, state)
        console.print(
            f"\n  Done. Cleaned {cleaned}, skipped {skipped}. "
            f"Manifests preserved at otto_logs/queue/<task-id>/; history preserved."
        )

    # ---- run (the watcher) ----
    @queue.command(context_settings=CONTEXT_SETTINGS)
    @click.option("--concurrent", "-j", default=None, type=click.IntRange(1),
                  help="Max concurrent tasks (default from otto.yaml queue.concurrent)")
    @click.option("--quiet", is_flag=True,
                  help="Suppress watcher event lines (spawn/reap/cancel) on stdout")
    @click.option("--no-dashboard", is_flag=True,
                  help="Deprecated no-op; the watcher always uses prefixed stdout")
    @click.option(
        "--exit-when-empty",
        is_flag=True,
        help="Exit cleanly once the queue has no queued or in-flight tasks",
    )
    def run(
        concurrent: int | None,
        quiet: bool,
        no_dashboard: bool,
        exit_when_empty: bool,
    ) -> None:
        """Start the foreground queue watcher. Run in a tmux pane like `vite dev`."""
        from otto.config import ConfigError, load_config
        from otto.queue.runner import (
            Runner,
            WatcherAlreadyRunning,
            runner_config_from_otto_config,
        )
        project_dir = _project_dir()
        # `otto queue build/improve/certify` work without otto.yaml using
        # defaults (see _enqueue → `load_config(project_dir / "otto.yaml")`
        # which returns DEFAULT_CONFIG when absent). Be consistent: the
        # watcher uses defaults too if otto.yaml is missing.
        try:
            cfg = load_config(project_dir / "otto.yaml")
        except (ConfigError, ValueError) as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(2)
        rcfg = runner_config_from_otto_config(cfg)
        if concurrent is not None:
            rcfg.concurrent = concurrent
        rcfg.exit_when_empty = exit_when_empty
        otto_bin = _resolve_otto_bin()

        # Install handlers so watcher's spawn/reap/cancel/heartbeat events
        # appear on stdout (and persist to otto_logs/queue/watcher.log).
        rcfg.prefix_child_output = True
        _install_runner_logging(project_dir, quiet=quiet)

        try:
            runner = Runner(project_dir, rcfg, otto_bin=otto_bin)
        except Exception as exc:
            error_console.print(f"[error]Runner init failed: {rich_escape(str(exc))}[/error]")
            sys.exit(1)

        print("Queue worker", flush=True)
        print(f"[watcher] concurrent={rcfg.concurrent}", flush=True)
        print("[watcher] event_log=otto_logs/queue/watcher.log", flush=True)
        print("[watcher] Ctrl-C=graceful Ctrl-C-twice=immediate\n", flush=True)

        try:
            exit_code = runner.run()
        except WatcherAlreadyRunning as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(1)
        sys.exit(exit_code)


def _color_status(status: str) -> str:
    colors = {
        "queued": "yellow",
        "running": "cyan",
        "terminating": "magenta",
        INTERRUPTED_STATUS: "bright_blue",
        "done": "green",
        "failed": "red",
        "cancelled": "magenta",
        "removed": "dim",
    }
    return f"[{colors.get(status, 'white')}]{status}[/{colors.get(status, 'white')}]"


def _color_resume_status(task_status: str, resume_status: str) -> str:
    if task_status != INTERRUPTED_STATUS:
        return "[dim]—[/dim]" if resume_status in {"ready", "no checkpoint", "n/a"} else rich_escape(resume_status)
    colors = {
        "ready": "green",
        "no checkpoint": "red",
        "n/a": "dim",
    }
    color = colors.get(resume_status, "white")
    return f"[{color}]{resume_status}[/{color}]"
