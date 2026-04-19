"""Cross-run memory for the certifier.

Tracks what was tested, found, and fixed across runs — regardless of whether
the run was build, certify, or improve. The certifier reads this to:
  1. Regression-check previously fixed issues
  2. Focus on NEW untested areas
  3. Know the project's testing history

Design principles (from research):
  - Memory GUIDES focus, never SKIPS verification
  - Cite specific files/commits so memory can be verified against current code
  - Cap at last N entries to prevent bloat
  - Stale memory is worse than no memory — verify before trusting
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

MAX_ENTRIES = 5
HISTORY_FILE = "otto_logs/certifier-memory.jsonl"


def record_run(
    project_dir: Path,
    *,
    command: str,
    certifier_mode: str,
    stories: list[dict[str, Any]],
    cost: float,
) -> None:
    """Append one entry after a run completes. Best-effort — never raises."""
    try:
        _record_run_impl(project_dir, command=command, certifier_mode=certifier_mode,
                         stories=stories, cost=cost)
    except Exception:
        logging.getLogger("otto.memory").warning("Failed to record certifier memory")


def _record_run_impl(
    project_dir: Path,
    *,
    command: str,
    certifier_mode: str,
    stories: list[dict[str, Any]],
    cost: float,
) -> None:
    history_path = project_dir / HISTORY_FILE
    history_path.parent.mkdir(parents=True, exist_ok=True)

    # Collect git info for citations
    head_sha = ""
    try:
        head_sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(project_dir), capture_output=True, text=True,
        ).stdout.strip()
    except Exception:
        pass

    findings = []
    for s in stories:
        findings.append({
            "id": s.get("story_id", ""),
            "status": "passed" if s.get("passed") else "failed",
            "summary": s.get("summary", "")[:120],
        })

    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": command,
        "certifier_mode": certifier_mode,
        "commit": head_sha,
        "findings": findings,
        "tested": len(stories),
        "passed": sum(1 for s in stories if s.get("passed")),
        "cost": round(cost, 2),
    }

    with open(history_path, "a") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")


def load_history(project_dir: Path) -> list[dict[str, Any]]:
    """Read last N entries from certifier memory."""
    history_path = project_dir / HISTORY_FILE
    if not history_path.exists():
        return []

    entries = []
    try:
        for line in history_path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []

    return entries[-MAX_ENTRIES:]


def format_for_prompt(project_dir: Path) -> str:
    """Format cross-run memory as a prompt section.

    Returns empty string if no history. Otherwise returns a section
    that guides the certifier to verify previous findings and probe new areas.
    """
    entries = load_history(project_dir)
    if not entries:
        return ""

    lines = [
        "## Previous Certification History",
        "",
        "These are results from recent runs. Use them to GUIDE your testing:",
        "- VERIFY that previously fixed issues are STILL fixed (regression check)",
        "- PRIORITIZE areas that haven't been tested before",
        "- Do NOT skip testing an area just because it passed before — code may have changed",
        "",
    ]

    for entry in entries:
        ts = entry.get("ts", "?")[:10]
        cmd = entry.get("command", "?")
        mode = entry.get("certifier_mode", "?")
        commit = entry.get("commit", "?")

        lines.append(f"### {ts} — {cmd} ({mode}) @ {commit}")

        findings = entry.get("findings", [])
        failed = [f for f in findings if f["status"] == "failed"]
        passed_list = [f for f in findings if f["status"] == "passed"]

        if failed:
            lines.append(f"**Failed ({len(failed)}):**")
            for f in failed:
                lines.append(f"- {f['id']}: {f['summary']}")
        if passed_list:
            lines.append(f"**Passed ({len(passed_list)}):** {', '.join(f['id'] for f in passed_list)}")

        lines.append("")

    return "\n".join(lines)


def inject_memory(prompt: str, project_dir: Path, config: dict) -> str:
    """Append cross-run memory to prompt if enabled in config."""
    if not config.get("memory"):
        return prompt
    section = format_for_prompt(project_dir)
    if section:
        return prompt + f"\n\n{section}"
    return prompt
