"""Canonical CLI namespace for proof packets and run artifacts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click

from otto import paths
from otto.config import ConfigError, require_git, resolve_project_dir
from otto.display import CONTEXT_SETTINGS, console, rich_escape
from otto.theme import error_console


def proof_html_path(project_dir: Path, session_id: str | None) -> Path:
    """Return the preferred human-readable proof artifact for a session."""
    if session_id:
        session_dir = paths.session_dir(project_dir, session_id)
        if not session_dir.exists():
            raise FileNotFoundError(f"session not found: {session_id}")
    else:
        latest_session = paths.resolve_pointer(project_dir, paths.LATEST_POINTER)
        if latest_session is None:
            raise FileNotFoundError("no latest session found; run `otto run` first.")
        session_dir = latest_session

    candidates = [
        session_dir / "proof-packet.html",
        session_dir / "certify" / "proof-of-work.html",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    label = session_id or session_dir.name
    raise FileNotFoundError(
        f"proof HTML not found for session {label}; try `otto proof list` "
        "to inspect recent runs."
    )


def resolve_render_session_dir(
    session: Path,
    *,
    project_dir: Path | None = None,
) -> Path:
    """Resolve a proof render argument to an i2p session directory."""
    expanded = session.expanduser()
    if expanded.exists():
        if not expanded.is_dir():
            raise ValueError(f"render target is not a directory: {expanded}")
        return expanded.resolve()
    if any(part in ("..", "") for part in expanded.parts):
        raise ValueError(f"invalid session id: {session}")
    root = (
        project_dir.expanduser().resolve()
        if project_dir is not None
        else resolve_project_dir(Path.cwd())
    )
    from otto import paths as _paths
    candidate = (_paths.sessions_root(root) / str(session)).resolve()
    sessions_root = _paths.sessions_root(root).resolve()
    try:
        candidate.relative_to(sessions_root)
    except ValueError as exc:
        raise ValueError(f"invalid session id: {session}") from exc
    if not candidate.exists() or not candidate.is_dir():
        raise FileNotFoundError(f"session not found: {candidate}")
    return candidate


def open_proof_html(proof_path: Path) -> None:
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        result = subprocess.run(
            [opener, str(proof_path)],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        error_console.print(
            f"[error]Failed to launch {opener}: {rich_escape(str(exc))}[/error]\n"
            "  Use `otto proof path` to print the report path."
        )
        sys.exit(1)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        error_console.print(
            f"[error]Failed to open proof HTML with {opener}.[/error]"
            + (f"\n  {rich_escape(details)}" if details else "")
            + "\n  Use `otto proof path` to print the report path."
        )
        sys.exit(1)


def _render_proof_packet(
    session: Path,
    *,
    project_dir: Path | None,
    rewrite_json: bool,
) -> None:
    session_dir = resolve_render_session_dir(session, project_dir=project_dir)
    from otto.render import rerender_proof_packet

    html_path, json_path = rerender_proof_packet(
        session_dir,
        rewrite_json=rewrite_json,
    )
    console.print(f"  Rendered proof packet: {html_path}")
    if rewrite_json:
        console.print(f"  Rewrote JSON packet: {json_path}")


def register_proof_command(main: click.Group) -> None:
    """Register the canonical `otto proof ...` command group."""

    @main.group("proof", context_settings=CONTEXT_SETTINGS)
    def proof() -> None:
        """Inspect proof packets and run artifacts."""

    @proof.command("open", context_settings=CONTEXT_SETTINGS)
    @click.argument("session_id", required=False)
    def proof_open(session_id: str | None) -> None:
        """Open a proof packet for SESSION_ID, or the latest run."""
        require_git()
        project_dir = resolve_project_dir(Path.cwd())
        try:
            proof_path = proof_html_path(project_dir, session_id)
        except FileNotFoundError as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(1)
        open_proof_html(proof_path)

    @proof.command("path", context_settings=CONTEXT_SETTINGS)
    @click.argument("session_id", required=False)
    def proof_path(session_id: str | None) -> None:
        """Print a proof packet path for SESSION_ID, or the latest run."""
        require_git()
        project_dir = resolve_project_dir(Path.cwd())
        try:
            click.echo(str(proof_html_path(project_dir, session_id)))
        except FileNotFoundError as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(1)

    @proof.command("render", context_settings=CONTEXT_SETTINGS)
    @click.argument("session", type=click.Path(exists=False, path_type=Path))
    @click.option(
        "--project-dir",
        type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
        default=None,
        help=(
            "Project root used when SESSION is a session id. Defaults to the "
            "current git worktree root."
        ),
    )
    @click.option(
        "--rewrite-json",
        is_flag=True,
        help=(
            "Also rewrite proof-packet.json in canonical current format. "
            "By default only proof-packet.html is regenerated."
        ),
    )
    def proof_render(
        session: Path,
        project_dir: Path | None,
        rewrite_json: bool,
    ) -> None:
        """Re-render proof-packet.html for an existing i2p session."""
        try:
            _render_proof_packet(
                session,
                project_dir=project_dir,
                rewrite_json=rewrite_json,
            )
        except (ConfigError, FileNotFoundError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc

    @proof.command("list", context_settings=CONTEXT_SETTINGS)
    @click.option("-n", "--limit", "limit_", default=20, help="Number of runs to show")
    @click.option(
        "--command",
        "command_filter",
        type=click.Choice(["all", "run", "build", "certify", "improve"], case_sensitive=False),
        default="all",
        show_default=True,
        help="Filter history by command family",
    )
    def proof_list(limit_: int, command_filter: str) -> None:
        """Show recent run history."""
        require_git()
        project_dir = resolve_project_dir(Path.cwd())
        from otto.cli_logs import print_history

        print_history(project_dir, limit_=limit_, command_filter=command_filter)

    @proof.command("cleanup", context_settings=CONTEXT_SETTINGS)
    @click.argument("run_id")
    def proof_cleanup(run_id: str) -> None:
        """Remove one terminal or abandoned live Mission Control record."""
        from otto.runs.registry import cleanup_live_record

        try:
            record = cleanup_live_record(Path.cwd(), run_id)
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


def register_debug_command(main: click.Group) -> None:
    """Register `otto debug ...` diagnostics."""

    @main.group("debug", context_settings=CONTEXT_SETTINGS)
    def debug() -> None:
        """Developer diagnostics for existing sessions."""

    @debug.command("narrative", context_settings=CONTEXT_SETTINGS)
    @click.argument("session_id", required=False)
    def debug_narrative(session_id: str | None) -> None:
        """Regenerate narrative logs for a session without rerunning agents."""
        require_git()
        project_dir = resolve_project_dir(Path.cwd())
        from otto.cli_logs import regenerate_narrative

        regenerate_narrative(project_dir, session_id)
