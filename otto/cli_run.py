"""`otto run` — intent-to-product pipeline (Phase A: compile-only landing).

Step 9 of the unified intent-to-product plan. This subcommand is the
new entry point for the structured `Spec` pipeline. The current PR lands
the **compile path** only:

  intent  →  compile_spec()  →  spec.json  →  (build / audit / render: stubbed)

Once the deferred steps (2, 4–7, 8a/8b, 10, 11) land, the same command
will route through the full pipeline. Until then it exits with a
"build/audit/render not yet implemented" message and a pointer to the
written `spec.json`.

Old commands (`otto build`, `otto certify`, `otto improve`) are
untouched — `otto run` is non-clobbering.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import click

from otto import paths as _paths
from otto.config import (
    ConfigError,
    _normalize_intent,
    load_config,
    require_git,
    resolve_project_dir,
)
from otto.display import CONTEXT_SETTINGS, console, rich_escape
from otto.spec_compile import (
    PROJECT_KINDS,
    SpecValidationError,
    compile_spec,
)
from otto.theme import error_console


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
) -> Path:
    """Run the compile agent and return the path to the persisted spec.json."""
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
    # `compile_spec` already calls persist_spec(allow_initial=True). The
    # canonical path is the same one we'd pass into review/build/audit.
    written = spec_dir / "spec.json"
    console.print(
        f"  [bold]Compile complete[/bold] — {len(spec.slices)} slice(s), "
        f"project_kind={spec.project_kind}"
    )
    console.print(f"  Spec: {written}")
    return written


def _emit_phase_a_stub_message(spec_path: Path) -> None:
    """Print the not-yet-implemented message for build/audit/render."""
    console.print()
    console.print("  [yellow]Build, audit, and render are not yet implemented[/yellow]")
    console.print(f"  Compiled spec available at: {spec_path}")
    console.print(
        "  Follow-up PRs will land Steps 2 (checks runtime), 4 (build loop), "
        "5 (merge queue),\n  6 (audit), 7 (render)."
    )


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
    def run(intent: str | None, project_kind: str, break_lock: bool) -> None:
        """Run the intent-to-product pipeline (Phase A: compile-only).

        Compiles the intent into a structured spec.json. Build, audit,
        and render are stubbed pending follow-up PRs.

        Examples:
            otto run "a bookmark manager"
            otto run --project-kind cli "a small linter"
        """
        require_git()
        project_dir = resolve_project_dir(Path.cwd())
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
                console.print(f"  [bold]otto run[/bold] — session {session_id}\n")
                spec_path = asyncio.run(
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

        _emit_phase_a_stub_message(spec_path)
        sys.exit(0)
