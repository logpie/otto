"""Otto build pipeline — plan, build, certify, fix, verify."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("otto.pipeline")


@dataclass
class BuildMode:
    """Resolved build mode — requested then finalized after planning."""
    use_planner: bool = False
    parallel: bool = False
    grounding: str = "intent"  # "intent" or "spec"

    def finalize(self, plan_mode: str) -> BuildMode:
        """Finalize mode after planner output is known."""
        return BuildMode(
            use_planner=self.use_planner,
            parallel=self.parallel and plan_mode != "single_task",
            grounding=self.grounding,
        )


@dataclass
class BuildResult:
    """Result of the entire build pipeline."""
    passed: bool
    build_id: str
    rounds: int = 1
    total_cost: float = 0.0
    journeys: list[dict[str, Any]] = field(default_factory=list)
    break_findings: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    tasks_passed: int = 0
    tasks_failed: int = 0


def resolve_build_mode(config: dict[str, Any]) -> BuildMode:
    """Resolve build mode from config + flags."""
    execution_mode = str(config.get("execution_mode", "monolithic") or "monolithic").strip().lower()
    use_planner = config.get("use_planner", execution_mode == "planned")
    parallel = execution_mode == "planned" and int(config.get("max_parallel", 1)) > 1
    grounding = "spec" if use_planner else "intent"
    return BuildMode(use_planner=use_planner, parallel=parallel, grounding=grounding)


async def build_product(
    intent: str,
    project_dir: Path,
    config: dict[str, Any],
    *,
    on_progress: Any = None,
) -> BuildResult:
    """The entire otto build pipeline: plan -> build -> certify -> fix -> verify."""
    import asyncio
    from otto.tasks import add_tasks

    build_id = f"build-{int(time.time())}-{os.getpid()}"
    build_dir = project_dir / "otto_logs" / "builds" / build_id
    build_dir.mkdir(parents=True, exist_ok=True)
    total_cost = 0.0
    build_config = dict(config)
    build_config["build_id"] = build_id

    mode = resolve_build_mode(config)
    tasks_path = project_dir / "tasks.yaml"

    # Grounding: write intent to project root for reference
    grounding_path = project_dir / "intent.md"
    if not grounding_path.exists():
        grounding_path.write_text(intent)
    certifier_grounding_path = grounding_path

    # Plan (optional)
    if mode.use_planner:
        from otto.product_planner import run_product_planner
        plan = await run_product_planner(intent, project_dir, config)
        total_cost += plan.cost_usd
        mode = mode.finalize(plan.mode)

        # Certifier grounding = product-spec.md (what planner wrote)
        certifier_grounding_path = plan.product_spec_path or grounding_path
        certifier_intent = certifier_grounding_path.read_text()

        tasks = [
            {"prompt": t.prompt, "depends_on": t.depends_on if t.depends_on else None}
            for t in plan.tasks
        ]

        # Persist plan manifest (immutable sidecar)
        manifest = {
            "build_id": build_id,
            "mode": mode.grounding,
            "fingerprint": _plan_fingerprint(plan, project_dir),
            "task_count": len(tasks),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        (build_dir / "plan-manifest.json").write_text(json.dumps(manifest, indent=2))
    else:
        # Monolithic: intent IS the task and the grounding
        certifier_intent = intent
        tasks = [{"prompt": f"Build the product described below.\n\n{intent}"}]

    # Persist artifacts before worktree creation
    add_tasks(tasks_path, tasks, build_id=build_id)

    _commit_artifacts(project_dir)

    # Skip per-task LLM QA when product verification handles product validation.
    # The coding agent's own tests still run (test_command).
    if not build_config.get("skip_product_qa"):
        build_config["skip_qa"] = True
        build_config["skip_spec"] = True

    # Build
    from otto.orchestrator import run_per
    exit_code = await run_per(build_config, tasks_path, project_dir)
    total_cost += _read_last_run_cost(project_dir)

    tasks_passed, tasks_failed = _build_task_counts(tasks_path, build_id)

    if exit_code != 0 and tasks_failed > 0 and build_config.get("skip_product_qa"):
        return BuildResult(
            passed=False, build_id=build_id, error="build failed",
            total_cost=total_cost, tasks_passed=tasks_passed, tasks_failed=tasks_failed,
        )

    # Certify -> Fix -> Verify
    if not build_config.get("skip_product_qa"):
        build_config.setdefault("proof_of_work", True)

        loop = asyncio.get_event_loop()
        verify_result = await loop.run_in_executor(
            None,
            lambda: _run_verification_sync(
                certifier_intent,
                certifier_grounding_path,
                project_dir,
                tasks_path,
                build_config,
            ),
        )
        total_cost += verify_result.get("total_cost", 0.0)
        verification_passed = bool(verify_result.get("product_passed", False))
        tasks_passed, tasks_failed = _build_task_counts(tasks_path, build_id)

        return BuildResult(
            passed=verification_passed,
            build_id=build_id,
            rounds=verify_result.get("rounds", 1),
            total_cost=total_cost,
            journeys=verify_result.get("journeys", []),
            break_findings=verify_result.get("break_findings", []),
            tasks_passed=tasks_passed,
            tasks_failed=tasks_failed,
        )

    return BuildResult(
        passed=exit_code == 0 and tasks_failed == 0, build_id=build_id, total_cost=total_cost,
        tasks_passed=tasks_passed, tasks_failed=tasks_failed,
    )


def _run_verification_sync(
    intent: str,
    product_spec_path: Path,
    project_dir: Path,
    tasks_path: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Run verification synchronously (certifier uses asyncio.run internally)."""
    import asyncio
    from otto.verification import run_product_verification
    from otto.tasks import task_build_scope

    with task_build_scope(config.get("build_id")):
        return asyncio.run(run_product_verification(
            product_spec_path=product_spec_path,
            project_dir=project_dir,
            tasks_path=tasks_path,
            config=config,
            intent=intent,
        ))


