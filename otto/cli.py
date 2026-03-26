"""Otto CLI — entrypoint for all otto commands."""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

# Clear CLAUDECODE at startup so otto can run from inside Claude Code sessions.
# Without this, agent SDK query() spawns a Claude Code subprocess that detects
# the env var and refuses to start ("cannot launch inside another session").
os.environ.pop("CLAUDECODE", None)

import click

from otto.config import create_config, git_meta_dir, load_config, require_git
from otto.display import TaskDisplay, build_status_table, console, format_cost, rich_escape, watch_status
from otto.theme import error_console
from otto.spec import filter_generated_spec_items, generate_spec, parse_markdown_tasks
from otto.tasks import (
    add_task,
    add_tasks,
    delete_task,
    load_tasks,
    spec_binding,
    spec_is_verifiable,
    spec_text,
    update_task,
)


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}




def _make_task_display_progress_callback(display: TaskDisplay):
    """Bridge runner progress events into the live TaskDisplay."""

    def _on_progress(event_type: str, data: dict) -> None:
        try:
            if event_type == "phase":
                display.update_phase(
                    name=data.get("name", ""),
                    status=data.get("status", ""),
                    time_s=data.get("time_s", 0.0),
                    error=data.get("error", ""),
                    detail=data.get("detail", ""),
                    cost=data.get("cost", 0),
                )
            elif event_type == "agent_tool":
                display.add_tool(data=data)
            elif event_type == "agent_tool_result":
                display.add_tool_result(data=data)
            elif event_type == "spec_item":
                display.add_spec_item(data.get("text", ""))
            elif event_type == "qa_finding":
                display.add_finding(data.get("text", ""))
            elif event_type == "qa_item_result":
                display.add_qa_item_result(
                    text=data.get("text", ""),
                    passed=data.get("passed", True),
                    evidence=data.get("evidence", ""),
                )
            elif event_type == "qa_summary":
                display.set_qa_summary(
                    total=data.get("total", 0),
                    passed=data.get("passed", 0),
                    failed=data.get("failed", 0),
                    proof_count=data.get("proof_count", 0),
                    proof_coverage=data.get("proof_coverage", ""),
                )
        except Exception:
            pass

    return _on_progress


async def _run_one_off_with_display(
    task: dict,
    config: dict,
    project_dir: Path,
):
    """Run a one-off task with live progress output."""
    from otto.runner import run_task_v45

    console.print()
    console.print(f"  ● [bold]Running[/bold]  [dim]#0  {task['key'][:8]}[/dim]")

    display = TaskDisplay(console)
    display.start()

    result = None
    try:
        result = await run_task_v45(
            task,
            config,
            project_dir,
            tasks_file=None,
            on_progress=_make_task_display_progress_callback(display),
        )
        return result
    finally:
        elapsed_str = display.stop()
        cost = float((result or {}).get("cost_usd", 0.0) or 0.0)
        if result and result.get("success"):
            console.print(
                f"    {time.strftime('%H:%M:%S')}  [green]✓[/green] passed  "
                f"[dim]{elapsed_str}  {format_cost(cost)}[/dim]"
            )
        else:
            console.print(
                f"    {time.strftime('%H:%M:%S')}  [red]✗[/red] failed  "
                f"[dim]{elapsed_str}  {format_cost(cost)}[/dim]"
            )






@click.group(context_settings=CONTEXT_SETTINGS)
def main():
    """Otto — autonomous Claude Code agent runner.

    Run 'otto COMMAND -h' for command-specific options.
    """
    pass



