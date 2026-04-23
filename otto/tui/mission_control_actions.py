"""Shared Mission Control action primitives."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ActionState:
    key: str
    label: str
    enabled: bool
    reason: str | None
    preview: str


def make_action(
    key: str,
    label: str,
    *,
    enabled: bool,
    reason: str | None,
    preview: str,
) -> ActionState:
    return ActionState(key=key, label=label, enabled=enabled, reason=reason, preview=preview)
