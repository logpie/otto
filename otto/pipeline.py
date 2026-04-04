"""Otto build pipeline — plan, build, certify, fix, verify."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
    from otto.git_ops import build_candidate_commit
    from otto.session import AgentSession

    build_id = f"build-{int(time.time())}-{os.getpid()}"
    build_dir = project_dir / "otto_logs" / "builds" / build_id
    build_dir.mkdir(parents=True, exist_ok=True)

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

    report = None
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
                    continue
            break

        # Snapshot candidate
        candidate_sha = _snapshot_candidate(project_dir, round_num, session.base_sha)
        session.checkpoint(candidate_sha, state="certifying")

        # Certify in isolated worktree
        report = certify_with_retry(
            intent=intent,
            candidate_sha=candidate_sha,
            project_dir=project_dir,
            config=config,
            port_override=config.get("port_override"),
            skip_story_ids=None,  # TODO: targeted re-verify
        )
        session.checkpoint(
            candidate_sha,
            findings=[{"description": f.description, "severity": f.severity}
                      for f in report.findings] if report.findings else None,
            state="certified",
            certifier_outcome=report.outcome.value,
        )

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

        # Outcome dispatch
        if report.outcome == CertificationOutcome.PASSED:
            if on_human_feedback:
                human = await on_human_feedback(report)
                if human:
                    result = await session.resume(
                        f"Product passed testing. The user has additional feedback:\n{human}"
                    )
                    continue
            break

        if report.outcome in (CertificationOutcome.BLOCKED,):
            break
        # INFRA_ERROR already retried by certify_with_retry
        if hasattr(CertificationOutcome, "INFRA_ERROR") and report.outcome == CertificationOutcome.INFRA_ERROR:
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
        session.checkpoint(candidate_sha, state="fixing")
        fix_prompt = VARIANT_B_FIX_PROMPT.format(feedback=feedback)
        result = await session.resume(fix_prompt)

    passed = report.passed if report else False
    return BuildResult(
        passed=passed,
        build_id=build_id,
        rounds=round_num if report else 0,
        total_cost=session.total_cost + (report.cost_usd if report else 0),
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
    from otto.agent import ClaudeAgentOptions, _subprocess_env, run_agent_query
    from otto.certifier.mcp_tool import CertifyTool

    build_id = f"build-{int(time.time())}-{os.getpid()}"
    build_dir = project_dir / "otto_logs" / "builds" / build_id
    build_dir.mkdir(parents=True, exist_ok=True)

    # Write intent
    grounding_path = project_dir / "intent.md"
    if not grounding_path.exists():
        grounding_path.write_text(intent)
    _commit_artifacts(project_dir)

    # Create certify tool
    certify_tool = CertifyTool(
        project_dir=project_dir,
        intent=intent,
        config=config,
        max_calls=int(config.get("max_verification_rounds", 3)),
    )

    # The agent gets standard CC tools + certify()
    # NOTE: certify() is not a real MCP server in V1.
    # We inject it as a tool via the system prompt and handle it
    # through the SDK's tool_use mechanism. For now, we simulate
    # by using Variant B with the certify prompt — true MCP integration
    # requires SDK support for custom tool handlers.
    #
    # V1 approximation: use Variant B but with Variant A's prompt.
    # The agent believes it has certify() but the orchestrator intercepts
    # session completion as the "certify" signal.

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        system_prompt=VARIANT_A_SYSTEM_PROMPT,
        env=_subprocess_env(),
        setting_sources=["project"],
    )
    model = config.get("model")
    if model:
        options.model = str(model)

    # For true Variant A, we'd need MCP tool handler support in the SDK.
    # V1: fall back to Variant B with Variant A's prompt.
    from otto.feedback import format_certifier_as_feedback, finding_fingerprints
    from otto.certifier.isolated import certify_with_retry
    from otto.certifier.report import CertificationOutcome
    from otto.session import AgentSession

    max_rounds = int(config.get("max_verification_rounds", 3))

    session = AgentSession(
        intent=intent, options=options, project_dir=project_dir,
        config=config, checkpoint_dir=build_dir,
    )

    prompt = f"Build this product:\n\n{intent}\n\nWhen done, say 'ready for review'."
    result = await session.start(prompt)

    report = None
    prev_fps: set[str] = set()
    last_journeys: list[dict[str, Any]] = []
    last_break: list[dict[str, Any]] = []

    for round_num in range(1, max_rounds + 1):
        candidate_sha = _snapshot_candidate(project_dir, round_num, session.base_sha)
        session.checkpoint(candidate_sha, state="certifying")

        report = certify_with_retry(
            intent=intent, candidate_sha=candidate_sha,
            project_dir=project_dir, config=config,
            port_override=config.get("port_override"),
        )
        session.checkpoint(
            candidate_sha, state="certified",
            certifier_outcome=report.outcome.value,
            findings=[{"description": f.description} for f in report.findings] if report.findings else None,
        )

        tier4 = next((t for t in report.tiers if t.tier == 4), None)
        if tier4 and hasattr(tier4, "_stories_output"):
            last_journeys = tier4._stories_output
        last_break = [
            {"severity": f.severity, "description": f.description,
             "diagnosis": f.diagnosis, "fix_suggestion": f.fix_suggestion}
            for f in report.break_findings()
        ]

        if report.outcome == CertificationOutcome.PASSED:
            break
        if report.outcome == CertificationOutcome.BLOCKED:
            break
        if hasattr(CertificationOutcome, "INFRA_ERROR") and report.outcome == CertificationOutcome.INFRA_ERROR:
            break

        feedback = format_certifier_as_feedback(report)
        if not feedback:
            break

        current_fps = finding_fingerprints(report.critical_findings())
        if round_num > 1 and current_fps == prev_fps:
            break
        prev_fps = current_fps

        session.checkpoint(candidate_sha, state="fixing")
        result = await session.resume(
            f"certify() returned:\n\n{feedback}\n\nFix these issues and say 'ready for review' when done."
        )

    passed = report.passed if report else False
    return BuildResult(
        passed=passed, build_id=build_id,
        rounds=round_num if report else 0,
        total_cost=session.total_cost + (report.cost_usd if report else 0),
        journeys=last_journeys, break_findings=last_break,
    )


def _snapshot_candidate(project_dir: Path, round_num: int, base_sha: str) -> str:
    """Create an immutable candidate ref from the agent's current work."""
    # Stage all changes (excluding otto-owned files)
    from otto.git_ops import build_candidate_commit, _anchor_candidate_ref
    candidate_sha = build_candidate_commit(project_dir, base_sha, pre_existing_untracked=set())
    _anchor_candidate_ref(project_dir, f"build-round-{round_num}", round_num, candidate_sha)
    return candidate_sha


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
