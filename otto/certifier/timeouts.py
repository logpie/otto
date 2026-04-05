"""Shared timeout helpers for certifier story execution and monitoring."""

from __future__ import annotations

from typing import Any

DEFAULT_STORY_TIMEOUT_BASE_S = 600.0
DEFAULT_STORY_TIMEOUT_PER_STEP_S = 120.0
DEFAULT_STORY_TIMEOUT_BREAK_S = 120.0
DEFAULT_HEARTBEAT_GRACE_S = 120.0
DEFAULT_HEARTBEAT_STEPS = 3


def resolve_story_timeout_config(config: dict[str, Any] | None = None) -> dict[str, float]:
    """Resolve certifier timeout settings from config with shared defaults."""
    config = config or {}
    return {
        "certifier_story_timeout_base": float(
            config.get("certifier_story_timeout_base", DEFAULT_STORY_TIMEOUT_BASE_S)
        ),
        "certifier_story_timeout_per_step": float(
            config.get("certifier_story_timeout_per_step", DEFAULT_STORY_TIMEOUT_PER_STEP_S)
        ),
        "certifier_story_timeout_break": float(
            config.get("certifier_story_timeout_break", DEFAULT_STORY_TIMEOUT_BREAK_S)
        ),
    }


def story_timeout_seconds(config: dict[str, Any] | None = None, *, steps: int) -> float:
    """Compute the per-story safety timeout from resolved config values."""
    resolved = resolve_story_timeout_config(config)
    return (
        resolved["certifier_story_timeout_base"]
        + (resolved["certifier_story_timeout_per_step"] * max(steps, 1))
        + resolved["certifier_story_timeout_break"]
    )


def heartbeat_stale_after_seconds(
    config: dict[str, Any] | None = None,
    *,
    steps: int = DEFAULT_HEARTBEAT_STEPS,
) -> float:
    """Heartbeat grace window for one in-flight story."""
    return story_timeout_seconds(config, steps=steps) + DEFAULT_HEARTBEAT_GRACE_S
