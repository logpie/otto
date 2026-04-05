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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from otto.agent import (
    ClaudeAgentOptions,
    ResultMessage,
    AssistantMessage,
    TextBlock,
    _subprocess_env,
    query,
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
# Agent prompt
# ---------------------------------------------------------------------------


JOURNEY_AGENT_SYSTEM_PROMPT = """\
You are a QA tester simulating a real user of a web product. You will execute
a user story step by step, interacting with the running application via HTTP
requests.

Your job is to determine: does this product actually work for this user scenario?

RULES:
1. Use the product manifest to find the right API routes, field names, and auth flow.
2. Make REAL HTTP requests — do not simulate or assume responses.
3. After each step, VERIFY the outcome. Don't just check status codes — check that
   the data makes sense (correct values, correct structure, correct user context).
4. Carry state between steps. If step 2 creates a task and returns ID "abc123",
   use that ID in later steps.
5. If a step fails, DIAGNOSE why. Is the endpoint missing? Wrong fields? Auth issue?
   Server error? Explain the root cause.
6. Continue to the next step even if one fails — test as much as possible.
7. For browser verification: if you have browser tools, use them to check the UI.
   If not, verify via API only and note that browser verification was skipped.

WORKFLOW:
1. Execute all happy path steps, verifying each one
2. If BREAK strategies are specified, spend a few turns trying to break the product
3. When done, your final response will be collected as the verdict automatically

Your final output will be structured as JSON automatically. Include all step
results and any break findings in your final response.
"""


def _build_journey_prompt(
    story: UserStory,
    manifest: ProductManifest,
    base_url: str,
) -> str:
    """Build the prompt for the journey verification agent."""
    manifest_text = format_manifest_for_agent(manifest)

    steps_text = ""
    for i, step in enumerate(story.steps):
        dep = f" (uses output from step {step.uses_output_from + 1})" if step.uses_output_from is not None else ""
        browser = f"\n   Browser check: {step.verify_in_browser}" if step.verify_in_browser else ""
        steps_text += f"""
Step {i + 1}: {step.action}
   Verify: {step.verify}{browser}
   Entity: {step.entity}, Operation: {step.operation}, Mode: {step.mode}{dep}
"""

    break_text = ""
    if story.break_strategies:
        break_text = f"""

BREAK PHASE (after happy path):
Try these strategies to find quality issues:
{chr(10).join(f'- {s}' for s in story.break_strategies)}

Report what you find. These do NOT affect the pass/fail verdict.
"""

    return f"""\
Execute this user story and verify each step.

STORY: {story.title}
Persona: {story.persona}
Narrative: {story.narrative}

{manifest_text}

Base URL: {base_url}

STEPS:
{steps_text}
{break_text}
"""


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

    # Health check: verify app is still alive before each story
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

    prompt = _build_journey_prompt(story, manifest, base_url)

    # Configure agent with HTTP tools (Bash for curl) + optional browser (chrome-devtools)
    mcp_servers = {}
    chrome_config = config.get("chrome_mcp")
    if chrome_config:
        mcp_servers["chrome-devtools"] = chrome_config

    # Structured output: SDK enforces verdict JSON as the agent's final response.
    # Session terminates naturally when the agent produces its output — no hang.
    verdict_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "story_passed": {"type": "boolean"},
            "blocked_at": {"type": ["string", "null"]},
            "summary": {"type": "string"},
            "diagnosis": {"type": ["string", "null"]},
            "fix_suggestion": {"type": ["string", "null"]},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "outcome": {"type": "string", "enum": ["pass", "fail", "blocked"]},
                        "verification": {"type": "string"},
                        "diagnosis": {"type": ["string", "null"]},
                        "fix_suggestion": {"type": ["string", "null"]},
                    },
                },
            },
            "break_findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "technique": {"type": "string"},
                        "description": {"type": "string"},
                        "result": {"type": "string"},
                        "severity": {"type": "string"},
                        "fix_suggestion": {"type": "string"},
                    },
                },
            },
        },
        "required": ["story_passed", "summary", "steps"],
    }

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=["project"],
        env=_subprocess_env(),
        system_prompt=JOURNEY_AGENT_SYSTEM_PROMPT,
        output_format={"type": "json_schema", "schema": verdict_schema},
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

    # Session terminates when agent produces structured output (ResultMessage).
    text_parts: list[str] = []
    cost_usd = 0.0
    verdict_data: dict[str, Any] | None = None

    stream = query(prompt=prompt, options=options)
    try:
        async for message in stream:
            if isinstance(message, ResultMessage):
                raw_cost = getattr(message, "total_cost_usd", None)
                if isinstance(raw_cost, (int, float)):
                    cost_usd = float(raw_cost)
                # Structured output is on the ResultMessage
                structured = getattr(message, "structured_output", None)
                if isinstance(structured, dict):
                    verdict_data = structured
                break
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text:
                        text_parts.append(block.text)
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
            f"verdict_source: {'structured_output' if verdict_data else 'fallback'}",
        ],
    )

    # Use structured output (preferred), fall back to file/prose parsing
    if verdict_data:
        result = _verdict_from_dict(verdict_data, story)
        # Also write verdict to file for debugging/auditing
        verdict_file.write_text(json.dumps(verdict_data, indent=2))
    else:
        result = _parse_verdict(verdict_file, raw_output, story)
    result.cost_usd = cost_usd
    result.duration_s = duration_s

    # Save raw agent output for debugging
    raw_log = log_dir / f"journey-{story.id}-agent.log"
    raw_log.write_text(raw_output)

    return result


