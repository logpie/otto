"""Otto CLI — entrypoint for all otto commands."""

import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any, cast

# Clear CLAUDECODE at startup so otto can run from inside Claude Code sessions.
# Without this, agent SDK query() spawns a Claude Code subprocess that detects
# the env var and refuses to start ("cannot launch inside another session").
os.environ.pop("CLAUDECODE", None)

import click

from otto.config import (
    ConfigError,
    _normalize_intent,
    agent_effort,
    agent_provider,
    detect_project_kind,
    effective_agent_model,
    require_git,
    resolve_project_dir,
)
from otto.cli_options import (
    max_turns_option,
    positive_budget_option,
    rounds_option,
)
from otto.display import CONTEXT_SETTINGS, console, rich_escape
from otto.theme import error_console


def _check_venv_guard(
    *,
    cwd: str,
    otto_src: str,
    queue_runner_env: str | None,
    cwd_repo_root: str | None = None,
    otto_repo_root: str | None = None,
    cwd_git_dir: str | None = None,
    cwd_git_common_dir: str | None = None,
) -> tuple[bool, str | None]:
    """Pure logic for the worktree-venv guard. Returns (should_block, error_message).

    Catches the shared-venv bug where a user runs otto from inside a worktree
    but the otto package is loaded from the main repo's venv. Bypassed by
    OTTO_INTERNAL_QUEUE_RUNNER=1 for queue-runner-spawned child processes.

    Extracted for testability — see tests/test_env_bypass.py.
    """
    def _norm(path_str: str | None) -> str | None:
        if not path_str:
            return None
        return str(Path(path_str).expanduser().resolve(strict=False))

    def _looks_like_linked_worktree(path_str: str) -> bool:
        normalized = _norm(path_str) or path_str
        marker = f"{os.sep}.worktrees{os.sep}"
        return marker in normalized

    def _same_project() -> bool:
        normalized_cwd_root = _norm(cwd_repo_root)
        normalized_otto_root = _norm(otto_repo_root)
        if normalized_cwd_root and normalized_otto_root:
            return normalized_cwd_root == normalized_otto_root
        normalized_cwd = _norm(cwd) or cwd
        normalized_otto = _norm(otto_src) or otto_src
        cwd_path = Path(normalized_cwd)
        otto_path = Path(normalized_otto)
        return otto_path.is_relative_to(cwd_path)

    normalized_git_dir = _norm(cwd_git_dir)
    normalized_common_dir = _norm(cwd_git_common_dir)
    in_linked_worktree = (
        normalized_git_dir is not None
        and normalized_common_dir is not None
        and normalized_git_dir != normalized_common_dir
    )
    if not in_linked_worktree:
        in_linked_worktree = _looks_like_linked_worktree(cwd)

    if in_linked_worktree and not _same_project():
        if queue_runner_env != "1":
            return (True, (
                f"ERROR: otto loaded from {otto_src}\n"
                f"  but cwd is a linked worktree ({cwd}).\n"
                f"  Use the worktree's own venv: .venv/bin/otto\n"
                f"  (or set OTTO_INTERNAL_QUEUE_RUNNER=1 if you are the queue runner)"
            ))
    return (False, None)


