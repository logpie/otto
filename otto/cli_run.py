"""`otto run` — intent-to-product pipeline driver.

Step 9 wired through to the full pipeline. Drives:

    intent  →  compile_spec()  →  spec.json
            →  run_build()      →  per-slice build agents + checks
            →  run_merge_queue() →  serial-FIFO merges
            →  run_audit()      →  end-of-run LLM audit + artifacts
            →  render_run()     →  proof-packet.html + proof-packet.json

`--no-build` keeps the original Phase A behaviour: compile only, then
exit. Useful for review-and-edit-before-build cycles.

Old commands (`otto build`, `otto certify`, `otto improve`) are
untouched — `otto run` is non-clobbering.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from otto import paths as _paths
from otto.audit import (
    AuditBudget,
    AuditResult,
    AuditVerdict,
    default_audit_agent,
    default_walkthrough_from_spec,
    run_audit,
)
from otto.build import (
    BuildBudget,
    BuildResult,
    default_build_agent,
    run_build,
)
from otto.config import (
    ConfigError,
    _normalize_intent,
    load_config,
    require_git,
    resolve_project_dir,
)
from otto.display import CONTEXT_SETTINGS, console, rich_escape
from otto.merge_queue import (
    MergeBudget,
    MergeQueueResult,
    run_merge_queue,
)
from otto.queue.runtime import mark_queue_child_ready
from otto.resume import (
    ResumeError,
    ResumePlan,
    plan_resume,
    recover_mid_merge_state_for_project,
    resolve_paused_session,
    verify_spec_hash_matches,
)
from otto.runner import RunResult, run_pipeline
from otto.spec_compile import (
    PROJECT_KINDS,
    Spec,
    SpecValidationError,
    compile_spec,
    load_spec,
)
from otto.spec_state import emit
from otto.theme import error_console

logger = logging.getLogger("otto.cli_run")


def _positive_budget_option(
    _ctx: click.Context,
    _param: click.Parameter,
    value: int | None,
) -> int | None:
    if value is not None and value <= 0:
        raise click.BadParameter("must be > 0")
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


def resolve_pipeline_choice(
    *,
    i2p_flag: bool,
    legacy_flag: bool,
    project_dir: Path,
) -> str:
    """Decide whether to dispatch to the new ``i2p`` stack or the legacy
    pipeline given user flags + the otto.yaml ``default_pipeline`` setting.

    Phase B.3 prep: this helper is the single source of truth for the
    dispatch decision. CLI commands consult it; the actual default flip
    happens by changing the otto.yaml default — no code change needed.

    Returns ``"i2p"`` or ``"legacy"``. Raises ``click.UsageError`` if both
    flags are passed (ambiguous).
    """
    if i2p_flag and legacy_flag:
        import click as _click
        raise _click.UsageError(
            "--i2p and --legacy are mutually exclusive — pass at most one."
        )
    if i2p_flag:
        return "i2p"
    if legacy_flag:
        return "legacy"
    # Neither flag — consult config.
    config_path = project_dir / "otto.yaml"
    try:
        config = load_config(config_path)
    except ConfigError:
        # Bad config; fall back to legacy (the safe default).
        return "legacy"
    raw = str(config.get("default_pipeline", "legacy")).strip().lower()
    return "i2p" if raw == "i2p" else "legacy"


def _resolve_intent_or_exit(intent: str | None, project_dir: Path) -> str:
    """Resolve intent from argument or project files, mirroring `otto certify`."""
    intent = _normalize_intent(intent or "")
    if intent:
        return intent
    from otto.config import resolve_intent
    try:
        resolved = _normalize_intent(resolve_intent(project_dir) or "")
    except (ConfigError, ValueError) as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(2)
    if not resolved:
        error_console.print(
            "[error]No intent provided. Pass as argument or create intent.md[/error]"
        )
        sys.exit(2)
    console.print("  [dim]Intent from project files[/dim]")
    return resolved


def _record_cli_override(
    config: dict[str, Any],
    key: str,
    value: Any,
    *,
    agent_type: str | None = None,
) -> None:
    overrides = config.setdefault("_cli_overrides", {})
    if not isinstance(overrides, dict):
        return
    if agent_type:
        agents = overrides.setdefault("agents", {})
        if isinstance(agents, dict):
            agent_overrides = agents.setdefault(agent_type, {})
            if isinstance(agent_overrides, dict):
                agent_overrides[key] = value
        return
    overrides[key] = value


def apply_i2p_cli_overrides(
    config: dict[str, Any],
    *,
    budget: int | None = None,
    max_turns: int | None = None,
    model: str | None = None,
    provider: str | None = None,
    effort: str | None = None,
    build_provider: str | None = None,
    build_model: str | None = None,
    build_effort: str | None = None,
    certifier_provider: str | None = None,
    certifier_model: str | None = None,
    certifier_effort: str | None = None,
    fix_provider: str | None = None,
    fix_model: str | None = None,
    fix_effort: str | None = None,
    verbose: bool = False,
    debug_unredacted: bool = False,
) -> None:
    """Apply CLI runtime overrides consumed by the i2p orchestrators."""
    if budget is not None:
        config["run_budget_seconds"] = budget
    if max_turns is not None:
        config["max_turns_per_call"] = max_turns
    if model:
        config["model"] = model
        _record_cli_override(config, "model", model)
    if provider:
        config["provider"] = provider
        _record_cli_override(config, "provider", provider)
    if effort:
        config["effort"] = effort
        _record_cli_override(config, "effort", effort)

    phase_values = {
        "build": {
            "provider": build_provider,
            "model": build_model,
            "effort": build_effort,
        },
        "certifier": {
            "provider": certifier_provider,
            "model": certifier_model,
            "effort": certifier_effort,
        },
        "fix": {
            "provider": fix_provider,
            "model": fix_model,
            "effort": fix_effort,
        },
    }
    for agent_type, overrides in phase_values.items():
        agent_config = config.setdefault("agents", {}).setdefault(agent_type, {})
        if not isinstance(agent_config, dict):
            continue
        for key, value in overrides.items():
            if not value:
                continue
            agent_config[key] = value
            _record_cli_override(config, key, value, agent_type=agent_type)

    if debug_unredacted:
        config["debug_unredacted"] = True
    config["_verbose"] = bool(verbose)


def _new_session_id(project_dir: Path) -> str:
    """Allocate a session id, honouring `OTTO_RUN_ID` for testability."""
    injected = os.environ.get("OTTO_RUN_ID", "").strip()
    if injected:
        return injected
    from otto.runs.registry import allocate_run_id
    return allocate_run_id(project_dir)


async def _run_compile_phase(
    *,
    project_dir: Path,
    intent: str,
    project_kind: str,
    session_id: str,
    config: dict[str, Any],
) -> tuple[Path, Spec]:
    """Run the compile agent. Returns (spec_path, spec)."""
    spec_dir = _paths.spec_dir(project_dir, session_id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    config = dict(config)
    config.setdefault("_intent_source", "cli-argument")
    config.setdefault("_spec_source", "compile-agent")
    spec = await compile_spec(
        intent,
        project_dir=project_dir,
        run_dir=spec_dir,
        config=config,
        project_kind=project_kind,
    )
    written = spec_dir / "spec.json"
    console.print(
        f"  [bold]Compile complete[/bold] — {len(spec.groups)} group(s), "
        f"project_kind={spec.project_kind}"
    )
    console.print(f"  Spec: {written}")
    # Surface validator warnings to the operator. The validator catches
    # under-specified slices (S1: vague tasks; S4: missing cross_slice
    # checks; S5: missing tasks/deps/owned_paths fields) but the warnings
    # used to be silently dropped after compile. They're now logged AND
    # printed so the operator can decide whether to amend or proceed.
    warnings = getattr(spec, "_validator_warnings", []) or []
    if warnings:
        console.print(
            f"  [yellow]Spec validator warnings ({len(warnings)}):[/yellow]"
        )
        for warning in warnings:
            console.print(f"    [yellow]·[/yellow] {warning}")
    return written, spec


async def _drive_full_pipeline(
    *,
    spec: Spec,
    project_dir: Path,
    session_dir: Path,
    base_url: str | None,
) -> tuple[BuildResult, MergeQueueResult, AuditResult]:
    """Drive build → merge → audit using the default LLM agents."""
    console.print()
    # C1 fix: ONE BuildBudget instance threaded across build, merge,
    # and audit phases so cost/repair-time accumulate into a single
    # pool. Without this, each phase's fresh budget could silently
    # exceed the documented "$30 total" ceiling because nobody owns
    # the shared accounting.
    shared_budget = BuildBudget()
    console.print("  [bold]Build phase[/bold] — dispatching group agents")
    build_result = await run_build(
        spec,
        project_dir=project_dir,
        session_dir=session_dir,
        build_agent=default_build_agent,
        base_url=base_url,
        budget=shared_budget,
    )
    console.print(
        f"  Build: {len(build_result.passing_ids)}/{len(build_result.group_results)} "
        f"groups passing, ${build_result.total_cost_usd:.2f}, "
        f"{build_result.total_wall_s:.0f}s"
    )
    if build_result.blocked_ids:
        console.print(
            f"  [yellow]Blocked groups:[/yellow] {', '.join(build_result.blocked_ids)}"
        )

    console.print()
    console.print("  [bold]Merge phase[/bold] — landing groups in dep order")
    merge_result = await run_merge_queue(
        spec,
        build_result,
        project_dir=project_dir,
        session_dir=session_dir,
        base_url=base_url,
        build_agent=default_build_agent,
        budget=MergeBudget(),
        shared_budget=shared_budget,
    )
    console.print(
        f"  Merge: {len(merge_result.landed_ids)} landed, "
        f"{len(merge_result.blocked_ids)} blocked, "
        f"${merge_result.total_cost_usd:.2f}"
    )

    console.print()
    console.print("  [bold]Audit phase[/bold] — final integrated review")
    audit_result = await run_audit(
        spec,
        project_dir=project_dir,
        session_dir=session_dir,
        build_result=build_result,
        merge_result=merge_result,
        audit_agent=default_audit_agent,
        base_url=base_url,
        walkthrough=default_walkthrough_from_spec(spec, base_url=base_url),
        fix_agent=default_build_agent,
        budget=AuditBudget(),
        shared_budget=shared_budget,
    )
    console.print(
        f"  Audit verdict: [bold]{audit_result.verdict.value}[/bold]; "
        f"${audit_result.cost_usd:.2f}, {audit_result.wall_s:.0f}s"
    )
    return build_result, merge_result, audit_result


def _emit_compile_only_message(spec_path: Path) -> None:
    """Print the message for `--no-build` mode."""
    console.print()
    console.print("  [yellow]Build, audit, and render skipped (--no-build)[/yellow]")
    console.print(f"  Compiled spec available at: {spec_path}")
    console.print("  Edit the spec, then re-run `otto run` to drive the full pipeline.")


def register_run_command(main: click.Group) -> None:
    """Register `otto run` on the main CLI group."""

    @main.command("run", context_settings=CONTEXT_SETTINGS)
    @click.argument("intent", required=False)
    @click.option(
        "--project-kind",
        type=click.Choice(PROJECT_KINDS),
        default="webapp",
        show_default=True,
        help="Project kind hint for the compile agent's structure schema.",
    )
    @click.option(
        "--break-lock",
        is_flag=True,
        help="Force-clear the project lock before starting.",
    )
    @click.option(
        "--no-build",
        is_flag=True,
        help="Stop after compile; do not run build/merge/audit/render.",
    )
    @click.option(
        "--base-url",
        type=str,
        default=None,
        help="Base URL for ApiProbe / StateInvariant http_get checks.",
    )
    @click.option(
        "--from-spec",
        type=click.Path(dir_okay=False, exists=True, path_type=Path),
        default=None,
        help="Drive the pipeline from an existing spec.json instead of compiling.",
    )
    @click.option(
        "--resume",
        is_flag=True,
        help="Resume the paused i2p session at otto_logs/paused.",
    )
    @click.option(
        "--reset-budget",
        "reset_budget",
        is_flag=True,
        help="On --resume, do NOT count the prior attempt's cost against the cap.",
    )
    @click.option(
        "--force",
        is_flag=True,
        help="On --resume, bypass the spec-hash check.",
    )
    @click.option(
        "--budget",
        default=None,
        type=int,
        callback=_positive_budget_option,
        help=(
            "Total wall-clock budget in seconds, must be > 0 "
            "(default from otto.yaml or 3600)."
        ),
    )
    @click.option(
        "--max-turns",
        default=None,
        type=int,
        callback=_max_turns_option,
        help="Max agent turns per call, 1-200 (default from otto.yaml or 200).",
    )
    @click.option("--model", default=None, help="Override model for every agent.")
    @click.option(
        "--provider",
        default=None,
        help="Override provider for every agent: claude | codex.",
    )
    @click.option(
        "--effort",
        default=None,
        help="Override effort level for every agent: low | medium | high | max.",
    )
    @click.option("--build-provider", default=None, help="Override provider for build agents.")
    @click.option("--build-model", default=None, help="Override model for build agents.")
    @click.option("--build-effort", default=None, help="Override effort for build agents.")
    @click.option("--certifier-provider", default=None, help="Override provider for certifier agents.")
    @click.option("--certifier-model", default=None, help="Override model for certifier agents.")
    @click.option("--certifier-effort", default=None, help="Override effort for certifier agents.")
    @click.option("--fix-provider", default=None, help="Override provider for fix agents.")
    @click.option("--fix-model", default=None, help="Override model for fix agents.")
    @click.option("--fix-effort", default=None, help="Override effort for fix agents.")
    @click.option("--verbose", is_flag=True, help="Show provider command details.")
    @click.option(
        "--debug-unredacted",
        is_flag=True,
        help=(
            "Also write unredacted raw logs under sessions/<id>/raw/ "
            "(do not share)."
        ),
    )
    @click.option(
        "--review-gate",
        "review_gate",
        is_flag=True,
        help=(
            "Pause after compile and wait for spec.review_approved before "
            "running build. Approve via Mission Control or by re-running "
            "with --resume --auto-approve. Opt-in (off by default to "
            "preserve CI/script automation)."
        ),
    )
    @click.option(
        "--auto-approve",
        "auto_approve",
        is_flag=True,
        help=(
            "Explicitly opt out of --review-gate; the build phase runs "
            "immediately after compile. This is the default behaviour — "
            "the flag exists so scripts can be unambiguous."
        ),
    )
    @click.option(
        "--gate-timeout",
        "gate_timeout_s",
        type=float,
        default=24 * 60 * 60.0,
        show_default=True,
        help="Wall-clock cap (seconds) on --review-gate before timing out.",
    )
    def run(
        intent: str | None,
        project_kind: str,
        break_lock: bool,
        no_build: bool,
        base_url: str | None,
        from_spec: Path | None,
        resume: bool,
        reset_budget: bool,
        force: bool,
        budget: int | None,
        max_turns: int | None,
        model: str | None,
        provider: str | None,
        effort: str | None,
        build_provider: str | None,
        build_model: str | None,
        build_effort: str | None,
        certifier_provider: str | None,
        certifier_model: str | None,
        certifier_effort: str | None,
        fix_provider: str | None,
        fix_model: str | None,
        fix_effort: str | None,
        verbose: bool,
        debug_unredacted: bool,
        review_gate: bool,
        auto_approve: bool,
        gate_timeout_s: float,
    ) -> None:
        """Run the intent-to-product pipeline.

        With no flags: compile, then drive build → merge → audit → render
        end-to-end against the integrated worktree.

        Examples:
            otto run "a bookmark manager"
            otto run --provider codex --budget 3600 "a bookmark manager"
            otto run --project-kind cli "a small linter"
            otto run --no-build "review-only mode"
            otto run --from-spec otto_logs/sessions/x/spec/spec.json
        """
        require_git()
        project_dir = resolve_project_dir(Path.cwd())
        if review_gate and auto_approve:
            raise click.UsageError(
                "--review-gate and --auto-approve are mutually exclusive."
            )
        orchestrate_run(
            intent=intent,
            project_kind=project_kind,
            break_lock=break_lock,
            no_build=no_build,
            base_url=base_url,
            from_spec=from_spec,
            project_dir=project_dir,
            resume=resume,
            reset_budget=reset_budget,
            force=force,
            review_gate=review_gate,
            gate_timeout_s=gate_timeout_s,
            budget=budget,
            max_turns=max_turns,
            model=model,
            provider=provider,
            effort=effort,
            build_provider=build_provider,
            build_model=build_model,
            build_effort=build_effort,
            certifier_provider=certifier_provider,
            certifier_model=certifier_model,
            certifier_effort=certifier_effort,
            fix_provider=fix_provider,
            fix_model=fix_model,
            fix_effort=fix_effort,
            verbose=verbose,
            debug_unredacted=debug_unredacted,
        )


_PHASE_HEADINGS = {
    "compile": "  [bold]Compile phase[/bold] — running compile agent",
    "seed": "  [bold]Seed phase[/bold] — applying audit fixtures",
    "build": "  [bold]Build phase[/bold] — dispatching group agents",
    "merge": "  [bold]Merge phase[/bold] — landing groups in dep order",
    "audit": "  [bold]Audit phase[/bold] — final integrated review",
    "repair": "  [bold]Repair phase[/bold] — Layer 2 feature retry",
    "render": "  [bold]Render phase[/bold] — assembling proof packet",
}


def _default_gate_announce(session_id: str) -> None:
    """Emit operator-facing instructions when the review gate engages.

    Mission Control's spec-review surface lives at
    ``/runs/<session_id>/spec/review``. The default port (8765) is the
    one ``otto web``/``otto mc`` bind to; we honour ``OTTO_WEB_PORT``
    so contributors running on a custom port still see the right URL.
    """
    port = os.environ.get("OTTO_WEB_PORT", "8765").strip() or "8765"
    url = f"http://localhost:{port}/runs/{session_id}/spec/review"
    console.print()
    console.print("  [bold yellow]Spec review gate active.[/bold yellow]")
    console.print(f"  Open Mission Control at: [bold]{url}[/bold]")
    console.print(
        "  Edit and approve, or run: [dim]otto run --resume --auto-approve[/dim]"
    )
    console.print("  Waiting for approval (Ctrl-C to cancel)...")


def _default_phase_callback(phase: str) -> None:
    """Print a heading when the runner enters a new phase.

    Bridges ``runner.run_pipeline``'s ``on_phase`` callback into
    user-facing ``console`` output.
    """
    heading = _PHASE_HEADINGS.get(phase)
    if heading is None:
        return
    console.print()
    console.print(heading)


def _prepare_resume_plan(
    *,
    project_dir: Path,
    force: bool,
    reset_budget: bool,
) -> tuple[ResumePlan, Path, Spec, str]:
    """Resolve the paused session and produce a ready-to-use ResumePlan.

    Returns ``(plan, session_dir, spec, intent_text)``. Calls
    ``sys.exit(...)`` if no paused session exists or the spec-hash
    check fails without ``--force``.

    ``reset_budget`` is honoured here: it returns a plan with
    ``prior_cost_usd`` zeroed so the runner does not pre-charge the
    shared BuildBudget. We deliberately leave wall_s alone — it has
    no effect on enforcement, only on the surfaced summary.
    """
    paused_dir = resolve_paused_session(project_dir)
    if paused_dir is None:
        error_console.print(
            "[error]No paused session found. "
            "`otto run --resume` needs an in-flight session — "
            "check `otto history` or `otto_logs/paused`.[/error]"
        )
        sys.exit(1)
    try:
        plan = plan_resume(paused_dir, project_dir=project_dir)
    except ResumeError as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(1)
    spec_path = paused_dir / "spec" / "spec.json"
    if not force:
        try:
            verify_spec_hash_matches(plan, spec_path)
        except ResumeError as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(1)
    else:
        try:
            emit(
                paused_dir,
                "spec.regenerated",
                detail="resume --force: spec hash check bypassed",
            )
        except Exception:
            pass
    try:
        spec = load_spec(spec_path)
    except SpecValidationError as exc:
        error_console.print(
            f"[error]Failed to load resumed spec: {exc}[/error]"
        )
        sys.exit(1)
    intent_text = spec.intent
    if reset_budget and plan.prior_cost_usd > 0.0:
        # Honour --reset-budget by zeroing prior cost. We don't mutate
        # the original plan (frozen dataclass); rebuild a copy.
        from dataclasses import replace as _dc_replace
        plan = _dc_replace(plan, prior_cost_usd=0.0)
    return plan, paused_dir, spec, intent_text


def _print_resume_banner(plan: ResumePlan) -> None:
    """Print a one-paragraph resume summary so the operator sees what's
    being skipped + what's pending."""
    landed = len(plan.landed_components)
    pending = len(plan.pending_components)
    audit_state = (
        f"audit verdict={plan.audit_verdict}" if plan.audit_finished else "audit pending"
    )
    console.print(
        f"  [bold]Resuming session {plan.session_id}[/bold]: "
        f"{landed} landed, {pending} pending; {audit_state}; "
        f"prior cost ${plan.prior_cost_usd:.2f}"
    )
    if plan.unreconciled_landed_ids:
        console.print(
            "  [yellow]Unreconciled LANDED ids (journal claims missing "
            f"from git): {', '.join(plan.unreconciled_landed_ids)}[/yellow]"
        )
    if plan.halted_reason:
        console.print(f"  [dim]Prior halt reason: {plan.halted_reason}[/dim]")


