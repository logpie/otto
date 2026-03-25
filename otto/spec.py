"""Otto spec generation — agentic spec generation + markdown parsing."""

import asyncio
import json
import os
import re
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

    Each item is a dict with {text, binding, verifiable}.

    Format per line (after stripping numbering):
      [must] concrete requirement            → binding="must", verifiable=True
      [must ◈] visual/subjective requirement → binding="must", verifiable=False
      [should] nice-to-have suggestion       → binding="should", verifiable=True
      [should ◈] visual nice-to-have         → binding="should", verifiable=False
      plain text (no tag)                    → binding="must", verifiable=True

    Backward compat:
      [verifiable] ...                       → binding="must", verifiable=True
      [visual] ...                           → binding="should", verifiable=False
    """
    # Tags with optional ◈ marker for non-verifiable items
    _MUST_VISUAL_RE = re.compile(r"^\[must\s*◈\]\s*", re.IGNORECASE)
    _MUST_RE = re.compile(r"^\[must\]\s*", re.IGNORECASE)
    _SHOULD_VISUAL_RE = re.compile(r"^\[should\s*◈\]\s*", re.IGNORECASE)
    _SHOULD_RE = re.compile(r"^\[should\]\s*", re.IGNORECASE)
    _VERIFIABLE_RE = re.compile(r"^\[verifiable\]\s*", re.IGNORECASE)
    _VISUAL_RE = re.compile(r"^\[visual\]\s*", re.IGNORECASE)

    items = []
    for line in text.splitlines():
        stripped = _LIST_PREFIX_RE.sub("", line).strip()
        if not stripped:
            continue

        # [must ◈] → must, non-verifiable (visual/subjective)
        if _MUST_VISUAL_RE.match(stripped):
            text_part = _MUST_VISUAL_RE.sub("", stripped).strip()
            if text_part:
                items.append({"text": text_part, "binding": "must", "verifiable": False})
            continue

        # [should ◈] → should, non-verifiable
        if _SHOULD_VISUAL_RE.match(stripped):
            text_part = _SHOULD_VISUAL_RE.sub("", stripped).strip()
            if text_part:
                items.append({"text": text_part, "binding": "should", "verifiable": False})
            continue

        # [should] or [visual] → should, verifiable by default
        if _SHOULD_RE.match(stripped):
            text_part = _SHOULD_RE.sub("", stripped).strip()
            if text_part:
                items.append({"text": text_part, "binding": "should", "verifiable": True})
            continue

        if _VISUAL_RE.match(stripped):
            text_part = _VISUAL_RE.sub("", stripped).strip()
            if text_part:
                items.append({"text": text_part, "binding": "should", "verifiable": False})
            continue

        # [must] or [verifiable] → must, verifiable
        if _MUST_RE.match(stripped):
            stripped = _MUST_RE.sub("", stripped).strip()
        elif _VERIFIABLE_RE.match(stripped):
            stripped = _VERIFIABLE_RE.sub("", stripped).strip()

        # Default (plain text or after stripping [must]/[verifiable]) → must, verifiable
        if stripped:
            items.append({"text": stripped, "binding": "must", "verifiable": True})

    return items


def generate_spec(prompt: str, project_dir: Path, setting_sources: list[str] | None = None) -> list:
    """Generate a spec for a single task using an agentic QA engineer.

    Returns a list of spec items (dicts with text/verifiable),
    or empty list on failure.
    """
    spec, _cost, _error = asyncio.run(_run_spec_agent(prompt, project_dir, setting_sources=setting_sources))
    return spec


def generate_spec_sync(prompt: str, project_dir: Path, setting_sources: list[str] | None = None) -> tuple[list, float, str | None]:
    """Sync version returning full (spec_items, cost, error) tuple.

    Safe to call from asyncio.to_thread() — creates its own event loop.
    """
    return asyncio.run(_run_spec_agent(prompt, project_dir, setting_sources=setting_sources))


async def async_generate_spec(prompt: str, project_dir: Path, setting_sources: list[str] | None = None) -> tuple[list, float, str | None]:
    """Async version of generate_spec. Returns (spec_items, cost, error)."""
    return await _run_spec_agent(prompt, project_dir, setting_sources=setting_sources)


async def _run_spec_agent(prompt: str, project_dir: Path, setting_sources: list[str] | None = None) -> tuple[list, float, str | None]:
    """Run the spec generation agent.

    Uses a structured system prompt for constraint faithfulness,
    with a compliance self-check step before output.
    """
    with tempfile.NamedTemporaryFile(suffix=".txt", prefix="otto_spec_", delete=False) as temp_file:
        spec_file = Path(temp_file.name)

    system_prompt = """\
