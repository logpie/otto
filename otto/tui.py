"""Otto TUI — full-screen terminal dashboard using Textual.

Provides live dashboards for long-running commands (run, add, arch).
Falls back to plain Rich console output when not connected to a terminal.

Key classes:
- OttoRunApp: Full-screen TUI for `otto run`
- ToolLog: Scrollable tool call history (RichLog-based)
- PhaseBar: Always-visible phase progress indicator
- TaskPanel: Per-task container (header + tool log)
"""

from __future__ import annotations

import time
from typing import Any

from rich.markup import escape as rich_escape
from rich.text import Text

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import RichLog, Static


# ---------------------------------------------------------------------------
# Path utilities (shared with display.py)
# ---------------------------------------------------------------------------

def _shorten_path(path: str) -> str:
    """Shorten absolute paths to relative project paths."""
    if not path or not path.startswith("/"):
        if "/" in path and not any(path.startswith(m) for m in
                                    ("src/", "__tests__/", "tests/", "test/", "lib/")):
            fname = path.rsplit("/", 1)[-1]
            if "." in fname:
                return fname
        return path
    for marker in ("src/", "__tests__/", "tests/", "test/", "lib/", "app/", "components/"):
        idx = path.find(marker)
        if idx >= 0:
            return path[idx:]
    return path.rsplit("/", 1)[-1] if "/" in path else path


_INTERNAL_PATTERNS = {"otto_arch/", "task-notes/"}

PHASE_ORDER = ["prepare", "coding", "test", "qa", "merge"]


# ---------------------------------------------------------------------------
# Custom Messages (for thread-safe event passing)
# ---------------------------------------------------------------------------

class ProgressEvent(Message):
    """A progress event from the JSONL side-channel."""
    def __init__(self, event_type: str, data: dict) -> None:
        super().__init__()
        self.event_type = event_type
        self.data = data


class RunComplete(Message):
    """The run has finished."""
    def __init__(self, exit_code: int = 0, summary: str = "") -> None:
        super().__init__()
        self.exit_code = exit_code
        self.summary = summary


# ---------------------------------------------------------------------------
# PhaseBar — always-visible phase progress
# ---------------------------------------------------------------------------

