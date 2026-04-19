"""Otto CLI — history command for build log inspection."""

import json
import sys
from datetime import datetime
from pathlib import Path

import click

from otto.display import CONTEXT_SETTINGS, console, format_cost, format_duration, rich_escape
from otto.theme import error_console
HISTORY_FILE = "otto_logs/run-history.jsonl"


def register_history_command(main: click.Group) -> None:
    """Register the history command on the main CLI group."""

    @main.command(context_settings=CONTEXT_SETTINGS)
    @click.option("-n", "--limit", "limit_", default=20, help="Number of builds to show")
    def history(limit_):
        """Show build history."""
        from rich.table import Table
        from rich.text import Text

        project_dir = Path.cwd()
        history_path = project_dir / HISTORY_FILE

        if not history_path.exists():
            console.print("[dim]No build history. Run 'otto build' to get started.[/dim]")
            return

        entries = []
        try:
            for line in history_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            error_console.print("Error reading history file", style="error")
            sys.exit(1)

        if not entries:
            console.print("[dim]No build history.[/dim]")
            return

        entries.reverse()
        entries = entries[:limit_]

        table = Table(show_header=True, box=None, pad_edge=False, show_edge=False, expand=False)
        table.add_column("#", width=3, justify="right")
        table.add_column("Date", width=12)
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
            stories_passed = entry.get("stories_passed", entry.get("tasks_passed", 0))
            passed = entry.get("passed", stories_passed == stories_tested and stories_tested > 0)
            cost = entry.get("cost_usd", 0.0)
            duration = entry.get("duration_s", entry.get("time_s", 0.0))
            intent = entry.get("intent", entry.get("failure_summary", ""))
            rounds = entry.get("certify_rounds", 1)

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
                Text(result_text, style=result_style),
                stories_str,
                cost_str,
                time_str,
                intent_str,
            )

        console.print()
        console.print(table)
        console.print()
