"""Canonical live run registry."""

from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from otto import paths
from otto.runs.schema import RunRecord, is_terminal_status

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows best effort
    fcntl = None


RUN_ID_MAX_ATTEMPTS = 64
HEARTBEAT_INTERVAL_S = 2.0
_PROCESS_START_TIME_NS = int(time.time() * 1_000_000_000)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def allocate_run_id(project_dir: Path) -> str:
    """Reserve a unique run id without taking the project lock."""
    live_dir = paths.live_runs_dir(project_dir)
    live_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(RUN_ID_MAX_ATTEMPTS):
        now = datetime.now(timezone.utc)
        run_id = f"{now:%Y-%m-%d-%H%M%S}-{secrets.token_hex(3)}"
        reserve_path = _reservation_path(project_dir, run_id)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(reserve_path, flags, 0o644)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"run_id": run_id, "reserved_at": utc_now_iso()}) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                os.unlink(reserve_path)
            except OSError:
                pass
            raise
        return run_id
    raise RuntimeError(f"could not allocate unique run_id after {RUN_ID_MAX_ATTEMPTS} tries")


def write_record(project_dir: Path, record: RunRecord | dict[str, Any]) -> RunRecord:
    """Atomically write a complete live record."""
    parsed = _coerce_record(record)
    _normalize_record_before_write(parsed, heartbeat=True)
    path = paths.live_run_path(project_dir, parsed.run_id)
    _atomic_write_json(path, parsed.to_dict())
    try:
        _reservation_path(project_dir, parsed.run_id).unlink(missing_ok=True)
    except OSError:
        pass
    return parsed


def update_record(
    project_dir: Path,
    run_id: str,
    updates: dict[str, Any] | None = None,
    *,
    heartbeat: bool = True,
    mutate: Callable[[RunRecord], None] | None = None,
) -> RunRecord:
    """Atomically read-modify-write one live record."""
    record = _read_record_path(paths.live_run_path(project_dir, run_id))
    if updates:
        _merge_dict(record, updates)
    if mutate is not None:
        mutate(record)
    record.version = int(record.version or 0) + 1
    _normalize_record_before_write(record, heartbeat=heartbeat)
    _atomic_write_json(paths.live_run_path(project_dir, run_id), record.to_dict())
    return record


def finalize_record(
    project_dir: Path,
    run_id: str,
    *,
    status: str,
    terminal_outcome: str | None = None,
    updates: dict[str, Any] | None = None,
) -> RunRecord:
    if not is_terminal_status(status):
        raise ValueError(f"final status must be terminal, got {status!r}")

    def _mutate(record: RunRecord) -> None:
        record.status = status
        record.terminal_outcome = terminal_outcome
        now = utc_now_iso()
        record.timing["finished_at"] = record.timing.get("finished_at") or now
        if updates:
            _merge_dict(record, updates)

    return update_record(project_dir, run_id, heartbeat=True, mutate=_mutate)


def read_live_records(project_dir: Path) -> list[RunRecord]:
    live_dir = paths.live_runs_dir(project_dir)
    if not live_dir.exists():
        return []
    records: list[RunRecord] = []
    with _directory_scan_lock(project_dir, exclusive=False):
        for path in sorted(live_dir.glob("*.json")):
            try:
                records.append(_read_record_path(path))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
    return records


