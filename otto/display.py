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


_PROJECT_DIR_RE = re.compile(r"/(?:private/)?tmp/[^\s/]+/")

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
        self._edit_streak_first: str = ""

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

    def set_qa_summary(self, total: int, passed: int, failed: int = 0) -> None:
        """Set authoritative QA counts from the runner."""
        with self._lock:
            total = max(0, int(total))
            passed = max(0, min(int(passed), total))
            failed = max(0, min(int(failed), total - passed))
            self._qa_spec_count = total
            self._qa_pass_count = passed
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
            if self._current_phase == "coding" and name in ("Write", "Edit"):
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

            # Collapse consecutive reads in coding phase (show first 3, then batch)
            if self._current_phase == "coding" and name in ("Read", "Glob", "Grep"):
                self._read_count += 1
                if self._read_count > 3:
                    return  # suppress — will flush on next Write/Edit/Bash
            elif self._current_phase == "coding" and self._read_count > 3:
                self._console.print(f"      [dim]... explored {self._read_count} files[/dim]")
                self._read_count = 0

            # Collapse consecutive similar edits (e.g., adding same field to 20 test files)
            if self._current_phase == "coding" and name == "Edit":
                self._edit_streak += 1
                if self._edit_streak == 1:
                    self._edit_streak_first = detail
                elif self._edit_streak <= 2:
                    pass  # show first 2 normally
                else:
                    return  # suppress — will flush on next non-Edit
            elif self._edit_streak > 2:
                self._console.print(
                    f"      [dim]... edited {self._edit_streak} files (similar changes)[/dim]"
                )
                self._edit_streak = 0
            else:
                self._edit_streak = 0

        if self._current_phase == "qa":
            label = self._qa_tool_label(name, detail)
            if not label:
                return  # suppressed (temp file ops, etc.)
            with self._lock:
                # Collapse consecutive Read/Glob/Grep into a counter
                if name in ("Read", "Glob", "Grep"):
                    self._read_count += 1
                    if self._read_count <= 2:
                        # Show first 2 reads normally
                        pass
                    else:
                        # Suppress further reads — will show count on next non-read
                        return
                elif self._read_count > 2:
                    # Flush collapsed reads as a single line
                    self._console.print(f"      [dim]... read {self._read_count} files[/dim]")
                    self._read_count = 0

                if label == self._last_qa_label:
                    return
                self._last_qa_label = label
            self._console.print(f"      [dim]{label}[/dim]")
            return

        # Truncate Bash commands at first newline (multi-line node -e commands)
        if name == "Bash":
            first_line = detail.split("\n")[0].strip()
            short = rich_escape(first_line[:68])
        else:
            short = rich_escape(_shorten_path(detail)[:68])

        # Breathing room on tool type change (Read→Bash, Write→Read, etc.)
        with self._lock:
            prev_type = getattr(self, '_last_tool_type', "")
            self._last_tool_type = name
        if prev_type and prev_type != name:
            self._console.print()

        # Consistent style for all phases — ● Name  detail
        self._console.print(f"      [bold cyan]\u25cf {name}[/bold cyan]  [dim]{short}[/dim]")

        # Inline diff/preview below tool call (like CC)
        if data:
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
                    for ol in old_lines:
                        self._console.print(f"        [red]- {rich_escape(ol)}[/red]")
                    if old_total > len(old_lines):
                        self._console.print(f"        [dim]  ...{old_total - len(old_lines)} more[/dim]")
                    for nl in new_lines:
                        self._console.print(f"        [green]+ {rich_escape(nl)}[/green]")
                    if new_total > len(new_lines):
                        self._console.print(f"        [dim]  ...{new_total - len(new_lines)} more[/dim]")

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
            icon = "[green]\u2713[/green]" if has_pass else "[red]\u2717[/red]" if has_fail else " "
            self._console.print(f"      {icon} {rich_escape(spec_text[:68])}")

        elif is_table_row:
            parts = [p.strip() for p in clean.split("|") if p.strip()]
            if len(parts) >= 2:
                desc = f"{parts[0]}  {parts[1]}"[:55]
            else:
                desc = parts[0][:55] if parts else clean[:55]
            icon = "[green]\u2713[/green]" if has_pass else "[red]\u2717[/red]"
            self._console.print(f"      {icon} {rich_escape(desc)}")

        elif is_numbered_check:
            # "✓ 4. LIFO order  Pushed..." or "✗ 2. Edge case..."
            check_pass = clean[0] in ("\u2713", "\u2705")
            desc = clean[1:].lstrip().lstrip("0123456789").lstrip(".").strip()[:62]
            icon = "[green]\u2713[/green]" if check_pass else "[red]\u2717[/red]"
            self._console.print(f"      {icon} {rich_escape(desc)}")

        elif is_result_line and has_pass:
            # "**RESULT**: ✅ PASS — detail" or "**PASS** — detail"
            detail_text = clean.replace("RESULT", "").replace("PASS", "").replace("\u2705", "")
            detail_text = detail_text.lstrip(": \u2014-*()").strip()
            if detail_text:
                short = detail_text[:60] + "..." if len(detail_text) > 60 else detail_text
                self._console.print(f"        [green]\u2713[/green] [dim]{rich_escape(short)}[/dim]")

        elif is_result_line and has_fail:
            detail_text = clean.replace("RESULT", "").replace("FAIL", "").replace("\u274c", "")
            detail_text = detail_text.lstrip(": \u2014-*()").strip()
            short = detail_text[:60] + "..." if len(detail_text) > 60 else detail_text
            self._console.print(f"        [red]\u2717[/red] {rich_escape(short)}")

        elif has_fail and text.startswith(("**FAIL", "FAIL")):
            detail_text = clean.replace("FAIL", "").replace("\u274c", "").lstrip(" \u2014-()").strip()
            short = detail_text[:62] + "..." if len(detail_text) > 62 else detail_text
            self._console.print(f"        [red]\u2717[/red] {rich_escape(short)}")

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
            self._console.print(f"      [dim]{rich_escape(text)}[/dim]")

    def flush_spec_summary(self) -> None:
        """Print a collapsed summary of buffered spec items."""
        with self._lock:
            items = self._spec_items_buffer
            self._spec_items_buffer = []
        if not items:
            return
        must_items = [t for t in items if "[must]" in t]
        preview = must_items[:3] or items[:3]
        for item in preview:
            self._console.print(f"      [dim]{rich_escape(item[:80])}[/dim]")
        remaining = len(items) - len(preview)
        if remaining > 0:
            self._console.print(f"      [dim]... +{remaining} more[/dim]")

    def add_qa_item_result(self, text: str, passed: bool = True, evidence: str = "") -> None:
        """Print a QA per-item result."""
        if not text:
            return
        if passed:
            # Passed items: dim with green checkmark (not all-green)
            self._console.print(f"      [dim]{rich_escape(text)}[/dim]")
            return
        # Failed items: red with evidence
        self._console.print(f"      [red]{rich_escape(text)}[/red]")
        if evidence:
            self._console.print(f"        [dim]evidence: {rich_escape(evidence)}[/dim]")

    # -- Private --

    def _print_phase_done(self, name: str, time_s: float, detail: str, cost: float) -> None:
        # Suppress trivial phases (0s, no info)
        if name in ("prepare", "merge") and time_s < 1 and not detail:
            return

        timestamp = time.strftime("%H:%M:%S")
        phase_label = name.replace("_", " ")
        parts = []
        if time_s >= 1:
            parts.append(f"{time_s:.0f}s")
        if cost:
            parts.append(f"${cost:.2f}")

        if name == "prepare" and detail:
            parts.append(detail)
        elif name == "coding":
            if self._coding_files:
                line_info = ""
                if self._lines_added or self._lines_removed:
                    line_parts = []
                    if self._lines_added:
                        line_parts.append(f"[green]+{self._lines_added}[/green]")
                    if self._lines_removed:
                        line_parts.append(f"[red]-{self._lines_removed}[/red]")
                    line_info = f"  {' '.join(line_parts)}"
                parts.append(f"{len(self._coding_files)} files{line_info}")
        elif name == "spec_gen":
            # Show spec count + binding breakdown
            if detail:
                parts.append(detail)
        elif name == "test" and detail:
            m = re.search(r'(\d+) passed', detail)
            if m:
                parts.append(f"[green]{m.group(0)}[/green]")
            else:
                parts.append(detail[:35])
        elif name == "qa":
            if self._qa_spec_count:
                t, p = self._qa_spec_count, self._qa_pass_count
                parts.append(f"{t} specs passed" if p == t else f"{p}/{t} specs passed")
            tier_detail = self._phase_details.get("qa", "")
            if tier_detail:
                parts.append(tier_detail)
        elif name == "candidate":
            return

        info = "  ".join(parts)
        self._console.print(f"  [green]{timestamp}  \u2713 {phase_label}[/green]  [dim]{info}[/dim]")

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
    return f"{minutes}m{secs}s"


def format_cost(cost: float) -> str:
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"
