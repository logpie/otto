"""Cross-session history helpers."""

from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path
from typing import Any

from otto import paths
from otto.observability import append_text_log
from otto.redaction import redact_text


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
    """Append one normalized entry to cross-session history."""
    history_path = paths.history_jsonl(project_dir)
    history_path.parent.mkdir(parents=True, exist_ok=True)

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

    append_text_log(
        history_path,
        [json.dumps(payload, separators=(",", ":"))],
        retries=1,
    )
    return payload


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
