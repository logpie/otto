"""Otto git operations — branch management, workspace cleanup, candidate refs."""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from rich.markup import escape as rich_escape

from otto.theme import console
from otto.tasks import update_task

_CANDIDATE_ATTEMPT_RE = re.compile(r"/attempt-(\d+)$")


def _log_warn(msg: str) -> None:
    console.print(f"  [yellow]Warning: {rich_escape(msg)}[/yellow]")


# Otto-owned paths — dirty changes to these are ignored (not user code).
# Prefix-based: anything under these directories is otto-owned.
_OTTO_OWNED_FILES = {"tasks.yaml", ".tasks.lock"}
_OTTO_OWNED_PREFIXES = ("otto_logs/", "otto_arch/", ".otto-scratch/", ".otto/", ".otto-worktrees/")


def _is_otto_owned(filepath: str) -> bool:
    """Check if a file path belongs to otto runtime (not user code)."""
    return filepath in _OTTO_OWNED_FILES or any(
        filepath.startswith(p) for p in _OTTO_OWNED_PREFIXES
    )


def check_clean_tree(project_dir: Path) -> bool:
    """Check that tracked files have no uncommitted user changes.

    Otto-owned files (otto_logs/, tasks.yaml, etc.) are ignored.
    Returns False if user files are dirty — caller should refuse to run.
    No auto-stash — dirty user files are the user's responsibility.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain", "-uno"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False

    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) < 2:
            return False  # can't parse — assume dirty
        filename = parts[1].strip('"')
        if not _is_otto_owned(filename):
            return False

    return True


def _snapshot_untracked(project_dir: Path) -> set[str]:
    """Return the set of currently untracked files (excluding ignored).

    Used before agent runs so build_candidate_commit can distinguish
    pre-existing untracked files from agent-created ones.
    """
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=project_dir, capture_output=True, text=True,
    )
    return {f for f in result.stdout.split("\0") if f}


def _prune_empty_parents(path: Path, root: Path) -> None:
    """Remove empty parent directories up to, but not including, root."""
    current = path
    while current != root:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _remove_path(path: Path, root: Path) -> None:
    """Remove a file/symlink/directory and prune empty parents."""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            return
    _prune_empty_parents(path.parent, root)


def _remove_otto_created_untracked(
    project_dir: Path,
    pre_existing_untracked: set[str] | None,
) -> None:
    """Delete only untracked files created during the run."""
    if pre_existing_untracked is None:
        return

    current_untracked = _snapshot_untracked(project_dir)
    created_untracked = sorted(
        current_untracked - pre_existing_untracked,
        key=lambda rel: len(Path(rel).parts),
        reverse=True,
    )
    for rel_path in created_untracked:
        _remove_path(project_dir / rel_path, project_dir)


def _run_cleanup_git_command(
    project_dir: Path,
    cmd: list[str],
    action: str,
) -> subprocess.CompletedProcess:
    """Run a best-effort git cleanup command and warn on failure."""
    result = subprocess.run(
        cmd,
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        _log_warn(f"Cleanup failed during {action}: {details}")
    return result


def _worktrees_dir(repo_root: Path) -> Path:
    """Return the .otto-worktrees directory for task worktrees."""
    return repo_root / ".otto-worktrees"


def create_task_worktree(repo_root: Path, task_key: str, base_sha: str) -> Path:
    """Create a per-task git worktree for parallel execution.

    Uses detached HEAD at base_sha — no branch created.
    Returns the worktree path.
    """
    wt_dir = _worktrees_dir(repo_root)
    wt_dir.mkdir(parents=True, exist_ok=True)
    worktree_path = wt_dir / f"otto-task-{task_key}"

    # Remove stale worktree if it exists (crashed previous run)
    if worktree_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_root, capture_output=True,
        )
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)

    result = subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree_path), base_sha],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Failed to create worktree for {task_key}: {stderr}")

    return worktree_path


def cleanup_task_worktree(repo_root: Path, task_key: str) -> None:
    """Remove a task's worktree and prune metadata."""
    worktree_path = _worktrees_dir(repo_root) / f"otto-task-{task_key}"
    if worktree_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_root, capture_output=True,
        )
        # Belt and suspenders — remove leftover directory
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)
    # Prune any orphaned worktree metadata
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=repo_root, capture_output=True,
    )


