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
from otto.display import CONTEXT_SETTINGS, console, rich_escape
from otto.theme import error_console


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


def _new_run_id() -> str:
    """Human-readable + unique-ish identifier for a full otto run."""
    import secrets
    stamp = time.strftime("%Y-%m-%d")
    slug = secrets.token_hex(3)
    return f"{stamp}-{slug}"


async def _run_spec_phase(
    *,
    project_dir: Path,
    intent: str,
    spec: bool,
    spec_file: Path | None,
    auto_approve: bool,
    resume_state,
    config: dict,
    budget=None,
) -> tuple[str, str, float]:
    """Drive the spec phase before the main build.

    Returns (run_id, spec_content, total_spec_cost). Writes checkpoint at
    each phase boundary (`spec` → `spec_review` → `spec_approved`).

    Raises SystemExit(2) with a user message on failure.
    """
    from otto.checkpoint import (
        load_checkpoint,
        spec_phase_completed,
        write_checkpoint,
    )
    from otto.spec import (
        SpecResult,
        count_open_questions,
        format_spec_section,
        read_spec_file,
        review_spec,
        run_spec_agent,
        spec_hash,
        validate_spec,
    )

    # Determine run_id: resume preserves, otherwise fresh.
    run_id = resume_state.run_id or _new_run_id()
    run_dir = project_dir / "otto_logs" / "runs" / run_id

    # Resume fast-path: already approved.
    if resume_state.resumed and spec_phase_completed(resume_state.phase):
        # Load the approved spec from disk, verify hash, skip spec phase.
        if not resume_state.spec_path:
            error_console.print("[error]Resume from spec_approved but no spec_path in checkpoint.[/error]")
            sys.exit(2)
        spec_md = Path(resume_state.spec_path)
        if not spec_md.exists():
            error_console.print(f"[error]Approved spec file missing: {spec_md}[/error]")
            sys.exit(2)
        content = spec_md.read_text()
        if resume_state.spec_hash and spec_hash(content) != resume_state.spec_hash:
            error_console.print(
                f"[error]Spec hash mismatch at {spec_md}. The file was modified after approval.\n"
                "  Run without --resume to start a fresh spec, or restore the original file.[/error]"
            )
            sys.exit(2)
        return run_id, content, resume_state.spec_cost

    spec_cost = resume_state.spec_cost or 0.0
    current_phase = "spec"
    current_spec_path = resume_state.spec_path or ""
    current_spec_hash = resume_state.spec_hash or ""
    current_spec_version = resume_state.spec_version or 0

    # Resume mid-review: spec.md exists, re-open the gate.
    resume_mid_review = (
        resume_state.resumed
        and resume_state.phase == "spec_review"
        and resume_state.spec_path
        and Path(resume_state.spec_path).exists()
    )

    # Resume mid-spec-agent: if spec.md exists and validates, promote to review.
    resume_mid_spec_with_file = (
        resume_state.resumed
        and resume_state.phase == "spec"
        and resume_state.spec_path
        and Path(resume_state.spec_path).exists()
    )

    try:
        if spec_file:
            # External spec: load, validate, skip agent.
            spec_path_out = run_dir / "spec.md"
            run_dir.mkdir(parents=True, exist_ok=True)
            intent_from_file, content = read_spec_file(spec_file)
            # Only overwrite the run-dir copy on fresh runs; on resume keep existing.
            if not spec_path_out.exists():
                spec_path_out.write_text(content)
            spec_result = SpecResult(
                path=spec_path_out,
                content=content,
                open_questions=count_open_questions(content),
                cost=spec_cost,
                duration_s=0.0,
                version=resume_state.spec_version,
            )
            # --spec-file implies auto-approve
            auto_approve = True
            # Write spec_review checkpoint before approval (so crash = resumable)
            current_phase = "spec_review"
            current_spec_path = str(spec_path_out)
            current_spec_hash = spec_hash(content)
            current_spec_version = resume_state.spec_version
            write_checkpoint(
                project_dir,
                run_id=run_id,
                command="build",
                phase="spec_review",
                intent=intent,
                spec_path=str(spec_path_out),
                spec_hash=current_spec_hash,
                spec_version=current_spec_version,
                spec_cost=spec_cost,
            )
        elif resume_mid_review or resume_mid_spec_with_file:
            # Re-open the review gate using existing on-disk spec.
            existing_path = Path(resume_state.spec_path)
            content = existing_path.read_text()
            errors = validate_spec(content)
            if errors:
                console.print(f"  [yellow]Existing spec has issues: {'; '.join(errors)}[/yellow]")
                console.print("  [yellow]Re-running spec agent.[/yellow]\n")
                # Fall through to agent path
                current_phase = "spec"
                write_checkpoint(
                    project_dir, run_id=run_id, command="build", phase="spec",
                    intent=intent, spec_cost=spec_cost, spec_version=resume_state.spec_version,
                )
                spec_result = await run_spec_agent(
                    intent, project_dir, run_dir, config,
                    version=resume_state.spec_version, budget=budget,
                )
                spec_cost += spec_result.cost
                current_phase = "spec_review"
                current_spec_path = str(spec_result.path)
                current_spec_hash = spec_hash(spec_result.content)
                current_spec_version = spec_result.version
                write_checkpoint(
                    project_dir, run_id=run_id, command="build", phase="spec_review",
                    intent=intent, spec_path=str(spec_result.path),
                    spec_hash=current_spec_hash, spec_version=current_spec_version, spec_cost=spec_cost,
                )
            else:
                spec_result = SpecResult(
                    path=existing_path,
                    content=content,
                    open_questions=count_open_questions(content),
                    cost=spec_cost,
                    duration_s=0.0,
                    version=resume_state.spec_version,
                )
                current_phase = "spec_review"
                current_spec_path = str(existing_path)
                current_spec_hash = spec_hash(content)
                current_spec_version = resume_state.spec_version
                console.print(f"  [info]Resuming at review gate (existing spec at {existing_path})[/info]\n")
        else:
            # Fresh spec generation.
            current_phase = "spec"
            write_checkpoint(
                project_dir, run_id=run_id, command="build", phase="spec",
                intent=intent, spec_cost=spec_cost, spec_version=resume_state.spec_version,
            )
            console.print("  [bold]Spec phase[/bold] — generating product spec...\n")
            spec_result = await run_spec_agent(
                intent, project_dir, run_dir, config, budget=budget,
            )
            spec_cost += spec_result.cost
            current_phase = "spec_review"
            current_spec_path = str(spec_result.path)
            current_spec_hash = spec_hash(spec_result.content)
            current_spec_version = spec_result.version
            write_checkpoint(
                project_dir, run_id=run_id, command="build", phase="spec_review",
                intent=intent, spec_path=str(spec_result.path),
                spec_hash=current_spec_hash, spec_version=current_spec_version, spec_cost=spec_cost,
            )

        # Review gate
        approved = await review_spec(
            spec_result, project_dir, run_dir, run_id, intent, config,
            auto_approve=auto_approve,
            initial_regen_count=resume_state.spec_version,
        )
        spec_cost = approved.cost
        current_phase = "spec_approved"
        current_spec_path = str(approved.path)
        current_spec_hash = spec_hash(approved.content)
        current_spec_version = approved.version

        # Record approved state
        write_checkpoint(
            project_dir, run_id=run_id, command="build", phase="spec_approved",
            intent=intent, spec_path=str(approved.path),
            spec_hash=current_spec_hash, spec_version=current_spec_version, spec_cost=spec_cost,
        )
        return run_id, approved.content, spec_cost

    except ValueError as exc:
        error_console.print(f"[error]{exc}[/error]")
        sys.exit(2)
    except KeyboardInterrupt:
        prior_cp = load_checkpoint(project_dir) or {}
        write_checkpoint(
            project_dir,
            run_id=run_id,
            command="build",
            phase=(prior_cp.get("phase", "") or current_phase),
            intent=intent,
            status="paused",
            spec_path=(prior_cp.get("spec_path", "") or current_spec_path),
            spec_hash=(prior_cp.get("spec_hash", "") or current_spec_hash),
            spec_version=int(prior_cp.get("spec_version", current_spec_version) or 0),
            spec_cost=float(prior_cp.get("spec_cost", spec_cost) or 0.0),
        )
        raise
    except Exception as exc:
        # AgentCallError from budget exhaustion / timeout during spec: catch
        # here and write a paused checkpoint so --resume works. Other
        # RuntimeErrors (invalid spec content, missing file) exit non-zero
        # without checkpoint munging.
        from otto.agent import AgentCallError
        if isinstance(exc, AgentCallError):
            prior_cp = load_checkpoint(project_dir) or {}
            session_id = exc.session_id or prior_cp.get("session_id", "") or ""
            write_checkpoint(
                project_dir, run_id=run_id, command="build",
                status="paused", phase=current_phase,
                intent=intent,
                spec_path=current_spec_path or None,
                spec_hash=current_spec_hash or None,
                spec_version=current_spec_version,
                spec_cost=spec_cost,
                session_id=session_id,
            )
            error_console.print(
                f"[error]Run budget exhausted during spec ({exc.reason}).[/error]\n"
                "  Use `otto build --resume` to continue, or raise "
                "`run_budget_seconds` in otto.yaml."
            )
            sys.exit(1)
        error_console.print(f"[error]Spec phase failed: {exc}[/error]")
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

    console.print()
    console.print(f"  [bold]Build Summary[/bold]  ({result.build_id})")
    console.print(f"  Intent: {rich_escape(intent[:80])}")
    if result.journeys:
        console.print(f"  Stories: {result.tasks_passed} passed, {result.tasks_failed} failed")
    else:
        console.print(f"  Tasks: {result.tasks_passed} passed, {result.tasks_failed} failed")
    console.print(f"  [bold]Total cost: ${result.total_cost:.2f}[/bold]")
    console.print(f"  Duration: {build_duration / 60:.1f} min")
    console.print()


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("intent", required=False)
@click.option("--no-qa", is_flag=True, help="Skip product certification after build")
@click.option("--fast", is_flag=True, help="Fast certification — happy path smoke test only")
@click.option("--split", is_flag=True, help="Split mode: system-controlled certify loop with build journal")
@click.option("--rounds", "-n", default=None, type=int, help="Max certification rounds (default: 8)")
@click.option("--resume", is_flag=True, help="Resume from last checkpoint (requires an in-progress run)")
@click.option("--spec", is_flag=True, help="Generate a reviewable spec before building")
@click.option("--spec-file", type=click.Path(exists=False, dir_okay=False, path_type=Path),
              default=None, help="Use a pre-written spec file (implies --yes)")
