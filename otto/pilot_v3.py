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
from otto.display import TaskDisplay, console, rich_escape
from otto.runner import (
    _log_info, _log_warn,
    _print_summary, _subprocess_env,
    preflight_checks,
)

# Optional import — may not exist in older SDK versions
try:
    from claude_agent_sdk.types import AgentDefinition
except (ImportError, AttributeError):
    AgentDefinition = None  # type: ignore[assignment,misc]
from otto.tasks import load_tasks, update_task


# ---------------------------------------------------------------------------
# Pilot output helpers — three-tier display system
#
# Tier 1 (primary):   Phase progress, task results, failures — always visible
# Tier 2 (secondary): Pilot reasoning, substantive tool calls — visible, dimmed
# Tier 3 (noise):     ToolSearch, save_run_state, bookkeeping — suppressed
# ---------------------------------------------------------------------------

import threading

# Tool categories for display tiering
_PRIMARY_TOOLS = {"run_task_with_qa", "finish_run"}
_SECONDARY_TOOLS = {"read_verify_output", "abort_task"}
_NOISE_TOOLS = {"save_run_state", "ToolSearch", "write_task_notes", "write_learning", "get_run_state"}

_TOOL_DISPLAY = {
    "get_run_state": ("\u25cf", "Loading task state"),
    "run_task_with_qa": ("\u25cf", "Running"),
    "read_verify_output": ("\u25cf", "Reading verify output"),
    "abort_task": ("\u2717", "Aborting"),
    "save_run_state": ("\u25cf", "Saving state"),
    "finish_run": ("\u2713", "Done"),
    "write_task_notes": ("\u25cf", "Writing task notes"),
    "write_learning": ("\u25cf", "Recording learning"),
}

# Active display for the currently running task
_active_display: TaskDisplay | None = None
_active_task_key: str | None = None

# Track last-displayed tool call to avoid duplicate headers
_last_displayed_tool: str | None = None

# Accumulated progress events per task key (for summary display)
_task_progress: dict[str, list[dict]] = {}

# Cache: task_key -> task id (populated during run_piloted)
_task_key_to_id: dict[str, int] = {}

# Track retry count per task key
_task_attempt_count: dict[str, int] = {}


def _resolve_task_number(task_key: str) -> int | None:
    """Resolve a task key to its task number for display."""
    return _task_key_to_id.get(task_key)


def _process_progress_event(data: dict) -> None:
    """Process a progress event from the JSONL side-channel.

    Routes events to the active TaskDisplay and accumulates them for summary.
    """
    event_type = data.get("event")
    task_key = data.get("task_key", "")

    if not event_type:
        return

    # Accumulate for summary
    if task_key:
        _task_progress.setdefault(task_key, []).append(data)

    display = _active_display
    if display and (_active_task_key is None or task_key == _active_task_key):
        try:
            if event_type == "phase":
                display.update_phase(
                    name=data.get("name", ""),
                    status=data.get("status", ""),
                    time_s=data.get("time_s", 0.0),
                    error=data.get("error", ""),
                    detail=data.get("detail", ""),
                    cost=data.get("cost", 0),
                )
            elif event_type == "agent_tool":
                display.add_tool(data=data)
            elif event_type == "agent_tool_result":
                display.add_tool_result(data=data)
            elif event_type == "qa_finding":
                display.add_finding(data.get("text", ""))
            elif event_type == "qa_summary":
                display.set_qa_summary(
                    total=data.get("total", 0),
                    passed=data.get("passed", 0),
                    failed=data.get("failed", 0),
                )
        except Exception:
            pass