def _import_tasks(import_path: Path, tasks_path: Path) -> None:
    """Import tasks from .md, .txt, or .yaml files with spec generation.

    Replaces any existing tasks — the import file is the source of truth.
    """
    import yaml as _yaml

    project_dir = Path.cwd()
    suffix = import_path.suffix.lower()
    batch = []

    if suffix == ".md":
        console.print(f"Parsing {rich_escape(import_path.name)} (this may take 10-20s)...")
        parsed = parse_markdown_tasks(import_path, project_dir)
        console.print(f"Extracted {len(parsed)} tasks from markdown.\n")
        for t in parsed:
            item = {"prompt": t["prompt"]}
            if t.get("spec"):
                item["spec"] = t["spec"]
            if t.get("verify"):
                item["verify"] = t["verify"]
            if t.get("max_retries") is not None:
                item["max_retries"] = t["max_retries"]
            if t.get("depends_on") is not None:
                item["depends_on"] = t["depends_on"]
            batch.append(item)

    elif suffix == ".txt":
        lines = [l.strip() for l in import_path.read_text().splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        console.print(f"Found {len(lines)} tasks in {rich_escape(import_path.name)}.\n")
        for i, line in enumerate(lines, 1):
            console.print(f"  [dim]{i}/{len(lines)}[/dim] {rich_escape(line[:50])}")
            spec_items = filter_generated_spec_items(generate_spec(line, project_dir))
            item = {"prompt": line}
            if spec_items:
                item["spec"] = spec_items
                console.print(f"  {len(spec_items)} criteria generated")
            else:
                console.print(f"  no spec generated")
            batch.append(item)
        console.print()

    else:
        data = _yaml.safe_load(import_path.read_text()) or {}
        imported = data.get("tasks", [])
        console.print(f"Found {len(imported)} tasks in {rich_escape(import_path.name)}.\n")
        for i, t in enumerate(imported, 1):
            item = {"prompt": t["prompt"]}
            if t.get("spec"):
                item["spec"] = t["spec"]
                console.print(f"  [dim]{i}/{len(imported)}[/dim] {rich_escape(t['prompt'][:50])}")
            else:
                console.print(f"  [dim]{i}/{len(imported)}[/dim] {rich_escape(t['prompt'][:50])}")
                spec_items = filter_generated_spec_items(generate_spec(t["prompt"], project_dir))
                if spec_items:
                    item["spec"] = spec_items
                    console.print(f"  {len(spec_items)} criteria generated")
                else:
                    console.print(f"  no spec generated")
            if t.get("verify"):
                item["verify"] = t["verify"]
            if t.get("max_retries") is not None:
                item["max_retries"] = t["max_retries"]
            if t.get("depends_on") is not None:
                item["depends_on"] = t["depends_on"]
            batch.append(item)
        console.print()

    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(tasks_path.parent),
        prefix=f".{tasks_path.stem}.import.",
        suffix=tasks_path.suffix,
    )
    os.close(tmp_fd)
    replacement_path = Path(tmp_name)
    try:
        results = add_tasks(replacement_path, batch)
        os.replace(replacement_path, tasks_path)
    except Exception:
        replacement_path.unlink(missing_ok=True)
        raise
    _print_imported_tasks(results)


def _print_imported_tasks(tasks: list) -> None:
    """Print summary of imported tasks with spec details."""
    for task in tasks:
        spec = task.get("spec", [])
        console.print(f"  [success]✓[/success] [bold]#{task['id']}[/bold] {rich_escape(task['prompt'][:80])}")
        if spec:
            for item in spec:
                console.print(f"       [dim]-[/dim] {rich_escape(str(item))}")
    console.print(f"\n[success]✓[/success] Imported [bold]{len(tasks)}[/bold] tasks. Review specs in tasks.yaml before running.")


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("prompt", required=False)
@click.option("--verify", default=None, help="Custom verification command")
@click.option("--max-retries", default=None, type=int, help="Max retry attempts")
@click.option("-f", "--file", "import_file", default=None, type=click.Path(exists=True),
              help="Import tasks from a file (.yaml, .md, .txt)")
