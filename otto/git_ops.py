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


def check_clean_tree(project_dir: Path) -> bool:
    """Check that tracked files have no uncommitted changes.

    Only checks tracked files — untracked files are fine.
    Otto runtime files (tasks.yaml, .tasks.lock) are ignored.
    If the tree is dirty with non-otto changes, auto-stash them.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain", "-uno"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False

    otto_runtime = {"tasks.yaml", ".tasks.lock"}
    has_non_otto_changes = False
    for line in result.stdout.strip().splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) < 2:
            has_non_otto_changes = True
            break
        filename = parts[1].strip('"')
        if filename not in otto_runtime:
            has_non_otto_changes = True
            break

    if has_non_otto_changes:
        # Auto-stash non-otto changes so we can proceed
        stash = subprocess.run(
            ["git", "stash", "push", "-m", "otto: auto-stash before run"],
            cwd=project_dir, capture_output=True, text=True,
        )
        if stash.returncode == 0 and "No local changes" not in stash.stdout:
            console.print("  Auto-stashed uncommitted changes", style="dim")
            return True
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
                f"manually resolve or run 'otto reset' first"
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
                   "tasks.yaml", ".tasks.lock", "otto.lock")
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
) -> None:
    """Unified cleanup for all task failure paths: retries exhausted, interruption, exceptions."""
    _restore_workspace_state(
        project_dir,
        pre_existing_untracked=pre_existing_untracked,
    )
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
