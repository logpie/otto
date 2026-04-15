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
    break_findings: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    tasks_passed: int = 0
    tasks_failed: int = 0


def _load_build_prompt() -> str:
    """Load the v3 build prompt from otto/prompts/build.md."""
    from otto.prompts import build_prompt
    return build_prompt()


async def build_agentic_v3(
    intent: str,
    project_dir: Path,
    config: dict[str, Any],
) -> BuildResult:
    """Fully agent-driven build: one session, certifier as environment.

    The coding agent does everything — build, self-test, dispatch certifier,
    read findings, fix, re-certify. The orchestrator just launches and waits.
    """
    from otto.agent import ClaudeAgentOptions, _subprocess_env, make_live_logger, run_agent_query
    from otto.observability import append_text_log

    build_id = f"build-{int(time.time())}-{os.getpid()}"
    build_dir = project_dir / "otto_logs" / "builds" / build_id
    build_dir.mkdir(parents=True, exist_ok=True)

    # Append intent to cumulative log
    _append_intent(project_dir, intent, build_id)
    _commit_artifacts(project_dir)

    # Record HEAD before build so the improvement report can show only new commits
    try:
        _head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_dir), capture_output=True, text=True,
        ).stdout.strip()
    except Exception:
        _head_before = ""

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

    max_certify_rounds = int(config.get("max_certify_rounds", 8))
    raw_prompt = _load_build_prompt().replace("{max_certify_rounds}", str(max_certify_rounds))
    prompt = raw_prompt + f"\n\nBuild this product:\n\n{intent}"

    # Skip certifier if requested (e.g., otto improve handles verification itself)
    if config.get("skip_product_qa"):
        prompt += ("\n\n## IMPORTANT: Skip Certification\n"
                   "Do NOT dispatch a certifier agent. Just fix the code, run tests, "
                   "commit, and report your results. Certification is handled externally.")
    else:
        # Pre-fill the certifier prompt so the agent can dispatch it directly.
        # This avoids the agent needing to read files or fill placeholders.
        from otto.prompts import certifier_prompt
        evidence_dir = str(project_dir / "otto_logs" / "certifier" / "evidence")
        filled_certifier = certifier_prompt(mode="thorough").format(
            intent=intent, evidence_dir=evidence_dir, focus_section="")
        prompt += (f"\n\n## Pre-filled Certifier Prompt\n"
                   f"When you dispatch the certifier agent, use this EXACT prompt:\n"
                   f"```\n{filled_certifier}\n```")

    # Check for previous failed build — inject findings so agent doesn't repeat mistakes
    prev_failure = _get_previous_failure(project_dir)
    if prev_failure:
        prompt += f"\n\n## Previous Build Failed\n{prev_failure}"

    logger.info("Starting agentic v3 build: %s", build_id)
    start_time = time.monotonic()

    # One agent call — the agent drives everything.
    # capture_tool_output=True so subagent output (certifier results) is included
    # in the returned text for parsing.
    try:
        timeout = int(config.get("certifier_timeout", 1800))
    except (ValueError, TypeError):
        logger.warning("Invalid certifier_timeout, using default 1800s")
        timeout = 1800
    if timeout <= 0:
        logger.warning("certifier_timeout must be positive, using default 1800s")
        timeout = 1800
    result_msg = None
    build_live_log = build_dir / "live.log"
    build_callbacks = make_live_logger(build_live_log)
    _close_build_log = build_callbacks.pop("_close")
    try:
        text, cost, result_msg = await asyncio.wait_for(
            run_agent_query(prompt, options, capture_tool_output=True,
                            **build_callbacks),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.error("Build timed out after %ds", timeout)
        text, cost = f"BUILD TIMED OUT after {timeout}s", 0.0
        _cleanup_orphan_processes(project_dir)
    except KeyboardInterrupt:
        _close_build_log()
        _cleanup_orphan_processes(project_dir)
        raise
    except Exception as exc:
        logger.exception("Build agent crashed")
        text, cost = f"BUILD ERROR: {exc}", 0.0
        _cleanup_orphan_processes(project_dir)
    finally:
        _close_build_log()

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
                # Close any orphaned block before starting a new one
                current_eid = stripped.split(":", 1)[1].strip()
                ev_lines = []
            elif stripped.startswith("STORY_EVIDENCE_END:"):
                if current_eid:
                    story_evidence[current_eid] = "\n".join(ev_lines)
                current_eid = None
                ev_lines = []
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
                parts = stripped[len("STORY_RESULT:"):].strip().split("|", 2)
                if len(parts) >= 2:
                    sid = parts[0].strip()
                    if not sid or sid in ("(id)", "<story_id>", "<id>", "id"):
                        continue  # skip empty or template placeholder entries
                    passed = "PASS" in parts[1].upper()
                    summary = parts[2].strip() if len(parts) > 2 else ""
                    current_round["stories"].append({
                        "story_id": sid,
                        "passed": passed,
                        "summary": summary,
                        "evidence": story_evidence.get(sid, ""),
                    })
            elif stripped.startswith("VERDICT:"):
                # Skip template placeholders like "VERDICT: PASS or FAIL"
                verdict_text = stripped.split(":", 1)[1].strip()
                if "or" in verdict_text.lower():
                    continue
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
            # Deduplicate stories by story_id — if the same story appears multiple
            # times within a round (agent re-reporting after a fix without using
            # CERTIFY_ROUND markers), keep only the LAST result per story_id.
            seen: dict[str, dict[str, Any]] = {}
            for s in final_round["stories"]:
                seen[s["story_id"]] = s
            story_results = list(seen.values())
            stories_tested = final_round.get("tested", len(story_results))
            stories_passed = final_round.get("passed_count", sum(1 for s in story_results if s["passed"]))
            verdict_pass = bool(final_round.get("verdict", False))
            overall_diagnosis = final_round.get("diagnosis", "")
        else:
            # Fallback: scan from end for verdict/diagnosis (no CERTIFY_ROUND markers)
            found_verdict = False
            for line in reversed(text.split("\n")):
                stripped = line.strip()
                if stripped.startswith("VERDICT:") and not found_verdict:
                    verdict_text = stripped.split(":", 1)[1].strip()
                    if "or" in verdict_text.lower():
                        continue  # skip template placeholder
                    verdict_pass = "PASS" in stripped.upper()
                    found_verdict = True
                elif stripped.startswith("DIAGNOSIS:") and not overall_diagnosis:
                    diag = stripped[len("DIAGNOSIS:"):].strip()
                    if diag.lower().startswith("null"):
                        diag = diag[4:].strip()
                    if diag:
                        overall_diagnosis = diag
                if found_verdict and overall_diagnosis:
                    break
            # Also extract STORY_RESULTs from flat output (no round markers).
            # Use a dict keyed by story_id so that if the agent output contains
            # multiple implicit rounds (fix loop without CERTIFY_ROUND markers),
            # we keep only the LAST result per story (the final state).
            story_by_id: dict[str, dict[str, Any]] = {}
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
                    parts = stripped[len("STORY_RESULT:"):].strip().split("|", 2)
                    if len(parts) >= 2:
                        sid = parts[0].strip()
                        if not sid or sid in ("(id)", "<story_id>", "<id>", "id"):
                            continue  # skip empty or template placeholder entries
                        p = "PASS" in parts[1].upper()
                        summary = parts[2].strip() if len(parts) > 2 else ""
                        story_by_id[sid] = {
                            "story_id": sid, "passed": p, "summary": summary,
                            "evidence": story_evidence.get(sid, ""),
                        }
            story_results = list(story_by_id.values())

    # When QA is skipped (--no-qa), the agent won't produce certification markers.
    # Consider the build passed if the agent completed without error.
    skip_qa = bool(config.get("skip_product_qa"))
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


async def build_split(
    intent: str,
    project_dir: Path,
    config: dict[str, Any],
    *,
    max_rounds: int = 3,
) -> BuildResult:
    """Split build: system-controlled certify loop with build journal.

    The Python orchestrator drives every step:
      1. Build agent builds (no certifier knowledge)
      2. System runs certifier independently
      3. System writes journal (current-state.md, round evidence)
      4. If fail: build agent fixes (reads journal for context)
      5. Repeat until pass or max_rounds
    """
    from otto.certifier import run_agentic_certifier
    from otto.journal import (
        append_journal, init_round, record_build, record_certifier,
        update_current_state,
    )

    build_id = f"build-{int(time.time())}-{os.getpid()}"
    start_time = time.monotonic()
    total_cost = 0.0

    _append_intent(project_dir, intent, build_id)
    _commit_artifacts(project_dir)

    # --- Round 1: Initial build ---
    round_id = init_round(project_dir, f"build: {intent[:60]}")

    build_config = dict(config)
    build_config["skip_product_qa"] = True

    logger.info("Split build round 1: building")
    result = await build_agentic_v3(intent, project_dir, build_config)
    total_cost += result.total_cost
    record_build(project_dir, round_id, result)

    # --- Certify + fix loop ---
    last_stories: list[dict[str, Any]] = []
    passed = False

    for round_num in range(1, max_rounds + 1):
        # Certify (system-controlled)
        logger.info("Split build round %d: certifying", round_num)
        report = await run_agentic_certifier(
            intent=intent,
            project_dir=project_dir,
            config=config,
            mode="thorough",
        )
        total_cost += report.cost_usd
        stories = getattr(report, "_story_results", [])
        last_stories = stories

        record_certifier(project_dir, round_id, report, stories)
        update_current_state(project_dir, round_id, stories,
                             f"certify round {round_num}")

        failures = [s for s in stories if not s.get("passed")]
        result_str = f"PASS {len(stories) - len(failures)}/{len(stories)}"
        if failures:
            result_str = f"FAIL {len(stories) - len(failures)}/{len(stories)}"
        append_journal(project_dir, round_id, f"certify round {round_num}",
                       result_str, report.cost_usd)

        if not failures:
            passed = True
            logger.info("Split build: PASS on round %d", round_num)
            break

        if round_num >= max_rounds:
            logger.info("Split build: max rounds (%d) reached", max_rounds)
            break

        # --- Fix round ---
        round_id = init_round(project_dir, f"fix round {round_num}")

        # Build fix intent from failures
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

        fix_config = dict(config)
        fix_config["skip_product_qa"] = True

        logger.info("Split build round %d: fixing %d issues", round_num, len(failures))
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
        rounds=max_rounds,
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