@click.option("--spec", "gen_spec", is_flag=True, help="Pre-generate acceptance spec (runs LLM)")
def add(prompt, verify, max_retries, import_file, gen_spec):
    """Add a task to the queue (or import from file with -f)."""
    require_git()
    project_dir = Path.cwd()

    # Auto-init if otto.yaml doesn't exist
    config_path = project_dir / "otto.yaml"
    if not config_path.exists():
        create_config(project_dir)
        config = load_config(config_path)
        console.print(f"[yellow]First run — created otto.yaml[/yellow]")
        console.print(f"  Run [bold]otto setup[/bold] to generate CLAUDE.md with project conventions.")
        console.print(f"  The coding agent follows CLAUDE.md — it makes a real difference.")
        console.print()

    tasks_path = project_dir / "tasks.yaml"

    if import_file:
        _import_tasks(Path(import_file), tasks_path)
        console.print(f"\n  [dim]Run 'otto arch' to analyze codebase and establish shared conventions[/dim]")
        return

    if not prompt:
        error_console.print("Error: provide a prompt or use -f to import", style="error")
        sys.exit(2)

    # Default: instant add (no LLM call). Use --spec to pre-generate.
    if not gen_spec:
        task = add_task(tasks_path, prompt, verify=verify, max_retries=max_retries)
        console.print(f"[success]\u2713[/success] Added task [bold]#{task['id']}[/bold] [dim]({task['key']})[/dim]: {rich_escape(prompt[:70])}")
        console.print(f"  [dim]Spec will be generated at run time (parallel with coding)[/dim]")
        return

    # --spec: pre-generate acceptance spec via LLM
    spec = None
    try:
        import threading
        _spec_start = time.time()
        _spec_done = False
        def _update_timer(status):
            while not _spec_done:
                elapsed = int(time.time() - _spec_start)
                status.update(f"[dim]Generating spec... {elapsed}s[/dim]")
                time.sleep(1)
        with console.status("[dim]Generating spec...[/dim]", spinner="dots") as status:
            timer_thread = threading.Thread(target=_update_timer, args=(status,), daemon=True)
            timer_thread.start()
            spec_items = generate_spec(prompt, Path.cwd())
            _spec_done = True
    except Exception as e:
        error_console.print(f"[error]\u2717[/error] Spec generation failed: {rich_escape(str(e))}")
        error_console.print(f"[dim]Task not created. Fix the issue or retry without --spec.[/dim]")
        sys.exit(1)
    filtered_spec = filter_generated_spec_items(spec_items)
    if filtered_spec:
        spec = filtered_spec
        must_count = sum(1 for i in spec if spec_binding(i) == "must")
        should_count = len(spec) - must_count
        label = f"{must_count} must"
        if should_count:
            label += f", {should_count} should"

        console.print(f"[success]\u2713[/success] Spec ([bold]{len(spec)}[/bold] criteria \u2014 {label})")
        console.print()
        from rich.table import Table
        spec_table = Table(box=None, show_header=True, pad_edge=False,
                           show_edge=False, expand=False, padding=(0, 1))
        spec_table.add_column("#", style="dim", width=3, justify="right")
        spec_table.add_column("", width=6)  # binding tag
        spec_table.add_column("Criterion", ratio=1, no_wrap=False)
        for idx, item in enumerate(spec, 1):
            text = spec_text(item)
            short = text[:80] + "..." if len(text) > 80 else text
            binding = spec_binding(item)
            verifiable = spec_is_verifiable(item)
            marker = "" if verifiable else " \u25c8"
            if binding == "must":
                tag = f"[success]\\[must{marker}][/success]"
            else:
                tag = f"[info]\\[should{marker}][/info]"
            spec_table.add_row(str(idx), tag, rich_escape(short))
        console.print(spec_table)
        console.print()
    else:
        error_console.print(f"[warning]\u26a0[/warning] Spec generation returned empty \u2014 task not created.")
        error_console.print(f"[dim]Retry or add without --spec.[/dim]")
        sys.exit(1)

    task = add_task(tasks_path, prompt, verify=verify, max_retries=max_retries,
                    spec=spec)
    console.print(f"[success]\u2713[/success] Added task [bold]#{task['id']}[/bold] [dim]({task['key']})[/dim]: {rich_escape(prompt[:70])}")


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("prompt", required=False)
@click.option("--dry-run", is_flag=True, help="Show what would run without executing")
def run(prompt, dry_run):
    """Run pending tasks (or a one-off task if prompt given)."""
    require_git()
    project_dir = Path.cwd()
    config_path = project_dir / "otto.yaml"
    if not config_path.exists():
        create_config(project_dir)
        console.print(f"[yellow]First run — created otto.yaml[/yellow]")
        console.print(f"  Run [bold]otto setup[/bold] to generate CLAUDE.md with project conventions.")
        console.print()
    config = load_config(config_path)

    if dry_run:
        tasks_path = project_dir / "tasks.yaml"
        tasks = load_tasks(tasks_path)
        pending = [t for t in tasks if t.get("status") == "pending"]
        console.print(f"Config: {rich_escape(str(project_dir / 'otto.yaml'))}")
        console.print(f"  max_retries: {config['max_retries']}")
        console.print(f"\nPending tasks: {len(pending)}")
        for t in pending:
            console.print(f"  #{t['id']} ({rich_escape(t['key'])}): {rich_escape(t['prompt'][:60])}")
        return

    if prompt:
        # One-off mode — adhoc-<timestamp>-<pid> per spec
        import fcntl
        import os
        import time

        lock_path = git_meta_dir(project_dir) / "otto.lock"
        lock_path.touch()
        lock_fh = open(lock_path, "r")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            error_console.print("Another otto process is running", style="error")
            sys.exit(2)

        try:
            from otto.runner import check_clean_tree
            if not check_clean_tree(project_dir):
                error_console.print(f"[error]✗[/error] Working tree is dirty \u2014 fix before running otto")
                sys.exit(2)

            key = f"adhoc-{int(time.time())}-{os.getpid()}"
            task = {
                "id": 0,
                "key": key,
                "prompt": prompt,
                "status": "pending",
            }
            result = asyncio.run(_run_one_off_with_display(task, config, project_dir))
            success = result.get("success", False)
            sys.exit(0 if success else 1)
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()
    else:
        tasks_path = project_dir / "tasks.yaml"
        from otto.orchestrator import run_per
        exit_code = asyncio.run(run_per(config, tasks_path, project_dir))
        sys.exit(exit_code)


