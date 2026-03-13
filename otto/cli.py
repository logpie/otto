"""Otto CLI — entrypoint for all otto commands."""

import asyncio
import logging
import sys
from pathlib import Path

import click

from otto.config import create_config, git_meta_dir, load_config
from otto.tasks import add_task, load_tasks, reset_all_tasks, save_tasks, update_task


@click.group()
def main():
    """Otto — autonomous Claude Code agent runner."""
    pass


@main.command()
def init():
    """Initialize otto for this project."""
    project_dir = Path.cwd()
    config_path = create_config(project_dir)
    config = load_config(config_path)
    click.echo(f"Created {config_path}")
    click.echo(f"  test_command: {config['test_command'] or '(not detected)'}")
    click.echo(f"  default_branch: {config['default_branch']}")
    click.echo(f"  max_retries: {config['max_retries']}")
    click.echo(f"  model: {config['model']}")
    click.echo("\nCommit otto.yaml to share config with your team.")


@main.command()
@click.argument("prompt", required=False)
@click.option("--verify", default=None, help="Custom verification command")
@click.option("--max-retries", default=None, type=int, help="Max retry attempts")
@click.option("-f", "--file", "import_file", default=None, type=click.Path(exists=True),
              help="Import tasks from a YAML file")
def add(prompt, verify, max_retries, import_file):
    """Add a task to the queue (or import from file with -f)."""
    import yaml as _yaml

    tasks_path = Path.cwd() / "tasks.yaml"

    if import_file:
        data = _yaml.safe_load(Path(import_file).read_text()) or {}
        imported = data.get("tasks", [])
        for t in imported:
            task = add_task(tasks_path, t["prompt"],
                           verify=t.get("verify"), max_retries=t.get("max_retries"))
            click.echo(f"Added task #{task['id']} ({task['key']}): {t['prompt'][:60]}")
        click.echo(f"Imported {len(imported)} tasks")
        return

    if not prompt:
        click.echo("Error: provide a prompt or use -f to import", err=True)
        sys.exit(2)

    task = add_task(tasks_path, prompt, verify=verify, max_retries=max_retries)
    click.echo(f"Added task #{task['id']} ({task['key']}): {prompt}")


@main.command()
@click.argument("prompt", required=False)
@click.option("--dry-run", is_flag=True, help="Show what would run without executing")
def run(prompt, dry_run):
    """Run pending tasks (or a one-off task if prompt given)."""
    from otto.runner import run_all, run_task

    project_dir = Path.cwd()
    config_path = project_dir / "otto.yaml"
    if not config_path.exists():
        click.echo("Error: otto.yaml not found. Run 'otto init' first.", err=True)
        sys.exit(2)
    config = load_config(config_path)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if dry_run:
        tasks_path = project_dir / "tasks.yaml"
        tasks = load_tasks(tasks_path)
        pending = [t for t in tasks if t.get("status") == "pending"]
        click.echo(f"Config: {project_dir / 'otto.yaml'}")
        click.echo(f"  test_command: {config.get('test_command') or '(none)'}")
        click.echo(f"  model: {config['model']}")
        click.echo(f"  max_retries: {config['max_retries']}")
        click.echo(f"\nPending tasks: {len(pending)}")
        for t in pending:
            click.echo(f"  #{t['id']} ({t['key']}): {t['prompt'][:60]}")
        return

    if prompt:
        # One-off mode — adhoc-<timestamp>-<pid> per spec
        # Still acquires process lock to prevent concurrent runs
        import fcntl
        import os
        import time

        lock_path = git_meta_dir(project_dir) / "otto.lock"
        lock_path.touch()
        lock_fh = open(lock_path, "r")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            click.echo("Another otto process is running", err=True)
            sys.exit(2)

        try:
            key = f"adhoc-{int(time.time())}-{os.getpid()}"
            task = {
                "id": 0,
                "key": key,
                "prompt": prompt,
                "status": "pending",
            }
            success = asyncio.run(
                run_task(task, config, project_dir, tasks_file=None)
            )
            sys.exit(0 if success else 1)
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()
    else:
        tasks_path = project_dir / "tasks.yaml"
        exit_code = asyncio.run(run_all(config, tasks_path, project_dir))
        sys.exit(exit_code)


@main.command()
def status():
    """Show task status."""
    tasks_path = Path.cwd() / "tasks.yaml"
    tasks = load_tasks(tasks_path)
    if not tasks:
        click.echo("No tasks found. Use 'otto add' to create one.")
        return

    # Simple table
    click.echo(f"{'ID':>4}  {'Key':12}  {'Status':10}  {'Att':>3}  Prompt")
    click.echo("-" * 70)
    for t in tasks:
        click.echo(
            f"{t.get('id', '?'):>4}  {t.get('key', '?'):12}  "
            f"{t.get('status', '?'):10}  {t.get('attempts', 0):>3}  "
            f"{t['prompt'][:40]}"
        )


@main.command()
@click.argument("task_id", type=int)
def retry(task_id):
    """Reset a failed task to pending."""
    tasks_path = Path.cwd() / "tasks.yaml"
    tasks = load_tasks(tasks_path)
    for t in tasks:
        if t.get("id") == task_id:
            if t.get("status") != "failed":
                click.echo(
                    f"Task #{task_id} is '{t.get('status')}', not 'failed'", err=True
                )
                sys.exit(1)
            update_task(
                tasks_path, t["key"],
                status="pending", attempts=0, session_id=None, error=None,
            )
            click.echo(f"Reset task #{task_id} to pending")
            return
    click.echo(f"Task #{task_id} not found", err=True)
    sys.exit(1)


@main.command()
@click.argument("task_id", type=int)
def logs(task_id):
    """Show logs for a task."""
    tasks_path = Path.cwd() / "tasks.yaml"
    tasks = load_tasks(tasks_path)
    for t in tasks:
        if t.get("id") == task_id:
            log_dir = Path.cwd() / "otto_logs" / t["key"]
            if not log_dir.exists():
                click.echo(f"No logs for task #{task_id}")
                return
            for log_file in sorted(log_dir.iterdir()):
                click.echo(f"\n=== {log_file.name} ===")
                click.echo(log_file.read_text())
            return
    click.echo(f"Task #{task_id} not found", err=True)
    sys.exit(1)


@main.command()
@click.option("--yes", is_flag=True, help="Skip confirmation")
def reset(yes):
    """Reset all tasks and clean up branches."""
    if not yes:
        click.confirm("Reset all tasks to pending and delete otto/* branches?", abort=True)

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
        click.echo("Cannot reset while otto is running", err=True)
        sys.exit(2)

    try:
        # Lock order: otto.lock (process) → .tasks.lock (CRUD) to prevent deadlocks
        tasks_path = project_dir / "tasks.yaml"
        count = reset_all_tasks(tasks_path)

        # Delete otto/* branches
        result = subprocess.run(
            ["git", "branch", "--list", "otto/*"],
            capture_output=True, text=True,
        )
        for branch in result.stdout.strip().split("\n"):
            branch = branch.strip()
            if branch:
                subprocess.run(["git", "branch", "-D", branch], capture_output=True)

        # Clean logs
        import shutil
        log_dir = project_dir / "otto_logs"
        if log_dir.exists():
            shutil.rmtree(log_dir)

        # Clean testgen artifacts (use git_meta_dir for linked worktree support)
        testgen_dir = git_meta_dir(project_dir) / "otto"
        if testgen_dir.exists():
            shutil.rmtree(testgen_dir)

        click.echo(f"Reset {count} tasks to pending. Cleaned branches, logs, and testgen.")
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
