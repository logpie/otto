#!/usr/bin/env python3
"""CLI entrypoint for certify() — called by the coding agent via Bash.

Usage: python -m otto.certifier.certify_cli <project_dir> <intent_file> [--config <config_file>]

The agent calls this via Bash to submit code for certification.
Outputs JSON to stdout: {status, issues, warnings}.
Runs the certifier in an isolated worktree.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Certify the current project")
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("intent_file", type=Path)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--max-calls-file", type=Path, default=None,
                        help="File tracking call count for budget enforcement")
    args = parser.parse_args()

    project_dir = args.project_dir.resolve()
    intent = args.intent_file.read_text().strip()

    # Load config
    config: dict = {}
    if args.config and args.config.exists():
        import yaml
        config = yaml.safe_load(args.config.read_text()) or {}

    # Budget enforcement via call count file
    max_calls = int(config.get("max_verification_rounds", 5))
    calls_file = args.max_calls_file or project_dir / "otto_logs" / "certify-calls.txt"
    call_count = 0
    if calls_file.exists():
        try:
            call_count = int(calls_file.read_text().strip())
        except (ValueError, OSError):
            pass
    call_count += 1
    calls_file.parent.mkdir(parents=True, exist_ok=True)
    calls_file.write_text(str(call_count))

    if call_count > max_calls:
        print(json.dumps({
            "status": "error",
            "message": f"Certification budget exceeded ({call_count}/{max_calls} calls).",
        }))
        sys.exit(0)

    # Snapshot current work
    candidate_sha = _snapshot(project_dir)

    # Run isolated certifier
    try:
        from otto.certifier.isolated import run_isolated_certifier
        from otto.certifier.report import CertificationOutcome

        report = run_isolated_certifier(
            intent=intent,
            candidate_sha=candidate_sha,
            project_dir=project_dir,
            config=config,
        )
    except Exception as exc:
        print(json.dumps({
            "status": "error",
            "message": f"Certification infrastructure failed: {exc}",
        }))
        sys.exit(0)

    # Map outcome
    if report.outcome == CertificationOutcome.PASSED:
        status = "passed"
    elif report.outcome in (CertificationOutcome.BLOCKED, CertificationOutcome.INFRA_ERROR):
        status = "error"
    else:
        status = "failed"

    # Coarse output
    issues = []
    for f in report.critical_findings():
        issues.append({
            "what": f.description,
            "detail": f.diagnosis,
            "suggestion": f.fix_suggestion,
        })

    warnings = [f.description for f in report.break_findings()]

    result = {
        "status": status,
        "issues": issues,
        "warnings": warnings,
        "call": call_count,
        "max_calls": max_calls,
    }
    if status == "error":
        result["message"] = report.findings[0].description if report.findings else "Unknown error"

    print(json.dumps(result, indent=2))


def _snapshot(project_dir: Path) -> str:
    """Commit current state as a candidate."""
    subprocess.run(["git", "add", "-A"], cwd=project_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "otto: certify candidate"],
        cwd=project_dir, capture_output=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir, capture_output=True, text=True,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    main()