@main.command(context_settings=CONTEXT_SETTINGS)
@click.option("--specs", is_flag=True, help="Also generate specs for preview")
def plan(specs):
    """Show execution plan without running tasks."""
    require_git()
    project_dir = Path.cwd()
    config_path = project_dir / "otto.yaml"
    if not config_path.exists():
        console.print("[dim]No otto.yaml found. Run 'otto init' first.[/dim]")
        return

    tasks_path = project_dir / "tasks.yaml"
    tasks = load_tasks(tasks_path)
    pending = [t for t in tasks if t.get("status") == "pending"]

    if not pending:
        console.print("[dim]No pending tasks.[/dim]")
        return

    console.print(f"\n  [bold]Execution Plan[/bold]  [dim]({len(pending)} tasks)[/dim]\n")

    # Show dependency graph
    from otto.planner import default_plan
    execution_plan = default_plan(pending)

    for batch_idx, batch in enumerate(execution_plan.batches):
        batch_label = "parallel" if len(batch.tasks) > 1 else "single"
        console.print(f"  [bold]Batch {batch_idx + 1}[/bold]  [dim]{len(batch.tasks)} tasks ({batch_label})[/dim]")
        for tp in batch.tasks:
            # Find the task
            task = next((t for t in pending if t.get("key") == tp.task_key), None)
            if task:
                spec_count = len(task.get("spec") or [])
                spec_str = f"  [dim]({spec_count} spec)[/dim]" if spec_count else "  [dim](no spec)[/dim]"
                deps = task.get("depends_on", [])
                dep_str = f" \u2192 #{', #'.join(str(d) for d in deps)}" if deps else ""
                console.print(f"    [dim]\u25cb[/dim] [bold]#{task['id']}[/bold]  {rich_escape(task.get('prompt', '')[:55])}{spec_str}[dim]{dep_str}[/dim]")
        console.print()

    # Summary
    console.print(f"  [dim]Run 'otto run' to execute.[/dim]")

    # Optional spec preview
    if specs:
        console.print(f"\n  [dim]Generating specs for preview...[/dim]")
        for task in pending:
            if not task.get("spec"):
                console.print(f"\n  [dim]#{task['id']}[/dim]  {rich_escape(task.get('prompt', '')[:60])}")
                try:
                    spec_items = generate_spec(task["prompt"], project_dir)
                    filtered = filter_generated_spec_items(spec_items)
                    if filtered:
                        for item in filtered:
                            binding = spec_binding(item)
                            text = spec_text(item)
                            marker = "" if spec_is_verifiable(item) else " \u25c8"
                            tag = f"[{binding}{marker}]"
                            console.print(f"    [dim]{tag}[/dim] {rich_escape(text[:75])}")
                    else:
                        console.print(f"    [dim](no spec generated)[/dim]")
                except Exception as e:
                    console.print(f"    [error]spec gen failed: {rich_escape(str(e)[:60])}[/error]")


