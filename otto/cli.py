"""Otto CLI — entrypoint for all otto commands."""

import asyncio
import json
import os
import subprocess
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
    mutate_and_recompute,
    refresh_planner_state,
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
        cost_available = bool((result or {}).get("cost_available", True))
        cost_text = format_cost(cost) if cost_available else "cost unavailable"
        if result and result.get("success"):
            console.print(
                f"    {time.strftime('%H:%M:%S')}  [green]✓[/green] passed  "
                f"[dim]{elapsed_str}  {cost_text}[/dim]"
            )
        else:
            console.print(
                f"    {time.strftime('%H:%M:%S')}  [red]✗[/red] failed  "
                f"[dim]{elapsed_str}  {cost_text}[/dim]"
            )






@click.group(context_settings=CONTEXT_SETTINGS)
def main():
    """Otto — build and certify software products.

    Run 'otto COMMAND -h' for command-specific options.
    """
    # Fail early if otto is loaded from a different source than expected.
    # This catches the shared-venv bug where worktree otto runs main repo code.
    import otto as _otto_pkg
    _otto_src = str(Path(_otto_pkg.__file__).resolve().parent)
    _cwd = str(Path.cwd().resolve())
    if "worktree" in _cwd and "worktree" not in _otto_src:
        click.echo(
            f"ERROR: otto loaded from {_otto_src}\n"
            f"  but cwd is a worktree ({_cwd}).\n"
            f"  Use the worktree's own venv: .venv/bin/otto",
            err=True,
        )
        sys.exit(1)



def _import_tasks(import_path: Path, tasks_path: Path, config: dict | None = None) -> None:
    """Import tasks from .md, .txt, or .yaml files with spec generation.

    Replaces any existing tasks — the import file is the source of truth.
    """
    import yaml as _yaml

    project_dir = Path.cwd()
    suffix = import_path.suffix.lower()
    batch = []

    if suffix == ".md":
        console.print(f"Parsing {rich_escape(import_path.name)} (this may take 10-20s)...")
        parsed = parse_markdown_tasks(import_path, project_dir, config=config)
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
            spec_items = filter_generated_spec_items(generate_spec(line, project_dir, config=config))
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
                spec_items = filter_generated_spec_items(generate_spec(t["prompt"], project_dir, config=config))
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


def _collect_failed_tasks(tasks: list[dict]) -> list[dict]:
    """Return terminal task failures that should block product QA."""
    failed_statuses = {"failed", "merge_failed", "blocked", "conflict"}
    return [task for task in tasks if task.get("status") in failed_statuses]


def _print_failed_tasks(tasks: list[dict]) -> None:
    """Show which tasks blocked the build before product QA."""
    if not tasks:
        return

    console.print("  [red]Skipping product QA because some tasks failed[/red]")
    for task in tasks:
        status = rich_escape(str(task.get("status", "failed")))
        prompt = rich_escape(str(task.get("prompt", ""))[:80])
        console.print(f"    [red]✗[/red] #{task.get('id', '?')} [{status}] {prompt}")
        if task.get("error"):
            console.print(f"      {rich_escape(str(task['error'])[:100])}")


