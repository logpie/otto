"""Otto CLI — entrypoint for all otto commands."""

from contextlib import contextmanager
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

# Clear CLAUDECODE at startup so otto can run from inside Claude Code sessions.
# Without this, agent SDK query() spawns a Claude Code subprocess that detects
# the env var and refuses to start ("cannot launch inside another session").
os.environ.pop("CLAUDECODE", None)

import click

from otto.config import (
    ConfigError,
    PROVIDER_DEFAULT_MODEL_OVERRIDE,
    _normalize_intent,
    agent_effort,
    agent_provider,
    detect_project_kind,
    effective_agent_model,
    load_config,
    normalize_provider,
    require_git,
    resolve_project_dir,
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


def _load_config_or_exit(config_path: Path) -> dict[str, Any]:
    try:
        return load_config(config_path)
    except (ConfigError, ValueError) as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(2)


def _record_cli_override(
    config: dict[str, Any],
    key: str,
    value: Any,
    *,
    agent_type: str | None = None,
) -> None:
    overrides = config.setdefault("_cli_overrides", {})
    if not isinstance(overrides, dict):
        return
    if agent_type:
        agents = overrides.setdefault("agents", {})
        if isinstance(agents, dict):
            agent_overrides = agents.setdefault(agent_type, {})
            if isinstance(agent_overrides, dict):
                agent_overrides[key] = value
        return
    if isinstance(overrides, dict):
        overrides[key] = value


def _provider_changed(current: str | None, override: str | None) -> bool:
    if not override:
        return False
    try:
        next_provider = normalize_provider(override, default=None)
        current_provider = normalize_provider(current, default=None) if current else None
    except ValueError:
        return False
    return bool(next_provider and current_provider and next_provider != current_provider)


def _record_phase_agent_cli_overrides(
    *,
    config: dict[str, Any],
    sources: dict[str, str],
    values: dict[str, dict[str, str | None]],
) -> None:
    for agent_type, overrides in values.items():
        agent_config = config.setdefault("agents", {}).setdefault(agent_type, {})
        if not isinstance(agent_config, dict):
            continue
        previous_provider = agent_provider(config, agent_type)
        if (
            overrides.get("provider")
            and not overrides.get("model")
            and _provider_changed(previous_provider, overrides.get("provider"))
        ):
            _record_cli_override(
                config,
                "model",
                PROVIDER_DEFAULT_MODEL_OVERRIDE,
                agent_type=agent_type,
            )
        for key, value in overrides.items():
            if not value:
                continue
            agent_config[key] = value
            sources[f"agents.{agent_type}.{key}"] = f"--{agent_type}-{key}"
            _record_cli_override(config, key, value, agent_type=agent_type)


def _apply_build_cli_overrides(
    *,
    config: dict[str, Any],
    config_path: Path,
    no_qa: bool,
    fast: bool,
    standard_: bool,
    thorough: bool,
    split: bool,
    agentic: bool,
    rounds: int | None,
    budget: int | None,
    max_turns: int | None,
    model: str | None,
    provider: str | None,
    effort: str | None,
    build_provider: str | None,
    build_model: str | None,
    build_effort: str | None,
    certifier_provider: str | None,
    certifier_model: str | None,
    certifier_effort: str | None,
    fix_provider: str | None,
    fix_model: str | None,
    fix_effort: str | None,
    strict: bool,
    verbose: bool,
    debug_unredacted: bool,
    allow_dirty: bool,
    resume_state: Any,
    spec_file: Path | None,
    intent_source: str,
    intent_fallback_reason: str,
) -> tuple[bool, bool, str, dict[str, str]]:
    """Apply build CLI/checkpoint overrides after any config reload."""
    config["_intent_source"] = intent_source
    config["_intent_fallback_reason"] = intent_fallback_reason
    if spec_file:
        config["_intent_source"] = "cli-argument"

    resolved_skip_qa, skip_qa_source = _resolve_bool_setting(
        config=config,
        config_path=config_path,
        key="skip_product_qa",
        cli_enabled=no_qa,
        cli_label="--no-qa",
    )
    config["skip_product_qa"] = resolved_skip_qa
    if split and agentic:
        raise ConfigError("--split and --agentic are mutually exclusive.")
    resolved_split_bool = False
    sources: dict[str, str] = {}
    if split:
        resolved_split_bool = True
        sources["split_mode"] = "--split"
    elif agentic:
        resolved_split_bool = False
        sources["split_mode"] = "--agentic"
    else:
        resolved_split, _split_source = _resolve_bool_setting(
            config=config,
            config_path=config_path,
            key="split_mode",
            cli_enabled=False,
            cli_label="--split",
        )
        resolved_split_bool = bool(resolved_split)
    if resume_state.resumed and resume_state.split_mode is not None:
        if split and resume_state.split_mode is False:
            raise ConfigError("Cannot change an agentic checkpoint to split mode on resume.")
        if agentic and resume_state.split_mode is True:
            raise ConfigError("Cannot change a split checkpoint to agentic mode on resume.")
        resolved_split_bool = resume_state.split_mode
        sources["split_mode"] = "checkpoint"
    config["split_mode"] = resolved_split_bool

    mode_flag = "fast" if fast else ("standard" if standard_ else ("thorough" if thorough else None))
    if mode_flag:
        config["certifier_mode"] = mode_flag
        sources["certifier_mode"] = f"--{mode_flag}"
    if rounds is not None:
        config["max_certify_rounds"] = rounds
        sources["max_certify_rounds"] = "--rounds"
    elif resume_state.resumed and resume_state.max_rounds:
        config["max_certify_rounds"] = resume_state.max_rounds
        sources["max_certify_rounds"] = "checkpoint"
    if budget is not None:
        config["run_budget_seconds"] = budget
        sources["run_budget_seconds"] = "--budget"
    if max_turns is not None:
        config["max_turns_per_call"] = max_turns
        sources["max_turns_per_call"] = "--max-turns"
    previous_provider = agent_provider(config)
    if model:
        config["model"] = model
        sources["model"] = "--model"
        _record_cli_override(config, "model", model)
    if provider:
        config["provider"] = provider
        sources["provider"] = "--provider"
        _record_cli_override(config, "provider", provider)
        if not model and _provider_changed(previous_provider, provider):
            _record_cli_override(config, "model", PROVIDER_DEFAULT_MODEL_OVERRIDE)
    if effort:
        config["effort"] = effort
        sources["effort"] = "--effort"
        _record_cli_override(config, "effort", effort)
    _record_phase_agent_cli_overrides(
        config=config,
        sources=sources,
        values={
            "build": {"provider": build_provider, "model": build_model, "effort": build_effort},
            "certifier": {
                "provider": certifier_provider,
                "model": certifier_model,
                "effort": certifier_effort,
            },
            "fix": {"provider": fix_provider, "model": fix_model, "effort": fix_effort},
        },
    )
    if strict:
        config["strict_mode"] = True
        sources["strict_mode"] = "--strict"
    if allow_dirty:
        config["allow_dirty_repo"] = True
        sources["allow_dirty_repo"] = "--allow-dirty"
    if debug_unredacted:
        config["debug_unredacted"] = True
        sources["debug_unredacted"] = "--debug-unredacted"
    config["_verbose"] = bool(verbose)

    from otto.config import get_max_rounds, get_max_turns_per_call

    config["max_certify_rounds"] = get_max_rounds(config)
    config["max_turns_per_call"] = get_max_turns_per_call(config)
    return resolved_skip_qa, resolved_split_bool, skip_qa_source, sources


@contextmanager
def _signal_interrupt_guard() -> Any:
    """Treat SIGTERM/SIGHUP like Ctrl-C so pause/cleanup paths stay consistent."""
    installed: list[tuple[int, Any]] = []

    def _raise_keyboard_interrupt(_signum, _frame) -> None:
        raise KeyboardInterrupt

    for signum in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if signum is None:
            continue
        installed.append((signum, signal.getsignal(signum)))
        signal.signal(signum, _raise_keyboard_interrupt)
    try:
        yield
    finally:
        for signum, previous in reversed(installed):
            signal.signal(signum, previous)


def _positive_budget_option(
    _ctx: click.Context,
    _param: click.Parameter,
    value: int | None,
) -> int | None:
    if value is not None and value <= 0:
        raise click.BadParameter("must be > 0")
    return value


def _rounds_option(
    _ctx: click.Context,
    _param: click.Parameter,
    value: int | None,
) -> int | None:
    if value is None:
        return None
    if value <= 0:
        raise click.BadParameter("must be >= 1")
    if value > 50:
        raise click.BadParameter("must be <= 50")
    return value


def _max_turns_option(
    _ctx: click.Context,
    _param: click.Parameter,
    value: int | None,
) -> int | None:
    if value is None:
        return None
    if value < 1:
        raise click.BadParameter("must be >= 1")
    if value > 200:
        raise click.BadParameter("must be <= 200")
    return value


def _resolve_bool_setting(
    *,
    config: dict[str, Any],
    config_path: Path,
    key: str,
    cli_enabled: bool,
    cli_label: str,
) -> tuple[bool, str]:
    yaml_raw = _load_yaml_raw(config_path)
    if cli_enabled:
        return True, cli_label
    if key in yaml_raw:
        return bool(config.get(key)), "yaml"
    return bool(config.get(key)), "default"


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
    if provider == "codex":
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
    config: dict,
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

    pairs = list(zip(rows[::2], rows[1::2], strict=False))
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


def _open_command_hint(project_dir: Path) -> str:
    index_html = project_dir / "index.html"
    if index_html.exists():
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        return f"Open it:  {opener} index.html"

    package_json = project_dir / "package.json"
    if package_json.exists():
        try:
            pkg = json.loads(package_json.read_text())
        except (OSError, json.JSONDecodeError):
            pkg = {}
        scripts = pkg.get("scripts", {}) if isinstance(pkg, dict) else {}
        if isinstance(scripts, dict) and isinstance(scripts.get("start"), str) and scripts.get("start", "").strip():
            if (project_dir / "pnpm-lock.yaml").exists():
                cmd = "pnpm start"
            elif (project_dir / "yarn.lock").exists():
                cmd = "yarn start"
            else:
                cmd = "npm start"
            return f"Open it:  {cmd}"

    return f"Project:  {project_dir.resolve()}"


def _spent_line(result: Any, build_duration: float) -> str:
    breakdown = getattr(result, "breakdown", {}) or {}
    build_entry = breakdown.get("build", {})
    certify_entry = breakdown.get("certify", {})
    build_seconds = float(build_entry.get("duration_s", build_duration))
    verify_seconds = float(certify_entry.get("duration_s", 0.0))
    line = f"Spent: {_format_elapsed_compact(build_seconds)} building"
    if certify_entry:
        line += f", {_format_elapsed_compact(verify_seconds)} verifying"

    phase_costs: list[str] = []
    estimated = False
    for entry in (build_entry, certify_entry):
        cost = entry.get("cost_usd")
        if isinstance(cost, int | float):
            prefix = "~" if entry.get("estimated") is True else ""
            estimated = estimated or entry.get("estimated") is True
            phase_costs.append(f"{prefix}${float(cost):.2f}")

    if phase_costs:
        detail = " / ".join(phase_costs)
        if estimated:
            detail += " estimated"
        line += f"  ({detail}, total ${result.total_cost:.2f})"
    else:
        line += f"  (total ${result.total_cost:.2f})"
    return line


def _verification_heading(result: Any, strict_mode: bool) -> str:
    if result.passed:
        if strict_mode:
            rounds = getattr(result, "rounds", 1)
            return f"Verification passed (strict mode, {rounds} rounds)"
        return "Verification passed"
    return f"Verification failed after {result.rounds} round(s)"


def _print_build_result(
    project_dir: Path,
    intent: str,
    result,
    build_duration: float,
    *,
    strict_mode: bool = False,
) -> None:
    """Render build verification output and summary."""
    from otto import paths as _paths

    pow_html = _paths.certify_dir(project_dir, result.build_id) / "proof-of-work.html"
    runtime_json = _paths.session_dir(project_dir, result.build_id) / "runtime.json"
    crash_json = _paths.session_dir(project_dir, result.build_id) / "crash.json"

    if result.passed:
        console.print()
        console.print(f"  [bold]{_open_command_hint(project_dir)}[/bold]")
        console.print(f"  Built: {rich_escape(intent)}")

    if result.journeys:
        console.print()
        status_style = "success" if result.passed else "red"
        console.print(f"  [{status_style}]{_verification_heading(result, strict_mode)}[/{status_style}]")
        for j in result.journeys:
            status_icon = "[success]\u2713[/success]" if j.get("passed") else "[red]\u2717[/red]"
            console.print(f"    {status_icon} {rich_escape(j.get('name', ''))}")
        if pow_html.exists():
            console.print("  Full evidence (screenshots, video, tool traces): otto_logs/latest/certify/proof-of-work.html")

    console.print()
    console.print(f"  [bold]Build Summary[/bold]  \u00b7  Run ID: {result.build_id}")
    console.print(f"  Intent: {rich_escape(intent[:200])}")
    if result.journeys:
        console.print(f"  Stories: {result.tasks_passed} passed, {result.tasks_failed} failed")
    else:
        console.print(f"  Tasks: {result.tasks_passed} passed, {result.tasks_failed} failed")
    console.print(f"  {_spent_line(result, build_duration)}")
    if runtime_json.exists():
        try:
            runtime = json.loads(runtime_json.read_text())
            console.print(
                "  Runtime: "
                f"otto {runtime.get('otto_version', '?')}, "
                f"python {runtime.get('python_version', '?')}, "
                f"{str(runtime.get('platform', '?')).split()[0]} — see runtime.json"
            )
        except Exception:
            console.print("  Runtime: see otto_logs/latest/runtime.json")
    if crash_json.exists():
        console.print(f"  crash details: {crash_json}")
    if pow_html.exists():
        console.print("  View report:  otto_logs/latest/certify/proof-of-work.html")
    console.print("  Tail live log:  otto_logs/latest/build/narrative.log")
    console.print("  See past runs:  otto history")
    console.print()


def _exit_for_lock_busy(exc) -> None:
    holder = exc.holder or {}
    pid = holder.get("pid", "?")
    command = holder.get("command", "?")
    started_at = holder.get("started_at", "?")
    session_id = holder.get("session_id", "") or "unknown"
    error_console.print(
        "[error]Another otto command is already running in this project.[/error]\n"
        f"  Holder: pid={pid} command={command} started_at={started_at} session={session_id}\n"
        "  Re-run with `--break-lock` only if you are sure the lock is stuck."
    )
    sys.exit(1)


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
@click.option("--rounds", "-n", default=None, type=int, callback=_rounds_option, help="Max certification rounds, 1-50 (default from otto.yaml or 8)")
@click.option("--budget", default=None, type=int, callback=_positive_budget_option, help="Total wall-clock budget in seconds, must be > 0 (default from otto.yaml or 3600)")
@click.option("--max-turns", default=None, type=int, callback=_max_turns_option, help="Max agent turns per call, 1-200 (default from otto.yaml or 200)")
@click.option("--model", default=None, help="Override model for every agent (e.g. sonnet, haiku, gpt-5)")
@click.option("--provider", default=None, help="Override provider for every agent: claude | codex")
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
        # ``--yes`` is silently accepted: the i2p path has no interactive
        # spec-approval step, so the flag is a definitional no-op (kept
        # for CI/script compatibility with the legacy CLI).
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
                ("--spec", spec),
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
        if review_gate and auto_approve:
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
            review_gate=review_gate,
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


def _build_locked(*_args, **_kwargs) -> None:
    """Phase C.3 stub — the legacy v3 build pipeline is gone.

    The build CLI now routes everything through `_exit_legacy_build_removed()`
    before this function is reached. We keep the symbol so test patches
    (`tests/test_cli_run.py`, `tests/test_hardening.py`) keep importing
    cleanly; calling it directly hard-errors the same way the CLI does.
    """
    _exit_legacy_build_removed()


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("intent", required=False)
@click.option("--thorough", is_flag=True, help="Thorough mode — adversarial edge cases + code review")
@click.option("--fast", is_flag=True, help="Fast mode — happy path smoke test only")
@click.option("--standard", "standard_", is_flag=True, help="Standard mode — Must-Have + generic CRUD/edge/access checklist")
@click.option("--budget", default=None, type=int, callback=_positive_budget_option, help="Total wall-clock budget in seconds, must be > 0 (default from otto.yaml or 3600)")
@click.option("--max-turns", default=None, type=int, callback=_max_turns_option, help="Max agent turns per call, 1-200 (default from otto.yaml or 200)")
@click.option("--strict", is_flag=True, help="Require two consecutive PASS runs before reporting success")
@click.option("--model", default=None, help="Override model for every agent (e.g. sonnet, haiku, gpt-5)")
@click.option("--provider", default=None, help="Override provider for every agent: claude | codex")
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


if __name__ == "__main__":
    main()
