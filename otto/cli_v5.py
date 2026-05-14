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
        "--tier",
        type=click.Choice(["auto", "solo", "lead", "modular"]),
        default="auto", show_default=True,
        help="Decomposition preset. solo=force inline; lead=allow subtasks; "
             "modular=require Architecture-first thinking; auto=Lead chooses.",
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
        tier: str,
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

        # Pass review-first-decomp + tier preset into the v5 pipeline.
        if review_first_decomp:
            config["v5_review_first_decomp"] = True
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

        # Exit code: 0 unless catastrophic.
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
