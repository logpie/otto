"""Otto CLI — entrypoint for all otto commands."""

import asyncio
import sys
from pathlib import Path

import click

from otto.config import create_config, git_meta_dir, load_config
from otto.rubric import generate_rubric, parse_markdown_tasks
from otto.tasks import add_task, add_tasks, load_tasks, reset_all_tasks, save_tasks, update_task


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}

# ANSI styling
_B = "\033[1m"       # bold
_D = "\033[2m"       # dim
_G = "\033[32m"      # green
_Y = "\033[33m"      # yellow
_C = "\033[36m"      # cyan
_R = "\033[31m"      # red
_0 = "\033[0m"       # reset


def _require_git():
    """Exit with a friendly error if not in a git repo."""
    import subprocess
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        capture_output=True, cwd=Path.cwd(),
    )
    if result.returncode != 0:
        click.echo("Error: not a git repository. Run 'git init' first.", err=True)
        sys.exit(2)


@click.group(context_settings=CONTEXT_SETTINGS)
def main():
    """Otto — autonomous Claude Code agent runner."""
    pass


@main.command()
def init():
    """Initialize otto for this project."""
    _require_git()
    project_dir = Path.cwd()
    config_path = create_config(project_dir)
    config = load_config(config_path)
    click.echo(f"{_G}✓{_0} Created {_B}{config_path.name}{_0}")
    click.echo(f"  {_D}default_branch:{_0} {config['default_branch']}")
    click.echo(f"  {_D}max_retries:{_0}    {config['max_retries']}")
    click.echo(f"\n{_D}Commit otto.yaml to share config with your team.{_0}")


