"""Otto gate pilot — LLM intelligence at batch decision boundaries.

The gate pilot is invoked after a batch completes with failures. It reads
disk artifacts (verify logs, QA verdicts, task summaries) and returns
structured decisions: failure analysis, retry strategies, routed context
for upcoming tasks, and re-batching.

Replaces the simpler replan() call with richer context and structured output.
Falls back to replan() if pilot parsing fails.

Design principle: the pilot makes SYSTEM-LEVEL decisions that span across
tasks. It does NOT interpret per-task results for the coding agent (no
telephone game). QA verdicts, error logs, and diffs go directly to the
coding agent. The pilot decides what to do next, not how to fix code.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from otto.agent import ClaudeAgentOptions, _subprocess_env, run_agent_query
from otto.context import PipelineContext, TaskResult
from otto.observability import append_text_log
from otto.planner import (
    ExecutionPlan,
    _planner_effort,
    _planner_model,
    _planner_settings,
)

logger = logging.getLogger("otto.pilot")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RetryStrategy:
    """Per-task retry strategy decided by the pilot."""
    action: str       # "retry" | "retry_different" | "skip"
    guidance: str     # specific advice ("use vitest not jest")
    reason: str       # why this strategy ("environment issue, not code bug")


@dataclass
class RoutedContext:
    """Context to inject into a specific upcoming task."""
    target_task: str  # which upcoming task this is for
    context: str      # what to tell it


@dataclass
class PilotDecision:
    """Structured output from the gate pilot at a batch boundary."""
    failure_analysis: dict[str, str]          # task_key → why it failed
    retry_strategies: dict[str, RetryStrategy]  # task_key → strategy
    skip_tasks: list[str]                     # tasks to skip entirely
    routed_context: list[RoutedContext]        # context for specific tasks
    new_learnings: list[str]                  # patterns inferred from failures
    batches: list[dict[str, Any]]             # re-batched remaining tasks


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _pilot_log(project_dir: Path, *lines: str) -> None:
    append_text_log(project_dir / "otto_logs" / "pilot.log", lines)


def _attempt_sort_key(path: Path) -> int:
    """Sort attempt logs by numeric attempt index instead of lexical name order."""
    match = re.search(r"attempt-(\d+)", path.name)
    return int(match.group(1)) if match else 0


# ---------------------------------------------------------------------------
# Context assembly — reads disk artifacts, builds pilot prompt context
# ---------------------------------------------------------------------------

def assemble_pilot_context(
    batch_results: list[TaskResult],
    remaining_plan: ExecutionPlan,
    context: PipelineContext,
    project_dir: Path,
    pending_by_key: dict[str, Any],
) -> str:
    """Build pilot context from disk artifacts.

    Reads task-summary.json, verify logs, QA verdicts for failed tasks,
    and diff summaries for passed tasks. Richer than what replan() gets.
    """
    sections: list[tuple[str, str]] = []

    # 1. Architecture context (if exists — i2p or user-provided)
    arch = project_dir / "otto_arch" / "architecture.md"
    if arch.exists():
        try:
            sections.append(("Architecture", _truncate(arch.read_text(), 3000)))
        except OSError:
            pass

    # 2. Per-task results — richer than replan's one-line summaries
    for result in batch_results:
        task_dir = project_dir / "otto_logs" / result.task_key
        is_rolled_back = result.error_code in {
            "batch_qa_rolled_back",
            "batch_qa_infrastructure_error",
        }
        parts = [
            f"Task: {result.task_key}",
            f"Status: {'PASSED' if result.success else ('ROLLED_BACK' if is_rolled_back else 'FAILED')}",
            f"Cost: ${result.cost_usd:.2f}, Duration: {result.duration_s:.0f}s",
        ]

        if result.diff_summary:
            parts.append(f"Changes: {result.diff_summary}")

        if not result.success:
            # Actual error output from last attempt
            verify_logs = sorted(task_dir.glob("attempt-*-verify.log"), key=_attempt_sort_key)
            if verify_logs:
                try:
                    parts.append(
                        f"Error output (last attempt):\n{_truncate(verify_logs[-1].read_text(), 2000)}"
                    )
                except OSError:
                    pass

            # QA verdict if available
            qa_verdict = task_dir / "qa-verdict.json"
            if qa_verdict.exists():
                try:
                    parts.append(f"QA verdict:\n{_truncate(qa_verdict.read_text(), 1500)}")
                except OSError:
                    pass

            if result.qa_report:
                parts.append(f"QA report:\n{_truncate(result.qa_report, 1500)}")

            # Error string from TaskResult
            if result.error:
                parts.append(f"Error: {result.error}")
        else:
            # For passed tasks: summary (useful for routing discoveries)
            summary = task_dir / "task-summary.json"
            if summary.exists():
                try:
                    parts.append(f"Summary:\n{_truncate(summary.read_text(), 1000)}")
                except OSError:
                    pass

        sections.append((result.task_key, "\n".join(parts)))

    # 3. Cross-task learnings accumulated so far
    if context.learnings:
        learning_lines = [f"- [{l.source}] {l.text}" for l in context.learnings]
        sections.append(("Learnings so far", "\n".join(learning_lines)))

    # 4. Remaining tasks with prompts
    remaining_lines: list[str] = []
    for batch in remaining_plan.batches:
        for tp in batch.tasks:
            task = pending_by_key.get(tp.task_key, {})
            remaining_lines.append(
                f"- {tp.task_key}: {str(task.get('prompt', ''))[:200]}"
            )
    if remaining_lines:
        sections.append(("Upcoming tasks", "\n".join(remaining_lines)))

    return "\n\n".join(f"## {title}\n{body}" for title, body in sections)


def _truncate(text: str, max_chars: int) -> str:
    """Truncate text, keeping the END (where errors usually are)."""
    if len(text) <= max_chars:
        return text
    return f"... (truncated)\n{text[-max_chars:]}"


# ---------------------------------------------------------------------------
# Pilot invocation
# ---------------------------------------------------------------------------

BATCH_PILOT_PROMPT = """You are otto's gate pilot. A batch of coding tasks just completed.
Analyze the results and make decisions about what to do next.

