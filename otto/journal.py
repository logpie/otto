"""Build journal — memory system for the certify→fix loop.

Three layers:
  sessions/<id>/improve/current-state.md  — handoff for the next agent
  sessions/<id>/improve/build-journal.md  — index of all rounds
  sessions/<id>/improve/rounds/  — immutable per-round evidence (new layout)
  project-root current-state.md / build-journal.md / otto_logs/rounds/
                                 — legacy fallback only
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from otto import paths


def _rounds_dir(project_dir: Path, session_id: str | None) -> Path:
    """Per-session rounds dir under improve/. Falls back to the legacy
    top-level otto_logs/rounds/ if session_id is not provided (legacy callers)."""
    if session_id:
        return paths.improve_dir(project_dir, session_id) / "rounds"
    return project_dir / "otto_logs" / "rounds"


def _current_state_path(project_dir: Path, session_id: str | None) -> Path:
    if session_id:
        return paths.improve_dir(project_dir, session_id) / "current-state.md"
    return project_dir / "current-state.md"


def _journal_path(project_dir: Path, session_id: str | None) -> Path:
    if session_id:
        return paths.improve_dir(project_dir, session_id) / "build-journal.md"
    return project_dir / "build-journal.md"


def init_round(project_dir: Path, action: str, session_id: str | None = None) -> str:
    """Start a new round. Returns round_id."""
    rounds_dir = _rounds_dir(project_dir, session_id)
    rounds_dir.mkdir(parents=True, exist_ok=True)

    # Sequential round number
    existing = sorted(rounds_dir.glob("round-*"))
    num = len(existing) + 1
    round_id = f"round-{num:03d}"

    round_dir = rounds_dir / round_id
    round_dir.mkdir()

    # Write manifest
    sha = _get_head_sha(project_dir)
    manifest = {
        "round_id": round_id,
        "round_num": num,
        "action": action,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit_before": sha,
    }
    (round_dir / "round-manifest.json").write_text(
        json.dumps(manifest, indent=2))

    return round_id


def record_certifier(
    project_dir: Path,
    round_id: str,
    report: Any,
    stories: list[dict[str, Any]],
    session_id: str | None = None,
) -> None:
    """Record certifier findings for a round."""
    round_dir = _rounds_dir(project_dir, session_id) / round_id

    # Machine-readable outcome
    outcome = {
        "outcome": getattr(report, "outcome", None),
        "stories_tested": len(stories),
        "stories_passed": sum(1 for s in stories if s.get("passed")),
        "cost_usd": getattr(report, "cost_usd", 0.0),
        "duration_s": getattr(report, "duration_s", 0.0),
    }
    if hasattr(outcome["outcome"], "value"):
        outcome["outcome"] = outcome["outcome"].value
    (round_dir / "outcome.json").write_text(
        json.dumps(outcome, indent=2, default=str))

    # Human-readable findings
    lines = ["# Certifier Findings\n"]
    for s in stories:
        status = "PASS" if s.get("passed") else "FAIL"
        lines.append(f"- **{s.get('story_id', '?')}** [{status}]: {s.get('summary', '')}")
        if not s.get("passed") and s.get("evidence"):
            lines.append(f"  ```\n  {s['evidence'][:500]}\n  ```")
    (round_dir / "certifier-findings.md").write_text("\n".join(lines) + "\n")


def record_build(
    project_dir: Path,
    round_id: str,
    build_result: Any,
    session_id: str | None = None,
) -> None:
    """Record build agent actions for a round."""
    round_dir = _rounds_dir(project_dir, session_id) / round_id

    sha_after = _get_head_sha(project_dir)

    # Git diff
    manifest_path = round_dir / "round-manifest.json"
    sha_before = ""
    if manifest_path.exists():
        m = json.loads(manifest_path.read_text())
        sha_before = m.get("commit_before", "")

    if sha_before and sha_after and sha_before != sha_after:
        diff = subprocess.run(
            ["git", "diff", "--stat", sha_before, sha_after],
            cwd=str(project_dir), capture_output=True, text=True,
        ).stdout.strip()

        full_diff = subprocess.run(
            ["git", "diff", sha_before, sha_after],
            cwd=str(project_dir), capture_output=True, text=True,
        ).stdout
        (round_dir / "git-diff.patch").write_text(full_diff)
    else:
        diff = "(no changes)"

    # Commit messages since before
    commits = ""
    if sha_before and sha_after and sha_before != sha_after:
        commits = subprocess.run(
            ["git", "log", "--oneline", f"{sha_before}..{sha_after}"],
            cwd=str(project_dir), capture_output=True, text=True,
        ).stdout.strip()

    # Builder summary
    lines = [
        "# Build Summary\n",
        f"Cost: ${getattr(build_result, 'total_cost', 0):.2f}",
        f"Passed: {getattr(build_result, 'passed', '?')}",
        f"\n## Files Changed\n```\n{diff}\n```",
    ]
    if commits:
        lines.append(f"\n## Commits\n```\n{commits}\n```")
    (round_dir / "builder-summary.md").write_text("\n".join(lines) + "\n")

    # Update manifest with after-SHA
    if manifest_path.exists():
        m = json.loads(manifest_path.read_text())
        m["commit_after"] = sha_after
        m["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        m["cost_usd"] = getattr(build_result, "total_cost", 0.0)
        m["passed"] = getattr(build_result, "passed", False)
        manifest_path.write_text(json.dumps(m, indent=2, default=str))


def update_current_state(
    project_dir: Path,
    round_id: str,
    stories: list[dict[str, Any]],
    action: str,
    session_id: str | None = None,
) -> None:
    """Rewrite current-state.md — the handoff for the next agent."""
    failures = [s for s in stories if not s.get("passed")]
    passes = [s for s in stories if s.get("passed")]

    lines = [
        f"# Current State (after {round_id})",
        f"**Last round:** {round_id} ({time.strftime('%Y-%m-%d %H:%M')})",
        f"**Action:** {action}",
        f"**Status:** {'all passing' if not failures else f'{len(failures)} open failure(s)'}",
        "",
    ]

    if failures:
        lines.append("## Open Failures")
        for s in failures:
            lines.append(f"- **{s.get('story_id', '?')}**: {s.get('summary', '')}")
        evidence_prefix = f"rounds/{round_id}" if session_id else f"otto_logs/rounds/{round_id}"
        lines.append(f"\nEvidence: {evidence_prefix}/certifier-findings.md")
        lines.append("")

    if passes:
        lines.append("## Known Good")
        for s in passes:
            lines.append(f"- {s.get('story_id', '?')}: {s.get('summary', '')[:80]}")
        lines.append("")

    # Collect traps from previous rounds
    traps = _collect_traps(project_dir, round_id, session_id=session_id)
    if traps:
        lines.append("## Previous Fix Attempts")
        for t in traps:
            lines.append(f"- Round {t['round']}: {t['summary']}")
        lines.append("")

    lines.append("## Round Detail")
    evidence_dir = f"rounds/{round_id}/" if session_id else f"otto_logs/rounds/{round_id}/"
    lines.append(f"Full evidence: {evidence_dir}")
    lines.append("")

    current_state = _current_state_path(project_dir, session_id)
    current_state.parent.mkdir(parents=True, exist_ok=True)
    current_state.write_text("\n".join(lines) + "\n")


def append_journal(
    project_dir: Path,
    round_id: str,
    action: str,
    result: str,
    cost: float,
    session_id: str | None = None,
) -> None:
    """Append one line to the build journal index."""
    journal = _journal_path(project_dir, session_id)
    ts = time.strftime("%m-%d %H:%M")

    if not journal.exists():
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(
            "# Build Journal\n\n"
            "| # | Time | Action | Result | Cost | Detail |\n"
            "|---|------|--------|--------|------|--------|\n"
        )

    num = round_id.split("-")[-1] if "-" in round_id else "?"
    line = f"| {num} | {ts} | {action[:40]} | {result} | ${cost:.2f} | → {round_id}/ |\n"

    with open(journal, "a") as f:
        f.write(line)


def _collect_traps(project_dir: Path, current_round_id: str, session_id: str | None = None) -> list[dict]:
    """Collect failed fix attempts from previous rounds for the handoff."""
    traps = []
    rounds_dir = _rounds_dir(project_dir, session_id)
    if not rounds_dir.exists():
        return traps

    for rd in sorted(rounds_dir.iterdir()):
        if rd.name >= current_round_id:
            break
        outcome_path = rd / "outcome.json"
        if outcome_path.exists():
            try:
                o = json.loads(outcome_path.read_text())
                if o.get("outcome") == "failed":
                    findings_path = rd / "certifier-findings.md"
                    summary = ""
                    if findings_path.exists():
                        # First FAIL line
                        for line in findings_path.read_text().split("\n"):
                            if "[FAIL]" in line:
                                summary = line.strip("- *").strip()[:100]
                                break
                    traps.append({"round": rd.name, "summary": summary or "failed"})
            except (json.JSONDecodeError, OSError):
                pass
    return traps


def _get_head_sha(project_dir: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_dir), capture_output=True, text=True,
        ).stdout.strip()
    except Exception:
        return ""
