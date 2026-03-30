"""Otto outer loop — plan → execute → verify → fix/replan.

The outer loop wraps the v4.5 inner loop (run_per) with product-level
planning and verification. It implements the i2p feedback cycle:

1. Product planner decomposes intent → tasks
2. Inner loop (run_per) executes tasks
3. Product QA verifies user journeys
4. If QA fails → classify failure → fix task or replan → re-execute → re-verify

The outer loop interacts with the inner loop through three surfaces:
- tasks.yaml (what to execute)
- run_per() (trigger execution)
- project-root files (product-spec.md, architecture.md, context.md)
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


async def run_outer_loop(
    product_spec_path: Path,
    project_dir: Path,
    tasks_path: Path,
    config: dict[str, Any],
    *,
    max_rounds: int = 3,
) -> dict[str, Any]:
    """Run the outer loop: execute → verify → fix/replan → re-verify.

    Args:
        product_spec_path: Path to product-spec.md with user journeys.
        project_dir: Project root.
        tasks_path: Path to tasks.yaml.
        config: Otto config.
        max_rounds: Max product QA rounds (initial + retries).

    Returns dict with:
        product_passed (bool), rounds (int), total_cost (float),
        journeys (list), fix_tasks_created (int).
    """
    from otto.orchestrator import run_per
    from otto.product_qa import run_product_qa
    from otto.tasks import add_task, load_tasks

    total_cost = 0.0
    fix_tasks_created = 0
    last_qa_result: dict[str, Any] = {}

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

        # Product QA
        _outer_log(project_dir, "running product QA")
        prev_failed_count = len([
            j for j in last_qa_result.get("journeys", [])
            if not j.get("passed")
        ])
        qa_result = await run_product_qa(
            product_spec_path=product_spec_path,
            project_dir=project_dir,
            config=config,
        )
        total_cost += qa_result.get("cost_usd", 0.0)
        last_qa_result = qa_result

        if qa_result.get("product_passed"):
            _outer_log(
                project_dir,
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] product QA PASSED (round {round_num})",
                "",
            )
            return {
                "product_passed": True,
                "rounds": round_num,
                "total_cost": total_cost,
                "journeys": qa_result.get("journeys", []),
                "fix_tasks_created": fix_tasks_created,
            }

        # Product QA failed — analyze and decide next action
        failed_journeys = [
            j for j in qa_result.get("journeys", [])
            if not j.get("passed")
        ]

        _outer_log(
            project_dir,
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] product QA FAILED (round {round_num})",
            f"failed journeys: {[j.get('name') for j in failed_journeys]}",
        )

        if round_num >= max_rounds:
            _outer_log(project_dir, f"max rounds ({max_rounds}) reached — stopping")
            break

        # Check if this round made progress (fewer failures than last time)
        curr_failures = len(failed_journeys)

        if round_num > 1 and curr_failures >= prev_failed_count > 0:
            _outer_log(
                project_dir,
                f"no progress (failures: {prev_failed_count} → {curr_failures}) — stopping",
            )
            break

        # Create fix tasks for failed journeys
        for journey in failed_journeys:
            name = journey.get("name", "unknown journey")
            error = journey.get("error", "journey failed")
            evidence = journey.get("evidence", "")

            fix_prompt = (
                f"Fix product QA failure in journey '{name}': {error}"
            )
            if evidence:
                fix_prompt += f"\n\nEvidence from QA:\n{evidence[:2000]}"

            # Add context: point to product-spec.md
            fix_prompt += (
                f"\n\nSee product-spec.md for the full journey definition. "
                f"Fix the specific failure — do not change the product spec or scope."
            )

            add_task(tasks_path, fix_prompt)
            fix_tasks_created += 1
            _outer_log(project_dir, f"created fix task for journey '{name}'")

    return {
        "product_passed": False,
        "rounds": round_num,
        "total_cost": total_cost,
        "journeys": last_qa_result.get("journeys", []),
        "fix_tasks_created": fix_tasks_created,
    }
