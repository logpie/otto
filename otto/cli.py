"""Otto CLI — entrypoint for all otto commands."""

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import click

from otto.config import create_config, git_meta_dir, load_config
from otto.display import console, rich_escape
from otto.theme import error_console
from otto.spec import generate_spec, parse_markdown_tasks
from otto.tasks import add_task, add_tasks, delete_task, load_tasks, reset_all_tasks, save_tasks, update_task


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m{secs:02d}s"


def _format_cost(cost: float) -> str:
    """Format cost for display."""
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"


# ---------------------------------------------------------------------------
# Log parsing helpers (shared by show/logs commands)
# ---------------------------------------------------------------------------

def _load_progress_events(log_dir: Path) -> list[dict]:
    """Load progress events from pilot_results.jsonl that match this task key."""
    results_file = log_dir.parent / "pilot_results.jsonl"
    task_key = log_dir.name
    events = []
    if not results_file.exists():
        return events
    try:
        for line in results_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("task_key") == task_key:
                    events.append(data)
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return events


def _extract_phase_timings(events: list[dict]) -> dict[str, float]:
    """Extract per-phase timings from progress events."""
    timings: dict[str, float] = {}
    for evt in events:
        if evt.get("event") == "phase" and evt.get("status") in ("done", "fail"):
            name = evt.get("name", "")
            time_s = evt.get("time_s", 0.0)
            if name and time_s:
                timings[name] = timings.get(name, 0) + time_s
    return timings


def _parse_qa_report(log_dir: Path, events: list[dict]) -> dict:
    """Parse QA report from file or progress events.

    Returns dict with keys: exists, passed, total, summary_lines.
    """
    result = {"exists": False, "passed": 0, "total": 0, "summary_lines": []}

    # Try qa-report.md file first
    qa_file = log_dir / "qa-report.md"
    if qa_file.exists():
        result["exists"] = True
        content = qa_file.read_text()
        lines = content.strip().splitlines()
        # Extract pass/fail counts from markdown checkboxes or verdict lines
        passed = 0
        total = 0
        summary = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("- [x]") or stripped.startswith("- [X]"):
                total += 1
                passed += 1
            elif stripped.startswith("- [ ]"):
                total += 1
            # Capture layer summaries
            if stripped.lower().startswith("layer") or stripped.lower().startswith("## layer"):
                summary.append(stripped.lstrip("#").strip())
            # Capture verdict line
            if "verdict" in stripped.lower() or "result" in stripped.lower():
                summary.append(stripped.lstrip("#").strip())
        result["passed"] = passed
        result["total"] = total
        result["summary_lines"] = summary[:5]
        return result

    # Fall back to progress events for qa_report field
    for evt in reversed(events):
        if evt.get("tool") == "run_task_with_qa":
            qa_text = evt.get("qa_report", "")
            if qa_text:
                result["exists"] = True
                # Parse pass/fail from the report text
                passed = 0
                total = 0
                summary = []
                for line in qa_text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("- [x]") or stripped.startswith("- [X]"):
                        total += 1
                        passed += 1
                    elif stripped.startswith("- [ ]"):
                        total += 1
                    if "PASS" in stripped.upper() and ("/" in stripped or "of" in stripped):
                        summary.append(stripped)
                    if stripped.lower().startswith("layer") or stripped.lower().startswith("## layer"):
                        summary.append(stripped.lstrip("#").strip())
                result["passed"] = passed
                result["total"] = total
                result["summary_lines"] = summary[:5]
            break

    return result


def _get_diff_stat(task_id: int, project_dir: Path) -> list[str]:
    """Get diff --stat lines for a task's commit."""
    import subprocess
    result = subprocess.run(
        ["git", "log", "--oneline", "--all", f"--grep=(#{task_id})"],
        capture_output=True, text=True, cwd=project_dir,
    )
    if not result.stdout.strip():
        return []
    sha = result.stdout.strip().splitlines()[0].split()[0]
    stat = subprocess.run(
        ["git", "diff", "--stat", f"{sha}~1", sha],
        capture_output=True, text=True, cwd=project_dir,
    )
    if stat.returncode != 0:
        return []
    return [l.strip() for l in stat.stdout.strip().splitlines() if l.strip()]


def _get_agent_log_highlights(log_dir: Path) -> tuple[list[str], list[str]]:
    """Get first and last few lines of the most recent agent log.

    Returns (first_lines, last_lines).
    """
    logs = sorted(log_dir.glob("attempt-*-agent.log"), reverse=True)
    if not logs:
        return ([], [])
    try:
        content = logs[0].read_text()
    except OSError:
        return ([], [])
    lines = [l for l in content.splitlines() if l.strip()]
    if not lines:
        return ([], [])
    first = lines[:3]
    last = lines[-3:] if len(lines) > 6 else []
    return (first, last)