def orchestrate_run(
    *,
    intent: str | None,
    project_kind: str,
    break_lock: bool,
    no_build: bool,
    base_url: str | None,
    from_spec: Path | None,
    project_dir: Path,
    resume: bool = False,
    reset_budget: bool = False,
    force: bool = False,
    review_gate: bool = False,
    gate_timeout_s: float = 24 * 60 * 60.0,
    budget: int | None = None,
    max_turns: int | None = None,
    model: str | None = None,
    provider: str | None = None,
    effort: str | None = None,
    build_provider: str | None = None,
    build_model: str | None = None,
    build_effort: str | None = None,
    certifier_provider: str | None = None,
    certifier_model: str | None = None,
    certifier_effort: str | None = None,
    fix_provider: str | None = None,
    fix_model: str | None = None,
    fix_effort: str | None = None,
    verbose: bool = False,
    debug_unredacted: bool = False,
) -> None:
    """Drive the intent-to-product pipeline.

    A1.6 refactor: this function owns CLI concerns (lock acquisition,
    intent resolution, console output, exit codes, ``--from-spec``,
    ``--no-build``) and delegates the compile → seed → build → merge →
    audit → repair → render chain to ``otto.runner.run_pipeline``.
    Calls ``sys.exit(...)`` on completion to mirror the click handler's
    contract — callers should not expect a return value.

    ``resume`` reuses the paused session's spec + journal — incompatible
    with positional intent and ``--from-spec`` (which both imply a
    fresh-spec semantic).
    """
    if resume:
        if intent or from_spec is not None:
            error_console.print(
                "[error]--resume cannot be combined with a positional intent "
                "or --from-spec; resume always reuses the paused session's "
                "spec.[/error]"
            )
            sys.exit(2)
        if no_build:
            error_console.print(
                "[error]--resume implies running build; --no-build is "
                "incompatible.[/error]"
            )
            sys.exit(2)
    resume_plan: ResumePlan | None = None
    if resume:
        # Resume path: the paused session is the spec source. We
        # bypass compile entirely (re-using spec.json from the prior
        # session) and reuse its session_dir so logs accumulate.
        resume_plan, session_dir, spec, intent_text = _prepare_resume_plan(
            project_dir=project_dir,
            force=force,
            reset_budget=reset_budget,
        )
        session_id = session_dir.name
        spec_path = session_dir / "spec" / "spec.json"
        config_path = project_dir / "otto.yaml"
        try:
            config = load_config(config_path)
        except ConfigError as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(2)
        apply_i2p_cli_overrides(
            config,
            budget=budget,
            max_turns=max_turns,
            model=model,
            provider=provider,
            effort=effort,
            build_provider=build_provider,
            build_model=build_model,
            build_effort=build_effort,
            certifier_provider=certifier_provider,
            certifier_model=certifier_model,
            certifier_effort=certifier_effort,
            fix_provider=fix_provider,
            fix_model=fix_model,
            fix_effort=fix_effort,
            verbose=verbose,
            debug_unredacted=debug_unredacted,
        )
        compiled_inline = True
        # Mid-merge git recovery before we start dispatching anything.
        rec = recover_mid_merge_state_for_project(project_dir)
        if rec.cleaned:
            console.print(
                f"  [yellow]Recovered stuck git state ({rec.kind}) — "
                f"{rec.detail}[/yellow]"
            )
        _print_resume_banner(resume_plan)
        console.print(
            f"  [bold]otto run --resume[/bold] — session {session_id}\n"
        )
    elif from_spec is not None:
        try:
            spec = load_spec(from_spec)
        except SpecValidationError as exc:
            error_console.print(f"[error]Failed to load spec: {exc}[/error]")
            sys.exit(2)
        spec_path = from_spec.resolve()
        session_id = _resolve_session_from_spec_path(spec_path)
        session_dir = spec_path.parent.parent  # spec/ → session/
        console.print(f"  [bold]otto run[/bold] — driving from {spec_path}")
        intent_text = spec.intent
        config: dict[str, Any] = {}
        compiled_inline = True  # caller already produced the spec
    else:
        intent_text = _resolve_intent_or_exit(intent, project_dir)
        spec = None
        spec_path = None
        compiled_inline = False

        config_path = project_dir / "otto.yaml"
        try:
            config = load_config(config_path)
        except ConfigError as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(2)
        apply_i2p_cli_overrides(
            config,
            budget=budget,
            max_turns=max_turns,
            model=model,
            provider=provider,
            effort=effort,
            build_provider=build_provider,
            build_model=build_model,
            build_effort=build_effort,
            certifier_provider=certifier_provider,
            certifier_model=certifier_model,
            certifier_effort=certifier_effort,
            fix_provider=fix_provider,
            fix_model=fix_model,
            fix_effort=fix_effort,
            verbose=verbose,
            debug_unredacted=debug_unredacted,
        )

        # When `--no-build` is set, we still need the compile to run inside
        # the lock and exit early with the spec path. Keep that behaviour
        # by handling compile here and short-circuiting before run_pipeline.
        if no_build:
            try:
                with _paths.project_lock(project_dir, "run", break_lock=break_lock):
                    session_id = _new_session_id(project_dir)
                    session_dir = _paths.session_dir(project_dir, session_id)
                    console.print(
                        f"  [bold]otto run[/bold] — session {session_id}\n"
                    )
                    spec_path, spec = asyncio.run(
                        _run_compile_phase(
                            project_dir=project_dir,
                            intent=intent_text,
                            project_kind=project_kind,
                            session_id=session_id,
                            config=config,
                        )
                    )
            except _paths.LockBreakError as exc:
                error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
                sys.exit(1)
            except _paths.LockBusy as exc:
                error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
                sys.exit(1)
            except SpecValidationError as exc:
                error_console.print(f"[error]Spec compile failed:[/error]\n{exc}")
                sys.exit(1)
            _emit_compile_only_message(spec_path)
            sys.exit(0)

    # Full pipeline path: compile inside the lock (with user-facing
    # console output), then delegate the rest of the chain to
    # ``runner.run_pipeline`` via the ``spec=`` short-circuit.
    if not compiled_inline:
        try:
            with _paths.project_lock(project_dir, "run", break_lock=break_lock):
                session_id = _new_session_id(project_dir)
                session_dir = _paths.session_dir(project_dir, session_id)
                console.print(f"  [bold]otto run[/bold] — session {session_id}\n")
                spec_path, spec = asyncio.run(
                    _run_compile_phase(
                        project_dir=project_dir,
                        intent=intent_text,
                        project_kind=project_kind,
                        session_id=session_id,
                        config=config,
                    )
                )
        except _paths.LockBreakError as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(1)
        except _paths.LockBusy as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(1)
        except SpecValidationError as exc:
            error_console.print(f"[error]Spec compile failed:[/error]\n{exc}")
            sys.exit(1)

    mark_queue_child_ready(
        project_dir,
        run_id=session_id,
        phase="i2p",
        session_dir=session_dir,
        checkpoint_path=session_dir / "checkpoint.json",
    )

    # Drive seed → build → merge → audit → repair → render via runner.
    try:
        run_result = asyncio.run(
            run_pipeline(
                intent_text,
                project_dir,
                session_dir,
                project_kind=project_kind,
                brownfield=False,
                base_url=base_url,
                config=config,
                build_agent=default_build_agent,
                audit_agent=default_audit_agent,
                fix_agent=default_build_agent,
                spec=spec,
                on_phase=_default_phase_callback,
                resume_plan=resume_plan,
                review_gate=review_gate,
                gate_timeout_s=gate_timeout_s,
                gate_announce=_default_gate_announce,
                command="build",
            )
        )
    except Exception as exc:
        error_console.print(
            f"[error]Pipeline crashed: {type(exc).__name__}: {exc}[/error]"
        )
        try:
            emit(
                session_dir,
                "run.finished",
                detail=str(exc),
                verdict="blocked",
            )
        except Exception:
            pass
        sys.exit(1)

    _persist_session_summary(
        project_dir=project_dir,
        session_dir=session_dir,
        intent_text=intent_text,
        command="build",
        project_kind=project_kind,
        run_result=run_result,
    )

    _print_run_result(run_result)

    # Exit code reflects the verdict: passed → 0, partial/blocked → 1.
    if run_result.verdict == AuditVerdict.PASSED:
        sys.exit(0)
    sys.exit(1)


