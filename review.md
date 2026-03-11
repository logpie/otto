# Implementation Review Log

## Implementation Gate — 2026-03-11 — CC Autonomous v2

### Round 1 — Codex
- [CRITICAL] Task queue has no project affinity — stranded in_progress tasks on worker crash — **fixed**: added `requeue_stale_tasks()` in worker.py, `_requeue_worker_tasks()` in manager.py
- [IMPORTANT] Task completed without checking ResultMessage success — **fixed**: check `result_msg.subtype != "success"`, skip verification on non-success
- [IMPORTANT] Verification discovery inconsistent between worker and manager — **fixed**: extracted shared `_detect_verification()` into `verify_utils.py`
- [IMPORTANT] Unsafe delete/retry on in_progress tasks — **fixed**: return 409 Conflict
- [IMPORTANT] Stopped/crashed workers strand tasks — **fixed**: stop_worker requeues tasks
- [NOTE] Lock file naming — **fixed**: documented shared convention in comments

### Round 2 — Codex re-reviewed fixes
- [CRITICAL] Stale requeue uses only started_at — can requeue healthy long-running tasks — **fixed**: heartbeat mechanism (`heartbeat_at` updated every 60s, `_heartbeat_task()` async background task)
- [IMPORTANT] stop_worker requeues before process is confirmed dead — **fixed**: `terminate()` → `wait(timeout=5)` → `kill()` escalation before requeue
- [NOTE] Requeued tasks retain stale run-state fields — **fixed**: shared `_reset_task_for_requeue()` clears all fields

### Round 3 — Codex re-reviewed fixes
- [IMPORTANT] Shutdown paths don't requeue tasks — **fixed**: both FastAPI shutdown handler and signal handler use stop-and-requeue pattern
- [NOTE] stale_timeout has no lower bound relative to heartbeat interval — **fixed**: `MIN_STALE_TIMEOUT = HEARTBEAT_INTERVAL * 5` with clamping

### Round 4 — Codex re-reviewed fixes
- [IMPORTANT] Heartbeat stops during blocking `subprocess.run()` in `run_verify()` — **fixed**: `asyncio.to_thread(run_verify, ...)` keeps event loop unblocked
- APPROVED. No new issues.