def _get_verify_summary(log_dir: Path) -> str:
    """Get a one-line verify summary from the most recent verify log.

    Returns a Rich-markup string.
    """
    # Check verify.log first (written on pass)
    verify_file = log_dir / "verify.log"
    if verify_file.exists():
        content = verify_file.read_text().strip()
        if content == "PASSED":
            return "[success]PASSED[/success]"

    # Check attempt verify logs
    logs = sorted(log_dir.glob("attempt-*-verify.log"), reverse=True)
    if not logs:
        return ""
    try:
        content = logs[0].read_text()
    except OSError:
        return ""

    # Parse tier results
    passed = content.count(": PASS")
    failed = content.count(": FAIL")
    total = passed + failed
    if total == 0:
        return ""

    # Count tests if available (look for "N passed" or "N tests")
    import re
    test_count = ""
    m = re.search(r"(\d+)\s+passed", content)
    if m:
        test_count = f" ({m.group(1)} tests)"

    if failed == 0:
        return f"[success]PASSED[/success][dim]{test_count}[/dim]"
    return f"[error]FAILED[/error][dim] ({passed}/{total} tiers){test_count}[/dim]"


def _require_git():
    """Exit with a friendly error if not in a git repo."""
    import subprocess
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        capture_output=True, cwd=Path.cwd(),
    )
    if result.returncode != 0:
        error_console.print("Error: not a git repository. Run 'git init' first.", style="error")
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
    console.print(f"[success]✓[/success] Created [bold]{rich_escape(config_path.name)}[/bold]")
    console.print(f"  [dim]default_branch:[/dim] {config['default_branch']}")
    console.print(f"  [dim]max_retries:[/dim]    {config['max_retries']}")
    console.print(f"\n[dim]Commit otto.yaml to share config with your team.[/dim]")


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
        console.print(f"Parsing {rich_escape(import_path.name)} (this may take 10-20s)...")
        parsed = parse_markdown_tasks(import_path, project_dir)
        console.print(f"Extracted {len(parsed)} tasks from markdown.\n")
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
        console.print(f"Found {len(lines)} tasks in {rich_escape(import_path.name)}.\n")
        batch = []
        for i, line in enumerate(lines, 1):
            console.print(f"  [dim]{i}/{len(lines)}[/dim] {rich_escape(line[:50])}")
            spec_items = generate_spec(line, project_dir)
            item = {"prompt": line}
            if spec_items:
                item["spec"] = spec_items
                console.print(f"  {len(spec_items)} criteria generated")
            else:
                console.print(f"  no spec generated")
            batch.append(item)
        console.print()
        results = add_tasks(tasks_path, batch)
        _print_imported_tasks(results)

    else:
        data = _yaml.safe_load(import_path.read_text()) or {}
        imported = data.get("tasks", [])
        console.print(f"Found {len(imported)} tasks in {rich_escape(import_path.name)}.\n")
        batch = []
        for i, t in enumerate(imported, 1):
            item = {"prompt": t["prompt"]}
            if t.get("spec"):
                item["spec"] = t["spec"]
                console.print(f"  [dim]{i}/{len(imported)}[/dim] {rich_escape(t['prompt'][:50])}")
            else:
                console.print(f"  [dim]{i}/{len(imported)}[/dim] {rich_escape(t['prompt'][:50])}")
                spec_items = generate_spec(t["prompt"], project_dir)
                if spec_items:
                    item["spec"] = spec_items
                    console.print(f"  {len(spec_items)} criteria generated")
                else:
                    console.print(f"  no spec generated")
            if t.get("verify"):
                item["verify"] = t["verify"]
            if t.get("max_retries") is not None:
                item["max_retries"] = t["max_retries"]
            batch.append(item)
        console.print()
        results = add_tasks(tasks_path, batch)
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
        console.print(f"[success]✓[/success] Auto-initialized otto  [dim](default_branch: {config['default_branch']}, max_retries: {config['max_retries']})[/dim]")
        console.print(f"  [dim]Tip: commit otto.yaml and customize settings if needed[/dim]")

    tasks_path = project_dir / "tasks.yaml"

    if import_file:
        _import_tasks(Path(import_file), tasks_path)
        console.print(f"\n  [dim]Run 'otto arch' to analyze codebase and establish shared conventions[/dim]")
        return

    if not prompt:
        error_console.print("Error: provide a prompt or use -f to import", style="error")
        sys.exit(2)

    # Generate spec unless --no-spec
    spec = None
    if no_spec:
        console.print(f"[warning][bold]⚠ WARNING:[/bold][/warning] [warning]No spec → no adversarial tests → no verification gate.[/warning]")
        console.print(f"  [warning]The coding agent's output will be merged with zero quality checks.[/warning]")
    if not no_spec:
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
            error_console.print(f"[error]✗[/error] Spec generation failed: {rich_escape(str(e))}")
            error_console.print(f"[dim]Task not created. Fix the issue or use --no-spec.[/dim]")
            sys.exit(1)
        if spec_items:
            spec = spec_items
            from otto.tasks import spec_text, spec_is_verifiable
            verifiable_count = sum(1 for i in spec_items if spec_is_verifiable(i))
            visual_count = len(spec_items) - verifiable_count
            label = f"{verifiable_count} verifiable"
            if visual_count:
                label += f", {visual_count} visual"
            console.print(f"[success]✓[/success] Spec ([bold]{len(spec_items)}[/bold] criteria \u2014 {label}):")
            for idx, item in enumerate(spec_items, 1):
                text = rich_escape(spec_text(item))
                if spec_is_verifiable(item):
                    console.print(f"  [dim]{idx}.[/dim] [dim]\u25b8[/dim] {text}")
                else:
                    console.print(f"  [dim]{idx}.[/dim] [info]\u25c9[/info] {text}")
        else:
            error_console.print(f"[warning]⚠[/warning] Spec generation returned empty \u2014 task not created.")
            error_console.print(f"[dim]Retry or use --no-spec to skip spec generation.[/dim]")
            sys.exit(1)

    task = add_task(tasks_path, prompt, verify=verify, max_retries=max_retries,
                    spec=spec)
    console.print(f"[success]✓[/success] Added task [bold]#{task['id']}[/bold] [dim]({task['key']})[/dim]: {rich_escape(prompt[:70])}")


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
        console.print(f"[success]✓[/success] Auto-initialized otto")
    config = load_config(config_path)
    if no_architect:
        config["no_architect"] = True
    if tdd:
        config["tdd"] = True

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
        # Still acquires process lock to prevent concurrent runs
        import fcntl
        import os
        import time

        if tdd:
            error_console.print(f"[warning]⚠[/warning] --tdd ignored for one-off prompts (no spec). Use 'otto add' + 'otto run --tdd'.")

        lock_path = git_meta_dir(project_dir) / "otto.lock"
        lock_path.touch()
        lock_fh = open(lock_path, "r")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            error_console.print("Another otto process is running", style="error")
            sys.exit(2)

        try:
            # Dirty-tree protection
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
            error_console.print("Error: mcp library required. Install with: pip install mcp", style="error")
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
            error_console.print("Cannot clean architect docs while otto is running", style="error")
            sys.exit(2)
        try:
            if arch_dir.exists():
                shutil.rmtree(arch_dir)
                console.print(f"[success]✓[/success] Deleted otto_arch/")
            else:
                console.print(f"[dim]No otto_arch/ to clean[/dim]")
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()
        return

    if show:
        if not arch_dir.exists():
            console.print(f"[dim]No otto_arch/ found. Run 'otto arch' to create.[/dim]")
            return
        for f in sorted(arch_dir.iterdir()):
            if f.name.startswith("."):
                continue
            rule = "\u2500" * 40
            console.print(f"\n[bold]{rule}[/bold]")
            console.print(f"[bold]  {rich_escape(f.name)}[/bold]")
            console.print(f"[bold]{rule}[/bold]")
            console.print(f.read_text())
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
        error_console.print("Cannot run architect while otto is running", style="error")
        sys.exit(2)

    try:
        tasks_path = project_dir / "tasks.yaml"
        from otto.tasks import load_tasks
        tasks = load_tasks(tasks_path) if tasks_path.exists() else []
        pending = [t for t in tasks if t.get("status") == "pending"]

        action = "Updating" if arch_dir.exists() else "Analyzing codebase"
        console.print(f"[dim]{action}...[/dim]")

        from otto.architect import run_architect_agent
        result = asyncio.run(run_architect_agent(pending, project_dir))
        if result:
            console.print(f"[success]✓[/success] Architecture docs ready in [bold]otto_arch/[/bold]")
            for f in sorted(result.iterdir()):
                if f.name.startswith("."):
                    continue
                console.print(f"  [dim]-[/dim] {rich_escape(f.name)}")
        else:
            error_console.print(f"[error]✗[/error] Architect agent failed")
            sys.exit(1)
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


