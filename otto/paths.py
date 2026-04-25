"""Otto log paths — single choke point for all `otto_logs/` writes and reads.

New layout (one dir per invocation):

    otto_logs/
    ├── sessions/<session_id>/
    │   ├── intent.txt
    │   ├── summary.json             # only for completed sessions
    │   ├── checkpoint.json          # only while in-flight/paused
    │   ├── spec/                    # only if --spec used
    │   ├── build/                   # coding-agent artifacts
    │   ├── certify/                 # verification artifacts
    │   └── improve/                 # only for `otto improve`
    ├── latest                       # symlink → sessions/<id>
    ├── paused                       # symlink → sessions/<id> (or missing)
    ├── .lock                        # project lock
    └── cross-sessions/
        ├── history.jsonl
        └── certifier-memory.jsonl

Legacy layout (pre-restructure) is still readable. Managed runtime path helpers
live here; new product code should prefer these helpers instead of spelling out
`otto_logs/...` paths inline.

Project locking uses a kernel `flock` on Unix for correctness. On Windows,
locking is best-effort; `release()` has an unavoidable TOCTOU. Use flock
(Unix) for full correctness.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:
    fcntl = None

logger = logging.getLogger("otto.paths")
_BEST_EFFORT_LOCK_WARNING_EMITTED = False

# ---------------------------------------------------------------------------
# Path constants and layout helpers
# ---------------------------------------------------------------------------

LOGS_ROOT_NAME = "otto_logs"
SESSIONS_DIR_NAME = "sessions"
CROSS_SESSIONS_DIR_NAME = "cross-sessions"
LOCK_FILE_NAME = ".lock"
LATEST_POINTER = "latest"
PAUSED_POINTER = "paused"
HISTORY_FILE_NAME = "history.jsonl"
CERTIFIER_MEMORY_FILE_NAME = "certifier-memory.jsonl"
RUNS_DIR_NAME = "runs"
LIVE_RUNS_DIR_NAME = "live"
RUN_GC_DIR_NAME = "gc"
RUN_GC_TOMBSTONES_FILE_NAME = "tombstones.jsonl"
COMMANDS_DIR_NAME = "commands"
REQUESTS_FILE_NAME = "requests.jsonl"
REQUESTS_PROCESSING_FILE_NAME = "requests.jsonl.processing"
ACKS_FILE_NAME = "acks.jsonl"
MERGE_DIR_NAME = "merge"
QUEUE_DIR_NAME = "queue"
QUEUE_TASK_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:-\d+)?$")

# Legacy (pre-restructure) path constants — reader fallback only.
LEGACY_CHECKPOINT = "checkpoint.json"
LEGACY_RUN_HISTORY = "run-history.jsonl"
LEGACY_CERTIFIER_MEMORY = "certifier-memory.jsonl"
LEGACY_BUILDS_DIR_NAME = "builds"
LEGACY_CERTIFIER_DIR_NAME = "certifier"
LEGACY_ROUNDS_DIR_NAME = "rounds"
LEGACY_RUNS_DIR_NAME = "runs"


def logs_dir(project_dir: Path) -> Path:
    """Return the otto_logs directory for a project."""
    return Path(project_dir) / LOGS_ROOT_NAME


def project_intent_md(project_dir: Path) -> Path:
    """Return the project-root intent.md path."""
    return Path(project_dir) / "intent.md"


def project_readme_md(project_dir: Path) -> Path:
    """Return the project-root README.md path."""
    return Path(project_dir) / "README.md"


def project_otto_yaml(project_dir: Path) -> Path:
    """Return the project-root otto.yaml path."""
    return Path(project_dir) / "otto.yaml"


def project_claude_md(project_dir: Path) -> Path:
    """Return the project-root CLAUDE.md path."""
    return Path(project_dir) / "CLAUDE.md"


def sessions_root(project_dir: Path) -> Path:
    return logs_dir(project_dir) / SESSIONS_DIR_NAME


def session_dir(project_dir: Path, session_id: str) -> Path:
    return sessions_root(project_dir) / session_id


def spec_dir(project_dir: Path, session_id: str) -> Path:
    return session_dir(project_dir, session_id) / "spec"


def build_dir(project_dir: Path, session_id: str) -> Path:
    return session_dir(project_dir, session_id) / "build"


def certify_dir(project_dir: Path, session_id: str) -> Path:
    return session_dir(project_dir, session_id) / "certify"


def improve_dir(project_dir: Path, session_id: str) -> Path:
    return session_dir(project_dir, session_id) / "improve"


def cross_sessions_dir(project_dir: Path) -> Path:
    return logs_dir(project_dir) / CROSS_SESSIONS_DIR_NAME


def history_jsonl(project_dir: Path) -> Path:
    """New cross-session history path."""
    return cross_sessions_dir(project_dir) / HISTORY_FILE_NAME


def certifier_memory_jsonl(project_dir: Path) -> Path:
    """New cross-session certifier memory path."""
    return cross_sessions_dir(project_dir) / CERTIFIER_MEMORY_FILE_NAME


def runs_dir(project_dir: Path) -> Path:
    return cross_sessions_dir(project_dir) / RUNS_DIR_NAME


def live_runs_dir(project_dir: Path) -> Path:
    return runs_dir(project_dir) / LIVE_RUNS_DIR_NAME


def live_run_path(project_dir: Path, run_id: str) -> Path:
    return live_runs_dir(project_dir) / f"{run_id}.json"


def run_gc_dir(project_dir: Path) -> Path:
    return runs_dir(project_dir) / RUN_GC_DIR_NAME


def run_gc_tombstones_jsonl(project_dir: Path) -> Path:
    return run_gc_dir(project_dir) / RUN_GC_TOMBSTONES_FILE_NAME


def session_checkpoint(project_dir: Path, session_id: str) -> Path:
    """Per-session resume checkpoint (only exists while in-flight/paused)."""
    return session_dir(project_dir, session_id) / "checkpoint.json"


def session_summary(project_dir: Path, session_id: str) -> Path:
    """Post-run summary for completed sessions (permanent final record)."""
    return session_dir(project_dir, session_id) / "summary.json"


def session_intent(project_dir: Path, session_id: str) -> Path:
    """Runtime snapshot of the resolved intent for one session.

    Project-root `intent.md` is a user-owned product description input, not an
    Otto-managed runtime log.
    """
    return session_dir(project_dir, session_id) / "intent.txt"


def session_commands_dir(project_dir: Path, run_id: str) -> Path:
    return session_dir(project_dir, run_id) / COMMANDS_DIR_NAME


def session_command_requests(project_dir: Path, run_id: str) -> Path:
    return session_commands_dir(project_dir, run_id) / REQUESTS_FILE_NAME


def session_command_requests_processing(project_dir: Path, run_id: str) -> Path:
    return session_commands_dir(project_dir, run_id) / REQUESTS_PROCESSING_FILE_NAME


def session_command_acks(project_dir: Path, run_id: str) -> Path:
    return session_commands_dir(project_dir, run_id) / ACKS_FILE_NAME


def merge_dir(project_dir: Path) -> Path:
    return logs_dir(project_dir) / MERGE_DIR_NAME


def queue_dir(project_dir: Path) -> Path:
    return logs_dir(project_dir) / QUEUE_DIR_NAME


def queue_manifest_path(project_dir: Path, task_id: str) -> Path:
    if not QUEUE_TASK_ID_RE.fullmatch(str(task_id)):
        raise ValueError(
            f"Invalid queue task id {task_id!r}. Expected a lowercase slug "
            "like 'add-csv-export' or 'add-csv-export-2'."
        )
    return queue_dir(project_dir) / task_id / "manifest.json"


def legacy_builds_dir(project_dir: Path) -> Path:
    return logs_dir(project_dir) / LEGACY_BUILDS_DIR_NAME


def legacy_certifier_dir(project_dir: Path) -> Path:
    return logs_dir(project_dir) / LEGACY_CERTIFIER_DIR_NAME


def legacy_certifier_latest_pow_html(project_dir: Path) -> Path:
    return legacy_certifier_dir(project_dir) / "latest" / "proof-of-work.html"


def legacy_rounds_dir(project_dir: Path) -> Path:
    return logs_dir(project_dir) / LEGACY_ROUNDS_DIR_NAME


def legacy_runs_dir(project_dir: Path) -> Path:
    return logs_dir(project_dir) / LEGACY_RUNS_DIR_NAME


def merge_commands_dir(project_dir: Path) -> Path:
    return merge_dir(project_dir) / COMMANDS_DIR_NAME


def merge_command_requests(project_dir: Path) -> Path:
    return merge_commands_dir(project_dir) / REQUESTS_FILE_NAME


def merge_command_requests_processing(project_dir: Path) -> Path:
    return merge_commands_dir(project_dir) / REQUESTS_PROCESSING_FILE_NAME


def merge_command_acks(project_dir: Path) -> Path:
    return merge_commands_dir(project_dir) / ACKS_FILE_NAME


def queue_state_path(project_dir: Path) -> Path:
    return Path(project_dir) / ".otto-queue-state.json"


def queue_commands_path(project_dir: Path) -> Path:
    return Path(project_dir) / ".otto-queue-commands.jsonl"


def queue_commands_processing_path(project_dir: Path) -> Path:
    return Path(project_dir) / ".otto-queue-commands.jsonl.processing"


def queue_command_acks_path(project_dir: Path) -> Path:
    return Path(project_dir) / ".otto-queue-commands.acks.jsonl"


def legacy_checkpoint(project_dir: Path) -> Path:
    return logs_dir(project_dir) / LEGACY_CHECKPOINT


def legacy_certifier_memory_jsonl(project_dir: Path) -> Path:
    return logs_dir(project_dir) / LEGACY_CERTIFIER_MEMORY


def legacy_run_history_jsonl(project_dir: Path) -> Path:
    return logs_dir(project_dir) / LEGACY_RUN_HISTORY


def ensure_session_scaffold(project_dir: Path, session_id: str, phase: str | None = None) -> Path:
    """Create the session dir and optionally one phase subdir. Idempotent."""
    sess = session_dir(project_dir, session_id)
    sess.mkdir(parents=True, exist_ok=True)
    if phase:
        (sess / phase).mkdir(parents=True, exist_ok=True)
    return sess


def archived_pre_restructure_dirs(project_dir: Path) -> list[Path]:
    """Return any existing otto_logs.pre-restructure.<ts>/ sibling archives.

    Used by readers to fall back onto archived history/memory post-migration.
    """
    project = Path(project_dir)
    prefix = f"{LOGS_ROOT_NAME}.pre-restructure."
    return sorted(
        p for p in project.iterdir()
        if p.is_dir() and p.name.startswith(prefix)
    )


# ---------------------------------------------------------------------------
# Session ID
# ---------------------------------------------------------------------------

_SESSION_ID_MAX_ATTEMPTS = 16


def new_session_id(project_dir: Path) -> str:
    """Allocate a fresh session_id of form `<yyyy-mm-dd>-<HHMMSS>-<6hex>`.

    Retries up to 16 times on directory collision. Caller MUST hold the
    project lock; this function doesn't re-enter the lock.
    """
    root = sessions_root(project_dir)
    for _ in range(_SESSION_ID_MAX_ATTEMPTS):
        now = datetime.now(timezone.utc)
        sid = f"{now:%Y-%m-%d-%H%M%S}-{secrets.token_hex(3)}"
        if not (root / sid).exists():
            return sid
    raise RuntimeError(
        f"could not allocate unique session_id after {_SESSION_ID_MAX_ATTEMPTS} tries"
    )


def is_session_id(candidate: str) -> bool:
    """Best-effort validation that a string looks like a session_id."""
    if not candidate or len(candidate) != len("YYYY-MM-DD-HHMMSS-abcdef"):
        return False
    return candidate[4] == "-" and candidate[7] == "-" and candidate[10] == "-" and candidate[17] == "-"


# ---------------------------------------------------------------------------
# Pointer files (latest, paused)
# ---------------------------------------------------------------------------

def _pointer_text_file(logs_root: Path, name: str) -> Path:
    return logs_root / f"{name}.txt"


def _pointer_link(logs_root: Path, name: str) -> Path:
    return logs_root / name


def _pointer_tmp_path(root: Path, name: str, suffix: str) -> Path:
    token = f"{os.getpid()}.{uuid.uuid4().hex}"
    return root / f".{name}.{token}.{suffix}.tmp"


def set_pointer(project_dir: Path, name: str, session_id: str, *, strict: bool = False) -> None:
    """Atomically point `name` at sessions/<session_id>.

    Prefers an OS symlink. Falls back to a `.txt` pointer file if symlinks
    are unsupported (Windows without admin, some CI volumes).
    """
    root = logs_dir(project_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = f"{SESSIONS_DIR_NAME}/{session_id}"
    link = _pointer_link(root, name)
    tmp_link = _pointer_tmp_path(root, name, "link")

    try:
        os.symlink(target, tmp_link)
        os.replace(tmp_link, link)
        # If a stale .txt fallback existed, remove it now that symlink is live.
        try:
            _pointer_text_file(root, name).unlink(missing_ok=True)
        except OSError:
            pass
        return
    except (OSError, NotImplementedError) as exc:
        logger.warning("symlink pointer %s failed (%s); using .txt fallback", name, exc)

    # Fallback: atomic write of .txt. First remove any stale symlink at
    # `link` — otherwise resolve_pointer (symlink-first) returns pre-failure
    # target.
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
    except OSError:
        pass

    tmp_txt = _pointer_tmp_path(root, name, "txt")
    try:
        tmp_txt.write_text(session_id, encoding="utf-8")
        os.replace(tmp_txt, _pointer_text_file(root, name))
    except OSError as exc:
        try:
            tmp_txt.unlink(missing_ok=True)
        except OSError:
            pass
        logger.warning("pointer .txt fallback also failed for %s: %s", name, exc)
        if strict:
            raise


def resolve_pointer(project_dir: Path, name: str) -> Path | None:
    """Resolve `name` pointer to an absolute session directory.

    Try in order:
      1. symlink at otto_logs/<name>
      2. pointer file at otto_logs/<name>.txt

    Returns absolute session dir path, or None if no pointer resolves.
    """
    root = logs_dir(project_dir)
    if not root.exists():
        return None

    def _valid_paused_target(session_path: Path) -> bool:
        if not session_path.exists():
            return False
        checkpoint_path = session_path / "checkpoint.json"
        if not checkpoint_path.exists():
            return False
        summary_path = session_path / "summary.json"
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text())
            except (OSError, json.JSONDecodeError):
                return False
            if summary.get("status") == "completed":
                return False
        return True

    def _is_valid_pointer_target(session_path: Path) -> bool:
        if name == PAUSED_POINTER:
            return _valid_paused_target(session_path)
        return session_path.exists()

    link = _pointer_link(root, name)
    pointer_seen = False
    try:
        if link.is_symlink():
            pointer_seen = True
            resolved = link.resolve(strict=False)
            if _is_valid_pointer_target(resolved):
                return resolved
            if name != PAUSED_POINTER:
                return None
    except OSError:
        pass

    txt = _pointer_text_file(root, name)
    if txt.exists():
        pointer_seen = True
        try:
            sid = txt.read_text().strip()
            if sid:
                sess = session_dir(project_dir, sid)
                if _is_valid_pointer_target(sess):
                    return sess
        except OSError:
            pass

    if name == PAUSED_POINTER and pointer_seen:
        sessions = sessions_root(project_dir)
        if not sessions.exists():
            return None

        def _checkpoint_timestamp(checkpoint_path: Path) -> float:
            try:
                data = json.loads(checkpoint_path.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
            updated_at = data.get("updated_at")
            if isinstance(updated_at, str) and updated_at:
                try:
                    return datetime.fromisoformat(updated_at.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
            try:
                return checkpoint_path.stat().st_mtime
            except OSError:
                return 0.0

        best: tuple[float, Path] | None = None
        for checkpoint_path in sessions.glob("*/checkpoint.json"):
            session_path = checkpoint_path.parent
            if not _valid_paused_target(session_path):
                continue
            candidate = (_checkpoint_timestamp(checkpoint_path), session_path)
            if best is None or candidate[0] >= best[0]:
                best = candidate
        if best is not None:
            return best[1]

    return None


def clear_pointer(project_dir: Path, name: str) -> None:
    """Remove the named pointer (symlink and/or .txt). Never raises."""
    root = logs_dir(project_dir)
    for p in (_pointer_link(root, name), _pointer_text_file(root, name)):
        try:
            if p.is_symlink() or p.exists():
                p.unlink()
        except OSError as exc:
            logger.warning("clear_pointer(%s): %s", p.name, exc)


# ---------------------------------------------------------------------------
# Project lock — one invocation per project at a time
# ---------------------------------------------------------------------------

class LockBusy(Exception):
    """Raised when another live PID holds the project lock."""

    def __init__(self, holder: dict):
        self.holder = holder or {}
        super().__init__(
            f"project lock held by pid={self.holder.get('pid')!r} "
            f"command={self.holder.get('command')!r} "
            f"started_at={self.holder.get('started_at')!r}"
        )


class LockBreakError(RuntimeError):
    """Raised when --break-lock targets a live Unix flock holder."""


@dataclass
class LockHandle:
    """Returned by acquire_project_lock. Use as a context manager."""

    _path: Path
    _fd: int | None
    _pid: int
    _started_at: str
    _command: str
    _nonce: str
    _session_id: str | None = None
    _released: bool = False

    def set_session_id(self, session_id: str) -> None:
        """Remember the bound session_id without mutating the lock record."""
        self._session_id = session_id
        logger.info("lock now bound to session %s", session_id)

    def release(self) -> None:
        """Release the kernel lock and unlink the lock path if it is still ours."""
        if self._released:
            return
        self._released = True

        if self._fd is not None:
            try:
                fd_stat = os.fstat(self._fd)
            except OSError:
                fd_stat = None

            try:
                path_stat = os.stat(self._path)
            except FileNotFoundError:
                logger.warning("lock record no longer ours (broken?)")
                path_stat = None
            except OSError as exc:
                logger.warning("lock release failed: %s", exc)
                path_stat = None

            try:
                if (
                    fd_stat is not None
                    and path_stat is not None
                    and fd_stat.st_ino == path_stat.st_ino
                    and fd_stat.st_dev == path_stat.st_dev
                ):
                    os.unlink(self._path)
                elif path_stat is not None:
                    logger.warning("lock record no longer ours (broken?)")
            except FileNotFoundError:
                logger.warning("lock record no longer ours (broken?)")
            except OSError as exc:
                logger.warning("lock release failed: %s", exc)
            finally:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
            return

        try:
            holder = json.loads(self._path.read_text())
        except FileNotFoundError:
            logger.warning("lock record no longer ours (broken?)")
            return
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("lock release failed: %s", exc)
            return

        if holder.get("nonce") != self._nonce:
            logger.warning("lock record no longer ours (broken?)")
            return

        try:
            # Windows fallback: release-time TOCTOU is inherent here.
            # Between the nonce check and the unlink, another process could
            # replace `.lock` (e.g., via `otto --break-lock`). We accept this
            # because (a) the window is microseconds, (b) the `--break-lock`
            # operator knows they're forcing things, and (c) a spurious
            # unlink just means the next acquire creates a fresh lock. Full
            # correctness would require fcntl/flock which Windows does not
            # provide.
            os.unlink(self._path)
        except FileNotFoundError:
            logger.warning("lock record no longer ours (broken?)")
        except OSError as exc:
            logger.warning("lock release failed: %s", exc)

    def __enter__(self) -> LockHandle:
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def _pid_alive(pid: int) -> bool:
    """Best-effort alive check. On POSIX, signal 0 to the pid."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Another user's process — treat as alive (don't steal their lock).
        return True
    except OSError:
        return False