You produce acceptance criteria for a coding task. Your output sets
the minimum bar — the coding agent may EXCEED it but must not
violate constraints.

Two binding levels:
  [must]   — gating. Task fails QA if not met. Agent may exceed.
             Use for: functional requirements, measurable constraints,
             error handling, API contracts, security, performance limits.
  [should] — non-gating. QA notes but does not block merge.
             Use for: UX preferences, style, quality suggestions,
             "nice to have" behaviors.

Rules:
- Describe OBSERVABLE BEHAVIOR — what the user sees and experiences.
- Do not prescribe implementation (no file names, no code patterns).
- Do NOT include testing/build requirements ("tests pass", "test suite exists",
  "tests cover X"). Those are enforced by the verification system, not by specs.
- Each item must be a distinct user-visible behavior, not a sub-detail or
  restatement of another item. Do not split one requirement into positive
  and negative forms (e.g., "accepts valid input" and "rejects invalid input"
  are one item, not two).
- Preserve every user constraint exactly. Do not weaken thresholds.
- Produce as many or as few criteria as the task warrants.
  Most tasks need 5-15 items. If you have more than 15, you are probably
  duplicating or splitting items unnecessarily.
- Explore the codebase and research external APIs/libraries/services
  as needed to write accurate, grounded criteria.
- When the prompt is ambiguous, prefer [should] over [must].
  Don't promote ambiguous preferences to hard constraints.
  Don't invent product decisions.

Output format — one item per line:
  [must] rate-limited requests return HTTP 429
  [must] response completes within 200ms p95
  [should] prefer inline explanation of each contributing factor
  [should] credit source model if attribution is available"""

    agent_prompt = f"""TASK: {prompt}

Instructions:
- Read only what you need: the data types/models file and 1-2 existing components
  for context. Do not read every file in the project.
- If the task references an external site/app/example, inspect it before writing the spec
  and emit concrete behavioral criteria derived from what you observed.
- Include the necessary happy path, error cases, negative cases, edge cases, and retained behavior.
- Reference user-visible placement when useful, but do not mention internal implementation names or file structure.
- Tag each criterion as [must] or [should] based on binding level.
- For visual/subjective items that need browser inspection (not code-testable),
  add ◈ marker: [must ◈] or [should ◈]. Examples: layout appearance, color
  consistency, visual alignment, animation smoothness, responsive feel.
- Do not ask questions. Write the criteria based on the task description.

Write acceptance criteria to: {spec_file}
Write only [must]/[should] criteria lines to the file — no headings, notes, or prose."""

    # Persistent log for debugging
    log_dir = project_dir / "otto_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []

    spec_cost = 0.0
    try:
        agent_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=str(project_dir),
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": system_prompt},
            setting_sources=setting_sources or ["project"],
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
        error_msg = str(e)
        print(f"  spec agent error: {error_msg}", flush=True)
        log_lines.append(f"ERROR: {error_msg}")
        _write_log(log_dir / "spec-agent.log", log_lines)
        # Clean up temp file on error path
        if spec_file.exists():
            spec_file.unlink(missing_ok=True)
        return [], spec_cost, error_msg

    _write_log(log_dir / "spec-agent.log", log_lines)

    # Read the spec file
    if spec_file.exists():
        text = spec_file.read_text()
        spec_file.unlink()
        return _parse_spec_output(text), spec_cost, None

    # Clean up temp file if it doesn't exist (shouldn't happen, but be safe)
    spec_file.unlink(missing_ok=True)
    error_msg = "Spec agent did not write the output file"
    print(f"  spec agent error: {error_msg}", flush=True)
    log_lines.append(f"ERROR: {error_msg}")
    _write_log(log_dir / "spec-agent.log", log_lines)
    return [], spec_cost, error_msg


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

        result_msg = None
        async for message in query(prompt=agent_prompt, options=agent_opts):
            if isinstance(message, ResultMessage):
                result_msg = message
            elif hasattr(message, "session_id") and hasattr(message, "is_error"):
                result_msg = message
            elif AssistantMessage and isinstance(message, AssistantMessage):
                for block in message.content:
                    if TextBlock and isinstance(block, TextBlock) and block.text:
                        log_lines.append(block.text)
                    elif ToolUseBlock and isinstance(block, ToolUseBlock):
                        print_agent_tool(block, quiet=True)
                        log_lines.append(f"\u25cf {block.name}  {_tool_use_summary(block)}")
        if result_msg and getattr(result_msg, "is_error", False):
            error_detail = getattr(result_msg, "result", None) or "unknown error"
            raise ValueError(f"Failed to parse markdown tasks: agent error: {error_detail}")
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