def _build_task_counts(tasks_path: Path, build_id: str) -> tuple[int, int]:
    """Count only tasks created for the current build."""
    from otto.tasks import load_tasks

    all_tasks = load_tasks(tasks_path) if tasks_path.exists() else []
    build_tasks = [
        task for task in all_tasks
        if str(task.get("build_id", "") or "") == build_id
    ]
    tasks_passed = sum(1 for task in build_tasks if task.get("status") in ("passed", "merged", "verified"))
    tasks_failed = sum(1 for task in build_tasks if task.get("status") in ("failed", "merge_failed"))
    return tasks_passed, tasks_failed


def _plan_fingerprint(plan: Any, project_dir: Path) -> str:
    """Compute deterministic fingerprint from parsed plan structure."""
    canonical = json.dumps({
        "tasks": [{"prompt": t.prompt, "depends_on": t.depends_on or []} for t in plan.tasks],
        "spec": (plan.product_spec_path.read_text() if plan.product_spec_path and plan.product_spec_path.exists() else ""),
        "arch": ((project_dir / "architecture.md").read_text() if (project_dir / "architecture.md").exists() else ""),
    }, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _read_last_run_cost(project_dir: Path) -> float:
    """Read the build phase cost from the last run-history.jsonl entry.

    run_per() records cost to run-history.jsonl but only returns an exit code.
    This reads the last entry to recover the build phase cost.
    """
    history_file = project_dir / "otto_logs" / "run-history.jsonl"
    if not history_file.exists():
        return 0.0
    try:
        last_line = ""
        with open(history_file) as f:
            for line in f:
                if line.strip():
                    last_line = line.strip()
        if last_line:
            entry = json.loads(last_line)
            return float(entry.get("cost_usd", 0.0))
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    return 0.0


VARIANT_B_SYSTEM_PROMPT = """\
You are building a product. Build it, write tests, make them pass.
When you're done building, provide your final status as structured output.

Rules:
- Explore any existing codebase first
- Write comprehensive tests for your code
- Make all tests pass before finishing
- Do not invent features not in the intent
"""

VARIANT_B_BUILD_PROMPT = """\
Build this product:

{intent}
"""

VARIANT_B_FIX_PROMPT = """\
A user tested your product and found these issues:

{feedback}
"""


async def build_agent_driven(
    intent: str,
    project_dir: Path,
    config: dict[str, Any],
    *,
    variant: str = "b",
    on_human_feedback: Any = None,
) -> BuildResult:
    """Agent-driven build: continuous session with certifier feedback.

    Variant B (default): orchestrator drives the loop, injects feedback.
    Variant A: agent calls certify() tool — fully agentic.

    The coding agent keeps its session across build→certify→fix cycles.
    No session killing, no fix tasks, no context loss.
    """
    if variant == "a":
        return await _build_variant_a(intent, project_dir, config)

    from otto.agent import ClaudeAgentOptions, _subprocess_env
    from otto.certifier.isolated import certify_with_retry
    from otto.certifier.report import CertificationOutcome
    from otto.feedback import format_certifier_as_feedback, finding_fingerprints
    from otto.git_ops import _snapshot_untracked, check_clean_tree
    from otto.session import AgentSession

    build_id = f"build-{int(time.time())}-{os.getpid()}"
    build_dir = project_dir / "otto_logs" / "builds" / build_id
    build_dir.mkdir(parents=True, exist_ok=True)
    pre_existing_untracked = _snapshot_untracked(project_dir)

    # Fail early on dirty workspace — don't waste a build turn
    if not check_clean_tree(project_dir):
        raise RuntimeError(
            "Agent-driven build requires a clean working tree. "
            "Commit or stash your changes before running otto build --agent-driven."
        )
    # Filter to eligible untracked (non-otto-owned source files)
    from otto.git_ops import _should_stage_untracked
    eligible_untracked = {f for f in pre_existing_untracked if _should_stage_untracked(f)}
    if eligible_untracked:
        raise RuntimeError(
            f"Agent-driven build requires no pre-existing untracked source files. "
            f"Found: {', '.join(sorted(eligible_untracked)[:5])}. "
            f"Add them to .gitignore or commit them first."
        )

    # Write intent to project root
    grounding_path = project_dir / "intent.md"
    if not grounding_path.exists():
        grounding_path.write_text(intent)
    _commit_artifacts(project_dir)

    # Configure agent
    max_rounds = int(config.get("max_verification_rounds", 3))

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        system_prompt=VARIANT_B_SYSTEM_PROMPT,
        env=_subprocess_env(),
        setting_sources=["project"],
    )
    model = config.get("model")
    if model:
        options.model = str(model)

    session = AgentSession(
        intent=intent,
        options=options,
        project_dir=project_dir,
        config=config,
        checkpoint_dir=build_dir,
    )

    # Round 0: Build
    build_prompt = VARIANT_B_BUILD_PROMPT.format(intent=intent)
    result = await session.start(build_prompt)
    session.checkpoint(
        None,
        state="building",
        verification_round=1,
        last_status=result.end_status,
    )

    report = None
    certifier_total_cost = 0.0
    checkpoint_findings: list[dict[str, Any]] | None = None
    prev_fingerprints: set[str] = set()
    last_journeys: list[dict[str, Any]] = []
    last_break_findings: list[dict[str, Any]] = []

    for round_num in range(1, max_rounds + 1):
        # Check agent's end state
        status = result.end_status
        if status == "blocked":
            break
        if status == "needs_human_input":
            if on_human_feedback:
                human = await on_human_feedback(None)
                if human:
                    result = await session.resume(human)
                    session.checkpoint(
                        None,
                        findings=checkpoint_findings,
                        state="building",
                        verification_round=round_num,
                        last_status=result.end_status,
                        certifier_cost_so_far=certifier_total_cost,
                        journeys=last_journeys,
                        break_findings=last_break_findings,
                    )
                    continue
            break

        # Snapshot candidate
        candidate_sha = _snapshot_candidate(
            project_dir,
            round_num,
            session.base_sha,
            pre_existing_untracked=pre_existing_untracked,
        )
        session.checkpoint(
            candidate_sha,
            findings=checkpoint_findings,
            state="certifying",
            verification_round=round_num,
            last_status=result.end_status,
            certifier_cost_so_far=certifier_total_cost,
            journeys=last_journeys,
            break_findings=last_break_findings,
        )

        # Certify in isolated worktree
        report = certify_with_retry(
            intent=intent,
            candidate_sha=candidate_sha,
            project_dir=project_dir,
            config=config,
            port_override=config.get("port_override"),
            skip_story_ids=None,  # TODO: targeted re-verify
        )
        certifier_total_cost += float(report.cost_usd or 0.0)
        checkpoint_findings = _report_findings_payload(report)

        # Extract display data
        tier4 = next((t for t in report.tiers if t.tier == 4), None)
        if tier4 and hasattr(tier4, "_stories_output"):
            last_journeys = tier4._stories_output
        last_break_findings = [
            {"severity": f.severity, "description": f.description,
             "diagnosis": f.diagnosis, "fix_suggestion": f.fix_suggestion,
             "story_id": f.story_id}
            for f in report.break_findings()
        ]
        session.checkpoint(
            candidate_sha,
            findings=checkpoint_findings,
            state="certified",
            certifier_outcome=report.outcome.value,
            verification_round=round_num,
            last_status=result.end_status,
            certifier_cost_so_far=certifier_total_cost,
            journeys=last_journeys,
            break_findings=last_break_findings,
        )

        # Outcome dispatch
        if report.outcome == CertificationOutcome.PASSED:
            if on_human_feedback:
                human = await on_human_feedback(report)
                if human:
                    result = await session.resume(
                        f"Product passed testing. The user has additional feedback:\n{human}"
                    )
                    session.checkpoint(
                        candidate_sha,
                        findings=checkpoint_findings,
                        state="fixing",
                        verification_round=round_num + 1,
                        last_status=result.end_status,
                        certifier_cost_so_far=certifier_total_cost,
                        journeys=last_journeys,
                        break_findings=last_break_findings,
                    )
                    continue
            break

        if report.outcome in (CertificationOutcome.BLOCKED, CertificationOutcome.INFRA_ERROR):
            break

        # Format actionable findings as feedback
        feedback = format_certifier_as_feedback(report)
        if not feedback:
            break

        # No-progress check
        current_fps = finding_fingerprints(report.critical_findings())
        if round_num > 1 and current_fps == prev_fingerprints:
            break
        prev_fingerprints = current_fps

        # Human feedback (if interactive)
        if on_human_feedback:
            human = await on_human_feedback(report)
            if human:
                feedback += f"\n\nAdditional feedback from the user:\n{human}"

        # Resume session with feedback
        fix_prompt = VARIANT_B_FIX_PROMPT.format(feedback=feedback)
        result = await session.resume(fix_prompt)
        session.checkpoint(
            candidate_sha,
            findings=checkpoint_findings,
            state="fixing",
            verification_round=round_num + 1,
            last_status=result.end_status,
            certifier_cost_so_far=certifier_total_cost,
            journeys=last_journeys,
            break_findings=last_break_findings,
        )

    passed = report.passed if report else False
    return BuildResult(
        passed=passed,
        build_id=build_id,
        rounds=round_num if report else 0,
        total_cost=session.total_cost + certifier_total_cost,
        journeys=last_journeys,
        break_findings=last_break_findings,
    )