def _print_run_result(run_result: RunResult) -> None:
    """Console summary of a completed pipeline run."""
    spec = run_result.spec
    if run_result.build_result is not None:
        br = run_result.build_result
        console.print(
            f"  Build: {len(br.passing_ids)}/{len(br.group_results)} "
            f"groups passing, ${br.total_cost_usd:.2f}, "
            f"{br.total_wall_s:.0f}s"
        )
        if br.blocked_ids:
            console.print(
                f"  [yellow]Blocked groups:[/yellow] {', '.join(br.blocked_ids)}"
            )
    if run_result.merge_result is not None:
        mr = run_result.merge_result
        console.print(
            f"  Merge: {len(mr.landed_ids)} landed, "
            f"{len(mr.blocked_ids)} blocked, "
            f"${mr.total_cost_usd:.2f}"
        )
    if run_result.audit_result is not None:
        ar = run_result.audit_result
        console.print(
            f"  Audit verdict: [bold]{ar.verdict.value}[/bold]; "
            f"${ar.cost_usd:.2f}, {ar.wall_s:.0f}s"
        )
    if run_result.html_path is not None and run_result.json_path is not None:
        # Render-phase heading was already emitted by the on_phase callback.
        console.print(f"  Proof: {run_result.html_path}")
        console.print(f"        {run_result.json_path}")
    if run_result.halted_reason:
        console.print(
            f"  [yellow]Run halted:[/yellow] {run_result.halted_reason}"
        )
    _ = spec  # kept for symmetry / future verdict-by-feature surface