def _import_tasks(import_path: Path, tasks_path: Path) -> None:
    """Import tasks from .md, .txt, or .yaml files with rubric generation.

    Replaces any existing tasks — the import file is the source of truth.
    """
    import yaml as _yaml

    # Clear existing tasks — import replaces, not appends
    if tasks_path.exists():
        tasks_path.unlink()

    project_dir = Path.cwd()
    suffix = import_path.suffix.lower()

    if suffix == ".md":
        click.echo(f"Parsing {import_path.name} (this may take 10-20s)...")
        parsed = parse_markdown_tasks(import_path, project_dir)
        click.echo(f"Extracted {len(parsed)} tasks from markdown.\n")
        batch = []
        for t in parsed:
            item = {"prompt": t["prompt"]}
            if t.get("rubric"):
                item["rubric"] = t["rubric"]
            if t.get("verify"):
                item["verify"] = t["verify"]
            if t.get("max_retries") is not None:
                item["max_retries"] = t["max_retries"]
            batch.append(item)
        results = add_tasks(tasks_path, batch)
        _print_imported_tasks(results)

    elif suffix == ".txt":
        lines = [l.strip() for l in import_path.read_text().splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        click.echo(f"Found {len(lines)} tasks in {import_path.name}.\n")
        batch = []
        for i, line in enumerate(lines, 1):
            click.echo(f"[{i}/{len(lines)}] Generating rubric for: {line[:50]}...")
            rubric_items = generate_rubric(line, project_dir)
            item = {"prompt": line}
            if rubric_items:
                item["rubric"] = rubric_items
                click.echo(f"  {len(rubric_items)} criteria generated")
            else:
                click.echo(f"  no rubric generated")
            batch.append(item)
        click.echo()
        results = add_tasks(tasks_path, batch)
        _print_imported_tasks(results)

    else:
        data = _yaml.safe_load(import_path.read_text()) or {}
        imported = data.get("tasks", [])
        click.echo(f"Found {len(imported)} tasks in {import_path.name}.\n")
        batch = []
        for i, t in enumerate(imported, 1):
            item = {"prompt": t["prompt"]}
            if t.get("rubric"):
                item["rubric"] = t["rubric"]
                click.echo(f"[{i}/{len(imported)}] {t['prompt'][:50]} — {len(t['rubric'])} rubric items (from file)")
            else:
                click.echo(f"[{i}/{len(imported)}] Generating rubric for: {t['prompt'][:50]}...")
                rubric_items = generate_rubric(t["prompt"], project_dir)
                if rubric_items:
                    item["rubric"] = rubric_items
                    click.echo(f"  {len(rubric_items)} criteria generated")
                else:
                    click.echo(f"  no rubric generated")
            if t.get("verify"):
                item["verify"] = t["verify"]
            if t.get("max_retries") is not None:
                item["max_retries"] = t["max_retries"]
            batch.append(item)
        click.echo()
        results = add_tasks(tasks_path, batch)
        _print_imported_tasks(results)


def _print_imported_tasks(tasks: list) -> None:
    """Print summary of imported tasks with rubric details."""
    for task in tasks:
        rubric = task.get("rubric", [])
        click.echo(f"  {_G}✓{_0} {_B}#{task['id']}{_0} {task['prompt'][:55]}")
        if rubric:
            for item in rubric:
                click.echo(f"       {_D}-{_0} {item}")
    click.echo(f"\n{_G}✓{_0} Imported {_B}{len(tasks)}{_0} tasks. Review rubrics in tasks.yaml before running.")


@main.command()
@click.argument("prompt", required=False)
@click.option("--verify", default=None, help="Custom verification command")
@click.option("--max-retries", default=None, type=int, help="Max retry attempts")
@click.option("-f", "--file", "import_file", default=None, type=click.Path(exists=True),
              help="Import tasks from a file (.yaml, .md, .txt)")
@click.option("--no-rubric", is_flag=True, help="Skip rubric generation")
def add(prompt, verify, max_retries, import_file, no_rubric):
    """Add a task to the queue (or import from file with -f)."""
    tasks_path = Path.cwd() / "tasks.yaml"

    if import_file:
        _import_tasks(Path(import_file), tasks_path)
        return

    if not prompt:
        click.echo("Error: provide a prompt or use -f to import", err=True)
        sys.exit(2)

    # Generate rubric unless --no-rubric
    rubric = None
    if not no_rubric:
        click.echo(f"{_D}Generating rubric...{_0}")
        rubric_items = generate_rubric(prompt, Path.cwd())
        if rubric_items:
            rubric = rubric_items
            click.echo(f"{_G}✓{_0} Rubric ({_B}{len(rubric_items)}{_0} criteria):")
            for item in rubric_items:
                click.echo(f"  {_D}-{_0} {item}")
        else:
            click.echo(f"{_Y}⚠{_0} No rubric generated.")

    task = add_task(tasks_path, prompt, verify=verify, max_retries=max_retries,
                    rubric=rubric)
    click.echo(f"{_G}✓{_0} Added task {_B}#{task['id']}{_0} {_D}({task['key']}){_0}: {prompt}")


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

    if dry_run:
        tasks_path = project_dir / "tasks.yaml"
        tasks = load_tasks(tasks_path)
        pending = [t for t in tasks if t.get("status") == "pending"]
        click.echo(f"Config: {project_dir / 'otto.yaml'}")
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
        click.echo(f"{_D}No tasks found. Use 'otto add' to create one.{_0}")
        return

    click.echo(f"{_B}{'ID':>4}  {'Status':10}  {'Att':>3}  {'Rubric':>6}  Prompt{_0}")
    click.echo(f"{_D}{'─' * 70}{_0}")
    for t in tasks:
        status_str = t.get("status", "?")
        rubric_count = len(t.get("rubric", []))
        # Color status
        if status_str == "passed":
            status_styled = f"{_G}{status_str:10}{_0}"
        elif status_str == "failed":
            status_styled = f"{_R}{status_str:10}{_0}"
        elif status_str == "running":
            status_styled = f"{_C}{status_str:10}{_0}"
        else:
            status_styled = f"{_D}{status_str:10}{_0}"
        click.echo(
            f"{t.get('id', '?'):>4}  {status_styled}  {t.get('attempts', 0):>3}  "
            f"{rubric_count:>6}  {t['prompt'][:40]}"
        )


@main.command()
@click.argument("task_id", type=int)
@click.option("--force", is_flag=True, help="Reset any task, not just failed ones")
def retry(task_id, force):
    """Reset a failed task to pending (use --force for any status)."""
    tasks_path = Path.cwd() / "tasks.yaml"
    tasks = load_tasks(tasks_path)
    for t in tasks:
        if t.get("id") == task_id:
            if not force and t.get("status") != "failed":
                click.echo(
                    f"Task #{task_id} is '{t.get('status')}', not 'failed'. Use --force to override.", err=True
                )
                sys.exit(1)
            if force and t.get("status") == "passed":
                click.echo(f"{_Y}⚠{_0} Task #{task_id} was previously passed. Its code is still on {_B}main{_0}.")
                click.echo(f"  {_D}Edit rubrics in tasks.yaml to change acceptance criteria, then run.{_0}")
            update_task(
                tasks_path, t["key"],
                status="pending", attempts=0, session_id=None,
                error=None, error_code=None,
            )
            click.echo(f"{_G}✓{_0} Reset task {_B}#{task_id}{_0} to pending")
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
                click.echo(f"{_D}No logs for task #{task_id}{_0}")
                return
            for log_file in sorted(log_dir.iterdir()):
                click.echo(f"\n{_B}{'━' * 40}{_0}")
                click.echo(f"{_B}  {log_file.name}{_0}")
                click.echo(f"{_B}{'━' * 40}{_0}")
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

        click.echo(f"{_G}✓{_0} Reset {_B}{count}{_0} tasks. Cleaned branches, logs, and testgen.")
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
