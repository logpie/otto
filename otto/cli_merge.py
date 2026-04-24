"""Otto CLI — `otto merge ...`.

Single command with mode flags:
    otto merge --all                  # land all done queue tasks into target
    otto merge t3 build/x             # explicit task ids or branches
    otto merge --target develop       # merge target other than default_branch
    otto merge --no-certify           # skip post-merge verification
    otto merge --full-verify          # test every merged story
    otto merge --fast                 # pure git, NO LLM, bail on first conflict
    otto merge --cleanup-on-success   # remove worktrees after merge
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click

from otto import paths
from otto.display import CONTEXT_SETTINGS, console, rich_escape
from otto.theme import error_console

logger = logging.getLogger("otto.cli_merge")


def _install_merge_logging(project_dir: Path) -> None:
    """Attach a file handler to the `otto.merge` logger tree so the
    orchestrator and conflict-agent events get persisted to
    otto_logs/merge/merge.log. Without this they vanish.

    Idempotent: removes any prior `_otto_merge_handler` we added.
    """
    log_dir = paths.merge_dir(project_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    parent = logging.getLogger("otto.merge")
    parent.setLevel(logging.INFO)
    # Remove our previously-installed handler if any (re-runs in same process)
    for h in list(parent.handlers):
        if getattr(h, "_otto_merge_handler", False):
            parent.removeHandler(h)
    handler = logging.FileHandler(log_dir / "merge.log", mode="a")
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    ))
    handler._otto_merge_handler = True  # type: ignore[attr-defined]
    parent.addHandler(handler)
    # Also wire the CLI logger (different namespace)
    cli_log = logging.getLogger("otto.cli_merge")
    cli_log.setLevel(logging.INFO)
    if not any(getattr(h, "_otto_merge_handler", False) for h in cli_log.handlers):
        cli_handler = logging.FileHandler(log_dir / "merge.log", mode="a")
        cli_handler.setLevel(logging.INFO)
        cli_handler.setFormatter(handler.formatter)
        cli_handler._otto_merge_handler = True  # type: ignore[attr-defined]
        cli_log.addHandler(cli_handler)


def register_merge_command(main: click.Group) -> None:
    """Register `otto merge` on the main CLI group."""

    @main.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("ids_or_branches", nargs=-1)
    @click.option("--all", "all_done", is_flag=True,
                  help="Merge all done queue tasks into target")
    @click.option("--target", default=None,
                  help="Target branch (default from otto.yaml default_branch)")
    @click.option("--no-certify", is_flag=True,
                  help="Skip post-merge story verification")
    @click.option("--full-verify", is_flag=True,
                  help="Verify the full merged story union; don't allow per-story skips")
    @click.option("--fast", is_flag=True,
                  help="Pure git merge; bail on first conflict (no LLM)")
    @click.option("--cleanup-on-success", is_flag=True,
                  help="Remove worktrees of merged tasks on successful merge")
    @click.option("--allow-any-branch", is_flag=True,
                  help="Allow arbitrary local branches, not just otto-managed branches")
    def merge(
        ids_or_branches: tuple[str, ...],
        all_done: bool,
        target: str | None,
        no_certify: bool,
        full_verify: bool,
        fast: bool,
        cleanup_on_success: bool,
        allow_any_branch: bool,
    ) -> None:
        """Land queued / built branches into the target branch.

        Python-driven git merge. The conflict agent is invoked ONLY when
        git can't auto-merge — clean merges burn $0. After all branches
        merge, the certifier verifies the union of merged branches'
        stories; it skips per-story when the merge diff doesn't touch the
        story's feature, and flags genuine cross-branch contradictions
        for human review.

        \b
        Examples:
            otto merge --all
            otto merge build/csv-export build/settings-redesign
            otto merge --all --no-certify
            otto merge --all --fast        # pure git, bail on conflict
        """
        from otto.config import load_config
        from otto.merge.orchestrator import (
            MergeAlreadyRunning,
            MergeOptions,
            merge_lock,
            run_merge,
        )

        project_dir = Path.cwd()
        # `load_config` returns DEFAULT_CONFIG when otto.yaml is absent — be
        # consistent with `otto queue build|run`, which also tolerate it.
        config = load_config(project_dir / "otto.yaml")
        # Defensive: if a user upgraded otto without re-running `otto setup`,
        # their .gitignore / .gitattributes may not be configured, making
        # working_tree_clean and the bookkeeping-driver precondition fail.
        # Idempotent — no-op on repos that already have them.
        try:
            from otto.config import first_touch_bookkeeping
            first_touch_bookkeeping(project_dir, config)
        except Exception:
            pass  # non-fatal; downstream precondition checks will surface a clearer error

        target_branch = target or str(config.get("default_branch", "main"))
        opts = MergeOptions(
            target=target_branch,
            no_certify=no_certify,
            full_verify=full_verify,
            fast=fast,
            cleanup_on_success=cleanup_on_success,
            allow_any_branch=allow_any_branch,
        )

        if not (all_done or ids_or_branches):
            error_console.print(
                "[error]Specify branches/task ids, or pass --all to merge all done queue tasks.[/error]"
            )
            sys.exit(2)

        from otto.budget import RunBudget
        budget = RunBudget.start_from(config)

        console.print(f"  [bold]Merging[/bold] into [info]{target_branch}[/info]")
        if fast:
            console.print("  [dim]Mode:[/dim] [yellow]--fast[/yellow] (pure git, no LLM)")
        if no_certify:
            console.print("  [dim]Mode:[/dim] [yellow]--no-certify[/yellow]")
        if full_verify:
            console.print("  [dim]Mode:[/dim] [yellow]--full-verify[/yellow]")
        if allow_any_branch:
            console.print("  [dim]Mode:[/dim] [yellow]--allow-any-branch[/yellow]")

        # Wire up merge logger so orchestrator/conflict-agent events land
        # in otto_logs/merge/merge.log.
        _install_merge_logging(project_dir)

        try:
            with merge_lock(project_dir):
                result = asyncio.run(run_merge(
                    project_dir=project_dir,
                    config=config,
                    options=opts,
                    explicit_ids_or_branches=list(ids_or_branches) or None,
                    all_done_queue_tasks=all_done,
                    budget=budget,
                ))
        except MergeAlreadyRunning as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(1)
        except ValueError as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(2)
        except KeyboardInterrupt:
            console.print("\n  [yellow]Aborted. Working tree may have an in-progress merge.[/yellow]")
            sys.exit(130)
        except Exception as exc:
            logger.exception("merge failed")
            error_console.print(f"[error]Merge failed: {rich_escape(str(exc))}[/error]")
            sys.exit(1)

        # Print summary
        console.print()
        outcomes = result.state.outcomes
        for o in outcomes:
            icon = {
                "merged": "[success]✓[/success]",
                "merged_with_markers": "[yellow]⚠[/yellow]",
                "conflict_resolved": "[success]✓[/success]",
                "agent_giveup": "[red]✗[/red]",
                "skipped": "[dim]–[/dim]",
                "pending": "[yellow]?[/yellow]",
            }.get(o.status, "?")
            console.print(f"    {icon} {o.branch} ({o.status})")
            if o.note:
                console.print(f"      [dim]{rich_escape(o.note)}[/dim]")

        console.print()
        if result.success:
            console.print(f"  [success bold]Merge complete[/success bold] (id: {result.merge_id})")
            if result.source_pow_paths or result.post_merge_pow_path:
                console.print("  PoWs from this batch:")
                if result.source_pow_paths:
                    console.print("    Per-task:")
                    for record in result.source_pow_paths:
                        label = record.get("branch", "?")
                        task_id = record.get("task_id")
                        if task_id:
                            label = f"{task_id} ({record['branch']})"
                        console.print(
                            f"      {label}:  {record.get('path', '(missing — see manifest for details)')}"
                        )
                if result.post_merge_pow_path:
                    console.print(f"    Post-merge: {result.post_merge_pow_path}")
            if result.cert_story_results:
                # Group cert verdicts inline. The cert prunes (SKIPPED) and
                # flags contradictions (FLAG_FOR_HUMAN) itself via the
                # merge_context preamble — no separate planning call.
                buckets: dict[str, list[dict[str, str]]] = {
                    "PASS": [], "FAIL": [], "SKIPPED": [], "FLAG_FOR_HUMAN": [],
                }
                for s in result.cert_story_results:
                    buckets.setdefault(s.get("verdict", "FAIL"), []).append(s)
                console.print(
                    f"  Verification: {len(buckets['PASS'])} passed, "
                    f"{len(buckets['FAIL'])} failed, "
                    f"{len(buckets['SKIPPED'])} skipped, "
                    f"{len(buckets['FLAG_FOR_HUMAN'])} flagged for human"
                )
                if buckets["FLAG_FOR_HUMAN"]:
                    console.print()
                    for s in buckets["FLAG_FOR_HUMAN"]:
                        sid = s.get("story_id") or s.get("name") or "?"
                        rat = s.get("summary") or ""
                        console.print(f"    [yellow]⚠ {rich_escape(sid)}[/yellow]")
                        if rat:
                            console.print(f"      [dim]{rich_escape(rat)}[/dim]")
        else:
            if result.merge_id:
                console.print(f"  [red bold]Merge incomplete[/red bold] (id: {result.merge_id})")
            else:
                console.print("  [red bold]Merge blocked[/red bold]")
            console.print(f"  {rich_escape(result.note)}")

        sys.exit(0 if result.success else 1)
