# Resuming the Phase 2-6 implementation

If this conversation has been compacted, this file has everything needed
to pick up where we left off.

## Current state

- **Working dir**: `/Users/yuxuan/work/cc-autonomous/.worktrees/parallel-otto/` (a worktree, NOT main)
- **Branch**: `parallel-otto`
- **Never `cd` to main repo for git operations.** Stay in this worktree.
- **Last commit**: `e7f9f6f1 feat(phase-1): foundations for parallel otto`
- **Test baseline**: 253 passing
- **Plan**: `plan-parallel.md` (v5, post-codex-gate, 6 phases)
- **Review trail**: `review.md` (Phase 1 in 3 rounds, APPROVED)
- **venv**: `.venv/bin/python` (Python 3.13 with otto installed editable)
- **otto bin**: `.venv/bin/otto`

## Phase status

- ✅ Phase 1: Foundations — committed `e7f9f6f1`, 253 tests
- 🔄 Phase 2: Queue MVP — IN PROGRESS NEXT
- ⏳ Phase 3: --after dependencies
- ⏳ Phase 4: otto merge MVP
- ⏳ Phase 5: Merge mode variants
- ⏳ Phase 6: Polish (incl. otto queue cleanup command)

## Per-phase protocol (user-confirmed)

For each remaining phase:
1. Implement per `plan-parallel.md` §5
2. Add unit tests
3. Run full test suite — must pass
4. **Run `/code-health`** (which includes codex-gate as Step 7) — NOT just codex-gate alone
5. **Codex fixes Codex-found bugs** (per `~/.claude/.../memory/feedback_codex_fixes_own_bugs.md` — never let Claude fix Codex-found findings; dispatch a workspace-write Codex call instead)
6. **Run real E2E** before claiming success — exercise the phase end-to-end on a toy repo with actual `otto queue` / `otto merge` commands. Burn API tokens if needed for trustworthy verification.
7. Commit phase + append review.md trail
8. Mark task completed in TaskList, move to next

## Phase 2 starting brief (Queue MVP)

**Goal**: Foreground watcher process that schedules N parallel `otto build/improve/certify` subprocesses in worktrees, with single-writer state, process-group cancel, and restart policy.

**Key files to create** (per plan-parallel.md §5 Phase 2):
- `otto/queue/__init__.py` — module init
- `otto/queue/schema.py` — QueueTask dataclass, atomic file r/w with flock
- `otto/queue/ids.py` — slug + dedup (must reject reserved words `ls/show/rm/cancel/run`; permanent IDs across queue.yml lifetime per Codex round 3)
- `otto/queue/runner.py` — the watcher main loop
- `otto/cli_queue.py` — CLI commands `otto queue build|improve|certify|ls|show|rm|cancel|run`

**Key constraints (HARD INVARIANTS — replicates OTP mailbox guarantee):**
- Watcher is the SOLE writer of `.otto-queue-state.json` and `.otto-queue.yml`
- CLI commands write only to `.otto-queue.yml` (append-only) and `.otto-queue-commands.jsonl` (append-only)
- Signal handlers (SIGINT/SIGTERM/SIGCHLD) MUST only set flags or enqueue messages — never touch state files directly
- Subprocess reaping via `os.waitpid(-1, WNOHANG)` IN the main loop tick, not via SIGCHLD handler
- Spawn children with `preexec_fn=os.setsid`; cancel via `os.killpg(pgid, SIGTERM)`
- Validate pid+pgid+start_time_ns+argv+cwd before any kill (PID-reuse safety)
- Spawn env: `{**os.environ, "OTTO_INTERNAL_QUEUE_RUNNER": "1", "OTTO_QUEUE_TASK_ID": task.id}`
- Manifest path discovery: child writes to `<project>/otto_logs/queue/<task-id>/manifest.json` (deterministic via env var, already wired in Phase 1)
- Project-level exclusive lock: `.otto-queue.lock` with `flock(LOCK_EX|LOCK_NB)` — refuse second `otto queue run` per project
- Reject user-supplied `--resume` in `command_argv` at enqueue (watcher is sole resume injector)
- Per-task `resumable: bool` — true for build/improve, false for certify (no `--resume` path exists per cli.py:617)
- queue.yml is **strictly append-only from CLI**; removal = state.json transition (`status: removed`); IDs are permanent for queue.yml lifetime

**Wire-up notes:**
- The `slug_source` parameter on `enter_worktree_for_atomic_command` (added in Phase 1) is reusable — queue can use it to compute task worktree paths
- Existing `slugify_intent` in `otto/branching.py` already does dedup-aware slugs with hash suffix on collision/truncation

**Phase 2 verify criteria** (full list in plan-parallel.md §5 Step 2.7-2.9):
- Queue 3 simple tasks, run with `--concurrent 2`, all complete in order
- Append a 4th while running → picked up within 2s
- SIGINT once → graceful shutdown; SIGINT twice → fast kill
- Crash a child with `kill -9` → state correctly marked failed, watcher continues
- Two simultaneous `otto queue run` → second exits with "another watcher running"
- Restart watcher with on_watcher_restart=resume + checkpoint exists → re-spawn with --resume
- Restart with certify task running → marked failed (NOT respawned with --resume)
- PID-reuse safety: synthetic state edit with bad start_time → refusal to kill
- Bookkeeping not committed in queue mode (intent.md/otto.yaml stay un-committed)

**E2E for Phase 2**:
Set up a small temp git repo with intent.md, queue 2 trivial otto build tasks (with very short prompts to keep API cost minimal), run `otto queue run --concurrent 2`, verify both branches are produced, manifests in `otto_logs/queue/<id>/`, no merge conflicts on intent.md.
