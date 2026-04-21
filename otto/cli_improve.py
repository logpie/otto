"""Otto CLI — improve command group (bugs, feature, target)."""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import click

from otto.display import CONTEXT_SETTINGS, console, rich_escape
from otto.theme import error_console


def _exit_for_lock_busy(exc) -> None:
    holder = exc.holder or {}
    error_console.print(
        "[error]Another otto command is already running in this project.[/error]\n"
        f"  Holder: pid={holder.get('pid', '?')} command={holder.get('command', '?')} "
        f"started_at={holder.get('started_at', '?')} "
        f"session={holder.get('session_id', '') or 'unknown'}\n"
        "  Re-run with `--break-lock` only if you are sure the lock is stuck."
    )
    sys.exit(1)


def _resolve_intent(project_dir: Path) -> str | None:
    """Resolve product description from intent.md or README.md."""
    from otto.config import _normalize_intent, resolve_intent
    intent = _normalize_intent(resolve_intent(project_dir) or "")
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
    in_worktree: bool = False,
    break_lock: bool = False,
    cli_overrides: dict | None = None,
) -> None:
    """CLI wrapper: branch creation, display, and report around the shared loop.

    ``subcommand`` is "bugs" | "feature" | "target" — written into the
    checkpoint as ``command="improve.<subcommand>"`` so resuming preserves the
    exact intent and the mismatch warning can fire if the user switches modes.
    """
    from otto.checkpoint import print_resume_status, resolve_resume
    from otto.config import load_config

    command_id = f"improve.{subcommand}"
    config = load_config(project_dir / "otto.yaml") if (project_dir / "otto.yaml").exists() else {}
    if resume_state is None:
        resume_state = resolve_resume(project_dir, resume, expected_command=command_id)
    print_resume_status(console, resume_state, resume, expected_command=command_id)

    # --in-worktree creates an isolated worktree before branching.
    if in_worktree:
        if resume:
            error_console.print(
                "[error]--in-worktree is not compatible with --resume.[/error]\n"
                "  cd into the existing worktree directly to resume."
            )
            sys.exit(2)
        from otto.worktree import (
            WorktreeAlreadyCheckedOut,
            setup_worktree_for_atomic_cli,
        )
        worktree_slug_source = focus or target or intent
        try:
            wt_path, config = setup_worktree_for_atomic_cli(
                project_dir=project_dir,
                mode=f"improve-{subcommand}",
                intent=intent,
                config=config,
                slug_source=worktree_slug_source,
            )
        except WorktreeAlreadyCheckedOut as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(1)
        except (RuntimeError, ValueError) as exc:
            error_console.print(f"[error]Worktree setup failed: {rich_escape(str(exc))}[/error]")
            sys.exit(1)
        console.print(f"  [dim]Worktree:[/dim] [info]{wt_path}[/info]")
        project_dir = wt_path

    from otto import paths as _paths
    try:
        with _paths.project_lock(project_dir, command_id, break_lock=break_lock):
            run_id = resume_state.run_id or ""
            if not run_id:
                run_id = _paths.new_session_id(project_dir)
            _run_improve_locked(
                project_dir=project_dir,
                intent=intent,
                rounds=rounds,
                focus=focus,
                certifier_mode=certifier_mode,
                command_label=command_label,
                command_id=command_id,
                subcommand=subcommand,
                target=target,
                split=split,
                resume=resume,
                resume_state=resume_state,
                run_id=run_id,
                in_worktree=in_worktree,
                cli_overrides=cli_overrides or {},
            )
    except _paths.LockBusy as exc:
        _exit_for_lock_busy(exc)