def cleanup_all_worktrees(repo_root: Path) -> None:
    """Remove all otto task worktrees. Called on startup to clean up crashes."""
    wt_dir = _worktrees_dir(repo_root)
    if not wt_dir.exists():
        return
    for child in wt_dir.iterdir():
        if child.name.startswith("otto-task-") and child.is_dir():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(child)],
                cwd=repo_root, capture_output=True,
            )
            if child.exists():
                shutil.rmtree(child, ignore_errors=True)
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=repo_root, capture_output=True,
    )
    # Remove the directory itself if empty
    try:
        wt_dir.rmdir()
    except OSError:
        pass


def _restore_workspace_state(
    project_dir: Path,
    reset_ref: str | None = None,
    pre_existing_untracked: set[str] | None = None,
) -> None:
    """Restore tracked files and remove only Otto-created untracked files."""
    cmd = ["git", "reset", "--hard"]
    if reset_ref:
        cmd.append(reset_ref)
    _run_cleanup_git_command(project_dir, cmd, "git reset --hard")
    _remove_otto_created_untracked(project_dir, pre_existing_untracked)


def create_task_branch(
    project_dir: Path, key: str, default_branch: str,
    task: dict[str, Any] | None = None,
) -> str:
    """Create otto/<key> branch. Returns base SHA.

    If branch exists and was preserved from a diverge failure, raises RuntimeError.
    Otherwise deletes stale branch and recreates.
    """
    branch_name = f"otto/{key}"

    # Ensure we're on the default branch before branching
    current = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_dir, capture_output=True, text=True,
    ).stdout.strip()
    if current != default_branch:
        subprocess.run(
            ["git", "checkout", default_branch],
            cwd=project_dir, capture_output=True, check=True,
        )

    # Check if branch exists
    check = subprocess.run(
        ["git", "rev-parse", "--verify", branch_name],
        cwd=project_dir,
        capture_output=True,
    )
    if check.returncode == 0:
        # Check if this was preserved from a diverge failure (structured error_code)
        if task and task.get("status") == "failed" and task.get("error_code") == "merge_diverged":
            raise RuntimeError(
                f"Branch otto/{key} preserved from diverge failure — "
                f"manually resolve or run 'otto drop --all' first"
            )
        # Delete stale branch
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=project_dir,
            capture_output=True,
        )

    # Record base SHA
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    # Create and checkout new branch
    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=project_dir,
        capture_output=True,
        check=True,
    )

    return base_sha


def _should_stage_untracked(rel_path: str) -> bool:
    """Decide if an untracked file should be included in the candidate commit.

    Stages all project source files. Excludes otto runtime files and
    obvious build artifacts/caches — even if .gitignore doesn't cover them.
    """
    # Otto runtime files — never commit
    _OTTO_PATHS = ("otto_logs/", "otto_arch/", ".otto-scratch/",
                   ".otto-worktrees/", "tasks.yaml", ".tasks.lock", "otto.lock")
    if any(rel_path == p or rel_path.startswith(p) for p in _OTTO_PATHS):
        return False

    # Build artifacts and caches — never commit
    _ARTIFACT_PATTERNS = (
        "__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
        ".venv/", "node_modules/", ".next/", "dist/", "build/", "coverage/",
        "target/", ".turbo/", ".egg-info",
    )
    if any(p in rel_path for p in _ARTIFACT_PATTERNS):
        return False

    # Compiled files
    if rel_path.endswith((".pyc", ".pyo", ".o", ".so", ".dylib")):
        return False

    # Everything else is candidate-eligible (source files, assets, configs)
    return True