def _persist_session_summary(
    *,
    project_dir: Path,
    session_dir: Path,
    intent_text: str,
    command: str,
    project_kind: str,
    run_result: RunResult,
) -> None:
    """Emit the canonical ``summary.json`` for a completed i2p session.

    The runner is intentionally headless and only writes the proof
    packet. ``summary.json`` is the cross-session contract consumed by
    ``otto/resume.py:_read_prior_accounting``, ``otto/runs/history.py``,
    ``otto/runs/atomic_repair.py``, and Mission Control. Write failures
    are non-fatal: the run already succeeded, so we log + carry on
    rather than letting a bookkeeping crash mask the verdict.
    """
    from otto.runs.lifecycle import _write_session_summary

    audit = run_result.audit_result
    if audit is not None:
        feature_audits = list(audit.feature_audits)
        stories_tested = len(feature_audits)
        stories_passed = sum(
            1 for fa in feature_audits if fa.status == "passed"
        )
        rounds = int(audit.retries) + 1 if stories_tested else 0
        verdict_str = audit.verdict.value
        passed = audit.verdict == AuditVerdict.PASSED
    else:
        # Halted before audit (e.g. seed_failed).
        stories_tested = 0
        stories_passed = 0
        rounds = 0
        verdict_str = run_result.verdict.value
        passed = False

    status = "halted" if run_result.halted_reason else "completed"

    try:
        _write_session_summary(
            project_dir,
            session_dir.name,
            verdict=verdict_str,
            passed=passed,
            cost=float(run_result.cost_usd),
            duration=float(run_result.wall_s),
            stories_passed=stories_passed,
            stories_tested=stories_tested,
            rounds=rounds,
            status=status,
            intent=intent_text,
            command=command,
        )
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not mask verdict
        logger.warning(
            "session summary write failed for %s: %s: %s",
            session_dir,
            type(exc).__name__,
            exc,
        )
    _ = project_kind  # reserved for future summary fields


