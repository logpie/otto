"""Otto build pipeline — agentic v3 build with certifier loop."""

from __future__ import annotations

import asyncio
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
    tasks_passed: int = 0
    tasks_failed: int = 0


def _load_build_prompt() -> str:
    """Load the v3 build prompt (with certification steps)."""
    from otto.prompts import build_prompt
    return build_prompt()


def _load_code_prompt() -> str:
    """Load the code-only prompt (no certification knowledge)."""
    from otto.prompts import code_prompt
    return code_prompt()


async def build_agentic_v3(
    intent: str,
    project_dir: Path,
    config: dict[str, Any],
) -> BuildResult:
    """Fully agent-driven build: one session, certifier as environment.

    The coding agent does everything — build, self-test, dispatch certifier,
    read findings, fix, re-certify. The orchestrator just launches and waits.
    """
    from otto.agent import AgentCallError, make_agent_options, run_agent_with_timeout
    from otto.observability import append_text_log

    build_id = f"build-{int(time.time())}-{os.getpid()}"
    build_dir = project_dir / "otto_logs" / "builds" / build_id
    build_dir.mkdir(parents=True, exist_ok=True)

    # Append intent to cumulative log
    _append_intent(project_dir, intent, build_id)
    _commit_artifacts(project_dir)

    # Record HEAD before build so the improvement report can show only new commits
    from otto.journal import _get_head_sha
    _head_before = _get_head_sha(project_dir)

    options = make_agent_options(project_dir, config)

    skip_qa = bool(config.get("skip_product_qa"))

    if skip_qa:
        # Code-only: no certification knowledge, clean prompt
        prompt = _load_code_prompt() + f"\n\nBuild this product:\n\n{intent}"
    else:
        # Mono mode: full build + certification loop
        from otto.config import get_max_rounds
        max_certify_rounds = get_max_rounds(config)
        raw_prompt = _load_build_prompt().replace("{max_certify_rounds}", str(max_certify_rounds))
        prompt = raw_prompt + f"\n\nBuild this product:\n\n{intent}"

        # Pre-fill the certifier prompt so the agent can dispatch it directly.
        from otto.prompts import certifier_prompt
        evidence_dir = str(project_dir / "otto_logs" / "certifier" / "evidence")
        safe_intent = intent.replace("</certifier_prompt>", "")
        filled_certifier = certifier_prompt(mode="thorough").format(
            intent=safe_intent, evidence_dir=evidence_dir, focus_section="")
        prompt += (f"\n\n## Pre-filled Certifier Prompt\n"
                   f"When you dispatch the certifier agent, use this EXACT prompt:\n"
                   f"<certifier_prompt>\n{filled_certifier}\n</certifier_prompt>")

    # Check for previous failed build — inject findings so agent doesn't repeat mistakes
    prev_failure = _get_previous_failure(project_dir)
    if prev_failure:
        prompt += f"\n\n## Previous Build Failed\n{prev_failure}"

    logger.info("Starting agentic v3 build: %s", build_id)
    start_time = time.monotonic()

    # One agent call — the agent drives everything.
    # capture_tool_output=True so subagent output (certifier results) is included
    # in the returned text for parsing.
    from otto.config import get_timeout
    timeout = get_timeout(config)
    try:
        text, cost = await run_agent_with_timeout(
            prompt, options,
            log_path=build_dir / "live.log",
            timeout=timeout,
            project_dir=project_dir,
            capture_tool_output=True,
        )
    except AgentCallError as err:
        text, cost = f"BUILD ERROR: {err.reason}", 0.0

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
                git_log_cmd = ["git", "log", "--oneline"]
                if _head_before:
                    git_log_cmd.append(f"{_head_before}..HEAD")
                else:
                    git_log_cmd.append("--max-count=20")
                git_log = _sp.run(
                    git_log_cmd,
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

    # Parse certification results from agent output
    from otto.markers import parse_certifier_markers
    parsed = parse_certifier_markers(text or "")
    stories_tested = parsed.stories_tested
    stories_passed = parsed.stories_passed
    story_results = parsed.stories
    story_evidence = parsed.story_evidence
    verdict_pass = parsed.verdict_pass
    overall_diagnosis = parsed.diagnosis
    certify_rounds = parsed.certify_rounds

    # When QA is skipped (--no-qa), the agent won't produce certification markers.
    # Consider the build passed if the agent completed without error.
    if skip_qa:
        # Agent completed (text is real output, not an error placeholder)
        passed = bool(text) and not text.startswith("BUILD ")
    else:
        # Require at least one story — VERDICT: PASS with no stories is not a real pass
        passed = verdict_pass and bool(story_results) and all(s["passed"] for s in story_results)

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
                 "passed_count": r.get("passed_count", sum(1 for s in r.get("stories", []) if s.get("passed")))}
                for i, r in enumerate(certify_rounds)
            ] if len(certify_rounds) > 1 else [],
            "mode": "agentic_v3",
        }
        from otto.observability import write_json_file
        write_json_file(report_dir / "proof-of-work.json", pow_data)

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
    from otto.observability import write_json_file
    write_json_file(build_dir / "checkpoint.json", checkpoint)

    logger.info("Agentic v3 done: %s, %d/%d stories, %.1fs, $%.2f",
                "passed" if passed else "failed",
                stories_passed, stories_tested, total_duration, float(cost or 0))

    # Improvement report — human-readable summary for post-auditing.
    try:
        _write_improvement_report(
            build_dir, build_id, intent, project_dir,
            certify_rounds, story_results, passed,
            stories_passed, stories_tested,
            total_duration, float(cost or 0),
            head_before=_head_before,
        )
    except Exception as exc:
        logger.warning("Failed to write improvement report: %s", exc)

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
        rounds=max(len(certify_rounds), 1),
        total_cost=float(cost or 0),
        journeys=journeys,
        tasks_passed=sum(1 for j in journeys if j["passed"]),
        tasks_failed=sum(1 for j in journeys if not j["passed"]),
    )


