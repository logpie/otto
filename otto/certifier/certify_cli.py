#!/usr/bin/env python3
"""Certify CLI — job-based certification for the agentic build mode.

Usage:
  certify start <project_dir> <intent_file>   → starts certification, returns job_id
  certify status <project_dir>                → returns progress or final results
  certify results <project_dir>               → returns detailed results (after completion)

The agent calls these via Bash. No blocking — start returns immediately,
status polls until done. Each call is fast (< 1s), no timeout issues.

Results are written to otto_logs/certify-job/ in the project directory.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from otto.certifier.timeouts import (
    heartbeat_stale_after_seconds as story_heartbeat_stale_after_seconds,
    resolve_story_timeout_config,
)


HELP_TEXT = """\
Certify CLI — submit your product for user testing.

WORKFLOW:
  1. Build your product. Write tests. Make tests pass.
  2. Run: certify start <project_dir> <intent_file>
     → Starts full certification in the background. Returns immediately.
     → Your tests are checked first — if they fail, certification is refused.
  3. Run: certify status <project_dir>
     → Check progress. Call every 30-60 seconds.
     → Shows: "running, 4/7 stories verified" or final results.
  4. When status shows "passed", "failed", or "error" — read the results.
  5. Run: certify results <project_dir>
     → Detailed results with per-story pass/fail, diagnosis, and fix suggestions.

IF FAILED:
  - Read the issues carefully. Each has a diagnosis and fix suggestion.
  - Fix your code. Then certify again.
  - Use --retest-failed to only re-run failed stories (faster, cheaper).
  - Use --only "story name" to test a single specific story.
  - Progress tracking tells you if you're making progress or stuck.

IF ERROR:
  - This means the testing infrastructure failed, NOT your code.
  - Do NOT attempt code fixes. Report the error.

COMMANDS:
  certify start <project_dir> <intent_file> [OPTIONS]
    Start a certification round. Runs tests first as a gate.
    Returns: {"status": "started", "job_id": "...", "round": N}

    Options:
      --config <file>       Path to otto.yaml config
      --retest-failed       Only re-run stories that failed last round (skip passed)
      --only "<story>"      Run only the named story (for quick targeted testing)
      --skip-tests          Skip the pre-certification test gate
      --skip-break          Skip break/adversarial testing (faster, less thorough)

  certify status <project_dir>
    Check progress of the running certification.
    Returns: {"status": "running|passed|failed|error", ...}

  certify results <project_dir>
    Get detailed results after certification completes.
    Returns: full JSON with per-story results, issues, warnings, progress.

  certify stories <project_dir> <intent_file> [--config <file>]
    List compiled stories without running certification.
    Useful for seeing what will be tested before starting.

  certify help
    Show this help text.

TIPS:
  - Full certification: 5-15 min, 7 stories. Use for final validation.
  - Targeted retest: 1-3 min, only failed stories. Use after fixing specific bugs.
  - Single story: ~1 min. Use to quickly verify a specific fix.
  - Budget: max 5 rounds by default (configurable in otto.yaml).
  - Results include cost tracking — be mindful of certification spend.
