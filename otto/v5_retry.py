"""Helpers for `otto v5 retry-children` — targeted broken-state recovery.

Born out of the iTracker Opus run (2026-05-20) where 3 of 4 feature
children ended in `merge_blocked` via fixable upstream bugs. After
fixing the bugs, we want to retry ONLY the affected children (~$30-90)
without re-running the whole pipeline (~$150 fresh).

Plan: plan-checkpoint-resume-v2.md Phase 1 (Codex Plan Gate APPROVED).

Locked invariants:
1. All-or-nothing transaction — full validation before any mutation;
   rollback on any failure inside the transaction.
2. Stable lock — `otto_logs/.locks/retry-children.lock` (NOT
   timestamped, must serialize concurrent retries; Codex R3#1).
3. TOCTOU revalidation under the lock (R5#3) — an external otto run or
   agent could have started between prevalidation and lock acquisition.
4. Refuses foundation, non-leaf, inline-decomposed tasks (R2#5).
5. Refuses retry-children on a `pass` child unless --force AND no
   downstream pass children depend on it (R2#4).
6. Dependency closure with --cascade-dependents (R2#4).
"""
from __future__ import annotations

import copy
import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ValidationFailure:
    task_id: str
    reason: str


@dataclass
class RetryPlan:
    """The fully-expanded plan that will (or would, in --dry-run) execute."""
    targets: list[str] = field(default_factory=list)
    cascaded: list[str] = field(default_factory=list)
    failures: list[ValidationFailure] = field(default_factory=list)
    sessions_to_archive: dict[str, Path] = field(default_factory=dict)
    worktrees: dict[str, Path] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def all_tasks(self) -> list[str]:
        return [*self.targets, *self.cascaded]


def _lock_path(project_dir: Path) -> Path:
    """Stable, single-canonical lock path (Codex R3#1)."""
    return project_dir / "otto_logs" / ".locks" / "retry-children.lock"


def _git_worktree_branch(worktree: Path) -> str | None:
    """Return the branch checked out in `worktree`, or None on failure."""
    try:
        r = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _git_worktree_is_dirty(worktree: Path) -> bool:
    """Return True if `worktree` has uncommitted changes."""
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True  # err on the safe side
    if r.returncode != 0:
        return True
    return bool(r.stdout.strip())


