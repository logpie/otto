"""Otto CLI — history command for build log inspection."""

import json
import sys
from datetime import datetime
from pathlib import Path

import click

from otto.display import CONTEXT_SETTINGS, console, format_cost, format_duration, rich_escape
from otto.config import require_git, resolve_project_dir
from otto.history import command_family, normalize_command_label
from otto import paths
from otto.runs.history import load_project_history_rows


def _is_merge_cert_session(project_dir: Path, run_id: str) -> bool:
    if not run_id:
        return False
    summary_path = paths.session_summary(project_dir, run_id)
    if not summary_path.exists():
        return False
    try:
        summary = json.loads(summary_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return bool(summary.get("merged_from"))


def _load_history_entries(project_dir: Path, *, limit_hint: int = 50) -> list[dict]:
    del limit_hint
    return load_project_history_rows(project_dir)


def register_history_command(main: click.Group) -> None:
    """Register the history command on the main CLI group."""

    @main.command(context_settings=CONTEXT_SETTINGS)
    @click.option("-n", "--limit", "limit_", default=20, help="Number of builds to show")
    @click.option(
        "--command",
        "command_filter",
        type=click.Choice(["all", "build", "certify", "improve"], case_sensitive=False),
        default="all",
        show_default=True,
        help="Filter history by command family",
    )
    def history(limit_, command_filter):
        """Show build history."""
        from rich.table import Table
        from rich.text import Text

        require_git()
        project_dir = resolve_project_dir(Path.cwd())
        entries = _load_history_entries(project_dir, limit_hint=max(limit_ * 3, 50))

        if not entries:
            console.print("[dim]No build history. Run 'otto build' to get started.[/dim]")
            return

        if command_filter != "all":
            entries = [
                entry for entry in entries
                if command_family(entry.get("command") or "build") == command_filter
            ]
            if not entries:
                console.print(f"[dim]No {command_filter} history found.[/dim]")
                return

        entries.reverse()
        entries = entries[:limit_]

        table = Table(show_header=True, box=None, pad_edge=False, show_edge=False, expand=False)
        table.add_column("#", width=3, justify="right")
        table.add_column("Date", width=12)
        table.add_column("Cmd", width=24)
        table.add_column("Result", width=10)
        table.add_column("Stories", width=8, justify="right")
        table.add_column("Cost", width=8, justify="right", style="dim")
        table.add_column("Time", width=8, justify="right", style="dim")
        table.add_column("Intent", ratio=1, no_wrap=True)

        for i, entry in enumerate(entries):
            ts = entry.get("timestamp", "?")
            try:
                dt = datetime.fromisoformat(ts)
                ts_str = dt.strftime("%m-%d %H:%M")
            except (ValueError, TypeError):
                ts_str = str(ts)[:12]

            # v3 format (stories) or legacy format (tasks)
            stories_tested = entry.get("stories_tested", entry.get("tasks_total", 0))
            stories_passed = entry.get(
                "stories_passed",
                entry.get("tasks_passed", entry.get("passed_count", 0) + entry.get("warn_count", 0)),
            )
            passed = entry.get("passed", stories_passed == stories_tested and stories_tested > 0)
            cost = entry.get("cost_usd", 0.0)
            duration = entry.get("duration_s", entry.get("time_s", 0.0))
            intent = entry.get("intent", entry.get("failure_summary", ""))
            rounds = entry.get("certify_rounds", 1)
            command_label = normalize_command_label(entry.get("command") or "build")
            if _is_merge_cert_session(project_dir, str(entry.get("run_id") or "")):
                command_label += " [merge-cert]"

            result_text = "PASS" if passed else "FAIL"
            result_style = "success" if passed else "red"
            if rounds > 1:
                result_text += f" ({rounds}r)"

            stories_str = f"{stories_passed}/{stories_tested}" if stories_tested else "-"
            cost_str = format_cost(cost) if cost > 0 else ""
            time_str = format_duration(duration) if duration > 0 else ""
            intent_str = rich_escape(intent[:60]) if intent else ""

            num = len(entries) - i

            table.add_row(
                str(num),
                ts_str,
                rich_escape(command_label[:24]),
                Text(result_text, style=result_style),
                stories_str,
                cost_str,
                time_str,
                intent_str,
            )

        console.print()
        console.print(table)
        console.print()


def register_replay_command(main: click.Group) -> None:
    """Register `otto replay <session-id>` — regenerate narrative.log
    from messages.jsonl via the current formatter. Use after upgrading
    otto to re-render old sessions with the new layout/glyphs.
    """

    @main.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("session_id", required=False)
    def replay(session_id):
        """Regenerate narrative.log for a session from messages.jsonl.

        Without an argument, replays the most recent session. Writes
        narrative.regenerated.log alongside each messages.jsonl.
        """
        require_git()
        project_dir = resolve_project_dir(Path.cwd())
        from otto import paths as _paths
        from otto.replay import replay_session

        if not session_id:
            latest = _paths.resolve_pointer(project_dir, _paths.LATEST_POINTER)
            if latest is None:
                console.print("[yellow]No recent session found.[/yellow]")
                sys.exit(1)
            session_id = latest.name

        console.print(f"\n  [bold]Replaying session[/bold] {session_id}\n")
        try:
            written = replay_session(project_dir, session_id)
        except FileNotFoundError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            sys.exit(1)
        console.print(
            f"\n  Regenerated {len(written)} narrative file(s). "
            f"Open them alongside the original narrative.log to compare.\n"
        )