def _write_improvement_report(
    build_dir: Path,
    build_id: str,
    intent: str,
    project_dir: Path,
    certify_rounds: list[dict[str, Any]],
    story_results: list[dict[str, Any]],
    passed: bool,
    stories_passed: int,
    stories_tested: int,
    duration: float,
    cost: float,
    head_before: str = "",
) -> None:
    """Write a human-readable improvement report for post-auditing.

    Shows: what was found (bugs), what was changed (commits + diff stat),
    and what was verified (certifier results). Designed for human review.
    """
    lines = [
        f"# Improvement Report — {build_id}",
        f"> {time.strftime('%Y-%m-%d %H:%M')} | "
        f"{'PASSED' if passed else 'FAILED'} | "
        f"${cost:.2f} | {duration / 60:.1f} min",
        "",
        f"**Intent:** {intent[:300]}",
        "",
    ]


    # Filter out template placeholder rounds (from build prompt examples)
    real_rounds = [
        r for r in certify_rounds
        if r.get("stories") and not all(
            s.get("story_id") in ("(id)", "<story_id>", "<id>", "id", "")
            for s in r.get("stories", [])
        )
    ]

    # === Bugs Found ===
    # Extract failures from all rounds — these are the bugs that were found.
    # Failures in early rounds that become passes in later rounds = bugs fixed.
    all_failures: list[dict[str, Any]] = []
    for r in real_rounds:
        for s in r.get("stories", []):
            if not s.get("passed"):
                all_failures.append(s)

    if all_failures:
        lines.append("## Bugs Found")
        for f in all_failures:
            sid = f.get("story_id", "?")
            summary = f.get("summary", "")
            lines.append(f"- **{sid}**: {summary}")
        lines.append("")

    # === Changes Made ===
    # Git commits + diff stat — what code was actually changed
    try:
        git_range = f"{head_before}..HEAD" if head_before else "--max-count=20"
        git_log = subprocess.run(
            ["git", "log", "--oneline", git_range],
            cwd=str(project_dir), capture_output=True, text=True,
        ).stdout.strip()
        git_stat = subprocess.run(
            ["git", "diff", "--stat", git_range],
            cwd=str(project_dir), capture_output=True, text=True,
        ).stdout.strip() if head_before else ""
        if git_log:
            lines.append("## Changes Made")
            for commit_line in git_log.split("\n"):
                lines.append(f"- `{commit_line}`")
            if git_stat:
                # Just the summary line (e.g., "6 files changed, 122 insertions(+)")
                stat_lines = git_stat.strip().split("\n")
                if stat_lines:
                    lines.append(f"- {stat_lines[-1].strip()}")
            lines.append("")
    except Exception:
        pass

    # === Verification ===
    # Show certifier rounds — what was tested and whether fixes hold
    if real_rounds:
        lines.append(f"## Verification ({len(real_rounds)} round{'s' if len(real_rounds) != 1 else ''})")
        for i, r in enumerate(real_rounds):
            rn = r.get("round", i + 1)
            v = r.get("verdict")
            stories = r.get("stories", [])
            pc = r.get("passed_count", sum(1 for s in stories if s.get("passed")))
            tc = r.get("tested", len(stories))
            verdict_str = "PASS" if v else "FAIL"
            lines.append(f"### Round {rn} — {verdict_str} ({pc}/{tc})")
            for s in stories:
                icon = "\u2713" if s.get("passed") else "\u2717"
                sid = s.get("story_id", "?")
                summary = s.get("summary", "")
                lines.append(f"- {icon} {sid}: {summary}")
            diag = r.get("diagnosis", "")
            if diag:
                lines.append(f"- **Diagnosis:** {diag}")
            lines.append("")


    # === Summary ===
    lines.append("## Summary")
    lines.append(f"- **Result:** {'PASSED' if passed else 'FAILED'}")
    lines.append(f"- **Bugs found:** {len(all_failures)}")
    lines.append(f"- **Stories verified:** {stories_passed}/{stories_tested}")
    lines.append(f"- **Certification rounds:** {len(real_rounds)}")
    lines.append(f"- **Cost:** ${cost:.2f}")
    lines.append(f"- **Duration:** {duration / 60:.1f} min")
    lines.append("")

    report_path = build_dir / "improvement-report.md"
    report_path.write_text("\n".join(lines))


