"""Otto CLI — improve command group (STUB).

The legacy improve loop has been removed. The three subcommands
(``otto improve bugs|feature|target``) remain as stubs so that
operators who still type the old commands get a clear migration
error pointing them at ``otto v5 run``.

Pure helpers ``_VERDICT_GLYPHS``, ``_journey_verdict``,
``_render_results_section`` are preserved because they have direct
unit-test coverage in ``tests/test_improvement_report_*`` and remain
useful for any future renderer.
"""

from __future__ import annotations

import sys

import click

from otto.display import CONTEXT_SETTINGS
from otto.theme import error_console


# --------------------------------------------------------------------------- #
# Pure report-rendering helpers (kept — see module docstring).
# --------------------------------------------------------------------------- #


_VERDICT_GLYPHS: dict[str, tuple[str, str, str]] = {
    # verdict        icon  text-label   semantic class
    "PASS":          ("✓", "PASS", "success"),
    "WARN":          ("!", "WARN", "warning"),
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
    """Render the `## Results` block as markdown lines."""
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


# --------------------------------------------------------------------------- #
# Stub CLI surface.
# --------------------------------------------------------------------------- #


_LEGACY_REMOVED_MSG = (
    "[error]`otto improve` has been removed along with the legacy "
    "pipeline. Use `otto v5 run \"<intent>\"` instead.[/error]"
)


def _exit_legacy_removed() -> None:
    """Hard-error landing pad for the deleted legacy improve loop."""
    error_console.print(_LEGACY_REMOVED_MSG)
    sys.exit(1)


def register_improve_commands(main: click.Group) -> None:
    """Register the stubbed `otto improve {bugs,feature,target}` group."""

    @main.group(context_settings=CONTEXT_SETTINGS, invoke_without_command=True)
    @click.pass_context
    def improve(ctx: click.Context) -> None:
        """[REMOVED] Use `otto v5 run` instead."""
        if ctx.invoked_subcommand is None:
            _exit_legacy_removed()

    @improve.command(
        "bugs",
        context_settings={**CONTEXT_SETTINGS, "ignore_unknown_options": True},
    )
    @click.argument("args", nargs=-1, type=click.UNPROCESSED)
    def bugs(args: tuple[str, ...]) -> None:  # noqa: ARG001
        """[REMOVED] Use `otto v5 run` instead."""
        _exit_legacy_removed()

    @improve.command(
        "feature",
        context_settings={**CONTEXT_SETTINGS, "ignore_unknown_options": True},
    )
    @click.argument("args", nargs=-1, type=click.UNPROCESSED)
    def feature(args: tuple[str, ...]) -> None:  # noqa: ARG001
        """[REMOVED] Use `otto v5 run` instead."""
        _exit_legacy_removed()

    @improve.command(
        "target",
        context_settings={**CONTEXT_SETTINGS, "ignore_unknown_options": True},
    )
    @click.argument("args", nargs=-1, type=click.UNPROCESSED)
    def target(args: tuple[str, ...]) -> None:  # noqa: ARG001
        """[REMOVED] Use `otto v5 run` instead."""
        _exit_legacy_removed()


__all__ = [
    "_VERDICT_GLYPHS",
    "_journey_verdict",
    "_render_results_section",
    "register_improve_commands",
]
