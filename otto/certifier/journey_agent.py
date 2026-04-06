"""Journey agent — agentic user story verification.

THE MAIN EVENT of the certifier. For each user story, an agent simulates
a real user interacting with the product. The agent:
1. Reads the story steps and product manifest
2. Executes each step via HTTP API calls (and browser when available)
3. Verifies each step's expected outcome
4. After the happy path, tries to BREAK the product
5. Produces actionable diagnosis for failures

The agent has no turn limit — it works until it reaches a verdict.
Evidence is captured by the runtime, not authored by the agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from otto.agent import (
    ClaudeAgentOptions,
    ResultMessage,
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    _subprocess_env,
    query,
    run_agent_query,
)
from otto.certifier.manifest import ProductManifest, format_manifest_for_agent
from otto.certifier.stories import UserStory
from otto.certifier.timeouts import DEFAULT_HEARTBEAT_GRACE_S, story_timeout_seconds
from otto.observability import append_text_log

logger = logging.getLogger("otto.certifier.journey_agent")


# ---------------------------------------------------------------------------
# Result data structures
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    """Result of verifying one story step."""
    action: str
    outcome: str                    # "pass" | "fail" | "blocked"
    verification: str = ""          # what was checked
    evidence: list[dict[str, Any]] = field(default_factory=list)
    diagnosis: str = ""             # root cause if failed
    fix_suggestion: str = ""        # what to fix if failed


@dataclass
class BreakFinding:
    """A quality issue found during the BREAK phase."""
    technique: str                  # "double_submit", "long_input", etc.
    description: str                # what was tried
    result: str                     # what happened
    severity: str = "minor"         # "critical" | "moderate" | "minor" | "cosmetic"
    fix_suggestion: str = ""


@dataclass
class JourneyResult:
    """Result of verifying one user story."""
    story_id: str
    story_title: str
    persona: str
    passed: bool                    # happy path passed?
    steps: list[StepResult] = field(default_factory=list)
    break_findings: list[BreakFinding] = field(default_factory=list)
    summary: str = ""               # agent's narrative summary
    evidence_chain: list[dict[str, Any]] = field(default_factory=list)  # runtime-captured
    blocked_at: str = ""            # which step broke the flow
    diagnosis: str = ""             # overall root cause
    fix_suggestion: str = ""        # overall fix suggestion
    cost_usd: float = 0.0
    duration_s: float = 0.0


@dataclass
class CertificationResult:
    """Overall certification result from all journeys."""
    intent: str
    product_dir: str
    stories_tested: int
    stories_passed: int
    stories_failed: int
    critical_passed: int
    critical_total: int
    results: list[JourneyResult]
    break_findings: list[BreakFinding]  # aggregated from all stories
    certified: bool
    total_cost_usd: float = 0.0
    total_duration_s: float = 0.0


# ---------------------------------------------------------------------------
# Serialization — for subprocess IPC (JSON files)
# ---------------------------------------------------------------------------


def _journey_result_to_dict(r: JourneyResult) -> dict[str, Any]:
    """Serialize a JourneyResult to a JSON-friendly dict."""
    return asdict(r)


def _journey_result_from_dict(d: dict[str, Any]) -> JourneyResult:
    """Reconstruct a JourneyResult from a dict (JSON round-trip)."""
    steps = [
        StepResult(
            action=s.get("action", ""),
            outcome=s.get("outcome", "blocked"),
            verification=s.get("verification", ""),
            evidence=s.get("evidence", []),
            diagnosis=s.get("diagnosis", ""),
            fix_suggestion=s.get("fix_suggestion", ""),
        )
        for s in d.get("steps", [])
    ]
    break_findings = [
        BreakFinding(
            technique=b.get("technique", ""),
            description=b.get("description", ""),
            result=b.get("result", ""),
            severity=b.get("severity", "minor"),
            fix_suggestion=b.get("fix_suggestion", ""),
        )
        for b in d.get("break_findings", [])
    ]
    return JourneyResult(
        story_id=d.get("story_id", ""),
        story_title=d.get("story_title", ""),
        persona=d.get("persona", ""),
        passed=d.get("passed", False),
        steps=steps,
        break_findings=break_findings,
        summary=d.get("summary", ""),
        evidence_chain=d.get("evidence_chain", []),
        blocked_at=d.get("blocked_at", ""),
        diagnosis=d.get("diagnosis", ""),
        fix_suggestion=d.get("fix_suggestion", ""),
        cost_usd=d.get("cost_usd", 0.0),
        duration_s=d.get("duration_s", 0.0),
    )


def _story_from_dict(d: dict[str, Any]) -> UserStory:
    """Reconstruct a UserStory from a dict (JSON round-trip)."""
    from otto.certifier.stories import StoryStep, UserStory as _UserStory

    steps = [
        StoryStep(
            action=s.get("action", ""),
            verify=s.get("verify", ""),
            verify_in_browser=s.get("verify_in_browser", ""),
            entity=s.get("entity", ""),
            operation=s.get("operation", ""),
            mode=s.get("mode", "api"),
            uses_output_from=s.get("uses_output_from"),
        )
        for s in d.get("steps", [])
    ]
    return _UserStory(
        id=d.get("id", ""),
        persona=d.get("persona", ""),
        title=d.get("title", ""),
        narrative=d.get("narrative", ""),
        steps=steps,
        critical=d.get("critical", False),
        tests_integration=d.get("tests_integration", []),
        break_strategies=d.get("break_strategies", []),
    )


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically — temp file + os.replace to prevent partial writes."""
    import os as _os
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    _os.replace(str(tmp), str(path))


# ---------------------------------------------------------------------------
# Agent prompt
# ---------------------------------------------------------------------------


JOURNEY_AGENT_SYSTEM_PROMPT = """\
You are a QA tester verifying a product works for a real user scenario.
Execute each step using the product manifest for routes, fields, and auth.

RULES:
1. Test the product by ACTUALLY USING it. For HTTP/API products, make real
   requests via curl. For CLI tools, run actual CLI commands via Bash.
   Never simulate or assume responses.
2. Verify outcomes by checking response data, stdout output, or exit codes.
   Check that values, structure, and user context are correct.
3. Carry state between steps. If step 1 returns an ID or token, use it in later steps.
4. If a step fails, DIAGNOSE the root cause: missing endpoint? wrong fields? auth
   issue? server error? Bad exit code? Be specific so a developer can fix it.
5. Continue testing even if a step fails — cover as much as possible.
6. For each failure, suggest a concrete fix (what code change would resolve it).
7. Do NOT use WebFetch for localhost URLs — it cannot reach them. Use curl via Bash.
8. Be efficient. One command per verification, check the result, move on.
9. For CLI tools: run commands, check stdout/stderr and exit codes. State persists
   in the filesystem between commands. Verify data persistence by running follow-up commands.

VERDICT FORMAT — when you finish testing, end with these exact markers:

VERDICT: PASS or VERDICT: FAIL
BLOCKED_AT: <step description or null>
DIAGNOSIS: <one-line root cause or null>
SUGGESTED_FIX: <one-line fix or null>
FAILED_STEP: <action> | <diagnosis>
FAILED_STEP: <action> | <diagnosis>

Include one FAILED_STEP line per failed step. Omit if all passed.
These markers MUST appear in your final message — they are machine-parsed.
"""


