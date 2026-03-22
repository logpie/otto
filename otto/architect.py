"""Otto architect — codebase analysis agent that produces shared conventions for downstream agents."""

import json
import os
import re
from pathlib import Path

import yaml

try:
    from claude_agent_sdk import ClaudeAgentOptions, query
    from claude_agent_sdk.types import ResultMessage, TextBlock, ToolUseBlock
except ImportError:
    from otto._agent_stub import ClaudeAgentOptions, query, ResultMessage
    TextBlock = None  # type: ignore[assignment,misc]
    ToolUseBlock = None  # type: ignore[assignment,misc]

from otto.display import print_agent_tool


# Files each role receives from otto_arch/
_ROLE_FILES: dict[str, list[str]] = {
    "coding": ["conventions.md", "data-model.md", "interfaces.md", "task-decisions.md", "gotchas.md"],
    "pilot": ["codebase.md", "task-decisions.md", "file-plan.md"],
}

_META_FILE = ".meta"


async def run_architect_agent(
    tasks: list[dict],
    project_dir: Path,
    quiet: bool = False,
) -> Path | None:
    """Run the architect agent to analyze the codebase and produce otto_arch/ docs.

    Uses Agent SDK with full read access to explore the codebase.
    Returns path to otto_arch/ or None on failure.
    """
    arch_dir = project_dir / "otto_arch"
    existing = arch_dir.exists()

    task_summaries = "\n".join(
        f"- Task #{t.get('id', '?')}: {t.get('prompt', '')}"
        for t in tasks
    )

    existing_section = ""
    if existing:
        existing_section = (
            "\notto_arch/ already exists — UPDATE the files, don't rewrite from scratch. "
            "Add new task-specific decisions, extend gotchas, update interfaces if needed. "
            "Also create file-plan.md if it doesn't exist yet.\n"
        )

    prompt = f"""You are a software architect analyzing a codebase before multiple coding agents work on it.

Your job: explore the project, understand how it works, and document shared conventions
so that independent agents make consistent decisions.

TASKS THAT WILL BE IMPLEMENTED:
{task_summaries}
{existing_section}
EXPLORE the codebase:
1. Read the main source files (models, store, CLI, __main__.py)
2. Read existing test files to understand test patterns
3. Run --help if there's a CLI
4. Check the data storage format

Then write analysis files to: {project_dir}/otto_arch/

Write these files:
- codebase.md: architecture overview, module map, data flow
  Include ASCII box diagrams (use ┌─┐│└─┘ characters):
  1. Module dependency graph showing which files import what
  2. Data flow diagram showing how data moves through the system (CLI → store → file)
  Example style:
  ```
  ┌──────────┐     ┌──────────┐     ┌──────────┐
  │  cli.py  │────→│ store.py │────→│ data.json│
  └──────────┘     └──────────┘     └──────────┘
  ```
- conventions.md: coding style, error handling, naming patterns
- data-model.md: storage format, schema, example data
- interfaces.md: cross-module function signatures, public APIs
- test-patterns.md: test framework, fixtures, CLI test approach, shared helpers
  Include a ```conftest.py fenced block with shared test fixtures
- task-decisions.md: per-task design decisions for the tasks listed above
  - In task-decisions.md, for each task that CHANGES existing behavior (replaces mock with real API,
    changes function signatures, removes features), add a ⚠ CONTRACT CHANGE warning explaining
    what existing tests may break and why.
- gotchas.md: anti-patterns, known pitfalls to avoid
- file-plan.md: predict which files each task will modify (exact paths).
  If multiple tasks modify the same file, recommend depends_on to chain them serially.
  Format as YAML (IMPORTANT: use "depends_on" not "on" as the key — "on" is a YAML reserved word):
  ```yaml
  tasks:
    - id: 1
      predicted_files: [path/to/file1.py, path/to/file2.py]
    - id: 2
      predicted_files: [path/to/file1.py]
  recommended_dependencies:
    - from: 2
      depends_on: 1
      reason: "both modify path/to/file1.py"
  ```

Rules:
- Be CONCRETE — exact field names, exact function signatures, exact code
- Reference actual code you found (class names, function names, file paths)
- Keep each file focused and SHORT (not a novel)
- If otto_arch/ already exists, UPDATE files — don't rewrite from scratch
"""

    try:
        agent_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=str(project_dir),
            setting_sources=["user", "project"],
            env=dict(os.environ),
        )

        result_msg = None
        num_turns = 0
        async for message in query(prompt=prompt, options=agent_opts):
            if isinstance(message, ResultMessage):
                result_msg = message
            elif hasattr(message, "session_id") and hasattr(message, "is_error"):
                result_msg = message
            elif hasattr(message, "content"):
                num_turns += 1
                for block in message.content:
                    if TextBlock and isinstance(block, TextBlock) and block.text:
                        if not quiet:
                            print(block.text, flush=True)
                    elif ToolUseBlock and isinstance(block, ToolUseBlock):
                        print_agent_tool(block, quiet=quiet)

        # Check if agent reported an error
        if result_msg and getattr(result_msg, "is_error", False):
            error_detail = getattr(result_msg, "result", None) or "unknown error"
            raise RuntimeError(f"Architect agent error: {error_detail}")

        # Check if agent never started (no result message at all)
        if num_turns == 0 and result_msg is None:
            raise RuntimeError("Architect agent produced no output — agent may have failed to start")

        # Validate that architect produced at least the core files
        expected = ["conventions.md", "data-model.md", "interfaces.md"]
        produced = {f.name for f in arch_dir.iterdir()} if arch_dir.exists() else set()
        if arch_dir.exists() and all(f in produced for f in expected):
            # Reset gotcha counter on successful architect run
            meta = _load_meta(project_dir)
            meta["gotcha_count"] = 0
            _save_meta(project_dir, meta)

            # Extract and write conftest.py if present in test-patterns.md
            test_patterns = arch_dir / "test-patterns.md"
            if test_patterns.exists():
                conftest_code = _extract_conftest(test_patterns.read_text())
                if conftest_code:
                    conftest_path = project_dir / "tests" / "conftest.py"
                    conftest_path.parent.mkdir(parents=True, exist_ok=True)
                    if not conftest_path.exists():
                        conftest_path.write_text(conftest_code)

            return arch_dir
        return None

    except Exception as e:
        if not quiet:
            print(f"  architect agent error: {e}", flush=True)
        return None


