"""Otto CLI — entrypoint for all otto commands."""

import asyncio
from contextlib import contextmanager
import json
import os
import re
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

from otto.agent import AgentCallError
from otto.config import (
    ConfigError,
    _normalize_intent,
    agent_effort,
    agent_model,
    agent_provider,
    load_config,
    require_git,
    resolve_project_dir,
    resolve_certifier_mode,
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

    commit = _git(["rev-parse", "--short", "HEAD"]) or "unknown"
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]) or "unknown"
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
    should_block, msg = _check_venv_guard(
        cwd=_cwd,
        otto_src=str(Path(_otto_pkg.__file__).resolve().parent),
        queue_runner_env=os.environ.get("OTTO_INTERNAL_QUEUE_RUNNER"),
        cwd_repo_root=cwd_context.get("repo_root"),
        otto_repo_root=otto_context.get("repo_root"),
        cwd_git_dir=cwd_context.get("git_dir"),
        cwd_git_common_dir=cwd_context.get("git_common_dir"),
    )
    if should_block:
        click.echo(msg, err=True)
        sys.exit(1)
    # Scope the bypass to ONE level — strip from env now so nested subprocesses
    # do not inherit it. Safe to do unconditionally (no-op if not set).
    os.environ.pop("OTTO_INTERNAL_QUEUE_RUNNER", None)


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


