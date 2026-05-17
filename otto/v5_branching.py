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

import contextlib
import json
import logging
import re
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger("otto.v5_branching")


class MergeWorktreeDirtyError(RuntimeError):
    """Raised when a merge target worktree is dirty before branch checkout."""

    def __init__(
        self,
        *,
        project_dir: Path,
        current_branch: str,
        target_branch: str,
        source_branch: str,
        dirty_status: list[str],
    ) -> None:
        self.project_dir = project_dir
        self.current_branch = current_branch
        self.target_branch = target_branch
        self.source_branch = source_branch
        self.dirty_status = tuple(dirty_status)
        preview = "\n".join(f"  {line}" for line in dirty_status[:20])
        if len(dirty_status) > 20:
            preview += f"\n  ... {len(dirty_status) - 20} more"
        super().__init__(
            "merge worktree dirty before checkout "
            f"(cwd={project_dir}, current={current_branch or 'HEAD'}, "
            f"target={target_branch}, source={source_branch}):\n{preview}"
        )


class ChildWorktreeSetupError(RuntimeError):
    """Raised when a child worktree cannot be established safely."""


def git_current_branch(project_dir: Path) -> str:
    """Return the checked-out branch name, or ``HEAD`` when detached/unknown."""
    cp = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    branch = (cp.stdout or "").strip()
    if cp.returncode == 0 and branch:
        return branch
    cp = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    branch = (cp.stdout or "").strip()
    if cp.returncode == 0 and branch:
        return branch
    return "HEAD"


def _worktree_path_for_branch(project_dir: Path, branch: str) -> Path | None:
    """Return the existing worktree that has ``branch`` checked out, if any."""
    cp = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        return None
    target_ref = f"refs/heads/{branch}"
    block_path: Path | None = None
    for raw_line in (cp.stdout or "").splitlines():
        if raw_line.startswith("worktree "):
            block_path = Path(raw_line[len("worktree "):].strip())
            continue
        if not raw_line.startswith("branch ") or block_path is None:
            continue
        ref = raw_line[len("branch "):].strip()
        if ref == target_ref or ref == branch:
            return block_path
    return None


def _git_common_dir(project_dir: Path) -> Path:
    cp = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        return project_dir / ".git"
    raw = (cp.stdout or "").strip()
    if not raw:
        return project_dir / ".git"
    path = Path(raw)
    if not path.is_absolute():
        path = project_dir / path
    return path


@contextlib.contextmanager
def _merge_target_lock(project_dir: Path, target_branch: str) -> Iterator[None]:
    """Serialize merges that mutate the same integration target branch."""
    import fcntl

    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", target_branch).strip("-") or "target"
    lock_dir = _git_common_dir(project_dir) / "otto-merge-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{safe}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(
            "target_branch="
            + target_branch
            + "\n"
            + "_written_at="
            + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            + "\n"
        )
        lock_file.flush()
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def git_status_porcelain(project_dir: Path) -> list[str]:
    """Return ``git status --porcelain`` lines for ``project_dir``."""
    cp = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "").strip()
        raise RuntimeError(f"git status failed in {project_dir}: {detail}")
    out: list[str] = []
    for line in (cp.stdout or "").splitlines():
        if not line.strip():
            continue
        # Linked worktrees live under `.worktrees/` and may be present in
        # older repos before Otto has refreshed its managed .gitignore block.
        # They are Otto's own git administration state, not product dirt.
        if line in {"?? .worktrees", "?? .worktrees/"}:
            continue
        out.append(line)
    return out


def _split_checkout_dirty_status(dirty: list[str]) -> tuple[list[str], list[str]]:
    """Split porcelain status into checkout-blocking tracked dirt and untracked files."""
    untracked = [line for line in dirty if line.startswith("?? ")]
    tracked = [line for line in dirty if not line.startswith("?? ")]
    return tracked, untracked


def assert_clean_before_checkout(
    *,
    project_dir: Path,
    source_branch: str,
    target_branch: str,
) -> None:
    """Fail before a checkout would carry dirty files across branches."""
    dirty = git_status_porcelain(project_dir)
    tracked_dirty, untracked = _split_checkout_dirty_status(dirty)
    if tracked_dirty:
        raise MergeWorktreeDirtyError(
            project_dir=project_dir,
            current_branch=git_current_branch(project_dir),
            target_branch=target_branch,
            source_branch=source_branch,
            dirty_status=dirty,
        )
    if untracked:
        preview = "\n".join(f"  {line}" for line in untracked[:20])
        if len(untracked) > 20:
            preview += f"\n  ... {len(untracked) - 20} more"
        logger.warning(
            "merge worktree has untracked files before checkout; proceeding "
            "because git preserves unrelated untracked files "
            "(cwd=%s, current=%s, target=%s, source=%s):\n%s",
            project_dir,
            git_current_branch(project_dir),
            target_branch,
            source_branch,
            preview,
        )


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


