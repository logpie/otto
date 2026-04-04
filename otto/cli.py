"""Otto CLI — entrypoint for all otto commands."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
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
    """Otto — autonomous coding agent runner.

    Run 'otto COMMAND -h' for command-specific options.
    """
    pass



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


@main.command(context_settings=CONTEXT_SETTINGS)
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
@click.option("--no-review", is_flag=True, help="Skip plan review, execute immediately")
@click.option("--no-qa", is_flag=True, help="Skip product-level QA after execution")
def build(intent, no_review, no_qa):
    """Build a product from a natural language intent.

    Decomposes intent into tasks, executes them, and verifies the product.

    Examples:
        otto build "bookmark manager with tags and search"
        otto build "CLI tool that converts CSV to JSON"
        otto build "weather app like Apple's" --no-review
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

    # Generate build ID and initialize telemetry
    build_id = f"build-{int(time.time())}-{os.getpid()}"
    build_start = time.time()
    from otto.telemetry import (
        Telemetry, BuildStarted, ProductPlanCompleted, BuildCompleted,
    )
    telemetry = Telemetry(project_dir / "otto_logs")
    telemetry.log(BuildStarted(build_id=build_id, intent=intent))

    # Track costs across all phases
    cost_planning = 0.0
    cost_tasks = 0.0
    cost_qa = 0.0
    cost_context = 0.0

    # Step 1: Product planning
    from otto.product_planner import run_product_planner, ProductPlan
    import threading

    console.print()
    _plan_start = time.time()
    _plan_done = False

    def _update_timer(status_widget):
        while not _plan_done:
            elapsed = int(time.time() - _plan_start)
            status_widget.update(f"[dim]Planning... {elapsed}s[/dim]")
            time.sleep(1)

    plan: ProductPlan | None = None
    try:
        with console.status("[dim]Planning...[/dim]", spinner="dots") as status_widget:
            timer_thread = threading.Thread(target=_update_timer, args=(status_widget,), daemon=True)
            timer_thread.start()
            plan = asyncio.run(run_product_planner(intent, project_dir, config))
            _plan_done = True
            cost_planning = plan.cost_usd
            telemetry.log(ProductPlanCompleted(
                build_id=build_id, mode=plan.mode,
                num_tasks=len(plan.tasks), cost_usd=plan.cost_usd,
                duration_s=plan.duration_s,
            ))
    except Exception as e:
        _plan_done = True
        error_console.print(f"[error]Planning failed: {rich_escape(str(e))}[/error]")
        sys.exit(1)

    # Step 2: Show plan and get approval
    console.print()
    if plan.mode == "single_task":
        console.print(f"  [bold]Single task[/bold] (no decomposition needed)")
        console.print(f"  {rich_escape(plan.tasks[0].prompt[:100])}")
    else:
        console.print(f"  [bold]Product Plan[/bold]  ({len(plan.tasks)} tasks)")
        if plan.product_spec_path:
            console.print(f"  [dim]Product spec: {plan.product_spec_path.name}[/dim]")
        if plan.architecture_path:
            console.print(f"  [dim]Architecture: {plan.architecture_path.name}[/dim]")
        console.print()
        for idx, task in enumerate(plan.tasks):
            deps = ""
            if task.depends_on:
                deps = f" [dim](depends: {', '.join(f'#{d+1}' for d in task.depends_on)})[/dim]"
            console.print(f"  #{idx+1}  {rich_escape(task.prompt[:80])}{deps}")

    if plan.assumptions:
        console.print()
        console.print(f"  [yellow]Assumptions:[/yellow]")
        for a in plan.assumptions:
            console.print(f"    - {rich_escape(a)}")

    console.print()
    console.print(f"  [dim]Planning: ${plan.cost_usd:.2f}, {plan.duration_s:.0f}s[/dim]")
    console.print()

    if not no_review:
        try:
            choice = click.prompt(
                "  [enter] accept  [e] edit  [q] quit",
                default="", show_default=False, prompt_suffix=" "
            )
            if choice.lower() == "q":
                console.print("  Aborted.")
                return
            if choice.lower() == "e":
                # Open product-spec.md for editing if it exists
                if plan.product_spec_path and plan.product_spec_path.exists():
                    click.edit(filename=str(plan.product_spec_path))
                    console.print(f"  [dim]Edited {plan.product_spec_path.name}. Continuing with current tasks.[/dim]")
                else:
                    console.print(f"  [dim]No product-spec.md to edit. Continuing.[/dim]")
        except (KeyboardInterrupt, EOFError):
            console.print("\n  Aborted.")
            return

    # Step 3: Add tasks to tasks.yaml
    tasks_path = project_dir / "tasks.yaml"
    task_batch = [
        {
            "prompt": t.prompt,
            "depends_on": t.depends_on if t.depends_on else None,
        }
        for t in plan.tasks
    ]
    created = add_tasks(tasks_path, task_batch)
    build_task_keys = {t["key"] for t in created}
    console.print(f"  [success]Added {len(created)} task(s) to tasks.yaml[/success]")

    # Step 3.5: Commit plan artifacts so worktrees can see them
    plan_files = [str(tasks_path)]
    if plan.product_spec_path and plan.product_spec_path.exists():
        plan_files.append(str(plan.product_spec_path))
    if plan.architecture_path and plan.architecture_path.exists():
        plan_files.append(str(plan.architecture_path))
    subprocess.run(["git", "add"] + plan_files, cwd=project_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "otto: plan artifacts"],
        cwd=project_dir, capture_output=True,
    )

    # Step 4: Execute via inner loop
    console.print()
    from otto.orchestrator import run_per
    exit_code = asyncio.run(run_per(config, tasks_path, project_dir))

    # Step 5: Product verification — always run after successful build
    outer_result = None
    if not config.get("skip_product_qa"):
        if exit_code == 0 and plan.product_spec_path and plan.product_spec_path.exists():
            console.print()
            console.print("  [bold]Product Verification[/bold] — user journey testing...")
            try:
                # PoW on by default for outer loop — fix tasks need auditable proofs
                config.setdefault("proof_of_work", True)
                from otto.outer_loop import run_product_verification
                outer_result = asyncio.run(run_product_verification(
                    product_spec_path=plan.product_spec_path,
                    project_dir=project_dir,
                    tasks_path=tasks_path,
                    config=config,
                    intent=intent,
                ))

                if outer_result.get("product_passed"):
                    console.print(f"  [success]All journeys passed[/success]"
                                  f" (round {outer_result.get('rounds', 1)})")
                else:
                    console.print(f"  [red]Some journeys failed[/red]"
                                  f" (after {outer_result.get('rounds', 1)} round(s))")
                    for j in outer_result.get("journeys", []):
                        status_icon = "[success]✓[/success]" if j.get("passed") else "[red]✗[/red]"
                        console.print(f"    {status_icon} {rich_escape(j.get('name', ''))}")
                        if not j.get("passed") and j.get("error"):
                            console.print(f"      {rich_escape(j['error'][:100])}")

                    if outer_result.get("inner_loop_failed"):
                        _print_failed_tasks(outer_result.get("failed_tasks", []))

                if outer_result.get("fix_tasks_created", 0) > 0:
                    console.print(f"  [dim]{outer_result['fix_tasks_created']} fix task(s) created[/dim]")
                console.print(f"  [dim]Product QA cost: ${outer_result.get('total_cost', 0):.2f}[/dim]")

            except Exception as e:
                console.print(f"  [yellow]Product QA failed: {rich_escape(str(e))}[/yellow]")
                exit_code = 1
        elif exit_code != 0:
            _print_failed_tasks(_collect_failed_tasks(load_tasks(tasks_path)))

    if outer_result is not None:
        exit_code = 0 if outer_result.get("product_passed") else 1
        cost_qa = outer_result.get("total_cost", 0.0)

    # Compute costs scoped to THIS build's tasks only
    all_tasks = load_tasks(tasks_path) if tasks_path.exists() else []
    build_tasks = [t for t in all_tasks if t.get("key") in build_task_keys]
    cost_tasks = sum(t.get("cost_usd", 0) for t in build_tasks)

    # Read context update cost from log
    context_log = project_dir / "otto_logs" / "product-context.log"
    if context_log.exists():
        import re as _re
        for line in context_log.read_text().splitlines():
            m = _re.search(r"\$(\d+\.\d+)", line)
            if m:
                cost_context += float(m.group(1))

    cost_total = cost_planning + cost_tasks + cost_qa + cost_context
    build_duration = time.time() - build_start

    # Emit build completed event with full cost breakdown
    FAILED_STATUSES = {"failed", "merge_failed", "blocked", "conflict"}
    product_passed = outer_result.get("product_passed", True) if outer_result else (exit_code == 0)
    tasks_passed = sum(1 for t in build_tasks if t.get("status") == "passed")
    tasks_failed = sum(1 for t in build_tasks if t.get("status") in FAILED_STATUSES)

    telemetry.log(BuildCompleted(
        build_id=build_id, intent=intent,
        product_passed=product_passed,
        total_tasks=len(plan.tasks), tasks_passed=tasks_passed, tasks_failed=tasks_failed,
        cost_planning=round(cost_planning, 4),
        cost_tasks=round(cost_tasks, 4),
        cost_qa=round(cost_qa, 4),
        cost_context=round(cost_context, 4),
        cost_total=round(cost_total, 4),
        duration_s=round(build_duration, 1),
    ))

    # Print build summary
    console.print()
    console.print(f"  [bold]Build Summary[/bold]  ({build_id})")
    console.print(f"  Intent: {rich_escape(intent[:80])}")
    console.print(f"  Tasks: {tasks_passed} passed, {tasks_failed} failed")
    console.print(f"  Cost breakdown:")
    console.print(f"    Planning:  ${cost_planning:.2f}")
    console.print(f"    Tasks:     ${cost_tasks:.2f}")
    if cost_qa > 0:
        console.print(f"    Product QA: ${cost_qa:.2f}")
    if cost_context > 0:
        console.print(f"    Context:   ${cost_context:.2f}")
    console.print(f"    [bold]Total:     ${cost_total:.2f}[/bold]")
    console.print(f"  Duration: {build_duration / 60:.1f} min")
    console.print()

    sys.exit(exit_code)


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("project_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.argument("intent")
@click.option("--port", "port_override", type=int, default=None, help="Connect to an already-running app on this port")
@click.option("--output", default="-", help="Path to write the JSON report ('-' for stdout)")
@click.option("--tier", type=click.IntRange(0, 2), default=1, show_default=True,
              help="Certification tier: 0=adapter only, 1=baseline, 2=full (baseline + journeys + PoW)")
@click.option("--matrix", "matrix_path_str", default=None, type=click.Path(exists=True),
              help="Path to a pre-compiled matrix JSON (for fair cross-product comparisons)")
@click.option("--journeys", "journeys_path_str", default=None, type=click.Path(exists=True),
              help="Path to a pre-compiled journeys JSON (for fair Tier 2 comparisons)")
@click.option("--plan", "plan_path_str", default=None, type=click.Path(exists=True),
              help="Path to a pre-bound certifier plan JSON")
def certify(project_dir, intent, port_override, output, tier, matrix_path_str, journeys_path_str, plan_path_str):
    """Certify a project against a product intent."""

    from otto.certifier.adapter import analyze_project
    from otto.certifier.binder import bind, load_bound_plan, save_bound_plan
    from otto.certifier.baseline import (
        _report_payload,
        AppRunner,
        judge,
        load_or_compile_journeys,
        load_or_compile_matrix,
        print_report,
        run_baseline_from_bound_plan,
        save_report,
    )
    from otto.certifier.classifier import classify
    from otto.certifier.journey_compiler import JourneyMatrix

    project_dir = project_dir.resolve()
    config: dict = {}
    report_dir = project_dir / "certifier-reports"
    bound_plan = None
    bound_plan_path: Path | None = None
    matrix = None
    matrix_source = "bound_plan" if plan_path_str else "compiled"
    matrix_path = Path(plan_path_str) if plan_path_str else project_dir / "otto_logs" / "certifier" / "matrix.json"
    compile_duration_s = 0.0
    j_source, j_path, j_duration, j_compile_cost = "bound_plan", matrix_path, 0.0, 0.0

    if matrix_path_str:
        click.echo("Warning: --matrix is deprecated; prefer --plan.", err=True)
    if journeys_path_str:
        click.echo("Warning: --journeys is deprecated; prefer --plan.", err=True)
    if plan_path_str and (matrix_path_str or journeys_path_str):
        click.echo("Warning: ignoring deprecated --matrix/--journeys because --plan was provided.", err=True)
    if plan_path_str and tier == 0:
        raise click.ClickException("--plan is only supported for tier 1 and tier 2 execution")

    def _tier2_payload(tier2_result):
        return {
            "product_dir": tier2_result.product_dir,
            "intent": tier2_result.intent,
            "base_url": tier2_result.base_url,
            "mode": tier2_result.mode,
            "score": tier2_result.score(),
            "journeys_tested": tier2_result.journeys_tested,
            "journeys_passed": tier2_result.journeys_passed,
            "journeys_failed": tier2_result.journeys_failed,
            "duration_s": tier2_result.duration_s,
            "journeys": [
                {
                    "name": journey.name,
                    "description": journey.description,
                    "passed": journey.passed,
                    "stopped_at": journey.stopped_at,
                    "steps": [
                        {
                            "action": step.action,
                            "detail": step.detail,
                            "passed": step.passed,
                            "error": step.error,
                            "proof": {
                                "timestamp": step.proof.timestamp,
                                "request": step.proof.request,
                                "response": step.proof.response,
                            } if step.proof.timestamp else None,
                        }
                        for step in journey.steps
                    ],
                }
                for journey in tier2_result.journeys
            ],
        }

    def _combined_tier2_payload(tier1_result, tier2_result, *, report_path, j_source, j_path, j_duration, j_compile_cost):
        product_passed = (
            tier1_result.certified
            and tier2_result.journeys_tested > 0
            and tier2_result.journeys_failed == 0
        )
        return {
            "summary": {
                "project_dir": str(project_dir),
                "intent": intent,
                "tier": 2,
                "product_passed": product_passed,
                "tier1_certified": tier1_result.certified,
                "tier2_score": tier2_result.score(),
                "tier2_journeys_tested": tier2_result.journeys_tested,
                "tier2_journeys_failed": tier2_result.journeys_failed,
                "matrix_source": matrix_source,
                "matrix_path": str(matrix_path),
                "journey_matrix_source": j_source,
                "journey_matrix_path": str(j_path),
                "tier1_compile_cost_usd": tier1_result.compile_cost_usd,
                "tier1_compile_duration_s": tier1_result.compile_duration_s,
                "tier2_compile_cost_usd": j_compile_cost,
                "tier2_compile_duration_s": j_duration,
                "report_path": str(report_path),
            },
            "tier1": _report_payload(tier1_result),
            "tier2": _tier2_payload(tier2_result),
            "raw": {
                "tier1_result": asdict(tier1_result),
                "tier2_result": asdict(tier2_result),
            },
        }

    test_config = analyze_project(project_dir)
    profile = classify(project_dir)
    if port_override is not None:
        profile.port = int(port_override)
        profile.extra["reuse_existing_app"] = True
    runner = None

    if tier == 2:
        from otto.certifier.pow_report import generate_pow_report
        from otto.certifier.tier2 import run_tier2_from_bound_plan

        runner = AppRunner(project_dir, profile)
        app_evidence = runner.start()
        if not app_evidence.passed:
            raise click.ClickException(f"App failed to start: {app_evidence.actual}")

    if plan_path_str:
        bound_plan_path = Path(plan_path_str)
        bound_plan = load_bound_plan(bound_plan_path)
        matrix_source = "bound_plan"
        matrix_path = bound_plan_path
        j_source, j_path = "bound_plan", bound_plan_path
    else:
        if matrix_path_str:
            # Shared matrix for fair cross-product comparison — no schema adaptation
            from otto.certifier.intent_compiler import load_matrix
            matrix = load_matrix(Path(matrix_path_str))
            matrix_source = "shared"
            matrix_path = Path(matrix_path_str)
            compile_duration_s = 0.0
        else:
            matrix, matrix_source, matrix_path, compile_duration_s = load_or_compile_matrix(
                project_dir,
                intent,
                config=config,
                test_config=test_config,
            )

        if tier == 2:
            if journeys_path_str:
                from otto.certifier.journey_compiler import load_journey_matrix
                journey_matrix = load_journey_matrix(Path(journeys_path_str))
                j_source, j_path, j_duration = "shared", Path(journeys_path_str), 0.0
            else:
                journey_matrix, j_source, j_path, j_duration = load_or_compile_journeys(
                    project_dir, intent, config=config,
                )
            j_compile_cost = journey_matrix.cost_usd if j_source != "cache" else 0.0
        else:
            journey_matrix = JourneyMatrix(intent=matrix.intent, journeys=[])

        bound_plan = bind(matrix, journey_matrix, test_config, profile)
        bound_plan_path = report_dir / "bound-plan.json"
        save_bound_plan(bound_plan, bound_plan_path)

    if tier == 2:
        # Shared app lifecycle
        try:
            # Tier 1 — endpoint probes
            tier1_result = run_baseline_from_bound_plan(
                bound_plan,
                project_dir,
                profile,
                app_runner=runner,
            )
            tier1_result.compile_duration_s = compile_duration_s
            tier1_result.compile_cost_usd = (
                matrix.cost_usd if matrix is not None and matrix_source != "cache" else 0.0
            )
            tier1_result.compiled_at = matrix.compiled_at if matrix is not None else bound_plan.compiled_at
            tier1_result.matrix_source = matrix_source
            tier1_result.matrix_path = str(matrix_path)
            tier1_result.app_start_evidence = app_evidence
            tier1_result.verdict = judge(tier1_result)
            tier1_result.certified = tier1_result.verdict.certified

            # Tier 2 — user journeys
            tier2_result = run_tier2_from_bound_plan(
                bound_plan, runner.base_url, project_dir,
            )
        finally:
            runner.stop()

        # Proof-of-work report
        report_path = generate_pow_report(tier1_result, tier2_result, report_dir)
        combined_payload = _combined_tier2_payload(
            tier1_result,
            tier2_result,
            report_path=report_path,
            j_source=j_source,
            j_path=j_path,
            j_duration=j_duration,
            j_compile_cost=j_compile_cost,
        )
        payload_json = json.dumps(combined_payload, indent=2, default=str)

        if output in {"-", "stdout"}:
            click.echo(payload_json)
            return

        # Print summary
        print_report(tier1_result)
        click.echo(f"\nTier 2: {tier2_result.score()}")
        click.echo(f"Report: {report_path}")

        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload_json)
        return

    if tier == 0:
        payload = {
            "summary": {
                "project_dir": str(project_dir),
                "intent": intent,
                "tier": tier,
                "product_type": profile.product_type,
                "framework": profile.framework,
                "port": profile.port,
                "matrix_source": matrix_source,
                "matrix_path": str(matrix_path),
                "compiled_at": matrix.compiled_at,
                "compile_cost_usd": matrix.cost_usd if matrix_source != "cache" else 0.0,
                "compile_duration_s": compile_duration_s,
                "claim_count": len(matrix.claims),
                "critical_claim_count": len(matrix.critical_claims()),
            },
            "adapter": {
                "auth_type": test_config.auth_type,
                "register_endpoint": test_config.register_endpoint,
                "login_endpoint": test_config.login_endpoint,
                "seeded_users": [
                    {"email": user.email, "role": user.role}
                    for user in test_config.seeded_users
                ],
                "routes": [
                    {
                        "path": route.path,
                        "methods": route.methods,
                        "requires_auth": route.requires_auth,
                        "requires_admin": route.requires_admin,
                    }
                    for route in test_config.routes
                ],
                "models": test_config.models,
                "has_cart_model": test_config.has_cart_model,
            },
            "matrix": matrix.to_dict(),
        }
        click.echo(
            "\n".join(
                [
                    "",
                    "Certification: ADAPTER ONLY",
                    f"Project:       {project_dir}",
                    f"Type:          {profile.product_type}",
                    f"Framework:     {profile.framework}",
                    f"Claims:        {len(matrix.claims)} total, {len(matrix.critical_claims())} critical",
                    f"Matrix:        {matrix_source}",
                    f"Auth:          {test_config.auth_type}",
                    "",
                ]
            )
        )
        payload_json = json.dumps(payload, indent=2, default=str)
        if output in {"-", "stdout"}:
            click.echo(payload_json)
        else:
            output_path = Path(output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(payload_json)
        return

    result = run_baseline_from_bound_plan(bound_plan, project_dir, profile)
    result.compile_duration_s = compile_duration_s
    result.compile_cost_usd = matrix.cost_usd if matrix is not None and matrix_source != "cache" else 0.0
    result.compiled_at = matrix.compiled_at if matrix is not None else bound_plan.compiled_at
    result.matrix_source = matrix_source
    result.matrix_path = str(matrix_path)

    print_report(result)
    save_report(result, output)


@main.command(context_settings=CONTEXT_SETTINGS)
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


@main.command(context_settings=CONTEXT_SETTINGS)
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


# Hidden alias: 'otto delete' -> 'otto drop' (backward compat)
@main.command("delete", hidden=True, context_settings=CONTEXT_SETTINGS)
@click.argument("task_id", type=int, required=False)
@click.option("--all", "drop_all", is_flag=True, hidden=True)
@click.option("--yes", is_flag=True, hidden=True)
@click.pass_context
def delete_alias(ctx, task_id, drop_all, yes):
    """Alias for 'otto drop' (deprecated)."""
    ctx.invoke(drop, task_id=task_id, drop_all=drop_all, yes=yes)



# Setup command (registered from otto/cli_setup.py)
from otto.cli_setup import register_setup_command
register_setup_command(main)

# Log/show/diff commands (registered from otto/cli_logs.py)
from otto.cli_logs import register_log_commands
register_log_commands(main)


@main.command(context_settings=CONTEXT_SETTINGS)
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


# Hidden alias: 'otto reset' -> backward compat
@main.command("reset", hidden=True, context_settings=CONTEXT_SETTINGS)
@click.option("--yes", is_flag=True, hidden=True)
@click.option("--revert-commits", is_flag=True, hidden=True)
@click.option("--hard", "revert_commits_compat", is_flag=True, hidden=True)
@click.pass_context
def reset_alias(ctx, yes, revert_commits, revert_commits_compat):
    """Alias for 'otto drop --all' or 'otto revert --all' (deprecated)."""
    hard = revert_commits or revert_commits_compat
    if hard:
        ctx.invoke(revert, task_id=None, revert_all=True, yes=yes)
    else:
        ctx.invoke(drop, task_id=None, drop_all=True, yes=yes)



# Bench subcommands (registered from otto/cli_bench.py)
from otto.cli_bench import register_bench_commands
register_bench_commands(main)
