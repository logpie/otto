"""Otto CLI - `otto pow` convenience command."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click

from otto.config import require_git, resolve_project_dir
from otto.display import CONTEXT_SETTINGS, rich_escape
from otto.theme import error_console


def _pow_html_path(project_dir: Path, run_id: str | None) -> Path:
    from otto.cli_proof import proof_html_path

    return proof_html_path(project_dir, run_id)


def register_pow_command(main: click.Group) -> None:
    """Register `otto pow` on the main CLI group."""

    @main.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("run_id", required=False)
    @click.option("--print", "print_only", is_flag=True, help="Print the PoW path instead of opening it")
    def pow(run_id: str | None, print_only: bool) -> None:
        """Compatibility alias for `otto proof open/path`."""
        require_git()
        project_dir = resolve_project_dir(Path.cwd())
        try:
            pow_path = _pow_html_path(project_dir, run_id)
        except FileNotFoundError as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(1)

        if print_only:
            click.echo(str(pow_path))
            return

        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            result = subprocess.run([opener, str(pow_path)], check=False, capture_output=True, text=True)
        except OSError as exc:
            error_console.print(
                f"[error]Failed to launch {opener}: {rich_escape(str(exc))}[/error]\n"
                "  Use `otto proof path` to print the report path."
            )
            sys.exit(1)
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            error_console.print(
                f"[error]Failed to open proof-of-work with {opener}.[/error]"
                + (f"\n  {rich_escape(details)}" if details else "")
                + "\n  Use `otto proof path` to print the report path."
            )
            sys.exit(1)
