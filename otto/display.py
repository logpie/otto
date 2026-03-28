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


_PROJECT_DIR_RE = re.compile(r"/(?:private/)?tmp/[^\s/]+/?")  # trailing slash optional

def _strip_temp_prefix(detail: str) -> str:
    """Strip otto temp dir prefixes and project dir paths for cleaner display."""
    detail = _TEMP_DIR_PATTERNS.sub("", detail)
    # Strip /private/tmp/<project>/ and /tmp/<project>/ from commands
    detail = _PROJECT_DIR_RE.sub("", detail)
    return detail


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
# Agent tool event helpers (used by runner + QA agent loops)
# ---------------------------------------------------------------------------


def _tool_use_summary(block) -> str:
    """Return a one-line summary of a tool use for logging."""
    from otto.agent import tool_use_summary
    return tool_use_summary(block)


def build_agent_tool_event(block) -> dict | None:
    """Build a progress event dict for a tool use block.

    Returns None if the tool call should not be displayed.
    """
    def _should_emit_tool(tool_name: str, detail: str) -> bool:
        if tool_name in ("Write", "Edit"):
            return bool(detail)
        if tool_name == "Read":
            return any(ext in detail for ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go"))
        if tool_name == "Bash":
            cmd = detail.strip()
            first_word = cmd.split()[0] if cmd else ""
            if first_word in ("python", "python3", "node") and any(flag in cmd for flag in (" -c ", " -e ")):
                return False
            return first_word in (
                "pytest", "python", "python3", "npx", "npm", "jest",
                "make", "cargo", "go", "ruby", "dotnet", "node",
                "cat", "ls", "find", "grep", "head", "tail",
                "pnpm", "yarn", "uv", "bash", "sh", "tsc",
            )
        return True

    name = block.name
    inputs = block.input or {}
    raw_detail = _tool_use_summary(block)

    if name == "Glob":
        if not _should_emit_tool("Read", raw_detail):
            return None
        return {"name": "Read", "detail": raw_detail[:80]}

    # Show Agent/Skill/TodoWrite dispatches for visibility
    if name == "Agent":
        desc = inputs.get("description") or inputs.get("prompt", "")[:60] or "subagent"
        return {"name": "Agent", "detail": desc[:80]}
    if name in ("Skill", "TodoWrite", "ToolSearch"):
        return {"name": name, "detail": (inputs.get("skill") or inputs.get("query") or "")[:60]}

    if name not in ("Read", "Write", "Edit", "Bash"):
        return None
    if not _should_emit_tool(name, raw_detail):
        return None

    event: dict = {"name": name, "detail": raw_detail[:80]}
    if name == "Edit":
        old = inputs.get("old_string", "")
        new = inputs.get("new_string", "")
        if old or new:
            event["old_lines"] = old.splitlines()[:4]
            event["new_lines"] = new.splitlines()[:4]
            event["old_total"] = old.count("\n") + 1 if old else 0
            event["new_total"] = new.count("\n") + 1 if new else 0
    elif name == "Write":
        content = inputs.get("content", "")
        if content:
            event["preview_lines"] = content.splitlines()[:3]
            event["total_lines"] = content.count("\n") + 1
    return event


# ---------------------------------------------------------------------------
# Internal file patterns to suppress
# ---------------------------------------------------------------------------

_INTERNAL_PATTERNS = {"otto_arch/", "task-notes/"}

PHASE_ORDER = ["prepare", "spec_gen", "coding", "test", "qa", "merge"]


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
        self._active_phases: set[str] = set()  # v4.5: track parallel phases
        self._phase_details: dict[str, str] = {}  # v4.5: detail per phase
        self._current_cost: float = 0.0
        self._qa_spec_count: int = 0
        self._qa_pass_count: int = 0
        self._qa_proof_count: int = 0
        self._qa_proof_coverage: str = ""
        self._qa_summary_authoritative: bool = False
        self._coding_files: list[str] = []
        self._coding_start_detail: str = ""
        self._lines_added: int = 0
        self._lines_removed: int = 0
        self._last_tool_key: str = ""
        self._last_tool_type: str = ""
        self._last_qa_label: str = ""
        self._read_count: int = 0
        self._spec_gen_announced: bool = False
        self._spec_items_buffer: list[str] = []
        self._edit_streak: int = 0

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
                self._active_phases.add(name)
                self._current_phase = name
                self._phase_start = time.monotonic()
                self._current_cost = 0.0
                if detail:
                    self._phase_details[name] = detail
                self._last_tool_key = ""
                self._last_tool_type = ""
                self._last_qa_label = ""
                self._read_count = 0
                if name == "qa":
                    self._qa_spec_count = 0
                    self._qa_pass_count = 0
                    self._qa_summary_authoritative = False
                if name == "coding":
                    self._coding_start_detail = f"({detail})" if detail else ""
                    self._coding_files.clear()
                    self._lines_added = 0
                    self._lines_removed = 0

            elif status == "done":
                self._active_phases.discard(name)
                self._print_phase_done(name, time_s, detail, cost)
                if name == "coding":
                    self._coding_start_detail = ""
                if name == self._current_phase:
                    # Fall back to another active phase for footer
                    self._current_phase = next(iter(self._active_phases), None)
                self._last_qa_label = ""

            elif status == "fail":
                self._active_phases.discard(name)
                self._print_phase_fail(name, time_s, error, cost)
                if name == "coding":
                    self._coding_start_detail = ""
                if name == self._current_phase:
                    self._current_phase = next(iter(self._active_phases), None)
                self._last_qa_label = ""

            if cost:
                self._current_cost = cost

        # Print phase start headers for v4.5 observability
        if status == "running":
            timestamp = time.strftime("%H:%M:%S")
            if name == "spec_gen":
                # Only print spec_gen start once (not on "awaiting" re-emit)
                if detail and "awaiting" in detail:
                    self._console.print(
                        f"  [dim]{timestamp}  ⧗ awaiting specs before QA...[/dim]"
                    )
                elif not self._spec_gen_announced:
                    self._spec_gen_announced = True
                    self._console.print(
                        f"  [dim]{timestamp}  ● spec gen[/dim]  [dim](parallel with coding)[/dim]"
                    )
            elif name == "coding":
                # Print a permanent coding start line (not just footer)
                label = detail if detail else ""
                parallel = "spec_gen" in self._active_phases
                suffix = "  [dim]· spec gen[/dim]" if parallel else ""
                self._console.print()
                self._console.print(
                    f"  [cyan]{timestamp}  ● coding[/cyan]  [dim]({label}){suffix}[/dim]"
                )
            elif name == "qa":
                tier_labels = {
                    "tier 0": "skip - all specs have tests",
                    "tier 1": "targeted - checking spec gaps",
                    "tier 2": "full - adversarial testing",
                }
                tier_label = detail if detail else ""
                human_label = tier_labels.get(tier_label, tier_label)
                if tier_label and " — " in tier_label and human_label == tier_label:
                    prefix, suffix = tier_label.split(" — ", 1)
                    human_prefix = tier_labels.get(prefix, prefix)
                    human_label = f"{human_prefix} — {suffix}"
                self._console.print()
                if human_label:
                    self._console.print(
                        f"  [cyan]{timestamp}  ● qa[/cyan]  [dim]({human_label})[/dim]"
                    )
                else:
                    self._console.print(f"  [cyan]{timestamp}  ● qa[/cyan]")
            elif name == "test":
                pass  # test phase shown inline via done/fail, no start header needed
            elif name not in ("prepare", "merge", "spec_gen"):
                self._console.print()

    def set_qa_summary(self, total: int, passed: int, failed: int = 0,
                       proof_count: int = 0, proof_coverage: str = "") -> None:
        """Set authoritative QA counts from the runner."""
        with self._lock:
            total = max(0, int(total))
            passed = max(0, min(int(passed), total))
            failed = max(0, min(int(failed), total - passed))
            self._qa_spec_count = total
            self._qa_pass_count = passed
            self._qa_proof_count = max(0, int(proof_count))
            self._qa_proof_coverage = str(proof_coverage or "")
            self._qa_summary_authoritative = True

    def add_attempt_boundary(self, attempt: int, reason: str = "") -> None:
        """Print an attempt boundary for retries."""
        if attempt <= 1 and not reason:
            return
        timestamp = time.strftime("%H:%M:%S")
        if reason:
            self._console.print(
                f"\n  [bold]{timestamp}  ━━ Attempt {attempt}[/bold]  [dim]({rich_escape(reason)})[/dim]"
            )
        else:
            self._console.print(f"\n  [bold]{timestamp}  ━━ Attempt {attempt}[/bold]")

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

        # Strip temp dir prefixes from all tool details (project paths, otto work dirs)
        raw_detail = _strip_temp_prefix(raw_detail)

        # Skip internal files (check BEFORE path shortening)
        if any(p in raw_detail for p in _INTERNAL_PATTERNS):
            return

        # Shorten paths for file operations, not for Bash commands
        # _shorten_path strips path prefixes which corrupts command text
        if name not in ("Bash",):
            detail = _shorten_path(raw_detail)
        else:
            detail = raw_detail

        # Also skip after shortening (catches relative paths)
        if any(p in detail for p in _INTERNAL_PATTERNS):
            return

        # Deduplicate silently
        tool_key = f"{name}:{detail}"

        with self._lock:
            phase = self._current_phase
            if phase != "coding" or name != "Edit":
                self._flush_edit_streak_locked()
            if phase == "coding" and name in ("Write", "Edit"):
                if detail and detail not in self._coding_files:
                    self._coding_files.append(detail)
                # Track line counts from event data
                if data:
                    if name == "Write":
                        self._lines_added += data.get("total_lines", 0)
                    elif name == "Edit":
                        self._lines_added += data.get("new_total", 0)
                        self._lines_removed += data.get("old_total", 0)
            if tool_key == self._last_tool_key:
                return
            self._last_tool_key = tool_key

        # Dispatch to phase-specific renderer
        if phase == "coding":
            self._render_tool_coding(name, detail, data)
        elif phase == "qa":
            self._render_tool_qa(name, detail)
        else:
            self._render_tool_default(name, detail, data)

    def _flush_edit_streak_locked(self) -> None:
        """Flush a collapsed edit streak. Call with self._lock held."""
        if self._edit_streak > 2:
            self._console.print(
                f"      [dim]... edited {self._edit_streak} files (similar changes)[/dim]"
            )
        self._edit_streak = 0

    def _collapse_reads(self, name: str, threshold: int, flush_label: str) -> bool | None:
        """Collapse consecutive Read/Glob/Grep calls under a threshold.

        Call with self._lock held. Returns:
          True  — suppress this tool (over threshold)
          False — show this tool normally (at or under threshold)
          None  — not a read tool; flushes collapsed reads if any pending
        """
        if name in ("Read", "Glob", "Grep"):
            self._read_count += 1
            return self._read_count > threshold
        if self._read_count > threshold:
            self._console.print(f"      [dim]... {flush_label} {self._read_count} files[/dim]")
            self._read_count = 0
        return None

    def _render_tool_coding(self, name: str, detail: str, data: dict | None) -> None:
        """Render a tool call during the coding phase."""
        with self._lock:
            # Collapse consecutive reads (show first 3, then batch)
            collapse = self._collapse_reads(name, threshold=3, flush_label="explored")
            if collapse is True:
                return  # suppress — will flush on next Write/Edit/Bash

            # Collapse consecutive similar edits (e.g., adding same field to 20 test files)
            if name == "Edit":
                self._edit_streak += 1
                if self._edit_streak <= 2:
                    pass  # show first 2 normally
                else:
                    return  # suppress — will flush on next non-Edit
            else:
                self._edit_streak = 0

        self._render_tool_default(name, detail, data)

    def _render_tool_qa(self, name: str, detail: str) -> None:
        """Render a tool call during the QA phase."""
        label = self._qa_tool_label(name, detail)
        if not label:
            return  # suppressed (temp file ops, etc.)
        with self._lock:
            # Collapse consecutive reads (show first 2, then batch)
            collapse = self._collapse_reads(name, threshold=2, flush_label="read")
            if collapse is True:
                return

            if label == self._last_qa_label:
                return
            self._last_qa_label = label

        # Style QA activity by type — key actions bold, reads dim, narration dim
        if label.startswith(("Testing:", "Curl:")):
            self._console.print(f"      [bold]{label}[/bold]")
        elif label.startswith("Browser:"):
            self._console.print(f"      [bold magenta]{label}[/bold magenta]")
        elif label.startswith(("Reading", "Writing verdict")):
            self._console.print(f"      [dim]{label}[/dim]")
        else:
            # Other tool actions (building, starting server, etc.)
            self._console.print(f"      {label}")

    def _render_tool_default(self, name: str, detail: str, data: dict | None) -> None:
        """Render a tool call with per-type styling + inline diff/preview."""
        # Truncate Bash commands at first newline (multi-line node -e commands)
        if name == "Bash":
            first_line = detail.split("\n")[0].strip()
            short = rich_escape(first_line[:68])
        else:
            short = rich_escape(_shorten_path(detail)[:68])

        # Breathing room on tool type change (Read->Bash, Write->Read, etc.)
        with self._lock:
            prev_type = self._last_tool_type
            self._last_tool_type = name
        if prev_type and prev_type != name:
            self._console.print()

        # Per-tool-type styling — matches theme.py semantics
        if name in ("Write", "Edit"):
            # Key actions: green bullet + name, visible detail
            self._console.print(f"      [bold green]\u25cf {name}[/bold green]  {short}")
        elif name == "Bash":
            # Commands: cyan bullet + name, visible detail
            self._console.print(f"      [bold cyan]\u25cf {name}[/bold cyan]  {short}")
        elif name in ("Agent", "Skill", "TodoWrite", "ToolSearch"):
            # CC overhead — visible so user can see what's happening
            self._console.print(f"      [bold]\u25cf {name}[/bold]  {short}")
        else:
            # Read/Glob/Grep: dim bullet + name + detail (background exploration)
            self._console.print(f"      [dim]\u25cf {name}  {short}[/dim]")

        # Inline diff/preview below tool call (like CC)
        if data:
            self._render_inline_diff(name, data)

    def _render_inline_diff(self, name: str, data: dict) -> None:
        """Render inline diff/preview below a tool call (like CC)."""
        old_lines = data.get("old_lines", [])
        new_lines = data.get("new_lines", [])
        old_total = data.get("old_total", 0)
        new_total = data.get("new_total", 0)
        preview = data.get("preview_lines", [])
        total_lines = data.get("total_lines", 0)

        if name == "Edit" and (old_lines or new_lines):
            # For large edits (>20 lines), just show summary
            if old_total + new_total > 20:
                self._console.print(
                    f"        [dim][red]-{old_total}[/red] [green]+{new_total}[/green] lines[/dim]"
                )
            else:
                # Skip identical leading lines — show where change starts
                skip = 0
                while (skip < len(old_lines) and skip < len(new_lines)
                       and old_lines[skip] == new_lines[skip]):
                    skip += 1

                # If ALL preview lines are identical, change is beyond preview
                if skip >= len(old_lines) and skip >= len(new_lines):
                    self._console.print(
                        f"        [dim][red]-{old_total}[/red] [green]+{new_total}[/green] lines[/dim]"
                    )
                else:
                    if skip > 0:
                        # Show one context line before the change
                        self._console.print(f"        [dim]  {rich_escape(old_lines[skip - 1])}[/dim]")
                        if skip > 1:
                            self._console.print(f"        [dim]  ...{skip - 1} more[/dim]")
                    for ol in old_lines[skip:]:
                        self._console.print(f"        [red]- {rich_escape(ol)}[/red]")
                    remaining_old = old_total - len(old_lines)
                    if remaining_old > 0:
                        self._console.print(f"        [dim]  ...{remaining_old} more[/dim]")
                    for nl in new_lines[skip:]:
                        self._console.print(f"        [green]+ {rich_escape(nl)}[/green]")
                    remaining_new = new_total - len(new_lines)
                    if remaining_new > 0:
                        self._console.print(f"        [dim]  ...{remaining_new} more[/dim]")

        elif name == "Write" and preview:
            for pl in preview:
                self._console.print(f"        [green]+ {rich_escape(pl)}[/green]")
            if total_lines > len(preview):
                self._console.print(f"        [dim]  ...{total_lines - len(preview)} more lines[/dim]")

    def _qa_tool_label(self, name: str, detail: str) -> str:
        """Build a concise, informative QA activity label from tool call data."""
        # Suppress temp file operations (verdict file writes, etc.)
        if detail and ("otto_qa_" in detail or "/var/folders/" in detail
                       or "/tmp/otto_" in detail):
            if name == "Write":
                return "Writing verdict"
            if name == "Bash" and ("cat " in detail or "cat>" in detail):
                return ""  # suppress cat to temp file
            # Let Read through but clean up the path
        short_detail = _shorten_path(detail)[:60] if detail else ""

        if name in ("Read", "Glob", "Grep"):
            return f"Reading {short_detail}" if short_detail else "Reading code"

        if name.startswith("mcp__chrome-devtools__"):
            action = name.replace("mcp__chrome-devtools__", "")
            if short_detail:
                return f"Browser: {action} {short_detail}"
            return f"Browser: {action}"

        if name == "Write":
            return f"Writing {short_detail}" if short_detail else "Writing file"

        if name == "Bash":
            cmd = detail.strip()
            first_line = cmd.split("\n")[0][:65]
            # Extract the meaningful part of the command
            lower = cmd.lower()
            if any(m in lower for m in ("jest", "pytest", "vitest", "cargo test", "go test")):
                # Show which test file or pattern
                return f"Testing: {first_line}"
            if "curl " in lower:
                return f"Curl: {first_line}"
            if "tsc" in lower or "--noemit" in lower:
                return "Checking types"
            if any(m in lower for m in ("next build", "npm run build", "cargo build")):
                return "Building project"
            if any(m in lower for m in ("node -e", "python -c", "python3 -c")):
                return f"Running: {first_line}"
            if any(m in lower for m in ("next dev", "npm start", "npm run dev")):
                return "Starting dev server"
            if "kill " in lower or "pkill " in lower:
                return "Stopping server"
            return first_line

        return f"{name} {short_detail}".strip()

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

    @staticmethod
    def _get_check_icon(passed: bool) -> str:
        """Return a Rich-markup icon for a pass/fail check."""
        return "[green]\u2713[/green]" if passed else "[red]\u2717[/red]"

    @staticmethod
    def _clean_finding_text(text: str, *remove_words: str, strip_chars: str = ": \u2014-*()") -> str:
        """Strip result keywords and leading punctuation from finding text."""
        for word in remove_words:
            text = text.replace(word, "")
        return text.lstrip(strip_chars).strip()

    def add_finding(self, text: str) -> None:
        """Print a QA finding permanently. Handles all QA output formats."""
        if not text:
            return

        clean = text.replace("**", "").replace("__", "")
        has_pass = "\u2705" in text or "PASS" in text
        has_fail = "\u274c" in text or "FAIL" in text

        # Detect spec-level findings across formats
        is_spec_header = (
            text.startswith("###")
            or text.startswith("**Spec")
            or (clean.startswith("Spec ") and clean[5:6].isdigit())  # "Spec 1: ..."
        )
        is_table_row = text.startswith("|") and (has_pass or has_fail)
        is_table_header = text.startswith("|") and ("Spec" in text[:15] or "Check" in text[:20] or "---" in text[:5])
        # Detect "✓ N." or "✗ N." pattern (e.g. "✓ 4. LIFO order  Pushed...")
        is_numbered_check = (
            len(clean) > 2
            and clean[0] in ("\u2713", "\u2717", "\u2705", "\u274c")
            and clean[1:].lstrip().split(".")[0].strip().isdigit()
        )

        # Detect result lines: "**RESULT**: ✅ PASS", "**PASS**", "PASS —"
        is_result_line = (
            text.startswith(("**PASS", "PASS", "**RESULT"))
            or (text.startswith("- \u2705") and "PASS" not in text[:5])  # "- ✅ Component renders"
        )

        # Detect standalone pass/fail checks: "✓ description" without a number
        is_standalone_check = (
            len(clean) > 2
            and clean[0] in ("\u2713", "\u2717", "\u2705", "\u274c")
            and not is_numbered_check  # not "✓ 4. desc"
            and not is_spec_header     # not "Spec N —"
        )

        with self._lock:
            if not self._qa_summary_authoritative:
                # Fallback counting when the runner does not emit qa_summary.
                if is_table_row or is_numbered_check or is_standalone_check:
                    self._qa_spec_count += 1
                    check_pass = has_pass or clean[0] in ("\u2713", "\u2705")
                    if check_pass:
                        self._qa_pass_count += 1
                elif is_result_line and has_pass:
                    self._qa_spec_count += 1
                    self._qa_pass_count += 1
                elif is_result_line and has_fail:
                    self._qa_spec_count += 1
                # Spec headers (Spec N — Title) are just labels, don't count

            # Suppress noise
            if "VERDICT" in text:
                return
            if is_table_header:
                return
            if text.startswith(("- Container", "- Header", "\u25cb Edge")):
                return
            if "Minor Observation" in text or "not spec violation" in text.lower():
                return

        # Render — spec findings are KEY info, not dim
        if is_spec_header:
            spec_text = clean.lstrip("# ").strip()
            for s in [": \u2705 PASS", ": \u274c FAIL", "\u2705", "\u274c"]:
                spec_text = spec_text.replace(s, "").rstrip(": ").strip()
            icon = self._get_check_icon(has_pass) if (has_pass or has_fail) else " "
            self._console.print(f"      {icon} {rich_escape(spec_text[:68])}")

        elif is_table_row:
            parts = [p.strip() for p in clean.split("|") if p.strip()]
            if len(parts) >= 2:
                desc = f"{parts[0]}  {parts[1]}"[:55]
            else:
                desc = parts[0][:55] if parts else clean[:55]
            self._console.print(f"      {self._get_check_icon(has_pass)} {rich_escape(desc)}")

        elif is_numbered_check:
            # "✓ 4. LIFO order  Pushed..." or "✗ 2. Edge case..."
            check_pass = clean[0] in ("\u2713", "\u2705")
            desc = clean[1:].lstrip().lstrip("0123456789").lstrip(".").strip()[:62]
            self._console.print(f"      {self._get_check_icon(check_pass)} {rich_escape(desc)}")

        elif is_result_line and has_pass:
            # "**RESULT**: ✅ PASS — detail" or "**PASS** — detail"
            detail_text = self._clean_finding_text(clean, "RESULT", "PASS", "\u2705")
            if detail_text:
                short = detail_text[:60] + "..." if len(detail_text) > 60 else detail_text
                self._console.print(f"        {self._get_check_icon(True)} [dim]{rich_escape(short)}[/dim]")

        elif is_result_line and has_fail:
            detail_text = self._clean_finding_text(clean, "RESULT", "FAIL", "\u274c")
            short = detail_text[:60] + "..." if len(detail_text) > 60 else detail_text
            self._console.print(f"        {self._get_check_icon(False)} {rich_escape(short)}")

        elif has_fail and text.startswith(("**FAIL", "FAIL")):
            detail_text = self._clean_finding_text(clean, "FAIL", "\u274c", strip_chars=" \u2014-()")
            short = detail_text[:62] + "..." if len(detail_text) > 62 else detail_text
            self._console.print(f"        {self._get_check_icon(False)} {rich_escape(short)}")

        elif text.startswith("- **("):
            sub = clean.lstrip("- ").strip()
            self._console.print(f"        [dim]{rich_escape(sub[:70])}[/dim]")

    def add_spec_item(self, text: str) -> None:
        """Buffer spec items during a live run; print directly in tests/helpers."""
        if not text:
            return
        with self._lock:
            if self._live is None and not self._active_phases:
                print_now = True
            else:
                self._spec_items_buffer.append(text)
                print_now = False
        if print_now:
            self._print_spec_item(text)

    def _print_spec_item(self, text: str) -> None:
        """Print a spec item with binding-level styling."""
        t = text[:80]
        if "[must" in t:
            has_visual = "\u25c8" in t
            stripped = t
            for removal in ("[must ◈]", "[must]"):
                stripped = stripped.replace(removal, "", 1)
            rest = rich_escape(stripped.strip())
            if has_visual:
                tag = "[cyan]\\[must[/cyan] [magenta]◈[/magenta][cyan]][/cyan]"
            else:
                tag = "[cyan]\\[must][/cyan]"
            self._console.print(f"      {tag} {rest}")
        else:
            self._console.print(f"      [dim]{rich_escape(t)}[/dim]")

    def flush_spec_summary(self) -> None:
        """Print a collapsed summary of buffered spec items."""
        with self._lock:
            items = self._spec_items_buffer
            self._spec_items_buffer = []
        if not items:
            return
        must_items = [t for t in items if "[must" in t]
        preview = must_items[:3] or items[:3]
        for item in preview:
            self._print_spec_item(item)
        remaining = len(items) - len(preview)
        if remaining > 0:
            self._console.print(f"      [dim]... +{remaining} more[/dim]")

    def add_qa_item_result(self, text: str, passed: bool | None = True, evidence: str = "") -> None:
        """Print a QA per-item result."""
        if not text:
            return
        # Strip existing ✓/✗ prefix if present — we add styled ones
        clean = text.lstrip()
        if clean and clean[0] in ("\u2713", "\u2717", "\u2705", "\u274c"):
            clean = clean[1:].lstrip()

        # Color the [must]/[should] tag for scannability
        # ◈ marks non-verifiable (visual/subjective) items
        has_visual = "\u25c8" in clean
        # Strip tag + marker from text to get the description
        stripped = clean
        for removal in ("[must ◈]", "[must]", "[should ◈]", "[should]"):
            stripped = stripped.replace(removal, "", 1)
        rest = rich_escape(stripped.strip())

        if "[must" in clean:
            if has_visual:
                tag = "[cyan]\\[must[/cyan] [magenta]◈[/magenta][cyan]][/cyan]"
            else:
                tag = "[cyan]\\[must][/cyan]"
            display_text = f"{tag} {rest}"
        elif "[should" in clean:
            if has_visual:
                tag = "[dim]\\[should[/dim] [magenta]◈[/magenta][dim]][/dim]"
            else:
                tag = "[dim]\\[should][/dim]"
            display_text = f"{tag} {rest}"
        else:
            display_text = rich_escape(clean)

        if passed is True:
            self._console.print(f"      [green]\u2713[/green] {display_text}")
            if evidence:
                self._console.print(f"        [dim]{rich_escape(evidence)}[/dim]")
            return
        if passed is None:
            bullet = "[dim]\u00b7[/dim]"
            self._console.print(f"      {bullet} {display_text}")
            if clean.startswith("[should") and evidence:
                self._console.print(f"        [dim]{rich_escape(evidence)}[/dim]")
            return
        # Failed items: red with evidence
        self._console.print(f"      [red]\u2717[/red] [red]{display_text}[/red]")
        if evidence:
            self._console.print(f"        [dim]{rich_escape(evidence)}[/dim]")

    # -- Private --

    def _print_phase_done(self, name: str, time_s: float, detail: str, cost: float) -> None:
        # Suppress trivial phases (0s, no info)
        if name in ("prepare", "merge") and time_s < 1 and not detail:
            return

        timestamp = time.strftime("%H:%M:%S")
        phase_label = name.replace("_", " ")

        # Build info with targeted colors — NOT wrapped in [dim] (kills nested styles)
        meta = []  # dim metadata (time, cost)
        highlight = []  # colored highlights (line counts, test results)

        if time_s >= 1:
            meta.append(f"{time_s:.0f}s")
        if cost:
            meta.append(f"${cost:.2f}")

        if name == "prepare" and detail:
            highlight.append(detail)
        elif name == "coding":
            if self._coding_files:
                highlight.append(f"{len(self._coding_files)} files")
                if self._lines_added:
                    highlight.append(f"[green]+{self._lines_added}[/green]")
                if self._lines_removed:
                    highlight.append(f"[red]-{self._lines_removed}[/red]")
        elif name == "spec_gen":
            if detail:
                highlight.append(detail)
        elif name == "test" and detail:
            m = re.search(r'(\d+) passed', detail)
            if m:
                highlight.append(f"[green]{m.group(0)}[/green]")
            else:
                highlight.append(detail[:35])
        elif name == "qa":
            if self._qa_spec_count:
                t, p = self._qa_spec_count, self._qa_pass_count
                if p == t:
                    highlight.append(f"[green]{t} specs passed[/green]")
                else:
                    highlight.append(f"{p}/{t} specs passed")
            if self._qa_proof_coverage:
                highlight.append(f"{self._qa_proof_coverage} proved")
            elif self._qa_proof_count:
                highlight.append(f"{self._qa_proof_count} proofs saved")
            tier_detail = self._phase_details.get("qa", "")
            if tier_detail:
                meta.append(tier_detail)
        elif name == "candidate":
            return

        meta_str = f"  [dim]{'  '.join(meta)}[/dim]" if meta else ""
        highlight_str = f"  {'  '.join(highlight)}" if highlight else ""
        self._console.print(
            f"  [green]{timestamp}  \u2713 {phase_label}[/green]{meta_str}{highlight_str}"
        )

    def _print_phase_fail(self, name: str, time_s: float, error: str, cost: float) -> None:
        timestamp = time.strftime("%H:%M:%S")
        phase_label = name.replace("_", " ")
        parts = []
        if time_s:
            parts.append(f"{time_s:.0f}s")
        if cost:
            parts.append(f"${cost:.2f}")
        if error:
            parts.append(error[:80])
        info = "  ".join(parts)
        self._console.print(f"  [red]{timestamp}  \u2717[/red] [bold]{phase_label}[/bold]  [dim]{info}[/dim]")

    _SPINNER_FRAMES = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"

    def _render_footer(self) -> Text:
        with self._lock:
            if not self._active_phases:
                return Text("")
            elapsed = time.monotonic() - self._phase_start
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)
            time_str = f"{mins}:{secs:02d}" if mins else f"{secs}s"
            cost_str = f"  ${self._current_cost:.2f}" if self._current_cost else ""
            # Animated spinner
            frame_idx = int(elapsed * 5) % len(self._SPINNER_FRAMES)
            spinner = self._SPINNER_FRAMES[frame_idx]
            line = Text()
            line.append(f"  {spinner} ", style="cyan")
            # Show all active phases
            phase_labels = []
            seen_phases = set()
            for p in PHASE_ORDER:
                if p in self._active_phases:
                    label = p.replace("_", " ")
                    if p == "coding" and self._coding_start_detail:
                        label = f"{label} {self._coding_start_detail}"
                    phase_labels.append(label)
                    seen_phases.add(p)
            # Show non-ordered phases too
            for p in self._active_phases:
                if p not in seen_phases:
                    label = p.replace("_", " ")
                    if p == "coding" and self._coding_start_detail:
                        label = f"{label} {self._coding_start_detail}"
                    phase_labels.append(label)
            line.append(" · ".join(phase_labels), style="bold cyan")
            line.append(f"  {time_str}{cost_str}", style="dim")
            if "coding" in self._active_phases and self._coding_files:
                file_count = len(self._coding_files)
                line_info = ""
                if self._lines_added:
                    line_info += f" +{self._lines_added}"
                line.append(f"  {file_count} files{line_info}", style="dim")
            return line


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