{context}

PERMANENTLY FAILED TASK KEYS (terminal, do not include in batches): {failed_keys}
ROLLED_BACK TASK KEYS (still remaining, should be re-batched): {rolled_back_keys}
REMAINING TASK KEYS (the only keys batches may contain): {remaining_keys}

Your job:
1. FAILURE ANALYSIS: For each failed task, explain WHY it failed in one sentence.
   Read the error output carefully. Distinguish:
   - environment issue (wrong test framework, missing dependency, config problem)
   - code bug (logic error, wrong API usage, missing implementation)
   - spec ambiguity (task description unclear, multiple valid interpretations)
   - infrastructure error (timeout, OOM, network — retry same approach)

2. RETRY STRATEGIES: For each failed task that will be retried:
   - "retry" = same approach, the failure was transient (infrastructure)
   - "retry_different" = must change approach, include specific guidance
   - "skip" = can't succeed (dependency failed, fundamentally impossible)

3. ROUTED CONTEXT: What should specific upcoming tasks know?
   If a passed task discovered something relevant to an upcoming task, route it.
   Be specific: "task X should know that the project uses vitest, not jest"
   Only include genuinely useful context — don't route everything to everyone.

4. SKIP TASKS: Which upcoming tasks should be skipped?
   Only skip if a task's core dependency failed AND can't be worked around.
   Don't skip aggressively — tasks may partially succeed even with failed deps.

5. BATCHING: Re-order remaining tasks only.
   Do NOT include permanently failed tasks in batches — they are already terminal.
   ROLLED_BACK tasks are still in the remaining set and should be included.
   Respect dependencies, parallelize independent tasks, keep batch count minimal.

6. NEW LEARNINGS: Any patterns you inferred from the failures that future tasks
   should know about. Only factual observations, not speculation.

