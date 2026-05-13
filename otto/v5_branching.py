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

# Also match SYMLINKS named the same way (no trailing slash matches
# symlinks where the trailing-slash pattern only matches real dirs).
# Otto's install-dir-sharing propagates these as symlinks; without the
# no-slash patterns git tracks them and breaks subsequent merges.
.venv
venv

# Node
node_modules/
node_modules
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

# TypeScript incremental build cache — frequent cause of cross-worktree
# merge conflicts (different siblings produce different .tsbuildinfo for
# the same project). Tooling regenerates these on demand; don't commit.
*.tsbuildinfo
tsconfig.tsbuildinfo
tsconfig.*.tsbuildinfo
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


def _gitignored_paths(repo: Path, paths: list[str]) -> set[str]:
    """Subset of ``paths`` that the repo's gitignore rules would ignore.

    Delegates to ``git check-ignore`` so the project's own .gitignore +
    .git/info/exclude + core.excludesFile are the source of truth. We
    don't maintain a hardcoded list of "noise" patterns — whatever the
    project says is ignorable is ignorable, period.

    ``--no-index`` lets check-ignore answer for files even when they are
    tracked (which they are during a merge conflict).
    """
    paths = [p for p in paths if p]
    if not paths:
        return set()
    cp = subprocess.run(
        ["git", "check-ignore", "--no-index", "--", *paths],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    # check-ignore: exit 0 = some matched, 1 = none matched, 128 = error.
    # stdout lists matched paths, one per line.
    if cp.returncode not in (0, 1):
        return set()
    return {p.strip() for p in (cp.stdout or "").splitlines() if p.strip()}


# Backwards-compatible single-path predicate (used by tests that exercise
# the noise-recognition oracle in isolation).
def _is_noise_path(path: str, *, repo: Path | None = None) -> bool:
    """True iff ``path`` is gitignored in ``repo`` (or in the current dir)."""
    repo = repo or Path(".")
    return path in _gitignored_paths(repo, [path])


def merge_branch_into(
    *,
    project_dir: Path,
    source_branch: str,
    target_branch: str,
) -> tuple[bool, str]:
    """Merge ``source_branch`` into ``target_branch`` with gitignore-aware
    noise auto-resolve. Generic version of ``merge_child_into_integration``;
    used by ``_run_child`` (build branch → parent integration) AND by
    ``_propagate_integration_up`` (subtree integration → grandparent
    integration), which is what makes nested decompositions land on main.

    Returns (success, detail). On real source conflicts, aborts and returns
    (False, "conflict on: <files>"). On noise-only conflicts, auto-resolves
    --ours and completes.
    """
    branch = source_branch
    parent_integration_branch = target_branch
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
        # Conflict — identify conflicting files.
        diag = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
        )
        files = [f for f in (diag.stdout or "").strip().split("\n") if f]
        # Layer 2: gitignore-aware noise auto-resolve.
        noise_set = _gitignored_paths(project_dir, files)
        noise_files = [f for f in files if f in noise_set]
        non_noise = [f for f in files if f not in noise_set]

        # Layer 3: structured-file merge drivers for non-noise conflicts.
        from otto.v5_merge_drivers import find_driver, is_discard_signal
        structured_resolved: list[str] = []
        truly_blocked: list[str] = []
        for f in non_noise:
            driver = find_driver(f)
            if driver is None:
                truly_blocked.append(f)
                continue
            # Read --ours and --theirs versions and ask the driver to merge.
            ours_proc = subprocess.run(
                ["git", "show", f":2:{f}"],
                cwd=str(project_dir), capture_output=True, text=True,
            )
            theirs_proc = subprocess.run(
                ["git", "show", f":3:{f}"],
                cwd=str(project_dir), capture_output=True, text=True,
            )
            if ours_proc.returncode != 0 or theirs_proc.returncode != 0:
                truly_blocked.append(f)
                continue
            merged = driver(ours_proc.stdout, theirs_proc.stdout, None)
            if merged is None:
                truly_blocked.append(f)
                continue
            full_path = project_dir / f
            if is_discard_signal(merged):
                # Lockfile — delete it. The next build will regenerate.
                try:
                    full_path.unlink(missing_ok=True)
                    subprocess.run(
                        ["git", "rm", "-f", "--", f],
                        cwd=str(project_dir), capture_output=True,
                    )
                except OSError:
                    truly_blocked.append(f)
                    continue
            else:
                try:
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    full_path.write_text(merged, encoding="utf-8")
                    subprocess.run(
                        ["git", "add", "--", f],
                        cwd=str(project_dir), capture_output=True,
                    )
                except OSError:
                    truly_blocked.append(f)
                    continue
            structured_resolved.append(f)

        # If everything resolvable (noise or structured), complete the merge.
        if files and not truly_blocked:
            # Apply noise resolution.
            for f in noise_files:
                subprocess.run(
                    ["git", "checkout", "--ours", "--", f],
                    cwd=str(project_dir), capture_output=True,
                )
                subprocess.run(
                    ["git", "add", "--", f],
                    cwd=str(project_dir), capture_output=True,
                )
            parts: list[str] = []
            if noise_files:
                parts.append(f"{len(noise_files)} noise")
            if structured_resolved:
                parts.append(f"{len(structured_resolved)} structured")
            commit = subprocess.run(
                [
                    "git", "commit", "--no-edit",
                    "-m", f"merge {branch} (auto-resolved: {'; '.join(parts) or 'clean'})",
                ],
                cwd=str(project_dir), capture_output=True, text=True,
            )
            if commit.returncode == 0:
                return True, (
                    f"merged {branch} into {parent_integration_branch} "
                    f"(auto-resolved: {'; '.join(parts) or 'clean'})"
                )
            # Auto-resolve commit failed; fall through to abort.

        # Layer 4 fallback would dispatch an LLM resolver here; for now,
        # surface the unresolvable files honestly.
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=str(project_dir), capture_output=True,
        )
        if truly_blocked:
            return False, f"conflict on: {', '.join(truly_blocked[:5])}"
        # All files were auto-resolvable individually, but commit failed
        # (something downstream like hooks / index in bad state). Report
        # the *original* conflict set so the log isn't a bare "?". Without
        # this, the auto-resolution path emptied truly_blocked + the
        # working file list before we abort, and we'd lose track of what
        # the merge actually tripped on.
        if files:
            return False, (
                f"auto-resolve commit failed for {branch}; "
                f"originally conflicted on: {', '.join(files[:5])}"
            )
        return False, f"merge of {branch} aborted with no conflict files reported"
    except Exception as exc:  # noqa: BLE001
        return False, f"merge crashed: {exc}"


