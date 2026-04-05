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
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: certify start|status|results <project_dir> [<intent_file>]"}))
        sys.exit(1)

    command = sys.argv[1]
    if command == "start":
        _cmd_start()
    elif command == "status":
        _cmd_status()
    elif command == "results":
        _cmd_results()
    else:
        print(json.dumps({"error": f"Unknown command: {command}. Use: start, status, results"}))
        sys.exit(1)


def _cmd_start():
    """Start certification in the background. Returns immediately."""
    if len(sys.argv) < 4:
        print(json.dumps({"error": "Usage: certify start <project_dir> <intent_file> [--config <file>]"}))
        sys.exit(1)

    project_dir = Path(sys.argv[2]).resolve()
    intent_file = Path(sys.argv[3]).resolve()
    config_path = None
    if "--config" in sys.argv:
        idx = sys.argv.index("--config")
        if idx + 1 < len(sys.argv):
            config_path = Path(sys.argv[idx + 1]).resolve()

    job_dir = project_dir / "otto_logs" / "certify-job"
    job_dir.mkdir(parents=True, exist_ok=True)

    # Budget check
    history = _load_history(job_dir)
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

    # Snapshot candidate
    candidate_sha = _snapshot(project_dir)

    # Write job state
    job_state = {
        "status": "running",
        "round": call_count + 1,
        "round_max": max_calls,
        "candidate_sha": candidate_sha,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pid": None,  # filled after fork
        "stories_total": 0,
        "stories_completed": 0,
    }
    _write_job(job_dir, job_state)

    # Fork the certifier as a background process
    pid = os.fork()
    if pid == 0:
        # Child: run certifier, write results, exit
        try:
            _run_certifier_child(project_dir, intent_file, config_path, job_dir, candidate_sha, call_count + 1)
        except Exception as exc:
            _write_job(job_dir, {
                "status": "error",
                "message": str(exc),
                "round": call_count + 1,
                "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        os._exit(0)
    else:
        # Parent: record PID, return immediately
        job_state["pid"] = pid
        _write_job(job_dir, job_state)
        print(json.dumps({
            "status": "started",
            "message": f"Certification round {call_count + 1}/{max_calls} started. Use 'certify status' to check progress.",
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
    job_dir = project_dir / "otto_logs" / "certify-job"
    job_state = _read_job(job_dir)

    if not job_state:
        print(json.dumps({"status": "no_job", "message": "No certification running. Use 'certify start' first."}))
        return

    # Check if process is still alive
    pid = job_state.get("pid")
    if pid and job_state.get("status") == "running":
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
    job_dir = project_dir / "otto_logs" / "certify-job"
    result_path = job_dir / "result.json"

    if not result_path.exists():
        print(json.dumps({"error": "No results available. Run 'certify start' and wait for completion."}))
        return

    result = json.loads(result_path.read_text())
    print(json.dumps(result, indent=2))


def _run_certifier_child(project_dir, intent_file, config_path, job_dir, candidate_sha, round_num):
    """Run the certifier (in forked child process). Writes results to job_dir."""
    # Detach from parent's process group so we don't get killed
    os.setsid()

    intent = intent_file.read_text().strip()
    config = _load_config(config_path)

    from otto.certifier.isolated import run_isolated_certifier
    from otto.certifier.report import CertificationOutcome

    report = run_isolated_certifier(
        intent=intent,
        candidate_sha=candidate_sha,
        project_dir=project_dir,
        config=config,
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
    history = _load_history(job_dir)
    prev_issues = set()
    if history:
        prev = history[-1]
        prev_issues = {i.get("what", "") for i in prev.get("issues", [])}
    current_issues = {i["what"] for i in issues}
    same_issues = prev_issues & current_issues
    new_issues = current_issues - prev_issues
    resolved_issues = prev_issues - current_issues

    result = {
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
    _save_history(job_dir, history)


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

def _snapshot(project_dir):
    subprocess.run(["git", "add", "-A"], cwd=project_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "otto: certify candidate"],
        cwd=project_dir, capture_output=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir, capture_output=True, text=True,
    )
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
    wt_parent = job_dir.parent.parent / ".otto-worktrees"
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


if __name__ == "__main__":
    main()