def _build_journey_prompt(
    story: UserStory,
    manifest: ProductManifest,
    base_url: str,
    *,
    skip_break: bool = False,
    has_browser: bool = False,
    evidence_dir: str = "./evidence",
) -> str:
    """Build the prompt for the journey verification agent."""
    manifest_text = format_manifest_for_agent(manifest)

    steps_text = ""
    for i, step in enumerate(story.steps):
        dep = f" (uses output from step {step.uses_output_from + 1})" if step.uses_output_from is not None else ""
        browser_check = ""
        if has_browser and step.verify_in_browser:
            browser_check = f"\n   Browser: {step.verify_in_browser}"
        steps_text += f"\n{i + 1}. {step.action}\n   Verify: {step.verify}{dep}{browser_check}"

    break_text = ""
    if story.break_strategies and not skip_break:
        break_text = f"""

BREAK PHASE (after happy path):
Try these strategies to find quality issues:
{chr(10).join(f'- {s}' for s in story.break_strategies)}
Report what you find. These do NOT affect the pass/fail verdict.
"""

    browser_text = ""
    if has_browser:
        browser_text = f"""
BROWSER VERIFICATION:
At the START of testing, begin video recording:
  agent-browser record start {evidence_dir}/recording.webm

After each major step, take a screenshot AND verify the UI:
  agent-browser open <url>
  agent-browser snapshot -i             # accessibility tree with @refs
  agent-browser screenshot {evidence_dir}/  # visual proof per step
  agent-browser click @e3               # interact with elements

At the END of testing (before writing VERDICT), stop recording:
  agent-browser record stop
  agent-browser close

The screenshots and video are evidence for the proof-of-work report.
Verify the UI reflects the API state (created items appear, deleted items gone).
"""

    base_url_line = f"\nBase URL: {base_url}" if base_url else ""

    return f"""\
{story.title} — {story.persona}
{story.narrative}

{manifest_text}{base_url_line}

STEPS:
{steps_text}
{break_text}{browser_text}"""


# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------


async def verify_story(
    story: UserStory,
    manifest: ProductManifest,
    base_url: str,
    project_dir: Path,
    config: dict[str, Any] | None = None,
) -> JourneyResult:
    """Verify a single user story by running a journey agent."""
    import requests as _requests

    config = config or {}
    log_dir = project_dir / "otto_logs" / "certifier"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Health check: verify app is still alive before each story (HTTP only)
    if base_url:
        try:
            r = _requests.get(base_url, timeout=5)
            if r.status_code >= 500:
                return JourneyResult(
                    story_id=story.id, story_title=story.title, persona=story.persona,
                    passed=False, diagnosis=f"App health check failed: HTTP {r.status_code}",
                )
        except Exception as exc:
            return JourneyResult(
                story_id=story.id, story_title=story.title, persona=story.persona,
                passed=False, diagnosis=f"App not responding: {exc}",
            )

    # Verdict file for the agent to write to
    verdict_file = log_dir / f"journey-{story.id}-verdict.json"
    verdict_file.unlink(missing_ok=True)

    import shutil as _shutil
    skip_break = config.get("certifier_skip_break", True)  # default: skip break phase

    # Browser testing: determined by product type, not manual config.
    # Web apps with UI (interaction=browser) need browser verification.
    # Override with certifier_browser: true/false in config.
    if "certifier_browser" in config:
        has_browser = bool(config["certifier_browser"])
    else:
        needs_browser = manifest.interaction in ("browser",)
        has_browser = needs_browser and _shutil.which("agent-browser") is not None

    # Evidence dir: absolute path under log_dir so it persists regardless
    # of execution mode (direct or subprocess worker).
    evidence_dir = (log_dir / f"evidence-{story.id}").resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)

    prompt = _build_journey_prompt(
        story, manifest, base_url,
        skip_break=skip_break, has_browser=has_browser,
        evidence_dir=str(evidence_dir),
    )

    # Configure agent with HTTP tools (Bash for curl) + optional browser (chrome-devtools)
    mcp_servers = {}
    chrome_config = config.get("chrome_mcp")
    if chrome_config:
        mcp_servers["chrome-devtools"] = chrome_config

    # No structured output — verdict extracted from tagged text markers
    # in the agent's natural output. Saves ~22s of model re-reasoning.
    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=["project"],
        env=_subprocess_env(),
        system_prompt=JOURNEY_AGENT_SYSTEM_PROMPT,
    )
    if mcp_servers:
        options.mcp_servers = mcp_servers
    model = config.get("model") or config.get("planner_model")
    if model:
        options.model = str(model)

    started_at = time.monotonic()

    append_text_log(
        log_dir / "journey-agent.log",
        [
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Verifying story: {story.title}",
            f"persona: {story.persona}",
            f"steps: {len(story.steps)}",
            f"break_strategies: {story.break_strategies}",
        ],
    )

    # Session ends naturally when agent stops producing tool calls.
    # Verdict extracted from tagged text markers in the agent's output.
    # Tool calls captured as evidence for PoW audit trail.
    text_parts: list[str] = []
    evidence_chain: list[dict[str, Any]] = []
    cost_usd = 0.0
    _pending_tool: dict[str, Any] | None = None

    stream = query(prompt=prompt, options=options)
    try:
        async for message in stream:
            if isinstance(message, ResultMessage):
                raw_cost = getattr(message, "total_cost_usd", None)
                if isinstance(raw_cost, (int, float)):
                    cost_usd = float(raw_cost)
                break
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text:
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        _pending_tool = {
                            "tool": block.name,
                            "input": str(getattr(block, "input", {}))[:1000],
                            "timestamp": time.strftime("%H:%M:%S"),
                        }
                    elif isinstance(block, ToolResultBlock) and _pending_tool:
                        _pending_tool["output"] = block.content[:1000] if block.content else ""
                        _pending_tool["is_error"] = block.is_error
                        evidence_chain.append(_pending_tool)
                        _pending_tool = None
    finally:
        close_stream = getattr(stream, "aclose", None)
        if callable(close_stream):
            await close_stream()

    duration_s = round(time.monotonic() - started_at, 1)
    raw_output = "".join(text_parts)

    append_text_log(
        log_dir / "journey-agent.log",
        [
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Story completed: {story.title}",
            f"cost: ${cost_usd:.3f}, duration: {duration_s}s",
        ],
    )

    # Parse verdict from tagged text markers in agent output
    result = _parse_tagged_verdict(raw_output, story)
    result.cost_usd = cost_usd
    result.duration_s = duration_s
    result.evidence_chain = evidence_chain

    # Write verdict + evidence for debugging/auditing
    verdict_data = _journey_result_to_dict(result)
    verdict_file.write_text(json.dumps(verdict_data, indent=2))

    # Save raw agent output for debugging
    raw_log = log_dir / f"journey-{story.id}-agent.log"
    raw_log.write_text(raw_output)

    return result