def _build_status_table(tasks: list[dict], show_phase: bool = False):
    """Build a Rich Table for task status display.

    Returns a Rich renderable (Table + optional extras via Group).
    """
    from rich.table import Table
    from rich.text import Text
    from rich.console import Group

    table = Table(show_header=True, box=None, pad_edge=False, show_edge=False, expand=False)
    table.add_column("#", style="bold", min_width=2, justify="right")
    table.add_column("Status", min_width=7)
    table.add_column("Att", min_width=3, justify="right")
    table.add_column("Spec", min_width=4, justify="right")
    table.add_column("Cost", min_width=6, justify="right", style="dim")
    table.add_column("Time", min_width=5, justify="right", style="dim")
    table.add_column("Prompt", ratio=1, no_wrap=True)

    for t in tasks:
        status_str = t.get("status", "?")
        spec_count = len(t.get("spec", []))
        cost = t.get("cost_usd", 0.0)
        cost_str = f"${cost:.2f}" if cost else ""
        dur = t.get("duration_s", 0.0)
        dur_str = _format_duration(dur) if dur else ""

        # Style the status text
        status_styles = {
            "passed": "success",
            "failed": "error",
            "blocked": "error",
            "running": "info",
            "pending": "dim",
        }
        status_style = status_styles.get(status_str, "dim")
        status_text = Text(status_str, style=status_style)

        # Determine row style
        row_style = ""
        if status_str == "pending":
            row_style = "dim"
        elif status_str == "failed":
            row_style = "dim"

        prompt_text = t['prompt'][:50]
        # For failed/blocked, append error on same prompt line
        error_suffix = ""
        if status_str in ("failed", "blocked") and t.get("error"):
            error_suffix = f"\n        [error]\u21b3 {rich_escape(t['error'][:70])}[/error]"

        table.add_row(
            str(t.get("id", "?")),
            status_text,
            str(t.get("attempts", 0)),
            str(spec_count) if spec_count else "",
            cost_str,
            dur_str,
            rich_escape(prompt_text) + error_suffix,
            style=row_style,
        )

    # Summary line
    counts: dict[str, int] = {}
    total_cost = 0.0
    total_dur = 0.0
    for t in tasks:
        s = t.get("status", "?")
        counts[s] = counts.get(s, 0) + 1
        total_cost += t.get("cost_usd", 0.0)
        total_dur += t.get("duration_s", 0.0)

    parts = []
    if counts.get("passed"):
        parts.append(f"[success]{counts['passed']} passed[/success]")
    if counts.get("failed"):
        parts.append(f"[error]{counts['failed']} failed[/error]")
    if counts.get("blocked"):
        parts.append(f"[error]{counts['blocked']} blocked[/error]")
    if counts.get("pending"):
        parts.append(f"[dim]{counts['pending']} pending[/dim]")
    if counts.get("running"):
        parts.append(f"[info]{counts['running']} running[/info]")
    summary = ", ".join(parts)
    extras = []
    if total_cost > 0:
        extras.append(f"${total_cost:.2f}")
    if total_dur > 0:
        extras.append(_format_duration(total_dur))
    if extras:
        summary += f"  [dim]\u2014 {', '.join(extras)}[/dim]"

    renderables = [table, Text(""), Text.from_markup(f"  {summary}")]

    # Show live task progress from live-state.json if available
    if show_phase:
        live_lines = _build_live_phase_lines()
        if live_lines:
            renderables.append(Text(""))
            renderables.extend(live_lines)

    return Group(*renderables)


