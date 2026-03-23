"""Otto spec generation — agentic spec generation + markdown parsing."""

import asyncio
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

try:
    from claude_agent_sdk import ClaudeAgentOptions, query
    from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
except ImportError:
    from otto._agent_stub import ClaudeAgentOptions, query, ResultMessage
    AssistantMessage = None  # type: ignore[assignment,misc]
    TextBlock = None  # type: ignore[assignment,misc]
    ToolUseBlock = None  # type: ignore[assignment,misc]

try:
    from claude_agent_sdk.types import ThinkingBlock
except (ImportError, AttributeError):
    ThinkingBlock = None  # type: ignore[assignment,misc]

from otto.display import print_agent_tool


def _tool_use_summary(block) -> str:
    """One-line summary of a tool use for logging."""
    inputs = block.input or {}
    name = block.name
    if name in ("Read", "Glob", "Grep"):
        return inputs.get("file_path") or inputs.get("path") or inputs.get("pattern") or ""
    elif name in ("Edit", "Write"):
        return inputs.get("file_path") or ""
    elif name == "Bash":
        cmd = inputs.get("command") or ""
        return cmd[:120]
    return ""


def _write_log(path: Path, lines: list[str]) -> None:
    """Write log lines to file (best-effort)."""
    try:
        path.write_text("\n".join(lines))
    except OSError:
        pass


# Regex to strip numbered ("1. ", "1) ") or bullet ("- ", "* ") prefixes
_LIST_PREFIX_RE = re.compile(r"^\s*(?:\d+[.)]\s*|[-*]\s+)")


def _parse_spec_output(text: str) -> list:
    """Parse LLM output into a list of spec items.

    Each item is either a plain string (backward compat) or a dict with
    {text, verifiable}.

    Format per line (after stripping numbering):
      [verifiable] description text
      [visual] description text
      plain description text  (treated as verifiable)
    """
    _VERIFIABLE_RE = re.compile(r"^\[verifiable\]\s*", re.IGNORECASE)
    _VISUAL_RE = re.compile(r"^\[visual\]\s*", re.IGNORECASE)

    items = []
    for line in text.splitlines():
        stripped = _LIST_PREFIX_RE.sub("", line).strip()
        if not stripped:
            continue

        # Check for [visual] prefix
        if _VISUAL_RE.match(stripped):
            text_part = _VISUAL_RE.sub("", stripped).strip()
            if text_part:
                items.append({"text": text_part, "verifiable": False})
            continue

        # Check for [verifiable] prefix
        if _VERIFIABLE_RE.match(stripped):
            stripped = _VERIFIABLE_RE.sub("", stripped).strip()

        if stripped:
            items.append({"text": stripped, "verifiable": True})

    return items


def generate_spec(prompt: str, project_dir: Path) -> list:
    """Generate a spec for a single task using an agentic QA engineer.

    Returns a list of spec items (dicts with text/verifiable),
    or empty list on failure.
    """
    spec, _cost = asyncio.run(_run_spec_agent(prompt, project_dir))
    return spec