"""


def main():
    if len(sys.argv) < 2:
        print(HELP_TEXT)
        sys.exit(0)

    command = sys.argv[1]
    if command in ("help", "--help", "-h"):
        print(HELP_TEXT)
    elif command == "start":
        _cmd_start()
    elif command == "status":
        _cmd_status()
    elif command == "results":
        _cmd_results()
    elif command == "stories":
        _cmd_stories()
    elif command == "_run_child":
        _cmd_run_child()
    else:
        print(f"Unknown command: {command}\n")
        print(HELP_TEXT)
        sys.exit(1)


def _parse_flag(name: str) -> bool:
    """Check if a flag like --retest-failed is present in argv."""
    return name in sys.argv


def _parse_option(name: str) -> str | None:
    """Parse a --name value option from argv."""
    if name in sys.argv:
        idx = sys.argv.index(name)
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    return None


def _cmd_start():
    """Start certification in the background. Returns immediately."""
    if len(sys.argv) < 4:
        print(json.dumps({"error": "Usage: certify start <project_dir> <intent_file> [OPTIONS]"}))
        sys.exit(1)

    project_dir = Path(sys.argv[2]).resolve()
    intent_file = Path(sys.argv[3]).resolve()
    config_path_str = _parse_option("--config")
    config_path = Path(config_path_str).resolve() if config_path_str else None

    # Parse options
    retest_failed = _parse_flag("--retest-failed")
    only_story = _parse_option("--only")
    skip_tests = _parse_flag("--skip-tests")
    skip_break = _parse_flag("--skip-break")

    job_root = _job_root(project_dir)
    job_root.mkdir(parents=True, exist_ok=True)

    # Budget check
    history = _load_history(job_root)
    config = _load_config(config_path)
    max_calls = int(config.get("max_verification_rounds", 5))
    call_count = len(history)

    if call_count >= max_calls:
        print(json.dumps({
            "status": "error",
            "message": f"Certification budget exceeded ({call_count}/{max_calls} rounds used).",
            "rounds_used": call_count,
            "rounds_max": max_calls,
            "history": _format_history_summary(history),
        }))
        return

    active_job = _find_running_job(job_root)
    if active_job is not None:
        active_dir, active_state = active_job
        print(json.dumps({
            "status": "already_running",
            "message": "A certification job is already running.",
            "job_id": active_dir.name,
            "pid": active_state.get("pid"),
        }))
        return

    job_id = _new_job_id()
    job_dir = job_root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    timeout_config = resolve_story_timeout_config(config)
    job_state = {
        "job_id": job_id,
        "status": "starting",
        "round": call_count + 1,
        "round_max": max_calls,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pid": None,
        "stories_total": 0,
        "stories_completed": 0,
        "timeout_config": timeout_config,
    }
    _write_job(job_dir, job_state)

    # Snapshot candidate
    try:
        candidate_sha = _snapshot(project_dir, job_dir)
    except RuntimeError:
        print(json.dumps(_read_job(job_dir) or {
            "status": "error",
            "job_id": job_id,
            "message": "Failed to snapshot certification candidate.",
        }))
        return

    # Pre-certification test gate: tests must pass before running certifier
    test_gate_result = None if skip_tests else _run_test_gate(project_dir)
    if test_gate_result is not None:
        job_state["status"] = "error"
        job_state["message"] = test_gate_result["message"]
        job_state["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_job(job_dir, job_state)
        print(json.dumps({
            "status": "error",
            "message": test_gate_result["message"],
            "test_output": test_gate_result["test_output"],
            "job_id": job_id,
            "round": call_count + 1,
        }))
        return

    # Write job state
    job_state = {
        **job_state,
        "status": "running",
        "candidate_sha": candidate_sha,
    }
    _write_job(job_dir, job_state)

    # Spawn certifier as a separate process (not fork — fork breaks asyncio/threads).
    # Run certify_cli.py with a special _run_child subcommand.
    child_cmd = [
        sys.executable, "-m", "otto.certifier.certify_cli",
        "_run_child",
        str(project_dir),
        str(intent_file),
        str(job_dir),
        candidate_sha,
        str(call_count + 1),
    ]
    if config_path:
        child_cmd.extend(["--config", str(config_path)])
    if retest_failed:
        child_cmd.append("--retest-failed")
    if only_story:
        child_cmd.extend(["--only", only_story])
    if skip_break:
        child_cmd.append("--skip-break")

    # Capture stderr for debugging — if the certifier crashes, we need the traceback
    stderr_path = job_dir / "stderr.log"
    stderr_fh = open(stderr_path, "w")
    proc = subprocess.Popen(
        child_cmd,
        cwd=str(project_dir),
        start_new_session=True,  # detach from parent
        stdout=subprocess.DEVNULL,
        stderr=stderr_fh,
    )
    job_state["pid"] = proc.pid
    _write_job(job_dir, job_state)
    print(json.dumps({
        "status": "started",
        "message": f"Certification round {call_count + 1}/{max_calls} started. Use 'certify status' to check progress.",
            "job_id": job_id,
            "round": call_count + 1,
            "round_max": max_calls,
            "candidate_sha": candidate_sha[:8],
        }))


def _cmd_status():
    """Check certification progress. Returns current state."""
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: certify status <project_dir>"}))
        sys.exit(1)

    project_dir = Path(sys.argv[2]).resolve()
    job_dir = _latest_job_dir(_job_root(project_dir))
    if job_dir is None:
        print(json.dumps({"status": "no_job", "message": "No certification running. Use 'certify start' first."}))
        return
    job_state = _read_job(job_dir)

    if not job_state:
        print(json.dumps({"status": "no_job", "message": "No certification running. Use 'certify start' first."}))
        return

    # Check if process is still alive
    pid = job_state.get("pid")
    if pid and job_state.get("status") == "running":
        # Check heartbeat — certifier writes one every story start/complete.
        heartbeat = _read_heartbeat(job_dir)
        if heartbeat:
            job_state["heartbeat"] = heartbeat
            stale_seconds = heartbeat.get("stale_seconds", 0)
            stale_after_s = heartbeat.get("stale_after_s") or _heartbeat_stale_after_seconds(job_state)
            job_state["heartbeat_stale_after_s"] = stale_after_s
            if stale_seconds > stale_after_s:
                job_state["warning"] = f"No heartbeat for {int(stale_seconds)}s — certifier may be stuck"

        try:
            os.kill(pid, 0)  # check if alive
            # Still running — report progress
            progress = _read_progress(job_dir)
            job_state.update(progress)
            print(json.dumps(job_state))
        except ProcessLookupError:
            # Process died — check if results were written
            if (job_dir / "result.json").exists():
                job_state = _read_job(job_dir)  # re-read, should be updated
                print(json.dumps(job_state))
            else:
                job_state["status"] = "error"
                job_state["message"] = "Certifier process died without writing results"
                _write_job(job_dir, job_state)
                print(json.dumps(job_state))
    else:
        # Already done (or error)
        print(json.dumps(job_state))


def _cmd_results():
    """Return detailed results. Only available after completion."""
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: certify results <project_dir>"}))
        sys.exit(1)

    project_dir = Path(sys.argv[2]).resolve()
    job_dir = _latest_job_dir(_job_root(project_dir))
    if job_dir is None:
        print(json.dumps({"error": "No results available. Run 'certify start' and wait for completion."}))
        return
    result_path = job_dir / "result.json"

    if not result_path.exists():
        print(json.dumps({"error": "No results available. Run 'certify start' and wait for completion."}))
        return

    result = json.loads(result_path.read_text())
    print(json.dumps(result, indent=2))


def _cmd_stories():
    """List compiled stories without running certification."""
    if len(sys.argv) < 4:
        print(json.dumps({"error": "Usage: certify stories <project_dir> <intent_file> [--config <file>]"}))
        sys.exit(1)

    project_dir = Path(sys.argv[2]).resolve()
    intent_file = Path(sys.argv[3]).resolve()
    config_path_str = _parse_option("--config")
    config = _load_config(Path(config_path_str).resolve() if config_path_str else None)

    intent = intent_file.read_text().strip()

    from otto.certifier.stories import load_or_compile_stories
    import asyncio

    story_set, source, _, _ = load_or_compile_stories(project_dir, intent, config=config)

    stories_out = []
    for s in story_set.stories:
        stories_out.append({
            "id": s.id,
            "title": s.title,
            "persona": s.persona,
            "critical": s.critical,
            "steps": len(s.steps),
            "break_strategies": s.break_strategies if hasattr(s, "break_strategies") else [],
        })

    print(json.dumps({
        "stories_count": len(stories_out),
        "source": source,
        "stories": stories_out,
    }, indent=2))


def _cmd_run_child():
    """Internal: run certifier in a spawned subprocess. Not for user use."""
    # _run_child <project_dir> <intent_file> <job_dir> <candidate_sha> <round_num> [--config <file>]
    if len(sys.argv) < 7:
        sys.exit(1)
    project_dir = Path(sys.argv[2]).resolve()
    intent_file = Path(sys.argv[3]).resolve()
    job_dir = Path(sys.argv[4]).resolve()
    candidate_sha = sys.argv[5]
    round_num = int(sys.argv[6])
    config_path = None
    if "--config" in sys.argv:
        idx = sys.argv.index("--config")
        if idx + 1 < len(sys.argv):
            config_path = Path(sys.argv[idx + 1]).resolve()

    # Parse flags forwarded from start
    retest_failed = _parse_flag("--retest-failed")
    only_story = _parse_option("--only")
    skip_break = _parse_flag("--skip-break")

    try:
        _run_certifier_child(
            project_dir, intent_file, config_path, job_dir, candidate_sha, round_num,
            retest_failed=retest_failed, only_story=only_story, skip_break=skip_break,
        )
    except Exception as exc:
        _write_job(job_dir, {
            "job_id": job_dir.name,
            "status": "error",
            "message": str(exc),
            "round": round_num,
            "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })


def _run_certifier_child(
    project_dir, intent_file, config_path, job_dir, candidate_sha, round_num,
    *, retest_failed=False, only_story=None, skip_break=False,
):
    """Run the certifier (in spawned subprocess). Writes results to job_dir."""

    intent = intent_file.read_text().strip()
    config = _load_config(config_path)

    from otto.certifier.isolated import run_isolated_certifier
    from otto.certifier.report import CertificationOutcome

    # Selective re-verify: skip stories based on flags
    skip_story_ids: set[str] | None = None

    if retest_failed or (round_num > 1):
        # --retest-failed or auto on round 2+: skip previously passed stories
        history = _load_history(job_dir.parent)
        skip_story_ids = _passed_story_ids_from_history(history)
        skipped = len(skip_story_ids) if skip_story_ids else 0
        if skipped > 0:
            import sys as _sys
            print(f"Round {round_num}: skipping {skipped} previously-passed stories", file=_sys.stderr)

    # --only "story name": test a single specific story (skip all others)
    # This is handled by compiling stories then filtering — the certifier
    # needs a story_filter parameter for this. For now, we pass it via config.
    if only_story:
        config["certifier_only_story"] = only_story

    # --skip-break: skip adversarial/break testing (faster)
    if skip_break:
        config["certifier_skip_break"] = True

    report = run_isolated_certifier(
        intent=intent,
        candidate_sha=candidate_sha,
        project_dir=project_dir,
        config=config,
        skip_story_ids=skip_story_ids,
    )

    # Map outcome
    if report.outcome == CertificationOutcome.PASSED:
        status = "passed"
    elif report.outcome in (CertificationOutcome.BLOCKED, CertificationOutcome.INFRA_ERROR):
        status = "error"
    else:
        status = "failed"

    # Build rich result for the agent
    issues = []
    for f in report.critical_findings():
        issues.append({
            "category": f.category,
            "what": f.description,
            "detail": f.diagnosis,
            "suggestion": f.fix_suggestion,
            "story": f.story_id,
        })

    warnings = []
    for f in report.break_findings():
        warnings.append({
            "severity": f.severity,
            "what": f.description,
            "suggestion": f.fix_suggestion,
        })

    # Per-story summary
    stories = []
    tier4 = next((t for t in report.tiers if t.tier == 4), None)
    if tier4 and hasattr(tier4, "_cert_result"):
        for r in tier4._cert_result.results:
            stories.append({
                "name": r.story_title,
                "passed": r.passed,
                "blocked_at": r.blocked_at,
                "diagnosis": r.diagnosis if not r.passed else None,
            })

    # Round context from history
    history = _load_history(job_dir.parent)
    prev_issues: set[str] = set()
    if history:
        prev = history[-1]
        prev_issues = _issue_fingerprints(prev.get("issues", []))
    current_issues = _issue_fingerprints(issues)
    same_issues = prev_issues & current_issues
    new_issues = current_issues - prev_issues
    resolved_issues = prev_issues - current_issues

    result = {
        "job_id": job_dir.name,
        "status": status,
        "round": round_num,
        "stories_passed": sum(1 for s in stories if s["passed"]),
        "stories_total": len(stories),
        "stories": stories,
        "issues": issues,
        "issues_count": len(issues),
        "warnings": warnings,
        "warnings_count": len(warnings),
        "cost_usd": round(report.cost_usd, 2),
        "duration_s": round(report.duration_s, 1),
        "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        # Progress context — help agent decide if it's making progress
        "progress": {
            "same_issues": len(same_issues),
            "new_issues": len(new_issues),
            "resolved_issues": len(resolved_issues),
            "message": _progress_message(same_issues, new_issues, resolved_issues, status),
        },
        # Cost awareness
        "budget": {
            "rounds_used": round_num,
            "rounds_max": int(config.get("max_verification_rounds", 5)),
            "total_cost_so_far": round(sum(h.get("cost_usd", 0) for h in history) + report.cost_usd, 2),
        },
    }

    # Write result
    (job_dir / "result.json").write_text(json.dumps(result, indent=2))

    # Update job state
    job_state = {**result, "pid": None}
    _write_job(job_dir, job_state)

    # Append to history
    history.append(result)
    _save_history(job_dir.parent, history)


def _progress_message(same, new, resolved, status):
    """Human-readable progress message for the agent."""
    if status == "passed":
        return "All stories passed! Product is certified."
    if status == "error":
        return "Certification infrastructure failed. This is NOT a code bug — do not attempt fixes."

    parts = []
    if resolved:
        parts.append(f"{len(resolved)} issue(s) you fixed are now resolved")
    if same:
        parts.append(f"{len(same)} issue(s) persist from last round (same bugs, not yet fixed)")
    if new:
        parts.append(f"{len(new)} new issue(s) found")
    if not parts:
        parts.append("Issues found — see details above")

    if same and not resolved and not new:
        parts.append("WARNING: No progress since last round. Consider a different approach or stopping.")

    return ". ".join(parts) + "."


# ── Helpers ──

def _snapshot(project_dir, job_dir):
    add_result = subprocess.run(
        ["git", "add", "-A"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if add_result.returncode != 0:
        _write_job_error(
            job_dir,
            "Failed to stage certification snapshot.",
            phase="snapshot",
            command="git add -A",
            stdout=add_result.stdout.strip(),
            stderr=add_result.stderr.strip(),
        )
        raise RuntimeError("Failed to stage certification snapshot.")

    commit_result = subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "otto: certify candidate"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if commit_result.returncode != 0:
        _write_job_error(
            job_dir,
            "Failed to create certification snapshot commit.",
            phase="snapshot",
            command="git commit --allow-empty -m 'otto: certify candidate'",
            stdout=commit_result.stdout.strip(),
            stderr=commit_result.stderr.strip(),
        )
        raise RuntimeError("Failed to create certification snapshot commit.")

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        _write_job_error(
            job_dir,
            "Failed to read certification snapshot commit SHA.",
            phase="snapshot",
            command="git rev-parse HEAD",
            stdout=result.stdout.strip(),
            stderr=result.stderr.strip(),
        )
        raise RuntimeError("Failed to read certification snapshot commit SHA.")
    return result.stdout.strip()


def _write_job(job_dir, state):
    (job_dir / "job.json").write_text(json.dumps(state, indent=2, default=str))


def _read_job(job_dir):
    path = job_dir / "job.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
    return None


def _read_progress(job_dir):
    """Read partial progress from certifier worktree (if available)."""
    # Check for journey-agent.log in any active worktree
    wt_parent = _project_dir_from_job_dir(job_dir) / ".otto-worktrees"
    if wt_parent.exists():
        for wt in sorted(wt_parent.iterdir(), reverse=True):
            if wt.name.startswith("certifier-"):
                log = wt / "otto_logs" / "certifier" / "journey-agent.log"
                if log.exists():
                    content = log.read_text()
                    started = content.count("Verifying story:")
                    completed = content.count("Story completed:")
                    return {
                        "stories_started": started,
                        "stories_completed": completed,
                        "message": f"Testing in progress: {completed}/{started} stories verified so far...",
                    }
    return {}


def _read_heartbeat(job_dir):
    """Read certifier heartbeat. Returns dict with staleness info."""
    # Heartbeat is written by journey_agent.py to the worktree
    wt_parent = _project_dir_from_job_dir(job_dir) / ".otto-worktrees"
    if not wt_parent.exists():
        return None
    for wt in sorted(wt_parent.iterdir(), reverse=True):
        if wt.name.startswith("certifier-"):
            hb_path = wt / "otto_logs" / "certifier" / "heartbeat.json"
            if hb_path.exists():
                try:
                    hb = json.loads(hb_path.read_text())
                    # Compute staleness
                    ts = hb.get("timestamp", "")
                    if ts:
                        hb_time = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
                        hb["stale_seconds"] = round(time.time() - hb_time)
                    if "stale_after_s" not in hb:
                        hb["stale_after_s"] = _heartbeat_stale_after_seconds(_read_job(job_dir) or {})
                    return hb
                except (json.JSONDecodeError, ValueError):
                    pass
    return None


def _load_history(job_dir):
    path = job_dir / "history.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return []
    return []


def _save_history(job_dir, history):
    (job_dir / "history.json").write_text(json.dumps(history, indent=2, default=str))


def _format_history_summary(history):
    """Brief summary of past rounds for budget-exceeded message."""
    lines = []
    for h in history:
        r = h.get("round", "?")
        s = h.get("status", "?")
        issues = h.get("issues_count", 0)
        lines.append(f"Round {r}: {s} ({issues} issues)")
    return "; ".join(lines)


def _load_config(config_path):
    if config_path and Path(config_path).exists():
        import yaml
        return yaml.safe_load(Path(config_path).read_text()) or {}
    return {}


def _job_root(project_dir: Path) -> Path:
    return project_dir / "otto_logs" / "certify-job"


def _latest_job_dir(job_root: Path) -> Path | None:
    if not job_root.exists():
        return None
    job_dirs = sorted((path for path in job_root.iterdir() if path.is_dir()), reverse=True)
    return job_dirs[0] if job_dirs else None


def _new_job_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _find_running_job(job_root: Path) -> tuple[Path, dict[str, object]] | None:
    if not job_root.exists():
        return None
    for job_dir in sorted((path for path in job_root.iterdir() if path.is_dir()), reverse=True):
        state = _read_job(job_dir)
        if not state or state.get("status") != "running":
            continue
        pid = state.get("pid")
        if isinstance(pid, int) and _pid_alive(pid):
            return job_dir, state
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _project_dir_from_job_dir(job_dir: Path) -> Path:
    return job_dir.parent.parent.parent


def _heartbeat_stale_after_seconds(job_state: dict[str, object]) -> float:
    timeout_config = job_state.get("timeout_config")
    if isinstance(timeout_config, dict):
        return story_heartbeat_stale_after_seconds(timeout_config)
    return story_heartbeat_stale_after_seconds({})


def _issue_fingerprints(issues: list[dict[str, object]]) -> set[str]:
    from otto.feedback import finding_fingerprints

    return finding_fingerprints([
        SimpleNamespace(
            category=str(issue.get("category", "") or ""),
            description=str(issue.get("what", "") or ""),
            story_id=str(issue.get("story", "") or ""),
        )
        for issue in issues
    ])


def _passed_story_ids_from_history(history: list[dict[str, object]]) -> set[str] | None:
    """Extract story IDs that passed in the most recent round.

    Returns None if no story data is available (don't skip anything).
    """
    if not history:
        return None
    last_round = history[-1]
    stories = last_round.get("stories")
    if not stories or not isinstance(stories, list):
        return None
    passed = set()
    for story in stories:
        if isinstance(story, dict) and story.get("passed"):
            name = story.get("name", "")
            if name:
                passed.add(name)
    return passed if passed else None


def _write_job_error(job_dir: Path, message: str, **extra: object) -> None:
    state = _read_job(job_dir) or {"job_id": job_dir.name}
    state.update({
        "status": "error",
        "message": message,
        "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        **extra,
    })
    _write_job(job_dir, state)


def _run_test_gate(project_dir: Path) -> dict[str, str] | None:
    """Run the project's test command as a pre-certification gate.

    Returns None if tests pass or no test command is found (don't block).
    Returns {"message": ..., "test_output": ...} if tests fail.
    """
    from otto.config import detect_test_command

    test_cmd = detect_test_command(project_dir)
    if not test_cmd:
        return None  # No test command found — skip the gate

    result = subprocess.run(
        test_cmd,
        shell=True,
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode == 0:
        return None  # Tests passed

    # Tests failed — combine stdout/stderr for output
    output_parts = []
    if result.stdout.strip():
        output_parts.append(result.stdout.strip())
    if result.stderr.strip():
        output_parts.append(result.stderr.strip())
    test_output = "\n".join(output_parts)
    # Truncate to avoid oversized JSON
    if len(test_output) > 2000:
        test_output = test_output[-2000:]

    return {
        "message": "Tests must pass before certification. Fix your tests first.",
        "test_output": test_output,
    }


if __name__ == "__main__":
    main()
