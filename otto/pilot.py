"""Otto pilot — LLM-driven orchestrator that replaces run_all().

The pilot is a long-running Claude agent that drives task execution by calling
otto functions exposed as MCP tools. It decides what to run next, reads results,
analyzes failures, and makes strategic decisions like a tech lead.
"""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    from claude_agent_sdk import ClaudeAgentOptions, query
    from claude_agent_sdk.types import (
        AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock,
    )
except ImportError:
    from otto._agent_stub import ClaudeAgentOptions, query, ResultMessage
    AssistantMessage = None
    TextBlock = None
    ToolUseBlock = None
    ToolResultBlock = None

from otto.config import git_meta_dir
from otto.runner import (
    _DIM, _GREEN, _RED, _YELLOW, _RESET,
    _log_info, _log_warn,
    _print_summary, _subprocess_env,
)
from otto.tasks import load_tasks, update_task


# ---------------------------------------------------------------------------
# Pilot output helpers — three-tier display system
#
# Tier 1 (primary):   Phase progress, task results, failures — always visible
# Tier 2 (secondary): Pilot reasoning, substantive tool calls — visible, dimmed
# Tier 3 (noise):     ToolSearch, save_run_state, bookkeeping — suppressed
# ---------------------------------------------------------------------------

import threading

_CYAN = "\033[36m"
_BOLD = "\033[1m"

# Tool categories for display tiering
_PRIMARY_TOOLS = {"run_coding_agent", "run_qa_agent", "finish_run"}
_SECONDARY_TOOLS = {"get_run_state", "read_verify_output", "merge_task", "abort_task"}
_NOISE_TOOLS = {"save_run_state", "ToolSearch", "write_task_notes", "write_learning"}

_TOOL_DISPLAY = {
    "get_run_state": ("📋", "Loading task state"),
    "run_coding_agent": ("🔨", "Coding"),
    "read_verify_output": ("📖", "Reading verify output"),
    "merge_task": ("🔀", "Merging"),
    "abort_task": ("❌", "Aborting"),
    "save_run_state": ("💾", "Saving state"),
    "finish_run": ("🏁", "Done"),
    "write_task_notes": ("📝", "Writing task notes"),
    "write_learning": ("📝", "Recording learning"),
    "run_qa_agent": ("🔍", "QA Testing"),
}