async def _run_spec_agent(prompt: str, project_dir: Path) -> list:
    """Run the spec generation agent.

    Uses a structured system prompt for constraint faithfulness,
    with a compliance self-check step before output.
    """
    with tempfile.NamedTemporaryFile(suffix=".txt", prefix="otto_spec_", delete=False) as temp_file:
        spec_file = Path(temp_file.name)

    # Get file tree for minimal project context (no deep exploration needed)
    file_tree_result = subprocess.run(
        ["git", "ls-files"], cwd=project_dir, capture_output=True, text=True,
    )
    file_tree = file_tree_result.stdout.strip() if file_tree_result.returncode == 0 else ""

    system_prompt = """\
You generate acceptance specs. Your output is the contract a coding agent must satisfy.

Rules:
- Describe OBSERVABLE BEHAVIOR — what the user sees and experiences.
- You MAY reference existing user-facing surfaces to anchor placement or context
  (for example: "appears in the Weather Details panel", "shown on the checkout page").
- Do NOT prescribe implementation: no file names, no internal component/module/class/function names,
  no framework patterns, no "use TypeScript interfaces", no "add data-testid",
  no "create a lib module in src/lib/". The coding agent decides how to build it.
- Do NOT include testing/build requirements ("tests pass", "TypeScript compiles", "no regressions").
  Those are enforced by the verification system, not by spec items.
- Preserve every user constraint exactly. Do not weaken thresholds or add conditions.
- Cover the full behavior depth that matters for the task: happy path, error handling,
  negative cases ("does not", "cannot", "is not shown"), edge cases, and retained behavior
  when existing functionality must keep working.
- Keep specs tight. Most tasks land around 5-12 items; use fewer for small fixes,
  and more when the behavior genuinely has multiple distinct states.
  Each item should be a distinct user-visible behavior, not a sub-detail of another item.
- If the task references an external site, app, or example ("like car.com"), inspect it.
  Convert what you observe into concrete criteria about layout, hierarchy, copy, interactions,
  spacing, or styling. Do not write vague "similar to X" criteria without stating what that means.

Output format — one item per line:
  [verifiable] concrete, testable behavioral criterion
  [visual] subjective criterion (style, UX, aesthetics)

Write only criteria lines to the file — no headings, notes, or prose."""

    agent_prompt = f"""TASK: {prompt}

PROJECT FILES (for context — do not prescribe file structure in specs):
{file_tree}

Instructions:
- Use the project files to understand existing user-facing surfaces and current behavior.
- If the task references an external site/app/example, inspect it before writing the spec
  and emit concrete visual/behavioral criteria derived from what you observed.
- Include the necessary happy path, error cases, negative cases, edge cases, and retained behavior.
- Reference user-visible placement when useful, but do not mention internal implementation names or file structure.

Write acceptance criteria to: {spec_file}
Write only criteria lines to the file — no headings, notes, or prose."""

    # Persistent log for debugging
    log_dir = project_dir / "otto_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []

    spec_cost = 0.0
    try:
        agent_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=str(project_dir),
            system_prompt=system_prompt,
            setting_sources=["user", "project"],
            env=dict(os.environ),
        )

        num_turns = 0
        result_msg = None
        async for message in query(prompt=agent_prompt, options=agent_opts):
            if isinstance(message, ResultMessage):
                result_msg = message
                raw_cost = getattr(message, "total_cost_usd", None)
                if isinstance(raw_cost, (int, float)):
                    spec_cost = float(raw_cost)
            elif hasattr(message, "session_id") and hasattr(message, "is_error"):
                result_msg = message
                raw_cost = getattr(message, "total_cost_usd", None)
                if isinstance(raw_cost, (int, float)):
                    spec_cost = float(raw_cost)
            elif AssistantMessage and isinstance(message, AssistantMessage):
                num_turns += 1
                for block in message.content:
                    if ThinkingBlock and isinstance(block, ThinkingBlock):
                        thinking = getattr(block, "thinking", "")
                        if thinking:
                            log_lines.append(f"[thinking] {thinking}")
                    elif TextBlock and isinstance(block, TextBlock) and block.text:
                        # Don't print spec agent narration — log only
                        log_lines.append(block.text)
                    elif ToolUseBlock and isinstance(block, ToolUseBlock):
                        print_agent_tool(block, quiet=True)
                        log_lines.append(f"● {block.name}  {_tool_use_summary(block)}")

        # Check if agent reported an error
        if result_msg and getattr(result_msg, "is_error", False):
            error_detail = getattr(result_msg, "result", None) or "unknown error"
            raise RuntimeError(f"Spec agent error: {error_detail}")

        # Check if agent never started (no result message at all)
        if num_turns == 0 and result_msg is None:
            raise RuntimeError("Spec agent produced no output — agent may have failed to start")

    except Exception as e:
        print(f"  spec agent error: {e}", flush=True)
        log_lines.append(f"ERROR: {e}")
        _write_log(log_dir / "spec-agent.log", log_lines)
        # Clean up temp file on error path
        if spec_file.exists():
            spec_file.unlink(missing_ok=True)
        return [], spec_cost

    _write_log(log_dir / "spec-agent.log", log_lines)

    # Read the spec file
    if spec_file.exists():
        text = spec_file.read_text()
        spec_file.unlink()
        return _parse_spec_output(text), spec_cost

    # Clean up temp file if it doesn't exist (shouldn't happen, but be safe)
    spec_file.unlink(missing_ok=True)
    return [], spec_cost


