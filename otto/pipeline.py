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

    # Certify -> Fix -> Re-certify loop
    # Runs in a subprocess because the Claude SDK installs signal handlers
    # that only work in the main thread. The subprocess gets its own main
    # thread and runs the full verification loop (certify → fix → re-certify).
    if not build_config.get("skip_product_qa"):
        build_config.setdefault("proof_of_work", True)

        import subprocess as _sp
        import sys as _sys

        verify_payload = json.dumps({
            "intent": certifier_intent,
            "project_dir": str(project_dir),
            "tasks_path": str(tasks_path),
            "product_spec_path": str(certifier_grounding_path) if certifier_grounding_path else None,
            "config": build_config,
        }, default=str)

        certify_result = _sp.run(
            [_sys.executable, "-m", "otto.certifier._verify_subprocess"],
            input=verify_payload,
            capture_output=True, text=True,
            cwd=str(project_dir),
            timeout=int(build_config.get("certifier_timeout", 900)),
        )
        if certify_result.returncode == 0 and certify_result.stdout.strip():
            # Last line of stdout is the JSON result
            verify_result = json.loads(certify_result.stdout.strip().split("\n")[-1])
        else:
            logger.warning("Verification subprocess failed (exit %d): %s",
                          certify_result.returncode, certify_result.stderr[-500:])
            verify_result = {"product_passed": False, "total_cost": 0.0}
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
    """Run the full certify → fix → re-certify loop synchronously.

    Called from run_in_executor (already in a thread). Uses nest_asyncio
    to allow nested event loops — the SDK internally uses asyncio.run()
    which installs signal handlers, and new_event_loop alone doesn't
    prevent that.
    """
    import asyncio
    import nest_asyncio
    from otto.verification import run_product_verification

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    nest_asyncio.apply(loop)
    try:
        return loop.run_until_complete(run_product_verification(
            product_spec_path=product_spec_path,
            project_dir=project_dir,
            tasks_path=tasks_path,
            config=config,
            intent=intent,
        ))
    finally:
        loop.close()


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


CONTINUOUS_SYSTEM_PROMPT = """\
You are building a product. Build it, write tests, make them pass.
When you're done building, provide your final status as structured output.

Rules:
- Explore any existing codebase first
- Write comprehensive tests for your code
- Make all tests pass before finishing
- Do not invent features not in the intent
"""

CONTINUOUS_BUILD_PROMPT = """\
Build this product:

{intent}
"""

CONTINUOUS_FIX_PROMPT = """\
A user tested your product and found these issues:

{feedback}
"""


