"""`otto run` — STUB (legacy pipeline removed).

The legacy intent-to-product pipeline (compile_spec -> run_build ->
run_merge_queue -> run_audit -> render_run) has been deleted. All callers
should use ``otto v5 run`` instead.

This module is kept as a thin shim because ``otto/cli.py`` imports
``register_run_command`` here. The stubbed ``otto run`` command prints
a friendly migration message and exits 1.
"""

from __future__ import annotations

import sys

import click

from otto.display import CONTEXT_SETTINGS
from otto.theme import error_console

_LEGACY_REMOVED_MSG = (
    "[error]The legacy `otto run` pipeline has been removed. "
    "Use `otto v5 run \"<intent>\"` instead.[/error]"
)


def _exit_legacy_removed() -> None:
    """Hard-error landing pad for the deleted legacy pipeline."""
    error_console.print(_LEGACY_REMOVED_MSG)
    sys.exit(1)


def register_run_command(main: click.Group) -> None:
    """Register the stubbed `otto run` command.

    Accepts arbitrary args so users who still pass legacy flags get the
    migration message instead of a Click usage error.
    """

    @main.command(
        "run",
        context_settings={**CONTEXT_SETTINGS, "ignore_unknown_options": True},
    )
    @click.argument("args", nargs=-1, type=click.UNPROCESSED)
    def run(args: tuple[str, ...]) -> None:  # noqa: ARG001
        """[REMOVED] Use `otto v5 run` instead."""
        _exit_legacy_removed()


__all__ = ["register_run_command"]
