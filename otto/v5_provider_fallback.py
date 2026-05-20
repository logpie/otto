"""Task-level provider fallback for v5.

When a Lead session crashes with a provider-exhausted / auth-failed error,
we re-dispatch the SAME task with the configured fallback provider. Mid-
session migration is impossible (different SDKs / session_ids), so fallback
is task-level: the task fails with verdict=catastrophic + failure_reason; the
v5_runner detects this and re-runs with a different provider.

Cost accounting accumulates across attempts via ``cost_attempts[]`` in the
task's summary.json:
    [
      {"provider": "codex", "cost_usd": 0.13, "outcome": "exhausted",
       "duration_s": 240, "started_at": "..."},
      {"provider": "claude", "cost_usd": 2.10, "outcome": "pass",
       "duration_s": 600, "started_at": "..."}
    ]

If fallback also fails, terminal verdict = catastrophic with all attempts
recorded for the user to see.

Default mapping (from otto.yaml or otto/config.py defaults):
    preferred_provider: codex-app-server
    fallback_provider: claude
    fallback_on: ["provider_exhausted", "auth_failed", "rate_limit_exhausted"]
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger("otto.v5_provider_fallback")


FallbackReason = Literal[
    "provider_exhausted",
    "auth_failed",
    "rate_limit_exhausted",
]


# Heuristic patterns for detecting fallback-eligible failures.
# Match conservatively — false positives swap providers unnecessarily.
_PATTERNS_BY_REASON: dict[FallbackReason, tuple[str, ...]] = {
    "provider_exhausted": (
        "402",
        "out of credits",
        "insufficient credits",
        "provider_exhausted",
        "Insufficient quota",
        "rate_limit_exhausted_for_period",
    ),
    "auth_failed": (
        "401",
        "Invalid API key",
        "auth_failed",
        "unauthorized",
        "AuthenticationError",
    ),
    "rate_limit_exhausted": (
        "rate_limit_exhausted",
        "RateLimitError",
    ),
}


def detect_fallback_reason(failure_message: str) -> FallbackReason | None:
    """Inspect a task's failure_reason and return a FallbackReason if it
    matches a known pattern; else None.
    """
    if not failure_message:
        return None
    msg = failure_message.lower()
    for reason, patterns in _PATTERNS_BY_REASON.items():
        for pat in patterns:
            if pat.lower() in msg:
                return reason
    return None


def should_fallback(
    failure_message: str,
    config: dict[str, Any],
) -> tuple[bool, FallbackReason | None]:
    """True iff the failure matches a configured fallback_on rule."""
    reason = detect_fallback_reason(failure_message)
    if reason is None:
        return False, None
    fallback_on = config.get("fallback_on") or [
        "provider_exhausted",
        "auth_failed",
        "rate_limit_exhausted",
    ]
    if reason in fallback_on:
        return True, reason
    return False, reason


def fallback_provider(config: dict[str, Any]) -> str | None:
    """Return the configured fallback provider, or None if not set."""
    return (
        config.get("fallback_provider")
        or (config.get("defaults", {}) or {}).get("fallback_provider")
        or "claude"  # default fallback
    )


def _summary_lock_path(summary_path: Path) -> Path:
    return summary_path.with_suffix(summary_path.suffix + ".lock")


def append_attempt(
    summary_path: Path,
    *,
    provider: str,
    cost_usd: float,
    outcome: str,
    duration_s: float,
    started_at: str | None = None,
    fallback_reason: FallbackReason | None = None,
) -> None:
    """Append a per-provider attempt entry to a session's summary.json.

    Atomic: read-modify-write is serialized with a sidecar fcntl lock because
    parallel child tasks can append to the same root session summary.
    """
    started_at = started_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not summary_path.exists():
        return  # caller should ensure summary exists; nothing to do

    lock_fd = os.open(
        str(_summary_lock_path(summary_path)),
        os.O_CREAT | os.O_RDWR,
        0o644,
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if not summary_path.exists():
            return
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return

        attempts = data.setdefault("cost_attempts", [])
        if not isinstance(attempts, list):
            attempts = []
            data["cost_attempts"] = attempts
        attempts.append({
            "provider": provider,
            "cost_usd": float(cost_usd),
            "outcome": outcome,
            "duration_s": float(duration_s),
            "started_at": started_at,
            "fallback_reason": fallback_reason,
        })
        # Keep top-level cost_usd as the cumulative sum.
        data["cost_usd"] = sum(
            float(a.get("cost_usd", 0.0))
            for a in attempts if isinstance(a, dict)
        )

        tmp = summary_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, summary_path)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