def garbage_collect_live_records(
    project_dir: Path,
    *,
    terminal_retention_s: float = 24 * 60 * 60,
    reserve_retention_s: float = 60 * 60,
    now: datetime | None = None,
) -> list[str]:
    """Remove old terminal live records and stale reservations."""
    now_dt = now or datetime.now(timezone.utc)
    removed: list[str] = []
    live_dir = paths.live_runs_dir(project_dir)
    if not live_dir.exists():
        return removed
    with _directory_scan_lock(project_dir, exclusive=True):
        for path in sorted(live_dir.glob("*.json")):
            try:
                record = _read_record_path(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not is_terminal_status(record.status):
                continue
            if not writer_identity_gone_or_stale(record.writer):
                continue
            finished_at = _parse_iso(record.timing.get("finished_at") or record.timing.get("updated_at"))
            if finished_at is None:
                continue
            if (now_dt - finished_at).total_seconds() < terminal_retention_s:
                continue
            _append_tombstone(project_dir, record)
            path.unlink(missing_ok=True)
            removed.append(record.run_id)
        for reserve_path in live_dir.glob("*.reserve"):
            try:
                age_s = now_dt.timestamp() - reserve_path.stat().st_mtime
            except OSError:
                continue
            if age_s >= reserve_retention_s:
                reserve_path.unlink(missing_ok=True)
    return removed


def cleanup_live_record(project_dir: Path, run_id: str) -> RunRecord:
    """Remove one terminal or abandoned live record and append a GC tombstone."""
    path = paths.live_run_path(project_dir, run_id)
    with _directory_scan_lock(project_dir, exclusive=True):
        if not path.exists():
            raise FileNotFoundError(run_id)
        record = _read_record_path(path)
        if not writer_identity_gone_or_stale(record.writer):
            raise ValueError("writer still alive — wait for finalization")
        if not is_terminal_status(record.status):
            record.last_event = record.last_event or "cleaned up stale abandoned run"
        _append_tombstone(project_dir, record)
        path.unlink(missing_ok=True)
        return record


def load_live_record(project_dir: Path, run_id: str) -> RunRecord:
    path = paths.live_run_path(project_dir, run_id)
    if not path.exists():
        raise FileNotFoundError(run_id)
    return _read_record_path(path)


def current_writer_identity(writer_id: str) -> dict[str, Any]:
    start_time_ns = _PROCESS_START_TIME_NS
    try:
        import psutil

        start_time_ns = int(psutil.Process(os.getpid()).create_time() * 1_000_000_000)
    except Exception:
        pass
    try:
        pgid = os.getpgid(0)
    except Exception:
        pgid = os.getpid()
    return {
        "pid": os.getpid(),
        "pgid": pgid,
        "writer_id": writer_id,
        "boot_id": _boot_id(),
        "process_start_time_ns": start_time_ns,
    }


def writer_identity_matches_live_process(writer: dict[str, Any]) -> bool:
    if not writer:
        return False
    pid = writer.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    expected_pgid = writer.get("pgid")
    expected_start_ns = writer.get("process_start_time_ns")
    expected_boot_id = str(writer.get("boot_id") or "").strip()
    if expected_boot_id:
        current_boot_id = _boot_id()
        if current_boot_id and current_boot_id != expected_boot_id:
            return False
    try:
        import psutil

        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return False
        if isinstance(expected_start_ns, int):
            try:
                actual_start_ns = int(proc.create_time() * 1_000_000_000)
            except (psutil.NoSuchProcess, ProcessLookupError):
                return False
            if abs(actual_start_ns - expected_start_ns) > 100_000_000:
                return False
        if isinstance(expected_pgid, int):
            try:
                actual_pgid = os.getpgid(pid)
            except ProcessLookupError:
                return False
            if actual_pgid != expected_pgid:
                return False
        return proc.is_running()
    except ImportError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        if isinstance(expected_pgid, int):
            try:
                if os.getpgid(pid) != expected_pgid:
                    return False
            except ProcessLookupError:
                return False
        return True


def writer_identity_gone_or_stale(writer: dict[str, Any]) -> bool:
    if not writer:
        return True
    return not writer_identity_matches_live_process(writer)


def make_run_record(
    *,
    project_dir: Path,
    run_id: str,
    domain: str,
    run_type: str,
    command: str,
    display_name: str,
    status: str,
    cwd: Path | None = None,
    writer_id: str | None = None,
    identity: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
    git: dict[str, Any] | None = None,
    intent: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    adapter_key: str | None = None,
    last_event: str = "",
) -> RunRecord:
    writer_id = writer_id or f"{domain}:{run_id}"
    return RunRecord(
        run_id=run_id,
        domain=domain,
        run_type=run_type,
        command=command,
        display_name=display_name,
        status=status,
        project_dir=str(Path(project_dir).resolve(strict=False)),
        cwd=str(Path(cwd or project_dir).resolve(strict=False)),
        writer=current_writer_identity(writer_id),
        identity=dict(identity or {}),
        source=dict(source or {}),
        timing=base_timing(),
        git=dict(git or {}),
        intent=dict(intent or {}),
        artifacts=dict(artifacts or {}),
        metrics=dict(metrics or {}),
        adapter_key=adapter_key or f"{domain}.{run_type}",
        last_event=last_event,
    )


def publisher_for(
    domain: str,
    run_type: str,
    command: str,
    *,
    project_dir: Path,
    run_id: str,
    intent: str,
    display_name: str | None = None,
    cwd: Path | None = None,
    identity: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
    git: dict[str, Any] | None = None,
    intent_meta: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    adapter_key: str | None = None,
    last_event: str = "starting",
    heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
) -> "RunPublisher":
    from otto.history import normalize_command_label

    command_label = normalize_command_label(command)
    record = make_run_record(
        project_dir=project_dir,
        run_id=run_id,
        domain=domain,
        run_type=run_type,
        command=command,
        display_name=display_name or f"{command_label}: {intent[:80]}".strip(),
        status="running",
        cwd=cwd or project_dir,
        identity={
            "queue_task_id": os.environ.get("OTTO_QUEUE_TASK_ID"),
            "merge_id": None,
            "parent_run_id": None,
            **dict(identity or {}),
        },
        source={
            "invoked_via": "queue" if os.environ.get("OTTO_INTERNAL_QUEUE_RUNNER") == "1" else "cli",
            "argv": list(sys.argv[1:]),
            "resumable": run_type != "certify" and domain != "merge",
            **dict(source or {}),
        },
        git=dict(git or {}),
        intent={
            "summary": intent[:200],
            "intent_path": None,
            "spec_path": None,
            **dict(intent_meta or {}),
        },
        artifacts=dict(artifacts or {}),
        metrics=dict(metrics or {}),
        adapter_key=adapter_key or f"{domain}.{run_type}",
        last_event=last_event,
    )
    return RunPublisher(
        project_dir,
        record,
        heartbeat_interval_s=heartbeat_interval_s,
    )


def base_timing(*, started_at: str | None = None) -> dict[str, Any]:
    now = started_at or utc_now_iso()
    return {
        "started_at": now,
        "updated_at": now,
        "heartbeat_at": now,
        "finished_at": None,
        "duration_s": 0.0,
        "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
        "heartbeat_seq": 0,
    }


def append_jsonl_row(path: Path, row: dict[str, Any]) -> dict[str, Any]:
    """Append one JSONL row with flock + fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(row, separators=(",", ":"), sort_keys=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return dict(row)


def append_command_ack(
    ack_path: Path,
    cmd: dict[str, Any],
    *,
    writer_id: str,
    outcome: str = "applied",
    state_version: int | None = None,
    note: str | None = None,
) -> dict[str, Any] | None:
    command_id = str(cmd.get("command_id") or "").strip()
    if not command_id:
        return None
    return append_jsonl_row(
        ack_path,
        {
            "schema_version": 1,
            "command_id": command_id,
            "run_id": cmd.get("run_id"),
            "acked_at": utc_now_iso(),
            "writer_id": writer_id,
            "outcome": outcome,
            "state_version": state_version,
            "note": note,
        },
    )


def read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        return []
    return rows


def load_command_ack_ids(ack_path: Path) -> set[str]:
    return {
        str(row.get("command_id") or "")
        for row in read_jsonl_rows(ack_path)
        if row.get("command_id")
    }


def begin_command_drain(
    request_path: Path,
    processing_path: Path,
    ack_path: Path,
) -> list[dict[str, Any]]:
    """Rename request log to `.processing` and return unacked command rows."""
    request_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = request_path.with_suffix(request_path.suffix + ".lock")
    with lock_path.open("a", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            if processing_path.exists():
                if request_path.exists():
                    with processing_path.open("a", encoding="utf-8") as proc:
                        proc.write(request_path.read_text(encoding="utf-8"))
                        proc.flush()
                        os.fsync(proc.fileno())
                    request_path.unlink()
            elif request_path.exists():
                request_path.rename(processing_path)
            else:
                return []
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    acked = load_command_ack_ids(ack_path)
    commands: list[dict[str, Any]] = []
    try:
        lines = processing_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return commands
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        command_id = str(value.get("command_id") or "")
        if command_id and command_id in acked:
            continue
        commands.append(value)
    return commands


def finish_command_drain(processing_path: Path) -> None:
    processing_path.unlink(missing_ok=True)


class RunPublisher:
    """Small single-process helper for 2s live-record heartbeats."""

    def __init__(
        self,
        project_dir: Path,
        record: RunRecord,
        *,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        patch_getter: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.project_dir = project_dir
        self.record = record
        self.heartbeat_interval_s = heartbeat_interval_s
        self.patch_getter = patch_getter
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._finalized = False

    def __enter__(self) -> "RunPublisher":
        write_record(self.project_dir, self.record)
        self._thread = threading.Thread(
            target=self._loop,
            name=f"otto-run-heartbeat-{self.record.run_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.stop()

    def update(self, updates: dict[str, Any], *, heartbeat: bool = True) -> RunRecord:
        with self._lock:
            if self._finalized:
                return self.record
            self.record = update_record(
                self.project_dir,
                self.record.run_id,
                updates,
                heartbeat=heartbeat,
            )
        return self.record

    def finalize(
        self,
        *,
        status: str,
        terminal_outcome: str | None,
        updates: dict[str, Any] | None = None,
    ) -> RunRecord:
        with self._lock:
            if self._finalized:
                return self.record
            self._finalized = True
        self.stop()
        with self._lock:
            self.record = finalize_record(
                self.project_dir,
                self.record.run_id,
                status=status,
                terminal_outcome=terminal_outcome,
                updates=updates,
            )
        return self.record

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval_s):
            try:
                with self._lock:
                    if self._finalized:
                        return
                    updates = self.patch_getter() if self.patch_getter is not None else {}
                    self.record = update_record(
                        self.project_dir,
                        self.record.run_id,
                        updates or {},
                        heartbeat=True,
                    )
            except Exception:
                continue


def _coerce_record(record: RunRecord | dict[str, Any]) -> RunRecord:
    if isinstance(record, RunRecord):
        return record
    return RunRecord.from_dict(record)


def _normalize_record_before_write(record: RunRecord, *, heartbeat: bool) -> None:
    now = utc_now_iso()
    if not record.timing:
        record.timing = base_timing(started_at=now)
    record.timing.setdefault("started_at", now)
    record.timing["updated_at"] = now
    record.timing.setdefault("heartbeat_interval_s", HEARTBEAT_INTERVAL_S)
    record.timing.setdefault("finished_at", None)
    if heartbeat:
        record.timing["heartbeat_at"] = now
        record.timing["heartbeat_seq"] = int(record.timing.get("heartbeat_seq") or 0) + 1
    if not record.writer:
        record.writer = current_writer_identity(f"{record.domain}:{record.run_id}")
    started = _parse_iso(record.timing.get("started_at"))
    if started is not None:
        end = _parse_iso(record.timing.get("finished_at")) if record.timing.get("finished_at") else None
        reference = end or datetime.now(timezone.utc)
        record.timing["duration_s"] = max(0.0, round((reference - started).total_seconds(), 1))


def _merge_dict(record: RunRecord, updates: dict[str, Any]) -> None:
    data = record.to_dict()
    _merge_mapping(data, updates)
    updated = RunRecord.from_dict(data)
    record.__dict__.update(updated.__dict__)


def _merge_mapping(target: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_mapping(target[key], value)
        else:
            target[key] = deepcopy(value)


def _read_record_path(path: Path) -> RunRecord:
    return RunRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=False))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _reservation_path(project_dir: Path, run_id: str) -> Path:
    return paths.live_runs_dir(project_dir) / f"{run_id}.reserve"


@contextmanager
def _directory_scan_lock(project_dir: Path, *, exclusive: bool):
    lock_path = paths.runs_dir(project_dir) / ".scan.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _append_tombstone(project_dir: Path, record: RunRecord) -> None:
    append_jsonl_row(
        paths.run_gc_tombstones_jsonl(project_dir),
        {
            "schema_version": 1,
            "run_id": record.run_id,
            "removed_at": utc_now_iso(),
            "status": record.status,
            "terminal_outcome": record.terminal_outcome,
            "live_record_path": str(paths.live_run_path(project_dir, record.run_id)),
        },
    )


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
