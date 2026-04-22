"""Otto CLI - `otto pow` convenience command."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click

from otto import paths
from otto.config import require_git, resolve_project_dir
from otto.display import CONTEXT_SETTINGS, console, rich_escape
from otto.theme import error_console


def _pow_html_path(project_dir: Path, run_id: str | None) -> Path:
    if run_id:
        session_dir = paths.session_dir(project_dir, run_id)
        if not session_dir.exists():
            raise FileNotFoundError(f"session not found: {run_id}")
        pow_path = session_dir / "certify" / "proof-of-work.html"
        if not pow_path.exists():
            raise FileNotFoundError(
                f"proof-of-work not found for session {run_id}; try `otto history` to inspect recent runs."
            )
        return pow_path.resolve()

    latest_session = paths.resolve_pointer(project_dir, paths.LATEST_POINTER)
    if latest_session is None:
        raise FileNotFoundError("no latest session found; run `otto build`, `otto certify`, or `otto merge` first.")
    pow_path = latest_session / "certify" / "proof-of-work.html"
    if not pow_path.exists():
        raise FileNotFoundError(
            "latest session has no proof-of-work.html; try `otto history` to find a completed cert session."
        )
    return pow_path.resolve()


def register_pow_command(main: click.Group) -> None:
    """Register `otto pow` on the main CLI group."""

    @main.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("run_id", required=False)
    @click.option("--print", "print_only", is_flag=True, help="Print the PoW path instead of opening it")
    def pow(run_id: str | None, print_only: bool) -> None:
        """Open a proof-of-work report.

        Without RUN_ID, opens the latest session's PoW report.
        With RUN_ID, opens that session's PoW report directly.
        """
        require_git()
        project_dir = resolve_project_dir(Path.cwd())
        try:
            pow_path = _pow_html_path(project_dir, run_id)
        except FileNotFoundError as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(1)

        if print_only:
            console.print(str(pow_path))
            return

        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            result = subprocess.run([opener, str(pow_path)], check=False, capture_output=True, text=True)
        except OSError as exc:
            error_console.print(
                f"[error]Failed to launch {opener}: {rich_escape(str(exc))}[/error]\n"
                "  Use `otto pow --print` to print the report path."
            )
            sys.exit(1)
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            error_console.print(
                f"[error]Failed to open proof-of-work with {opener}.[/error]"
                + (f"\n  {rich_escape(details)}" if details else "")
                + "\n  Use `otto pow --print` to print the report path."
            )
            sys.exit(1)
