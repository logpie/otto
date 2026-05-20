"""Otto CLI — improve command group (bugs, feature, target).

Phase C deletion (tick 63): the legacy `_run_improve` / `_run_improve_locked`
outer loops + their helpers are gone. The click subcommand bodies stay so
the surface (`otto improve bugs|feature|target`) keeps responding, but the
``--legacy`` flag is now a hard error pointing operators at the new
``--i2p`` (default) pipeline. Pure helpers (`_render_results_section`,
`_VERDICT_GLYPHS`, `_journey_verdict`, intent/option callbacks) are
preserved because external tests still cover them and they remain useful
for future renderers.
"""

import sys
from pathlib import Path

import click

from otto.cli_options import (
    max_turns_option,
    positive_budget_option,
    rounds_option,
)
from otto.display import CONTEXT_SETTINGS, console, rich_escape
from otto.config import detect_project_kind, require_git, resolve_project_dir
from otto.theme import error_console


# --------------------------------------------------------------------------- #
# Report rendering helpers (pure — kept post-Phase-C deletion because they
# have direct unit tests in tests/test_improvement_report_*.py and remain
# useful for any future report renderer wired to the new audit pipeline).
# --------------------------------------------------------------------------- #


# Glyphs/labels per verdict tier. The icon is what the operator sees first;
# the trailing label disambiguates for screen-readers and grep-able audits.
# WARN is intentionally NOT a check — see W3-IMPORTANT-3 in
# docs/mc-audit/live-findings.md (operator misread three WARN observations
# as PASS because the prior renderer hard-coded ✓ for any `passed`-truthy
# story, including warnings).
_VERDICT_GLYPHS: dict[str, tuple[str, str, str]] = {
    # verdict        icon  text-label   semantic class (for any future HTML
    #                                   /ANSI styling — kept as plain text
    #                                   in markdown so reports stay
    #                                   render-anywhere).
    "PASS":          ("✓", "PASS", "success"),
    "WARN":          ("!",      "WARN", "warning"),
    "FAIL":          ("✗", "FAIL", "danger"),
    "SKIPPED":       ("–", "SKIP", "muted"),
    "FLAG_FOR_HUMAN":("⚠", "FLAG", "warning"),
}


def _journey_verdict(journey: dict[str, object]) -> str:
    """Return the canonical verdict tag for a journey row.

    Falls back to PASS/FAIL by `passed` for any caller that hasn't been
    upgraded to populate `verdict` (older serialized state, mock data).
    """
    raw = journey.get("verdict")
    if isinstance(raw, str) and raw:
        verdict = raw.upper()
        if verdict in _VERDICT_GLYPHS:
            return verdict
    return "PASS" if journey.get("passed") else "FAIL"


def _render_results_section(journeys: list[dict[str, object]]) -> list[str]:
    """Render the `## Results` block as markdown lines.

    Each row carries the verdict glyph AND a bracketed `[PASS]/[WARN]/[FAIL]`
    label so the report is unambiguous even where the glyph fails to render
    (terminals without unicode, plain-text exports). The label also makes
    the rows trivially greppable for downstream audit tooling.
    """
    if not journeys:
        return []
    lines: list[str] = ["## Results"]
    for j in journeys:
        verdict = _journey_verdict(j)
        icon, label, _cls = _VERDICT_GLYPHS.get(verdict, ("?", verdict, "muted"))
        name = j.get("name") or j.get("story_id") or ""
        lines.append(f"- {icon} [{label}] {name}")
    lines.append("")
    return lines


def _exit_legacy_removed() -> None:
    """Phase C: legacy improve loop is gone. Point the user at the new path.

    Kept as a single helper so the three subcommand bodies all surface the
    exact same migration message. The CLI is the only place an operator
    sees this — the new ``--i2p`` path (now the default in
    ``otto/config.py::default_pipeline``) covers every prior code path.
    """
    error_console.print(
        "[error]Legacy improve loop has been removed in Phase C. "
        "Use --i2p (default) or pin --legacy in older versions.[/error]"
    )
    sys.exit(1)