def _parse_tagged_verdict(raw_output: str, story: UserStory) -> JourneyResult:
    """Parse verdict from tagged text markers in agent output.

    Looks for markers like:
        VERDICT: PASS
        DIAGNOSIS: NextAuth CSRF flow broken
        SUGGESTED_FIX: Fix the authorize() callback
        FAILED_STEP: create task | 404 on POST /api/tasks

    Defaults to FAIL if no VERDICT marker found (safe — won't false-pass).
    """

    lines = raw_output.split("\n")

    # Extract tagged values (search from end — verdict is at the bottom)
    def _find_tag(tag: str) -> str:
        for line in reversed(lines):
            stripped = line.strip()
            if stripped.upper().startswith(tag.upper() + ":"):
                return stripped[len(tag) + 1:].strip()
        return ""

    verdict_str = _find_tag("VERDICT")
    diagnosis = _find_tag("DIAGNOSIS")
    fix_suggestion = _find_tag("SUGGESTED_FIX")
    blocked_at = _find_tag("BLOCKED_AT")

    # Parse FAILED_STEP lines
    failed_steps: list[StepResult] = []
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("FAILED_STEP:"):
            rest = stripped[len("FAILED_STEP:"):].strip()
            if "|" in rest:
                action, step_diag = rest.split("|", 1)
                failed_steps.append(StepResult(
                    action=action.strip(), outcome="fail",
                    diagnosis=step_diag.strip()))
            else:
                failed_steps.append(StepResult(
                    action=rest, outcome="fail"))

    # Determine pass/fail — require explicit VERDICT marker.
    # Do NOT infer from loose prose ("pass"/"fail" in text) — too fragile
    # (e.g. "password" contains "pass").
    if verdict_str.upper() in ("PASS", "PASSED"):
        passed = True
    elif verdict_str.upper() in ("FAIL", "FAILED", "BLOCKED"):
        passed = False
    else:
        # No explicit marker — default to FAIL (safe: won't false-pass).
        # The agent was instructed to produce markers; missing = something wrong.
        passed = False
        if not diagnosis:
            diagnosis = "No VERDICT marker found in agent output"

    # Clean up null-like values
    if diagnosis.lower() in ("null", "none", "n/a", ""):
        diagnosis = ""
    if fix_suggestion.lower() in ("null", "none", "n/a", ""):
        fix_suggestion = ""
    if blocked_at.lower() in ("null", "none", "n/a", ""):
        blocked_at = ""

    # Summary from last ~500 chars of output
    summary = raw_output[-500:].strip() if raw_output else "Agent did not produce output"

    return JourneyResult(
        story_id=story.id,
        story_title=story.title,
        persona=story.persona,
        passed=passed,
        steps=failed_steps,
        summary=summary,
        blocked_at=blocked_at,
        diagnosis=diagnosis,
        fix_suggestion=fix_suggestion,
    )


# ---------------------------------------------------------------------------
# Run all stories
# ---------------------------------------------------------------------------


def _story_timeout(story: UserStory, config: dict[str, Any]) -> float:
    """Compute the per-story watchdog timeout from shared certifier settings."""
    return story_timeout_seconds(config, steps=len(story.steps) if story.steps else 3)


def _write_heartbeat(
    project_dir: Path,
    story_title: str,
    stories_completed: int,
    stories_total: int,
    *,
    story_timeout_s: float | None = None,
) -> None:
    """Write a heartbeat file so external watchers know we're alive."""
    heartbeat_path = project_dir / "otto_logs" / "certifier" / "heartbeat.json"
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    import json
    payload: dict[str, Any] = {
        "alive": True,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "current_story": story_title,
        "stories_completed": stories_completed,
        "stories_total": stories_total,
    }
    if story_timeout_s is not None:
        payload["story_timeout_s"] = round(story_timeout_s, 1)
        payload["stale_after_s"] = round(story_timeout_s + DEFAULT_HEARTBEAT_GRACE_S, 1)
    heartbeat_path.write_text(json.dumps(payload))


async def verify_all_stories(
    stories: list[UserStory],
    manifest: ProductManifest,
    base_url: str,
    project_dir: Path,
    config: dict[str, Any] | None = None,
    *,
    on_between_stories: Any | None = None,
) -> CertificationResult:
    """Verify all user stories and produce a certification result.

    on_between_stories: optional callable invoked between stories.
    Used by the certifier to check app health and auto-restart if needed.
    """
    config = config or {}
    all_break_findings: list[BreakFinding] = []
    total_cost = 0.0
    start_time = time.monotonic()

    # Default 3 — each story runs in its own subprocess with its own project
    # copy and app instance, so parallelism is safe. Set to 1 for sequential.
    max_parallel = int(config.get("certifier_parallel_stories", 3))

    async def _run_one_story(i: int, story: UserStory) -> JourneyResult:
        """Run a single story with optional timeout."""
        logger.info("Verifying story: %s (%s)", story.title, story.persona)
        timeout = _story_timeout(story, config)
        _write_heartbeat(project_dir, story.title, i, len(stories), story_timeout_s=timeout)

        if timeout is not None:
            task = asyncio.create_task(
                verify_story(story, manifest, base_url, project_dir, config)
            )
            try:
                return await asyncio.wait_for(task, timeout=timeout)
            except asyncio.TimeoutError:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                logger.warning("Story timed out after %.0fs: %s", timeout, story.title)
                return JourneyResult(
                    story_id=story.id,
                    story_title=story.title,
                    persona=story.persona,
                    passed=False,
                    blocked_at=f"timed out after {int(timeout)}s",
                    summary=f"Story verification timed out after {int(timeout)}s",
                    diagnosis="The journey agent did not complete within the configured deadline.",
                    fix_suggestion="Check if the app is responding.",
                    steps=[],
                    break_findings=[],
                    cost_usd=0.0,
                    duration_s=timeout,
                )
        else:
            return await verify_story(story, manifest, base_url, project_dir, config)

    if max_parallel <= 1:
        # Sequential (original behavior)
        results: list[JourneyResult] = []
        for i, story in enumerate(stories):
            if i > 0 and on_between_stories is not None:
                try:
                    on_between_stories()
                except Exception as exc:
                    logger.warning("Between-story callback failed: %s", exc)
            result = await _run_one_story(i, story)
            results.append(result)
            total_cost += result.cost_usd
            all_break_findings.extend(result.break_findings)
    else:
        # Batched subprocess parallelism: split stories into batches, each batch
        # runs in one subprocess with one agent session and one app instance.
        # Fewer LLM sessions (3 instead of 7), same parallelism.
        import uuid as _uuid
        from otto.certifier.baseline import AppRunner

        logger.info("Running %d stories with max %d parallel (batched subprocess)",
                     len(stories), max_parallel)
        _scavenge_stale_workers(project_dir)
        _scavenge_old_story_runs(project_dir)
        _ensure_deps_installed(project_dir)
        _build_project(project_dir, config)

        # Split stories into batches (one per subprocess)
        batches: list[list[UserStory]] = [[] for _ in range(max_parallel)]
        for i, story in enumerate(stories):
            batches[i % max_parallel].append(story)
        batches = [b for b in batches if b]  # remove empty

        try:
            run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{_uuid.uuid4().hex[:8]}"
            stories_dir = project_dir / "otto_logs" / "certifier" / "stories" / run_id
            stories_dir.mkdir(parents=True, exist_ok=True)

            async def _run_batch(batch_idx: int, batch: list[UserStory]) -> list[JourneyResult]:
                story_dir = stories_dir / f"batch-{batch_idx:02d}"
                story_dir.mkdir(parents=True, exist_ok=True)
                try:
                    return await _run_stories_in_subprocess(
                        batch, project_dir, config, story_dir, manifest.interaction, manifest)
                except Exception as exc:
                    logger.exception("Batch %d subprocess failed", batch_idx)
                    return [JourneyResult(
                        story_id=s.id, story_title=s.title,
                        persona=s.persona, passed=False,
                        diagnosis=f"Subprocess error: {exc}") for s in batch]

            batch_tasks = [_run_batch(i, b) for i, b in enumerate(batches)]
            batch_results = await asyncio.gather(*batch_tasks)
            results = [r for batch in batch_results for r in batch]

            for result in results:
                total_cost += result.cost_usd
                all_break_findings.extend(result.break_findings)
        finally:
            AppRunner.clear_build_metadata(project_dir)

    _write_heartbeat(project_dir, "done", len(stories), len(stories))

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    critical_stories = [s for s in stories if s.critical]
    critical_results = [r for r, s in zip(results, stories) if s.critical]
    critical_passed = sum(1 for r in critical_results if r.passed)

    certified = all(r.passed for r in critical_results) if critical_results else passed > 0

    return CertificationResult(
        intent=stories[0].narrative if stories else "",
        product_dir=str(project_dir),
        stories_tested=len(results),
        stories_passed=passed,
        stories_failed=failed,
        critical_passed=critical_passed,
        critical_total=len(critical_stories),
        results=results,
        break_findings=all_break_findings,
        certified=certified,
        total_cost_usd=total_cost,
        total_duration_s=round(time.monotonic() - start_time, 1),
    )