class PhaseBar(Static):
    """Bottom bar showing phase progress for all tasks."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._phases: dict[str, dict[str, Any]] = {
            p: {"status": "pending"} for p in PHASE_ORDER
        }
        self._start_time = time.monotonic()
        self._cost: float = 0.0
        self._task_info: str = ""

    def update_phase(self, name: str, status: str, time_s: float = 0,
                     cost: float = 0, **kwargs) -> None:
        if name in self._phases:
            self._phases[name]["status"] = status
            if time_s:
                self._phases[name]["time_s"] = time_s
        if cost:
            self._cost = cost
        self._refresh_display()

    def set_task_info(self, info: str) -> None:
        self._task_info = info
        self._refresh_display()

    def _refresh_display(self) -> None:
        elapsed = time.monotonic() - self._start_time
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        time_str = f"{mins}:{secs:02d}" if mins else f"{secs}s"

        parts = []
        for p in PHASE_ORDER:
            s = self._phases[p]["status"]
            if s == "done":
                t = self._phases[p].get("time_s", 0)
                ts = f" {t:.0f}s" if t else ""
                parts.append(f"[green]✓ {p}{ts}[/green]")
            elif s == "fail":
                parts.append(f"[red]✗ {p}[/red]")
            elif s == "running":
                parts.append(f"[bold cyan]● {p}[/bold cyan]")
            else:
                parts.append(f"[dim]○ {p}[/dim]")

        cost_str = f"  ${self._cost:.2f}" if self._cost else ""
        task_str = f"  {self._task_info}" if self._task_info else ""

        bar = "  ".join(parts) + f"  [dim]{time_str}{cost_str}{task_str}[/dim]"
        self.update(bar)


# ---------------------------------------------------------------------------
# TaskPanel — per-task container
# ---------------------------------------------------------------------------

class TaskPanel(Vertical):
    """A single task's display: header + scrollable tool log."""

    DEFAULT_CSS = """
    TaskPanel {
        height: 1fr;
        border: round $primary-lighten-2;
        padding: 0 1;
    }
    TaskPanel .task-header {
        height: 1;
        color: $text;
        text-style: bold;
    }
    TaskPanel RichLog {
        height: 1fr;
        scrollbar-size: 1 1;
    }
    """

    def __init__(self, task_id: int = 0, task_prompt: str = "",
                 task_key: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.task_id = task_id
        self.task_prompt = task_prompt
        self.task_key = task_key
        self._last_tool_key = ""
        self._coding_files: list[str] = []
        self._qa_spec_count = 0
        self._qa_pass_count = 0

    def compose(self) -> ComposeResult:
        prompt_short = self.task_prompt[:50] + "..." if len(self.task_prompt) > 50 else self.task_prompt
        yield Static(
            f"[bold]Task #{self.task_id}[/bold]  [dim]{rich_escape(prompt_short)}[/dim]",
            classes="task-header",
        )
        yield RichLog(highlight=False, markup=True, wrap=True, auto_scroll=True)

    @property
    def log(self) -> RichLog:
        return self.query_one(RichLog)

    def add_phase_start(self, name: str, attempt: int = 0) -> None:
        """Log a phase starting."""
        if name in ("prepare", "merge"):
            return  # skip trivial phases
        retry = f"  [dim](retry {attempt})[/dim]" if attempt and attempt > 1 else ""
        self.log.write(Text(""))  # blank line for spacing
        self.log.write(Text.from_markup(f"  [bold]{name}[/bold]{retry}"))

    def add_phase_done(self, name: str, time_s: float = 0, cost: float = 0,
                       detail: str = "") -> None:
        """Log a phase completion."""
        if name in ("prepare", "merge") and time_s < 1 and not detail:
            return  # suppress trivial

        import re
        parts = []
        if time_s >= 1:
            parts.append(f"{time_s:.0f}s")
        if cost:
            parts.append(f"${cost:.2f}")

        if name == "coding":
            if detail:
                parts.append(detail[:40])
            elif self._coding_files:
                parts.append(f"{len(self._coding_files)} files")
        elif name == "test" and detail:
            m = re.search(r'(\d+) passed', detail)
            parts.append(m.group(0) if m else detail[:35])
        elif name == "qa" and self._qa_spec_count:
            t, p = self._qa_spec_count, self._qa_pass_count
            parts.append(f"{t} specs passed" if p == t else f"{p}/{t} specs passed")

        info = "  ".join(parts)
        self.log.write(Text.from_markup(f"  [green]✓ {name}[/green]  [dim]{info}[/dim]"))

    def add_phase_fail(self, name: str, time_s: float = 0, error: str = "",
                       cost: float = 0) -> None:
        parts = []
        if time_s >= 1:
            parts.append(f"{time_s:.0f}s")
        if cost:
            parts.append(f"${cost:.2f}")
        if error:
            parts.append(error[:40])
        info = "  ".join(parts)
        self.log.write(Text.from_markup(f"  [red]✗ {name}[/red]  [dim]{info}[/dim]"))

    def add_tool(self, data: dict) -> None:
        """Add a tool call to the log."""
        name = data.get("name", "")
        detail = _shorten_path(data.get("detail", ""))

        # Skip internal files
        if any(p in detail for p in _INTERNAL_PATTERNS):
            return

        # Deduplicate
        fname = detail.rsplit("/", 1)[-1] if "/" in detail else detail
        tool_key = f"{name}:{fname}"
        if tool_key == self._last_tool_key:
            return
        self._last_tool_key = tool_key

        # Track coding files
        if name in ("Write", "Edit") and fname:
            if fname not in self._coding_files:
                self._coding_files.append(fname)

        # Write/Edit/Bash get a blank line before (visual breathing room)
        if name in ("Write", "Edit", "Bash"):
            self.log.write(Text(""))

        # Tool call line
        short = rich_escape(detail[:68])
        self.log.write(Text.from_markup(
            f"      [bold cyan]● {name}[/bold cyan]  [dim]{short}[/dim]"
        ))

        # Inline content
        old_lines = data.get("old_lines", [])
        new_lines = data.get("new_lines", [])
        old_total = data.get("old_total", 0)
        new_total = data.get("new_total", 0)
        preview = data.get("preview_lines", [])
        total_lines = data.get("total_lines", 0)

        if name == "Edit" and (old_lines or new_lines):
            for ol in old_lines:
                self.log.write(Text.from_markup(f"        [red]- {rich_escape(ol)}[/red]"))
            if old_total > len(old_lines):
                self.log.write(Text.from_markup(f"        [dim]...{old_total - len(old_lines)} more[/dim]"))
            for nl in new_lines:
                self.log.write(Text.from_markup(f"        [green]+ {rich_escape(nl)}[/green]"))
            if new_total > len(new_lines):
                self.log.write(Text.from_markup(f"        [dim]...{new_total - len(new_lines)} more[/dim]"))

        elif name == "Write" and preview:
            for pl in preview:
                self.log.write(Text.from_markup(f"        [green]+ {rich_escape(pl)}[/green]"))
            if total_lines > len(preview):
                self.log.write(Text.from_markup(
                    f"        [dim]...{total_lines - len(preview)} more lines[/dim]"
                ))

    def add_tool_result(self, data: dict) -> None:
        """Add a tool result inline (e.g., test output)."""
        detail = data.get("detail", "")
        passed = data.get("passed", False)
        if detail:
            style = "green" if passed else "red"
            self.log.write(Text.from_markup(f"        [{style}]{rich_escape(detail)}[/{style}]"))

    def add_finding(self, text: str) -> None:
        """Add a QA finding to the log."""
        if not text:
            return

        clean = text.replace("**", "").replace("__", "")
        has_pass = "✅" in text or "PASS" in text
        has_fail = "❌" in text or "FAIL" in text

        is_spec = (
            text.startswith("###")
            or (text.startswith("**Spec") and (has_pass or has_fail))
            or (text.startswith("|") and (has_pass or has_fail) and "Spec" not in text[:5])
        )
        is_header = text.startswith("|") and ("Spec" in text[:15] or "Check" in text[:20] or "---" in text[:5])

        if is_spec:
            self._qa_spec_count += 1
            if has_pass:
                self._qa_pass_count += 1
        elif has_pass and text.startswith(("**PASS", "PASS")):
            self._qa_pass_count += 1

        if "VERDICT" in text or is_header:
            return
        if text.startswith(("- Container", "- Header")):
            return

        # Render
        if is_spec:
            if text.startswith("|"):
                parts = [p.strip() for p in clean.split("|") if p.strip()]
                desc = f"{parts[0]}  {parts[1]}"[:55] if len(parts) >= 2 else parts[0][:55] if parts else ""
            elif text.startswith("###"):
                desc = clean.lstrip("# ").strip()
            else:
                desc = clean.lstrip("# ").strip()
                for s in [": ✅ PASS", ": ❌ FAIL", "✅", "❌"]:
                    desc = desc.replace(s, "").rstrip(": ").strip()
            icon = "[green]✓[/green]" if has_pass else "[red]✗[/red]" if has_fail else " "
            self.log.write(Text.from_markup(f"      {icon} [dim]{rich_escape(desc[:65])}[/dim]"))

        elif has_pass and text.startswith(("**PASS", "PASS")):
            detail = clean.replace("PASS", "").replace("✅", "").lstrip(" —-()").strip()
            if detail:
                self.log.write(Text.from_markup(
                    f"        [green]✓[/green] [dim]{rich_escape(detail[:60])}[/dim]"
                ))

        elif has_fail and text.startswith(("**FAIL", "FAIL")):
            detail = clean.replace("FAIL", "").replace("❌", "").lstrip(" —-()").strip()
            self.log.write(Text.from_markup(f"        [red]✗[/red] {rich_escape(detail[:60])}"))


# ---------------------------------------------------------------------------
# RunScreen — the main otto run display
# ---------------------------------------------------------------------------

class OttoRunApp(App):
    """Inline TUI for otto run — renders in the terminal buffer, not alternate screen."""

    DEFAULT_CSS = """
    Screen {
        layout: vertical;
        height: auto;
        max-height: 100vh;
    }
    :inline Screen {
        max-height: 40;
    }
    #task-container {
        height: 1fr;
        min-height: 10;
    }
    #phase-bar {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $surface;
    }
    #run-header {
        height: 1;
        padding: 0 1;
        background: $surface;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self, tasks: list[dict] | None = None,
                 config: dict | None = None,
                 tasks_path: Any = None,
                 project_dir: Any = None,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self._tasks = tasks or []
        self._panels: dict[str, TaskPanel] = {}
        self._active_task_key: str | None = None
        self._summary_lines: list[str] = []
        # Pilot integration
        self._config = config
        self._tasks_path = tasks_path
        self._project_dir = project_dir
        self._exit_code: int = 0

    def compose(self) -> ComposeResult:
        # Header
        task_count = len(self._tasks)
        total_specs = sum(len(t.get("spec") or []) for t in self._tasks)
        yield Static(
            f" [bold]otto run[/bold]  [dim]{task_count} task{'s' if task_count != 1 else ''}, {total_specs} specs[/dim]",
            id="run-header",
        )

        # Task panels container
        with Horizontal(id="task-container"):
            if self._tasks:
                for t in self._tasks:
                    panel = TaskPanel(
                        task_id=t.get("id", 0),
                        task_prompt=t.get("prompt", ""),
                        task_key=t.get("key", ""),
                    )
                    self._panels[t["key"]] = panel
                    yield panel
            else:
                # No tasks known yet — create a default panel
                panel = TaskPanel(task_id=0, task_prompt="", task_key="default")
                self._panels["default"] = panel
                yield panel

        # Phase bar
        yield PhaseBar(id="phase-bar")

    @property
    def phase_bar(self) -> PhaseBar:
        return self.query_one("#phase-bar", PhaseBar)

    def get_panel(self, task_key: str) -> TaskPanel:
        """Get the panel for a task, or the first/default panel."""
        if task_key in self._panels:
            return self._panels[task_key]
        # Fall back to first panel
        if self._panels:
            return next(iter(self._panels.values()))
        raise KeyError(f"No panel for task {task_key}")

    def handle_progress(self, event_type: str, data: dict) -> None:
        """Handle a progress event (called from background thread via post_message)."""
        task_key = data.get("task_key", "")

        try:
            if event_type == "phase":
                name = data.get("name", "")
                status = data.get("status", "")
                time_s = data.get("time_s", 0)
                cost = data.get("cost", 0)
                error = data.get("error", "")
                detail = data.get("detail", "")
                attempt = data.get("attempt", 0)

                # Update phase bar
                self.phase_bar.update_phase(name, status, time_s, cost)

                # Update task panel
                panel = self.get_panel(task_key)
                if status == "running":
                    self._active_task_key = task_key
                    panel.add_phase_start(name, attempt)
                    panel._last_tool_key = ""
                    if name == "coding":
                        panel._coding_files.clear()
                    if name == "qa":
                        panel._qa_spec_count = 0
                        panel._qa_pass_count = 0
                elif status == "done":
                    panel.add_phase_done(name, time_s, cost, detail)
                elif status == "fail":
                    panel.add_phase_fail(name, time_s, error, cost)

            elif event_type == "agent_tool":
                panel = self.get_panel(task_key)
                panel.add_tool(data)

            elif event_type == "agent_tool_result":
                panel = self.get_panel(task_key)
                panel.add_tool_result(data)

            elif event_type == "qa_finding":
                panel = self.get_panel(task_key)
                panel.add_finding(data.get("text", ""))

        except (KeyError, Exception):
            pass  # panel not found or widget error

    def on_mount(self) -> None:
        """Start the pilot agent when the app mounts (if config is provided)."""
        if self._config is not None:
            self.run_pilot()

    @work(thread=True)
    def run_pilot(self) -> None:
        """Run the pilot agent in a background thread."""
        import asyncio
        from otto.pilot import _run_pilot_core

        loop = asyncio.new_event_loop()
        try:
            exit_code = loop.run_until_complete(
                _run_pilot_core(self._config, self._tasks_path, self._project_dir,
                                tui_app=self)
            )
        except Exception:
            exit_code = 2
        finally:
            loop.close()
        self.post_message(RunComplete(exit_code=exit_code))

    @on(ProgressEvent)
    def on_progress_event(self, event: ProgressEvent) -> None:
        """Handle progress events posted from background threads."""
        self.handle_progress(event.event_type, event.data)

    @on(RunComplete)
    def on_run_complete(self, event: RunComplete) -> None:
        """Run finished — exit the TUI."""
        self._exit_code = event.exit_code
        self.exit(result=event.summary)
