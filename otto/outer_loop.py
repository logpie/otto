"""Otto product verification loop.

After a successful build, verify the product works for real users.
If verification fails, generate targeted fix tasks and re-run.

Everything is "add tasks and run" — fix tasks go through the same
inner loop as the initial build. The certifier is just a task generator.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from otto.observability import append_text_log

logger = logging.getLogger("otto.outer_loop")


def _outer_log(project_dir: Path, *lines: str) -> None:
    append_text_log(project_dir / "otto_logs" / "outer-loop.log", lines)


def _bundle_fix_tasks(failed_journeys: list[dict[str, Any]]) -> str:
    """Bundle all failed journeys into a single fix task prompt."""
    if len(failed_journeys) == 1:
        return _fix_task_from_journey(failed_journeys[0])

    lines = [f"Fix {len(failed_journeys)} product issues found by user journey testing:\n"]
    for i, story in enumerate(failed_journeys, 1):
        lines.append(f"--- Issue {i}: {story.get('name', 'unknown')} ---")
        if story.get("diagnosis"):
            lines.append(f"Diagnosis: {story['diagnosis']}")
        if story.get("fix_suggestion"):
            lines.append(f"Suggested fix: {story['fix_suggestion']}")
        failed_steps = [s for s in story.get("steps", []) if s.get("outcome") == "fail"]
        if failed_steps:
            lines.append("Failed steps:")
            for step in failed_steps:
                lines.append(f"  - {step.get('action', '?')}")
                if step.get("diagnosis"):
                    lines.append(f"    {step['diagnosis']}")
        lines.append("")

    lines.append(
        "Fix all issues above. Do not change the product spec or scope. "
        "See product-spec.md for the full product definition."
    )
    return "\n".join(lines)


def _fix_task_from_journey(story: dict[str, Any]) -> str:
    """Build a targeted fix task prompt from a failed journey story.

    Includes: what failed, where it failed, diagnosis, suggested fix,
    and failing step details — so the coding agent has full context.
    """
    name = story.get("name", "unknown story")
    diagnosis = story.get("diagnosis", "")
    fix_suggestion = story.get("fix_suggestion", "")
    blocked_at = story.get("blocked_at", "")
    steps = story.get("steps", [])

    lines = [f"Fix product issue: user story '{name}' failed"]
    if blocked_at:
        lines[0] += f" (blocked at: {blocked_at})"

    if diagnosis:
        lines.append(f"\nDiagnosis: {diagnosis}")
    if fix_suggestion:
        lines.append(f"\nSuggested fix: {fix_suggestion}")

    # Include failing step details for targeted context
    failed_steps = [s for s in steps if s.get("outcome") == "fail"]
    if failed_steps:
        lines.append("\nFailed steps:")
        for step in failed_steps:
            lines.append(f"- {step.get('action', '?')}")
            if step.get("diagnosis"):
                lines.append(f"  Diagnosis: {step['diagnosis']}")
            if step.get("fix_suggestion"):
                lines.append(f"  Fix: {step['fix_suggestion']}")

    lines.append(
        "\nFix the specific failure. Do not change the product spec or scope. "
        "See product-spec.md for the full product definition."
    )
    return "\n".join(lines)


async def run_product_verification(
    product_spec_path: Path,
    project_dir: Path,
    tasks_path: Path,
    config: dict[str, Any],
    *,
    intent: str = "",
    max_rounds: int = 3,
) -> dict[str, Any]:
    """Verify product → if fails → add fix tasks → re-run → re-verify.

    The inner loop (run_per) is the only execution engine. This function
    just decides what to verify and what fix tasks to create.

    Returns dict with:
        product_passed, rounds, total_cost, journeys, fix_tasks_created.
    """
    import asyncio

    from otto.certifier import run_certifier_v2
    from otto.orchestrator import run_per
    from otto.tasks import add_task, load_tasks

    total_cost = 0.0
    fix_tasks_created = 0
    last_result: dict[str, Any] = {}
    prev_failure_count = 0
    port_override = config.get("port_override")

    for round_num in range(1, max_rounds + 1):
        _outer_log(
            project_dir,
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] verification round {round_num}/{max_rounds}",
        )

        # Run any pending fix tasks through the inner loop (same pipeline as build)
        pending = [t for t in load_tasks(tasks_path) if t.get("status") == "pending"]
        if pending:
            _outer_log(project_dir, f"running {len(pending)} fix task(s)")
            exit_code = await run_per(config, tasks_path, project_dir)
            if exit_code != 0:
                _outer_log(project_dir, f"fix tasks failed (exit {exit_code})")
                return {
                    "product_passed": False,
                    "rounds": round_num,
                    "total_cost": total_cost,
                    "journeys": last_result.get("journeys", []),
                    "fix_tasks_created": fix_tasks_created,
                    "inner_loop_failed": True,
                }

        # Certify: run journey agents against the product
        _outer_log(project_dir, "running certifier (user journey verification)")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: run_certifier_v2(
                intent=intent,
                project_dir=project_dir,
                config=config,
                port_override=port_override,
            ),
        )
        total_cost += result.get("cost_usd", 0.0)
        last_result = result

        _outer_log(
            project_dir,
            f"certifier: {result.get('duration_s', 0):.0f}s, "
            f"${result.get('cost_usd', 0):.2f}",
        )

        # Passed — done
        if result.get("product_passed"):
            _outer_log(project_dir, f"PASSED (round {round_num})")
            return {
                "product_passed": True,
                "rounds": round_num,
                "total_cost": total_cost,
                "journeys": result.get("journeys", []),
                "fix_tasks_created": fix_tasks_created,
            }

        # Failed — generate fix tasks
        failed_journeys = [
            j for j in result.get("journeys", [])
            if not j.get("passed")
        ]
        failure_count = len(failed_journeys)

        _outer_log(
            project_dir,
            f"FAILED (round {round_num}): {failure_count} journey(s) failed",
            f"  stories: {[j.get('name') for j in failed_journeys]}",
        )

        # Stop conditions
        if round_num >= max_rounds:
            _outer_log(project_dir, f"max rounds ({max_rounds}) reached")
            break

        if round_num > 1 and failure_count >= prev_failure_count > 0:
            _outer_log(
                project_dir,
                f"no progress ({prev_failure_count} → {failure_count} failures)",
            )
            break

        prev_failure_count = failure_count

        # Bundle all failures into one fix task (one agent, one pass)
        fix_prompt = _bundle_fix_tasks(failed_journeys)
        add_task(tasks_path, fix_prompt)
        fix_tasks_created += 1
        _outer_log(
            project_dir,
            f"  fix task created ({len(failed_journeys)} failure(s) bundled)",
        )

    return {
        "product_passed": False,
        "rounds": round_num,  # noqa: F821 — loop always runs at least once
        "total_cost": total_cost,
        "journeys": last_result.get("journeys", []),
        "fix_tasks_created": fix_tasks_created,
    }
