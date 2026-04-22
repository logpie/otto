"""Phase 4: low-level git wrappers used by the merge orchestrator.

All functions take `project_dir` and shell out to git. Returns are kept
simple (status code, stdout, stderr, parsed structures) — the orchestrator
makes decisions based on these.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_git(project_dir: Path, *args: str) -> GitResult:
    """Run `git <args>` in project_dir; return captured output."""
    cp = subprocess.run(
        ["git", *args],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    return GitResult(returncode=cp.returncode, stdout=cp.stdout, stderr=cp.stderr)


def head_sha(project_dir: Path) -> str:
    r = run_git(project_dir, "rev-parse", "HEAD")
    if not r.ok:
        raise RuntimeError(f"git rev-parse HEAD failed: {r.stderr.strip()}")
    return r.stdout.strip()


def head_parents(project_dir: Path, ref: str = "HEAD") -> list[str]:
    """Return parent SHAs of `ref` (1 for normal commit, 2 for merge commit, etc.)."""
    r = run_git(project_dir, "rev-list", "--parents", "-n", "1", ref)
    if not r.ok:
        raise RuntimeError(f"git rev-list failed: {r.stderr.strip()}")
    parts = r.stdout.strip().split()
    return parts[1:] if len(parts) > 1 else []


def current_branch(project_dir: Path) -> str:
    r = run_git(project_dir, "branch", "--show-current")
    if not r.ok:
        raise RuntimeError(f"git branch --show-current failed: {r.stderr.strip()}")
    return r.stdout.strip()


def branch_exists(project_dir: Path, branch: str) -> bool:
    r = run_git(project_dir, "rev-parse", "--verify", branch)
    return r.ok


def resolve_branch(project_dir: Path, branch: str) -> str:
    """Return the SHA the branch points at."""
    r = run_git(project_dir, "rev-parse", branch)
    if not r.ok:
        raise RuntimeError(f"branch {branch!r} not found")
    return r.stdout.strip()


def working_tree_clean(project_dir: Path) -> bool:
    """True iff `git status --porcelain` is empty."""
    return len(status_porcelain_entries(project_dir)) == 0


def status_porcelain_entries(project_dir: Path) -> list[str]:
    """Return raw `git status --porcelain` entries."""
    r = run_git(project_dir, "status", "--porcelain")
    if not r.ok:
        return []
    return [line.rstrip() for line in r.stdout.splitlines() if line.strip()]


def conflicted_files(project_dir: Path) -> list[str]:
    """Return list of paths in conflict (UU / AA / DD / etc. unmerged statuses).

    Parses `git status --porcelain`; unmerged states are XY where both X and Y
    are in {D, A, U} or both 'A'/'D'/'U' — we conservatively treat any line
    starting with 'UU', 'AA', 'DD', 'AU', 'UA', 'DU', 'UD' as conflicted.
    """
    r = run_git(project_dir, "status", "--porcelain")
    if not r.ok:
        return []
    out: list[str] = []
    for line in r.stdout.splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        if xy in ("UU", "AA", "DD", "AU", "UA", "DU", "UD"):
            out.append(line[3:].strip())
    return out


def untracked_files(project_dir: Path) -> list[str]:
    """Return untracked paths from `git status --porcelain`."""
    r = run_git(project_dir, "status", "--porcelain")
    if not r.ok:
        return []
    out: list[str] = []
    for line in r.stdout.splitlines():
        if line.startswith("?? "):
            out.append(line[3:].strip())
    return out


def changed_files(project_dir: Path, base: str = "HEAD") -> list[str]:
    """Return all files changed in worktree+index vs `base`."""
    r = run_git(project_dir, "diff", "--name-only", base)
    if not r.ok:
        return []
    return [line for line in r.stdout.splitlines() if line]


def changed_files_between(project_dir: Path, base_sha: str, head_sha: str) -> list[str]:
    """Files changed between two commits."""
    r = run_git(project_dir, "diff", "--name-only", base_sha, head_sha)
    if not r.ok:
        return []
    return [line for line in r.stdout.splitlines() if line]


def files_in_branch_diff(project_dir: Path, branch: str, target: str) -> list[str]:
    """Files that branch changed relative to target (used for collision preview)."""
    r = run_git(project_dir, "diff", "--name-only", f"{target}...{branch}")
    if not r.ok:
        return []
    return [line for line in r.stdout.splitlines() if line]


def merge_no_ff(project_dir: Path, branch: str, *, message: str | None = None) -> GitResult:
    """Run `git merge --no-ff <branch>` (no commit on conflict)."""
    args = ["merge", "--no-ff", "--no-edit"]
    if message:
        args += ["-m", message]
    args.append(branch)
    return run_git(project_dir, *args)


def merge_in_progress(project_dir: Path) -> bool:
    """True iff a merge is currently in progress (MERGE_HEAD exists)."""
    return (project_dir / ".git" / "MERGE_HEAD").exists() or _gitdir_has_merge_head(project_dir)


def _gitdir_has_merge_head(project_dir: Path) -> bool:
    """Resolve git dir for worktrees too."""
    r = run_git(project_dir, "rev-parse", "--git-dir")
    if not r.ok:
        return False
    return (Path(r.stdout.strip()) / "MERGE_HEAD").exists()


def add_paths(project_dir: Path, paths: list[str]) -> GitResult:
    return run_git(project_dir, "add", "--", *paths)


def commit_no_edit(project_dir: Path) -> GitResult:
    """Commit with the message git already prepared (e.g., merge message)."""
    return run_git(project_dir, "commit", "--no-edit")


def merge_abort(project_dir: Path) -> GitResult:
    return run_git(project_dir, "merge", "--abort")


def diff_check(project_dir: Path) -> GitResult:
    """`git diff --check` — catches unresolved markers and whitespace errors."""
    return run_git(project_dir, "diff", "--check")


def checkout(project_dir: Path, branch: str) -> GitResult:
    return run_git(project_dir, "checkout", branch)


def is_merge_commit(project_dir: Path, ref: str = "HEAD") -> bool:
    """True iff `ref` is a merge commit (has 2+ parents)."""
    return len(head_parents(project_dir, ref)) >= 2
