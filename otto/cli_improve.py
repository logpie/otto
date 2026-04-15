"""Otto CLI — fix and improve commands."""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import click

from otto.certifier.report import CertificationOutcome
from otto.display import console, rich_escape
from otto.theme import error_console


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def _resolve_intent(project_dir: Path) -> str | None:
    """Resolve product description from intent.md or README.md."""
    intent_path = project_dir / "intent.md"
    readme_path = project_dir / "README.md"
    if intent_path.exists():
        intent = intent_path.read_text().strip()
        if intent:
            console.print(f"  [dim]Intent from intent.md[/dim]")
            return intent
    if readme_path.exists():
        intent = readme_path.read_text().strip()[:2000]
        if intent:
            console.print(f"  [dim]Intent from README.md[/dim]")
            return intent
    return None


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


def _run_improve_loop(
    project_dir: Path,
    intent: str,
    rounds: int,
    focus: str | None,
    certifier_mode: str,
    command_label: str,
) -> None:
    """Shared loop for fix and improve commands.

    certifier_mode: "thorough" (for fix) or "hillclimb" (for improve)
    command_label: display label ("Fixing" or "Improving")
    """
    # Create improvement branch
    branch = _create_improve_branch(project_dir)
    console.print(f"\n  [bold]{command_label}[/bold] — branch: [info]{branch}[/info]")
    if focus:
        console.print(f"  Focus: {rich_escape(focus)}")
    console.print(f"  Rounds: up to {rounds}")
    console.print()

    from otto.certifier import run_agentic_certifier
    from otto.config import load_config
    from otto.pipeline import build_agentic_v3

    config_path = project_dir / "otto.yaml"
    config = load_config(config_path) if config_path.exists() else {}

    # Use longer timeout for thorough/hillclimb certifier
    certify_config = dict(config)
    certify_config["certifier_timeout"] = max(
        int(certify_config.get("certifier_timeout", 900)), 3600
    )

    mode_label = "quality review" if certifier_mode == "hillclimb" else "thorough"
    total_start = time.time()
    total_cost = 0.0
    total_issues = 0
    round_reports: list[str] = []

    for round_num in range(1, rounds + 1):
        console.print(f"  [bold]Round {round_num}/{rounds}[/bold]")

        # --- Certify ---
        console.print(f"    Certifying ({mode_label})...")
        try:
            report = asyncio.run(run_agentic_certifier(
                intent=intent,
                project_dir=project_dir,
                config=certify_config,
                mode=certifier_mode,
                focus=focus,
            ))
        except KeyboardInterrupt:
            console.print("\n  Aborted.")
            sys.exit(1)
        except Exception as e:
            error_console.print(f"[error]Certifier failed: {rich_escape(str(e))}[/error]")
            break

        certify_cost = getattr(report, "cost_usd", 0.0)
        total_cost += certify_cost
        stories = getattr(report, "_story_results", [])
        failures = [s for s in stories if not s.get("passed")]
        total_issues += len(failures)

        if getattr(report, "outcome", None) == CertificationOutcome.INFRA_ERROR:
            console.print(
                f"    [yellow]Warning: certifier infrastructure error "
                f"(${certify_cost:.2f}); stopping[/yellow]"
            )
            round_reports.append(
                f"## Round {round_num}\n"
                f"Certifier: infrastructure error. ${certify_cost:.2f}\n"
            )
            break

        # Empty stories = certifier produced no results (parsing failed, agent
        # produced no markers). Treat as failed — don't mark passed.
        if not stories:
            console.print(
                f"    [yellow]Warning: certifier returned no stories "
                f"(${certify_cost:.2f}); stopping[/yellow]"
            )
            round_reports.append(
                f"## Round {round_num}\n"
                f"Certifier: no stories returned (possible parsing failure). ${certify_cost:.2f}\n"
            )
            break

        if not failures:
            console.print(f"    [success]No issues found[/success] (${certify_cost:.2f})")
            round_reports.append(
                f"## Round {round_num}\n"
                f"Certifier: no issues found. ${certify_cost:.2f}\n"
            )
            break

        console.print(f"    Found {len(failures)} issue(s) (${certify_cost:.2f})")
        for f in failures:
            console.print(f"      [red]\u2717[/red] {rich_escape(f.get('summary', '')[:80])}")

        # --- Build (fix) ---
        console.print(f"    Fixing...")

        fix_lines = [f"Fix these issues found by certification:\n"]
        for f in failures:
            sid = f.get("story_id", "?")
            summary = f.get("summary", "")
            evidence = f.get("evidence", "")
            fix_lines.append(f"### {sid}")
            fix_lines.append(f"**Symptom:** {summary}")
            if evidence:
                fix_lines.append(f"**Evidence:**\n```\n{evidence[:500]}\n```")
            fix_lines.append("")
        fix_lines.append(
            "Diagnose the root causes in the code and fix them. "
            "Do NOT fix by changing prompts unless the fix is generic."
        )
        fix_intent = "\n".join(fix_lines)

        fix_config = dict(config)
        fix_config["skip_product_qa"] = True

        try:
            build_result = asyncio.run(build_agentic_v3(
                intent=fix_intent,
                project_dir=project_dir,
                config=fix_config,
            ))
        except KeyboardInterrupt:
            console.print("\n  Aborted.")
            sys.exit(1)
        except Exception as e:
            error_console.print(f"[error]Build failed: {rich_escape(str(e))}[/error]")
            break

        build_cost = getattr(build_result, "total_cost", 0.0)
        total_cost += build_cost
        if build_result.passed:
            console.print(f"    Fixed ({len(failures)} issues, ${build_cost:.2f})")
        else:
            console.print(
                f"    [yellow]Warning: fix phase did not complete cleanly "
                f"(${build_cost:.2f})[/yellow]"
            )

        # Round report
        round_report_lines = [
            f"## Round {round_num}",
            f"Certifier: {len(failures)} issues found. ${certify_cost:.2f}",
        ]
        for f in failures:
            round_report_lines.append(f"- **{f.get('story_id', '?')}**: {f.get('summary', '')}")
        if build_result.passed:
            round_report_lines.append(
                f"\nBuild: {build_result.tasks_passed} verified. ${build_cost:.2f}"
            )
        else:
            round_report_lines.append(
                f"\nBuild: warning - fix phase did not complete cleanly. ${build_cost:.2f}"
            )
        round_report_lines.append("")
        round_reports.append("\n".join(round_report_lines))

        console.print()

    # --- Aggregate report ---
    total_duration = time.time() - total_start
    report_lines = [
        f"# {command_label} Report",
        f"> {time.strftime('%Y-%m-%d %H:%M')} | "
        f"{rounds} rounds | ${total_cost:.2f} | {total_duration / 60:.1f} min",
        f"",
        f"**Branch:** {branch}",
        f"**Intent:** {intent[:200]}",
        "",
    ]
    if focus:
        report_lines.append(f"**Focus:** {focus}")
        report_lines.append("")

    for rr in round_reports:
        report_lines.append(rr)

    report_lines.append("## Summary")
    report_lines.append(f"- Issues found: {total_issues}")
    report_lines.append(f"- Rounds: {len(round_reports)}/{rounds}")
    report_lines.append(f"- Cost: ${total_cost:.2f}")
    report_lines.append(f"- Duration: {total_duration / 60:.1f} min")
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
    console.print(f"  Issues found: {total_issues}")
    console.print(f"  Rounds: {len(round_reports)}/{rounds}")
    console.print(f"  Cost: ${total_cost:.2f}")
    console.print(f"  Duration: {total_duration / 60:.1f} min")
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

        _run_improve_loop(
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

        _run_improve_loop(
            project_dir=project_dir,
            intent=intent,
            rounds=rounds,
            focus=focus,
            certifier_mode="hillclimb",
            command_label="Improving",
        )