def _brownfield_compile_locked(
    *,
    intent_text: str,
    project_kind: str,
    config: dict[str, Any],
    project_dir: Path,
    lock_label: str,
    cli_heading: str,
    break_lock: bool,
    brownfield_mode: str = "baseline",
) -> tuple[Path, Spec]:
    """Acquire the project lock, allocate a session, brownfield-compile a spec.

    Shared between ``orchestrate_certify`` and ``orchestrate_improve``:
    they need identical lock + session-id + brownfield-compile plumbing.
    The caller then drives audit/render via ``run_pipeline(brownfield=True,
    spec=spec)`` outside the lock.

    Calls ``sys.exit(...)`` on lock or compile failures, mirroring
    ``orchestrate_run``'s contract.

    Returns ``(session_dir, spec)``.
    """
    try:
        with _paths.project_lock(project_dir, lock_label, break_lock=break_lock):
            session_id = _new_session_id(project_dir)
            session_dir = _paths.session_dir(project_dir, session_id)
            console.print(f"  [bold]{cli_heading}[/bold] — session {session_id}\n")
            mode_label = "target" if brownfield_mode == "target" else "baseline"
            console.print(
                f"  [bold]Compile phase[/bold] — brownfield {mode_label} spec"
            )
            run_dir = session_dir / "spec"
            run_dir.mkdir(parents=True, exist_ok=True)
            try:
                spec = asyncio.run(
                    compile_spec(
                        intent_text,
                        project_dir,
                        run_dir,
                        config,
                        project_kind=project_kind,
                        brownfield=True,
                        brownfield_mode=brownfield_mode,
                    )
                )
            except SpecValidationError as exc:
                error_console.print(
                    f"[error]Brownfield compile failed:[/error]\n{exc}"
                )
                sys.exit(1)
    except _paths.LockBreakError as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(1)
    except _paths.LockBusy as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(1)
    return session_dir, spec