async def build_continuous(
    intent: str,
    project_dir: Path,
    config: dict[str, Any],
    *,
    on_human_feedback: Any = None,
) -> BuildResult:
    """Continuous build: session-continuous mode with certifier feedback.

    The orchestrator drives the loop, injects feedback.
    The coding agent keeps its session across build→certify→fix cycles.
    No session killing, no fix tasks, no context loss.
    """

    from otto.agent import ClaudeAgentOptions, _subprocess_env
    from otto.certifier.isolated import certify_with_retry
    from otto.certifier.report import CertificationOutcome
    from otto.feedback import format_certifier_as_feedback, finding_fingerprints
    from otto.git_ops import _snapshot_untracked, check_clean_tree
    from otto.session import AgentSession

    build_id = f"build-{int(time.time())}-{os.getpid()}"
    build_dir = project_dir / "otto_logs" / "builds" / build_id
    build_dir.mkdir(parents=True, exist_ok=True)

    # Write and commit build artifacts (intent.md, otto.yaml) BEFORE clean-start check.
    # These are created by the pipeline/CLI and should be committed, not flagged.
    grounding_path = project_dir / "intent.md"
    if not grounding_path.exists():
        grounding_path.write_text(intent)
    _commit_artifacts(project_dir)

    # Now check for a clean workspace (after our own artifacts are committed)
    pre_existing_untracked = _snapshot_untracked(project_dir)
    if not check_clean_tree(project_dir):
        raise RuntimeError(
            "Continuous build requires a clean working tree. "
            "Commit or stash your changes before running otto build --continuous."
        )
    from otto.git_ops import _should_stage_untracked
    eligible_untracked = {f for f in pre_existing_untracked if _should_stage_untracked(f)}
    if eligible_untracked:
        raise RuntimeError(
            f"Continuous build requires no pre-existing untracked source files. "
            f"Found: {', '.join(sorted(eligible_untracked)[:5])}. "
            f"Add them to .gitignore or commit them first."
        )

    # Configure agent
    max_rounds = int(config.get("max_verification_rounds", 3))

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        system_prompt=CONTINUOUS_SYSTEM_PROMPT,
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
    build_prompt = CONTINUOUS_BUILD_PROMPT.format(intent=intent)
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

        # Certify in isolated worktree.
        # Run in executor: certifier is sync but internally creates its
        # own event loop for async journey agents.
        import asyncio as _asyncio
        _loop = _asyncio.get_event_loop()
        report = await _loop.run_in_executor(
            None,
            lambda: certify_with_retry(
                intent=intent,
                candidate_sha=candidate_sha,
                project_dir=project_dir,
                config=config,
                port_override=config.get("port_override"),
                skip_story_ids=None,  # TODO: targeted re-verify
            ),
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
        fix_prompt = CONTINUOUS_FIX_PROMPT.format(feedback=feedback)
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


AGENTIC_SYSTEM_PROMPT = """\
You are building a product from scratch. You are an autonomous developer.

1. Read the intent carefully. Plan your approach.
2. Build the product — write code, write tests, make tests pass.
3. When ready, use the certify tool to get real user feedback.
   Run `{certify_help}` to see full usage instructions.
   Quick version: `certify start` → poll `certify status` → read results.
   Poll certify status every 15 seconds (it's instant). Don't sleep for long intervals.
4. If "failed": read the issues, fix them, certify again.
5. If "passed": you're done.
6. If "error": infrastructure problem, NOT your code. Stop and report.

If the progress info says "no progress since last round" — try a different
approach or stop. Don't repeat the same fix.
"""


async def build_agentic(
    intent: str,
    project_dir: Path,
    config: dict[str, Any],
) -> BuildResult:
    """Agentic build: agent drives everything. Calls certify via Bash.

    One session. The agent decides when to request certification using
    a job-based CLI: start (non-blocking) → poll status → read results.
    The certifier runs in an isolated worktree.
    """
    import asyncio
    import sys
    from otto.agent import ClaudeAgentOptions, _subprocess_env, run_agent_query, tool_use_summary
    from otto.certifier.certify_cli import _job_root as _certify_job_root
    from otto.certifier.certify_cli import _latest_job_dir as _latest_certify_job_dir
    from otto.certifier.certify_cli import _read_job as _read_certify_job
    from otto.certifier.timeouts import heartbeat_stale_after_seconds

    build_id = f"build-{int(time.time())}-{os.getpid()}"
    build_dir = project_dir / "otto_logs" / "builds" / build_id
    build_dir.mkdir(parents=True, exist_ok=True)

    # Write intent
    grounding_path = project_dir / "intent.md"
    if not grounding_path.exists():
        grounding_path.write_text(intent)
    _commit_artifacts(project_dir)

    # Build certify commands
    python = sys.executable
    certify_base = f"{python} -m otto.certifier.certify_cli"
    config_flag = f" --config {project_dir / 'otto.yaml'}" if (project_dir / "otto.yaml").exists() else ""

    certify_help = f"{certify_base} help"
    certify_start = f"{certify_base} start {project_dir} {project_dir / 'intent.md'}{config_flag}"
    certify_status = f"{certify_base} status {project_dir}"
    certify_results = f"{certify_base} results {project_dir}"

    prompt = AGENTIC_SYSTEM_PROMPT.format(
        certify_help=certify_help,
    ) + f"""

Now build this product:

{intent}

Certify commands for this project:
  Help:    {certify_help}
  Start:   {certify_start}
  Status:  {certify_status}
  Results: {certify_results}
"""

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        system_prompt={"type": "preset", "preset": "claude_code"},
        env=_subprocess_env(),
        setting_sources=["project"],
    )
    model = config.get("model")
    if model:
        options.model = str(model)

    # Agent session logging — capture all tool calls and text to a log file
    agent_log_path = build_dir / "agent-session.log"
    agent_log_lines: list[str] = []
    _agent_start = time.monotonic()

    def _on_text(text_chunk: str) -> None:
        elapsed = round(time.monotonic() - _agent_start, 1)
        agent_log_lines.append(f"[{elapsed:6.1f}s] {text_chunk}")

    def _on_tool(block) -> None:
        elapsed = round(time.monotonic() - _agent_start, 1)
        summary = tool_use_summary(block)
        agent_log_lines.append(f"[{elapsed:6.1f}s] \u25cf {block.name}  {summary}")

    def _on_tool_result(block) -> None:
        elapsed = round(time.monotonic() - _agent_start, 1)
        content = str(getattr(block, "content", ""))
        truncated = content[:200] + "..." if len(content) > 200 else content
        agent_log_lines.append(f"[{elapsed:6.1f}s]   \u2192 {truncated}")

    # One session — agent drives everything
    text, cost, result_msg = await run_agent_query(
        prompt, options,
        on_text=_on_text,
        on_tool=_on_tool,
        on_tool_result=_on_tool_result,
    )

    # Persist agent session log
    _elapsed_total = round(time.monotonic() - _agent_start, 1)
    agent_log_lines.append(f"[{_elapsed_total:6.1f}s] SESSION END  cost=${cost:.2f}")
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        agent_log_path.write_text(f"<!-- generated: {ts} -->\n" + "\n".join(agent_log_lines))
    except OSError:
        pass

    job_root = _certify_job_root(project_dir)
    latest_job_dir = _latest_certify_job_dir(job_root)
    latest_job_state = _read_certify_job(latest_job_dir) if latest_job_dir else None
    build_error = ""

    if latest_job_dir and latest_job_state and latest_job_state.get("status") == "running":
        wait_timeout_s = float(config.get("agentic_certify_wait_timeout_s") or 0.0)
        if wait_timeout_s <= 0:
            wait_timeout_s = max(600.0, heartbeat_stale_after_seconds(config))
        deadline = time.monotonic() + wait_timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            latest_job_state = _read_certify_job(latest_job_dir)
            if not latest_job_state or latest_job_state.get("status") != "running":
                break
        if latest_job_state and latest_job_state.get("status") == "running":
            build_error = (
                f"Timed out waiting for certification job "
                f"{latest_job_state.get('job_id', latest_job_dir.name)} to finish."
            )

    # Parse result from certify job history
    history_path = job_root / "history.json"
    rounds = 0
    passed = False
    certifier_cost = 0.0
    history_rounds = 0

    if history_path.exists():
        try:
            history = json.loads(history_path.read_text())
            history_rounds = len(history)
            rounds = history_rounds
            certifier_cost = sum(h.get("cost_usd", 0) for h in history)
            if history:
                passed = history[-1].get("status") == "passed"
        except (json.JSONDecodeError, KeyError):
            pass

    if latest_job_state:
        latest_round = int(latest_job_state.get("round", 0) or 0)
        rounds = max(rounds, latest_round)
        passed = latest_job_state.get("status") == "passed"
        if latest_round > history_rounds:
            certifier_cost += float(latest_job_state.get("cost_usd", 0.0) or 0.0)

    return BuildResult(
        passed=passed,
        build_id=build_id,
        rounds=rounds,
        total_cost=cost + certifier_cost,
        error=build_error,
    )


