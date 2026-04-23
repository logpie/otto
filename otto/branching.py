"""Shared branching + slug logic for atomic commands and queue tasks.

This module is the single source of truth for:
- intent → slug (used by both atomic branch names and queue task IDs)
- branch name composition
- "should we auto-branch?" policy (only auto-branch from default_branch)
- branch create-or-switch operations

By centralising here, atomic `otto build`, `otto improve`, and the future
`otto queue` runner all use the exact same conventions.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import time
from pathlib import Path

# Reserved single-word IDs that would collide with `otto queue` management
# verbs (ls/show/rm/cancel/cleanup/run/dashboard/resume). Refused as task IDs to keep CLI parsing
# unambiguous. (Plan-parallel.md §5 Step 2.2.)
RESERVED_TASK_IDS: frozenset[str] = frozenset({
    "ls", "show", "rm", "cancel", "cleanup", "run", "dashboard", "resume",
})


def slugify_intent(intent: str, *, max_chars: int = 40) -> str:
    """Turn a free-form intent into a filesystem/branch-safe slug.

    - lowercase
    - non-alphanumeric → '-'
    - collapse runs of '-'
    - strip leading/trailing '-'
    - trim to max_chars (preserving word boundary if possible)
    - falls back to 'task' if the intent slugifies to empty (e.g. emoji-only)
    """
    original = intent or ""
    if not original:
        return f"task-{hashlib.sha1(original.encode('utf-8')).hexdigest()[:6]}"
    s = intent.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    needs_hash = False
    if not s:
        s = "task"
        needs_hash = True
    if len(s) > max_chars:
        needs_hash = True
        # Trim to max_chars but try not to split mid-word
        cut = s[:max_chars]
        if "-" in cut[max_chars - 10 :]:
            cut = cut.rsplit("-", 1)[0]
        s = cut.strip("-") or s[:max_chars].strip("-") or "task"
    if s == "task":
        needs_hash = True
    if needs_hash:
        return f"{s}-{hashlib.sha1(original.encode('utf-8')).hexdigest()[:6]}"
    return s


def compute_branch_name(
    mode: str, slug: str, *, date: str | None = None
) -> str:
    """Compose a branch name like `build/add-csv-export-2026-04-19`.

    `mode` is the otto subcommand (build / improve / certify / etc.).
    `date` defaults to today (YYYY-MM-DD).
    """
    if not mode:
        raise ValueError("mode is required")
    if not slug:
        slug = "task"
    if date is None:
        date = time.strftime("%Y-%m-%d")
    return f"{mode}/{slug}-{date}"


def _git(project_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )


def current_branch(project_dir: Path) -> str:
    """Return current branch name. Empty string if detached HEAD or no commits."""
    return _git(project_dir, "branch", "--show-current").stdout.strip()


def repo_has_commits(project_dir: Path) -> bool:
    """True iff the repo has at least one commit on the current ref.

    Greenfield repos (`git init` with no commits) have no HEAD, so
    auto-branching is a no-op until the first commit lands.
    """
    return _git(project_dir, "rev-parse", "--verify", "HEAD").returncode == 0


def should_auto_branch(current: str, default_branch: str) -> bool:
    """Auto-branch only when the user is on the project's default branch.

    Mirrors `otto improve`'s long-standing "stay on improve branch" pattern
    (`cli_improve.py:33`): if the user is on a feature branch, otto respects
    that and lets new commits land there. If on default_branch, otto creates
    a fresh branch to keep main clean. (Plan-parallel.md §3.2.)
    """
    if not current or not default_branch:
        return False
    return current == default_branch


def create_or_switch_branch(project_dir: Path, branch: str) -> str:
    """Create `branch` and switch to it; if it exists, switch to existing.

    Returns the branch name that is now checked out. Raises RuntimeError
    if the post-switch branch doesn't match (caller should treat as fatal).
    """
    create = _git(project_dir, "checkout", "-b", branch)
    if create.returncode != 0:
        # Branch likely exists from earlier same-day run — try switching
        switch = _git(project_dir, "checkout", branch)
        if switch.returncode != 0:
            err = create.stderr.strip() or switch.stderr.strip()
            raise RuntimeError(f"Failed to create or switch to branch {branch!r}: {err}")
    actual = current_branch(project_dir)
    if actual != branch:
        raise RuntimeError(
            f"Branch checkout reported success but current branch is {actual!r}, expected {branch!r}"
        )
    return branch


def ensure_branch_for_atomic_command(
    *,
    mode: str,
    intent: str,
    project_dir: Path,
    default_branch: str,
) -> tuple[str, bool]:
    """For an atomic `otto build|improve|...` invocation, decide and apply branch policy.

    Returns (branch_name, created_new). If the user is on the default branch,
    creates `<mode>/<slug>-<date>` and switches. If on any other branch,
    stays put. If the repo has no commits yet (greenfield), does nothing.

    This is the single entry point atomic commands should call right before
    starting their pipeline. Queue dispatch uses a separate path that always
    uses worktrees with explicit branch names from the QueueTask record.

    Raises RuntimeError on git failure (caller should treat as fatal).
    """
    if not repo_has_commits(project_dir):
        # Greenfield — no commits, no branch concept yet. Atomic commands
        # building from scratch land their first commit on whatever git init
        # left as HEAD; nothing for us to do.
        return ("", False)

    cur = current_branch(project_dir)
    if not should_auto_branch(cur, default_branch):
        # User is on a feature branch (or anywhere not the default) — respect it.
        return (cur, False)

    slug = slugify_intent(intent)
    branch = compute_branch_name(mode, slug)
    _ = create_or_switch_branch(project_dir, branch)
    return (branch, True)
