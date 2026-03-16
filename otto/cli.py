"""Otto CLI — entrypoint for all otto commands."""

import asyncio
import sys
from pathlib import Path

import click

from otto.config import create_config, git_meta_dir, load_config
from otto.rubric import generate_rubric, parse_markdown_tasks
from otto.tasks import add_task, add_tasks, delete_task, load_tasks, reset_all_tasks, save_tasks, update_task


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m{secs}s"

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
    """Otto — autonomous Claude Code agent runner.

    Run 'otto COMMAND -h' for command-specific options.
    """
    pass


@main.command(context_settings=CONTEXT_SETTINGS)
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
            if t.get("depends_on") is not None:
                item["depends_on"] = t["depends_on"]
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
        click.echo(f"  {_G}✓{_0} {_B}#{task['id']}{_0} {task['prompt'][:80]}")
        if rubric:
            for item in rubric:
                click.echo(f"       {_D}-{_0} {item}")
    click.echo(f"\n{_G}✓{_0} Imported {_B}{len(tasks)}{_0} tasks. Review rubrics in tasks.yaml before running.")


@main.command(context_settings=CONTEXT_SETTINGS)
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
    if no_rubric:
        click.echo(f"{_Y}{_B}⚠ WARNING:{_0} {_Y}No rubric → no adversarial tests → no verification gate.{_0}")
        click.echo(f"  {_Y}The coding agent's output will be merged with zero quality checks.{_0}")
    if not no_rubric:
        click.echo(f"{_D}Generating rubric...{_0}")
        try:
            rubric_items = generate_rubric(prompt, Path.cwd())
        except Exception as e:
            click.echo(f"{_R}✗{_0} Rubric generation failed: {e}", err=True)
            click.echo(f"{_D}Task not created. Fix the issue or use --no-rubric.{_0}", err=True)
            sys.exit(1)
        if rubric_items:
            rubric = rubric_items
            click.echo(f"{_G}✓{_0} Rubric ({_B}{len(rubric_items)}{_0} criteria):")
            for item in rubric_items:
                click.echo(f"  {_D}-{_0} {item}")
        else:
            click.echo(f"{_Y}⚠{_0} Rubric generation returned empty — task not created.", err=True)
            click.echo(f"{_D}Retry or use --no-rubric to skip rubric generation.{_0}", err=True)
            sys.exit(1)

    task = add_task(tasks_path, prompt, verify=verify, max_retries=max_retries,
                    rubric=rubric)
    click.echo(f"{_G}✓{_0} Added task {_B}#{task['id']}{_0} {_D}({task['key']}){_0}: {prompt}")


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("prompt", required=False)
@click.option("--dry-run", is_flag=True, help="Show what would run without executing")
@click.option("--no-integration", is_flag=True, help="Skip post-run integration tests")
@click.option("--no-parallel", is_flag=True, help="Force serial execution (full streaming output per task)")
def run(prompt, dry_run, no_integration, no_parallel):
    """Run pending tasks (or a one-off task if prompt given)."""
    from otto.runner import run_all, run_task

    project_dir = Path.cwd()
    config_path = project_dir / "otto.yaml"
    if not config_path.exists():
        click.echo("Error: otto.yaml not found. Run 'otto init' first.", err=True)
        sys.exit(2)
    config = load_config(config_path)
    if no_integration:
        config["no_integration"] = True
    if no_parallel:
        config["max_parallel"] = 1

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