def build_candidate_commit(
    project_dir: Path,
    base_sha: str,
    pre_existing_untracked: set[str] | None = None,
) -> str:
    """Build a candidate commit with the agent's changes."""
    # If agent made commits, squash them
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()

    if head != base_sha:
        # Agent made commits — squash
        subprocess.run(
            ["git", "reset", "--mixed", base_sha],
            cwd=project_dir, capture_output=True, check=True,
        )

    # Stage all agent changes explicitly (never git add -A per spec)
    # Stage modified/deleted tracked files
    subprocess.run(
        ["git", "add", "-u"],
        cwd=project_dir, capture_output=True, check=True,
    )
    # Stage untracked project source files — includes both agent-created
    # AND pre-existing untracked files (they may be imported by agent code).
    # Excludes otto runtime files and build artifacts via _should_stage_untracked.
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=project_dir, capture_output=True, text=True,
    )
    for f in untracked.stdout.split("\0"):
        if f and _should_stage_untracked(f):
            subprocess.run(
                ["git", "add", "--", f],
                cwd=project_dir, capture_output=True,
            )

    # Create candidate commit
    subprocess.run(
        ["git", "commit", "-m", "otto: candidate commit", "--allow-empty"],
        cwd=project_dir, capture_output=True, check=True,
    )

    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()


def merge_to_default(project_dir: Path, key: str, default_branch: str) -> bool:
    """Fast-forward merge task branch to default branch. Returns True on success."""
    branch_name = f"otto/{key}"
    subprocess.run(
        ["git", "checkout", default_branch],
        cwd=project_dir, capture_output=True, check=True,
    )
    result = subprocess.run(
        ["git", "merge", "--ff-only", branch_name],
        cwd=project_dir, capture_output=True,
    )
    if result.returncode == 0:
        # Delete merged branch
        subprocess.run(
            ["git", "branch", "-d", branch_name],
            cwd=project_dir, capture_output=True,
        )
        return True
    # Merge failed (branch diverged) — stay on default branch, preserve task branch
    return False


def cleanup_branch(project_dir: Path, key: str, default_branch: str = "main") -> None:
    """Delete a task branch. Checks out default_branch if on the task branch."""
    branch_name = f"otto/{key}"
    current = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_dir, capture_output=True, text=True,
    ).stdout.strip()
    if current == branch_name:
        _run_cleanup_git_command(
            project_dir,
            ["git", "checkout", default_branch],
            f"git checkout {default_branch}",
        )
    _run_cleanup_git_command(
        project_dir,
        ["git", "branch", "-D", branch_name],
        f"git branch -D {branch_name}",
    )


def rebase_and_merge(project_dir: Path, task_branch: str, default_branch: str) -> bool:
    """Rebase task_branch onto default_branch then ff-only merge.

    Used for serial merge of parallel tasks.
    Returns False on rebase conflict.
    """
    # Rebase task branch onto default
    rebase = subprocess.run(
        ["git", "rebase", default_branch, task_branch],
        cwd=project_dir, capture_output=True, text=True,
    )
    if rebase.returncode != 0:
        # Abort the failed rebase
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=project_dir, capture_output=True,
        )
        return False

    # Fast-forward merge
    subprocess.run(
        ["git", "checkout", default_branch],
        cwd=project_dir, capture_output=True, check=True,
    )
    result = subprocess.run(
        ["git", "merge", "--ff-only", task_branch],
        cwd=project_dir, capture_output=True,
    )
    if result.returncode == 0:
        subprocess.run(
            ["git", "branch", "-d", task_branch],
            cwd=project_dir, capture_output=True,
        )
        return True
    return False


def cherry_pick_candidate(
    repo_root: Path,
    candidate_ref: str,
    default_branch: str,
) -> tuple[bool, str]:
    """Cherry-pick a candidate ref onto the current HEAD of default_branch.

    Creates a temporary branch and cherry-picks the candidate there. Used in
    the serial merge phase for parallel tasks.

    Returns (success, new_head_sha). On conflict, aborts and returns (False, "").
    The caller is responsible for fast-forwarding default_branch to new_head_sha
    after post-rebase verification passes.
    """
    temp_branch = f"otto/_merge_temp_{candidate_ref.replace('/', '_')}"

    # Resolve the candidate ref to a SHA
    resolve = subprocess.run(
        ["git", "rev-parse", candidate_ref],
        cwd=repo_root, capture_output=True, text=True,
    )
    if resolve.returncode != 0:
        return False, ""
    candidate_sha = resolve.stdout.strip()

    # Ensure we're on default branch
    subprocess.run(
        ["git", "checkout", default_branch],
        cwd=repo_root, capture_output=True, check=True,
    )

    # Create temp branch from current HEAD
    subprocess.run(
        ["git", "branch", "-D", temp_branch],
        cwd=repo_root, capture_output=True,  # ignore if not exists
    )
    subprocess.run(
        ["git", "checkout", "-b", temp_branch],
        cwd=repo_root, capture_output=True, check=True,
    )

    # Cherry-pick the candidate
    pick = subprocess.run(
        ["git", "cherry-pick", candidate_sha],
        cwd=repo_root, capture_output=True, text=True,
    )
    if pick.returncode != 0:
        # Abort cherry-pick and clean up
        subprocess.run(
            ["git", "cherry-pick", "--abort"],
            cwd=repo_root, capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", default_branch],
            cwd=repo_root, capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", temp_branch],
            cwd=repo_root, capture_output=True,
        )
        return False, ""

    # Get the new HEAD sha
    new_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Return to default branch and clean up the temp branch. The caller can
    # fast-forward default_branch to new_sha after verification passes.
    subprocess.run(
        ["git", "checkout", default_branch],
        cwd=repo_root, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "branch", "-D", temp_branch],
        cwd=repo_root, capture_output=True,
    )
    return True, new_sha


