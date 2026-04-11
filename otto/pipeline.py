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

    return BuildResult(
        passed=exit_code == 0 and tasks_failed == 0, build_id=build_id, total_cost=total_cost,
        tasks_passed=tasks_passed, tasks_failed=tasks_failed,
    )


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
    """Agentic build with certify->fix loop.

    Two agents, builder-blind:
    - Coding agent: builds (session persists across fix rounds via resume)
    - Certifier agent: tests (fresh session each round, blind to builder)

    Loop: build -> certify -> if failed: resume coding with findings -> re-certify
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

        # -- Coding agent: build or fix --
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

        # -- Certifier agent: test (fresh session, blind to builder) --
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
You are a senior developer. Work autonomously.

## Process

1. **Explore**: Read the project directory. Is there existing code?
   - If YES (existing project): read README, key source files, understand the
     architecture, conventions, test setup. Run existing tests to know the baseline.
   - If NO (empty/new project): skip to step 2.

2. **Plan**: Read the intent.
   - Existing project: plan what to ADD or CHANGE. Identify which files to modify,
     what new files to create, and what existing behavior must not break.
   - New project: design the architecture — data models, API routes or CLI commands.

3. **Build**: Implement.
   - Existing project: follow existing conventions (naming, structure, patterns).
     Don't rewrite what works — add to it.
   - New project: build from scratch. Use subagents for parallel work on
     independent features.

4. **Test**:
   - Run EXISTING tests first (if any). Fix any regressions you introduced.
   - Write NEW tests for the new/changed functionality.
   - All tests must pass before proceeding.

5. **Self-review**: Read your changes. Check for regressions, missing error
   handling, and consistency with existing code style.

6. **Commit**: When all tests pass, commit.

6. **Certify**: Dispatch a certifier agent to test your product as a real user.
   Use the Agent tool with this EXACT prompt (fill in the intent):

   ```
   Agent("You are a QA lead certifying a software product. Test it thoroughly as a real user.

   Product intent: <PASTE THE FULL INTENT HERE>

   Process:
   1. Read the project — understand what it is, what framework, what files exist
   2. Install dependencies if needed
   3. Start the app if it's a server. For CLI/library, skip this
   4. Discover auth (if the app has auth):
      - Register a test user, login, capture the token/cookie
      - Save the EXACT working curl commands — include them in every subagent prompt
      - Do NOT make each subagent figure out auth from scratch
   5. Plan test stories. Include BOTH:
      a) Stories for the NEW/CHANGED functionality (from the intent)
      b) Regression stories for EXISTING functionality — verify nothing is broken
      Use this checklist (skip inapplicable ones):
      - First Experience, CRUD Lifecycle, Data Isolation, Persistence
      - Access Control, Search/Filter, Edge Cases
   6. Dispatch 3-5 subagents for parallel testing. Give each:
      - What to test + what to verify
      - Working auth commands (from step 4) if applicable
      - Base URL / CLI entrypoint / import path
      - Ask it to report PASS/FAIL with key commands and their output
   7. Collect results and report

   Rules:
   - Make REAL requests (curl, CLI commands, test scripts)
   - Never simulate. For failures: report WHAT is wrong + WHERE. No fix suggestions.
   - IMPORTANT: For web apps with HTML pages, you MUST also do visual verification.
     Use the agent-browser CLI tool to take screenshots of key pages:
       agent-browser record start otto_logs/certifier/evidence/recording.webm
       agent-browser open http://localhost:PORT/
       agent-browser screenshot otto_logs/certifier/evidence/homepage.png
       agent-browser open http://localhost:PORT/other-page
       agent-browser screenshot otto_logs/certifier/evidence/other-page.png
       agent-browser record stop
       agent-browser close
     Take at least one screenshot per page. This is REQUIRED for web apps.

   End with EXACT markers:
   STORY_EVIDENCE_START: (id)
   (key commands and output)
   STORY_EVIDENCE_END: (id)
   STORIES_TESTED: N
   STORIES_PASSED: N
   STORY_RESULT: (id) | PASS or FAIL | (summary)
   VERDICT: PASS or FAIL
   DIAGNOSIS: (assessment or null)")
   ```

7. **Read the certifier's findings.** If it reports FAIL:
   - Read each failed story's diagnosis carefully
   - Fix the root causes in your code
   - Run your tests again
   - Commit the fix
   - Re-dispatch the certifier, but this time ADD the failed stories to the prompt:

     "Previous certification found these failures:
     - <story_id>: <one-line summary of what failed>
     - <story_id>: <one-line summary of what failed>

     You MUST re-test these specific failures first to verify they are fixed.
     Then test additional stories for broader coverage."

     Append this BEFORE the "Process:" section in the certifier prompt.
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
- For existing projects: don't break what works. Run existing tests after your changes.
- Write tests for new functionality BEFORE claiming done.
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

    # Append intent to cumulative log
    _append_intent(project_dir, intent, build_id)
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
            f"[{ts}] Raw output: {len(text or '')} chars -> agent-raw.log",
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

    # Append to run history (one line per build for `otto log`)
    from otto.observability import append_text_log
    history_path = project_dir / "otto_logs" / "run-history.jsonl"
    history_entry = json.dumps({
        "build_id": build_id,
        "intent": intent[:200],
        "passed": passed,
        "stories_passed": stories_passed,
        "stories_tested": stories_tested,
        "certify_rounds": len(certify_rounds),
        "cost_usd": round(float(cost or 0), 2),
        "duration_s": total_duration,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    append_text_log(history_path, [history_entry])

    return BuildResult(
        passed=passed,
        build_id=build_id,
        rounds=1,
        total_cost=float(cost or 0),
        journeys=journeys,
        tasks_passed=sum(1 for j in journeys if j["passed"]),
        tasks_failed=sum(1 for j in journeys if not j["passed"]),
    )


def _append_intent(project_dir: Path, intent: str, build_id: str) -> None:
    """Append intent to cumulative log. Preserves history across builds."""
    intent_path = project_dir / "intent.md"
    ts = time.strftime("%Y-%m-%d %H:%M")
    entry = f"\n## {ts} ({build_id})\n{intent}\n"
    if intent_path.exists():
        existing = intent_path.read_text()
        if intent not in existing:
            intent_path.write_text(existing.rstrip() + "\n" + entry)
    else:
        intent_path.write_text(f"# Build Intents\n{entry}")


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
