"""Best-effort secret redaction for logs and reports."""

from __future__ import annotations

import re

_TOKEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}\b"), "sk-ant-REDACTED"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "ghp_REDACTED"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "github_pat_REDACTED"),
    (re.compile(r"\bgho_[A-Za-z0-9]{20,}\b"), "gho_REDACTED"),
    (re.compile(r"\bghs_[A-Za-z0-9]{20,}\b"), "ghs_REDACTED"),
    (re.compile(r"\bghu_[A-Za-z0-9]{20,}\b"), "ghu_REDACTED"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "AIzaREDACTED"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "sk-REDACTED"),
)
_ENV_ASSIGNMENT_RE = re.compile(
    r'(?P<prefix>\b(?P<key>[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD))\b\s*[:=]\s*[\'"]?)'
    r'(?P<value>[A-Za-z0-9._/\-+=]{20,200})'
    r'(?P<suffix>[\'"]?)'
)


def redact_text(text: str) -> str:
    """Mask obvious secrets in text rendered to disk or reports."""
    if not text:
        return text
    def _replace_env(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        key = match.group("key")
        suffix = match.group("suffix")
        return f"{prefix}[REDACTED:{key}]{suffix}"

    redacted = _ENV_ASSIGNMENT_RE.sub(_replace_env, text)
    for pattern, replacement in _TOKEN_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact(text: str) -> str:
    """Backward-compatible alias for callers that prefer ``redact(...)``."""
    return redact_text(text)
