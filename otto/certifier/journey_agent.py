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
1. Make REAL HTTP requests via Bash (curl). Never simulate responses.
2. Verify outcomes by checking response data, not just status codes.
3. Carry state between steps (IDs, tokens, cookies).
4. If a step fails, diagnose the root cause (missing endpoint, wrong fields, auth, server error).
5. Continue testing even if a step fails — cover as much as possible.
6. Do NOT use WebFetch — it cannot access localhost. Use curl via Bash only.
7. Be concise. One curl command per verification, check the result, move on.
"""


def _build_journey_prompt(
    story: UserStory,
    manifest: ProductManifest,
    base_url: str,
    *,
    skip_break: bool = False,
    has_browser: bool = False,
) -> str:
    """Build the prompt for the journey verification agent."""
    manifest_text = format_manifest_for_agent(manifest)

    steps_text = ""
    for i, step in enumerate(story.steps):
        dep = f" (uses output from step {step.uses_output_from + 1})" if step.uses_output_from is not None else ""
        steps_text += f"\n{i + 1}. {step.action}\n   Verify: {step.verify}{dep}"

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
        browser_text = """
BROWSER: You have chrome-devtools tools available. Use them to verify UI
renders correctly after each API step (navigate, take screenshot, check elements).
"""

    return f"""\
{story.title} — {story.persona}
{story.narrative}

{manifest_text}
Base URL: {base_url}

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

    skip_break = config.get("certifier_skip_break", True)  # default: skip break phase
    has_browser = bool(config.get("chrome_mcp"))

    prompt = _build_journey_prompt(
        story, manifest, base_url,
        skip_break=skip_break, has_browser=has_browser,
    )

    # Configure agent with HTTP tools (Bash for curl) + optional browser (chrome-devtools)
    mcp_servers = {}
    chrome_config = config.get("chrome_mcp")
    if chrome_config:
        mcp_servers["chrome-devtools"] = chrome_config

    # Simplified structured output — smaller schema = faster verdict generation.
    verdict_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "story_passed": {"type": "boolean"},
            "summary": {"type": "string"},
            "diagnosis": {"type": ["string", "null"]},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "outcome": {"type": "string", "enum": ["pass", "fail", "blocked"]},
                        "diagnosis": {"type": ["string", "null"]},
                    },
                },
            },
        },
        "required": ["story_passed", "summary", "steps"],
    }
    # Only include break_findings in schema when break phase is enabled
    if not skip_break:
        verdict_schema["properties"]["break_findings"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "technique": {"type": "string"},
                    "description": {"type": "string"},
                    "severity": {"type": "string"},
                },
            },
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
        # Subprocess-per-story: each story in own process with own app instance
        import uuid as _uuid

        logger.info("Running %d stories with max %d parallel (subprocess-isolated)",
                     len(stories), max_parallel)
        _scavenge_stale_workers(project_dir)
        _ensure_deps_installed(project_dir)

        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{_uuid.uuid4().hex[:8]}"
        stories_dir = project_dir / "otto_logs" / "certifier" / "stories" / run_id
        stories_dir.mkdir(parents=True, exist_ok=True)

        sem = asyncio.Semaphore(max_parallel)

        async def _bounded(i: int, story: UserStory) -> JourneyResult:
            async with sem:
                story_dir = stories_dir / f"{i:02d}-{story.id}"
                story_dir.mkdir(parents=True, exist_ok=True)
                try:
                    return await _run_story_in_subprocess(
                        story, project_dir, config, story_dir)
                except Exception as exc:
                    logger.exception("Subprocess failed for %s", story.id)
                    return JourneyResult(
                        story_id=story.id, story_title=story.title,
                        persona=story.persona, passed=False,
                        diagnosis=f"Subprocess error: {exc}")

        tasks = [_bounded(i, s) for i, s in enumerate(stories)]
        results = list(await asyncio.gather(*tasks))

        for result in results:
            total_cost += result.cost_usd
            all_break_findings.extend(result.break_findings)

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

    workers_dir = project_dir / ".otto-workers" / "stories"
    worker_dir = workers_dir / worker_id
    workers_dir.mkdir(parents=True, exist_ok=True)

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
            return [c for c in contents
                    if c in WORKER_EXCLUDE_DIRS
                    or any(fnmatch.fnmatch(c, p) for p in WORKER_EXCLUDE_PATTERNS)]
        shutil.copytree(project_dir, worker_dir, ignore=_ignore,
                        symlinks=True, ignore_dangling_symlinks=True)

    # Symlink node_modules back to the original — avoids 30s npm install
    # per worker. Symlink works because Node.js resolves require() relative
    # to the symlink target, so .bin/ scripts find their siblings correctly.
    nm = project_dir / "node_modules"
    nm_link = worker_dir / "node_modules"
    if nm.exists() and not nm_link.exists():
        _os.symlink(str(nm.resolve()), str(nm_link))

    return worker_dir