@main.command(context_settings=CONTEXT_SETTINGS)
def status():
    """Show task status."""
    import fcntl
    tasks_path = Path.cwd() / "tasks.yaml"
    tasks = load_tasks(tasks_path)
    if not tasks:
        click.echo(f"{_D}No tasks found. Use 'otto add' to create one.{_0}")
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
                click.echo(f"{_Y}⚠ Task #{t['id']} stuck in 'running' (otto crashed?) — will auto-recover on next 'otto run'{_0}")
            click.echo()

    click.echo(f"{_B}{'ID':>4}  {'Status':10}  {'Att':>3}  {'Deps':>4}  {'Rubric':>6}  {'Cost':>7}  {'Time':>6}  Prompt{_0}")
    click.echo(f"{_D}{'─' * 94}{_0}")
    for t in tasks:
        status_str = t.get("status", "?")
        rubric_count = len(t.get("rubric", []))
        deps = t.get("depends_on") or []
        deps_str = str(len(deps)) if deps else ""
        cost = t.get("cost_usd", 0.0)
        cost_str = f"${cost:.2f}" if cost else ""
        dur = t.get("duration_s", 0.0)
        dur_str = _format_duration(dur) if dur else ""
        # Color status
        if status_str == "passed":
            status_styled = f"{_G}{status_str:10}{_0}"
        elif status_str in ("failed", "blocked"):
            status_styled = f"{_R}{status_str:10}{_0}"
        elif status_str == "running":
            status_styled = f"{_C}{status_str:10}{_0}"
        else:
            status_styled = f"{_D}{status_str:10}{_0}"
        click.echo(
            f"{t.get('id', '?'):>4}  {status_styled}  {t.get('attempts', 0):>3}  "
            f"{deps_str:>4}  {rubric_count:>6}  {cost_str:>7}  {dur_str:>6}  {t['prompt'][:50]}"
        )
        # Show error for failed/blocked tasks
        if status_str in ("failed", "blocked") and t.get("error"):
            click.echo(f"        {_R}↳ {t['error'][:70]}{_0}")

    # Summary
    counts = {}
    total_cost = 0.0
    total_dur = 0.0
    for t in tasks:
        s = t.get("status", "?")
        counts[s] = counts.get(s, 0) + 1
        total_cost += t.get("cost_usd", 0.0)
        total_dur += t.get("duration_s", 0.0)
    click.echo(f"{_D}{'─' * 94}{_0}")
    parts = []
    if counts.get("passed"):
        parts.append(f"{_G}{counts['passed']} passed{_0}")
    if counts.get("failed"):
        parts.append(f"{_R}{counts['failed']} failed{_0}")
    if counts.get("blocked"):
        parts.append(f"{_R}{counts['blocked']} blocked{_0}")
    if counts.get("pending"):
        parts.append(f"{_D}{counts['pending']} pending{_0}")
    if counts.get("running"):
        parts.append(f"{_C}{counts['running']} running{_0}")
    summary = ", ".join(parts)
    extras = []
    if total_cost > 0:
        extras.append(f"${total_cost:.2f}")
    if total_dur > 0:
        extras.append(_format_duration(total_dur))
    if extras:
        summary += f"  {_D}— {', '.join(extras)}{_0}"
    click.echo(f"  {summary}")


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
                click.echo(
                    f"Task #{task_id} is '{t.get('status')}', not 'failed'. Use --force to override.", err=True
                )
                sys.exit(1)
            updates: dict = {
                "status": "pending", "attempts": 0,
                "session_id": None, "error": None, "error_code": None,
            }
            if feedback:
                updates["feedback"] = feedback
            update_task(tasks_path, t["key"], **updates)
            click.echo(f"{_G}✓{_0} Reset task {_B}#{task_id}{_0} to pending")
            if feedback:
                click.echo(f"  {_D}Feedback: {feedback}{_0}")
            return
    click.echo(f"Task #{task_id} not found", err=True)
    sys.exit(1)


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("task_id", type=int)
def delete(task_id):
    """Remove a task from the status list (does NOT revert code).

    For pending tasks: removes the task before it runs.
    For passed tasks: only removes from tracking — merged code stays on main.
    For failed tasks: removes from tracking (branch already cleaned up).

    To undo all otto code changes, use 'otto reset --hard'.
    """
    tasks_path = Path.cwd() / "tasks.yaml"
    tasks = load_tasks(tasks_path)
    target = None
    for t in tasks:
        if t.get("id") == task_id:
            target = t
            break
    if not target:
        click.echo(f"Task #{task_id} not found", err=True)
        sys.exit(1)

    task_status = target.get("status", "pending")
    if task_status == "running":
        click.echo(f"{_R}✗{_0} Cannot delete a running task. Wait for it to finish or reset.", err=True)
        sys.exit(1)
    if task_status == "passed":
        click.echo(f"{_Y}⚠{_0} Task already merged — this only removes it from the status list.")
        click.echo(f"  {_D}Code, commits, and test files stay on main.{_0}")
        click.echo(f"  {_D}To undo code changes: 'otto reset --hard' or 'git revert'.{_0}")
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
        click.echo(f"{_Y}⚠{_0} Pending tasks depend on this one: {dep_ids}")
        click.confirm("  Continue?", abort=True)

    delete_task(tasks_path, task_id)
    click.echo(f"{_G}✓{_0} Deleted task {_B}#{task_id}{_0}: {target['prompt'][:60]}")


