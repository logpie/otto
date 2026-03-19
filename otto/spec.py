"""Otto spec generation — agentic spec generation + markdown parsing."""

import asyncio
import json
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

from otto.display import print_agent_tool


# Regex to strip numbered ("1. ", "1) ") or bullet ("- ", "* ") prefixes
_LIST_PREFIX_RE = re.compile(r"^\s*(?:\d+[.)]\s*|[-*]\s+)")


def _parse_spec_output(text: str) -> list[str]:
    """Parse LLM output into a list of spec criteria strings.

    Strips numbering (1. , 1) ) and bullets (- , * ) prefixes.
    Skips empty lines.
    """
    criteria = []
    for line in text.splitlines():
        stripped = _LIST_PREFIX_RE.sub("", line).strip()
        if stripped:
            criteria.append(stripped)
    return criteria


def generate_spec(prompt: str, project_dir: Path) -> list[str]:
    """Generate a spec for a single task using an agentic QA engineer.

    The agent explores the project (reads source files, runs CLI --help,
    checks existing tests) then writes acceptance criteria grounded in
    what it actually observed.

    Returns a list of spec criterion strings, or empty list on failure.
    """
    spec, _cost = asyncio.run(_run_spec_agent(prompt, project_dir))
    return spec


async def _run_spec_agent(prompt: str, project_dir: Path) -> list[str]:
    """Run the spec generation agent with pre-loaded context (adaptive mode).

    The agent has full tool access to explore the project as needed.
    """
    spec_file = Path(tempfile.mktemp(suffix=".txt", prefix="otto_spec_"))

    agent_prompt = f"""You are a senior engineer writing the acceptance spec for a coding task.

TASK: {prompt}

You are working in {project_dir}. Explore the codebase as needed to understand what exists.

Write the acceptance spec to: {spec_file}

Steps:
1. EXTRACT hard requirements from the task description first.
   Look for: numbers, thresholds, "must", "always", "never", "hard constraint",
   specific behaviors the user explicitly stated.
   These become your top-priority spec items — preserve them faithfully.
   Do NOT weaken them (e.g., don't change "all lookups < 300ms" to "cached lookups < 300ms").
2. WRITE the spec — hard requirements first, then supporting requirements.
3. SELF-REVIEW: Read your spec back and ask:
   - Did I preserve the user's hard requirements verbatim?
   - Are any items duplicating the same behavior? Merge them.
   - Am I unsure about how something works? Read the specific file to verify.
   - Missing: error handling, edge cases, regression? Add briefly.

Write the SPEC — what must be true when this task is done.
Each item is a testable requirement, not a grading checkbox.
Prioritize: hard constraints first, then supporting requirements.

- BAD: "reads a TSV file (label\\ttext per line)" — too prescriptive about format
- GOOD: "accepts a file with labeled examples and trains successfully" — describes behavior

Total: 5-8 items. No bikeshedding (formatting details, unit labels, value ranges).

Write ONLY a numbered list to {spec_file}. One criterion per line. No prose."""

    spec_cost = 0.0
    try:
        agent_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=str(project_dir),
            max_turns=8,  # adaptive: typically 2-4 turns (write, review, rewrite)
        )

        async for message in query(prompt=agent_prompt, options=agent_opts):
            if isinstance(message, ResultMessage):
                raw_cost = getattr(message, "total_cost_usd", None)
                if isinstance(raw_cost, (int, float)):
                    spec_cost = float(raw_cost)
            elif AssistantMessage and isinstance(message, AssistantMessage):
                for block in message.content:
                    if TextBlock and isinstance(block, TextBlock) and block.text:
                        print(block.text, flush=True)
                    elif ToolUseBlock and isinstance(block, ToolUseBlock):
                        print_agent_tool(block)
    except Exception as e:
        print(f"  spec agent error: {e}", flush=True)
        return [], spec_cost

    # Read the spec file
    if spec_file.exists():
        text = spec_file.read_text()
        spec_file.unlink()
        return _parse_spec_output(text), spec_cost

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
    output_file = Path(tempfile.mktemp(suffix=".json", prefix="otto_tasks_"))

    agent_prompt = f"""You are a senior engineer breaking a feature document into coding tasks.

DOCUMENT:
{md_content}

You are working in {project_dir}. Explore the codebase as needed to understand what exists.

Write a JSON array to: {output_file}

Each element should have:
- "prompt": a clear, actionable description of what to implement
- "spec": 5-10 concrete, testable acceptance criteria
- "depends_on": indices (0-based) of tasks this one requires, or [] if independent.
  Task B depends on A if B needs code/APIs/data that A creates.
  When unsure, include the dependency (safe default).

RULES:
- Each task is a complete, self-contained unit of work
- Do NOT create separate tasks for writing tests — test expectations belong in the spec
- One heading/section = one task
- Spec items describe BEHAVIOR, not implementation details
- Include: happy path, error handling, negative ("does NOT"), edge cases
- Reference actual class/function names you found in the project
- If a task modifies existing functionality, include regression criteria

Write ONLY a valid JSON array to {output_file}. No prose.
Example: [{{"prompt": "Add search", "spec": ["search works", "case-insensitive"], "depends_on": []}}, {{"prompt": "Add search filters", "spec": ["filters work"], "depends_on": [0]}}]"""

    try:
        agent_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=str(project_dir),
            max_turns=10,
        )

        async for message in query(prompt=agent_prompt, options=agent_opts):
            if AssistantMessage and isinstance(message, AssistantMessage):
                for block in message.content:
                    if TextBlock and isinstance(block, TextBlock) and block.text:
                        print(block.text, flush=True)
                    elif ToolUseBlock and isinstance(block, ToolUseBlock):
                        print_agent_tool(block)
    except Exception as e:
        raise ValueError(f"Failed to parse markdown tasks: {e}") from e

    if not output_file.exists():
        raise ValueError("Agent did not write the output file")

    output = output_file.read_text().strip()
    output_file.unlink()

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