def _ensure_deps_installed(project_dir: Path) -> None:
    """Ensure node_modules exists in the original project (for symlink sharing).

    Python .venv is created per-worker by AppRunner (absolute paths break in copies).
    """
    import subprocess as _sp

    pkg_json = project_dir / "package.json"
    node_modules = project_dir / "node_modules"
    if pkg_json.exists() and not node_modules.exists():
        _sp.run(["npm", "install", "--no-audit", "--no-fund"],
                cwd=str(project_dir), capture_output=True, timeout=120)


def _scavenge_stale_workers(project_dir: Path, max_age_s: float = 3600) -> None:
    """Remove orphaned worker copies from previous crashed runs."""
    import shutil
    workers_dir = project_dir / ".otto-workers" / "stories"
    if not workers_dir.exists():
        return
    now = time.time()
    for d in workers_dir.iterdir():
        if d.is_dir() and (now - d.stat().st_mtime) > max_age_s:
            shutil.rmtree(d, ignore_errors=True)


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
    """Copy journey agent logs from worker copy to story_dir for observability."""
    import shutil
    src = worker_dir / "otto_logs" / "certifier"
    if not src.exists():
        return
    for item in src.iterdir():
        if item.is_file() and (item.name.startswith("journey-") or item.name == "heartbeat.json"):
            shutil.copy2(item, story_dir / item.name)


async def _run_story_in_subprocess(
    story: UserStory,
    project_dir: Path,
    config: dict[str, Any],
    story_dir: Path,
) -> JourneyResult:
    """Run a single story verification in an isolated subprocess.

    Each subprocess gets its own project copy, app instance, and SDK session.
    """
    import os as _os
    import shutil
    import signal as _signal
    import sys as _sys

    from otto.certifier.manifest import manifest_to_dict

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
        "config": config,
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

    result_dict: dict[str, Any] | None = None
    story_id = "unknown"
    runner: AppRunner | None = None
    story_dir = input_path.parent

    try:
        payload = json.loads(input_path.read_text())
        story = _story_from_dict(payload["story"])
        story_id = story.id
        worker_dir = Path(payload["worker_dir"]).resolve()
        config = payload.get("config", {})

        _os.chdir(worker_dir)

        # Start own isolated app instance.
        # Extended timeout: Next.js/heavy frameworks recompile in cloned dirs.
        profile = classify(worker_dir)
        runner = AppRunner(worker_dir, profile)
        start_timeout = int(config.get("certifier_app_start_timeout", 90))
        evidence = runner.start(timeout=start_timeout)

        # Write app PID so parent can kill orphan on hard kill
        if runner.process:
            try:
                (story_dir / "app.pid").write_text(
                    str(_os.getpgid(runner.process.pid)))
            except (ProcessLookupError, OSError):
                pass

        if not evidence.passed:
            result_dict = {
                "story_id": story_id, "story_title": story.title,
                "persona": story.persona, "passed": False,
                "diagnosis": f"App failed to start: {evidence.actual}",
                "steps": [], "break_findings": [],
                "cost_usd": 0.0, "duration_s": 0.0,
            }
        else:
            # Build manifest from THIS app instance (correct base_url)
            test_config = analyze_project(worker_dir)
            manifest = build_manifest(test_config, profile, runner.base_url)

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    verify_story(story, manifest, runner.base_url, worker_dir, config))
            finally:
                loop.close()
            result_dict = _journey_result_to_dict(result)

    except SystemExit:
        pass  # signal handler raised this — fall through to finally
    except Exception:
        tb = traceback.format_exc()
        result_dict = {
            "story_id": story_id, "story_title": "", "persona": "",
            "passed": False, "diagnosis": f"Worker crashed: {tb}",
            "steps": [], "break_findings": [],
            "cost_usd": 0.0, "duration_s": 0.0,
        }
        print(tb, file=_sys.stderr, flush=True)
    finally:
        # Stop app
        if runner is not None:
            try:
                runner.stop()
            except Exception:
                pass
        # SINGLE WRITER — only this block writes output
        if result_dict is None:
            sig_name = "unknown"
            if _killed_by_signal is not None:
                try:
                    import signal as _s
                    sig_name = _s.Signals(_killed_by_signal).name
                except (ValueError, AttributeError):
                    sig_name = str(_killed_by_signal)
            result_dict = {
                "story_id": story_id, "story_title": "", "persona": "",
                "passed": False, "diagnosis": f"Worker killed by signal {sig_name}",
                "steps": [], "break_findings": [],
                "cost_usd": 0.0, "duration_s": 0.0,
            }
        _atomic_write_json(output_path, result_dict)


# ---------------------------------------------------------------------------
# Module entry point — subprocess worker
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) == 4 and _sys.argv[1] == "_worker":
        _worker_main(Path(_sys.argv[2]), Path(_sys.argv[3]))