def load_design_context(project_dir: Path, role: str) -> str:
    """Load and concatenate relevant otto_arch/ files for a given role.

    role: one of "coding", "pilot"
    Returns combined string or empty string if otto_arch/ doesn't exist.
    """
    arch_dir = project_dir / "otto_arch"
    if not arch_dir.exists():
        return ""

    files = _ROLE_FILES.get(role, [])
    parts: list[str] = []
    for filename in files:
        filepath = arch_dir / filename
        if filepath.exists():
            try:
                content = filepath.read_text().strip()
                if content:
                    parts.append(f"## {filename}\n\n{content}")
            except OSError:
                continue

    return "\n\n---\n\n".join(parts)


def _extract_conftest(test_patterns_content: str) -> str | None:
    """Extract conftest.py code from test-patterns.md fenced block."""
    pattern = r"```(?:python\s*)?(?:#\s*)?conftest\.py\s*\n(.*?)```"
    match = re.search(pattern, test_patterns_content, re.DOTALL)
    if match:
        return match.group(1).strip() + "\n"
    return None


def parse_file_plan(project_dir: Path) -> list[tuple[int, int]]:
    """Parse otto_arch/file-plan.md for recommended dependencies.

    Returns list of (from_id, on_id) dependency pairs.
    Gracefully returns empty list on any parsing error.
    """
    file_plan = project_dir / "otto_arch" / "file-plan.md"
    if not file_plan.exists():
        return []

    content = file_plan.read_text()

    # Extract YAML block from markdown (may be fenced or raw)
    yaml_content = content
    yaml_match = re.search(r"```ya?ml\s*\n(.*?)```", content, re.DOTALL)
    if yaml_match:
        yaml_content = yaml_match.group(1)

    try:
        data = yaml.safe_load(yaml_content)
    except yaml.YAMLError:
        return []

    if not isinstance(data, dict):
        return []

    deps: list[tuple[int, int]] = []
    for rec in data.get("recommended_dependencies") or []:
        if not isinstance(rec, dict):
            continue
        from_id = rec.get("from")
        # Accept "depends_on", "on", or True (YAML parses bare "on" as boolean True)
        on_id = rec.get("depends_on") or rec.get("on") or rec.get(True)
        if isinstance(from_id, int) and isinstance(on_id, int):
            deps.append((from_id, on_id))

    # Deterministic overlap detection: if two tasks share predicted files
    # but have no dependency between them, chain them to prevent merge conflicts.
    # This catches cases the architect missed (e.g., both modify cli.py but
    # architect said "independent").
    task_files: dict[int, set[str]] = {}
    for task_entry in data.get("tasks") or []:
        if not isinstance(task_entry, dict):
            continue
        tid = task_entry.get("id")
        files = task_entry.get("predicted_files") or []
        if isinstance(tid, int) and isinstance(files, list):
            task_files[tid] = {str(f) for f in files}

    # Build set of already-connected pairs (in either direction)
    connected = set()
    for a, b in deps:
        connected.add((a, b))
        connected.add((b, a))

    # Check all task pairs for file overlap
    task_ids = sorted(task_files.keys())
    for i, t1 in enumerate(task_ids):
        for t2 in task_ids[i + 1:]:
            if (t1, t2) in connected or (t2, t1) in connected:
                continue
            overlap = task_files[t1] & task_files[t2]
            if overlap:
                # Chain: later task depends on earlier
                deps.append((t2, t1))
                connected.add((t2, t1))
                connected.add((t1, t2))

    return deps


def append_gotcha(project_dir: Path, warning_text: str) -> None:
    """Append a learning to otto_arch/gotchas.md (cheap file write, no agent)."""
    arch_dir = project_dir / "otto_arch"
    if not arch_dir.exists():
        return

    gotchas = arch_dir / "gotchas.md"
    if gotchas.exists():
        content = gotchas.read_text()
    else:
        content = "# Gotchas\n\n"

    content += f"- {warning_text}\n"
    gotchas.write_text(content)

    # Increment counter
    meta = _load_meta(project_dir)
    meta["gotcha_count"] = meta.get("gotcha_count", 0) + 1
    _save_meta(project_dir, meta)


def is_stale(project_dir: Path) -> bool:
    """Check if otto_arch/ is stale based on accumulated gotchas.

    Returns True if 3+ gotchas accumulated since last architect refresh.
    Cheap check — no agent call, just file reads.
    """
    meta = _load_meta(project_dir)
    return meta.get("gotcha_count", 0) >= 3


def _load_meta(project_dir: Path) -> dict:
    """Load otto_arch/.meta JSON file."""
    meta_path = project_dir / "otto_arch" / _META_FILE
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_meta(project_dir: Path, meta: dict) -> None:
    """Save otto_arch/.meta JSON file."""
    meta_path = project_dir / "otto_arch" / _META_FILE
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta))


def feed_reconciliation_learnings(
    project_dir: Path,
    warnings: list[str],
) -> None:
    """Feed reconciliation warnings into gotchas.md for next architect run."""
    for warning in warnings:
        append_gotcha(project_dir, f"[reconciliation] {warning}")
