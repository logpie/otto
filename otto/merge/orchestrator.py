"""Python-driven merge orchestration.

Otto now has one merge strategy:
- merge each branch with `git merge --no-ff`
- commit conflicted files with markers so later branches can keep landing
- run one consolidated Claude session on the union of unresolved files
- validate the agent result, commit the cleanup, then run one cert call

Bookkeeping conflicts (`intent.md`, `otto.yaml`) are handled by git's
union/ours merge drivers; the Python loop only handles real code/content
conflicts. `state.json` is informative bookkeeping for reporting and
manual follow-up after a stopped merge.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import logging
import os
import re
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from otto import paths
from otto.merge import git_ops
from otto.observability import write_json_atomic
from otto.queue.artifacts import preserve_queue_session_artifacts
from otto.merge.state import (
    BranchOutcome,
    MergeState,
    load_state,
    new_merge_id,
    write_state,
)
from otto.merge.stories import collect_stories_from_branches, dedupe_stories, find_manifest_for_branch
from otto.runs.history import read_history_rows
from otto.runs.registry import (
    append_command_ack,
    begin_command_drain,
    finalize_record,
    finish_command_drain,
    make_run_record,
    update_record,
    publisher_for,
    write_record,
)
from otto.runs.schema import is_terminal_status

logger = logging.getLogger("otto.merge.orchestrator")

MERGE_LOCK_FILE = ".merge.lock"


class MergeAlreadyRunning(RuntimeError):
    """Raised when another `otto merge` process holds the merge lock."""

    def __init__(self, holder_pid: int | None):
        self.holder_pid = holder_pid
        super().__init__(
            "another otto merge is in progress "
            f"(holder PID={holder_pid if holder_pid is not None else '?'}); "
            "retry when it completes."
        )


@dataclass
class MergeRunResult:
    success: bool
    merge_id: str
    state: MergeState
    cert_passed: bool | None = None
    cert_story_results: list[dict[str, Any]] = field(default_factory=list)
    source_pow_paths: list[dict[str, str]] = field(default_factory=list)
    post_merge_pow_path: str | None = None
    note: str = ""


@dataclass
class MergeOptions:
    target: str = "main"
    no_certify: bool = False
    full_verify: bool = False
    fast: bool = False                  # pure git, bail on first conflict
    cleanup_on_success: bool = False    # remove worktrees after merge
    allow_any_branch: bool = False
    transactional: bool = False         # experimental: stage pure-git fast merges before target update


def _short_sha(value: str | None) -> str:
    return str(value or "")[:12]


def _partial_merge_note(project_dir: Path, state: MergeState, target_head_before: str) -> str:
    landed = [
        outcome.branch
        for outcome in state.outcomes
        if outcome.status in {"merged", "merged_with_markers", "conflict_resolved"}
    ]
    if not landed:
        return ""
    try:
        current_head = git_ops.head_sha(project_dir)
    except Exception:
        current_head = ""
    if current_head and target_head_before and current_head == target_head_before:
        return ""
    failed = [
        outcome.branch
        for outcome in state.outcomes
        if outcome.status == "agent_giveup"
    ]
    parts = [
        "Partial merge: target advanced"
        f" {_short_sha(target_head_before)} -> {_short_sha(current_head)};",
        f"landed {len(landed)} branch(es): {', '.join(landed)}.",
    ]
    if failed:
        parts.append(f"Needs action on: {', '.join(failed)}.")
    return " ".join(parts)


def _with_partial_merge_note(
    note: str,
    *,
    project_dir: Path,
    state: MergeState,
    target_head_before: str,
) -> str:
    partial = _partial_merge_note(project_dir, state, target_head_before)
    if not partial:
        return note
    return f"{note}\n\n{partial}" if note else partial


_ATOMIC_BRANCH_RE = re.compile(
    r"^(?P<mode>[a-z0-9][a-z0-9._-]*)/(?P<slug>[a-z0-9][a-z0-9-]*)-(?P<date>\d{4}-\d{2}-\d{2})$"
)


@contextmanager
def merge_lock(project_dir: Path):
    """Hold the per-project merge lock for the entire `otto merge` run."""
    path = paths.logs_dir(project_dir) / MERGE_LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder_pid = None
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                try:
                    handle.seek(0)
                    raw = handle.read().strip()
                    if raw:
                        holder = json.loads(raw)
                        if isinstance(holder, dict):
                            holder_pid = int(holder.get("pid")) if holder.get("pid") is not None else None
                except (OSError, ValueError, json.JSONDecodeError, TypeError):
                    holder_pid = None
                raise MergeAlreadyRunning(holder_pid) from exc
            raise

        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "started_at": _now_iso()}))
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _resolve_branches(
    project_dir: Path,
    *,
    explicit_ids_or_branches: list[str] | None,
    all_done_queue_tasks: bool,
    allow_any_branch: bool = False,
) -> tuple[list[str], dict[str, str]]:
    """Decide which branches to merge.

    Returns (branches, queue_task_lookup) where queue_task_lookup maps
    branch name → queue task id (used by stories.collect for fast lookup).
    """
    branches: list[str] = []
    lookup: dict[str, str] = {}
    already_merged_done: list[tuple[str, str]] = []

    if explicit_ids_or_branches:
        # Either queue task ids or raw branch names
        from otto.queue.schema import load_queue
        queue_load_error: Exception | None = None
        try:
            tasks = load_queue(project_dir)
        except (OSError, ValueError) as exc:
            logger.warning("could not load queue.yml for explicit-id resolution: %s", exc)
            queue_load_error = exc
            tasks = []
        by_id = {t.id: t for t in tasks}
        queue_branches = {t.branch for t in tasks if t.branch}
        for item in explicit_ids_or_branches:
            if item in by_id and by_id[item].branch:
                branches.append(by_id[item].branch)
                lookup[by_id[item].branch] = item
            elif git_ops.branch_exists(project_dir, item):
                _validate_managed_branch(
                    item,
                    queue_branches=queue_branches,
                    allow_any_branch=allow_any_branch,
                )
                branches.append(item)
            else:
                if queue_load_error is not None:
                    raise ValueError(
                        f"could not load queue.yml for task-id resolution: {queue_load_error}"
                    ) from queue_load_error
                raise ValueError(f"unknown task id or branch: {item!r}")

    if all_done_queue_tasks:
        from otto.queue.schema import load_queue, load_state
        try:
            tasks = load_queue(project_dir)
        except ValueError:
            raise
        except OSError as exc:
            logger.warning("could not load queue state for --all resolution: %s", exc)
            tasks = []
            state = {"tasks": {}}
        else:
            try:
                state = load_state(project_dir)
            except (OSError, ValueError) as exc:
                logger.warning("could not load queue state for --all resolution: %s", exc)
                state = {"tasks": {}}
        done_ids = {
            tid for tid, ts in state.get("tasks", {}).items()
            if ts.get("status") == "done"
        }
        already_merged = _previously_merged_branches(project_dir)
        for t in tasks:
            if t.id in done_ids and t.branch and t.branch in already_merged:
                already_merged_done.append((t.id, t.branch))
                continue
            if (
                t.id in done_ids
                and t.branch
                and t.branch not in branches
            ):
                branches.append(t.branch)
                lookup[t.branch] = t.id

    if not branches:
        # If --all found no done tasks but failed/queued tasks have branches
        # with commits, list them in the error so the user can salvage via
        # explicit-branch merge instead of seeing a generic "no branches".
        if all_done_queue_tasks:
            if already_merged_done:
                preview = ", ".join(
                    f"{task_id} ({branch})" for task_id, branch in already_merged_done[:5]
                )
                if len(already_merged_done) > 5:
                    preview += f", ... (+{len(already_merged_done) - 5} more)"
                raise ValueError(
                    "no unmerged done branches to merge; already merged: "
                    f"{preview}"
                )
            non_done_with_branch = [
                (tid, ts.get("status"), t.branch)
                for tid, ts in state.get("tasks", {}).items()
                if ts.get("status") not in ("done", "queued")
                for t in tasks if t.id == tid and t.branch
            ]
            if non_done_with_branch:
                hint_lines = [
                    "no DONE branches to merge, but the following queued "
                    "tasks have branches with commits:",
                ]
                for tid, status, branch in non_done_with_branch[:10]:
                    hint_lines.append(f"  - {tid} ({status}): {branch}")
                hint_lines.append(
                    "Merge them explicitly if you've reviewed the work: "
                    f"  otto merge {' '.join(b for _,_,b in non_done_with_branch[:3])}"
                )
                raise ValueError("\n".join(hint_lines))
        raise ValueError(
            "no branches to merge (queue has no done tasks; pass explicit "
            "task ids or branch names)"
        )
    return (branches, lookup)


def _previously_merged_branches(project_dir: Path) -> set[str]:
    merged: set[str] = set()
    for state_path in sorted(paths.merge_dir(project_dir).glob("*/state.json")):
        try:
            state = load_state(project_dir, state_path.parent.name)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        for outcome in state.outcomes:
            if outcome.status in {"merged", "conflict_resolved"}:
                merged.add(outcome.branch)
    return merged


def _looks_like_atomic_mode_branch(branch: str) -> bool:
    match = _ATOMIC_BRANCH_RE.fullmatch(branch)
    if match is None:
        return False
    try:
        datetime.strptime(match.group("date"), "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _validate_managed_branch(
    branch: str,
    *,
    queue_branches: set[str],
    allow_any_branch: bool,
) -> None:
    if allow_any_branch:
        return
    if branch in queue_branches or _looks_like_atomic_mode_branch(branch):
        return
    raise ValueError(
        "branch "
        f"'{branch}' is not a queue task or atomic-mode branch; otto merge only works on "
        "otto-managed branches. Use plain `git merge` for arbitrary branches."
    )


def _merge_artifacts(project_dir: Path, merge_id: str) -> dict[str, Any]:
    merge_run_dir = paths.merge_dir(project_dir) / merge_id
    return {
        "session_dir": str(merge_run_dir),
        "manifest_path": None,
        "checkpoint_path": None,
        "summary_path": None,
        "primary_log_path": str(paths.merge_dir(project_dir) / "merge.log"),
        "extra_log_paths": [str(merge_run_dir / "state.json")],
    }


def _terminal_outcome_for_status(status: str) -> str | None:
    mapping = {
        "done": "success",
        "failed": "failure",
        "cancelled": "cancelled",
        "removed": "removed",
        "interrupted": "interrupted",
    }
    return mapping.get(str(status or ""))


def _persist_merge_terminal_state(
    project_dir: Path,
    state: MergeState,
    *,
    status: str,
    note: str,
) -> MergeState:
    state.status = status
    state.terminal_outcome = _terminal_outcome_for_status(status)
    state.note = note
    state.finished_at = state.finished_at or _now_iso()
    write_state(project_dir, state)
    return state


def _append_merge_history(project_dir: Path, state: MergeState) -> None:
    from otto.runs.history import append_history_snapshot, build_terminal_snapshot

    merge_run_dir = paths.merge_dir(project_dir) / state.merge_id
    extra_artifacts: list[str] = []
    if state.cert_run_id:
        cert_session_dir = paths.session_dir(project_dir, state.cert_run_id)
        extra_artifacts.extend(
            [
                str(paths.session_summary(project_dir, state.cert_run_id)),
                str(cert_session_dir / "manifest.json"),
                str(paths.certify_dir(project_dir, state.cert_run_id) / "proof-of-work.html"),
            ]
        )
    append_history_snapshot(
        project_dir,
        build_terminal_snapshot(
            run_id=state.merge_id,
            domain="merge",
            run_type="merge",
            command="merge",
            intent_meta={
                "summary": f"merge {len(state.branches_in_order)} branch(es)",
                "intent_path": str(paths.project_intent_md(project_dir)),
                "spec_path": None,
            },
            status=state.status,
            terminal_outcome=state.terminal_outcome,
            timing={
                "started_at": state.started_at or None,
                "finished_at": state.finished_at or _now_iso(),
                "timestamp": state.finished_at or _now_iso(),
            },
            git={
                "branch": state.target,
                "target_branch": state.target,
                "head_sha": state.target_head_before,
            },
            source={"resumable": False},
            identity={"merge_id": state.merge_id},
            artifacts={
                "session_dir": str(merge_run_dir),
                "manifest_path": None,
                "checkpoint_path": None,
                "summary_path": None,
                "primary_log_path": str(paths.merge_dir(project_dir) / "merge.log"),
                "extra_log_paths": [str(merge_run_dir / "state.json"), *extra_artifacts],
            },
        ),
        strict=True,
    )


def _merge_run_artifacts(project_dir: Path, state: MergeState) -> dict[str, Any]:
    merge_run_dir = paths.merge_dir(project_dir) / state.merge_id
    artifacts = _merge_artifacts(project_dir, state.merge_id)
    if state.cert_run_id:
        cert_session_dir = paths.session_dir(project_dir, state.cert_run_id)
        artifacts["extra_log_paths"] = [
            *list(artifacts.get("extra_log_paths") or []),
            str(paths.session_summary(project_dir, state.cert_run_id)),
            str(cert_session_dir / "manifest.json"),
            str(paths.certify_dir(project_dir, state.cert_run_id) / "proof-of-work.html"),
        ]
    artifacts["session_dir"] = str(merge_run_dir)
    return artifacts


def _write_merge_run_record(project_dir: Path, state: MergeState, *, status: str) -> None:
    record = make_run_record(
        project_dir=project_dir,
        run_id=state.merge_id,
        domain="merge",
        run_type="merge",
        command="merge",
        display_name=f"merge: {len(state.branches_in_order)} branch(es)",
        status=status,
        cwd=project_dir,
        writer_id=f"merge:{state.merge_id}",
        identity={"merge_id": state.merge_id},
        source={"invoked_via": "cli", "argv": ["merge"], "resumable": False},
        git={"branch": state.target, "worktree": None, "target_branch": state.target, "head_sha": state.target_head_before},
        intent={
            "summary": f"merge {len(state.branches_in_order)} branch(es)",
            "intent_path": str(paths.project_intent_md(project_dir)),
            "spec_path": None,
        },
        artifacts=_merge_run_artifacts(project_dir, state),
        adapter_key="merge.run",
        last_event=str(state.note or status),
    )
    record.timing["started_at"] = state.started_at or record.timing.get("started_at")
    if state.finished_at:
        record.timing["finished_at"] = state.finished_at
    write_record(project_dir, record)


def _repair_merge_run_records(project_dir: Path) -> None:
    for state_path in sorted(paths.merge_dir(project_dir).glob("*/state.json")):
        merge_id = state_path.parent.name
        try:
            state = load_state(project_dir, merge_id)
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            continue
        status = str(state.status or "running")
        terminal_status = is_terminal_status(status) and bool(state.finished_at)
        updates = {
            "identity": {"merge_id": state.merge_id},
            "git": {
                "branch": state.target,
                "worktree": None,
                "target_branch": state.target,
                "head_sha": state.target_head_before,
            },
            "intent": {
                "summary": f"merge {len(state.branches_in_order)} branch(es)",
                "intent_path": str(paths.project_intent_md(project_dir)),
                "spec_path": None,
            },
            "artifacts": _merge_run_artifacts(project_dir, state),
            "last_event": str(state.note or status),
            "timing": {
                "started_at": state.started_at or None,
                "finished_at": state.finished_at or None,
            },
        }
        try:
            if terminal_status:
                finalize_record(
                    project_dir,
                    merge_id,
                    status=status,
                    terminal_outcome=state.terminal_outcome or _terminal_outcome_for_status(status),
                    updates=updates,
                )
            else:
                update_record(project_dir, merge_id, {"status": status, **updates}, heartbeat=False)
        except FileNotFoundError:
            if not terminal_status:
                continue
            _write_merge_run_record(project_dir, state, status=status)
            finalize_record(
                project_dir,
                merge_id,
                status=status,
                terminal_outcome=state.terminal_outcome or _terminal_outcome_for_status(status),
                updates=updates,
            )


def _repair_merge_history(project_dir: Path) -> None:
    history_rows = read_history_rows(paths.history_jsonl(project_dir))
    seen = {
        str(row.get("dedupe_key") or "")
        for row in history_rows
        if isinstance(row, dict)
    }
    for state_path in sorted(paths.merge_dir(project_dir).glob("*/state.json")):
        merge_id = state_path.parent.name
        dedupe_key = f"terminal_snapshot:{merge_id}"
        if dedupe_key in seen:
            continue
        try:
            state = load_state(project_dir, merge_id)
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            continue
        if not state.finished_at or state.status not in {"done", "failed", "cancelled"}:
            continue
        _append_merge_history(project_dir, state)
        seen.add(dedupe_key)


def _drain_merge_cancel_commands(project_dir: Path, merge_id: str, state: MergeState) -> bool:
    commands = begin_command_drain(
        paths.merge_command_requests(project_dir),
        paths.merge_command_requests_processing(project_dir),
        paths.merge_command_acks(project_dir),
    )
    cancelled = False
    state_version: int | None = None
    for cmd in commands:
        kind = str(cmd.get("kind") or cmd.get("cmd") or "")
        matches_cancel = kind == "cancel" and str(cmd.get("run_id") or "") == merge_id
        if matches_cancel:
            if not cancelled:
                _persist_merge_terminal_state(
                    project_dir,
                    state,
                    status="cancelled",
                    note="cancelled by command",
                )
                state_version = len(state.outcomes)
            cancelled = True
        append_command_ack(
            paths.merge_command_acks(project_dir),
            cmd,
            writer_id=f"merge:{merge_id}",
            outcome="applied" if matches_cancel else "ignored",
            state_version=state_version,
        )
    if commands:
        finish_command_drain(paths.merge_command_requests_processing(project_dir))
    return cancelled


def _cancelled_merge_result(
    project_dir: Path,
    *,
    state: MergeState,
    merge_id: str,
    note: str,
) -> MergeRunResult:
    _persist_merge_terminal_state(
        project_dir,
        state,
        status="cancelled",
        note=note,
    )
    return MergeRunResult(
        success=False,
        merge_id=merge_id,
        state=state,
        note=note,
    )


def _remove_transactional_staging(project_dir: Path, staging_path: Path, staging_branch: str) -> None:
    if staging_path.exists():
        result = git_ops.run_git(project_dir, "worktree", "remove", "--force", str(staging_path))
        if not result.ok:
            logger.warning("failed to remove transactional staging worktree %s: %s", staging_path, result.stderr.strip())
    if git_ops.branch_exists(project_dir, staging_branch):
        result = git_ops.run_git(project_dir, "branch", "-D", staging_branch)
        if not result.ok:
            logger.warning("failed to remove transactional staging branch %s: %s", staging_branch, result.stderr.strip())


def _mark_transactional_staging_rolled_back(state: MergeState, reason: str) -> None:
    for outcome in state.outcomes:
        if outcome.status != "merged":
            continue
        if not str(outcome.note or "").startswith("staged on "):
            continue
        outcome.status = "skipped"
        outcome.merge_commit = None
        outcome.note = reason


def _run_transactional_fast_merge(
    *,
    project_dir: Path,
    options: MergeOptions,
    state: MergeState,
    merge_id: str,
    branches: list[str],
    queue_lookup: dict[str, str],
    target_head_before: str,
) -> MergeRunResult:
    """Stage pure-git fast merges outside the target, then fast-forward target.

    This intentionally supports only ``--fast``. Agent conflict resolution and
    post-merge certification need a broader refactor to separate "repository
    under test" from "project directory where Otto writes state".
    """
    if _drain_merge_cancel_commands(project_dir, merge_id, state):
        return _cancelled_merge_result(
            project_dir,
            state=state,
            merge_id=merge_id,
            note="merge cancelled before transactional staging",
        )
    staging_branch = f"otto/merge-staging/{merge_id}"
    staging_path = paths.merge_dir(project_dir) / merge_id / "staging-worktree"
    if staging_path.exists():
        return MergeRunResult(
            success=False,
            merge_id=merge_id,
            state=state,
            note=f"transactional staging path already exists: {staging_path}",
        )

    add_result = git_ops.run_git(
        project_dir,
        "worktree",
        "add",
        "-b",
        staging_branch,
        str(staging_path),
        target_head_before,
    )
    if not add_result.ok:
        return MergeRunResult(
            success=False,
            merge_id=merge_id,
            state=state,
            note=f"transactional staging setup failed: {(add_result.stderr or add_result.stdout).strip()}",
        )

    try:
        for branch in branches:
            if _drain_merge_cancel_commands(project_dir, merge_id, state):
                _mark_transactional_staging_rolled_back(
                    state,
                    "transactional staging discarded because merge was cancelled",
                )
                write_state(project_dir, state)
                return _cancelled_merge_result(
                    project_dir,
                    state=state,
                    merge_id=merge_id,
                    note=f"merge cancelled while staging {branch}",
                )
            result = git_ops.merge_no_ff(staging_path, branch)
            if result.ok:
                state.outcomes.append(BranchOutcome(
                    branch=branch,
                    status="merged",
                    merge_commit=git_ops.head_sha(staging_path),
                    note=f"staged on {staging_branch}",
                ))
                write_state(project_dir, state)
                continue

            conflicts = git_ops.conflicted_files(staging_path)
            if conflicts:
                git_ops.merge_abort(staging_path)
                _mark_transactional_staging_rolled_back(
                    state,
                    "transactional staging discarded because a later branch conflicted",
                )
                state.outcomes.append(BranchOutcome(
                    branch=branch,
                    status="agent_giveup",
                    note="conflict in transactional --fast staging; target unchanged",
                ))
                write_state(project_dir, state)
                return MergeRunResult(
                    success=False,
                    merge_id=merge_id,
                    state=state,
                    note=(
                        f"transactional --fast stopped on conflict in {branch!r}; "
                        f"target {options.target!r} remains at {_short_sha(target_head_before)}. "
                        "Retry without --transactional to resolve manually or with agent mode."
                    ),
                )

            _mark_transactional_staging_rolled_back(
                state,
                "transactional staging discarded because a later branch failed",
            )
            state.outcomes.append(BranchOutcome(
                branch=branch,
                status="agent_giveup",
                note=f"git merge failed in transactional staging: {result.stderr.strip()[:200]}",
            ))
            write_state(project_dir, state)
            return MergeRunResult(
                success=False,
                merge_id=merge_id,
                state=state,
                note=f"transactional staging merge of {branch!r} failed: {result.stderr.strip()[:200]}",
            )

        current_head = git_ops.head_sha(project_dir)
        if current_head != target_head_before:
            _mark_transactional_staging_rolled_back(
                state,
                "transactional staging discarded because target moved",
            )
            write_state(project_dir, state)
            return MergeRunResult(
                success=False,
                merge_id=merge_id,
                state=state,
                note=(
                    f"transactional merge aborted: target moved from "
                    f"{_short_sha(target_head_before)} to {_short_sha(current_head)} "
                    "before staged changes could land."
                ),
            )
        ff_result = git_ops.run_git(project_dir, "merge", "--ff-only", staging_branch)
        if not ff_result.ok:
            _mark_transactional_staging_rolled_back(
                state,
                "transactional staging discarded because target fast-forward failed",
            )
            write_state(project_dir, state)
            return MergeRunResult(
                success=False,
                merge_id=merge_id,
                state=state,
                note=f"transactional fast-forward failed: {(ff_result.stderr or ff_result.stdout).strip()}",
            )
        if options.cleanup_on_success:
            _graduate_merged_task_sessions(project_dir, queue_lookup)
        return MergeRunResult(
            success=True,
            merge_id=merge_id,
            state=state,
            note=(
                f"transactional --fast merged {len(branches)} branch(es) in staging, "
                f"then fast-forwarded {options.target}"
            ),
        )
    finally:
        _remove_transactional_staging(project_dir, staging_path, staging_branch)


async def run_merge(
    *,
    project_dir: Path,
    config: dict[str, Any],
    options: MergeOptions,
    explicit_ids_or_branches: list[str] | None = None,
    all_done_queue_tasks: bool = False,
    budget: Any | None = None,
) -> MergeRunResult:
    """Main entry. Returns MergeRunResult with success/state/plan."""
    from otto.config import agent_provider, repo_preflight_issues
    from otto.runs.registry import garbage_collect_live_records

    _repair_merge_run_records(project_dir)
    _repair_merge_history(project_dir)
    garbage_collect_live_records(project_dir)

    # Pre-flight: must be on target, working tree clean
    cur = git_ops.current_branch(project_dir)
    if cur != options.target:
        return MergeRunResult(
            success=False, merge_id="", state=MergeState(),
            note=f"must be on {options.target!r}; currently on {cur!r}. Run `git checkout {options.target}` first.",
        )
    preflight = repo_preflight_issues(project_dir)
    # W5-CRITICAL-1: the merge orchestrator must mirror the web-action
    # preflight and refuse on user-owned untracked files too. Without
    # this, even if the web layer's ``_merge_preflight`` started to
    # block, a CLI ``otto merge --fast`` would still slip the silent
    # merge through.
    problems = [
        *preflight["blocking"],
        *preflight["dirty"],
        *preflight.get("untracked", []),
    ]
    if problems:
        dirty_files = list(preflight.get("dirty_files", []) or [])
        preview = ", ".join(dirty_files[:5])
        if len(dirty_files) > 5:
            preview += f", ... (+{len(dirty_files) - 5} more)"
        detail_parts = list(problems)
        if preview:
            detail_parts.append(f"affected paths: {preview}")
        return MergeRunResult(
            success=False, merge_id="", state=MergeState(),
            note=(
                "working tree must be clean before merge "
                f"({'; '.join(detail_parts)}). "
                "Commit, stash, or clean tracked/staged changes, resolve any in-progress "
                "merge/rebase, and retry."
            ),
        )

    # Optional precondition: bookkeeping merge drivers must be set up
    # (skipped if user opted out via queue.bookkeeping_files: [])
    bookkeeping = (config.get("queue") or {}).get("bookkeeping_files") or []
    if bookkeeping:
        from otto.setup_gitattributes import GitAttributesConflict, assert_setup
        try:
            assert_setup(project_dir)
        except GitAttributesConflict as exc:
            return MergeRunResult(
                success=False, merge_id="", state=MergeState(),
                note=f".gitattributes precondition failed: {exc}",
            )

    branches, queue_lookup = _resolve_branches(
        project_dir,
        explicit_ids_or_branches=explicit_ids_or_branches,
        all_done_queue_tasks=all_done_queue_tasks,
        allow_any_branch=options.allow_any_branch,
    )
    if options.transactional and not options.fast:
        return MergeRunResult(
            success=False,
            merge_id="",
            state=MergeState(),
            note="--transactional currently supports only --fast pure-git merges.",
        )
    provider = agent_provider(config)
    if provider != "claude" and not options.fast:
        return MergeRunResult(
            success=False,
            merge_id="",
            state=MergeState(),
            note=(
                f"otto merge requires the 'claude' provider for conflict resolution "
                f"(got {provider!r}). Either switch provider in otto.yaml, OR use "
                f"`otto merge --fast` (pure git, bail on first conflict, no agent)."
            ),
        )

    merge_id = new_merge_id()
    target_head_before = git_ops.head_sha(project_dir)
    state = MergeState(
        merge_id=merge_id,
        started_at=_now_iso(),
        target=options.target,
        target_head_before=target_head_before,
        branches_in_order=list(branches),
        outcomes=[],
    )
    write_state(project_dir, state)
    publisher = publisher_for(
        "merge",
        "merge",
        "merge",
        project_dir=project_dir,
        run_id=merge_id,
        intent=f"merge {len(branches)} branch(es)",
        display_name=f"merge: {len(branches)} branch(es)",
        cwd=project_dir,
        identity={"merge_id": merge_id},
        source={"resumable": False},
        git={"branch": cur, "worktree": None, "target_branch": options.target, "head_sha": target_head_before},
        intent_meta={"intent_path": str(project_dir / "intent.md"), "spec_path": None},
        artifacts=_merge_artifacts(project_dir, merge_id),
        adapter_key="merge.run",
    )
    publisher.__enter__()

    try:
        logger.info("merge %s starting: target=%s, branches=%s", merge_id, options.target, branches)
        if _drain_merge_cancel_commands(project_dir, merge_id, state):
            result = MergeRunResult(
                success=False,
                merge_id=merge_id,
                state=state,
                note="merge cancelled before start",
            )
        elif options.transactional:
            result = _run_transactional_fast_merge(
                project_dir=project_dir,
                options=options,
                state=state,
                merge_id=merge_id,
                branches=branches,
                queue_lookup=queue_lookup,
                target_head_before=target_head_before,
            )
        else:
            result = await _run_consolidated_agentic_merge(
                project_dir=project_dir,
                config=config,
                options=options,
                state=state,
                merge_id=merge_id,
                branches=branches,
                queue_lookup=queue_lookup,
                target_head_before=target_head_before,
                budget=budget,
            )

        if not result.success:
            updated_note = _with_partial_merge_note(
                result.note,
                project_dir=project_dir,
                state=result.state,
                target_head_before=target_head_before,
            )
            if updated_note != result.note:
                result.note = updated_note
                if is_terminal_status(str(result.state.status or "")):
                    result.state.note = updated_note
                    write_state(project_dir, result.state)
        final_status = result.state.status
        if not is_terminal_status(final_status):
            final_status = "done" if result.success else "failed"
            _persist_merge_terminal_state(
                project_dir,
                result.state,
                status=final_status,
                note=result.note or ("completed" if result.success else "failed"),
            )

        publisher.finalize(
            status=final_status,
            terminal_outcome=result.state.terminal_outcome,
            updates={
                "artifacts": _merge_run_artifacts(project_dir, result.state),
                "last_event": result.note or result.state.note or final_status,
            },
        )
        _append_merge_history(project_dir, result.state)
        return result
    except Exception as exc:
        logger.exception("merge %s failed unexpectedly", merge_id)
        if not is_terminal_status(str(state.status or "")):
            _persist_merge_terminal_state(
                project_dir,
                state,
                status="failed",
                note=f"merge failed: {exc}",
            )
        try:
            publisher.finalize(
                status=state.status or "failed",
                terminal_outcome=state.terminal_outcome or _terminal_outcome_for_status(state.status or "failed"),
                updates={
                    "artifacts": _merge_run_artifacts(project_dir, state),
                    "last_event": state.note or str(exc),
                },
            )
        except Exception:
            logger.exception("failed to finalize merge %s after unexpected error", merge_id)
        try:
            _append_merge_history(project_dir, state)
        except Exception:
            logger.exception("failed to append merge history for %s after unexpected error", merge_id)
        raise
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        logger.warning("merge %s interrupted", merge_id)
        if not is_terminal_status(str(state.status or "")):
            _persist_merge_terminal_state(
                project_dir,
                state,
                status="interrupted",
                note=f"merge interrupted: {exc}",
            )
        try:
            publisher.finalize(
                status=state.status or "interrupted",
                terminal_outcome=state.terminal_outcome or _terminal_outcome_for_status(state.status or "interrupted"),
                updates={
                    "artifacts": _merge_run_artifacts(project_dir, state),
                    "last_event": state.note or "merge interrupted",
                },
            )
        except Exception:
            logger.exception("failed to finalize merge %s after interruption", merge_id)
        try:
            _append_merge_history(project_dir, state)
        except Exception:
            logger.exception("failed to append merge history for %s after interruption", merge_id)
        raise
    finally:
        publisher.stop()


def _update_consolidated_conflict_outcomes(
    *,
    state: MergeState,
    status: str,
    note: str,
    agent_invoked: bool,
    merge_commit: str | None = None,
) -> list[str]:
    """Rewrite phase-1 marker rows to the final consolidated outcome."""
    updated_branches: list[str] = []
    for outcome in state.outcomes:
        if outcome.status != "merged_with_markers":
            continue
        outcome.status = status
        outcome.agent_invoked = agent_invoked
        outcome.merge_commit = merge_commit
        outcome.note = note
        updated_branches.append(outcome.branch)
    return updated_branches


def _merged_from_labels(branches: list[str], queue_lookup: dict[str, str]) -> list[str]:
    return [queue_lookup.get(branch, branch) for branch in branches]


def _proof_of_work_html_from_manifest(manifest: dict[str, Any]) -> str | None:
    pow_path = manifest.get("proof_of_work_path")
    if not pow_path:
        return None
    return str(Path(str(pow_path)).with_name("proof-of-work.html").resolve())


def _resolve_source_pow_paths(
    project_dir: Path,
    *,
    branches: list[str],
    queue_lookup: dict[str, str],
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for branch in branches:
        task_id = queue_lookup.get(branch)
        manifest = find_manifest_for_branch(
            project_dir=project_dir,
            branch=branch,
            queue_task_lookup=queue_lookup,
        )
        record: dict[str, str] = {"branch": branch}
        if task_id:
            record["task_id"] = task_id
        pow_html = _proof_of_work_html_from_manifest(manifest or {})
        if pow_html:
            record["path"] = pow_html
        records.append(record)
    return records


def _annotate_merge_cert_summary(
    project_dir: Path,
    *,
    run_id: str,
    merged_from: list[str],
    merge_verification_plan: dict[str, Any] | None = None,
) -> None:
    from otto import paths

    summary_path = paths.session_summary(project_dir, run_id)
    if not summary_path.exists():
        logger.warning("merge-cert summary missing for %s: %s", run_id, summary_path)
        return
    try:
        summary = _read_json(summary_path)
    except Exception as exc:
        logger.warning("merge-cert summary unreadable for %s: %s", run_id, exc)
        return
    summary["merged_from"] = list(merged_from)
    if merge_verification_plan:
        summary["merge_verification_plan"] = merge_verification_plan
    _atomic_write_json(summary_path, summary)


def _graduate_merged_task_sessions(
    project_dir: Path, queue_lookup: dict[str, str],
) -> None:
    """Graduate merged queue sessions into the main repo, then remove worktrees."""
    if not queue_lookup:
        return
    try:
        from otto.queue.schema import load_queue
    except ImportError:
        return

    try:
        tasks_by_id = {t.id: t for t in load_queue(project_dir)}
    except (OSError, ValueError) as exc:
        logger.warning("cleanup-on-success: could not load queue metadata: %s", exc)
        return

    try:
        merge_commit_sha = git_ops.head_sha(project_dir)
    except Exception as exc:
        logger.warning("cleanup-on-success: could not resolve merge head: %s", exc)
        return
    merged_at = _now_iso()
    graduated: list[str] = []
    for task_id in set(queue_lookup.values()):
        task = tasks_by_id.get(task_id)
        if not task or not task.worktree:
            continue
        wt_path = Path(task.worktree)
        if not wt_path.is_absolute():
            wt_path = project_dir / wt_path
        if not wt_path.exists():
            continue
        step = "preserve queue artifacts"
        remove_worktree = False
        try:
            preserve_queue_session_artifacts(
                project_dir,
                task_id=task_id,
                worktree_path=wt_path,
                merge_commit_sha=merge_commit_sha,
                merged_at=merged_at,
                refuse_existing_destination=True,
                strict=True,
            )
            remove_worktree = True
        except FileExistsError as exc:
            logger.warning(
                "cleanup-on-success: graduation skipped for %s; destination exists: %s",
                task_id, exc,
            )
        except (FileNotFoundError, ValueError) as exc:
            logger.warning(
                "cleanup-on-success: queue artifacts unavailable for %s; removing worktree anyway: %s",
                task_id, exc,
            )
            remove_worktree = True
        except Exception as exc:
            logger.warning(
                "cleanup-on-success: graduation failed for %s at step=%s: %s",
                task_id, step, exc,
            )
        if not remove_worktree:
            continue
        r = subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_path)],
            cwd=project_dir, capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            logger.warning(
                "cleanup-on-success: git worktree remove failed for %s: %s",
                task_id, (r.stderr or "").strip(),
            )
            continue
        graduated.append(task_id)
    if graduated:
        logger.info("cleanup-on-success: graduated and removed worktrees for %s", graduated)


async def _run_post_merge_verification(
    *,
    project_dir: Path,
    config: dict[str, Any],
    options: MergeOptions,
    state: MergeState,
    merge_id: str,
    branches: list[str],
    queue_lookup: dict[str, str],
    target_head_before: str,
    budget: Any | None = None,
) -> MergeRunResult:
    """Cert merged-branch integration through a merge-specific plan.

    The planner computes the risk level from branch overlap, high-risk files,
    and conflict-resolution outcomes, then feeds a dedicated merge certifier
    prompt. The certifier may emit PASS / FAIL / WARN / SKIPPED /
    FLAG_FOR_HUMAN per story, but the scope is deterministic instead of left
    to general certifier story discovery.
    """
    # Collect all stories from merged branches
    all_stories = collect_stories_from_branches(
        project_dir=project_dir, branches=branches, queue_task_lookup=queue_lookup,
    )
    deduped, _ = dedupe_stories(all_stories)
    if _drain_merge_cancel_commands(project_dir, merge_id, state):
        return _cancelled_merge_result(
            project_dir,
            state=state,
            merge_id=merge_id,
            note="merge cancelled before post-merge verification",
        )

    if not deduped:
        # No stories registered for these branches — nothing to verify.
        state.cert_passed = True
        write_state(project_dir, state)
        return MergeRunResult(
            success=True, merge_id=merge_id, state=state,
            cert_passed=True,
            note="no stories registered for the merged branches",
        )

    # Files changed by the merge — drives the cert's per-story skip decision.
    diff_files = git_ops.changed_files_between(
        project_dir, target_head_before, git_ops.head_sha(project_dir),
    )
    from otto.merge.verification import build_merge_verification_plan, format_merge_verification_plan

    plan = build_merge_verification_plan(
        project_dir=project_dir,
        target=options.target,
        branches=branches,
        queue_lookup=queue_lookup,
        target_head_before=target_head_before,
        target_head_after=git_ops.head_sha(project_dir),
        changed_files=diff_files,
        stories=deduped,
        outcomes=list(state.outcomes),
        full_verify=options.full_verify,
    )
    merge_context: dict[str, Any] = {
        "target": options.target,
        "diff_files": diff_files,
        "allow_skip": plan.allow_skip,
        "verification_plan": plan.to_dict(),
        "plan_text": format_merge_verification_plan(plan),
    }

    from otto.config import resolve_intent
    from otto.certifier import run_agentic_certifier
    intent = resolve_intent(project_dir) or "(no intent.md found)"
    cert_task: asyncio.Task[Any] | None = None
    try:
        cert_task = asyncio.create_task(run_agentic_certifier(
            intent=intent,
            project_dir=project_dir,
            config=config,
            mode=str((config.get("queue") or {}).get("merge_certifier_mode", "standard")),
            budget=budget,
            stories=deduped,
            merge_context=merge_context,
        ))
        while True:
            done, _ = await asyncio.wait({cert_task}, timeout=0.2)
            if cert_task in done:
                cert_report = await cert_task
                break
            if _drain_merge_cancel_commands(project_dir, merge_id, state):
                cert_task.cancel()
                try:
                    await cert_task
                except asyncio.CancelledError:
                    pass
                return _cancelled_merge_result(
                    project_dir,
                    state=state,
                    merge_id=merge_id,
                    note="merge cancelled during post-merge verification",
                )
    except asyncio.CancelledError:
        if cert_task is not None and not cert_task.done():
            cert_task.cancel()
            try:
                await cert_task
            except asyncio.CancelledError:
                pass
        raise
    except Exception as exc:
        logger.exception("certifier raised during post-merge verification")
        state.cert_passed = False
        write_state(project_dir, state)
        return MergeRunResult(
            success=False, merge_id=merge_id, state=state,
            cert_passed=False,
            note=f"certifier failed: {exc}",
        )

    if _drain_merge_cancel_commands(project_dir, merge_id, state):
        return _cancelled_merge_result(
            project_dir,
            state=state,
            merge_id=merge_id,
            note="merge cancelled after post-merge verification",
        )

    from otto.certifier.report import CertificationOutcome
    flagged_for_human = any(
        str(story.get("verdict") or "").strip().upper() == "FLAG_FOR_HUMAN"
        for story in cert_report.story_results
    )
    cert_passed = cert_report.outcome == CertificationOutcome.PASSED and not flagged_for_human
    state.cert_passed = cert_passed
    state.cert_run_id = cert_report.run_id
    write_state(project_dir, state)
    _annotate_merge_cert_summary(
        project_dir,
        run_id=cert_report.run_id,
        merged_from=_merged_from_labels(branches, queue_lookup),
        merge_verification_plan=plan.to_dict(),
    )

    return MergeRunResult(
        success=cert_passed, merge_id=merge_id, state=state,
        cert_passed=cert_passed,
        cert_story_results=list(cert_report.story_results),
        post_merge_pow_path=str(
            (paths.certify_dir(project_dir, cert_report.run_id) / "proof-of-work.html").resolve()
        ),
        note=(
            f"cert {'PASSED' if cert_passed else 'FAILED'} "
            f"({cert_report.outcome.value}"
            f"{'; human review required' if flagged_for_human else ''}); see "
            f"{paths.certify_dir(project_dir, cert_report.run_id).relative_to(project_dir) / 'proof-of-work.html'}"
        ),
    )


# ----------------------------------------------------------------------
# Consolidated agent-mode merge — the only merge path
# ----------------------------------------------------------------------

async def _run_consolidated_agentic_merge(
    *,
    project_dir: Path,
    config: dict[str, Any],
    options: MergeOptions,
    state: MergeState,
    merge_id: str,
    branches: list[str],
    queue_lookup: dict[str, str],
    target_head_before: str,
    budget: Any | None = None,
) -> MergeRunResult:
    """Consolidated agent-mode merge.

    Strategy:
    1. `git merge --no-ff <branch>` for each branch in order.
       - On clean merge: commit normally.
       - On conflict: stage marker-laden files (`git add -u`) and commit
         (markers retained in the commit). This is intentionally weird —
         it lets us continue to the next branch's merge without stopping.
    2. After all branches attempted, accumulate the union of marker-laden
       files into one ConsolidatedConflictContext.
    3. ONE agent call (full Bash, no retry loop) to resolve all markers.
       The agent runs project tests to verify.
    4. Validate (HEAD unchanged, no out-of-scope edits, no markers remain)
       and commit the cleanup as a "resolve all conflicts" commit.

    On any unrecoverable failure (e.g., agent leaves markers, edits out
    of scope), the merge bails. State.json records which branches still
    need manual follow-up.
    """
    from otto.merge.conflict_agent import (
        ConsolidatedConflictContext,
        resolve_all_conflicts,
    )
    from otto.merge.edit_scope import (
        EditScopeError,
        build_edit_scope,
        collect_branch_touch_union,
    )
    logger.info("consolidated agent-mode merge of %d branches", len(branches))
    branch_touch_union = collect_branch_touch_union(
        project_dir,
        target=options.target,
        branches=branches,
    )

    # Phase 1: sequential merges. Conflicts get staged as marker-laden
    # commits so the loop can continue to the next branch (we resolve them
    # all in phase 2). We collect the per-branch conflict diffs BEFORE
    # committing the markers, because `git diff HEAD` against an
    # already-committed marker file returns empty.
    accumulated_conflict_files: list[str] = []
    accumulated_diffs: list[str] = []  # per-branch conflict diff (captured pre-commit)
    for branch in branches:
        if _drain_merge_cancel_commands(project_dir, merge_id, state):
            return _cancelled_merge_result(
                project_dir,
                state=state,
                merge_id=merge_id,
                note=f"merge cancelled while processing {branch}",
            )
        result = git_ops.merge_no_ff(project_dir, branch)
        if result.ok:
            state.outcomes.append(BranchOutcome(
                branch=branch, status="merged",
                merge_commit=git_ops.head_sha(project_dir),
            ))
            write_state(project_dir, state)
            continue
        conflicts = git_ops.conflicted_files(project_dir)
        if not conflicts:
            state.outcomes.append(BranchOutcome(
                branch=branch, status="agent_giveup",
                note=f"git merge failed without UU files: {result.stderr.strip()[:200]}",
            ))
            write_state(project_dir, state)
            return MergeRunResult(
                success=False, merge_id=merge_id, state=state,
                note=f"git merge of {branch!r} failed: {result.stderr.strip()[:200]}",
            )
        if options.fast:
            # --fast: bail on first conflict. Leave the in-progress merge
            # in the worktree (UU files staged, no commit yet) so the user
            # can resolve manually and `git merge --continue`.
            state.outcomes.append(BranchOutcome(
                branch=branch, status="agent_giveup",
                note="conflict; --fast mode does not invoke agent",
            ))
            write_state(project_dir, state)
            return MergeRunResult(
                success=False, merge_id=merge_id, state=state,
                note=(
                    f"conflict on {branch!r}; --fast mode requires manual "
                    f"resolution. Fix the conflict, `git merge --continue`, "
                    f"then run `otto merge` (without --fast) for any remaining branches."
                ),
            )
        # Capture the conflict diff BEFORE committing markers (otherwise
        # `git diff HEAD` returns empty since markers are now in HEAD).
        branch_diff = git_ops.run_git(
            project_dir, "diff", "--merge", "--", *conflicts,
        ).stdout
        branch_snapshot = _render_conflict_file_snapshots(project_dir, conflicts)
        branch_sections = [section for section in (branch_diff, branch_snapshot) if section]
        if branch_sections:
            accumulated_diffs.append(f"=== {branch} ===\n" + "\n\n".join(branch_sections))
        add_r = git_ops.add_paths(project_dir, conflicts)
        if not add_r.ok:
            state.outcomes.append(BranchOutcome(
                branch=branch, status="agent_giveup",
                note=f"git add failed: {add_r.stderr.strip()}",
            ))
            write_state(project_dir, state)
            return MergeRunResult(
                success=False, merge_id=merge_id, state=state,
                note=f"git add failed for {branch!r}",
            )
        commit_r = git_ops.commit_no_edit(project_dir)
        if not commit_r.ok:
            state.outcomes.append(BranchOutcome(
                branch=branch, status="agent_giveup",
                note=f"git commit (with markers) failed: {commit_r.stderr.strip()}",
            ))
            write_state(project_dir, state)
            return MergeRunResult(
                success=False, merge_id=merge_id, state=state,
                note="git commit failed during marker accumulation",
            )
        state.outcomes.append(BranchOutcome(
            branch=branch, status="merged_with_markers",
            merge_commit=git_ops.head_sha(project_dir),
            note=f"{len(conflicts)} files have unresolved markers; consolidated agent will handle",
        ))
        accumulated_conflict_files.extend(conflicts)
        write_state(project_dir, state)

    # Phase 2: if no conflicts accumulated, we're done with merging
    accumulated_conflict_files = sorted(set(accumulated_conflict_files))
    if not accumulated_conflict_files:
        logger.info("merge %s: all branches merged clean, no agent call needed", merge_id)
        if _drain_merge_cancel_commands(project_dir, merge_id, state):
            return _cancelled_merge_result(
                project_dir,
                state=state,
                merge_id=merge_id,
                note="merge cancelled after clean merge phase",
            )
        # --fast is documented as pure git/no LLM, so it must not fall
        # through into post-merge certification.
        if options.fast or options.no_certify:
            if options.cleanup_on_success:
                _graduate_merged_task_sessions(project_dir, queue_lookup)
            reason = "--fast" if options.fast else "--no-certify"
            return MergeRunResult(
                success=True, merge_id=merge_id, state=state,
                note=f"all clean merges, cert skipped per {reason}",
            )
        # Run cert phase
        result = await _run_post_merge_verification(
            project_dir=project_dir, config=config, options=options,
            state=state, merge_id=merge_id, branches=branches,
            queue_lookup=queue_lookup, target_head_before=target_head_before,
            budget=budget,
        )
        if result.success and options.cleanup_on_success:
            _graduate_merged_task_sessions(project_dir, queue_lookup)
        return result

    # Phase 3: ONE agent call to resolve all accumulated markers
    logger.info(
        "merge %s: invoking agent on %d files across %d branches",
        merge_id, len(accumulated_conflict_files), len(branches),
    )
    if _drain_merge_cancel_commands(project_dir, merge_id, state):
        return _cancelled_merge_result(
            project_dir,
            state=state,
            merge_id=merge_id,
            note="merge cancelled before conflict resolution",
        )

    # Capture pre-state for orchestrator's single final validation
    pre_head = git_ops.head_sha(project_dir)
    pre_diff_files = set(git_ops.changed_files(project_dir))
    pre_untracked_files = set(git_ops.untracked_files(project_dir))
    try:
        edit_scope = build_edit_scope(
            project_dir=project_dir,
            conflict_files=set(accumulated_conflict_files),
            branch_touch_union=branch_touch_union,
        )
    except EditScopeError as exc:
        conflicted_branch_count = len(
            [outcome for outcome in state.outcomes if outcome.status == "merged_with_markers"]
        )
        unresolved_branches = _update_consolidated_conflict_outcomes(
            state=state,
            status="agent_giveup",
            agent_invoked=False,
            note=(
                "consolidated agent scope construction failed on the shared conflict set "
                f"for {len(accumulated_conflict_files)} files across {conflicted_branch_count} "
                f"conflicted branches: {exc}"
            ),
        )
        state.paused_stage = "manual_fix_required"
        state.paused_at_index = (
            state.branches_in_order.index(unresolved_branches[0])
            if unresolved_branches
            else None
        )
        state.paused_branch = unresolved_branches[0] if unresolved_branches else None
        write_state(project_dir, state)
        return MergeRunResult(
            success=False,
            merge_id=merge_id,
            state=state,
            note=(
                "consolidated agent-mode resolver gave up before invocation: "
                f"{exc}\n"
                f"  Files with markers: {accumulated_conflict_files}\n"
                "  Resolve manually: edit files, `git add`, `git commit --amend`."
            ),
        )

    # Build context: ALL branches' intents + stories, full diff
    intents = _gather_intents(project_dir, branches, queue_lookup)
    stories = collect_stories_from_branches(
        project_dir=project_dir, branches=branches, queue_task_lookup=queue_lookup,
    )
    # Use the per-branch conflict diffs we captured before each marker
    # commit. Concatenating with branch separators preserves the "ours vs
    # theirs per branch" structure the agent needs.
    diff = "\n\n".join(accumulated_diffs)
    test_command = config.get("test_command")  # e.g., "pytest -q" or "npm test"

    ctx = ConsolidatedConflictContext(
        target=options.target,
        all_branches=list(branches),
        all_intents=intents,
        all_stories=stories,
        conflict_files=accumulated_conflict_files,
        secondary_files=sorted(edit_scope.secondary_files),
        branch_touch_union=sorted(edit_scope.branch_touch_union),
        conflict_diff=diff,
        test_command=test_command,
    )
    attempt = await resolve_all_conflicts(
        project_dir=project_dir, config=config, ctx=ctx,
        pre_head=pre_head,
        edit_scope=edit_scope,
        pre_untracked_files=pre_untracked_files,
        pre_diff_files=pre_diff_files,
        budget=budget,
    )
    if _drain_merge_cancel_commands(project_dir, merge_id, state):
        return _cancelled_merge_result(
            project_dir,
            state=state,
            merge_id=merge_id,
            note="merge cancelled after conflict resolution",
        )
    if not attempt.success:
        conflicted_branch_count = len(
            [outcome for outcome in state.outcomes if outcome.status == "merged_with_markers"]
        )
        unresolved_branches = _update_consolidated_conflict_outcomes(
            state=state,
            status="agent_giveup",
            agent_invoked=attempt.agent_invoked,
            note=(
                "consolidated agent failed on the shared conflict set "
                f"for {len(edit_scope.primary_files)} files across {conflicted_branch_count} "
                f"conflicted branches: {attempt.note}"
            ),
        )
        state.paused_stage = "manual_fix_required"
        state.paused_at_index = (
            state.branches_in_order.index(unresolved_branches[0])
            if unresolved_branches
            else None
        )
        state.paused_branch = unresolved_branches[0] if unresolved_branches else None
        write_state(project_dir, state)
        return MergeRunResult(
            success=False, merge_id=merge_id, state=state,
            note=(
                f"consolidated agent-mode resolver gave up: {attempt.note}\n"
                f"  Files with markers: {accumulated_conflict_files}\n"
                f"  Resolve manually: edit files, `git add`, `git commit --amend`."
            ),
        )

    # Stage + commit the agent's resolution
    edited_files = sorted(attempt.edited_files)
    add_r = git_ops.add_paths(project_dir, edited_files)
    if not add_r.ok:
        unresolved_branches = _update_consolidated_conflict_outcomes(
            state=state,
            status="agent_giveup",
            agent_invoked=True,
            note=f"git add failed after consolidated resolution: {add_r.stderr.strip()}",
        )
        state.paused_stage = "manual_fix_required"
        state.paused_at_index = (
            state.branches_in_order.index(unresolved_branches[0])
            if unresolved_branches
            else None
        )
        state.paused_branch = unresolved_branches[0] if unresolved_branches else None
        write_state(project_dir, state)
        return MergeRunResult(
            success=False, merge_id=merge_id, state=state,
            note=f"git add failed after agent resolution: {add_r.stderr.strip()}",
        )
    # Fresh commit (not --amend) so the marker-laden merge commits and the
    # resolution are separate in history.
    commit_msg = f"resolve {len(accumulated_conflict_files)} files across {len(branches)} branches"
    commit_r = git_ops.run_git(project_dir, "commit", "-m", commit_msg)
    if not commit_r.ok:
        unresolved_branches = _update_consolidated_conflict_outcomes(
            state=state,
            status="agent_giveup",
            agent_invoked=True,
            note=f"git commit failed after consolidated resolution: {commit_r.stderr.strip()}",
        )
        state.paused_stage = "manual_fix_required"
        state.paused_at_index = (
            state.branches_in_order.index(unresolved_branches[0])
            if unresolved_branches
            else None
        )
        state.paused_branch = unresolved_branches[0] if unresolved_branches else None
        write_state(project_dir, state)
        return MergeRunResult(
            success=False, merge_id=merge_id, state=state,
            note=f"git commit failed after resolution: {commit_r.stderr.strip()}",
        )
    final_head = git_ops.head_sha(project_dir)
    conflicted_branch_count = len(
        [outcome for outcome in state.outcomes if outcome.status == "merged_with_markers"]
    )
    secondary_edit_note = ""
    if attempt.edited_secondary_files:
        secondary_listing = ", ".join(sorted(attempt.edited_secondary_files))
        secondary_edit_note = f"; secondary edits: {secondary_listing}"
        logger.info("merge %s secondary edits: %s", merge_id, secondary_listing)
    _update_consolidated_conflict_outcomes(
        state=state,
        status="conflict_resolved",
        agent_invoked=True,
        merge_commit=final_head,
        note=(
            "resolved by consolidated agent in one shared call "
            f"(total cost ${attempt.cost_usd:.2f} across {conflicted_branch_count} "
            f"conflicted branches){secondary_edit_note}"
        ),
    )
    state.paused_stage = None
    state.paused_at_index = None
    state.paused_branch = None
    write_state(project_dir, state)

    # Phase 4: post-merge certification (unless disabled). --fast cannot
    # reach conflict-agent resolution, but keep this guard defensive.
    if options.fast or options.no_certify:
        if options.cleanup_on_success:
            _graduate_merged_task_sessions(project_dir, queue_lookup)
        reason = "--fast" if options.fast else "--no-certify"
        return MergeRunResult(
            success=True, merge_id=merge_id, state=state,
            source_pow_paths=_resolve_source_pow_paths(
                project_dir,
                branches=branches,
                queue_lookup=queue_lookup,
            ),
            note=(
                f"cert skipped per {reason}"
                + (
                    f"; secondary edits: {', '.join(sorted(attempt.edited_secondary_files))}"
                    if attempt.edited_secondary_files
                    else ""
                )
            ),
        )
    result = await _run_post_merge_verification(
        project_dir=project_dir, config=config, options=options,
        state=state, merge_id=merge_id, branches=branches,
        queue_lookup=queue_lookup, target_head_before=target_head_before,
        budget=budget,
    )
    if result.success and options.cleanup_on_success:
        _graduate_merged_task_sessions(project_dir, queue_lookup)
    result.source_pow_paths = _resolve_source_pow_paths(
        project_dir,
        branches=branches,
        queue_lookup=queue_lookup,
    )
    return result


def _render_conflict_file_snapshots(project_dir: Path, conflicts: list[str]) -> str:
    """Render raw conflicted file contents with markers for agent context."""
    sections: list[str] = []
    for rel in conflicts:
        path = project_dir / rel
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        sections.append(f"--- {rel} (worktree with markers) ---\n{text}")
    return "\n\n".join(sections)


def _gather_intents(
    project_dir: Path,
    branches: list[str],
    queue_lookup: dict[str, str],
) -> dict[str, str]:
    """Map each branch to its resolved intent (from queue or atomic manifest)."""
    out: dict[str, str] = {}
    for branch in branches:
        manifest = find_manifest_for_branch(
            project_dir=project_dir,
            branch=branch,
            queue_task_lookup=queue_lookup,
        )
        if manifest and manifest.get("resolved_intent"):
            out[branch] = str(manifest["resolved_intent"])
    # Queue tasks (best-effort; missing/corrupt queue.yml is not fatal here)
    try:
        from otto.queue.schema import load_queue
        for t in load_queue(project_dir):
            if t.branch in branches and t.resolved_intent:
                out.setdefault(t.branch, t.resolved_intent)
    except (OSError, ValueError) as exc:
        logger.debug("intent gathering: skipping queue.yml: %s", exc)
    sessions = paths.sessions_root(project_dir)
    if sessions.exists():
        for run_dir in sessions.iterdir():
            mp = run_dir / "manifest.json"
            if not mp.exists():
                continue
            try:
                m = json.loads(mp.read_text())
            except json.JSONDecodeError:
                continue
            b = m.get("branch")
            if b in branches and m.get("resolved_intent"):
                out.setdefault(b, m["resolved_intent"])
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return data


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    write_json_atomic(path, payload, sort_keys=False)
