"""Otto display system — Rich-based terminal output.

Permanent scrolling log with live status footer. Events (tool calls, QA
findings, phase completions) print permanently via console.print() and are
visible in scrollback / captured by tee. A minimal Rich Live footer shows
the current phase timer, pinned at the bottom of the terminal.

When console.print() is called during a Rich Live session, Rich renders it
ABOVE the live area — scrolling log above, status footer below.
"""

import re
import threading
import time
from pathlib import Path
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
    """Print an agent tool use block with Rich styling and return a log line."""
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
# Phase progress display — permanent log + live footer
# ---------------------------------------------------------------------------

PHASE_ORDER = ["prepare", "coding", "test", "qa", "merge"]

_PHASE_ICONS = {
    "done": ("\u2713", "green"),
    "fail": ("\u2717", "red"),
}

# Internal files to exclude from coding file summaries
_INTERNAL_PATTERNS = {"otto_arch/", "task-notes/", ".md"}


class TaskDisplay:
    """Task progress display: permanent scrolling log + live status footer.

    Phase completions, tool calls, and QA findings print permanently via
    console.print(). A minimal Rich Live footer shows the current phase
    timer pinned at the bottom. Rich renders console.print() output above
    the live area automatically.

    All methods are thread-safe (called from the background JSONL reader).
    """

    def __init__(self, console_: Console | None = None):
        self._console = console_ or console
        self._lock = threading.Lock()
        self._live: Live | None = None
        self._start_time: float = 0.0
        self._phase_start: float = 0.0

        # State (protected by _lock)
        self._current_phase: str | None = None
        self._current_cost: float = 0.0
        self._qa_spec_count: int = 0
        self._qa_pass_count: int = 0
        self._coding_files: list[str] = []  # unique files written/edited

    def start(self) -> None:
        """Start the live status footer."""
        self._start_time = time.monotonic()
        self._phase_start = self._start_time
        self._live = Live(
            self._render_footer(),
            console=self._console,
            refresh_per_second=2,
            transient=True,
            get_renderable=self._render_footer,
        )
        self._live.start()

    def stop(self) -> str:
        """Stop the live footer. Returns elapsed time string."""
        if self._live:
            self._live.stop()
            self._live = None

        elapsed = time.monotonic() - self._start_time
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        return f"{mins}m{secs:02d}s" if mins else f"{secs}s"

    def update_phase(self, name: str, status: str, time_s: float = 0.0,
                     error: str = "", detail: str = "", **kwargs) -> None:
        """Update phase state. Prints permanent lines for transitions."""
        with self._lock:
            cost = kwargs.get("cost", 0)

            if status == "running":
                self._current_phase = name
                self._phase_start = time.monotonic()
                self._current_cost = 0.0
                if name == "qa":
                    self._qa_spec_count = 0
                    self._qa_pass_count = 0
                if name == "coding":
                    self._coding_files.clear()

            elif status == "done":
                self._print_phase_done(name, time_s, detail, cost)
                if name == self._current_phase:
                    self._current_phase = None

            elif status == "fail":
                self._print_phase_fail(name, time_s, error, cost)
                if name == self._current_phase:
                    self._current_phase = None

            if cost:
                self._current_cost = cost

        # Print "phase" header when entering running (permanent)
        if status == "running":
            self._console.print(f"  [bold cyan]{name}[/bold cyan]")

    def add_tool(self, line: str = "", name: str = "", detail: str = "") -> None:
        """Print a tool call permanently. Thread-safe."""
        if not line and name:
            line = f"{name}  {detail}" if detail else name
        if not line:
            return

        # Track files for coding summary
        with self._lock:
            if self._current_phase == "coding" and name in ("Write", "Edit"):
                fname = detail.rsplit("/", 1)[-1] if detail else ""
                if fname and not any(p in detail for p in _INTERNAL_PATTERNS):
                    if fname not in self._coding_files:
                        self._coding_files.append(fname)

        # Print permanently (appears above live footer)
        self._console.print(f"      [dim]{rich_escape(line[:80])}[/dim]")

    def add_finding(self, text: str) -> None:
        """Print a QA finding permanently. Thread-safe."""
        if not text:
            return

        with self._lock:
            if text.startswith("###"):
                self._qa_spec_count += 1
            if text.startswith("**PASS"):
                self._qa_pass_count += 1
            # Don't print the verdict line — it's summarized in phase_done
            if "VERDICT" in text:
                return

        # Format QA findings nicely
        if text.startswith("###"):
            # Spec header: "### Spec 1: Title" → "  ● Spec 1: Title"
            spec_text = text.lstrip("# ").strip()
            self._console.print(f"      [dim]{rich_escape(spec_text)}[/dim]")
        elif text.startswith("**PASS"):
            # Pass detail: "**PASS** — detail" → "      ✓ detail"
            detail_text = text.replace("**PASS**", "").lstrip(" \u2014-").strip()
            short = detail_text[:70] + "..." if len(detail_text) > 70 else detail_text
            self._console.print(f"        [green]\u2713[/green] [dim]{rich_escape(short)}[/dim]")
        elif text.startswith("**FAIL"):
            detail_text = text.replace("**FAIL**", "").lstrip(" \u2014-").strip()
            short = detail_text[:70] + "..." if len(detail_text) > 70 else detail_text
            self._console.print(f"        [red]\u2717[/red] [dim]{rich_escape(short)}[/dim]")
        elif text.startswith("- **("):
            # Sub-finding: "- **(a) desc**: PASS — detail"
            self._console.print(f"        [dim]{rich_escape(text[:78])}[/dim]")

    # -- Private helpers --

    def _print_phase_done(self, name: str, time_s: float, detail: str, cost: float) -> None:
        """Print a permanent phase completion line."""
        parts = []
        if time_s:
            parts.append(f"{time_s:.0f}s")
        if cost:
            parts.append(f"${cost:.2f}")

        # Phase-specific summaries
        if name == "coding":
            if detail:
                parts.append(detail[:50])
            elif self._coding_files:
                parts.append(f"{len(self._coding_files)} files")
        elif name == "test" and detail:
            parts.append(detail[:50])
        elif name == "qa":
            if self._qa_spec_count:
                if self._qa_pass_count == self._qa_spec_count:
                    parts.append(f"{self._qa_spec_count}/{self._qa_spec_count} specs passed")
                else:
                    parts.append(f"{self._qa_pass_count}/{self._qa_spec_count} specs passed")

        info = "  ".join(parts)
        self._console.print(f"  [green]\u2713[/green] {name:<10}[dim]{info}[/dim]")

    def _print_phase_fail(self, name: str, time_s: float, error: str, cost: float) -> None:
        """Print a permanent phase failure line."""
        parts = []
        if time_s:
            parts.append(f"{time_s:.0f}s")
        if cost:
            parts.append(f"${cost:.2f}")
        if error:
            parts.append(error[:50])
        info = "  ".join(parts)
        self._console.print(f"  [red]\u2717[/red] {name:<10}[dim]{info}[/dim]")

    def _render_footer(self) -> Text:
        """Render the minimal live footer (1 line: current phase + timer)."""
        with self._lock:
            if not self._current_phase:
                return Text("")

            elapsed = time.monotonic() - self._phase_start
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)
            time_str = f"{mins}:{secs:02d}" if mins else f"{secs}s"
            cost_str = f"  ${self._current_cost:.2f}" if self._current_cost else ""

            line = Text()
            line.append(f"  \u25cf {self._current_phase}  {time_str}{cost_str}", style="dim cyan")
            return line


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