def _build_live_phase_lines() -> list:
    """Build Rich Text lines for live phase progress."""
    from rich.text import Text

    lines = []
    live_file = Path.cwd() / "otto_logs" / "live-state.json"
    if live_file.exists():
        try:
            live = json.loads(live_file.read_text())
            tid = live.get("task_id", "?")
            prompt = live.get("prompt", "")[:50]
            elapsed = live.get("elapsed_s", 0)
            cost = live.get("cost_usd", 0)
            elapsed_str = _format_duration(elapsed)
            lines.append(Text.from_markup(f"  [info]▸ Task #{tid}:[/info] {rich_escape(prompt)}"))
            phases = live.get("phases", {})
            _icons = {
                "done": "[success]✓[/success]",
                "fail": "[error]✗[/error]",
                "running": "[info]●[/info]",
                "pending": "[dim]◦[/dim]",
            }
            for pname in ["prepare", "coding", "test", "qa", "merge"]:
                pdata = phases.get(pname, {})
                pstatus = pdata.get("status", "pending")
                icon = _icons.get(pstatus, "◦")
                ptime = pdata.get("time_s", 0)
                extra = ""
                if pstatus == "running":
                    extra = f"  [dim]{elapsed_str}[/dim]"
                elif pstatus == "done" and ptime:
                    extra = f"  [dim]{ptime:.0f}s[/dim]"
                elif pstatus == "fail":
                    err = rich_escape(pdata.get("error", "")[:40])
                    extra = f"  [error]{err}[/error]"
                lines.append(Text.from_markup(f"    {icon} {pname:<10}{extra}"))
            # Show recent tools
            tools = live.get("recent_tools", [])
            for tool_line in tools[-3:]:
                lines.append(Text.from_markup(f"        [dim]{rich_escape(tool_line[:60])}[/dim]"))
            if cost > 0:
                lines.append(Text.from_markup(f"    [dim]${cost:.2f} so far[/dim]"))
        except (json.JSONDecodeError, OSError):
            pass
    else:
        # Fallback: check run-state.json for high-level phase
        state_file = Path.cwd() / "otto_arch" / "run-state.json"
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
                phase = state.get("phase", "")
                if phase:
                    lines.append(Text.from_markup(f"  [info]Phase:[/info] {rich_escape(phase)}"))
            except (json.JSONDecodeError, OSError):
                pass

    return lines


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
        _watch_status(tasks_path)
        return

    console.print(_build_status_table(tasks, show_phase=True))


