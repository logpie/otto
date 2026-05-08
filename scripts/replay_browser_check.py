#!/usr/bin/env python3
"""Replay one Otto BrowserJourney check from a saved project/worktree.

This is a diagnostics path, not a shortcut for product validation. It lets an
operator rerun the exact browser check shape from a failed worktree after fixing
Otto's check runner, preflight, or environment handling.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from otto.checks import run_check
from otto.spec_compile import BrowserJourney


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--cwd", type=Path, default=None)
    parser.add_argument(
        "--command",
        action="append",
        default=[],
        help="Command argument. Repeat once per argv item, e.g. --command npm --command run --command browser.",
    )
    parser.add_argument(
        "--evidence-glob",
        action="append",
        default=[],
        help="Evidence glob. Repeat for multiple globs.",
    )
    parser.add_argument("--timeout-s", type=int, default=600)
    return parser


def main() -> int:
    args = _parser().parse_args()
    cwd = args.cwd or args.project_dir
    check = BrowserJourney(
        command=tuple(str(item) for item in args.command),
        evidence_globs=tuple(str(item) for item in args.evidence_glob),
        timeout_s=args.timeout_s,
    )
    evidence = run_check(check, project_dir=args.project_dir, cwd=cwd)
    print(json.dumps({
        "passed": evidence.passed,
        "detail": evidence.detail,
        "artifacts": [str(path) for path in evidence.artifacts],
        "raw": evidence.raw,
    }, indent=2, sort_keys=True))
    return 0 if evidence.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
