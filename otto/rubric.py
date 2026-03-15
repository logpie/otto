"""Otto rubric generation — generate evaluation rubrics via claude -p."""

import json
import re
import subprocess
from pathlib import Path


# Regex to strip numbered ("1. ", "1) ") or bullet ("- ", "* ") prefixes
_LIST_PREFIX_RE = re.compile(r"^\s*(?:\d+[.)]\s*|[-*]\s+)")

RUBRIC_TIMEOUT = 120  # seconds


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
    """Generate a rubric for a single task via claude -p.

    Returns a list of rubric criterion strings, or empty list on failure.
    """
    from otto.testgen import build_blackbox_context
    context = build_blackbox_context(project_dir, task_hint=prompt)

    system_prompt = f"""You are a senior QA engineer. Given a coding task and project context,
generate a rubric: a numbered list of specific, testable acceptance criteria
that verify the task was completed correctly.

PROJECT CONTEXT:
{context}

TASK: {prompt}

IMPORTANT: Write criteria about BEHAVIOR the user experiences, not implementation details.
- BAD: "reads a TSV file (label\\ttext per line)" — too prescriptive about format
- GOOD: "accepts a file with labeled examples and trains successfully" — describes behavior
- BAD: "uses Laplace smoothing with alpha=1" — implementation detail
- GOOD: "handles words not seen during training without crashing" — describes behavior

Include ALL of these categories:
- Happy path: the feature works as described for a real user
- Error handling: what happens when the user provides wrong/missing/malformed input?
- Negative/anti-pattern: things that must NOT happen ("does NOT crash", "does NOT corrupt data",
  "does NOT return wrong results")
- Edge cases: empty inputs, boundary values, special characters, zero/null, unicode
- Real-world usage: how would a user actually use this? What would they try that might break?

If this task MODIFIES existing functionality (not a brand new feature):
- Include regression criteria: verify that existing behavior is preserved after the change

Scale the number of criteria to the task's complexity:
- Simple tasks (typo fix, rename, config change): 3-5 criteria
- Medium tasks (add a method, new CLI command): 6-10 criteria
- Complex tasks (new feature with multiple components, API design): 10-12 criteria
Focus on the MOST IMPORTANT criteria. Prefer fewer, high-quality criteria over many weak ones.

Output ONLY a numbered list of criteria. No prose, no explanations.
Each criterion should be a single clear sentence. Use "does NOT" or "must NOT" for negative criteria."""

    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=system_prompt,
            capture_output=True,
            text=True,
            timeout=RUBRIC_TIMEOUT,
            start_new_session=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    if result.returncode != 0 or not result.stdout.strip():
        return []

    return _parse_rubric_output(result.stdout)


MARKDOWN_PARSE_TIMEOUT = 120  # seconds


def parse_markdown_tasks(md_file: Path, project_dir: Path) -> list[dict]:
    """Parse a markdown file into structured tasks via claude -p.

    Sends the markdown content + project context to Claude, expects a JSON array
    where each element has: prompt (str), rubric (list[str]), context (str).

    Returns list of task dicts.
    Raises ValueError on parse failure or invalid task structure.
    """
    md_content = md_file.read_text()
    from otto.testgen import build_blackbox_context
    context = build_blackbox_context(project_dir, task_hint=md_content)

    system_prompt = f"""You are a senior QA engineer and technical PM breaking down a feature document into coding tasks.

PROJECT CONTEXT:
{context}

DOCUMENT:
{md_content}

Break this document into individual coding tasks. For each task, provide:
- "prompt": a clear, actionable description of what to implement
- "rubric": 5-8 concrete, testable acceptance criteria

IMPORTANT RULES:
- Each task should be a complete, self-contained unit of work.
- Do NOT create separate tasks for writing tests — test expectations belong in the rubric.
- One heading/section in the document = one task (unless a section is too large, then split by feature, not by "implement" vs "test").
- Rubric items must be specific enough to write a test from. Cover: happy path, edge cases,
  error conditions, and anti-patterns (things that must NOT happen — e.g., "delete does NOT
  affect unrelated records", "invalid input does NOT silently succeed").
- If a task MODIFIES existing functionality (not a brand new feature), include regression
  criteria: verify that existing behavior is preserved after the change. Example: "existing
  search still works after adding tags", "delete still works after adding favorites". These
  protect against the change breaking something that already works.
- Reference actual function/class names from the project source when possible.

Output ONLY a valid JSON array. No prose, no markdown fences, no explanations.
Example format:
[{{"prompt": "Add search method to BookmarkStore", "rubric": ["search('python') returns matching bookmarks", "search is case-insensitive", "no matches returns empty list"]}}]"""

    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=system_prompt,
            capture_output=True,
            text=True,
            timeout=MARKDOWN_PARSE_TIMEOUT,
            start_new_session=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise ValueError(f"Failed to parse markdown tasks: {exc}") from exc

    if result.returncode != 0:
        raise ValueError(f"Failed to parse markdown tasks: claude returned {result.returncode}")

    output = result.stdout.strip()

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

    # Validate structure
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"Task {i} is not a dict")
        if not task.get("prompt"):
            raise ValueError(f"Task {i} missing 'prompt'")

    return tasks