def _live_pids_in_path(target: Path) -> list[int]:
    """Return PIDs whose `lsof` output references `target` as cwd/file.

    Best-effort — used to refuse retry when a live otto/agent process is
    inside the target worktree or session. Returns [] if lsof unavailable.
    """
    try:
        r = subprocess.run(
            ["lsof", "+D", str(target)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    pids: set[int] = set()
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            try:
                pids.add(int(parts[1]))
            except ValueError:
                continue
    return sorted(pids)


def _branch_for_task(task_id: str) -> str:
    return f"i2p/build/{task_id}"


def _branch_exists(project_dir: Path, branch: str) -> bool:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _dependents_with_satisfactory_verdict(
    project_dir: Path, target_ids: set[str],
) -> list[str]:
    """Find any task that (a) `depends_on` one of `target_ids` AND
    (b) currently has a satisfactory-terminal verdict. These are the
    "would be stale after retry" tasks (Codex R2#4)."""
    from otto.queue.task_graph import entry_is_satisfactory_terminal, read_graph

    graph = read_graph(project_dir)
    tasks = graph.get("tasks") or {}
    if isinstance(tasks, list):
        tasks = {t["id"]: t for t in tasks if isinstance(t, dict)}
    dependents: list[str] = []
    for tid, t in tasks.items():
        if tid in target_ids:
            continue
        if not isinstance(t, dict):
            continue
        deps = set(t.get("depends_on") or [])
        if deps & target_ids:
            if entry_is_satisfactory_terminal(t):
                dependents.append(tid)
    return dependents


def _satisfactory_dependents_by_source(project_dir: Path) -> dict[str, list[str]]:
    """Map task_id -> satisfactory-terminal tasks depending on that task."""
    from otto.queue.task_graph import entry_is_satisfactory_terminal, read_graph

    graph = read_graph(project_dir)
    tasks = graph.get("tasks") or {}
    if isinstance(tasks, list):
        tasks = {t["id"]: t for t in tasks if isinstance(t, dict) and t.get("id")}
    if not isinstance(tasks, dict):
        return {}

    dependents_by_source: dict[str, list[str]] = {}
    for tid, task in tasks.items():
        if not isinstance(task, dict) or not entry_is_satisfactory_terminal(task):
            continue
        for dep in task.get("depends_on") or []:
            dep_id = str(dep or "")
            if dep_id:
                dependents_by_source.setdefault(dep_id, []).append(str(tid))
    return dependents_by_source


def _find_retry_dependency_cycle(
    dependents_by_source: dict[str, list[str]],
    roots: list[str],
) -> list[str] | None:
    """Iteratively detect a reachable cycle in dependency-closure expansion."""
    seen: set[str] = set()
    visiting: set[str] = set()

    for root in roots:
        if root in seen:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        path: list[str] = [root]
        visiting.add(root)

        while stack:
            node, index = stack[-1]
            neighbors = dependents_by_source.get(node, [])
            if index >= len(neighbors):
                visiting.discard(node)
                seen.add(node)
                stack.pop()
                path.pop()
                continue

            neighbor = neighbors[index]
            stack[-1] = (node, index + 1)
            if neighbor in visiting:
                cycle_start = path.index(neighbor)
                return [*path[cycle_start:], neighbor]
            if neighbor in seen:
                continue
            visiting.add(neighbor)
            path.append(neighbor)
            stack.append((neighbor, 0))

    return None


def _find_session_for_task(project_dir: Path, task_id: str) -> Path | None:
    """Locate the session dir whose `worktree` symlink points at
    `.worktrees/<task_id>`. Returns None if not found."""
    sessions_dir = project_dir / "otto_logs" / "sessions"
    if not sessions_dir.exists():
        return None
    # Reverse-lookup: scan sessions, follow `worktree` symlink.
    matches: list[tuple[str, Path]] = []
    for sdir in sorted(sessions_dir.iterdir()):
        if not sdir.is_dir() or sdir.name.startswith("."):
            continue
        wt_link = sdir / "worktree"
        if not wt_link.is_symlink():
            continue
        try:
            target = os.readlink(str(wt_link))
        except OSError:
            continue
        if Path(target).name == task_id:
            matches.append((sdir.name, sdir))
    if not matches:
        return None
    # Latest by name (sessions are timestamp-prefixed).
    matches.sort(key=lambda x: x[0])
    return matches[-1][1]


def _validate_retry_task(
    *,
    project_dir: Path,
    task_id: str,
    allow_continue_dirty: bool,
    force_pass: bool,
) -> tuple[list[ValidationFailure], Path | None, Path | None]:
    from otto.queue.task_graph import get_task

    task = get_task(project_dir, task_id)
    if task is None:
        return [ValidationFailure(task_id, "task not found in graph")], None, None

    if task.get("child_task_ids"):
        return [
            ValidationFailure(
                task_id,
                f"task has {len(task.get('child_task_ids') or [])} children; "
                f"retry only supports leaves. Use --fresh to rebuild a "
                f"non-leaf subtree.",
            )
        ], None, None
    if task.get("decomposition") == "emit":
        return [
            ValidationFailure(
                task_id,
                "task decomposed (decomposition=emit); not a leaf. "
                "Retry only supports leaves.",
            )
        ], None, None

    if str(task.get("task_role") or "") == "foundation":
        return [
            ValidationFailure(
                task_id,
                "task_role=foundation; foundation rebuild requires --fresh "
                "(it cascades to all dependent children).",
            )
        ], None, None

    if str(task.get("verdict") or "") == "pass" and not force_pass:
        return [
            ValidationFailure(
                task_id,
                f"verdict={task.get('verdict')}; use --force to retry a "
                f"pass-verdict task (consider --cascade-dependents to "
                f"invalidate downstream pass tasks too).",
            )
        ], None, None

    branch = _branch_for_task(task_id)
    if not _branch_exists(project_dir, branch):
        return [
            ValidationFailure(
                task_id,
                f"branch {branch} not found — task may have been pruned "
                f"or its decomposition was inline (no separate branch).",
            )
        ], None, None

    worktree = project_dir / ".worktrees" / task_id
    if not worktree.exists():
        return [
            ValidationFailure(
                task_id,
                f"worktree {worktree} not found — recreate it manually "
                f"or use --fresh.",
            )
        ], None, None
    current_branch = _git_worktree_branch(worktree)
    if current_branch != branch:
        return [
            ValidationFailure(
                task_id,
                f"worktree {worktree} is on branch {current_branch!r}, "
                f"expected {branch!r}.",
            )
        ], None, None
    if not allow_continue_dirty and _git_worktree_is_dirty(worktree):
        return [
            ValidationFailure(
                task_id,
                f"worktree {worktree} has uncommitted changes; commit "
                f"first or use --continue to commit-then-retry.",
            )
        ], None, None
    live_pids = _live_pids_in_path(worktree)
    if live_pids:
        return [
            ValidationFailure(
                task_id,
                f"live processes in worktree {worktree}: {live_pids}. "
                f"Refusing retry — stop them first.",
            )
        ], None, None

    session = _find_session_for_task(project_dir, task_id)
    if session is not None:
        live_session_pids = _live_pids_in_path(session)
        if live_session_pids:
            return [
                ValidationFailure(
                    task_id,
                    f"live processes in session {session}: "
                    f"{live_session_pids}. Refusing retry — stop them "
                    f"first.",
                )
            ], None, None

    return [], worktree, session


def validate_and_plan(
    *,
    project_dir: Path,
    task_ids: list[str],
    cascade_dependents: bool,
    allow_continue_dirty: bool,
    force_pass: bool,
) -> RetryPlan:
    """Full validation gate + dependency closure. NO mutations.

    Returns RetryPlan with `ok=True` if every check passes, or
    `ok=False` with failures listed. Caller (CLI) prints failures and
    exits non-zero on the False case.
    """
    plan = RetryPlan()

    # 1. Per-task validation gate.
    for tid in task_ids:
        failures, worktree, session = _validate_retry_task(
            project_dir=project_dir,
            task_id=tid,
            allow_continue_dirty=allow_continue_dirty,
            force_pass=force_pass,
        )
        if failures:
            plan.failures.extend(failures)
            continue
        if worktree is None:
            plan.failures.append(ValidationFailure(tid, "worktree validation failed"))
            continue
        plan.worktrees[tid] = worktree
        if session is not None:
            plan.sessions_to_archive[tid] = session
        plan.targets.append(tid)

    # 2. Dependency closure (only if base validation passed).
    if plan.targets:
        target_set = set(plan.targets)
        dependents = _dependents_with_satisfactory_verdict(project_dir, target_set)
        if dependents:
            if cascade_dependents:
                dependents_by_source = _satisfactory_dependents_by_source(project_dir)
                cycle = _find_retry_dependency_cycle(
                    dependents_by_source, plan.targets
                )
                if cycle:
                    plan.failures.append(ValidationFailure(
                        "(dependency-cycle)",
                        "cycle detected in retry dependency closure: "
                        + " -> ".join(cycle),
                    ))
                    return plan

                queue: list[str] = list(dependents)
                queued: set[str] = set(queue)
                while queue:
                    dep = queue.pop(0)
                    queued.discard(dep)
                    if dep in target_set:
                        continue
                    failures, worktree, session = _validate_retry_task(
                        project_dir=project_dir,
                        task_id=dep,
                        allow_continue_dirty=allow_continue_dirty,
                        force_pass=True,  # cascaded -> bypass pass check
                    )
                    target_set.add(dep)
                    if failures:
                        plan.failures.extend(failures)
                        continue
                    if worktree is None:
                        plan.failures.append(ValidationFailure(
                            dep, "worktree validation failed"
                        ))
                        continue
                    plan.cascaded.append(dep)
                    plan.worktrees[dep] = worktree
                    if session is not None:
                        plan.sessions_to_archive[dep] = session
                    for next_dep in dependents_by_source.get(dep, []):
                        if next_dep not in target_set and next_dep not in queued:
                            queue.append(next_dep)
                            queued.add(next_dep)
            else:
                plan.failures.append(ValidationFailure(
                    "(dependency-closure)",
                    f"refusing — {len(dependents)} downstream task(s) have "
                    f"satisfactory verdicts that would become stale: "
                    f"{', '.join(dependents)}. Re-run with "
                    f"--cascade-dependents to include them, or accept the "
                    f"stale risk by using --force on each.",
                ))

    return plan


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _archive_session(session_dir: Path) -> Path:
    """Rename `session_dir` to `<session_dir>.archived-<timestamp>`.
    Returns the new path."""
    archived = session_dir.parent / f"{session_dir.name}.archived-{_now_iso().replace(':', '')}"
    session_dir.rename(archived)
    return archived


@dataclass
class RetryExecution:
    """Result of executing a (validated) RetryPlan."""
    archived: dict[str, Path] = field(default_factory=dict)
    reset_task_ids: list[str] = field(default_factory=list)
    pending_summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    rolled_back: bool = False


def execute_plan(
    *,
    project_dir: Path,
    plan: RetryPlan,
) -> RetryExecution:
    """Execute the validated plan atomically (all-or-nothing).

    Order (Codex Plan Gate R4#3):
      1. Acquire stable lock.
      2. Revalidate under lock (TOCTOU — R5#3).
      3. Archive sessions.
      4. Snapshot graph entries for rollback.
      5. Reset graph entries (`clear_task_for_retry` per task).
      6. Rewrite pending file (`rewrite_pending_for_retry`).
      7. Release lock.

    On any failure at steps 3+, rollback (restore archived sessions,
    restore graph from snapshot, leave pending file with whatever the
    atomic-rewrite achieved or didn't).

    Caller MUST have already validated the plan.
    """
    from otto.queue.subtask import rewrite_pending_for_retry
    from otto.queue.task_graph import (
        clear_task_for_retry,
        mark_retry_in_progress,
        read_graph,
    )

    result = RetryExecution()
    if not plan.ok:
        result.error = "plan not validated; refusing to execute"
        return result
    if not plan.all_tasks:
        result.error = "empty plan; nothing to do"
        return result

    lock_path = _lock_path(project_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    retry_task_ids = list(dict.fromkeys(plan.all_tasks))
    retry_owner = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started_at": _now_iso(),
    }
    lock_acquired = False
    retry_marked = False

    import fcntl
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            result.error = (
                "could not acquire retry-children lock — another retry "
                "may be in progress"
            )
            return result
        lock_acquired = True

        mark_retry_in_progress(
            project_dir,
            retry_task_ids,
            True,
            owner=retry_owner,
        )
        retry_marked = True

        # 2. Revalidate under the lock (TOCTOU defense — R5#3).
        revalidation = validate_and_plan(
            project_dir=project_dir,
            task_ids=retry_task_ids,
            cascade_dependents=False,  # don't re-cascade
            allow_continue_dirty=False,
            force_pass=True,  # under-lock revalidation skips the force-pass UX gate
        )
        if not revalidation.ok:
            result.error = (
                "state changed between validation and lock acquisition: "
                + "; ".join(
                    f"{f.task_id}: {f.reason}" for f in revalidation.failures
                )
            )
            return result

        # 3. Archive sessions.
        for tid, sdir in list(plan.sessions_to_archive.items()):
            try:
                archived = _archive_session(sdir)
                result.archived[tid] = archived
            except OSError as exc:
                result.error = f"archive failed for {tid}: {exc}"
                _rollback_archives(result.archived)
                return result

        # 4. Snapshot graph entries (deep-copy so rollback is safe).
        graph_before = read_graph(project_dir)
        tasks_before = graph_before.get("tasks") or {}
        snapshot: dict[str, dict[str, Any]] = {}
        for tid in retry_task_ids:
            t = tasks_before.get(tid)
            if isinstance(t, dict):
                snapshot[tid] = copy.deepcopy(t)

        # 5. Reset graph entries (clear_task_for_retry atomically per task).
        try:
            for tid in retry_task_ids:
                clear_task_for_retry(
                    project_dir, tid, retry_reason="cli_retry_children"
                )
                result.reset_task_ids.append(tid)
        except Exception as exc:
            result.error = f"graph reset failed: {exc}"
            _rollback_graph(project_dir, snapshot)
            _rollback_archives(result.archived)
            result.rolled_back = True
            return result

        # 6. Rewrite pending file.
        try:
            pending_summary = rewrite_pending_for_retry(
                project_dir, retry_task_ids, retry_reason="cli_retry_children"
            )
            result.pending_summary = pending_summary
            if pending_summary.get("missing"):
                result.error = (
                    "pending rewrite could not synthesize task(s): "
                    + ", ".join(str(t) for t in pending_summary.get("missing", []))
                )
                _rollback_graph(project_dir, snapshot)
                _rollback_archives(result.archived)
                result.rolled_back = True
                return result
        except Exception as exc:
            result.error = f"pending rewrite failed: {exc}"
            _rollback_graph(project_dir, snapshot)
            _rollback_archives(result.archived)
            result.rolled_back = True
            return result

    finally:
        if retry_marked:
            try:
                mark_retry_in_progress(
                    project_dir,
                    retry_task_ids,
                    False,
                    owner=retry_owner,
                )
            except Exception:
                pass
        if lock_acquired:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        else:
            os.close(lock_fd)

    return result


def _rollback_archives(archived: dict[str, Path]) -> None:
    """Restore archived session dirs to their original names."""
    for _tid, archived_path in archived.items():
        original = archived_path.parent / archived_path.name.split(".archived-")[0]
        try:
            archived_path.rename(original)
        except OSError:
            # Best-effort; log via stderr below
            pass


def _rollback_graph(
    project_dir: Path,
    snapshot: dict[str, dict[str, Any]],
) -> None:
    """Restore the task graph entries from the snapshot taken pre-mutation."""
    from otto.queue.task_graph import _locked_graph

    try:
        with _locked_graph(project_dir) as (_path, graph):
            tasks = graph.get("tasks") or {}
            if isinstance(tasks, dict):
                for tid, entry in snapshot.items():
                    tasks[tid] = entry
            graph["tasks"] = tasks
    except Exception:
        # Last-resort: log; don't re-raise from rollback path
        pass