@main.command(context_settings=CONTEXT_SETTINGS)
@click.option("-w", "--watch", is_flag=True, help="Auto-refresh every 2 seconds")
def status(watch):
    """Show task status."""
    import fcntl

    tasks_path = Path.cwd() / "tasks.yaml"
    tasks = load_tasks(tasks_path)
    if not tasks:
        console.print(f"[dim]No tasks found. Use 'otto add' to create one.[/dim]")
        return

    # Detect stale "running" tasks — if no otto process holds the lock, they crashed
    running = [t for t in tasks if t.get("status") == "running"]
    if running:
        lock_path = git_meta_dir(Path.cwd()) / "otto.lock"
        otto_is_running = False
        if lock_path.exists():
            try:
                lock_fh = open(lock_path, "r")
                fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
                lock_fh.close()
            except BlockingIOError:
                otto_is_running = True
        if not otto_is_running:
            for t in running:
                console.print(f"[warning]⚠ Task #{t['id']} stuck in 'running' (otto crashed?) — will auto-recover on next 'otto run'[/warning]")
            console.print()

    if watch:
        watch_status(lambda: load_tasks(tasks_path), console)
        return

    console.print(build_status_table(tasks, show_phase=True))


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("task_id", type=int)
@click.argument("feedback", required=False)
@click.option("--force", is_flag=True, help="Reset any task, not just failed ones")
def retry(task_id, feedback, force):
    """Reset a failed task to pending (use --force for any status).

    Optionally provide feedback to guide the agent on what to fix:
      otto retry --force 2 "Output format is ugly, use a table"
    """
    tasks_path = Path.cwd() / "tasks.yaml"
    tasks = load_tasks(tasks_path)
    for t in tasks:
        if t.get("id") == task_id:
            if not force and t.get("status") != "failed":
                error_console.print(
                    f"Task #{task_id} is '{t.get('status')}', not 'failed'. Use --force to override.", style="error"
                )
                sys.exit(1)
            # Warn if retrying a task whose code is already merged to main
            if t.get("status") == "passed":
                import subprocess as _sp
                commit_check = _sp.run(
                    ["git", "log", "--oneline", "--all", f"--grep=(#{task_id})"],
                    capture_output=True, text=True,
                )
                if commit_check.stdout.strip():
                    console.print(
                        f"[warning]⚠ Task #{task_id} was already merged to main. "
                        f"The coding agent will see no diff and may waste time.[/warning]"
                    )
                    console.print(
                        f"  [dim]Consider: otto add 'new task' instead of retrying a completed one.[/dim]"
                    )

            updates: dict = {
                "status": "pending", "attempts": 0,
                "session_id": None, "error": None, "error_code": None,
            }
            if feedback:
                updates["feedback"] = feedback
            update_task(tasks_path, t["key"], **updates)
            console.print(f"[success]✓[/success] Reset task [bold]#{task_id}[/bold] to pending")
            if feedback:
                console.print(f"  [dim]Feedback: {rich_escape(feedback)}[/dim]")
            return
    error_console.print(f"Task #{task_id} not found", style="error")
    sys.exit(1)


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("task_id", type=int)
def delete(task_id):
    """Remove a task from the status list (does NOT revert code).

    For pending tasks: removes the task before it runs.
    For passed tasks: only removes from tracking — merged code stays on main.
    For failed tasks: removes from tracking (branch already cleaned up).

    To undo all otto code changes, use 'otto reset --revert-commits'.
    """
    tasks_path = Path.cwd() / "tasks.yaml"
    tasks = load_tasks(tasks_path)
    target = None
    for t in tasks:
        if t.get("id") == task_id:
            target = t
            break
    if not target:
        error_console.print(f"Task #{task_id} not found", style="error")
        sys.exit(1)

    task_status = target.get("status", "pending")
    if task_status == "running":
        error_console.print(f"[error]✗[/error] Cannot delete a running task. Wait for it to finish or reset.")
        sys.exit(1)
    if task_status == "passed":
        console.print(f"[warning]⚠[/warning] Task already merged \u2014 this only removes it from the status list.")
        console.print(f"  [dim]Code, commits, and test files stay on main.[/dim]")
        console.print(f"  [dim]To undo code changes: 'otto reset --revert-commits' or 'git revert'.[/dim]")
        click.confirm("  Continue?", abort=True)

    # Warn if other pending tasks depend on this one
    dependents = [
        t for t in tasks
        if t.get("id") != task_id
        and t.get("status") == "pending"
        and task_id in (t.get("depends_on") or [])
    ]
    if dependents:
        dep_ids = ", ".join(f"#{t['id']}" for t in dependents)
        console.print(f"[warning]⚠[/warning] Pending tasks depend on this one: {dep_ids}")
        click.confirm("  Continue?", abort=True)

    delete_task(tasks_path, task_id)
    console.print(f"[success]✓[/success] Deleted task [bold]#{task_id}[/bold]: {rich_escape(target['prompt'][:60])}")