def _resolve_pipeline_or_exit(*, i2p: bool, legacy: bool, project_dir: Path) -> str:
    from otto.cli_run import resolve_pipeline_choice

    pipeline_choice = resolve_pipeline_choice(
        i2p_flag=i2p,
        legacy_flag=legacy,
        project_dir=project_dir,
    )
    if pipeline_choice == "legacy":
        _exit_legacy_removed()
    return pipeline_choice


def _require_intent(
    project_dir: Path,
    *,
    fallback: str | None = None,
    fallback_label: str = "argument",
    prefer_fallback: bool = False,
) -> str:
    """Resolve intent or exit with error. Normalizes whitespace so multiline
    intent files don't leak embedded line-wraps into resolved_intent."""
    from otto.config import ConfigError, _normalize_intent, resolve_intent

    fallback_intent = _normalize_intent(fallback or "")
    if prefer_fallback and fallback_intent:
        console.print(f"  [dim]Intent from {fallback_label}[/dim]")
        return fallback_intent
    try:
        intent = _normalize_intent(resolve_intent(project_dir) or "")
    except ConfigError as exc:
        if fallback_intent:
            console.print(f"  [dim]Intent from {fallback_label}[/dim]")
            return fallback_intent
        error_console.print(f"[error]{rich_escape(str(exc))}[/error]")
        sys.exit(2)
    if not intent:
        if fallback_intent:
            console.print(f"  [dim]Intent from {fallback_label}[/dim]")
            return fallback_intent
        error_console.print(
            "[error]No product description found. Create intent.md[/error]"
        )
        sys.exit(2)
    console.print("  [dim]Intent from project files[/dim]")
    return intent


