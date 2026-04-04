"""Otto outer loop — plan → execute → verify → fix/replan.

The outer loop wraps the v4.5 inner loop (run_per) with product-level
planning and verification. It implements the i2p feedback cycle:

1. Product planner decomposes intent → tasks
2. Inner loop (run_per) executes tasks
3. Certifier verifies the product (Tier 1 endpoints + Tier 2 journeys)
4. If verification fails → create fix tasks → re-execute → re-verify

The outer loop interacts with the inner loop through three surfaces:
- tasks.yaml (what to execute)
- run_per() (trigger execution)
- project-root files (product-spec.md, architecture.md, context.md)

Verification uses the certifier (compile-then-execute), not a live LLM agent.
Test matrices are compiled once and reused across rounds — re-verification
costs $0 and runs in seconds.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from otto.observability import append_text_log

logger = logging.getLogger("otto.outer_loop")


def _outer_log(project_dir: Path, *lines: str) -> None:
    append_text_log(project_dir / "otto_logs" / "outer-loop.log", lines)


def _collect_failed_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return terminal task failures that should stop the outer loop."""
    failed_statuses = {"failed", "merge_failed", "blocked", "conflict"}
    return [
        {
            "id": task.get("id"),
            "key": task.get("key"),
            "prompt": task.get("prompt"),
            "status": task.get("status"),
            "error": task.get("error"),
        }
        for task in tasks
        if task.get("status") in failed_statuses
    ]


def _failed_tier2_journeys(qa_result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        journey for journey in qa_result.get("journeys", [])
        if not journey.get("passed")
    ]


def _failed_tier1_claims(qa_result: dict[str, Any]) -> list[Any]:
    tier1_result = qa_result.get("tier1_result")
    if not tier1_result or getattr(tier1_result, "certified", True):
        return []

    return [
        claim for claim in getattr(tier1_result, "results", [])
        if not getattr(claim, "passed", False)
        and getattr(claim, "outcome", "") != "not_applicable"
    ]


def _tier1_fix_prompt(claim: Any) -> str:
    claim_id = getattr(claim, "claim_id", "unknown claim")
    description = getattr(claim, "claim_description", "Tier 1 verification failed")
    outcome = getattr(claim, "outcome", "fail")
    error = getattr(claim, "error", "") or "claim failed"

    prompt_lines = [
        f"Fix product verification failure in Tier 1 claim '{claim_id}': {description}",
        f"Outcome: {outcome}",
        f"Error: {error}",
    ]

    evidence = list(getattr(claim, "evidence", []) or [])
    failing_evidence = [item for item in evidence if not getattr(item, "passed", False)]
    relevant_evidence = failing_evidence or evidence[:3]
    if relevant_evidence:
        prompt_lines.append("")
        prompt_lines.append("Evidence:")
        for item in relevant_evidence:
            prompt_lines.append(
                f"- {getattr(item, 'step', '?')}: "
                f"expected {getattr(item, 'expected', '?')}; "
                f"actual {getattr(item, 'actual', '?')}"
            )
            command = getattr(item, "command", "")
            if command:
                prompt_lines.append(f"  Command: {command}")

    prompt_lines.extend([
        "",
        "See product-spec.md for the full product definition. "
        "Fix the specific verification failure without changing product scope.",
    ])
    return "\n".join(prompt_lines)


