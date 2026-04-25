"""Otto CLI — improve command group (bugs, feature, target)."""

import asyncio
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import click

from otto.display import CONTEXT_SETTINGS, console, rich_escape
from otto.config import ConfigError, require_git, resolve_project_dir
from otto.theme import error_console


def _positive_budget_option(
    _ctx: click.Context,
    _param: click.Parameter,
    value: int | None,
) -> int | None:
    if value is not None and value <= 0:
        raise click.BadParameter("must be > 0")
    return value


def _rounds_option(
    _ctx: click.Context,
    _param: click.Parameter,
    value: int | None,
) -> int | None:
    if value is None:
        return None
    if value <= 0:
        raise click.BadParameter("must be >= 1")
    if value > 50:
        raise click.BadParameter("must be <= 50")
    return value


def _max_turns_option(
    _ctx: click.Context,
    _param: click.Parameter,
    value: int | None,
) -> int | None:
    if value is None:
        return None
    if value < 1:
        raise click.BadParameter("must be >= 1")
    if value > 200:
        raise click.BadParameter("must be <= 200")
    return value


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


def _create_improve_branch(project_dir: Path) -> str:
    """Create an improvement branch and switch to it. Returns branch name."""
    branch = f"improve/{time.strftime('%Y-%m-%d')}-{secrets.token_hex(3)}"
    # Check if already on an improve branch
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_dir, capture_output=True, text=True,
    )
    current = result.stdout.strip()
    if current.startswith("improve/"):
        return current  # already on an improve branch

    # Create and switch to a unique per-run branch.
    result = subprocess.run(
        ["git", "checkout", "-b", branch],
        cwd=project_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        error_console.print(
            "[error]Failed to create improvement branch "
            f"{branch}: {stderr or stdout or 'unknown git error'}[/error]"
        )
        sys.exit(1)

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


def _resolve_improve_certifier_mode(
    *,
    default_mode: str,
    fast: bool = False,
    standard: bool = False,
    thorough: bool = False,
) -> str:
    from otto.config import resolve_certifier_mode

    if sum(bool(x) for x in (fast, standard, thorough)) > 1:
        error_console.print(
            "[error]--fast, --standard, and --thorough are mutually exclusive.[/error]"
        )
        sys.exit(2)
    cli_mode = "fast" if fast else ("standard" if standard else ("thorough" if thorough else None))
    return resolve_certifier_mode(
        {"certifier_mode": default_mode},
        cli_mode=cli_mode,
        allow_internal=True,
    )


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
    force_cross_command_resume: bool = False,
    in_worktree: bool = False,
    break_lock: bool = False,
    force: bool = False,
    allow_dirty: bool = False,
    cli_overrides: dict | None = None,
) -> None:
    """CLI wrapper: branch creation, display, and report around the shared loop.

    ``subcommand`` is "bugs" | "feature" | "target" — written into the
    checkpoint as ``command="improve.<subcommand>"`` so resuming preserves the
    exact intent and the mismatch warning can fire if the user switches modes.
    """
    from otto.checkpoint import (
        enforce_resume_available,
        enforce_resume_command_match,
        print_resume_status,
        resolve_resume,
    )
    from otto.config import load_config

    command_id = f"improve.{subcommand}"
    try:
        config = load_config(project_dir / "otto.yaml") if (project_dir / "otto.yaml").exists() else {}
    except (ConfigError, ValueError) as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(2)
    if resume_state is None:
        resume_state = resolve_resume(
            project_dir,
            resume,
            expected_command=command_id,
            force=force,
            reject_incompatible=False,
        )
    if resume_state.fingerprint_mismatch and not force:
        error_console.print(
            "[error]Checkpoint fingerprint does not match the current code/prompt state.[/error]\n"
            "  Pass `--force --resume` to override."
        )
        sys.exit(2)
    enforce_resume_available(
        resume_state,
        resume_flag=resume,
        expected_command=command_id,
    )
    enforce_resume_command_match(
        resume_state,
        command_id,
        force_cross_command_resume=force_cross_command_resume,
    )
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
        from otto.cli import _signal_interrupt_guard

        with _signal_interrupt_guard():
            with _paths.project_lock(project_dir, command_id, break_lock=break_lock):
                run_id = resume_state.run_id or ""
                if not run_id:
                    run_id = os.environ.get("OTTO_RUN_ID", "").strip()
                if not run_id:
                    from otto.runs.registry import allocate_run_id
                    run_id = allocate_run_id(project_dir)
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
                    allow_dirty=allow_dirty,
                    cli_overrides=cli_overrides or {},
                )
    except _paths.LockBreakError as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(1)
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
    allow_dirty: bool = False,
    cli_overrides: dict | None = None,
) -> None:
    from otto import paths as _paths
    from otto.cli import _load_config_or_exit, _print_config_banner, _record_cli_override
    from otto.config import ensure_safe_repo_state, get_max_rounds, get_max_turns_per_call
    from otto.pipeline import build_agentic_v3, run_certify_fix_loop

    config_path = project_dir / "otto.yaml"
    config = _load_config_or_exit(config_path)
    resume_rounds = getattr(resume_state, "max_rounds", 0) or 0
    if rounds is not None:
        resolved_rounds = rounds
    elif resume_state.resumed and resume_rounds:
        resolved_rounds = resume_rounds
    else:
        resolved_rounds = get_max_rounds(config)
    resolved_split = bool(split or config.get("split_mode"))
    if resume_state.resumed and resume_state.split_mode is not None:
        resolved_split = resume_state.split_mode
    if resume_state.resumed and not focus and getattr(resume_state, "focus", ""):
        focus = resume_state.focus
    if (
        resume_state.resumed
        and not (cli_overrides or {}).get("certifier_mode_explicit")
        and getattr(resume_state, "certifier_mode", "")
        and subcommand == "bugs"
    ):
        certifier_mode = resume_state.certifier_mode

    pre_branch_allow_dirty = bool(allow_dirty or config.get("allow_dirty_repo"))
    if not resume_state.resumed:
        try:
            ensure_safe_repo_state(project_dir, allow_dirty=pre_branch_allow_dirty)
        except ConfigError as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(2)

    if in_worktree:
        from otto.branching import current_branch as _cb

        branch = _cb(project_dir)
    else:
        branch = _create_improve_branch(project_dir)
    mode_label = "split" if resolved_split else "agentic"
    console.print(f"\n  [bold]{command_label}[/bold] ({mode_label}) — branch: [info]{branch}[/info]")
    if focus:
        console.print(f"  Focus: {rich_escape(focus)}")
    if target:
        console.print(f"  Target: {rich_escape(target)}")
    console.print(f"  Rounds: up to {resolved_rounds}")
    console.print()

    config["max_certify_rounds"] = resolved_rounds
    config["split_mode"] = resolved_split
    config["certifier_mode"] = certifier_mode

    # Apply CLI overrides to the loaded config.
    overrides = cli_overrides or {}
    sources: dict[str, str] = {}
    if rounds is not None:
        sources["max_certify_rounds"] = "--rounds"
    elif resume_state.resumed and resume_rounds:
        sources["max_certify_rounds"] = "checkpoint"
    if (
        resume_state.resumed
        and not overrides.get("certifier_mode_explicit")
        and getattr(resume_state, "certifier_mode", "")
        and subcommand == "bugs"
    ):
        sources["certifier_mode"] = "checkpoint"
    if overrides.get("budget") is not None:
        config["run_budget_seconds"] = overrides["budget"]
        sources["run_budget_seconds"] = "--budget"
    if overrides.get("max_turns") is not None:
        config["max_turns_per_call"] = overrides["max_turns"]
        sources["max_turns_per_call"] = "--max-turns"
    if overrides.get("model"):
        config["model"] = overrides["model"]
        sources["model"] = "--model"
        _record_cli_override(config, "model", overrides["model"])
    if overrides.get("provider"):
        config["provider"] = overrides["provider"]
        sources["provider"] = "--provider"
        _record_cli_override(config, "provider", overrides["provider"])
    if overrides.get("effort"):
        config["effort"] = overrides["effort"]
        sources["effort"] = "--effort"
        _record_cli_override(config, "effort", overrides["effort"])
    if overrides.get("strict"):
        config["strict_mode"] = True
        sources["strict_mode"] = "--strict"
    if allow_dirty:
        config["allow_dirty_repo"] = True
        sources["allow_dirty_repo"] = "--allow-dirty"
    if overrides.get("debug_unredacted"):
        config["debug_unredacted"] = True
        sources["debug_unredacted"] = "--debug-unredacted"
    config["_verbose"] = bool(overrides.get("verbose"))
    try:
        from otto.config import resolve_intent_provenance

        intent_meta = resolve_intent_provenance(project_dir)
    except Exception:
        intent_meta = {}
    config["_intent_source"] = intent_meta.get("source") or "cli-argument"
    config["_intent_fallback_reason"] = intent_meta.get("fallback_reason") or ""
    try:
        config["max_certify_rounds"] = get_max_rounds(config)
        config["max_turns_per_call"] = get_max_turns_per_call(config)
    except Exception as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(2)
    _print_config_banner(console, config, sources, config_path)
    if overrides.get("debug_unredacted"):
        console.print("  [bold red]UNREDACTED LOGS — do not share[/bold red]")
    try:
        ensure_safe_repo_state(
            project_dir,
            allow_dirty=bool(resume_state.resumed or allow_dirty or config.get("allow_dirty_repo")),
        )
    except ConfigError as exc:
        if resume_state.run_id:
            try:
                from otto.checkpoint import load_checkpoint, write_checkpoint
                from otto.observability import dirty_worktree_files

                prior_cp = load_checkpoint(project_dir, run_id=resume_state.run_id) or {}
                write_checkpoint(
                    project_dir,
                    run_id=resume_state.run_id,
                    command=prior_cp.get("command", command_id) or command_id,
                    certifier_mode=prior_cp.get("certifier_mode", certifier_mode) or certifier_mode,
                    prompt_mode=prior_cp.get("prompt_mode", "improve") or "improve",
                    focus=prior_cp.get("focus"),
                    target=prior_cp.get("target"),
                    max_rounds=int(prior_cp.get("max_rounds", resolved_rounds) or resolved_rounds),
                    status=prior_cp.get("status", "paused") or "paused",
                    phase=prior_cp.get("phase", "") or "",
                    split_mode=prior_cp.get("split_mode"),
                    session_id=prior_cp.get("agent_session_id", "") or "",
                    current_round=int(prior_cp.get("current_round", 0) or 0),
                    total_cost=float(prior_cp.get("total_cost", 0.0) or 0.0),
                    total_duration=float(prior_cp.get("total_duration", 0.0) or 0.0),
                    rounds=list(prior_cp.get("rounds", []) or []),
                    child_session_ids=list(prior_cp.get("child_session_ids", []) or []),
                    intent=prior_cp.get("intent"),
                    spec_path=prior_cp.get("spec_path"),
                    spec_hash=prior_cp.get("spec_hash"),
                    spec_version=int(prior_cp.get("spec_version", 0) or 0),
                    spec_cost=float(prior_cp.get("spec_cost", 0.0) or 0.0),
                    dirty_files=dirty_worktree_files(project_dir),
                )
            except Exception:
                pass
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(2)

    from otto.budget import RunBudget
    budget = RunBudget.start_from(
        config,
        session_started_at=resume_state.session_started_at or None,
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
        if resolved_split:
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
                resume_duration=resume_state.total_duration,
                resume_rounds=resume_state.rounds,
                resume_session_id=resume_state.agent_session_id or None,
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
                prior_total_cost=resume_state.total_cost,
                prior_total_duration=resume_state.total_duration,
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

    duration = result.total_duration or (time.time() - start)
    default_branch = str(config.get("default_branch") or "main")

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
    report_lines.append(f"Review: `git diff {default_branch}...{branch}`")
    report_lines.append(f"Merge: `git merge {branch}`")
    report_lines.append("")

    # Under the new layout the report lives inside the improve session dir.
    report_path = _paths.improve_dir(project_dir, result.build_id) / "improvement-report.md"
    report_text = "\n".join(report_lines)
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_text)
    except OSError as exc:
        console.print()
        console.print(f"  [bold]{command_label} complete[/bold]")
        console.print(f"  Rounds: {result.rounds}")
        console.print(f"  Cost: ${result.total_cost:.2f}")
        console.print(f"  Duration: {duration / 60:.1f} min")
        console.print(f"  [yellow]Warning: could not write report file {report_path}: {exc}[/yellow]")
        console.print()
        sys.exit(0 if result.passed else 1)

    # Per-run manifest. The canonical record always lives at the session root;
    # queue-backed runs also mirror it into otto_logs/queue/<task-id>/.
    try:
        from otto import paths as _paths
        from otto.manifest import (
            QUEUE_TASK_ENV,
            current_head_sha,
            make_manifest,
            write_manifest,
        )
        session_dir_path = _paths.session_dir(project_dir, result.build_id)
        certify_dir_path = _paths.certify_dir(project_dir, result.build_id)
        manifest = make_manifest(
            command="improve",
            argv=list(sys.argv[1:]),
            queue_task_id=os.environ.get(QUEUE_TASK_ENV),
            run_id=result.build_id,
            branch=branch,
            checkpoint_path=session_dir_path / "checkpoint.json",
            proof_of_work_path=certify_dir_path / "proof-of-work.json",
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
        write_manifest(manifest, project_dir=project_dir, fallback_dir=session_dir_path)
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
    console.print(f"  Review: [info]git diff {default_branch}...{branch}[/info]")
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
    @click.option("--rounds", "-n", default=None, type=int, callback=_rounds_option, help="Maximum rounds, 1-50 (default from otto.yaml or 8)")
    @click.option("--split", is_flag=True, help="System-controlled loop (vs agent-driven)")
    @click.option("--resume", is_flag=True, help="Resume from last checkpoint")
    @click.option(
        "--force-cross-command-resume",
        is_flag=True,
        help="Allow --resume to reuse a checkpoint written by a different otto command",
    )
    @click.option("--in-worktree", "in_worktree", is_flag=True,
                  help="Run in an isolated git worktree (./.worktrees/improve-bugs-<slug>-<date>/)")
    @click.option("--budget", default=None, type=int, callback=_positive_budget_option, help="Total wall-clock budget in seconds, must be > 0 (default from otto.yaml or 3600)")
    @click.option("--max-turns", default=None, type=int, callback=_max_turns_option, help="Max agent turns per call, 1-200 (default from otto.yaml or 200)")
    @click.option("--model", default=None, help="Override model for every agent (e.g. sonnet, haiku, gpt-5)")
    @click.option("--provider", default=None, help="Override provider for every agent: claude | codex")
    @click.option("--effort", default=None, help="Override effort level for every agent: low | medium | high | max")
    @click.option("--fast", is_flag=True, help="Downgrade bug certification to fast mode")
    @click.option("--standard", is_flag=True, help="Downgrade bug certification to standard mode")
    @click.option("--thorough", is_flag=True, help="Bug certification depth (default)")
    @click.option("--strict", is_flag=True, help="Require two consecutive PASS rounds before stopping")
    @click.option("--verbose", is_flag=True, help="Show detailed live progress, including tool-call counts")
    @click.option("--debug-unredacted", is_flag=True, help="Also write unredacted raw logs under sessions/<id>/raw/ (do not share)")
    @click.option("--allow-dirty", is_flag=True, help="Proceed even if the repo has tracked modifications, staged changes, or an in-progress git operation")
    @click.option("--break-lock", is_flag=True, help="Force-clear the project lock before starting")
    @click.option("--force", is_flag=True, help="Override resume checkpoint mismatch checks")
    def bugs(focus, rounds, split, resume, force_cross_command_resume, in_worktree, budget, max_turns, model, provider, effort, fast, standard, thorough, strict, verbose, debug_unredacted, allow_dirty, break_lock, force):
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
        require_git()
        project_dir = resolve_project_dir(Path.cwd())
        intent = _require_intent(project_dir)
        _run_improve(
            project_dir=project_dir,
            intent=intent,
            rounds=rounds,
            focus=focus,
            certifier_mode=_resolve_improve_certifier_mode(
                default_mode="thorough",
                fast=fast,
                standard=standard,
                thorough=thorough,
            ),
            command_label="Bug fixing",
            subcommand="bugs",
            split=split,
            resume=resume,
            force_cross_command_resume=force_cross_command_resume,
            in_worktree=in_worktree,
            break_lock=break_lock,
            force=force,
            allow_dirty=allow_dirty,
            cli_overrides={
                "budget": budget,
                "max_turns": max_turns,
                "model": model,
                "provider": provider,
                "effort": effort,
                "strict": strict,
                "verbose": verbose,
                "debug_unredacted": debug_unredacted,
                "certifier_mode_explicit": bool(fast or standard or thorough),
            },
        )

    @improve.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("focus", required=False)
    @click.option("--rounds", "-n", default=None, type=int, callback=_rounds_option, help="Maximum rounds, 1-50 (default from otto.yaml or 8)")
    @click.option("--split", is_flag=True, help="System-controlled loop (vs agent-driven)")
    @click.option("--resume", is_flag=True, help="Resume from last checkpoint")
    @click.option(
        "--force-cross-command-resume",
        is_flag=True,
        help="Allow --resume to reuse a checkpoint written by a different otto command",
    )
    @click.option("--in-worktree", "in_worktree", is_flag=True,
                  help="Run in an isolated git worktree (./.worktrees/improve-feature-<slug>-<date>/)")
    @click.option("--budget", default=None, type=int, callback=_positive_budget_option, help="Total wall-clock budget in seconds, must be > 0 (default from otto.yaml or 3600)")
    @click.option("--max-turns", default=None, type=int, callback=_max_turns_option, help="Max agent turns per call, 1-200 (default from otto.yaml or 200)")
    @click.option("--model", default=None, help="Override model for every agent (e.g. sonnet, haiku, gpt-5)")
    @click.option("--provider", default=None, help="Override provider for every agent: claude | codex")
    @click.option("--effort", default=None, help="Override effort level for every agent: low | medium | high | max")
    @click.option("--strict", is_flag=True, help="Require two consecutive PASS rounds before stopping")
    @click.option("--verbose", is_flag=True, help="Show detailed live progress, including tool-call counts")
    @click.option("--debug-unredacted", is_flag=True, help="Also write unredacted raw logs under sessions/<id>/raw/ (do not share)")
    @click.option("--allow-dirty", is_flag=True, help="Proceed even if the repo has tracked modifications, staged changes, or an in-progress git operation")
    @click.option("--break-lock", is_flag=True, help="Force-clear the project lock before starting")
    @click.option("--force", is_flag=True, help="Override resume checkpoint mismatch checks")
    def feature(focus, rounds, split, resume, force_cross_command_resume, in_worktree, budget, max_turns, model, provider, effort, strict, verbose, debug_unredacted, allow_dirty, break_lock, force):
        """Suggest and implement product improvements.

        One agent evaluates the product, identifies improvements, implements
        them, and re-evaluates. Use --split for system-controlled loop.

        \b
        Examples:
            otto improve feature               # suggest and implement improvements
            otto improve feature "search UX"   # focus on search experience
            otto improve feature -n 5          # 5 rounds
        """
        require_git()
        project_dir = resolve_project_dir(Path.cwd())
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
            force_cross_command_resume=force_cross_command_resume,
            in_worktree=in_worktree,
            break_lock=break_lock,
            force=force,
            allow_dirty=allow_dirty,
            cli_overrides={
                "budget": budget,
                "max_turns": max_turns,
                "model": model,
                "provider": provider,
                "effort": effort,
                "strict": strict,
                "verbose": verbose,
                "debug_unredacted": debug_unredacted,
            },
        )

    @improve.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("goal", required=False)
    @click.option("--rounds", "-n", default=None, type=int, callback=_rounds_option, help="Maximum rounds, 1-50 (default from otto.yaml or 8)")
    @click.option("--split", is_flag=True, help="System-controlled loop (vs agent-driven)")
    @click.option("--resume", is_flag=True, help="Resume from last checkpoint")
    @click.option(
        "--force-cross-command-resume",
        is_flag=True,
        help="Allow --resume to reuse a checkpoint written by a different otto command",
    )
    @click.option("--in-worktree", "in_worktree", is_flag=True,
                  help="Run in an isolated git worktree (./.worktrees/improve-target-<slug>-<date>/)")
    @click.option("--budget", default=None, type=int, callback=_positive_budget_option, help="Total wall-clock budget in seconds, must be > 0 (default from otto.yaml or 3600)")
    @click.option("--max-turns", default=None, type=int, callback=_max_turns_option, help="Max agent turns per call, 1-200 (default from otto.yaml or 200)")
    @click.option("--model", default=None, help="Override model for every agent (e.g. sonnet, haiku, gpt-5)")
    @click.option("--provider", default=None, help="Override provider for every agent: claude | codex")
    @click.option("--effort", default=None, help="Override effort level for every agent: low | medium | high | max")
    @click.option("--strict", is_flag=True, help="Require two consecutive PASS rounds before stopping")
    @click.option("--verbose", is_flag=True, help="Show detailed live progress, including tool-call counts")
    @click.option("--debug-unredacted", is_flag=True, help="Also write unredacted raw logs under sessions/<id>/raw/ (do not share)")
    @click.option("--allow-dirty", is_flag=True, help="Proceed even if the repo has tracked modifications, staged changes, or an in-progress git operation")
    @click.option("--break-lock", is_flag=True, help="Force-clear the project lock before starting")
    @click.option("--force", is_flag=True, help="Override resume checkpoint mismatch checks")
    def target(goal, rounds, split, resume, force_cross_command_resume, in_worktree, budget, max_turns, model, provider, effort, strict, verbose, debug_unredacted, allow_dirty, break_lock, force):
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
        require_git()
        project_dir = resolve_project_dir(Path.cwd())
        from otto.checkpoint import (
            enforce_resume_available,
            enforce_resume_command_match,
            resolve_resume,
        )

        resume_state = resolve_resume(
            project_dir,
            resume,
            expected_command="improve.target",
            force=force,
            reject_incompatible=False,
        )
        if resume_state.fingerprint_mismatch and not force:
            error_console.print(
                "[error]Checkpoint fingerprint does not match the current code/prompt state.[/error]\n"
                "  Pass `--force --resume` to override."
            )
            sys.exit(2)
        enforce_resume_available(
            resume_state,
            resume_flag=resume,
            expected_command="improve.target",
        )
        enforce_resume_command_match(
            resume_state,
            "improve.target",
            force_cross_command_resume=force_cross_command_resume,
        )
        from otto.config import _normalize_intent

        checkpoint_goal = _normalize_intent(resume_state.target or "")
        requested_goal = _normalize_intent(goal or "")

        if resume and resume_state.resumed:
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
            force_cross_command_resume=force_cross_command_resume,
            in_worktree=in_worktree,
            break_lock=break_lock,
            force=force,
            allow_dirty=allow_dirty,
            cli_overrides={
                "budget": budget,
                "max_turns": max_turns,
                "model": model,
                "provider": provider,
                "effort": effort,
                "strict": strict,
                "verbose": verbose,
                "debug_unredacted": debug_unredacted,
            },
        )
