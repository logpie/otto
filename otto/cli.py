"""Otto CLI — entrypoint for all otto commands."""

import asyncio
import sys
from pathlib import Path

import click

from otto.config import create_config, git_meta_dir, load_config
from otto.spec import generate_spec, parse_markdown_tasks
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
    """Import tasks from .md, .txt, or .yaml files with spec generation.

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
            if t.get("spec"):
                item["spec"] = t["spec"]
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
            click.echo(f"[{i}/{len(lines)}] Generating spec for: {line[:50]}...")
            spec_items = generate_spec(line, project_dir)
            item = {"prompt": line}
            if spec_items:
                item["spec"] = spec_items
                click.echo(f"  {len(spec_items)} criteria generated")
            else:
                click.echo(f"  no spec generated")
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
            if t.get("spec"):
                item["spec"] = t["spec"]
                click.echo(f"[{i}/{len(imported)}] {t['prompt'][:50]} — {len(t['spec'])} spec items (from file)")
            else:
                click.echo(f"[{i}/{len(imported)}] Generating spec for: {t['prompt'][:50]}...")
                spec_items = generate_spec(t["prompt"], project_dir)
                if spec_items:
                    item["spec"] = spec_items
                    click.echo(f"  {len(spec_items)} criteria generated")
                else:
                    click.echo(f"  no spec generated")
            if t.get("verify"):
                item["verify"] = t["verify"]
            if t.get("max_retries") is not None:
                item["max_retries"] = t["max_retries"]
            batch.append(item)
        click.echo()
        results = add_tasks(tasks_path, batch)
        _print_imported_tasks(results)


def _print_imported_tasks(tasks: list) -> None:
    """Print summary of imported tasks with spec details."""
    for task in tasks:
        spec = task.get("spec", [])
        click.echo(f"  {_G}✓{_0} {_B}#{task['id']}{_0} {task['prompt'][:80]}")
        if spec:
            for item in spec:
                click.echo(f"       {_D}-{_0} {item}")
    click.echo(f"\n{_G}✓{_0} Imported {_B}{len(tasks)}{_0} tasks. Review specs in tasks.yaml before running.")


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("prompt", required=False)
@click.option("--verify", default=None, help="Custom verification command")
@click.option("--max-retries", default=None, type=int, help="Max retry attempts")
@click.option("-f", "--file", "import_file", default=None, type=click.Path(exists=True),
              help="Import tasks from a file (.yaml, .md, .txt)")
@click.option("--no-spec", is_flag=True, help="Skip spec generation")
def add(prompt, verify, max_retries, import_file, no_spec):
    """Add a task to the queue (or import from file with -f)."""
    _require_git()
    project_dir = Path.cwd()

    # Auto-init if otto.yaml doesn't exist
    config_path = project_dir / "otto.yaml"
    if not config_path.exists():
        create_config(project_dir)
        config = load_config(config_path)
        click.echo(f"{_G}✓{_0} Auto-initialized otto  {_D}(default_branch: {config['default_branch']}, max_retries: {config['max_retries']}){_0}")
        click.echo(f"  {_D}Tip: commit otto.yaml and customize settings if needed{_0}")

    tasks_path = project_dir / "tasks.yaml"

    if import_file:
        _import_tasks(Path(import_file), tasks_path)
        click.echo(f"\n  {_D}Run 'otto arch' to analyze codebase and establish shared conventions{_0}")
        return

    if not prompt:
        click.echo("Error: provide a prompt or use -f to import", err=True)
        sys.exit(2)

    # Generate spec unless --no-spec
    spec = None
    if no_spec:
        click.echo(f"{_Y}{_B}⚠ WARNING:{_0} {_Y}No spec → no adversarial tests → no verification gate.{_0}")
        click.echo(f"  {_Y}The coding agent's output will be merged with zero quality checks.{_0}")
    if not no_spec:
        click.echo(f"{_D}Generating spec...{_0}")
        try:
            spec_items = generate_spec(prompt, Path.cwd())
        except Exception as e:
            click.echo(f"{_R}✗{_0} Spec generation failed: {e}", err=True)
            click.echo(f"{_D}Task not created. Fix the issue or use --no-spec.{_0}", err=True)
            sys.exit(1)
        if spec_items:
            spec = spec_items
            click.echo(f"{_G}✓{_0} Spec ({_B}{len(spec_items)}{_0} criteria):")
            for item in spec_items:
                click.echo(f"  {_D}-{_0} {item}")
        else:
            click.echo(f"{_Y}⚠{_0} Spec generation returned empty — task not created.", err=True)
            click.echo(f"{_D}Retry or use --no-spec to skip spec generation.{_0}", err=True)
            sys.exit(1)

    task = add_task(tasks_path, prompt, verify=verify, max_retries=max_retries,
                    spec=spec)
    click.echo(f"{_G}✓{_0} Added task {_B}#{task['id']}{_0} {_D}({task['key']}){_0}: {prompt}")


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("prompt", required=False)
@click.option("--dry-run", is_flag=True, help="Show what would run without executing")
@click.option("--no-architect", is_flag=True, help="Skip architect agent (no codebase analysis)")
@click.option("--tdd", is_flag=True, help="Generate adversarial tests before coding (optional)")
def run(prompt, dry_run, no_architect, tdd):
    """Run pending tasks (or a one-off task if prompt given)."""
    from otto.runner import run_task

    _require_git()
    project_dir = Path.cwd()
    config_path = project_dir / "otto.yaml"
    if not config_path.exists():
        create_config(project_dir)
        click.echo(f"{_G}✓{_0} Auto-initialized otto")
    config = load_config(config_path)
    if no_architect:
        config["no_architect"] = True
    if tdd:
        config["tdd"] = True

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

        if tdd:
            click.echo(f"{_Y}⚠{_0} --tdd ignored for one-off prompts (no spec). Use 'otto add' + 'otto run --tdd'.", err=True)

        lock_path = git_meta_dir(project_dir) / "otto.lock"
        lock_path.touch()
        lock_fh = open(lock_path, "r")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            click.echo("Another otto process is running", err=True)
            sys.exit(2)

        try:
            # Dirty-tree protection
            from otto.runner import check_clean_tree
            if not check_clean_tree(project_dir):
                click.echo(f"{_R}✗{_0} Working tree is dirty — fix before running otto", err=True)
                sys.exit(2)

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
        # Preflight: check mcp is importable
        try:
            import importlib
            importlib.import_module("mcp")
        except ImportError:
            click.echo("Error: mcp library required. Install with: pip install mcp", err=True)
            sys.exit(2)
        from otto.pilot import run_piloted
        exit_code = asyncio.run(run_piloted(config, tasks_path, project_dir))
        sys.exit(exit_code)


@main.command(context_settings=CONTEXT_SETTINGS)
@click.option("--show", is_flag=True, help="Print summary of current architect docs")
@click.option("--clean", is_flag=True, help="Delete otto_arch/")
def arch(show, clean):
    """Analyze codebase and establish shared conventions for agents."""
    import shutil

    project_dir = Path.cwd()
    arch_dir = project_dir / "otto_arch"

    if clean:
        import fcntl
        _require_git()
        lock_path = git_meta_dir(project_dir) / "otto.lock"
        lock_path.touch()
        lock_fh = open(lock_path, "r")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            click.echo("Cannot clean architect docs while otto is running", err=True)
            sys.exit(2)
        try:
            if arch_dir.exists():
                shutil.rmtree(arch_dir)
                click.echo(f"{_G}✓{_0} Deleted otto_arch/")
            else:
                click.echo(f"{_D}No otto_arch/ to clean{_0}")
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()
        return

    if show:
        if not arch_dir.exists():
            click.echo(f"{_D}No otto_arch/ found. Run 'otto arch' to create.{_0}")
            return
        for f in sorted(arch_dir.iterdir()):
            if f.name.startswith("."):
                continue
            click.echo(f"\n{_B}{'─' * 40}{_0}")
            click.echo(f"{_B}  {f.name}{_0}")
            click.echo(f"{_B}{'─' * 40}{_0}")
            click.echo(f.read_text())
        return

    # Acquire process lock — prevent concurrent arch + run
    import fcntl
    _require_git()
    lock_path = git_meta_dir(project_dir) / "otto.lock"
    lock_path.touch()
    lock_fh = open(lock_path, "r")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        click.echo("Cannot run architect while otto is running", err=True)
        sys.exit(2)

    try:
        tasks_path = project_dir / "tasks.yaml"
        from otto.tasks import load_tasks
        tasks = load_tasks(tasks_path) if tasks_path.exists() else []
        pending = [t for t in tasks if t.get("status") == "pending"]

        action = "Updating" if arch_dir.exists() else "Analyzing codebase"
        click.echo(f"{_D}{action}...{_0}")

        from otto.architect import run_architect_agent
        result = asyncio.run(run_architect_agent(pending, project_dir))
        if result:
            click.echo(f"{_G}✓{_0} Architecture docs ready in {_B}otto_arch/{_0}")
            for f in sorted(result.iterdir()):
                if f.name.startswith("."):
                    continue
                click.echo(f"  {_D}-{_0} {f.name}")
        else:
            click.echo(f"{_R}✗{_0} Architect agent failed", err=True)
            sys.exit(1)
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


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

    click.echo(f"{_B}{'ID':>4}  {'Status':10}  {'Att':>3}  {'Deps':>4}  {'Spec':>6}  {'Cost':>7}  {'Time':>6}  Prompt{_0}")
    click.echo(f"{_D}{'─' * 94}{_0}")
    for t in tasks:
        status_str = t.get("status", "?")
        spec_count = len(t.get("spec", []))
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
            f"{deps_str:>4}  {spec_count:>6}  {cost_str:>7}  {dur_str:>6}  {t['prompt'][:50]}"
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
            spec = t.get("spec", [])
            if spec:
                click.echo(f"\n  {_D}Spec ({len(spec)}):{_0}")
                for i, item in enumerate(spec, 1):
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

        # Clean logs and architect docs (hard only)
        import shutil
        log_dir = project_dir / "otto_logs"
        if log_dir.exists():
            shutil.rmtree(log_dir)
        if hard:
            arch_dir = project_dir / "otto_arch"
            if arch_dir.exists():
                shutil.rmtree(arch_dir)

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


# ---------------------------------------------------------------------------
# Benchmark subcommands
# ---------------------------------------------------------------------------

@main.group(context_settings=CONTEXT_SETTINGS)
def bench():
    """Benchmark system — measure and compare pipeline effectiveness."""
    pass


def _get_bench_dir() -> Path:
    """Locate the bench/ directory (in the otto repo root)."""
    # Walk up from CWD or use the otto package location
    otto_root = Path(__file__).parent.parent
    bench_dir = otto_root / "bench"
    if not bench_dir.exists():
        click.echo("Error: bench/ directory not found.", err=True)
        sys.exit(2)
    return bench_dir


def _get_runner(name: str):
    """Import and instantiate a runner by name."""
    import importlib.util

    bench_dir = _get_bench_dir()
    runner_files = {
        "otto": "otto_runner.py",
        "bare-cc": "bare_cc_runner.py",
        "ralph": "ralph_runner.py",
        "self-test": "self_test_runner.py",
    }
    runner_classes = {
        "otto": "OttoRunner",
        "bare-cc": "BareClaudeRunner",
        "ralph": "RalphRunner",
        "self-test": "SelfTestRunner",
    }

    if name not in runner_files:
        click.echo(f"Unknown runner: {name}. Available: {', '.join(runner_files)}", err=True)
        sys.exit(2)

    runner_path = bench_dir / "runners" / runner_files[name]
    if not runner_path.exists():
        click.echo(f"Runner file not found: {runner_path}", err=True)
        sys.exit(2)

    spec = importlib.util.spec_from_file_location(f"bench_runner_{name}", runner_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cls = getattr(mod, runner_classes[name])
    return cls()


@bench.command("run", context_settings=CONTEXT_SETTINGS)
@click.option("--task", "task_names", multiple=True, help="Run specific task(s) by name")
@click.option("--runner", "runner_name", default="otto", help="Runner to use (otto, bare-cc, ralph, self-test)")
@click.option("--label", default="", help="Label for this run (for comparison)")
@click.option("--difficulty", type=click.Choice(["easy", "medium", "hard"]), help="Filter by difficulty")
@click.option("--suite", "suite_file", default="suite.yaml", help="Suite file (default: suite.yaml)")
def bench_run(task_names, runner_name, label, difficulty, suite_file):
    """Run benchmark tasks and save results."""
    from otto.bench import filter_tasks, load_suite, run_bench, save_results

    bench_dir = _get_bench_dir()
    suite_path = bench_dir / suite_file

    if not suite_path.exists():
        click.echo(f"Error: {suite_path} not found.", err=True)
        sys.exit(2)

    tasks = load_suite(suite_path)
    if not tasks:
        click.echo("No tasks found in suite.", err=True)
        sys.exit(2)

    # Apply filters
    tasks = filter_tasks(
        tasks,
        difficulty=difficulty,
        names=list(task_names) if task_names else None,
    )
    if not tasks:
        click.echo("No tasks match the given filters.", err=True)
        sys.exit(2)

    runner = _get_runner(runner_name)
    click.echo(f"{_B}Otto Bench{_0} — {len(tasks)} tasks, runner: {_C}{runner_name}{_0}")
    if label:
        click.echo(f"  Label: {label}")
    click.echo()

    run = asyncio.run(run_bench(bench_dir, tasks, runner, label=label))

    # Save results
    results_dir = bench_dir / "results"
    out_path = save_results(run, results_dir)

    # Print summary
    s = run.summary
    click.echo(f"\n{'━' * 50}")
    click.echo(f"{_B}Results:{_0} {out_path.name}")
    click.echo(f"  Success: {_G}{s['passed']}{_0}/{s['total']}  ({s['success_rate'] * 100:.1f}%)")
    click.echo(f"  Cost:    ${s['total_cost']:.2f}  (${s['cost_per_success']:.2f}/success)")
    click.echo(f"  Time:    {s['total_time_s']:.0f}s  ({s['time_per_success_s']:.0f}s/success)")
    if s["mean_mutation_score"] > 0:
        click.echo(f"  Mutation: {s['mean_mutation_score']:.3f}")


@bench.command("compare", context_settings=CONTEXT_SETTINGS)
@click.argument("baseline")
@click.argument("current")
def bench_compare(baseline, current):
    """Compare two benchmark runs.

    Arguments are result filenames or labels. Searches bench/results/ for matches.
    """
    from otto.bench import compare_runs, load_results, list_results

    bench_dir = _get_bench_dir()
    results_dir = bench_dir / "results"

    def _find_result(query: str) -> Path:
        """Find a result file by filename or label."""
        # Try exact filename
        exact = results_dir / query
        if exact.exists():
            return exact
        # Try with .json suffix
        with_ext = results_dir / f"{query}.json"
        if with_ext.exists():
            return with_ext
        # Search by label
        for name, run in list_results(results_dir):
            if run.label == query or run.run_id == query:
                return results_dir / name
        click.echo(f"Result not found: {query}", err=True)
        sys.exit(2)

    baseline_path = _find_result(baseline)
    current_path = _find_result(current)

    baseline_run = load_results(baseline_path)
    current_run = load_results(current_path)

    click.echo(compare_runs(baseline_run, current_run))


@bench.command("history", context_settings=CONTEXT_SETTINGS)
@click.option("--limit", "-n", default=20, help="Number of runs to show")
def bench_history(limit):
    """Show recent benchmark run history."""
    from otto.bench import list_results

    bench_dir = _get_bench_dir()
    results_dir = bench_dir / "results"

    runs = list_results(results_dir)
    if not runs:
        click.echo(f"{_D}No benchmark results found.{_0}")
        return

    click.echo(f"{_B}{'Run ID':24}  {'Runner':10}  {'Label':20}  {'Pass':>6}  {'Cost':>8}  {'Time':>8}{_0}")
    click.echo(f"{_D}{'─' * 90}{_0}")

    for name, run in runs[:limit]:
        s = run.summary
        sr = s["success_rate"] * 100
        click.echo(
            f"{run.run_id:24}  {run.runner:10}  {run.label:20}  "
            f"{s['passed']:>2}/{s['total']:<2} {sr:>3.0f}%  "
            f"${s['total_cost']:>6.2f}  {s['total_time_s']:>6.0f}s"
        )