def _git_rev_parse(path: Path, arg: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", arg],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value:
        return None
    resolved = Path(value)
    if not resolved.is_absolute():
        resolved = (path / resolved).resolve()
    else:
        resolved = resolved.resolve()
    return str(resolved)


def _resolve_git_worktree_context(path: Path) -> dict[str, str] | None:
    repo_root = _git_rev_parse(path, "--show-toplevel")
    git_dir = _git_rev_parse(path, "--git-dir")
    git_common_dir = _git_rev_parse(path, "--git-common-dir")
    if repo_root is None or git_dir is None or git_common_dir is None:
        return None
    return {
        "repo_root": repo_root,
        "git_dir": git_dir,
        "git_common_dir": git_common_dir,
    }


def _version_callback(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    """Print otto version + git commit + branch + source path, then exit."""
    if not value or ctx.resilient_parsing:
        return
    import subprocess as _sp
    import otto as _otto_pkg
    src = Path(_otto_pkg.__file__).resolve().parent
    tree = src.parent  # repo root (src is .../otto)

    def _git(args: list[str]) -> str:
        try:
            r = _sp.run(["git", "-C", str(tree)] + args,
                        capture_output=True, text=True, timeout=2)
            return r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, _sp.SubprocessError):
            return ""

    from otto.merge.git_ops import try_current_branch, try_head_sha

    head = try_head_sha(tree)
    commit = (head[:7] if head else "") or "unknown"
    branch = try_current_branch(tree) or "unknown"
    dirty = " (dirty)" if _git(["status", "--porcelain"]) else ""
    try:
        from importlib.metadata import version as _pkg_version
        pkg_ver = _pkg_version("otto")
    except Exception:
        pkg_ver = "dev"
    click.echo(f"otto {pkg_ver}  —  {branch}@{commit}{dirty}")
    click.echo(f"  source: {src}")
    ctx.exit(0)


@click.group(context_settings=CONTEXT_SETTINGS)
@click.option("--version", is_flag=True, expose_value=False, is_eager=True,
              callback=_version_callback,
              help="Show version, git commit, branch, and source path.")
def main():
    """Otto — build and certify software products.

    Run 'otto COMMAND -h' for command-specific options.
    """
    # Phase 1.5: scoped venv guard. See _check_venv_guard() for the logic.
    # After accepting the bypass, POP the env var so any nested subprocess
    # (Claude SDK spawn, codex subprocess, agent tools) does NOT inherit it
    # — the bypass is one-level-deep by design.
    import otto as _otto_pkg
    try:
        _cwd = str(Path.cwd().resolve())
    except FileNotFoundError:
        click.echo(
            "ERROR: current directory no longer exists (deleted out from "
            "under the shell). cd to a real directory and retry.",
            err=True,
        )
        sys.exit(1)
    cwd_context = _resolve_git_worktree_context(Path(_cwd)) or {}
    otto_context = _resolve_git_worktree_context(Path(_otto_pkg.__file__).resolve().parent) or {}
    queue_runner_env = os.environ.get("OTTO_INTERNAL_QUEUE_RUNNER")
    should_block, msg = _check_venv_guard(
        cwd=_cwd,
        otto_src=str(Path(_otto_pkg.__file__).resolve().parent),
        queue_runner_env=queue_runner_env,
        cwd_repo_root=cwd_context.get("repo_root"),
        otto_repo_root=otto_context.get("repo_root"),
        cwd_git_dir=cwd_context.get("git_dir"),
        cwd_git_common_dir=cwd_context.get("git_common_dir"),
    )
    if should_block:
        click.echo(msg, err=True)
        sys.exit(1)
    from otto.queue.runtime import set_queue_runner_child

    set_queue_runner_child(queue_runner_env == "1")
    # Scope the bypass to ONE level — strip from env now so nested subprocesses
    # do not inherit it. Safe to do unconditionally (no-op if not set).
    os.environ.pop("OTTO_INTERNAL_QUEUE_RUNNER", None)


@main.command(context_settings=CONTEXT_SETTINGS)
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind address.")
@click.option("--port", default=8765, show_default=True, type=int, help="Bind port.")
@click.option("--open/--no-open", "open_browser", default=True, show_default=True,
              help="Open the browser after the server starts.")
@click.option("--allow-remote", is_flag=True,
              help="Allow binding to a non-localhost address.")
@click.option("--project-launcher", is_flag=True,
              help="Start at the managed project launcher instead of selecting the current directory.")
@click.option("--projects-root", default="~/otto-projects", show_default=True,
              type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
              help="Managed projects root used by the launcher.")
@click.option("--cwd-project", is_flag=True,
              help="Use the current directory as the active project even for remote binds.")
def dashboard(
    host: str,
    port: int,
    open_browser: bool,
    allow_remote: bool,
    project_launcher: bool,
    projects_root: Path,
    cwd_project: bool,
) -> None:
    """Compatibility alias for `otto web`."""
    console.print("  `otto dashboard` is deprecated; opening web Mission Control.")
    _run_web_command(
        host=host,
        port=port,
        open_browser=open_browser,
        allow_remote=allow_remote,
        project_launcher=project_launcher,
        projects_root=projects_root,
        cwd_project=cwd_project,
    )


@main.command("web", context_settings=CONTEXT_SETTINGS)
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind address.")
@click.option("--port", default=8765, show_default=True, type=int, help="Bind port.")
@click.option("--open/--no-open", "open_browser", default=True, show_default=True,
              help="Open the browser after the server starts.")
@click.option("--allow-remote", is_flag=True,
              help="Allow binding to a non-localhost address.")
@click.option("--project-launcher", is_flag=True,
              help="Start at the managed project launcher instead of selecting the current directory.")
@click.option("--projects-root", default="~/otto-projects", show_default=True,
              type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
              help="Managed projects root used by the launcher.")
@click.option("--cwd-project", is_flag=True,
              help="Use the current directory as the active project even for remote binds.")
def web_command(
    host: str,
    port: int,
    open_browser: bool,
    allow_remote: bool,
    project_launcher: bool,
    projects_root: Path,
    cwd_project: bool,
) -> None:
    """Open local web Mission Control for this project."""
    _run_web_command(
        host=host,
        port=port,
        open_browser=open_browser,
        allow_remote=allow_remote,
        project_launcher=project_launcher,
        projects_root=projects_root,
        cwd_project=cwd_project,
    )


@main.command("clean-verify", context_settings=CONTEXT_SETTINGS)
@click.option("--json", "json_output", is_flag=True, help="Emit JSON oracle result.")
@click.option(
    "--verify-scope",
    type=click.Choice(["scaffold", "subtree", "full"]),
    default="subtree",
    show_default=True,
    help="Clean verifier depth; independent from repair-unit id or phase.",
)
@click.option(
    "--repair-packet",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Repair packet snapshot path; also read from OTTO_REPAIR_PACKET_PATH.",
)
@click.option(
    "--spec-path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Flat spec path containing behavior_journeys for journey probes.",
)
@click.option(
    "--journey-scope",
    type=click.Choice(["leaf", "subtree_integration", "root_integration"]),
    default=None,
    help="Behavior journey execution scope for clean-oracle probes.",
)
@click.option(
    "--journey-artifact-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Directory for UI journey probe artifacts.",
)
def clean_verify_command(
    json_output: bool,
    verify_scope: str,
    repair_packet: Path | None,
    spec_path: Path | None,
    journey_scope: str | None,
    journey_artifact_dir: Path | None,
) -> None:
    """Run the deterministic clean-deploy oracle for this worktree."""
    from otto.v5_clean_verify import Scope, verify_from_clean_oracle

    env_spec = os.environ.get("OTTO_CLEAN_VERIFY_SPEC_PATH", "").strip()
    env_journey_scope = os.environ.get("OTTO_CLEAN_VERIFY_JOURNEY_SCOPE", "").strip()
    env_artifact_dir = os.environ.get("OTTO_CLEAN_VERIFY_JOURNEY_ARTIFACT_DIR", "").strip()
    packet_path = repair_packet
    if packet_path is None:
        env_packet = os.environ.get("OTTO_REPAIR_PACKET_PATH", "").strip()
        packet_path = Path(env_packet) if env_packet else None
    env_worktree = os.environ.get("OTTO_CLEAN_VERIFY_WORKTREE", "").strip()
    project_dir = Path(env_worktree) if packet_path is not None and env_worktree else Path.cwd()
    result = verify_from_clean_oracle(
        project_dir,
        scope=cast(Scope, verify_scope),
        spec_path=spec_path or (Path(env_spec) if env_spec else None),
        journey_scope=cast(
            Any,
            journey_scope or env_journey_scope or "subtree_integration",
        ),
        journey_artifact_dir=(
            journey_artifact_dir
            or (Path(env_artifact_dir) if env_artifact_dir else None)
        ),
    )
    if packet_path is not None:
        from otto.v5_preflight_repair import append_repair_packet_oracle_event

        append_repair_packet_oracle_event(packet_path, result, source="cli")
    payload = result.to_jsonable()
    if json_output:
        click.echo(json.dumps(payload, sort_keys=True))
    else:
        click.echo("pass" if result.passed else "fail")
        if not result.passed:
            for issue in result.issues:
                click.echo(f"{issue.kind}: {issue.message}", err=True)
    if not result.passed:
        sys.exit(1)


def _run_web_command(
    *,
    host: str,
    port: int,
    open_browser: bool,
    allow_remote: bool,
    project_launcher: bool,
    projects_root: Path,
    cwd_project: bool,
) -> None:
    remote_bind = host not in {"127.0.0.1", "localhost", "::1"}
    if remote_bind and not allow_remote:
        error_console.print(
            "[error]Refusing to bind web Mission Control outside localhost without --allow-remote.[/error]"
        )
        sys.exit(2)

    from threading import Timer
    import webbrowser

    import uvicorn

    from otto.web.app import create_app

    project_dir = Path.cwd()
    launcher_mode = project_launcher or (remote_bind and not cwd_project)
    if remote_bind and launcher_mode:
        console.print(
            "  Remote web access starts in the managed project launcher. "
            "Use --cwd-project only when you intentionally want this directory as the active project."
        )
    app = create_app(project_dir, project_launcher=launcher_mode, projects_root=projects_root)
    url_host = "localhost" if host in {"127.0.0.1", "::1"} else host
    url = f"http://{url_host}:{port}/"
    console.print(f"  Web Mission Control: [info]{rich_escape(url)}[/info]")
    if open_browser:
        Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="info")


