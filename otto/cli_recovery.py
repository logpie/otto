"""`otto recover` command group — broken-state recovery primitives.

Four read-mostly / surgical-write commands for diagnosing and recovering
a v5 run that landed in a degraded state (`merge_blocked` children, stale
verdicts after upstream bug fixes, etc.):

  - ``otto recover status``         — diagnostic: project state + resume eligibility
  - ``otto recover plan-resume``    — read-only simulation: "what would resume do?"
  - ``otto recover reset-verdict``  — surgical: clear bogus verdict metadata
  - ``otto recover retry-children`` — atomic transaction: re-execute selected children

Born out of the iTracker Opus broken-state case (2026-05-20): three of
four Opus children landed `merge_blocked` via upstream bugs that are now
fixed. To validate the fixes via resume — without paying $150 for a
fresh re-run — operators needed a way to clear bogus verdicts and
retry specific children. Pre-recovery: manual graph.json edits.

These commands moved here from the cc-i2p-2 `cli_v5.py` 4-subcommand
block when v5 was promoted to the top-level `otto` namespace; the
recovery verbs stay grouped together because they share invariants
(targeted, read-mostly, transactional) and live closer to operations
than to the build pipeline.
"""

from __future__ import annotations

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

logger = logging.getLogger("otto.cli_recovery")


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


def register_recover_commands(main: click.Group) -> None:
    """Register the `otto recover <subcommand>` group on the root CLI."""

    @main.group("recover", context_settings=CONTEXT_SETTINGS)
    def recover_group() -> None:
        """Broken-state recovery for v5 runs.

        See `docs/recovery-workflow.md` for the diagnostic → reset →
        retry-children flow.
        """

    _register_status_command(recover_group)
    _register_plan_resume_command(recover_group)
    _register_reset_verdict_command(recover_group)
    _register_retry_children_command(recover_group)


def _register_status_command(recover_group: click.Group) -> None:
    @recover_group.command("status")
    @click.option(
        "--verbose", "-v", is_flag=True,
        help="Show structured failure reasons + per-child intents.",
    )
    def status_cmd(verbose: bool) -> None:
        """Show v5 pipeline state for this project.

        Reports phase reached, per-task verdicts, resume eligibility,
        and what a non-fresh `otto run` would do next. No mutations.
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
        # diagnostic stays in sync with the runner + plan-resume command.
        from otto.v5_resume_plan import compute_resume_plan

        plan = compute_resume_plan(
            project_dir=project_dir,
            intent_for_match=None,
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
                "  [dim]Next `otto run` (no --fresh) will skip "
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


def _register_reset_verdict_command(recover_group: click.Group) -> None:
    @recover_group.command("reset-verdict")
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
        """Reset task verdict(s) so subsequent `otto run` (without
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
            set_verdict,
            update_task_metadata,
        )
        from otto.observability import iso_timestamp

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
                reset_via_cli_at=iso_timestamp(),
            )

        if dry_run:
            console.print("[dim]dry-run — no changes written[/dim]")
        else:
            console.print(
                f"[green]reset {len(task_ids)} task verdict(s) to {new_verdict}[/green]"
            )


def _register_retry_children_command(recover_group: click.Group) -> None:
    @recover_group.command("retry-children")
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

        After retry, dispatch via `otto run "<original intent>"`.
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
            "\n[bold]Next:[/bold] `otto run \"<original intent>\"` "
            "(no --fresh). The scheduler will pick up the reset entries."
        )


def _register_plan_resume_command(recover_group: click.Group) -> None:
    @recover_group.command("plan-resume")
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
        """Predict what `otto run` (no --fresh) would do for this
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
            click.echo(plan_to_json(plan))
            return

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
