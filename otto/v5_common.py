"""Shared low-level helpers used across the v5 module family.

These exist to deduplicate four near-identical helpers that previously lived
in three to five separate v5_*.py files. Extracted in the simplification audit
(see archive/audits/audit-v5.md).
"""

from __future__ import annotations

import copy
import subprocess
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


def git_capture(worktree: Path, args: list[str], *, timeout: int = 10) -> str:
    """Run ``git <args>`` in ``worktree`` and return stripped stdout.

    Returns "" on any error (non-zero exit, OS error, timeout). Used for
    read-only git probing where the caller treats no-output and error the same.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def iso_now() -> str:
    """UTC timestamp in ``YYYY-MM-DDTHH:MM:SSZ`` form, suitable for journals."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def coerce_spec(spec: Any) -> dict[str, Any]:
    """Coerce a Spec dataclass or dict into a JSON-shaped dict (deep-copied).

    Callers often mutate the returned dict, so we deepcopy to protect the
    source. Returns {} for non-spec inputs (None, primitives, classes).
    """
    if isinstance(spec, dict):
        return copy.deepcopy(spec)
    if is_dataclass(spec) and not isinstance(spec, type):
        payload = asdict(spec)
        return payload if isinstance(payload, dict) else {}
    return {}


def read_text(path: Path) -> str:
    """Best-effort UTF-8 read; returns "" on OS or decode error."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