def _load_yaml_raw(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    import yaml as _yaml

    try:
        raw = _yaml.safe_load(config_path.read_text()) or {}
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _agent_setting_source(
    *,
    yaml_raw: dict[str, Any],
    cli_sources: dict[str, str],
    agent_type: str,
    key: str,
) -> str:
    scoped_key = f"agents.{agent_type}.{key}"
    if scoped_key in cli_sources:
        return cli_sources[scoped_key]
    if key in cli_sources:
        return cli_sources[key]
    raw_agents = yaml_raw.get("agents", {})
    if isinstance(raw_agents, dict):
        raw_agent = raw_agents.get(agent_type, {})
        if isinstance(raw_agent, dict) and raw_agent.get(key) not in (None, ""):
            return f"agents.{agent_type}.{key}"
    if key in yaml_raw:
        return "yaml"
    return "default"


def _format_elapsed_compact(seconds: float) -> str:
    total = max(0, int(seconds))
    if total >= 3600:
        hours, rem = divmod(total, 3600)
        minutes, secs = divmod(rem, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}"
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


def _format_budget_value(seconds: Any) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    if total < 60:
        return f"{total}s"
    minutes, rem = divmod(total, 60)
    if rem == 0:
        return f"{minutes}m"
    return f"{minutes}m {rem:02d}s"


def _runtime_model_name(provider: str | None) -> str | None:
    provider = (provider or "").strip().lower()
    if provider in {"codex", "codex-app-server"}:
        for path in (
            Path.home() / ".codex" / "config.toml",
            Path.home() / ".config" / "codex" / "config.toml",
        ):
            try:
                data = tomllib.loads(path.read_text())
            except (OSError, tomllib.TOMLDecodeError):
                continue
            model = data.get("model")
            if isinstance(model, str) and model.strip():
                return model.strip()
    if provider == "claude":
        for path in (
            Path.home() / ".claude" / "settings.json",
            Path.home() / ".claude" / "settings.local.json",
        ):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            for key in ("model", "defaultModel"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _config_source(key: str, cli_sources: dict[str, str], yaml_raw: dict[str, Any]) -> str:
    if key in cli_sources:
        return cli_sources[key]
    if key in yaml_raw:
        return "yaml"
    return "default"


def _render_config_value(value: Any, source: str, *, show_default_suffix: bool) -> str:
    label = rich_escape(str(value))
    if source == "default" and not show_default_suffix:
        return label
    if source == "default":
        return f"{label} [dim](default)[/dim]"
    if source == "yaml":
        return f"{label} [dim](yaml)[/dim]"
    return f"{label} [dim]({rich_escape(source)})[/dim]"


def _print_config_banner(
    console_: Any,
    config: dict[str, Any],
    cli_sources: dict[str, str],
    config_path: Path,
    *,
    primary_agent_type: str | None = None,
) -> None:
    """Print the resolved configuration with concise source labeling."""
    from rich.table import Table

    yaml_raw = _load_yaml_raw(config_path)
    model_value = effective_agent_model(config, primary_agent_type) or _runtime_model_name(
        str(config.get("provider") or "")
    )

    rows: list[tuple[str, Any, str]] = [
        ("Execution", "split" if config.get("split_mode") else "agentic", "split_mode"),
        ("Mode", config.get("certifier_mode"), "certifier_mode"),
        ("Time budget", _format_budget_value(config.get("run_budget_seconds")), "run_budget_seconds"),
        ("Provider", config.get("provider"), "provider"),
        ("Max build rounds", config.get("max_certify_rounds"), "max_certify_rounds"),
        ("Max turns", config.get("max_turns_per_call"), "max_turns_per_call"),
    ]
    if model_value:
        rows.insert(3, ("Model", model_value, "model"))

    all_default = all(_config_source(key, cli_sources, yaml_raw) == "default" for _, _, key in rows)

    table = Table(box=None, show_header=False, pad_edge=False, show_edge=False, expand=False)
    table.add_column(justify="left", no_wrap=True)
    table.add_column(justify="left", no_wrap=True)
    table.add_column(justify="left", no_wrap=True)
    table.add_column(justify="left", no_wrap=True)

    pairs: list[tuple[tuple[str, Any, str], tuple[str, Any, str] | None]] = list(
        zip(rows[::2], rows[1::2], strict=False)
    )
    if len(rows) % 2 == 1:
        pairs.append((rows[-1], None))

    for left, right in pairs:
        left_label, left_value, left_key = left
        row = [
            f"  {left_label}",
            _render_config_value(
                left_value,
                _config_source(left_key, cli_sources, yaml_raw),
                show_default_suffix=not all_default,
            ),
        ]
        if right is None:
            row.extend(["", ""])
        else:
            right_label, right_value, right_key = right
            row.extend([
                right_label,
                _render_config_value(
                    right_value,
                    _config_source(right_key, cli_sources, yaml_raw),
                    show_default_suffix=not all_default,
                ),
            ])
        table.add_row(*row)

    console_.print(table)

    for key, label, resolver in (
        ("provider", "Agent providers", agent_provider),
        ("model", "Agent models", effective_agent_model),
        ("effort", "Agent efforts", agent_effort),
    ):
        global_value = resolver(config)
        entries: list[str] = []
        raw_agent_types = config.get("_agent_types_for_banner")
        agent_types = (
            tuple(str(item) for item in raw_agent_types)
            if isinstance(raw_agent_types, (list, tuple))
            else ("build", "certifier", "spec", "fix")
        )
        raw_agent_labels = config.get("_agent_label_overrides")
        agent_labels = raw_agent_labels if isinstance(raw_agent_labels, dict) else {}
        for agent_type in agent_types:
            value = resolver(config, agent_type)
            if value == global_value:
                continue
            source = _agent_setting_source(
                yaml_raw=yaml_raw,
                cli_sources=cli_sources,
                agent_type=agent_type,
                key=key,
            )
            entries.append(
                f"{agent_labels.get(agent_type, agent_type)}="
                f"{_render_config_value(value, source, show_default_suffix=not all_default)}"
            )
        if entries:
            console_.print(f"  {label}: " + ", ".join(entries))

    if config.get("memory"):
        try:
            from otto.memory import load_history

            findings_count = sum(len(entry.get("findings", []) or []) for entry in load_history(config_path.parent))
        except Exception:
            findings_count = 0
        if findings_count > 0:
            source = _config_source("memory", cli_sources, yaml_raw)
            source_label = "otto.yaml:memory: true" if source == "yaml" else "memory: true"
            console_.print(f"  • cross-run memory: {findings_count} prior findings loaded ({source_label})")

    if all_default:
        console_.print("  [dim](all defaults — override with --model, --budget, --rounds, etc.)[/dim]")


def _print_startup_context(console_: Any, project_dir: Path, run_id: str) -> None:
    from otto import paths as _paths

    session_dir = _paths.session_dir(project_dir, run_id)
    try:
        session_display = session_dir.relative_to(project_dir)
    except ValueError:
        session_display = session_dir

    console_.print("  Working on:")
    console_.print(f"    Project: {project_dir.resolve()}")
    console_.print(f"    Session: {session_display}")
    console_.print("  Live log: otto_logs/latest/build/narrative.log  (tail in another terminal for full detail)")


def _new_run_id(project_dir: "Path | None" = None) -> str:
    """Unified session_id allocation (see otto.paths.new_session_id)."""
    injected = os.environ.get("OTTO_RUN_ID", "").strip()
    if injected:
        return injected
    if project_dir is None:
        # Fallback for callers that don't pass project_dir (legacy tests).
        import secrets
        stamp = time.strftime("%Y-%m-%d-%H%M%S")
        return f"{stamp}-{secrets.token_hex(3)}"
    from otto.runs.registry import allocate_run_id
    return allocate_run_id(project_dir)


def _exit_legacy_build_removed() -> None:
    """Phase C.3: legacy v3 build pipeline (build_agentic_v3 + run_certify_fix_loop)
    is gone. Point users at the new --i2p path (now the default in
    ``otto/config.py::default_pipeline``).
    """
    error_console.print(
        "[error]Legacy v3 build pipeline has been removed in Phase C. "
        "Use --i2p (default) or pin --legacy in older otto versions.[/error]"
    )
    sys.exit(1)


def _exit_legacy_certify_removed() -> None:
    """Phase C.2: legacy ``run_agentic_certifier`` dispatch is gone. Point
    users at the new ``--i2p`` path (default in
    ``otto/config.py::default_pipeline``).

    Mirrors ``_exit_legacy_build_removed`` for the certify subcommand —
    one helper per subcommand keeps each migration message close to
    its trigger so log readers don't have to hunt across modules.
    """
    error_console.print(
        "[error]Legacy certify pipeline has been removed in Phase C. "
        "Use --i2p (default) or pin --legacy in older otto versions.[/error]"
    )
    sys.exit(1)


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("intent", required=False)
@click.option("--no-qa", is_flag=True, help="Skip product certification after build")
@click.option("--fast", is_flag=True, help="Fast certification — happy path smoke test only (default)")
@click.option("--standard", "standard_", is_flag=True, help="Standard certification — Must-Have + generic CRUD/edge/access checklist")
@click.option("--thorough", is_flag=True, help="Thorough certification — adversarial edge cases + code review")
@click.option("--split", is_flag=True, help="Split mode: system-controlled certify loop with build journal")
@click.option("--agentic", is_flag=True, help="Agentic mode: one provider session owns build, certify, and fix")
@click.option("--rounds", "-n", default=None, type=int, callback=rounds_option, help="Max certification rounds, 1-50 (default from otto.yaml or 8)")
@click.option("--budget", default=None, type=int, callback=positive_budget_option, help="Total wall-clock budget in seconds, must be > 0 (default from otto.yaml or 3600)")
@click.option("--max-turns", default=None, type=int, callback=max_turns_option, help="Max agent turns per call, 1-200 (default from otto.yaml or 200)")
@click.option("--model", default=None, help="Override model for every agent (e.g. sonnet, haiku, gpt-5)")
@click.option("--provider", default=None, help="Override provider for every agent: codex-app-server | codex | claude")
@click.option("--effort", default=None, help="Override effort level for every agent: low | medium | high | max")
@click.option("--build-provider", default=None, help="Override provider for the build phase")
@click.option("--build-model", default=None, help="Override model for the build phase")
@click.option("--build-effort", default=None, help="Override effort for the build phase")
@click.option("--certifier-provider", default=None, help="Override provider for the certifier phase")
@click.option("--certifier-model", default=None, help="Override model for the certifier phase")
@click.option("--certifier-effort", default=None, help="Override effort for the certifier phase")
@click.option("--fix-provider", default=None, help="Override provider for the fix phase")
@click.option("--fix-model", default=None, help="Override model for the fix phase")
@click.option("--fix-effort", default=None, help="Override effort for the fix phase")
@click.option("--strict", is_flag=True, help="Require two consecutive PASS rounds before stopping")
@click.option("--verbose", is_flag=True, help="Show detailed live progress, including tool-call counts")
@click.option("--debug-unredacted", is_flag=True, help="Also write unredacted raw logs under sessions/<id>/raw/ (do not share)")
@click.option("--resume", is_flag=True, help="Resume from last checkpoint (requires an in-progress run)")
@click.option(
    "--reset-budget",
    "reset_budget",
    is_flag=True,
    help="On --resume, do NOT count the prior attempt's spend against the budget cap (i2p only).",
)
@click.option(
    "--force-cross-command-resume",
    is_flag=True,
    help="Allow --resume to reuse a checkpoint written by a different otto command",
)
@click.option("--spec", is_flag=True, help="Generate a reviewable spec before building")
@click.option("--spec-file", type=click.Path(exists=False, dir_okay=False, path_type=Path),
              default=None, help="Use a pre-written spec file (implies --yes)")
@click.option("--yes", is_flag=True, help="Auto-approve the generated spec (for CI/scripts)")
@click.option(
    "--spec-review-mode",
    type=click.Choice(["interactive", "web"]),
    default="interactive",
    hidden=True,
)
@click.option("--force", is_flag=True, help="Discard an active paused spec run and start fresh")
@click.option("--in-worktree", "in_worktree", is_flag=True,
              help="Run in an isolated git worktree (./.worktrees/build-<slug>-<date>/) "
                   "instead of modifying the current working tree")
@click.option("--allow-dirty", is_flag=True, help="Proceed even if the repo has local modifications or untracked files")
@click.option("--break-lock", is_flag=True, help="Force-clear the project lock before starting")
@click.option(
    "--i2p",
    is_flag=True,
    help=(
        "Force-route through the new intent-to-product stack (compile_spec → "
        "build → merge → audit → render). Overrides otto.yaml default_pipeline. "
        "Phase B.0 opt-in."
    ),
)
@click.option(
    "--legacy",
    is_flag=True,
    help=(
        "Force-route through the legacy v3 pipeline (build_agentic_v3). "
        "Overrides otto.yaml default_pipeline. Use this if you've migrated "
        "to the i2p default but hit a regression."
    ),
)
@click.option(
    "--review-gate",
    "review_gate",
    is_flag=True,
    help=(
        "Pause after compile and wait for spec.review_approved before "
        "running build. Approve via Mission Control or by re-running "
        "with --resume --auto-approve. Opt-in (off by default) — A13."
    ),
)
@click.option(
    "--auto-approve",
    "auto_approve",
    is_flag=True,
    help=(
        "Explicitly opt out of --review-gate (default behaviour). The "
        "flag exists so scripts can be unambiguous."
    ),
)
@click.option(
    "--gate-timeout",
    "gate_timeout_s",
    type=float,
    default=24 * 60 * 60.0,
    show_default=True,
    help="Wall-clock cap (seconds) on --review-gate before timing out.",
)
def build(intent, no_qa, fast, standard_, thorough, split, agentic, rounds, budget, max_turns, model, provider, effort, build_provider, build_model, build_effort, certifier_provider, certifier_model, certifier_effort, fix_provider, fix_model, fix_effort, strict, verbose, debug_unredacted, resume, reset_budget, force_cross_command_resume, spec, spec_file, yes, spec_review_mode, force, in_worktree, allow_dirty, break_lock, i2p, legacy, review_gate, auto_approve, gate_timeout_s):
    """Compatibility build command.

    Prefer `otto run` for direct intent-to-product runs. `otto build --i2p`
    routes to the same i2p stack while preserving older script entrypoints.

    If a prior build was interrupted, pass --resume to continue it. Intent
    is optional on resume and is inherited from the checkpoint.

    Examples:

        otto build "bookmark manager with tags and search"

        otto build "bookmark manager" --fast    # quick smoke test

        otto build "CLI tool that converts CSV to JSON" --no-qa

        otto build --resume                     # continue interrupted run

    Limits: intent text is capped at 8KB, approved specs at 32KB.
    """
    require_git()
    project_dir = resolve_project_dir(Path.cwd())

    from otto.cli_run import resolve_pipeline_choice
    pipeline_choice = resolve_pipeline_choice(
        i2p_flag=i2p,
        legacy_flag=legacy,
        project_dir=project_dir,
    )
    if pipeline_choice == "i2p":
        # Phase B.0/B.3: route through the new intent-to-product stack.
        # Most legacy --build flags don't map cleanly; surface which
        # knobs the new stack consumes vs ignores so the user isn't
        # surprised. orchestrate_run holds its own lock and exits on
        # completion.
        # In i2p mode, ``--resume`` and ``--force`` map to the new
        # resume planner (see otto/resume.py); the rest remain
        # legacy-only knobs.
        # Legacy Mission Control queue entries used --spec plus
        # --spec-review-mode/--yes before i2p grew first-class flags.
        # Map those spellings onto the real i2p review behavior instead
        # of warning that they are ignored.
        legacy_review_gate = bool(spec and spec_review_mode == "web" and not yes)
        legacy_auto_approve = bool(spec and yes)
        effective_review_gate = bool(review_gate or legacy_review_gate)
        effective_auto_approve = bool(auto_approve or legacy_auto_approve)
        _ignored = [
            name
            for name, val in (
                ("--no-qa", no_qa),
                ("--fast", fast),
                ("--standard", standard_),
                ("--thorough", thorough),
                ("--split", split),
                ("--agentic", agentic),
                ("--rounds", rounds is not None),
                ("--strict", strict),
                ("--force-cross-command-resume", force_cross_command_resume),
                ("--spec", spec and not (legacy_review_gate or legacy_auto_approve)),
                ("--spec-file", spec_file),
                ("--in-worktree", in_worktree),
                ("--allow-dirty", allow_dirty),
            )
            if val
        ]
        if _ignored:
            console.print(
                "  [yellow]i2p mode: these flags are ignored: "
                f"{', '.join(_ignored)}[/yellow]"
            )
        if effective_review_gate and effective_auto_approve:
            raise click.UsageError(
                "--review-gate and --auto-approve are mutually exclusive."
            )
        from otto.cli_run import orchestrate_run
        orchestrate_run(
            intent=intent,
            project_kind=detect_project_kind(project_dir),
            break_lock=break_lock,
            no_build=False,
            base_url=None,
            from_spec=None,
            project_dir=project_dir,
            resume=resume,
            reset_budget=reset_budget,
            force=force,
            review_gate=effective_review_gate,
            gate_timeout_s=gate_timeout_s,
            budget=budget,
            max_turns=max_turns,
            model=model,
            provider=provider,
            effort=effort,
            build_provider=build_provider,
            build_model=build_model,
            build_effort=build_effort,
            certifier_provider=certifier_provider,
            certifier_model=certifier_model,
            certifier_effort=certifier_effort,
            fix_provider=fix_provider,
            fix_model=fix_model,
            fix_effort=fix_effort,
            verbose=verbose,
            debug_unredacted=debug_unredacted,
        )
        return  # orchestrate_run sys.exit's on its own; defensive return

    # Phase C.3: only the i2p path remains. Anything else (legacy flag,
    # or default_pipeline=legacy in otto.yaml) hard-errors with a
    # migration hint — the v3 pipeline (build_agentic_v3 +
    # run_certify_fix_loop) was deleted with this commit.
    _exit_legacy_build_removed()


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("intent", required=False)
@click.option("--thorough", is_flag=True, help="Thorough mode — adversarial edge cases + code review")
@click.option("--fast", is_flag=True, help="Fast mode — happy path smoke test only")
@click.option("--standard", "standard_", is_flag=True, help="Standard mode — Must-Have + generic CRUD/edge/access checklist")
@click.option("--budget", default=None, type=int, callback=positive_budget_option, help="Total wall-clock budget in seconds, must be > 0 (default from otto.yaml or 3600)")
@click.option("--max-turns", default=None, type=int, callback=max_turns_option, help="Max agent turns per call, 1-200 (default from otto.yaml or 200)")
@click.option("--strict", is_flag=True, help="Require two consecutive PASS runs before reporting success")
@click.option("--model", default=None, help="Override model for every agent (e.g. sonnet, haiku, gpt-5)")
@click.option("--provider", default=None, help="Override provider for every agent: codex-app-server | codex | claude")
@click.option("--effort", default=None, help="Override effort level for every agent: low | medium | high | max")
@click.option("--certifier-provider", default=None, help="Override provider for this certification run")
@click.option("--certifier-model", default=None, help="Override model for this certification run")
@click.option("--certifier-effort", default=None, help="Override effort for this certification run")
@click.option("--break-lock", is_flag=True, help="Force-clear the project lock before starting")
@click.option("--resume", is_flag=True, help="Resume the paused i2p audit instead of re-compiling fresh.")
@click.option("--reset-budget", "reset_budget", is_flag=True, help="On --resume, do NOT count the prior attempt's spend against the budget cap.")
@click.option("--force", is_flag=True, help="On --resume, bypass the spec-hash check.")
@click.option(
    "--i2p",
    is_flag=True,
    help=(
        "Force-route through the new intent-to-product stack "
        "(brownfield-compile → audit → render). Overrides otto.yaml "
        "default_pipeline. Phase B.1 opt-in."
    ),
)
@click.option(
    "--legacy",
    is_flag=True,
    help=(
        "Force-route through the legacy certifier (run_agentic_certifier). "
        "Overrides otto.yaml default_pipeline."
    ),
)
def certify(intent, thorough, fast, standard_, budget, max_turns, strict, model, provider, effort, certifier_provider, certifier_model, certifier_effort, break_lock, resume, reset_budget, force, i2p, legacy):
    """Certify a product — independent, builder-blind verification.

    Tests the product in the current directory as a real user. Works on
    any project regardless of how it was built (otto, bare CC, human).

    If no intent is given, reads intent.md or README.md from the project.

    Examples:
        otto certify                   # mode from otto.yaml or built-in fast default
        otto certify --fast            # quick smoke test (~1-2 min)
        otto certify --thorough        # adversarial deep inspection

    Limits: intent text is capped at 8KB.
    """
    require_git()
    project_dir = resolve_project_dir(Path.cwd())

    if sum(bool(x) for x in (fast, standard_, thorough)) > 1:
        error_console.print(
            "[error]--fast, --standard, and --thorough are mutually exclusive.[/error]"
        )
        sys.exit(2)
    intent = _normalize_intent(intent or "")

    from otto.cli_run import resolve_pipeline_choice
    pipeline_choice = resolve_pipeline_choice(
        i2p_flag=i2p,
        legacy_flag=legacy,
        project_dir=project_dir,
    )
    if pipeline_choice == "i2p":
        # Phase B.1/B.3: route through the new intent-to-product stack
        # (brownfield-compile → audit → render). Most legacy certify
        # flags don't map cleanly; surface ignored ones honestly so
        # users aren't surprised. orchestrate_certify holds its own
        # lock and exits on completion.
        _ignored = [
            name
            for name, val in (
                ("--fast", fast),
                ("--standard", standard_),
                ("--thorough", thorough),
                ("--rounds (n/a in i2p)", False),
                ("--strict", strict),
            )
            if val
        ]
        if _ignored:
            console.print(
                "  [yellow]i2p mode: these flags are ignored: "
                f"{', '.join(_ignored)}[/yellow]"
            )
        from otto.cli_run import orchestrate_certify
        orchestrate_certify(
            intent=intent,
            project_kind=detect_project_kind(project_dir),
            break_lock=break_lock,
            project_dir=project_dir,
            resume=resume,
            reset_budget=reset_budget,
            force=force,
            budget=budget,
            max_turns=max_turns,
            model=model,
            provider=provider,
            effort=effort,
            certifier_provider=certifier_provider,
            certifier_model=certifier_model,
            certifier_effort=certifier_effort,
        )
        return  # orchestrate_certify sys.exit's; defensive return

    # Phase C.2 (tick 64): the legacy ``run_agentic_certifier`` dispatch
    # was removed. Anything that wasn't routed to ``orchestrate_certify``
    # above is the legacy ``--legacy`` path — surface the migration
    # message and exit. Mirrors ``--legacy`` handling in cli_improve.
    _exit_legacy_certify_removed()


@main.command("render", context_settings=CONTEXT_SETTINGS)
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
def render_command(session: Path, project_dir: Path | None, rewrite_json: bool) -> None:
    """Compatibility alias for `otto proof render`."""
    try:
        from otto.cli_proof import resolve_render_session_dir
        from otto.render import rerender_proof_packet

        session_dir = resolve_render_session_dir(session, project_dir=project_dir)
        html_path, json_path = rerender_proof_packet(
            session_dir,
            rewrite_json=rewrite_json,
        )
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    console.print(f"  Rendered proof packet: {html_path}")
    if rewrite_json:
        console.print(f"  Rewrote JSON packet: {json_path}")

# Setup command (registered from otto/cli_setup.py)
from otto.cli_setup import register_setup_command  # noqa: E402
register_setup_command(main)

# History command (registered from otto/cli_logs.py)
from otto.cli_logs import register_history_command, register_replay_command  # noqa: E402
register_history_command(main)
register_replay_command(main)

# PoW command (registered from otto/cli_pow.py)
from otto.cli_pow import register_pow_command  # noqa: E402
register_pow_command(main)

# Proof/debug commands (canonical artifact and diagnostic namespaces)
from otto.cli_proof import register_debug_command, register_proof_command  # noqa: E402
register_proof_command(main)
register_debug_command(main)

# Improve commands (registered from otto/cli_improve.py)
from otto.cli_improve import register_improve_commands  # noqa: E402
register_improve_commands(main)

# Queue commands (Phase 2 — registered from otto/cli_queue.py)
from otto.cli_queue import register_queue_commands  # noqa: E402
register_queue_commands(main)

# Cleanup command (Mission Control terminal-record GC)
from otto.cli_cleanup import register_cleanup_command  # noqa: E402
register_cleanup_command(main)

# Run command (intent-to-product pipeline, Phase A: compile-only)
from otto.cli_run import register_run_command  # noqa: E402
register_run_command(main)

from otto.cli_v5 import register_v5_command  # noqa: E402
register_v5_command(main)


if __name__ == "__main__":
    main()