def latest_conflict_packet_path(project_dir: Path) -> Path:
    return project_dir / ".otto" / "merge-conflicts" / "latest.json"


def _git_show_stage(project_dir: Path, stage: int, path: str) -> str:
    proc = subprocess.run(
        ["git", "show", f":{stage}:{path}"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _write_conflict_packet(
    *,
    project_dir: Path,
    source_branch: str,
    target_branch: str,
    files: list[str],
    packet_project_dir: Path | None = None,
) -> Path:
    packet_dir = project_dir / ".otto" / "merge-conflicts"
    packet_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    safe_source = re.sub(r"[^A-Za-z0-9_.-]+", "-", source_branch)[:48]
    packet_path = packet_dir / f"{timestamp}-{safe_source}.json"
    payload = {
        "schema_version": 1,
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_branch": source_branch,
        "target_branch": target_branch,
        "unmerged_paths": list(files),
        "conflicts": [
            {
                "path": path,
                "base": _git_show_stage(project_dir, 1, path),
                "ours": _git_show_stage(project_dir, 2, path),
                "theirs": _git_show_stage(project_dir, 3, path),
            }
            for path in files
        ],
        "instruction": (
            "Analyze base, ours, and theirs for each path. Preserve both the "
            "target branch behavior and the source branch behavior; do not "
            "blindly choose an entire --ours or --theirs side."
        ),
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    packet_path.write_text(text, encoding="utf-8")
    latest_conflict_packet_path(project_dir).write_text(text, encoding="utf-8")
    if packet_project_dir is not None and packet_project_dir.resolve() != project_dir.resolve():
        mirror_dir = packet_project_dir / ".otto" / "merge-conflicts"
        mirror_dir.mkdir(parents=True, exist_ok=True)
        mirror_path = mirror_dir / packet_path.name
        mirror_path.write_text(text, encoding="utf-8")
        latest_conflict_packet_path(packet_project_dir).write_text(text, encoding="utf-8")
        return mirror_path
    return packet_path


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

# Otto runtime / orchestration artifacts
.worktrees/
.worktrees
otto_logs/
otto_logs
uploads/
uploads

# TypeScript incremental build cache — frequent cause of cross-worktree
# merge conflicts (different siblings produce different .tsbuildinfo for
# the same project). Tooling regenerates these on demand; don't commit.
*.tsbuildinfo
tsconfig.tsbuildinfo
tsconfig.*.tsbuildinfo

# Runtime artifacts — SQLite DBs, logs, pids. Same shape as tsbuildinfo:
# agents run their code, the runtime creates these files, `git add -A`
# tracks them, then merges conflict because each sibling has a divergent
# binary. Live run on 2026-05-13 had `api/itracker.db` block a merge.
*.db
*.db.bak
*.sqlite
*.sqlite3
*.sqlite3-journal
*.db-journal
*.db-wal
*.db-shm
*-wal
*-shm
*.pid
*.log
"""

# BEGIN/END markers wrap the managed block so re-runs can replace the
# block in place (adding new patterns to existing repos) without
# clobbering anything users put outside the markers.
_OTTO_GITIGNORE_BEGIN = "# --- otto v5 default ignores (BEGIN) ---"
_OTTO_GITIGNORE_END = "# --- otto v5 default ignores (END) ---"
# Legacy single-line sentinel from an earlier format. Migrated on first
# encounter (replaced with BEGIN/END block).
_OTTO_GITIGNORE_LEGACY_SENTINEL = "# --- otto v5 default ignores ---"


def _apply_managed_gitignore(gi_path: Path) -> bool:
    """Idempotently install / refresh the Otto-managed .gitignore block.

    Reads existing (if any), preserves anything outside the BEGIN/END
    markers, rewrites the managed block in place. Returns True if the
    file was modified, False if no change was needed.
    """
    try:
        existing = gi_path.read_text(encoding="utf-8") if gi_path.exists() else ""
    except OSError:
        existing = ""
    managed = (
        _OTTO_GITIGNORE_BEGIN + "\n"
        + _DEFAULT_GITIGNORE.rstrip() + "\n"
        + _OTTO_GITIGNORE_END
    )
    if _OTTO_GITIGNORE_BEGIN in existing and _OTTO_GITIGNORE_END in existing:
        import re as _re
        new_text = _re.sub(
            _re.escape(_OTTO_GITIGNORE_BEGIN) + r".*?" + _re.escape(_OTTO_GITIGNORE_END),
            managed,
            existing,
            flags=_re.DOTALL,
        )
    elif _OTTO_GITIGNORE_LEGACY_SENTINEL in existing:
        # Old format: legacy sentinel header but no end marker. Strip
        # everything from the legacy sentinel onward (it was Otto-managed)
        # and replace with the new bounded block.
        idx = existing.find(_OTTO_GITIGNORE_LEGACY_SENTINEL)
        preamble = existing[:idx].rstrip()
        sep = "\n\n" if preamble else ""
        new_text = preamble + sep + managed + "\n"
    else:
        sep = "\n\n" if existing.strip() else ""
        new_text = existing.rstrip() + sep + managed + "\n"
    if new_text == existing:
        return False
    try:
        gi_path.write_text(new_text, encoding="utf-8")
        return True
    except OSError as exc:
        logger.warning("could not write managed .gitignore: %s", exc)
        return False


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
        # Refresh the managed .gitignore block on every call, even for
        # existing repos. New patterns added to _DEFAULT_GITIGNORE
        # (e.g., the WAL sidecars or symlink no-slash forms) only land
        # on existing projects if we re-apply at pipeline start.
        _apply_managed_gitignore(project_dir / ".gitignore")

        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(project_dir),
            capture_output=True,
        )
        if head.returncode == 0:
            # If the refresh modified .gitignore, commit it so the
            # update propagates to subsequently-created child worktrees.
            status = subprocess.run(
                ["git", "status", "--porcelain", ".gitignore"],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
            )
            if status.stdout.strip():
                subprocess.run(
                    ["git", "add", ".gitignore"],
                    cwd=str(project_dir),
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "commit", "-m", "otto: refresh managed gitignore block"],
                    cwd=str(project_dir),
                    capture_output=True,
                )
            return False

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
) -> Path:
    """Create a worktree for ``child_task_id`` off the parent's integration branch.

    Raises when setup fails. Running a child in ``project_dir`` after a
    worktree identity failure is unsafe because child edits can leak into the
    parent/root worktree.
    """
    from otto.worktree import add_worktree

    # Ensure parent's integration branch exists. If we're root's child,
    # parent_integration_branch is "main" — must already exist.
    if parent_integration_branch != "main":
        ensure_branch_exists(project_dir, parent_integration_branch, base_ref="main")

    branch = child_branch_name(child_task_id)
    wt_path = child_worktree_path(project_dir, child_task_id)

    if wt_path.exists() and (wt_path / ".git").exists():
        # Already exists; reuse — but refresh the managed gitignore
        # block so existing worktrees pick up new patterns added since
        # the worktree was created.
        _apply_managed_gitignore(wt_path / ".gitignore")
        return wt_path

    try:
        add_worktree(
            project_dir=project_dir,
            worktree_path=wt_path,
            branch=branch,
            base_ref=parent_integration_branch,
        )
        # New worktree: ensure managed gitignore block is present from
        # day one (commit_worktree's apply happens at commit time, but
        # we want the patterns active throughout the agent's session).
        _apply_managed_gitignore(wt_path / ".gitignore")
        return wt_path
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "setup_child_worktree(%s) failed: %s",
            child_task_id, exc,
        )
        raise ChildWorktreeSetupError(
            f"setup_child_worktree({child_task_id}) failed: {exc}"
        ) from exc


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
        with _merge_target_lock(project_dir, parent_integration_branch):
            owner_worktree = _worktree_path_for_branch(
                project_dir,
                parent_integration_branch,
            )
            if owner_worktree is not None:
                merge_worktree = owner_worktree
            else:
                merge_worktree = project_dir
            return _merge_branch_into_worktree(
                project_dir=project_dir,
                merge_worktree=merge_worktree,
                branch=branch,
                parent_integration_branch=parent_integration_branch,
            )
    except MergeWorktreeDirtyError:
        raise
    except Exception as exc:  # noqa: BLE001
        return False, f"merge crashed: {exc}"


def _merge_branch_into_worktree(
    *,
    project_dir: Path,
    merge_worktree: Path,
    branch: str,
    parent_integration_branch: str,
) -> tuple[bool, str]:
    assert_clean_before_checkout(
        project_dir=merge_worktree,
        source_branch=branch,
        target_branch=parent_integration_branch,
    )
    if git_current_branch(merge_worktree) != parent_integration_branch:
        cp = subprocess.run(
            ["git", "checkout", parent_integration_branch],
            cwd=str(merge_worktree),
            capture_output=True,
            text=True,
        )
        if cp.returncode != 0:
            return False, f"checkout {parent_integration_branch} failed: {(cp.stderr or '').strip()}"
    # Attempt merge --no-ff (preserve graph).
    cp = subprocess.run(
        ["git", "merge", "--no-ff", "--no-edit", branch],
        cwd=str(merge_worktree),
        capture_output=True,
        text=True,
    )
    if cp.returncode == 0:
        return True, f"merged {branch} into {parent_integration_branch}"
    # Conflict — identify conflicting files.
    diag = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=str(merge_worktree),
        capture_output=True,
        text=True,
    )
    files = [f for f in (diag.stdout or "").strip().split("\n") if f]
    # Layer 2: gitignore-aware noise auto-resolve.
    noise_set = _gitignored_paths(merge_worktree, files)
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
            cwd=str(merge_worktree), capture_output=True, text=True,
        )
        theirs_proc = subprocess.run(
            ["git", "show", f":3:{f}"],
            cwd=str(merge_worktree), capture_output=True, text=True,
        )
        if ours_proc.returncode != 0 or theirs_proc.returncode != 0:
            truly_blocked.append(f)
            continue
        merged = driver(ours_proc.stdout, theirs_proc.stdout, None)
        if merged is None:
            truly_blocked.append(f)
            continue
        full_path = merge_worktree / f
        if is_discard_signal(merged):
            # Lockfile — delete it. The next build will regenerate.
            try:
                full_path.unlink(missing_ok=True)
                subprocess.run(
                    ["git", "rm", "-f", "--", f],
                    cwd=str(merge_worktree), capture_output=True,
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
                    cwd=str(merge_worktree), capture_output=True,
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
                cwd=str(merge_worktree), capture_output=True,
            )
            subprocess.run(
                ["git", "add", "--", f],
                cwd=str(merge_worktree), capture_output=True,
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
            cwd=str(merge_worktree), capture_output=True, text=True,
        )
        if commit.returncode == 0:
            return True, (
                f"merged {branch} into {parent_integration_branch} "
                f"(auto-resolved: {'; '.join(parts) or 'clean'})"
            )
        # Auto-resolve commit failed; fall through to abort.

    conflict_packet: Path | None = None
    if truly_blocked:
        try:
            conflict_packet = _write_conflict_packet(
                project_dir=merge_worktree,
                packet_project_dir=project_dir,
                source_branch=branch,
                target_branch=parent_integration_branch,
                files=truly_blocked,
            )
        except OSError as exc:
            logger.warning("could not write merge conflict packet: %s", exc)
    subprocess.run(
        ["git", "merge", "--abort"],
        cwd=str(merge_worktree), capture_output=True,
    )
    if truly_blocked:
        detail = f"conflict on: {', '.join(truly_blocked[:5])}"
        if conflict_packet is not None:
            detail += f"; conflict packet: {conflict_packet}"
        return False, detail
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
        _apply_managed_gitignore(worktree_path / ".gitignore")

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


_INTEGRATION_ALLOWED_PREFIXES = (
    "api/",
    "app/",
    "apps/",
    "backend/",
    "client/",
    "docs/",
    "frontend/",
    "lib/",
    "packages/",
    "public/",
    "scripts/",
    "server/",
    "spec/",
    "src/",
    "test/",
    "tests/",
    "web/",
)

_INTEGRATION_ALLOWED_EXACT = frozenset({
    ".gitignore",
    "CHARTER.md",
    "Dockerfile",
    "Makefile",
    "README.md",
    "decisions.md",
    "docker-compose.yml",
    "eslint.config.js",
    "index.html",
    "package-lock.json",
    "package.json",
    "pnpm-lock.yaml",
    "pyproject.toml",
    "requirements-dev.txt",
    "requirements.txt",
    "start.sh",
    "tsconfig.json",
    "uv.lock",
    "vite.config.js",
    "vite.config.ts",
    "yarn.lock",
})


def _normalise_git_path(path: str) -> str:
    path = (path or "").replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    return path


def _is_integration_product_path(path: str) -> bool:
    path = _normalise_git_path(path)
    return path in _INTEGRATION_ALLOWED_EXACT or any(
        path.startswith(prefix) for prefix in _INTEGRATION_ALLOWED_PREFIXES
    )


def _git_lines(repo: Path, args: list[str]) -> list[str]:
    cp = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return [line for line in (cp.stdout or "").splitlines() if line.strip()]


def _changed_paths(repo: Path) -> list[str]:
    paths = _git_lines(
        repo,
        ["ls-files", "--modified", "--deleted", "--others", "--exclude-standard"],
    )
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        norm = _normalise_git_path(path)
        if norm and norm not in seen:
            out.append(norm)
            seen.add(norm)
    return out


def _cached_name_status(repo: Path) -> list[tuple[str, str]]:
    lines = _git_lines(repo, ["diff", "--cached", "--name-status"])
    out: list[tuple[str, str]] = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append((parts[0].strip(), _normalise_git_path(parts[-1])))
    return out


def _allowed_cached_path(repo: Path, status: str, path: str) -> bool:
    if _is_integration_product_path(path):
        return True
    # Deleting runtime files from the index is allowed. This commits removal
    # from git history without committing the runtime file contents.
    if status.startswith("D") and path in _gitignored_paths(repo, [path]):
        return True
    return False


def commit_integration_worktree(
    *,
    worktree_path: Path,
    task_id: str,
) -> tuple[bool, str]:
    """Commit integration-agent product edits with a strict path allowlist.

    Unlike ``commit_worktree`` for leaf build branches, this deliberately does
    not run ``git add -A``. Integration agents may have produced runtime logs,
    uploads, databases, and nested worktrees while verifying the merged product.
    The runner stages only product/source paths plus removals of files now
    covered by the managed ignore block.
    """
    message = f"integration: {task_id} runner-managed changes"
    try:
        is_repo = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
        )
        if is_repo.returncode != 0:
            return True, "no-op (not a git repo)"

        _apply_managed_gitignore(worktree_path / ".gitignore")

        # Start from a clean index so an agent's accidental `git add -A` cannot
        # bypass the runner allowlist.
        reset = subprocess.run(
            ["git", "reset", "-q"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
        )
        if reset.returncode != 0:
            return False, f"git reset failed: {(reset.stderr or '').strip()}"

        tracked = _git_lines(worktree_path, ["ls-files"])
        ignored = _gitignored_paths(worktree_path, tracked)
        if ignored:
            rm = subprocess.run(
                ["git", "rm", "--cached", "--", *sorted(ignored)],
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
            )
            if rm.returncode != 0:
                return False, f"git rm --cached ignored files failed: {(rm.stderr or '').strip()}"

        changed = _changed_paths(worktree_path)
        allowed = [p for p in changed if _is_integration_product_path(p)]
        disallowed = [p for p in changed if p not in allowed]
        if allowed:
            add = subprocess.run(
                ["git", "add", "--", *allowed],
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
            )
            if add.returncode != 0:
                return False, f"git add allowlist failed: {(add.stderr or '').strip()}"

        bad_cached = [
            f"{status}\t{path}"
            for status, path in _cached_name_status(worktree_path)
            if not _allowed_cached_path(worktree_path, status, path)
        ]
        if bad_cached:
            return False, (
                "refusing to commit disallowed staged integration paths: "
                + ", ".join(bad_cached[:10])
            )

        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(worktree_path),
            capture_output=True,
        )
        if diff.returncode == 0:
            if disallowed:
                return False, (
                    "integration worktree has dirty paths outside commit allowlist: "
                    + ", ".join(disallowed[:10])
                )
            return True, "no-op (nothing to commit)"

        commit = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
        )
        if commit.returncode != 0:
            return False, f"git commit failed: {(commit.stderr or '').strip()}"

        remaining = git_status_porcelain(worktree_path)
        if remaining:
            return False, (
                "committed allowed integration changes, but worktree remains dirty: "
                + ", ".join(remaining[:10])
            )
        return True, f"committed: {message}"
    except Exception as exc:  # noqa: BLE001
        return False, f"integration commit crashed: {exc}"