class _Spinner:
    """Animated spinner for long-running operations. Shows elapsed time and coding agent progress."""

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str, progress_file: str | None = None):
        self._label = label
        self._running = False
        self._thread = None
        self._start_time = 0.0
        self._progress_file = progress_file
        self._last_progress_pos = 0
        self._last_progress_line = ""

    def _read_progress(self):
        """Read new lines from coding agent progress file and print them."""
        if not self._progress_file:
            return
        try:
            import sys as _sys
            from pathlib import Path
            p = Path(self._progress_file)
            if not p.exists():
                return
            with open(p) as f:
                f.seek(self._last_progress_pos)
                new_lines = f.readlines()
                self._last_progress_pos = f.tell()
            for line in new_lines:
                line = line.rstrip()
                if line:
                    # Clear spinner, print progress line, spinner will redraw
                    _sys.stdout.write(f"\r{' ' * 80}\r")
                    _sys.stdout.write(f"  {_DIM}  {line[:78]}{_RESET}\n")
                    _sys.stdout.flush()
                    self._last_progress_line = line
        except OSError:
            pass

    def start(self):
        import sys as _sys
        self._running = True
        self._start_time = time.monotonic()

        def _spin():
            idx = 0
            last_printed = ""
            while self._running:
                elapsed = time.monotonic() - self._start_time
                frame = self._FRAMES[idx % len(self._FRAMES)]
                mins = int(elapsed // 60)
                secs = int(elapsed % 60)
                time_str = f"{mins}:{secs:02d}" if mins else f"{secs}s"

                # Check for coding agent progress every ~2 seconds
                if idx % 13 == 0:
                    self._read_progress()

                _sys.stdout.write(f"\r  {_DIM}{frame} {self._label} ({time_str}){_RESET}  ")
                _sys.stdout.flush()
                idx += 1
                time.sleep(0.15)

        self._thread = threading.Thread(target=_spin, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)
        elapsed = time.monotonic() - self._start_time
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        time_str = f"{mins}m{secs:02d}s" if mins else f"{secs}s"
        # Clear spinner line
        import sys as _sys
        _sys.stdout.write(f"\r{' ' * 60}\r")
        _sys.stdout.flush()
        return time_str


# Active spinner (module-level so tool call/result can manage it)
_active_spinner: _Spinner | None = None

# Track last-displayed tool call to avoid duplicate headers
# (e.g., when the pilot retries a tool or when side-channel processing
# triggers a new ToolUseBlock for the same tool+task)
_last_displayed_tool: str | None = None


def _print_pilot_tool_call(block) -> None:
    """Print a pilot tool call with tiered display."""
    global _active_spinner, _last_displayed_tool

    # Always stop any existing spinner before printing anything
    if _active_spinner:
        _active_spinner.stop()
        _active_spinner = None

    name = block.name
    inputs = block.input or {}

    # Strip mcp prefix
    tool_name = name.replace("mcp__otto-pilot__", "")
    icon, label = _TOOL_DISPLAY.get(tool_name, ("●", tool_name))

    # Build detail string based on tool type
    detail = ""
    if "task_key" in inputs:
        detail = f"task {inputs['task_key'][:8]}"
    elif "task_keys" in inputs:
        keys = inputs["task_keys"]
        detail = f"{len(keys)} tasks"
    if inputs.get("hint"):
        hint_preview = str(inputs["hint"])[:60]
        detail += f' — hint: "{hint_preview}"'
    # Show file paths and commands for common tools
    if not detail:
        if name in ("Read", "Glob", "Grep"):
            detail = inputs.get("file_path") or inputs.get("path") or inputs.get("pattern") or ""
        elif name in ("Edit", "Write"):
            detail = inputs.get("file_path") or ""
        elif name == "Bash":
            cmd = inputs.get("command") or ""
            detail = cmd[:80]
        elif name.startswith("mcp__chrome-devtools__"):
            # Show chrome-devtools action details
            if inputs.get("url"):
                detail = inputs["url"]
            elif inputs.get("uid"):
                detail = f"element {inputs['uid']}"
            elif inputs.get("value"):
                detail = f'"{inputs["value"][:40]}"'

    # Tier 3: suppress noise tools entirely
    if tool_name in _NOISE_TOOLS or name == "ToolSearch":
        return

    # Deduplicate: if the same tool+detail was just displayed, skip the header
    display_key = f"{tool_name}:{detail}"
    if display_key == _last_displayed_tool:
        # Still start a spinner for long-running tools (the previous one was stopped above)
        if tool_name in ("run_coding_agent",):
            _active_spinner = _Spinner(label)
            _active_spinner.start()
        return
    _last_displayed_tool = display_key

    # Tier 2: secondary tools shown dimmed, one line
    if tool_name in _SECONDARY_TOOLS:
        if detail:
            print(f"  {_DIM}{icon} {label}  {detail}{_RESET}", flush=True)
        else:
            print(f"  {_DIM}{icon} {label}{_RESET}", flush=True)
        return

    # Tier 1: otto primary tools — prominent with separator, start spinner
    if tool_name in _PRIMARY_TOOLS:
        print(f"\n  {_DIM}{'─' * 50}{_RESET}", flush=True)
        if detail:
            print(f"  {icon} {_BOLD}{label}{_RESET}  {_DIM}{detail}{_RESET}", flush=True)
        else:
            print(f"  {icon} {_BOLD}{label}{_RESET}", flush=True)
        if tool_name in ("run_coding_agent",):
            # Show coding agent progress via progress file
            task_key = inputs.get("task_key", "")
            progress_path = None
            if task_key:
                from pathlib import Path as _Path
                progress_path = str(_Path.cwd() / "otto_logs" / task_key / "progress.txt")
                try:
                    _Path(progress_path).unlink(missing_ok=True)
                except OSError:
                    pass
            _active_spinner = _Spinner(label, progress_file=progress_path)
            _active_spinner.start()
        return

    # Default: show tool with detail, dimmed (Read, Bash, Grep, chrome-devtools, etc.)
    if detail:
        print(f"  {_DIM}{icon} {label}  {detail}{_RESET}", flush=True)
    else:
        print(f"  {_DIM}{icon} {label}{_RESET}", flush=True)


def _print_pilot_tool_result(block) -> None:
    """Print a pilot tool result with structured parsing."""
    global _active_spinner, _last_displayed_tool

    # Stop spinner if active
    elapsed_str = ""
    if _active_spinner:
        elapsed_str = _active_spinner.stop()
        _active_spinner = None

    # Reset last-displayed tool so the next call shows its header
    _last_displayed_tool = None

    # Extract text content from ToolResultBlock — may be str, list of blocks, or nested
    raw = block.content
    if raw is None:
        return
    if isinstance(raw, str):
        content = raw
    elif isinstance(raw, list):
        # List of content blocks — extract text from each
        parts = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
            elif hasattr(item, "text"):
                parts.append(item.text)
            elif isinstance(item, dict) and "text" in item:
                parts.append(item["text"])
        content = "\n".join(parts)
    else:
        content = str(raw)

    if not content.strip():
        return

    # Try to parse JSON results for structured display
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            # Strip the side-channel "tool" key before pattern matching —
            # _emit_result adds it but it breaks heuristic checks downstream
            data.pop("tool", None)
            # Single task result (from run_coding_agent)
            if "success" in data and "status" in data:
                icon = f"{_GREEN}✓{_RESET}" if data["success"] else f"{_RED}✗{_RESET}"
                parts = []
                if data.get("status"):
                    parts.append(data["status"])
                if data.get("cost_usd"):
                    parts.append(f"${data['cost_usd']:.2f}")
                if elapsed_str:
                    parts.append(elapsed_str)
                if data.get("error"):
                    parts.append(f"{_RED}{data['error'][:80]}{_RESET}")
                detail = f" · ".join(parts)
                print(f"    {icon} {detail}", flush=True)
                # Show code diff if present
                diff = data.get("diff", "")
                if diff:
                    for dline in diff.split("\\n"):
                        if dline.strip():
                            # Diff lines have embedded ANSI for +/- coloring
                            print(f"  {dline}", flush=True)
                # Show verify output on failure
                verify_out = data.get("verify_output", "")
                if verify_out and not data["success"]:
                    print(f"    {_DIM}{'─' * 40}{_RESET}", flush=True)
                    for vline in verify_out.split("\\n")[-10:]:
                        if "FAILED" in vline or "ERROR" in vline or "error" in vline.lower():
                            print(f"    {_RED}{vline}{_RESET}", flush=True)
                        elif vline.strip():
                            print(f"    {_DIM}{vline}{_RESET}", flush=True)
                return

            # Multi-task results (from run_coding_agents)
            if data and all(isinstance(v, dict) for v in data.values()):
                has_task_results = any(
                    isinstance(v, dict) and "success" in v for v in data.values()
                )
                if has_task_results:
                    if elapsed_str:
                        print(f"    {_DIM}{elapsed_str} total{_RESET}", flush=True)
                    for key, result in data.items():
                        if isinstance(result, dict) and "success" in result:
                            icon = f"{_GREEN}✓{_RESET}" if result["success"] else f"{_RED}✗{_RESET}"
                            error = result.get("error", "")
                            err_str = f" — {_RED}{error[:60]}{_RESET}" if error else ""
                            print(f"    {icon} {key[:8]}{err_str}", flush=True)
                    return

            # Integration gate / holistic testgen result
            if "passed" in data:
                icon = f"{_GREEN}✓{_RESET}" if data["passed"] else f"{_RED}✗{_RESET}"
                label = "passed" if data["passed"] else "FAILED"
                suffix = f"  {_DIM}{elapsed_str}{_RESET}" if elapsed_str else ""
                if data.get("skipped"):
                    print(f"    {_DIM}skipped{_RESET}", flush=True)
                else:
                    print(f"    {icon} {label}{suffix}", flush=True)
                return

            # Done signal
            if "done" in data:
                return  # finish_run — the summary below handles this

            # Generic ok response (save_run_state, update_task_status, etc.)
            if "ok" in data:
                return  # suppressed — bookkeeping

            # Holistic testgen returns {key: path}
            non_null = sum(1 for v in data.values() if v is not None)
            total = len(data)
            if total > 0 and all(
                v is None or (isinstance(v, str) and "test_otto" in v)
                for v in data.values()
            ):
                suffix = f"  {_DIM}{elapsed_str}{_RESET}" if elapsed_str else ""
                print(f"    {_GREEN}✓{_RESET} {non_null}/{total} test files generated{suffix}", flush=True)
                return

        # Task state summary (list of dicts from get_run_state)
        if isinstance(data, list) and data and isinstance(data[0], dict) and "id" in data[0]:
            for t in data:
                status = t.get("status", "?")
                deps = t.get("depends_on", [])
                deps_str = f" → #{', #'.join(str(d) for d in deps)}" if deps else ""
                spec_count = t.get("spec_count", 0)
                status_icon = {
                    "pending": f"{_DIM}○{_RESET}",
                    "running": f"{_CYAN}◉{_RESET}",
                    "passed": f"{_GREEN}✓{_RESET}",
                    "failed": f"{_RED}✗{_RESET}",
                    "blocked": f"{_RED}⊘{_RESET}",
                }.get(status, "?")
                print(
                    f"    {status_icon} #{t.get('id', '?')} "
                    f"{_DIM}[{spec_count} spec{deps_str}]{_RESET}  "
                    f"{t.get('prompt', '')[:55]}",
                    flush=True,
                )
            return
    except (json.JSONDecodeError, TypeError, KeyError):
        pass

    # Fallback: show truncated raw content (for errors, unexpected responses)
    if block.is_error:
        lines = content.strip().splitlines()
        for line in lines[-5:]:
            print(f"    {_RED}{line}{_RESET}", flush=True)
    elif len(content) > 300:
        print(f"    {_DIM}{content[:300]}...{_RESET}", flush=True)
    else:
        print(f"    {_DIM}{content}{_RESET}", flush=True)


# ---------------------------------------------------------------------------
# Run-state management
# ---------------------------------------------------------------------------

def _load_run_state(project_dir: Path) -> dict:
    """Load run state from otto_arch/run-state.json."""
    state_file = project_dir / "otto_arch" / "run-state.json"
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_run_state(project_dir: Path, state: dict) -> None:
    """Save run state to otto_arch/run-state.json."""
    state_dir = project_dir / "otto_arch"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "run-state.json").write_text(
        json.dumps(state, indent=2, default=str)
    )


