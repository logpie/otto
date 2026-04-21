"""Otto log paths — single choke point for all `otto_logs/` writes and reads.

New layout (one dir per invocation):

    otto_logs/
    ├── sessions/<session_id>/
    │   ├── intent.txt
    │   ├── summary.json
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

Legacy layout (pre-restructure) is still readable. Writers go through this
module exclusively — no `otto_logs/...` literals elsewhere.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("otto.paths")

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

# Legacy (pre-restructure) path constants — reader fallback only.
LEGACY_CHECKPOINT = "checkpoint.json"
LEGACY_RUN_HISTORY = "run-history.jsonl"
LEGACY_CERTIFIER_MEMORY = "certifier-memory.jsonl"
LEGACY_RUNS_DIR = "runs"
LEGACY_BUILDS_DIR = "builds"
LEGACY_CERTIFIER_DIR = "certifier"
LEGACY_IMPROVE_DIR = "improve"
LEGACY_ROUNDS_DIR = "rounds"
LEGACY_IMPROVEMENT_REPORT = "improvement-report.md"


def logs_dir(project_dir: Path) -> Path:
    """Return the otto_logs directory for a project."""
    return Path(project_dir) / LOGS_ROOT_NAME


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


def session_checkpoint(project_dir: Path, session_id: str) -> Path:
    """Per-session resume checkpoint (only exists while in-flight/paused)."""
    return session_dir(project_dir, session_id) / "checkpoint.json"


def session_summary(project_dir: Path, session_id: str) -> Path:
    """Post-run summary (permanent record: verdict, cost, duration, status)."""
    return session_dir(project_dir, session_id) / "summary.json"


def session_intent(project_dir: Path, session_id: str) -> Path:
    """Archival copy of the intent at session start (project-root
    intent.md is the runtime contract)."""
    return session_dir(project_dir, session_id) / "intent.txt"


def legacy_checkpoint(project_dir: Path) -> Path:
    return logs_dir(project_dir) / LEGACY_CHECKPOINT


def legacy_run_history(project_dir: Path) -> Path:
    return logs_dir(project_dir) / LEGACY_RUN_HISTORY


def legacy_certifier_memory(project_dir: Path) -> Path:
    return logs_dir(project_dir) / LEGACY_CERTIFIER_MEMORY


def ensure_session_scaffold(project_dir: Path, session_id: str) -> Path:
    """Create the session dir and its phase subdirs. Idempotent."""
    sess = session_dir(project_dir, session_id)
    for sub in (sess, sess / "spec", sess / "build", sess / "certify", sess / "improve"):
        sub.mkdir(parents=True, exist_ok=True)
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


def set_pointer(project_dir: Path, name: str, session_id: str) -> None:
    """Atomically point `name` at sessions/<session_id>.

    Prefers an OS symlink. Falls back to a `.txt` pointer file if symlinks
    are unsupported (Windows without admin, some CI volumes). Never raises
    — a failure logs at WARNING and leaves the previous pointer (if any)
    intact for the non-fallback path; the fallback path explicitly removes
    any stale symlink before writing the `.txt`.
    """
    root = logs_dir(project_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = f"{SESSIONS_DIR_NAME}/{session_id}"
    link = _pointer_link(root, name)
    tmp_link = root / f".{name}.link.tmp"

    try:
        # Clean any stale temp from a prior crash.
        try:
            if tmp_link.is_symlink() or tmp_link.exists():
                tmp_link.unlink()
        except OSError:
            pass
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

    tmp_txt = root / f".{name}.txt.tmp"
    try:
        tmp_txt.write_text(session_id)
        os.replace(tmp_txt, _pointer_text_file(root, name))
    except OSError as exc:
        logger.warning("pointer .txt fallback also failed for %s: %s", name, exc)


def resolve_pointer(project_dir: Path, name: str) -> Path | None:
    """Resolve `name` pointer to an absolute session directory.

    Try in order:
      1. symlink at otto_logs/<name>
      2. pointer file at otto_logs/<name>.txt
      3. (paused only) scan sessions/*/checkpoint.json for resumable state.

    Returns absolute session dir path, or None if no pointer resolves.
    """
    root = logs_dir(project_dir)
    if not root.exists():
        return None

    link = _pointer_link(root, name)
    try:
        if link.is_symlink():
            return link.resolve(strict=False)
    except OSError:
        pass

    txt = _pointer_text_file(root, name)
    if txt.exists():
        try:
            sid = txt.read_text().strip()
            if sid:
                sess = session_dir(project_dir, sid)
                if sess.exists():
                    return sess
        except OSError:
            pass

    # Scan fallback — only for `paused` because it must never strand a
    # resumable run. Covers both clean-pause (status=paused) and hard-crash
    # (status=in_progress). Prefer paused; tie-break by newest updated_at.
    if name == PAUSED_POINTER:
        candidates: list[tuple[int, float, Path]] = []
        for cp in sessions_root(project_dir).glob("*/checkpoint.json"):
            try:
                data = json.loads(cp.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            status = data.get("status", "")
            if status not in ("paused", "in_progress"):
                continue
            updated = data.get("updated_at", "") or data.get("started_at", "")
            if updated:
                try:
                    updated_ts = datetime.fromisoformat(
                        updated.replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    updated_ts = cp.stat().st_mtime
            else:
                try:
                    updated_ts = cp.stat().st_mtime
                except OSError:
                    updated_ts = 0.0
            # Sort key: 0 for paused (higher priority), 1 for in_progress;
            # then newest updated_ts (negative so smaller sorts first).
            priority = 0 if status == "paused" else 1
            candidates.append((priority, -updated_ts, cp.parent))
        if candidates:
            candidates.sort()
            return candidates[0][2]

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
        self.holder = holder
        super().__init__(
            f"project lock held by pid={holder.get('pid')!r} "
            f"command={holder.get('command')!r} "
            f"started_at={holder.get('started_at')!r}"
        )


@dataclass
class LockHandle:
    """Returned by acquire_project_lock. Use as a context manager."""

    _path: Path
    _pid: int
    _started_at: str
    _command: str
    _session_id: str | None = None

    def set_session_id(self, session_id: str) -> None:
        """Update the lock record once session_id is allocated."""
        self._session_id = session_id
        self._write_record()

    def _write_record(self) -> None:
        record = {
            "pid": self._pid,
            "started_at": self._started_at,
            "command": self._command,
            "session_id": self._session_id,
        }
        tmp = self._path.with_suffix(".lock.tmp")
        try:
            tmp.write_text(json.dumps(record))
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("lock record write failed: %s", exc)

    def release(self) -> None:
        """Remove the lock file. Idempotent."""
        try:
            if self._path.exists():
                self._path.unlink()
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


def acquire_project_lock(
    project_dir: Path,
    command: str,
    *,
    break_stale: bool = True,
) -> LockHandle:
    """Acquire exclusive project lock. Returns a LockHandle (context manager).

    Raises LockBusy if another live PID holds the lock. If the holder PID is
    gone and `break_stale=True`, auto-releases the stale lock with a WARN.

    Called BEFORE session_id allocation. Caller updates the record via
    `handle.set_session_id(sid)` once the ID is chosen.
    """
    root = logs_dir(project_dir)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / LOCK_FILE_NAME

    if lock_path.exists():
        try:
            holder = json.loads(lock_path.read_text())
        except (OSError, json.JSONDecodeError):
            holder = {}
        holder_pid = int(holder.get("pid", 0) or 0)
        if holder_pid and _pid_alive(holder_pid):
            raise LockBusy(holder)
        if break_stale:
            logger.warning(
                "stale lock from pid=%s (gone) — breaking", holder_pid
            )
            try:
                lock_path.unlink()
            except OSError:
                pass
        else:
            raise LockBusy(holder)

    handle = LockHandle(
        _path=lock_path,
        _pid=os.getpid(),
        _started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        _command=command,
    )
    handle._write_record()
    return handle


@contextmanager
def project_lock(project_dir: Path, command: str) -> Iterator[LockHandle]:
    """Context-manager convenience wrapper around acquire_project_lock."""
    handle = acquire_project_lock(project_dir, command)
    try:
        yield handle
    finally:
        handle.release()
