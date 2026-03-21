"""Otto display system — Rich-based terminal output.

Provides thread-safe display components using Rich Live for real-time
progress updates. Replaces all raw ANSI escape codes and manual
stdout management.

Also re-exports utility functions used by other otto modules.
"""

import re
import threading
import time
from typing import Any

from rich.console import Console, Group
from rich.markup import escape as rich_escape
from rich.text import Text
from rich.spinner import Spinner
from rich.live import Live

# ---------------------------------------------------------------------------
# Module-level console (Rich Console is thread-safe by design)
# ---------------------------------------------------------------------------

console = Console(highlight=False)

# ---------------------------------------------------------------------------
# Utility functions (preserved API — used by runner.py, architect.py, etc.)
# ---------------------------------------------------------------------------

# Temp dir patterns to strip from displayed paths
_TEMP_DIR_PATTERNS = re.compile(r".*/otto_(?:testgen|spec)_[^/]+/")


def _truncate_at_word(text: str, max_len: int) -> str:
    """Truncate text at a word boundary, appending '...' if truncated."""
    if len(text) <= max_len:
        return text
    cut = text.rfind(" ", 0, max_len)
    if cut <= 0:
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
    """Print an agent tool use block with Rich styling and return a log line.

    Accepts any object with .name and .input attributes (ToolUseBlock or similar).
    When quiet=True, skips printing but still returns the log line.
    """
    name = block.name
    inputs = block.input or {}

    detail = _extract_tool_detail(name, inputs)
    detail = _strip_temp_prefix(detail)

    if not quiet:
        label = Text()
        label.append(f"\u25cf {name}", style="bold cyan")
        if detail:
            label.append(f"  {detail}", style="dim")
        console.print(f"  ", end="")
        console.print(label)

    return f"\u2192 {name}  {detail}"


# ---------------------------------------------------------------------------
# Phase progress display — Rich Live-based
# ---------------------------------------------------------------------------

PHASE_ORDER = ["prepare", "coding", "test", "qa", "merge"]

# Phase status -> (icon, style)
_PHASE_STYLES: dict[str, tuple[str, str]] = {
    "done":    ("\u2713", "green"),
    "fail":    ("\u2717", "red"),
    "running": ("\u25cf", "cyan"),
    "pending": (" ", "dim"),
}