# Setup command (registered from otto/cli_setup.py)
from otto.cli_setup import register_setup_command
register_setup_command(main)

# Log/show/diff commands (registered from otto/cli_logs.py)
from otto.cli_logs import register_log_commands
register_log_commands(main)


@main.command(context_settings=CONTEXT_SETTINGS)
@click.option("--yes", is_flag=True, help="Skip confirmation")
@click.option("--revert-commits", is_flag=True, help="Also revert otto commits from git history")
@click.option("--hard", "revert_commits_compat", is_flag=True, hidden=True,
              help="Alias for --revert-commits (deprecated)")
def reset(yes, revert_commits, revert_commits_compat):
    """Reset all tasks and clean up branches.

    --revert-commits also reverts otto's git commits, restoring the codebase
    to the state before otto ran.
    """
    # Support --hard as backward-compat alias
    hard = revert_commits or revert_commits_compat
    if not yes:
        msg = "Reset all tasks and delete otto/* branches?"
        if hard:
            msg = "HARD RESET: revert all otto commits and restore codebase?"
        click.confirm(msg, abort=True)

    import fcntl
    import subprocess

    project_dir = Path.cwd()

    # Acquire process lock — refuse to reset while a worker is active
    lock_path = git_meta_dir(project_dir) / "otto.lock"
    lock_path.touch()
    lock_fh = open(lock_path, "r")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        error_console.print("Cannot reset while otto is running", style="error")
        sys.exit(2)

    try:
        # Delete tasks.yaml entirely (not just reset status)
        tasks_path = project_dir / "tasks.yaml"
        count = 0
        if tasks_path.exists():
            from otto.tasks import load_tasks
            count = len(load_tasks(tasks_path))
            tasks_path.unlink()
        tasks_lock = tasks_path.parent / ".tasks.lock"
        if tasks_lock.exists():
            tasks_lock.unlink()

        # Hard reset: reset to before the first otto commit
        if hard:
            result = subprocess.run(
                ["git", "log", "--oneline", "--all", "--grep=otto:"],
                capture_output=True, text=True, cwd=project_dir,
            )
            otto_commits = [line.split()[0] for line in result.stdout.strip().splitlines() if line]
            if otto_commits:
                # Find the parent of the oldest otto commit
                oldest = otto_commits[-1]  # last in the list = oldest
                parent = subprocess.run(
                    ["git", "rev-parse", f"{oldest}^"],
                    cwd=project_dir, capture_output=True, text=True,
                )
                if parent.returncode == 0 and parent.stdout.strip():
                    subprocess.run(
                        ["git", "reset", "--hard", parent.stdout.strip()],
                        cwd=project_dir, capture_output=True,
                    )
                    console.print(f"  [dim]Reset to before first otto commit ({len(otto_commits)} commits removed)[/dim]")
                else:
                    error_console.print(f"[warning]⚠[/warning] Could not find parent of oldest otto commit")

        # Delete otto/* branches
        result = subprocess.run(
            ["git", "branch", "--list", "otto/*"],
            capture_output=True, text=True,
        )
        for branch in result.stdout.strip().split("\n"):
            branch = branch.strip()
            if branch:
                subprocess.run(["git", "branch", "-D", branch], capture_output=True)

        msg = f"[success]✓[/success] Reset [bold]{count}[/bold] tasks. Cleaned branches."
        if hard:
            msg += " Reverted otto commits."
        console.print(msg)
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()



# Bench subcommands (registered from otto/cli_bench.py)
from otto.cli_bench import register_bench_commands
register_bench_commands(main)