VARIANT_A_SYSTEM_PROMPT = """\
You are building a product from scratch. You are an autonomous developer.

1. Read the intent carefully. Plan your approach.
2. Build the product — write code, write tests, make tests pass.
3. When ready, call certify() to get user feedback on your product.
4. Read the feedback. Fix issues. Call certify() again.
5. Repeat until it passes or you've addressed all issues.

certify() submits your current code for user testing and returns feedback.
Each call takes several minutes. Use it when you believe the product is ready.

certify() returns {status, issues, warnings}:
- status "passed": product works, you're done.
- status "failed": issues found, fix them and certify again.
- status "error": testing infrastructure failed, NOT a code bug. Stop and report.
  Do NOT attempt code fixes for "error" status.
"""


async def _build_variant_a(
    intent: str,
    project_dir: Path,
    config: dict[str, Any],
) -> BuildResult:
    """Variant A: agent calls certify() as a tool. Fully agentic."""
    from otto.certifier.mcp_tool import CertifyTool

    _ = CertifyTool(
        project_dir=project_dir,
        intent=intent,
        config=config,
        max_calls=int(config.get("max_verification_rounds", 3)),
    )
    raise NotImplementedError(
        "Variant A is not yet implemented. It requires MCP/custom tool handler support for certify(). "
        "The CertifyTool code is preserved for that future integration."
    )