# ---------------------------------------------------------------------------
# Status table (used by `otto status` and `otto status -w`)
# ---------------------------------------------------------------------------

def _relative_time(iso_timestamp: str) -> str:
    """Convert ISO timestamp to relative time string."""
    from datetime import datetime, timezone
    try:
        # Normalize: replace Z suffix and ensure timezone awareness
        ts = iso_timestamp.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return "just now"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}min ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}hr ago"
        days = hours // 24
        if days == 1:
            return "yesterday"
        return f"{days} days ago"
    except (ValueError, TypeError):
        return ""


def _read_proof_coverage(task_key: str) -> str:
    """Read proof coverage from qa-proofs/proof-report.md on disk.

    Returns a string like "4/5 proved" or "" if not available.
    """
    from pathlib import Path
    proof_report = Path.cwd() / "otto_logs" / task_key / "qa-proofs" / "proof-report.md"
    if not proof_report.exists():
        return ""
    try:
        content = proof_report.read_text()
        # Look for "Proof coverage: N/M" pattern
        m = re.search(r"[Pp]roof [Cc]overage:\s*(\d+/\d+)", content)
        if m:
            return f"{m.group(1)} proved"
    except OSError:
        pass
    return ""


def build_status_table(tasks: list[dict], show_phase: bool = False):
    """Build a Rich renderable — card-style task list with summary."""
    from pathlib import Path
    from rich.console import Group

    _STATUS_ICONS = {
        "passed": "[green]\u2713[/green]",
        "failed": "[red]\u2717[/red]",
        "blocked": "[yellow]\u26a0[/yellow]",
        "conflict": "[yellow]\u26a0[/yellow]",
        "merge_failed": "[red]\u2717[/red]",
        "running": "[cyan]\u25cf[/cyan]",
        "pending": "[dim]\u25cb[/dim]",
        "verified": "[blue]\u25c9[/blue]",
        "merged": "[blue]\u25c9[/blue]",
        "merge_pending": "[blue]\u25c9[/blue]",
    }

    _SEP = " \u00b7 "  # middle dot separator for detail lines

    lines: list[str] = []
    for t in tasks:
        status_str = t.get("status", "?")
        task_id = t.get("id", "?")
        prompt_text = _truncate_at_word(t["prompt"], 80)
        icon = _STATUS_ICONS.get(status_str, "[dim]?[/dim]")
        spec_count = len(t.get("spec", []))
        cost = t.get("cost_usd", 0.0)
        dur = t.get("duration_s", 0.0)
        attempts = t.get("attempts", 0)
        task_key = t.get("key", "")

        # Line 1: icon + id + prompt
        if status_str in ("passed",):
            lines.append(f"  {icon} [bold]#{task_id}[/bold]  {rich_escape(prompt_text)}")
        elif status_str in ("failed", "blocked", "conflict", "merge_failed"):
            lines.append(f"  {icon} [bold]#{task_id}[/bold]  {rich_escape(prompt_text)}")
        elif status_str in ("running", "verified", "merged", "merge_pending"):
            lines.append(f"  {icon} [bold]#{task_id}[/bold]  {rich_escape(prompt_text)}")
        else:
            lines.append(f"  {icon} [dim]#{task_id}[/dim]  [dim]{rich_escape(prompt_text)}[/dim]")

        # Line 2: detail line (status-dependent, colorized key info)
        detail_parts: list[str] = []
        if status_str in ("passed", "failed", "blocked", "conflict", "merge_failed"):
            # Relative time from completed_at
            completed_at = t.get("completed_at", "")
            rel = _relative_time(completed_at) if completed_at else ""
            if status_str == "passed":
                status_label = f"[green]passed[/green]"
            elif status_str == "failed":
                status_label = f"[red]failed[/red]"
            elif status_str == "conflict":
                status_label = f"[yellow]conflict[/yellow]"
            elif status_str == "merge_failed":
                status_label = f"[red]merge failed[/red]"
            else:
                status_label = f"[yellow]{status_str}[/yellow]"
            detail_parts.append(f"{status_label} {rel}" if rel else status_label)
            if attempts and attempts > 1:
                detail_parts.append(f"[yellow]{attempts} attempts[/yellow]")
            if cost:
                detail_parts.append(f"[dim]${cost:.2f}[/dim]")
            if dur:
                detail_parts.append(f"[dim]{format_duration(dur)}[/dim]")
            if status_str == "passed":
                if spec_count:
                    detail_parts.append(f"[dim]{spec_count} specs[/dim]")
                # Check proof coverage from disk — show as "N must proved"
                proof_cov = _read_proof_coverage(task_key) if task_key else ""
                if proof_cov:
                    detail_parts.append(f"[green]{proof_cov.replace('proved', 'must proved')}[/green]")
            lines.append(f"    {_SEP.join(detail_parts)}")
        elif status_str == "running":
            detail_parts.append("running")
            if dur:
                detail_parts.append(f"{format_duration(dur)} elapsed")
            if cost:
                detail_parts.append(f"${cost:.2f}")
            lines.append(f"    [dim]{_SEP.join(detail_parts)}[/dim]")
        elif status_str == "verified":
            detail_parts.append("[blue]verified[/blue]")
            if cost:
                detail_parts.append(f"[dim]${cost:.2f}[/dim]")
            if dur:
                detail_parts.append(f"[dim]{format_duration(dur)}[/dim]")
            if spec_count:
                detail_parts.append(f"[dim]{spec_count} specs[/dim]")
            lines.append(f"    {_SEP.join(detail_parts)}")
        elif status_str == "merged":
            detail_parts.append("[blue]merged (QA pending)[/blue]")
            if cost:
                detail_parts.append(f"[dim]${cost:.2f}[/dim]")
            if dur:
                detail_parts.append(f"[dim]{format_duration(dur)}[/dim]")
            if spec_count:
                detail_parts.append(f"[dim]{spec_count} specs[/dim]")
            lines.append(f"    {_SEP.join(detail_parts)}")
        elif status_str == "merge_pending":
            detail_parts.append("[blue]merging...[/blue]")
            if cost:
                detail_parts.append(f"[dim]${cost:.2f}[/dim]")
            lines.append(f"    {_SEP.join(detail_parts)}")
        else:
            # pending
            detail_parts.append("pending")
            if spec_count:
                detail_parts.append(f"{spec_count} specs")
            lines.append(f"    [dim]{_SEP.join(detail_parts)}[/dim]")

        # Line 3: error line for failed/blocked/merge_failed
        if status_str in ("failed", "blocked", "conflict", "merge_failed") and t.get("error"):
            error_text = t["error"].splitlines()[0][:70] if t.get("error") else ""
            color = "yellow" if status_str in ("blocked", "conflict") else "red"
            lines.append(f"    [{color}]\u21b3 {rich_escape(error_text)}[/{color}]")

    # Summary line
    counts: dict[str, int] = {}
    total_cost = 0.0
    total_dur = 0.0
    for t in tasks:
        s = t.get("status", "?")
        counts[s] = counts.get(s, 0) + 1
        total_cost += t.get("cost_usd", 0.0)
        total_dur += t.get("duration_s", 0.0)

    parts = []
    if counts.get("passed"):
        parts.append(f"[green]{counts['passed']} passed[/green]")
    if counts.get("verified"):
        parts.append(f"[blue]{counts['verified']} verified[/blue]")
    if counts.get("merged"):
        parts.append(f"[blue]{counts['merged']} merged[/blue]")
    if counts.get("merge_pending"):
        parts.append(f"[blue]{counts['merge_pending']} merging[/blue]")
    if counts.get("failed"):
        parts.append(f"[red]{counts['failed']} failed[/red]")
    if counts.get("merge_failed"):
        parts.append(f"[red]{counts['merge_failed']} merge failed[/red]")
    if counts.get("blocked"):
        parts.append(f"[yellow]{counts['blocked']} blocked[/yellow]")
    if counts.get("conflict"):
        parts.append(f"[yellow]{counts['conflict']} conflict[/yellow]")
    if counts.get("pending"):
        parts.append(f"[dim]{counts['pending']} pending[/dim]")
    if counts.get("running"):
        parts.append(f"[cyan]{counts['running']} running[/cyan]")
    summary = ", ".join(parts)
    extras = []
    if total_cost > 0:
        extras.append(f"${total_cost:.2f}")
    if total_dur > 0:
        extras.append(format_duration(total_dur))
    if extras:
        summary += f" [dim]\u2014 {', '.join(extras)}[/dim]"

    lines.append("")
    lines.append(f"  {summary}")

    markup = "\n".join(lines)
    renderables = [Text.from_markup(markup)]

    if show_phase:
        live_lines = _build_live_phase_lines()
        if live_lines:
            renderables.append(Text(""))
            renderables.extend(live_lines)

    return Group(*renderables)


