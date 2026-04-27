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

Process groups: every child spawned with ``start_new_session=True``;
cancel uses ``os.killpg(pgid, SIGTERM)`` with PID-reuse validation
(pid+pgid+start_time_ns+cwd all match before any kill).

Exclusive lock: ``.otto-queue.lock`` with ``flock(LOCK_EX | LOCK_NB)``.
A second ``otto queue run`` against the same project refuses to start.

See plan-parallel.md §5 Phase 2 (steps 2.7-2.9).
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from otto.config import DEFAULTS
from otto.manifest import queue_index_path_for
from otto import paths
from otto.queue.artifacts import preserve_queue_session_artifacts, queue_primary_log_path
from otto.token_usage import (
    TOKEN_USAGE_KEYS,
    add_token_usage,
    empty_token_usage,
    prune_zero_token_usage,
    token_usage_from_mapping,
)
from otto.queue.runtime import (
    IN_FLIGHT_STATUSES,
    INITIALIZING_STATUS,
    INTERRUPTED_STATUS,
    RUNNING_STATUS,
    RESUMABLE_QUEUE_STATUSES,
    checkpoint_path_for_task,
    task_display_status,
    task_resume_block_reason,
)
from otto.queue import schema as queue_schema
from otto.queue.schema import (
    QueueTask,
    append_command_ack,
    begin_command_drain,
    finish_command_drain,
    load_queue,
    load_state,
    lock_path,
    remove_task,
    write_state,
)
from otto.queue.ids import detect_cycles
from otto.runs.registry import (
    HEARTBEAT_INTERVAL_S,
    allocate_run_id,
    finalize_record,
    garbage_collect_live_records,
    make_run_record,
    update_record,
    write_record,
)
from otto.runs.schema import TERMINAL_STATUSES

logger = logging.getLogger("otto.queue.runner")


