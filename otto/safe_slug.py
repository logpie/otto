"""Filesystem-safe slug helpers for runner-generated artifact paths."""

from __future__ import annotations

import hashlib
import re
from typing import Any


_SAFE_COMPONENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_UNSAFE_CHARS_RE = re.compile(r"[^a-z0-9._-]+")
_MULTI_DASH_RE = re.compile(r"-+")


def short_hash(label: Any, *, length: int = 8) -> str:
    """Stable short hash for disambiguating modified/truncated labels."""
    text = "" if label is None else str(label)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def safe_slug(label: Any, *, max_len: int = 48, fallback: str = "artifact") -> str:
    """Return a portable single path component for a prose label.

    Safe short labels are preserved for backward compatibility. Labels that
    require normalization or truncation get a short hash suffix so two long
    labels with the same prefix do not collide.
    """
    if max_len < 12:
        raise ValueError("safe_slug max_len must be at least 12")

    original = "" if label is None else str(label)
    normalized = original.strip().lower()
    normalized = _UNSAFE_CHARS_RE.sub("-", normalized)
    normalized = _MULTI_DASH_RE.sub("-", normalized).strip("-._")
    if not normalized:
        normalized = fallback

    if (
        normalized == original
        and len(normalized) <= max_len
        and _SAFE_COMPONENT_RE.fullmatch(normalized)
        and normalized not in {".", ".."}
    ):
        return normalized

    suffix = "-" + short_hash(original)
    body_limit = max_len - len(suffix)
    body = normalized[:body_limit].strip("-._") or fallback[:body_limit].strip("-._") or "artifact"
    return f"{body}{suffix}"