class TaskDisplay:
    """Thread-safe task progress display using Rich Live.

    All state updates go through update_phase() / add_tool() which
    acquire _lock, then call _live.update() which Rich handles
    thread-safely.

    The display overwrites itself in-place — no line-by-line accumulation,
    no ANSI escape management.
    """

    def __init__(self, console: Console | None = None):
        self._console = console or globals()["console"]
        self._lock = threading.Lock()
        self._live: Live | None = None
        self._start_time: float = 0.0

        # State (all protected by _lock)
        self._phases: dict[str, dict[str, Any]] = {
            p: {"status": "pending", "time_s": 0.0}
            for p in PHASE_ORDER
        }
        self._tools: list[str] = []  # recent tool/finding lines
        self._current_phase: str | None = None
        self._max_tools = 8  # show last N tool lines

    def start(self) -> None:
        """Start the live display."""
        self._start_time = time.monotonic()
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=2,
            transient=True,  # clear live display when stopped
            get_renderable=self._render,  # Rich calls this on each refresh cycle
        )
        self._live.start()

    def stop(self) -> str:
        """Stop the live display and print final static state. Returns elapsed time string."""
        if self._live:
            self._live.stop()
            self._live = None

        # Print final static renderable (non-transient)
        final = self._render_final()
        if final:
            self._console.print(final)

        elapsed = time.monotonic() - self._start_time
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        return f"{mins}m{secs:02d}s" if mins else f"{secs}s"

    def update_phase(self, name: str, status: str, time_s: float = 0.0,
                     error: str = "", detail: str = "", **kwargs) -> None:
        """Update a phase's state. Thread-safe."""
        with self._lock:
            if name in self._phases:
                if status == "running":
                    # Reset all fields when entering running to prevent stale
                    # data from a previous attempt leaking into the display
                    self._phases[name] = {"status": "running", "time_s": 0.0}
                    self._current_phase = name
                    self._tools.clear()
                else:
                    self._phases[name]["status"] = status
                    # Always assign values (not truthiness-gated) so zeros
                    # and empty strings properly clear previous values
                    self._phases[name]["time_s"] = time_s
                    if error:
                        self._phases[name]["error"] = error
                    elif "error" in self._phases[name]:
                        del self._phases[name]["error"]
                    if detail:
                        self._phases[name]["detail"] = detail
                    elif "detail" in self._phases[name]:
                        del self._phases[name]["detail"]
                    cost = kwargs.get("cost", 0)
                    if cost:
                        self._phases[name]["cost"] = cost
                    elif "cost" in self._phases[name]:
                        del self._phases[name]["cost"]
        self._refresh()

    def add_tool(self, line: str = "", name: str = "", detail: str = "") -> None:
        """Record an agent tool call or QA finding. Thread-safe."""
        with self._lock:
            if not line and name:
                line = f"{name}  {detail}" if detail else name
            if line:
                self._tools.append(line)
                # Trim to max
                if len(self._tools) > self._max_tools:
                    self._tools = self._tools[-self._max_tools:]
        self._refresh()

    def add_finding(self, text: str) -> None:
        """Record a QA finding. Thread-safe."""
        self.add_tool(line=text)

    def _refresh(self) -> None:
        """Push a new renderable to the live display."""
        if self._live:
            try:
                self._live.update(self._render())
            except Exception:
                pass  # live display was stopped between check and use

    def _render(self) -> Group:
        """Build the complete live display. Called on every update."""
        with self._lock:
            parts: list[Text | Spinner] = []

            for phase in PHASE_ORDER:
                pdata = self._phases[phase]
                pstatus = pdata["status"]
                icon, style = _PHASE_STYLES.get(pstatus, (" ", "dim"))

                line = Text()
                line.append(f"  {icon} ", style=style)
                line.append(f"{phase:<10}", style=style if pstatus != "running" else "bold cyan")

                if pstatus in ("done", "fail"):
                    extras = []
                    ptime = pdata.get("time_s", 0.0)
                    if ptime:
                        extras.append(f"{ptime:.0f}s")
                    cost = pdata.get("cost", 0)
                    if cost:
                        extras.append(f"${cost:.2f}")
                    detail = pdata.get("detail", "")
                    if detail:
                        extras.append(detail[:50])
                    if pstatus == "fail":
                        err = pdata.get("error", "")[:50]
                        if err:
                            extras.append(err)
                    if extras:
                        line.append("  " + "  ".join(extras), style="dim")

                elif pstatus == "running":
                    elapsed = time.monotonic() - self._start_time
                    mins = int(elapsed // 60)
                    secs = int(elapsed % 60)
                    time_str = f"{mins}:{secs:02d}" if mins else f"{secs}s"
                    cost = pdata.get("cost", 0)
                    cost_str = f"  ${cost:.2f}" if cost else ""
                    line.append(f"  {time_str}{cost_str}", style="dim")

                parts.append(line)

                # Show tool lines under the active phase
                if pstatus == "running" and self._tools:
                    for tool_line in self._tools:
                        tl = Text()
                        tl.append(f"      {tool_line[:72]}", style="dim")
                        parts.append(tl)

            return Group(*parts)

    def _render_final(self) -> Group | None:
        """Build the final static display after live stops."""
        with self._lock:
            parts: list[Text] = []
            has_content = False

            for phase in PHASE_ORDER:
                pdata = self._phases[phase]
                pstatus = pdata["status"]

                if pstatus == "pending":
                    continue  # don't show pending phases in final output

                has_content = True
                icon, style = _PHASE_STYLES.get(pstatus, (" ", "dim"))

                line = Text()
                line.append(f"  {icon} ", style=style)
                line.append(f"{phase:<10}", style=style)

                if pstatus in ("done", "fail"):
                    extras = []
                    ptime = pdata.get("time_s", 0.0)
                    if ptime:
                        extras.append(f"{ptime:.0f}s")
                    cost = pdata.get("cost", 0)
                    if cost:
                        extras.append(f"${cost:.2f}")
                    detail = pdata.get("detail", "")
                    if detail:
                        extras.append(detail[:50])
                    if pstatus == "fail":
                        err = pdata.get("error", "")[:50]
                        if err:
                            extras.append(err)
                    if extras:
                        line.append("  " + "  ".join(extras), style="dim")

                elif pstatus == "running":
                    # Phase was still running when stopped (interrupted)
                    line.append("  interrupted", style="dim")

                parts.append(line)

            return Group(*parts) if has_content else None


# ---------------------------------------------------------------------------
# Summary helpers (used by runner.py's _print_summary)
# ---------------------------------------------------------------------------

def format_duration(seconds: float) -> str:
    """Format a duration in seconds to a human-readable string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m{secs}s"


def format_cost(cost: float) -> str:
    """Format a cost value to a string."""
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"