def _record_cli_override(config: dict[str, Any], key: str, value: Any) -> None:
    overrides = config.setdefault("_cli_overrides", {})
    if isinstance(overrides, dict):
        overrides[key] = value


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
) -> None:
    """Print the resolved configuration with concise source labeling."""
    from rich.table import Table

    yaml_raw = _load_yaml_raw(config_path)
    model_value = config.get("model") or _runtime_model_name(str(config.get("provider") or ""))

    rows: list[tuple[str, Any, str]] = [
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
        ("model", "Agent models", agent_model),
        ("effort", "Agent efforts", agent_effort),
    ):
        global_value = resolver(config)
        if key == "model" and not global_value:
            global_value = _runtime_model_name(agent_provider(config))
        entries: list[str] = []
        for agent_type in ("build", "certifier", "spec", "fix"):
            value = resolver(config, agent_type)
            if key == "model" and not value:
                value = _runtime_model_name(agent_provider(config, agent_type))
            if value == global_value:
                continue
            source = _agent_setting_source(
                yaml_raw=yaml_raw,
                cli_sources=cli_sources,
                agent_type=agent_type,
                key=key,
            )
            entries.append(
                f"{agent_type}={_render_config_value(value, source, show_default_suffix=not all_default)}"
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


async def _run_spec_phase(
    *,
    project_dir: Path,
    intent: str,
    spec: bool,
    spec_file: Path | None,
    auto_approve: bool,
    resume_state,
    config: dict,
    split_mode: bool,
    run_id: str | None = None,
    budget=None,
) -> tuple[str, str, float, float]:
    """Drive the spec phase before the main build.

    Returns (run_id, spec_content, total_spec_cost, total_spec_duration). Writes checkpoint at
    each phase boundary (`spec` → `spec_review` → `spec_approved`).

    Raises SystemExit(2) with a user message on failure.
    """
    from otto.checkpoint import (
        load_checkpoint,
        spec_phase_completed,
        write_checkpoint,
    )
    from otto.spec import (
        SpecResult,
        count_open_questions,
        read_spec_file,
        review_spec,
        run_spec_agent,
        spec_hash,
        validate_spec,
    )
    from otto.observability import sha256_text, update_input_provenance
    from otto.pipeline import _runtime_metadata
    from otto.observability import write_runtime_metadata

    from otto import paths as _paths
    # Determine run_id (unified session_id): resume preserves, otherwise fresh.
    run_id = run_id or resume_state.run_id or _new_run_id(project_dir)
    _paths.ensure_session_scaffold(project_dir, run_id, phase="spec")
    run_dir = _paths.spec_dir(project_dir, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    _paths.set_pointer(project_dir, _paths.LATEST_POINTER, run_id)
    write_runtime_metadata(_paths.session_dir(project_dir, run_id), _runtime_metadata(project_dir))
    update_input_provenance(
        _paths.session_dir(project_dir, run_id),
        intent={
            "source": str(config.get("_intent_source") or "cli-argument"),
            "fallback_reason": str(config.get("_intent_fallback_reason") or ""),
            "resolved_text": intent,
            "sha256": sha256_text(intent),
        },
        spec={"source": "none", "path": "", "sha256": ""},
    )

    # Resume fast-path: already approved.
    if resume_state.resumed and spec_phase_completed(resume_state.phase):
        # Load the approved spec from disk, verify hash, skip spec phase.
        if not resume_state.spec_path:
            error_console.print("[error]Resume from spec_approved but no spec_path in checkpoint.[/error]")
            sys.exit(2)
        spec_md = Path(resume_state.spec_path)
        if not spec_md.exists():
            error_console.print(f"[error]Approved spec file missing: {spec_md}[/error]")
            sys.exit(2)
        content = spec_md.read_text()
        if resume_state.spec_hash and spec_hash(content) != resume_state.spec_hash:
            error_console.print(
                f"[error]Spec hash mismatch at {spec_md}. The file was modified after approval.\n"
                "  Run without --resume to start a fresh spec, or restore the original file.[/error]"
            )
            sys.exit(2)
        return run_id, content, resume_state.spec_cost, 0.0

    spec_cost = resume_state.spec_cost or 0.0
    spec_duration = 0.0
    current_phase = "spec"
    current_spec_path = resume_state.spec_path or ""
    current_spec_hash = resume_state.spec_hash or ""
    current_spec_version = resume_state.spec_version or 0

    # Resume mid-review: spec.md exists, re-open the gate.
    resume_mid_review = (
        resume_state.resumed
        and resume_state.phase == "spec_review"
        and resume_state.spec_path
        and Path(resume_state.spec_path).exists()
    )

    # Resume mid-spec-agent: if spec.md exists and validates, promote to review.
    resume_mid_spec_with_file = (
        resume_state.resumed
        and resume_state.phase == "spec"
        and resume_state.spec_path
        and Path(resume_state.spec_path).exists()
    )

    try:
        if spec_file:
            config["_spec_source"] = "--spec-file"
            config["_spec_path"] = str(spec_file)
            # External spec: load, validate, skip agent.
            spec_path_out = run_dir / "spec.md"
            run_dir.mkdir(parents=True, exist_ok=True)
            intent_from_file, content = read_spec_file(spec_file)
            # Only overwrite the run-dir copy on fresh runs; on resume keep existing.
            if not spec_path_out.exists():
                spec_path_out.write_text(content)
            spec_result = SpecResult(
                path=spec_path_out,
                content=content,
                open_questions=count_open_questions(content),
                cost=spec_cost,
                duration_s=0.0,
                version=resume_state.spec_version,
            )
            # --spec-file implies auto-approve
            auto_approve = True
            # Write spec_review checkpoint before approval (so crash = resumable)
            current_phase = "spec_review"
            current_spec_path = str(spec_path_out)
            current_spec_hash = spec_hash(content)
            current_spec_version = resume_state.spec_version
            write_checkpoint(
                project_dir,
                run_id=run_id,
                command="build",
                phase="spec_review",
                split_mode=split_mode,
                intent=intent,
                spec_path=str(spec_path_out),
                spec_hash=current_spec_hash,
                spec_version=current_spec_version,
                spec_cost=spec_cost,
            )
            update_input_provenance(
                _paths.session_dir(project_dir, run_id),
                spec={"source": "--spec-file", "path": str(spec_path_out), "sha256": current_spec_hash},
            )
        elif resume_mid_review or resume_mid_spec_with_file:
            # Re-open the review gate using existing on-disk spec.
            existing_path = Path(resume_state.spec_path)
            content = existing_path.read_text()
            errors = validate_spec(content)
            if errors:
                console.print(f"  [yellow]Existing spec has issues: {'; '.join(errors)}[/yellow]")
                console.print("  [yellow]Re-running spec agent.[/yellow]\n")
                # Fall through to agent path
                current_phase = "spec"
                write_checkpoint(
                    project_dir, run_id=run_id, command="build", phase="spec",
                    split_mode=split_mode,
                    intent=intent, spec_cost=spec_cost, spec_version=resume_state.spec_version,
                )
                spec_result = await run_spec_agent(
                    intent, project_dir, run_dir, config,
                    version=resume_state.spec_version, budget=budget,
                )
                config["_spec_source"] = "spec-agent"
                config["_spec_path"] = str(spec_result.path)
                spec_cost += spec_result.cost
                spec_duration += spec_result.duration_s
                current_phase = "spec_review"
                current_spec_path = str(spec_result.path)
                current_spec_hash = spec_hash(spec_result.content)
                current_spec_version = spec_result.version
                write_checkpoint(
                    project_dir, run_id=run_id, command="build", phase="spec_review",
                    split_mode=split_mode,
                    intent=intent, spec_path=str(spec_result.path),
                    spec_hash=current_spec_hash, spec_version=current_spec_version, spec_cost=spec_cost,
                )
                update_input_provenance(
                    _paths.session_dir(project_dir, run_id),
                    spec={"source": "spec-agent", "path": str(spec_result.path), "sha256": current_spec_hash},
                )
            else:
                spec_result = SpecResult(
                    path=existing_path,
                    content=content,
                    open_questions=count_open_questions(content),
                    cost=spec_cost,
                    duration_s=0.0,
                    version=resume_state.spec_version,
                )
                current_phase = "spec_review"
                current_spec_path = str(existing_path)
                current_spec_hash = spec_hash(content)
                current_spec_version = resume_state.spec_version
                console.print(f"  [info]Resuming at review gate (existing spec at {existing_path})[/info]\n")
        else:
            # Fresh spec generation.
            current_phase = "spec"
            write_checkpoint(
                project_dir, run_id=run_id, command="build", phase="spec",
                split_mode=split_mode,
                intent=intent, spec_cost=spec_cost, spec_version=resume_state.spec_version,
            )
            console.print("  [bold]Spec phase[/bold] — generating product spec...\n")
            spec_result = await run_spec_agent(
                intent, project_dir, run_dir, config, budget=budget,
            )
            config["_spec_source"] = "spec-agent"
            config["_spec_path"] = str(spec_result.path)
            spec_cost += spec_result.cost
            spec_duration += spec_result.duration_s
            current_phase = "spec_review"
            current_spec_path = str(spec_result.path)
            current_spec_hash = spec_hash(spec_result.content)
            current_spec_version = spec_result.version
            write_checkpoint(
                project_dir, run_id=run_id, command="build", phase="spec_review",
                split_mode=split_mode,
                intent=intent, spec_path=str(spec_result.path),
                spec_hash=current_spec_hash, spec_version=current_spec_version, spec_cost=spec_cost,
            )
            update_input_provenance(
                _paths.session_dir(project_dir, run_id),
                spec={"source": "spec-agent", "path": str(spec_result.path), "sha256": current_spec_hash},
            )

        # Review gate
        approved = await review_spec(
            spec_result, project_dir, run_dir, run_id, intent, config,
            auto_approve=auto_approve,
            initial_regen_count=resume_state.spec_version,
            budget=budget,
        )
        spec_cost = approved.cost
        spec_duration = approved.duration_s
        current_phase = "spec_approved"
        current_spec_path = str(approved.path)
        current_spec_hash = spec_hash(approved.content)
        current_spec_version = approved.version

        # Record approved state
        write_checkpoint(
            project_dir, run_id=run_id, command="build", phase="spec_approved",
            split_mode=split_mode,
            intent=intent, spec_path=str(approved.path),
            spec_hash=current_spec_hash, spec_version=current_spec_version, spec_cost=spec_cost,
        )
        update_input_provenance(
            _paths.session_dir(project_dir, run_id),
            spec={"source": str(config.get("_spec_source") or "spec-agent"), "path": str(approved.path), "sha256": current_spec_hash},
        )
        return run_id, approved.content, spec_cost, spec_duration

    except ValueError as exc:
        error_console.print(f"[error]{exc}[/error]")
        sys.exit(2)
    except KeyboardInterrupt:
        prior_cp = load_checkpoint(project_dir, run_id=run_id) or {}
        write_checkpoint(
            project_dir,
            run_id=run_id,
            command="build",
            phase=(prior_cp.get("phase", "") or current_phase),
            split_mode=bool(prior_cp.get("split_mode", split_mode)),
            intent=intent,
            status="paused",
            spec_path=(prior_cp.get("spec_path", "") or current_spec_path),
            spec_hash=(prior_cp.get("spec_hash", "") or current_spec_hash),
            spec_version=int(prior_cp.get("spec_version", current_spec_version) or 0),
            spec_cost=float(prior_cp.get("spec_cost", spec_cost) or 0.0),
        )
        raise
    except Exception as exc:
        if isinstance(exc, AgentCallError):
            prior_cp = load_checkpoint(project_dir, run_id=run_id) or {}
            session_id = (
                exc.session_id
                or prior_cp.get("agent_session_id")
                or prior_cp.get("session_id", "")
            )
            write_checkpoint(
                project_dir, run_id=run_id, command="build",
                status="paused", phase=current_phase,
                split_mode=bool(prior_cp.get("split_mode", split_mode)),
                intent=intent,
                spec_path=current_spec_path or None,
                spec_hash=current_spec_hash or None,
                spec_version=current_spec_version,
                spec_cost=spec_cost,
                session_id=session_id,
            )
            error_console.print(
                f"[error]Run budget exhausted during spec ({exc.reason}).[/error]\n"
                "  Use `otto build --resume` to continue, or raise "
                "`run_budget_seconds` in otto.yaml."
            )
            sys.exit(1)
        # Validation failures / missing-spec-file errors leave no resumable
        # state — clear the in_progress checkpoint so retry doesn't demand
        # --force for a checkpoint that couldn't be resumed anyway.
        from otto.checkpoint import clear_checkpoint
        try:
            clear_checkpoint(project_dir, run_id=run_id)
        except Exception:
            pass
        error_console.print(
            f"[error]Spec phase failed: {exc}[/error]\n"
            "  The invalid spec has been discarded — re-run `otto build --spec` to retry."
        )
        sys.exit(1)


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


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("intent", required=False)
@click.option("--no-qa", is_flag=True, help="Skip product certification after build")
@click.option("--fast", is_flag=True, help="Fast certification — happy path smoke test only (default)")
@click.option("--standard", "standard_", is_flag=True, help="Standard certification — Must-Have + generic CRUD/edge/access checklist")
@click.option("--thorough", is_flag=True, help="Thorough certification — adversarial edge cases + code review")
@click.option("--split", is_flag=True, help="Split mode: system-controlled certify loop with build journal")
@click.option("--rounds", "-n", default=None, type=int, callback=_rounds_option, help="Max certification rounds, 1-50 (default from otto.yaml or 8)")
@click.option("--budget", default=None, type=int, callback=_positive_budget_option, help="Total wall-clock budget in seconds, must be > 0 (default from otto.yaml or 3600)")
@click.option("--max-turns", default=None, type=int, callback=_max_turns_option, help="Max agent turns per call, 1-200 (default from otto.yaml or 200)")
@click.option("--model", default=None, help="Override model for every agent (e.g. sonnet, haiku, gpt-5)")
@click.option("--provider", default=None, help="Override provider for every agent: claude | codex")
@click.option("--effort", default=None, help="Override effort level for every agent: low | medium | high | max")
@click.option("--strict", is_flag=True, help="Require two consecutive PASS rounds before stopping")
@click.option("--verbose", is_flag=True, help="Show detailed live progress, including tool-call counts")
@click.option("--debug-unredacted", is_flag=True, help="Also write unredacted raw logs under sessions/<id>/raw/ (do not share)")
@click.option("--resume", is_flag=True, help="Resume from last checkpoint (requires an in-progress run)")
@click.option(
    "--force-cross-command-resume",
    is_flag=True,
    help="Allow --resume to reuse a checkpoint written by a different otto command",
)
@click.option("--spec", is_flag=True, help="Generate a reviewable spec before building")
@click.option("--spec-file", type=click.Path(exists=False, dir_okay=False, path_type=Path),
              default=None, help="Use a pre-written spec file (implies --yes)")
@click.option("--yes", is_flag=True, help="Auto-approve the generated spec (for CI/scripts)")
@click.option("--force", is_flag=True, help="Discard an active paused spec run and start fresh")
@click.option("--in-worktree", "in_worktree", is_flag=True,
              help="Run in an isolated git worktree (./.worktrees/build-<slug>-<date>/) "
                   "instead of modifying the current working tree")
@click.option("--allow-dirty", is_flag=True, help="Proceed even if the repo has local modifications or untracked files")
@click.option("--break-lock", is_flag=True, help="Force-clear the project lock before starting")
def build(intent, no_qa, fast, standard_, thorough, split, rounds, budget, max_turns, model, provider, effort, strict, verbose, debug_unredacted, resume, force_cross_command_resume, spec, spec_file, yes, force, in_worktree, allow_dirty, break_lock):
    """Build a product from a natural language intent.

    One agent builds, certifies, and fixes autonomously. The certifier
    verifies the product works by running real user stories (HTTP, CLI,
    import, WebSocket — any product type).

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
    from otto import paths as _paths

    try:
        with _signal_interrupt_guard():
            with _paths.project_lock(project_dir, "build", break_lock=break_lock):
                _build_locked(
                    intent, no_qa, fast, standard_, thorough, split, rounds,
                    budget, max_turns, model, provider, effort, strict, verbose, debug_unredacted,
                    resume, force_cross_command_resume, spec, spec_file, yes, force, in_worktree, allow_dirty, project_dir,
                )
    except _paths.LockBreakError as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(1)
    except _paths.LockBusy as exc:
        _exit_for_lock_busy(exc)


def _build_locked(
    intent,
    no_qa,
    fast,
    standard_,
    thorough,
    split,
    rounds,
    budget,
    max_turns,
    model,
    provider,
    effort,
    strict,
    verbose,
    debug_unredacted,
    resume,
    force_cross_command_resume,
    spec,
    spec_file,
    yes,
    force,
    in_worktree,
    allow_dirty,
    project_dir: Path,
):
    from otto import paths as _paths

    from otto.checkpoint import (
        enforce_resume_available,
        enforce_resume_command_match,
        initial_build_completed,
        load_checkpoint,
        is_spec_phase,
        print_resume_status,
        resolve_resume,
    )
    from otto.config import PROJECT_INTENT_MIN_CHARS, read_project_intent_md
    from otto.spec import read_spec_file
    from otto.config import ensure_safe_repo_state

    bootstrap_intent = "Bootstrap and maintain the product described in intent.md"

    if spec and spec_file:
        error_console.print("[error]--spec and --spec-file are mutually exclusive.[/error]")
        sys.exit(2)

    cp = load_checkpoint(project_dir)

    # Protect paused spec checkpoints before resolve_resume() can clear them.
    if cp and is_spec_phase(cp.get("phase", "") or "") and not resume:
        if not force:
            error_console.print(
                "[error]Paused spec run detected at "
                f"phase={cp.get('phase')!r}, run_id={cp.get('run_id', '')!r}.[/error]\n"
                "  Use --resume to continue, or --force to discard."
            )
            sys.exit(2)
        from otto.checkpoint import clear_checkpoint
        run_id_to_archive = cp.get("run_id", "") or ""
        if run_id_to_archive:
            # New layout: mark the session as abandoned via summary.json
            # (preserves dir for forensics, no rename). Legacy fallback does
            # the old rename for pre-restructure layouts.
            session_dir = _paths.session_dir(project_dir, run_id_to_archive)
            if session_dir.exists():
                try:
                    from otto.observability import write_json_atomic

                    summary_path = session_dir / "summary.json"
                    write_json_atomic(summary_path, {
                        "status": "abandoned",
                        "abandoned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "run_id": run_id_to_archive,
                    })
                    console.print(f"  [yellow]Marked prior session as abandoned: {session_dir}[/yellow]")
                except OSError as exc:
                    console.print(f"  [yellow]Could not mark prior session abandoned: {exc}[/yellow]")
            # Legacy path (pre-restructure) archive-by-rename.
            legacy_run_dir = project_dir / "otto_logs" / "runs" / run_id_to_archive
            legacy_abandoned = project_dir / "otto_logs" / "runs" / f"{run_id_to_archive}.abandoned"
            if legacy_run_dir.exists():
                try:
                    legacy_run_dir.rename(legacy_abandoned)
                    console.print(f"  [yellow]Archived legacy spec run to {legacy_abandoned}[/yellow]")
                except OSError as exc:
                    console.print(f"  [yellow]Could not archive legacy spec run: {exc}[/yellow]")
        clear_checkpoint(project_dir, run_id=run_id_to_archive or None)
        cp = None

    resume_state = resolve_resume(
        project_dir,
        resume,
        expected_command="build",
        force=force,
        reject_incompatible=False,
    )
    if resume_state.fingerprint_mismatch and not force:
        error_console.print(
            "[error]Checkpoint fingerprint does not match the current code/prompt state.[/error]\n"
            "  Pass `--force --resume` to override."
        )
        sys.exit(2)
    enforce_resume_available(
        resume_state,
        resume_flag=resume,
        expected_command="build",
    )
    enforce_resume_command_match(
        resume_state,
        "build",
        force_cross_command_resume=force_cross_command_resume,
    )
    if resume_state.resumed and resume_state.split_mode is not None:
        split = resume_state.split_mode
    if resume and not resume_state.resumed and resume_state.missing_paused_session_path and not intent:
        error_console.print(
            "[error]Your paused session at "
            f"{resume_state.missing_paused_session_path} was deleted; nothing to resume.[/error]"
        )
        sys.exit(2)
    use_spec = (
        bool(spec or spec_file)
        or is_spec_phase(resume_state.phase)
        or bool(resume_state.spec_path)
    )

    intent = _normalize_intent(intent or "")

    if spec_file:
        try:
            file_intent, _ = read_spec_file(Path(spec_file))
            file_intent = _normalize_intent(file_intent)
        except ValueError as exc:
            error_console.print(f"[error]{exc}[/error]")
            sys.exit(2)
        if intent and intent != file_intent:
            error_console.print(
                f"[error]Intent mismatch: CLI intent does not match {spec_file}.[/error]"
            )
            sys.exit(2)
        if resume and resume_state.resumed and resume_state.spec_path:
            expected_spec_path = Path(resume_state.spec_path).resolve(strict=False)
            given_spec_path = Path(spec_file).resolve(strict=False)
            if given_spec_path != expected_spec_path:
                error_console.print(
                    "[error]Cannot change --spec-file on resume. "
                    f"Checkpoint spec is {expected_spec_path}, got {given_spec_path}.[/error]"
                )
                sys.exit(2)
        intent = file_intent

    config_path = project_dir / "otto.yaml"
    config = _load_config_or_exit(config_path)

    checkpoint_intent = _normalize_intent(resume_state.intent or "")
    if resume and resume_state.resumed and intent and checkpoint_intent and intent != checkpoint_intent:
        error_console.print(
            "[error]Intent mismatch on resume.[/error]\n"
            f"  checkpoint intent: '{rich_escape(checkpoint_intent)}'\n"
            f"  CLI intent:        '{rich_escape(intent)}'\n"
            "  Resume preserves the checkpoint intent. Omit INTENT on resume, or run "
            "`otto build <new intent>` to start a fresh run."
        )
        sys.exit(2)

    resume_without_intent = bool(resume and resume_state.resumed and not intent)
    display_intent = intent or "(resumed run)"
    intent_source = "cli-argument"
    intent_fallback_reason = ""

    try:
        ensure_safe_repo_state(
            project_dir,
            allow_dirty=bool(allow_dirty or config.get("allow_dirty_repo")),
        )
    except ConfigError as exc:
        if resume_state.run_id:
            try:
                from otto.checkpoint import load_checkpoint, write_checkpoint
                from otto.observability import dirty_worktree_files

                prior_cp = load_checkpoint(project_dir, run_id=resume_state.run_id) or {}
                write_checkpoint(
                    project_dir,
                    run_id=resume_state.run_id,
                    command=prior_cp.get("command", "build") or "build",
                    certifier_mode=prior_cp.get("certifier_mode", "thorough") or "thorough",
                    prompt_mode=prior_cp.get("prompt_mode", "build") or "build",
                    focus=prior_cp.get("focus"),
                    target=prior_cp.get("target"),
                    max_rounds=int(prior_cp.get("max_rounds", 8) or 8),
                    status=prior_cp.get("status", "paused") or "paused",
                    phase=prior_cp.get("phase", "") or "",
                    split_mode=prior_cp.get("split_mode"),
                    session_id=prior_cp.get("agent_session_id", "") or "",
                    current_round=int(prior_cp.get("current_round", 0) or 0),
                    total_cost=float(prior_cp.get("total_cost", 0.0) or 0.0),
                    total_duration=float(prior_cp.get("total_duration", 0.0) or 0.0),
                    rounds=list(prior_cp.get("rounds", []) or []),
                    child_session_ids=list(prior_cp.get("child_session_ids", []) or []),
                    intent=prior_cp.get("intent"),
                    spec_path=prior_cp.get("spec_path"),
                    spec_hash=prior_cp.get("spec_hash"),
                    spec_version=int(prior_cp.get("spec_version", 0) or 0),
                    spec_cost=float(prior_cp.get("spec_cost", 0.0) or 0.0),
                    dirty_files=dirty_worktree_files(project_dir),
                )
            except Exception:
                pass
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(2)

    # Inherit intent from checkpoint for spec-phase resume, or fall back to
    # split-mode intent.md resolution for backwards compat.
    if not intent:
        if resume_without_intent:
            if resume_state.intent:
                intent = _normalize_intent(resume_state.intent)
                display_intent = intent
                intent_source = "checkpoint"
            elif split and not no_qa:
                from otto.config import resolve_intent_provenance
                try:
                    intent_meta = resolve_intent_provenance(project_dir)
                    intent = _normalize_intent(intent_meta.get("resolved_text", "") or "")
                except (ConfigError, ValueError) as exc:
                    error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
                    sys.exit(2)
                if not intent:
                    error_console.print(
                        "[error]Resume needs a product description for split mode. "
                        "Provide INTENT or create intent.md/README.md.[/error]"
                    )
                    sys.exit(2)
                if intent:
                    intent_source = intent_meta.get("source") or "intent.md"
                    intent_fallback_reason = intent_meta.get("fallback_reason") or ""
        else:
            try:
                project_intent = read_project_intent_md(
                    project_dir,
                    min_chars=PROJECT_INTENT_MIN_CHARS,
                )
            except (ConfigError, ValueError) as exc:
                error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
                sys.exit(2)
            if project_intent:
                intent = bootstrap_intent
                display_intent = intent
                intent_source = "intent.md"
                console.print(
                    f"  [dim]Using project intent from intent.md ({len(project_intent)} chars)[/dim]"
                )
            else:
                error_console.print(
                    "[error]Intent cannot be empty. Either pass a description as the first "
                    "argument, or write a description of the product to ./intent.md[/error]"
                )
                sys.exit(2)

    display_intent = intent or display_intent
    from otto.config import MAX_INTENT_CHARS, validate_text_limit
    try:
        intent = validate_text_limit(
            intent,
            kind="intent",
            source="CLI argument" if not resume_without_intent else "resolved intent",
            max_chars=MAX_INTENT_CHARS,
        )
    except ConfigError as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(2)
    print_resume_status(console, resume_state, resume, expected_command="build")
    run_id: str = resume_state.run_id or ""
    if not run_id:
        run_id = _new_run_id(project_dir)

    # No auto-create: `otto.yaml` only exists if the user ran `otto setup`.
    # `load_config` returns built-in defaults + auto-detected project values
    # when the yaml is absent.
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
    resolved_split, _split_source = _resolve_bool_setting(
        config=config,
        config_path=config_path,
        key="split_mode",
        cli_enabled=split,
        cli_label="--split",
    )
    split = bool(resolved_split)
    if resume_state.resumed and resume_state.split_mode is not None:
        split = resume_state.split_mode

    if use_spec and resolved_skip_qa:
        source_text = "--no-qa" if skip_qa_source == "--no-qa" else "otto.yaml: skip_product_qa: true"
        error_console.print(
            "[error]--spec requires the certifier (Must-NOT-Have scope check); "
            f"skip_product_qa is enabled via {rich_escape(source_text)}.[/error]"
        )
        sys.exit(2)
    if sum(bool(x) for x in (fast, standard_, thorough)) > 1:
        error_console.print(
            "[error]--fast, --standard, and --thorough are mutually exclusive.[/error]"
        )
        sys.exit(2)

    # Resolve CLI overrides into config. Track sources so the banner can
    # show where each active value came from.
    sources: dict[str, str] = {}
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
    if model:
        config["model"] = model
        sources["model"] = "--model"
        _record_cli_override(config, "model", model)
    if provider:
        config["provider"] = provider
        sources["provider"] = "--provider"
        _record_cli_override(config, "provider", provider)
    if effort:
        config["effort"] = effort
        sources["effort"] = "--effort"
        _record_cli_override(config, "effort", effort)
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
    try:
        config["max_certify_rounds"] = get_max_rounds(config)
        config["max_turns_per_call"] = get_max_turns_per_call(config)
    except ConfigError as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(2)

    _print_config_banner(console, config, sources, config_path)
    _print_startup_context(console, project_dir, run_id)
    if debug_unredacted:
        console.print("  [bold red]UNREDACTED LOGS — do not share[/bold red]")

    # Phase 1.2: --in-worktree creates an isolated worktree (in-process chdir,
    # no subprocess re-entry) and runs the rest of the pipeline from there.
    # The branch policy below then runs from the new cwd.
    if in_worktree:
        if resume and resume_state.resumed:
            error_console.print(
                "[error]--in-worktree is not compatible with --resume.[/error]\n"
                "  Resume continues an existing run in its original cwd. To resume "
                "a worktree run, cd into the worktree directly and run `otto build --resume`."
            )
            sys.exit(2)
        from otto.worktree import (
            WorktreeAlreadyCheckedOut,
            setup_worktree_for_atomic_cli,
        )
        try:
            wt_path, config = setup_worktree_for_atomic_cli(
                project_dir=project_dir,
                mode="build",
                intent=intent,
                config=config,
            )
        except WorktreeAlreadyCheckedOut as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(1)
        except (RuntimeError, ValueError) as exc:
            error_console.print(f"[error]Worktree setup failed: {rich_escape(str(exc))}[/error]")
            sys.exit(1)
        console.print(f"  [dim]Worktree:[/dim] [info]{wt_path}[/info]")
        # Re-anchor project_dir to the worktree — the rest of the pipeline
        # operates on this isolated checkout.
        project_dir = wt_path
        if no_qa:
            config["skip_product_qa"] = True
        if fast:
            config["_certifier_mode"] = "fast"
        elif thorough:
            config["_certifier_mode"] = "thorough"
        if rounds is not None:
            config["max_certify_rounds"] = rounds

    # Branch policy (Phase 1.1): if user is on the default branch, create a
    # build/<slug>-<date> branch and switch. If on any other branch, stay put
    # (mirrors `otto improve`'s long-standing pattern). Resume reuses the
    # current branch — never re-branches mid-flight. With --in-worktree, the
    # branch was already created by enter_worktree_for_atomic_command.
    if not (resume and resume_state.resumed) and not in_worktree:
        from otto.branching import ensure_branch_for_atomic_command
        try:
            branch_name, created_new = ensure_branch_for_atomic_command(
                mode="build",
                intent=intent,
                project_dir=project_dir,
                default_branch=config.get("default_branch", "main"),
            )
            if created_new:
                console.print(f"  [dim]Branch:[/dim] [info]{branch_name}[/info] (created)")
            elif branch_name:
                console.print(f"  [dim]Branch:[/dim] [info]{branch_name}[/info] (current)")
            else:
                console.print("  [dim]Branch:[/dim] (greenfield — no commits yet)")
        except RuntimeError as exc:
            error_console.print(f"[error]Branch setup failed: {rich_escape(str(exc))}[/error]")
            sys.exit(1)

    from otto.pipeline import build_agentic_v3, run_certify_fix_loop, BuildResult
    from otto.budget import RunBudget
    run_budget = RunBudget.start_from(
        config,
        session_started_at=resume_state.session_started_at or None,
    )

    # --- Spec phase (if --spec or --spec-file) ---
    # Start timer BEFORE spec so reported duration matches budget accounting.
    build_start = time.time()
    spec_content: str | None = None
    spec_cost_total: float = resume_state.spec_cost or 0.0
    spec_duration_total: float = 0.0
    if use_spec:
        try:
            run_id, spec_content, spec_cost_total, spec_duration_total = asyncio.run(_run_spec_phase(
                project_dir=project_dir,
                intent=intent,
                spec=spec,
                spec_file=spec_file,
                auto_approve=yes,
                resume_state=resume_state,
                config=config,
                split_mode=split,
                run_id=run_id or None,
                budget=run_budget,
            ))
        except KeyboardInterrupt:
            console.print("\n  [yellow]Paused. Run `otto build --resume` to continue.[/yellow]")
            sys.exit(0)

    console.print()
    build_dir = _paths.build_dir(project_dir, run_id)
    certify_dir = _paths.certify_dir(project_dir, run_id)
    narrative_log = build_dir / "narrative.log"

    # Priority: CLI flag (stored in _certifier_mode) > otto.yaml (certifier_mode)
    # > "fast" fallback (cheap default for quick iteration; users who want real
    # QA set `certifier_mode: standard` or `thorough` in otto.yaml).
    certifier_mode = resolve_certifier_mode(
        config,
        cli_mode=config.pop("_certifier_mode", None),
    )

    try:
        if split and not resolved_skip_qa:
            console.print("  [bold]Split mode[/bold] \u2014 system-controlled certify loop\n")
            # Resume skips the initial build only after it has completed.
            skip_initial_build = resume_state.resumed and initial_build_completed(
                resume_state.phase
            )
            result: BuildResult = asyncio.run(
                run_certify_fix_loop(intent, project_dir, config,
                                     certifier_mode=certifier_mode,
                                     skip_initial_build=skip_initial_build,
                                     start_round=resume_state.start_round,
                                     resume_cost=resume_state.total_cost,
                                     resume_duration=resume_state.total_duration,
                                     resume_rounds=resume_state.rounds,
                                     resume_session_id=resume_state.agent_session_id or None,
                                     session_id=run_id or None,
                                     command="build",
                                     record_intent=not resume_without_intent,
                                     spec=spec_content,
                                     spec_cost=spec_cost_total,
                                     spec_duration=spec_duration_total,
                                     budget=run_budget,
                                     strict_mode=bool(config.get("strict_mode")),
                                     verbose=bool(verbose))
            )
        else:
            console.print("  Verifying core requirements after each build.\n")
            result: BuildResult = asyncio.run(
                build_agentic_v3(intent, project_dir, config,
                                 certifier_mode=certifier_mode,
                                 resume_session_id=resume_state.agent_session_id or None,
                                 record_intent=not resume_without_intent,
                                 resume_existing_session=resume_without_intent,
                                 spec=spec_content,
                                 run_id=run_id or None,
                                 prior_total_cost=resume_state.total_cost,
                                 prior_total_duration=resume_state.total_duration,
                                 budget=run_budget,
                                 spec_cost=spec_cost_total,
                                 spec_duration=spec_duration_total,
                                 strict_mode=bool(config.get("strict_mode")),
                                 verbose=bool(verbose))
            )
    except KeyboardInterrupt:
        console.print("\n  [yellow]Paused. Run `otto build --resume` to continue.[/yellow]")
        sys.exit(0)
    except Exception as e:
        crash_details_line = f"  crash details: {e.crash_path}\n" if isinstance(e, AgentCallError) and getattr(e, "crash_path", "") else ""
        if isinstance(e, AgentCallError) and e.reason.startswith("Timed out after"):
            error_console.print(
                (
                    f"[error]Run timed out after {rich_escape(e.reason.removeprefix('Timed out after ').strip())} "
                    "(run_budget_seconds).[/error]\n"
                    f"  Narrative log: {build_dir / 'narrative.log'}\n"
                    f"{crash_details_line}"
                    "  Resume:        otto build --resume"
                )
            )
            sys.exit(1)
        if isinstance(e, AgentCallError) and e.reason.startswith("Agent crashed"):
            crash_reason = e.reason.removeprefix("Agent crashed:").strip()
            error_console.print(
                (
                    f"[error]Agent crashed: {rich_escape(crash_reason)}[/error]\n"
                    f"  Narrative log: {build_dir / 'narrative.log'}  "
                    "(last events may be incomplete)\n"
                    f"{crash_details_line}"
                    "  Resume:        otto build --resume"
                )
            )
            sys.exit(1)
        error_console.print(
            f"[error]Build failed: {rich_escape(str(e))}[/error]\n"
            f"  Narrative log: {build_dir / 'narrative.log'}  (for full context)"
        )
        sys.exit(1)

    build_duration = time.time() - build_start
    _print_build_result(
        project_dir,
        display_intent,
        result,
        build_duration,
        strict_mode=bool(config.get("strict_mode")),
    )

    # Per-run manifest for queue/merge subsystems. The canonical record lives
    # at the session root; queue-backed runs also mirror it by task slug.
    session_dir = _paths.session_dir(project_dir, result.build_id)
    build_dir = _paths.build_dir(project_dir, result.build_id)
    certify_dir = _paths.certify_dir(project_dir, result.build_id)
    try:
        from otto.manifest import (
            QUEUE_TASK_ENV,
            current_head_sha,
            make_manifest,
            write_manifest,
        )
        from otto.branching import current_branch
        manifest = make_manifest(
            command="build",
            argv=list(sys.argv[1:]),
            queue_task_id=os.environ.get(QUEUE_TASK_ENV),
            run_id=result.build_id,
            branch=(current_branch(project_dir) or None),
            checkpoint_path=session_dir / "checkpoint.json",
            proof_of_work_path=certify_dir / "proof-of-work.json",
            cost_usd=result.total_cost,
            duration_s=build_duration,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(build_start)),
            head_sha=current_head_sha(project_dir),
            resolved_intent=intent,
            exit_status="success" if result.passed else "failure",
        )
        write_manifest(manifest, project_dir=project_dir, fallback_dir=session_dir)
    except Exception as exc:
        # Manifest writing is observability — never let it crash the user
        from otto.theme import error_console as _err
        _err.print(f"[yellow]warning: manifest write failed: {exc}[/yellow]")

    if not result.passed:
        narrative_log = build_dir / "narrative.log"
        total_stories = result.tasks_passed + result.tasks_failed
        pow_html = certify_dir / "proof-of-work.html"
        try:
            narrative_text = narrative_log.read_text() if narrative_log.exists() else ""
        except OSError:
            narrative_text = ""
        timeout_match = re.search(r"Timed out after (\d+s)", narrative_text)
        crash_match = re.search(r"Agent crashed: (.+)", narrative_text)
        if timeout_match:
            error_console.print(
                f"[error]Run timed out after {timeout_match.group(1)} "
                "(run_budget_seconds).[/error]\n"
                f"  Narrative log: {narrative_log}\n"
                "  Resume:        otto build --resume"
            )
            sys.exit(1)
        if crash_match:
            error_console.print(
                f"[error]Agent crashed: {rich_escape(crash_match.group(1).strip())}[/error]\n"
                f"  Narrative log: {narrative_log}  (last events may be incomplete)\n"
                "  Resume:        otto build --resume"
            )
            sys.exit(1)
        if pow_html.exists():
            error_console.print(
                f"[error]Build did not pass certification ({result.tasks_passed}/{total_stories} "
                "stories passed).[/error]\n"
                f"  Report: {pow_html}\n"
                f"  Narrative: {narrative_log}"
            )
        else:
            error_console.print(
                "[error]Build failed.[/error]\n"
                f"  Narrative log: {narrative_log}  (for full context)"
            )
        sys.exit(1)

    sys.exit(0)


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
@click.option("--break-lock", is_flag=True, help="Force-clear the project lock before starting")
def certify(intent, thorough, fast, standard_, budget, max_turns, strict, model, provider, effort, break_lock):
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
    from otto import paths as _paths

    if sum(bool(x) for x in (fast, standard_, thorough)) > 1:
        error_console.print(
            "[error]--fast, --standard, and --thorough are mutually exclusive.[/error]"
        )
        sys.exit(2)
    intent = _normalize_intent(intent or "")

    try:
        with _signal_interrupt_guard():
            with _paths.project_lock(project_dir, "certify", break_lock=break_lock):
                session_id = _new_run_id(project_dir)
                _certify_locked(
                    intent, thorough, fast, standard_,
                    budget, max_turns, strict, model, provider, effort,
                    project_dir, session_id,
                )
    except _paths.LockBreakError as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(1)
    except _paths.LockBusy as exc:
        _exit_for_lock_busy(exc)


def _certify_locked(
    intent, thorough, fast, standard_,
    budget, max_turns, strict, model, provider, effort,
    project_dir: Path, session_id: str,
):

    # Load config so run_budget_seconds and other settings are respected
    config_path = project_dir / "otto.yaml"
    config = _load_config_or_exit(config_path)

    sources: dict[str, str] = {}
    if budget is not None:
        config["run_budget_seconds"] = budget
        sources["run_budget_seconds"] = "cli"
    if max_turns is not None:
        config["max_turns_per_call"] = max_turns
        sources["max_turns_per_call"] = "cli"
    if strict:
        config["strict_mode"] = True
        sources["strict_mode"] = "cli"
    if model:
        config["model"] = model
        sources["model"] = "cli"
        _record_cli_override(config, "model", model)
    if provider:
        config["provider"] = provider
        sources["provider"] = "cli"
        _record_cli_override(config, "provider", provider)
    if effort:
        config["effort"] = effort
        sources["effort"] = "cli"
        _record_cli_override(config, "effort", effort)
    mode_flag = "fast" if fast else ("standard" if standard_ else ("thorough" if thorough else None))
    if mode_flag:
        config["certifier_mode"] = mode_flag
        sources["certifier_mode"] = "cli"
    from otto.config import get_max_turns_per_call
    try:
        config["max_turns_per_call"] = get_max_turns_per_call(config)
    except ConfigError as exc:
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(2)
    _print_config_banner(console, config, sources, config_path)

    # Resolve intent: argument > intent.md > README.md
    if not intent:
        from otto.config import resolve_intent
        try:
            intent = _normalize_intent(resolve_intent(project_dir) or "")
        except (ConfigError, ValueError) as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(2)
        if intent:
            console.print("  [dim]Intent from project files[/dim]")
        else:
            error_console.print("[error]No intent provided. Pass as argument or create intent.md[/error]")
            sys.exit(2)
    else:
        from otto.config import MAX_INTENT_CHARS, validate_text_limit

        try:
            intent = validate_text_limit(
                intent,
                kind="intent",
                source="CLI argument",
                max_chars=MAX_INTENT_CHARS,
            )
        except ConfigError as exc:
            error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
            sys.exit(2)

    _mode = resolve_certifier_mode(config, cli_mode=mode_flag)

    if _mode == "fast":
        mode_label = "happy-path only (Must-Have stories, ~30s)"
    elif _mode == "thorough":
        mode_label = "adversarial (Must-Have + edge probes + code review)"
    else:
        mode_label = "standard (Must-Have + generic checklist: CRUD, edge cases, access control)"
    console.print(f"\n  [bold]Certifying[/bold] \u2014 {mode_label}\n")

    from otto.certifier import run_agentic_certifier
    from otto.budget import RunBudget
    budget = RunBudget.start_from(config)

    start = time.time()
    try:
        report = None
        total_certify_cost = 0.0
        required_passes = 2 if strict else 1
        completed_passes = 0
        while completed_passes < required_passes:
            active_session_id = session_id if completed_passes == 0 else _new_run_id(project_dir)
            report = asyncio.run(run_agentic_certifier(
                intent=intent,
                project_dir=project_dir,
                config=config,
                mode=_mode,
                budget=budget,
                session_id=active_session_id,
            ))
            total_certify_cost += float(report.cost_usd or 0.0)
            if report.outcome.value != "passed":
                break
            completed_passes += 1
            if completed_passes < required_passes:
                console.print(
                    f"  [dim]\u2713 pass {completed_passes}/{required_passes} "
                    "\u2014 re-running verification for consistency (strict mode)[/dim]"
                )
    except KeyboardInterrupt:
        console.print("\n  Aborted.")
        sys.exit(1)
    except Exception as e:
        from otto.markers import MalformedCertifierOutputError

        if isinstance(e, AgentCallError):
            message = str(e)
            if message.startswith("Timed out after"):
                error_console.print(
                    f"[error]{rich_escape(message)}.[/error]\n"
                    "  Raise `--budget` or `run_budget_seconds` in otto.yaml. "
                    "Standalone certify has no resume — rerun the command."
                )
                sys.exit(1)
            error_console.print(
                f"[error]{rich_escape(message)}[/error]\n"
                "  Standalone certify has no resume — rerun the command."
            )
            sys.exit(1)
        if isinstance(e, MalformedCertifierOutputError):
            error_console.print(f"[error]{rich_escape(str(e))}[/error]")
            sys.exit(1)
        error_console.print(f"[error]Certification failed: {rich_escape(str(e))}[/error]")
        sys.exit(1)

    duration = time.time() - start
    assert report is not None
    story_results = report.story_results
    passed_count = sum(1 for s in story_results if s.get("passed"))

    # Display results
    if story_results:
        for s in story_results:
            icon = "[success]\u2713[/success]" if s.get("passed") else "[red]\u2717[/red]"
            console.print(f"    {icon} {rich_escape(s.get('summary', s.get('story_id', '')))}")

    console.print()
    outcome = report.outcome.value
    if outcome == "passed":
        console.print(f"  [success bold]PASSED[/success bold] \u2014 {passed_count}/{len(story_results)} stories")
    else:
        console.print(f"  [red bold]FAILED[/red bold] \u2014 {passed_count}/{len(story_results)} stories")

    console.print(f"  Cost: ${total_certify_cost:.2f}  Duration: {duration:.0f}s")

    # PoW report location — new layout uses session-scoped paths, pointed
    # to by the `latest` symlink. Fall back to the legacy certifier/latest
    # path for projects still on pre-restructure layout.
    from otto import paths as _paths
    latest_session = _paths.resolve_pointer(project_dir, _paths.LATEST_POINTER)
    if latest_session is not None:
        pow_html = latest_session / "certify" / "proof-of-work.html"
        if pow_html.exists():
            console.print(f"  Report: {pow_html}")
    else:
        legacy_pow = project_dir / "otto_logs" / "certifier" / "latest" / "proof-of-work.html"
        if legacy_pow.exists():
            console.print(f"  Report: {legacy_pow}")

    # Per-run manifest for queue/merge consumption. Canonical at session root,
    # with a queue mirror when OTTO_QUEUE_TASK_ID is present.
    try:
        from otto.manifest import (
            QUEUE_TASK_ENV,
            current_head_sha,
            make_manifest,
            write_manifest,
        )
        from otto.branching import current_branch
        cert_run_id = report.run_id or "unknown"
        cert_session_dir = _paths.session_dir(project_dir, cert_run_id)
        cert_certify_dir = _paths.certify_dir(project_dir, cert_run_id)
        manifest = make_manifest(
            command="certify",
            argv=list(sys.argv[1:]),
            queue_task_id=os.environ.get(QUEUE_TASK_ENV),
            run_id=cert_run_id,
            branch=(current_branch(project_dir) or None),
            checkpoint_path=None,  # certify doesn't write a checkpoint.json
            proof_of_work_path=cert_certify_dir / "proof-of-work.json",
            cost_usd=float(report.cost_usd),
            duration_s=duration,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
            head_sha=current_head_sha(project_dir),
            resolved_intent=intent,
            exit_status="success" if outcome == "passed" else "failure",
        )
        write_manifest(manifest, project_dir=project_dir, fallback_dir=cert_session_dir)
    except Exception as exc:
        from otto.theme import error_console as _err
        _err.print(f"[yellow]warning: manifest write failed: {exc}[/yellow]")

    console.print()
    sys.exit(0 if outcome == "passed" else 1)


# Setup command (registered from otto/cli_setup.py)
from otto.cli_setup import register_setup_command
register_setup_command(main)

# History command (registered from otto/cli_logs.py)
from otto.cli_logs import register_history_command, register_replay_command
register_history_command(main)
register_replay_command(main)

# PoW command (registered from otto/cli_pow.py)
from otto.cli_pow import register_pow_command
register_pow_command(main)

# Improve commands (registered from otto/cli_improve.py)
from otto.cli_improve import register_improve_commands
register_improve_commands(main)

# Queue commands (Phase 2 — registered from otto/cli_queue.py)
from otto.cli_queue import register_queue_commands
register_queue_commands(main)

# Merge command (Phase 4 — registered from otto/cli_merge.py)
from otto.cli_merge import register_merge_command
register_merge_command(main)