def _print_pilot_tool_call(block) -> None:
    """Print a pilot tool call with tiered display."""
    global _active_display, _active_task_key, _last_displayed_tool

    # Always stop any existing display before printing anything
    if _active_display:
        _active_display.stop()
        _active_display = None
        _active_task_key = None

    name = block.name
    inputs = block.input or {}

    # Strip mcp prefix
    tool_name = name.replace("mcp__otto-pilot__", "")
    icon, label = _TOOL_DISPLAY.get(tool_name, ("\u25cf", tool_name))

    # Build detail string based on tool type
    detail = ""
    if "task_key" in inputs:
        task_key = inputs['task_key']
        # Resolve task number from key for display
        task_num = _resolve_task_number(task_key)
        if task_num:
            detail = f"#{task_num}  {task_key[:8]}"
        else:
            detail = task_key[:8]
    elif "task_keys" in inputs:
        keys = inputs["task_keys"]
        detail = f"{len(keys)} tasks"
    if inputs.get("hint"):
        hint_preview = str(inputs["hint"])[:60]
        detail += f' \u2014 hint: "{hint_preview}"'
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
        # Still start a task display for long-running tools (the previous one was stopped above)
        if tool_name == "run_task_with_qa":
            _active_display = TaskDisplay(console)
            _active_task_key = inputs.get("task_key", "")
            _active_display.start()
        return
    _last_displayed_tool = display_key

    # Tier 2: secondary tools shown dimmed, one line
    if tool_name in _SECONDARY_TOOLS:
        line = f"  {icon} {label}"
        if detail:
            line += f"  {rich_escape(detail)}"
        console.print(line, style="dim")
        return

    # Tier 1: otto primary tools — prominent, no separator
    if tool_name in _PRIMARY_TOOLS:
        console.print()
        if tool_name == "run_task_with_qa":
            task_key = inputs.get("task_key", "")
            attempt = _task_attempt_count.get(task_key, 0) + 1
            _task_attempt_count[task_key] = attempt
            hint = inputs.get("hint", "")

            if attempt > 1:
                # Retry — show prominently with hint
                task_num = _resolve_task_number(task_key)
                task_label = f"#{task_num}" if task_num else task_key[:8]
                console.print(f"  [yellow]\u21bb Retrying {task_label}[/yellow]  [dim](attempt {attempt})[/dim]")
                if hint:
                    hint_short = str(hint)[:120]
                    console.print(f"    [dim]{rich_escape(hint_short)}[/dim]")
            else:
                # First attempt
                if detail:
                    console.print(f"  {icon} [bold]{label}[/bold]  [dim]{rich_escape(detail)}[/dim]")
                else:
                    console.print(f"  {icon} [bold]{label}[/bold]")

            _active_display = TaskDisplay(console)
            _active_task_key = inputs.get("task_key", "")
            _active_display.start()
        else:
            # Other primary tools (finish_run, etc.)
            if detail:
                console.print(f"  {icon} [bold]{label}[/bold]  [dim]{rich_escape(detail)}[/dim]")
            else:
                console.print(f"  {icon} [bold]{label}[/bold]")
        return

    # Default: show tool with detail, dimmed (Read, Bash, Grep, chrome-devtools, etc.)
    line = f"  {icon} {label}"
    if detail:
        line += f"  {rich_escape(detail)}"
    console.print(line, style="dim")


