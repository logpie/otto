"""Otto CLI — entrypoint for all otto commands."""

import asyncio
import json
import os
import re
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
from otto.config import _normalize_intent, load_config, require_git
from otto.display import CONTEXT_SETTINGS, console, rich_escape
from otto.theme import error_console


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
    # Fail early if otto is loaded from a different source than expected.
    # This catches the shared-venv bug where worktree otto runs main repo code.
    import otto as _otto_pkg
    _otto_src = str(Path(_otto_pkg.__file__).resolve().parent)
    try:
        _cwd = str(Path.cwd().resolve())
    except FileNotFoundError:
        click.echo(
            "ERROR: current directory no longer exists (deleted out from "
            "under the shell). cd to a real directory and retry.",
            err=True,
        )
        sys.exit(1)
    if "worktree" in _cwd and "worktree" not in _otto_src:
        click.echo(
            f"ERROR: otto loaded from {_otto_src}\n"
            f"  but cwd is a worktree ({_cwd}).\n"
            f"  Use the worktree's own venv: .venv/bin/otto",
            err=True,
        )
        sys.exit(1)


def _load_yaml_raw(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    import yaml as _yaml

    try:
        raw = _yaml.safe_load(config_path.read_text()) or {}
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


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
    from otto import paths
    if project_dir is None:
        # Fallback for callers that don't pass project_dir (legacy tests).
        import secrets
        stamp = time.strftime("%Y-%m-%d-%H%M%S")
        return f"{stamp}-{secrets.token_hex(3)}"
    return paths.new_session_id(project_dir)


async def _run_spec_phase(
    *,
    project_dir: Path,
    intent: str,
    spec: bool,
    spec_file: Path | None,
    auto_approve: bool,
    resume_state,
    config: dict,
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
        format_spec_section,
        read_spec_file,
        review_spec,
        run_spec_agent,
        spec_hash,
        validate_spec,
    )

    from otto import paths as _paths
    # Determine run_id (unified session_id): resume preserves, otherwise fresh.
    run_id = run_id or resume_state.run_id or _new_run_id(project_dir)
    _paths.ensure_session_scaffold(project_dir, run_id)
    run_dir = _paths.spec_dir(project_dir, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    _paths.set_pointer(project_dir, _paths.LATEST_POINTER, run_id)

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
                intent=intent,
                spec_path=str(spec_path_out),
                spec_hash=current_spec_hash,
                spec_version=current_spec_version,
                spec_cost=spec_cost,
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
                    intent=intent, spec_cost=spec_cost, spec_version=resume_state.spec_version,
                )
                spec_result = await run_spec_agent(
                    intent, project_dir, run_dir, config,
                    version=resume_state.spec_version, budget=budget,
                )
                spec_cost += spec_result.cost
                spec_duration += spec_result.duration_s
                current_phase = "spec_review"
                current_spec_path = str(spec_result.path)
                current_spec_hash = spec_hash(spec_result.content)
                current_spec_version = spec_result.version
                write_checkpoint(
                    project_dir, run_id=run_id, command="build", phase="spec_review",
                    intent=intent, spec_path=str(spec_result.path),
                    spec_hash=current_spec_hash, spec_version=current_spec_version, spec_cost=spec_cost,
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
                intent=intent, spec_cost=spec_cost, spec_version=resume_state.spec_version,
            )
            console.print("  [bold]Spec phase[/bold] — generating product spec...\n")
            spec_result = await run_spec_agent(
                intent, project_dir, run_dir, config, budget=budget,
            )
            spec_cost += spec_result.cost
            spec_duration += spec_result.duration_s
            current_phase = "spec_review"
            current_spec_path = str(spec_result.path)
            current_spec_hash = spec_hash(spec_result.content)
            current_spec_version = spec_result.version
            write_checkpoint(
                project_dir, run_id=run_id, command="build", phase="spec_review",
                intent=intent, spec_path=str(spec_result.path),
                spec_hash=current_spec_hash, spec_version=current_spec_version, spec_cost=spec_cost,
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
            intent=intent, spec_path=str(approved.path),
            spec_hash=current_spec_hash, spec_version=current_spec_version, spec_cost=spec_cost,
        )
        return run_id, approved.content, spec_cost, spec_duration

    except ValueError as exc:
        error_console.print(f"[error]{exc}[/error]")
        sys.exit(2)
    except KeyboardInterrupt:
        prior_cp = load_checkpoint(project_dir) or {}
        write_checkpoint(
            project_dir,
            run_id=run_id,
            command="build",
            phase=(prior_cp.get("phase", "") or current_phase),
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
            prior_cp = load_checkpoint(project_dir) or {}
            session_id = (
                exc.session_id
                or prior_cp.get("agent_session_id")
                or prior_cp.get("session_id", "")
            )
            write_checkpoint(
                project_dir, run_id=run_id, command="build",
                status="paused", phase=current_phase,
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
            clear_checkpoint(project_dir)
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
@click.option("--rounds", "-n", default=None, type=int, help="Max certification rounds (default from otto.yaml or 8)")
@click.option("--budget", default=None, type=int, help="Total wall-clock budget in seconds (default from otto.yaml or 3600)")
@click.option("--model", default=None, help="Override model for every agent (e.g. sonnet, haiku, gpt-5)")
@click.option("--provider", default=None, help="Override provider for every agent: claude | codex")
@click.option("--effort", default=None, help="Override effort level for every agent: low | medium | high | max")
@click.option("--strict", is_flag=True, help="Require two consecutive PASS rounds before stopping")
@click.option("--verbose", is_flag=True, help="Show detailed live progress, including tool-call counts")
@click.option("--resume", is_flag=True, help="Resume from last checkpoint (requires an in-progress run)")
@click.option("--spec", is_flag=True, help="Generate a reviewable spec before building")
@click.option("--spec-file", type=click.Path(exists=False, dir_okay=False, path_type=Path),
              default=None, help="Use a pre-written spec file (implies --yes)")
@click.option("--yes", is_flag=True, help="Auto-approve the generated spec (for CI/scripts)")
@click.option("--force", is_flag=True, help="Discard an active paused spec run and start fresh")
@click.option("--break-lock", is_flag=True, help="Force-clear the project lock before starting")
def build(intent, no_qa, fast, standard_, thorough, split, rounds, budget, model, provider, effort, strict, verbose, resume, spec, spec_file, yes, force, break_lock):
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
    """
    require_git()
    project_dir = Path.cwd()
    from otto import paths as _paths

    try:
        with _paths.project_lock(project_dir, "build", break_lock=break_lock):
            _build_locked(
                intent, no_qa, fast, standard_, thorough, split, rounds,
                budget, model, provider, effort, strict, verbose,
                resume, spec, spec_file, yes, force, project_dir,
            )
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
    model,
    provider,
    effort,
    strict,
    verbose,
    resume,
    spec,
    spec_file,
    yes,
    force,
    project_dir: Path,
):
    from otto import paths as _paths

    from otto.checkpoint import (
        initial_build_completed,
        load_checkpoint,
        is_spec_phase,
        print_resume_status,
        resolve_resume,
    )
    from otto.spec import read_spec_file

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
                    import json as _json
                    summary_path = session_dir / "summary.json"
                    summary_path.write_text(_json.dumps({
                        "status": "abandoned",
                        "abandoned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "run_id": run_id_to_archive,
                    }, indent=2))
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
        clear_checkpoint(project_dir)
        cp = None

    resume_state = resolve_resume(project_dir, resume, expected_command="build")
    use_spec = (
        bool(spec or spec_file)
        or is_spec_phase(resume_state.phase)
        or bool(resume_state.spec_path)
    )
    if use_spec and no_qa:
        error_console.print(
            "[error]--spec requires the certifier (Must-NOT-Have scope check); "
            "--no-qa is incompatible.[/error]"
        )
        sys.exit(2)

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

    resume_without_intent = bool(resume and resume_state.resumed and not intent)
    display_intent = intent or "(resumed run)"

    # Inherit intent from checkpoint for spec-phase resume, or fall back to
    # split-mode intent.md resolution for backwards compat.
    if not intent:
        if resume_without_intent:
            if resume_state.intent:
                intent = _normalize_intent(resume_state.intent)
                display_intent = intent
            elif split and not no_qa:
                from otto.config import resolve_intent
                intent = _normalize_intent(resolve_intent(project_dir) or "")
                if not intent:
                    error_console.print(
                        "[error]Resume needs a product description for split mode. "
                        "Provide INTENT or create intent.md/README.md.[/error]"
                    )
                    sys.exit(2)
        else:
            error_console.print("[error]Intent cannot be empty. Provide a description of what to build.[/error]")
            sys.exit(2)

    display_intent = intent or display_intent
    print_resume_status(console, resume_state, resume, expected_command="build")
    run_id: str = resume_state.run_id or ""
    if not run_id:
        run_id = _new_run_id(project_dir)

    # No auto-create: `otto.yaml` only exists if the user ran `otto setup`.
    # `load_config` returns built-in defaults + auto-detected project values
    # when the yaml is absent.
    config_path = project_dir / "otto.yaml"
    config = load_config(config_path)

    if no_qa:
        config["skip_product_qa"] = True
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
    if budget is not None:
        config["run_budget_seconds"] = budget
        sources["run_budget_seconds"] = "--budget"
    if model:
        config["model"] = model
        sources["model"] = "--model"
    if provider:
        config["provider"] = provider
        sources["provider"] = "--provider"
    if effort:
        config["effort"] = effort
        sources["effort"] = "--effort"
    if strict:
        config["strict_mode"] = True
        sources["strict_mode"] = "--strict"
    config["_verbose"] = bool(verbose)

    _print_config_banner(console, config, sources, config_path)
    _print_startup_context(console, project_dir, run_id)

    from otto.pipeline import build_agentic_v3, run_certify_fix_loop, BuildResult
    from otto.budget import RunBudget
    run_budget = RunBudget.start_from(config)

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
    certifier_mode = config.pop("_certifier_mode", None) or config.get("certifier_mode", "fast")

    try:
        if split and not no_qa:
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
                                     resume_rounds=resume_state.rounds,
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
        if isinstance(e, AgentCallError) and e.reason.startswith("Timed out after"):
            error_console.print(
                f"[error]Run timed out after {rich_escape(e.reason.removeprefix('Timed out after ').strip())} "
                "(run_budget_seconds).[/error]\n"
                f"  Narrative log: {build_dir / 'narrative.log'}\n"
                "  Resume:        otto build --resume"
            )
            sys.exit(1)
        if isinstance(e, AgentCallError) and e.reason.startswith("Agent crashed"):
            crash_reason = e.reason.removeprefix("Agent crashed:").strip()
            error_console.print(
                f"[error]Agent crashed: {rich_escape(crash_reason)}[/error]\n"
                f"  Narrative log: {build_dir / 'narrative.log'}  "
                "(last events may be incomplete)\n"
                "  Resume:        otto build --resume"
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

    if not result.passed:
        build_dir = _paths.build_dir(project_dir, result.build_id)
        certify_dir = _paths.certify_dir(project_dir, result.build_id)
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
@click.option("--standard", "standard_", is_flag=True, help="Standard mode — Must-Have + generic CRUD/edge/access checklist (default when no flag given)")
@click.option("--budget", default=None, type=int, help="Total wall-clock budget in seconds (default from otto.yaml or 3600)")
@click.option("--model", default=None, help="Override model for every agent (e.g. sonnet, haiku, gpt-5)")
@click.option("--provider", default=None, help="Override provider for every agent: claude | codex")
@click.option("--effort", default=None, help="Override effort level for every agent: low | medium | high | max")
@click.option("--break-lock", is_flag=True, help="Force-clear the project lock before starting")
def certify(intent, thorough, fast, standard_, budget, model, provider, effort, break_lock):
    """Certify a product — independent, builder-blind verification.

    Tests the product in the current directory as a real user. Works on
    any project regardless of how it was built (otto, bare CC, human).

    If no intent is given, reads intent.md or README.md from the project.

    Examples:
        otto certify                   # standard mode (default)
        otto certify --fast            # quick smoke test (~1-2 min)
        otto certify --thorough        # adversarial deep inspection
    """
    project_dir = Path.cwd()
    from otto import paths as _paths

    if sum(bool(x) for x in (fast, standard_, thorough)) > 1:
        error_console.print(
            "[error]--fast, --standard, and --thorough are mutually exclusive.[/error]"
        )
        sys.exit(2)
    intent = _normalize_intent(intent or "")

    try:
        with _paths.project_lock(project_dir, "certify", break_lock=break_lock):
            session_id = _new_run_id(project_dir)
            _certify_locked(
                intent, thorough, fast, standard_,
                budget, model, provider, effort,
                project_dir, session_id,
            )
    except _paths.LockBusy as exc:
        _exit_for_lock_busy(exc)


def _certify_locked(
    intent, thorough, fast, standard_,
    budget, model, provider, effort,
    project_dir: Path, session_id: str,
):

    # Load config so run_budget_seconds and other settings are respected
    config_path = project_dir / "otto.yaml"
    config = load_config(config_path)

    sources: dict[str, str] = {}
    if budget is not None:
        config["run_budget_seconds"] = budget
        sources["run_budget_seconds"] = "cli"
    if model:
        config["model"] = model
        sources["model"] = "cli"
    if provider:
        config["provider"] = provider
        sources["provider"] = "cli"
    if effort:
        config["effort"] = effort
        sources["effort"] = "cli"
    mode_flag = "fast" if fast else ("standard" if standard_ else ("thorough" if thorough else None))
    if mode_flag:
        config["certifier_mode"] = mode_flag
        sources["certifier_mode"] = "cli"
    _print_config_banner(console, config, sources, config_path)

    # Resolve intent: argument > intent.md > README.md
    if not intent:
        from otto.config import resolve_intent
        intent = _normalize_intent(resolve_intent(project_dir) or "")
        if intent:
            console.print("  [dim]Intent from project files[/dim]")
        else:
            error_console.print("[error]No intent provided. Pass as argument or create intent.md[/error]")
            sys.exit(2)

    if fast:
        mode_label = "happy-path only (Must-Have stories, ~30s)"
        _mode = "fast"
    elif thorough:
        mode_label = "adversarial (Must-Have + edge probes + code review)"
        _mode = "thorough"
    else:
        mode_label = "standard (Must-Have + generic checklist: CRUD, edge cases, access control)"
        _mode = "standard"
    console.print(f"\n  [bold]Certifying[/bold] \u2014 {mode_label}\n")

    from otto.certifier import run_agentic_certifier
    from otto.budget import RunBudget
    budget = RunBudget.start_from(config)

    start = time.time()
    try:
        report = asyncio.run(run_agentic_certifier(
            intent=intent,
            project_dir=project_dir,
            config=config,
            mode=_mode,
            budget=budget,
            session_id=session_id,
        ))
    except KeyboardInterrupt:
        console.print("\n  Aborted.")
        sys.exit(1)
    except Exception as e:
        if isinstance(e, AgentCallError):
            error_console.print(
                f"[error]Run budget exhausted ({e.reason}).[/error]\n"
                "  Raise `run_budget_seconds` in otto.yaml. Standalone "
                "certify has no resume — rerun the command."
            )
            sys.exit(1)
        error_console.print(f"[error]Certification failed: {rich_escape(str(e))}[/error]")
        sys.exit(1)

    duration = time.time() - start
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

    console.print(f"  Cost: ${report.cost_usd:.2f}  Duration: {duration:.0f}s")

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

    console.print()
    sys.exit(0 if outcome == "passed" else 1)


# Setup command (registered from otto/cli_setup.py)
from otto.cli_setup import register_setup_command
register_setup_command(main)

# History command (registered from otto/cli_logs.py)
from otto.cli_logs import register_history_command, register_replay_command
register_history_command(main)
register_replay_command(main)

# Improve commands (registered from otto/cli_improve.py)
from otto.cli_improve import register_improve_commands
register_improve_commands(main)
