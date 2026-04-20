"""Otto CLI — `otto queue ...` command group (Phase 2.3-2.7).

Wrapper syntax: prepend `otto queue` to the otto command you'd already write.
    otto queue build "add csv export"        # enqueue a build
    otto queue improve bugs --rounds 3       # enqueue an improve
    otto queue certify --thorough            # enqueue a certify

Plus management verbs:
    otto queue ls
    otto queue show <id>
    otto queue rm <id>
    otto queue cancel <id>
    otto queue run --concurrent N

The CLI is **strictly append-only** to queue.yml + commands.jsonl.
The watcher (`otto queue run`) is the SOLE writer of state.json.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import click
from rich.table import Table

from otto.display import CONTEXT_SETTINGS, console, rich_escape
from otto.theme import error_console


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
    from otto.config import load_config
    cfg = load_config(project_dir / "otto.yaml") if (project_dir / "otto.yaml").exists() else {}
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
    return Path.cwd()


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

    log_dir = project_dir / "otto_logs" / "queue"
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


def _watcher_alive(state: dict[str, Any], *, max_age_s: float = 10.0) -> bool:
    """Return True iff state.json's watcher heartbeat is fresh (<max_age_s old)."""
    w = state.get("watcher")
    if not w:
        return False
    hb = w.get("heartbeat")
    if not hb:
        return False
    try:
        from datetime import datetime, timezone
        when = datetime.strptime(hb, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = (datetime.now(tz=timezone.utc) - when).total_seconds()
        return age < max_age_s
    except Exception:
        return False


def _print_added(task_id: str, project_dir: Path) -> None:
    from otto.queue.schema import load_state
    try:
        state = load_state(project_dir)
    except Exception:
        state = {}
    if _watcher_alive(state):
        running = sum(1 for ts in state.get("tasks", {}).values() if ts.get("status") == "running")
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
) -> None:
    """The shared path for `otto queue build|improve|certify`."""
    from otto.branching import compute_branch_name
    from otto.config import load_config
    from otto.queue.ids import generate_task_id, validate_after_refs
    from otto.queue.schema import QueueTask, append_task, load_queue
    project_dir = _project_dir()

    # Reject user-supplied --resume in the args (watcher is sole resume injector,
    # per Codex round 3 finding)
    if "--resume" in raw_args:
        error_console.print(
            "[error]--resume is not allowed in queued commands.[/error]\n"
            "  The queue runner injects --resume automatically when it respawns "
            "a task after a watcher restart."
        )
        sys.exit(2)

    # First-touch init: idempotent setup of .gitignore + .gitattributes for
    # users who skipped `otto setup`. Without this, `otto merge` later fails
    # its working_tree_clean and bookkeeping-driver preconditions.
    config = load_config(project_dir / "otto.yaml")
    try:
        from otto.config import first_touch_bookkeeping
        first_touch_bookkeeping(project_dir, config)
    except Exception as exc:
        from otto.theme import error_console as _err
        _err.print(f"[yellow]warning: bookkeeping setup skipped: {exc}[/yellow]")

    existing = load_queue(project_dir)
    existing_ids = [t.id for t in existing]

    try:
        task_id = generate_task_id(
            intent=intent, command=command,
            existing_ids=existing_ids, explicit_as=explicit_as,
        )
        if after:
            validate_after_refs(
                after=after, self_id=task_id, all_ids=existing_ids,
            )
    except ValueError as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(2)

    # Compose full argv: [<command>, ...raw_args]
    argv = [command, *raw_args]
    worktree_dir = str(config.get("queue", {}).get("worktree_dir", ".worktrees"))
    branch = compute_branch_name(command, task_id)
    worktree = str(Path(worktree_dir) / task_id)

    task = QueueTask(
        id=task_id,
        command_argv=argv,
        after=after,
        resumable=resumable,
        added_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        resolved_intent=intent,
        focus=focus,
        target=target,
        branch=branch,
        worktree=worktree,
    )
    try:
        append_task(project_dir, task)
    except ValueError as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(2)
    _print_added(task_id, project_dir)


# ---------- command group ----------

