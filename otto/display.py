"""Otto display system — permanent scrolling log with live status footer.

Design principle: look like Claude Code. Minimal chrome, clean spacing,
semantic color, let the content speak. No decorative icons, no fancy
formatting — just clear information hierarchy.

Events print permanently via console.print() (visible in scrollback,
captured by tee). A minimal Rich Live footer shows current phase timer.
"""

import re
import threading
import time
from typing import Any

from rich.markup import escape as rich_escape
from rich.text import Text
from rich.live import Live

from otto.theme import console  # noqa: F401 — re-exported for backward compat

# ---------------------------------------------------------------------------
# Utility functions (preserved API — used by runner.py, architect.py, etc.)
# ---------------------------------------------------------------------------

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


def _shorten_path(path: str) -> str:
    """Shorten absolute paths to relative project paths."""
    if not path or not path.startswith("/"):
        # Also handle relative paths with project dir prefix
        # e.g., "otto-e2e-display2/calc.py" → "calc.py"
        if "/" in path and not any(path.startswith(m) for m in
                                    ("src/", "__tests__/", "tests/", "test/", "lib/")):
            fname = path.rsplit("/", 1)[-1]
            if "." in fname:  # has extension → likely a file
                return fname
        return path
    for marker in ("src/", "__tests__/", "tests/", "test/", "lib/", "app/", "components/"):
        idx = path.find(marker)
        if idx >= 0:
            return path[idx:]
    # Fall back to just the filename
    return path.rsplit("/", 1)[-1] if "/" in path else path


def print_agent_tool(block, quiet: bool = False) -> str:
    """Print an agent tool use block (CC style) and return a log line."""
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
# Internal file patterns to suppress
# ---------------------------------------------------------------------------

_INTERNAL_PATTERNS = {"otto_arch/", "task-notes/"}

PHASE_ORDER = ["prepare", "coding", "test", "qa", "merge"]


# ---------------------------------------------------------------------------
# TaskDisplay — the main display engine for otto run
# ---------------------------------------------------------------------------