async def resume_agent_driven(
    checkpoint_path: Path,
    project_dir: Path,
    config: dict[str, Any],
    *,
    on_human_feedback: Any = None,
) -> BuildResult:
    """Resume an agent-driven Variant B build from a saved checkpoint."""
    from otto.agent import ClaudeAgentOptions, _subprocess_env
    from otto.certifier.isolated import certify_with_retry
    from otto.certifier.report import CertificationOutcome
    from otto.feedback import finding_fingerprints
    from otto.git_ops import _snapshot_untracked, check_clean_tree
    from otto.session import AgentSession, SessionCheckpoint

    checkpoint_path = Path(checkpoint_path)
    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        system_prompt=VARIANT_B_SYSTEM_PROMPT,
        env=_subprocess_env(),
        setting_sources=["project"],
    )
    model = config.get("model")
    if model:
        options.model = str(model)

    session = AgentSession(
        intent="",
        options=options,
        project_dir=project_dir,
        config=config,
        checkpoint_dir=checkpoint_path.parent,
    )
    cp = session.load_checkpoint() if checkpoint_path.name == "checkpoint.json" else SessionCheckpoint.load(checkpoint_path)
    if cp is None:
        raise FileNotFoundError(f"No valid checkpoint found at {checkpoint_path}")

    session.intent = cp.intent
    session.session_id = cp.session_id
    session.base_sha = cp.base_sha
    session.round = int(cp.round or 0)
    session.last_summary = cp.last_summary
    session.total_cost = float(
        cp.agent_cost_so_far or max(float(cp.cost_so_far or 0.0) - float(cp.certifier_cost_so_far or 0.0), 0.0)
    )

    build_id = checkpoint_path.parent.name
    pre_existing_untracked = _snapshot_untracked(project_dir)

    # Same clean-start enforcement as fresh builds
    if not check_clean_tree(project_dir):
        raise RuntimeError(
            "Resumed agent-driven build requires a clean working tree. "
            "Commit or stash your changes before resuming."
        )
    from otto.git_ops import _should_stage_untracked
    eligible_untracked = {f for f in pre_existing_untracked if _should_stage_untracked(f)}
    if eligible_untracked:
        raise RuntimeError(
            f"Resumed agent-driven build requires no pre-existing untracked source files. "
            f"Found: {', '.join(sorted(eligible_untracked)[:5])}. "
            f"Add them to .gitignore or commit them first."
        )

    certifier_total_cost = float(
        cp.certifier_cost_so_far or max(float(cp.cost_so_far or 0.0) - session.total_cost, 0.0)
    )
    checkpoint_findings = cp.findings
    prev_fingerprints = _checkpoint_fingerprints(cp.findings)
    last_journeys = list(cp.journeys or [])
    last_break_findings = list(cp.break_findings or [])
    current_status = cp.last_status or "ready_for_review"
    round_num = int(cp.verification_round or 1)
    report = None

    if cp.state == "certified":
        if cp.certifier_outcome == CertificationOutcome.PASSED.value:
            return BuildResult(
                passed=True,
                build_id=build_id,
                rounds=round_num,
                total_cost=session.total_cost + certifier_total_cost,
                journeys=last_journeys,
                break_findings=last_break_findings,
            )
        if cp.certifier_outcome in (
            CertificationOutcome.BLOCKED.value,
            CertificationOutcome.INFRA_ERROR.value,
        ):
            return BuildResult(
                passed=False,
                build_id=build_id,
                rounds=round_num,
                total_cost=session.total_cost + certifier_total_cost,
                journeys=last_journeys,
                break_findings=last_break_findings,
            )
        feedback = _format_checkpoint_feedback(cp.findings)
        if feedback:
            result = await session.resume(VARIANT_B_FIX_PROMPT.format(feedback=feedback))
            round_num += 1
            current_status = result.end_status
            session.checkpoint(
                cp.candidate_sha,
                findings=checkpoint_findings,
                state="fixing",
                verification_round=round_num,
                last_status=current_status,
                certifier_cost_so_far=certifier_total_cost,
                journeys=last_journeys,
                break_findings=last_break_findings,
            )
        else:
            return BuildResult(
                passed=False,
                build_id=build_id,
                rounds=round_num,
                total_cost=session.total_cost + certifier_total_cost,
                journeys=last_journeys,
                break_findings=last_break_findings,
            )

    max_rounds = int(config.get("max_verification_rounds", 3))
    while round_num <= max_rounds:
        if current_status == "blocked":
            break
        if current_status == "needs_human_input":
            if on_human_feedback:
                human = await on_human_feedback(None)
                if human:
                    result = await session.resume(human)
                    current_status = result.end_status
                    session.checkpoint(
                        cp.candidate_sha,
                        findings=checkpoint_findings,
                        state="fixing" if cp.state == "fixing" else "building",
                        verification_round=round_num,
                        last_status=current_status,
                        certifier_cost_so_far=certifier_total_cost,
                        journeys=last_journeys,
                        break_findings=last_break_findings,
                    )
                    continue
            break

        if cp.state == "certifying" and round_num == int(cp.verification_round or 1):
            candidate_sha = cp.candidate_sha
        else:
            candidate_sha = _snapshot_candidate(
                project_dir,
                round_num,
                session.base_sha,
                pre_existing_untracked=pre_existing_untracked,
            )
            session.checkpoint(
                candidate_sha,
                findings=checkpoint_findings,
                state="certifying",
                verification_round=round_num,
                last_status=current_status,
                certifier_cost_so_far=certifier_total_cost,
                journeys=last_journeys,
                break_findings=last_break_findings,
            )

        if not candidate_sha:
            break

        report = certify_with_retry(
            intent=session.intent,
            candidate_sha=candidate_sha,
            project_dir=project_dir,
            config=config,
            port_override=config.get("port_override"),
            skip_story_ids=None,
        )
        certifier_total_cost += float(report.cost_usd or 0.0)
        checkpoint_findings = _report_findings_payload(report)

        tier4 = next((t for t in report.tiers if t.tier == 4), None)
        if tier4 and hasattr(tier4, "_stories_output"):
            last_journeys = tier4._stories_output
        last_break_findings = [
            {"severity": f.severity, "description": f.description,
             "diagnosis": f.diagnosis, "fix_suggestion": f.fix_suggestion,
             "story_id": f.story_id}
            for f in report.break_findings()
        ]
        session.checkpoint(
            candidate_sha,
            findings=checkpoint_findings,
            state="certified",
            certifier_outcome=report.outcome.value,
            verification_round=round_num,
            last_status=current_status,
            certifier_cost_so_far=certifier_total_cost,
            journeys=last_journeys,
            break_findings=last_break_findings,
        )

        if report.outcome == CertificationOutcome.PASSED:
            break
        if report.outcome in (CertificationOutcome.BLOCKED, CertificationOutcome.INFRA_ERROR):
            break

        feedback = _format_checkpoint_feedback(checkpoint_findings)
        if not feedback:
            break

        current_fps = finding_fingerprints(report.critical_findings())
        if round_num > 1 and current_fps == prev_fingerprints:
            break
        prev_fingerprints = current_fps

        if on_human_feedback:
            human = await on_human_feedback(report)
            if human:
                feedback += f"\n\nAdditional feedback from the user:\n{human}"

        result = await session.resume(VARIANT_B_FIX_PROMPT.format(feedback=feedback))
        round_num += 1
        current_status = result.end_status
        session.checkpoint(
            candidate_sha,
            findings=checkpoint_findings,
            state="fixing",
            verification_round=round_num,
            last_status=current_status,
            certifier_cost_so_far=certifier_total_cost,
            journeys=last_journeys,
            break_findings=last_break_findings,
        )
        cp = SessionCheckpoint(
            session_id=session.session_id,
            base_sha=session.base_sha,
            round=session.round,
            verification_round=round_num,
            state="fixing",
            certifier_outcome=report.outcome.value,
            candidate_sha=candidate_sha,
            intent=session.intent,
            last_status=current_status,
            last_summary=session.last_summary,
            findings=checkpoint_findings,
            cost_so_far=session.total_cost + certifier_total_cost,
            agent_cost_so_far=session.total_cost,
            certifier_cost_so_far=certifier_total_cost,
            journeys=last_journeys,
            break_findings=last_break_findings,
        )

    return BuildResult(
        passed=report.passed if report else False,
        build_id=build_id,
        rounds=round_num if report else int(cp.verification_round or 0),
        total_cost=session.total_cost + certifier_total_cost,
        journeys=last_journeys,
        break_findings=last_break_findings,
    )