def register_queue_commands(main: click.Group) -> None:
    """Register `otto queue ...` on the main CLI group."""

    @main.group(context_settings=CONTEXT_SETTINGS)
    def queue():
        """Schedule otto build/improve/certify runs in parallel worktrees.

        Wrap any otto command with `otto queue` to defer execution:

        \b
            otto queue build "add csv export"
            otto queue improve bugs --rounds 3
            otto queue certify --thorough

        Then start the watcher to process queued tasks:

        \b
            otto queue run --concurrent 3
        """

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
        """Enqueue an `otto build` run."""
        _validate_target_args(main.commands["build"], [intent, *extra_args])
        _enqueue(
            command="build",
            raw_args=[intent, *extra_args],
            intent=intent,
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
        """Enqueue an `otto improve <bugs|feature|target>` run."""
        from otto.config import resolve_intent_for_enqueue

        snapshot_intent = resolve_intent_for_enqueue(_project_dir())
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
        """Enqueue an `otto certify` run."""
        from otto.config import resolve_intent_for_enqueue

        resolved = resolve_intent_for_enqueue(_project_dir(), explicit=intent)
        raw = [intent] if intent else []
        raw.extend(extra_args)
        _validate_target_args(main.commands["certify"], raw)
        _enqueue(
            command="certify",
            raw_args=raw,
            intent=resolved,
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
        project_dir = _project_dir()
        try:
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
        table.add_column("COST", justify="right")
        table.add_column("DURATION", justify="right")
        table.add_column("BLOCKED-ON")
        any_shown = False
        for t in tasks:
            ts = state.get("tasks", {}).get(t.id, {"status": "queued"})
            status = ts.get("status", "queued")
            if status == "removed" and not show_all:
                continue
            any_shown = True
            mode = t.command_argv[0] if t.command_argv else "?"
            cost = ts.get("cost_usd")
            cost_s = f"${cost:.2f}" if isinstance(cost, (int, float)) else "—"
            dur = ts.get("duration_s")
            dur_s = f"{dur:.0f}s" if isinstance(dur, (int, float)) else "—"
            after_s = ", ".join(t.after) if t.after else "—"
            table.add_row(t.id, _color_status(status), mode, cost_s, dur_s, after_s)
        if not any_shown:
            console.print("  Queue is empty (all tasks are removed; use --all to show).")
            return
        console.print(table)
        if not _watcher_alive(state):
            console.print("\n  [yellow]Worker is not running.[/yellow] "
                          "Start with: [info]otto queue run --concurrent N[/info]")

        if post_merge_preview:
            _print_post_merge_preview(project_dir, tasks, state)

    # ---- show ----
    @queue.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("task_id")
    def show(task_id: str) -> None:
        """Show full details of one task."""
        from otto.queue.schema import load_queue, load_state
        project_dir = _project_dir()
        tasks = load_queue(project_dir)
        state = load_state(project_dir)
        task = next((t for t in tasks if t.id == task_id), None)
        if task is None:
            error_console.print(f"[error]No such task: {task_id!r}[/error]")
            sys.exit(2)
        ts = state.get("tasks", {}).get(task_id, {"status": "queued"})
        console.print(f"\n  [bold]Task:[/bold] {task.id}")
        console.print(f"  [dim]Status:[/dim] {_color_status(ts.get('status', 'queued'))}")
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
        """Remove a task from the queue (queues a remove command for the watcher)."""
        from otto.queue.schema import append_command, load_queue
        project_dir = _project_dir()
        if not any(t.id == task_id for t in load_queue(project_dir)):
            error_console.print(f"[error]No such task: {task_id!r}[/error]")
            sys.exit(2)
        append_command(project_dir, {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cmd": "remove",
            "id": task_id,
        })
        console.print(f"  Remove requested for [info]{task_id}[/info].")

    # ---- cancel ----
    @queue.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("task_id")
    def cancel(task_id: str) -> None:
        """Signal a running task to stop (process group SIGTERM)."""
        from otto.queue.schema import append_command, load_queue
        project_dir = _project_dir()
        if not any(t.id == task_id for t in load_queue(project_dir)):
            error_console.print(f"[error]No such task: {task_id!r}[/error]")
            sys.exit(2)
        append_command(project_dir, {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cmd": "cancel",
            "id": task_id,
        })
        console.print(f"  Cancel requested for [info]{task_id}[/info].")

    # ---- cleanup (Phase 6.4) ----
    @queue.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("task_ids", nargs=-1)
    @click.option("--done", "scope_done", is_flag=True,
                  help="Remove worktrees for tasks with status=done (default)")
    @click.option("--all", "scope_all", is_flag=True,
                  help="Also include failed/cancelled/removed tasks")
    @click.option("--force", is_flag=True, help="Remove even if worktree is dirty")
    def cleanup(task_ids: tuple[str, ...], scope_done: bool, scope_all: bool, force: bool) -> None:
        """Explicitly remove worktrees for done/failed tasks.

        Branches and manifests are preserved. Only the working tree directory
        is removed (`git worktree remove <path>`). To remove the branch as
        well, use `git branch -d <branch>` afterward.

        \b
        Examples:
            otto queue cleanup --done       # remove done-task worktrees
            otto queue cleanup --all        # also failed/cancelled/removed
            otto queue cleanup t1 t2        # specific tasks
        """
        from otto.queue.schema import load_queue, load_state
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
            statuses_in_scope = {"done", "failed", "cancelled", "removed"}
        if task_ids:
            for tid in task_ids:
                t = next((t for t in tasks if t.id == tid), None)
                if t is None:
                    error_console.print(f"[error]No such task: {tid!r}[/error]")
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

        cleaned = 0
        skipped = 0
        for t in targets:
            if not t.worktree:
                continue
            wt_path = project_dir / t.worktree
            if not wt_path.exists():
                continue
            r = _git_worktree_remove(project_dir, wt_path, force=force)
            if r.returncode == 0:
                console.print(f"  [success]✓[/success] removed [info]{t.worktree}[/info] (task: {t.id})")
                cleaned += 1
            else:
                console.print(f"  [yellow]✗[/yellow] could not remove {t.worktree}: "
                              f"{rich_escape(r.stderr.strip())}")
                skipped += 1
        console.print(
            f"\n  Done. Cleaned {cleaned}, skipped {skipped}. "
            f"Manifests preserved at otto_logs/queue/<task-id>/."
        )

    # ---- run (the watcher) ----
    @queue.command(context_settings=CONTEXT_SETTINGS)
    @click.option("--concurrent", "-j", default=None, type=int,
                  help="Max concurrent tasks (default from otto.yaml queue.concurrent)")
    @click.option("--quiet", is_flag=True,
                  help="Suppress watcher event lines (spawn/reap/cancel) on stdout")
    def run(concurrent: int | None, quiet: bool) -> None:
        """Start the foreground queue watcher. Run in a tmux pane like `vite dev`."""
        from otto.config import load_config
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
        cfg = load_config(project_dir / "otto.yaml")
        rcfg = runner_config_from_otto_config(cfg)
        if concurrent is not None:
            rcfg.concurrent = max(1, concurrent)
        otto_bin = _resolve_otto_bin()

        # F7: install logging handlers so watcher events are visible. Without
        # this, the user sees only the spawned otto's stdout — no spawn / reap
        # / heartbeat / cancel events from the runner itself, making it
        # impossible to debug "why didn't my task dispatch?" or "did the cancel
        # actually take effect?".
        _install_runner_logging(project_dir, quiet=quiet)

        try:
            runner = Runner(project_dir, rcfg, otto_bin=otto_bin)
        except Exception as exc:
            error_console.print(f"[error]Runner init failed: {rich_escape(str(exc))}[/error]")
            sys.exit(1)

        console.print(
            f"  [bold]Queue worker[/bold] — "
            f"concurrent={rcfg.concurrent}, project={project_dir.name}"
        )
        console.print(f"  [dim]Event log: otto_logs/queue/watcher.log[/dim]")
        console.print("  Press Ctrl-C to stop gracefully (twice for immediate)\n")

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
        "done": "green",
        "failed": "red",
        "cancelled": "magenta",
        "removed": "dim",
    }
    return f"[{colors.get(status, 'white')}]{status}[/{colors.get(status, 'white')}]"
