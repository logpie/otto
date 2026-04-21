"""Otto CLI — history command for build log inspection."""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import click

from otto.display import CONTEXT_SETTINGS, console, format_cost, format_duration, rich_escape
from otto.theme import error_console
from otto import paths

logger = logging.getLogger("otto.cli_logs")

# Legacy history path — still READ for upgrade safety; new writes go to
# otto_logs/cross-sessions/history.jsonl via paths.py.
LEGACY_HISTORY_FILE = "otto_logs/run-history.jsonl"


def _load_history_entries(project_dir: Path) -> list[dict]:
    """Read history entries from new, legacy, and archived sources.

    Preserves chronological order (append-only); de-dups by session_id/build_id.
    """
    sources: list[Path] = [
        paths.history_jsonl(project_dir),
        project_dir / LEGACY_HISTORY_FILE,
    ]
    for archive in paths.archived_pre_restructure_dirs(project_dir):
        sources.append(archive / paths.LEGACY_RUN_HISTORY)

    seen_ids: set[str] = set()
    entries: list[tuple[tuple[float, int, int], dict]] = []
    for source_index, src in enumerate(sources):
        if not src.exists():
            continue
        try:
            fallback_ts = src.stat().st_mtime
        except OSError:
            fallback_ts = 0.0
        try:
            for line_index, line in enumerate(src.read_text().splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # De-dup: prefer first occurrence (new file wins over legacy).
                key = entry.get("session_id") or entry.get("build_id") or ""
                if key and key in seen_ids:
                    continue
                if key:
                    seen_ids.add(key)
                entries.append((
                    _history_sort_key(
                        entry,
                        fallback_ts=fallback_ts,
                        source_index=source_index,
                        line_index=line_index,
                    ),
                    entry,
                ))
        except OSError:
            continue
    entries.sort(key=lambda item: item[0])
    return [entry for _, entry in entries]


def _history_sort_key(
    entry: dict,
    *,
    fallback_ts: float,
    source_index: int,
    line_index: int,
) -> tuple[float, int, int]:
    ts = entry.get("timestamp") or entry.get("started_at") or entry.get("updated_at")
    if isinstance(ts, str) and ts:
        try:
            return (
                datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp(),
                source_index,
                line_index,
            )
        except ValueError:
            logger.warning("Unparseable history timestamp %r; falling back to file mtime", ts)
    else:
        logger.warning("Missing history timestamp; falling back to file mtime")
    return (fallback_ts, source_index, line_index)


def register_history_command(main: click.Group) -> None:
    """Register the history command on the main CLI group."""

    @main.command(context_settings=CONTEXT_SETTINGS)
    @click.option("-n", "--limit", "limit_", default=20, help="Number of builds to show")
    def history(limit_):
        """Show build history."""
        from rich.table import Table
        from rich.text import Text

        project_dir = Path.cwd()
        entries = _load_history_entries(project_dir)

        if not entries:
            console.print("[dim]No build history. Run 'otto build' to get started.[/dim]")
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
        project_dir = Path.cwd()
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
