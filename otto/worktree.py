"""Phase 1.2: per-task worktree creation for atomic commands.

`otto build --in-worktree` and `otto improve --in-worktree` use this to
create an isolated working tree without spawning a subprocess. We
``os.chdir`` into the new worktree IN-PROCESS so the cli.py:32 venv-guard
never re-fires (it only fires for nested otto invocations).

Queue dispatch (Phase 2) uses a different code path that DOES spawn
subprocesses; that path sets ``OTTO_INTERNAL_QUEUE_RUNNER=1`` to bypass
the guard. Atomic --in-worktree avoids the subprocess and avoids the
guard issue entirely.

See plan-parallel.md §3.2, §5 Step 1.2.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from otto.branching import compute_branch_name, slugify_intent


class WorktreeAlreadyCheckedOut(RuntimeError):
    """Raised when the requested branch is already checked out in another worktree."""


def worktree_path_for(
    *,
    project_dir: Path,
    worktree_dir: str,
    mode: str,
    intent: str,
    slug_source: str | None = None,
    date: str | None = None,
) -> Path:
    """Compute the standard worktree path: ``<project>/<worktree_dir>/<mode>-<slug>-<date>/``."""
    slug = slugify_intent(slug_source or intent)
    if date is None:
        date = time.strftime("%Y-%m-%d")
    name = f"{mode}-{slug}-{date}"
    return project_dir / worktree_dir / name


def add_worktree(
    *,
    project_dir: Path,
    worktree_path: Path,
    branch: str,
) -> None:
    """Create a worktree at ``worktree_path`` checked out on ``branch``.

    - If branch doesn't exist: ``git worktree add -b <branch> <path>``
    - If branch exists but isn't checked out: ``git worktree add <path> <branch>``
    - If branch is already checked out elsewhere: raise WorktreeAlreadyCheckedOut

    Caller should NOT have already created the directory.
    """
    if worktree_path.exists():
        # Maybe it's already a worktree we can reuse?
        if (worktree_path / ".git").exists():
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
            )
            current = result.stdout.strip()
            if result.returncode != 0:
                raise RuntimeError(
                    f"failed to inspect existing worktree at {worktree_path}"
                )
            if current != branch:
                raise RuntimeError(
                    f"worktree path {worktree_path} is on branch {current!r}, "
                    f"expected {branch!r}"
                )
            return
        raise RuntimeError(
            f"worktree path {worktree_path} already exists and is not a worktree"
        )

    # Try create-and-checkout-new-branch first
    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path)],
        cwd=project_dir, capture_output=True, text=True,
    )
    if result.returncode == 0:
        return

    # Branch may already exist — try without -b
    result2 = subprocess.run(
        ["git", "worktree", "add", str(worktree_path), branch],
        cwd=project_dir, capture_output=True, text=True,
    )
    if result2.returncode == 0:
        return

    err = (result.stderr + "\n" + result2.stderr).strip()
    if "already checked out" in err or "is already used by worktree" in err:
        raise WorktreeAlreadyCheckedOut(
            f"branch {branch!r} is already checked out in another worktree:\n{err}"
        )
    raise RuntimeError(f"git worktree add failed for {worktree_path}: {err}")


def enter_worktree_for_atomic_command(
    *,
    project_dir: Path,
    worktree_dir: str,
    mode: str,
    intent: str,
    slug_source: str | None = None,
) -> tuple[Path, str]:
    """Create + enter a worktree for an atomic --in-worktree invocation.

    Returns (worktree_path, branch_name). After this call, ``os.getcwd()``
    is the worktree.

    The branch is computed via the standard `<mode>/<slug>-<date>` policy.
    Same-day re-runs reuse the existing worktree if present.

    Raises WorktreeAlreadyCheckedOut if the branch is checked out in some
    other worktree (caller should surface a clear error).
    """
    slug_input = slug_source or intent
    if not slug_input:
        raise ValueError("intent is required to compute worktree slug")
    slug = slugify_intent(slug_input)
    branch = compute_branch_name(mode, slug)
    wt_path = worktree_path_for(
        project_dir=project_dir,
        worktree_dir=worktree_dir,
        mode=mode,
        intent=intent,
        slug_source=slug_source,
    )
    add_worktree(project_dir=project_dir, worktree_path=wt_path, branch=branch)
    os.chdir(wt_path)
    return (wt_path, branch)