def _print_pilot_tool_result(block) -> None:
    """Print a pilot tool result with structured parsing."""
    global _active_display, _active_task_key, _last_displayed_tool

    # Stop task display if active
    elapsed_str = ""
    if _active_display:
        elapsed_str = _active_display.stop()
        _active_display = None
        _active_task_key = None

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
            # Strip the side-channel "tool" key before pattern matching
            data.pop("tool", None)
            # Legacy single task result (has success + status keys)
            if "success" in data and "status" in data:
                if data["success"]:
                    icon = "[green]\u2713[/green]"
                else:
                    icon = "[red]\u2717[/red]"
                parts = []
                if data.get("status"):
                    parts.append(data["status"])
                if data.get("cost_usd"):
                    parts.append(f"${data['cost_usd']:.2f}")
                if elapsed_str:
                    parts.append(elapsed_str)
                if data.get("error"):
                    parts.append(f"[red]{rich_escape(data['error'][:80])}[/red]")
                detail = " \u00b7 ".join(parts)
                console.print(f"    {icon} {detail}")
                # Show code diff if present
                diff = data.get("diff", "")
                if diff:
                    for dline in diff.split("\\n"):
                        if dline.strip():
                            console.print(f"  {rich_escape(dline)}")
                # Show verify output on failure
                verify_out = data.get("verify_output", "")
                if verify_out and not data["success"]:
                    for vline in verify_out.split("\\n")[-10:]:
                        if "FAILED" in vline or "ERROR" in vline or "error" in vline.lower():
                            console.print(f"    {rich_escape(vline)}", style="red")
                        elif vline.strip():
                            console.print(f"    {rich_escape(vline)}", style="dim")
                return

            # Multi-task results (from run_coding_agents)
            if data and all(isinstance(v, dict) for v in data.values()):
                has_task_results = any(
                    isinstance(v, dict) and "success" in v for v in data.values()
                )
                if has_task_results:
                    if elapsed_str:
                        console.print(f"    {elapsed_str} total", style="dim")
                    for key, result in data.items():
                        if isinstance(result, dict) and "success" in result:
                            if result["success"]:
                                r_icon = "[green]\u2713[/green]"
                            else:
                                r_icon = "[red]\u2717[/red]"
                            error = result.get("error", "")
                            err_str = f" \u2014 [red]{rich_escape(error[:60])}[/red]" if error else ""
                            console.print(f"    {r_icon} {key[:8]}{err_str}")
                    return

            # Integration gate / holistic testgen result
            if "passed" in data:
                if data["passed"]:
                    icon = "[green]\u2713[/green]"
                    label = "passed"
                else:
                    icon = "[red]\u2717[/red]"
                    label = "FAILED"
                suffix = f"  [dim]{elapsed_str}[/dim]" if elapsed_str else ""
                if data.get("skipped"):
                    console.print("    skipped", style="dim")
                else:
                    console.print(f"    {icon} {label}{suffix}")
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
                suffix = f"  [dim]{elapsed_str}[/dim]" if elapsed_str else ""
                console.print(f"    [green]\u2713[/green] {non_null}/{total} test files generated{suffix}")
                return

        # Task state summary (list of dicts from get_run_state)
        if isinstance(data, list) and data and isinstance(data[0], dict) and "id" in data[0]:
            _STATUS_ICONS = {
                "pending": "[dim]\u25cb[/dim]",
                "running": "[cyan]\u25c9[/cyan]",
                "passed": "[green]\u2713[/green]",
                "failed": "[red]\u2717[/red]",
                "blocked": "[red]\u2298[/red]",
            }
            for t in data:
                status = t.get("status", "?")
                deps = t.get("depends_on", [])
                deps_str = f" \u2192 #{', #'.join(str(d) for d in deps)}" if deps else ""
                spec_count = t.get("spec_count", 0)
                s_icon = _STATUS_ICONS.get(status, "?")
                console.print(
                    f"    {s_icon} #{t.get('id', '?')} "
                    f"[dim]\\[{spec_count} spec{deps_str}][/dim]  "
                    f"{rich_escape(t.get('prompt', '')[:55])}"
                )
            return
    except (json.JSONDecodeError, TypeError, KeyError):
        pass

    # Fallback: show truncated raw content (for errors, unexpected responses)
    if block.is_error:
        lines = content.strip().splitlines()
        for line in lines[-5:]:
            console.print(f"    {rich_escape(line)}", style="red")
    elif len(content) > 300:
        console.print(f"    {rich_escape(content[:300])}...", style="dim")
    else:
        console.print(f"    {rich_escape(content)}", style="dim")


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
async def run_task_with_qa(task_key: str, hint: str = "") -> str:
    """Run full task loop: prepare -> code -> verify -> QA -> merge.
    The coding agent grinds until verification passes (up to max_retries).
    Then QA agent adversarially tests. Merges only after both pass."""
    from otto.runner import run_task_with_qa as _run
    from otto.tasks import load_tasks, update_task

    tasks = load_tasks(TASKS_FILE)
    task = next((t for t in tasks if t.get("key") == task_key), None)
    if not task:
        return json.dumps({{"error": f"task {{task_key}} not found"}})

    if hint:
        update_task(TASKS_FILE, task_key, feedback=hint)
        task["feedback"] = hint

    def _on_progress(event, data):
        _emit_result("progress", {{"event": event, "task_key": task_key, **data}})

    try:
        result = await _run(task, CONFIG, PROJECT_DIR, TASKS_FILE,
                            hint=hint or None, on_progress=_on_progress)
        _emit_result("run_task_with_qa", result)
        return json.dumps(result)
    except Exception as exc:
        error_data = {{
            "success": False, "status": "failed",
            "cost_usd": 0, "error": f"task crashed: {{str(exc)[:200]}}",
            "diff_summary": "", "qa_report": "",
        }}
        try:
            update_task(TASKS_FILE, task_key, status="failed",
                        error=f"crashed: {{str(exc)[:200]}}")
        except Exception:
            pass
        _emit_result("run_task_with_qa", error_data)
        return json.dumps(error_data)


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
    return content


