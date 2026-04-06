"""Otto product verification — verify the product works for real users.

After a successful build, run certifier journeys against the product.
If verification fails, generate targeted fix tasks and re-run.

Everything is "add tasks and run" — fix tasks go through the same
pipeline as the initial build. The certifier is just a task generator.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from otto.observability import append_text_log

logger = logging.getLogger("otto.verification")


def _verification_log(project_dir: Path, *lines: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    append_text_log(
        project_dir / "otto_logs" / "verification.log",
        [f"[{timestamp}] {line}" for line in lines],
    )


def _grounding_reference(product_spec_path: Path) -> str:
    """Render the grounding file name for fix prompts."""
    return product_spec_path.name or str(product_spec_path)


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

    run_per is the only execution engine. This function just decides
    what to verify and what fix tasks to create.

    Returns dict with:
        product_passed, rounds, total_cost, journeys, fix_tasks_created.
    """
    import asyncio

    from otto.orchestrator import run_per
    from otto.tasks import add_task, load_tasks

    total_cost = 0.0
    fix_tasks_created = 0
    last_journeys: list[dict[str, Any]] = []
    last_break_findings: list[dict[str, Any]] = []
    prev_failure_count = 0
    passed_story_ids: set[str] = set()
    port_override = config.get("port_override")

    for round_num in range(1, max_rounds + 1):
        _verification_log(
            project_dir,
            f"verification round {round_num}/{max_rounds}",
        )

        # Run any pending fix tasks through the same pipeline as build
        pending = [t for t in load_tasks(tasks_path) if t.get("status") == "pending"]
        if pending:
            _verification_log(project_dir, f"running {len(pending)} fix task(s)")
            exit_code = await run_per(config, tasks_path, project_dir)
            if exit_code != 0:
                _verification_log(project_dir, f"fix tasks failed (exit {exit_code})")
                return {
                    "product_passed": False,
                    "rounds": round_num,
                    "total_cost": total_cost,
                    "journeys": last_journeys,
                    "fix_tasks_created": fix_tasks_created,
                    "build_failed": True,
                }

        # Certify
        focus = passed_story_ids if round_num > 1 else None
        if focus:
            _verification_log(project_dir, f"targeted re-verify: skipping {len(focus)} passed stories")
        else:
            _verification_log(project_dir, "running certifier (all stories)")

        loop = asyncio.get_event_loop()
        _focus = focus

        from otto.certifier import run_unified_certifier
        from otto.certifier.report import CertificationOutcome

        report = await loop.run_in_executor(
            None,
            lambda: run_unified_certifier(
                intent=intent,
                project_dir=project_dir,
                config=config,
                port_override=port_override,
                skip_story_ids=_focus,
            ),
        )
        total_cost += report.cost_usd

        # Extract journeys and break findings for display
        tier4 = next((t for t in report.tiers if t.tier == 4), None)
        if tier4 and hasattr(tier4, "_stories_output"):
            last_journeys = tier4._stories_output  # type: ignore[attr-defined]
        last_break_findings = [
            {
                "severity": f.severity,
                "description": f.description,
                "diagnosis": f.diagnosis,
                "fix_suggestion": f.fix_suggestion,
                "story_id": f.story_id,
            }
            for f in report.break_findings()
        ]

        _verification_log(
            project_dir,
            f"certifier: {report.duration_s:.0f}s, ${report.cost_usd:.2f}, "
            f"outcome={report.outcome.value}",
        )

        # Per-tier summary in verification.log for debuggability
        for tier in report.tiers:
            detail = f"{tier.duration_s:.1f}s"
            if tier.cost_usd > 0:
                detail += f", ${tier.cost_usd:.2f}"
            if tier.skip_reason:
                detail += f" ({tier.skip_reason})"
            elif tier.blocked_by:
                detail += f" (blocked by {tier.blocked_by})"
            # Tier 4 extras: story count from raw cert result
            if tier.tier == 4 and hasattr(tier, "_cert_result"):
                cr = tier._cert_result  # type: ignore[attr-defined]
                detail += f", {cr.stories_tested} stories"
            _verification_log(
                project_dir,
                f"  Tier {tier.tier} ({tier.name}): {tier.status.value}, {detail}",
            )

        # Track passed stories from tier 4 results
        if tier4 and hasattr(tier4, "_cert_result"):
            for r in tier4._cert_result.results:  # type: ignore[attr-defined]
                if r.passed:
                    passed_story_ids.add(r.story_id)

        # BLOCKED — not a product bug, stop without fix tasks
        if report.outcome == CertificationOutcome.BLOCKED:
            _verification_log(project_dir, f"BLOCKED (round {round_num})")
            break

        # PASSED — done
        if report.passed:
            _verification_log(project_dir, f"PASSED (round {round_num})")
            return {
                "product_passed": True,
                "rounds": round_num,
                "total_cost": total_cost,
                "journeys": last_journeys,
                "break_findings": last_break_findings,
                "fix_tasks_created": fix_tasks_created,
            }

        # FAILED — use native findings for fix tasks
        # Diagnosis compaction: only critical/important, dedupe by root cause,
        # suppress blocked derivatives
        critical = report.critical_findings()
        failure_count = len(critical)

        _verification_log(
            project_dir,
            f"FAILED (round {round_num}): {failure_count} critical finding(s)",
            f"  findings: {[f.description[:60] for f in critical]}",
        )

        if failure_count == 0:
            _verification_log(project_dir, "no critical findings to fix — stopping")
            break

        if round_num >= max_rounds:
            _verification_log(project_dir, f"max rounds ({max_rounds}) reached")
            break

        if round_num > 1 and failure_count >= prev_failure_count > 0:
            _verification_log(
                project_dir,
                f"no progress ({prev_failure_count} → {failure_count} findings)",
            )
            break

        prev_failure_count = failure_count

        # Build fix prompt from findings (not journey dicts)
        fix_prompt, fix_specs = _bundle_fix_from_findings(critical, product_spec_path)
        add_task(tasks_path, fix_prompt, spec=fix_specs)
        fix_tasks_created += 1
        _verification_log(
            project_dir,
            f"  fix task created ({failure_count} finding(s) bundled)",
        )

    return {
        "product_passed": False,
        "rounds": round_num,  # noqa: F821 — loop always runs at least once
        "total_cost": total_cost,
        "journeys": last_journeys,
        "break_findings": last_break_findings,
        "fix_tasks_created": fix_tasks_created,
    }


