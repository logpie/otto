"""Fail if ``otto/web/static`` has uncommitted changes after a build.

Used by ``npm run web:verify`` and CI: after running a fresh build, the
working tree should match what's checked in. Any diff means either the
developer rebuilt without committing, or the committed bundle was stale —
both block the merge.

The script ignores the build-stamp's ``built_at`` and ``git_commit`` fields
when those are the *only* differences: they change every build by design
and would make the check tautologically failing. Anything else in the stamp
(notably ``source_hash``) must match exactly.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR_REL = "otto/web/static"
STAMP_REL = f"{STATIC_DIR_REL}/build-stamp.json"
# Stamp keys that legitimately change every build. If the diff against HEAD
# touches only these keys, we treat the stamp as "effectively unchanged".
_VOLATILE_STAMP_KEYS = frozenset({"built_at", "git_commit"})


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def _changed_files() -> list[str]:
    """Return paths under ``otto/web/static`` that differ from HEAD."""
    proc = _git("status", "--porcelain", "--", STATIC_DIR_REL)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        sys.exit(proc.returncode)
    out: list[str] = []
    for line in proc.stdout.splitlines():
        # porcelain format: "XY path" with X,Y status codes
        if len(line) < 4:
            continue
        out.append(line[3:].lstrip())
    return out


def _stamp_diff_is_only_volatile() -> bool:
    """True when build-stamp.json differs only in volatile metadata."""
    head = _git("show", f"HEAD:{STAMP_REL}")
    if head.returncode != 0:
        return False  # no committed version yet
    stamp_path = REPO_ROOT / STAMP_REL
    if not stamp_path.is_file():
        return False
    try:
        a = json.loads(head.stdout)
        b = json.loads(stamp_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    keys = set(a) | set(b)
    for key in keys:
        if key in _VOLATILE_STAMP_KEYS:
            continue
        if a.get(key) != b.get(key):
            return False
    return True


def main() -> int:
    changed = _changed_files()
    if not changed:
        print(f"OK: {STATIC_DIR_REL} matches HEAD.")
        return 0

    # Allow stamp-only volatile drift.
    if changed == [STAMP_REL] and _stamp_diff_is_only_volatile():
        print(
            f"OK: only {STAMP_REL} changed and the diff is metadata "
            "(built_at/git_commit). Bundle content is unchanged."
        )
        return 0

    sys.stderr.write(
        "Bundle was rebuilt but not committed; "
        "run `git add otto/web/static && git commit`.\n"
        "Changed files:\n"
    )
    for path in changed:
        sys.stderr.write(f"  {path}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