# ---------------------------------------------------------------------------
# Pilot prompt builder
# ---------------------------------------------------------------------------

def _build_pilot_prompt(
    tasks: list[dict],
    config: dict,
    project_dir: Path,
) -> str:
    """Build the user prompt for the pilot agent. Kept lean — rules are in system_prompt."""
    return "Start by calling get_run_state and running `git log --oneline` for project context. Then plan and execute."


# ---------------------------------------------------------------------------
# MCP server script content (written to temp file and run as subprocess)
# ---------------------------------------------------------------------------

def _build_mcp_server_script(
    config: dict,
    tasks_file: Path,
    project_dir: Path,
) -> str:
    """Build the MCP server Python script content.

    This script runs as a subprocess and exposes otto functions as MCP tools.
    Uses the `mcp` library for the server infrastructure.
    """
    config_json = json.dumps(config)
    return f'''#!/usr/bin/env python3
"""Otto pilot MCP server — exposes otto functions as tools for the pilot agent."""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

# Ensure the otto package is importable
sys.path.insert(0, {repr(str(Path(__file__).resolve().parent.parent))})

CONFIG = json.loads({repr(config_json)})
TASKS_FILE = Path({repr(str(tasks_file))})
PROJECT_DIR = Path({repr(str(project_dir))})

# Capture run-start SHA for scoped diffs
_start_result = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    cwd=PROJECT_DIR, capture_output=True, text=True,
)
RUN_START_SHA = _start_result.stdout.strip() if _start_result.returncode == 0 else None

# Side-channel for tool results — the display loop reads this file
_RESULTS_FILE = PROJECT_DIR / "otto_logs" / "pilot_results.jsonl"
_RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
# Clear from previous run
if _RESULTS_FILE.exists():
    _RESULTS_FILE.unlink()


def _emit_result(tool_name: str, data: dict) -> None:
    """Write a tool result to the side-channel file for the display loop."""
    import json as _json
    with open(_RESULTS_FILE, "a") as f:
        f.write(_json.dumps({{"tool": tool_name, **data}}) + "\\n")

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("ERROR: mcp library not installed. Install with: pip install mcp", file=sys.stderr)
    sys.exit(1)

mcp = FastMCP("otto-pilot")


@mcp.tool()
def get_run_state() -> str:
    """Get current status of all tasks. Returns JSON summary."""
    from otto.tasks import load_tasks
    tasks = load_tasks(TASKS_FILE)
    summary = []
    for t in tasks:
        summary.append({{
            "id": t.get("id"),
            "key": t.get("key"),
            "prompt": t.get("prompt", ""),
            "status": t.get("status"),
            "attempts": t.get("attempts", 0),
            "depends_on": t.get("depends_on") or [],
            "spec_count": len(t.get("spec", [])),
            "cost_usd": t.get("cost_usd", 0),
            "error": t.get("error"),
        }})
    return json.dumps(summary, indent=2)


@mcp.tool()
async def run_coding_agent(task_key: str, hint: str = "") -> str:
    """Run a coding agent for one task. Optional hint for retry guidance."""
    from otto.runner import run_task
    from otto.tasks import load_tasks, update_task

    tasks = load_tasks(TASKS_FILE)
    task = next((t for t in tasks if t.get("key") == task_key), None)
    if not task:
        return json.dumps({{"error": f"task {{task_key}} not found"}})

    if hint:
        update_task(TASKS_FILE, task_key, feedback=hint)
        task["feedback"] = hint

    pre_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_DIR, capture_output=True, text=True,
    ).stdout.strip()

    try:
        success = await run_task(task, CONFIG, PROJECT_DIR, TASKS_FILE)
    except Exception as exc:
        # Runner crashed — mark task failed, return structured error
        try:
            update_task(TASKS_FILE, task_key, status="failed",
                        error=f"runner crashed: {{str(exc)[:200]}}")
        except Exception:
            pass
        error_data = {{
            "success": False, "status": "failed",
            "cost_usd": 0, "error": f"runner crashed: {{str(exc)[:200]}}",
            "diff": "", "verify_output": "",
        }}
        _emit_result("run_coding_agent", error_data)
        return json.dumps(error_data)

    diff_summary = _build_diff_summary(PROJECT_DIR, pre_sha)

    verify_snippet = ""
    if not success:
        log_dir = PROJECT_DIR / "otto_logs" / task_key
        if log_dir.exists():
            verify_logs = sorted(log_dir.glob("attempt-*-verify.log"), reverse=True)
            if verify_logs:
                content = verify_logs[0].read_text()
                lines = content.strip().splitlines()
                verify_snippet = "\\n".join(lines[-20:])

    tasks = load_tasks(TASKS_FILE)
    updated = next((t for t in tasks if t.get("key") == task_key), {{}})

    result_data = {{
        "success": success,
        "status": updated.get("status"),
        "cost_usd": updated.get("cost_usd", 0),
        "error": updated.get("error"),
        "diff": diff_summary,
        "verify_output": verify_snippet,
    }}
    _emit_result("run_coding_agent", result_data)
    return json.dumps(result_data)


def _build_diff_summary(project_dir, pre_sha, max_lines_per_file=20, max_files=5):
    """Build a code diff summary with actual +/- lines, like CC TUI.

    Shows the real code changes per file, truncated for readability.
    Skips test files generated by otto (already known to the user).
    """
    # Get list of changed files (excluding otto test files)
    stat_result = subprocess.run(
        ["git", "diff", "--stat", pre_sha, "HEAD"],
        cwd=project_dir, capture_output=True, text=True,
    )
    stat_line = ""
    if stat_result.returncode == 0:
        for line in stat_result.stdout.strip().splitlines():
            if "changed" in line and ("insertion" in line or "deletion" in line):
                stat_line = line.strip()

    # Get actual diff
    result = subprocess.run(
        ["git", "diff", pre_sha, "HEAD", "--", "*.py",
         ":!tests/test_otto_*", ":!tests/conftest.py"],
        cwd=project_dir, capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return stat_line

    # Parse diff into per-file sections
    output_lines = []
    current_file = None
    file_lines = []
    file_count = 0

    for line in result.stdout.splitlines():
        if line.startswith("diff --git"):
            # Flush previous file
            if current_file and file_lines:
                if file_count < max_files:
                    output_lines.append(f"  {{current_file}}")
                    output_lines.extend(file_lines[:max_lines_per_file])
                    if len(file_lines) > max_lines_per_file:
                        output_lines.append(f"    ... ({{len(file_lines) - max_lines_per_file}} more lines)")
                    output_lines.append("")
                file_count += 1
            # Start new file
            parts = line.split(" b/", 1)
            current_file = parts[1] if len(parts) > 1 else "?"
            file_lines = []
        elif line.startswith("@@"):
            # Hunk header — show as context
            file_lines.append(f"    {{line}}")
        elif line.startswith("+") and not line.startswith("+++"):
            file_lines.append(f"    \\033[32m{{line}}\\033[0m")
        elif line.startswith("-") and not line.startswith("---"):
            file_lines.append(f"    \\033[31m{{line}}\\033[0m")

    # Flush last file
    if current_file and file_lines:
        if file_count < max_files:
            output_lines.append(f"  {{current_file}}")
            output_lines.extend(file_lines[:max_lines_per_file])
            if len(file_lines) > max_lines_per_file:
                output_lines.append(f"    ... ({{len(file_lines) - max_lines_per_file}} more lines)")
        file_count += 1

    if file_count > max_files:
        output_lines.append(f"  ... and {{file_count - max_files}} more files")

    if stat_line:
        output_lines.append(f"  {{stat_line}}")

    return "\\n".join(output_lines)


def _merge_task_branch_to_default(project_dir, key, default_branch):
    """Merge a task branch to default with rebase retry (mirrors runner._merge_task_branch)."""
    from otto.runner import merge_to_default
    if merge_to_default(project_dir, key, default_branch):
        return True
    # ff-only failed (main advanced from earlier merge) — try rebase
    branch_name = f"otto/{{key}}"
    rebase = subprocess.run(
        ["git", "rebase", default_branch, branch_name],
        cwd=project_dir, capture_output=True,
    )
    if rebase.returncode != 0:
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=project_dir, capture_output=True,
        )
        return False
    return merge_to_default(project_dir, key, default_branch)


@mcp.tool()
def read_verify_output(task_key: str) -> str:
    """Read verification output summary for a task. Returns the last verify log."""
    log_dir = PROJECT_DIR / "otto_logs" / task_key
    if not log_dir.exists():
        return json.dumps({{"error": "no logs found"}})

    # Find most recent verify log
    verify_logs = sorted(log_dir.glob("attempt-*-verify.log"), reverse=True)
    if not verify_logs:
        return json.dumps({{"error": "no verify logs found"}})

    content = verify_logs[0].read_text()
    # Return truncated summary
    if len(content) > 10000:
        content = content[:10000] + "\\n... (truncated)"
    return content


@mcp.tool()
def merge_task(task_key: str) -> str:
    """Merge a verified task branch to the default branch (with rebase retry).
    Returns JSON with success status."""
    default_branch = CONFIG.get("default_branch", "main")
    success = _merge_task_branch_to_default(PROJECT_DIR, task_key, default_branch)
    return json.dumps({{"success": success}})


@mcp.tool()
def abort_task(task_key: str, reason: str) -> str:
    """Abort a task with a reason. Marks it as failed."""
    from otto.tasks import update_task
    update_task(TASKS_FILE, task_key, status="failed", error=f"aborted: {{reason}}")
    return json.dumps({{"ok": True}})


@mcp.tool()
def save_run_state(phase: str, notes: str = "") -> str:
    """Persist current run state for context recovery.

    Auto-populates task details from tasks.yaml. Just pass the current
    phase name and optional notes about decisions/approaches tried.

    If the pilot session runs out of context, a new session can read
    run-state.json to resume seamlessly.
    """
    from otto.tasks import load_tasks
    tasks = load_tasks(TASKS_FILE)
    state = {{
        "phase": phase,
        "notes": notes,
        "run_start_sha": RUN_START_SHA,
        "tasks": {{
            t.get("key"): {{
                "id": t.get("id"),
                "status": t.get("status"),
                "attempts": t.get("attempts", 0),
                "cost_usd": t.get("cost_usd", 0),
                "error": t.get("error"),
                "depends_on": t.get("depends_on") or [],
                "prompt": t.get("prompt", ""),
            }}
            for t in tasks
        }},
    }}
    state_dir = PROJECT_DIR / "otto_arch"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "run-state.json").write_text(json.dumps(state, indent=2))
    return json.dumps({{"ok": True}})


@mcp.tool()
def write_task_notes(task_key: str, notes: str) -> str:
    """Write notes about a task for future attempts."""
    notes_dir = PROJECT_DIR / "otto_arch" / "task-notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    (notes_dir / f"{{task_key}}.md").write_text(notes)
    return json.dumps({{"ok": True}})


@mcp.tool()
def write_learning(learning: str) -> str:
    """Record a cross-task learning."""
    learnings_file = PROJECT_DIR / "otto_arch" / "learnings.md"
    learnings_file.parent.mkdir(parents=True, exist_ok=True)
    content = learnings_file.read_text() if learnings_file.exists() else "# Learnings\\n\\n"
    content += f"- {{learning}}\\n"
    learnings_file.write_text(content)
    return json.dumps({{"ok": True}})


@mcp.tool()
async def run_qa_agent(task_key: str, spec_items: str, diff_summary: str) -> str:
    """Run an adversarial QA agent that tries to BREAK the implementation.
    Pass the spec items and diff summary. Returns findings report."""
    from otto.tasks import load_tasks
    import os

    tasks = load_tasks(TASKS_FILE)
    task = next((t for t in tasks if t.get("key") == task_key), None)
    if not task:
        return json.dumps({{"error": f"task {{task_key}} not found"}})

    qa_system_prompt = \"\"\"You are an adversarial QA tester. Your ONLY job is to find ways the
implementation does NOT meet the spec. You get rewarded for finding bugs.

Rules:
- Test the HARDEST cases first. If the spec says "<200ms on cold start",
  clear ALL caches and measure. Don't test the warm path.
- Test in layers: curl/API first (fast), browser second (slow).
  If curl shows a violation, report immediately — no browser needed.
- For each spec item, try to find the ONE case that breaks it.
- Report exactly what you tested, what you expected, and what happened.
- If everything genuinely passes the hardest cases, say so honestly.
- Do NOT assume the implementation is correct. Verify everything.
\"\"\"

    qa_prompt = f\"\"\"SPEC ITEMS TO VERIFY:
{{spec_items}}

DIFF SUMMARY:
{{diff_summary}}

TASK: {{task.get("prompt", "")}}

You are working in {{PROJECT_DIR}}.

Steps:
1. Start the dev server if needed
2. CURL first: test API endpoints, check responses, measure timing
3. BROWSER second (if curl passes): interact with the app, click everything
4. For EACH spec item: find the hardest test case and try it
5. Write findings to {{PROJECT_DIR}}/otto_logs/{{task_key}}/qa-report.md
6. Kill any dev servers you started (by PID)

Report format:
- PASS: spec item met, with evidence (what you tested)
- FAIL: spec item NOT met, with evidence (expected vs actual)
- CONCERN: technically passes but might be gaming the spec
\"\"\"

    try:
        from claude_agent_sdk import ClaudeAgentOptions as _QAOpts, query as _qa_query
        from claude_agent_sdk.types import ResultMessage as _QAResult

        qa_opts = _QAOpts(
            permission_mode="bypassPermissions",
            cwd=str(PROJECT_DIR),
            max_turns=50,
            system_prompt=qa_system_prompt,
            env=dict(os.environ),
            setting_sources=["user", "project"],
        )

        qa_log = []
        async for message in _qa_query(prompt=qa_prompt, options=qa_opts):
            if hasattr(message, "content"):
                for block in message.content:
                    if hasattr(block, "text") and block.text:
                        qa_log.append(block.text)
                    elif hasattr(block, "name"):
                        qa_log.append(f"● {{block.name}}")

        # Read QA report if written
        qa_report_path = PROJECT_DIR / "otto_logs" / task_key / "qa-report.md"
        report = ""
        if qa_report_path.exists():
            report = qa_report_path.read_text()
        else:
            report = "\\n".join(qa_log[-20:])  # fallback: last 20 lines of QA output

        # Check for failures
        has_fail = "FAIL" in report.upper() and "FAIL" in report

        result = {{
            "passed": not has_fail,
            "report": report[:5000],
            "has_failures": has_fail,
        }}
        _emit_result("run_qa_agent", result)
        return json.dumps(result)

    except Exception as e:
        return json.dumps({{"error": f"QA agent failed: {{str(e)[:200]}}", "passed": True}})


@mcp.tool()
def finish_run(summary: str) -> str:
    """Signal that the run is complete. Pass a summary of what happened."""
    return json.dumps({{"done": True, "summary": summary}})


if __name__ == "__main__":
    mcp.run()
'''


