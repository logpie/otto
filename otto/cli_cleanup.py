"""Thin cleanup CLI for terminal or abandoned non-queue Mission Control records."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from otto.display import CONTEXT_SETTINGS, console, rich_escape
from otto.theme import error_console


def register_cleanup_command(main: click.Group) -> None:
    @main.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("run_id")
    def cleanup(run_id: str) -> None:
        """Remove one terminal or abandoned live Mission Control record."""
        from otto.runs.registry import cleanup_live_record

        project_dir = Path.cwd()
        try:
            record = cleanup_live_record(project_dir, run_id)
        except FileNotFoundError:
            error_console.print(f"[error]No live record found for {rich_escape(run_id)}[/error]")
            sys.exit(2)
        except ValueError as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(2)
        console.print(
            f"  [success]Removed[/success] live record [info]{record.run_id}[/info] "
            f"({record.status}); history preserved."
        )
