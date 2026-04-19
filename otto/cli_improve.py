"""Otto CLI — improve command group (bugs, feature, target)."""

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
        console.print("  [dim]Intent from project files[/dim]")
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


def _run_improve(
    project_dir: Path,
    intent: str,
    rounds: int,
    focus: str | None,
    certifier_mode: str,
    command_label: str,
    *,
    subcommand: str,
    target: str | None = None,
    split: bool = False,
    resume: bool = False,
    resume_state=None,
) -> None:
    """CLI wrapper: branch creation, display, and report around the shared loop.

    ``subcommand`` is "bugs" | "feature" | "target" — written into the
    checkpoint as ``command="improve.<subcommand>"`` so resuming preserves the
    exact intent and the mismatch warning can fire if the user switches modes.
    """
    from otto.checkpoint import print_resume_status, resolve_resume
    from otto.config import load_config
    from otto.pipeline import build_agentic_v3, run_certify_fix_loop

    command_id = f"improve.{subcommand}"
    if resume_state is None:
        resume_state = resolve_resume(project_dir, resume, expected_command=command_id)
    print_resume_status(console, resume_state, resume, expected_command=command_id)

    # Create improvement branch
    branch = _create_improve_branch(project_dir)
    mode_label = "split" if split else "agentic"
    console.print(f"\n  [bold]{command_label}[/bold] ({mode_label}) — branch: [info]{branch}[/info]")
    if focus:
        console.print(f"  Focus: {rich_escape(focus)}")
    if target:
        console.print(f"  Target: {rich_escape(target)}")
    console.print(f"  Rounds: up to {rounds}")
    console.print()

    config_path = project_dir / "otto.yaml"
    config = load_config(config_path) if config_path.exists() else {}
    config["max_certify_rounds"] = max(1, rounds)

    # Use longer timeout for improve modes
    config["certifier_timeout"] = max(
        int(config.get("certifier_timeout", 900)), 3600
    )

    # Pass target to config for prompt filling
    if target:
        config["_target"] = target

    # Build the improve intent with focus/target context
    improve_intent = intent
    if focus:
        improve_intent += f"\n\n## Improvement Focus\n{focus}"
    if target:
        improve_intent += f"\n\n## Target\n{target}"

    start = time.time()
    try:
        if split:
            # System-driven: Python controls certify→fix loop
            result = asyncio.run(run_certify_fix_loop(
                intent=intent,
                project_dir=project_dir,
                config=config,
                certifier_mode=certifier_mode,
                focus=focus,
                target=target,
                skip_initial_build=True,
                start_round=resume_state.start_round,
                resume_cost=resume_state.total_cost,
                resume_rounds=resume_state.rounds,
                command=command_id,
            ))
        else:
            # Agent-driven: one session, agent drives certify→fix loop
            result = asyncio.run(build_agentic_v3(
                improve_intent,
                project_dir,
                config,
                certifier_mode=certifier_mode,
                prompt_mode="improve",
                resume_session_id=resume_state.session_id or None,
                command=command_id,
            ))
    except KeyboardInterrupt:
        console.print("\n  [yellow]Paused. Run with --resume to continue.[/yellow]")
        sys.exit(0)
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
    if target:
        report_lines.append(f"**Target:** {target}")
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

    # Exit code mirrors `otto build` — non-zero when the run didn't reach
    # its goal so CI/wrappers can detect failure.
    sys.exit(0 if result.passed else 1)


def _require_intent(project_dir: Path) -> str:
    """Resolve intent or exit with error."""
    intent = _resolve_intent(project_dir)
    if not intent:
        error_console.print(
            "[error]No product description found. Create intent.md[/error]"
        )
        sys.exit(2)
    return intent


