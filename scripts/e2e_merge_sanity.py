"""E2E sanity check after the i2p merge.

Two phases:

1. **Single-task**: `otto build --fast` on a tiny intent. Verifies the new
   per-session layout (otto_logs/sessions/<id>/) is populated correctly:
   narrative.log streams, messages.jsonl is readable, summary.json + manifest
   land in the session dir.

2. **Parallel + merge**: two `otto queue build` tasks on non-conflicting
   intents, `otto queue run --concurrent 2`, then `otto merge --all`.
   Verifies each queued task gets its own sessions/<id>/ (no collision),
   manifests are readable, and the merge orchestrator finds them via the
   new sessions/*/manifest.json scan path.

Usage: OTTO_ALLOW_REAL_COST=1 .venv/bin/python scripts/e2e_merge_sanity.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from bench_runner import (  # noqa: E402
    OTTO_BIN,
    log,
    make_repo,
    otto_run,
    queue_state,
    start_watcher,
    stop_watcher,
    wait_for_all_done,
)
from real_cost_guard import require_real_cost_opt_in  # noqa: E402


INTENT_SINGLE = (
    "Build a Python script `greet.py` that prints 'hello world' when run "
    "with `python greet.py`. Include a test `test_greet.py` that asserts "
    "the output is 'hello world\\n'."
)

INTENT_A = (
    "Build a Python script `add.py` with argparse: `python add.py A B` prints "
    "A+B (integer addition). Include `test_add.py` that asserts "
    "`python add.py 2 3` outputs `5`."
)

INTENT_B = (
    "Build a Python script `mul.py` with argparse: `python mul.py A B` prints "
    "A*B (integer multiplication). Include `test_mul.py` that asserts "
    "`python mul.py 2 3` outputs `6`."
)


def _fail(msg: str) -> None:
    log(f"FAIL: {msg}")
    sys.exit(1)


def _check(cond: bool, msg: str) -> None:
    if not cond:
        _fail(msg)
    log(f"  OK: {msg}")


# --------------------------------------------------------------------- phase 1

def phase1_single_task() -> tuple[float, float]:
    log("=" * 60)
    log("PHASE 1: single-task — validate new per-session layout")
    log("=" * 60)
    repo = make_repo("e2e-single-")
    log(f"repo: {repo}")

    t0 = time.time()
    result = subprocess.run(
        [str(OTTO_BIN), "build", "--fast", INTENT_SINGLE],
        cwd=repo, capture_output=True, text=True, timeout=1200,
        env={**os.environ},
    )
    elapsed = time.time() - t0
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    log(f"otto build finished in {elapsed:.0f}s, rc={result.returncode}")
    if result.returncode != 0:
        log(f"stdout tail: {stdout[-500:]}")
        log(f"stderr tail: {stderr[-500:]}")
        _fail(f"otto build rc={result.returncode}")

    # === Structural checks on the new per-session layout ===
    logs_dir = repo / "otto_logs"
    _check(logs_dir.exists(), f"otto_logs/ exists at {logs_dir}")

    sessions_dir = logs_dir / "sessions"
    _check(sessions_dir.exists(), "otto_logs/sessions/ exists")

    # Exactly one session dir
    sessions = [p for p in sessions_dir.iterdir() if p.is_dir()]
    _check(len(sessions) >= 1, f"at least one session dir (found {len(sessions)})")
    session = sessions[0]
    log(f"  session_id: {session.name}")

    # latest pointer
    latest = logs_dir / "latest"
    _check(latest.exists(), "otto_logs/latest pointer exists")
    try:
        resolved = latest.resolve()
        _check(resolved.name == session.name, f"latest → {resolved.name} (== {session.name})")
    except OSError as exc:
        _fail(f"could not resolve latest pointer: {exc}")

    # summary.json
    summary_path = session / "summary.json"
    _check(summary_path.exists(), "summary.json exists")
    summary = json.loads(summary_path.read_text())
    for key in ("run_id", "command", "verdict", "cost_usd", "duration_s", "stories_passed"):
        _check(key in summary, f"summary.json has '{key}'")
    _check(summary["cost_usd"] > 0, f"cost_usd > 0 (got {summary['cost_usd']})")
    log(f"  verdict={summary['verdict']} cost=${summary['cost_usd']:.3f} duration={summary['duration_s']:.1f}s")

    # manifest.json (per-run, consumed by queue/merge)
    manifest_path = session / "manifest.json"
    _check(manifest_path.exists(), "manifest.json exists in session dir")
    manifest = json.loads(manifest_path.read_text())
    _check(manifest["command"] == "build", "manifest.command == 'build'")
    _check(manifest["run_id"] == session.name, "manifest.run_id == session_id")

    # build/ subdir with narrative.log + messages.jsonl
    build_dir = session / "build"
    _check(build_dir.exists(), "session/build/ exists")
    narrative = build_dir / "narrative.log"
    messages = build_dir / "messages.jsonl"
    _check(narrative.exists(), "build/narrative.log exists")
    _check(messages.exists(), "build/messages.jsonl exists")
    _check(narrative.stat().st_size > 0, "narrative.log is non-empty")
    _check(messages.stat().st_size > 0, "messages.jsonl is non-empty")

    # narrative.log structure — must show at least a phase banner + story marker
    narrative_text = narrative.read_text()
    has_phase = "BUILD" in narrative_text or "CERTIFY" in narrative_text
    has_story = "STORY_RESULT" in narrative_text or "stories" in narrative_text.lower()
    _check(has_phase, "narrative.log has BUILD/CERTIFY phase banner")
    _check(has_story, "narrative.log mentions stories/STORY_RESULT")
    log(f"  narrative.log: {narrative.stat().st_size} bytes, "
        f"{narrative_text.count(chr(10))} lines")

    # messages.jsonl is parseable JSONL
    jsonl_lines = [ln for ln in messages.read_text().splitlines() if ln.strip()]
    for i, line in enumerate(jsonl_lines[:5]):
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            _fail(f"messages.jsonl line {i} not valid JSON: {exc}")
    _check(True, f"messages.jsonl: {len(jsonl_lines)} events, first 5 parseable")

    # certify/ subdir (we ran --fast, cert runs by default)
    certify_dir = session / "certify"
    _check(certify_dir.exists(), "session/certify/ exists")
    pow_json = certify_dir / "proof-of-work.json"
    _check(pow_json.exists(), "certify/proof-of-work.json exists")

    # cross-sessions history
    history = logs_dir / "cross-sessions" / "history.jsonl"
    _check(history.exists(), "cross-sessions/history.jsonl exists")
    entries = [ln for ln in history.read_text().splitlines() if ln.strip()]
    _check(len(entries) >= 1, f"history has >= 1 entry (got {len(entries)})")

    # otto replay works on this session
    log("  testing `otto replay` regenerates narrative.log")
    replay_r = subprocess.run(
        [str(OTTO_BIN), "replay", session.name],
        cwd=repo, capture_output=True, text=True, timeout=60,
        env={**os.environ},
    )
    _check(replay_r.returncode == 0,
           f"otto replay rc={replay_r.returncode} (stderr={replay_r.stderr[-200:]!r})")

    log("")
    log(f"PHASE 1 PASSED — single-task layout is correct (${summary['cost_usd']:.3f}, {elapsed:.0f}s)")
    log("")
    return float(summary["cost_usd"]), elapsed


# --------------------------------------------------------------------- phase 2

def phase2_parallel_merge() -> float:
    log("=" * 60)
    log("PHASE 2: parallel queue + merge — validate isolation + merge integration")
    log("=" * 60)
    repo = make_repo("e2e-parallel-")
    log(f"repo: {repo}")

    t0 = time.time()
    log("enqueue 2 non-conflicting builds")
    otto_run(repo, "queue", "build", INTENT_A, "--as", "add", "--", "--fast")
    otto_run(repo, "queue", "build", INTENT_B, "--as", "mul", "--", "--fast")

    log("start watcher (concurrent=2)")
    w = start_watcher(repo, concurrent=2)
    try:
        statuses = wait_for_all_done(repo, timeout=900, interval=20)
        log(f"watcher done: {statuses}")
    finally:
        stop_watcher(w)

    # === Check: each queued task has its own session dir in its worktree ===
    # Each task runs in .worktrees/<task-slug>/, with its own otto_logs/.
    worktrees_dir = repo / ".worktrees"
    _check(worktrees_dir.exists(), ".worktrees/ exists")
    wts = [p for p in worktrees_dir.iterdir() if p.is_dir()]
    _check(len(wts) >= 2, f"at least 2 worktrees (got {len(wts)})")

    task_sessions: list[tuple[str, Path]] = []
    for wt in wts:
        wt_sessions = wt / "otto_logs" / "sessions"
        if not wt_sessions.exists():
            continue
        for s in wt_sessions.iterdir():
            if s.is_dir():
                task_sessions.append((wt.name, s))
                break
    _check(len(task_sessions) >= 2,
           f"each worktree has its own session dir (got {len(task_sessions)})")
    # sanity: the two session ids must differ
    ids = {s.name for _, s in task_sessions}
    _check(len(ids) >= 2, f"session ids differ across tasks (got {ids})")
    log(f"  per-task sessions: {[(w, s.name) for w, s in task_sessions]}")

    # Each task's narrative.log must be non-empty.
    # NOTE: in queue mode, manifest.json lives at otto_logs/queue/<task-id>/
    # on the MAIN repo (deterministic for watcher discovery), NOT in the
    # per-task worktree session dir. We assert the queue path further down.
    for wt_name, session in task_sessions:
        narr = session / "build" / "narrative.log"
        _check(narr.exists() and narr.stat().st_size > 0,
               f"{wt_name}: narrative.log non-empty")

    # === Check queue task manifests are discoverable at the deterministic path ===
    queue_manifests_dir = repo / "otto_logs" / "queue"
    _check(queue_manifests_dir.exists(),
           "otto_logs/queue/ exists on main repo (queue manifest anchor)")
    queue_manifests = list(queue_manifests_dir.glob("*/manifest.json"))
    _check(len(queue_manifests) >= 2,
           f"at least 2 queue manifests at otto_logs/queue/<task-id>/manifest.json "
           f"(got {len(queue_manifests)})")

    # === Identify done task IDs and run merge ===
    from otto.queue.schema import load_queue
    state = queue_state(repo)
    done_task_ids: list[str] = []
    for t in load_queue(repo):
        tstate = state.get("tasks", {}).get(t.id, {})
        if tstate.get("status") == "done" and t.branch:
            done_task_ids.append(t.id)
    _check(len(done_task_ids) >= 2,
           f"at least 2 done task IDs for merge (got {done_task_ids})")

    log(f"merging {len(done_task_ids)} branches with --cleanup-on-success: {done_task_ids}")
    merge_t0 = time.time()
    merge_r = subprocess.run(
        [str(OTTO_BIN), "merge", *done_task_ids, "--no-certify", "--cleanup-on-success"],
        cwd=repo, capture_output=True, text=True, timeout=900,
        env={**os.environ},
    )
    merge_elapsed = time.time() - merge_t0
    log(f"merge rc={merge_r.returncode} in {merge_elapsed:.0f}s")
    merge_out = (merge_r.stdout or "") + (merge_r.stderr or "")
    if merge_r.returncode != 0:
        log(f"merge output tail: {merge_out[-500:]}")
        _fail(f"otto merge rc={merge_r.returncode}")

    # Merge should have created otto_logs/merge/<merge-id>/state.json
    merge_runs = sorted((repo / "otto_logs" / "merge").glob("merge-*/state.json"))
    _check(len(merge_runs) >= 1, f"at least one merge state.json (got {len(merge_runs)})")

    merge_state = json.loads(merge_runs[-1].read_text())
    outcomes = merge_state.get("outcomes", [])
    _check(len(outcomes) >= 2, f"merge recorded >= 2 branch outcomes (got {len(outcomes)})")
    succeeded = [o for o in outcomes if o.get("status") in ("merged", "merged_with_markers")]
    _check(len(succeeded) >= 2, f"at least 2 branches merged successfully (got {len(succeeded)})")

    # Verify both commits actually landed on the main branch HEAD
    log_out = subprocess.run(
        ["git", "log", "--oneline", "-20"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    _check("merge" in log_out.lower() or "add" in log_out.lower() or "mul" in log_out.lower(),
           "git log contains evidence of merged work")

    # === GRADUATION CHECKS (--cleanup-on-success path) ===
    # After cleanup-on-success, the .worktrees/ should be empty AND each
    # task's session should be relocated to main_repo/otto_logs/sessions/<id>/
    # with merge_commit_sha + merged_at fields amended.
    log("verifying graduation outcome")
    remaining_wts = [p for p in (repo / ".worktrees").iterdir() if p.is_dir()]
    _check(remaining_wts == [],
           f".worktrees/ empty after cleanup-on-success (got {[p.name for p in remaining_wts]})")

    main_sessions_dir = repo / "otto_logs" / "sessions"
    _check(main_sessions_dir.exists(), "main repo otto_logs/sessions/ exists post-graduation")
    main_sessions = [p for p in main_sessions_dir.iterdir() if p.is_dir()]
    _check(len(main_sessions) >= 2,
           f"at least 2 graduated sessions on main (got {len(main_sessions)})")

    expected_session_ids = {s.name for _, s in task_sessions}
    found_session_ids = {p.name for p in main_sessions}
    missing = expected_session_ids - found_session_ids
    _check(not missing,
           f"all task session ids present on main "
           f"(expected={expected_session_ids}, found={found_session_ids})")

    # Pick one graduated session and verify its summary.json was amended
    sample = next(iter(main_sessions))
    summary = json.loads((sample / "summary.json").read_text())
    _check("merge_commit_sha" in summary,
           f"graduated summary.json has merge_commit_sha (sample={sample.name})")
    _check("merged_at" in summary,
           f"graduated summary.json has merged_at (sample={sample.name})")
    _check(summary.get("queue_task_id") in {"add", "mul"},
           f"graduated summary.json has queue_task_id (got {summary.get('queue_task_id')!r})")
    log(f"  sample graduated session: {sample.name} "
        f"queue_task_id={summary['queue_task_id']} "
        f"merge_commit_sha={summary['merge_commit_sha'][:10]}")

    # Verify graduated session retains build/narrative.log + certify/proof-of-work.json
    _check((sample / "build" / "narrative.log").exists(),
           "graduated build/narrative.log preserved")
    _check((sample / "certify" / "proof-of-work.json").exists(),
           "graduated certify/proof-of-work.json preserved")

    # Verify queue manifest now points at graduated paths (not deleted worktree)
    queue_manifest = json.loads(
        (repo / "otto_logs" / "queue" / summary["queue_task_id"] / "manifest.json").read_text()
    )
    qm_pow = Path(queue_manifest.get("proof_of_work_path", ""))
    _check(qm_pow.exists() and "/.worktrees/" not in str(qm_pow),
           f"queue manifest proof_of_work_path points at live graduated file (got {qm_pow})")

    total_elapsed = time.time() - t0
    log("")
    log(f"PHASE 2 PASSED — parallel + merge + graduation works end-to-end ({total_elapsed:.0f}s total)")
    log("")
    return total_elapsed


# --------------------------------------------------------------------- main

def main() -> int:
    log(f"E2E merge sanity starting — using {OTTO_BIN}")
    try:
        require_real_cost_opt_in("E2E merge sanity")
    except SystemExit as exc:
        return int(exc.code or 2)
    t0 = time.time()
    try:
        p1_cost, p1_elapsed = phase1_single_task()
        p2_elapsed = phase2_parallel_merge()
        total = time.time() - t0
        log("=" * 60)
        log(f"ALL PHASES PASSED in {total:.0f}s")
        log(f"  phase 1 (single-task): ${p1_cost:.3f}, {p1_elapsed:.0f}s")
        log(f"  phase 2 (parallel+merge): {p2_elapsed:.0f}s")
        log("=" * 60)
        return 0
    except SystemExit as exc:
        return int(exc.code or 1)
    except Exception as exc:
        log(f"E2E crashed: {exc}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