@main.command(context_settings=CONTEXT_SETTINGS)
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


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("task_id", type=int)
def diff(task_id):
    """Show the git diff for a task's commit."""
    import subprocess
    # Find commit by message pattern
    result = subprocess.run(
        ["git", "log", "--oneline", "--all", f"--grep=(#{task_id})"],
        capture_output=True, text=True,
    )
    commits = result.stdout.strip().splitlines()
    if not commits:
        click.echo(f"No commit found for task #{task_id}", err=True)
        sys.exit(1)
    sha = commits[0].split()[0]
    # Show the diff
    subprocess.run(["git", "show", sha])


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("task_id", type=int)
def show(task_id):
    """Show details for a task."""
    tasks_path = Path.cwd() / "tasks.yaml"
    tasks = load_tasks(tasks_path)
    for t in tasks:
        if t.get("id") == task_id:
            # Print styled details
            click.echo(f"{_B}Task #{task_id}{_0}  {_D}({t.get('key', '?')}){_0}")
            click.echo(f"  {_D}Status:{_0}   {t.get('status', '?')}")
            click.echo(f"  {_D}Attempts:{_0} {t.get('attempts', 0)}")
            cost = t.get("cost_usd", 0.0)
            if cost:
                click.echo(f"  {_D}Cost:{_0}     ${cost:.2f}")
            deps = t.get("depends_on") or []
            if deps:
                click.echo(f"  {_D}Deps:{_0}     {', '.join(f'#{d}' for d in deps)}")
            click.echo(f"\n  {_D}Prompt:{_0}")
            click.echo(f"  {t['prompt']}")
            rubric = t.get("rubric", [])
            if rubric:
                click.echo(f"\n  {_D}Rubric ({len(rubric)}):{_0}")
                for i, item in enumerate(rubric, 1):
                    click.echo(f"    {i}. {item}")
            if t.get("feedback"):
                click.echo(f"\n  {_D}Feedback:{_0} {t['feedback']}")
            if t.get("error"):
                click.echo(f"\n  {_R}Error:{_0} {t['error']}")
            # Find commit
            import subprocess
            result = subprocess.run(
                ["git", "log", "--oneline", "--all", f"--grep=(#{task_id})"],
                capture_output=True, text=True,
            )
            if result.stdout.strip():
                click.echo(f"\n  {_D}Commit:{_0} {result.stdout.strip().splitlines()[0]}")
            # Check for test file
            test_file = Path.cwd() / "tests" / f"test_otto_{t['key']}.py"
            if test_file.exists():
                click.echo(f"  {_D}Test file:{_0} {test_file.relative_to(Path.cwd())}")
            # Check for logs
            log_dir = Path.cwd() / "otto_logs" / t["key"]
            if log_dir.exists():
                click.echo(f"  {_D}Logs:{_0} {log_dir.relative_to(Path.cwd())}/")
            return
    click.echo(f"Task #{task_id} not found", err=True)
    sys.exit(1)


@main.command(context_settings=CONTEXT_SETTINGS)
@click.option("--yes", is_flag=True, help="Skip confirmation")
@click.option("--hard", is_flag=True, help="Also revert otto commits from git history")
def reset(yes, hard):
    """Reset all tasks and clean up branches.

    --hard also reverts otto's git commits, restoring the codebase
    to the state before otto ran.
    """
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
        click.echo("Cannot reset while otto is running", err=True)
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
                    click.echo(f"  {_D}Reset to before first otto commit ({len(otto_commits)} commits removed){_0}")
                else:
                    click.echo(f"{_Y}⚠{_0} Could not find parent of oldest otto commit", err=True)

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

        # Clean committed otto test files (git rm so tree stays clean)
        import glob
        otto_test_files = (
            glob.glob(str(project_dir / "tests" / "test_otto_*.py"))
            + glob.glob(str(project_dir / "tests" / "otto_verify_*.py"))
            + [str(project_dir / "tests" / "otto_integration.py")]
        )
        removed_any = False
        for f in otto_test_files:
            if Path(f).exists():
                subprocess.run(["git", "rm", "-f", f], cwd=project_dir, capture_output=True)
                removed_any = True
        if removed_any:
            subprocess.run(
                ["git", "commit", "-m", "otto: clean up test files"],
                cwd=project_dir, capture_output=True,
            )

        msg = f"{_G}✓{_0} Reset {_B}{count}{_0} tasks. Cleaned branches, logs, and testgen."
        if hard:
            msg += " Reverted otto commits."
        click.echo(msg)
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