# ---------------------------------------------------------------------------
# Pilot orchestrator
# ---------------------------------------------------------------------------

async def run_piloted(
    config: dict[str, Any],
    tasks_file: Path,
    project_dir: Path,
) -> int:
    """LLM-driven orchestrator that replaces run_all().

    Launches a Claude agent with MCP tools exposing otto's internal functions.
    The agent decides execution order, handles failures, and drives the pipeline.

    Returns exit code (0=all passed, 1=any failed, 2=error).
    """
    import fcntl
    import signal
    import tempfile

    default_branch = config["default_branch"]

    # Acquire process lock
    lock_path = git_meta_dir(project_dir) / "otto.lock"
    lock_path.touch()
    lock_fh = open(lock_path, "r")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"{_RED}Another otto process is running{_RESET}", flush=True)
        return 2

    # Signal handling
    interrupted = False

    def _signal_handler(signum, frame):
        nonlocal interrupted
        if interrupted:
            print(f"\n{_RED}Force exit{_RESET}", flush=True)
            sys.exit(1)
        interrupted = True
        print(f"\n{_YELLOW}⚠ Interrupted — pilot will finish current tool call{_RESET}", flush=True)

    old_sigint = signal.signal(signal.SIGINT, _signal_handler)
    old_sigterm = signal.signal(signal.SIGTERM, _signal_handler)

    try:
        # Ensure we're on the default branch before starting
        subprocess.run(
            ["git", "checkout", default_branch],
            cwd=project_dir, capture_output=True,
        )

        # Dirty-tree protection — stash or abort
        from otto.runner import check_clean_tree
        if not check_clean_tree(project_dir):
            print(f"{_RED}✗ Working tree is dirty — fix before running otto{_RESET}", flush=True)
            return 2

        # Baseline check
        test_command = config.get("test_command")
        if test_command:
            _log_info("Running baseline check...")
            result = subprocess.run(
                test_command, shell=True, cwd=project_dir,
                capture_output=True, timeout=config["verify_timeout"],
                env=_subprocess_env(),
            )
            # Exit code 5 = "no tests collected" (empty test suite) — not a failure
            if result.returncode not in (0, 5):
                print(f"  {_RED}✗ Baseline tests failing — fix before running otto{_RESET}", flush=True)
                return 2

        # Recover stale "running" tasks
        tasks = load_tasks(tasks_file)
        for t in tasks:
            if t.get("status") == "running":
                update_task(tasks_file, t["key"], status="pending",
                            error=None, session_id=None)
                print(f"  {_YELLOW}⚠ Task #{t['id']} was stuck in 'running' — reset to pending{_RESET}", flush=True)

        # Load pending tasks
        tasks = load_tasks(tasks_file)
        pending = [t for t in tasks if t.get("status") == "pending"]
        if not pending:
            print(f"{_DIM}No pending tasks{_RESET}", flush=True)
            return 0

        # Inject dependencies from file-plan.md if otto_arch exists
        if not config.get("no_architect", False) and len(pending) >= 2:
            from otto.architect import parse_file_plan
            arch_deps = parse_file_plan(project_dir)
            if arch_deps:
                tasks = load_tasks(tasks_file)
                pending = [t for t in tasks if t.get("status") == "pending"]
                pending_by_id = {t["id"]: t for t in pending}
                injected = 0
                for dep_id, on_id in arch_deps:
                    task = pending_by_id.get(dep_id)
                    if task:
                        deps = list(task.get("depends_on") or [])
                        if on_id not in deps:
                            deps.append(on_id)
                            update_task(tasks_file, task["key"], depends_on=deps)
                            injected += 1
                if injected:
                    print(f"  {_DIM}Injected {injected} dependencies from file-plan.md{_RESET}", flush=True)

        # Write MCP server script to temp file
        mcp_script = _build_mcp_server_script(config, tasks_file, project_dir)
        with tempfile.NamedTemporaryFile(
            "w",
            suffix=".py",
            prefix="otto_pilot_mcp_",
            delete=False,
        ) as temp_file:
            temp_file.write(mcp_script)
            mcp_script_path = Path(temp_file.name)

        run_start = time.monotonic()

        try:
            # Build pilot prompt
            tasks = load_tasks(tasks_file)
            pending = [t for t in tasks if t.get("status") == "pending"]
            pilot_prompt = _build_pilot_prompt(pending, config, project_dir)

            # Configure MCP server
            mcp_server_config = {
                "command": sys.executable,
                "args": [str(mcp_script_path)],
            }

            # Merge otto-pilot MCP with user's MCP servers from ~/.claude.json
            all_mcp_servers = {"otto-pilot": mcp_server_config}
            user_claude_json = Path.home() / ".claude.json"
            if user_claude_json.exists():
                try:
                    user_config = json.loads(user_claude_json.read_text())
                    for name, srv in user_config.get("mcpServers", {}).items():
                        if name == "otto-pilot":
                            continue  # don't override our own
                        # Chrome-devtools: use dedicated otto profile + headless
                        # to avoid conflicts with user's browser. Reuse the same
                        # profile across runs to avoid accumulating macOS
                        # code_sign_clone copies (~1.9G each).
                        if name == "chrome-devtools":
                            srv = dict(srv)
                            args = list(srv.get("args", []))
                            if "--headless" not in args:
                                args.append("--headless")
                            if not any(a.startswith("--viewport") for a in args):
                                args.extend(["--viewport", "1280x720"])
                            if not any(a.startswith("--userDataDir") for a in args):
                                otto_chrome_profile = str(Path.home() / ".cache" / "otto" / "chrome-profile")
                                args.extend(["--userDataDir", otto_chrome_profile])
                            srv["args"] = args
                        all_mcp_servers[name] = srv
                except (json.JSONDecodeError, OSError):
                    pass

            _pilot_system_prompt = """\
<role>
You are a tech lead managing coding agents. You orchestrate task execution
and verify quality. A separate QA agent handles adversarial testing.
</role>

<workflow>
PHASE 1: PLAN
- get_run_state → git log → read what you need → plan execution order
- Respect depends_on. The coding agent does its own deep exploration.

PHASE 2: EXECUTE
- run_coding_agent for each task (it handles implement + test + verify)
- On failure: read_verify_output, retry with targeted hint
- Same error twice = doom loop → change strategy fundamentally, not just tweak
- After 3 total failures on a task, abort_task with structured escalation:
  what was tried, why it failed, what would need to change, next steps for human

PHASE 3: QA AGENT (after coding agent passes)
- Call run_qa_agent with the spec items and diff summary
- The QA agent adversarially tests the app (curl first, browser second)
- It tries to BREAK the implementation, not confirm it works
- If QA finds failures → retry run_coding_agent with the QA findings as hint
- If QA passes → task is done

PHASE 4: REPORT
- finish_run with summary
</workflow>

<tools>
AVAILABLE TOOLS:
- get_run_state: see all tasks and their status
- run_coding_agent(task_key, hint?): run coding agent, optional hint for retries
- run_qa_agent(task_key, spec_items, diff_summary): adversarial QA testing
- read_verify_output(task_key): read verification failure details
- merge_task(task_key): manual merge with rebase retry
- abort_task(task_key, reason): give up with structured reason
- save_run_state(phase, notes): persist state for session recovery
- write_task_notes(task_key, notes): document approach for future retries
- write_learning(learning): record cross-task learning
- finish_run(summary): signal completion
</tools>

<rules>
- Track progress: call save_run_state after major decisions
- Do NOT modify project files directly — let the coding agent do that
- Do NOT kill dev servers with pkill/killall — only by specific PID you started
- Never retry with the same approach twice
</rules>

<completion_check>
Before calling finish_run, verify:
1. Every task has passed both coding verification AND QA agent testing
2. QA report exists at otto_logs/<key>/qa-report.md
3. No unresolved FAIL findings from the QA agent
</completion_check>"""

            agent_opts = ClaudeAgentOptions(
                permission_mode="bypassPermissions",
                cwd=str(project_dir),
                max_turns=100,
                mcp_servers=all_mcp_servers,
                setting_sources=["user", "project"],
                env=_subprocess_env(),
                max_buffer_size=10 * 1024 * 1024,  # 10MB — screenshots can be large
                system_prompt=_pilot_system_prompt,
            )
            if config.get("model"):
                agent_opts.model = config["model"]

            print(flush=True)
            _log_info("Pilot taking control — LLM-driven execution")
            print(f"  {_DIM}The pilot will drive coding → verify → merge{_RESET}", flush=True)
            print(flush=True)

            # Run pilot agent — three-tier output display
            # Track last tool call name to match results
            _last_tool_name = None
            # Side-channel result file from MCP tools
            _results_file = project_dir / "otto_logs" / "pilot_results.jsonl"
            _results_read_pos = 0  # track how far we've read
            # Debug log to diagnose block types
            _debug_log = project_dir / "otto_logs" / "pilot_debug.log"
            _debug_log.parent.mkdir(parents=True, exist_ok=True)
            _debug_fh = open(_debug_log, "w")

            async for message in query(prompt=pilot_prompt, options=agent_opts):
                if isinstance(message, ResultMessage):
                    _debug_fh.write(f"[ResultMessage] is_error={getattr(message, 'is_error', '?')}\n")
                    _debug_fh.flush()
                    # Stop any active spinner on completion
                    global _active_spinner
                    if _active_spinner:
                        _active_spinner.stop()
                        _active_spinner = None
                elif hasattr(message, "session_id") and hasattr(message, "is_error"):
                    pass
                elif AssistantMessage and isinstance(message, AssistantMessage):
                    for block in message.content:
                        block_type = type(block).__name__
                        _debug_fh.write(f"[{block_type}] ")
                        if hasattr(block, "name"):
                            _debug_fh.write(f"name={block.name} ")
                        if hasattr(block, "content"):
                            c = block.content
                            preview = str(c)[:200] if c else "None"
                            _debug_fh.write(f"content_type={type(c).__name__} preview={preview}")
                        _debug_fh.write("\n")
                        _debug_fh.flush()

                        # Catch-all: stop spinner for any block type that isn't
                        # ToolUseBlock or ToolResultBlock (e.g., ThinkingBlock).
                        # ToolUseBlock and ToolResultBlock handle spinner
                        # themselves in their display functions.
                        is_tool_block = (
                            (ToolUseBlock and isinstance(block, ToolUseBlock))
                            or (ToolResultBlock and isinstance(block, ToolResultBlock))
                        )
                        if not is_tool_block and _active_spinner:
                            _active_spinner.stop()
                            _active_spinner = None

                        if TextBlock and isinstance(block, TextBlock) and block.text:
                            # (spinner already stopped by catch-all above)

                            # Check side-channel for tool results
                            if _results_file.exists():
                                try:
                                    with open(_results_file) as rf:
                                        rf.seek(_results_read_pos)
                                        new_lines = rf.readlines()
                                        _results_read_pos = rf.tell()
                                    for rline in new_lines:
                                        rline = rline.strip()
                                        if rline:
                                            try:
                                                rdata = json.loads(rline)
                                                # Create a fake result block and display it
                                                class _FakeResult:
                                                    def __init__(self, content, is_error=False):
                                                        self.content = content
                                                        self.is_error = is_error
                                                _print_pilot_tool_result(
                                                    _FakeResult(json.dumps(rdata))
                                                )
                                            except json.JSONDecodeError:
                                                pass
                                except OSError:
                                    pass

                            text = block.text.strip()
                            if not text:
                                continue

                            # Skip code fences and raw JSON
                            if text.startswith("```") or text.startswith("{"):
                                continue

                            # Tier 1: Execution plan — prominent cyan
                            if "EXECUTION PLAN" in text or "PLAN UPDATE" in text:
                                print(f"\n{_CYAN}{text}{_RESET}", flush=True)
                            # Tier 1: Decision points — the pilot's strategic thinking
                            elif any(marker in text.lower() for marker in [
                                "passed", "failed", "now executing", "starting",
                                "retrying", "aborting", "all tasks", "integration",
                            ]):
                                print(f"  {text}", flush=True)
                            # Tier 2: Other pilot reasoning — dimmed
                            else:
                                print(f"  {_DIM}{text}{_RESET}", flush=True)

                        elif ToolUseBlock and isinstance(block, ToolUseBlock):
                            _last_tool_name = block.name
                            _print_pilot_tool_call(block)
                        elif ToolResultBlock and isinstance(block, ToolResultBlock):
                            _print_pilot_tool_result(block)
                            _last_tool_name = None

        except Exception as pilot_err:
            # Pilot agent crashed (e.g., buffer overflow from large screenshot)
            # Still proceed to summary — tasks may have passed before the crash
            if _active_spinner:
                _active_spinner.stop()
                _active_spinner = None
            print(f"\n  {_YELLOW}⚠ Pilot agent error: {pilot_err}{_RESET}", flush=True)
            print(f"  {_DIM}Proceeding to summary — completed tasks are still merged.{_RESET}", flush=True)
        finally:
            # Clean up
            try:
                _debug_fh.close()
            except Exception:
                pass
            if mcp_script_path.exists():
                mcp_script_path.unlink()

        # Post-run: calculate results from final task states
        final_tasks = load_tasks(tasks_file)
        results: list[tuple[dict, bool]] = []
        total_cost = 0.0
        for t in final_tasks:
            if t.get("status") in ("passed", "failed", "blocked"):
                results.append((t, t.get("status") == "passed"))
                total_cost += t.get("cost_usd", 0.0)
            elif t.get("status") == "running":
                # Task stuck in "running" — pilot crashed mid-task
                update_task(tasks_file, t["key"], status="failed",
                            error="pilot crashed during execution")
                results.append((t, False))
                total_cost += t.get("cost_usd", 0.0)

        # Print summary
        _print_summary(results, time.monotonic() - run_start, total_cost=total_cost)

        any_failed = any(not s for _, s in results)
        return 1 if any_failed else 0

    finally:
        # Ensure we're back on the default branch after the pilot finishes
        subprocess.run(
            ["git", "checkout", default_branch],
            cwd=project_dir, capture_output=True,
        )
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
