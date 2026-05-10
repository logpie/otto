"""Per-task worktree creation + cross-task merge for v5.

Plan-v5 §6.5: each child task gets its own git worktree off the parent's
integration branch (or off main for root). Plan-v5 §1.5 + §6.3: merges
propagate bottom-up to per-parent integration branches; root's integration
branch eventually merges to main.

We layer on top of Otto's existing ``otto.worktree`` and ``otto.branching``.
This module is the v5-specific orchestration: which branch to create, how
to merge child branches into integration branches, and how to handle
conflicts (best-effort, with bounded retry).
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger("otto.v5_branching")


def integration_branch_name(parent_task_id: str | None) -> str:
    """Compute the integration branch name for a parent task.

    None (root) → "main" (the project's main branch is the root's integration).
    Otherwise → "i2p/integ/<parent_task_id>".

    Distinct prefix from child build branches (``i2p/build/<id>``) so a task
    that decomposes can have BOTH its own build branch and an integration
    branch — git refs are filesystem-style paths, so ``i2p/<id>`` and
    ``i2p/<id>/integration`` collide (one is a file, the other a directory).
    Siblings under different prefixes never collide.
    """
    if parent_task_id is None or parent_task_id == "root":
        return "main"
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", parent_task_id).strip("-")
    return f"i2p/integ/{safe}"


def child_branch_name(child_task_id: str) -> str:
    """Branch name for a child task's worktree.

    Uses the ``i2p/build/`` prefix so it never collides with the
    ``i2p/integ/`` integration namespace (see integration_branch_name).
    """
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", child_task_id).strip("-")
    return f"i2p/build/{safe}"


def child_worktree_path(project_dir: Path, child_task_id: str) -> Path:
    """Where the child's worktree lives on disk."""
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", child_task_id).strip("-")
    return project_dir / ".worktrees" / safe


_DEFAULT_GITIGNORE = """\
# Otto-managed default ignore set. Build/test agents commonly produce these
# artifacts; without them in .gitignore, agent commits include cache files
# that block parent integration merges across worktrees.

# Python
__pycache__/
*.py[cod]
*.pyo
*.egg-info/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.tox/
.coverage
htmlcov/
build/
dist/
*.egg
.venv/
venv/
env/

# Node
node_modules/
.npm/
.pnpm-store/
.yarn/
*.log
npm-debug.log*
yarn-error.log
.parcel-cache/
.next/
.nuxt/
.turbo/
.cache/
.vite/

# Editor / OS
.DS_Store
Thumbs.db
.idea/
.vscode/

# Test runtime artifacts
playwright-report/
test-results/
coverage/
"""