def parse_markdown_tasks(md_file: Path, project_dir: Path) -> list[dict]:
    """Parse a markdown file into structured tasks using an agentic PM.

    The agent explores the project, reads the markdown, and outputs a JSON
    array of tasks with prompts and specs.

    Returns list of task dicts.
    Raises ValueError on parse failure or invalid task structure.
    """
    return asyncio.run(_run_markdown_agent(md_file, project_dir))


async def _run_markdown_agent(md_file: Path, project_dir: Path) -> list[dict]:
    """Run the markdown parsing agent."""
    md_content = md_file.read_text()
    with tempfile.NamedTemporaryFile(suffix=".json", prefix="otto_tasks_", delete=False) as temp_file:
        output_file = Path(temp_file.name)

    log_dir = project_dir / "otto_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []

    agent_prompt = f"""You are a senior engineer breaking a feature document into coding tasks.

DOCUMENT:
{md_content}

You are working in {project_dir}. Explore the codebase as needed to understand what exists.

Write a JSON array to: {output_file}

Each element should have:
- "prompt": a clear, actionable description of what to implement
- "spec": concrete, testable acceptance criteria (as many as needed)
- "depends_on": indices (0-based) of tasks this one requires, or [] if independent.
  Task B depends on A if B needs code/APIs/data that A creates.
  When unsure, include the dependency (safe default).

RULES:
- Each task is a complete, self-contained unit of work
- Do NOT create separate tasks for writing tests — test expectations belong in the spec
- One heading/section = one task
- Spec items describe BEHAVIOR, not implementation details (no file names, component names, framework patterns)
- Include: happy path, error handling, negative ("does NOT"), edge cases
- If a task modifies existing functionality, include regression criteria

Write ONLY a valid JSON array to {output_file}. No prose.
Example: [{{"prompt": "Add search", "spec": ["search works", "case-insensitive"], "depends_on": []}}, {{"prompt": "Add search filters", "spec": ["filters work"], "depends_on": [0]}}]"""

    try:
        agent_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=str(project_dir),
        )

        async for message in query(prompt=agent_prompt, options=agent_opts):
            if AssistantMessage and isinstance(message, AssistantMessage):
                for block in message.content:
                    if TextBlock and isinstance(block, TextBlock) and block.text:
                        log_lines.append(block.text)
                    elif ToolUseBlock and isinstance(block, ToolUseBlock):
                        print_agent_tool(block, quiet=True)
                        log_lines.append(f"\u25cf {block.name}  {_tool_use_summary(block)}")
    except Exception as e:
        log_lines.append(f"ERROR: {e}")
        _write_log(log_dir / "markdown-agent.log", log_lines)
        output_file.unlink(missing_ok=True)
        raise ValueError(f"Failed to parse markdown tasks: {e}") from e

    _write_log(log_dir / "markdown-agent.log", log_lines)

    output = output_file.read_text().strip()
    output_file.unlink()
    if not output:
        raise ValueError("Agent did not write the output file")

    # Extract JSON from markdown fences if present
    fence_match = re.search(r"```(?:json)?\n(.*?)```", output, re.DOTALL)
    if fence_match:
        output = fence_match.group(1).strip()

    try:
        tasks = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse tasks JSON: {exc}") from exc

    if not isinstance(tasks, list):
        raise ValueError("Failed to parse tasks JSON: expected a JSON array")

    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"Task {i} is not a dict")
        if not task.get("prompt"):
            raise ValueError(f"Task {i} missing 'prompt'")
        # Normalize depends_on: ensure it's a list of ints or absent
        deps = task.get("depends_on")
        if deps is not None:
            if not isinstance(deps, list):
                raise ValueError(f"Task {i} 'depends_on' must be a list")
            task["depends_on"] = [int(d) for d in deps]

    return tasks
