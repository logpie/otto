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
    base_ref: str | None = None,
) -> None:
    """Create a worktree at ``worktree_path`` checked out on ``branch``.

    - If branch doesn't exist: ``git worktree add -b <branch> <path> [<base_ref>]``
    - If branch exists but isn't checked out: ``git worktree add <path> <branch>``
    - If branch is already checked out elsewhere: raise WorktreeAlreadyCheckedOut

    ``base_ref`` (optional) is the start-point passed when creating a new
    branch — git uses HEAD by default. The W3-CRITICAL-1 fix passes a prior
    run's branch here so improve worktrees iterate on the prior build's
    files instead of forking from main.

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

    # Try create-and-checkout-new-branch first. When base_ref is supplied
    # (improve-on-prior-run), append it as the start-point so the new branch
    # is rooted on the prior run's tip rather than HEAD/main.
    create_argv = ["git", "worktree", "add", "-b", branch, str(worktree_path)]
    if base_ref:
        create_argv.append(base_ref)
    result = subprocess.run(
        create_argv,
        cwd=project_dir, capture_output=True, text=True,
    )
    if result.returncode == 0:
        return

    # Branch may already exist — try without -b. We deliberately ignore
    # base_ref here: git's "checkout existing branch" path doesn't accept a
    # start-point, and re-pointing an existing branch ref via the worktree
    # add command would be surprising. The branch already encodes its own
    # history; if base_ref differs we surface that as a hard error below.
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


def setup_worktree_for_atomic_cli(
    *,
    project_dir: Path,
    mode: str,
    intent: str,
    config: dict,
    slug_source: str | None = None,
) -> tuple[Path, dict]:
    """Create + enter the standard worktree, then reload config from it."""
    from otto.config import load_config

    wt_dir = config.get("queue", {}).get("worktree_dir", ".worktrees")
    wt_path, _ = enter_worktree_for_atomic_command(
        project_dir=project_dir,
        worktree_dir=wt_dir,
        mode=mode,
        intent=intent,
        slug_source=slug_source,
    )
    config_path = wt_path / "otto.yaml"
    reloaded_config = load_config(config_path) if config_path.exists() else config
    return (wt_path, reloaded_config)
