"""Shared browser-testing policy for Otto.

This module is intentionally small: low-level harnesses can still be
Playwright, agent-browser, or a repo-native script, but the rules for command
classification, agent-browser session isolation, and evidence honesty live in
one place.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path


GENERIC_AGENT_BROWSER_SESSIONS = {"", "default", "main", "shared", "browser"}
MAX_AGENT_BROWSER_SESSION_LEN = 32


def classify_browser_command(command: Sequence[str]) -> str:
    """Return the browser tool family implied by a BrowserJourney command."""
    lowered = [str(part).lower() for part in command]
    if any("agent-browser" in part for part in lowered):
        return "agent-browser"
    if any("playwright" in part for part in lowered):
        return "playwright"
    return "other"


def validate_agent_browser_command(command: Sequence[str]) -> str | None:
    """Require named agent-browser sessions for concurrent Otto checks."""
    if classify_browser_command(command) != "agent-browser":
        return None
    lowered = [str(part).lower() for part in command]
    if "--session" not in lowered:
        return (
            "Agent-browser BrowserJourney preflight failed: command uses "
            "`agent-browser` without a unique `--session`. Parallel journeys "
            "must use a per-worktree/per-journey session so browser state does "
            "not collide."
        )
    try:
        session_index = lowered.index("--session") + 1
        session_name = lowered[session_index]
    except (ValueError, IndexError):
        return (
            "Agent-browser BrowserJourney preflight failed: `--session` is "
            "present but no session name follows it."
        )
    if session_name in GENERIC_AGENT_BROWSER_SESSIONS:
        return (
            "Agent-browser BrowserJourney preflight failed: session name "
            f"{session_name!r} is too generic for concurrent Otto checks. "
            "Use a unique journey/worktree session name."
        )
    if len(session_name) > MAX_AGENT_BROWSER_SESSION_LEN:
        return (
            "Agent-browser BrowserJourney preflight failed: session name "
            f"{session_name!r} is too long for reliable Unix socket paths. "
            f"Keep agent-browser sessions <= {MAX_AGENT_BROWSER_SESSION_LEN} "
            "characters and put path isolation in AGENT_BROWSER_SOCKET_DIR."
        )
    return None


def declared_browser_evidence_missing(
    *,
    returncode: int,
    evidence_globs: Sequence[str],
    artifact_count: int,
) -> bool:
    """Return True when a successful browser command produced no declared proof."""
    return returncode == 0 and bool(evidence_globs) and artifact_count == 0


def agent_browser_argv(session_name: str, *args: str) -> list[str]:
    """Build an agent-browser CLI argv with an explicit session."""
    clean = _session_slug(session_name)
    return ["agent-browser", "--session", clean, *args]


def _session_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip()).strip("-")
    if not slug:
        return "otto-browser"
    return slug[:MAX_AGENT_BROWSER_SESSION_LEN].rstrip("-_.") or "otto-browser"


def browser_artifact_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return "image"
    if suffix in {".webm", ".mp4", ".mov"}:
        return "video"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix in {".json", ".jsonl"}:
        return "json"
    if suffix in {".zip", ".har"}:
        return "trace"
    return "file"
