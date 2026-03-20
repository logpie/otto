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
_PRIMARY_TOOLS = {"run_coding_agent", "finish_run"}
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
}


class _Spinner:
    """Animated spinner for long-running operations. Shows elapsed time."""

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str):
        self._label = label
        self._running = False
        self._thread = None
        self._start_time = 0.0

    def start(self):
        import sys as _sys
        self._running = True
        self._start_time = time.monotonic()

        def _spin():
            idx = 0
            while self._running:
                elapsed = time.monotonic() - self._start_time
                frame = self._FRAMES[idx % len(self._FRAMES)]
                mins = int(elapsed // 60)
                secs = int(elapsed % 60)
                time_str = f"{mins}:{secs:02d}" if mins else f"{secs}s"
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

    # Build detail string
    detail = ""
    if "task_key" in inputs:
        detail = f"task {inputs['task_key'][:8]}"
    elif "task_keys" in inputs:
        keys = inputs["task_keys"]
        detail = f"{len(keys)} tasks"
    if inputs.get("hint"):
        hint_preview = str(inputs["hint"])[:60]
        detail += f' — hint: "{hint_preview}"'

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

    # Tier 1: primary tools — prominent with separator, start spinner
    print(f"\n  {_DIM}{'─' * 50}{_RESET}", flush=True)
    if detail:
        print(f"  {icon} {_BOLD}{label}{_RESET}  {_DIM}{detail}{_RESET}", flush=True)
    else:
        print(f"  {icon} {_BOLD}{label}{_RESET}", flush=True)

    # Start spinner for long-running primary tools
    if tool_name in ("run_coding_agent",):
        _active_spinner = _Spinner(label)
        _active_spinner.start()


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
    """Build the system prompt for the pilot agent."""
    task_summaries = []
    for t in tasks:
        deps = t.get("depends_on") or []
        deps_str = f" (depends_on: {deps})" if deps else ""
        spec = t.get("spec", [])
        spec_str = f" [{len(spec)} spec items]" if spec else " [no spec]"
        task_summaries.append(
            f"  #{t['id']} ({t['key']}): {t['prompt'][:80]}{deps_str}{spec_str}"
        )

    return f"""You are a tech lead managing coding agents. You drive execution
by calling the tools available to you.

PROJECT DIR: {project_dir}
CONFIG: max_retries={config.get('max_retries', 3)}, test_command={config.get('test_command')}

PENDING TASKS:
{chr(10).join(task_summaries)}

═══════════════════════════════════════════════════════
PHASE 1: PLAN (mandatory — do this BEFORE calling any execution tools)
═══════════════════════════════════════════════════════

Steps:
1. Call get_run_state to see pending tasks
2. Run `git log --oneline` to understand project history and recent changes
3. Read what you need to plan — the coding agent does its own deep exploration
4. Plan execution order:
   - Respect depends_on — run dependencies first
   - Run simpler/independent tasks first
   - Tasks that share files should run serially

═══════════════════════════════════════════════════════
PHASE 2: EXECUTE
═══════════════════════════════════════════════════════

For each task: run_coding_agent. The coding agent handles the FULL lifecycle:
- Reads codebase, plans approach, implements, writes tests, iterates until passing
- On success: code is committed and merged automatically

If a task fails:
1. read_verify_output to understand why
2. Retry with run_coding_agent and a targeted hint
3. If it fails 3 times with different approaches, abort_task

SPEC COMPLIANCE CHECK (after each task passes):
1. Read the diff in the result
2. For [verifiable] spec items: confirm a test exists that exercises it.
   If no test found for a verifiable item, retry: "missing test for spec item #N"
3. For [visual] spec items: review the diff for reasonable implementation.
4. Watch for spec-dodging: meeting a constraint by only handling the easy case
   (e.g., "<300ms" met by only measuring cache hits, not cold fetches)
5. If a spec item was dodged → retry with specific feedback

BEHAVIORAL TESTING (after spec compliance check passes):
Act as a real user — actually use the app end-to-end:

EFFICIENCY TIP: When using chrome-devtools, you can batch multiple DOM interactions
in a single evaluate_script call instead of separate click/fill/snapshot calls:
  evaluate_script("document.querySelector('#btn').click(); return document.querySelector('#result').textContent")
This is much faster than separate tool calls for each action.

For web apps:
- Start the dev server (`npm run dev` / `npm start`) in background
- Wait for it to be ready (curl localhost until 200)
- Navigate to key pages, try core features
- Take screenshots if chrome-devtools MCP is available (take_screenshot)
- Try edge cases: unusual inputs, empty states, common names that might break
- Check visual appearance: does the UI look right? layout broken? text readable?

For CLI tools:
- Run all main commands with realistic inputs
- Try edge cases: empty input, special characters, very long input, boundary values
- Check output formatting: is it readable, correctly aligned, properly formatted?

For APIs:
- Curl all endpoints with real payloads
- Try error cases: bad input, missing fields, unauthorized

TEST MULTI-STEP FLOWS (most bugs hide here):
- If the app has selection + action: does the action respect the current selection?
  (e.g., select item #3, click delete — does it delete #3 or #1?)
- If features interact: test them together, not just in isolation
  (e.g., add 3 cities, select the 3rd, compare — which cities are compared?)
- Test the full lifecycle: create → view → modify → delete, not just create

REGRESSION CHECK (quick — test existing features too):
- After adding a new feature, try 2-3 existing features to verify they still work
- Focus on features that share state or UI with the new feature
- Click every button visible on the page at least once

Focus on things unit tests CAN'T catch:
- Wrong API results (correct code but wrong data, like "New Jersey" → Trinidad)
- Display/formatting issues visible only in real output
- UX consistency: does the new feature work the way existing features set expectations?
- State bugs: does selection persist across actions? Does the right item get modified?
- Integration issues: features that work in isolation but break together

DOCUMENT your findings — write a behavioral test report:
- What you tested (inputs, actions)
- What worked correctly
- What broke or looked wrong
- Include screenshots paths if taken

Save the report to otto_logs/<task_key>/behavioral-test.md
Also print a summary to the user:
  ✓ Behavioral: <what worked>
  ✗ Behavioral: <what broke>

If you find bugs:
- Retry run_coding_agent with the specific bug as hint
- Be concrete: "Searching 'New Jersey' shows only Trinidad results, no US state"
- After the fix, re-run behavioral testing to verify

CLEANUP: Kill any dev servers or background processes you started when done.
Use `kill <pid>` or `pkill -f "next dev"` / `pkill -f "serve.py"` etc.

If the app can't be run (no entry point, build broken), skip and note it.

DOOM-LOOP DETECTION:
If you see the same error 2+ times, STOP and change strategy:
- Different error each time = progress (keep going)
- Same error repeating = doom loop (change approach or abort)
- After 3 total failures on a task, abort with structured report

═══════════════════════════════════════════════════════
PHASE 3: REPORT
═══════════════════════════════════════════════════════

After all tasks complete or are aborted, call finish_run with a summary.

ESCALATION PROTOCOL (when aborting a task):
Write a structured report as the abort reason:
- What was attempted (approaches tried)
- Why each approach failed
- What would need to change to make it work
- Suggested next steps for a human

AVAILABLE TOOLS:
- get_run_state: Get current status of all tasks
- run_coding_agent: Run FULL lifecycle for one task. Optional hint for retries.
- read_verify_output: Read verification output for a failed task
- merge_task: Manual merge with rebase retry
- abort_task: Give up on a task with structured reason
- save_run_state: Persist state for session recovery
- write_task_notes: Write notes about a task for future attempts
- write_learning: Record a cross-task learning
- finish_run: Signal run complete

RULES:
- PLAN FIRST — never call run_coding_agent before planning
- Use Read, Glob, Grep, Bash freely to explore the codebase and verify results
- Use Bash to start/stop dev servers and curl endpoints for behavioral testing
- Do NOT modify project files directly — let the coding agent do that
- Never retry with the same approach twice
- Track progress: call save_run_state after major decisions
- After all tasks pass or are aborted, call finish_run

Start by calling get_run_state, then plan your execution order.
"""


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
            "prompt": t.get("prompt", "")[:80],
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
    if len(content) > 2000:
        content = content[:2000] + "\\n... (truncated)"
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
                "prompt": t.get("prompt", "")[:100],
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
                        # Chrome-devtools: use --isolated --headless to avoid
                        # conflicts with user's browser and other CC sessions
                        if name == "chrome-devtools":
                            srv = dict(srv)
                            args = list(srv.get("args", []))
                            if "--isolated" not in args:
                                args.append("--isolated")
                            if "--headless" not in args:
                                args.append("--headless")
                            if not any(a.startswith("--viewport") for a in args):
                                args.extend(["--viewport", "1280x720"])
                            srv["args"] = args
                        all_mcp_servers[name] = srv
                except (json.JSONDecodeError, OSError):
                    pass

            agent_opts = ClaudeAgentOptions(
                permission_mode="bypassPermissions",
                cwd=str(project_dir),
                max_turns=100,
                mcp_servers=all_mcp_servers,
                setting_sources=["user", "project"],
                env=_subprocess_env(),
                max_buffer_size=10 * 1024 * 1024,  # 10MB — screenshots can be large
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