def _print_build_result(intent: str, result, build_duration: float) -> None:
    """Render build verification output and summary."""
    if result.journeys:
        console.print()
        if result.passed:
            console.print(f"  [success]All journeys passed[/success]"
                          f" (round {result.rounds})")
        else:
            console.print(f"  [red]Some journeys failed[/red]"
                          f" (after {result.rounds} round(s))")
        for j in result.journeys:
            status_icon = "[success]✓[/success]" if j.get("passed") else "[red]✗[/red]"
            console.print(f"    {status_icon} {rich_escape(j.get('name', ''))}")

    if result.break_findings:
        console.print()
        high_count = sum(1 for b in result.break_findings if b.get("severity") in ("critical", "important"))
        warn_count = len(result.break_findings) - high_count
        if high_count:
            console.print(f"  [red bold]⚠ {high_count} quality issue(s) found (will trigger fix)[/red bold]")
        if warn_count:
            console.print(f"  [yellow]⚠ {warn_count} quality warning(s)[/yellow]")
        for b in result.break_findings:
            sev = b.get("severity", "?")
            desc = rich_escape(b.get("description", "")[:100])
            if sev in ("critical", "important"):
                console.print(f"    [red]✗ [{sev}] {desc}[/red]")
            else:
                console.print(f"    [yellow]! [{sev}] {desc}[/yellow]")
            fix = b.get("fix_suggestion", "")
            if fix:
                console.print(f"      fix: {rich_escape(fix[:120])}")

    console.print()
    console.print(f"  [bold]Build Summary[/bold]  ({result.build_id})")
    console.print(f"  Intent: {rich_escape(intent[:80])}")
    if result.journeys:
        console.print(f"  Stories: {result.tasks_passed} passed, {result.tasks_failed} failed")
    else:
        console.print(f"  Tasks: {result.tasks_passed} passed, {result.tasks_failed} failed")
    console.print(f"  [bold]Total cost: ${result.total_cost:.2f}[/bold]")
    console.print(f"  Duration: {build_duration / 60:.1f} min")
    if result.error:
        console.print(f"  [red]Error: {rich_escape(result.error[:100])}[/red]")
    console.print()


@main.command(context_settings=CONTEXT_SETTINGS, hidden=True)
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
    else:
        config = load_config(config_path)

    tasks_path = project_dir / "tasks.yaml"

    if import_file:
        _import_tasks(Path(import_file), tasks_path, config=config)
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
            spec_items = generate_spec(prompt, Path.cwd(), config=config)
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


@main.command(context_settings=CONTEXT_SETTINGS, hidden=True)
@click.argument("prompt", required=False)
@click.option("--dry-run", is_flag=True, help="Show what would run without executing")
@click.option("--no-spec", is_flag=True, help="Skip spec generation")
@click.option("--no-qa", is_flag=True, help="Skip QA (merge after tests pass)")
@click.option("--no-test", is_flag=True, help="Skip testing (merge after coding)")
def run(prompt, dry_run, no_spec, no_qa, no_test):
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

    # CLI flags override config
    if no_spec:
        config["skip_spec"] = True
    if no_qa:
        config["skip_qa"] = True
    if no_test:
        config["skip_test"] = True

    if dry_run:
        tasks_path = project_dir / "tasks.yaml"
        tasks = load_tasks(tasks_path)
        pending = [t for t in tasks if t.get("status") == "pending"]
        if not pending:
            console.print("Pending tasks: 0")
            return

        # Run the planner to show actual execution plan
        from otto.planner import plan as smart_plan, serial_plan
        try:
            execution_plan = asyncio.run(smart_plan(pending, config, project_dir))
        except Exception:
            execution_plan = serial_plan(pending)

        console.print(f"\n  [bold]Execution Plan[/bold]  ({len(pending)} tasks)\n")
        for batch_idx, batch in enumerate(execution_plan.batches):
            task_keys = [tp.task_key for tp in batch.tasks]
            mode = "parallel" if len(task_keys) > 1 else "serial"
            console.print(f"  Batch {batch_idx + 1}  {len(task_keys)} task(s) ({mode})")
            for tp in batch.tasks:
                task = next((t for t in pending if t.get("key") == tp.task_key), None)
                prompt_text = (task.get("prompt", "") if task else tp.task_key)[:60]
                console.print(f"    {rich_escape(tp.task_key[:8])}  {rich_escape(prompt_text)}")
        console.print(f"\n  Run [bold]otto run[/bold] to execute.\n")
        return

    if prompt:
        # One-off mode — create temp tasks file and route through run_per
        import os
        import tempfile
        import time
        import yaml

        key = f"adhoc-{int(time.time())}-{os.getpid()}"
        # Use a temp file to avoid overwriting real tasks.yaml
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".yaml", prefix="otto-adhoc-", dir=str(project_dir))
        tasks_path = Path(tmp_path)
        try:
            os.close(tmp_fd)
            tasks_path.write_text(yaml.dump({"tasks": [
                {"id": 0, "key": key, "prompt": prompt, "status": "pending"},
            ]}))
            from otto.orchestrator import run_per
            exit_code = asyncio.run(run_per(config, tasks_path, project_dir))
            sys.exit(exit_code)
        finally:
            tasks_path.unlink(missing_ok=True)
    else:
        tasks_path = project_dir / "tasks.yaml"
        from otto.orchestrator import run_per
        exit_code = asyncio.run(run_per(config, tasks_path, project_dir))
        sys.exit(exit_code)




