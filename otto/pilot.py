"""Otto pilot — LLM-driven orchestrator that replaces run_all().

The pilot is a long-running Claude agent that drives task execution by calling
otto functions exposed as MCP tools. It decides what to run next, reads results,
analyzes failures, and makes strategic decisions like a tech lead.
"""

import json
import subprocess
import sys
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
_PRIMARY_TOOLS = {
    "run_holistic_testgen", "run_per_task_testgen",
    "run_coding_agent", "run_coding_agents",
    "run_integration_gate_tool", "run_architect_tool",
    "finish_run",
}
_SECONDARY_TOOLS = {
    "get_run_state", "run_verify", "read_verify_output",
    "merge_task", "update_task_status", "abort_task",
}
_NOISE_TOOLS = {
    "save_run_state", "ToolSearch",
}

_TOOL_DISPLAY = {
    "get_run_state": ("📋", "Loading task state"),
    "run_holistic_testgen": ("🧪", "Generating tests (holistic)"),
    "run_per_task_testgen": ("🧪", "Generating tests (per-task)"),
    "run_coding_agent": ("🔨", "Coding"),
    "run_coding_agents": ("⚡", "Coding (parallel)"),
    "run_verify": ("🔍", "Verifying"),
    "read_verify_output": ("📖", "Reading verify output"),
    "merge_task": ("🔀", "Merging"),
    "update_task_status": ("📝", "Updating status"),
    "run_integration_gate_tool": ("🔗", "Integration gate"),
    "run_architect_tool": ("🏗️", "Re-running architect"),
    "abort_task": ("❌", "Aborting"),
    "save_run_state": ("💾", "Saving state"),
    "finish_run": ("🏁", "Done"),
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
        if tool_name in ("run_holistic_testgen", "run_per_task_testgen",
                          "run_coding_agent", "run_coding_agents",
                          "run_integration_gate_tool", "run_architect_tool"):
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
    if tool_name in ("run_holistic_testgen", "run_per_task_testgen",
                      "run_coding_agent", "run_coding_agents",
                      "run_integration_gate_tool", "run_architect_tool"):
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
                rubric_count = t.get("rubric_count", 0)
                status_icon = {
                    "pending": f"{_DIM}○{_RESET}",
                    "running": f"{_CYAN}◉{_RESET}",
                    "passed": f"{_GREEN}✓{_RESET}",
                    "failed": f"{_RED}✗{_RESET}",
                    "blocked": f"{_RED}⊘{_RESET}",
                }.get(status, "?")
                print(
                    f"    {status_icon} #{t.get('id', '?')} "
                    f"{_DIM}[{rubric_count} rubric{deps_str}]{_RESET}  "
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
    from otto.architect import load_design_context

    task_summaries = []
    for t in tasks:
        deps = t.get("depends_on") or []
        deps_str = f" (depends_on: {deps})" if deps else ""
        rubric = t.get("rubric", [])
        rubric_str = f" [{len(rubric)} rubric items]" if rubric else " [no rubric]"
        task_summaries.append(
            f"  #{t['id']} ({t['key']}): {t['prompt'][:80]}{deps_str}{rubric_str}"
        )

    design_summary = load_design_context(project_dir, role="pilot")
    design_section = ""
    if design_summary:
        design_section = f"\n\nARCHITECT DOCS SUMMARY:\n{design_summary}\n"

    return f"""You are a tech lead managing a team of coding agents. You drive execution
by calling the tools available to you. Each tool wraps an internal otto function.

PROJECT DIR: {project_dir}
CONFIG: max_retries={config.get('max_retries', 3)}, test_command={config.get('test_command')}, max_parallel={config.get('max_parallel', 3)}

PENDING TASKS:
{chr(10).join(task_summaries)}
{design_section}

═══════════════════════════════════════════════════════
PHASE 1: PLAN (mandatory — do this BEFORE calling any execution tools)
═══════════════════════════════════════════════════════

After calling get_run_state, output your execution plan in this exact format:

```
── EXECUTION PLAN ──────────────────────────────────────
Testgen: holistic (all {len(tasks)} tasks)
Execution order:
  Group 1 (parallel): #X, #Y — no shared files
  Group 2 (serial):   #Z — depends on #X
Estimated steps: N testgen + M coding + integration gate
Risk assessment: [any concerns about task overlap, complexity]
────────────────────────────────────────────────────────
```

Consider:
- Which tasks share files? (check depends_on, read task prompts for clues like "modify cli.py")
- If tasks might modify the same file but have no declared depends_on, run them SERIALLY to be safe
- Order: simpler/independent tasks first, complex/dependent tasks later
- For 5+ tasks: identify critical path, parallelize only truly independent work

═══════════════════════════════════════════════════════
PHASE 2: EXECUTE (follow your plan, adapt when needed)
═══════════════════════════════════════════════════════

1. Generate tests: run_holistic_testgen for consistency. Fall back to run_per_task_testgen if holistic fails.
2. Run coding: follow your execution plan. run_coding_agents for parallel groups, run_coding_agent for serial.
3. run_coding_agent / run_coding_agents handle the FULL lifecycle: testgen → code → verify → retry → merge.
4. If a task fails: read_verify_output, analyze, retry with targeted hint.
5. After all pass: run_integration_gate.

When something unexpected happens (failure, merge conflict, test bug), output:

```
── PLAN UPDATE ─────────────────────────────────────────
What happened: [describe the failure]
Decision: [what you're doing differently]
Updated order: [new execution sequence]
────────────────────────────────────────────────────────
```

═══════════════════════════════════════════════════════
PHASE 3: REPORT
═══════════════════════════════════════════════════════

After all tasks complete or are aborted, call finish_run with a summary.

RULES:
- PLAN FIRST — never call run_coding_agent before outputting your execution plan
- If a task fails, ALWAYS read_verify_output before deciding to retry
- Never retry with the same approach twice — analyze what went wrong first
- If a task fails 3 times with different approaches, abort it with abort_task
- Track progress: call save_run_state(phase, notes) after every major decision
- When tasks might share files but have no explicit depends_on, default to SERIAL execution
- After all tasks pass or are aborted, call finish_run to complete

AVAILABLE TOOLS:
- get_run_state: Get current status of all tasks
- run_holistic_testgen: Generate tests for multiple tasks at once
- run_per_task_testgen: Generate tests for a single task (fallback)
- run_coding_agent: Run FULL lifecycle for one task (code → verify → merge). Optional hint for retries.
- run_coding_agents: Run FULL lifecycle for multiple tasks in parallel (with merge)
- read_verify_output: Read verification output summary for a failed task
- run_integration_gate: Run cross-feature integration tests
- run_architect: Re-run architect to refresh conventions
- abort_task: Give up on a task with a reason
- save_run_state(phase, notes): Persist state for context recovery (auto-populates task details)
- finish_run: Signal that the run is complete
- merge_task: [Advanced] Manual merge with rebase retry
- run_verify: [Advanced] Manual verification in disposable worktree
- update_task_status: [Advanced] Manually update task status

Start by calling get_run_state, then output your execution plan.
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
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Ensure the otto package is importable
sys.path.insert(0, {repr(str(Path(__file__).resolve().parent.parent))})

CONFIG = json.loads({repr(config_json)})
TASKS_FILE = Path({repr(str(tasks_file))})
PROJECT_DIR = Path({repr(str(project_dir))})

# Capture run-start SHA for scoped cross-task review diffs
_start_result = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    cwd=PROJECT_DIR, capture_output=True, text=True,
)
RUN_START_SHA = _start_result.stdout.strip() if _start_result.returncode == 0 else None

# Track which test files were generated in THIS run (not stale from prior runs)
_CURRENT_RUN_TESTS: set[str] = set()

# Track pilot-level retry attempts and accumulated cost per task
# (run_task resets these in tasks.yaml, so we accumulate across calls here)
_PILOT_ATTEMPTS: dict[str, int] = {{}}
_PILOT_COST: dict[str, float] = {{}}

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
            "rubric_count": len(t.get("rubric", [])),
            "cost_usd": t.get("cost_usd", 0),
            "error": t.get("error"),
        }})
    return json.dumps(summary, indent=2)


@mcp.tool()
async def run_holistic_testgen(task_keys: list[str]) -> str:
    """Generate tests for multiple tasks at once using holistic testgen.
    Returns JSON mapping task_key to test file path (or null if failed)."""
    from otto.tasks import load_tasks
    from otto.testgen import build_blackbox_context, run_holistic_testgen as _holistic

    tasks = load_tasks(TASKS_FILE)
    selected = [t for t in tasks if t.get("key") in task_keys and t.get("rubric")]
    if not selected:
        return json.dumps({{"error": "no tasks with rubrics found for given keys"}})

    all_hints = " ".join(
        t["prompt"] + " " + " ".join(t.get("rubric", []))
        for t in selected
    )
    ctx = build_blackbox_context(PROJECT_DIR, task_hint=all_hints)
    results = await _holistic(selected, PROJECT_DIR, ctx, quiet=True)

    # Copy generated files from .git/otto/testgen/ into tests/ and commit
    # so run_coding_agent can find them and worktrees include them.
    import shutil as _shutil
    committed_any = False

    # Commit conftest.py first if holistic testgen created it
    conftest = PROJECT_DIR / "tests" / "conftest.py"
    if conftest.exists():
        subprocess.run(
            ["git", "add", str(conftest.relative_to(PROJECT_DIR))],
            cwd=PROJECT_DIR, capture_output=True,
        )
        committed_any = True

    for key, path in results.items():
        if path and path.exists():
            dest = PROJECT_DIR / "tests" / path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copy2(str(path), str(dest))
            results[key] = dest
            _CURRENT_RUN_TESTS.add(key)
            subprocess.run(
                ["git", "add", str(dest.relative_to(PROJECT_DIR))],
                cwd=PROJECT_DIR, capture_output=True,
            )
            committed_any = True

    if committed_any:
        subprocess.run(
            ["git", "commit", "-m", "otto: holistic testgen (pilot)"],
            cwd=PROJECT_DIR, capture_output=True,
        )

    # Validate test quality
    from otto.test_validation import validate_test_quality
    for key, path in results.items():
        if path and Path(str(path)).exists():
            qw = validate_test_quality(Path(str(path)), PROJECT_DIR)
            qe = [w for w in qw if w.severity == "error"]
            if qe:
                _emit_result("test_quality_warning", {{"key": key, "errors": [str(w) for w in qe]}})

    emit_data = {{k: str(v) if v else None for k, v in results.items()}}
    _emit_result("run_holistic_testgen", emit_data)
    return json.dumps(emit_data)


@mcp.tool()
async def run_per_task_testgen(task_key: str) -> str:
    """Generate tests for a single task. Returns test file path or null."""
    from otto.tasks import load_tasks
    from otto.testgen import build_blackbox_context, run_testgen_agent

    tasks = load_tasks(TASKS_FILE)
    task = next((t for t in tasks if t.get("key") == task_key), None)
    if not task or not task.get("rubric"):
        return json.dumps({{"error": f"task {{task_key}} not found or has no rubric"}})

    rubric = task["rubric"]
    hint = task["prompt"] + "\\n" + "\\n".join(rubric)
    ctx = build_blackbox_context(PROJECT_DIR, task_hint=hint)
    path, _ = await run_testgen_agent(rubric, task_key, ctx, PROJECT_DIR, quiet=True)
    return json.dumps({{"test_file": str(path) if path else None}})


@mcp.tool()
async def run_coding_agent(task_key: str, hint: str = "") -> str:
    """Run a coding agent for one task. Optional hint for retry guidance.
    Returns JSON with success status and cost."""
    from otto.runner import run_task
    from otto.tasks import load_tasks, update_task

    tasks = load_tasks(TASKS_FILE)
    task = next((t for t in tasks if t.get("key") == task_key), None)
    if not task:
        return json.dumps({{"error": f"task {{task_key}} not found"}})

    if hint:
        update_task(TASKS_FILE, task_key, feedback=hint)
        task["feedback"] = hint

    # Track pilot-level attempts (run_task resets attempts in tasks.yaml)
    _PILOT_ATTEMPTS[task_key] = _PILOT_ATTEMPTS.get(task_key, 0) + 1

    # Use pre-generated holistic test only if it was generated in THIS run
    pre_test = None
    if task_key in _CURRENT_RUN_TESTS:
        test_file = PROJECT_DIR / "tests" / f"test_otto_{{task_key}}.py"
        if test_file.exists():
            pre_test = test_file

    # Build sibling test exclusion list — other tasks' tests that can't pass yet
    from pathlib import Path as _Path
    sibling_tests = []
    for other_key in _CURRENT_RUN_TESTS:
        if other_key != task_key:
            sibling_path = _Path("tests") / f"test_otto_{{other_key}}.py"
            sibling_tests.append(sibling_path)

    # Capture pre-task SHA for diff
    pre_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_DIR, capture_output=True, text=True,
    ).stdout.strip()

    success = await run_task(
        task, CONFIG, PROJECT_DIR, TASKS_FILE,
        pre_generated_test=pre_test,
        sibling_test_files=sibling_tests or None,
    )

    # Build diff summary
    diff_summary = _build_diff_summary(PROJECT_DIR, pre_sha)

    # Read verify output for failures
    verify_snippet = ""
    if not success:
        log_dir = PROJECT_DIR / "otto_logs" / task_key
        if log_dir.exists():
            verify_logs = sorted(log_dir.glob("attempt-*-verify.log"), reverse=True)
            if verify_logs:
                content = verify_logs[0].read_text()
                # Extract last 20 lines (most useful part)
                lines = content.strip().splitlines()
                verify_snippet = "\\n".join(lines[-20:])

    # Reload to get updated cost/status
    tasks = load_tasks(TASKS_FILE)
    updated = next((t for t in tasks if t.get("key") == task_key), {{}})

    # Accumulate cost across pilot-level retries (run_task resets cost each call)
    new_cost = updated.get("cost_usd", 0)
    _PILOT_COST[task_key] = _PILOT_COST.get(task_key, 0) + new_cost
    accumulated_cost = _PILOT_COST[task_key]
    pilot_attempts = _PILOT_ATTEMPTS[task_key]

    # Write back accumulated cost and pilot-level attempts
    update_task(TASKS_FILE, task_key,
                cost_usd=accumulated_cost, attempts=pilot_attempts)

    result_data = {{
        "success": success,
        "status": updated.get("status"),
        "cost_usd": accumulated_cost,
        "error": updated.get("error"),
        "diff": diff_summary,
        "verify_output": verify_snippet,
    }}
    _emit_result("run_coding_agent", result_data)
    return json.dumps(result_data)


@mcp.tool()
async def run_coding_agents(task_keys: list[str]) -> str:
    """Run coding agents for multiple tasks in parallel.
    Returns JSON with per-task results."""
    from otto.runner import run_task, _setup_task_worktree, _teardown_task_worktree, merge_to_default
    from otto.tasks import load_tasks, update_task

    tasks = load_tasks(TASKS_FILE)
    selected = [t for t in tasks if t.get("key") in task_keys]

    default_branch = CONFIG.get("default_branch", "main")

    # Ensure we're on the default branch before creating worktrees
    subprocess.run(
        ["git", "checkout", default_branch],
        cwd=PROJECT_DIR, capture_output=True,
    )

    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_DIR, capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Build pre-generated test map and sibling exclusions
    pre_tests = {{}}
    for t in selected:
        key = t.get("key")
        if key in _CURRENT_RUN_TESTS:
            tf = PROJECT_DIR / "tests" / f"test_otto_{{key}}.py"
            if tf.exists():
                pre_tests[key] = tf

    async def _run_one(t, wt_dir, pre_test, siblings):
        success = await run_task(
            t, CONFIG, PROJECT_DIR, TASKS_FILE,
            work_dir=wt_dir,
            pre_generated_test=pre_test,
            sibling_test_files=siblings,
        )
        return {{"success": success, "status": "passed" if success else "failed"}}

    results = {{}}
    coros = []
    for t in selected:
        key = t.get("key")
        wt_dir = _setup_task_worktree(PROJECT_DIR, key, base_sha)
        # Remap pre-generated test into worktree
        wt_pre_test = None
        if key in pre_tests:
            rel = pre_tests[key].relative_to(PROJECT_DIR)
            wt_test_path = wt_dir / rel
            if wt_test_path.exists():
                wt_pre_test = wt_test_path
        # Build sibling test file list for exclusion
        sibling_paths = [
            p.relative_to(PROJECT_DIR)
            for k, p in pre_tests.items()
            if k != key
        ]
        coros.append(_run_one(t, wt_dir, wt_pre_test, sibling_paths or None))

    parallel_results = await asyncio.gather(*coros, return_exceptions=True)

    # Sequential merge for successful tasks (same pattern as runner.run_all)
    for i, result in enumerate(parallel_results):
        t = selected[i]
        _teardown_task_worktree(PROJECT_DIR, t["key"])
        if isinstance(result, Exception):
            results[t["key"]] = {{"success": False, "error": str(result)}}
        else:
            if result.get("success"):
                # Merge with rebase retry
                merged = _merge_task_branch_to_default(
                    PROJECT_DIR, t["key"], default_branch,
                )
                if not merged:
                    update_task(TASKS_FILE, t["key"],
                                status="failed",
                                error="merge conflict after parallel execution")
                    results[t["key"]] = {{"success": False, "error": "merge conflict"}}
                else:
                    results[t["key"]] = result
            else:
                results[t["key"]] = result
        # Clean up worktree branch if merge didn't delete it
        subprocess.run(
            ["git", "branch", "-D", f"otto/{{t['key']}}"],
            cwd=PROJECT_DIR, capture_output=True,
        )

    # Ensure we're back on the default branch after merging
    subprocess.run(
        ["git", "checkout", default_branch],
        cwd=PROJECT_DIR, capture_output=True,
    )

    _emit_result("run_coding_agents", results)
    return json.dumps(results)


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
def run_verify(task_key: str) -> str:
    """Run verification for a task in a disposable worktree.
    Returns JSON with pass/fail and tier results."""
    from otto.verify import run_verification
    from otto.tasks import load_tasks

    tasks = load_tasks(TASKS_FILE)
    task = next((t for t in tasks if t.get("key") == task_key), None)
    if not task:
        return json.dumps({{"error": f"task {{task_key}} not found"}})

    candidate_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_DIR, capture_output=True, text=True, check=True,
    ).stdout.strip()

    result = run_verification(
        project_dir=PROJECT_DIR,
        candidate_sha=candidate_sha,
        test_command=CONFIG.get("test_command"),
        verify_cmd=task.get("verify"),
        timeout=CONFIG.get("verify_timeout", 300),
    )
    tiers = [
        {{"tier": t.tier, "passed": t.passed, "skipped": t.skipped,
         "output_preview": t.output[:500] if t.output else ""}}
        for t in result.tiers
    ]
    return json.dumps({{"passed": result.passed, "tiers": tiers}})


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
def update_task_status(task_key: str, status: str, error: str = "") -> str:
    """Update a task's status and optional error message."""
    from otto.tasks import update_task
    updates = {{"status": status}}
    if error:
        updates["error"] = error
    update_task(TASKS_FILE, task_key, **updates)
    return json.dumps({{"ok": True}})


@mcp.tool()
async def run_integration_gate_tool() -> str:
    """Run cross-feature integration tests on all passed tasks.
    Returns JSON with pass/fail status."""
    from otto.runner import _run_integration_gate
    from otto.tasks import load_tasks

    tasks = load_tasks(TASKS_FILE)
    passed = [t for t in tasks if t.get("status") == "passed"]
    if len(passed) < 2:
        return json.dumps({{"skipped": True, "reason": "fewer than 2 passed tasks"}})

    # Retry once on API/transient errors
    try:
        result = await _run_integration_gate(
            passed, CONFIG, PROJECT_DIR, run_start_sha=RUN_START_SHA,
        )
    except Exception as exc:
        import traceback
        print(f"Integration gate error (retrying in 5s): {{exc}}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        await asyncio.sleep(5)
        try:
            result = await _run_integration_gate(
                passed, CONFIG, PROJECT_DIR, run_start_sha=RUN_START_SHA,
            )
        except Exception as exc2:
            print(f"Integration gate retry also failed: {{exc2}}", file=sys.stderr)
            return json.dumps({{"passed": False, "skipped": False, "error": str(exc2)}})

    gate_data = {{"passed": result, "skipped": result is None}}
    _emit_result("run_integration_gate_tool", gate_data)
    return json.dumps(gate_data)


@mcp.tool()
async def run_architect_tool() -> str:
    """Re-run the architect agent to refresh conventions and docs."""
    from otto.architect import run_architect_agent
    from otto.tasks import load_tasks

    tasks = load_tasks(TASKS_FILE)
    pending = [t for t in tasks if t.get("status") in ("pending", "running")]
    result = await run_architect_agent(pending, PROJECT_DIR, quiet=True)
    return json.dumps({{"success": result is not None}})


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

        # Baseline check
        test_command = config.get("test_command")
        if test_command:
            _log_info("Running baseline check...")
            result = subprocess.run(
                test_command, shell=True, cwd=project_dir,
                capture_output=True, timeout=config["verify_timeout"],
                env=_subprocess_env(),
            )
            if result.returncode != 0:
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

        # Architect phase
        if not config.get("no_architect", False) and len(pending) >= 2:
            from otto.architect import run_architect_agent, is_stale, parse_file_plan
            arch_dir = project_dir / "otto_arch"

            should_run = not arch_dir.exists()
            if arch_dir.exists() and is_stale(project_dir):
                should_run = True
            # Always run if file-plan.md is missing (needed for dependency injection)
            if arch_dir.exists() and not (arch_dir / "file-plan.md").exists():
                should_run = True

            if should_run:
                print(flush=True)
                _log_info("Architect — analyzing codebase")
                arch_spinner = _Spinner("Architect analyzing")
                arch_spinner.start()
                try:
                    arch_path = await run_architect_agent(pending, project_dir, quiet=True)
                    arch_time = arch_spinner.stop()
                    if arch_path:
                        # Show summary of what was produced
                        produced = sorted(f.name for f in arch_path.iterdir() if not f.name.startswith("."))
                        print(f"  {_GREEN}✓{_RESET} Architecture docs ready  {_DIM}{arch_time}{_RESET}", flush=True)
                        print(f"    {_DIM}{', '.join(produced)}{_RESET}", flush=True)
                        has_file_plan = "file-plan.md" in produced
                        if has_file_plan:
                            # Show file-plan summary
                            fp_content = (arch_path / "file-plan.md").read_text()
                            shared_files = set()
                            for line in fp_content.splitlines():
                                stripped = line.strip()
                                if stripped.startswith("- ") and "/" in stripped:
                                    shared_files.add(stripped.lstrip("- ").strip())
                            if shared_files:
                                print(f"    {_DIM}Predicted shared files: {', '.join(sorted(shared_files)[:5])}{_RESET}", flush=True)
                        # Commit conftest.py if generated
                        conftest = project_dir / "tests" / "conftest.py"
                        if conftest.exists():
                            subprocess.run(
                                ["git", "add", str(conftest.relative_to(project_dir))],
                                cwd=project_dir, capture_output=True,
                            )
                            subprocess.run(
                                ["git", "commit", "-m", "otto: architect conftest.py"],
                                cwd=project_dir, capture_output=True,
                            )
                except Exception as e:
                    arch_spinner.stop()
                    _log_warn(f"Architect failed: {e} — continuing")

            # Inject dependencies from file-plan.md
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
        mcp_script_path = Path(tempfile.mktemp(suffix=".py", prefix="otto_pilot_mcp_"))
        mcp_script_path.write_text(mcp_script)

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

            agent_opts = ClaudeAgentOptions(
                permission_mode="bypassPermissions",
                cwd=str(project_dir),
                max_turns=100,
                mcp_servers={"otto-pilot": mcp_server_config},
            )
            if config.get("model"):
                agent_opts.model = config["model"]

            print(flush=True)
            _log_info("Pilot taking control — LLM-driven execution")
            print(f"  {_DIM}The pilot will drive testgen → coding → verify → merge{_RESET}", flush=True)
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

        finally:
            # Clean up
            _debug_fh.close()
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
