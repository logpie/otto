"""Shared browser-testing policy for Otto.

This module is intentionally small: low-level harnesses can still be
Playwright, agent-browser, or a repo-native script, but the rules for command
classification, agent-browser session isolation, and evidence honesty live in
one place.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from typing import Mapping


GENERIC_AGENT_BROWSER_SESSIONS = {"", "default", "main", "shared", "browser"}
MAX_AGENT_BROWSER_SESSION_LEN = 32


class BrowserToolFamily(str, Enum):
    AGENT_BROWSER = "agent-browser"
    PLAYWRIGHT = "playwright"
    OTHER = "other"


_EXECUTABLE_TOOL_FAMILIES: dict[str, BrowserToolFamily] = {
    "agent-browser": BrowserToolFamily.AGENT_BROWSER,
    "playwright": BrowserToolFamily.PLAYWRIGHT,
}
_PYTHON_MODULE_TOOL_FAMILIES: dict[str, BrowserToolFamily] = {
    "agent_browser": BrowserToolFamily.AGENT_BROWSER,
    "playwright": BrowserToolFamily.PLAYWRIGHT,
}


def classify_browser_command(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    package_scripts: Mapping[str, str] | None = None,
) -> BrowserToolFamily:
    """Return the browser tool family implied by a BrowserJourney command."""
    return _classify_browser_tokens(
        [str(part) for part in command],
        cwd=cwd,
        package_scripts=package_scripts,
        depth=0,
    )


def validate_agent_browser_command(command: Sequence[str]) -> str | None:
    """Require named agent-browser sessions for concurrent Otto checks."""
    if classify_browser_command(command) != BrowserToolFamily.AGENT_BROWSER:
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


def _classify_browser_tokens(
    tokens: Sequence[str],
    *,
    cwd: Path | None,
    package_scripts: Mapping[str, str] | None,
    depth: int,
) -> BrowserToolFamily:
    if not tokens or depth > 3:
        return BrowserToolFamily.OTHER
    first = _token_name(tokens[0])
    direct = _EXECUTABLE_TOOL_FAMILIES.get(first)
    if direct is not None:
        return direct
    if first in {"python", "python3"} and len(tokens) >= 3 and tokens[1] == "-m":
        return _PYTHON_MODULE_TOOL_FAMILIES.get(tokens[2], BrowserToolFamily.OTHER)
    if first in {"uv", "poetry", "pipx"} and len(tokens) >= 3 and tokens[1] == "run":
        return _classify_browser_tokens(
            tokens[2:],
            cwd=cwd,
            package_scripts=package_scripts,
            depth=depth + 1,
        )
    if first in {"npx", "pnpx"} and len(tokens) >= 2:
        return _EXECUTABLE_TOOL_FAMILIES.get(_token_name(tokens[1]), BrowserToolFamily.OTHER)
    if first in {"npm", "pnpm", "yarn"}:
        family = _classify_package_manager_command(
            first,
            tokens,
            cwd=cwd,
            package_scripts=package_scripts,
            depth=depth,
        )
        if family is not None:
            return family
    return BrowserToolFamily.OTHER


def _classify_package_manager_command(
    executable: str,
    tokens: Sequence[str],
    *,
    cwd: Path | None,
    package_scripts: Mapping[str, str] | None,
    depth: int,
) -> BrowserToolFamily | None:
    if len(tokens) >= 3 and tokens[1] in {"exec", "dlx"}:
        return _EXECUTABLE_TOOL_FAMILIES.get(_token_name(tokens[2]), BrowserToolFamily.OTHER)
    script_name = _package_script_name(executable, tokens)
    if script_name is None:
        if executable == "yarn" and len(tokens) >= 2:
            return _EXECUTABLE_TOOL_FAMILIES.get(_token_name(tokens[1]), BrowserToolFamily.OTHER)
        return None
    scripts = package_scripts if package_scripts is not None else _read_package_scripts(cwd)
    script = str(scripts.get(script_name) or "")
    if not script.strip():
        return BrowserToolFamily.OTHER
    try:
        script_tokens = shlex.split(script)
    except ValueError:
        return BrowserToolFamily.OTHER
    return _classify_browser_tokens(
        script_tokens,
        cwd=cwd,
        package_scripts=package_scripts,
        depth=depth + 1,
    )


def _package_script_name(executable: str, tokens: Sequence[str]) -> str | None:
    if executable == "npm" and len(tokens) >= 3 and tokens[1] == "run":
        return str(tokens[2])
    if executable in {"pnpm", "yarn"} and len(tokens) >= 3 and tokens[1] == "run":
        return str(tokens[2])
    return None


def _read_package_scripts(cwd: Path | None) -> dict[str, str]:
    if cwd is None:
        return {}
    try:
        payload = json.loads((cwd / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    scripts = payload.get("scripts") if isinstance(payload, dict) else None
    if not isinstance(scripts, dict):
        return {}
    return {str(key): str(value) for key, value in scripts.items()}


def _token_name(value: str) -> str:
    return Path(str(value)).name.lower()


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
