"""Build journal — memory system for the certify→fix loop.

Three layers:
  current-state.md  — handoff for the next agent (rewritten each round)
  build-journal.md  — index of all rounds (append-only)
  otto_logs/rounds/  — immutable per-round evidence
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any


def init_round(project_dir: Path, action: str) -> str:
    """Start a new round. Returns round_id."""
    rounds_dir = project_dir / "otto_logs" / "rounds"
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
) -> None:
    """Record certifier findings for a round."""
    round_dir = project_dir / "otto_logs" / "rounds" / round_id

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
) -> None:
    """Record build agent actions for a round."""
    round_dir = project_dir / "otto_logs" / "rounds" / round_id

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
        lines.append(f"\nEvidence: otto_logs/rounds/{round_id}/certifier-findings.md")
        lines.append("")

    if passes:
        lines.append("## Known Good")
        for s in passes:
            lines.append(f"- {s.get('story_id', '?')}: {s.get('summary', '')[:80]}")
        lines.append("")

    # Collect traps from previous rounds
    traps = _collect_traps(project_dir, round_id)
    if traps:
        lines.append("## Previous Fix Attempts")
        for t in traps:
            lines.append(f"- Round {t['round']}: {t['summary']}")
        lines.append("")

    lines.append(f"## Round Detail")
    lines.append(f"Full evidence: otto_logs/rounds/{round_id}/")
    lines.append("")

    (project_dir / "current-state.md").write_text("\n".join(lines) + "\n")


def append_journal(
    project_dir: Path,
    round_id: str,
    action: str,
    result: str,
    cost: float,
) -> None:
    """Append one line to the build journal index."""
    journal = project_dir / "build-journal.md"
    ts = time.strftime("%m-%d %H:%M")

    if not journal.exists():
        journal.write_text(
            "# Build Journal\n\n"
            "| # | Time | Action | Result | Cost | Detail |\n"
            "|---|------|--------|--------|------|--------|\n"
        )

    num = round_id.split("-")[-1] if "-" in round_id else "?"
    line = f"| {num} | {ts} | {action[:40]} | {result} | ${cost:.2f} | → {round_id}/ |\n"

    with open(journal, "a") as f:
        f.write(line)


def _collect_traps(project_dir: Path, current_round_id: str) -> list[dict]:
    """Collect failed fix attempts from previous rounds for the handoff."""
    traps = []
    rounds_dir = project_dir / "otto_logs" / "rounds"
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
