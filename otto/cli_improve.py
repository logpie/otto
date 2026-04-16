"""Otto CLI — fix and improve commands."""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import click

from otto.display import CONTEXT_SETTINGS, console, rich_escape
from otto.theme import error_console


def _resolve_intent(project_dir: Path) -> str | None:
    """Resolve product description from intent.md or README.md."""
    from otto.config import resolve_intent
    intent = resolve_intent(project_dir)
    if intent:
        console.print(f"  [dim]Intent from project files[/dim]")
    return intent


def _create_improve_branch(project_dir: Path) -> str:
    """Create an improvement branch and switch to it. Returns branch name."""
    branch = f"improve/{time.strftime('%Y-%m-%d')}"
    # Check if already on an improve branch
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_dir, capture_output=True, text=True,
    )
    current = result.stdout.strip()
    if current.startswith("improve/"):
        return current  # already on an improve branch

    # Create and switch — if branch exists (from earlier run today), switch to it
    result = subprocess.run(
        ["git", "checkout", "-b", branch],
        cwd=project_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        # Branch already exists — switch to it
        subprocess.run(
            ["git", "checkout", branch],
            cwd=project_dir, capture_output=True, text=True,
        )

    current_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_dir, capture_output=True, text=True,
    ).stdout.strip()
    if current_branch != branch:
        error_console.print(
            "[error]Failed to switch to improvement branch. "
            f"Expected {branch}, got {current_branch or '(none)'}[/error]"
        )
        sys.exit(1)

    return branch


def _run_fix_or_improve(
    project_dir: Path,
    intent: str,
    rounds: int,
    focus: str | None,
    certifier_mode: str,
    command_label: str,
) -> None:
    """CLI wrapper: branch creation, display, and report around the shared loop."""
    from otto.config import load_config
    from otto.pipeline import run_certify_fix_loop

    # Create improvement branch
    branch = _create_improve_branch(project_dir)
    console.print(f"\n  [bold]{command_label}[/bold] — branch: [info]{branch}[/info]")
    if focus:
        console.print(f"  Focus: {rich_escape(focus)}")
    console.print(f"  Rounds: up to {rounds}")
    console.print()

    config_path = project_dir / "otto.yaml"
    config = load_config(config_path) if config_path.exists() else {}
    config["max_certify_rounds"] = max(1, rounds)

    # Use longer timeout for thorough/hillclimb certifier
    config["certifier_timeout"] = max(
        int(config.get("certifier_timeout", 900)), 3600
    )

    start = time.time()
    try:
        result = asyncio.run(run_certify_fix_loop(
            intent=intent,
            project_dir=project_dir,
            config=config,
            certifier_mode=certifier_mode,
            focus=focus,
            skip_initial_build=True,  # code already exists
        ))
    except KeyboardInterrupt:
        console.print("\n  Aborted.")
        sys.exit(1)
    except Exception as e:
        error_console.print(f"[error]{command_label} failed: {rich_escape(str(e))}[/error]")
        sys.exit(1)

    duration = time.time() - start

    # --- Write report ---
    report_lines = [
        f"# {command_label} Report",
        f"> {time.strftime('%Y-%m-%d %H:%M')} | "
        f"{result.rounds} rounds | ${result.total_cost:.2f} | {duration / 60:.1f} min",
        "",
        f"**Branch:** {branch}",
        f"**Intent:** {intent[:200]}",
        "",
    ]
    if focus:
        report_lines.append(f"**Focus:** {focus}")
        report_lines.append("")

    if result.journeys:
        report_lines.append("## Results")
        for j in result.journeys:
            icon = "\u2713" if j.get("passed") else "\u2717"
            report_lines.append(f"- {icon} {j.get('name', '')}")
        report_lines.append("")

    report_lines.append("## Summary")
    report_lines.append(f"- **Result:** {'PASSED' if result.passed else 'FAILED'}")
    report_lines.append(f"- **Stories:** {result.tasks_passed}/{result.tasks_passed + result.tasks_failed}")
    report_lines.append(f"- **Rounds:** {result.rounds}")
    report_lines.append(f"- **Cost:** ${result.total_cost:.2f}")
    report_lines.append(f"- **Duration:** {duration / 60:.1f} min")
    report_lines.append("")
    report_lines.append(f"Review: `git diff main...{branch}`")
    report_lines.append(f"Merge: `git merge {branch}`")
    report_lines.append("")

    report_path = project_dir / "otto_logs" / "improvement-report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(report_lines))

    # --- Summary ---
    console.print()
    console.print(f"  [bold]{command_label} complete[/bold]")
    if result.journeys:
        for j in result.journeys:
            icon = "[success]\u2713[/success]" if j.get("passed") else "[red]\u2717[/red]"
            console.print(f"    {icon} {rich_escape(j.get('name', ''))}")
    console.print(f"  Rounds: {result.rounds}")
    console.print(f"  Cost: ${result.total_cost:.2f}")
    console.print(f"  Duration: {duration / 60:.1f} min")
    console.print(f"  Report: {report_path}")
    console.print()
    console.print(f"  Review: [info]git diff main...{branch}[/info]")
    console.print(f"  Merge:  [info]git merge {branch}[/info]")
    console.print()


def register_improve_commands(main: click.Group) -> None:
    """Register fix and improve commands on the main CLI group."""

    @main.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("focus", required=False)
    @click.option("--rounds", "-n", default=3, help="Maximum rounds (default: 3)")
    def fix(focus, rounds):
        """Find and fix bugs in the current project.

        Runs a thorough certifier to find edge cases, crashes, and error
        handling gaps, then fixes them automatically.

        Creates an improvement branch for isolation.

        Examples:
            otto fix                       # find and fix all bugs
            otto fix "error handling"      # focus on error handling
            otto fix --rounds 5            # 5 rounds
        """
        project_dir = Path.cwd()
        intent = _resolve_intent(project_dir)
        if not intent:
            error_console.print(
                "[error]No product description found. Create intent.md[/error]"
            )
            sys.exit(2)

        _run_fix_or_improve(
            project_dir=project_dir,
            intent=intent,
            rounds=rounds,
            focus=focus,
            certifier_mode="thorough",
            command_label="Fixing",
        )

    @main.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("focus", required=False)
    @click.option("--rounds", "-n", default=3, help="Maximum rounds (default: 3)")
    def improve(focus, rounds):
        """Suggest and implement product improvements.

        Runs a product advisor to find missing features, UX gaps, and
        design improvements, then implements them automatically.

        Creates an improvement branch for isolation.

        Examples:
            otto improve                   # suggest and implement improvements
            otto improve "search UX"       # focus on search experience
            otto improve --rounds 5        # 5 rounds
        """
        project_dir = Path.cwd()
        intent = _resolve_intent(project_dir)
        if not intent:
            error_console.print(
                "[error]No product description found. Create intent.md[/error]"
            )
            sys.exit(2)

        _run_fix_or_improve(
            project_dir=project_dir,
            intent=intent,
            rounds=rounds,
            focus=focus,
            certifier_mode="hillclimb",
            command_label="Improving",
        )