def _read_lock_record_fd(fd: int) -> dict[str, object]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 65536)
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_lock_record_path(path: Path) -> dict[str, object]:
    try:
        parsed = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_lock_record_fd(fd: int, record: dict[str, object]) -> None:
    payload = json.dumps(record).encode("utf-8")
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, payload)
    os.fsync(fd)


def _warn_best_effort_lock_once() -> None:
    global _BEST_EFFORT_LOCK_WARNING_EMITTED
    if _BEST_EFFORT_LOCK_WARNING_EMITTED:
        return
    logger.warning(
        "kernel-level lock unavailable on this platform; using best-effort file lock with PID check"
    )
    _BEST_EFFORT_LOCK_WARNING_EMITTED = True


def acquire_project_lock(
    project_dir: Path,
    command: str,
    *,
    break_stale: bool = True,
) -> LockHandle:
    """Acquire exclusive project lock. Returns a LockHandle (context manager).

    Raises LockBusy if another process holds the project lock. On Unix this is
    backed by a kernel flock. On platforms without `fcntl`, it falls back to a
    best-effort PID check around `O_EXCL`; that path has a check-then-act race.
    """
    root = logs_dir(project_dir)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / LOCK_FILE_NAME
    has_kernel_lock = fcntl is not None
    if not has_kernel_lock:
        _warn_best_effort_lock_once()

    if has_kernel_lock:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            raise LockBusy({})

        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                holder = _read_lock_record_fd(fd)
                raise LockBusy(holder)
            except OSError:
                holder = _read_lock_record_fd(fd)
                raise LockBusy(holder)

            holder = _read_lock_record_fd(fd)
            holder_pid = int(holder.get("pid", 0) or 0)
            if holder and break_stale:
                logger.warning("stale lock from pid=%s (flock released) — reusing lock file", holder_pid)
            elif holder and not break_stale:
                raise LockBusy(holder)

            handle = LockHandle(
                _path=lock_path,
                _fd=fd,
                _pid=os.getpid(),
                _started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                _command=command,
                _nonce=uuid.uuid4().hex,
            )
            _write_lock_record_fd(fd, {
                "pid": handle._pid,
                "started_at": handle._started_at,
                "command": handle._command,
                "nonce": handle._nonce,
                "session_id": None,
            })
            return handle
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    holder: dict = {}
    for attempt in range(8):
        fd: int | None = None
        try:
            fd = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_RDWR,
                0o644,
            )
        except FileExistsError:
            try:
                fd = os.open(lock_path, os.O_RDWR)
            except FileNotFoundError:
                continue
            except OSError:
                holder = {}
                raise LockBusy(holder)

            holder = _read_lock_record_fd(fd)
            holder_pid = int(holder.get("pid", 0) or 0)
            if not break_stale:
                os.close(fd)
                raise LockBusy(holder)

            if _pid_alive(holder_pid):
                os.close(fd)
                raise LockBusy(holder)

            logger.warning(
                "stale lock from pid=%s (best-effort pid check) — breaking",
                holder_pid,
            )
            os.close(fd)
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                continue
            except OSError:
                raise LockBusy(holder)
            continue

        handle = LockHandle(
            _path=lock_path,
            _fd=fd,
            _pid=os.getpid(),
            _started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            _command=command,
            _nonce=uuid.uuid4().hex,
        )
        try:
            _write_lock_record_fd(fd, {
                "pid": handle._pid,
                "started_at": handle._started_at,
                "command": handle._command,
                "nonce": handle._nonce,
                "session_id": None,
            })
            if not has_kernel_lock:
                os.close(fd)
                handle._fd = None
        except OSError:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if not has_kernel_lock:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
            raise
        return handle

    raise LockBusy(holder)


