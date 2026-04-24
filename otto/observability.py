"""Best-effort helpers for Otto observability artifacts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("otto.observability")


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


def iso_timestamp() -> str:
    """Return an RFC3339/ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_json_file(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def dirty_worktree_files(project_dir: Path, *, limit: int = 20) -> list[str]:
    """Return the first N paths from `git status --porcelain`."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    files: list[str] = []
    for line in (result.stdout or "").splitlines():
        if not line.strip():
            continue
        raw_path = line[3:] if len(line) > 3 else line
        path = raw_path.strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            files.append(path)
        if len(files) >= limit:
            break
    return files


def gather_runtime_metadata(project_dir: Path) -> dict[str, Any]:
    """Capture the execution environment for session forensics."""
    def _git(args: list[str]) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return (result.stdout or "").strip() if result.returncode == 0 else ""

    path_head = os.environ.get("PATH", "")
    if len(path_head) > 200:
        path_head = path_head[:200] + "..."

    return {
        "otto_version": "",
        "otto_commit": _git(["rev-parse", "--short", "HEAD"]),
        "python_version": sys.version.split()[0],
        "platform": f"{platform.system().lower()} {platform.release()} {platform.machine()}",
        "sys_executable": sys.executable,
        "cwd": str(project_dir),
        "git_branch": _git(["branch", "--show-current"]),
        "git_commit": _git(["rev-parse", "--short", "HEAD"]),
        "env_fingerprint": {
            "PATH_head": path_head,
            "VIRTUAL_ENV": os.environ.get("VIRTUAL_ENV"),
            "PYTHONPATH": os.environ.get("PYTHONPATH"),
            "CI": os.environ.get("CI"),
        },
    }


def write_runtime_metadata(session_dir: Path, runtime: dict[str, Any]) -> Path:
    path = session_dir / "runtime.json"
    write_json_atomic(path, runtime)
    return path


def write_attempt_history(path: Path, attempts: list[dict[str, Any]]) -> Path:
    write_json_atomic(path, attempts)
    return path


def load_attempt_history(path: Path) -> list[dict[str, Any]]:
    data = load_json_file(path)
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def save_rendered_prompt(prompts_dir: Path, *, template: str, rendered_text: str) -> dict[str, str]:
    """Persist rendered prompt text for audit and return provenance entry."""
    prompts_dir.mkdir(parents=True, exist_ok=True)
    digest = sha256_text(rendered_text)
    stem = Path(template).stem or "prompt"
    suffix = Path(template).suffix or ".md"
    rendered_path = prompts_dir / f"{stem}-{digest[:12]}{suffix}"
    write_text_atomic(rendered_path, rendered_text)
    return {
        "template": template,
        "rendered_sha256": digest,
        "rendered_path": str(rendered_path),
    }


def update_input_provenance(
    session_dir: Path,
    *,
    intent: dict[str, Any] | None = None,
    spec: dict[str, Any] | None = None,
    prompts: list[dict[str, Any]] | None = None,
) -> Path:
    """Merge additive provenance data into `input-provenance.json`."""
    path = session_dir / "input-provenance.json"
    existing = load_json_file(path)
    data = existing if isinstance(existing, dict) else {}
    data.setdefault("intent", {"source": "", "fallback_reason": "", "resolved_text": "", "sha256": ""})
    data.setdefault("spec", {"source": "none", "path": "", "sha256": ""})
    data.setdefault("prompts", [])

    if isinstance(intent, dict):
        data["intent"].update({k: v for k, v in intent.items() if v is not None})
    if isinstance(spec, dict):
        data["spec"].update({k: v for k, v in spec.items() if v is not None})
    if prompts:
        seen = {
            (
                str(item.get("template") or ""),
                str(item.get("rendered_sha256") or ""),
                str(item.get("rendered_path") or ""),
            )
            for item in data["prompts"]
            if isinstance(item, dict)
        }
        for entry in prompts:
            if not isinstance(entry, dict):
                continue
            key = (
                str(entry.get("template") or ""),
                str(entry.get("rendered_sha256") or ""),
                str(entry.get("rendered_path") or ""),
            )
            if key in seen:
                continue
            data["prompts"].append(entry)
            seen.add(key)

    write_json_atomic(path, data)
    return path


def write_crash_artifact(session_dir: Path, payload: dict[str, Any]) -> Path:
    path = session_dir / "crash.json"
    write_json_atomic(path, payload)
    return path
