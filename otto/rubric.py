"""Otto rubric generation — agentic rubric generation + markdown parsing."""

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


def _parse_rubric_output(text: str) -> list[str]:
    """Parse LLM output into a list of rubric criteria strings.

    Strips numbering (1. , 1) ) and bullets (- , * ) prefixes.
    Skips empty lines.
    """
    criteria = []
    for line in text.splitlines():
        stripped = _LIST_PREFIX_RE.sub("", line).strip()
        if stripped:
            criteria.append(stripped)
    return criteria


def generate_rubric(prompt: str, project_dir: Path) -> list[str]:
    """Generate a rubric for a single task using an agentic QA engineer.

    The agent explores the project (reads source files, runs CLI --help,
    checks existing tests) then writes acceptance criteria grounded in
    what it actually observed.

    Returns a list of rubric criterion strings, or empty list on failure.
    """
    return asyncio.run(_run_rubric_agent(prompt, project_dir))


async def _run_rubric_agent(prompt: str, project_dir: Path) -> list[str]:
    """Run the rubric generation agent."""
    rubric_file = Path(tempfile.mktemp(suffix=".txt", prefix="otto_rubric_"))

    agent_prompt = f"""You are a senior QA engineer writing acceptance criteria for a coding task.

TASK: {prompt}

PROJECT DIRECTORY: {project_dir}

Follow these steps:
1. Read the 2-3 source files most directly related to the task (e.g., the module to modify + its CLI). Run --help if there's a CLI. Do NOT read every file in the project.
2. Write initial criteria to: {rubric_file}
4. SELF-REVIEW: Read your criteria back and ask:
   - Are any criteria about implementation details instead of user behavior? Rewrite them.
   - Are any criteria trivial (would pass with a broken implementation)? Strengthen them.
   - Did I miss error handling, edge cases, or anti-patterns? Add them.
   - Would these criteria actually catch real bugs? If not, improve them.
5. Write the improved criteria to the same file (overwrite)

Write criteria about BEHAVIOR the user experiences, not implementation details.
- BAD: "reads a TSV file (label\\ttext per line)" — too prescriptive about format
- GOOD: "accepts a file with labeled examples and trains successfully" — describes behavior

Include ALL of these categories:
- Happy path: the feature works as described for a real user
- Error handling: what happens with wrong/missing/malformed input?
- Negative/anti-pattern: things that must NOT happen ("does NOT crash", "does NOT corrupt data")
- Edge cases: empty inputs, boundary values, special characters
- Real-world usage: what would a user actually try that might break?

If this task MODIFIES existing functionality:
- Include regression criteria: existing behavior is preserved after the change

Scale by complexity:
- Simple tasks (typo, rename): 3-5 criteria
- Medium tasks (new method, CLI command): 6-10 criteria
- Complex tasks (new feature with multiple components): 10-12 criteria

Write ONLY a numbered list to {rubric_file}. One criterion per line. No prose."""

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
        print(f"  rubric agent error: {e}", flush=True)
        return []

    # Read the rubric file
    if rubric_file.exists():
        text = rubric_file.read_text()
        rubric_file.unlink()
        return _parse_rubric_output(text)

    return []


def parse_markdown_tasks(md_file: Path, project_dir: Path) -> list[dict]:
    """Parse a markdown file into structured tasks using an agentic PM.

    The agent explores the project, reads the markdown, and outputs a JSON
    array of tasks with prompts and rubrics.

    Returns list of task dicts.
    Raises ValueError on parse failure or invalid task structure.
    """
    return asyncio.run(_run_markdown_agent(md_file, project_dir))


async def _run_markdown_agent(md_file: Path, project_dir: Path) -> list[dict]:
    """Run the markdown parsing agent."""
    md_content = md_file.read_text()
    output_file = Path(tempfile.mktemp(suffix=".json", prefix="otto_tasks_"))

    agent_prompt = f"""You are a senior QA engineer and technical PM. Break this feature document into coding tasks.

DOCUMENT:
{md_content}

PROJECT DIRECTORY: {project_dir}

BEFORE writing tasks:
1. Read at most 3-5 source files most relevant to the document
2. If there's a CLI, run --help (one command)
3. Write the tasks — don't explore further

Then write a JSON array to: {output_file}

Each element should have:
- "prompt": a clear, actionable description of what to implement
- "rubric": 5-10 concrete, testable acceptance criteria

RULES:
- Each task is a complete, self-contained unit of work
- Do NOT create separate tasks for writing tests — test expectations belong in the rubric
- One heading/section = one task
- Rubric items describe BEHAVIOR, not implementation details
- Include: happy path, error handling, negative ("does NOT"), edge cases
- Reference actual class/function names you found in the project
- If a task modifies existing functionality, include regression criteria

Write ONLY a valid JSON array to {output_file}. No prose.
Example: [{{"prompt": "Add search", "rubric": ["search works", "case-insensitive"]}}]"""

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

    return tasks