def _snapshot_candidate(
    project_dir: Path,
    round_num: int,
    base_sha: str,
    *,
    pre_existing_untracked: set[str] | None = None,
) -> str:
    """Create an immutable candidate ref from the agent's current work."""
    from otto.git_ops import _anchor_candidate_ref, _should_stage_untracked, build_candidate_commit

    eligible_pre_existing = sorted(
        rel_path
        for rel_path in (pre_existing_untracked or set())
        if _should_stage_untracked(rel_path)
    )
    if eligible_pre_existing:
        preview = ", ".join(repr(path) for path in eligible_pre_existing[:5])
        if len(eligible_pre_existing) > 5:
            preview += f", ... (+{len(eligible_pre_existing) - 5} more)"
        raise RuntimeError(
            "Agent-driven candidate snapshot refused because the repo already had eligible "
            f"untracked files before the agent run: {preview}"
        )

    # Stage all changes (excluding otto-owned files)
    candidate_sha = build_candidate_commit(
        project_dir,
        base_sha,
        pre_existing_untracked=pre_existing_untracked,
    )
    _anchor_candidate_ref(project_dir, f"build-round-{round_num}", round_num, candidate_sha)
    return candidate_sha


def _report_findings_payload(report: Any) -> list[dict[str, Any]] | None:
    if not getattr(report, "findings", None):
        return None
    return [
        {
            "severity": f.severity,
            "category": f.category,
            "description": f.description,
            "diagnosis": f.diagnosis,
            "fix_suggestion": f.fix_suggestion,
            "story_id": f.story_id,
        }
        for f in report.findings
    ]


