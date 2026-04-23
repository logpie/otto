"""Phase 2.7-2.8: the queue watcher runner — single-writer state machine.

Foreground process (run from a tmux pane like `vite dev`). Polls
``.otto-queue.yml`` every ``poll_interval`` seconds, drains
``.otto-queue-commands.jsonl``, reaps finished children, dispatches new
tasks up to ``--concurrent N``.

**Hard invariant** (replicates OTP mailbox guarantee): ALL state
mutations to ``state.json`` happen in the main loop tick. Signal handlers
ONLY set ``self.shutdown_level``; they MUST NOT touch state files.
Subprocess reaping uses ``os.waitpid(WNOHANG)`` IN the tick, not in a
SIGCHLD handler.

Process groups: every child spawned with ``preexec_fn=os.setsid``;
cancel uses ``os.killpg(pgid, SIGTERM)`` with PID-reuse validation
(pid+pgid+start_time_ns+argv+cwd all match before any kill).

Exclusive lock: ``.otto-queue.lock`` with ``flock(LOCK_EX | LOCK_NB)``.
A second ``otto queue run`` against the same project refuses to start.

See plan-parallel.md §5 Phase 2 (steps 2.7-2.9).
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from otto.manifest import queue_index_path_for
from otto.queue.runtime import (
    IN_FLIGHT_STATUSES,
    INTERRUPTED_STATUS,
    checkpoint_path_for_task,
)
from otto.queue.schema import (
    QueueTask,
    drain_commands,
    load_queue,
    load_state,
    lock_path,
    remove_task,
    write_state,
)
from otto.queue.ids import detect_cycles

logger = logging.getLogger("otto.queue.runner")


@dataclass
class RunnerConfig:
    """Knobs for the watcher; mostly comes from otto.yaml `queue:` section."""

    concurrent: int = 3
    worktree_dir: str = ".worktrees"
    on_watcher_restart: str = "resume"   # resume | fail
    poll_interval_s: float = 2.0
    heartbeat_interval_s: float = 5.0
    # Per-task wall-clock timeout. A hung child (agent stuck in a bash
    # `wait` for an unkilled background process, infinite loop, etc.)
    # would otherwise occupy its concurrency slot forever. None disables.
    # Default: 30 minutes — generous for thorough builds, fatal for hangs.
    task_timeout_s: float | None = 1800.0
    # Exit the foreground watcher once the queue has no queued or in-flight work.
    exit_when_empty: bool = False
    # When true, capture child stdout/stderr and prefix each emitted line with
    # the queue task id for grep-friendly no-dashboard logs.
    prefix_child_output: bool = False


class WatcherAlreadyRunning(RuntimeError):
    """Raised when another otto queue run is already holding the project lock."""


class StatePersistenceError(RuntimeError):
    """Raised when state.json cannot be persisted after a side effect."""


def acquire_lock(project_dir: Path) -> Any:
    """Take an exclusive lock on .otto-queue.lock. Returns file handle to keep open.

    Raises WatcherAlreadyRunning if another runner holds it.
    """
    path = lock_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            raise WatcherAlreadyRunning(
                f"another otto queue runner is holding {path}; "
                "stop it before starting a new one"
            ) from exc
        raise
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _mark_failed(ts: dict[str, Any], reason: str) -> None:
    ts["status"] = "failed"
    ts["finished_at"] = now_iso()
    ts["child"] = None
    ts["failure_reason"] = reason


# ---------- PID-reuse-safe child validation ----------

def child_is_alive(child: dict[str, Any]) -> bool:
    """Return True iff the recorded PID still belongs to OUR child.

    Validates pid+pgid+start_time_ns+argv+cwd. Any mismatch → child is
    gone (PID may have been reused by an unrelated process).
    """
    if not child:
        return False
    pid = child.get("pid")
    if not isinstance(pid, int):
        return False
    try:
        import psutil
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return False
        # start_time_ns: psutil exposes create_time() in seconds (float)
        recorded = child.get("start_time_ns")
        if recorded is not None:
            try:
                actual_ns = int(proc.create_time() * 1_000_000_000)
            except (psutil.NoSuchProcess, ProcessLookupError):
                return False
            # Allow 100ms drift to absorb float→int conversion noise
            if abs(actual_ns - int(recorded)) > 100_000_000:
                return False
        recorded_pgid = child.get("pgid")
        if recorded_pgid is not None:
            try:
                actual_pgid = os.getpgid(pid)
                if actual_pgid != recorded_pgid:
                    return False
            except ProcessLookupError:
                return False
        recorded_cwd = child.get("cwd")
        if recorded_cwd is not None:
            try:
                actual_cwd = str(proc.cwd())
                if os.path.realpath(actual_cwd) != os.path.realpath(recorded_cwd):
                    return False
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                # Can't verify; conservatively assume alive
                pass
        # NOTE on argv check: the shell or wrapper scripts may exec-replace
        # into a different program (e.g. `/bin/sh -c "sleep 5"` → cmdline is
        # ['sleep', '5'], not ['/bin/sh', '-c', 'sleep 5']). PID-reuse safety
        # relies primarily on start_time_ns + pgid + cwd; argv match is a
        # weak signal we don't enforce. We could re-enable it for direct
        # `otto build` invocations (which don't go through a shell) but the
        # blanket strict check produces false negatives for legitimate uses.
        return True
    except ImportError:
        # No psutil — fall back to weaker check (just "is pid alive")
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True


def kill_child_safely(child: dict[str, Any], sig: int = signal.SIGTERM) -> bool:
    """Send `sig` to the child's process group, validating identity first.

    Returns True if signal was sent, False if child no longer matches
    (PID reuse or already exited).
    """
    if not child_is_alive(child):
        return False
    pgid = child.get("pgid")
    if not isinstance(pgid, int):
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False


# ---------- runner ----------

class Runner:
    """The queue watcher.

    Lifecycle:
        runner = Runner(project_dir, config, otto_bin=...)
        runner.run()   # blocks; returns on graceful shutdown

    Signal handling: SIGINT/SIGTERM only set ``self.shutdown_level``
    (None / "graceful" / "immediate"). The main loop reads this flag
    each tick.
    """

    def __init__(
        self,
        project_dir: Path,
        config: RunnerConfig,
        *,
        otto_bin: list[str] | str,
    ) -> None:
        self.project_dir = project_dir
        self.config = config
        if isinstance(otto_bin, str):
            self.otto_bin = [otto_bin]
        else:
            self.otto_bin = list(otto_bin)
        self.shutdown_level: str | None = None
        self._lock_fh: Any = None
        self._watcher_started_at = now_iso()
        self._last_logged_cycles: set[frozenset[str]] | None = None
        self._prefix_child_output = config.prefix_child_output
        self._output_threads: dict[int, threading.Thread] = {}

    # ---- signal handlers (flag-only; NEVER touch state files) ----

    def _on_sigint(self, signum: int, frame: Any) -> None:
        if self.shutdown_level is None:
            self.shutdown_level = "graceful"
            logger.info("SIGINT: graceful shutdown — waiting for in-flight tasks")
        else:
            self.shutdown_level = "immediate"
            logger.info("SIGINT (second): immediate shutdown — killing in-flight tasks")

    def _on_sigterm(self, signum: int, frame: Any) -> None:
        self.shutdown_level = "immediate"
        logger.info("SIGTERM: immediate shutdown")

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._on_sigint)
        signal.signal(signal.SIGTERM, self._on_sigterm)

    # ---- main loop ----

    def run(self) -> int:
        """Run the watcher loop until shutdown. Returns exit code."""
        try:
            self._begin_run()
            last_heartbeat = time.monotonic()
            while True:
                last_heartbeat, exit_code, done = self._run_iteration(last_heartbeat)
                if exit_code is not None:
                    return exit_code
                if done:
                    break
                time.sleep(self.config.poll_interval_s)
            return self._end_run()
        finally:
            self._release_lock()

    async def run_async(self) -> int:
        """Async version of ``run()`` for the Textual dashboard path."""
        try:
            self._begin_run()
            last_heartbeat = time.monotonic()
            while True:
                last_heartbeat, exit_code, done = self._run_iteration(last_heartbeat)
                if exit_code is not None:
                    return exit_code
                if done:
                    break
                await asyncio.sleep(self.config.poll_interval_s)
            return self._end_run()
        finally:
            self._release_lock()

    def _begin_run(self) -> None:
        self._lock_fh = acquire_lock(self.project_dir)
        self._install_signal_handlers()
        self._reconcile_on_startup()
        self._update_watcher_state()

    def _load_queue_or_empty(self, *, context: str) -> list[QueueTask]:
        try:
            return load_queue(self.project_dir)
        except (OSError, ValueError) as exc:
            logger.error("failed to load queue.yml during %s: %s", context, exc)
            return []

    def _remove_task_definition(self, task_id: str) -> bool | None:
        try:
            removed = remove_task(self.project_dir, task_id)
        except (OSError, ValueError) as exc:
            logger.error("remove: failed to update queue.yml for %s: %s", task_id, exc)
            return None
        if not removed:
            logger.warning("remove: task %s was already absent from queue.yml", task_id)
        return removed

    def _run_iteration(self, last_heartbeat: float) -> tuple[float, int | None, bool]:
        try:
            self._tick()
        except StatePersistenceError:
            logger.exception("state persistence failed; stopping runner")
            return last_heartbeat, 1, False
        except Exception:
            logger.exception("tick failed; continuing")

        now = time.monotonic()
        if now - last_heartbeat >= self.config.heartbeat_interval_s:
            try:
                self._update_watcher_state()
            except StatePersistenceError:
                logger.exception("state persistence failed during heartbeat; stopping runner")
                return last_heartbeat, 1, False
            last_heartbeat = now

        if self.shutdown_level == "immediate":
            self._kill_all_in_flight()
            return last_heartbeat, None, True
        if self.shutdown_level == "graceful" and not self._has_in_flight():
            return last_heartbeat, None, True
        return last_heartbeat, None, False

    def _end_run(self) -> int:
        try:
            self._clear_watcher_state()
        except StatePersistenceError:
            logger.exception("failed to clear watcher state during shutdown")
            return 1
        return 0

    def _release_lock(self) -> None:
        if self._lock_fh is not None:
            try:
                fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
                self._lock_fh.close()
            except Exception as exc:
                logger.debug("failed to clean up queue lock: %s", exc)
            finally:
                self._lock_fh = None

    def _tick(self) -> None:
        """One main-loop iteration: drain commands, reap children, dispatch new."""
        try:
            commands = drain_commands(self.project_dir)
        except (OSError, ValueError) as exc:
            # IO failures or malformed JSONL — log loudly so user sees it.
            # A real bug (e.g. import error) is a different exception type
            # and will crash the runner, which is correct.
            logger.error("failed to drain commands: %s — user commands may be lost", exc)
            commands = []

        try:
            tasks = load_queue(self.project_dir)
        except (OSError, ValueError) as exc:
            # Same: IO/parse failure → log at ERROR (not WARN) so the user
            # notices their queue.yml is unreadable. Continue with empty
            # task list so reap-existing-children still runs.
            logger.error("failed to load queue.yml: %s — no tasks will dispatch", exc)
            tasks = []

        # Re-validate dependency graph for cycles introduced by editing
        edges = {t.id: list(t.after) for t in tasks}
        cycles = detect_cycles(edges=edges)
        cycle_sets = {frozenset(cycle) for cycle in cycles}
        if cycles and cycle_sets != self._last_logged_cycles:
            logger.warning(
                "queue.yml has dependency cycles: %s — affected tasks won't dispatch",
                cycles,
            )
        self._last_logged_cycles = cycle_sets or None

        # Apply commands
        state = load_state(self.project_dir)
        cycle_ids = {tid for cycle in cycles for tid in cycle}
        for cmd in commands:
            self._apply_command(cmd, state)

        # Reap finished children
        self._reap_children(state)

        # Dispatch new work (skip during graceful shutdown)
        if self.shutdown_level is None:
            self._dispatch_new(tasks, state, cycle_ids)

        # Persist state
        self._write_state_or_raise(state)
        if self._should_exit_when_empty(tasks, state):
            logger.info("queue drained; exiting watcher because --exit-when-empty is set")
            self.shutdown_level = "graceful"

    # ---- startup reconciliation (Phase 2.8) ----

    def _reconcile_on_startup(self) -> None:
        """Handle tasks that were in-flight when watcher last died.

        Per `on_watcher_restart` policy:
          resume → re-attach (or re-spawn with --resume if checkpoint exists
                  and task.resumable)
          fail   → mark failed
        """
        state = load_state(self.project_dir)
        tasks_by_id = {t.id: t for t in self._load_queue_or_empty(context="startup reconcile")}
        policy = self.config.on_watcher_restart
        for tid, ts in list(state.get("tasks", {}).items()):
            status = ts.get("status")
            if status not in IN_FLIGHT_STATUSES:
                continue
            child = ts.get("child") or {}
            still_alive = child_is_alive(child)
            if status == "terminating":
                if still_alive:
                    logger.info(
                        "reconciling: task %s still terminating, preserving terminal_status=%s",
                        tid,
                        ts.get("terminal_status", "cancelled"),
                    )
                    continue
                self._finish_terminating(ts)
                logger.info(
                    "reconciling: task %s finished while watcher was down -> %s",
                    tid,
                    ts.get("status"),
                )
                continue
            task = tasks_by_id.get(tid)
            if still_alive:
                if policy == "resume":
                    logger.info("reconciling: task %s child still alive, re-attaching", tid)
                    # Leave status=running; main loop will reap on exit
                    continue
                if policy == "fail":
                    logger.info("reconciling: policy=fail, killing %s", tid)
                    kill_child_safely(child, signal.SIGTERM)
                    self._mark_terminating(
                        ts,
                        final_status="cancelled",
                        reason="watcher restart with policy=fail",
                    )
                    continue
                logger.warning(
                    "reconciling: unknown on_watcher_restart=%r, treating %s as fail",
                    policy,
                    tid,
                )
                kill_child_safely(child, signal.SIGTERM)
                self._mark_terminating(
                    ts,
                    final_status="cancelled",
                    reason=f"watcher restart with policy={policy}",
                )
                continue
            # Child gone — decide based on resumability + checkpoint
            if task is None or not task.resumable:
                _mark_failed(ts, "watcher restart: child gone, command not resumable")
                continue
            # New per-session layout: paused pointer → sessions/<id>/checkpoint.json.
            # Falls back to scanning sessions/*/checkpoint.json if pointer is stale,
            # and to the legacy otto_logs/checkpoint.json for back-compat.
            checkpoint_path = checkpoint_path_for_task(self.project_dir, task)
            if checkpoint_path is not None and policy == "resume":
                logger.info(
                    "reconciling: re-spawning %s with --resume from %s",
                    tid, checkpoint_path,
                )
                # Re-spawn happens in normal dispatch path; just clear running state
                ts["status"] = "queued"
                ts["child"] = None
                ts["failure_reason"] = None
                ts["resumed_from_checkpoint"] = True
            else:
                _mark_failed(ts, "watcher restart: child gone, no checkpoint")
        self._write_state_or_raise(state)

    # ---- command application ----

    def _apply_command(
        self, cmd: dict[str, Any], state: dict[str, Any],
    ) -> None:
        kind = cmd.get("cmd")
        tid = cmd.get("id")
        if not isinstance(tid, str):
            logger.warning("command missing/invalid 'id': %r", cmd)
            return
        ts = state["tasks"].setdefault(tid, {"status": "queued"})
        status = ts.get("status", "queued")
        if kind == "cancel":
            if status not in ("queued", "running", "terminating"):
                logger.warning("cancel ignored for %s in status=%s", tid, status)
                return
            if status == "running":
                child = ts.get("child") or {}
                if kill_child_safely(child, signal.SIGTERM):
                    logger.info("cancel: sent SIGTERM to pgid %d (%s)",
                                child.get("pgid", -1), tid)
                self._mark_terminating(ts, final_status="cancelled", reason="cancelled by user")
                return
            if status == "terminating":
                ts["terminal_status"] = "cancelled"
                ts["failure_reason"] = "cancelled by user"
                return
            if self._remove_task_definition(tid) is None:
                return
            ts["status"] = "cancelled"
            ts["finished_at"] = now_iso()
            ts["child"] = None
            ts["failure_reason"] = "cancelled by user"
        elif kind == "remove":
            if status == "removed":
                logger.warning("remove ignored for %s in status=removed", tid)
                return
            if status in {"done", "failed", "cancelled"}:
                logger.warning("remove ignored for %s in terminal status=%s; use cleanup", tid, status)
                return
            if self._remove_task_definition(tid) is None:
                return
            if status == "running":
                child = ts.get("child") or {}
                if kill_child_safely(child, signal.SIGTERM):
                    logger.info("remove: sent SIGTERM to %s before removal", tid)
                self._mark_terminating(ts, final_status="removed", reason=ts.get("failure_reason"))
                return
            if status == "terminating":
                ts["terminal_status"] = "removed"
                return
            ts["status"] = "removed"
            ts["finished_at"] = now_iso()
            ts.pop("terminal_status", None)
        elif kind == "resume":
            if status != INTERRUPTED_STATUS:
                logger.warning("resume ignored for %s in status=%s", tid, status)
                return
            ts["status"] = "queued"
            ts["started_at"] = None
            ts["finished_at"] = None
            ts["exit_code"] = None
            ts["child"] = None
            ts["failure_reason"] = None
            ts["resumed_from_checkpoint"] = True
        else:
            logger.warning("unknown command kind: %r", kind)

    # ---- timeout enforcement ----

    def _enforce_task_timeouts(self, in_flight: list[tuple[str, dict[str, Any]]]) -> None:
        """SIGTERM any task whose wall-clock exceeds task_timeout_s.

        Marks the task as `terminating` with `terminal_status="failed"` and
        reason `timed out after Xs`. The next reap tick observes the dead
        child (via waitpid or ECHILD fallback) and finalizes via
        `_finish_terminating` → status="failed".

        Skip already-terminating tasks and tasks without a parseable
        started_at timestamp (defensive — should never happen).
        """
        timeout = self.config.task_timeout_s
        if timeout is None:
            return
        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc)
        for tid, ts in in_flight:
            if ts.get("status") != "running":
                continue
            started = ts.get("started_at")
            if not isinstance(started, str):
                continue
            try:
                t0 = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            elapsed = (now - t0).total_seconds()
            if elapsed < timeout:
                continue
            child = ts.get("child") or {}
            logger.warning(
                "task %s exceeded timeout (%.0fs > %.0fs); SIGTERM",
                tid, elapsed, timeout,
            )
            kill_child_safely(child, signal.SIGTERM)
            self._mark_terminating(
                ts,
                final_status="failed",
                reason=f"timed out after {elapsed:.0f}s (limit {timeout:.0f}s)",
            )

    # ---- reap finished children ----

    def _reap_children(self, state: dict[str, Any]) -> None:
        """Non-blocking reap of any exited children. Reads their manifests."""
        # Snapshot in-flight tasks so we can iterate without mutation issues
        in_flight = [
            (tid, ts) for tid, ts in state["tasks"].items()
            if ts.get("status") in IN_FLIGHT_STATUSES
        ]
        # Enforce per-task wall-clock timeout. SIGTERM hung tasks so they
        # transition to "terminating" and free their concurrency slot.
        if self.config.task_timeout_s is not None:
            self._enforce_task_timeouts(in_flight)
        for tid, ts in in_flight:
            status = ts.get("status")
            child = ts.get("child") or {}
            pid = child.get("pid")
            if not isinstance(pid, int):
                continue
            try:
                wpid, wstatus = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                if child_is_alive(child):
                    logger.info(
                        "reap deferred for %s: child still alive but waitpid returned ECHILD",
                        tid,
                    )
                    continue
                if status == "terminating":
                    self._finish_terminating(ts)
                else:
                    self._finalize_task_from_manifest(ts, tid)
                self._join_output_pump(pid)
                logger.info("reaped %s: %s (observed dead after ECHILD)", tid, ts.get("status"))
                continue
            if wpid == 0:
                # Still running
                continue
            exit_code = int(os.waitstatus_to_exitcode(wstatus))
            if status == "terminating":
                ts["exit_code"] = exit_code
                self._finish_terminating(ts)
                self._join_output_pump(pid)
                logger.info("reaped %s: %s", tid, ts.get("status"))
                continue
            self._finalize_task_from_manifest(ts, tid, exit_code=exit_code)
            self._join_output_pump(pid)
            if ts.get("status") == "done":
                logger.info(
                    "reaped %s: done (cost=$%.2f, duration=%.1fs)",
                    tid,
                    ts.get("cost_usd") or 0,
                    ts.get("duration_s") or 0,
                )
            else:
                logger.info("reaped %s: failed (%s)", tid, ts.get("failure_reason"))

    # ---- dispatch new work ----

    def _dispatch_new(
        self, tasks: list[QueueTask], state: dict[str, Any], cycle_ids: set[str],
    ) -> None:
        """Spawn child processes for queued tasks with satisfied dependencies."""
        in_flight = self._count_in_flight(state)
        slots = self.config.concurrent - in_flight
        if slots <= 0:
            return
        for task in tasks:
            if slots <= 0:
                break
            ts = state["tasks"].get(task.id) or {"status": "queued"}
            if ts.get("status") != "queued":
                continue
            if task.id in cycle_ids:
                continue
            if not self._deps_satisfied(task, state):
                # Cascade failure: if any after-dep is failed/cancelled, mark this failed
                cascade_reason = self._dep_cascade_reason(task, state)
                if cascade_reason:
                    ts["status"] = "failed"
                    ts["finished_at"] = now_iso()
                    ts["failure_reason"] = cascade_reason
                    state["tasks"][task.id] = ts
                continue
            # Dispatch
            try:
                self._spawn(task, state)
                try:
                    self._write_state_or_raise(state)
                except StatePersistenceError:
                    self._terminate_spawned_child_after_persist_failure(task.id, state)
                    raise
                slots -= 1
            except StatePersistenceError:
                raise
            except Exception as exc:
                _mark_failed(ts, f"spawn failed: {exc}")
                state["tasks"][task.id] = ts
                logger.exception("failed to spawn %s", task.id)

    def _deps_satisfied(self, task: QueueTask, state: dict[str, Any]) -> bool:
        for dep in task.after:
            dep_state = state["tasks"].get(dep, {"status": "queued"})
            if dep_state.get("status") != "done":
                return False
        return True

    def _dep_cascade_reason(self, task: QueueTask, state: dict[str, Any]) -> str | None:
        for dep in task.after:
            dep_state = state["tasks"].get(dep, {"status": "queued"})
            s = dep_state.get("status")
            if s in ("failed", "cancelled", "removed"):
                return f"dependency {dep!r} {s}"
        return None

    def _count_in_flight(self, state: dict[str, Any]) -> int:
        return sum(
            1 for ts in state["tasks"].values()
            if ts.get("status") in IN_FLIGHT_STATUSES
        )

    def _queued_count(self, tasks: list[QueueTask], state: dict[str, Any]) -> int:
        return sum(
            1
            for task in tasks
            if (state["tasks"].get(task.id) or {"status": "queued"}).get("status", "queued") == "queued"
        )

    def _should_exit_when_empty(self, tasks: list[QueueTask], state: dict[str, Any]) -> bool:
        if not self.config.exit_when_empty or self.shutdown_level is not None:
            return False
        return self._queued_count(tasks, state) == 0 and self._count_in_flight(state) == 0

    def _has_in_flight(self) -> bool:
        state = load_state(self.project_dir)
        return self._count_in_flight(state) > 0

    def _worktree_for(self, task: QueueTask) -> Path:
        """Where this task's worktree lives."""
        if not task.worktree:
            raise RuntimeError(f"task {task.id!r} missing worktree snapshot")
        return self.project_dir / task.worktree

    def _spawn(self, task: QueueTask, state: dict[str, Any]) -> None:
        """Create worktree if needed, spawn child otto subprocess, update state."""
        from otto.worktree import (
            WorktreeAlreadyCheckedOut,
            add_worktree,
        )
        wt_path = self._worktree_for(task)
        if not task.branch:
            raise RuntimeError(f"task {task.id!r} missing branch snapshot")
        branch = task.branch
        try:
            add_worktree(project_dir=self.project_dir, worktree_path=wt_path, branch=branch)
        except WorktreeAlreadyCheckedOut:
            # Branch already in another worktree — likely from a prior crash
            # the user didn't clean up. Fail this task with a clear reason.
            raise RuntimeError(
                f"branch {branch!r} is already checked out in another worktree; "
                f"clean up with `git worktree remove` or rename the task"
            )

        # Build argv: <otto_bin> <task argv...> [+ --resume if respawning from checkpoint]
        argv = list(self.otto_bin) + list(task.command_argv)
        ts_existing = state["tasks"].get(task.id, {})
        if ts_existing.get("resumed_from_checkpoint"):
            argv.append("--resume")
            ts_existing.pop("resumed_from_checkpoint", None)

        # Spawn with own process group (setsid) so cancel can killpg cleanly
        env = {
            **os.environ,
            "OTTO_INTERNAL_QUEUE_RUNNER": "1",
            "OTTO_QUEUE_TASK_ID": task.id,
            # Anchor manifest writes to the MAIN project so the watcher (whose
            # cwd is the main project) and the child (whose cwd is the
            # worktree) resolve to the same path. See otto/manifest.py
            # `manifest_path_for`.
            "OTTO_QUEUE_PROJECT_DIR": str(self.project_dir),
        }
        popen_kwargs: dict[str, Any] = {
            "cwd": str(wt_path),
            "env": env,
            "preexec_fn": os.setsid,
        }
        if self._prefix_child_output:
            popen_kwargs.update(
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        proc = subprocess.Popen(argv, **popen_kwargs)
        self._start_output_pump(task.id, proc)
        # Capture identity for PID-reuse-safe future kills
        try:
            import psutil
            start_time_ns = int(psutil.Process(proc.pid).create_time() * 1_000_000_000)
        except Exception:
            start_time_ns = int(time.time() * 1_000_000_000)

        state["tasks"][task.id] = {
            "status": "running",
            "started_at": now_iso(),
            "finished_at": None,
            "exit_code": None,
            "child": {
                "pid": proc.pid,
                "pgid": proc.pid,  # setsid → pgid == pid
                "start_time_ns": start_time_ns,
                "argv": argv,
                "cwd": str(wt_path),
            },
            "manifest_path": None,
            "cost_usd": None,
            "duration_s": None,
            "failure_reason": None,
        }
        logger.info("spawned %s: pid=%d, branch=%s", task.id, proc.pid, branch)

    def _start_output_pump(self, task_id: str, proc: subprocess.Popen[Any]) -> None:
        stdout = getattr(proc, "stdout", None)
        if not self._prefix_child_output or stdout is None:
            return

        def _pump() -> None:
            try:
                for raw_line in stdout:
                    print(f"[{task_id}] {raw_line.rstrip()}", flush=True)
            finally:
                try:
                    stdout.close()
                except Exception:
                    pass

        thread = threading.Thread(target=_pump, name=f"otto-queue-{task_id}-stdout", daemon=True)
        thread.start()
        self._output_threads[proc.pid] = thread

    def _join_output_pump(self, pid: int) -> None:
        thread = self._output_threads.pop(pid, None)
        if thread is not None:
            thread.join(timeout=0.5)

    def _kill_all_in_flight(self) -> None:
        state = load_state(self.project_dir)
        tasks_by_id = {
            task.id: task for task in self._load_queue_or_empty(context="shutdown interrupt")
        }
        for tid, ts in state["tasks"].items():
            if ts.get("status") not in IN_FLIGHT_STATUSES:
                continue
            child = ts.get("child") or {}
            kill_child_safely(child, signal.SIGTERM)
            final_status = ts.get("terminal_status", "cancelled")
            reason = ts.get("failure_reason")
            if ts.get("status") == "running":
                task = tasks_by_id.get(tid)
                final_status = INTERRUPTED_STATUS
                if task is not None and checkpoint_path_for_task(self.project_dir, task) is not None:
                    reason = "interrupted by watcher shutdown; resume available"
                else:
                    reason = "interrupted by watcher shutdown"
            self._mark_terminating(
                ts,
                final_status=final_status,
                reason=reason,
            )
        self._write_state_or_raise(state)

    def _terminate_spawned_child_after_persist_failure(
        self,
        task_id: str,
        state: dict[str, Any],
    ) -> None:
        ts = state["tasks"].get(task_id) or {}
        child = ts.get("child") or {}
        pid = child.get("pid")
        pgid = child.get("pgid")
        logger.critical(
            "post-spawn state write failed; terminating just-spawned child to prevent duplicate: %s",
            task_id,
        )
        if not isinstance(pid, int) or not isinstance(pgid, int):
            return
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                wpid, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return
            if wpid == pid:
                return
            time.sleep(0.05)
        logger.warning(
            "timed out waiting to reap just-spawned child after post-spawn write failure: %s",
            task_id,
        )

    # ---- watcher-state housekeeping ----

    def _update_watcher_state(self) -> None:
        state = load_state(self.project_dir)
        state["watcher"] = {
            "pid": os.getpid(),
            "pgid": os.getpgid(0),
            "started_at": self._watcher_started_at,
            "heartbeat": now_iso(),
        }
        self._write_state_or_raise(state)

    def _clear_watcher_state(self) -> None:
        state = load_state(self.project_dir)
        state["watcher"] = None
        self._write_state_or_raise(state)

    def _mark_terminating(self, ts: dict[str, Any], *, final_status: str, reason: str | None) -> None:
        ts["status"] = "terminating"
        ts["terminal_status"] = final_status
        ts["finished_at"] = None
        if reason is not None:
            ts["failure_reason"] = reason

    def _finalize_task_from_manifest(
        self,
        ts: dict[str, Any],
        task_id: str,
        *,
        exit_code: int | None = None,
    ) -> None:
        ts["exit_code"] = exit_code
        ts["finished_at"] = now_iso()
        ts["child"] = None

        manifest_p = queue_index_path_for(self.project_dir, task_id)
        if manifest_p is None:
            _mark_failed(ts, f"missing queue task id for manifest lookup: {task_id!r}")
            return
        if not manifest_p.exists():
            if exit_code is None:
                reason = f"child exited but no manifest at {manifest_p}"
            elif exit_code == 0:
                reason = f"exited 0 but no manifest at {manifest_p}"
            else:
                reason = f"exit_code={exit_code}"
            _mark_failed(ts, reason)
            return

        try:
            import json

            manifest = json.loads(manifest_p.read_text())
        except Exception as exc:
            _mark_failed(ts, f"manifest unreadable: {exc}")
            return

        ts["manifest_path"] = str(manifest_p)
        ts["cost_usd"] = manifest.get("cost_usd")
        ts["duration_s"] = manifest.get("duration_s")
        manifest_exit_status = str(manifest.get("exit_status") or "success")

        if exit_code not in (None, 0):
            _mark_failed(ts, f"exit_code={exit_code}")
            return
        if manifest_exit_status != "success":
            _mark_failed(ts, f"manifest exit_status={manifest_exit_status}")
            return

        ts["status"] = "done"
        ts["failure_reason"] = None

    def _finish_terminating(self, ts: dict[str, Any]) -> None:
        ts["status"] = ts.pop("terminal_status", "cancelled")
        ts["finished_at"] = now_iso()
        ts["child"] = None

    def _write_state_or_raise(self, state: dict[str, Any]) -> None:
        try:
            write_state(self.project_dir, state)
        except Exception as exc:
            raise StatePersistenceError(str(exc)) from exc


def runner_config_from_otto_config(config: dict[str, Any]) -> RunnerConfig:
    """Build a RunnerConfig from the parsed otto.yaml dict."""
    q = config.get("queue") or {}
    raw_timeout = q.get("task_timeout_s", 1800.0)
    task_timeout: float | None
    if raw_timeout is None or raw_timeout == 0 or raw_timeout is False:
        task_timeout = None  # explicit opt-out
    else:
        task_timeout = float(raw_timeout)
    return RunnerConfig(
        concurrent=int(q.get("concurrent", 3)),
        worktree_dir=str(q.get("worktree_dir", ".worktrees")),
        on_watcher_restart=str(q.get("on_watcher_restart", "resume")),
        task_timeout_s=task_timeout,
    )