def register_improve_commands(main: click.Group) -> None:
    """Register the improve command group on the main CLI group."""

    @main.group(context_settings=CONTEXT_SETTINGS, invoke_without_command=True)
    @click.pass_context
    def improve(ctx):
        """Improve the current project — find bugs, add features, or hit targets.

        Requires a subcommand:

        \b
            otto improve bugs                  # find and fix bugs
            otto improve feature "search UX"   # add/improve features
            otto improve target "latency < 100ms"  # hit a metric target
        """
        if ctx.invoked_subcommand is None:
            error_console.print(
                "[error]Specify a mode: bugs, feature, target[/error]\n"
            )
            click.echo(ctx.get_help())
            ctx.exit(2)

    @improve.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("focus", required=False)
    @click.option("--rounds", "-n", default=None, type=int, callback=rounds_option, help="Maximum rounds, 1-50 (default from otto.yaml or 8)")
    @click.option("--split", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--agentic", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--resume", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option(
        "--force-cross-command-resume",
        is_flag=True,
        help="(legacy-only; ignored in --i2p mode)",
    )
    @click.option("--in-worktree", "in_worktree", is_flag=True,
                  help="(legacy-only; ignored in --i2p mode)")
    @click.option("--budget", default=None, type=int, callback=positive_budget_option, help="Total wall-clock budget in seconds, must be > 0 (default from otto.yaml or 3600)")
    @click.option("--max-turns", default=None, type=int, callback=max_turns_option, help="Max agent turns per call, 1-200 (default from otto.yaml or 200)")
    @click.option("--model", default=None, help="Override model for every agent (e.g. sonnet, haiku, gpt-5)")
    @click.option("--provider", default=None, help="Override provider for every agent: codex-app-server | codex | claude")
    @click.option("--effort", default=None, help="Override effort level for every agent: low | medium | high | max")
    @click.option("--improver-provider", default=None, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--improver-model", default=None, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--improver-effort", default=None, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--build-provider", default=None, hidden=True)
    @click.option("--build-model", default=None, hidden=True)
    @click.option("--build-effort", default=None, hidden=True)
    @click.option("--certifier-provider", default=None, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--certifier-model", default=None, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--certifier-effort", default=None, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--fix-provider", default=None, hidden=True)
    @click.option("--fix-model", default=None, hidden=True)
    @click.option("--fix-effort", default=None, hidden=True)
    @click.option("--fast", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--standard", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--thorough", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--strict", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--verbose", is_flag=True, help="Show detailed live progress, including tool-call counts")
    @click.option("--debug-unredacted", is_flag=True, help="Also write unredacted raw logs under sessions/<id>/raw/ (do not share)")
    @click.option("--allow-dirty", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--break-lock", is_flag=True, help="Force-clear the project lock before starting")
    @click.option("--force", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option(
        "--i2p",
        is_flag=True,
        help=(
            "Force-route through the new intent-to-product stack "
            "(brownfield-compile + audit/repair loop). Overrides otto.yaml "
            "default_pipeline. Phase B.3 default."
        ),
    )
    @click.option(
        "--legacy",
        is_flag=True,
        help=(
            "REMOVED in Phase C — passing this flag now exits with an "
            "error. Use --i2p (default) instead."
        ),
    )
    def bugs(focus, rounds, split, agentic, resume, force_cross_command_resume, in_worktree, budget, max_turns, model, provider, effort, improver_provider, improver_model, improver_effort, build_provider, build_model, build_effort, certifier_provider, certifier_model, certifier_effort, fix_provider, fix_model, fix_effort, fast, standard, thorough, strict, verbose, debug_unredacted, allow_dirty, break_lock, force, i2p, legacy):
        """Find and fix bugs, edge cases, and error handling gaps.

        Routes through the new intent-to-product stack by default. Pass
        ``--legacy`` to surface the Phase C migration error message.

        \b
        Examples:
            otto improve bugs                  # find and fix all bugs
            otto improve bugs "error handling" # focus on error handling
            otto improve bugs -n 5             # 5 rounds
        """
        require_git()
        project_dir = resolve_project_dir(Path.cwd())
        _resolve_pipeline_or_exit(i2p=i2p, legacy=legacy, project_dir=project_dir)
        intent = _require_intent(
            project_dir,
            fallback=focus,
            fallback_label="focus",
            prefer_fallback=True,
        )
        # i2p path
        _ignored = [
            name for name, val in (
                ("--split", split),
                ("--agentic", agentic),
                ("--resume", resume),
                ("--in-worktree", in_worktree),
                ("--fast", fast),
                ("--standard", standard),
                ("--thorough", thorough),
                ("--strict", strict),
                ("--force", force),
            ) if val
        ]
        if _ignored:
            console.print(
                "  [yellow]i2p mode: these flags are ignored — pass them to "
                f"`otto run` for full pipeline control: {', '.join(_ignored)}[/yellow]"
            )
        from otto.cli_run import orchestrate_improve
        orchestrate_improve(
            intent=intent,
            project_kind=detect_project_kind(project_dir),
            break_lock=break_lock,
            project_dir=project_dir,
            rounds=rounds,
            focus=focus,
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

    @improve.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("focus", required=False)
    @click.option("--rounds", "-n", default=None, type=int, callback=rounds_option, help="Maximum rounds, 1-50 (default from otto.yaml or 8)")
    @click.option("--split", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--agentic", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--resume", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option(
        "--force-cross-command-resume",
        is_flag=True,
        help="(legacy-only; ignored in --i2p mode)",
    )
    @click.option("--in-worktree", "in_worktree", is_flag=True,
                  help="(legacy-only; ignored in --i2p mode)")
    @click.option("--budget", default=None, type=int, callback=positive_budget_option, help="Total wall-clock budget in seconds, must be > 0 (default from otto.yaml or 3600)")
    @click.option("--max-turns", default=None, type=int, callback=max_turns_option, help="Max agent turns per call, 1-200 (default from otto.yaml or 200)")
    @click.option("--model", default=None, help="Override model for every agent (e.g. sonnet, haiku, gpt-5)")
    @click.option("--provider", default=None, help="Override provider for every agent: codex-app-server | codex | claude")
    @click.option("--effort", default=None, help="Override effort level for every agent: low | medium | high | max")
    @click.option("--improver-provider", default=None, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--improver-model", default=None, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--improver-effort", default=None, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--build-provider", default=None, hidden=True)
    @click.option("--build-model", default=None, hidden=True)
    @click.option("--build-effort", default=None, hidden=True)
    @click.option("--certifier-provider", default=None, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--certifier-model", default=None, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--certifier-effort", default=None, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--fix-provider", default=None, hidden=True)
    @click.option("--fix-model", default=None, hidden=True)
    @click.option("--fix-effort", default=None, hidden=True)
    @click.option("--strict", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--verbose", is_flag=True, help="Show detailed live progress, including tool-call counts")
    @click.option("--debug-unredacted", is_flag=True, help="Also write unredacted raw logs under sessions/<id>/raw/ (do not share)")
    @click.option("--allow-dirty", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--break-lock", is_flag=True, help="Force-clear the project lock before starting")
    @click.option("--force", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option(
        "--i2p",
        is_flag=True,
        help=(
            "Force-route through the new intent-to-product stack "
            "(brownfield-compile + audit/repair loop). Overrides "
            "otto.yaml default_pipeline. Phase B.3 default."
        ),
    )
    @click.option(
        "--legacy",
        is_flag=True,
        help=(
            "REMOVED in Phase C — passing this flag now exits with an "
            "error. Use --i2p (default) instead."
        ),
    )
    def feature(focus, rounds, split, agentic, resume, force_cross_command_resume, in_worktree, budget, max_turns, model, provider, effort, improver_provider, improver_model, improver_effort, build_provider, build_model, build_effort, certifier_provider, certifier_model, certifier_effort, fix_provider, fix_model, fix_effort, strict, verbose, debug_unredacted, allow_dirty, break_lock, force, i2p, legacy):
        """Suggest and implement product improvements.

        Routes through the new intent-to-product stack by default. Pass
        ``--legacy`` to surface the Phase C migration error message.

        \b
        Examples:
            otto improve feature               # suggest and implement improvements
            otto improve feature "search UX"   # focus on search experience
            otto improve feature -n 5          # 5 rounds
        """
        require_git()
        project_dir = resolve_project_dir(Path.cwd())
        _resolve_pipeline_or_exit(i2p=i2p, legacy=legacy, project_dir=project_dir)
        intent = _require_intent(
            project_dir,
            fallback=focus,
            fallback_label="focus",
            prefer_fallback=True,
        )
        _ignored = [
            name for name, val in (
                ("--split", split),
                ("--agentic", agentic),
                ("--resume", resume),
                ("--in-worktree", in_worktree),
                ("--strict", strict),
                ("--force", force),
            ) if val
        ]
        if _ignored:
            console.print(
                "  [yellow]i2p mode: these flags are ignored — pass them to "
                f"`otto run` for full pipeline control: {', '.join(_ignored)}[/yellow]"
            )
        from otto.cli_run import orchestrate_improve
        orchestrate_improve(
            intent=intent,
            project_kind=detect_project_kind(project_dir),
            break_lock=break_lock,
            project_dir=project_dir,
            rounds=rounds,
            focus=focus,
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

    @improve.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("goal", required=False)
    @click.option("--rounds", "-n", default=None, type=int, callback=rounds_option, help="Maximum rounds, 1-50 (default from otto.yaml or 8)")
    @click.option("--split", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--agentic", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--resume", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option(
        "--force-cross-command-resume",
        is_flag=True,
        help="(legacy-only; ignored in --i2p mode)",
    )
    @click.option("--in-worktree", "in_worktree", is_flag=True,
                  help="(legacy-only; ignored in --i2p mode)")
    @click.option("--budget", default=None, type=int, callback=positive_budget_option, help="Total wall-clock budget in seconds, must be > 0 (default from otto.yaml or 3600)")
    @click.option("--max-turns", default=None, type=int, callback=max_turns_option, help="Max agent turns per call, 1-200 (default from otto.yaml or 200)")
    @click.option("--model", default=None, help="Override model for every agent (e.g. sonnet, haiku, gpt-5)")
    @click.option("--provider", default=None, help="Override provider for every agent: codex-app-server | codex | claude")
    @click.option("--effort", default=None, help="Override effort level for every agent: low | medium | high | max")
    @click.option("--improver-provider", default=None, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--improver-model", default=None, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--improver-effort", default=None, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--build-provider", default=None, hidden=True)
    @click.option("--build-model", default=None, hidden=True)
    @click.option("--build-effort", default=None, hidden=True)
    @click.option("--certifier-provider", default=None, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--certifier-model", default=None, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--certifier-effort", default=None, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--fix-provider", default=None, hidden=True)
    @click.option("--fix-model", default=None, hidden=True)
    @click.option("--fix-effort", default=None, hidden=True)
    @click.option("--strict", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--verbose", is_flag=True, help="Show detailed live progress, including tool-call counts")
    @click.option("--debug-unredacted", is_flag=True, help="Also write unredacted raw logs under sessions/<id>/raw/ (do not share)")
    @click.option("--allow-dirty", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option("--break-lock", is_flag=True, help="Force-clear the project lock before starting")
    @click.option("--force", is_flag=True, help="(legacy-only; ignored in --i2p mode)")
    @click.option(
        "--i2p",
        is_flag=True,
        help=(
            "Force-route through the new intent-to-product stack "
            "(brownfield-compile + audit/repair loop). Overrides "
            "otto.yaml default_pipeline. Phase B.3 default."
        ),
    )
    @click.option(
        "--legacy",
        is_flag=True,
        help=(
            "REMOVED in Phase C — passing this flag now exits with an "
            "error. Use --i2p (default) instead."
        ),
    )
    def target(goal, rounds, split, agentic, resume, force_cross_command_resume, in_worktree, budget, max_turns, model, provider, effort, improver_provider, improver_model, improver_effort, build_provider, build_model, build_effort, certifier_provider, certifier_model, certifier_effort, fix_provider, fix_model, fix_effort, strict, verbose, debug_unredacted, allow_dirty, break_lock, force, i2p, legacy):
        """Optimize toward a measurable target.

        Routes through the new intent-to-product stack by default. Pass
        ``--legacy`` to surface the Phase C migration error message.

        \b
        Examples:
            otto improve target "latency < 100ms"
            otto improve target "bundle size < 500kb"
            otto improve target "test coverage > 90%"
            otto improve target "lighthouse score > 95" -n 10
        """
        require_git()
        project_dir = resolve_project_dir(Path.cwd())
        _resolve_pipeline_or_exit(i2p=i2p, legacy=legacy, project_dir=project_dir)
        _ignored = [
            name for name, val in (
                ("--split", split),
                ("--agentic", agentic),
                ("--resume", resume),
                ("--in-worktree", in_worktree),
                ("--strict", strict),
                ("--force", force),
            ) if val
        ]
        if _ignored:
            console.print(
                "  [yellow]i2p mode: these flags are ignored — pass them to "
                f"`otto run` for full pipeline control: {', '.join(_ignored)}[/yellow]"
            )
        from otto.cli_run import orchestrate_improve
        # `target` uses `goal` as the focus equivalent.
        orchestrate_improve(
            intent=_require_intent(
                project_dir,
                fallback=goal,
                fallback_label="goal",
                prefer_fallback=True,
            ),
            project_kind=detect_project_kind(project_dir),
            break_lock=break_lock,
            project_dir=project_dir,
            rounds=rounds,
            focus=goal,
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
