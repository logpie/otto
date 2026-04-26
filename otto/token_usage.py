"""Token usage helpers shared by run summaries and Mission Control."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TOKEN_USAGE_KEYS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)


def empty_token_usage() -> dict[str, int]:
    return dict.fromkeys(TOKEN_USAGE_KEYS, 0)


def normalize_token_usage(mapping: Any) -> dict[str, int]:
    if not isinstance(mapping, dict):
        return empty_token_usage()
    raw_usage = mapping.get("token_usage")
    if isinstance(raw_usage, dict):
        mapping = {**mapping, **raw_usage}
    cache_creation = _coerce_int(mapping.get("cache_creation_input_tokens"))
    cache_read = _coerce_int(mapping.get("cache_read_input_tokens"))
    legacy_cached = _coerce_int(mapping.get("cached_input_tokens"))
    cached_total = max(legacy_cached, cache_creation + cache_read)
    totals = {
        "input_tokens": _coerce_int(mapping.get("input_tokens") or mapping.get("tokens_in")),
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "cached_input_tokens": cached_total,
        "output_tokens": _coerce_int(mapping.get("output_tokens") or mapping.get("tokens_out")),
        "reasoning_tokens": _coerce_int(mapping.get("reasoning_tokens")),
        "total_tokens": 0,
    }
    totals["total_tokens"] = max(_coerce_int(mapping.get("total_tokens")), token_total(totals))
    return totals


def add_token_usage(target: dict[str, int], usage: dict[str, int] | None) -> None:
    normalized = normalize_token_usage(usage or {})
    for key in TOKEN_USAGE_KEYS:
        if key == "total_tokens":
            continue
        target[key] = int(target.get(key, 0) or 0) + int(normalized.get(key, 0) or 0)
    target["total_tokens"] = token_total(target)


def token_total(token_usage: dict[str, int] | None) -> int:
    if not token_usage:
        return 0
    explicit = int(token_usage.get("total_tokens", 0) or 0)
    cache_creation = int(token_usage.get("cache_creation_input_tokens", 0) or 0)
    cache_read = int(token_usage.get("cache_read_input_tokens", 0) or 0)
    derived = (
        int(token_usage.get("input_tokens", 0) or 0)
        + cache_creation
        + cache_read
        + int(token_usage.get("output_tokens", 0) or 0)
        + int(token_usage.get("reasoning_tokens", 0) or 0)
    )
    return max(explicit, derived)


def prune_zero_token_usage(token_usage: dict[str, int]) -> dict[str, int]:
    return {
        key: int(token_usage.get(key, 0) or 0)
        for key in TOKEN_USAGE_KEYS
        if int(token_usage.get(key, 0) or 0)
    }


def token_usage_from_mapping(mapping: Any) -> dict[str, int]:
    totals = normalize_token_usage(mapping)
    return prune_zero_token_usage(totals) if any(totals.values()) else {}


def phase_token_usage_from_messages(session_dir: Path) -> dict[str, dict[str, int]]:
    """Read phase token totals from ``*/messages.jsonl`` under a session.

    Claude/Codex split-mode runs can emit accurate usage only in the phase
    message streams. Prefer explicit ``phase_end`` events to avoid double
    counting intermediate assistant/result events.
    """
    by_phase: dict[str, dict[str, int]] = {}
    if not session_dir.exists():
        return by_phase
    for messages_path in sorted(session_dir.glob("*/messages.jsonl")):
        phase_events: list[tuple[str, dict[str, int], dict[str, Any]]] = []
        result_events: list[dict[str, Any]] = []
        fallback = empty_token_usage()
        fallback_seen = False
        default_phase = messages_path.parent.name
        try:
            lines = messages_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            usage = token_usage_from_mapping(event.get("usage"))
            if not usage:
                continue
            if event.get("type") == "phase_end":
                phase = str(event.get("phase") or default_phase)
                phase_events.append((phase, usage, event))
            elif event.get("type") == "result":
                result_events.append(event)
                add_token_usage(fallback, usage)
                fallback_seen = True
        if phase_events:
            resolved_phase_events = _prefer_subset_cached_result_usage(phase_events, result_events)
            for phase, usage in resolved_phase_events:
                add_token_usage(by_phase.setdefault(phase, empty_token_usage()), usage)
        elif fallback_seen:
            add_token_usage(by_phase.setdefault(default_phase, empty_token_usage()), fallback)
    return {phase: prune_zero_token_usage(usage) for phase, usage in by_phase.items() if any(usage.values())}


def _prefer_subset_cached_result_usage(
    phase_events: list[tuple[str, dict[str, int], dict[str, Any]]],
    result_events: list[dict[str, Any]],
) -> list[tuple[str, dict[str, int]]]:
    """Handle legacy Codex logs whose phase_end inflated cached tokens.

    Older logstream code copied ``cached_input_tokens`` into
    ``cache_read_input_tokens``. That is correct for Anthropic cache-read
    fields, but not for Codex/OpenAI-style usage where cached tokens are a
    subset of input tokens. When matching raw result events lack explicit
    cache read/create fields, use those result events with current
    normalization instead of the inflated phase_end totals.
    """
    if len(phase_events) != len(result_events) or not phase_events:
        return [(phase, usage) for phase, usage, _event in phase_events]
    corrected: list[tuple[str, dict[str, int]]] = []
    for (phase, phase_usage, phase_event), result_event in zip(phase_events, result_events, strict=False):
        raw_phase_usage = phase_event.get("usage")
        raw_result_usage = result_event.get("usage")
        if _looks_like_subset_cached_result(raw_phase_usage, raw_result_usage):
            result_usage = token_usage_from_mapping(raw_result_usage)
            corrected.append((phase, result_usage))
        else:
            corrected.append((phase, phase_usage))
    return corrected


def _looks_like_subset_cached_result(phase_usage: Any, result_usage: Any) -> bool:
    if not isinstance(phase_usage, dict) or not isinstance(result_usage, dict):
        return False
    if result_usage.get("cache_read_input_tokens") or result_usage.get("cache_creation_input_tokens"):
        return False
    cached = _coerce_int(result_usage.get("cached_input_tokens"))
    if cached <= 0:
        return False
    return (
        _coerce_int(phase_usage.get("cache_creation_input_tokens")) == 0
        and _coerce_int(phase_usage.get("cache_read_input_tokens")) == cached
        and _coerce_int(phase_usage.get("cached_input_tokens")) == cached
    )


def total_token_usage_from_phases(phases: dict[str, dict[str, int]] | None) -> dict[str, int]:
    totals = empty_token_usage()
    for usage in (phases or {}).values():
        add_token_usage(totals, usage)
    return prune_zero_token_usage(totals) if any(totals.values()) else {}


def _coerce_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0