async def run_outer_loop(
    product_spec_path: Path,
    project_dir: Path,
    tasks_path: Path,
    config: dict[str, Any],
    *,
    intent: str = "",
    max_rounds: int = 3,
) -> dict[str, Any]:
    """Run the outer loop: execute → verify → fix/replan → re-verify.

    Verification uses the certifier v2 (agentic journey verification).
    Story agents simulate real users interacting with the product.

    Args:
        product_spec_path: Path to product-spec.md.
        project_dir: Project root.
        tasks_path: Path to tasks.yaml.
        config: Otto config.
        intent: Product intent string (for certifier).
        max_rounds: Max verification rounds (initial + retries).

    Returns dict with:
        product_passed (bool), rounds (int), total_cost (float),
        journeys (list), fix_tasks_created (int).
    """
    import asyncio

    from otto.certifier import run_certifier_v2
    from otto.orchestrator import run_per
    from otto.tasks import add_task, load_tasks

    total_cost = 0.0
    fix_tasks_created = 0
    last_qa_result: dict[str, Any] = {}

    # Resolve port override from config
    port_override = config.get("port_override")

    for round_num in range(1, max_rounds + 1):
        _outer_log(
            project_dir,
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] outer loop round {round_num}/{max_rounds}",
        )

        # Execute pending tasks (inner loop)
        pending = [t for t in load_tasks(tasks_path) if t.get("status") == "pending"]
        if pending:
            _outer_log(project_dir, f"executing {len(pending)} pending task(s)")
            exit_code = await run_per(config, tasks_path, project_dir)
            if exit_code != 0:
                _outer_log(project_dir, f"inner loop exited with code {exit_code}")
                return {
                    "product_passed": False,
                    "rounds": round_num,
                    "total_cost": total_cost,
                    "journeys": last_qa_result.get("journeys", []),
                    "fix_tasks_created": fix_tasks_created,
                    "inner_loop_failed": True,
                    "failed_tasks": _collect_failed_tasks(load_tasks(tasks_path)),
                }
        else:
            if round_num == 1:
                _outer_log(project_dir, "no pending tasks — skipping execution")

        # Product verification via certifier v2 (agentic journey verification)
        _outer_log(project_dir, "running certifier v2 (agentic journeys)")
        prev_failed_journeys = _failed_tier2_journeys(last_qa_result)
        prev_failed_tier1_claims = _failed_tier1_claims(last_qa_result)
        prev_failed_count = len(prev_failed_journeys) or len(prev_failed_tier1_claims)

        # Certifier v2 is sync (manages its own async internally)
        loop = asyncio.get_event_loop()
        qa_result = await loop.run_in_executor(
            None,
            lambda: run_certifier_v2(
                # Intentionally certify against the raw user intent, not the planner's
                # expanded product-spec.md, so verification stays builder-blind.
                intent=intent,
                project_dir=project_dir,
                config=config,
                port_override=port_override,
            ),
        )
        total_cost += qa_result.get("cost_usd", 0.0)
        last_qa_result = qa_result

        _outer_log(
            project_dir,
            f"certifier: duration={qa_result.get('duration_s', 0)}s, "
            f"cost=${qa_result.get('cost_usd', 0):.3f}",
        )

        if qa_result.get("product_passed"):
            _outer_log(
                project_dir,
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] verification PASSED (round {round_num})",
                "",
            )
            return {
                "product_passed": True,
                "rounds": round_num,
                "total_cost": total_cost,
                "journeys": qa_result.get("journeys", []),
                "fix_tasks_created": fix_tasks_created,
            }

        # Verification failed — analyze and decide next action
        failed_journeys = _failed_tier2_journeys(qa_result)
        failed_tier1_claims = []
        if not failed_journeys:
            failed_tier1_claims = _failed_tier1_claims(qa_result)

        _outer_log(
            project_dir,
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] verification FAILED (round {round_num})",
            f"failed journeys: {[j.get('name') for j in failed_journeys]}",
        )
        if failed_tier1_claims:
            _outer_log(
                project_dir,
                "failed Tier 1 claims: "
                f"{[getattr(claim, 'claim_id', '?') for claim in failed_tier1_claims]}",
            )

        if round_num >= max_rounds:
            _outer_log(project_dir, f"max rounds ({max_rounds}) reached — stopping")
            break

        # Check if this round made progress (fewer failures than last time)
        curr_failures = len(failed_journeys) or len(failed_tier1_claims)

        if round_num > 1 and curr_failures >= prev_failed_count > 0:
            _outer_log(
                project_dir,
                f"no progress (failures: {prev_failed_count} → {curr_failures}) — stopping",
            )
            break

        # Create fix tasks from failed stories (v2: rich diagnosis + fix suggestions)
        for story in failed_journeys:
            name = story.get("name", "unknown story")
            diagnosis = story.get("diagnosis", "")
            fix_suggestion = story.get("fix_suggestion", "")
            blocked_at = story.get("blocked_at", "")
            steps = story.get("steps", [])

            fix_prompt = f"Fix product issue in user story '{name}'"
            if blocked_at:
                fix_prompt += f" (blocked at: {blocked_at})"
            fix_prompt += "\n"

            if diagnosis:
                fix_prompt += f"\nDiagnosis: {diagnosis}\n"
            if fix_suggestion:
                fix_prompt += f"\nSuggested fix: {fix_suggestion}\n"

            # Include failing step details
            failed_steps = [s for s in steps if s.get("outcome") == "fail"]
            if failed_steps:
                fix_prompt += "\nFailed steps:\n"
                for step in failed_steps:
                    fix_prompt += f"- {step.get('action', '?')}\n"
                    if step.get("diagnosis"):
                        fix_prompt += f"  Diagnosis: {step['diagnosis']}\n"
                    if step.get("fix_suggestion"):
                        fix_prompt += f"  Fix: {step['fix_suggestion']}\n"

            fix_prompt += (
                "\nFix the specific failure — do not change the product spec or scope."
            )

            add_task(tasks_path, fix_prompt)
            fix_tasks_created += 1
            _outer_log(project_dir, f"created fix task for story '{name}'")

    return {
        "product_passed": False,
        "rounds": round_num,
        "total_cost": total_cost,
        "journeys": last_qa_result.get("journeys", []),
        "fix_tasks_created": fix_tasks_created,
    }
