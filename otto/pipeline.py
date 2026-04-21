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
    breakdown: dict[str, dict[str, Any]] = field(default_factory=dict)


def _stories_to_journeys(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert story results to journey dicts for BuildResult."""
    return [
        {"name": s.get("summary", s.get("story_id", "")),
         "passed": s.get("passed", False),
         "story_id": s.get("story_id", "")}
        for s in stories
    ]


def _write_session_summary(
    project_dir: Path,
    session_id: str,
    *,
    verdict: str,
    passed: bool,
    cost: float,
    duration: float,
    stories_passed: int,
    stories_tested: int,
    rounds: int,
    status: str = "completed",
    intent: str = "",
    command: str = "build",
    breakdown: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Write the canonical summary artifact for a completed session.

    Includes `intent` and `command` so a single read of summary.json
    answers "what was this session about?" — no crossref to
    project-root intent.md required.
    """
    from otto import paths
    from otto.observability import write_json_file

    # Full precision preserved in JSON; round at display time only.
    summary = {
        "run_id": session_id,
        "command": command,
        "intent": intent,
        "verdict": verdict,
        "passed": passed,
        "cost_usd": float(cost),
        "duration_s": duration,
        "stories_passed": stories_passed,
        "stories_tested": stories_tested,
        "status": status,
        "rounds": rounds,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    queue_task_id = os.environ.get("OTTO_QUEUE_TASK_ID")
    if queue_task_id:
        summary["queue_task_id"] = queue_task_id
    branch = _current_branch_name(project_dir)
    if branch:
        summary["branch"] = branch
    head_sha = _current_head_sha(project_dir)
    if head_sha:
        summary["head_sha"] = head_sha
    if breakdown is not None:
        summary["breakdown"] = breakdown
    write_json_file(paths.session_summary(project_dir, session_id), summary)


def _round_cost(value: float) -> float:
    return round(float(value), 4)


def _current_branch_name(project_dir: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


def _current_head_sha(project_dir: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def _strict_mode_guidance(strict_mode: bool) -> str:
    if not strict_mode:
        return ""
    return (
        "   - STRICT MODE: after the first PASS, run the certifier one more time.\n"
        "     Stop only after you get two consecutive PASS verdicts.\n"
    )


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
    spec_duration: float = 0.0,
    budget: "RunBudget | None" = None,
    is_improve_run: bool = False,
    strict_mode: bool = False,
    verbose: bool = False,
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
    from otto import paths
    from otto.display import console

    # run_id is the unified session_id in the new layout. Older callers
    # (e.g. some tests) may omit it; allocate one locally in that case so
    # the path plumbing never has to deal with an empty id.
    session_id = run_id or paths.new_session_id(project_dir)
    paths.ensure_session_scaffold(project_dir, session_id)
    build_id = session_id                 # kept as a local alias for logs
    checkpoint_run_id = session_id
    build_dir = paths.build_dir(project_dir, session_id)
    # Point `latest` at this session so users can `tail -f $(readlink latest)/build/live.log`.
    paths.set_pointer(project_dir, paths.LATEST_POINTER, session_id)

    # Resumed SDK sessions already carry prior context; avoid polluting
    # intent.md or stdin when the user resumes without a fresh intent.
    if record_intent:
        _append_intent(project_dir, intent, build_id)
    _commit_artifacts(project_dir)

    # Record HEAD before build so the improvement report can show only new commits
    from otto.journal import _get_head_sha
    _head_before = _get_head_sha(project_dir)

    # "code" prompt_mode = split-mode surgical fix; treat as the "fix" agent.
    # Everything else (build, improve) is the "build" agent.
    _agent_type = "fix" if prompt_mode == "code" else "build"
    options = make_agent_options(project_dir, config, agent_type=_agent_type)
    if resume_session_id:
        options.resume = resume_session_id

    evidence_dir_path: Path | None = None
    skip_qa = bool(config.get("skip_product_qa"))
    strict_mode = bool(strict_mode or config.get("strict_mode"))
    verbose = bool(verbose or config.get("_verbose"))

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
                                   spec_section=spec_section,
                                   strict_mode=_strict_mode_guidance(strict_mode))
            prompt += f"\n\nImprove this product:\n\n{intent}"
        else:
            # Default: build mode
            from otto.config import get_max_rounds
            max_certify_rounds = get_max_rounds(config)
            prompt = render_prompt("build.md",
                                   max_certify_rounds=str(max_certify_rounds),
                                   spec_section=spec_section,
                                   strict_mode=_strict_mode_guidance(strict_mode))
            prompt += f"\n\nBuild this product:\n\n{intent}"

        # Pre-fill certifier prompt for modes that use certification
        if prompt_mode != "code":
            # Per-run evidence dir so parallel/sequential runs don't clobber each other
            evidence_dir_path = paths.certify_dir(project_dir, session_id) / "evidence"
            evidence_dir_path.mkdir(parents=True, exist_ok=True)
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
            checkpoint_data = load_checkpoint(project_dir) or {}
            checkpoint_session_id = (
                checkpoint_data.get("agent_session_id")
                or checkpoint_data.get("session_id", "")
                or ""
            )
        except Exception as exc:
            logger.warning("Failed to read checkpoint for session resume: %s", exc)

    def _cp(
        status: str,
        session_id: str = "",
        phase: str = "build",
        current_round: int = 0,
        rounds: list[dict[str, Any]] | None = None,
    ) -> None:
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
                phase=phase,
                current_round=current_round,
                rounds=rounds or [],
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
    # Timeout derives from the run budget. `None` means no timeout (asyncio
    # wait_for accepts this), so a call-level safety cap is unnecessary —
    # run_budget_seconds bounds the whole run.
    timeout = budget.for_call() if budget is not None else None

    try:
        text, cost, session_id, breakdown_data = await run_agent_with_timeout(
            prompt, options,
            log_dir=build_dir,
            phase_name="BUILD",
            timeout=timeout,
            project_dir=project_dir,
            capture_tool_output=True,
            on_terminal_event=console.print,
            verbose=verbose,
            strict_mode=strict_mode,
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
        breakdown_data = {"round_timings": [], "build_duration_s": None}
        if session_id:
            logger.info("Agent failed but session_id preserved (%s) — --resume supported", session_id)
        else:
            logger.warning("Agent failed with no session_id — --resume will start fresh")
    total_run_cost += float(cost or 0)

    total_duration = round(time.monotonic() - start_time, 1)

    # Determine final status. Actual checkpoint write happens after we parse
    # certification markers so round history is captured in the checkpoint.
    final_status = "paused" if text.startswith("BUILD ERROR:") else "completed"

    # Session logs (messages.jsonl, narrative.log) streamed during the run
    # and were closed by run_agent_with_timeout. Nothing to write here —
    # narrative.log IS the debuggable log, and messages.jsonl is the
    # machine-readable replay.

    # Parse certification results from agent output
    from otto.markers import compact_story_results, parse_certifier_markers
    parsed = parse_certifier_markers(text or "")
    stories_tested = parsed.stories_tested
    stories_passed = parsed.stories_passed
    story_results = compact_story_results(parsed.stories)
    verdict_pass = parsed.verdict_pass
    overall_diagnosis = parsed.diagnosis
    certify_rounds = parsed.certify_rounds
    target_mode = bool(config.get("_target")) or certifier_mode == "target"
    round_timings = list(breakdown_data.get("round_timings", []))

    breakdown: dict[str, dict[str, Any]] = {}
    if spec_cost > 0.0 and spec_duration > 0.0:
        breakdown["spec"] = {
            "duration_s": round(spec_duration, 1),
            "cost_usd": _round_cost(spec_cost),
        }

    rounds = len(round_timings)
    total_certify_s = sum(end - start for start, end in round_timings)
    build_duration_s = breakdown_data.get("build_duration_s")
    if rounds > 0:
        breakdown["certify"] = {
            "duration_s": round(total_certify_s, 1),
            "rounds": rounds,
        }

    if isinstance(build_duration_s, int | float):
        breakdown["build"] = {"duration_s": round(float(build_duration_s), 1)}
    elif rounds > 0:
        breakdown["build"] = {"duration_s": round(max(total_duration - total_certify_s, 0.0), 1)}
    else:
        breakdown["build"] = {"duration_s": round(total_duration, 1)}

    estimated_phase_costs = None
    if not skip_qa:
        from otto.logstream import estimate_phase_costs

        estimated_phase_costs = estimate_phase_costs(build_dir / "messages.jsonl", cost)
    if estimated_phase_costs:
        for phase_name, phase_costs in estimated_phase_costs.items():
            phase_entry = breakdown.get(phase_name)
            if not phase_entry:
                continue
            cost_value = phase_costs.get("cost_usd")
            if isinstance(cost_value, int | float):
                phase_entry["cost_usd"] = _round_cost(float(cost_value))
            if phase_costs.get("estimated") is True:
                phase_entry["estimated"] = True

    # Summarize certify rounds for the checkpoint so forensic reads see real
    # history instead of `current_round: 0, rounds: []`.
    _checkpoint_rounds = [
        {
            "round": r.get("round", i + 1),
            "verdict": r.get("verdict"),
            "stories_tested": len(r.get("stories", [])),
            "stories_passed": r.get(
                "passed_count",
                sum(1 for s in r.get("stories", []) if s.get("passed")),
            ),
        }
        for i, r in enumerate(certify_rounds)
    ]
    _cp(
        final_status,
        session_id=session_id,
        current_round=len(certify_rounds),
        rounds=_checkpoint_rounds,
    )

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
        if strict_mode:
            last_two_rounds = certify_rounds[-2:]
            passed = passed and len(last_two_rounds) == 2 and all(
                round_data.get("verdict") is True
                for round_data in last_two_rounds
            )

    journeys = _stories_to_journeys(story_results)

    # Write PoW report
    try:
        from otto.certifier import _generate_agentic_html_pow
        # NB: `session_id` here is the SDK session, not the otto session_id
        # (that's `build_id` in this function scope).
        report_dir = paths.certify_dir(project_dir, build_id)
        report_dir.mkdir(parents=True, exist_ok=True)

        certifier_cost = float(cost or 0)
        pow_data = {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "outcome": "passed" if passed else "failed",
            "duration_s": total_duration,
            # certifier-only cost — the cost of the agent call that drove certification.
            "certifier_cost_usd": certifier_cost,
            # total run cost — full precision (spec + agent).
            "total_cost_usd": total_run_cost,
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
            total_duration, total_run_cost,
            stories_passed, stories_tested,
            diagnosis=overall_diagnosis,
            round_history=round_history,
            evidence_dir=evidence_dir_path,
            certifier_cost=certifier_cost,
        )

        # Markdown PoW — complements .html/.json with a text-only summary.
        costs_differ = abs(total_run_cost - certifier_cost) > 1e-9
        if costs_differ:
            cost_line = (
                f"> **Cost:** Certifier ${certifier_cost:.2f}, "
                f"Total ${total_run_cost:.2f}"
            )
        else:
            cost_line = f"> **Cost:** ${total_run_cost:.2f}"
        md_lines = [
            "# Proof-of-Work Certification Report",
            "",
            f"> **Generated:** {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            f"> **Outcome:** {'passed' if passed else 'failed'}",
            f"> **Duration:** {total_duration:.0f}s",
            cost_line,
            f"> **Stories:** {stories_passed}/{stories_tested}",
            f"> **Rounds:** {len(certify_rounds) or 1}",
            "",
        ]
        for s in story_results:
            status = "WARN" if s.get("warn") else ("PASS" if s["passed"] else "FAIL")
            md_lines.append(f"- **{status}** {s.get('story_id', '?')}: {s.get('summary', '')}")
        if overall_diagnosis:
            md_lines += ["", "## Diagnosis", "", overall_diagnosis]
        (report_dir / "proof-of-work.md").write_text("\n".join(md_lines) + "\n")
    except Exception as exc:
        logger.warning("Failed to write PoW: %s", exc)

    # Checkpoint — full precision, ISO-Z timestamp.
    # `build_id` kept as an alias of `run_id` for one release (back-compat).
    # `cost_usd` kept as alias of `total_cost_usd` for one release (back-compat).
    checkpoint = {
        "run_id": build_id,
        "build_id": build_id,
        "mode": "agentic_v3",
        "passed": passed,
        "duration_s": total_duration,
        "total_cost_usd": total_run_cost,
        "cost_usd": total_run_cost,
        "stories_tested": stories_tested,
        "stories_passed": stories_passed,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    from otto.observability import write_json_file
    write_json_file(build_dir / "checkpoint.json", checkpoint)
    if manage_checkpoint and final_status == "completed":
        _write_session_summary(
            project_dir,
            build_id,
            verdict="passed" if passed else "failed",
            passed=passed,
            cost=total_run_cost,
            duration=total_duration,
            stories_passed=stories_passed,
            stories_tested=stories_tested,
            rounds=max(len(certify_rounds), 1),
            status=final_status,
            intent=intent,
            command=command,
            breakdown=breakdown or None,
        )

    logger.info("Agentic v3 done: %s, %d/%d stories, %.1fs, $%.2f",
                "passed" if passed else "failed",
                stories_passed, stories_tested, total_duration, total_run_cost)

    # Session report — human-readable summary for post-auditing. Only
    # written for improve runs; regular builds use summary.json in the
    # session dir (leaner, JSON-only record). NB: use `build_id`, not
    # `session_id` — `session_id` is overwritten by the SDK return earlier.
    if is_improve_run:
        try:
            _write_improvement_report(
                paths.improve_dir(project_dir, build_id), build_id, intent, project_dir,
                certify_rounds, story_results, passed,
                stories_passed, stories_tested,
                total_duration, total_run_cost,
                head_before=_head_before,
            )
        except Exception as exc:
            logger.warning("Failed to write session report: %s", exc)

    # Append to run history (one line per build for `otto history`)
    from otto.observability import append_text_log
    history_path = paths.history_jsonl(project_dir)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_entry = json.dumps({
        "build_id": build_id,
        "intent": intent[:200],
        "passed": passed,
        "stories_passed": stories_passed,
        "stories_tested": stories_tested,
        "certify_rounds": len(certify_rounds),
        "cost_usd": float(total_run_cost),
        "duration_s": total_duration,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    append_text_log(history_path, [history_entry])

    # Record cross-run memory (only if certification produced stories)
    if story_results and not skip_qa:
        from otto.memory import record_run
        record_run(
            project_dir,
            run_id=build_id,
            command="build",
            certifier_mode=certifier_mode,
            stories=story_results,
            cost=float(cost or 0),
        )

    return BuildResult(
        passed=passed,
        build_id=build_id,
        rounds=max(len(certify_rounds), 1),
        total_cost=total_run_cost,
        journeys=journeys,
        tasks_passed=sum(1 for j in journeys if j["passed"]),
        tasks_failed=sum(1 for j in journeys if not j["passed"]),
        breakdown=breakdown,
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

    # Caller passes the target dir (improve_dir(session_id) for `otto improve`,
    # legacy build dir for older callers). Rename: session-report.md.
    build_dir.mkdir(parents=True, exist_ok=True)
    report_path = build_dir / "session-report.md"
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
    session_id: str | None = None,
    is_improve_run: bool = True,
    spec: str | None = None,
    spec_cost: float = 0.0,
    spec_duration: float = 0.0,
    strict_mode: bool = False,
    verbose: bool = False,
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
    from otto.display import console
    from otto.journal import (
        append_journal, init_round, record_build, record_certifier,
        update_current_state,
    )

    from otto.checkpoint import write_checkpoint as _write_cp
    from otto import paths as _paths

    # Unified session_id (was build_id). Allocate if caller didn't provide.
    build_id = session_id or _paths.new_session_id(project_dir)
    _paths.ensure_session_scaffold(project_dir, build_id)
    _paths.set_pointer(project_dir, _paths.LATEST_POINTER, build_id)
    total_cost = resume_cost
    loop_start = time.monotonic()
    from otto.config import get_max_rounds
    max_rounds = get_max_rounds(config)
    checkpoint_rounds = list(resume_rounds or [])
    last_completed_round = max(start_round - 1, 0)
    checkpoint_phase = "initial_build" if not skip_initial_build else "certify"
    split_breakdown: dict[str, dict[str, Any]] = {}
    if spec_cost > 0.0 and spec_duration > 0.0:
        split_breakdown["spec"] = {
            "duration_s": round(spec_duration, 1),
            "cost_usd": _round_cost(spec_cost),
        }
    build_phase_duration = 0.0
    build_phase_cost = 0.0
    certify_phase_duration = 0.0
    certify_phase_cost = 0.0
    certify_phase_rounds = 0
    spec_cost_remaining = float(spec_cost or 0.0)

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
                          else f"certify: {intent[:60]}",
                          session_id=build_id)

    if not skip_initial_build:
        build_config = dict(config)
        build_config["skip_product_qa"] = True

        logger.info("Certify-fix loop: initial build")
        _save_cp(phase="initial_build")
        # Pre-check budget before initial build.
        if budget is not None and budget.exhausted():
            return _paused_result("initial_build")
        try:
            build_call_start = time.monotonic()
            result = await build_agentic_v3(
                intent, project_dir, build_config,
                manage_checkpoint=False,
                run_id=build_id,
                spec=spec,
                spec_cost=spec_cost,
                spec_duration=spec_duration,
                budget=budget,
                verbose=verbose,
            )
        except AgentCallError as err:
            build_phase_duration += time.monotonic() - build_call_start
            logger.warning("Initial build hit budget/timeout: %s", err.reason)
            _save_cp(status="paused", phase="initial_build")
            return BuildResult(passed=False, build_id=build_id, total_cost=total_cost)
        build_phase_duration += time.monotonic() - build_call_start
        total_cost += result.total_cost
        build_cost = max(float(result.total_cost) - spec_cost_remaining, 0.0)
        build_phase_cost += build_cost
        spec_cost_remaining = 0.0
        record_build(project_dir, round_id, result, session_id=build_id)

    # --- Certify + fix loop ---
    passed = False
    actual_rounds = 0
    consecutive_passes = 0
    previous_attempts: list[dict[str, Any]] = []
    MAX_RETRIES = 2

    for round_num in range(start_round, max_rounds + 1):
        try:
            actual_rounds = round_num

            # Each certify round gets its own round_id
            round_id = init_round(project_dir, f"certify round {round_num}", session_id=build_id)

            _save_cp(phase="certify")

            # Pre-check budget before entering the certify call.
            if budget is not None and budget.exhausted():
                return _paused_result("certify", use_rounds=max(actual_rounds - 1, 1))

            # --- Certify with retry (AgentCallError re-raises to caller; other errors retry) ---
            report = None
            for attempt in range(MAX_RETRIES + 1):
                try:
                    logger.info("Certify-fix loop round %d: certifying (%s)", round_num, certifier_mode)
                    certify_call_start = time.monotonic()
                    report = await run_agentic_certifier(
                        intent=intent,
                        project_dir=project_dir,
                        config=config,
                        mode=certifier_mode,
                        focus=focus,
                        target=target,
                        budget=budget,
                        session_id=build_id,
                        write_session_summary=False,
                        verbose=verbose,
                    )
                    certify_phase_duration += time.monotonic() - certify_call_start
                    certify_phase_cost += float(report.cost_usd)
                    certify_phase_rounds += 1
                    break
                except AgentCallError:
                    certify_phase_duration += time.monotonic() - certify_call_start
                    # Budget exhaustion or agent timeout — don't retry.
                    raise
                except Exception as err:
                    certify_phase_duration += time.monotonic() - certify_call_start
                    if attempt < MAX_RETRIES:
                        logger.warning("Certify round %d attempt %d failed: %s. Retrying...",
                                       round_num, attempt + 1, err)
                        continue
                    logger.error("Certify round %d failed after %d attempts", round_num, MAX_RETRIES + 1)

            if report is None:
                append_journal(project_dir, round_id, f"certify round {round_num}",
                               "ERROR (all retries failed)", 0.0, session_id=build_id)
                break

            total_cost += report.cost_usd
            stories = report.story_results
            last_stories = stories

            record_certifier(project_dir, round_id, report, stories, session_id=build_id)

            # Infra error — certifier crashed/timed out
            if getattr(report, "outcome", None) == CertificationOutcome.INFRA_ERROR:
                logger.warning("Certify-fix loop: infra error on round %d", round_num)
                append_journal(project_dir, round_id, f"certify round {round_num}",
                               "INFRA_ERROR", report.cost_usd, session_id=build_id)
                break

            # Empty stories — certifier produced no results
            if not stories:
                logger.warning("Certify-fix loop round %d: no stories returned", round_num)
                append_journal(project_dir, round_id, f"certify round {round_num}",
                               "FAIL (no stories)", report.cost_usd, session_id=build_id)
                break

            # Update current state AFTER infra/empty checks
            update_current_state(project_dir, round_id, stories,
                                 f"certify round {round_num}", session_id=build_id)

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
                           result_str, report.cost_usd, session_id=build_id)

            round_summary = {
                "round": round_num,
                "stories_tested": len(stories),
                "stories_passed": len(stories) - len(failures),
                "cost": float(report.cost_usd),
            }

            # Determine if we should stop
            if certifier_mode == "target":
                if metric_met is True:
                    checkpoint_rounds.append(round_summary)
                    last_completed_round = round_num
                    _save_cp(phase="round_complete")
                    consecutive_passes += 1
                    if strict_mode and consecutive_passes < 2 and round_num < max_rounds:
                        console.print(
                            "  [dim]\u2713 round "
                            f"{round_num} passed \u2014 re-verifying for consistency (strict mode)[/dim]"
                        )
                        logger.info("Certify-fix loop: strict re-verification after round %d", round_num)
                        continue
                    passed = consecutive_passes >= (2 if strict_mode else 1)
                    logger.info("Certify-fix loop: target met on round %d (%s)",
                                round_num, metric_value)
                    break
                consecutive_passes = 0
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
                consecutive_passes += 1
                if strict_mode and consecutive_passes < 2 and round_num < max_rounds:
                    console.print(
                        "  [dim]\u2713 round "
                        f"{round_num} passed \u2014 re-verifying for consistency (strict mode)[/dim]"
                    )
                    logger.info("Certify-fix loop: strict re-verification after round %d", round_num)
                    continue
                passed = consecutive_passes >= (2 if strict_mode else 1)
                logger.info("Certify-fix loop: PASS on round %d", round_num)
                break
            else:
                consecutive_passes = 0

            if round_num >= max_rounds:
                checkpoint_rounds.append(round_summary)
                last_completed_round = round_num
                _save_cp(phase="round_complete")
                logger.info("Certify-fix loop: max rounds (%d) reached", max_rounds)
                break

            # --- Fix round with retry ---
            round_id = init_round(project_dir, f"fix round {round_num}", session_id=build_id)
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
                    fix_call_start = time.monotonic()
                    fix_result = await build_agentic_v3(
                        "\n".join(fix_lines), project_dir, fix_config,
                        manage_checkpoint=False,
                        run_id=build_id,
                        spec=spec,
                        budget=budget,
                        verbose=verbose,
                    )
                    build_phase_duration += time.monotonic() - fix_call_start
                    break
                except AgentCallError:
                    build_phase_duration += time.monotonic() - fix_call_start
                    # Budget exhaustion / timeout — don't retry.
                    raise
                except Exception as err:
                    build_phase_duration += time.monotonic() - fix_call_start
                    if attempt < MAX_RETRIES:
                        logger.warning("Fix round %d attempt %d failed: %s. Retrying...",
                                       round_num, attempt + 1, err)
                        continue
                    logger.error("Fix round %d failed after %d attempts", round_num, MAX_RETRIES + 1)

            if fix_result:
                total_cost += fix_result.total_cost
                build_phase_cost += float(fix_result.total_cost)
                record_build(project_dir, round_id, fix_result, session_id=build_id)
                append_journal(project_dir, round_id, f"fix round {round_num}",
                               "done" if fix_result.passed else "warning",
                               fix_result.total_cost, session_id=build_id)

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
    final_round_id = init_round(project_dir, "loop complete", session_id=build_id)
    append_journal(project_dir, final_round_id, "build complete",
                   "PASS" if passed else "FAIL", total_cost, session_id=build_id)

    # Mark checkpoint completed. Plumb the round history + current round so
    # forensic reads of the completed checkpoint reflect real history
    # (otherwise the checkpoint shows `current_round: 0, rounds: []` even
    # after multiple rounds actually ran).
    try:
        from otto.checkpoint import complete_checkpoint
        complete_checkpoint(
            project_dir, total_cost,
            current_round=last_completed_round,
            rounds=list(checkpoint_rounds),
        )
    except Exception as exc:
        logger.warning("Failed to mark checkpoint completed: %s", exc)

    journeys = _stories_to_journeys(last_stories)
    if build_phase_duration > 0.0:
        build_entry: dict[str, Any] = {"duration_s": round(build_phase_duration, 1)}
        if build_phase_cost > 0.0:
            build_entry["cost_usd"] = _round_cost(build_phase_cost)
        split_breakdown["build"] = build_entry
    if certify_phase_duration > 0.0:
        certify_entry: dict[str, Any] = {
            "duration_s": round(certify_phase_duration, 1),
            "rounds": certify_phase_rounds,
        }
        if certify_phase_cost > 0.0:
            certify_entry["cost_usd"] = _round_cost(certify_phase_cost)
        split_breakdown["certify"] = certify_entry
    _write_session_summary(
        project_dir,
        build_id,
        verdict="passed" if passed else "failed",
        passed=passed,
        cost=total_cost,
        duration=round(time.monotonic() - loop_start, 1),
        stories_passed=sum(1 for j in journeys if j.get("passed")),
        stories_tested=len(journeys),
        rounds=actual_rounds,
        intent=intent,
        command=command,
        breakdown=split_breakdown or None,
    )

    return BuildResult(
        passed=passed,
        build_id=build_id,
        rounds=actual_rounds,
        total_cost=total_cost,
        journeys=journeys,
        tasks_passed=sum(1 for j in journeys if j.get("passed")),
        tasks_failed=sum(1 for j in journeys if not j.get("passed")),
        breakdown=split_breakdown,
    )



def _commit_artifacts(project_dir: Path) -> None:
    """Commit otto artifacts (intent.md, etc.) so agents see them."""
    git_timeout = 30  # seconds — prevent hang on locked repo
    files_to_stage = ["intent.md", "otto.yaml"]
    if os.environ.get("OTTO_INTERNAL_QUEUE_RUNNER") == "1":
        from otto.config import DEFAULT_CONFIG, load_config

        queue_cfg = load_config(project_dir / "otto.yaml").get("queue", {})
        bookkeeping_files = queue_cfg.get(
            "bookkeeping_files",
            DEFAULT_CONFIG["queue"]["bookkeeping_files"],
        )
        files_to_stage = [path for path in files_to_stage if path not in set(bookkeeping_files)]
    if not files_to_stage:
        return
    try:
        subprocess.run(
            ["git", "add", *files_to_stage],
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
