"""Shared display helpers for agent tool-use output."""

import re

# ANSI escape sequences
_CYAN = "\033[36m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"

# Temp dir patterns to strip from displayed paths
_TEMP_DIR_PATTERNS = re.compile(r".*/otto_(?:testgen|rubric)_[^/]+/")


def _truncate_at_word(text: str, max_len: int) -> str:
    """Truncate text at a word boundary, appending '...' if truncated.

    If the text is shorter than max_len, returns it unchanged.
    Otherwise finds the last space at or before max_len and cuts there.
    Falls back to hard cut at max_len if no space is found.
    """
    if len(text) <= max_len:
        return text
    # Find last space at or before max_len
    cut = text.rfind(" ", 0, max_len)
    if cut <= 0:
        # No space found — hard cut (better than returning nothing)
        cut = max_len
    return text[:cut] + "..."


def _strip_temp_prefix(detail: str) -> str:
    """Strip otto temp dir prefixes from a path/command for cleaner display."""
    return _TEMP_DIR_PATTERNS.sub("", detail)


def _extract_tool_detail(name: str, inputs: dict) -> str:
    """Extract the most relevant detail string from a tool use block's inputs."""
    if name in ("Read", "Glob", "Grep"):
        return inputs.get("file_path") or inputs.get("path") or inputs.get("pattern") or ""
    elif name in ("Edit", "Write"):
        return inputs.get("file_path") or ""
    elif name == "Bash":
        cmd = inputs.get("command") or ""
        return _truncate_at_word(cmd, 80)
    return ""


def print_agent_tool(block, quiet: bool = False) -> str:
    """Print an agent tool use block with ANSI styling and return a log line.

    Accepts any object with .name and .input attributes (ToolUseBlock or similar).
    When quiet=True, skips printing but still returns the log line.

    Prints:  ``  \\033[36m\\033[1m\\u25cf ToolName\\033[0m  \\033[2mdetail\\033[0m``
    Returns: ``\\u2192 ToolName  detail``  (plain text for logging)
    """
    name = block.name
    inputs = block.input or {}

    detail = _extract_tool_detail(name, inputs)
    detail = _strip_temp_prefix(detail)

    if not quiet:
        label = f"{_CYAN}{_BOLD}\u25cf {name}{_RESET}"
        if detail:
            print(f"  {label}  {_DIM}{detail}{_RESET}", flush=True)
        else:
            print(f"  {label}", flush=True)

    return f"\u2192 {name}  {detail}"
