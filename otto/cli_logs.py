"""Otto CLI — history command for build log inspection."""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import click

from otto.display import CONTEXT_SETTINGS, console, format_cost, format_duration, rich_escape
from otto.config import require_git, resolve_project_dir
from otto.history import command_family, history_run_id, normalize_command_label, tail_jsonl_entries
from otto import paths

logger = logging.getLogger("otto.cli_logs")

# Legacy history path — still READ for upgrade safety; new writes go to
# otto_logs/cross-sessions/history.jsonl via paths.py.
LEGACY_HISTORY_FILE = "otto_logs/run-history.jsonl"


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
    """Read history entries from new, legacy, and archived sources.

    Preserves chronological order (append-only); de-dups by run_id/session_id/build_id.
    """
    sources: list[Path] = [
        paths.history_jsonl(project_dir),
        project_dir / LEGACY_HISTORY_FILE,
    ]
    for archive in paths.archived_pre_restructure_dirs(project_dir):
        sources.append(archive / paths.LEGACY_RUN_HISTORY)

    entries: list[tuple[tuple[float, int, int], int, int, dict]] = []
    for source_index, src in enumerate(sources):
        if not src.exists():
            continue
        try:
            fallback_ts = src.stat().st_mtime
        except OSError:
            fallback_ts = 0.0
        for line_index, line in tail_jsonl_entries(src, limit=limit_hint):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            entries.append((
                _history_sort_key(
                    entry,
                    fallback_ts=fallback_ts,
                    source_index=source_index,
                    line_index=line_index,
                ),
                source_index,
                line_index,
                entry,
            ))
    selected = _dedupe_history_entries(entries)
    selected.sort(key=lambda item: item[0])
    return [entry for _, _, _, entry in selected]


def _dedupe_history_entries(
    entries: list[tuple[tuple[float, int, int], int, int, dict]],
) -> list[tuple[tuple[float, int, int], int, int, dict]]:
    selected: list[tuple[tuple[float, int, int], int, int, dict]] = []
    selected_keys: set[tuple[str, str]] = set()
    selected_no_command_run_ids: set[str] = set()
    selected_command_run_ids: set[str] = set()

    def preference(item: tuple[tuple[float, int, int], int, int, dict]) -> tuple[int, int, float, int]:
        sort_key, source_index, line_index, entry = item
        is_snapshot = (
            entry.get("schema_version") == 2
            and entry.get("history_kind") == "terminal_snapshot"
        )
        return (1 if is_snapshot else 0, -source_index, sort_key[0], line_index)

    for item in sorted(entries, key=preference, reverse=True):
        _, _, _, entry = item
        run_id = history_run_id(entry)
        raw_command = str(entry.get("command") or "").strip()
        command = normalize_command_label(raw_command) if raw_command else ""
        dedupe_key = str(entry.get("dedupe_key") or "").strip()
        key = ("dedupe", dedupe_key) if dedupe_key else ("run-command", f"{run_id}:{command}")
        if key in selected_keys:
            continue
        if run_id and not command and run_id in selected_command_run_ids:
            continue
        if run_id and command and run_id in selected_no_command_run_ids:
            continue
        selected.append(item)
        selected_keys.add(key)
        if run_id and command:
            selected_command_run_ids.add(run_id)
        elif run_id:
            selected_no_command_run_ids.add(run_id)
    return selected


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