def merge_child_into_integration(
    *,
    project_dir: Path,
    child_task_id: str,
    parent_integration_branch: str,
) -> tuple[bool, str]:
    """Merge a child's BUILD branch (i2p/build/<id>) into its parent's
    integration branch. Thin wrapper over ``merge_branch_into``.
    """
    return merge_branch_into(
        project_dir=project_dir,
        source_branch=child_branch_name(child_task_id),
        target_branch=parent_integration_branch,
    )


def commit_worktree(*, worktree_path: Path, message: str) -> tuple[bool, str]:
    """Add+commit any changes in the child's worktree before merge.

    Belt-and-braces against agents committing artifacts:
      1. Ensure the worktree has a sensible .gitignore (seed defaults if
         absent, append defaults if present but missing key entries).
      2. Untrack any already-tracked paths that the (possibly-updated)
         .gitignore now matches — this catches files committed earlier
         on a stale ignore set.
      3. Then ``git add -A`` (which honors gitignore for untracked).

    Idempotent: if there's nothing to commit, returns (True, "no-op").
    """
    try:
        # 1. Ensure .gitignore covers the common artifact paths. Append rather
        # than overwrite so agents can still extend it.
        gi_path = worktree_path / ".gitignore"
        try:
            existing = gi_path.read_text(encoding="utf-8") if gi_path.exists() else ""
        except OSError:
            existing = ""
        # Sentinel marks our managed block; only inserted once.
        sentinel = "# --- otto v5 default ignores ---"
        if sentinel not in existing:
            new_text = existing.rstrip() + ("\n\n" if existing.strip() else "") + \
                       sentinel + "\n" + _DEFAULT_GITIGNORE
            try:
                gi_path.write_text(new_text, encoding="utf-8")
            except OSError as exc:
                logger.warning("could not update worktree .gitignore: %s", exc)

        # 2. Untrack already-tracked files that match the (now updated)
        # gitignore. ``git ls-files`` lists tracked files; ``check-ignore``
        # tells us which of those would now be ignored. ``rm --cached``
        # removes them from the index without touching the working tree.
        tracked_proc = subprocess.run(
            ["git", "ls-files"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
        )
        tracked = [
            p for p in (tracked_proc.stdout or "").splitlines() if p.strip()
        ]
        ignored = _gitignored_paths(worktree_path, tracked)
        if ignored:
            subprocess.run(
                ["git", "rm", "--cached", "--", *ignored],
                cwd=str(worktree_path),
                capture_output=True,
            )

        # 3. Stage everything else (gitignore already filters noise).
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