@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("intent")
@click.option("--no-qa", is_flag=True, help="Skip product certification after build")
@click.option("--orchestrated", is_flag=True, hidden=True, help="Legacy: orchestrator-driven build (PER mode)")
@click.option("--split", "use_split", is_flag=True, hidden=True, help="Legacy: split-session build")
def build(intent, no_qa, orchestrated, use_split):
    """Build a product from a natural language intent.

    One agent builds, certifies, and fixes autonomously. The certifier
    verifies the product works by running real user stories (HTTP, CLI,
    import, WebSocket — any product type).

    Examples:

        otto build "bookmark manager with tags and search"

        otto build "CLI tool that converts CSV to JSON" --no-qa
    """
    require_git()
    project_dir = Path.cwd()
    config_path = project_dir / "otto.yaml"
    if not config_path.exists():
        create_config(project_dir)
        console.print(f"[yellow]First run — created otto.yaml[/yellow]")
        console.print()
    config = load_config(config_path)

    if no_qa:
        config["skip_product_qa"] = True

    # Run the pipeline
    from otto.pipeline import build_product, build_agentic_v2, build_agentic_v3, BuildResult

    build_start = time.time()
    console.print()

    try:
        if orchestrated:
            result: BuildResult = asyncio.run(build_product(intent, project_dir, config))
        elif use_split:
            console.print("  [bold]Split mode[/bold] — separate build and certify sessions\n")
            result: BuildResult = asyncio.run(
                build_agentic_v2(intent, project_dir, config)
            )
        else:
            # Default: fully agentic — one agent drives everything
            console.print("  [bold]Agentic mode[/bold] — one agent builds, certifies, fixes\n")
            result: BuildResult = asyncio.run(
                build_agentic_v3(intent, project_dir, config)
            )
    except KeyboardInterrupt:
        console.print("\n  Aborted.")
        sys.exit(1)
    except Exception as e:
        error_console.print(f"[error]Build failed: {rich_escape(str(e))}[/error]")
        sys.exit(1)

    build_duration = time.time() - build_start
    _print_build_result(intent, result, build_duration)

    sys.exit(0 if result.passed else 1)


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("intent", required=False)
def certify(intent):
    """Certify a product — independent, builder-blind verification.

    Tests the product in the current directory as a real user. Works on
    any project regardless of how it was built (otto, bare CC, human).

    If no intent is given, reads intent.md or README.md from the project.

    Examples:
        otto certify "notes API with auth, CRUD, and search"
        otto certify                   # reads intent.md
        cd my-project && otto certify  # reads README.md
    """
    project_dir = Path.cwd()

    # Resolve intent: argument > intent.md > README.md
    if not intent:
        intent_path = project_dir / "intent.md"
        readme_path = project_dir / "README.md"
        if intent_path.exists():
            intent = intent_path.read_text().strip()
            console.print(f"  [dim]Intent from intent.md[/dim]")
        elif readme_path.exists():
            intent = readme_path.read_text().strip()[:2000]
            console.print(f"  [dim]Intent from README.md[/dim]")
        else:
            error_console.print("[error]No intent provided. Pass as argument or create intent.md[/error]")
            sys.exit(2)

    if not intent:
        error_console.print("[error]Intent is empty[/error]")
        sys.exit(2)

    console.print(f"\n  [bold]Certifying[/bold] — independent product verification\n")

    from otto.certifier import run_agentic_certifier

    start = time.time()
    try:
        report = asyncio.run(run_agentic_certifier(
            intent=intent,
            project_dir=project_dir,
        ))
    except KeyboardInterrupt:
        console.print("\n  Aborted.")
        sys.exit(1)
    except Exception as e:
        error_console.print(f"[error]Certification failed: {rich_escape(str(e))}[/error]")
        sys.exit(1)

    duration = time.time() - start
    story_results = getattr(report, "_story_results", [])
    passed_count = sum(1 for s in story_results if s.get("passed"))

    # Display results
    if story_results:
        for s in story_results:
            icon = "[success]✓[/success]" if s.get("passed") else "[red]✗[/red]"
            console.print(f"    {icon} {rich_escape(s.get('summary', s.get('story_id', '')))}")

    console.print()
    outcome = report.outcome.value
    if outcome == "passed":
        console.print(f"  [success bold]PASSED[/success bold] — {passed_count}/{len(story_results)} stories")
    else:
        console.print(f"  [red bold]FAILED[/red bold] — {passed_count}/{len(story_results)} stories")

    console.print(f"  Cost: ${report.cost_usd:.2f}  Duration: {duration:.0f}s")

    # PoW report location
    pow_dir = project_dir / "otto_logs" / "certifier"
    if (pow_dir / "proof-of-work.html").exists():
        console.print(f"  Report: {pow_dir / 'proof-of-work.html'}")

    console.print()
    sys.exit(0 if outcome == "passed" else 1)