@click.option("--yes", is_flag=True, help="Auto-approve the generated spec (for CI/scripts)")
@click.option("--force", is_flag=True, help="Discard an active paused spec run and start fresh")
def build(intent, no_qa, fast, split, rounds, resume, spec, spec_file, yes, force):
    """Build a product from a natural language intent.

    One agent builds, certifies, and fixes autonomously. The certifier
    verifies the product works by running real user stories (HTTP, CLI,
    import, WebSocket — any product type).

    If a prior build was interrupted, pass --resume to continue it. Intent
    is optional on resume and is inherited from the checkpoint.

    Examples:

        otto build "bookmark manager with tags and search"

        otto build "bookmark manager" --fast    # quick smoke test

        otto build "CLI tool that converts CSV to JSON" --no-qa

        otto build --resume                     # continue interrupted run
    """
    require_git()
    project_dir = Path.cwd()

    from otto.checkpoint import (
        initial_build_completed,
        load_checkpoint,
        is_spec_phase,
        print_resume_status,
        resolve_resume,
    )
    from otto.spec import read_spec_file

    if spec and spec_file:
        error_console.print("[error]--spec and --spec-file are mutually exclusive.[/error]")
        sys.exit(2)

    cp = load_checkpoint(project_dir)

    # Protect paused spec checkpoints before resolve_resume() can clear them.
    if cp and is_spec_phase(cp.get("phase", "") or "") and not resume:
        if not force:
            error_console.print(
                "[error]Paused spec run detected at "
                f"phase={cp.get('phase')!r}, run_id={cp.get('run_id', '')!r}.[/error]\n"
                "  Use --resume to continue, or --force to discard."
            )
            sys.exit(2)
        from otto.checkpoint import clear_checkpoint
        run_id_to_archive = cp.get("run_id", "") or ""
        if run_id_to_archive:
            run_dir_old = project_dir / "otto_logs" / "runs" / run_id_to_archive
            run_dir_abandoned = project_dir / "otto_logs" / "runs" / f"{run_id_to_archive}.abandoned"
            if run_dir_old.exists():
                try:
                    run_dir_old.rename(run_dir_abandoned)
                    console.print(f"  [yellow]Archived prior spec run to {run_dir_abandoned}[/yellow]")
                except OSError as exc:
                    console.print(f"  [yellow]Could not archive prior spec run: {exc}[/yellow]")
        clear_checkpoint(project_dir)
        cp = None

    resume_state = resolve_resume(project_dir, resume, expected_command="build")
    use_spec = (
        bool(spec or spec_file)
        or is_spec_phase(resume_state.phase)
        or bool(resume_state.spec_path)
    )
    if use_spec:
        if split:
            error_console.print("[error]--spec is not compatible with --split (v1).[/error]")
            sys.exit(2)
        if fast:
            error_console.print("[error]--spec is not compatible with --fast (v1).[/error]")
            sys.exit(2)
        if no_qa:
            error_console.print("[error]--spec requires the certifier; --no-qa is incompatible.[/error]")
            sys.exit(2)

    intent = (intent or "").strip()

    if spec_file:
        try:
            file_intent, _ = read_spec_file(Path(spec_file))
        except ValueError as exc:
            error_console.print(f"[error]{exc}[/error]")
            sys.exit(2)
        if intent and intent != file_intent:
            error_console.print(
                f"[error]Intent mismatch: CLI intent does not match {spec_file}.[/error]"
            )
            sys.exit(2)
        if resume and resume_state.resumed and resume_state.spec_path:
            expected_spec_path = Path(resume_state.spec_path).resolve(strict=False)
            given_spec_path = Path(spec_file).resolve(strict=False)
            if given_spec_path != expected_spec_path:
                error_console.print(
                    "[error]Cannot change --spec-file on resume. "
                    f"Checkpoint spec is {expected_spec_path}, got {given_spec_path}.[/error]"
                )
                sys.exit(2)
        intent = file_intent

    resume_without_intent = bool(resume and resume_state.resumed and not intent)
    display_intent = intent or "(resumed run)"

    # Inherit intent from checkpoint for spec-phase resume, or fall back to
    # split-mode intent.md resolution for backwards compat.
    if not intent:
        if resume_without_intent:
            if resume_state.intent:
                intent = resume_state.intent
                display_intent = intent
            elif split and not no_qa:
                from otto.config import resolve_intent
                intent = (resolve_intent(project_dir) or "").strip()
                if not intent:
                    error_console.print(
                        "[error]Resume needs a product description for split mode. "
                        "Provide INTENT or create intent.md/README.md.[/error]"
                    )
                    sys.exit(2)
                    return
        else:
            error_console.print("[error]Intent cannot be empty. Provide a description of what to build.[/error]")
            sys.exit(2)

    display_intent = intent or display_intent
    print_resume_status(console, resume_state, resume, expected_command="build")

    config_path = project_dir / "otto.yaml"
    if not config_path.exists():
        create_config(project_dir)
        console.print("[yellow]First run \u2014 created otto.yaml[/yellow]")
        console.print()
    config = load_config(config_path)

    if no_qa:
        config["skip_product_qa"] = True
    if fast:
        config["_certifier_mode"] = "fast"
    if rounds is not None:
        config["max_certify_rounds"] = rounds

    from otto.pipeline import build_agentic_v3, run_certify_fix_loop, BuildResult
    from otto.budget import RunBudget
    budget = RunBudget.start_from(config)

    # --- Spec phase (if --spec or --spec-file) ---
    # Start timer BEFORE spec so reported duration matches budget accounting.
    build_start = time.time()
    spec_content: str | None = None
    run_id: str = resume_state.run_id or ""
    spec_cost_total: float = resume_state.spec_cost or 0.0
    if use_spec:
        try:
            run_id, spec_content, spec_cost_total = asyncio.run(_run_spec_phase(
                project_dir=project_dir,
                intent=intent,
                spec=spec,
                spec_file=spec_file,
                auto_approve=yes,
                resume_state=resume_state,
                config=config,
                budget=budget,
            ))
        except KeyboardInterrupt:
            console.print("\n  [yellow]Paused. Run `otto build --resume` to continue.[/yellow]")
            sys.exit(0)

    console.print()

    certifier_mode = config.pop("_certifier_mode", "thorough")

    try:
        if split and not no_qa:
            console.print("  [bold]Split mode[/bold] \u2014 system-controlled certify loop\n")
            # Resume skips the initial build only after it has completed.
            skip_initial_build = resume_state.resumed and initial_build_completed(
                resume_state.phase
            )
            result: BuildResult = asyncio.run(
                run_certify_fix_loop(intent, project_dir, config,
                                     certifier_mode=certifier_mode,
                                     skip_initial_build=skip_initial_build,
                                     start_round=resume_state.start_round,
                                     resume_cost=resume_state.total_cost,
                                     resume_rounds=resume_state.rounds,
                                     command="build",
                                     record_intent=not resume_without_intent,
                                     budget=budget)
            )
        else:
            mode_label = "fast smoke test" if certifier_mode == "fast" else "one agent builds, certifies, fixes"
            console.print(f"  [bold]Agentic mode[/bold] \u2014 {mode_label}\n")
            result: BuildResult = asyncio.run(
                build_agentic_v3(intent, project_dir, config,
                                 certifier_mode=certifier_mode,
                                 resume_session_id=resume_state.session_id or None,
                                 record_intent=not resume_without_intent,
                                 resume_existing_session=resume_without_intent,
                                 spec=spec_content,
                                 run_id=run_id or None,
                                 budget=budget,
                                 spec_cost=spec_cost_total)
            )
    except KeyboardInterrupt:
        console.print("\n  [yellow]Paused. Run `otto build --resume` to continue.[/yellow]")
        sys.exit(0)
    except Exception as e:
        error_console.print(f"[error]Build failed: {rich_escape(str(e))}[/error]")
        sys.exit(1)

    build_duration = time.time() - build_start
    _print_build_result(display_intent, result, build_duration)

    sys.exit(0 if result.passed else 1)


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("intent", required=False)
@click.option("--thorough", is_flag=True, help="Thorough mode — find what's broken, not just verify")
@click.option("--fast", is_flag=True, help="Fast mode — happy path smoke test only")
def certify(intent, thorough, fast):
    """Certify a product — independent, builder-blind verification.

    Tests the product in the current directory as a real user. Works on
    any project regardless of how it was built (otto, bare CC, human).

    If no intent is given, reads intent.md or README.md from the project.

    Examples:
        otto certify                   # reads intent.md
        otto certify --fast            # quick smoke test (~1-2 min)
        otto certify --thorough        # adversarial deep inspection
    """
    project_dir = Path.cwd()

    # Load config so certifier_timeout and other settings are respected
    config_path = project_dir / "otto.yaml"
    config = load_config(config_path) if config_path.exists() else {}

    # Resolve intent: argument > intent.md > README.md
    if not intent:
        from otto.config import resolve_intent
        intent = resolve_intent(project_dir)
        if intent:
            console.print("  [dim]Intent from project files[/dim]")
        else:
            error_console.print("[error]No intent provided. Pass as argument or create intent.md[/error]")
            sys.exit(2)

    if fast:
        mode_label = "fast smoke test"
        _mode = "fast"
    elif thorough:
        mode_label = "thorough inspection"
        _mode = "thorough"
    else:
        mode_label = "independent product verification"
        _mode = "standard"
    console.print(f"\n  [bold]Certifying[/bold] \u2014 {mode_label}\n")

    from otto.certifier import run_agentic_certifier
    from otto.budget import RunBudget
    budget = RunBudget.start_from(config)

    start = time.time()
    try:
        report = asyncio.run(run_agentic_certifier(
            intent=intent,
            project_dir=project_dir,
            config=config,
            mode=_mode,
            budget=budget,
        ))
    except KeyboardInterrupt:
        console.print("\n  Aborted.")
        sys.exit(1)
    except Exception as e:
        from otto.agent import AgentCallError
        if isinstance(e, AgentCallError):
            error_console.print(
                f"[error]Run budget exhausted ({e.reason}).[/error]\n"
                "  Raise `run_budget_seconds` in otto.yaml. Standalone "
                "certify has no resume — rerun the command."
            )
            sys.exit(1)
        error_console.print(f"[error]Certification failed: {rich_escape(str(e))}[/error]")
        sys.exit(1)

    duration = time.time() - start
    story_results = report.story_results
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

# Improve commands (registered from otto/cli_improve.py)
from otto.cli_improve import register_improve_commands
register_improve_commands(main)
