"""Otto display theme — shared console and styling constants.

Single source of truth for all otto display styling. Every module
that prints to the terminal should import `console` from here.
"""

from dataclasses import dataclass

from rich.console import Console
from rich.text import Text
from rich.theme import Theme

OTTO_THEME = Theme({
    "success": "green",
    "error": "red",
    "info": "cyan",
})


@dataclass(frozen=True)
class MissionControlPalette:
    running: str = "#2b8a3e"
    starting: str = "#2f9e44"
    queued: str = "#1971c2"
    paused: str = "#9c6f19"
    done: str = "#2b8a3e"
    failed: str = "#c92a2a"
    cancelled: str = "#b08900"
    interrupted: str = "#e67700"
    removed: str = "#6c757d"
    stale: str = "#6c757d"
    lagging: str = "#e67700"
    focus: str = "#1864ab"
    banner_info: str = "#0b7285"
    banner_success: str = "#2b8a3e"
    banner_warning: str = "#e67700"
    banner_error: str = "#c92a2a"
    search_match: str = "black on #ffe066"
    search_current: str = "bold black on #fcc419"


MISSION_CONTROL_THEME = MissionControlPalette()


def mission_control_status_style(status: str, *, overlay: str | None = None) -> str:
    if overlay == "stale":
        return MISSION_CONTROL_THEME.stale
    if overlay == "lagging":
        return MISSION_CONTROL_THEME.lagging
    return {
        "running": MISSION_CONTROL_THEME.running,
        "starting": MISSION_CONTROL_THEME.starting,
        "initializing": MISSION_CONTROL_THEME.starting,
        "terminating": MISSION_CONTROL_THEME.lagging,
        "queued": MISSION_CONTROL_THEME.queued,
        "paused": MISSION_CONTROL_THEME.paused,
        "done": MISSION_CONTROL_THEME.done,
        "failed": MISSION_CONTROL_THEME.failed,
        "cancelled": MISSION_CONTROL_THEME.cancelled,
        "interrupted": MISSION_CONTROL_THEME.interrupted,
        "removed": MISSION_CONTROL_THEME.removed,
    }.get(status, "")


def mission_control_banner_style(severity: str | None) -> str:
    return {
        "success": MISSION_CONTROL_THEME.banner_success,
        "warning": MISSION_CONTROL_THEME.banner_warning,
        "error": MISSION_CONTROL_THEME.banner_error,
    }.get(severity or "", MISSION_CONTROL_THEME.banner_info)


def mission_control_status_text(
    label: str,
    *,
    status: str,
    overlay: str | None = None,
) -> Text:
    return Text(label, style=mission_control_status_style(status, overlay=overlay))

# The one console instance — thread-safe, themed, no auto-highlighting
console = Console(highlight=False, theme=OTTO_THEME)

# Stderr console for error messages (same theme)
error_console = Console(stderr=True, highlight=False, theme=OTTO_THEME)