def _json_fingerprint(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class RunnerConfig:
    """Knobs for the watcher; mostly comes from otto.yaml `queue:` section."""

    concurrent: int = 3
    worktree_dir: str = ".worktrees"
    on_watcher_restart: str = "resume"   # resume | fail
    poll_interval_s: float = 2.0
    heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S
    # Per-task wall-clock timeout. A hung child (agent stuck in a bash
    # `wait` for an unkilled background process, infinite loop, etc.)
    # would otherwise occupy its concurrency slot forever. None disables.
    # Default: 70 minutes. The child build budget defaults to 60 minutes; this
    # outer guard should catch wedged children without preempting normal budget
    # handling and manifest finalization.
    task_timeout_s: float | None = 4200.0
    # Grace period after SIGTERM before escalating a terminating child to
    # SIGKILL. Keeps cancellation graceful without letting ignored SIGTERM
    # wedge queue concurrency forever.
    termination_grace_s: float = 10.0
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
    fh = open(path, "a+")
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
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _mark_failed(ts: dict[str, Any], reason: str) -> None:
    ts["status"] = "failed"
    ts["finished_at"] = now_iso()
    ts["duration_s"] = _terminal_duration_s(ts)
    ts["child"] = None
    ts["failure_reason"] = reason


def _mark_interrupted(ts: dict[str, Any], reason: str) -> None:
    ts["status"] = INTERRUPTED_STATUS
    ts["finished_at"] = now_iso()
    ts["duration_s"] = _terminal_duration_s(ts)
    ts["child"] = None
    ts["failure_reason"] = reason


def _terminal_outcome_for_status(status: str) -> str | None:
    mapping = {
        "done": "success",
        "failed": "failure",
        "cancelled": "cancelled",
        "removed": "removed",
        INTERRUPTED_STATUS: "interrupted",
    }
    return mapping.get(str(status or ""))


def _session_id_from_artifact_path(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        parts = path.parts
        index = parts.index(paths.SESSIONS_DIR_NAME)
    except ValueError:
        return None
    if index + 1 >= len(parts):
        return None
    session_id = str(parts[index + 1]).strip()
    return session_id or None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return None


def _read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _summary_path_from_manifest(manifest: dict[str, Any]) -> Path | None:
    for key in ("summary_path",):
        value = manifest.get(key)
        if isinstance(value, str) and value.strip():
            return Path(value).expanduser()
    checkpoint = manifest.get("checkpoint_path")
    if isinstance(checkpoint, str) and checkpoint.strip():
        return Path(checkpoint).expanduser().with_name("summary.json")
    mirror = manifest.get("mirror_of")
    if isinstance(mirror, str) and mirror.strip():
        return Path(mirror).expanduser().with_name("summary.json")
    return None


def _token_usage_from_summary(summary: dict[str, Any]) -> dict[str, int]:
    direct = token_usage_from_mapping(summary)
    if direct:
        return direct
    breakdown = summary.get("breakdown")
    if not isinstance(breakdown, dict):
        return {}
    totals = empty_token_usage()
    for phase in breakdown.values():
        if not isinstance(phase, dict):
            continue
        add_token_usage(totals, token_usage_from_mapping(phase))
    return prune_zero_token_usage(totals)


# ---------- PID-reuse-safe child validation ----------

def child_is_alive(child: dict[str, Any]) -> bool:
    """Return True iff the recorded PID still belongs to OUR child.

    Validates pid+pgid+start_time_ns+cwd. Any mismatch → child is
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
        garbage_collect_live_records(self.project_dir)
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
            self._abort_after_tick_failure()
            return last_heartbeat, 1, False
        except Exception:
            logger.exception("tick failed; stopping runner")
            self._abort_after_tick_failure()
            return last_heartbeat, 1, False

        now = time.monotonic()
        if now - last_heartbeat >= self.config.heartbeat_interval_s:
            try:
                self._update_watcher_state()
            except StatePersistenceError:
                logger.exception("state persistence failed during heartbeat; stopping runner")
                self._abort_after_tick_failure()
                return last_heartbeat, 1, False
            last_heartbeat = now

        if self.shutdown_level == "immediate":
            self._kill_all_in_flight(force=True)
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

    def _abort_after_tick_failure(self) -> None:
        """Best-effort cleanup when the runner loop cannot safely continue."""
        try:
            self._kill_all_in_flight(force=True)
        except Exception:
            logger.exception("failed to clean up in-flight queue tasks after tick failure")
        try:
            self._clear_watcher_state()
        except Exception:
            logger.exception("failed to clear queue watcher state after tick failure")

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
        commands: list[dict[str, Any]] = []
        command_drain_started = False
        try:
            commands = begin_command_drain(self.project_dir)
            command_drain_started = queue_schema.commands_processing_path(self.project_dir).exists()
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

        state = load_state(self.project_dir)
        # Reap before command application so a cancel/remove arriving just
        # after child exit cannot overwrite an already-successful manifest.
        self._reap_children(state)
        self._promote_ready_children(tasks, state)

        # Apply commands
        cycle_ids = {tid for cycle in cycles for tid in cycle}
        known_task_ids = {task.id for task in tasks}
        applied_commands: list[dict[str, Any]] = []
        applied_command_ids: set[str] = set()
        for cmd in commands:
            self._apply_command(cmd, state, known_task_ids=known_task_ids)
            applied_commands.append(cmd)
            cid = str(cmd.get("command_id") or "")
            if cid:
                applied_command_ids.add(cid)

        # Late-drain hook: closes the cancel-vs-dispatch race. A cancel POST
        # that lands *after* the first drain (above) but *before* a queued
        # task's spawn would otherwise be deferred to the next tick — by
        # which time the task is already running. `_dispatch_new` invokes
        # this callback right before each `queued` task evaluation so a
        # late cancel applied here flips the task's status before spawn.
        # mc-audit live W2-IMPORTANT-3.
        nonlocal_state = {"started": command_drain_started}

        def late_drain() -> None:
            try:
                late_commands = begin_command_drain(self.project_dir)
            except (OSError, ValueError) as exc:
                logger.error("failed to re-drain commands before dispatch: %s", exc)
                return
            nonlocal_state["started"] = (
                nonlocal_state["started"]
                or queue_schema.commands_processing_path(self.project_dir).exists()
            )
            for cmd in late_commands:
                cid = str(cmd.get("command_id") or "")
                if cid and cid in applied_command_ids:
                    continue
                self._apply_command(cmd, state, known_task_ids=known_task_ids)
                applied_commands.append(cmd)
                if cid:
                    applied_command_ids.add(cid)

        # Dispatch new work (skip during graceful shutdown)
        if self.shutdown_level is None:
            self._dispatch_new(tasks, state, cycle_ids, late_drain=late_drain)
        command_drain_started = nonlocal_state["started"]

        # Persist state
        self._write_state_or_raise(state)
        maintenance_changed = self._repair_terminal_queue_history(tasks, state)
        maintenance_changed = self._refresh_queue_run_records(tasks, state) or maintenance_changed
        maintenance_changed = self._cleanup_removed_task_definitions(tasks, state) or maintenance_changed
        if maintenance_changed:
            self._write_state_or_raise(state)
        for cmd in applied_commands:
            append_command_ack(
                self.project_dir,
                cmd,
                writer_id=f"queue:{os.getpid()}",
                state_version=int(state.get("version") or 0),
            )
        if command_drain_started:
            finish_command_drain(self.project_dir)
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
        command_drain_started = False
        try:
            commands = begin_command_drain(self.project_dir)
            command_drain_started = queue_schema.commands_processing_path(self.project_dir).exists()
        except (OSError, ValueError) as exc:
            logger.error("startup reconcile: failed to drain commands: %s", exc)
            commands = []
        tasks_by_id = {t.id: t for t in self._load_queue_or_empty(context="startup reconcile")}
        policy = self.config.on_watcher_restart
        for tid, ts in list(state.get("tasks", {}).items()):
            status = ts.get("status")
            if status == "starting":
                if queue_index_path_for(self.project_dir, tid) is not None and queue_index_path_for(self.project_dir, tid).exists():
                    self._finalize_task_from_manifest(ts, tid, exit_code=None)
                    logger.info("reconciling: task %s completed from manifest while starting", tid)
                    continue
                if tid not in tasks_by_id:
                    _mark_failed(ts, "watcher restart: task was starting but definition is missing")
                    continue
                ts["status"] = "queued"
                ts["child"] = None
                ts["failure_reason"] = None
                logger.info("reconciling: task %s was starting with no child; re-queued", tid)
                continue
            task = tasks_by_id.get(tid)
            if status == INITIALIZING_STATUS and task is not None and self._promote_ready_task(task, ts):
                status = ts.get("status")
            if status not in IN_FLIGHT_STATUSES:
                continue
            child = ts.get("child") or {}
            still_alive = child_is_alive(child)
            if status == "terminating":
                if still_alive:
                    self._maybe_escalate_terminating(tid, ts)
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
            if still_alive:
                if policy == "resume":
                    logger.info("reconciling: task %s child still alive, re-attaching", tid)
                    # Leave status unchanged; main loop will promote/reap later.
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
            manifest_path = queue_index_path_for(self.project_dir, tid)
            if manifest_path is not None and manifest_path.exists():
                self._finalize_task_from_manifest(ts, tid, exit_code=None)
                logger.info("reconciling: task %s finalized from queue manifest", tid)
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
        known_task_ids = set(tasks_by_id)
        for cmd in commands:
            self._apply_command(cmd, state, known_task_ids=known_task_ids)
        self._write_state_or_raise(state)
        tasks = list(tasks_by_id.values())
        maintenance_changed = self._repair_terminal_queue_history(tasks, state)
        maintenance_changed = self._refresh_queue_run_records(tasks, state) or maintenance_changed
        maintenance_changed = self._cleanup_removed_task_definitions(tasks, state) or maintenance_changed
        if maintenance_changed:
            self._write_state_or_raise(state)
        for cmd in commands:
            append_command_ack(
                self.project_dir,
                cmd,
                writer_id=f"queue:{os.getpid()}",
                state_version=int(state.get("version") or 0),
            )
        if command_drain_started:
            finish_command_drain(self.project_dir)

    # ---- command application ----

    def _apply_command(
        self,
        cmd: dict[str, Any],
        state: dict[str, Any],
        *,
        known_task_ids: set[str] | None = None,
    ) -> None:
        kind = str(cmd.get("kind") or cmd.get("cmd") or "").strip()
        tid = cmd.get("id")
        if not isinstance(tid, str) or not tid:
            args = cmd.get("args")
            if isinstance(args, dict):
                candidate = args.get("task_id") or args.get("id")
                if isinstance(candidate, str) and candidate:
                    tid = candidate
        if not isinstance(tid, str) or not tid:
            logger.warning("command missing/invalid 'id': %r", cmd)
            return
        if tid not in state.get("tasks", {}) and known_task_ids is not None and tid not in known_task_ids:
            logger.warning("command ignored for unknown task id %s", tid)
            return
        ts = state["tasks"].setdefault(tid, {"status": "queued"})
        status = ts.get("status", "queued")
        if kind == "cancel":
            if status not in ("queued", INITIALIZING_STATUS, RUNNING_STATUS, "terminating"):
                logger.warning("cancel ignored for %s in status=%s", tid, status)
                return
            if status in {INITIALIZING_STATUS, RUNNING_STATUS}:
                if self._finalize_child_if_finished(tid, ts):
                    logger.info("cancel ignored for %s after child finalized as %s", tid, ts.get("status"))
                    return
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
            self._snapshot_task_definition(tid, ts)
            ts["definition_removal_pending"] = True
            ts["status"] = "cancelled"
            ts["finished_at"] = now_iso()
            ts["duration_s"] = _terminal_duration_s(ts)
            ts["child"] = None
            ts["failure_reason"] = "cancelled by user"
        elif kind == "remove":
            if status == "removed":
                logger.warning("remove ignored for %s in status=removed", tid)
                return
            if status in TERMINAL_STATUSES:
                logger.warning("remove ignored for %s in terminal status=%s; use cleanup", tid, status)
                return
            if status in {INITIALIZING_STATUS, RUNNING_STATUS}:
                if self._finalize_child_if_finished(tid, ts):
                    logger.info("remove ignored for %s after child finalized as %s", tid, ts.get("status"))
                    return
                self._snapshot_task_definition(tid, ts)
                ts["definition_removal_pending"] = True
                child = ts.get("child") or {}
                if kill_child_safely(child, signal.SIGTERM):
                    logger.info("remove: sent SIGTERM to %s before removal", tid)
                self._mark_terminating(ts, final_status="removed", reason=ts.get("failure_reason"))
                return
            if status == "terminating":
                self._snapshot_task_definition(tid, ts)
                ts["definition_removal_pending"] = True
                ts["terminal_status"] = "removed"
                return
            self._snapshot_task_definition(tid, ts)
            ts["definition_removal_pending"] = True
            ts["status"] = "removed"
            ts["finished_at"] = now_iso()
            ts["duration_s"] = _terminal_duration_s(ts)
            ts.pop("terminal_status", None)
        elif kind == "resume":
            display_status = task_display_status(ts)
            task = next((task for task in self._load_queue_or_empty(context="resume command") if task.id == tid), None)
            if task is None:
                logger.warning("resume ignored for %s; queue task definition missing", tid)
                return
            if display_status not in RESUMABLE_QUEUE_STATUSES:
                logger.warning("resume ignored for %s in status=%s", tid, display_status)
                return
            reason = task_resume_block_reason(self.project_dir, task, ts)
            if reason is not None:
                logger.warning("resume ignored for %s; %s", tid, reason)
                return
            ts["status"] = "queued"
            ts["started_at"] = None
            ts["finished_at"] = None
            ts["duration_s"] = None
            ts["exit_code"] = None
            ts["child"] = None
            ts["failure_reason"] = None
            ts["resumed_from_checkpoint"] = True
            ts.pop("history_appended", None)
        elif kind == "cleanup":
            if status not in {"done", "failed", "cancelled", "removed", INTERRUPTED_STATUS}:
                logger.warning("cleanup ignored for %s in status=%s", tid, status)
                return
            task = next((task for task in self._load_queue_or_empty(context="cleanup command") if task.id == tid), None)
            if task is not None and task.worktree:
                wt_path = self.project_dir / task.worktree
                if wt_path.exists():
                    try:
                        preserve_queue_session_artifacts(
                            self.project_dir,
                            task_id=tid,
                            worktree_path=wt_path,
                            strict=False,
                        )
                    except Exception as exc:
                        logger.warning("cleanup: could not preserve artifacts for %s: %s", tid, exc)
                        return
                    result = subprocess.run(
                        ["git", "worktree", "remove", str(wt_path)],
                        cwd=self.project_dir,
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode != 0:
                        detail = (result.stderr or result.stdout or "").strip()
                        logger.warning("cleanup: could not remove worktree for %s: %s", tid, detail)
                        return
            if self._remove_task_definition(tid) is None:
                return
            ts["status"] = "removed"
            ts["finished_at"] = ts.get("finished_at") or now_iso()
            ts["duration_s"] = _terminal_duration_s(ts)
            ts["child"] = None
            ts["failure_reason"] = ts.get("failure_reason") or "cleaned up"
            logger.info("cleanup: removed terminal task %s from queue", tid)
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
        now = datetime.now(tz=timezone.utc)
        for tid, ts in in_flight:
            if ts.get("status") not in {INITIALIZING_STATUS, RUNNING_STATUS}:
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
            if self._finalize_child_if_finished(tid, ts):
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

    def _finalize_child_if_finished(self, task_id: str, ts: dict[str, Any]) -> bool:
        """Finalize a just-exited child before cancel/remove/timeout wins.

        `_reap_children` already runs before commands and timeouts. This second
        non-blocking check closes the small race where the child exits after the
        first reap pass returned 0 but before a destructive transition is applied.
        """
        if ts.get("status") not in {INITIALIZING_STATUS, RUNNING_STATUS}:
            return False
        child = ts.get("child") or {}
        pid = child.get("pid")
        if not isinstance(pid, int):
            return False
        try:
            wpid, wstatus = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            if child_is_alive(child):
                return False
            self._finalize_task_from_manifest(ts, task_id)
            self._join_output_pump(pid)
            return True
        if wpid == 0:
            return False
        exit_code = int(os.waitstatus_to_exitcode(wstatus))
        self._finalize_task_from_manifest(ts, task_id, exit_code=exit_code)
        self._join_output_pump(pid)
        return True

    # ---- reap finished children ----

    def _reap_children(self, state: dict[str, Any]) -> None:
        """Non-blocking reap of any exited children. Reads their manifests."""
        # Snapshot in-flight tasks so we can iterate without mutation issues.
        # Reap before timeout enforcement; otherwise a child that already exited
        # successfully can be failed by a delayed watcher tick.
        in_flight = [
            (tid, ts) for tid, ts in state["tasks"].items()
            if ts.get("status") in IN_FLIGHT_STATUSES
        ]
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
                    if status == "terminating":
                        self._maybe_escalate_terminating(tid, ts)
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
                if status == "terminating":
                    self._maybe_escalate_terminating(tid, ts)
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
        if self.config.task_timeout_s is not None:
            still_in_flight = [
                (tid, ts) for tid, ts in state["tasks"].items()
                if ts.get("status") in IN_FLIGHT_STATUSES
            ]
            self._enforce_task_timeouts(still_in_flight)

    # ---- dispatch new work ----

    def _dispatch_new(
        self,
        tasks: list[QueueTask],
        state: dict[str, Any],
        cycle_ids: set[str],
        *,
        late_drain: Callable[[], None] | None = None,
    ) -> None:
        """Spawn child processes for queued tasks with satisfied dependencies.

        ``late_drain`` (W2-IMPORTANT-3): invoked once before the queue scan,
        and again before each candidate's spawn. Drains any cancel/remove
        commands that arrived after the tick's first drain. Without this,
        a cancel posted in the small window between the first command-drain
        and dispatch would be deferred to the next tick — by which time the
        target task is already spawned.
        """
        if late_drain is not None:
            late_drain()
        in_flight = self._count_in_flight(state)
        slots = self.config.concurrent - in_flight
        if slots <= 0:
            return
        for task in tasks:
            if slots <= 0:
                break
            # Re-check for late commands (cancel/remove) before *each*
            # spawn so newly-arrived cancels apply against the freshest
            # task list. Cheap when the commands.jsonl is empty.
            if late_drain is not None:
                late_drain()
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
                slots -= 1
            except StatePersistenceError:
                raise
            except Exception as exc:
                current_ts = state["tasks"].get(task.id) or ts
                _mark_failed(current_ts, f"spawn failed: {exc}")
                state["tasks"][task.id] = current_ts
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
            # base_ref is set when this task should iterate on a prior run's
            # branch (W3-CRITICAL-1). Default None preserves git's "branch
            # from HEAD" behaviour.
            add_worktree(
                project_dir=self.project_dir,
                worktree_path=wt_path,
                branch=branch,
                base_ref=task.base_ref,
            )
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
        attempt_run_id = str(ts_existing.get("attempt_run_id") or "").strip()
        if not attempt_run_id:
            attempt_run_id = allocate_run_id(self.project_dir)
        if ts_existing.get("resumed_from_checkpoint"):
            argv.append("--resume")
            ts_existing.pop("resumed_from_checkpoint", None)
        ready_path = paths.queue_ready_path(self.project_dir, task.id)
        try:
            ready_path.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(f"could not clear stale readiness marker for {task.id}: {exc}") from exc

        state["tasks"][task.id] = {
            **ts_existing,
            "status": "starting",
            "started_at": now_iso(),
            "finished_at": None,
            "attempt_run_id": attempt_run_id,
            "child": None,
            "failure_reason": None,
        }
        self._write_state_or_raise(state)
        self._write_queue_run_record(task, state["tasks"][task.id], status="starting")

        # Spawn in its own process group so cancel can killpg cleanly.
        env = {
            **os.environ,
            "OTTO_INTERNAL_QUEUE_RUNNER": "1",
            "OTTO_QUEUE_TASK_ID": task.id,
            "OTTO_RUN_ID": attempt_run_id,
            # Anchor manifest writes to the MAIN project so the watcher (whose
            # cwd is the main project) and the child (whose cwd is the
            # worktree) resolve to the same path. See otto/manifest.py
            # `manifest_path_for`.
            "OTTO_QUEUE_PROJECT_DIR": str(self.project_dir),
        }
        popen_kwargs: dict[str, Any] = {
            "cwd": str(wt_path),
            "env": env,
            "start_new_session": True,
            # A watcher can outlive the web/terminal process that launched it.
            # Do not let spawned Python children inherit a closed fd 0; Python
            # can fail during interpreter startup before Otto writes artifacts.
            "stdin": subprocess.DEVNULL,
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
            "status": INITIALIZING_STATUS,
            "started_at": now_iso(),
            "finished_at": None,
            "attempt_run_id": attempt_run_id,
            "exit_code": None,
            "child": {
                "pid": proc.pid,
                "pgid": proc.pid,  # start_new_session=True -> pgid == pid
                "start_time_ns": start_time_ns,
                "argv": argv,
                "cwd": str(wt_path),
            },
            "manifest_path": None,
            "ready_path": str(ready_path),
            "ready_at": None,
            "cost_usd": None,
            "duration_s": None,
            "failure_reason": None,
        }
        try:
            self._write_state_or_raise(state)
        except StatePersistenceError:
            self._terminate_spawned_child_after_persist_failure(task.id, state)
            raise
        try:
            self._write_queue_run_record(task, state["tasks"][task.id], status=INITIALIZING_STATUS)
        except Exception:
            logger.exception("failed to update queue run record after spawn; continuing")
        logger.info("spawned %s: pid=%d, branch=%s", task.id, proc.pid, branch)

    def _read_queue_ready(self, task_id: str, ts: dict[str, Any]) -> dict[str, Any] | None:
        ready_path = paths.queue_ready_path(self.project_dir, task_id)
        if not ready_path.exists():
            return None
        try:
            payload = json.loads(ready_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("ignoring unreadable queue readiness marker for %s: %s", task_id, exc)
            return None
        if not isinstance(payload, dict):
            logger.warning("ignoring invalid queue readiness marker for %s", task_id)
            return None
        expected_run_id = str(ts.get("attempt_run_id") or "").strip()
        ready_run_id = str(payload.get("run_id") or "").strip()
        if expected_run_id and ready_run_id != expected_run_id:
            logger.warning(
                "ignoring stale queue readiness marker for %s: expected run_id=%s got %s",
                task_id,
                expected_run_id,
                ready_run_id or "<missing>",
            )
            return None
        payload["_ready_path"] = str(ready_path)
        return payload

    def _promote_ready_task(self, task: QueueTask, ts: dict[str, Any]) -> bool:
        if ts.get("status") != INITIALIZING_STATUS:
            return False
        payload = self._read_queue_ready(task.id, ts)
        if payload is None:
            return False
        run_id = str(payload.get("run_id") or "").strip()
        if run_id:
            ts["child_run_id"] = run_id
        ts["status"] = RUNNING_STATUS
        ts["ready_at"] = payload.get("ready_at") or now_iso()
        ts["ready_path"] = payload.get("_ready_path")
        for src_key, dst_key in (
            ("session_dir", "session_dir"),
            ("checkpoint_path", "checkpoint_path"),
            ("phase", "ready_phase"),
        ):
            value = payload.get(src_key)
            if value:
                ts[dst_key] = value
        ts["failure_reason"] = None
        return True

    def _promote_ready_children(self, tasks: list[QueueTask], state: dict[str, Any]) -> bool:
        changed = False
        tasks_by_id = {task.id: task for task in tasks}
        for task_id, ts in state.get("tasks", {}).items():
            task = tasks_by_id.get(task_id)
            if task is None:
                continue
            before = _json_fingerprint(ts)
            self._promote_ready_task(task, ts)
            changed = changed or _json_fingerprint(ts) != before
        return changed

    def _recover_child_run_id(self, task: QueueTask, ts: dict[str, Any]) -> str | None:
        ready = self._read_queue_ready(task.id, ts)
        if ready is not None:
            run_id = str(ready.get("run_id") or "").strip()
            if run_id:
                ts["ready_path"] = ready.get("_ready_path")
                ts["ready_at"] = ready.get("ready_at") or ts.get("ready_at")
                return run_id
        manifest_path = queue_index_path_for(self.project_dir, task.id)
        if manifest_path is not None and manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                manifest = None
            if isinstance(manifest, dict):
                run_id = str(manifest.get("run_id") or "").strip()
                if run_id:
                    ts["manifest_path"] = str(manifest_path)
                    return run_id
                checkpoint_path = manifest.get("checkpoint_path")
                if isinstance(checkpoint_path, str) and checkpoint_path:
                    session_id = _session_id_from_artifact_path(Path(checkpoint_path).expanduser())
                    if session_id:
                        return session_id
        checkpoint_path = checkpoint_path_for_task(self.project_dir, task)
        if checkpoint_path is not None:
            session_id = _session_id_from_artifact_path(checkpoint_path)
            if session_id:
                return session_id
        manifest_path_str = str(ts.get("manifest_path") or "").strip()
        if manifest_path_str:
            session_id = _session_id_from_artifact_path(Path(manifest_path_str).expanduser())
            if session_id:
                return session_id
        return None

    def _reconcile_task_identity(self, task: QueueTask, ts: dict[str, Any]) -> None:
        attempt_run_id = str(ts.get("attempt_run_id") or "").strip()
        child_run_id = self._recover_child_run_id(task, ts) or str(ts.get("child_run_id") or "").strip()
        if child_run_id:
            ts["child_run_id"] = child_run_id
        if not attempt_run_id and child_run_id:
            ts["compatibility_warning"] = "child predates run-id"
            return
        if attempt_run_id and child_run_id and child_run_id != attempt_run_id:
            ts["compatibility_warning"] = "child predates run-id"
        elif not child_run_id:
            ts.pop("child_run_id", None)
            ts.pop("compatibility_warning", None)
        else:
            ts.pop("compatibility_warning", None)

    def _queue_record_run_id(self, ts: dict[str, Any]) -> str:
        attempt_run_id = str(ts.get("attempt_run_id") or "").strip()
        if attempt_run_id:
            return attempt_run_id
        return str(ts.get("child_run_id") or "").strip()

    def _history_snapshot_matches(self, run_id: str, status: str) -> bool:
        from otto.runs.history import read_history_rows

        dedupe_key = f"terminal_snapshot:{run_id}"
        expected_outcome = _terminal_outcome_for_status(status)
        for row in reversed(read_history_rows(paths.history_jsonl(self.project_dir))):
            if not isinstance(row, dict):
                continue
            if str(row.get("dedupe_key") or "") != dedupe_key:
                continue
            return (
                str(row.get("status") or "") == status
                and (row.get("terminal_outcome") or None) == expected_outcome
            )
        return False

    def _queue_run_artifacts(self, task: QueueTask, ts: dict[str, Any]) -> dict[str, Any]:
        self._reconcile_task_identity(task, ts)
        session_run_id = str(ts.get("child_run_id") or ts.get("attempt_run_id") or "")
        wt_path = self._worktree_for(task)
        session_dir = paths.session_dir(wt_path, session_run_id) if session_run_id else paths.sessions_root(wt_path)
        manifest_path = ts.get("manifest_path") or (session_dir / "manifest.json")
        primary_log = (
            queue_primary_log_path(wt_path, session_run_id, command_argv=task.command_argv)
            if session_run_id
            else None
        )
        return {
            "session_dir": str(session_dir),
            "manifest_path": str(manifest_path) if manifest_path else None,
            "checkpoint_path": str(paths.session_checkpoint(wt_path, session_run_id)) if session_run_id else None,
            "summary_path": str(paths.session_summary(wt_path, session_run_id)) if session_run_id else None,
            "primary_log_path": str(primary_log) if primary_log is not None else None,
            "extra_log_paths": self._queue_extra_log_paths(ts, primary_log),
        }

    def _queue_extra_log_paths(self, ts: dict[str, Any], primary_log: Path | None) -> list[str]:
        status = str(ts.get("status") or "")
        if status not in {"failed", "cancelled", INTERRUPTED_STATUS}:
            return []
        if primary_log is not None and primary_log.exists():
            return []
        candidates = [
            paths.logs_dir(self.project_dir) / "web" / "watcher.log",
            paths.queue_dir(self.project_dir) / "watcher.log",
        ]
        return [str(path.resolve(strict=False)) for path in candidates if path.exists()]

    def _queue_intent_path(self, task: QueueTask, ts: dict[str, Any]) -> str | None:
        session_run_id = str(ts.get("child_run_id") or ts.get("attempt_run_id") or "").strip()
        if not session_run_id:
            return None
        return str(paths.session_intent(self._worktree_for(task), session_run_id))

    def _queue_spec_path(self, task: QueueTask, ts: dict[str, Any]) -> str | None:
        session_run_id = str(ts.get("child_run_id") or ts.get("attempt_run_id") or "").strip()
        if session_run_id:
            session_spec = paths.spec_dir(self._worktree_for(task), session_run_id) / "spec.md"
            if session_spec.exists():
                return str(session_spec)
        spec_path = str(task.spec_file_path or "").strip()
        return spec_path or None

    def _queue_metrics(self, ts: dict[str, Any]) -> dict[str, Any]:
        metrics: dict[str, Any] = {
            "cost_usd": ts.get("cost_usd"),
            "stories_passed": ts.get("stories_passed"),
            "stories_tested": ts.get("stories_tested"),
        }
        if isinstance(ts.get("breakdown"), dict):
            metrics["breakdown"] = ts["breakdown"]
        metrics.update(self._queue_usage_fields(ts))
        return metrics

    def _queue_usage_fields(self, ts: dict[str, Any]) -> dict[str, int]:
        fields: dict[str, int] = {}
        for key in TOKEN_USAGE_KEYS:
            value = _int_or_none(ts.get(key))
            if value is not None:
                fields[key] = value
        return fields

    def _record_summary_usage(self, ts: dict[str, Any], manifest: dict[str, Any]) -> None:
        summary_path = _summary_path_from_manifest(manifest)
        summary = _read_json(summary_path) if summary_path is not None else None
        if not isinstance(summary, dict):
            return
        usage = _token_usage_from_summary(summary)
        for key, value in usage.items():
            ts[key] = value
        for key in ("stories_passed", "stories_tested"):
            value = _int_or_none(summary.get(key))
            if value is not None:
                ts[key] = value
        if isinstance(summary.get("breakdown"), dict):
            ts["breakdown"] = summary["breakdown"]

    def _append_queue_history_snapshot(
        self,
        task: QueueTask,
        ts: dict[str, Any],
        *,
        run_id: str,
        status: str,
        terminal_outcome: str | None,
    ) -> None:
        from otto.runs.history import append_history_snapshot, build_terminal_snapshot

        artifacts = self._queue_run_artifacts(task, ts)
        wt_path = self._worktree_for(task)
        source = {
            "invoked_via": "queue",
            "argv": list(task.command_argv),
            "resumable": bool(task.resumable),
        }
        identity = {
            "queue_task_id": task.id,
            "child_run_id": ts.get("child_run_id"),
            "expected_child_run_id": ts.get("attempt_run_id"),
            "compatibility_warning": ts.get("compatibility_warning"),
        }
        extra_fields = {
            **self._queue_usage_fields(ts),
            "argv": list(task.command_argv),
            "source": source,
            "child_run_id": identity["child_run_id"],
            "expected_child_run_id": identity["expected_child_run_id"],
            "compatibility_warning": identity["compatibility_warning"],
            "failure_reason": ts.get("failure_reason"),
        }
        append_history_snapshot(
            self.project_dir,
            build_terminal_snapshot(
                run_id=run_id,
                domain="queue",
                run_type="queue",
                command=" ".join(task.command_argv[:2]) if task.command_argv else "queue",
                intent_meta={
                    "summary": (task.resolved_intent or task.id)[:200],
                    "intent_path": self._queue_intent_path(task, ts),
                    "spec_path": self._queue_spec_path(task, ts),
                },
                status=status,
                terminal_outcome=terminal_outcome,
                timing={
                    "started_at": str(ts.get("started_at") or "") or None,
                    "finished_at": str(ts.get("finished_at") or now_iso()) or now_iso(),
                    "timestamp": str(ts.get("finished_at") or now_iso()),
                    "duration_s": _terminal_duration_s(ts),
                },
                metrics=self._queue_metrics(ts),
                git={
                    "branch": task.branch,
                    "worktree": str(wt_path.resolve(strict=False)) if task.worktree else None,
                },
                source=source,
                identity=identity,
                artifacts=artifacts,
                extra_fields=extra_fields,
            ),
            strict=True,
        )

    def _write_queue_run_record(self, task: QueueTask, ts: dict[str, Any], *, status: str) -> None:
        self._reconcile_task_identity(task, ts)
        attempt_run_id = self._queue_record_run_id(ts)
        if not attempt_run_id:
            return
        command = " ".join(task.command_argv[:2]) if task.command_argv else "queue"
        child = ts.get("child") or {}
        record = make_run_record(
            project_dir=self.project_dir,
            run_id=attempt_run_id,
            domain="queue",
            run_type="queue",
            command=command,
            display_name=f"{task.id}: {command}",
            status=status,
            cwd=self._worktree_for(task),
            writer_id=f"queue:{os.getpid()}:{attempt_run_id}",
            identity={
                "queue_task_id": task.id,
                "merge_id": None,
                "parent_run_id": None,
                "child_run_id": ts.get("child_run_id"),
                "expected_child_run_id": str(ts.get("attempt_run_id") or "").strip() or None,
                "compatibility_warning": ts.get("compatibility_warning"),
            },
            source={
                "invoked_via": "queue",
                "argv": list(task.command_argv),
                "resumable": bool(task.resumable),
            },
            git={"branch": task.branch, "worktree": task.worktree, "target_branch": None, "head_sha": None},
            intent={
                "summary": task.resolved_intent or task.id,
                "intent_path": self._queue_intent_path(task, ts),
                "spec_path": self._queue_spec_path(task, ts),
            },
            artifacts=self._queue_run_artifacts(task, ts),
            metrics=self._queue_metrics(ts),
            adapter_key="queue.attempt",
            last_event=str(ts.get("failure_reason") or status),
        )
        if child:
            record.writer.update({
                "pid": child.get("pid"),
                "pgid": child.get("pgid"),
                "process_start_time_ns": child.get("start_time_ns"),
            })
        elif status == "starting":
            record.writer = {
                "kind": "pending-child",
                "writer_id": record.writer.get("writer_id"),
            }
        record.timing["heartbeat_interval_s"] = self.config.heartbeat_interval_s
        write_record(self.project_dir, record)

    def _refresh_queue_run_records(self, tasks: list[QueueTask], state: dict[str, Any]) -> bool:
        changed = False
        tasks_by_id = {task.id: task for task in tasks}
        for task_id, ts in state.get("tasks", {}).items():
            task = tasks_by_id.get(task_id)
            if task is None:
                continue
            before = _json_fingerprint(ts)
            self._reconcile_task_identity(task, ts)
            attempt_run_id = self._queue_record_run_id(ts)
            if not attempt_run_id:
                changed = changed or _json_fingerprint(ts) != before
                continue
            status = str(ts.get("status") or "queued")
            timing_updates: dict[str, Any] = {
                "heartbeat_interval_s": self.config.heartbeat_interval_s,
            }
            if status in TERMINAL_STATUSES:
                started_at = str(ts.get("started_at") or "").strip()
                finished_at = str(ts.get("finished_at") or "").strip() or now_iso()
                ts["finished_at"] = finished_at
                duration_s = _terminal_duration_s(ts)
                ts["duration_s"] = duration_s
                timing_updates.update({
                    "finished_at": finished_at,
                    "duration_s": duration_s,
                })
                if started_at:
                    timing_updates["started_at"] = started_at
            try:
                update_record(
                    self.project_dir,
                    attempt_run_id,
                    {
                        "status": status,
                        "identity": {
                            "child_run_id": ts.get("child_run_id"),
                            "expected_child_run_id": str(ts.get("attempt_run_id") or "").strip() or None,
                            "compatibility_warning": ts.get("compatibility_warning"),
                        },
                        "timing": timing_updates,
                        "artifacts": self._queue_run_artifacts(task, ts),
                        "metrics": self._queue_metrics(ts),
                        "last_event": str(ts.get("failure_reason") or status),
                    },
                    heartbeat=status in IN_FLIGHT_STATUSES,
                )
            except FileNotFoundError:
                if status in TERMINAL_STATUSES and self._history_snapshot_matches(attempt_run_id, status):
                    ts["history_appended"] = True
                    changed = changed or _json_fingerprint(ts) != before
                    continue
                self._write_queue_run_record(task, ts, status=status)
            changed = changed or _json_fingerprint(ts) != before
        return changed

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

    def _kill_all_in_flight(self, *, force: bool = False) -> None:
        state = load_state(self.project_dir)
        tasks = self._load_queue_or_empty(context="shutdown interrupt")
        tasks_by_id = {task.id: task for task in tasks}
        for tid, ts in state["tasks"].items():
            if ts.get("status") not in IN_FLIGHT_STATUSES:
                continue
            child = ts.get("child") or {}
            kill_child_safely(child, signal.SIGTERM)
            final_status = ts.get("terminal_status", "cancelled")
            reason = ts.get("failure_reason")
            if ts.get("status") in {INITIALIZING_STATUS, RUNNING_STATUS}:
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
            if force:
                if kill_child_safely(child, signal.SIGKILL):
                    ts["sigkill_sent_at"] = now_iso()
                    logger.warning("immediate shutdown sent SIGKILL to task %s", tid)
                self._finish_terminating(ts)
        self._write_state_or_raise(state)
        maintenance_changed = self._repair_terminal_queue_history(tasks, state)
        maintenance_changed = self._refresh_queue_run_records(tasks, state) or maintenance_changed
        maintenance_changed = self._cleanup_removed_task_definitions(tasks, state) or maintenance_changed
        if maintenance_changed:
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
        ts.setdefault("terminating_since", now_iso())
        ts["finished_at"] = None
        if reason is not None:
            ts["failure_reason"] = reason

    def _terminating_elapsed_s(self, ts: dict[str, Any]) -> float | None:
        raw = ts.get("terminating_since") or ts.get("started_at")
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            started = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return (datetime.now(tz=timezone.utc) - started).total_seconds()

    def _maybe_escalate_terminating(self, task_id: str, ts: dict[str, Any]) -> None:
        if ts.get("status") != "terminating" or ts.get("sigkill_sent_at"):
            return
        elapsed = self._terminating_elapsed_s(ts)
        if elapsed is None:
            ts.setdefault("terminating_since", now_iso())
            return
        if elapsed < max(0.0, self.config.termination_grace_s):
            return
        child = ts.get("child") or {}
        if kill_child_safely(child, signal.SIGKILL):
            ts["sigkill_sent_at"] = now_iso()
            logger.warning(
                "task %s ignored SIGTERM for %.0fs; sent SIGKILL",
                task_id,
                elapsed,
            )
        elif not child_is_alive(child):
            self._finish_terminating(ts)

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
            if self.shutdown_level == "immediate":
                _mark_interrupted(ts, self._shutdown_interrupted_reason(task_id))
                return
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
        self._record_summary_usage(ts, manifest)
        manifest_exit_status = str(manifest.get("exit_status") or "success")

        if exit_code not in (None, 0):
            _mark_failed(ts, f"exit_code={exit_code}")
            return
        if manifest_exit_status == "paused":
            ts["status"] = "paused"
            ts["duration_s"] = _terminal_duration_s(ts)
            extra = manifest.get("extra") if isinstance(manifest.get("extra"), dict) else {}
            next_action = str(extra.get("next_action") or "").strip()
            ts["failure_reason"] = next_action or "paused; resume available"
            return
        if manifest_exit_status != "success":
            _mark_failed(ts, f"manifest exit_status={manifest_exit_status}")
            return

        ts["status"] = "done"
        ts["duration_s"] = _terminal_duration_s(ts)
        ts["failure_reason"] = None

    def _shutdown_interrupted_reason(self, task_id: str) -> str:
        task = next(
            (candidate for candidate in self._load_queue_or_empty(context="shutdown interrupt reason") if candidate.id == task_id),
            None,
        )
        if task is not None and checkpoint_path_for_task(self.project_dir, task) is not None:
            return "interrupted by watcher shutdown; resume available"
        return "interrupted by watcher shutdown before manifest was written"

    def _finish_terminating(self, ts: dict[str, Any]) -> None:
        ts["status"] = ts.pop("terminal_status", "cancelled")
        ts["finished_at"] = now_iso()
        ts["duration_s"] = _terminal_duration_s(ts)
        ts["child"] = None
        ts.pop("terminating_since", None)
        ts.pop("sigkill_sent_at", None)

    def _snapshot_task_definition(self, task_id: str, ts: dict[str, Any]) -> None:
        if isinstance(ts.get("task_definition"), dict):
            return
        tasks = {task.id: task for task in self._load_queue_or_empty(context="task definition snapshot")}
        task = tasks.get(task_id)
        if task is None:
            return
        ts["task_definition"] = {
            key: value
            for key, value in asdict(task).items()
            if value is not None and value != []
        }

    def _task_from_state_snapshot(self, task_id: str, ts: dict[str, Any]) -> QueueTask | None:
        raw = ts.get("task_definition")
        if not isinstance(raw, dict):
            return None
        argv = raw.get("command_argv")
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            logger.warning("queue task %s has invalid persisted task_definition", task_id)
            return None
        return QueueTask(
            id=str(raw.get("id") or task_id),
            command_argv=list(argv),
            after=list(raw.get("after") or []),
            resumable=bool(raw.get("resumable", True)),
            added_at=str(raw.get("added_at") or ""),
            resolved_intent=raw.get("resolved_intent"),
            focus=raw.get("focus"),
            target=raw.get("target"),
            spec_file_path=raw.get("spec_file_path"),
            branch=raw.get("branch"),
            worktree=raw.get("worktree"),
            base_ref=raw.get("base_ref"),
            notes=raw.get("notes"),
        )

    def _task_for_terminal_state(
        self,
        task_id: str,
        ts: dict[str, Any],
        tasks_by_id: dict[str, QueueTask] | None = None,
    ) -> QueueTask | None:
        if tasks_by_id is not None and task_id in tasks_by_id:
            return tasks_by_id[task_id]
        return self._task_from_state_snapshot(task_id, ts)

    def _finalize_queue_attempt(self, task_id: str, ts: dict[str, Any]) -> None:
        status = str(ts.get("status") or "")
        if status not in TERMINAL_STATUSES:
            return
        tasks = {task.id: task for task in self._load_queue_or_empty(context="queue attempt finalize")}
        task = self._task_for_terminal_state(task_id, ts, tasks)
        if task is None:
            return
        self._reconcile_task_identity(task, ts)
        attempt_run_id = self._queue_record_run_id(ts)
        if not attempt_run_id:
            # Task reached terminal status without ever starting (e.g. a
            # queued task that was cancelled before dispatch). There is no
            # live record to finalize, but we still owe the operator a
            # history row — otherwise the cancelled task vanishes from
            # /api/state entirely (W2-CRITICAL-1). Synthesize a stable
            # run_id so the snapshot dedupes across maintenance ticks.
            self._finalize_unstarted_queue_task(task, task_id, ts, status=status)
            return
        if self._history_snapshot_matches(attempt_run_id, status):
            ts["history_appended"] = True
            return
        terminal_outcome = _terminal_outcome_for_status(status)
        try:
            finalize_record(
                self.project_dir,
                attempt_run_id,
                status=status,
                terminal_outcome=terminal_outcome,
                updates={
                    "timing": {"heartbeat_interval_s": self.config.heartbeat_interval_s},
                    "artifacts": self._queue_run_artifacts(task, ts),
                    "metrics": self._queue_metrics(ts),
                    "last_event": str(ts.get("failure_reason") or status),
                },
            )
        except FileNotFoundError:
            self._write_queue_run_record(task, ts, status=status)
            finalize_record(
                self.project_dir,
                attempt_run_id,
                status=status,
                terminal_outcome=terminal_outcome,
                updates={"timing": {"heartbeat_interval_s": self.config.heartbeat_interval_s}},
            )
        try:
            self._append_queue_history_snapshot(
                task,
                ts,
                run_id=attempt_run_id,
                status=status,
                terminal_outcome=terminal_outcome,
            )
        except Exception:
            logger.exception("failed to append terminal history for queue task %s; will retry", task_id)
            return
        ts["history_appended"] = True

    def _finalize_unstarted_queue_task(
        self,
        task: QueueTask,
        task_id: str,
        ts: dict[str, Any],
        *,
        status: str,
    ) -> None:
        """Append a history snapshot for a task that terminated before dispatch.

        Used when the operator cancels a `queued` task that never spawned a
        child process. Since there is no `attempt_run_id` (and thus no live
        record), we synthesize a stable synthetic run_id keyed on the task
        id + added_at so the same row dedupes across repeat maintenance
        ticks. Skips the live-record finalize step entirely — there is no
        live record to update.
        """
        synthetic_run_id = str(ts.get("synthetic_history_run_id") or "").strip()
        if not synthetic_run_id:
            added_at = str(ts.get("added_at") or task.added_at or "").strip()
            suffix = added_at.replace(":", "").replace("-", "").replace("T", "").replace("Z", "")
            synthetic_run_id = f"queue-cancel:{task_id}" if not suffix else f"queue-cancel:{task_id}:{suffix}"
            ts["synthetic_history_run_id"] = synthetic_run_id
        if self._history_snapshot_matches(synthetic_run_id, status):
            ts["history_appended"] = True
            return
        terminal_outcome = _terminal_outcome_for_status(status)
        try:
            self._append_queue_history_snapshot(
                task,
                ts,
                run_id=synthetic_run_id,
                status=status,
                terminal_outcome=terminal_outcome,
            )
        except Exception:
            logger.exception(
                "failed to append terminal history for unstarted queue task %s; will retry",
                task_id,
            )
            return
        ts["history_appended"] = True

    def _repair_terminal_queue_history(self, tasks: list[QueueTask], state: dict[str, Any]) -> bool:
        changed = False
        tasks_by_id = {task.id: task for task in tasks}
        for task_id, ts in state.get("tasks", {}).items():
            task = self._task_for_terminal_state(task_id, ts, tasks_by_id)
            if task is None:
                continue
            status = str(ts.get("status") or "")
            if status not in TERMINAL_STATUSES:
                continue
            before = _json_fingerprint(ts)
            self._finalize_queue_attempt(task_id, ts)
            changed = changed or _json_fingerprint(ts) != before
        return changed

    def _cleanup_removed_task_definitions(self, tasks: list[QueueTask], state: dict[str, Any]) -> bool:
        changed = False
        tasks_by_id = {task.id: task for task in tasks}
        for task_id, ts in list(state.get("tasks", {}).items()):
            if task_id not in tasks_by_id:
                if ts.get("definition_removal_pending") and ts.get("status") in {"cancelled", "removed"}:
                    ts.pop("definition_removal_pending", None)
                    changed = True
                continue
            if not ts.get("definition_removal_pending"):
                continue
            if ts.get("status") not in {"cancelled", "removed"}:
                continue
            # Wait for the terminal history snapshot before removing the
            # task definition. Both real attempt runs and synthetic
            # cancel-before-start rows need history to land first; otherwise
            # the cancelled task would vanish from /api/state entirely
            # (W2-CRITICAL-1). For unstarted cancels (no attempt run id),
            # opportunistically run finalize here so callers that bypass
            # _repair_terminal_queue_history (e.g. unit tests, edge-case
            # restart paths) still produce a history row before cleanup.
            if not ts.get("history_appended"):
                if not self._queue_record_run_id(ts):
                    self._finalize_queue_attempt(task_id, ts)
                if not ts.get("history_appended"):
                    continue
            if self._remove_task_definition(task_id) is not None:
                ts.pop("definition_removal_pending", None)
                changed = True
        return changed

    def _write_state_or_raise(self, state: dict[str, Any]) -> None:
        try:
            write_state(self.project_dir, state)
        except Exception as exc:
            raise StatePersistenceError(str(exc)) from exc


def _terminal_duration_s(ts: dict[str, Any]) -> float | None:
    raw_duration = ts.get("duration_s")
    if raw_duration not in (None, ""):
        try:
            return float(raw_duration)
        except (TypeError, ValueError):
            pass
    started_at = str(ts.get("started_at") or "").strip()
    finished_at = str(ts.get("finished_at") or "").strip()
    if not started_at or not finished_at:
        return None
    try:
        started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        finished = datetime.strptime(finished_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0.0, (finished - started).total_seconds())


def runner_config_from_otto_config(config: dict[str, Any]) -> RunnerConfig:
    """Build a RunnerConfig from the parsed otto.yaml dict."""
    q = config.get("queue") or {}
    raw_concurrent = q.get("concurrent", 3)
    if not isinstance(raw_concurrent, int) or isinstance(raw_concurrent, bool):
        raise ValueError("queue.concurrent must be an integer >= 1")
    concurrent = raw_concurrent
    if concurrent < 1:
        raise ValueError("queue.concurrent must be >= 1")
    queue_defaults = DEFAULTS["queue"] if isinstance(DEFAULTS.get("queue"), dict) else {}
    raw_timeout = q.get("task_timeout_s", queue_defaults.get("task_timeout_s", 4200.0))
    task_timeout: float | None
    if raw_timeout is None:
        task_timeout = None  # explicit opt-out
    else:
        if (
            not isinstance(raw_timeout, (int, float))
            or isinstance(raw_timeout, bool)
            or float(raw_timeout) < 0.0
        ):
            raise ValueError("queue.task_timeout_s must be a number >= 0, or null")
        task_timeout = None if float(raw_timeout) == 0.0 else float(raw_timeout)
    return RunnerConfig(
        concurrent=concurrent,
        worktree_dir=str(q.get("worktree_dir", ".worktrees")),
        on_watcher_restart=str(q.get("on_watcher_restart", "resume")),
        task_timeout_s=task_timeout,
    )
