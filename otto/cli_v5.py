"""`otto v5` command — Phase 1 entry point for the v5 Lead pipeline.

Phase 1 scope: invoke a single Lead session against an intent. The Lead may
emit subtasks (recorded in v5_pending.jsonl for Phase 2's watcher) or run
inline. After the Lead returns, render a minimal proof.

Phase 1 does NOT yet:
  - Spawn child tasks (Phase 2).
  - Run integration Leads (Phase 2).
  - Show a tree view in MC (Phase 3).

Usage:
    otto v5 run "<intent>" [--provider claude] [--budget 3600]
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

import click

from otto.config import (
    ConfigError,
    load_config,
    require_git,
    resolve_project_dir,
)
from otto.display import CONTEXT_SETTINGS, console
from otto.theme import error_console

logger = logging.getLogger("otto.cli_v5")


def _new_session_id() -> str:
    """Generate a fresh session id of the form `2026-05-09-HHMMSS-xxxxxx`."""
    import uuid

    return time.strftime("%Y-%m-%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:6]


def register_v5_command(main: click.Group) -> None:
    """Register the `otto v5` command group."""

    @main.group("v5", context_settings=CONTEXT_SETTINGS)
    def v5_group() -> None:
        """v5 Lead-driven pipeline (Phase 1: single Lead per task)."""

    _register_review_commands(v5_group)
    _register_status_command(v5_group)
    _register_reset_verdict_command(v5_group)
    _register_retry_children_command(v5_group)
    _register_plan_resume_command(v5_group)

    @v5_group.command("run")
    @click.argument("intent", required=True)
    @click.option(
        "--budget", type=int, default=600, show_default=True,
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
        "--tree-budget-usd", type=float, default=25.0, show_default=True,
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
             "via 'otto v5 review' or MC. Sub-Leads' decompositions remain "
             "autonomous.",
    )
    @click.option(
        "--no-cache",
        is_flag=True,
        help="Disable v5 spec compile cache for this run.",
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
        """Run a v5 Lead session against the intent.

        Compiles a flat spec (intent + behavior_journeys), then runs the Lead.
        The Lead either calls begin_inline (and builds inline) or calls
        submit_subtask N times (Phase 2 picks up the subtasks).
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

        # Full v5 pipeline: compile + root Lead + children + integration.
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
                # Phase 1.2-A: a previously-failed run was resumed; the
                # pipeline SKIPPED compile + decompose + child rebuild and
                # is re-entering integration on the persisted task graph +
                # per-child branches. Phase 1.2-B: if prior repair packets
                # were carried, the repair agent will resume its prior SDK
                # session (option.resume=agent_session_id) instead of
                # starting fresh — surface that too.
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

        # Pass review-first-decomp + tier preset into the v5 pipeline.
        if review_first_decomp:
            config["v5_review_first_decomp"] = True
        if fresh:
            # Phase 1.2-A polish: explicit user override to refuse resume
            # even when a resumable checkpoint exists (partial / merge_blocked
            # root) for this project. Equivalent to setting
            # v5_resume_from_checkpoint: false in otto.yaml.
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
        console.print(f"  [bold]Verdict:[/bold] {_color_verdict(result.verdict)}")
        console.print(f"  cost: ${result.total_cost_usd:.4f}")
        console.print(f"  duration: {result.duration_s:.1f}s")
        if result.failure_reason:
            console.print(f"  [yellow]reason:[/yellow] {result.failure_reason}")

        # Exit code: 0 unless catastrophic (1). A `missing_toolchain` block is
        # an ENVIRONMENT failure, not a product defect or an Otto crash — give
        # it a DISTINCT non-success exit (3) so CI / callers can tell "fix your
        # host toolchain" apart from "Otto/product broke" (Codex Plan Gate
        # R3#2; never reported as a product merge_blocked success).
        if "missing_toolchain" in (result.failure_reason or ""):
            console.print(
                "  [yellow]environment:[/yellow] no usable uv / Python 3.12 "
                "toolchain on this host — not a product defect"
            )
            sys.exit(3)
        if result.verdict == "catastrophic":
            sys.exit(1)


