"""`otto list-pending` and `otto review` — manage paused decomposition tasks.

When `otto run --review-first-decomp` is used, the root Lead's emitted
child tasks land in `v5_pending.jsonl` waiting for human approval before
they dispatch. These commands list and act on those tasks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from otto.config import ConfigError, require_git, resolve_project_dir
from otto.display import CONTEXT_SETTINGS, console
from otto.theme import error_console


def register_review_commands(main: click.Group) -> None:
    """Register `otto list-pending` and `otto review`."""

    @main.command("list-pending", context_settings=CONTEXT_SETTINGS)
    def list_pending_cmd() -> None:
        """List tasks awaiting review (--review-first-decomp)."""
        require_git()
        try:
            project_dir = resolve_project_dir(Path.cwd())
        except ConfigError as exc:
            error_console.print(f"[error]{exc}[/error]")
            sys.exit(2)

        from otto.v5_review import list_pending_review

        pending = list_pending_review(project_dir)
        if not pending:
            console.print("[dim]no tasks awaiting review[/dim]")
            return
        for entry in pending:
            console.print(
                f"  {entry.get('task_id', '?')}  "
                f"parent={entry.get('parent_task_id', '?')}  "
                f"intent={(entry.get('intent') or '')[:80]!r}"
            )

    @main.command("review", context_settings=CONTEXT_SETTINGS)
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
        """Manage pending-review tasks.

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


__all__ = ["register_review_commands"]