Return JSON only:
{{
  "failure_analysis": {{"task_key": "one sentence why it failed"}},
  "retry_strategies": {{
    "task_key": {{
      "action": "retry|retry_different|skip",
      "guidance": "specific advice for the retry agent",
      "reason": "why this strategy"
    }}
  }},
  "routed_context": [
    {{"target_task": "task_key", "context": "what to tell this task"}}
  ],
  "skip_tasks": ["task_key_if_any"],
  "new_learnings": ["factual observations from the failure pattern"],
  "batches": [
    {{"tasks": [{{"task_key": "key"}}]}}
  ]
}}
"""


async def invoke_pilot(
    batch_results: list[TaskResult],
    remaining_plan: ExecutionPlan,
    context: PipelineContext,
    config: dict[str, Any],
    project_dir: Path,
    *,
    batch_failed_keys: set[str],
    batch_rolled_back_keys: set[str],
    pending_by_key: dict[str, Any],
) -> PilotDecision:
    """Invoke gate pilot at batch boundary. Returns structured decision."""

    pilot_context = assemble_pilot_context(
        batch_results=batch_results,
        remaining_plan=remaining_plan,
        context=context,
        project_dir=project_dir,
        pending_by_key=pending_by_key,
    )

    remaining_keys_list = [
        tp.task_key
        for batch in remaining_plan.batches
        for tp in batch.tasks
    ]
    remaining_keys = ", ".join(remaining_keys_list)

    prompt = BATCH_PILOT_PROMPT.format(
        context=pilot_context,
        failed_keys=", ".join(sorted(batch_failed_keys)) or "none",
        rolled_back_keys=", ".join(sorted(batch_rolled_back_keys)) or "none",
        remaining_keys=remaining_keys or "none",
    )

    _pilot_log(
        project_dir,
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pilot invoked",
        f"batch_failed_keys: {sorted(batch_failed_keys)}",
        f"batch_rolled_back_keys: {sorted(batch_rolled_back_keys)}",
        f"remaining_keys: {remaining_keys_list}",
    )

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=_planner_settings(config),
        env=_subprocess_env(),
        effort=_planner_effort(config),
        system_prompt={"type": "preset", "preset": "claude_code"},
    )
    model = _planner_model(config)
    if model:
        options.model = model

    started_at = time.monotonic()
    raw_output, cost, _result = await run_agent_query(prompt, options)
    cost_usd = float(cost or 0.0)
    duration_s = round(time.monotonic() - started_at, 1)

    decision = parse_pilot_decision(raw_output)

    _pilot_log(
        project_dir,
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pilot decision (${cost_usd:.3f}, {duration_s:.1f}s)",
        json.dumps(asdict(decision), indent=2),
        "",
    )

    return decision


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_pilot_decision(raw: str) -> PilotDecision:
    """Parse pilot LLM output into a PilotDecision.

    Robust to markdown fences, extra text, missing fields.
    """
    # Strip markdown fences
    text = raw.strip()
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            stripped = part.strip()
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
            if stripped.startswith("{"):
                text = stripped
                break

    # Find JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in pilot output: {text[:200]}")

    data = json.loads(text[start:end + 1])

    # Parse with defaults for missing fields
    failure_analysis = data.get("failure_analysis", {})
    if not isinstance(failure_analysis, dict):
        failure_analysis = {}

    retry_strategies: dict[str, RetryStrategy] = {}
    for key, val in data.get("retry_strategies", {}).items():
        if isinstance(val, dict):
            retry_strategies[key] = RetryStrategy(
                action=val.get("action", "retry"),
                guidance=val.get("guidance", ""),
                reason=val.get("reason", ""),
            )

    skip_tasks = data.get("skip_tasks", [])
    if not isinstance(skip_tasks, list):
        skip_tasks = []

    routed_context: list[RoutedContext] = []
    for item in data.get("routed_context", []):
        if isinstance(item, dict) and "target_task" in item and "context" in item:
            routed_context.append(RoutedContext(
                target_task=item["target_task"],
                context=item["context"],
            ))

    new_learnings = data.get("new_learnings", [])
    if not isinstance(new_learnings, list):
        new_learnings = []

    batches = data.get("batches", [])
    if not isinstance(batches, list):
        batches = []

    return PilotDecision(
        failure_analysis=failure_analysis,
        retry_strategies=retry_strategies,
        skip_tasks=skip_tasks,
        routed_context=routed_context,
        new_learnings=[str(l) for l in new_learnings],
        batches=batches,
    )