AGENTIC_V2_BUILD_PROMPT = """\
You are a senior developer building a product from scratch. Work autonomously.

## Process

1. **Plan**: Read the intent. Design the architecture — data models, API routes
   or CLI commands, key modules. Think before coding.

2. **Build**: Implement the product. For complex projects:
   - Break into independent modules/features
   - Use the Agent tool to dispatch subagents for parallel work when features
     are independent (e.g., one subagent builds auth, another builds CRUD API)
   - Each subagent gets: what to build, where to put it, interfaces to follow

3. **Test**: Write comprehensive tests. Run them. Fix failures.
   Cover: happy path, edge cases, error handling.

4. **Self-review**: Read your code back. Check for:
   - Missing error handling at boundaries
   - Incomplete features vs the intent
   - Security issues (injection, auth bypass)
   - Fix anything you find.

5. **Commit**: When tests pass and code is clean, commit all files.

## Rules
- Build EVERYTHING the intent asks for. Don't cut scope.
- Write tests BEFORE claiming done.
- Do NOT test the product as a user (a separate certifier will do that).
- Commit when done. One clean commit.
"""


async def build_agentic_v2(
    intent: str,
    project_dir: Path,
    config: dict[str, Any],
) -> BuildResult:
    """Agentic build with certify→fix loop.

    Two agents, builder-blind:
    - Coding agent: builds (session persists across fix rounds via resume)
    - Certifier agent: tests (fresh session each round, blind to builder)

    Loop: build → certify → if failed: resume coding with findings → re-certify
    """
    from otto.agent import ClaudeAgentOptions, _subprocess_env
    from otto.certifier import run_agentic_certifier
    from otto.certifier.report import CertificationOutcome
    from otto.observability import append_text_log
    from otto.session import AgentSession

    build_id = f"build-{int(time.time())}-{os.getpid()}"
    build_dir = project_dir / "otto_logs" / "builds" / build_id
    build_dir.mkdir(parents=True, exist_ok=True)

    grounding_path = project_dir / "intent.md"
    if not grounding_path.exists():
        grounding_path.write_text(intent)
    _commit_artifacts(project_dir)

    max_rounds = int(config.get("max_certify_rounds", 3))

    # Coding agent session (persists across rounds via resume)
    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        system_prompt={"type": "preset", "preset": "claude_code"},
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

    # Session log helper — appends timestamped lines to build-agent.log
    agent_log_path = build_dir / "build-agent.log"

    def _log_build(msg: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        append_text_log(agent_log_path, [f"[{ts}] {msg}"])

    total_cost = 0.0
    last_report = None
    journeys: list[dict[str, Any]] = []
    rounds_run = 0

    for round_num in range(1, max_rounds + 1):
        rounds_run = round_num
        _log_build(f"=== Round {round_num}/{max_rounds} ===")
        logger.info("=== Round %d/%d ===", round_num, max_rounds)

        # ── Coding agent: build or fix ──
        round_start = time.monotonic()
        if round_num == 1:
            build_prompt = AGENTIC_V2_BUILD_PROMPT + f"\n\nBuild this product:\n\n{intent}"
            result = await session.start(build_prompt)
        else:
            # Resume with certifier findings — agent has full context from build
            findings_text = _format_certifier_findings(last_report)
            fix_prompt = (
                f"The product certifier tested your code and found issues:\n\n"
                f"{findings_text}\n\n"
                f"Fix these issues. Make tests pass. Commit when done."
            )
            result = await session.resume(fix_prompt)

        round_duration = round(time.monotonic() - round_start, 1)
        total_cost += result.cost
        _commit_artifacts(project_dir)

        _log_build(f"Round {round_num} coding: {round_duration:.1f}s, ${result.cost:.2f}, "
                   f"session_id={session.session_id}, status={result.end_status}")
        if result.text:
            # Save coding agent output (last 5000 chars for auditability)
            _log_build(f"--- agent output tail ({len(result.text)} chars) ---")
            for line in result.text[-5000:].split("\n"):
                append_text_log(agent_log_path, [f"  {line}"])
            _log_build("--- end agent output ---")

        logger.info("Round %d coding: %.1fs, $%.2f, session_id=%s",
                     round_num, round_duration, result.cost, session.session_id)

        # ── Certifier agent: test (fresh session, blind to builder) ──
        certify_start = time.monotonic()
        report = await run_agentic_certifier(
            intent=intent,
            project_dir=project_dir,
            config=config,
        )
        certify_duration = round(time.monotonic() - certify_start, 1)
        last_report = report
        total_cost += report.cost_usd

        _log_build(f"Round {round_num} certify: {report.outcome.value}, "
                   f"{certify_duration:.1f}s, ${report.cost_usd:.2f}")
        logger.info("Round %d certify: %s, %.1fs, $%.2f",
                     round_num, report.outcome.value, certify_duration, report.cost_usd)

        # Parse journeys for display — use story_results from PoW if available
        journeys = _extract_journeys_from_report(report)

        # Save checkpoint
        session.checkpoint(
            candidate_sha=session._get_head_sha(),
            state="certified" if report.outcome == CertificationOutcome.PASSED else "fixing",
            certifier_outcome=report.outcome.value,
            certifier_cost_so_far=report.cost_usd,
        )

        if report.outcome == CertificationOutcome.PASSED:
            _log_build(f"Certification PASSED on round {round_num}")
            logger.info("Certification passed on round %d", round_num)
            break

        if round_num >= max_rounds:
            _log_build(f"Max rounds ({max_rounds}) reached — certification {report.outcome.value}")
            logger.info("Max rounds (%d) reached", max_rounds)
            break

        # Log findings for fix loop
        for f in report.findings:
            _log_build(f"  finding: [{f.severity}] {f.description}")

    passed = last_report and last_report.outcome == CertificationOutcome.PASSED

    # Count stories for display (tasks_passed/failed maps to stories in agentic mode)
    stories_passed = sum(1 for j in journeys if j.get("passed"))
    stories_failed = sum(1 for j in journeys if not j.get("passed"))

    return BuildResult(
        passed=bool(passed),
        build_id=build_id,
        rounds=rounds_run,
        total_cost=total_cost,
        journeys=journeys,
        tasks_passed=stories_passed,
        tasks_failed=stories_failed,
    )


def _extract_journeys_from_report(report: Any) -> list[dict[str, Any]]:
    """Extract journey dicts from a certifier report for CLI display.

    Reads the _story_results stash (set by run_agentic_certifier) if available,
    falls back to findings.
    """
    story_results = getattr(report, "_story_results", None)
    if story_results:
        return [
            {"name": s.get("summary", s.get("story_id", "")),
             "passed": s["passed"],
             "story_id": s.get("story_id", "")}
            for s in story_results
        ]
    # Fallback: derive from findings (only failures have findings)
    journeys = []
    for f in report.findings:
        sid = getattr(f, "story_id", "")
        journeys.append({"name": f.description, "passed": f.severity == "note", "story_id": sid})
    return journeys


def _format_certifier_findings(report: Any) -> str:
    """Format certifier findings as text for the coding agent's fix prompt."""
    if not report or not report.findings:
        return "No specific findings."
    lines = []
    for f in report.findings:
        sid = getattr(f, "story_id", "")
        prefix = f"[{sid}] " if sid else ""
        lines.append(f"- {prefix}{f.description}")
        if f.diagnosis:
            lines.append(f"  Diagnosis: {f.diagnosis}")
        if f.fix_suggestion:
            lines.append(f"  Fix: {f.fix_suggestion}")
    return "\n".join(lines)


AGENTIC_V3_PROMPT = """\
You are a senior developer building and shipping a product. Work autonomously.

## Process

1. **Plan**: Read the intent. Design the architecture — data models, API routes
   or CLI commands, key modules. Think before coding.

2. **Build**: Implement the product. For complex projects, use the Agent tool
   to dispatch subagents for parallel work on independent features.

3. **Test**: Write comprehensive tests. Run them. Fix failures.

4. **Self-review**: Read your code. Fix issues you find.

5. **Commit**: When tests pass, commit all files.

6. **Certify**: Dispatch a certifier agent to test your product as a real user.
   Use the Agent tool with this EXACT prompt (fill in the intent):

   ```
   Agent("You are a QA lead certifying a software product. Test it thoroughly as a real user.

   Product intent: <PASTE THE FULL INTENT HERE>

   Process:
   1. Read the project — understand what it is, what framework, what files exist
   2. Install dependencies if needed
   3. Start the app if it's a server. For CLI/library, skip this
   4. Plan test stories from this checklist:
      - First Experience: new user uses the core feature
      - CRUD Lifecycle: create, read, update, delete
      - Data Isolation: users' data doesn't leak
      - Persistence: data survives across sessions
      - Access Control: auth required where applicable
      - Search/Filter: find by criteria (if applicable)
      - Edge Cases: empty inputs, special chars, boundaries
      Skip stories that don't apply.
   5. Test using subagents for parallelism (dispatch 3-5 at once)
   6. Report results

   Rules:
   - Make REAL requests (curl, CLI commands, test scripts)
   - For web apps with HTML pages: verify HTML content via curl AND use
     agent-browser (a CLI tool) for visual verification:
       agent-browser record start /tmp/certifier-recording.webm  # start video
       agent-browser open http://localhost:PORT/page
       agent-browser snapshot -i       # accessibility tree with @refs
       agent-browser screenshot /tmp/  # save screenshot per page
       agent-browser click @e3         # interact with elements by ref
       agent-browser record stop       # stop video at end
       agent-browser close             # cleanup when done
     Use agent-browser to verify forms render, elements visible, styles applied.
   - Test the ACTUAL product, never simulate
   - For each failure: report WHAT is wrong and WHERE (symptom + evidence).
     Do NOT suggest fixes — the developer will figure that out.

   End your response with EXACT machine-parsed markers:
   STORIES_TESTED: (number)
   STORIES_PASSED: (number)
   For each story: STORY_RESULT: (story-id) | PASS or FAIL | (one-line summary)
   VERDICT: PASS or FAIL
   DIAGNOSIS: (assessment or null)")
   ```

7. **Read the certifier's findings.** If it reports FAIL:
   - Read each failed story's diagnosis carefully
   - Fix the root causes in your code
   - Run your tests again
   - Commit the fix
   - Run the certifier agent again (same prompt as step 6)
   - Repeat until VERDICT: PASS

8. **Report the final result.** After the certifier passes (or after your best effort),
   you MUST include the certifier's results in your final message. Copy them EXACTLY:

   CERTIFY_ROUND: <round number — 1 for first attempt, 2 for after first fix, etc.>
   STORIES_TESTED: <N>
   STORIES_PASSED: <N>
   STORY_RESULT: <id> | PASS or FAIL | <one-line summary>
   ...
   VERDICT: PASS or FAIL
   DIAGNOSIS: <assessment or null>

   If you ran the certifier multiple times, report ALL rounds:
   CERTIFY_ROUND: 1
   VERDICT: FAIL
   ... (round 1 results)
   CERTIFY_ROUND: 2
   VERDICT: PASS
   ... (round 2 results)

## Rules
- Build EVERYTHING the intent asks for. Don't cut scope.
- Write tests BEFORE claiming done.
- The certifier is your quality gate — don't ship until it passes.
- Commit before each certify run so the certifier sees clean code.
- ALWAYS include the certifier's structured markers in your final message.
"""


async def build_agentic_v3(
    intent: str,
    project_dir: Path,
    config: dict[str, Any],
) -> BuildResult:
    """Fully agent-driven build: one session, certifier as environment.

    The coding agent does everything — build, self-test, dispatch certifier,
    read findings, fix, re-certify. The orchestrator just launches and waits.
    """
    from otto.agent import ClaudeAgentOptions, _subprocess_env, run_agent_query
    from otto.observability import append_text_log

    build_id = f"build-{int(time.time())}-{os.getpid()}"
    build_dir = project_dir / "otto_logs" / "builds" / build_id
    build_dir.mkdir(parents=True, exist_ok=True)

    grounding_path = project_dir / "intent.md"
    if not grounding_path.exists():
        grounding_path.write_text(intent)
    _commit_artifacts(project_dir)

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        system_prompt={"type": "preset", "preset": "claude_code"},
        env=_subprocess_env(),
        setting_sources=["project"],
    )
    model = config.get("model")
    if model:
        options.model = str(model)

    prompt = AGENTIC_V3_PROMPT + f"\n\nBuild this product:\n\n{intent}"

    logger.info("Starting agentic v3 build: %s", build_id)
    start_time = time.monotonic()

    # One agent call — the agent drives everything.
    # capture_tool_output=True so subagent output (certifier results) is included
    # in the returned text for parsing.
    text, cost, result_msg = await run_agent_query(
        prompt, options, capture_tool_output=True)

    total_duration = round(time.monotonic() - start_time, 1)

    # Save agent output in two forms:
    # 1. agent-raw.log — full unfiltered output (for deep debugging)
    # 2. agent.log — structured summary: what was built, certifier results,
    #    fixes applied, timing. Enough to debug without reading raw.
    agent_log_path = build_dir / "agent.log"
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        # Raw output — write once, full content
        (build_dir / "agent-raw.log").write_text(text or "(no output)")

        summary_lines = [
            f"[{ts}] === Agentic v3 build ===",
            f"[{ts}] Duration: {total_duration:.1f}s, Cost: ${cost:.2f}",
            f"[{ts}] Raw output: {len(text or '')} chars → agent-raw.log",
        ]

        # Extract structured events from agent text
        if text:
            # Git commits = what was built/fixed
            import subprocess as _sp
            try:
                git_log = _sp.run(
                    ["git", "log", "--oneline", "--no-walk", "--all"],
                    cwd=str(project_dir), capture_output=True, text=True,
                ).stdout.strip()
                if git_log:
                    summary_lines.append(f"[{ts}] Git commits:")
                    for line in git_log.split("\n"):
                        summary_lines.append(f"[{ts}]   {line}")
            except Exception:
                pass

            # Certifier markers + diagnosis + failed story details
            for line in text.split("\n"):
                stripped = line.strip()
                if any(stripped.startswith(m) for m in (
                    "CERTIFY_ROUND:", "STORIES_TESTED:", "STORIES_PASSED:",
                    "VERDICT:", "DIAGNOSIS:",
                )):
                    summary_lines.append(f"[{ts}]   {stripped}")
                elif stripped.startswith("STORY_RESULT:"):
                    # Always log failures; log passes concisely
                    if "FAIL" in stripped.upper():
                        summary_lines.append(f"[{ts}]   {stripped}")
                    else:
                        summary_lines.append(f"[{ts}]   {stripped[:120]}")

            # Agent's own summary text (last ~500 chars of TextBlock content,
            # which is the agent's final message after certifier results)
            # Look for the agent's wrap-up after the last VERDICT
            last_verdict_idx = text.rfind("VERDICT:")
            if last_verdict_idx >= 0:
                tail = text[last_verdict_idx:].strip()
                # Skip the markers, get the prose after
                prose_lines = []
                past_markers = False
                for line in tail.split("\n"):
                    s = line.strip()
                    if past_markers and s and not s.startswith(("STORY_RESULT:", "STORIES_", "VERDICT:", "DIAGNOSIS:", "CERTIFY_ROUND:")):
                        prose_lines.append(s)
                    if s.startswith("DIAGNOSIS:"):
                        past_markers = True
                if prose_lines:
                    summary_lines.append(f"[{ts}] Agent summary:")
                    for p in prose_lines[:10]:  # cap at 10 lines
                        summary_lines.append(f"[{ts}]   {p[:200]}")

        append_text_log(agent_log_path, summary_lines)
    except Exception:
        logger.warning("Failed to write agent log")

    # Parse certification results from agent output.
    # The agent repeats the certifier's structured markers in its final message.
    # If multiple rounds, we take the LAST round's results (the final state).
    # We also track all rounds for the PoW report.
    stories_tested = 0
    stories_passed = 0
    story_results: list[dict[str, Any]] = []
    story_evidence: dict[str, str] = {}
    verdict_pass = False
    overall_diagnosis = ""
    certify_rounds: list[dict[str, Any]] = []
    max_round = 0

    if text:
        # Extract evidence blocks
        current_eid: str | None = None
        ev_lines: list[str] = []
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("STORY_EVIDENCE_START:"):
                current_eid = stripped.split(":", 1)[1].strip()
                ev_lines = []
            elif stripped.startswith("STORY_EVIDENCE_END:") and current_eid:
                story_evidence[current_eid] = "\n".join(ev_lines)
                current_eid = None
            elif current_eid is not None:
                ev_lines.append(line)

        # Parse per-round blocks. Each CERTIFY_ROUND starts a new round.
        # Within each round, collect STORY_RESULTs and VERDICT.
        current_round: dict[str, Any] = {"round": 0, "stories": [], "verdict": None, "diagnosis": ""}
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("CERTIFY_ROUND:"):
                # Save previous round if it had results
                if current_round["stories"] or current_round["verdict"] is not None:
                    certify_rounds.append(current_round)
                try:
                    rn = int(stripped.split(":", 1)[1].strip())
                except ValueError:
                    rn = len(certify_rounds) + 1
                max_round = max(max_round, rn)
                current_round = {"round": rn, "stories": [], "verdict": None, "diagnosis": ""}
            elif stripped.startswith("STORIES_TESTED:"):
                try:
                    current_round["tested"] = int(stripped.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif stripped.startswith("STORIES_PASSED:"):
                try:
                    current_round["passed_count"] = int(stripped.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif stripped.startswith("STORY_RESULT:"):
                parts = stripped[len("STORY_RESULT:"):].strip().split("|")
                if len(parts) >= 2:
                    sid = parts[0].strip()
                    passed = "PASS" in parts[1].upper()
                    summary = parts[2].strip() if len(parts) > 2 else ""
                    current_round["stories"].append({
                        "story_id": sid,
                        "passed": passed,
                        "summary": summary,
                        "evidence": story_evidence.get(sid, ""),
                    })
            elif stripped.startswith("VERDICT:"):
                current_round["verdict"] = "PASS" in stripped.upper()
            elif stripped.startswith("DIAGNOSIS:"):
                diag_text = stripped[len("DIAGNOSIS:"):].strip()
                if diag_text.lower().startswith("null"):
                    diag_text = diag_text[4:].strip()
                current_round["diagnosis"] = diag_text

        # Save last round
        if current_round["stories"] or current_round["verdict"] is not None:
            certify_rounds.append(current_round)

        # Use the LAST round with stories as the final result
        final_round = None
        for r in reversed(certify_rounds):
            if r["stories"]:
                final_round = r
                break

        if final_round:
            story_results = final_round["stories"]
            stories_tested = final_round.get("tested", len(story_results))
            stories_passed = final_round.get("passed_count", sum(1 for s in story_results if s["passed"]))
            verdict_pass = bool(final_round.get("verdict", False))
            overall_diagnosis = final_round.get("diagnosis", "")
        else:
            # Fallback: scan from end for verdict/diagnosis (no CERTIFY_ROUND markers)
            for line in reversed(text.split("\n")):
                stripped = line.strip()
                if stripped.startswith("VERDICT:") and not verdict_pass:
                    verdict_pass = "PASS" in stripped.upper()
                elif stripped.startswith("DIAGNOSIS:") and not overall_diagnosis:
                    diag = stripped[len("DIAGNOSIS:"):].strip()
                    if diag.lower().startswith("null"):
                        diag = diag[4:].strip()
                    if diag:
                        overall_diagnosis = diag
                if verdict_pass and overall_diagnosis:
                    break
            # Also extract STORY_RESULTs from flat output (no round markers)
            for line in text.split("\n"):
                stripped = line.strip()
                if stripped.startswith("STORIES_TESTED:"):
                    try:
                        stories_tested = int(stripped.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                elif stripped.startswith("STORIES_PASSED:"):
                    try:
                        stories_passed = int(stripped.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                elif stripped.startswith("STORY_RESULT:"):
                    parts = stripped[len("STORY_RESULT:"):].strip().split("|")
                    if len(parts) >= 2:
                        sid = parts[0].strip()
                        p = "PASS" in parts[1].upper()
                        summary = parts[2].strip() if len(parts) > 2 else ""
                        story_results.append({
                            "story_id": sid, "passed": p, "summary": summary,
                            "evidence": story_evidence.get(sid, ""),
                        })

    passed = verdict_pass and all(s["passed"] for s in story_results)

    journeys = [
        {"name": s.get("summary", s["story_id"]), "passed": s["passed"], "story_id": s["story_id"]}
        for s in story_results
    ]

    # Write PoW report
    try:
        from otto.certifier import _generate_agentic_html_pow
        report_dir = project_dir / "otto_logs" / "certifier"
        report_dir.mkdir(parents=True, exist_ok=True)

        pow_data = {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "outcome": "passed" if passed else "failed",
            "duration_s": total_duration,
            "cost_usd": float(cost or 0),
            "stories": story_results,
            "certify_rounds": len(certify_rounds),
            "round_history": [
                {"round": r.get("round", i+1), "verdict": r.get("verdict"),
                 "stories_count": len(r.get("stories", [])),
                 "passed_count": r.get("passed_count", 0)}
                for i, r in enumerate(certify_rounds)
            ] if len(certify_rounds) > 1 else [],
            "mode": "agentic_v3",
        }
        (report_dir / "proof-of-work.json").write_text(
            json.dumps(pow_data, indent=2, default=str))

        # Build round_history for HTML from certify_rounds
        html_round_history = [
            {"round": r.get("round", i+1), "verdict": r.get("verdict"),
             "stories_count": len(r.get("stories", [])),
             "passed_count": r.get("passed_count", sum(1 for s in r.get("stories", []) if s.get("passed")))}
            for i, r in enumerate(certify_rounds)
        ] if certify_rounds else []

        _generate_agentic_html_pow(
            report_dir, story_results,
            "passed" if passed else "failed",
            total_duration, float(cost or 0),
            stories_passed, stories_tested,
            diagnosis=overall_diagnosis,
            round_history=html_round_history,
        )
    except Exception as exc:
        logger.warning("Failed to write PoW: %s", exc)

    # Checkpoint
    checkpoint = {
        "build_id": build_id,
        "mode": "agentic_v3",
        "passed": passed,
        "duration_s": total_duration,
        "cost_usd": float(cost or 0),
        "stories_tested": stories_tested,
        "stories_passed": stories_passed,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (build_dir / "checkpoint.json").write_text(json.dumps(checkpoint, indent=2))

    logger.info("Agentic v3 done: %s, %d/%d stories, %.1fs, $%.2f",
                "passed" if passed else "failed",
                stories_passed, stories_tested, total_duration, float(cost or 0))

    return BuildResult(
        passed=passed,
        build_id=build_id,
        rounds=1,  # single session — no explicit rounds
        total_cost=float(cost or 0),
        journeys=journeys,
        tasks_passed=sum(1 for j in journeys if j["passed"]),
        tasks_failed=sum(1 for j in journeys if not j["passed"]),
    )


async def resume_continuous(
    checkpoint_path: Path,
    project_dir: Path,
    config: dict[str, Any],
    *,
    on_human_feedback: Any = None,
) -> BuildResult:
    """Resume a continuous build from a saved checkpoint."""
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
        system_prompt=CONTINUOUS_SYSTEM_PROMPT,
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
            "Resumed continuous build requires a clean working tree. "
            "Commit or stash your changes before resuming."
        )
    from otto.git_ops import _should_stage_untracked
    eligible_untracked = {f for f in pre_existing_untracked if _should_stage_untracked(f)}
    if eligible_untracked:
        raise RuntimeError(
            f"Resumed continuous build requires no pre-existing untracked source files. "
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
            result = await session.resume(CONTINUOUS_FIX_PROMPT.format(feedback=feedback))
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

        import asyncio as _asyncio
        _loop = _asyncio.get_event_loop()
        report = await _loop.run_in_executor(
            None,
            lambda: certify_with_retry(
                intent=session.intent,
                candidate_sha=candidate_sha,
                project_dir=project_dir,
                config=config,
                port_override=config.get("port_override"),
                skip_story_ids=None,
            ),
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

        result = await session.resume(CONTINUOUS_FIX_PROMPT.format(feedback=feedback))
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
            "Candidate snapshot refused because the repo already had eligible "
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
    files = []
    for name in ["tasks.yaml", "otto.yaml", "intent.md", "product-spec.md", "architecture.md"]:
        if (project_dir / name).exists():
            files.append(name)
    if not files:
        return
    subprocess.run(["git", "add"] + files, cwd=project_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "otto: build artifacts"],
        cwd=project_dir, capture_output=True,
    )
