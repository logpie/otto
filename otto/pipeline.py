"""Otto build pipeline — agentic v3 build with certifier loop."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from otto.agent import AgentCallError

if TYPE_CHECKING:
    from otto.budget import RunBudget


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


def _stories_to_journeys(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert story results to journey dicts for BuildResult."""
    return [
        {"name": s.get("summary", s.get("story_id", "")),
         "passed": s.get("passed", False),
         "story_id": s.get("story_id", "")}
        for s in stories
    ]


async def build_agentic_v3(
    intent: str,
    project_dir: Path,
    config: dict[str, Any],
    *,
    certifier_mode: str = "thorough",
    prompt_mode: str = "build",
    resume_session_id: str | None = None,
    command: str = "build",
    manage_checkpoint: bool = True,
    record_intent: bool = True,
    resume_existing_session: bool = False,
    spec: str | None = None,
    run_id: str | None = None,
    spec_cost: float = 0.0,
    budget: "RunBudget | None" = None,
) -> BuildResult:
    """Fully agent-driven session: one agent, certifier as environment.

    prompt_mode controls the starting prompt:
      "build"   — build.md: build first, then certify (otto build)
      "improve" — improve.md: certify first, then fix (otto improve)
      "code"    — code.md: just build/fix, no certification (skip_product_qa)

    resume_session_id: if set, resumes an existing SDK session instead of starting fresh.

    command: value written to the checkpoint's `command` field. Used by the
      CLI to show a warning when the user resumes with a different subcommand.

    manage_checkpoint: when False, callers (e.g. run_certify_fix_loop) are
      responsible for the checkpoint lifecycle and this function leaves it
      alone. Default True — the function writes in_progress before the agent
      call and updates to completed after.

      CONTRACT: when manage_checkpoint=False, ``AgentCallError`` is re-raised
      instead of swallowed; the outer loop must own checkpoint-on-failure.
      When True (agent mode), the exception is caught and converted to a
      failed BuildResult with a paused checkpoint (existing behavior).

    budget: optional ``RunBudget`` (from otto.budget). When provided, the
      agent call's timeout derives from ``budget.for_call()``. Callers MUST
      check ``budget.exhausted()`` before calling; this function does not.

    certifier_mode controls which certifier prompt is pre-filled.
    """
    from otto.agent import make_agent_options, run_agent_with_timeout

    build_id = f"build-{int(time.time())}-{os.getpid()}"
    checkpoint_run_id = run_id or build_id
    build_dir = project_dir / "otto_logs" / "builds" / build_id
    build_dir.mkdir(parents=True, exist_ok=True)

    # Resumed SDK sessions already carry prior context; avoid polluting
    # intent.md or stdin when the user resumes without a fresh intent.
    if record_intent:
        _append_intent(project_dir, intent, build_id)
    _commit_artifacts(project_dir)

    # Record HEAD before build so the improvement report can show only new commits
    from otto.journal import _get_head_sha
    _head_before = _get_head_sha(project_dir)

    options = make_agent_options(project_dir, config)
    if resume_session_id:
        options.resume = resume_session_id

    evidence_dir_path: Path | None = None
    skip_qa = bool(config.get("skip_product_qa"))

    if skip_qa:
        prompt_mode = "code"

    if resume_existing_session and resume_session_id:
        prompt = ""
    else:
        # Spec-aware prompt rendering via safe render_prompt helper.
        from otto.prompts import render_prompt
        from otto.spec import format_spec_section
        spec_section = format_spec_section(spec)

        # Select prompt based on mode
        if prompt_mode == "code":
            prompt = render_prompt("code.md", spec_section=spec_section) + f"\n\nBuild this product:\n\n{intent}"
        elif prompt_mode == "improve":
            from otto.config import get_max_rounds
            max_certify_rounds = get_max_rounds(config)
            prompt = render_prompt("improve.md",
                                   max_certify_rounds=str(max_certify_rounds),
                                   spec_section=spec_section)
            prompt += f"\n\nImprove this product:\n\n{intent}"
        else:
            # Default: build mode
            from otto.config import get_max_rounds
            max_certify_rounds = get_max_rounds(config)
            prompt = render_prompt("build.md",
                                   max_certify_rounds=str(max_certify_rounds),
                                   spec_section=spec_section)
            prompt += f"\n\nBuild this product:\n\n{intent}"

        # Pre-fill certifier prompt for modes that use certification
        if prompt_mode != "code":
            # Per-run evidence dir so parallel/sequential runs don't clobber each other
            evidence_dir_path = project_dir / "otto_logs" / "certifier" / "evidence" / build_id
            evidence_dir = str(evidence_dir_path)
            safe_intent = intent.replace("</certifier_prompt>", "")
            certifier_filename = {
                "standard": "certifier.md",
                "fast": "certifier-fast.md",
                "thorough": "certifier-thorough.md",
                "hillclimb": "certifier-hillclimb.md",
                "target": "certifier-target.md",
            }.get(certifier_mode, "certifier.md")
            filled_certifier = render_prompt(
                certifier_filename,
                intent=safe_intent,
                evidence_dir=evidence_dir,
                focus_section="",
                spec_section=spec_section,
                target=config.get("_target") or "",
            )
            prompt += (f"\n\n## Pre-filled Certifier Prompt\n"
                       f"When you dispatch the certifier agent, use this EXACT prompt:\n"
                       f"<certifier_prompt>\n{filled_certifier}\n</certifier_prompt>")

        # Inject cross-run memory (opt-in via config)
        from otto.memory import inject_memory
        prompt = inject_memory(prompt, project_dir, config)

    logger.info("Starting agentic v3 build: %s", build_id)
    start_time = time.monotonic()

    from otto.checkpoint import load_checkpoint, write_checkpoint

    checkpoint_session_id = resume_session_id or ""
    total_run_cost = float(spec_cost or 0.0)
    if manage_checkpoint and not checkpoint_session_id:
        try:
            checkpoint_session_id = (
                (load_checkpoint(project_dir) or {}).get("session_id", "") or ""
            )
        except Exception as exc:
            logger.warning("Failed to read checkpoint for session resume: %s", exc)

    def _cp(status: str, session_id: str = "") -> None:
        if not manage_checkpoint:
            return
        try:
            write_checkpoint(
                project_dir,
                run_id=checkpoint_run_id,
                command=command,
                certifier_mode=certifier_mode,
                prompt_mode=prompt_mode,
                session_id=session_id,
                total_cost=total_run_cost,
                status=status,
                spec_cost=float(spec_cost or 0.0),
            )
        except Exception as exc:
            logger.warning("Failed to write checkpoint: %s", exc)

    # Pre-write an in_progress checkpoint so Ctrl-C/crash before the agent
    # returns leaves a resumable marker. On resumed runs, preserve the prior
    # session_id so a second crash is still resumable.
    _cp("in_progress", session_id=checkpoint_session_id)

    # One agent call — the agent drives everything.
    # capture_tool_output=True so subagent output (certifier results) is included
    # in the returned text for parsing.
    from otto.config import get_timeout
    safety_cap = get_timeout(config)  # optional per-call cap (escape hatch)
    if budget is not None:
        timeout = budget.for_call(safety_cap=safety_cap)
    else:
        # No budget — use safety_cap if set, else a very large default.
        timeout = safety_cap if safety_cap is not None else 86400

    try:
        text, cost, session_id = await run_agent_with_timeout(
            prompt, options,
            log_path=build_dir / "live.log",
            timeout=timeout,
            project_dir=project_dir,
            capture_tool_output=True,
        )
    except AgentCallError as err:
        if not manage_checkpoint:
            # Outer loop owns the checkpoint and error handling. Re-raise.
            raise
        # Agent mode: preserve session_id from streaming so --resume can
        # continue the SDK conversation instead of starting fresh.
        text = f"BUILD ERROR: {err.reason}"
        cost = 0.0
        session_id = err.session_id or checkpoint_session_id
        if session_id:
            logger.info("Agent failed but session_id preserved (%s) — --resume supported", session_id)
        else:
            logger.warning("Agent failed with no session_id — --resume will start fresh")
    total_run_cost += float(cost or 0)

    total_duration = round(time.monotonic() - start_time, 1)

    # Mark the run as paused (not completed) on AgentCallError so --resume
    # picks it up. Successful runs still mark as completed.
    final_status = "paused" if text.startswith("BUILD ERROR:") else "completed"
    _cp(final_status, session_id=session_id)

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
            f"[{ts}] Duration: {total_duration:.1f}s, Cost: ${total_run_cost:.2f}",
            f"[{ts}] Raw output: {len(text or '')} chars -> agent-raw.log",
        ]

        # Extract structured events from agent text
        if text:
            # Git commits = what was built/fixed
            try:
                git_log_cmd = ["git", "log", "--oneline"]
                if _head_before:
                    git_log_cmd.append(f"{_head_before}..HEAD")
                else:
                    git_log_cmd.append("--max-count=20")
                git_log = subprocess.run(
                    git_log_cmd,
                    cwd=str(project_dir), capture_output=True, text=True,
                ).stdout.strip()
                if git_log:
                    summary_lines.append(f"[{ts}] Git commits:")
                    for line in git_log.split("\n"):
                        summary_lines.append(f"[{ts}]   {line}")
            except (OSError, subprocess.SubprocessError) as exc:
                logger.debug("git log read for agent.log failed: %s", exc)

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

        from otto.observability import append_text_log
        append_text_log(agent_log_path, summary_lines)
    except Exception as exc:
        logger.warning("Failed to write agent log: %s", exc)

    # Parse certification results from agent output
    from otto.markers import parse_certifier_markers
    parsed = parse_certifier_markers(text or "")
    stories_tested = parsed.stories_tested
    stories_passed = parsed.stories_passed
    story_results = parsed.stories
    verdict_pass = parsed.verdict_pass
    overall_diagnosis = parsed.diagnosis
    certify_rounds = parsed.certify_rounds
    target_mode = bool(config.get("_target")) or certifier_mode == "target"

    # When QA is skipped (--no-qa), the agent won't produce certification markers.
    # Consider the build passed if the agent completed without error.
    if skip_qa:
        # Agent completed (text is real output, not an error placeholder)
        passed = bool(text) and not text.startswith("BUILD ")
    else:
        # Require at least one story — VERDICT: PASS with no stories is not a real pass
        passed = verdict_pass and bool(story_results) and all(s["passed"] for s in story_results)
        if target_mode:
            passed = passed and parsed.metric_met is True

    journeys = _stories_to_journeys(story_results)

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
            "mode": "agentic_v3",
        }
        # Build round history once, reuse for JSON and HTML
        round_history = [
            {"round": r.get("round", i+1), "verdict": r.get("verdict"),
             "stories_count": len(r.get("stories", [])),
             "passed_count": r.get("passed_count", sum(1 for s in r.get("stories", []) if s.get("passed")))}
            for i, r in enumerate(certify_rounds)
        ] if certify_rounds else []
        pow_data["round_history"] = round_history if len(certify_rounds) > 1 else []

        from otto.observability import write_json_file
        write_json_file(report_dir / "proof-of-work.json", pow_data)

        _generate_agentic_html_pow(
            report_dir, story_results,
            "passed" if passed else "failed",
            total_duration, float(cost or 0),
            stories_passed, stories_tested,
            diagnosis=overall_diagnosis,
            round_history=round_history,
            evidence_dir=evidence_dir_path,
        )
    except Exception as exc:
        logger.warning("Failed to write PoW: %s", exc)

    # Checkpoint
    checkpoint = {
        "build_id": build_id,
        "mode": "agentic_v3",
        "passed": passed,
        "duration_s": total_duration,
        "cost_usd": total_run_cost,
        "stories_tested": stories_tested,
        "stories_passed": stories_passed,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    from otto.observability import write_json_file
    write_json_file(build_dir / "checkpoint.json", checkpoint)

    logger.info("Agentic v3 done: %s, %d/%d stories, %.1fs, $%.2f",
                "passed" if passed else "failed",
                stories_passed, stories_tested, total_duration, total_run_cost)

    # Improvement report — human-readable summary for post-auditing.
    try:
        _write_improvement_report(
            build_dir, build_id, intent, project_dir,
            certify_rounds, story_results, passed,
            stories_passed, stories_tested,
            total_duration, total_run_cost,
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
        "cost_usd": round(total_run_cost, 2),
        "duration_s": total_duration,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    append_text_log(history_path, [history_entry])

    # Record cross-run memory (only if certification produced stories)
    if story_results and not skip_qa:
        from otto.memory import record_run
        record_run(project_dir, command="build", certifier_mode=certifier_mode,
                   stories=story_results, cost=float(cost or 0))

    return BuildResult(
        passed=passed,
        build_id=build_id,
        rounds=max(len(certify_rounds), 1),
        total_cost=total_run_cost,
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
    from otto.markers import _PLACEHOLDER_IDS
    real_rounds = [
        r for r in certify_rounds
        if r.get("stories") and not all(
            s.get("story_id", "") in _PLACEHOLDER_IDS
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
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("git summary for improvement report failed: %s", exc)

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
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Orphan-process cleanup skipped: %s", exc)



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
    start_round: int = 1,
    resume_cost: float = 0.0,
    resume_rounds: list[dict[str, Any]] | None = None,
    command: str = "improve",
    record_intent: bool = True,
    budget: "RunBudget | None" = None,
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

    from otto.checkpoint import write_checkpoint as _write_cp

    build_id = f"build-{int(time.time())}-{os.getpid()}"
    total_cost = resume_cost
    from otto.config import get_max_rounds
    max_rounds = get_max_rounds(config)
    checkpoint_rounds = list(resume_rounds or [])
    last_completed_round = max(start_round - 1, 0)
    checkpoint_phase = "initial_build" if not skip_initial_build else "certify"

    if record_intent:
        _append_intent(project_dir, intent, build_id)
    _commit_artifacts(project_dir)

    def _save_cp(status: str = "in_progress", *, phase: str | None = None) -> None:
        """Write checkpoint with current loop state."""
        nonlocal checkpoint_phase
        if phase is not None:
            checkpoint_phase = phase
        try:
            _write_cp(
                project_dir,
                run_id=build_id, command=command,
                certifier_mode=certifier_mode,
                focus=focus, target=target,
                max_rounds=max_rounds, phase=checkpoint_phase,
                current_round=last_completed_round,
                total_cost=total_cost, rounds=checkpoint_rounds,
                status=status,
            )
        except Exception as exc:
            logger.warning("Failed to write split-mode checkpoint: %s", exc)

    # --- Certify + fix loop state (declared early so _paused_result can close over it) ---
    last_stories: list[dict[str, Any]] = []

    def _paused_result(phase: str, *, use_rounds: int = 1) -> BuildResult:
        logger.warning("Run budget exhausted before %s — pausing", phase)
        _save_cp(status="paused", phase=phase)
        return BuildResult(
            passed=False, build_id=build_id, total_cost=total_cost,
            rounds=use_rounds,
            journeys=_stories_to_journeys(last_stories) if last_stories else [],
        )

    # --- Optional initial build ---
    round_id = init_round(project_dir,
                          f"build: {intent[:60]}" if not skip_initial_build
                          else f"certify: {intent[:60]}")

    if not skip_initial_build:
        build_config = dict(config)
        build_config["skip_product_qa"] = True

        logger.info("Certify-fix loop: initial build")
        _save_cp(phase="initial_build")
        # Pre-check budget before initial build.
        if budget is not None and budget.exhausted():
            return _paused_result("initial_build")
        try:
            result = await build_agentic_v3(
                intent, project_dir, build_config,
                manage_checkpoint=False,
                budget=budget,
            )
        except AgentCallError as err:
            logger.warning("Initial build hit budget/timeout: %s", err.reason)
            _save_cp(status="paused", phase="initial_build")
            return BuildResult(passed=False, build_id=build_id, total_cost=total_cost)
        total_cost += result.total_cost
        record_build(project_dir, round_id, result)

    # --- Certify + fix loop ---
    passed = False
    actual_rounds = 0
    previous_attempts: list[dict[str, Any]] = []
    MAX_RETRIES = 2

    for round_num in range(start_round, max_rounds + 1):
        try:
            actual_rounds = round_num

            # Each certify round gets its own round_id
            round_id = init_round(project_dir, f"certify round {round_num}")

            _save_cp(phase="certify")

            # Pre-check budget before entering the certify call.
            if budget is not None and budget.exhausted():
                return _paused_result("certify", use_rounds=max(actual_rounds - 1, 1))

            # --- Certify with retry (AgentCallError re-raises to caller; other errors retry) ---
            report = None
            for attempt in range(MAX_RETRIES + 1):
                try:
                    logger.info("Certify-fix loop round %d: certifying (%s)", round_num, certifier_mode)
                    report = await run_agentic_certifier(
                        intent=intent,
                        project_dir=project_dir,
                        config=config,
                        mode=certifier_mode,
                        focus=focus,
                        target=target,
                        budget=budget,
                    )
                    break
                except AgentCallError:
                    # Budget exhaustion or agent timeout — don't retry.
                    raise
                except Exception as err:
                    if attempt < MAX_RETRIES:
                        logger.warning("Certify round %d attempt %d failed: %s. Retrying...",
                                       round_num, attempt + 1, err)
                        continue
                    logger.error("Certify round %d failed after %d attempts", round_num, MAX_RETRIES + 1)

            if report is None:
                append_journal(project_dir, round_id, f"certify round {round_num}",
                               "ERROR (all retries failed)", 0.0)
                break

            total_cost += report.cost_usd
            stories = report.story_results
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
            metric_met = report.metric_met
            metric_value = report.metric_value
            if certifier_mode == "target":
                if metric_met is True:
                    result_str = f"MET ({metric_value})"
                elif metric_met is False:
                    result_str = f"NOT MET ({metric_value})"
                else:
                    result_str = "FAIL (certifier omitted METRIC_MET)"

            append_journal(project_dir, round_id, f"certify round {round_num}",
                           result_str, report.cost_usd)

            round_summary = {
                "round": round_num,
                "stories_tested": len(stories),
                "stories_passed": len(stories) - len(failures),
                "cost": round(report.cost_usd, 2),
            }

            # Determine if we should stop
            if certifier_mode == "target":
                if metric_met is True:
                    checkpoint_rounds.append(round_summary)
                    last_completed_round = round_num
                    _save_cp(phase="round_complete")
                    passed = True
                    logger.info("Certify-fix loop: target met on round %d (%s)",
                                round_num, metric_value)
                    break
                if metric_met is None:
                    checkpoint_rounds.append(round_summary)
                    last_completed_round = round_num
                    _save_cp(phase="round_complete")
                    logger.warning(
                        "Certify-fix loop: stopping on round %d because certifier omitted METRIC_MET",
                        round_num,
                    )
                    break
            elif not failures:
                checkpoint_rounds.append(round_summary)
                last_completed_round = round_num
                _save_cp(phase="round_complete")
                passed = True
                logger.info("Certify-fix loop: PASS on round %d", round_num)
                break

            if round_num >= max_rounds:
                checkpoint_rounds.append(round_summary)
                last_completed_round = round_num
                _save_cp(phase="round_complete")
                logger.info("Certify-fix loop: max rounds (%d) reached", max_rounds)
                break

            # --- Fix round with retry ---
            round_id = init_round(project_dir, f"fix round {round_num}")
            _save_cp(phase="fix")

            fix_lines = [
                "Fix these issues found by the certifier.\n",
            ]

            # Inject memory: what was tried in previous rounds
            if previous_attempts:
                fix_lines.append("## Previous Attempts (DO NOT repeat these)")
                for attempt in previous_attempts:
                    fix_lines.append(f"\n### Round {attempt['round']}")
                    fix_lines.append(f"**Tried:** {attempt['commits']}")
                    fix_lines.append(f"**Changed:** {attempt['diff_stat']}")
                    still_failing = attempt.get("still_failing", [])
                    if still_failing:
                        fix_lines.append(f"**Still failing:** {', '.join(still_failing)}")
                fix_lines.append("")

            fix_lines.append("## Current Failures\n")
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

            from otto.journal import _get_head_sha
            head_before_fix = _get_head_sha(project_dir)

            # Pre-check budget before fix call.
            if budget is not None and budget.exhausted():
                return _paused_result("fix", use_rounds=actual_rounds)

            fix_result = None
            for attempt in range(MAX_RETRIES + 1):
                try:
                    logger.info("Certify-fix loop round %d: fixing %d issues (attempt %d)",
                                round_num, len(failures), attempt + 1)
                    fix_result = await build_agentic_v3(
                        "\n".join(fix_lines), project_dir, fix_config,
                        manage_checkpoint=False,
                        budget=budget,
                    )
                    break
                except AgentCallError:
                    # Budget exhaustion / timeout — don't retry.
                    raise
                except Exception as err:
                    if attempt < MAX_RETRIES:
                        logger.warning("Fix round %d attempt %d failed: %s. Retrying...",
                                       round_num, attempt + 1, err)
                        continue
                    logger.error("Fix round %d failed after %d attempts", round_num, MAX_RETRIES + 1)

            if fix_result:
                total_cost += fix_result.total_cost
                record_build(project_dir, round_id, fix_result)
                append_journal(project_dir, round_id, f"fix round {round_num}",
                               "done" if fix_result.passed else "warning",
                               fix_result.total_cost)

            # Record this attempt for future rounds
            head_after_fix = _get_head_sha(project_dir)
            commits = ""
            diff_stat = "(no changes)"
            if head_before_fix and head_after_fix and head_before_fix != head_after_fix:
                commits = subprocess.run(
                    ["git", "log", "--oneline", f"{head_before_fix}..{head_after_fix}"],
                    cwd=str(project_dir), capture_output=True, text=True,
                ).stdout.strip()
                diff_stat = subprocess.run(
                    ["git", "diff", "--stat", head_before_fix, head_after_fix],
                    cwd=str(project_dir), capture_output=True, text=True,
                ).stdout.strip().split("\n")[-1].strip()
            previous_attempts.append({
                "round": round_num,
                "commits": commits or "(no commits)",
                "diff_stat": diff_stat,
                "still_failing": [f.get("story_id", "?") for f in failures],
            })
            checkpoint_rounds.append(round_summary)
            last_completed_round = round_num
            _save_cp(phase="round_complete")

        except AgentCallError as err:
            # Budget exhausted or agent timed out mid-round. Don't run the
            # trailing complete_checkpoint path — return early with paused.
            logger.warning("Round %d paused (%s)", round_num, err.reason)
            try:
                _save_cp(status="paused")
            except OSError as exc:
                logger.warning("Failed to mark checkpoint paused: %s", exc)
            return BuildResult(
                passed=False, build_id=build_id, rounds=actual_rounds,
                total_cost=total_cost,
                journeys=_stories_to_journeys(last_stories) if last_stories else [],
            )
        except KeyboardInterrupt:
            logger.info("Paused at round %d", round_num)
            try:
                _save_cp(status="paused")
            except OSError as exc:
                logger.warning("Failed to mark checkpoint paused: %s", exc)
            raise

    # Final journal entry gets its own round_id so attribution is unambiguous
    # regardless of which branch above terminated the loop.
    final_round_id = init_round(project_dir, "loop complete")
    append_journal(project_dir, final_round_id, "build complete",
                   "PASS" if passed else "FAIL", total_cost)

    # Mark checkpoint completed
    try:
        from otto.checkpoint import complete_checkpoint
        complete_checkpoint(project_dir, total_cost)
    except Exception as exc:
        logger.warning("Failed to mark checkpoint completed: %s", exc)

    journeys = _stories_to_journeys(last_stories)

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
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("_commit_artifacts skipped: %s", exc)