def _build_live_phase_lines() -> list:
    """Build Rich Text lines showing live progress from per-task live-state.json files.

    Scans otto_logs/*/live-state.json to support multiple concurrent tasks.
    """
    import json
    from pathlib import Path

    lines = []
    otto_logs = Path.cwd() / "otto_logs"
    if not otto_logs.exists():
        return lines

    # Collect all live-state files: per-task otto_logs/{key}/live-state.json
    live_files = sorted(otto_logs.glob("*/live-state.json"))

    for live_file in live_files:
        try:
            live = json.loads(live_file.read_text())
            _render_live_state(live, lines)
        except (json.JSONDecodeError, OSError):
            pass

    return lines


def _render_live_state(live: dict, lines: list) -> None:
    """Render a single task's live-state into Rich Text lines."""
    tid = live.get("task_id", "?")
    prompt = live.get("prompt", "")[:50]
    elapsed = live.get("elapsed_s", 0)
    cost = live.get("cost_usd", 0)
    elapsed_str = format_duration(elapsed)
    lines.append(Text.from_markup(f"  [info]\u25b8 Task #{tid}:[/info] {rich_escape(prompt)}"))
    phases = live.get("phases", {})
    _icons = {
        "done": "[success]\u2713[/success]",
        "fail": "[error]\u2717[/error]",
        "running": "[info]\u25cf[/info]",
        "pending": "[dim]\u25e6[/dim]",
    }
    for pname in ["prepare", "coding", "test", "qa", "merge"]:
        pdata = phases.get(pname, {})
        pstatus = pdata.get("status", "pending")
        icon = _icons.get(pstatus, "\u25e6")
        ptime = pdata.get("time_s", 0)
        extra = ""
        if pstatus == "running":
            extra = f"  [dim]{elapsed_str}[/dim]"
        elif pstatus == "done" and ptime:
            extra = f"  [dim]{ptime:.0f}s[/dim]"
        elif pstatus == "fail":
            err = rich_escape(pdata.get("error", "")[:40])
            extra = f"  [error]{err}[/error]"
        lines.append(Text.from_markup(f"    {icon} {pname:<10}{extra}"))
    tools = live.get("recent_tools", [])
    for tool_line in tools[-3:]:
        lines.append(Text.from_markup(f"        [dim]{rich_escape(tool_line[:60])}[/dim]"))
    if cost > 0:
        lines.append(Text.from_markup(f"    [dim]${cost:.2f} so far[/dim]"))


def watch_status(tasks_loader, console_obj=None) -> None:
    """Auto-refresh status display every 2 seconds using Rich Live."""
    c = console_obj or console

    def render():
        tasks = tasks_loader()
        return build_status_table(tasks, show_phase=True)

    try:
        with Live(render(), refresh_per_second=0.5, console=c) as live:
            while True:
                time.sleep(2)
                live.update(render())
    except KeyboardInterrupt:
        c.print(f"\n[dim]Stopped.[/dim]")
