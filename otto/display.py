"""Otto display helpers — console, formatting utilities."""

from rich.markup import escape as rich_escape  # noqa: F401

from otto.theme import console  # noqa: F401 — re-exported for backward compat

# Shared Click context settings for all CLI modules
CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------

def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m{secs:02d}s"


def format_cost(cost: float) -> str:
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"