def _verdict_from_dict(verdict: dict[str, Any], story: UserStory) -> JourneyResult:
    """Convert a verdict dict into a JourneyResult."""
    steps = []
    for s in verdict.get("steps", []):
        steps.append(StepResult(
            action=s.get("action", ""),
            outcome=s.get("outcome", "blocked"),
            verification=s.get("verification", ""),
            diagnosis=s.get("diagnosis", ""),
            fix_suggestion=s.get("fix_suggestion", ""),
        ))
    break_findings = []
    for b in verdict.get("break_findings", []):
        break_findings.append(BreakFinding(
            technique=b.get("technique", ""),
            description=b.get("description", ""),
            result=b.get("result", ""),
            severity=b.get("severity", "minor"),
            fix_suggestion=b.get("fix_suggestion", ""),
        ))
    return JourneyResult(
        story_id=story.id,
        story_title=story.title,
        persona=story.persona,
        passed=verdict.get("story_passed", False),
        steps=steps,
        break_findings=break_findings,
        summary=verdict.get("summary", ""),
        blocked_at=verdict.get("blocked_at", ""),
        diagnosis=verdict.get("diagnosis", ""),
        fix_suggestion=verdict.get("fix_suggestion", ""),
    )


def _parse_verdict(
    verdict_file: Path,
    raw_output: str,
    story: UserStory,
) -> JourneyResult:
    """Parse the journey agent's verdict into a JourneyResult."""
    verdict = None

    # Try reading from verdict file first
    if verdict_file.exists():
        try:
            verdict = json.loads(verdict_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # Fallback: extract JSON from agent output
    if verdict is None:
        text = raw_output.strip()
        if "```json" in text:
            parts = text.split("```json")
            if len(parts) > 1:
                json_part = parts[-1].split("```")[0].strip()
                try:
                    verdict = json.loads(json_part)
                except json.JSONDecodeError:
                    pass
        if verdict is None:
            start = text.rfind("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    verdict = json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    pass

    # Parse verdict into JourneyResult
    if verdict is None:
        # Last resort: infer pass/fail from prose output
        raw_lower = raw_output.lower()
        prose_passed = (
            "all steps passed" in raw_lower
            or "story: passed" in raw_lower
            or "verdict: pass" in raw_lower
            or ("pass" in raw_lower and "fail" not in raw_lower)
        )
        return JourneyResult(
            story_id=story.id,
            story_title=story.title,
            persona=story.persona,
            passed=prose_passed,
            summary=raw_output[-500:] if raw_output else "Agent did not produce output",
            diagnosis="" if prose_passed else "Agent did not write verdict file; inferred from prose",
        )

    return _verdict_from_dict(verdict, story)


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
) -> CertificationResult:
    """Verify all user stories and produce a certification result."""
    config = config or {}
    results: list[JourneyResult] = []
    all_break_findings: list[BreakFinding] = []
    total_cost = 0.0
    start_time = time.monotonic()

    for i, story in enumerate(stories):
        logger.info("Verifying story: %s (%s)", story.title, story.persona)
        timeout = _story_timeout(story, config)
        _write_heartbeat(project_dir, story.title, i, len(stories), story_timeout_s=timeout)

        # Structured output is the normal completion path for journey verification.
        # This timeout is only a last-resort safety net for hung SDK sessions or
        # stuck network activity, so it must stay comfortably above legitimate runs.
        story_task = asyncio.create_task(
            verify_story(story, manifest, base_url, project_dir, config)
        )
        try:
            result = await asyncio.wait_for(story_task, timeout=timeout)
        except asyncio.TimeoutError:
            story_task.cancel()
            with suppress(asyncio.CancelledError):
                await story_task
            logger.warning("Story timed out after %.0fs: %s", timeout, story.title)
            result = JourneyResult(
                story_id=story.id,
                story_title=story.title,
                persona=story.persona,
                passed=False,
                blocked_at=f"timed out after {int(timeout)}s",
                summary=f"Story verification timed out after {int(timeout)}s",
                diagnosis="The journey agent did not complete within the deadline. "
                          "This may indicate a hung HTTP request, a stuck app, or an agent loop.",
                fix_suggestion="Check if the app is responding and if the endpoint under test is hanging.",
                steps=[],
                break_findings=[],
                cost_usd=0.0,
                duration_s=timeout,
            )

        results.append(result)
        total_cost += result.cost_usd
        all_break_findings.extend(result.break_findings)

        status = "✓" if result.passed else "✗"
        logger.info("  %s %s (%.1fs, $%.3f)", status, story.title, result.duration_s, result.cost_usd)

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
