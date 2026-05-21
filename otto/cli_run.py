"""`otto run` — intent-to-product pipeline.

The canonical entry point: compile a flat spec from the intent, run the
root Lead, dispatch child tasks in parallel, merge to the integration
worktree, and emit a proof packet.

Usage:
    otto run "<intent>" [--provider claude] [--budget 3600] [--tier auto]
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click

from otto import paths as _paths
from otto.config import (
    ConfigError,
    load_config,
    require_git,
    resolve_project_dir,
)
from otto.defaults import DEFAULT_RUN_BUDGET_S, DEFAULT_TREE_BUDGET_USD
from otto.display import CONTEXT_SETTINGS, console
from otto.schemas import VERDICT_CATASTROPHIC
from otto.theme import error_console

logger = logging.getLogger("otto.cli_run")


def _color_verdict(verdict: str) -> str:
    """Color-coded verdict for terminal display."""
    colors = {
        "pass": "green",
        "partial": "yellow",
        "pending_children": "cyan",
        "unverified": "yellow",
        "merge_blocked": "red",
        "catastrophic": "bold red",
    }
    color = colors.get(verdict, "white")
    return f"[{color}]{verdict}[/{color}]"


def register_run_command(main: click.Group) -> None:
    """Register the canonical `otto run` command."""

    @main.command("run", context_settings=CONTEXT_SETTINGS)
    @click.argument("intent", required=True)
    @click.option(
        "--budget", type=int, default=DEFAULT_RUN_BUDGET_S, show_default=True,
        help="Wall-clock budget in seconds.",
    )
    @click.option("--provider", default="claude", show_default=True,
                  help="claude | codex-app-server | codex")
    @click.option("--model", default=None, help="Override model.")
    @click.option("--max-turns", type=int, default=200, show_default=True)
    @click.option(
        "--max-parallel", type=int, default=3, show_default=True,
        help="Max concurrent child tasks.",
    )
    @click.option(
        "--tree-budget-usd", type=float, default=DEFAULT_TREE_BUDGET_USD, show_default=True,
        help="Tree-level cost cap in USD (refuses new dispatches when hit).",
    )
    @click.option(
        "--phase1-only", is_flag=True,
        help="Phase 1 mode: run root Lead only, do not process children. "
             "For testing the Lead primitive in isolation.",
    )
    @click.option(
        "--review-first-decomp", is_flag=True,
        help="Pause after the root Lead emits children to allow user review "
             "via 'otto review' or MC. Sub-Leads' decompositions remain "
             "autonomous.",
    )
    @click.option(
        "--no-cache",
        is_flag=True,
        help="Disable spec compile cache for this run.",
    )
    @click.option(
        "--slice-context",
        is_flag=True,
        help="Opt in to conservative child context slicing. Default is full context.",
    )
    @click.option(
        "--full-context",
        is_flag=True,
        help="Disable child context slicing even if project config enables it.",
    )
    @click.option(
        "--tier",
        type=click.Choice(["auto", "solo", "lead", "modular"]),
        default="auto", show_default=True,
        help="Decomposition preset. solo=force inline; lead=allow subtasks; "
             "modular=require Architecture-first thinking; auto=Lead chooses.",
    )
    @click.option(
        "--fresh",
        is_flag=True,
        help="Force a fresh from-scratch run; refuse to resume even if a "
             "resumable checkpoint exists for this project. "
             "(Equivalent to v5_resume_from_checkpoint: false in otto.yaml.)",
    )
    def run_cmd(
        intent: str,
        budget: int,
        provider: str,
        model: str | None,
        max_turns: int,
        max_parallel: int,
        tree_budget_usd: float,
        phase1_only: bool,
        review_first_decomp: bool,
        no_cache: bool,
        slice_context: bool,
        full_context: bool,
        tier: str,
        fresh: bool,
    ) -> None:
        """Run a Lead session against the intent.

        Compiles a flat spec (intent + behavior_journeys), then runs the Lead.
        The Lead either calls begin_inline (and builds inline) or calls
        submit_subtask N times (the watcher picks up the subtasks).
        """
        require_git()
        try:
            project_dir = resolve_project_dir(Path.cwd())
        except ConfigError as exc:
            error_console.print(f"[error]{exc}[/error]")
            sys.exit(2)

        try:
            config = load_config(project_dir / "otto.yaml")
        except ConfigError as exc:
            error_console.print(f"[error]{exc}[/error]")
            sys.exit(2)

        # Apply CLI overrides into config so make_agent_options picks them up.
        if provider:
            config["provider"] = provider
            overrides = config.setdefault("_cli_overrides", {})
            if isinstance(overrides, dict):
                overrides["provider"] = provider
        if model:
            config["model"] = model
        if budget:
            config["run_budget_seconds"] = int(budget)
        if max_turns:
            config["max_turns_per_call"] = int(max_turns)

        console.print(f"[dim]project: {project_dir}[/dim]")
        console.print(f"[dim]provider: {provider}, model: {model or '(default)'}[/dim]")
        if phase1_only:
            console.print("[dim]phase1-only mode: root Lead only, no children processed[/dim]")
        console.print()

        if no_cache:
            config["spec_compile_no_cache"] = True
        if slice_context and full_context:
            error_console.print("[error]choose only one of --slice-context or --full-context[/error]")
            sys.exit(2)
        if slice_context:
            config["v5_context_slicing"] = True
        if full_context:
            config["v5_context_slicing"] = False
            config["v5_full_context"] = True

        if phase1_only:
            _run_phase1_only(project_dir, intent, config)
            return

        # Full pipeline: compile + root Lead + children + integration.
        from otto.v5_runner import run_v5_pipeline

        def _on_event(payload: dict) -> None:
            ev = payload.get("event", "?")
            if ev == "compile_done":
                console.print(
                    f"  [bold]compile[/bold] → {payload['journey_count']} journeys "
                    f"({payload['lint_warnings']} lint warnings)"
                )
            elif ev == "lead_start":
                console.print(f"  [bold]lead[/bold] → {payload['task_id']}")
            elif ev == "lead_done":
                console.print(
                    f"    {payload['task_id']}: "
                    f"verdict={_color_verdict(payload['verdict'])}, "
                    f"decomposition={payload['decomposition']}, "
                    f"emitted={payload.get('emitted', 0)}"
                )
            elif ev == "child_dispatch":
                console.print(f"    [dim]→ child {payload['task_id'][:18]}[/dim]")
            elif ev == "child_done":
                console.print(
                    f"    [dim]✓[/dim] child {payload['task_id'][:18]}: "
                    f"{_color_verdict(payload['verdict'])}"
                )
            elif ev == "child_crash":
                console.print(
                    f"    [red]✗[/red] child {payload['task_id'][:18]}: {payload['error'][:60]}"
                )
            elif ev == "integration_start":
                console.print(f"  [bold]integration[/bold] → {payload['task_id']}")
            elif ev == "integration_done":
                console.print(
                    f"    integration {payload['task_id']}: {_color_verdict(payload['verdict'])}"
                )
            elif ev == "budget_cap_hit":
                console.print(
                    f"  [yellow]⚠ budget cap hit:[/yellow] spent ${payload['spent']:.2f} "
                    f"of ${payload['cap']:.2f}; refusing new dispatches"
                )
            elif ev == "v5_resume_from_checkpoint":
                # A previously-failed run was resumed; the pipeline SKIPPED
                # compile + decompose + child rebuild and is re-entering
                # integration on the persisted task graph + per-child branches.
                # If prior repair packets were carried, the repair agent will
                # resume its prior SDK session (option.resume=agent_session_id)
                # instead of starting fresh — surface that too.
                carried = int(payload.get("repair_packets_carried", 0) or 0)
                carried_note = (
                    f"; carried {carried} prior repair packet(s) "
                    f"— agent will continue prior conversation"
                    if carried > 0 else ""
                )
                console.print(
                    f"  [bold cyan]♻ resumed from checkpoint[/bold cyan] "
                    f"({payload.get('emitted', 0)} child branches preserved; "
                    f"skipped compile + decompose + child builds → "
                    f"re-entering integration{carried_note})"
                )

        # Pass review-first-decomp + tier preset into the pipeline.
        if review_first_decomp:
            config["v5_review_first_decomp"] = True
        if fresh:
            # Explicit user override to refuse resume even when a resumable
            # checkpoint exists (partial / merge_blocked root) for this
            # project. Equivalent to setting v5_resume_from_checkpoint:
            # false in otto.yaml.
            config["v5_resume_from_checkpoint"] = False
        config["v5_tier"] = tier

        result = asyncio.run(run_v5_pipeline(
            project_dir=project_dir,
            intent=intent,
            config=config,
            max_parallel=max_parallel,
            tree_budget_usd=tree_budget_usd,
            on_event=_on_event,
        ))

        console.print()
        # Pull advisory findings from the integration verification_plan.json
        # so operators see doc-coherence / quality flags even on a `pass`.
        # The verifier-gate demote is now advisory (P0a fix); surfacing them
        # here closes the "operator reads pass and skips the json" gap.
        findings: dict[str, list[dict[str, str]]] = {
            "advisories": [], "gate_failures": [], "journey_failures": [],
        }
        try:
            if result.root_session_dir is not None:
                from otto.v5_verification_plan import load_advisory_findings
                findings = load_advisory_findings(result.root_session_dir)
        except Exception:  # noqa: BLE001 — never block the summary on a side-channel read
            pass
        advisory_count = len(findings["advisories"])
        gate_count = len(findings["gate_failures"])
        journey_count = len(findings["journey_failures"])
        # Audit F-5: text-search-derived advisories (page_has_ia_route,
        # entity_has_empty_state, action_has_test, page_resolves, etc.)
        # are doc-coherence quibbles, not behavior failures. With the
        # integration Lead's behavioral journey self-verify proven, these
        # don't deserve front-page CLI real estate on a passing run — the
        # detail is preserved in proof-packet.html / verification_plan.json
        # for operators who want it.
        bits: list[str] = []
        # ONLY surface advisories in the verdict suffix when something else
        # went wrong (so the operator knows to look). On a clean pass,
        # they're suppressed entirely.
        show_advisories_inline = result.verdict != "pass" or gate_count or journey_count
        if advisory_count and show_advisories_inline:
            bits.append(f"{advisory_count} advisory")
        if gate_count:
            bits.append(f"[red]{gate_count} gate failure[/red]")
        if journey_count:
            bits.append(f"[red]{journey_count} journey failure[/red]")
        if advisory_count and not show_advisories_inline:
            verdict_suffix = (
                f" ({advisory_count} advisory finding"
                f"{'s' if advisory_count != 1 else ''} in proof-packet.html)"
            )
        else:
            verdict_suffix = f" ({', '.join(bits)} — see proof-packet.html)" if bits else ""
        console.print(
            f"  [bold]Verdict:[/bold] {_color_verdict(result.verdict)}{verdict_suffix}"
        )
        console.print(f"  cost: ${result.total_cost_usd:.4f}")
        console.print(f"  duration: {result.duration_s:.1f}s")
        if result.failure_reason:
            console.print(f"  [yellow]reason:[/yellow] {result.failure_reason}")
        # If verdict was demoted, print the offending findings inline so the
        # operator can act without opening proof-packet.html. On a clean
        # pass, advisories live in proof-packet only.
        if gate_count or journey_count:
            console.print()
            console.print("  [bold red]Gate / journey failures (verdict demoted):[/bold red]")
            for f in findings["gate_failures"] + findings["journey_failures"]:
                console.print(
                    f"    [red]✗[/red] {f['kind']}/{f['id']}: {f['detail'][:140]}"
                )
            if advisory_count:
                console.print()
                console.print("  [bold yellow]Advisories (recorded, verdict NOT demoted):[/bold yellow]")
                for f in findings["advisories"]:
                    console.print(
                        f"    [yellow]·[/yellow] {f['kind']}/{f['id']}: {f['detail'][:140]}"
                    )

        # Exit code: 0 unless catastrophic (1). A `missing_toolchain` block is
        # an ENVIRONMENT failure, not a product defect or an Otto crash — give
        # it a DISTINCT non-success exit (3) so CI / callers can tell "fix your
        # host toolchain" apart from "Otto/product broke" (never reported as a
        # product merge_blocked success).
        if "missing_toolchain" in (result.failure_reason or ""):
            console.print(
                "  [yellow]environment:[/yellow] no usable uv / Python 3.12 "
                "toolchain on this host — not a product defect"
            )
            sys.exit(3)
        if result.verdict == VERDICT_CATASTROPHIC:
            sys.exit(1)


def _run_phase1_only(project_dir: Path, intent: str, config: dict) -> None:
    """Phase 1 single-Lead path (no children processed). For isolation testing."""
    # Phase-1 runs do not currently hold the project lock; the canonical
    # allocator still retries existing session-dir collisions.
    session_id = _paths.new_session_id(project_dir)
    session_dir = _paths.session_dir(project_dir, session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"[dim]session: {session_id}[/dim]")
    console.print()
    console.print("  [bold]Compile (flat)[/bold]")
    from otto.spec_compile_flat import compile_flat_spec

    try:
        spec = asyncio.run(compile_flat_spec(
            project_dir=project_dir,
            session_dir=session_dir,
            intent=intent,
            config=config,
        ))
    except Exception as exc:  # noqa: BLE001
        error_console.print(f"[error]flat spec compile failed: {exc}[/error]")
        sys.exit(1)

    console.print(f"  → {len(spec.behavior_journeys)} journeys "
                  f"({len(spec.lint_warnings)} lint warnings)")
    console.print()
    console.print("  [bold]Lead[/bold] — root only")
    from otto.lead import run_lead

    result = asyncio.run(run_lead(
        task_id="root",
        intent=intent,
        project_dir=project_dir,
        session_dir=session_dir,
        integration_branch=None,
        config=config,
        kind="plan_or_inline",
    ))
    console.print(f"  decomposition: {result.decomposition}")
    console.print(f"  verify called: {result.verify_called}")
    console.print(f"  [bold]Verdict:[/bold] {_color_verdict(result.verdict)}")
    if result.verdict == VERDICT_CATASTROPHIC:
        sys.exit(1)


__all__ = ["register_run_command"]