def _bundle_fix_from_findings(
    findings: list[Any],
    product_spec_path: Path,
) -> tuple[str, list[dict[str, Any]]]:
    """Build fix task prompt + specs from unified certifier findings.

    Diagnosis compaction: groups by root cause, suppresses duplicates.
    """
    grounding_ref = _grounding_reference(product_spec_path)
    specs = []

    for f in findings:
        if f.fix_suggestion or f.diagnosis:
            specs.append({
                "text": f"{f.description}: {f.fix_suggestion or f.diagnosis}",
                "binding": "must",
                "verifiable": True,
            })

    if len(findings) == 1:
        f = findings[0]
        lines = [f"Fix product issue: {f.description}"]
        if f.diagnosis:
            lines.append(f"\nDiagnosis: {f.diagnosis}")
        if f.fix_suggestion:
            lines.append(f"\nSuggested fix: {f.fix_suggestion}")
        if f.evidence and f.evidence.get("steps"):
            lines.append("\nFailed steps:")
            for s in f.evidence["steps"]:
                lines.append(f"  - {s.get('action', '?')}")
                if s.get("diagnosis"):
                    lines.append(f"    {s['diagnosis']}")
        lines.append(
            f"\nFix the specific failure. Do not change the product spec or scope. "
            f"See {grounding_ref} for the full product definition."
        )
        return "\n".join(lines), specs

    lines = [f"Fix {len(findings)} product issues:\n"]
    for i, f in enumerate(findings, 1):
        lines.append(f"--- Issue {i}: {f.description} ---")
        if f.diagnosis:
            lines.append(f"Diagnosis: {f.diagnosis}")
        if f.fix_suggestion:
            lines.append(f"Suggested fix: {f.fix_suggestion}")
        lines.append("")

    lines.append(
        f"Fix all issues above. Do not change the product spec or scope. "
        f"See {grounding_ref} for the full product definition."
    )
    return "\n".join(lines), specs