def _cleanup_task_failure(
    project_dir: Path,
    key: str,
    default_branch: str,
    tasks_file: Path | None,
    pre_existing_untracked: set[str] | None = None,
    error: str = "unknown",
    error_code: str = "unknown",
    cost_usd: float = 0.0,
    duration_s: float = 0.0,
    parallel: bool = False,
) -> None:
    """Unified cleanup for all task failure paths: retries exhausted, interruption, exceptions.

    In parallel mode (parallel=True), skips branch checkout/deletion since the
    task runs in a detached-HEAD worktree that the orchestrator cleans up.
    """
    _restore_workspace_state(
        project_dir,
        pre_existing_untracked=pre_existing_untracked,
    )
    if not parallel:
        _run_cleanup_git_command(
            project_dir,
            ["git", "checkout", default_branch],
            f"git checkout {default_branch}",
        )
        cleanup_branch(project_dir, key, default_branch)
    if tasks_file:
        try:
            updates: dict[str, Any] = {
                "status": "failed", "error": error, "error_code": error_code,
            }
            if cost_usd > 0:
                updates["cost_usd"] = cost_usd
            if duration_s > 0:
                updates["duration_s"] = round(duration_s, 1)
            update_task(tasks_file, key, **updates)
        except Exception:
            pass


def _anchor_candidate_ref(project_dir: Path, task_key: str, attempt_num: int, commit_sha: str) -> str:
    """Anchor a verified candidate as a durable git ref.

    Returns the ref name. SHAs without refs can become dangling after reset.
    """
    ref_name = f"refs/otto/candidates/{task_key}/attempt-{attempt_num}"
    result = subprocess.run(
        ["git", "update-ref", ref_name, commit_sha],
        cwd=project_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"failed to anchor candidate ref {ref_name}: {stderr or 'git update-ref failed'}")
    return ref_name


def _find_best_candidate_ref(project_dir: Path, task_key: str) -> str | None:
    """Find the best verified candidate ref for a task.

    Returns the ref name of the most recent verified candidate, or None.
    """
    result = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", f"refs/otto/candidates/{task_key}/"],
        cwd=project_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    refs = [r.strip() for r in result.stdout.strip().splitlines() if r.strip()]
    if not refs:
        return None

    def _sort_key(ref_name: str) -> tuple[int, str]:
        match = _CANDIDATE_ATTEMPT_RE.search(ref_name)
        attempt_num = int(match.group(1)) if match else -1
        return (attempt_num, ref_name)

    return max(refs, key=_sort_key)


def _get_diff_info(project_dir: Path, base_sha: str) -> dict[str, Any]:
    """Get diff info for QA tiering."""
    result = subprocess.run(
        ["git", "diff", "--name-only", base_sha, "HEAD"],
        cwd=project_dir, capture_output=True, text=True,
    )
    files = [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]

    full_diff = subprocess.run(
        ["git", "diff", base_sha, "HEAD"],
        cwd=project_dir, capture_output=True, text=True,
    )

    return {
        "files": files,
        "full_diff": full_diff.stdout.strip() if full_diff.returncode == 0 else "",
    }