class TaskDisplay:
    """Task progress: permanent scrolling log + live status footer.

    CC-inspired: minimal chrome, clean spacing, semantic color.
    """

    def __init__(self, console_=None):
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
        self._coding_files: list[str] = []
        self._last_tool_key: str = ""
        self._read_count: int = 0

    def start(self) -> None:
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
        if self._live:
            self._live.stop()
            self._live = None
        elapsed = time.monotonic() - self._start_time
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        return f"{mins}m{secs:02d}s" if mins else f"{secs}s"

    def update_phase(self, name: str, status: str, time_s: float = 0.0,
                     error: str = "", detail: str = "", **kwargs) -> None:
        attempt = kwargs.get("attempt", 0)
        cost = kwargs.get("cost", 0)

        with self._lock:
            if status == "running":
                self._current_phase = name
                self._phase_start = time.monotonic()
                self._current_cost = 0.0
                self._last_tool_key = ""
                self._read_count = 0
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

        if status == "running":
            retry = f"  [dim](retry {attempt})[/dim]" if attempt and attempt > 1 else ""
            self._console.print(f"  [bold cyan]{name}[/bold cyan]{retry}")

    def add_tool(self, line: str = "", name: str = "", detail: str = "",
                 data: dict | None = None) -> None:
        """Print a tool call permanently (CC style with inline content). Thread-safe."""
        # Support both old API (name=, detail=) and new (data=)
        if data:
            name = data.get("name", name)
            detail = data.get("detail", detail)

        if not name and not line:
            return
        if not name and line:
            name = line.split()[0] if line else ""
            detail = line.split("  ", 1)[-1] if "  " in line else ""

        raw_detail = detail or ""

        # Skip internal files (check BEFORE path shortening)
        if any(p in raw_detail for p in _INTERNAL_PATTERNS):
            return

        detail = _shorten_path(raw_detail)

        # Also skip after shortening (catches relative paths)
        if any(p in detail for p in _INTERNAL_PATTERNS):
            return

        # Deduplicate silently
        fname = detail.rsplit("/", 1)[-1] if "/" in detail else detail
        tool_key = f"{name}:{fname}"

        with self._lock:
            if self._current_phase == "coding" and name in ("Write", "Edit"):
                if fname and fname not in self._coding_files:
                    self._coding_files.append(fname)
            if tool_key == self._last_tool_key:
                return
            self._last_tool_key = tool_key
            if name == "Read":
                self._read_count += 1
                if self._read_count > 6:
                    if self._read_count == 7:
                        self._console.print("      [dim]...[/dim]")
                    return

        short = rich_escape(_shorten_path(detail)[:68])

        # CC style: "  ● Name  path" with breathing room
        if name == "QA":
            self._console.print(f"      [dim]{rich_escape(name)}  {short}[/dim]")
        else:
            self._console.print(f"      [bold cyan]\u25cf {name}[/bold cyan]  [dim]{short}[/dim]")

        # Inline content below tool call (CC shows diffs/previews)
        if data:
            # Edit: show mini diff
            old_lines = data.get("old_lines", [])
            new_lines = data.get("new_lines", [])
            if old_lines or new_lines:
                for ol in old_lines[:3]:
                    self._console.print(f"        [red]- {rich_escape(ol)}[/red]")
                old_total = data.get("old_total", 0)
                if old_total > 3:
                    self._console.print(f"        [dim]  ...{old_total - 3} more lines[/dim]")
                for nl in new_lines[:3]:
                    self._console.print(f"        [green]+ {rich_escape(nl)}[/green]")
                new_total = data.get("new_total", 0)
                if new_total > 3:
                    self._console.print(f"        [dim]  ...{new_total - 3} more lines[/dim]")

            # Write: show content preview
            preview = data.get("preview_lines", [])
            if preview:
                for pl in preview[:3]:
                    self._console.print(f"        [green]+ {rich_escape(pl)}[/green]")
                total = data.get("total_lines", 0)
                if total > 3:
                    self._console.print(f"        [dim]  ...{total - 3} more lines[/dim]")

    def add_tool_result(self, data: dict | None = None) -> None:
        """Print a tool result inline (Bash test output). Thread-safe."""
        if not data:
            return
        detail = data.get("detail", "")
        passed = data.get("passed", False)
        if not detail:
            return
        style = "green" if passed else "red"
        self._console.print(f"        [{style}]{rich_escape(detail)}[/{style}]")

    def add_finding(self, text: str) -> None:
        """Print a QA finding permanently. Handles all QA output formats."""
        if not text:
            return

        clean = text.replace("**", "").replace("__", "")
        has_pass = "\u2705" in text or "PASS" in text
        has_fail = "\u274c" in text or "FAIL" in text

        # Detect spec-level findings across formats
        is_spec_header = text.startswith("###") or text.startswith("**Spec")
        is_table_row = text.startswith("|") and (has_pass or has_fail)
        is_table_header = text.startswith("|") and ("Spec" in text[:15] or "Check" in text[:20] or "---" in text[:5])

        with self._lock:
            if is_spec_header or is_table_row:
                self._qa_spec_count += 1
                if has_pass:
                    self._qa_pass_count += 1
            elif has_pass and text.startswith(("**PASS", "PASS")):
                self._qa_pass_count += 1

            # Suppress noise
            if "VERDICT" in text:
                return
            if is_table_header:
                return
            if text.startswith(("- Container", "- Header", "\u25cb Edge")):
                return

        # Render
        if is_spec_header:
            spec_text = clean.lstrip("# ").strip()
            for s in [": \u2705 PASS", ": \u274c FAIL", "\u2705", "\u274c"]:
                spec_text = spec_text.replace(s, "").rstrip(": ").strip()
            icon = "[green]\u2713[/green]" if has_pass else "[red]\u2717[/red]" if has_fail else " "
            self._console.print(f"      {icon} [dim]{rich_escape(spec_text[:68])}[/dim]")

        elif is_table_row:
            # Markdown table: "| 4 | add(a,b) returns sum | test | ✅ PASS |"
            parts = [p.strip() for p in clean.split("|") if p.strip()]
            # Join number + description: "4  add(a,b) returns sum"
            if len(parts) >= 2:
                desc = f"{parts[0]}  {parts[1]}"[:55]
            else:
                desc = parts[0][:55] if parts else clean[:55]
            icon = "[green]\u2713[/green]" if has_pass else "[red]\u2717[/red]"
            self._console.print(f"      {icon} [dim]{rich_escape(desc)}[/dim]")

        elif has_pass and text.startswith(("**PASS", "PASS")):
            detail_text = clean.replace("PASS", "").replace("\u2705", "").lstrip(" \u2014-()").strip()
            if detail_text:
                short = detail_text[:62] + "..." if len(detail_text) > 62 else detail_text
                self._console.print(f"        [green]\u2713[/green] [dim]{rich_escape(short)}[/dim]")

        elif has_fail and text.startswith(("**FAIL", "FAIL")):
            detail_text = clean.replace("FAIL", "").replace("\u274c", "").lstrip(" \u2014-()").strip()
            short = detail_text[:62] + "..." if len(detail_text) > 62 else detail_text
            self._console.print(f"        [red]\u2717[/red] {rich_escape(short)}")

        elif text.startswith("- **("):
            sub = clean.lstrip("- ").strip()
            self._console.print(f"        [dim]{rich_escape(sub[:70])}[/dim]")

    # -- Private --

    def _print_phase_done(self, name: str, time_s: float, detail: str, cost: float) -> None:
        parts = []
        if time_s:
            parts.append(f"{time_s:.0f}s")
        if cost:
            parts.append(f"${cost:.2f}")

        if name == "coding":
            if detail:
                parts.append(detail[:42])
            elif self._coding_files:
                parts.append(f"{len(self._coding_files)} files")
        elif name == "test" and detail:
            m = re.search(r'(\d+) passed', detail)
            parts.append(m.group(0) if m else detail[:35])
        elif name == "qa":
            if self._qa_spec_count:
                t, p = self._qa_spec_count, self._qa_pass_count
                parts.append(f"{t} specs passed" if p == t else f"{p}/{t} specs passed")

        info = "  ".join(parts)
        self._console.print(f"  [green]\u2713[/green] [bold]{name}[/bold]  [dim]{info}[/dim]")

    def _print_phase_fail(self, name: str, time_s: float, error: str, cost: float) -> None:
        parts = []
        if time_s:
            parts.append(f"{time_s:.0f}s")
        if cost:
            parts.append(f"${cost:.2f}")
        if error:
            parts.append(error[:42])
        info = "  ".join(parts)
        self._console.print(f"  [red]\u2717[/red] [bold]{name}[/bold]  [dim]{info}[/dim]")

    def _render_footer(self) -> Text:
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
# Summary helpers
# ---------------------------------------------------------------------------

def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m{secs}s"


def format_cost(cost: float) -> str:
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"