def register_improve_commands(main: click.Group) -> None:
    """Register the improve command group on the main CLI group."""

    @main.group(context_settings=CONTEXT_SETTINGS, invoke_without_command=True)
    @click.pass_context
    def improve(ctx):
        """Improve the current project — find bugs, add features, or hit targets.

        Requires a subcommand:

        \b
            otto improve bugs                  # find and fix bugs
            otto improve feature "search UX"   # add/improve features
            otto improve target "latency < 100ms"  # hit a metric target
        """
        if ctx.invoked_subcommand is None:
            error_console.print(
                "[error]Specify a mode: bugs, feature, target[/error]\n"
            )
            click.echo(ctx.get_help())
            ctx.exit(2)

    @improve.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("focus", required=False)
    @click.option("--rounds", "-n", default=3, help="Maximum rounds (default: 3)")
    @click.option("--split", is_flag=True, help="System-controlled loop (vs agent-driven)")
    @click.option("--resume", is_flag=True, help="Resume from last checkpoint")
    def bugs(focus, rounds, split, resume):
        """Find and fix bugs, edge cases, and error handling gaps.

        One agent certifies, reads findings, fixes, and re-certifies
        autonomously. Use --split for system-controlled loop instead.

        \b
        Examples:
            otto improve bugs                  # find and fix all bugs
            otto improve bugs "error handling" # focus on error handling
            otto improve bugs -n 5             # 5 rounds
            otto improve bugs --split          # system-controlled loop
        """
        project_dir = Path.cwd()
        intent = _require_intent(project_dir)
        _run_improve(
            project_dir=project_dir,
            intent=intent,
            rounds=rounds,
            focus=focus,
            certifier_mode="thorough",
            command_label="Bug fixing",
            subcommand="bugs",
            split=split,
            resume=resume,
        )

    @improve.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("focus", required=False)
    @click.option("--rounds", "-n", default=3, help="Maximum rounds (default: 3)")
    @click.option("--split", is_flag=True, help="System-controlled loop (vs agent-driven)")
    @click.option("--resume", is_flag=True, help="Resume from last checkpoint")
    def feature(focus, rounds, split, resume):
        """Suggest and implement product improvements.

        One agent evaluates the product, identifies improvements, implements
        them, and re-evaluates. Use --split for system-controlled loop.

        \b
        Examples:
            otto improve feature               # suggest and implement improvements
            otto improve feature "search UX"   # focus on search experience
            otto improve feature -n 5          # 5 rounds
        """
        project_dir = Path.cwd()
        intent = _require_intent(project_dir)
        _run_improve(
            project_dir=project_dir,
            intent=intent,
            rounds=rounds,
            focus=focus,
            certifier_mode="hillclimb",
            command_label="Feature improvement",
            subcommand="feature",
            split=split,
            resume=resume,
        )

    @improve.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("goal", required=False)
    @click.option("--rounds", "-n", default=5, help="Maximum rounds (default: 5)")
    @click.option("--split", is_flag=True, help="System-controlled loop (vs agent-driven)")
    @click.option("--resume", is_flag=True, help="Resume from last checkpoint")
    def target(goal, rounds, split, resume):
        """Optimize toward a measurable target.

        Measures a metric, compares to the target, and iterates until met.
        Use --split for system-controlled loop.

        \b
        Examples:
            otto improve target "latency < 100ms"
            otto improve target "bundle size < 500kb"
            otto improve target "test coverage > 90%"
            otto improve target "lighthouse score > 95" -n 10
        """
        project_dir = Path.cwd()
        from otto.checkpoint import resolve_resume

        resume_state = resolve_resume(
            project_dir, resume, expected_command="improve.target"
        )
        checkpoint_goal = (resume_state.target or "").strip()
        requested_goal = (goal or "").strip()

        if resume and resume_state.resumed:
            if resume_state.prior_command != "improve.target":
                error_console.print(
                    "[error]Checkpoint is not from `improve target`. "
                    "Run without --resume to start a new target-improvement run.[/error]"
                )
                sys.exit(2)
            if requested_goal:
                if checkpoint_goal and requested_goal != checkpoint_goal:
                    error_console.print(
                        "[error]Checkpoint target does not match the requested goal. "
                        "Resume without GOAL to inherit the checkpoint target, or run "
                        "without --resume to start a new target-improvement run.[/error]"
                    )
                    sys.exit(2)
            elif checkpoint_goal:
                goal = checkpoint_goal

        goal = (goal or "").strip()
        if not goal:
            error_console.print(
                "[error]Goal cannot be empty. Provide a measurable target, or use "
                "--resume to inherit it from an in-progress checkpoint.[/error]"
            )
            sys.exit(2)

        intent = _require_intent(project_dir)
        _run_improve(
            project_dir=project_dir,
            intent=intent,
            rounds=rounds,
            focus=None,
            certifier_mode="target",
            command_label=f"Target: {goal}",
            subcommand="target",
            target=goal,
            split=split,
            resume=resume,
            resume_state=resume_state,
        )
