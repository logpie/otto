"""Cross-session history helpers."""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from typing import Any

from otto import paths
from otto.redaction import redact_text
from otto.runs.history import append_history_snapshot


def history_run_id(entry: dict[str, Any]) -> str:
    """Return the canonical run identifier for a history entry."""
    return str(
        entry.get("run_id")
        or entry.get("session_id")
        or entry.get("build_id")
        or ""
    ).strip()


def normalize_command_label(command: str | None) -> str:
    """Normalize dotted command ids to a stable human-readable label."""
    raw = str(command or "").strip()
    if not raw:
        return "build"
    if raw.startswith("improve."):
        return f"improve {raw.split('.', 1)[1]}".strip()
    return raw.replace(".", " ")


def command_family(command: str | None) -> str:
    """Collapse concrete commands into build/certify/improve families."""
    label = normalize_command_label(command)
    head = label.split(" ", 1)[0].strip().lower()
    return head or "build"


def append_history_entry(project_dir: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Append one normalized terminal snapshot to cross-session history."""
    run_id = history_run_id(entry)
    payload = dict(entry)
    payload["run_id"] = run_id
    payload["command"] = normalize_command_label(payload.get("command"))
    payload["timestamp"] = payload.get("timestamp") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if run_id and "build_id" not in payload:
        payload["build_id"] = run_id
    if "certifier_mode" in payload and "mode" not in payload:
        payload["mode"] = payload["certifier_mode"]
    for key, value in list(payload.items()):
        if isinstance(value, str):
            payload[key] = redact_text(value)

    payload.setdefault("domain", "atomic")
    payload.setdefault("run_type", command_family(payload.get("command")))
    payload.setdefault("history_kind", "terminal_snapshot")
    payload.setdefault("status", "done" if payload.get("passed") else "failed")
    payload.setdefault("terminal_outcome", "success" if payload.get("passed") else "failure")
    payload.setdefault("finished_at", payload.get("timestamp"))
    payload.setdefault("started_at", payload.get("started_at"))
    payload.setdefault("queue_task_id", payload.get("queue_task_id"))
    payload.setdefault("merge_id", payload.get("merge_id"))
    payload.setdefault("branch", payload.get("branch"))
    payload.setdefault("worktree", payload.get("worktree"))
    payload.setdefault("resumable", payload.get("resumable", True))
    payload.setdefault("manifest_path", payload.get("manifest_path"))
    payload.setdefault("summary_path", payload.get("summary_path"))
    payload.setdefault("primary_log_path", payload.get("primary_log_path"))
    return append_history_snapshot(project_dir, payload, strict=True)


def tail_jsonl_entries(path: Path, *, limit: int) -> list[tuple[int, str]]:
    """Read only the last ``limit`` non-empty JSONL lines from disk."""
    lines: deque[tuple[int, str]] = deque(maxlen=max(limit, 1))
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle):
                line = raw_line.strip()
                if line:
                    lines.append((line_number, line))
    except OSError:
        return []
    return list(lines)
