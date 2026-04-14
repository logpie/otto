"""Otto CLI — entrypoint for all otto commands."""

import asyncio
import os
import sys
import time
from pathlib import Path

# Clear CLAUDECODE at startup so otto can run from inside Claude Code sessions.
# Without this, agent SDK query() spawns a Claude Code subprocess that detects
# the env var and refuses to start ("cannot launch inside another session").
os.environ.pop("CLAUDECODE", None)

import click

from otto.config import create_config, load_config, require_git
from otto.display import console, rich_escape
from otto.theme import error_console


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


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
            status_icon = "[success]\u2713[/success]" if j.get("passed") else "[red]\u2717[/red]"
            console.print(f"    {status_icon} {rich_escape(j.get('name', ''))}")

    if result.break_findings:
        console.print()
        high_count = sum(1 for b in result.break_findings if b.get("severity") in ("critical", "important"))
        warn_count = len(result.break_findings) - high_count
        if high_count:
            console.print(f"  [red bold]\u26a0 {high_count} quality issue(s) found (will trigger fix)[/red bold]")
        if warn_count:
            console.print(f"  [yellow]\u26a0 {warn_count} quality warning(s)[/yellow]")
        for b in result.break_findings:
            sev = b.get("severity", "?")
            desc = rich_escape(b.get("description", "")[:100])
            if sev in ("critical", "important"):
                console.print(f"    [red]\u2717 [{sev}] {desc}[/red]")
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


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("intent")
@click.option("--no-qa", is_flag=True, help="Skip product certification after build")
@click.option("--split", is_flag=True, help="Split mode: system-controlled certify loop with build journal")
def build(intent, no_qa, split):
    """Build a product from a natural language intent.

    One agent builds, certifies, and fixes autonomously. The certifier
    verifies the product works by running real user stories (HTTP, CLI,
    import, WebSocket — any product type).

    Examples:

        otto build "bookmark manager with tags and search"

        otto build "CLI tool that converts CSV to JSON" --no-qa
    """
    if not intent or not intent.strip():
        error_console.print("[error]Intent cannot be empty. Provide a description of what to build.[/error]")
        sys.exit(2)

    require_git()
    project_dir = Path.cwd()
    config_path = project_dir / "otto.yaml"
    if not config_path.exists():
        create_config(project_dir)
        console.print(f"[yellow]First run \u2014 created otto.yaml[/yellow]")
        console.print()
    config = load_config(config_path)

    if no_qa:
        config["skip_product_qa"] = True

    from otto.pipeline import build_agentic_v3, build_split, BuildResult

    build_start = time.time()
    console.print()

    try:
        if split and not no_qa:
            console.print("  [bold]Split mode[/bold] \u2014 system-controlled certify loop\n")
            result: BuildResult = asyncio.run(
                build_split(intent, project_dir, config)
            )
        else:
            console.print("  [bold]Agentic mode[/bold] \u2014 one agent builds, certifies, fixes\n")
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
@click.option("--thorough", is_flag=True, help="Thorough mode — find what's broken, not just verify")
def certify(intent, thorough):
    """Certify a product — independent, builder-blind verification.

    Tests the product in the current directory as a real user. Works on
    any project regardless of how it was built (otto, bare CC, human).

    If no intent is given, reads intent.md or README.md from the project.

    Use --thorough for deeper inspection: code review, edge case probing,
    and escalating difficulty for builder tools.

    Examples:
        otto certify "notes API with auth, CRUD, and search"
        otto certify                   # reads intent.md
        otto certify --thorough        # thorough inspection
    """
    project_dir = Path.cwd()

    # Load config so certifier_timeout and other settings are respected
    config_path = project_dir / "otto.yaml"
    config = load_config(config_path) if config_path.exists() else {}

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

    mode_label = "thorough inspection" if thorough else "independent product verification"
    console.print(f"\n  [bold]Certifying[/bold] \u2014 {mode_label}\n")

    from otto.certifier import run_agentic_certifier

    start = time.time()
    try:
        report = asyncio.run(run_agentic_certifier(
            intent=intent,
            project_dir=project_dir,
            config=config,
            thorough=thorough,
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
            icon = "[success]\u2713[/success]" if s.get("passed") else "[red]\u2717[/red]"
            console.print(f"    {icon} {rich_escape(s.get('summary', s.get('story_id', '')))}")

    console.print()
    outcome = report.outcome.value
    if outcome == "passed":
        console.print(f"  [success bold]PASSED[/success bold] \u2014 {passed_count}/{len(story_results)} stories")
    else:
        console.print(f"  [red bold]FAILED[/red bold] \u2014 {passed_count}/{len(story_results)} stories")

    console.print(f"  Cost: ${report.cost_usd:.2f}  Duration: {duration:.0f}s")

    # PoW report location
    pow_dir = project_dir / "otto_logs" / "certifier" / "latest"
    if (pow_dir / "proof-of-work.html").exists():
        console.print(f"  Report: {pow_dir / 'proof-of-work.html'}")

    console.print()
    sys.exit(0 if outcome == "passed" else 1)


# Setup command (registered from otto/cli_setup.py)
from otto.cli_setup import register_setup_command
register_setup_command(main)

# History command (registered from otto/cli_logs.py)
from otto.cli_logs import register_history_command
register_history_command(main)

# Fix and improve commands (registered from otto/cli_improve.py)
from otto.cli_improve import register_improve_commands
register_improve_commands(main)

