"""Otto product context — accumulates knowledge across tasks.

After each task completes and merges, a cheap LLM call reads the diff and
updates context.md at the project root. The coding agent discovers this
file naturally by exploring the codebase — no prompt injection needed.

context.md captures what the codebase alone doesn't tell you:
- WHY architecture decisions were made
- What conventions emerged
- What gotchas were discovered
- Current data model and API shape

This is analogous to CC agent teams' shared context, implemented as a
project file instead of peer messaging.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from otto.agent import ClaudeAgentOptions, _subprocess_env, run_agent_query
from otto.observability import append_text_log

logger = logging.getLogger("otto.product_context")


def _context_log(project_dir: Path, *lines: str) -> None:
    append_text_log(project_dir / "otto_logs" / "product-context.log", lines)


CONTEXT_UPDATE_PROMPT = """\
A coding task just completed in an autonomous multi-task build.
Update the product context file with any new information from this task.

CURRENT context.md:
---
{existing}
---

Task that just completed: {task_key}
Task prompt: {task_prompt}

Changes made (git diff --stat):
{diff}

YOUR OUTPUT MUST BE THE FULL UPDATED context.md FILE CONTENT.
Do NOT describe what you would write. Do NOT output meta-commentary.
Output ONLY the markdown content that should be written to context.md.

The file should contain these sections (add entries under each):
## Architecture Decisions
## Data Model
## API Endpoints
## Conventions
## Gotchas

Rules:
- Output the COMPLETE file content starting with "# Product Context"
- Do NOT remove existing entries — only add or update
- Keep entries concise (one line per item)
- Tag each new entry with the task key that discovered it
- If nothing meaningful changed, output the existing content unchanged
"""


async def update_product_context(
    project_dir: Path,
    task_key: str,
    task_prompt: str,
    diff: str,
    config: dict[str, Any],
) -> float:
    """Update context.md after a task completes. Returns cost in USD."""
    context_path = project_dir / "context.md"

    # Only update if context.md exists (created by otto build) or there are 3+ tasks
    if not context_path.exists():
        tasks_path = project_dir / "tasks.yaml"
        if tasks_path.exists():
            import yaml
            try:
                data = yaml.safe_load(tasks_path.read_text()) or {}
                task_count = len(data.get("tasks", []))
                if task_count < 3:
                    return 0.0
            except Exception:
                return 0.0
            # Auto-create for 3+ tasks
            context_path.write_text(
                "# Product Context\n"
                "<!-- Auto-updated after each task. DO NOT edit manually. -->\n\n"
            )
        else:
            return 0.0

    existing = context_path.read_text()

    # Truncate diff to avoid blowing up context
    truncated_diff = diff[:6000] if len(diff) > 6000 else diff

    prompt = CONTEXT_UPDATE_PROMPT.format(
        existing=existing,
        task_key=task_key,
        task_prompt=task_prompt[:500],
        diff=truncated_diff,
    )

    _context_log(
        project_dir,
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] updating context.md after {task_key}",
    )

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=_context_settings(config),
        env=_subprocess_env(),
        effort="low",  # cheap call, just structured extraction
        max_turns=1,  # single output, no tool use — just produce the file content
        system_prompt="You update a product context file. Output ONLY the file content. No commentary, no explanation, no markdown fences. Start with '# Product Context' and end with the last entry.",
    )
    model = _context_model(config)
    if model:
        options.model = model

    started_at = time.monotonic()
    try:
        raw_output, cost, _result = await run_agent_query(prompt, options)
        cost_usd = float(cost or 0.0)
        duration_s = round(time.monotonic() - started_at, 1)

        # The LLM returns the full updated context.md
        updated = raw_output.strip()
        # Strip markdown fences if present
        if updated.startswith("```"):
            lines = updated.split("\n")
            # Remove first line (```markdown or ```) and last line (```)
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            updated = "\n".join(lines).strip()
        if updated and len(updated) > 20 and updated.startswith("# Product Context"):  # sanity check
            context_path.write_text(updated)
            _context_log(
                project_dir,
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] context.md updated "
                f"(${cost_usd:.3f}, {duration_s:.1f}s, {len(updated)} chars)",
                "",
            )
        else:
            _context_log(
                project_dir,
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] context.md unchanged "
                f"(output too short: {len(updated)} chars)",
                "",
            )

        return cost_usd
    except Exception as exc:
        _context_log(
            project_dir,
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] context update failed: {exc}",
            "",
        )
        return 0.0


def _context_settings(config: dict[str, Any]) -> list[str]:
    return str(config.get("planner_agent_settings", "project") or "project").split(",")


def _context_model(config: dict[str, Any]) -> str | None:
    model = config.get("planner_model")
    return str(model) if model else None
