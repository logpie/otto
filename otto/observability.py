"""Best-effort helpers for Otto observability artifacts."""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("otto.observability")


def append_text_log(
    path: Path,
    lines: str | Iterable[str],
    *,
    retries: int = 0,
    strict: bool = False,
) -> None:
    """Append human-readable text to a log file.

    Best-effort: logs and swallows I/O failures so observability writes never
    break the caller's main path. The log message names the file so a silent
    write failure is still discoverable in the debug log.
    """
    if isinstance(lines, str):
        text = lines
    else:
        text = "\n".join(str(line) for line in lines)
    attempts = retries + 1
    last_exc: OSError | None = None
    for _ in range(attempts):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(text)
                if not text.endswith("\n"):
                    handle.write("\n")
            return
        except OSError as exc:
            last_exc = exc
            logger.warning("append_text_log(%s) failed: %s", path, exc)
    if strict and last_exc is not None:
        raise last_exc


def write_json_file(path: Path, data: Any, *, strict: bool = False) -> None:
    """Write readable JSON to a file."""
    try:
        write_json_atomic(path, data)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("write_json_file(%s) failed: %s", path, exc)
        if strict:
            raise


def write_text_atomic(path: Path, text: str) -> None:
    """Atomically replace a UTF-8 text file on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = text.encode("utf-8")
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    dir_fd: int | None = None
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        if os.name == "posix" and hasattr(os, "O_DIRECTORY"):
            dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            os.fsync(dir_fd)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


def write_json_atomic(path: Path, data: Any) -> None:
    """Atomically replace a JSON file on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    dir_fd: int | None = None
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        if os.name == "posix" and hasattr(os, "O_DIRECTORY"):
            dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            os.fsync(dir_fd)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        if dir_fd is not None:
            os.close(dir_fd)
