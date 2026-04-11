"""Otto build pipeline — agentic v3 build with certifier loop."""

from __future__ import annotations

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

    # Check for previous failed build — inject findings so agent doesn't repeat mistakes
    prev_failure = _get_previous_failure(project_dir)
    if prev_failure:
        prompt += f"\n\n## Previous Build Failed\n{prev_failure}"

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

    # Append to run history (one line per build for `otto history`)
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


def _get_previous_failure(project_dir: Path) -> str | None:
    """Read the most recent failed build's certifier findings, if any."""
    history_path = project_dir / "otto_logs" / "run-history.jsonl"
    if not history_path.exists():
        return None

    # Read last entry
    last_line = ""
    try:
        for line in history_path.read_text().splitlines():
            if line.strip():
                last_line = line.strip()
    except OSError:
        return None

    if not last_line:
        return None

    try:
        entry = json.loads(last_line)
    except json.JSONDecodeError:
        return None

    if entry.get("passed", True):
        return None  # last build passed, no failure context

    # Read certifier findings from PoW
    pow_path = project_dir / "otto_logs" / "certifier" / "proof-of-work.json"
    if not pow_path.exists():
        return f"The previous build failed but no certifier findings are available."

    try:
        pow_data = json.loads(pow_path.read_text())
    except (json.JSONDecodeError, OSError):
        return f"The previous build failed but certifier findings could not be read."

    failures = [s for s in pow_data.get("stories", []) if not s.get("passed")]
    if not failures:
        return f"The previous build failed but the certifier reported no specific story failures."

    lines = ["The previous build failed. The certifier found these issues:\n"]
    for f in failures:
        sid = f.get("story_id", "?")
        summary = f.get("summary", "")
        evidence = f.get("evidence", "")
        lines.append(f"- **{sid}**: {summary}")
        if evidence:
            lines.append(f"  Evidence: {evidence[:300]}")
    lines.append("\nFix these issues. Do NOT repeat the same mistakes.")
    return "\n".join(lines)


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
    """Commit otto artifacts (intent.md, etc.) so agents see them."""
    try:
        subprocess.run(
            ["git", "add", "intent.md", "otto.yaml"],
            cwd=project_dir, capture_output=True,
        )
        subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=project_dir, capture_output=True,
        )
        # Only commit if there are staged changes
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=project_dir, capture_output=True,
        )
        if result.returncode != 0:
            subprocess.run(
                ["git", "commit", "-q", "-m", "otto: commit artifacts"],
                cwd=project_dir, capture_output=True,
            )
    except Exception:
        pass
