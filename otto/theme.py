"""Otto display theme — shared console and styling constants.

Single source of truth for all otto display styling. Every module
that prints to the terminal should import `console` from here.
"""

from rich.console import Console
from rich.theme import Theme

OTTO_THEME = Theme({
    # Semantic styles
    "success": "green",
    "error": "red",
    "warning": "yellow",
    "info": "cyan",
    "active": "bold cyan",
    "secondary": "dim",

    # Phase styles
    "phase.done": "green",
    "phase.fail": "red",
    "phase.running": "bold cyan",
    "phase.pending": "dim",

    # Tool call styles
    "tool.write": "green",
    "tool.edit": "yellow",
    "tool.read": "dim",
    "tool.bash": "cyan",
    "tool.qa": "magenta",

    # QA findings
    "qa.pass": "green",
    "qa.fail": "red",
    "qa.spec": "bold",

    # Data styles
    "cost": "dim",
    "timing": "dim",
    "path": "dim",
})

# The one console instance — thread-safe, themed, no auto-highlighting
console = Console(highlight=False, theme=OTTO_THEME)