def _drive_brownfield_pipeline(
    *,
    intent_text: str,
    project_kind: str,
    project_dir: Path,
    session_dir: Path,
    spec: Spec,
    config: dict[str, Any],
    fix_agent: Any,
    audit_budget: AuditBudget | None,
    on_phase: "Callable[[str], None]",
    resume_plan: ResumePlan | None = None,
    command: str = "certify",
) -> None:
    """Run audit + render via ``runner.run_pipeline(brownfield=True, spec=spec)``.

    Wraps the ``orchestrate_certify`` / ``orchestrate_improve`` tail.
    Crashes are caught and surfaced as ``run.finished`` blocked emits;
    sys.exit reflects the verdict.
    """
    try:
        run_result = asyncio.run(
            run_pipeline(
                intent_text,
                project_dir,
                session_dir,
                project_kind=project_kind,
                brownfield=True,
                config=config,
                build_agent=default_build_agent,
                audit_agent=default_audit_agent,
                fix_agent=fix_agent,
                spec=spec,
                audit_budget=audit_budget,
                on_phase=on_phase,
                resume_plan=resume_plan,
                command=command,
            )
        )
    except Exception as exc:
        error_console.print(
            f"[error]Pipeline crashed: {type(exc).__name__}: {exc}[/error]"
        )
        try:
            emit(
                session_dir,
                "run.finished",
                detail=str(exc),
                verdict="blocked",
            )
        except Exception:
            pass
        sys.exit(1)

    _persist_session_summary(
        project_dir=project_dir,
        session_dir=session_dir,
        intent_text=intent_text,
        command=command,
        project_kind=project_kind,
        run_result=run_result,
    )

    _print_run_result(run_result)

    if run_result.verdict == AuditVerdict.PASSED:
        sys.exit(0)
    sys.exit(1)