# ---------------------------------------------------------------------------
# Subprocess-per-story infrastructure
# ---------------------------------------------------------------------------

# Dirs/patterns excluded from per-worker project copies.
# .venv excluded: absolute paths in shebangs/pyvenv.cfg break in copies.
# node_modules INCLUDED: uses relative requires, safe to clone.
WORKER_EXCLUDE_DIRS = {
    ".git", ".venv", "node_modules",
    "otto_logs", ".otto-worktrees", ".otto-workers",
    "__pycache__", ".pytest_cache", ".mypy_cache",
}
# .venv excluded: absolute paths in shebangs/pyvenv.cfg break in copies.
#   AppRunner re-creates per worker.
# node_modules excluded from COPY but SYMLINKED back (see below).
#   Symlink works because Node.js resolves require() relative to the
#   symlink target (the original), so .bin/ scripts resolve correctly.
#   Read-only at runtime — no concurrent write issues.
# NOTE: SQLite files (dev.db) are INCLUDED in copies. Each APFS clone gets
# copy-on-write isolation — writes in one worker don't affect others.
# On non-Mac, copytree creates a real copy, also isolated.
WORKER_EXCLUDE_PATTERNS: tuple[str, ...] = ()


def _shared_worker_paths(project_dir: Path) -> list[Path]:
    """Return project-relative paths that should be shared read-only with workers."""
    from otto.certifier.baseline import AppRunner

    shared: list[Path] = []
    node_modules = Path("node_modules")
    if (project_dir / node_modules).exists():
        shared.append(node_modules)

    metadata = AppRunner.load_build_metadata(project_dir) or {}
    for rel in metadata.get("artifacts", []):
        rel_path = Path(rel)
        if (project_dir / rel_path).exists() and rel_path not in shared:
            shared.append(rel_path)
    return shared


def _create_worker_copy(project_dir: Path, worker_id: str) -> Path:
    """Create a lightweight isolated copy of the project for one story worker.

    Uses APFS clone (cp -c) on macOS for near-instant copy-on-write.
    Falls back to shutil.copytree on other platforms.
    """
    import fnmatch
    import os as _os
    import shutil  # noqa: F811
    import subprocess as _sp
    import sys as _sys  # noqa: F811

    import tempfile as _tmp
    # Workers dir OUTSIDE project — avoids self-referential copy
    # (cp -c -r project project/.otto-workers/... is infinite recursion)
    workers_dir = Path(_tmp.gettempdir()) / "otto-workers" / project_dir.resolve().name
    worker_dir = workers_dir / worker_id
    workers_dir.mkdir(parents=True, exist_ok=True)
    shared_paths = _shared_worker_paths(project_dir)
    shared_rel = {str(path) for path in shared_paths}

    if _sys.platform == "darwin":
        _sp.run(["cp", "-c", "-r", str(project_dir), str(worker_dir)],
                capture_output=True, check=True)
        for d in WORKER_EXCLUDE_DIRS:
            p = worker_dir / d
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink(missing_ok=True)
        for pat in WORKER_EXCLUDE_PATTERNS:
            for f in worker_dir.glob(f"**/{pat}"):
                f.unlink(missing_ok=True)
    else:
        def _ignore(directory: str, contents: list[str]) -> list[str]:
            rel_dir = Path(directory).resolve().relative_to(project_dir.resolve())
            ignored: list[str] = []
            for c in contents:
                rel_path = rel_dir / c if str(rel_dir) != "." else Path(c)
                if c in WORKER_EXCLUDE_DIRS:
                    ignored.append(c)
                    continue
                if str(rel_path) in shared_rel:
                    ignored.append(c)
                    continue
                if any(fnmatch.fnmatch(c, p) for p in WORKER_EXCLUDE_PATTERNS):
                    ignored.append(c)
            return ignored
        shutil.copytree(project_dir, worker_dir, ignore=_ignore,
                        symlinks=True, ignore_dangling_symlinks=True)

    # Remove shared build/runtime artifacts from the cloned copy and symlink
    # them back to the original project. They are read-only during verification
    # and much cheaper to reuse than to duplicate in every worker.
    for rel_path in shared_paths:
        source = project_dir / rel_path
        link_path = worker_dir / rel_path
        if link_path.is_dir() and not link_path.is_symlink():
            shutil.rmtree(link_path, ignore_errors=True)
        elif link_path.exists() or link_path.is_symlink():
            link_path.unlink(missing_ok=True)
        link_path.parent.mkdir(parents=True, exist_ok=True)
        _os.symlink(str(source.resolve()), str(link_path))

    return worker_dir


def _ensure_deps_installed(project_dir: Path) -> None:
    """Ensure dependencies are installed in the original project.

    Node: installs node_modules (for symlink sharing with workers).
    Python: creates .venv + installs requirements (workers create their own).
    Prisma: generates client + pushes schema if needed (capability-based).
    """
    # Node
    pkg_json = project_dir / "package.json"
    node_modules = project_dir / "node_modules"
    if pkg_json.exists() and not node_modules.exists():
        try:
            _run_bootstrap_command(
                ["npm", "install", "--no-audit", "--no-fund"],
                project_dir,
                timeout=120,
                label="npm install",
            )
        except FileNotFoundError:
            logger.warning("npm not found while installing Node deps in %s", project_dir)

    # Python
    req = project_dir / "requirements.txt"
    pyproject = project_dir / "pyproject.toml"
    venv_dir = project_dir / ".venv"
    if (req.exists() or pyproject.exists()) and not venv_dir.exists():
        if _create_python_venv(venv_dir, project_dir):
            _install_python_deps(project_dir, venv_dir / "bin" / "python", req, pyproject)

    # Prisma (capability-based — if schema exists, regardless of product type)
    _ensure_prisma_if_needed(project_dir)