def _watch_status(tasks_path: Path) -> None:
    """Auto-refresh status display every 2 seconds using Rich Live."""
    from rich.live import Live

    def render():
        tasks = load_tasks(tasks_path)
        return _build_status_table(tasks, show_phase=True)

    try:
        with Live(render(), refresh_per_second=0.5, console=console) as live:
            while True:
                time.sleep(2)
                live.update(render())
    except KeyboardInterrupt:
        console.print(f"\n[dim]Stopped.[/dim]")


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
        error_console.print(f"Task #{task_id} not found", style="error")
        sys.exit(1)

    task_status = target.get("status", "pending")
    if task_status == "running":
        error_console.print(f"[error]✗[/error] Cannot delete a running task. Wait for it to finish or reset.")
        sys.exit(1)
    if task_status == "passed":
        console.print(f"[warning]⚠[/warning] Task already merged \u2014 this only removes it from the status list.")
        console.print(f"  [dim]Code, commits, and test files stay on main.[/dim]")
        console.print(f"  [dim]To undo code changes: 'otto reset --hard' or 'git revert'.[/dim]")
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


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("task_id", type=int)
@click.option("--raw", is_flag=True, help="Dump all log files without formatting")
@click.option("-f", "--follow", is_flag=True, help="Tail logs in real-time (useful during runs)")
def logs(task_id, raw, follow):
    """Show structured logs for a task."""
    import re

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

    log_dir = Path.cwd() / "otto_logs" / target["key"]
    if not log_dir.exists():
        console.print(f"[dim]No logs for task #{task_id}[/dim]")
        return

    # Follow mode: tail the most recent agent log
    if follow:
        _tail_logs(log_dir, task_id)
        return

    # Raw mode: dump everything (old behavior)
    if raw:
        for log_file in sorted(log_dir.iterdir()):
            if log_file.is_file():
                rule = "\u2501" * 40
                console.print(f"\n[bold]{rule}[/bold]")
                console.print(f"[bold]  {rich_escape(log_file.name)}[/bold]")
                console.print(f"[bold]{rule}[/bold]")
                try:
                    content = log_file.read_text()
                    console.print(content)
                except (OSError, UnicodeDecodeError):
                    console.print(f"  [dim](binary or unreadable)[/dim]")
        return

    # Structured mode: show sections with formatting
    console.print(f"\n[bold]Logs for Task #{task_id}[/bold]  [dim]({rich_escape(target['key'])})[/dim]")

    # 1. Verify logs — show result only
    verify_logs = sorted(log_dir.glob("attempt-*-verify.log"))
    if verify_logs:
        console.print(f"\n[bold]  Verification[/bold]")
        for vlog in verify_logs:
            attempt = vlog.stem.split("-")[1] if "-" in vlog.stem else "?"
            try:
                content = vlog.read_text()
                passed = content.count(": PASS")
                failed = content.count(": FAIL")
                # Extract test count
                test_count = ""
                m = re.search(r"(\d+)\s+passed", content)
                if m:
                    test_count = f" ({m.group(1)} tests)"
                if failed == 0 and passed > 0:
                    console.print(f"    [success]Attempt {attempt}: PASS[/success][dim]{test_count}[/dim]")
                elif failed > 0:
                    console.print(f"    [error]Attempt {attempt}: FAIL[/error][dim] ({passed}/{passed+failed} tiers){test_count}[/dim]")
                    # Show failure lines
                    for line in content.splitlines():
                        if ": FAIL" in line:
                            console.print(f"      [error]{rich_escape(line.strip()[:100])}[/error]")
                else:
                    console.print(f"    [dim]Attempt {attempt}: {rich_escape(content[:80])}[/dim]")
            except OSError:
                console.print(f"    [dim]Attempt {attempt}: (unreadable)[/dim]")

    # Also check verify.log (written on final pass)
    verify_file = log_dir / "verify.log"
    if verify_file.exists() and not verify_logs:
        try:
            content = verify_file.read_text().strip()
            if content == "PASSED":
                console.print(f"\n[bold]  Verification[/bold]")
                console.print(f"    [success]PASSED[/success]")
        except OSError:
            pass

    # 2. Agent logs — show tool calls only
    agent_logs = sorted(log_dir.glob("attempt-*-agent.log"))
    if agent_logs:
        console.print(f"\n[bold]  Agent Activity[/bold]")
        for alog in agent_logs:
            attempt = alog.stem.split("-")[1] if "-" in alog.stem else "?"
            try:
                content = alog.read_text()
                lines = content.splitlines()
                # Filter to tool calls (lines starting with bullet)
                tool_lines = [l for l in lines if l.strip().startswith("\u25cf") or l.strip().startswith("*")]
                if tool_lines:
                    console.print(f"    [dim]Attempt {attempt} \u2014 {len(tool_lines)} tool calls:[/dim]")
                    for tl in tool_lines[:10]:
                        console.print(f"      [dim]{rich_escape(tl.strip()[:90])}[/dim]")
                    if len(tool_lines) > 10:
                        console.print(f"      [dim]... ({len(tool_lines) - 10} more)[/dim]")
                else:
                    # No structured tool calls, show first/last few lines
                    console.print(f"    [dim]Attempt {attempt} \u2014 {len(lines)} lines:[/dim]")
                    for l in lines[:3]:
                        console.print(f"      [dim]{rich_escape(l[:90])}[/dim]")
                    if len(lines) > 6:
                        console.print(f"      [dim]...[/dim]")
                        for l in lines[-3:]:
                            console.print(f"      [dim]{rich_escape(l[:90])}[/dim]")
            except OSError:
                console.print(f"    [dim]Attempt {attempt}: (unreadable)[/dim]")

    # 3. QA report
    qa_file = log_dir / "qa-report.md"
    if qa_file.exists():
        console.print(f"\n[bold]  QA Report[/bold]")
        try:
            content = qa_file.read_text()
            # Show formatted (truncated to ~30 lines)
            lines = content.strip().splitlines()
            for line in lines[:30]:
                console.print(f"    {line}")
            if len(lines) > 30:
                console.print(f"    [dim]... ({len(lines) - 30} more lines)[/dim]")
        except OSError:
            console.print(f"    [dim](unreadable)[/dim]")

    # 4. Pilot debug log (if present)
    debug_log = log_dir.parent / "pilot_debug.log"
    if debug_log.exists():
        try:
            content = debug_log.read_text()
            if content.strip():
                console.print(f"\n[bold]  Pilot Debug[/bold]  [dim](use --raw for full output)[/dim]")
                lines = content.strip().splitlines()
                console.print(f"    [dim]{len(lines)} lines \u2014 last 5:[/dim]")
                for line in lines[-5:]:
                    console.print(f"    [dim]{rich_escape(line[:100])}[/dim]")
        except OSError:
            pass

    console.print()