@mcp.tool()
def abort_task(task_key: str, reason: str) -> str:
    """Abort a task with a reason. Marks it as failed.
    Will REFUSE if fewer than 3 attempts have been made — you must try harder."""
    from otto.tasks import load_tasks, update_task

    MIN_ATTEMPTS = CONFIG.get("max_retries", 3)
    tasks = load_tasks(TASKS_FILE)
    task = next((t for t in tasks if t.get("key") == task_key), None)
    if task:
        attempts = task.get("attempts", 0)
        if attempts < MIN_ATTEMPTS:
            return json.dumps({{
                "refused": True,
                "error": f"Cannot abort — only {{attempts}} attempts made (minimum {{MIN_ATTEMPTS}}). "
                         f"Try a different approach: different algorithm, different library, "
                         f"research online for solutions, study similar repos for inspiration.",
            }})

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
def finish_run(summary: str) -> str:
    """Signal that the run is complete. Pass a summary of what happened."""
    return json.dumps({{"done": True, "summary": summary}})


if __name__ == "__main__":
    mcp.run()
'''


# ---------------------------------------------------------------------------
# Pilot orchestrator — shared core + two entry points (TTY / non-TTY)
# ---------------------------------------------------------------------------


# _preflight_checks removed — use runner.preflight_checks() (imported at top)


async def _run_pilot_core(
    config: dict[str, Any],
    tasks_file: Path,
    project_dir: Path,
) -> int:
    """Core pilot agent loop. Returns 0 on success, 1 on task failure, 2 on error."""
    return await _run_pilot_core_impl(config, tasks_file, project_dir)


async def _run_pilot_core_impl(
    config: dict[str, Any],
    tasks_file: Path,
    project_dir: Path,
) -> int:
    """Implementation of the core pilot agent loop."""
    # Load pending tasks
    tasks = load_tasks(tasks_file)
    pending = [t for t in tasks if t.get("status") == "pending"]
    if not pending:
        return 0

    pending_keys = {t["key"] for t in pending}

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
        def _safe_console_print(*args, **kwargs) -> None:
            try:
                console.print(*args, **kwargs)
            except Exception:
                pass

        def _safe_print_tool_call(block) -> None:
            try:
                _print_pilot_tool_call(block)
            except Exception:
                pass

        def _safe_print_tool_result(block) -> None:
            try:
                _print_pilot_tool_result(block)
            except Exception:
                pass

        def _safe_stop_active_display() -> None:
            global _active_display, _active_task_key
            try:
                if _active_display:
                    _active_display.stop()
            except Exception:
                pass
            finally:
                _active_display = None
                _active_task_key = None

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
                        continue
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
and make strategic decisions. The coding, verification, QA, and merge steps
are handled automatically by run_task_with_qa — you decide WHAT to run,
in WHAT ORDER, and with what HINTS.
</role>

<workflow>
PHASE 1: PLAN
- get_run_state → git log → read what you need → plan execution order
- Respect depends_on. The coding agent does its own deep exploration.

PHASE 2: EXECUTE (for each task)
- Optional: dispatch Agent(researcher) in background for hard tasks
- run_task_with_qa(key) → full deterministic loop:
  prepare → code → verify → QA → merge (all automatic)
- Returns {{success, status, cost_usd, error, diff_summary, qa_report}}
- If failed: decide retry strategy:
  - Read the error carefully. Give a targeted, specific hint — not generic advice.
  - Different error from last time? Good — making progress. Keep going.
  - Same error repeating? Doom loop. Change strategy fundamentally:
    different algorithm, different library, different architecture.
  - Before retrying a hard failure, dispatch Agent(researcher, "how to ...") first.
    Feed the research findings into the hint parameter.
  - Think you're stuck? List 3 alternative approaches you haven't tried.
  - abort_task will REFUSE if fewer than 3 attempts have been made.
    You must genuinely try before giving up.
  - run_task_with_qa(key, hint="specific guidance based on failure analysis")

PHASE 3: REPORT
- finish_run with summary
</workflow>

<subagents>
You have one native subagent available via the Agent tool:
- researcher: Searches the web, reads docs, studies similar repos.
  Two patterns:
  1. Serial (after failure): research first, feed findings into retry hint.
  2. Parallel (hard task): dispatch researcher alongside run_task_with_qa.
     If task succeeds, ignore research. If it fails, findings are ready.
  Example: Agent(researcher, "how to achieve <200ms LCP in Next.js with React hydration")
</subagents>

<tools>
MCP TOOLS:
- get_run_state: see all tasks and their status
- run_task_with_qa(task_key, hint?): run full task loop (prepare → code → verify → QA → merge)
- read_verify_output(task_key): read verification failure details for crafting retry hints
- abort_task(task_key, reason): give up with structured reason (min-retry guardrail)
- save_run_state(phase, notes): persist state for session recovery
- write_task_notes(task_key, notes): document approach for future retries
- write_learning(learning): record cross-task learning
- finish_run(summary): signal completion
</tools>

<rules>
- Track progress: call save_run_state after major decisions
- Do NOT modify project files directly — let the coding agent do that
- Do NOT kill dev servers with pkill/killall — only by specific PID you started
- Never retry with the same approach twice — always provide a different hint
</rules>

<completion_check>
Before calling finish_run, verify:
1. Every task has been run through run_task_with_qa
2. No unresolved failures (all either passed or properly aborted)
</completion_check>"""

        agent_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=str(project_dir),
            mcp_servers=all_mcp_servers,
            setting_sources=["user", "project"],
            env=_subprocess_env(),
            effort=config.get("effort", "high"),
            max_buffer_size=10 * 1024 * 1024,
            system_prompt=_pilot_system_prompt,
        )
        if config.get("model"):
            agent_opts.model = config["model"]

        if AgentDefinition:
            try:
                agent_opts.agents = {
                    "researcher": AgentDefinition(
                        description="Research a technical topic: search the web, read docs, study similar repos. Use BEFORE retrying a failed coding task, or in parallel with coding on hard tasks.",
                        prompt="You are a research assistant. Search the web, read documentation, and study reference implementations. Report concrete findings: code patterns, API usage examples, library recommendations. Be specific — include URLs, code snippets, and exact function signatures.",
                        model=config.get("researcher_model", "sonnet"),
                    ),
                }
            except (TypeError, AttributeError, ValueError):
                pass

        tasks = load_tasks(tasks_file)
        pending = [t for t in tasks if t.get("status") == "pending"]
        total_specs = sum(len(t.get("spec") or []) for t in pending)
        _safe_console_print()
        _safe_console_print(f"  [bold]{len(pending)} task{'s' if len(pending) != 1 else ''}[/bold], [dim]{total_specs} specs[/dim]")
        for t in pending:
            deps = t.get("depends_on", [])
            dep_str = f" [dim]\u2192 #{', #'.join(str(d) for d in deps)}[/dim]" if deps else ""
            spec_count = len(t.get("spec") or [])
            _safe_console_print(f"    [dim]\u25cb[/dim] [bold]#{t['id']}[/bold]  {rich_escape(t.get('prompt', '')[:55])}  [dim]({spec_count} spec){dep_str}[/dim]")

        _safe_console_print()
        _safe_console_print(f"{'─' * 60}", style="dim")

        # Populate task key -> id cache for display
        for t in pending:
            _task_key_to_id[t.get("key", "")] = t.get("id", 0)

        # Run pilot agent — three-tier output display
        _last_tool_name = None
        _results_file = project_dir / "otto_logs" / "pilot_results.jsonl"
        _results_read_pos = 0
        # Truncate JSONL to prevent stale events from previous runs
        _results_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            _results_file.write_text("")
        except OSError:
            pass
        _debug_log = project_dir / "otto_logs" / "pilot_debug.log"
        _debug_log.parent.mkdir(parents=True, exist_ok=True)
        _debug_fh = open(_debug_log, "w")
        _current_debug_phase = "INIT"

        def _dlog(phase: str, msg: str) -> None:
            nonlocal _current_debug_phase
            _current_debug_phase = phase
            ts = time.strftime("%H:%M:%S")
            _debug_fh.write(f"[{ts}] [{phase}] {msg}\n")
            _debug_fh.flush()

        _dlog("INIT", f"pilot started — {len(pending)} pending tasks")
        try:
            git_head = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=project_dir, capture_output=True, text=True,
            )
            git_branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=project_dir, capture_output=True, text=True,
            )
            _dlog("INIT", f"git branch={git_branch.stdout.strip()} HEAD={git_head.stdout.strip()}")
        except Exception:
            pass

        _task_progress.clear()

        _bg_reader_running = True

        def _bg_read_results():
            nonlocal _results_read_pos
            _carry = ""
            _last_inode = 0
            try:
                _dlog("INIT", "background JSONL reader started")
            except Exception:
                pass
            while _bg_reader_running:
                try:
                    if _results_file.exists():
                        try:
                            st = _results_file.stat()
                            if st.st_ino != _last_inode or st.st_size < _results_read_pos:
                                _results_read_pos = 0
                                _carry = ""
                            _last_inode = st.st_ino
                        except OSError:
                            pass
                        with open(_results_file) as rf:
                            rf.seek(_results_read_pos)
                            raw = rf.read()
                        new_lines: list[str] = []
                        if raw:
                            raw = _carry + raw
                            if raw.endswith("\n"):
                                new_lines = raw.splitlines()
                                _carry = ""
                                _results_read_pos += len(raw.encode())
                            else:
                                parts = raw.rsplit("\n", 1)
                                new_lines = parts[0].splitlines() if len(parts) > 1 else []
                                _carry = parts[-1]
                                _results_read_pos += len(raw.encode()) - len(_carry.encode())
                        for rline in new_lines:
                            rline = rline.strip()
                            if not rline:
                                continue
                            try:
                                rdata = json.loads(rline)
                                tool = rdata.get("tool", "")
                                if tool == "progress":
                                    _process_progress_event(rdata)
                                    evt = rdata.get("event", "")
                                    task_key = rdata.get("task_key", "")[:8]
                                    if evt == "phase":
                                        pname = rdata.get("name", "")
                                        pstatus = rdata.get("status", "")
                                        ptime = rdata.get("time_s", 0)
                                        perr = rdata.get("error", "")[:60]
                                        if pstatus == "running":
                                            _dlog("EXEC", f"{pname} started — task={task_key}")
                                        elif pstatus == "done":
                                            _dlog("EXEC", f"{pname} done — {ptime:.0f}s task={task_key}")
                                        elif pstatus == "fail":
                                            _dlog("EXEC", f"{pname} FAILED — {ptime:.0f}s {perr} task={task_key}")
                                    elif evt == "agent_tool":
                                        aname = rdata.get("name", "")
                                        adetail = rdata.get("detail", "")[:60]
                                        _dlog("EXEC", f"agent: {aname} {adetail}")
                                elif tool == "run_task_with_qa":
                                    tk = rdata.get("task_key", "")
                                    if not tk:
                                        for evts in reversed(list(_task_progress.values())):
                                            if evts:
                                                tk = evts[-1].get("task_key", "")
                                                if tk:
                                                    break
                                    if tk:
                                        _task_progress.setdefault(tk, []).append(
                                            {"_result": True, **rdata}
                                        )
                            except json.JSONDecodeError:
                                pass
                except OSError:
                    pass
                time.sleep(0.2)

        _bg_thread = threading.Thread(target=_bg_read_results, daemon=True)
        _bg_thread.start()

        async for message in query(prompt=pilot_prompt, options=agent_opts):
            if isinstance(message, ResultMessage):
                _dlog("DONE", f"ResultMessage is_error={getattr(message, 'is_error', '?')}")
                _safe_stop_active_display()
            elif hasattr(message, "session_id") and hasattr(message, "is_error"):
                pass
            elif AssistantMessage and isinstance(message, AssistantMessage):
                for block in message.content:
                    if TextBlock and isinstance(block, TextBlock) and block.text:
                        text = block.text.strip()
                        if not text:
                            continue

                        if _last_tool_name and "finish_run" in _last_tool_name:
                            continue

                        _dlog(_current_debug_phase, f"text: {text[:120]}")

                        if text.startswith("```") or text.startswith("{"):
                            continue
                        _lower = text.lower()
                        if any(filler in _lower for filler in [
                            "let me", "i'll ", "i will ", "now let me",
                            "let me save", "let me get", "let me check",
                            "in parallel", "save the run state",
                        ]):
                            continue
                        if _lower.startswith(("good.", "good,", "great.", "here's", "here is", "the state is clear")):
                            continue
                        if text.startswith("|"):
                            continue

                        if "EXECUTION PLAN" in text or "PLAN UPDATE" in text:
                            _safe_console_print(f"\n{text}", style="cyan")
                        elif any(marker in text.lower() for marker in [
                            "passed", "failed", "now executing", "starting",
                            "retrying", "aborting", "all tasks", "integration",
                        ]):
                            _safe_console_print(f"  {text}")
                        else:
                            _safe_console_print(f"  {text}", style="dim")

                    elif ToolUseBlock and isinstance(block, ToolUseBlock):
                        _last_tool_name = block.name
                        tool_name = block.name.replace("mcp__otto-pilot__", "")
                        inputs = block.input or {}
                        detail_parts = []
                        if "task_key" in inputs:
                            detail_parts.append(f"task={inputs['task_key'][:8]}")
                        if inputs.get("hint"):
                            detail_parts.append(f'hint="{str(inputs["hint"])[:50]}"')
                        if "phase" in inputs:
                            detail_parts.append(f"phase={inputs['phase']}")
                        detail = " ".join(detail_parts)
                        if tool_name in ("get_run_state", "save_run_state"):
                            _dlog("PLAN", f"{tool_name} {detail}")
                        elif tool_name in ("run_task_with_qa", "abort_task"):
                            _dlog("EXEC", f"{tool_name} {detail}")
                        elif tool_name == "finish_run":
                            _dlog("REPORT", f"{tool_name} {detail}")
                        else:
                            _dlog(_current_debug_phase, f"{tool_name} {detail}")
                        _safe_print_tool_call(block)
                    elif ToolResultBlock and isinstance(block, ToolResultBlock):
                        raw = block.content
                        result_preview = ""
                        if isinstance(raw, str):
                            result_preview = raw[:100]
                        elif isinstance(raw, list):
                            parts = []
                            for item in raw:
                                if isinstance(item, str):
                                    parts.append(item)
                                elif hasattr(item, "text"):
                                    parts.append(item.text)
                            result_preview = " ".join(parts)[:100]
                        if block.is_error:
                            _dlog(_current_debug_phase, f"ERROR: {result_preview}")
                        else:
                            try:
                                rdata = json.loads(result_preview if len(result_preview) < 100 else str(raw)[:200])
                                if isinstance(rdata, dict):
                                    if "success" in rdata:
                                        s = "PASSED" if rdata["success"] else "FAILED"
                                        cost = rdata.get("cost_usd", 0)
                                        err = rdata.get("error", "")[:50]
                                        _dlog(_current_debug_phase, f"result: {s} cost=${cost:.2f} {err}")
                                    elif "done" in rdata:
                                        _dlog("REPORT", "run complete")
                                    else:
                                        _dlog(_current_debug_phase, f"result: {result_preview}")
                                else:
                                    _dlog(_current_debug_phase, f"result: {result_preview}")
                            except (json.JSONDecodeError, TypeError):
                                _dlog(_current_debug_phase, f"result: {result_preview}")
                        _resolved_tool = (_last_tool_name or "").replace("mcp__otto-pilot__", "")
                        if _resolved_tool not in _NOISE_TOOLS and _last_tool_name != "ToolSearch":
                            _safe_print_tool_result(block)
                        _last_tool_name = None

        _bg_reader_running = False
        _bg_thread.join(timeout=2)

    except Exception as pilot_err:
        _safe_stop_active_display()
        try:
            _dlog("ERROR", f"pilot crashed: {pilot_err}")
        except Exception:
            pass
        _safe_console_print(f"\n  [yellow]Warning: Pilot agent error: {pilot_err}[/yellow]")
        _safe_console_print("  Proceeding to summary -- completed tasks are still merged.", style="dim")
    finally:
        try:
            _bg_reader_running = False  # noqa: F841
        except NameError:
            pass
        try:
            _debug_fh.close()
        except Exception:
            pass
        if mcp_script_path.exists():
            mcp_script_path.unlink()

    # Post-run: calculate results
    final_tasks = load_tasks(tasks_file)
    results: list[tuple[dict, bool]] = []
    total_cost = 0.0
    for t in final_tasks:
        task_key = t.get("key", "")
        if task_key not in pending_keys:
            continue
        if t.get("status") in ("passed", "failed", "blocked"):
            results.append((t, t.get("status") == "passed"))
            total_cost += t.get("cost_usd", 0.0)
        elif t.get("status") == "running":
            update_task(tasks_file, t["key"], status="failed",
                        error="pilot crashed during execution")
            results.append((t, False))
            total_cost += t.get("cost_usd", 0.0)
        elif t.get("status") == "pending":
            results.append((t, False))

    run_duration = time.monotonic() - run_start

    try:
        _print_summary(results, run_duration, total_cost=total_cost, task_progress=_task_progress)
    except Exception:
        pass

    # Record run history
    try:
        history_file = project_dir / "otto_logs" / "run-history.jsonl"
        history_file.parent.mkdir(parents=True, exist_ok=True)
        tasks_passed = sum(1 for _, s in results if s)
        tasks_failed = sum(1 for _, s in results if not s)
        failure_summary = ""
        if tasks_failed > 0:
            failed_tasks = [(t, s) for t, s in results if not s]
            if len(failed_tasks) == 1:
                ft = failed_tasks[0][0]
                failure_summary = f"task #{ft.get('id', '?')} failed: {ft.get('error', 'unknown')[:40]}"
            else:
                failure_summary = f"{tasks_failed} tasks failed"
        commit_sha = ""
        try:
            sha_result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=project_dir, capture_output=True, text=True,
            )
            if sha_result.returncode == 0:
                commit_sha = sha_result.stdout.strip()
        except Exception:
            pass
        from datetime import datetime as _dt
        entry = {
            "timestamp": _dt.now().isoformat(timespec="seconds"),
            "tasks_total": len(results),
            "tasks_passed": tasks_passed,
            "tasks_failed": tasks_failed,
            "cost_usd": round(total_cost, 4),
            "time_s": round(run_duration, 1),
            "commit": commit_sha,
            "failure_summary": failure_summary,
        }
        with open(history_file, "a") as hf:
            hf.write(json.dumps(entry) + "\n")
    except Exception:
        pass

    any_failed = any(not s for _, s in results)
    return 1 if any_failed else 0


async def run_piloted(
    config: dict[str, Any],
    tasks_file: Path,
    project_dir: Path,
) -> int:
    """LLM-driven orchestrator that replaces run_all().

    Handles pre-flight setup (lock, branch, baseline) and cleanup, then
    delegates the pilot agent loop to _run_pilot_core() which has the
    taste-fixed display code.

    Returns exit code (0=all passed, 1=any failed, 2=error).
    """
    import fcntl
    import signal

    default_branch = config["default_branch"]

    # Acquire process lock (advisory flock — automatically released on process exit)
    lock_path = git_meta_dir(project_dir) / "otto.lock"
    lock_path.touch()
    lock_fh = open(lock_path, "r")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        console.print("Another otto process is running", style="red")
        return 2

    # Clean up stale task lock files from crashed runs
    tasks_lock = project_dir / ".tasks.lock"
    if tasks_lock.exists():
        try:
            tasks_lock.unlink()
        except OSError:
            pass

    # Signal handling
    interrupted = False

    def _signal_handler(signum, frame):
        nonlocal interrupted
        if interrupted:
            console.print("\nForce exit", style="red")
            sys.exit(1)
        interrupted = True
        console.print("\n[yellow]Warning: Interrupted -- pilot will finish current tool call[/yellow]")

    old_sigint = signal.signal(signal.SIGINT, _signal_handler)
    old_sigterm = signal.signal(signal.SIGTERM, _signal_handler)

    try:
        # Pre-flight checks (branch, baseline, stale recovery, deps)
        error_code, pending = preflight_checks(config, tasks_file, project_dir)
        if error_code is not None:
            return error_code

        # Delegate to the shared core (taste-fixed display)
        exit_code = await _run_pilot_core(config, tasks_file, project_dir)
        return exit_code

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