@main.command(context_settings=CONTEXT_SETTINGS, hidden=True)
@click.option("-w", "--watch", is_flag=True, help="Auto-refresh every 2 seconds")
def status(watch):
    """Show task status."""
    import fcntl

    tasks_path = Path.cwd() / "tasks.yaml"
    tasks = refresh_planner_state(tasks_path)
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


@main.command(context_settings=CONTEXT_SETTINGS, hidden=True)
@click.argument("task_id", type=int)
@click.argument("feedback", required=False)
@click.option("--force", is_flag=True, help="Reset any task, not just failed ones")
def retry(task_id, feedback, force):
    """Reset a failed task to pending (use --force for any status).

    Optionally provide feedback to guide the agent on what to fix:
      otto retry --force 2 "Output format is ugly, use a table"
    """
    tasks_path = Path.cwd() / "tasks.yaml"
    tasks = refresh_planner_state(tasks_path)
    for t in tasks:
        if t.get("id") == task_id:
            if not force and t.get("status") not in ("failed", "merge_failed", "conflict", "blocked"):
                error_console.print(
                    f"Task #{task_id} is '{t.get('status')}', not retryable by default. Use --force to override.", style="error"
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

            def _reset(tasks):
                for task in tasks:
                    if task.get("key") != t["key"]:
                        continue
                    task["status"] = "pending"
                    task["attempts"] = 0
                    task.pop("session_id", None)
                    task.pop("error", None)
                    task.pop("error_code", None)
                    task.pop("completed_at", None)
                    task.pop("planner_conflicts", None)
                    task.pop("blocked_by", None)
                    task.pop("blocked_reason", None)
                    if feedback:
                        task["feedback"] = feedback
                    break

            mutate_and_recompute(tasks_path, _reset)
            console.print(f"[success]✓[/success] Reset task [bold]#{task_id}[/bold] to pending")
            if feedback:
                console.print(f"  [dim]Feedback: {rich_escape(feedback)}[/dim]")
            return
    error_console.print(f"Task #{task_id} not found", style="error")
    sys.exit(1)


@main.command(context_settings=CONTEXT_SETTINGS, hidden=True)
@click.argument("task_id", type=int, required=False)
@click.option("--all", "drop_all", is_flag=True, help="Remove all tasks and clean otto/* branches")
@click.option("--yes", is_flag=True, help="Skip confirmation")
def drop(task_id, drop_all, yes):
    """Remove task(s) from the queue (does NOT revert code).

    Drop a single task:
      otto drop 3

    Drop all tasks and clean branches:
      otto drop --all

    To undo code changes, use 'otto revert'.
    """
    if drop_all:
        _drop_all(yes)
        return

    if task_id is None:
        error_console.print("Provide a task ID or use --all", style="error")
        sys.exit(2)

    tasks_path = Path.cwd() / "tasks.yaml"
    tasks = refresh_planner_state(tasks_path)
    target = None
    for t in tasks:
        if t.get("id") == task_id:
            target = t
            break
    if not target:
        error_console.print(f"Task #{task_id} not found", style="error")
        sys.exit(1)

    task_status = target.get("status", "pending")
    if task_status in ("running", "merge_pending"):
        error_console.print(f"[error]\u2717[/error] Cannot drop a {task_status} task. Wait for it to finish or retry.")
        sys.exit(1)
    if task_status == "passed":
        console.print(f"[warning]\u26a0[/warning] This only removes task [bold]#{task_id}[/bold] from the queue. "
                       f"The code it committed stays on main.")
        console.print(f"  [dim]Use 'otto revert {task_id}' to undo the code.[/dim]")
        if not yes:
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
        console.print(f"[warning]\u26a0[/warning] Pending tasks depend on this one: {dep_ids}")
        if not yes:
            click.confirm("  Continue?", abort=True)

    delete_task(tasks_path, task_id)
    console.print(f"[success]\u2713[/success] Dropped task [bold]#{task_id}[/bold]: {rich_escape(target['prompt'][:60])}")


def _drop_all(yes: bool) -> None:
    """Drop all tasks and clean otto/* branches (no code revert)."""
    import fcntl
    import subprocess

    project_dir = Path.cwd()

    # Acquire process lock — refuse while a worker is active
    lock_path = git_meta_dir(project_dir) / "otto.lock"
    lock_path.touch()
    lock_fh = open(lock_path, "r")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        error_console.print("Cannot drop while otto is running", style="error")
        sys.exit(2)

    try:
        tasks_path = project_dir / "tasks.yaml"
        count = 0
        if tasks_path.exists():
            count = len(load_tasks(tasks_path))

        # Count otto/* branches
        branch_result = subprocess.run(
            ["git", "branch", "--list", "otto/*"],
            capture_output=True, text=True, cwd=project_dir,
        )
        branches = [b.strip() for b in branch_result.stdout.strip().splitlines() if b.strip()]

        if not yes:
            console.print(f"[warning]\u26a0[/warning] Dropping ALL {count} tasks from the queue.")
            console.print(f"  Code on main: [bold]NOT affected[/bold]")
            console.print(f"  Otto logs: [bold]preserved[/bold] in otto_logs/")
            if branches:
                console.print(f"  Git branches: {len(branches)} otto/* will be deleted")
            click.confirm("  Continue?", abort=True)

        # Delete tasks.yaml + .tasks.lock
        if tasks_path.exists():
            tasks_path.unlink()
        tasks_lock = tasks_path.parent / ".tasks.lock"
        if tasks_lock.exists():
            tasks_lock.unlink()

        # Delete otto/* branches
        for branch in branches:
            subprocess.run(["git", "branch", "-D", branch], capture_output=True, cwd=project_dir)

        console.print(f"[success]\u2713[/success] Dropped [bold]{count}[/bold] tasks. Cleaned {len(branches)} branches.")
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()



# Setup command (registered from otto/cli_setup.py)
from otto.cli_setup import register_setup_command
register_setup_command(main)

# Log/show/diff commands (registered from otto/cli_logs.py)
from otto.cli_logs import register_log_commands
register_log_commands(main)


@main.command(context_settings=CONTEXT_SETTINGS, hidden=True)
@click.argument("task_id", type=int, required=False)
@click.option("--all", "revert_all", is_flag=True, help="Revert ALL otto commits")
@click.option("--yes", is_flag=True, help="Skip confirmation")
def revert(task_id, revert_all, yes):
    """Undo otto's git commits (destructive).

    Revert one task's commit:
      otto revert 3

    Revert all otto commits:
      otto revert --all
    """
    if revert_all:
        _revert_all(yes)
        return

    if task_id is None:
        error_console.print("Provide a task ID or use --all", style="error")
        sys.exit(2)

    _revert_one(task_id, yes)


def _revert_one(task_id: int, yes: bool) -> None:
    """Revert the git commit for a single task and remove it from the queue."""
    import subprocess

    project_dir = Path.cwd()
    tasks_path = project_dir / "tasks.yaml"
    tasks = load_tasks(tasks_path)

    target = None
    for t in tasks:
        if t.get("id") == task_id:
            target = t
            break
    if not target:
        error_console.print(f"Task #{task_id} not found", style="error")
        sys.exit(1)

    if target.get("status") in ("running", "merge_pending"):
        error_console.print(f"[error]\u2717[/error] Cannot revert a {target.get('status')} task.", style="error")
        sys.exit(1)

    # Find the commit for this task
    result = subprocess.run(
        ["git", "log", "--oneline", "--all", f"--grep=(#{task_id})"],
        capture_output=True, text=True, cwd=project_dir,
    )
    commits = [line for line in result.stdout.strip().splitlines() if line]

    if not commits:
        error_console.print(f"No git commit found for task #{task_id}", style="error")
        error_console.print(f"  [dim]Use 'otto drop {task_id}' to just remove from queue.[/dim]")
        sys.exit(1)

    commit_hash = commits[0].split()[0]
    commit_msg = " ".join(commits[0].split()[1:])

    if not yes:
        console.print(f"[warning]\u26a0[/warning] Reverting task [bold]#{task_id}[/bold]: {rich_escape(target['prompt'][:60])}")
        console.print(f"  This will undo commit {commit_hash} on main.")
        console.print(f"  The task will also be removed from the queue.")
        click.confirm("  Continue?", abort=True)

    # git revert --no-edit
    revert_result = subprocess.run(
        ["git", "revert", "--no-edit", commit_hash],
        capture_output=True, text=True, cwd=project_dir,
    )
    if revert_result.returncode != 0:
        error_console.print(f"[error]\u2717[/error] git revert failed:")
        error_console.print(f"  {revert_result.stderr.strip()}")
        error_console.print(f"  [dim]Resolve manually: git revert {commit_hash}[/dim]")
        sys.exit(1)

    # Remove from tasks.yaml
    delete_task(tasks_path, task_id)

    console.print(f"[success]\u2713[/success] Reverted commit {commit_hash} and dropped task [bold]#{task_id}[/bold]")


def _revert_all(yes: bool) -> None:
    """Revert all otto commits, clear tasks and branches."""
    import fcntl
    import subprocess

    project_dir = Path.cwd()

    # Acquire process lock
    lock_path = git_meta_dir(project_dir) / "otto.lock"
    lock_path.touch()
    lock_fh = open(lock_path, "r")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        error_console.print("Cannot revert while otto is running", style="error")
        sys.exit(2)

    try:
        # Find all otto commits
        result = subprocess.run(
            ["git", "log", "--oneline", "--all", "--grep=otto:"],
            capture_output=True, text=True, cwd=project_dir,
        )
        otto_commits = [line.split()[0] for line in result.stdout.strip().splitlines() if line]

        tasks_path = project_dir / "tasks.yaml"
        count = 0
        if tasks_path.exists():
            count = len(load_tasks(tasks_path))

        if not yes:
            console.print(f"[warning]\u26a0[/warning] Reverting ALL {len(otto_commits)} otto commits and restoring the codebase.")
            console.print(f"  Tasks: {count} will be removed from queue")
            console.print(f"  Otto logs: [bold]preserved[/bold] in otto_logs/")
            console.print(f"  [bold]This cannot be undone.[/bold]")
            click.confirm("  Continue?", abort=True)

        # Hard reset: reset to before the first otto commit
        if otto_commits:
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
                error_console.print(f"[warning]\u26a0[/warning] Could not find parent of oldest otto commit")

        # Delete tasks.yaml + .tasks.lock
        if tasks_path.exists():
            tasks_path.unlink()
        tasks_lock = tasks_path.parent / ".tasks.lock"
        if tasks_lock.exists():
            tasks_lock.unlink()

        # Delete otto/* branches
        branch_result = subprocess.run(
            ["git", "branch", "--list", "otto/*"],
            capture_output=True, text=True, cwd=project_dir,
        )
        for branch in branch_result.stdout.strip().split("\n"):
            branch = branch.strip()
            if branch:
                subprocess.run(["git", "branch", "-D", branch], capture_output=True, cwd=project_dir)

        console.print(f"[success]\u2713[/success] Reverted {len(otto_commits)} commits. "
                       f"Dropped {count} tasks. Cleaned branches.")
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()



# Bench subcommands (registered from otto/cli_bench.py)
from otto.cli_bench import register_bench_commands
register_bench_commands(main)