def ensure_initial_commit(project_dir: Path) -> bool:
    """Make sure the repo has at least one commit so refs/branches can be created.

    Greenfield projects often start with ``git init`` and no commits — every
    ``git branch <name>`` then fails with "fatal: not a valid object name: 'HEAD'".

    Also seeds a sensible ``.gitignore`` if absent. Without this, build agents
    routinely commit ``__pycache__/`` / ``node_modules/`` and parent
    integration merges fail with conflicts on cache files (the URL-shortener
    live run hit this on every Python child).

    Returns True if a commit was created, False if HEAD already existed.
    Best-effort: returns False on any failure.
    """
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(project_dir),
            capture_output=True,
        )
        if head.returncode == 0:
            return False

        # Seed .gitignore if the project doesn't ship one.
        gitignore_path = project_dir / ".gitignore"
        if not gitignore_path.exists():
            try:
                gitignore_path.write_text(_DEFAULT_GITIGNORE, encoding="utf-8")
            except OSError as exc:
                logger.warning("could not write default .gitignore: %s", exc)

        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(project_dir),
            capture_output=True,
        )
        commit = subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "v5 init"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
        )
        if commit.returncode != 0:
            logger.warning(
                "ensure_initial_commit: git commit failed: %s",
                (commit.stderr or "").strip(),
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure_initial_commit crashed: %s", exc)
        return False


def ensure_branch_exists(project_dir: Path, branch: str, base_ref: str = "HEAD") -> bool:
    """Idempotently create ``branch`` from ``base_ref`` if it doesn't exist.

    Returns True if created, False if already existed. Best-effort: returns
    False on any failure and logs.
    """
    try:
        # Check if branch already exists.
        cp = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=str(project_dir),
            capture_output=True,
        )
        if cp.returncode == 0:
            return False
        # Create from base_ref. base_ref might not exist either (e.g., for root
        # at first run); fall back to HEAD if base is missing.
        check_base = subprocess.run(
            ["git", "rev-parse", "--verify", base_ref],
            cwd=str(project_dir),
            capture_output=True,
        )
        if check_base.returncode != 0:
            base_ref = "HEAD"
        cp = subprocess.run(
            ["git", "branch", branch, base_ref],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
        )
        if cp.returncode != 0:
            logger.warning(
                "ensure_branch_exists(%s): git branch failed: %s",
                branch, (cp.stderr or "").strip(),
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure_branch_exists(%s) crashed: %s", branch, exc)
        return False


def setup_child_worktree(
    *,
    project_dir: Path,
    child_task_id: str,
    parent_integration_branch: str,
) -> Path | None:
    """Create a worktree for ``child_task_id`` off the parent's integration branch.

    Best-effort: returns None on any failure (caller falls back to
    project_dir for the child's CWD).
    """
    from otto.worktree import add_worktree

    # Ensure parent's integration branch exists. If we're root's child,
    # parent_integration_branch is "main" — must already exist.
    if parent_integration_branch != "main":
        ensure_branch_exists(project_dir, parent_integration_branch, base_ref="main")

    branch = child_branch_name(child_task_id)
    wt_path = child_worktree_path(project_dir, child_task_id)

    if wt_path.exists() and (wt_path / ".git").exists():
        # Already exists; reuse.
        return wt_path

    try:
        add_worktree(
            project_dir=project_dir,
            worktree_path=wt_path,
            branch=branch,
            base_ref=parent_integration_branch,
        )
        return wt_path
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "setup_child_worktree(%s) failed: %s; falling back to project_dir",
            child_task_id, exc,
        )
        return None


def merge_child_into_integration(
    *,
    project_dir: Path,
    child_task_id: str,
    parent_integration_branch: str,
) -> tuple[bool, str]:
    """Merge a child's branch into the parent's integration branch.

    Returns (success, detail). On conflict, aborts the merge and returns
    (False, "conflict: <files>") — caller may dispatch a conflict-resolution
    Lead. Best-effort throughout.
    """
    branch = child_branch_name(child_task_id)
    try:
        # Switch to integration branch.
        cp = subprocess.run(
            ["git", "checkout", parent_integration_branch],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
        )
        if cp.returncode != 0:
            return False, f"checkout {parent_integration_branch} failed: {(cp.stderr or '').strip()}"
        # Attempt merge --no-ff (preserve graph).
        cp = subprocess.run(
            ["git", "merge", "--no-ff", "--no-edit", branch],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
        )
        if cp.returncode == 0:
            return True, f"merged {branch} into {parent_integration_branch}"
        # Conflict.
        # Identify conflicting files.
        diag = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
        )
        files = (diag.stdout or "").strip().split("\n")
        # Abort to leave the integration branch clean.
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=str(project_dir),
            capture_output=True,
        )
        return False, f"conflict on: {', '.join(files[:5]) or '?'}"
    except Exception as exc:  # noqa: BLE001
        return False, f"merge crashed: {exc}"


def commit_worktree(*, worktree_path: Path, message: str) -> tuple[bool, str]:
    """Add+commit any changes in the child's worktree before merge.

    Idempotent: if there's nothing to commit, returns (True, "no-op").
    """
    try:
        # Stage all changes.
        cp = subprocess.run(
            ["git", "add", "-A"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
        )
        if cp.returncode != 0:
            return False, f"git add failed: {(cp.stderr or '').strip()}"
        # Check if anything to commit.
        cp = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(worktree_path),
            capture_output=True,
        )
        if cp.returncode == 0:
            return True, "no-op (nothing to commit)"
        # Commit.
        cp = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
        )
        if cp.returncode != 0:
            return False, f"git commit failed: {(cp.stderr or '').strip()}"
        return True, f"committed: {message}"
    except Exception as exc:  # noqa: BLE001
        return False, f"commit crashed: {exc}"
