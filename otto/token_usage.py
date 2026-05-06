"""Token usage helpers shared by run summaries and Mission Control."""

from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
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


def format_compact_token_count(value: int | float) -> str:
    amount = max(float(value or 0), 0)
    if amount >= 1_000_000:
        return _format_scaled_count(amount, 1_000_000) + "M"
    if amount >= 1_000:
        return _format_scaled_count(amount, 1_000) + "K"
    return str(int(amount))


def _format_scaled_count(amount: float, scale: int) -> str:
    scaled = (Decimal(str(amount)) / Decimal(scale)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return format(scaled, "f").removesuffix(".0")


def format_token_spend(
    token_usage: dict[str, int] | None,
    *,
    reported_cost_usd: float | None = None,
    pending: bool = False,
) -> str:
    """Return the canonical user-facing spend string.

    Tokens are the primary cross-provider spend unit. Provider-reported USD is
    kept as machine-readable metadata elsewhere, but it is not comparable
    across Claude/Codex runs and should not be shown as the spend unit.
    """
    summary = token_spend_summary(token_usage)
    if summary["total"]:
        if summary["cached"]:
            hit = summary.get("cache_hit_rate")
            hit_text = f" · {format_cache_hit_rate(hit)} hit" if hit is not None else ""
            return (
                f"{format_compact_token_count(summary['fresh'])} fresh + "
                f"{format_compact_token_count(summary['cached'])} cached"
                f"{hit_text}"
            )
        return f"{format_compact_token_count(summary['total'])} tokens"
    return "..." if pending else "-"


def token_spend_summary(token_usage: dict[str, int] | None) -> dict[str, int | float | None]:
    """Return provider-neutral fresh/cache token spend components.

    Providers report cache differently:

    * Anthropic/Claude reports cache creation/read as additive token classes.
    * Codex/OpenAI-style ``cached_input_tokens`` is a subset of input tokens.

    This summary treats cache-read and cached-input-subset as "cached", while
    cache creation, uncached input, output, and reasoning are "fresh". The
    resulting ``fresh + cached`` equals the normalized total whenever the run
    has component-level usage data.
    """
    usage = normalize_token_usage(token_usage or {})
    total = token_total(usage)
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    cache_creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    cached_total = int(usage.get("cached_input_tokens", 0) or 0)
    output = int(usage.get("output_tokens", 0) or 0)
    reasoning = int(usage.get("reasoning_tokens", 0) or 0)

    # With explicit additive cache fields, cached_input_tokens is normalized to
    # include them. Any remaining cached_input_tokens are legacy subset-style
    # cached input from Codex/OpenAI logs aggregated into the same total.
    legacy_cached_subset = max(cached_total - cache_creation - cache_read, 0)
    legacy_cached_subset = min(legacy_cached_subset, input_tokens)

    cached = cache_read + legacy_cached_subset
    fresh = max(input_tokens - legacy_cached_subset, 0) + cache_creation + output + reasoning
    if total and fresh + cached < total:
        fresh += total - fresh - cached
    cache_input_denominator = input_tokens + cache_creation + cache_read
    hit_rate = (cached / cache_input_denominator) if cached and cache_input_denominator else None
    return {
        "total": total,
        "fresh": fresh,
        "cached": cached,
        "cache_hit_rate": hit_rate,
    }


def format_cache_hit_rate(value: float | None) -> str:
    if value is None:
        return "-"
    percentage = max(0, min(100, value * 100))
    if 0 < percentage < 1:
        return "<1%"
    if 99 < percentage < 100:
        return ">99%"
    return f"{int(Decimal(str(percentage)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))}%"


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
    previous_total = int(target.get("total_tokens", 0) or 0)
    for key in TOKEN_USAGE_KEYS:
        if key == "total_tokens":
            continue
        target[key] = int(target.get(key, 0) or 0) + int(normalized.get(key, 0) or 0)
    accumulated_total = previous_total + int(normalized.get("total_tokens", 0) or 0)
    target["total_tokens"] = 0
    target["total_tokens"] = max(accumulated_total, token_total(target))


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


def phase_breakdown_from_messages(session_dir: Path) -> dict[str, dict[str, Any]]:
    """Read phase duration/token totals from ``messages.jsonl`` files.

    New i2p runs nest provider streams under paths such as
    ``spec/compile-agent/messages.jsonl``,
    ``build/<group>/attempt-01/messages.jsonl`` and
    ``audit/attempt-00/judge/messages.jsonl``. Scan recursively and infer
    the canonical phase from the top-level directory so an audit judge whose
    SDK logger reports ``phase=build`` is still accounted to ``audit``.
    """
    by_phase: dict[str, dict[str, Any]] = {}
    if not session_dir.exists():
        return by_phase
    for entry in message_file_breakdown_from_messages(session_dir):
        phase = str(entry.get("phase") or "")
        if not phase:
            continue
        aggregate = by_phase.setdefault(phase, {})
        add_token_usage(aggregate, _token_fields(entry))
        if entry.get("duration_s") is not None:
            aggregate["duration_s"] = (
                float(aggregate.get("duration_s", 0.0) or 0.0)
                + float(entry.get("duration_s") or 0.0)
            )
        if entry.get("cost_usd") is not None:
            aggregate["cost_usd"] = (
                float(aggregate.get("cost_usd", 0.0) or 0.0)
                + float(entry.get("cost_usd") or 0.0)
            )
    return {
        phase: _prune_phase_breakdown(data)
        for phase, data in by_phase.items()
        if _phase_has_data(data)
    }


def message_file_breakdown_from_messages(session_dir: Path) -> list[dict[str, Any]]:
    """Return compact per-provider-call metrics from session JSONL logs.

    This is deliberately metadata-only. It never returns prompt text,
    assistant text, tool payloads, or raw transcript content, which keeps
    summaries safe to pass through product surfaces without dragging large
    ``messages.jsonl`` blobs back into model context.
    """
    entries: list[dict[str, Any]] = []
    if not session_dir.exists():
        return entries
    for messages_path in _message_paths(session_dir):
        phase_events: list[tuple[str, dict[str, int], dict[str, Any]]] = []
        result_events: list[dict[str, Any]] = []
        fallback = empty_token_usage()
        fallback_seen = False
        default_phase = _phase_from_message_path(session_dir, messages_path, messages_path.parent.name)
        try:
            fh = messages_path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                usage = token_usage_from_mapping(event.get("usage"))
                event_type = event.get("type")
                if event_type == "phase_end":
                    phase = _phase_from_message_path(
                        session_dir,
                        messages_path,
                        str(event.get("phase") or default_phase),
                    )
                    phase_events.append((phase, usage, event))
                elif event_type == "result" and usage:
                    result_events.append(event)
                    add_token_usage(fallback, usage)
                    fallback_seen = True
        per_file: dict[str, dict[str, Any]] = {}
        if phase_events:
            resolved_phase_events = _prefer_subset_cached_result_usage(phase_events, result_events)
            for (phase, usage), (_raw_phase, _phase_usage, event) in zip(
                resolved_phase_events,
                phase_events,
                strict=False,
            ):
                _merge_metric_event(
                    per_file.setdefault(
                        phase,
                        {
                            "phase": phase,
                            "path": _relative_message_path(session_dir, messages_path),
                        },
                    ),
                    usage=usage,
                    event=event,
                )
        elif fallback_seen:
            entry = per_file.setdefault(
                default_phase,
                {
                    "phase": default_phase,
                    "path": _relative_message_path(session_dir, messages_path),
                },
            )
            add_token_usage(entry, fallback)
        entries.extend(
            _prune_phase_breakdown(entry)
            for entry in per_file.values()
            if _phase_has_data(entry)
        )
    entries.sort(
        key=lambda item: (
            -int(item.get("total_tokens", 0) or 0),
            -float(item.get("duration_s", 0.0) or 0.0),
            str(item.get("path") or ""),
        )
    )
    return entries


def phase_token_usage_from_messages(session_dir: Path) -> dict[str, dict[str, int]]:
    """Read phase token totals from ``messages.jsonl`` under a session.

    Claude/Codex split-mode runs can emit accurate usage only in the phase
    message streams. Prefer explicit ``phase_end`` events to avoid double
    counting intermediate assistant/result events.
    """
    return {
        phase: prune_zero_token_usage(_token_fields(data))
        for phase, data in phase_breakdown_from_messages(session_dir).items()
        if any(_token_fields(data).values())
    }


_TOP_LEVEL_PHASES = frozenset({
    "audit",
    "build",
    "certify",
    "compile",
    "fix",
    "improve",
    "merge",
    "repair",
    "seed",
    "spec",
})


def _message_paths(session_dir: Path) -> list[Path]:
    root_messages = session_dir / "messages.jsonl"
    paths: list[Path] = []
    if root_messages.exists():
        paths.append(root_messages)
    paths.extend(
        path
        for path in sorted(session_dir.rglob("messages.jsonl"))
        if path != root_messages
    )
    return paths


def _phase_from_message_path(session_dir: Path, messages_path: Path, fallback: str) -> str:
    try:
        parts = messages_path.relative_to(session_dir).parts
    except ValueError:
        return str(fallback or "build")
    if len(parts) > 1 and parts[0] in _TOP_LEVEL_PHASES:
        return parts[0]
    fallback = str(fallback or "").strip().lower()
    return fallback if fallback else "build"


def _relative_message_path(session_dir: Path, messages_path: Path) -> str:
    try:
        return str(messages_path.relative_to(session_dir))
    except ValueError:
        return str(messages_path)


def _merge_metric_event(
    target: dict[str, Any],
    *,
    usage: dict[str, int],
    event: dict[str, Any],
) -> None:
    if usage:
        add_token_usage(target, usage)
    duration_s = _coerce_float(event.get("duration_s"))
    if duration_s is not None:
        target["duration_s"] = float(target.get("duration_s", 0.0) or 0.0) + duration_s
    raw_usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
    cost_usd = _coerce_float(raw_usage.get("cost_usd") or event.get("cost_usd"))
    if cost_usd is not None:
        target["cost_usd"] = float(target.get("cost_usd", 0.0) or 0.0) + cost_usd


def _token_fields(mapping: dict[str, Any]) -> dict[str, int]:
    return {
        key: _coerce_int(mapping.get(key))
        for key in TOKEN_USAGE_KEYS
    }


def _phase_has_data(mapping: dict[str, Any]) -> bool:
    if any(_coerce_int(mapping.get(key)) for key in TOKEN_USAGE_KEYS):
        return True
    duration_s = _coerce_float(mapping.get("duration_s"))
    cost_usd = _coerce_float(mapping.get("cost_usd"))
    return bool((duration_s is not None and duration_s > 0) or (cost_usd is not None and cost_usd > 0))


def _prune_phase_breakdown(mapping: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("phase", "path"):
        if mapping.get(key):
            out[key] = str(mapping[key])
    for key in TOKEN_USAGE_KEYS:
        value = _coerce_int(mapping.get(key))
        if value:
            out[key] = value
    for key in ("duration_s", "cost_usd"):
        value = _coerce_float(mapping.get(key))
        if value is not None and value > 0:
            out[key] = round(value, 4 if key == "cost_usd" else 3)
    return out


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
        if _looks_like_authoritative_result_usage(raw_phase_usage, raw_result_usage):
            result_usage = token_usage_from_mapping(raw_result_usage)
            corrected.append((phase, result_usage))
        else:
            corrected.append((phase, phase_usage))
    return corrected


def _looks_like_authoritative_result_usage(phase_usage: Any, result_usage: Any) -> bool:
    """Return true when a final result event is the safer phase total.

    Claude Code streams assistant usage snapshots and then emits a final
    ``result`` usage payload. Older Otto logs accumulated the streamed
    snapshots and then added the final result again, inflating phase totals.
    When a phase log contains exactly one result for one phase, the result is
    the provider's authoritative total for that call.
    """
    if not isinstance(result_usage, dict):
        return False
    result = token_usage_from_mapping(result_usage)
    if not result:
        return False
    if _looks_like_subset_cached_result(phase_usage, result_usage):
        return True
    if not isinstance(phase_usage, dict):
        return True
    phase = token_usage_from_mapping(phase_usage)
    if not phase:
        return True
    return token_total(result) <= token_total(phase)


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


def _coerce_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return None
