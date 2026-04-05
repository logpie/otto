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
    elif command == "_run_child":
        _cmd_run_child()
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

    try:
        _run_certifier_child(project_dir, intent_file, config_path, job_dir, candidate_sha, round_num)
    except Exception as exc:
        _write_job(job_dir, {
            "job_id": job_dir.name,
            "status": "error",
            "message": str(exc),
            "round": round_num,
            "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })


def _run_certifier_child(project_dir, intent_file, config_path, job_dir, candidate_sha, round_num):
    """Run the certifier (in spawned subprocess). Writes results to job_dir."""

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


def _write_job_error(job_dir: Path, message: str, **extra: object) -> None:
    state = _read_job(job_dir) or {"job_id": job_dir.name}
    state.update({
        "status": "error",
        "message": message,
        "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        **extra,
    })
    _write_job(job_dir, state)


if __name__ == "__main__":
    main()