def _tail_logs(log_dir: Path, task_id: int) -> None:
    """Tail the most recent agent log and progress events in real-time."""
    import select

    console.print(f"[bold]Tailing logs for task #{task_id}[/bold]  [dim](Ctrl+C to stop)[/dim]\n")

    # Files to watch
    results_file = log_dir.parent / "pilot_results.jsonl"
    results_pos = results_file.stat().st_size if results_file.exists() else 0

    # Find latest agent log or wait for one
    agent_pos = 0
    last_agent_log = None

    try:
        while True:
            # Check for new agent logs
            agent_logs = sorted(log_dir.glob("attempt-*-agent.log"))
            if agent_logs and agent_logs[-1] != last_agent_log:
                last_agent_log = agent_logs[-1]
                agent_pos = 0

            # Read new agent log content
            if last_agent_log and last_agent_log.exists():
                try:
                    with open(last_agent_log) as f:
                        f.seek(agent_pos)
                        new = f.read()
                        agent_pos = f.tell()
                    if new:
                        for line in new.splitlines():
                            if line.strip():
                                console.print(f"  [dim]{rich_escape(line[:120])}[/dim]")
                except OSError:
                    pass

            # Read new progress events
            if results_file.exists():
                try:
                    with open(results_file) as f:
                        f.seek(results_pos)
                        new_lines = f.readlines()
                        results_pos = f.tell()
                    task_key = log_dir.name
                    for rline in new_lines:
                        rline = rline.strip()
                        if not rline:
                            continue
                        try:
                            data = json.loads(rline)
                            if data.get("task_key") != task_key:
                                continue
                            evt = data.get("event", "")
                            if evt == "phase":
                                name = data.get("name", "")
                                phase_status = data.get("status", "")
                                time_s = data.get("time_s", 0)
                                if phase_status == "running":
                                    console.print(f"  [info]{rich_escape(name)}[/info] started")
                                elif phase_status == "done":
                                    console.print(f"  [success]{rich_escape(name)}[/success] done  [dim]{_format_duration(time_s)}[/dim]")
                                elif phase_status == "fail":
                                    err = rich_escape(data.get("error", "")[:60])
                                    console.print(f"  [error]{rich_escape(name)}[/error] failed  [dim]{err}[/dim]")
                            elif evt == "agent_tool":
                                name = data.get("name", "")
                                detail = data.get("detail", "")[:60]
                                console.print(f"    [dim]{rich_escape(name)}  {rich_escape(detail)}[/dim]")
                        except json.JSONDecodeError:
                            pass
                except OSError:
                    pass

            time.sleep(0.5)
    except KeyboardInterrupt:
        console.print(f"\n[dim]Stopped.[/dim]")


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
        error_console.print(f"No commit found for task #{task_id}", style="error")
        sys.exit(1)
    sha = commits[0].split()[0]
    # Show the diff
    subprocess.run(["git", "show", sha])


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("task_id", type=int)
def show(task_id):
    """Show rich details for a task including timing, QA, and diff."""
    import subprocess
    from rich.panel import Panel
    from otto.tasks import spec_text, spec_is_verifiable

    tasks_path = Path.cwd() / "tasks.yaml"
    project_dir = Path.cwd()
    tasks = load_tasks(tasks_path)
    for t in tasks:
        if t.get("id") == task_id:
            key = t.get("key", "?")
            task_status = t.get("status", "?")
            log_dir = project_dir / "otto_logs" / key

            # Color the status
            status_styles = {
                "passed": "success", "failed": "error", "blocked": "error",
                "running": "info", "pending": "dim",
            }
            status_style = status_styles.get(task_status, "")
            status_styled = f"[{status_style}]{task_status}[/{status_style}]" if status_style else task_status

            att = t.get("attempts", 0)
            cost = t.get("cost_usd", 0.0)
            cost_str = _format_cost(cost) if cost else "n/a"
            dur = t.get("duration_s", 0.0)
            dur_str = _format_duration(dur) if dur else "n/a"

            # Header panel
            console.print(Panel(
                f"Status: {status_styled}  [dim]|[/dim]  Attempts: {att}  [dim]|[/dim]  Cost: {cost_str}\n"
                f"Time: {dur_str}",
                title=f"[bold]Task #{task_id}[/bold]  {rich_escape(t['prompt'][:50])}",
                border_style="dim",
                expand=False,
            ))

            # Duration per-phase timing
            if dur:
                # Try to get per-phase breakdown
                events = _load_progress_events(log_dir) if log_dir.exists() else []
                timings = _extract_phase_timings(events)
                if timings:
                    phase_order = ["prepare", "coding", "test", "qa", "merge"]
                    parts = []
                    for p in phase_order:
                        pt = timings.get(p, 0.0)
                        parts.append(f"{_format_duration(pt)} {p}")
                    console.print(f"  [dim]Phases:[/dim]   {dur_str}  [dim]({' + '.join(parts)})[/dim]")

            deps = t.get("depends_on") or []
            if deps:
                console.print(f"  [dim]Deps:[/dim]     {', '.join(f'#{d}' for d in deps)}")

            # Prompt
            console.print(f"\n  [dim]Prompt:[/dim] {rich_escape(t['prompt'])}")

            # Spec with pass/fail indicators
            spec = t.get("spec", [])
            if spec:
                verifiable = sum(1 for i in spec if spec_is_verifiable(i))
                visual = len(spec) - verifiable
                label = f"{verifiable} verifiable"
                if visual:
                    label += f", {visual} visual"
                console.print(f"\n  [dim]Spec ({len(spec)} criteria \u2014 {label}):[/dim]")
                for i, item in enumerate(spec, 1):
                    text = spec_text(item)
                    tag = "[success]\\[v][/success]" if spec_is_verifiable(item) else "[info]\\[~][/info]"
                    console.print(f"    {i}. {tag} {rich_escape(text)}")

            # Diff summary (files changed)
            diff_lines = _get_diff_stat(task_id, project_dir)
            if diff_lines:
                console.print(f"\n  [dim]Files changed:[/dim]")
                # Show individual file lines (not the summary line)
                for dl in diff_lines:
                    if "|" in dl:
                        console.print(f"    [dim]{rich_escape(dl)}[/dim]")
                    elif "changed" in dl or "insertion" in dl or "deletion" in dl:
                        console.print(f"    [dim]{rich_escape(dl)}[/dim]")

            # QA report summary
            if log_dir.exists():
                events = _load_progress_events(log_dir)
                qa = _parse_qa_report(log_dir, events)
                if qa["exists"]:
                    if qa["total"] > 0:
                        qa_style = "success" if qa["passed"] == qa["total"] else "error"
                        console.print(f"\n  [dim]QA:[/dim] [{qa_style}]{qa['passed']}/{qa['total']} specs passed[/{qa_style}]")
                    for line in qa["summary_lines"]:
                        console.print(f"    [dim]{rich_escape(line)}[/dim]")

                # Verify summary
                verify_str = _get_verify_summary(log_dir)
                if verify_str:
                    console.print(f"\n  [dim]Verify:[/dim] {verify_str}")

                # Agent log highlights
                first_lines, last_lines = _get_agent_log_highlights(log_dir)
                if first_lines:
                    console.print(f"\n  [dim]Agent log (latest attempt):[/dim]")
                    for line in first_lines:
                        console.print(f"    [dim]{rich_escape(line[:90])}[/dim]")
                    if last_lines:
                        console.print(f"    [dim]...[/dim]")
                        for line in last_lines:
                            console.print(f"    [dim]{rich_escape(line[:90])}[/dim]")

            # Feedback
            if t.get("feedback"):
                console.print(f"\n  [dim]Feedback:[/dim] {rich_escape(t['feedback'])}")

            # Error (full context for failed tasks)
            if t.get("error"):
                console.print(f"\n  [error]Error:[/error] {rich_escape(t['error'])}")

            # Last error from verify/agent logs
            if task_status == "failed" and log_dir.exists():
                # Show last few lines from the most recent verify log
                verify_logs = sorted(log_dir.glob("attempt-*-verify.log"), reverse=True)
                if verify_logs:
                    try:
                        verify_content = verify_logs[0].read_text()
                        fail_lines = [
                            l for l in verify_content.splitlines()
                            if any(kw in l.upper() for kw in ["FAIL", "ERROR", "ASSERT"])
                        ]
                        if fail_lines:
                            console.print(f"\n  [dim]Last verify errors:[/dim]")
                            for fl in fail_lines[-5:]:
                                console.print(f"    [error]{rich_escape(fl[:100])}[/error]")
                    except OSError:
                        pass

            # Commit
            result = subprocess.run(
                ["git", "log", "--oneline", "--all", f"--grep=(#{task_id})"],
                capture_output=True, text=True, cwd=project_dir,
            )
            if result.stdout.strip():
                console.print(f"\n  [dim]Commit:[/dim] {rich_escape(result.stdout.strip().splitlines()[0])}")

            # Test file
            test_file = project_dir / "tests" / f"test_otto_{key}.py"
            if test_file.exists():
                console.print(f"  [dim]Test file:[/dim] {rich_escape(str(test_file.relative_to(project_dir)))}")

            # Logs dir
            if log_dir.exists():
                console.print(f"  [dim]Logs:[/dim]     {rich_escape(str(log_dir.relative_to(project_dir)))}/")

            console.print()
            return
    error_console.print(f"Task #{task_id} not found", style="error")
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

        msg = f"[success]✓[/success] Reset [bold]{count}[/bold] tasks. Cleaned branches, logs, and testgen."
        if hard:
            msg += " Reverted otto commits."
        console.print(msg)
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------