def _ensure_prisma_if_needed(project_dir: Path) -> None:
    """Run prisma generate + db push if a schema exists and no database yet."""
    import subprocess as _sp

    schema = project_dir / "prisma" / "schema.prisma"
    if not schema.exists():
        return

    node_modules = project_dir / "node_modules"
    if not node_modules.exists():
        return

    shared_node_modules = node_modules.is_symlink()
    generated_client = node_modules / ".prisma" / "client" / "index.js"
    npx = str(node_modules / ".bin" / "prisma")
    if not Path(npx).exists():
        npx = "npx prisma"

    if shared_node_modules and generated_client.exists():
        logger.info(
            "Skipping prisma generate in %s because shared node_modules already has generated client",
            project_dir,
        )
    else:
        result = _sp.run(
            f"{npx} generate",
            shell=True,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning(
                "prisma generate failed in %s with exit code %s%s",
                project_dir,
                result.returncode,
                _bootstrap_failure_detail(result),
            )

    # Only push if no database file exists yet
    has_db = any(project_dir.glob("*.db")) or any(project_dir.glob("prisma/*.db"))
    if not has_db:
        result = _sp.run(
            f"{npx} db push --accept-data-loss",
            shell=True,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning(
                "prisma db push failed in %s with exit code %s%s",
                project_dir,
                result.returncode,
                _bootstrap_failure_detail(result),
            )


def _setup_worker_python_venv(worker_dir: Path) -> None:
    """Create a fresh Python .venv in the worker copy and install deps."""
    venv_dir = worker_dir / ".venv"
    if venv_dir.exists():
        return

    req = worker_dir / "requirements.txt"
    pyproject = worker_dir / "pyproject.toml"
    if not req.exists() and not pyproject.exists():
        return

    if _create_python_venv(venv_dir, worker_dir):
        _install_python_deps(worker_dir, venv_dir / "bin" / "python", req, pyproject)


def _activate_worker_venv(worker_dir: Path) -> None:
    """Prepend worker's .venv/bin to PATH so all python calls use it."""
    import os as _os
    venv_bin = worker_dir / ".venv" / "bin"
    if venv_bin.exists():
        _os.environ["PATH"] = str(venv_bin) + ":" + _os.environ.get("PATH", "")
        _os.environ["VIRTUAL_ENV"] = str(worker_dir / ".venv")


def _isolate_worker_cli_home(worker_dir: Path) -> None:
    """Route CLI HOME/XDG writes into the disposable worker directory."""
    import os as _os

    home_dir = worker_dir / ".otto-worker-home"
    xdg_config_home = home_dir / "xdg-config"
    xdg_data_home = home_dir / "xdg-data"
    xdg_cache_home = home_dir / "xdg-cache"
    for path in (home_dir, xdg_config_home, xdg_data_home, xdg_cache_home):
        path.mkdir(parents=True, exist_ok=True)

    _os.environ["HOME"] = str(home_dir)
    _os.environ["XDG_CONFIG_HOME"] = str(xdg_config_home)
    _os.environ["XDG_DATA_HOME"] = str(xdg_data_home)
    _os.environ["XDG_CACHE_HOME"] = str(xdg_cache_home)


def _resolve_worker_cli_entrypoint(
    worker_dir: Path,
    profile: Any,
    default_entrypoint: list[str],
) -> list[str]:
    """Resolve CLI entrypoint for a worker, preferring built binaries."""
    from otto.certifier.baseline import AppRunner

    # Check build marker for pre-built binary
    build_meta = AppRunner.load_build_metadata(worker_dir)
    if build_meta:
        # Cargo: use binary from symlinked target/
        binary_path = build_meta.get("binary_path")
        if binary_path:
            candidate = worker_dir / binary_path
            if candidate.exists():
                return [str(candidate)]

        # Go: use binary by stored name
        binary_name = build_meta.get("binary_name")
        if binary_name:
            candidate = worker_dir / binary_name
            if candidate.exists():
                return [str(candidate)]

    return default_entrypoint if default_entrypoint else ["python3", "main.py"]


def _capture_cli_help(manifest: Any, project_dir: Path) -> None:
    """Run --help to populate manifest.cli_help_text."""
    import subprocess as _sp

    entrypoint = getattr(manifest, "cli_entrypoint", [])
    if not entrypoint:
        return

    try:
        result = _sp.run(
            entrypoint + ["--help"],
            cwd=str(project_dir), capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            manifest.cli_help_text = result.stdout[:5000]
    except Exception:
        pass


def _bootstrap_failure_detail(result: Any) -> str:
    """Summarize a failed bootstrap subprocess result for logs."""
    stderr = (getattr(result, "stderr", "") or "").strip()
    stdout = (getattr(result, "stdout", "") or "").strip()
    detail = stderr or stdout
    if not detail:
        return ""
    if len(detail) > 200:
        detail = detail[:200] + "..."
    return f": {detail}"


def _run_bootstrap_command(
    argv: list[str],
    project_dir: Path,
    *,
    timeout: int,
    label: str,
) -> Any | None:
    """Run a bootstrap command, logging non-zero exits instead of failing silently."""
    import subprocess as _sp

    try:
        result = _sp.run(
            argv,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except _sp.TimeoutExpired:
        logger.warning("%s timed out in %s", label, project_dir)
        return None

    if result.returncode != 0:
        logger.warning(
            "%s failed in %s with exit code %s%s",
            label,
            project_dir,
            result.returncode,
            _bootstrap_failure_detail(result),
        )
    return result


def _create_python_venv(venv_dir: Path, project_dir: Path) -> bool:
    """Create a Python venv, falling back to stdlib venv when uv is unavailable."""
    import shutil
    import sys as _sys

    python_bin = venv_dir / "bin" / "python"

    try:
        _run_bootstrap_command(
            ["uv", "venv", str(venv_dir)],
            project_dir,
            timeout=30,
            label="uv venv",
        )
    except FileNotFoundError:
        logger.warning(
            "uv not found while creating Python venv in %s; falling back to python -m venv",
            project_dir,
        )
    else:
        if python_bin.exists():
            return True
        logger.warning(
            "uv venv did not create %s in %s; falling back to python -m venv",
            python_bin,
            project_dir,
        )

    shutil.rmtree(venv_dir, ignore_errors=True)
    result = _run_bootstrap_command(
        [_sys.executable, "-m", "venv", str(venv_dir)],
        project_dir,
        timeout=30,
        label="python -m venv",
    )
    return bool(result and result.returncode == 0 and python_bin.exists())


def _install_python_deps(project_dir: Path, python_bin: Path, req: Path, pyproject: Path) -> None:
    """Install Python deps into an existing venv, preferring uv with pip fallback."""
    install_with_requirements = req.exists()

    fallback_to_pip = False
    try:
        if install_with_requirements:
            result = _run_bootstrap_command(
                ["uv", "pip", "install", "-r", str(req), "--python", str(python_bin)],
                project_dir,
                timeout=120,
                label="uv pip install -r requirements.txt",
            )
        else:
            result = _run_bootstrap_command(
                ["uv", "pip", "install", ".", "--python", str(python_bin)],
                project_dir,
                timeout=120,
                label="uv pip install .",
            )
    except FileNotFoundError:
        logger.warning(
            "uv not found while installing Python deps in %s; falling back to pip",
            project_dir,
        )
        fallback_to_pip = True
    else:
        fallback_to_pip = result is None or result.returncode != 0
        if fallback_to_pip:
            logger.warning(
                "uv pip install did not complete successfully in %s; falling back to pip",
                project_dir,
            )

    if fallback_to_pip:
        if install_with_requirements:
            result = _run_bootstrap_command(
                [str(python_bin), "-m", "pip", "install", "-r", str(req)],
                project_dir,
                timeout=120,
                label="pip install -r requirements.txt",
            )
        else:
            result = _run_bootstrap_command(
                [str(python_bin), "-m", "pip", "install", "."],
                project_dir,
                timeout=120,
                label="pip install .",
            )

    if result is None:
        logger.warning("Python dependency install did not complete in %s", project_dir)


@dataclass
class ProjectDiscovery:
    """Result of LLM-assisted project discovery.

    One agent call that figures out: what is this project, how to start it,
    and how to interact with it. Replaces heuristic classification for all
    product types.
    """
    product_type: str = "unknown"       # web, api, cli, library, websocket
    interaction: str = "unknown"        # http, browser, cli, import, websocket
    base_url: str = ""                  # http://localhost:PORT (if server)
    cli_entrypoint: list[str] = field(default_factory=list)  # ["python3", "tool.py"]
    test_approach: str = ""             # how to test: curl, cli commands, import, ws client
    app_started: bool = False           # whether agent started a server
    diagnosis: str = ""                 # why it failed (if it did)
    cost: float = 0.0


def discover_project(
    project_dir: Path,
    config: dict[str, Any],
    *,
    hint_profile: Any | None = None,
    startup_error: str = "",
) -> ProjectDiscovery:
    """LLM agent reads the project and figures out everything the certifier needs.

    This is the primary classification + startup path. The heuristic classifier
    provides hints, but the agent makes the final decision. Handles ANY product
    type — web apps, CLI tools, libraries, WebSocket servers, data pipelines.

    Args:
        project_dir: Path to the project
        config: Certifier config dict
        hint_profile: Optional heuristic classifier output (for context)
        startup_error: If a previous startup attempt failed, include the error
    """
    import re as _re

    logger.info("Running project discovery agent on %s", project_dir.name)

    # Gather project context
    readme = ""
    for name in ("README.md", "readme.md", "README", "README.txt"):
        p = project_dir / name
        if p.exists():
            try:
                readme = p.read_text()[:3000]
            except OSError:
                pass
            break

    pkg_info = ""
    for name in ("package.json", "requirements.txt", "pyproject.toml",
                  "Cargo.toml", "go.mod", "Makefile", "setup.py", "setup.cfg"):
        p = project_dir / name
        if p.exists():
            try:
                pkg_info += f"\n--- {name} ---\n{p.read_text()[:2000]}\n"
            except OSError:
                pass

    # List source files for context
    files_list = []
    for ext in ("*.py", "*.js", "*.ts", "*.rs", "*.go"):
        for f in sorted(project_dir.glob(f"**/{ext}"))[:20]:
            rel = str(f.relative_to(project_dir))
            if not any(skip in rel for skip in ["node_modules", ".venv", "target/", "__pycache__"]):
                files_list.append(rel)

    hint_text = ""
    if hint_profile:
        hint_text = f"""
## Heuristic Classifier Output (may be wrong)
Product type: {getattr(hint_profile, 'product_type', 'unknown')}
Interaction: {getattr(hint_profile, 'interaction', 'unknown')}
Framework: {getattr(hint_profile, 'framework', 'unknown')}
Start command: {getattr(hint_profile, 'start_command', '')}
"""

    error_text = ""
    if startup_error:
        error_text = f"""
## Previous Startup Attempt Failed
{startup_error}
"""

    prompt = f"""\
Analyze this project and get it ready for testing.

## Project Files
{pkg_info}

## Source Files
{chr(10).join(files_list) if files_list else "(none found)"}

{f"## README{chr(10)}{readme}" if readme else ""}
{hint_text}{error_text}

## Your Tasks

1. **Read the project** to understand what it is and how it works.

2. **Classify it** — what type of product is this?
   - `web` = web app with UI (React, Next.js, Django templates)
   - `api` = HTTP API server (Express, Flask, FastAPI)
   - `cli` = command-line tool (argparse, click, clap)
   - `library` = importable library with no server/CLI
   - `websocket` = WebSocket or real-time server

3. **Install dependencies** if needed (npm install, pip install, etc.)

4. **Start the product** if it's a server (web/api/websocket):
   - Start it in the background on any available port
   - Verify it responds (curl for HTTP, or check process is running for WS)
   - If it's a library or CLI, skip this step

5. **Report results** using these EXACT markers at the end:

PRODUCT_TYPE: <web|api|cli|library|websocket>
INTERACTION: <http|browser|cli|import|websocket>
BASE_URL: <http://localhost:PORT or empty>
CLI_ENTRYPOINT: <command to run it, or empty>
TEST_APPROACH: <one line: how should a tester interact with this product>
APP_STARTED: <true|false>

Example for an Express API:
PRODUCT_TYPE: api
INTERACTION: http
BASE_URL: http://localhost:3000
CLI_ENTRYPOINT:
TEST_APPROACH: Make HTTP requests with curl to /api endpoints
APP_STARTED: true

Example for a Python CLI:
PRODUCT_TYPE: cli
INTERACTION: cli
BASE_URL:
CLI_ENTRYPOINT: python3 notes.py
TEST_APPROACH: Run CLI commands and check stdout/exit codes
APP_STARTED: false

Example for a Python library:
PRODUCT_TYPE: library
INTERACTION: import
BASE_URL:
CLI_ENTRYPOINT:
TEST_APPROACH: Write and run Python test scripts that import the library
APP_STARTED: false

Important:
- Do NOT modify application code. Only install deps and start the app.
- If it's a server, it must keep running in the background.
- If you can't figure it out, still report your best guess for the markers.
"""

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=["project"],
        env=_subprocess_env(),
        system_prompt={"type": "preset", "preset": "claude_code"},
        max_turns=8,
    )
    model = config.get("model") or config.get("planner_model")
    if model:
        options.model = str(model)

    try:
        loop = asyncio.new_event_loop()
        try:
            text, cost, _ = loop.run_until_complete(run_agent_query(prompt, options))
        finally:
            loop.close()
    except Exception as exc:
        logger.warning("Project discovery agent failed: %s", exc)
        return ProjectDiscovery(diagnosis=f"Discovery agent crashed: {exc}", cost=0.0)

    # Parse markers from agent output
    result = ProjectDiscovery(cost=cost)
    if not text:
        result.diagnosis = "Discovery agent produced no output"
        return result

    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("PRODUCT_TYPE:"):
            result.product_type = stripped.split(":", 1)[1].strip().lower()
        elif stripped.startswith("INTERACTION:"):
            result.interaction = stripped.split(":", 1)[1].strip().lower()
        elif stripped.startswith("BASE_URL:"):
            url = stripped.split(":", 1)[1].strip()
            if url and url.startswith("http"):
                result.base_url = url
        elif stripped.startswith("CLI_ENTRYPOINT:"):
            entry = stripped.split(":", 1)[1].strip()
            if entry:
                import shlex
                result.cli_entrypoint = shlex.split(entry)
        elif stripped.startswith("TEST_APPROACH:"):
            result.test_approach = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("APP_STARTED:"):
            result.app_started = stripped.split(":", 1)[1].strip().lower() == "true"

    if result.product_type == "unknown":
        result.diagnosis = "Agent could not determine product type"
    else:
        logger.info(
            "Discovery: type=%s, interaction=%s, base_url=%s, started=%s, cost=$%.2f",
            result.product_type, result.interaction, result.base_url,
            result.app_started, cost,
        )

    return result


def _build_project(project_dir: Path, config: dict[str, Any]) -> None:
    """Compile the project once before worker fanout.

    Build failures are logged and ignored so story verification can fall back
    to the original dev-mode startup path.
    """
    from otto.certifier.baseline import AppRunner
    from otto.certifier.classifier import classify

    profile = classify(project_dir)
    timeout = int(config.get("certifier_app_build_timeout", 120))
    runner = AppRunner(project_dir, profile)
    if not runner.build(timeout=timeout):
        logger.warning("Shared build failed for %s; workers will use dev mode", project_dir)


def _scavenge_stale_workers(project_dir: Path, max_age_s: float = 3600) -> None:
    """Remove orphaned worker copies from previous crashed runs."""
    import shutil
    import tempfile as _tmp
    workers_dir = Path(_tmp.gettempdir()) / "otto-workers" / project_dir.resolve().name
    if not workers_dir.exists():
        return
    now = time.time()
    for d in workers_dir.iterdir():
        if d.is_dir() and (now - d.stat().st_mtime) > max_age_s:
            shutil.rmtree(d, ignore_errors=True)


def _scavenge_old_story_runs(project_dir: Path, keep_latest: int = 5) -> None:
    """Remove old story run dirs, keeping the N most recent.

    Story run logs persist for debugging but accumulate over time.
    Keep the latest runs, remove the rest.
    """
    import shutil
    stories_dir = project_dir / "otto_logs" / "certifier" / "stories"
    if not stories_dir.exists():
        return
    runs = sorted(
        (d for d in stories_dir.iterdir() if d.is_dir()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    for old_run in runs[keep_latest:]:
        shutil.rmtree(old_run, ignore_errors=True)


async def _kill_process_tree(proc: asyncio.subprocess.Process, grace_s: float = 5.0) -> None:
    """Two-phase shutdown: SIGTERM → grace → SIGKILL. Kills entire process group."""
    import os as _os
    import signal as _signal

    try:
        _os.killpg(_os.getpgid(proc.pid), _signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_s)
    except asyncio.TimeoutError:
        try:
            _os.killpg(_os.getpgid(proc.pid), _signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        await proc.wait()


def _kill_orphan_app(story_dir: Path) -> None:
    """Kill app process group that survived worker hard-kill."""
    import os as _os
    import signal as _signal

    pid_file = story_dir / "app.pid"
    if pid_file.exists():
        try:
            pgid = int(pid_file.read_text().strip())
            _os.killpg(pgid, _signal.SIGKILL)
        except (ProcessLookupError, PermissionError, ValueError, OSError):
            pass


def _copy_story_logs(worker_dir: Path, story_dir: Path) -> None:
    """Copy certifier logs + evidence from worker copy to story_dir.

    Worker copies are deleted after each story — these logs are the only
    surviving record of what the journey agent did.
    """
    import shutil
    src = worker_dir / "otto_logs" / "certifier"
    if not src.exists():
        return
    for item in src.iterdir():
        dst = story_dir / item.name
        if item.is_file():
            shutil.copy2(item, dst)
        elif item.is_dir():
            # Evidence directories (evidence-{story-id}/) with screenshots/video
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(item, dst)


async def _run_stories_in_subprocess(
    stories: list[UserStory],
    project_dir: Path,
    config: dict[str, Any],
    story_dir: Path,
    interaction: str,
    manifest: Any = None,
) -> list[JourneyResult]:
    """Run a BATCH of stories in one subprocess with one app instance.

    One worker copy, one app, one agent session per batch.
    Returns a list of JourneyResults (one per story).
    """
    import shutil
    import sys as _sys

    input_path = story_dir / "input.json"
    output_path = story_dir / "output.json"
    stderr_path = story_dir / "stderr.log"
    worker_id = story_dir.name

    for p in (input_path, output_path, stderr_path):
        p.unlink(missing_ok=True)

    worker_dir = _create_worker_copy(project_dir, worker_id)

    _atomic_write_json(input_path, {
        "stories": [asdict(s) for s in stories],
        "worker_dir": str(worker_dir),
        "interaction": interaction,
        "config": config,
        "discovery": {
            "product_type": getattr(manifest, "product_type", ""),
            "interaction": interaction,
            "cli_entrypoint": getattr(manifest, "cli_entrypoint", []),
            "test_approach": getattr(manifest, "cli_help_text", ""),
        },
    })

    stderr_fh = open(stderr_path, "w", buffering=1)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            _sys.executable, "-m", "otto.certifier.journey_agent",
            "_worker", str(input_path), str(output_path),
            stderr=stderr_fh,
            stdout=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        # Batch timeout: sum of per-story timeouts
        total_timeout = sum(_story_timeout(s, config) or 300.0 for s in stories)
        try:
            await asyncio.wait_for(proc.wait(), timeout=total_timeout)
        except asyncio.TimeoutError:
            await _kill_process_tree(proc)
            _kill_orphan_app(story_dir)
            return [JourneyResult(
                story_id=s.id, story_title=s.title,
                persona=s.persona, passed=False,
                blocked_at=f"batch timed out after {int(total_timeout)}s",
                diagnosis="Story verification batch timed out.",
            ) for s in stories]
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            await _kill_process_tree(proc)
        _kill_orphan_app(story_dir)
        raise
    finally:
        stderr_fh.close()
        _kill_orphan_app(story_dir)
        _copy_story_logs(worker_dir, story_dir)
        shutil.rmtree(worker_dir, ignore_errors=True)

    if output_path.exists():
        try:
            data = json.loads(output_path.read_text())
            if isinstance(data, list):
                return [_journey_result_from_dict(d) for d in data]
            # Single result (backward compat)
            return [_journey_result_from_dict(data)]
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to parse batch output: %s", exc)

    stderr_content = ""
    if stderr_path.exists():
        stderr_content = stderr_path.read_text()
        if len(stderr_content) > 2000:
            stderr_content = "...\n" + stderr_content[-2000:]
    return [JourneyResult(
        story_id=s.id, story_title=s.title,
        persona=s.persona, passed=False,
        diagnosis=f"Worker crashed without output.\n{stderr_content}",
    ) for s in stories]


async def _run_story_in_subprocess(
    story: UserStory,
    project_dir: Path,
    config: dict[str, Any],
    story_dir: Path,
    interaction: str,
    manifest: Any = None,
) -> JourneyResult:
    """Run a single story verification in an isolated subprocess.

    Each subprocess gets its own project copy, app instance, and SDK session.
    """
    import shutil
    import sys as _sys

    input_path = story_dir / "input.json"
    output_path = story_dir / "output.json"
    stderr_path = story_dir / "stderr.log"
    worker_id = story_dir.name

    # Clear stale artifacts
    for p in (input_path, output_path, stderr_path):
        p.unlink(missing_ok=True)

    # Create isolated project copy
    worker_dir = _create_worker_copy(project_dir, worker_id)

    _atomic_write_json(input_path, {
        "story": asdict(story),
        "worker_dir": str(worker_dir),
        "interaction": interaction,
        "config": config,
        # Pass parent discovery so workers don't re-run the LLM agent
        "discovery": {
            "product_type": getattr(manifest, "product_type", ""),
            "interaction": interaction,
            "cli_entrypoint": getattr(manifest, "cli_entrypoint", []),
            "test_approach": getattr(manifest, "cli_help_text", ""),
        },
    })

    stderr_fh = open(stderr_path, "w", buffering=1)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            _sys.executable, "-m", "otto.certifier.journey_agent",
            "_worker", str(input_path), str(output_path),
            stderr=stderr_fh,
            stdout=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        timeout = _story_timeout(story, config)
        try:
            if timeout is not None:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            else:
                await proc.wait()
        except asyncio.TimeoutError:
            await _kill_process_tree(proc)
            _kill_orphan_app(story_dir)
            return JourneyResult(
                story_id=story.id, story_title=story.title,
                persona=story.persona, passed=False,
                blocked_at=f"timed out after {int(timeout)}s",
                diagnosis="Story verification timed out.",
            )
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            await _kill_process_tree(proc)
        _kill_orphan_app(story_dir)
        raise
    finally:
        stderr_fh.close()
        _kill_orphan_app(story_dir)  # idempotent — catches hard-kill orphans
        _copy_story_logs(worker_dir, story_dir)
        shutil.rmtree(worker_dir, ignore_errors=True)

    if output_path.exists():
        try:
            data = json.loads(output_path.read_text())
            return _journey_result_from_dict(data)
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to parse worker output for %s: %s", story.id, exc)

    stderr_content = ""
    if stderr_path.exists():
        stderr_content = stderr_path.read_text()
        if len(stderr_content) > 2000:
            stderr_content = "...\n" + stderr_content[-2000:]
    return JourneyResult(
        story_id=story.id, story_title=story.title,
        persona=story.persona, passed=False,
        diagnosis=f"Worker crashed without output.\n{stderr_content}",
    )


def _worker_main(input_path: Path, output_path: Path) -> None:
    """Subprocess entry point for story verification.

    Each worker:
    1. Creates its own app instance (AppRunner on OS-assigned port)
    2. Builds its own manifest from its running app
    3. Runs verify_story with its own event loop
    4. Writes result to output.json (single writer — signal-safe)

    IMPORTANT: input_path and output_path are resolved to absolute paths
    before os.chdir(worker_dir), so they remain valid after the chdir.
    """
    import os as _os
    import signal as _signal
    import sys as _sys
    import traceback

    from otto.certifier.baseline import AppRunner
    from otto.certifier.classifier import classify
    from otto.certifier.manifest import build_manifest
    from otto.certifier.adapter import analyze_project

    _killed_by_signal: int | None = None

    def _signal_handler(signum: int, frame: Any) -> None:
        nonlocal _killed_by_signal
        _killed_by_signal = signum
        raise SystemExit(128 + signum)

    _signal.signal(_signal.SIGTERM, _signal_handler)
    _signal.signal(_signal.SIGINT, _signal_handler)

    # Resolve to absolute BEFORE chdir — paths are relative to launch dir
    input_path = input_path.resolve()
    output_path = output_path.resolve()

    all_results: list[dict[str, Any]] | None = None
    story_id = "unknown"
    runner: AppRunner | None = None
    story_dir = input_path.parent

    try:
        payload = json.loads(input_path.read_text())
        # Support both single story ("story") and batch ("stories")
        if "stories" in payload:
            stories_batch = [_story_from_dict(s) for s in payload["stories"]]
        else:
            stories_batch = [_story_from_dict(payload["story"])]
        story_id = stories_batch[0].id
        worker_dir = Path(payload["worker_dir"]).resolve()
        interaction = payload.get("interaction")
        config = payload.get("config", {})

        _os.chdir(worker_dir)

        # ── Use parent discovery results (avoid re-running LLM per worker) ──
        profile = classify(worker_dir)
        parent_discovery = payload.get("discovery", {})
        interaction = interaction or parent_discovery.get("interaction") or profile.interaction

        # For HTTP/browser: each worker starts its own app (port isolation)
        # For CLI/library: no app needed, just build manifest
        if interaction in ("http", "browser"):
            runner = AppRunner(worker_dir, profile)
            start_timeout = int(config.get("certifier_app_start_timeout", 90))
            evidence = runner.start(timeout=start_timeout)

            if runner.process:
                try:
                    (story_dir / "app.pid").write_text(
                        str(_os.getpgid(runner.process.pid)))
                except (ProcessLookupError, OSError):
                    pass

            if not evidence.passed:
                # LLM recovery: let an agent figure out startup
                recovery = discover_project(
                    worker_dir, config, hint_profile=profile,
                    startup_error=str(evidence.actual))
                if recovery.app_started and recovery.base_url:
                    base_url = recovery.base_url
                else:
                    all_results = [{
                        "story_id": s.id, "story_title": s.title,
                        "persona": s.persona, "passed": False,
                        "diagnosis": f"App failed to start: {evidence.actual}\n"
                                     f"Recovery: {recovery.diagnosis}",
                        "steps": [], "break_findings": [],
                        "cost_usd": recovery.cost / len(stories_batch), "duration_s": 0.0,
                    } for s in stories_batch]
                    base_url = ""
            else:
                base_url = runner.base_url
        else:
            # CLI/library/websocket: no server to start per-worker
            if profile.language == "python":
                _setup_worker_python_venv(worker_dir)
            _ensure_prisma_if_needed(worker_dir)
            _activate_worker_venv(worker_dir)
            base_url = ""

        if all_results is None:
            test_config = analyze_project(worker_dir)
            manifest = build_manifest(
                test_config, profile,
                base_url=base_url or None,
                interaction=interaction,
            )
            if parent_discovery.get("cli_entrypoint"):
                manifest.cli_entrypoint = parent_discovery["cli_entrypoint"]
            if parent_discovery.get("test_approach"):
                manifest.cli_help_text = parent_discovery["test_approach"]

            # Run ALL stories in this batch sequentially in one event loop
            all_results = []
            loop = asyncio.new_event_loop()
            try:
                for story in stories_batch:
                    story_id = story.id
                    result = loop.run_until_complete(
                        verify_story(story, manifest, base_url, worker_dir, config))
                    all_results.append(_journey_result_to_dict(result))
            finally:
                loop.close()

    except SystemExit:
        pass  # signal handler raised this — fall through to finally
    except Exception:
        tb = traceback.format_exc()
        all_results = [{
            "story_id": story_id, "story_title": "", "persona": "",
            "passed": False, "diagnosis": f"Worker crashed: {tb}",
            "steps": [], "break_findings": [],
            "cost_usd": 0.0, "duration_s": 0.0,
        }]
        print(tb, file=_sys.stderr, flush=True)
    finally:
        # Stop app
        if runner is not None:
            try:
                runner.stop()
            except Exception:
                pass
        # SINGLE WRITER — only this block writes output
        if all_results is None:
            sig_name = "unknown"
            if _killed_by_signal is not None:
                try:
                    import signal as _s
                    sig_name = _s.Signals(_killed_by_signal).name
                except (ValueError, AttributeError):
                    sig_name = str(_killed_by_signal)
            all_results = [{
                "story_id": story_id, "story_title": "", "persona": "",
                "passed": False, "diagnosis": f"Worker killed by signal {sig_name}",
                "steps": [], "break_findings": [],
                "cost_usd": 0.0, "duration_s": 0.0,
            }]
        # Write list for batch, single dict for backward compat
        output = all_results if len(all_results) != 1 else all_results[0]
        _atomic_write_json(output_path, output)


# ---------------------------------------------------------------------------
# Module entry point — subprocess worker
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) == 4 and _sys.argv[1] == "_worker":
        _worker_main(Path(_sys.argv[2]), Path(_sys.argv[3]))