def _cleanup_orphan_processes(project_dir: Path) -> None:
    """Kill orphan processes (servers, watchers) left by the agent after timeout/crash."""
    try:
        # Find processes with cwd in the project directory
        import signal
        result = subprocess.run(
            ["lsof", "-ti", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip():
            for pid_str in result.stdout.strip().split("\n"):
                try:
                    pid = int(pid_str.strip())
                    # Check if process cwd matches project
                    cwd_check = subprocess.run(
                        ["lsof", "-p", str(pid), "-Fn"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if str(project_dir) in cwd_check.stdout:
                        os.kill(pid, signal.SIGTERM)
                        logger.info("Killed orphan process %d", pid)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
    except Exception:
        pass  # best-effort cleanup


def _get_previous_failure(project_dir: Path) -> str | None:
    """Read the most recent failed build's certifier findings, if any."""
    history_path = project_dir / "otto_logs" / "run-history.jsonl"
    if not history_path.exists():
        return None

    # Read last non-empty line efficiently (seek from end instead of reading all)
    last_line = ""
    try:
        with open(history_path, "rb") as f:
            # Seek to end, then scan backward for the last newline
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return None
            # Read up to last 4KB — each JSONL entry is well under this
            read_size = min(size, 4096)
            f.seek(size - read_size)
            chunk = f.read().decode("utf-8", errors="replace")
            for line in reversed(chunk.splitlines()):
                if line.strip():
                    last_line = line.strip()
                    break
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
        # Check per-section, not substring — prevents "build X" blocking "build X and Y"
        existing_intents = set()
        for section in existing.split("\n## "):
            lines = section.strip().split("\n", 1)
            if len(lines) > 1:
                existing_intents.add(lines[1].strip())
        if intent.strip() not in existing_intents:
            intent_path.write_text(existing.rstrip() + "\n" + entry)
    else:
        intent_path.write_text(f"# Build Intents\n{entry}")


async def run_certify_fix_loop(
    intent: str,
    project_dir: Path,
    config: dict[str, Any],
    *,
    certifier_mode: str = "thorough",
    focus: str | None = None,
    target: str | None = None,
    skip_initial_build: bool = False,
) -> BuildResult:
    """System-driven certify-fix loop.

    Python orchestrator drives every step:
      1. (Optional) Build agent builds from intent
      2. Certifier evaluates the product
      3. If issues found, build agent fixes
      4. Repeat until pass or max rounds

    For target mode (certifier_mode="target"), loop termination is based on
    METRIC_MET instead of story pass/fail.

    Used by: ``otto improve bugs``, ``otto improve feature``, ``otto improve target``.
    """
    from otto.certifier import run_agentic_certifier
    from otto.certifier.report import CertificationOutcome
    from otto.journal import (
        append_journal, init_round, record_build, record_certifier,
        update_current_state,
    )

    build_id = f"build-{int(time.time())}-{os.getpid()}"
    start_time = time.monotonic()
    total_cost = 0.0
    from otto.config import get_max_rounds
    max_rounds = get_max_rounds(config)

    _append_intent(project_dir, intent, build_id)
    _commit_artifacts(project_dir)

    # --- Optional initial build ---
    round_id = init_round(project_dir,
                          f"build: {intent[:60]}" if not skip_initial_build
                          else f"certify: {intent[:60]}")

    if not skip_initial_build:
        build_config = dict(config)
        build_config["skip_product_qa"] = True

        logger.info("Certify-fix loop: initial build")
        result = await build_agentic_v3(intent, project_dir, build_config)
        total_cost += result.total_cost
        record_build(project_dir, round_id, result)

    # --- Certify + fix loop ---
    last_stories: list[dict[str, Any]] = []
    passed = False
    actual_rounds = 0

    for round_num in range(1, max_rounds + 1):
        actual_rounds = round_num

        # Each certify round gets its own round_id so journal entries
        # don't land in the previous fix round's directory.
        round_id = init_round(project_dir, f"certify round {round_num}")

        logger.info("Certify-fix loop round %d: certifying (%s)", round_num, certifier_mode)
        report = await run_agentic_certifier(
            intent=intent,
            project_dir=project_dir,
            config=config,
            mode=certifier_mode,
            focus=focus,
            target=target,
        )
        total_cost += report.cost_usd
        stories = getattr(report, "_story_results", [])
        last_stories = stories

        record_certifier(project_dir, round_id, report, stories)

        # Infra error — certifier crashed/timed out
        if getattr(report, "outcome", None) == CertificationOutcome.INFRA_ERROR:
            logger.warning("Certify-fix loop: infra error on round %d", round_num)
            append_journal(project_dir, round_id, f"certify round {round_num}",
                           "INFRA_ERROR", report.cost_usd)
            break

        # Empty stories — certifier produced no results
        if not stories:
            logger.warning("Certify-fix loop round %d: no stories returned", round_num)
            append_journal(project_dir, round_id, f"certify round {round_num}",
                           "FAIL (no stories)", report.cost_usd)
            break

        # Update current state AFTER infra/empty checks
        update_current_state(project_dir, round_id, stories,
                             f"certify round {round_num}")

        failures = [s for s in stories if not s.get("passed")]
        result_str = (f"FAIL {len(stories) - len(failures)}/{len(stories)}"
                      if failures else
                      f"PASS {len(stories) - len(failures)}/{len(stories)}")

        # Target mode: check metric instead of story pass/fail
        metric_met = getattr(report, "_metric_met", None)
        metric_value = getattr(report, "_metric_value", "")
        if certifier_mode == "target" and metric_met is not None:
            result_str = f"{'MET' if metric_met else 'NOT MET'} ({metric_value})"

        append_journal(project_dir, round_id, f"certify round {round_num}",
                       result_str, report.cost_usd)

        # Determine if we should stop
        if certifier_mode == "target":
            if metric_met:
                passed = True
                logger.info("Certify-fix loop: target met on round %d (%s)",
                            round_num, metric_value)
                break
        elif not failures:
            passed = True
            logger.info("Certify-fix loop: PASS on round %d", round_num)
            break

        if round_num >= max_rounds:
            logger.info("Certify-fix loop: max rounds (%d) reached", max_rounds)
            break

        # --- Fix round ---
        round_id = init_round(project_dir, f"fix round {round_num}")

        fix_lines = [
            "Fix these issues found by the certifier.\n",
            "Read current-state.md for context on what was tried before.\n",
        ]
        for f in failures:
            sid = f.get("story_id", "?")
            summary = f.get("summary", "")
            evidence = f.get("evidence", "")
            fix_lines.append(f"### {sid}")
            fix_lines.append(f"**Symptom:** {summary}")
            if evidence:
                fix_lines.append(f"**Evidence:**\n```\n{evidence[:500]}\n```")
            fix_lines.append("")
        fix_lines.append(
            "Diagnose the root causes in the code and fix them. "
            "Do NOT fix by changing prompts unless the fix is generic."
        )

        fix_config = dict(config)
        fix_config["skip_product_qa"] = True

        logger.info("Certify-fix loop round %d: fixing %d issues", round_num, len(failures))
        fix_result = await build_agentic_v3(
            "\n".join(fix_lines), project_dir, fix_config)
        total_cost += fix_result.total_cost
        record_build(project_dir, round_id, fix_result)
        append_journal(project_dir, round_id, f"fix round {round_num}",
                       "done" if fix_result.passed else "warning",
                       fix_result.total_cost)

    total_duration = round(time.monotonic() - start_time, 1)

    # Final journal entry
    append_journal(project_dir, round_id, "build complete",
                   "PASS" if passed else "FAIL", total_cost)

    journeys = [
        {"name": s.get("summary", s.get("story_id", "")),
         "passed": s.get("passed", False),
         "story_id": s.get("story_id", "")}
        for s in last_stories
    ]

    return BuildResult(
        passed=passed,
        build_id=build_id,
        rounds=actual_rounds,
        total_cost=total_cost,
        journeys=journeys,
        tasks_passed=sum(1 for j in journeys if j.get("passed")),
        tasks_failed=sum(1 for j in journeys if not j.get("passed")),
    )



def _commit_artifacts(project_dir: Path) -> None:
    """Commit otto artifacts (intent.md, etc.) so agents see them."""
    git_timeout = 30  # seconds — prevent hang on locked repo
    try:
        subprocess.run(
            ["git", "add", "intent.md", "otto.yaml"],
            cwd=project_dir, capture_output=True, timeout=git_timeout,
        )
        # Only commit if there are staged changes
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=project_dir, capture_output=True, timeout=git_timeout,
        )
        if result.returncode != 0:
            subprocess.run(
                ["git", "commit", "-q", "-m", "otto: commit artifacts"],
                cwd=project_dir, capture_output=True, timeout=git_timeout,
            )
    except Exception:
        pass