HISTORY_FILE = "otto_logs/run-history.jsonl"


@main.command(context_settings=CONTEXT_SETTINGS)
@click.option("-n", "--limit", "limit_", default=20, help="Number of runs to show")
def history(limit_):
    """Show past run history."""
    from rich.table import Table

    project_dir = Path.cwd()
    history_path = project_dir / HISTORY_FILE

    if not history_path.exists():
        console.print(f"[dim]No run history found. History is recorded after each 'otto run'.[/dim]")
        return

    entries = []
    try:
        for line in history_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        error_console.print(f"Error reading history file", style="error")
        sys.exit(1)

    if not entries:
        console.print(f"[dim]No run history found.[/dim]")
        return

    # Show most recent first
    entries.reverse()
    entries = entries[:limit_]

    table = Table(show_header=True, box=None, pad_edge=False, show_edge=False, expand=False)
    table.add_column("Date", width=20)
    table.add_column("Tasks", width=6, justify="right")
    table.add_column("Pass", width=5, justify="right")
    table.add_column("Fail", width=5, justify="right")
    table.add_column("Cost", width=8, justify="right", style="dim")
    table.add_column("Time", width=8, justify="right", style="dim")
    table.add_column("Detail", ratio=1, no_wrap=True)

    for entry in entries:
        ts = entry.get("timestamp", "?")
        # Format timestamp
        try:
            dt = datetime.fromisoformat(ts)
            ts_str = dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            ts_str = str(ts)[:16]

        total = entry.get("tasks_total", 0)
        passed = entry.get("tasks_passed", 0)
        failed = entry.get("tasks_failed", 0)
        cost = entry.get("cost_usd", 0.0)
        time_s = entry.get("time_s", 0.0)

        tasks_str = f"{passed + failed}/{total}" if total else "0"
        pass_style = "success" if passed > 0 else "dim"
        fail_style = "error" if failed > 0 else "dim"
        cost_str = _format_cost(cost) if cost > 0 else ""
        time_str = _format_duration(time_s) if time_s > 0 else ""

        # Failure detail
        fail_detail = ""
        if failed > 0 and entry.get("failure_summary"):
            fail_detail = f"[error]({rich_escape(entry['failure_summary'][:50])})[/error]"

        from rich.text import Text
        pass_text = Text(str(passed), style=pass_style)
        fail_text = Text(str(failed), style=fail_style)

        table.add_row(
            ts_str,
            tasks_str,
            pass_text,
            fail_text,
            cost_str,
            time_str,
            fail_detail,
        )

    console.print()
    console.print(table)
    console.print()


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
        error_console.print("Error: bench/ directory not found.", style="error")
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
        error_console.print(f"Unknown runner: {name}. Available: {', '.join(runner_files)}", style="error")
        sys.exit(2)

    runner_path = bench_dir / "runners" / runner_files[name]
    if not runner_path.exists():
        error_console.print(f"Runner file not found: {rich_escape(str(runner_path))}", style="error")
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
        error_console.print(f"Error: {rich_escape(str(suite_path))} not found.", style="error")
        sys.exit(2)

    tasks = load_suite(suite_path)
    if not tasks:
        error_console.print("No tasks found in suite.", style="error")
        sys.exit(2)

    # Apply filters
    tasks = filter_tasks(
        tasks,
        difficulty=difficulty,
        names=list(task_names) if task_names else None,
    )
    if not tasks:
        error_console.print("No tasks match the given filters.", style="error")
        sys.exit(2)

    runner = _get_runner(runner_name)
    console.print(f"[bold]Otto Bench[/bold] \u2014 {len(tasks)} tasks, runner: [info]{rich_escape(runner_name)}[/info]")
    if label:
        console.print(f"  Label: {rich_escape(label)}")
    console.print()

    run = asyncio.run(run_bench(bench_dir, tasks, runner, label=label))

    # Save results
    results_dir = bench_dir / "results"
    out_path = save_results(run, results_dir)

    # Print summary
    s = run.summary
    rule = "\u2501" * 50
    console.print(f"\n{rule}")
    console.print(f"[bold]Results:[/bold] {rich_escape(out_path.name)}")
    console.print(f"  Success: [success]{s['passed']}[/success]/{s['total']}  ({s['success_rate'] * 100:.1f}%)")
    console.print(f"  Cost:    ${s['total_cost']:.2f}  (${s['cost_per_success']:.2f}/success)")
    console.print(f"  Time:    {s['total_time_s']:.0f}s  ({s['time_per_success_s']:.0f}s/success)")
    if s["mean_mutation_score"] > 0:
        console.print(f"  Mutation: {s['mean_mutation_score']:.3f}")


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
        error_console.print(f"Result not found: {rich_escape(query)}", style="error")
        sys.exit(2)

    baseline_path = _find_result(baseline)
    current_path = _find_result(current)

    baseline_run = load_results(baseline_path)
    current_run = load_results(current_path)

    console.print(compare_runs(baseline_run, current_run))


@bench.command("history", context_settings=CONTEXT_SETTINGS)
@click.option("--limit", "-n", default=20, help="Number of runs to show")
def bench_history(limit):
    """Show recent benchmark run history."""
    from rich.table import Table
    from otto.bench import list_results

    bench_dir = _get_bench_dir()
    results_dir = bench_dir / "results"

    runs = list_results(results_dir)
    if not runs:
        console.print(f"[dim]No benchmark results found.[/dim]")
        return

    table = Table(show_header=True, box=None, pad_edge=False, show_edge=False, expand=False)
    table.add_column("Run ID", width=24)
    table.add_column("Runner", width=10)
    table.add_column("Label", width=20)
    table.add_column("Pass", width=6, justify="right")
    table.add_column("Cost", width=8, justify="right", style="dim")
    table.add_column("Time", width=8, justify="right", style="dim")

    for name, run in runs[:limit]:
        s = run.summary
        sr = s["success_rate"] * 100
        table.add_row(
            run.run_id,
            run.runner,
            run.label,
            f"{s['passed']}/{s['total']} {sr:.0f}%",
            f"${s['total_cost']:.2f}",
            f"{s['total_time_s']:.0f}s",
        )

    console.print(table)