def _run_improve_locked(
    *,
    project_dir: Path,
    intent: str,
    rounds: int,
    focus: str | None,
    certifier_mode: str,
    command_label: str,
    command_id: str,
    subcommand: str,
    target: str | None,
    split: bool,
    resume: bool,
    resume_state,
    run_id: str,
    in_worktree: bool = False,
    cli_overrides: dict | None = None,
) -> None:
    from otto import paths as _paths
    from otto.config import load_config
    from otto.pipeline import build_agentic_v3, run_certify_fix_loop

    # Branch: --in-worktree already created one; otherwise make the improve/... branch now.
    if in_worktree:
        from otto.branching import current_branch as _cb
        branch = _cb(project_dir)
    else:
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
    config = load_config(config_path)
    config["max_certify_rounds"] = max(1, rounds)

    # Apply CLI overrides to the loaded config.
    overrides = cli_overrides or {}
    sources: dict[str, str] = {}
    if overrides.get("budget") is not None:
        config["run_budget_seconds"] = overrides["budget"]
        sources["run_budget_seconds"] = "--budget"
    if overrides.get("model"):
        config["model"] = overrides["model"]
        sources["model"] = "--model"
    if overrides.get("provider"):
        config["provider"] = overrides["provider"]
        sources["provider"] = "--provider"
    if overrides.get("effort"):
        config["effort"] = overrides["effort"]
        sources["effort"] = "--effort"
    if overrides.get("strict"):
        config["strict_mode"] = True
        sources["strict_mode"] = "--strict"
    config["_verbose"] = bool(overrides.get("verbose"))

    # Give improve modes a larger wall-clock budget by default, since
    # multi-round fix loops legitimately take longer than a single build.
    # User-set `run_budget_seconds` wins.
    if not config.get("run_budget_seconds") or config["run_budget_seconds"] == 3600:
        if "run_budget_seconds" not in sources:
            # No CLI --budget and no explicit yaml override → use improve default.
            import yaml as _yaml
            raw = {}
            if config_path.exists():
                try:
                    raw = _yaml.safe_load(config_path.read_text()) or {}
                except Exception:
                    pass
            if "run_budget_seconds" not in raw:
                config["run_budget_seconds"] = 7200  # 2h default for improve

    from otto.cli import _print_config_banner
    _print_config_banner(console, config, sources, config_path)

    from otto.budget import RunBudget
    budget = RunBudget.start_from(config)

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
                session_id=resume_state.run_id or run_id,
                budget=budget,
                strict_mode=bool(config.get("strict_mode")),
                verbose=bool(config.get("_verbose")),
            ))
        else:
            # Agent-driven: one session, agent drives certify→fix loop
            result = asyncio.run(build_agentic_v3(
                improve_intent,
                project_dir,
                config,
                certifier_mode=certifier_mode,
                prompt_mode="improve",
                resume_session_id=resume_state.agent_session_id or None,
                command=command_id,
                run_id=resume_state.run_id or run_id,
                budget=budget,
                strict_mode=bool(config.get("strict_mode")),
                verbose=bool(config.get("_verbose")),
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

    # Under the new layout the report lives inside the improve session dir.
    report_path = _paths.improve_dir(project_dir, result.build_id) / "improvement-report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(report_lines))

    # Phase 1.4: write per-run manifest. improve uses the build_dir
    # naming scheme (otto_logs/builds/<build_id>/) since it routes through
    # build_agentic_v3 / run_certify_fix_loop.
    try:
        from otto.manifest import (
            current_head_sha,
            make_manifest,
            write_manifest,
        )
        build_dir = project_dir / "otto_logs" / "builds" / result.build_id
        manifest = make_manifest(
            command="improve",
            argv=list(sys.argv[1:]),
            run_id=result.build_id,
            branch=branch,
            checkpoint_path=build_dir / "checkpoint.json",
            proof_of_work_path=project_dir / "otto_logs" / "certifier" / "proof-of-work.json",
            cost_usd=result.total_cost,
            duration_s=duration,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
            head_sha=None,  # filled below
            resolved_intent=intent,
            focus=focus,
            target=target,
            exit_status="success" if result.passed else "failure",
        )
        manifest.head_sha = current_head_sha(project_dir)
        write_manifest(manifest, project_dir=project_dir, fallback_dir=build_dir)
    except Exception as exc:
        error_console.print(f"[yellow]warning: manifest write failed: {exc}[/yellow]")

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
    """Resolve intent or exit with error. Normalizes whitespace so multiline
    intent files don't leak embedded line-wraps into resolved_intent."""
    from otto.config import _normalize_intent, resolve_intent

    intent = _normalize_intent(resolve_intent(project_dir) or "")
    if not intent:
        error_console.print(
            "[error]No product description found. Create intent.md[/error]"
        )
        sys.exit(2)
    console.print("  [dim]Intent from project files[/dim]")
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
    @click.option("--in-worktree", "in_worktree", is_flag=True,
                  help="Run in an isolated git worktree (./.worktrees/improve-bugs-<slug>-<date>/)")
    @click.option("--budget", default=None, type=int, help="Total wall-clock budget in seconds (default from otto.yaml or 7200 for improve)")
    @click.option("--model", default=None, help="Override model for every agent (e.g. sonnet, haiku, gpt-5)")
    @click.option("--provider", default=None, help="Override provider for every agent: claude | codex")
    @click.option("--effort", default=None, help="Override effort level for every agent: low | medium | high | max")
    @click.option("--strict", is_flag=True, help="Require two consecutive PASS rounds before stopping")
    @click.option("--verbose", is_flag=True, help="Show detailed live progress, including tool-call counts")
    @click.option("--break-lock", is_flag=True, help="Force-clear the project lock before starting")
    def bugs(focus, rounds, split, resume, in_worktree, budget, model, provider, effort, strict, verbose, break_lock):
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
            in_worktree=in_worktree,
            break_lock=break_lock,
            cli_overrides={
                "budget": budget,
                "model": model,
                "provider": provider,
                "effort": effort,
                "strict": strict,
                "verbose": verbose,
            },
        )

    @improve.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("focus", required=False)
    @click.option("--rounds", "-n", default=3, help="Maximum rounds (default: 3)")
    @click.option("--split", is_flag=True, help="System-controlled loop (vs agent-driven)")
    @click.option("--resume", is_flag=True, help="Resume from last checkpoint")
    @click.option("--in-worktree", "in_worktree", is_flag=True,
                  help="Run in an isolated git worktree (./.worktrees/improve-feature-<slug>-<date>/)")
    @click.option("--budget", default=None, type=int, help="Total wall-clock budget in seconds (default from otto.yaml or 7200 for improve)")
    @click.option("--model", default=None, help="Override model for every agent (e.g. sonnet, haiku, gpt-5)")
    @click.option("--provider", default=None, help="Override provider for every agent: claude | codex")
    @click.option("--effort", default=None, help="Override effort level for every agent: low | medium | high | max")
    @click.option("--strict", is_flag=True, help="Require two consecutive PASS rounds before stopping")
    @click.option("--verbose", is_flag=True, help="Show detailed live progress, including tool-call counts")
    @click.option("--break-lock", is_flag=True, help="Force-clear the project lock before starting")
    def feature(focus, rounds, split, resume, in_worktree, budget, model, provider, effort, strict, verbose, break_lock):
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
            in_worktree=in_worktree,
            break_lock=break_lock,
            cli_overrides={
                "budget": budget,
                "model": model,
                "provider": provider,
                "effort": effort,
                "strict": strict,
                "verbose": verbose,
            },
        )

    @improve.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("goal", required=False)
    @click.option("--rounds", "-n", default=5, help="Maximum rounds (default: 5)")
    @click.option("--split", is_flag=True, help="System-controlled loop (vs agent-driven)")
    @click.option("--resume", is_flag=True, help="Resume from last checkpoint")
    @click.option("--in-worktree", "in_worktree", is_flag=True,
                  help="Run in an isolated git worktree (./.worktrees/improve-target-<slug>-<date>/)")
    @click.option("--budget", default=None, type=int, help="Total wall-clock budget in seconds (default from otto.yaml or 7200 for improve)")
    @click.option("--model", default=None, help="Override model for every agent (e.g. sonnet, haiku, gpt-5)")
    @click.option("--provider", default=None, help="Override provider for every agent: claude | codex")
    @click.option("--effort", default=None, help="Override effort level for every agent: low | medium | high | max")
    @click.option("--strict", is_flag=True, help="Require two consecutive PASS rounds before stopping")
    @click.option("--verbose", is_flag=True, help="Show detailed live progress, including tool-call counts")
    @click.option("--break-lock", is_flag=True, help="Force-clear the project lock before starting")
    def target(goal, rounds, split, resume, in_worktree, budget, model, provider, effort, strict, verbose, break_lock):
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
        from otto.config import _normalize_intent

        checkpoint_goal = _normalize_intent(resume_state.target or "")
        requested_goal = _normalize_intent(goal or "")

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

        goal = _normalize_intent(goal or "")
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
            in_worktree=in_worktree,
            break_lock=break_lock,
            cli_overrides={
                "budget": budget,
                "model": model,
                "provider": provider,
                "effort": effort,
                "strict": strict,
                "verbose": verbose,
            },
        )
