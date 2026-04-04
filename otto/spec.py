"""Otto spec generation — agentic spec generation + markdown parsing."""

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from otto.agent import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    _subprocess_env,
    normalize_usage,
    query,
    tool_use_summary as _tool_use_summary,
)
from otto.config import agent_provider
from otto.display import print_agent_tool


def _write_log(path: Path, lines: list[str]) -> None:
    """Append timestamped log lines to file (best-effort).

    Uses append mode so retries accumulate rather than overwrite.
    """
    try:
        import time as _time
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{_time.strftime('%Y-%m-%d %H:%M:%S')}]\n")
            text = "\n".join(lines)
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
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


def generate_spec(
    prompt: str,
    project_dir: Path,
    setting_sources: list[str] | None = None,
    log_dir: Path | None = None,
    config: dict[str, Any] | None = None,
) -> list:
    """Generate a spec for a single task using an agentic QA engineer.

    Returns a list of spec items (dicts with text/verifiable),
    or empty list on failure.
    """
    spec, _cost, _error, _usage = asyncio.run(
        _run_spec_agent(
            prompt,
            project_dir,
            setting_sources=setting_sources,
            log_dir=log_dir,
            config=config,
        )
    )
    return spec


def generate_spec_sync(
    prompt: str,
    project_dir: Path,
    setting_sources: list[str] | None = None,
    log_dir: Path | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[list, float, str | None, dict[str, int]]:
    """Sync version returning full (spec_items, cost, error, usage) tuple.

    Safe to call from asyncio.to_thread() — creates its own event loop.
    """
    return asyncio.run(
        _run_spec_agent(
            prompt,
            project_dir,
            setting_sources=setting_sources,
            log_dir=log_dir,
            config=config,
        )
    )


async def async_generate_spec(
    prompt: str,
    project_dir: Path,
    setting_sources: list[str] | None = None,
    log_dir: Path | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[list, float, str | None, dict[str, int]]:
    """Async version of generate_spec. Returns (spec_items, cost, error, usage)."""
    return await _run_spec_agent(
        prompt,
        project_dir,
        setting_sources=setting_sources,
        log_dir=log_dir,
        config=config,
    )


async def _run_spec_agent(
    prompt: str,
    project_dir: Path,
    setting_sources: list[str] | None = None,
    log_dir: Path | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[list, float, str | None, dict[str, int]]:
    """Run the spec generation agent.

    Uses a structured system prompt for constraint faithfulness,
    with a compliance self-check step before output.

    If log_dir is provided, spec-agent.log is written there (per-task).
    Otherwise falls back to otto_logs/spec-agent.log (legacy).
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
- Produce as few criteria as needed to define the contract.
  Focus on WHAT the code must do, not every way it could fail.
  The QA agent runs adversarial tests beyond your specs — you don't
  need to anticipate every edge case, just the core requirements.
  Scale with task complexity: a bugfix may need 1-2, a full feature
  may need 8-12. If you're specifying standard behaviors that any
  competent implementation would include, you're over-specifying.
- Explore the codebase and research external APIs/libraries/services
  as needed to write accurate, grounded criteria.
- When the prompt is ambiguous, prefer [should] over [must].
  Don't promote ambiguous preferences to hard constraints.
  Don't invent product decisions.

Think about who uses this code:
- For CLI/UI tasks, the user is a human — spec the commands and visible output.
- For library/module/data-layer tasks, the user is calling code — spec the
  programmatic API: what parameters are accepted, what shape is returned,
  what errors are raised on bad input.
- For multi-layer tasks (data + API + CLI), spec each layer's contract.

Think about what inputs will be tried:
- Consider boundary values: zero, empty, negative, maximum.
- If the task involves numeric parameters, what happens at zero?
- If it involves collections, what happens when empty?
- These only need [must] items when the behavior is non-obvious or
  the task specifically mentions constraints.

Output format — one item per line:
  [must] rate-limited requests return HTTP 429
  [must] response completes within 200ms p95
  [should] prefer inline explanation of each contributing factor
  [should] credit source model if attribution is available"""

    agent_prompt = f"""{system_prompt}

TASK: {prompt}

Instructions:
- Read only what you need: the data types/models file and 1-2 existing components
  for context. Do not read every file in the project.
- If the task references an external site/app/example, inspect it before writing the spec
  and emit concrete behavioral criteria derived from what you observed.
- IMPORTANT: Check what already exists in the codebase that this task must integrate with.
  If the project has authentication/authorization, new data APIs MUST include ownership
  scoping (e.g., "items filtered by authenticated user"). If the project has foreign keys
  or relationships, deletion MUST handle cascading. If the project has validation on
  existing models, new endpoints MUST respect those constraints. These are [must] items —
  not obvious from the task prompt alone, but critical for correctness.
- Cover the core behavior. Add error/edge cases only when non-obvious or task-specific.
  Standard UI behaviors (click-outside, escape key, toggle) don't need specs unless the task calls them out.
- Reference user-visible placement when useful, but do not mention internal implementation names or file structure.
- Tag each criterion as [must] or [should] based on binding level.
- For visual/subjective items that need browser inspection (not code-testable),
  add ◈ marker: [must ◈] or [should ◈]. Examples: layout appearance, color
  consistency, visual alignment, animation smoothness, responsive feel.
- Do not ask questions. Write the criteria based on the task description.

Write acceptance criteria to: {spec_file}
Write only [must]/[should] criteria lines to the file — no headings, notes, or prose."""

    # Persistent log for debugging — per-task log_dir when available
    spec_log_dir = log_dir if log_dir else (project_dir / "otto_logs")
    spec_log_dir.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []

    spec_cost = 0.0
    spec_usage: dict[str, int] = {}
    try:
        agent_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=str(project_dir),
            system_prompt={"type": "preset", "preset": "claude_code"},
            setting_sources=setting_sources or ["project"],
            env=_subprocess_env(project_dir),
            provider=agent_provider(config or {}),
        )
        if config and config.get("model"):
            agent_opts.model = config["model"]

        num_turns = 0
        result_msg = None
        async for message in query(prompt=agent_prompt, options=agent_opts):
            if isinstance(message, ResultMessage):
                result_msg = message
                raw_cost = getattr(message, "total_cost_usd", None)
                if isinstance(raw_cost, (int, float)):
                    spec_cost = float(raw_cost)
                spec_usage = normalize_usage(getattr(result_msg, "usage", None))
            elif isinstance(message, AssistantMessage):
                num_turns += 1
                for block in message.content:
                    if isinstance(block, ThinkingBlock):
                        thinking = getattr(block, "thinking", "")
                        if thinking:
                            log_lines.append(f"[thinking] {thinking}")
                    elif isinstance(block, TextBlock) and block.text:
                        # Don't print spec agent narration — log only
                        log_lines.append(block.text)
                    elif isinstance(block, ToolUseBlock):
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
        _write_log(spec_log_dir / "spec-agent.log", log_lines)
        # Clean up temp file on error path
        if spec_file.exists():
            spec_file.unlink(missing_ok=True)
        return [], spec_cost, error_msg, spec_usage

    _write_log(spec_log_dir / "spec-agent.log", log_lines)

    # Read the spec file
    if spec_file.exists():
        text = spec_file.read_text()
        spec_file.unlink()
        parsed_items = _parse_spec_output(text)
        filtered_items = filter_generated_spec_items(parsed_items)
        skipped = len(parsed_items) - len(filtered_items)
        summary = f"spec parsed: items={len(filtered_items)}"
        if skipped:
            summary += f" skipped={skipped}"
        _write_log(spec_log_dir / "spec-agent.log", [summary])
        return filtered_items, spec_cost, None, spec_usage

    # Clean up temp file if it doesn't exist (shouldn't happen, but be safe)
    spec_file.unlink(missing_ok=True)
    error_msg = "Spec agent did not write the output file"
    print(f"  spec agent error: {error_msg}", flush=True)
    log_lines.append(f"ERROR: {error_msg}")
    _write_log(spec_log_dir / "spec-agent.log", log_lines)
    return [], spec_cost, error_msg, spec_usage


def parse_markdown_tasks(
    md_file: Path,
    project_dir: Path,
    config: dict[str, Any] | None = None,
) -> list[dict]:
    """Parse a markdown file into structured tasks using an agentic PM.

    The agent explores the project, reads the markdown, and outputs a JSON
    array of tasks with prompts and specs.

    Returns list of task dicts.
    Raises ValueError on parse failure or invalid task structure.
    """
    return asyncio.run(_run_markdown_agent(md_file, project_dir, config))


async def _run_markdown_agent(
    md_file: Path,
    project_dir: Path,
    config: dict[str, Any] | None = None,
) -> list[dict]:
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
            env=_subprocess_env(project_dir),
            provider=agent_provider(config or {}),
        )
        if config and config.get("model"):
            agent_opts.model = config["model"]

        result_msg = None
        async for message in query(prompt=agent_prompt, options=agent_opts):
            if isinstance(message, ResultMessage):
                result_msg = message
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text:
                        log_lines.append(block.text)
                    elif isinstance(block, ToolUseBlock):
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


# ---------------------------------------------------------------------------
# Spec preamble filtering — detect generated headings/context that aren't
# real acceptance criteria.
# ---------------------------------------------------------------------------

_SPEC_SEPARATOR_RE = re.compile(r"^[-=*_`#\s]+$")
_SPEC_LABEL_RE = re.compile(r"^[A-Z][A-Za-z0-9 /()_-]{1,40}:")
_SPEC_TITLE_RE = re.compile(r"^[A-Z][A-Za-z0-9()/'-]*(?: [A-Z][A-Za-z0-9()/'-]*){0,7}$")
_SPEC_REQUIREMENT_RE = re.compile(
    r"\b("
    r"must|should|shall|will|can|"
    r"display(?:ed|s)?|render(?:ed|s)?|show(?:n|s)?|"
    r"calculate(?:d|s)?|compute(?:d|s)?|convert(?:ed|s)?|format(?:ted|s)?|"
    r"classif(?:y|ied|ies)|match(?:es|ed)?|cover(?:ed|s)?|"
    r"respect(?:s|ed)?|handle(?:d|s)?|support(?:ed|s)?|"
    r"pass(?:es|ed)?|fail(?:s|ed)?|raise(?:s|d)?|"
    r"return(?:s|ed)?|update(?:s|d)?|store(?:s|d)?|"
    r"use(?:s|d)?|include(?:s|d)?|"
    r"is displayed|is rendered|is calculated|is shown|"
    r"are displayed|are rendered|are calculated|are shown"
    r")\b",
    re.IGNORECASE,
)
_SPEC_CONTEXT_PHRASES = (
    "existing ",
    "already ",
    "available on",
    "available in",
    "always arrives",
    "tests live in",
    "tests use",
    "using jest",
    "current.",
    "from api",
    "from the api",
)


def _has_requirement_signal(text: str) -> bool:
    """Heuristic: real acceptance criteria usually describe required behavior."""
    lower = text.lower()
    if lower.startswith(("the ", "a ", "an ", "when ", "if ")):
        return True
    return bool(_SPEC_REQUIREMENT_RE.search(text))


def _is_preamble(item) -> bool:
    """Detect generated spec preamble/context rows that should not be treated as criteria."""
    from otto.tasks import spec_text

    text = " ".join(spec_text(item).split()).strip()
    if not text:
        return True

    lower = text.lower()
    has_requirement = _has_requirement_signal(text)

    if _SPEC_SEPARATOR_RE.fullmatch(text):
        return True
    if lower.startswith(("acceptance spec", "acceptance criteria", "context:", "overview:")):
        return True
    if _SPEC_LABEL_RE.match(text) and not has_requirement:
        return True
    if len(text) < 40 and not has_requirement and _SPEC_TITLE_RE.fullmatch(text):
        return True
    if any(phrase in lower for phrase in _SPEC_CONTEXT_PHRASES) and not has_requirement:
        return True

    return False


def filter_generated_spec_items(spec_items: list) -> list:
    """Drop title/context rows from generated specs before display or storage."""
    return [item for item in spec_items if not _is_preamble(item)]