def _make_phase_callback(overrides: dict[str, str]) -> "Callable[[str], None]":
    """Build a phase-callback that overrides specific phase headings.

    Falls back to ``_PHASE_HEADINGS`` for unspecified phases. Used by
    certify/improve to keep their slightly different audit headings
    while still letting the runner own phase ordering.
    """

    def _cb(phase: str) -> None:
        heading = overrides.get(phase, _PHASE_HEADINGS.get(phase))
        if heading is None:
            return
        console.print()
        console.print(heading)

    return _cb


def orchestrate_improve(
    *,
    intent: str | None,
    project_kind: str,
    break_lock: bool,
    project_dir: Path,
    rounds: int | None = None,
    focus: str | None = None,
    budget: int | None = None,
    max_turns: int | None = None,
    model: str | None = None,
    provider: str | None = None,
    effort: str | None = None,
    build_provider: str | None = None,
    build_model: str | None = None,
    build_effort: str | None = None,
    certifier_provider: str | None = None,
    certifier_model: str | None = None,
    certifier_effort: str | None = None,
    fix_provider: str | None = None,
    fix_model: str | None = None,
    fix_effort: str | None = None,
    verbose: bool = False,
    debug_unredacted: bool = False,
) -> None:
    """Drive the new-stack `otto improve` flow (Phase B.2).

    Brownfield-compiles a baseline Spec (preserving `focus` as scope hint),
    then delegates audit → repair → render to ``runner.run_pipeline``
    (with ``brownfield=True`` to skip build/merge and
    ``fix_agent=default_build_agent`` to enable the repair loop —
    the load-bearing difference from certify mode). ``rounds`` maps to
    ``AuditBudget.audit_retries``.
    """
    intent_text = _resolve_intent_or_exit(intent, project_dir)
    if focus and focus not in intent_text:
        # Surface the focus as a scope hint to the compile agent.
        intent_text = f"{intent_text}\n\nFocus: {focus}"

    config_path = project_dir / "otto.yaml"
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(2)
    apply_i2p_cli_overrides(
        config,
        budget=budget,
        max_turns=max_turns,
        model=model,
        provider=provider,
        effort=effort,
        build_provider=build_provider,
        build_model=build_model,
        build_effort=build_effort,
        certifier_provider=certifier_provider,
        certifier_model=certifier_model,
        certifier_effort=certifier_effort,
        fix_provider=fix_provider,
        fix_model=fix_model,
        fix_effort=fix_effort,
        verbose=verbose,
        debug_unredacted=debug_unredacted,
    )

    session_dir, spec = _brownfield_compile_locked(
        intent_text=intent_text,
        project_kind=project_kind,
        config=config,
        project_dir=project_dir,
        lock_label="improve",
        cli_heading="otto improve --i2p",
        break_lock=break_lock,
        brownfield_mode="target",
    )

    # --rounds maps to AuditBudget.audit_retries. None = library default.
    audit_budget = AuditBudget(audit_retries=rounds) if rounds else AuditBudget()

    # `run_pipeline(spec=...)` skips the compile phase entirely, so we
    # only need to override the audit heading. Improve mode runs the
    # multi-round repair loop inside run_audit.
    on_phase = _make_phase_callback({
        "audit": (
            "  [bold]Audit + repair phase[/bold] "
            "— multi-round judge → fix loop"
        ),
    })

    _drive_brownfield_pipeline(
        intent_text=intent_text,
        project_kind=project_kind,
        project_dir=project_dir,
        session_dir=session_dir,
        spec=spec,
        config=config,
        fix_agent=default_build_agent,  # KEY: enables repair loop
        audit_budget=audit_budget,
        on_phase=on_phase,
        command="improve",
    )


