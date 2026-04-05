"""Shared timeout helpers for certifier story execution and monitoring.

By default, there is NO per-story timeout — structured output is the
primary termination mechanism and has 100% success rate in testing.

If a timeout is configured (certifier_story_timeout in otto.yaml),
it acts as a safety net for hung SDK sessions. Set it based on
observed story durations for your projects, not guesses.

Heartbeat is always active regardless of timeout config.
"""

from __future__ import annotations

from typing import Any

# Default: 300s (5 min). Observed stories take 47-91s, so 5 min is ~5x headroom.
# Structured output is the normal termination mechanism, but stories can hang
# (observed: "Todos Persist" story hung indefinitely in E2E testing).
# Set certifier_story_timeout in otto.yaml to override.
DEFAULT_STORY_TIMEOUT: float | None = 300.0

# Heartbeat grace: how long after last heartbeat before "stale" warning.
# This is informational — it doesn't kill anything.
DEFAULT_HEARTBEAT_GRACE_S = 300.0  # 5 min


def resolve_story_timeout(config: dict[str, Any] | None = None) -> float | None:
    """Resolve per-story timeout from config. Returns None if disabled."""
    config = config or {}
    raw = config.get("certifier_story_timeout", DEFAULT_STORY_TIMEOUT)
    if raw is None:
        return None
    return float(raw)


def resolve_story_timeout_config(config: dict[str, Any] | None = None) -> dict[str, float | None]:
    """Resolve timeout settings as a dict for serialization."""
    timeout = resolve_story_timeout(config)
    return {
        "certifier_story_timeout": timeout,
        "heartbeat_grace_s": DEFAULT_HEARTBEAT_GRACE_S,
    }


def story_timeout_seconds(config: dict[str, Any] | None = None, *, steps: int = 0) -> float | None:
    """Per-story timeout. Returns None if disabled (default)."""
    return resolve_story_timeout(config)


def heartbeat_stale_after_seconds(
    config: dict[str, Any] | None = None,
    *,
    steps: int = 3,
) -> float:
    """Heartbeat grace window. Always active regardless of timeout config."""
    config = config or {}
    return float(config.get("certifier_heartbeat_grace", DEFAULT_HEARTBEAT_GRACE_S))
