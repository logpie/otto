"""Provider-aware cost and usage helpers."""

from __future__ import annotations

from typing import Any


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return max(int(value), 0)
    return None


def _format_usd(amount: float) -> str:
    if amount < 0.01:
        return f"${amount:.4f}"
    return f"${amount:.2f}"


def normalize_usage(usage: Any, *, provider: str | None = None) -> dict[str, Any] | None:
    """Return a JSON-safe usage dict with an optional provider tag."""
    if usage is None:
        return None
    if isinstance(usage, dict):
        data = dict(usage)
    else:
        try:
            data = dict(usage)
        except Exception:
            data = {}
            for key in (
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "total_cost_usd",
                "provider",
                "tokens_in",
                "tokens_out",
                "total_tokens",
                "tokens_used",
            ):
                value = getattr(usage, key, None)
                if value is not None:
                    data[key] = value
    if provider and not data.get("provider"):
        data["provider"] = provider
    return data or None


def usage_provider(usage: Any) -> str | None:
    payload = normalize_usage(usage)
    if not isinstance(payload, dict):
        return None
    raw = str(payload.get("provider") or "").strip().lower()
    return raw or None


def usage_tokens_in(usage: Any) -> int | None:
    payload = normalize_usage(usage)
    if not isinstance(payload, dict):
        return None
    return _as_int(payload.get("tokens_in")) or _as_int(payload.get("input_tokens"))


def usage_tokens_out(usage: Any) -> int | None:
    payload = normalize_usage(usage)
    if not isinstance(payload, dict):
        return None
    return _as_int(payload.get("tokens_out")) or _as_int(payload.get("output_tokens"))


def usage_total_tokens(usage: Any) -> int | None:
    payload = normalize_usage(usage)
    if not isinstance(payload, dict):
        return None
    direct = (
        _as_int(payload.get("total_tokens"))
        or _as_int(payload.get("tokens_used"))
        or _as_int(payload.get("total_tokens_used"))
    )
    if direct is not None:
        return direct
    tokens_in = usage_tokens_in(payload)
    tokens_out = usage_tokens_out(payload)
    if tokens_in is None and tokens_out is None:
        return None
    return int(tokens_in or 0) + int(tokens_out or 0)


def build_cost_payload(
    *,
    provider: str | None = None,
    total_cost_usd: float | None = None,
    usage: Any = None,
) -> dict[str, Any] | None:
    usage_payload = normalize_usage(usage, provider=provider)
    resolved_provider = (
        (provider or "").strip().lower()
        or usage_provider(usage_payload)
        or ("claude" if _as_float(total_cost_usd) is not None else None)
    )
    if not resolved_provider and usage_payload is None and _as_float(total_cost_usd) is None:
        return None

    if resolved_provider == "codex":
        cost_value = None
    else:
        cost_value = _as_float(total_cost_usd)

    tokens_in = usage_tokens_in(usage_payload)
    tokens_out = usage_tokens_out(usage_payload)
    total_tokens = usage_total_tokens(usage_payload)

    display = "unknown"
    if cost_value is not None:
        display = _format_usd(cost_value)
    elif resolved_provider == "codex" and total_tokens is not None:
        display = f"{total_tokens:,} tokens"

    return {
        "provider": resolved_provider or "unknown",
        "total_cost_usd": cost_value,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "display": display,
    }


def coerce_cost_payload(
    value: Any,
    *,
    provider: str | None = None,
    total_cost_usd: float | None = None,
    usage: Any = None,
) -> dict[str, Any] | None:
    """Accept new or legacy shapes and normalize them to one cost payload."""
    if isinstance(value, dict):
        if isinstance(value.get("cost"), dict):
            return coerce_cost_payload(
                value.get("cost"),
                provider=provider or value.get("provider"),
                total_cost_usd=(
                    total_cost_usd
                    if total_cost_usd is not None
                    else value.get("total_cost_usd", value.get("cost_usd"))
                ),
                usage=usage if usage is not None else value.get("usage"),
            )
        if any(key in value for key in ("provider", "total_cost_usd", "tokens_in", "tokens_out", "display")):
            usage_payload = normalize_usage(usage if usage is not None else {})
            if usage_payload is None:
                usage_payload = {}
            if value.get("tokens_in") is not None:
                usage_payload["tokens_in"] = value.get("tokens_in")
            if value.get("tokens_out") is not None:
                usage_payload["tokens_out"] = value.get("tokens_out")
            return build_cost_payload(
                provider=provider or value.get("provider"),
                total_cost_usd=(
                    total_cost_usd
                    if total_cost_usd is not None
                    else value.get("total_cost_usd")
                ),
                usage=usage_payload or usage,
            )
        return build_cost_payload(
            provider=provider or value.get("provider"),
            total_cost_usd=(
                total_cost_usd
                if total_cost_usd is not None
                else value.get("total_cost_usd", value.get("cost_usd"))
            ),
            usage=usage if usage is not None else value.get("usage"),
        )
    return build_cost_payload(provider=provider, total_cost_usd=total_cost_usd, usage=usage)


def format_metric_display(cost_payload: dict[str, Any] | None) -> str:
    normalized = coerce_cost_payload(cost_payload)
    if normalized is None:
        return "unknown"
    return str(normalized.get("display") or "unknown")


def format_summary_metric(cost_payload: dict[str, Any] | None) -> str:
    normalized = coerce_cost_payload(cost_payload)
    if normalized is None:
        return "unknown"
    display = format_metric_display(normalized)
    if display != "unknown":
        return display
    if normalized.get("provider") == "codex":
        return "usage: unknown"
    return "unknown"


def format_status_metric(cost_payload: dict[str, Any] | None) -> str:
    normalized = coerce_cost_payload(cost_payload)
    if normalized is None:
        return "cost: unknown"
    display = format_metric_display(normalized)
    if normalized.get("provider") == "codex":
        if display == "unknown":
            return "usage: unknown"
        return f"usage: {display}"
    if display == "unknown":
        return "cost: unknown"
    return display