def _run_phase1_only(project_dir: Path, intent: str, config: dict) -> None:
    """Phase 1 single-Lead path (no children processed). For isolation testing."""
    session_id = _new_session_id()
    from otto import paths as _paths

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
    if result.verdict == "catastrophic":
        sys.exit(1)


def _register_review_commands(v5_group: click.Group) -> None:
    """Register list-pending and review subcommands on v5_group."""

    @v5_group.command("list-pending")
    def list_pending_cmd() -> None:
        """List v5 tasks awaiting review (--review-first-decomp)."""
        require_git()
        try:
            project_dir = resolve_project_dir(Path.cwd())
        except ConfigError as exc:
            error_console.print(f"[error]{exc}[/error]")
            sys.exit(2)

        from otto.v5_review import list_pending_review

        pending = list_pending_review(project_dir)
        if not pending:
            console.print("[dim]no v5 tasks awaiting review[/dim]")
            return
        for entry in pending:
            console.print(
                f"  {entry.get('task_id', '?')}  "
                f"parent={entry.get('parent_task_id', '?')}  "
                f"intent={(entry.get('intent') or '')[:80]!r}"
            )

    @v5_group.command("review")
    @click.argument("action", type=click.Choice(["approve", "cancel", "edit", "replace", "list"]))
    @click.option("--task", "task_ids", multiple=True, help="Task id(s) to act on (repeatable).")
    @click.option("--parent", default=None, help="Filter by parent task id.")
    @click.option("--intent", default=None, help="New intent for `edit`.")
    @click.option(
        "--intents-file", type=click.Path(dir_okay=False, exists=True, path_type=Path),
        default=None, help="Newline-separated list of new intents for `replace`.",
    )
    def review_cmd(
        action: str,
        task_ids: tuple[str, ...],
        parent: str | None,
        intent: str | None,
        intents_file: Path | None,
    ) -> None:
        """Manage v5 pending-review tasks.

        Actions:
          list      — list current pending tasks (alias for `list-pending`).
          approve   — flip pending → approved (use --task <id> repeatedly, or --parent).
          cancel    — drop pending tasks from the queue (use --task).
          edit      — change a pending task's intent (use --task and --intent).
          replace   — cancel all pending under --parent, append from --intents-file.
        """
        require_git()
        try:
            project_dir = resolve_project_dir(Path.cwd())
        except ConfigError as exc:
            error_console.print(f"[error]{exc}[/error]")
            sys.exit(2)

        from otto.v5_review import (
            approve as _approve,
            cancel as _cancel,
            edit as _edit,
            list_pending_review,
            replace as _replace,
        )

        if action == "list":
            for entry in list_pending_review(project_dir, parent_task_id=parent):
                console.print(
                    f"  {entry.get('task_id', '?')}  "
                    f"intent={(entry.get('intent') or '')[:80]!r}"
                )
            return

        if action == "approve":
            n = _approve(
                project_dir,
                task_ids=list(task_ids) if task_ids else None,
                parent_task_id=parent,
            )
            console.print(f"approved {n} task(s)")
            return

        if action == "cancel":
            if not task_ids:
                error_console.print("[error]cancel requires --task <id> [--task <id> ...][/error]")
                sys.exit(2)
            n = _cancel(project_dir, task_ids=list(task_ids))
            console.print(f"cancelled {n} task(s)")
            return

        if action == "edit":
            if len(task_ids) != 1 or not intent:
                error_console.print(
                    "[error]edit requires exactly one --task and a non-empty --intent[/error]"
                )
                sys.exit(2)
            ok = _edit(project_dir, task_id=task_ids[0], new_intent=intent)
            console.print(("edited" if ok else "no change") + f" task {task_ids[0]}")
            return

        if action == "replace":
            if not parent or not intents_file:
                error_console.print(
                    "[error]replace requires --parent <id> and --intents-file <path>[/error]"
                )
                sys.exit(2)
            new_intents = [
                line.strip()
                for line in intents_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            cancelled, new_ids = _replace(
                project_dir, parent_task_id=parent, new_intents=new_intents
            )
            console.print(f"cancelled {len(cancelled)} task(s); enqueued {len(new_ids)} new task(s)")
            return


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


def _register_status_command(v5_group: click.Group) -> None:
    """Read-only diagnostic: what state is this project in? What would
    resume do? This is the "look before you spend" command — answer the
    cheap question (what's the current checkpoint?) before paying the
    expensive one (fresh re-run).

    Born out of the iTracker Opus broken-state case (2026-05-20): figuring
    out resume eligibility required manually grep'ing graph.json and
    cross-referencing with v5_runner.py:_resume_root_from_checkpoint
    guard logic. This command surfaces it directly.
    """

    @v5_group.command("status")
    @click.option(
        "--verbose", "-v", is_flag=True,
        help="Show structured failure reasons + per-child intents.",
    )
    def status_cmd(verbose: bool) -> None:
        """Show v5 pipeline state for this project: phase reached, per-task
        verdicts, resume eligibility, what a non-fresh `otto v5 run` would
        do next.

        No mutations. Safe to run any time.
        """
        require_git()
        try:
            project_dir = resolve_project_dir(Path.cwd())
        except ConfigError as exc:
            error_console.print(f"[error]{exc}[/error]")
            sys.exit(2)

        from otto.queue.task_graph import read_graph
        from otto.v5_runner import ROOT_TASK_ID

        graph = read_graph(project_dir)
        tasks = graph.get("tasks") or {}
        if not tasks:
            console.print("[dim]no v5 task graph found — fresh run only[/dim]")
            return

        root = tasks.get(ROOT_TASK_ID) if isinstance(tasks, dict) else None
        if root is None and isinstance(tasks, list):
            for t in tasks:
                if isinstance(t, dict) and t.get("id") == ROOT_TASK_ID:
                    root = t
                    break
        if root is None:
            console.print("[dim]no root task found[/dim]")
            return

        root_verdict = str(root.get("verdict") or "unknown")
        intent_preview = (root.get("intent") or "")[:120].rstrip()

        console.print(f"[bold]Project:[/bold] {project_dir}")
        console.print(f"[bold]Root intent:[/bold] {intent_preview}...")
        console.print(f"[bold]Root verdict:[/bold] {_color_verdict(root_verdict)}")

        child_ids = list(root.get("child_task_ids") or [])
        if not child_ids:
            console.print("[dim]no children emitted yet — resume not applicable[/dim]")
        else:
            console.print(f"\n[bold]Children ({len(child_ids)}):[/bold]")
            for cid in child_ids:
                if isinstance(tasks, dict):
                    child = tasks.get(cid) or {}
                else:
                    child = next(
                        (t for t in tasks if isinstance(t, dict) and t.get("id") == cid),
                        {},
                    )
                v = str(child.get("verdict") or "unknown")
                intent = (child.get("intent") or "")[:70].rstrip()
                console.print(f"  {cid}  {_color_verdict(v):24s}  {intent}...")
                if verbose:
                    nested_md = child.get("metadata") or {}
                    for key in (
                        "merge_blocked_origin",
                        "merge_blocked_reason",
                        "annotation_origin",
                        "annotation_detail",
                        "landed_with_annotation",
                        "failure_reason",
                    ):
                        value = child.get(key)
                        if value is None and isinstance(nested_md, dict):
                            value = nested_md.get(key)
                        if value:
                            preview = (
                                str(value)[:120]
                                + ("..." if len(str(value)) > 120 else "")
                            )
                            console.print(f"      {key}: {preview}")

        # Resume eligibility — delegate to the canonical planner so this
        # diagnostic stays in sync with the runner + plan-resume command
        # (Phase 2 — Codex R2#7 + user "centralize APIs" mandate).
        from otto.v5_resume_plan import compute_resume_plan

        plan = compute_resume_plan(
            project_dir=project_dir,
            intent_for_match=None,  # status doesn't know an intent; skip the match
            config={},
        )
        console.print("\n[bold]Resume eligibility:[/bold]")
        if plan.status == "RESUMABLE":
            console.print(
                f"  [green]RESUMABLE[/green] — root verdict "
                f"'{plan.root_verdict}' + {len(plan.children)} children + "
                f"spec at session {plan.latest_spec_checkpoint}."
            )
            console.print(
                "  [dim]Next `otto v5 run` (no --fresh) will skip "
                f"{', '.join(plan.skipped_phases)} and re-enter at "
                f"{plan.phase_to_enter} phase.[/dim]"
            )
        else:
            console.print(
                f"  [yellow]{plan.status}[/yellow] — "
                f"{plan.not_resumable_reason or '(no reason recorded)'}"
            )
        if plan.suggested_next:
            console.print("  [bold]Suggested:[/bold]")
            for s in plan.suggested_next:
                console.print(f"    $ {s}")
        if plan.concerns:
            console.print("  [bold yellow]Concerns:[/bold yellow]")
            for c in plan.concerns:
                console.print(f"    • {c}")


def _register_reset_verdict_command(v5_group: click.Group) -> None:
    """Reset specified task verdicts to a clean state so a subsequent
    resume re-attempts them. Targeted recovery for broken-state cases
    where (a) the verdict was wrong (false demotion from an upstream bug
    now fixed) or (b) you want to retry a child after fixing otto code.

    Born out of the iTracker Opus broken-state (2026-05-20): 3 children
    were marked merge_blocked by upstream bugs that are now fixed. To
    validate the fixes via resume, we needed a way to clear those
    verdicts. Previously: manual graph.json edit. Now: explicit CLI.
    """

    @v5_group.command("reset-verdict")
    @click.option(
        "--task", "task_ids", multiple=True, required=True,
        help="Task id(s) whose verdict to clear (repeatable).",
    )
    @click.option(
        "--to", "new_verdict",
        type=click.Choice(["unverified", "pending_children"]),
        default="unverified", show_default=True,
        help="State to reset to. 'unverified' triggers a re-verify on resume; "
             "'pending_children' is for root tasks whose children should re-merge.",
    )
    @click.option(
        "--dry-run", is_flag=True,
        help="Show what would change without writing.",
    )
    def reset_verdict_cmd(
        task_ids: tuple[str, ...],
        new_verdict: str,
        dry_run: bool,
    ) -> None:
        """Reset task verdict(s) so subsequent `otto v5 run` (without
        --fresh) re-attempts them.

        Use case: an upstream bug demoted children unjustly. After fixing
        the bug, this command clears the bogus verdicts so resume re-runs
        the affected children's integration. Does NOT delete files or
        worktrees — only the verdict + failure_reason metadata.
        """
        require_git()
        try:
            project_dir = resolve_project_dir(Path.cwd())
        except ConfigError as exc:
            error_console.print(f"[error]{exc}[/error]")
            sys.exit(2)

        from otto.queue.task_graph import (
            clear_blocker_metadata,
            get_task,
            read_graph,
            set_verdict,
            update_task_metadata,
        )

        graph = read_graph(project_dir)
        tasks = graph.get("tasks") or {}
        if isinstance(tasks, list):
            tasks = {t["id"]: t for t in tasks if isinstance(t, dict)}

        for tid in task_ids:
            task = get_task(project_dir, tid)
            if task is None:
                error_console.print(f"[error]task {tid} not found[/error]")
                continue
            old_verdict = task.get("verdict") or "unknown"
            console.print(
                f"  {tid}: {_color_verdict(str(old_verdict))} → "
                f"{_color_verdict(new_verdict)}"
            )
            if dry_run:
                continue
            from typing import Any as _Any, cast as _cast
            clear_blocker_metadata(project_dir, tid)
            set_verdict(project_dir, tid, _cast(_Any, new_verdict), cost_usd=0.0)
            update_task_metadata(
                project_dir,
                tid,
                failure_reason="",
                annotation_origin="",
                annotation_detail="",
                annotation_cause="",
                landed_with_annotation=False,
                annotation_structured_reason=None,
                reset_via_cli_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )

        if dry_run:
            console.print("[dim]dry-run — no changes written[/dim]")
        else:
            console.print(
                f"[green]reset {len(task_ids)} task verdict(s) to {new_verdict}[/green]"
            )


def _register_retry_children_command(v5_group: click.Group) -> None:
    """`otto v5 retry-children` — targeted broken-state recovery."""

    @v5_group.command("retry-children")
    @click.option(
        "--task", "task_ids", multiple=True, required=True,
        help="Task id(s) to retry (repeatable).",
    )
    @click.option(
        "--cascade-dependents", is_flag=True,
        help="Also retry any downstream tasks that depend on the targets "
             "and currently have satisfactory verdicts.",
    )
    @click.option(
        "--continue", "continue_dirty", is_flag=True,
        help="Continue even if a target worktree is dirty.",
    )
    @click.option(
        "--force", is_flag=True,
        help="Override pass-verdict refusal.",
    )
    @click.option(
        "--dry-run", is_flag=True,
        help="Show the validated plan without making changes.",
    )
    def retry_children_cmd(
        task_ids: tuple[str, ...],
        cascade_dependents: bool,
        continue_dirty: bool,
        force: bool,
        dry_run: bool,
    ) -> None:
        """Retry specified child tasks. All-or-nothing transaction;
        rollback on any failure. Targets must be leaf children with
        persisted worktrees + branches.

        After retry, dispatch via `otto v5 run "<original intent>"`.
        """
        require_git()
        try:
            project_dir = resolve_project_dir(Path.cwd())
        except ConfigError as exc:
            error_console.print(f"[error]{exc}[/error]")
            sys.exit(2)

        from otto.v5_retry import execute_plan, validate_and_plan

        plan = validate_and_plan(
            project_dir=project_dir,
            task_ids=list(task_ids),
            cascade_dependents=cascade_dependents,
            allow_continue_dirty=continue_dirty,
            force_pass=force,
        )

        console.print(f"[bold]Retry plan for {len(task_ids)} task(s):[/bold]")
        if plan.targets:
            console.print(f"  targets ({len(plan.targets)}):")
            for tid in plan.targets:
                wt = plan.worktrees.get(tid, "?")
                sdir = plan.sessions_to_archive.get(tid, "<no session>")
                console.print(f"    {tid}")
                console.print(f"      worktree:  {wt}")
                console.print(f"      session:   {sdir}")
        if plan.cascaded:
            console.print(
                f"  [yellow]cascaded ({len(plan.cascaded)}):[/yellow]"
            )
            for tid in plan.cascaded:
                console.print(f"    {tid}")
        if plan.failures:
            console.print(
                f"  [red]validation failures ({len(plan.failures)}):[/red]"
            )
            for f in plan.failures:
                console.print(f"    [red]{f.task_id}[/red]: {f.reason}")

        if not plan.ok:
            error_console.print(
                "[error]plan validation failed; no state changes made.[/error]"
            )
            sys.exit(2)

        if dry_run:
            console.print(
                "[dim]dry-run — no changes written. Re-run without "
                "--dry-run to execute.[/dim]"
            )
            return

        console.print(
            "[dim]acquiring retry-children lock + executing...[/dim]"
        )
        result = execute_plan(project_dir=project_dir, plan=plan)
        if result.error:
            error_console.print(
                f"[error]execution failed: {result.error}[/error]"
            )
            if result.rolled_back:
                console.print(
                    "[yellow]rolled back: graph entries restored, archived "
                    "sessions un-archived.[/yellow]"
                )
            sys.exit(3)

        console.print(
            f"[green]retry-children complete:[/green] "
            f"reset {len(result.reset_task_ids)} task(s), "
            f"archived {len(result.archived)} session(s), "
            f"pending: "
            f"{result.pending_summary.get('rewritten', [])} rewritten, "
            f"{result.pending_summary.get('synthesized', [])} synthesized, "
            f"{result.pending_summary.get('missing', [])} missing, "
            f"{result.pending_summary.get('superseded_count', 0)} superseded."
        )
        console.print(
            "\n[bold]Next:[/bold] `otto v5 run \"<original intent>\"` "
            "(no --fresh). The scheduler will pick up the reset entries."
        )


def _register_plan_resume_command(v5_group: click.Group) -> None:
    """`otto v5 plan-resume` — read-only resume simulation.

    "Look before you spend $$" — predicts what `otto v5 run` (no
    --fresh) would do given the project's persisted state, including
    cost + wall-time ranges and per-child predicted actions. No
    mutations.

    Plan Phase 2 (plan-checkpoint-resume-v2.md, Codex APPROVED at R5).
    """

    @v5_group.command("plan-resume")
    @click.option(
        "--model", default="sonnet", show_default=True,
        help="Model to assume for cost estimates (sonnet|opus|other).",
    )
    @click.option(
        "--intent", default=None,
        help="Predict for THIS intent (matched against persisted root "
             "intent). If different, refuses (intent-drift guard).",
    )
    @click.option("--json", "json_out", is_flag=True,
                  help="Emit structured JSON for scripts/MC instead of human text.")
    def plan_resume_cmd(model: str, intent: str | None, json_out: bool) -> None:
        """Predict what `otto v5 run` (no --fresh) would do for this
        project right now. Read-only — safe to call any time.
        """
        require_git()
        try:
            project_dir = resolve_project_dir(Path.cwd())
        except ConfigError as exc:
            error_console.print(f"[error]{exc}[/error]")
            sys.exit(2)

        try:
            config = load_config(project_dir / "otto.yaml")
        except ConfigError:
            config = {}

        from otto.v5_resume_plan import compute_resume_plan, plan_to_json

        plan = compute_resume_plan(
            project_dir=project_dir,
            intent_for_match=intent,
            model=model,
            config=config if isinstance(config, dict) else {},
        )

        if json_out:
            # Raw stdout — bypass Rich wrapping so consumers can pipe the
            # output through `jq` etc. without control-character noise.
            click.echo(plan_to_json(plan))
            return

        # Human render.
        status_color = {
            "RESUMABLE": "green",
            "NOT_RESUMABLE": "yellow",
            "FRESH_ONLY": "yellow",
        }.get(plan.status, "white")
        console.print(f"[bold]Status:[/bold] [{status_color}]{plan.status}[/{status_color}]")
        if plan.not_resumable_reason:
            console.print(f"  [dim]{plan.not_resumable_reason}[/dim]")
        console.print(f"[bold]Root verdict:[/bold] {_color_verdict(plan.root_verdict)}")
        console.print(f"[bold]Root intent:[/bold] {plan.root_intent_preview}...")
        if plan.phase_to_enter:
            console.print(
                f"[bold]Phase to enter:[/bold] {plan.phase_to_enter} "
                f"(skipping: {', '.join(plan.skipped_phases)})"
            )
        if plan.latest_spec_checkpoint:
            console.print(
                f"[bold]Spec checkpoint:[/bold] {plan.latest_spec_checkpoint}"
            )
        if plan.repair_packets_carriable:
            console.print(
                f"[bold]Repair packets carriable:[/bold] "
                f"{plan.repair_packets_carriable}"
            )

        if plan.children:
            console.print(f"\n[bold]Children ({len(plan.children)}) predictions:[/bold]")
            for c in plan.children:
                action_color = {
                    "skip_pass": "green",
                    "merge_unmerged": "cyan",
                    "rebuild_via_retry": "cyan",
                    "stays_merge_blocked": "red",
                    "stays_unverified": "yellow",
                    "pending_children": "dim",
                    "unknown_state": "magenta",
                }.get(c.action, "white")
                console.print(
                    f"  {c.task_id}  {_color_verdict(c.current_verdict):16s}  "
                    f"[{action_color}]{c.action}[/{action_color}]  "
                    f"({c.intent_preview[:50]}...)"
                )
                if c.concern:
                    console.print(f"      [dim]{c.concern}[/dim]")

        if plan.status == "RESUMABLE":
            lo, p50, hi = plan.estimated_cost_usd_range
            wl, wp, wh = plan.estimated_wall_minutes_range
            console.print(
                f"\n[bold]Cost estimate ({plan.model_assumed}):[/bold] "
                f"${lo:.0f} – ${p50:.0f} – ${hi:.0f} (low/p50/high)"
            )
            console.print(
                f"[bold]Wall estimate:[/bold] "
                f"{wl:.0f} – {wp:.0f} – {wh:.0f} min"
            )

        if plan.concerns:
            console.print("\n[bold yellow]Concerns:[/bold yellow]")
            for c in plan.concerns:
                console.print(f"  • {c}")

        if plan.suggested_next:
            console.print("\n[bold]Suggested next:[/bold]")
            for s in plan.suggested_next:
                console.print(f"  $ {s}")
