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
import time
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
from otto.render import render_run
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
        f"  [bold]Compile complete[/bold] — {len(spec.slices)} slice(s), "
        f"project_kind={spec.project_kind}"
    )
    console.print(f"  Spec: {written}")
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
    console.print("  [bold]Build phase[/bold] — dispatching slice agents")
    build_result = await run_build(
        spec,
        project_dir=project_dir,
        session_dir=session_dir,
        build_agent=default_build_agent,
        base_url=base_url,
        budget=shared_budget,
    )
    console.print(
        f"  Build: {len(build_result.passing_ids)}/{len(build_result.slice_results)} "
        f"slices passing, ${build_result.total_cost_usd:.2f}, "
        f"{build_result.total_wall_s:.0f}s"
    )
    if build_result.blocked_ids:
        console.print(
            f"  [yellow]Blocked slices:[/yellow] {', '.join(build_result.blocked_ids)}"
        )

    console.print()
    console.print("  [bold]Merge phase[/bold] — landing slices in dep order")
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
        walkthrough=default_walkthrough_from_spec(spec),
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
    def run(
        intent: str | None,
        project_kind: str,
        break_lock: bool,
        no_build: bool,
        base_url: str | None,
        from_spec: Path | None,
    ) -> None:
        """Run the intent-to-product pipeline.

        With no flags: compile, then drive build → merge → audit → render
        end-to-end against the integrated worktree.

        Examples:
            otto run "a bookmark manager"
            otto run --project-kind cli "a small linter"
            otto run --no-build "review-only mode"
            otto run --from-spec otto_logs/sessions/x/spec/spec.json
        """
        require_git()
        project_dir = resolve_project_dir(Path.cwd())

        if from_spec is not None:
            try:
                spec = load_spec(from_spec)
            except SpecValidationError as exc:
                error_console.print(f"[error]Failed to load spec: {exc}[/error]")
                sys.exit(2)
            spec_path = from_spec.resolve()
            session_id = _resolve_session_from_spec_path(spec_path)
            session_dir = spec_path.parent.parent  # spec/ → session/
            console.print(f"  [bold]otto run[/bold] — driving from {spec_path}")
        else:
            intent_text = _resolve_intent_or_exit(intent, project_dir)

            config_path = project_dir / "otto.yaml"
            try:
                config = load_config(config_path)
            except ConfigError as exc:
                error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
                sys.exit(2)

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

        if no_build:
            _emit_compile_only_message(spec_path)
            sys.exit(0)

        # Drive build → merge → audit → render.
        run_t0 = time.monotonic()
        try:
            emit(session_dir, "audit.started", detail="run start")
        except Exception as exc:
            logger.warning("emit run start failed: %s", exc)

        try:
            build_result, merge_result, audit_result = asyncio.run(
                _drive_full_pipeline(
                    spec=spec,
                    project_dir=project_dir,
                    session_dir=session_dir,
                    base_url=base_url,
                )
            )
        except Exception as exc:
            error_console.print(f"[error]Pipeline crashed: {type(exc).__name__}: {exc}[/error]")
            try:
                emit(session_dir, "run.finished", detail=str(exc), verdict="blocked")
            except Exception:
                pass
            sys.exit(1)

        wall = time.monotonic() - run_t0
        cost = (
            build_result.total_cost_usd
            + merge_result.total_cost_usd
            + audit_result.cost_usd
        )

        console.print()
        console.print("  [bold]Render phase[/bold] — assembling proof packet")
        html_path, json_path = render_run(
            spec,
            session_dir=session_dir,
            build_result=build_result,
            merge_result=merge_result,
            audit_result=audit_result,
            wall_s=wall,
            cost_usd=cost,
        )
        console.print(f"  Proof: {html_path}")
        console.print(f"        {json_path}")

        try:
            emit(
                session_dir,
                "run.finished",
                detail=f"verdict={audit_result.verdict.value}",
                verdict=audit_result.verdict.value,
            )
        except Exception as exc:
            logger.warning("emit run.finished failed: %s", exc)

        # Exit code reflects the verdict: passed → 0, partial/blocked → 1.
        if audit_result.verdict == AuditVerdict.PASSED:
            sys.exit(0)
        sys.exit(1)


def _resolve_session_from_spec_path(spec_path: Path) -> str:
    """Extract the session id from an `otto_logs/sessions/<id>/spec/spec.json` path."""
    try:
        return spec_path.parent.parent.name
    except Exception:
        return spec_path.parent.name