def _checkpoint_fingerprints(findings: list[dict[str, Any]] | None) -> set[str]:
    return {
        f"{item.get('category', '')}:{str(item.get('description', ''))[:50]}:{item.get('story_id') or ''}"
        for item in (findings or [])
        if str(item.get("severity", "")) in ("critical", "important")
    }


def _format_checkpoint_feedback(findings: list[dict[str, Any]] | None) -> str | None:
    if not findings:
        return None

    critical = [
        item for item in findings
        if str(item.get("severity", "")) in ("critical", "important")
    ]
    if not critical:
        return None

    lines = ["A user tested your product and found these issues:\n"]
    for i, item in enumerate(critical, 1):
        lines.append(f"{i}. {item.get('description', '')}")
        if item.get("diagnosis"):
            lines.append(f"   What happened: {item['diagnosis']}")
        if item.get("fix_suggestion"):
            lines.append(f"   Suggested fix: {item['fix_suggestion']}")
        lines.append("")

    warnings = [item for item in findings if item.get("category") == "edge-case"]
    if warnings:
        lines.append("Quality warnings (edge cases found during testing):")
        for item in warnings:
            lines.append(f"- [{item.get('severity', 'warning')}] {item.get('description', '')}")
        lines.append("")

    lines.append("Please fix these issues and let me know when you're done.")
    return "\n".join(lines)


def _commit_artifacts(project_dir: Path) -> None:
    """Commit build artifacts so worktrees can see them."""
    files = ["tasks.yaml"]
    for name in ["intent.md", "product-spec.md", "architecture.md"]:
        if (project_dir / name).exists():
            files.append(name)
    subprocess.run(["git", "add"] + files, cwd=project_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "otto: build artifacts"],
        cwd=project_dir, capture_output=True,
    )