def break_project_lock(project_dir: Path) -> dict[str, object]:
    """Force-remove the project lock and return the prior holder record."""
    lock_path = logs_dir(project_dir) / LOCK_FILE_NAME
    if not lock_path.exists():
        return {}

    if fcntl is not None:
        try:
            fd = os.open(lock_path, os.O_RDWR)
        except FileNotFoundError:
            return {}
        try:
            holder = _read_lock_record_fd(fd)
            holder_pid = int(holder.get("pid", 0) or 0)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                if _pid_alive(holder_pid):
                    raise LockBreakError(
                        f"Refusing to break a live lock held by pid={holder_pid} "
                        f"command={holder.get('command')!r}."
                    ) from exc
                raise LockBreakError(
                    f"Refusing to break lock {lock_path}: flock is still held "
                    f"and pid={holder_pid} could not be proven dead."
                ) from exc

            if holder_pid and _pid_alive(holder_pid):
                raise LockBreakError(
                    f"Refusing to break a live lock held by pid={holder_pid} "
                    f"command={holder.get('command')!r}."
                )

            _write_lock_record_fd(fd, {})
            return holder
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    holder = _read_lock_record_path(lock_path)
    holder_pid = int(holder.get("pid", 0) or 0)
    if holder_pid and _pid_alive(holder_pid):
        raise LockBreakError(
            f"Refusing to break a live lock held by pid={holder_pid} "
            f"command={holder.get('command')!r}."
        )

    try:
        lock_path.unlink()
    except OSError as exc:
        logger.warning("manual lock break failed: %s", exc)
        raise
    return holder


@contextmanager
def project_lock(
    project_dir: Path,
    command: str,
    *,
    break_lock: bool = False,
) -> Iterator[LockHandle]:
    """Context-manager convenience wrapper around acquire_project_lock."""
    if break_lock:
        holder = break_project_lock(project_dir)
        logger.warning("project lock manually cleared for %s: %s", command, holder)
    handle = acquire_project_lock(project_dir, command)
    try:
        yield handle
    finally:
        handle.release()