def orchestrate_certify(
    *,
    intent: str | None,
    project_kind: str,
    break_lock: bool,
    project_dir: Path,
    resume: bool = False,
    reset_budget: bool = False,
    force: bool = False,
    budget: int | None = None,
    max_turns: int | None = None,
    model: str | None = None,
    provider: str | None = None,
    effort: str | None = None,
    certifier_provider: str | None = None,
    certifier_model: str | None = None,
    certifier_effort: str | None = None,
    verbose: bool = False,
    debug_unredacted: bool = False,
) -> None:
    """Drive the new-stack `otto certify` flow (Phase B.1).

    Brownfield-compiles a baseline Spec, then delegates audit → render to
    ``runner.run_pipeline`` (with ``brownfield=True`` to skip build/merge
    and ``fix_agent=None`` to disable the repair loop — certify just
    judges the project AS-IS). Calls ``sys.exit(...)`` on completion
    mirroring ``orchestrate_run``'s contract.

    Differences from ``orchestrate_run``:
      * Spec source is the existing project (brownfield compile), not a
        greenfield agent emit.
      * No build / merge phases — the project IS the integrated worktree.
      * ``fix_agent=None`` — nothing to repair (no build agent ran).

    ``resume`` reuses the paused session's brownfield spec; otherwise we
    re-compile fresh against the current project state.
    """
    resume_plan: ResumePlan | None = None
    if resume:
        if intent:
            error_console.print(
                "[error]--resume cannot be combined with a positional intent.[/error]"
            )
            sys.exit(2)
        resume_plan, session_dir, spec, intent_text = _prepare_resume_plan(
            project_dir=project_dir,
            force=force,
            reset_budget=reset_budget,
        )
        config_path = project_dir / "otto.yaml"
        try:
            config = load_config(config_path)
        except ConfigError as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(2)
        apply_i2p_cli_overrides(
            config,
            budget=budget,
            max_turns=max_turns,
            model=model,
            provider=provider,
            effort=effort,
            certifier_provider=certifier_provider,
            certifier_model=certifier_model,
            certifier_effort=certifier_effort,
            verbose=verbose,
            debug_unredacted=debug_unredacted,
        )
        _print_resume_banner(resume_plan)
        on_phase = _make_phase_callback({
            "compile": "",
            "audit": "  [bold]Audit phase[/bold] — judging the existing project",
        })
        _drive_brownfield_pipeline(
            intent_text=intent_text,
            project_kind=project_kind,
            project_dir=project_dir,
            session_dir=session_dir,
            spec=spec,
            config=config,
            fix_agent=None,
            audit_budget=None,
            on_phase=on_phase,
            resume_plan=resume_plan,
            command="certify",
        )
        return  # _drive_brownfield_pipeline sys.exit's
    intent_text = _resolve_intent_or_exit(intent, project_dir)

    config_path = project_dir / "otto.yaml"
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(2)
    apply_i2p_cli_overrides(
        config,
        budget=budget,
        max_turns=max_turns,
        model=model,
        provider=provider,
        effort=effort,
        certifier_provider=certifier_provider,
        certifier_model=certifier_model,
        certifier_effort=certifier_effort,
        verbose=verbose,
        debug_unredacted=debug_unredacted,
    )

    session_dir, spec = _brownfield_compile_locked(
        intent_text=intent_text,
        project_kind=project_kind,
        config=config,
        project_dir=project_dir,
        lock_label="certify",
        cli_heading="otto certify --i2p",
        break_lock=break_lock,
    )

    on_phase = _make_phase_callback({
        # Compile already emitted by _brownfield_compile_locked.
        "compile": "",
        # Certify mode: just judge the existing project.
        "audit": "  [bold]Audit phase[/bold] — judging the existing project",
    })

    _drive_brownfield_pipeline(
        intent_text=intent_text,
        project_kind=project_kind,
        project_dir=project_dir,
        session_dir=session_dir,
        spec=spec,
        config=config,
        fix_agent=None,  # certify mode: no repair loop
        audit_budget=None,
        on_phase=on_phase,
        command="certify",
    )


def _resolve_session_from_spec_path(spec_path: Path) -> str:
    """Extract the session id from an `otto_logs/sessions/<id>/spec/spec.json` path."""
    try:
        return spec_path.parent.parent.name
    except Exception:
        return spec_path.parent.name
