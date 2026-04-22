"""Python-driven merge orchestration.

Otto now has one merge strategy:
- merge each branch with `git merge --no-ff`
- commit conflicted files with markers so later branches can keep landing
- run one consolidated Claude session on the union of unresolved files
- validate the agent result, commit the cleanup, then run one cert call

Bookkeeping conflicts (`intent.md`, `otto.yaml`) are handled by git's
union/ours merge drivers; the Python loop only handles real code/content
conflicts. `--resume` is still deferred, so `state.json` is informative
bookkeeping rather than an active continuation contract.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from otto.merge import git_ops
from otto.merge.state import (
    BranchOutcome,
    MergeState,
    new_merge_id,
    write_state,
)
from otto.merge.stories import collect_stories_from_branches, dedupe_stories

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
    note: str = ""


@dataclass
class MergeOptions:
    target: str = "main"
    no_certify: bool = False
    full_verify: bool = False
    fast: bool = False                  # pure git, bail on first conflict
    cleanup_on_success: bool = False    # remove worktrees after merge
    allow_any_branch: bool = False


_ATOMIC_BRANCH_RE = re.compile(
    r"^(?P<mode>[a-z0-9][a-z0-9._-]*)/(?P<slug>[a-z0-9][a-z0-9-]*)-(?P<date>\d{4}-\d{2}-\d{2})$"
)


@contextmanager
def merge_lock(project_dir: Path):
    """Hold the per-project merge lock for the entire `otto merge` run."""
    path = project_dir / "otto_logs" / MERGE_LOCK_FILE
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

    if explicit_ids_or_branches:
        # Either queue task ids or raw branch names
        from otto.queue.schema import load_queue
        try:
            tasks = load_queue(project_dir)
        except (OSError, ValueError) as exc:
            logger.warning("could not load queue.yml for explicit-id resolution: %s", exc)
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
                raise ValueError(f"unknown task id or branch: {item!r}")

    if all_done_queue_tasks:
        from otto.queue.schema import load_queue, load_state
        try:
            tasks = load_queue(project_dir)
            state = load_state(project_dir)
        except (OSError, ValueError) as exc:
            logger.warning("could not load queue state for --all resolution: %s", exc)
            tasks = []
            state = {"tasks": {}}
        done_ids = {
            tid for tid, ts in state.get("tasks", {}).items()
            if ts.get("status") == "done"
        }
        for t in tasks:
            if t.id in done_ids and t.branch and t.branch not in branches:
                branches.append(t.branch)
                lookup[t.branch] = t.id

    if not branches:
        # If --all found no done tasks but failed/queued tasks have branches
        # with commits, list them in the error so the user can salvage via
        # explicit-branch merge instead of seeing a generic "no branches".
        if all_done_queue_tasks:
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
    from otto.config import agent_provider

    # Pre-flight: must be on target, working tree clean
    cur = git_ops.current_branch(project_dir)
    if cur != options.target:
        return MergeRunResult(
            success=False, merge_id="", state=MergeState(),
            note=f"must be on {options.target!r}; currently on {cur!r}. Run `git checkout {options.target}` first.",
        )
    dirty_entries = git_ops.status_porcelain_entries(project_dir)
    if dirty_entries:
        preview = ", ".join(dirty_entries[:5])
        if len(dirty_entries) > 5:
            preview += f", ... (+{len(dirty_entries) - 5} more)"
        return MergeRunResult(
            success=False, merge_id="", state=MergeState(),
            note=(
                "working tree must be clean before merge "
                f"(uncommitted changes detected: {preview}). "
                "Commit, stash, or clean these paths and retry."
            ),
        )
    if git_ops.merge_in_progress(project_dir):
        return MergeRunResult(
            success=False, merge_id="", state=MergeState(),
            note="a merge is already in progress; resolve or abort it first",
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

    logger.info("merge %s starting: target=%s, branches=%s", merge_id, options.target, branches)

    return await _run_consolidated_agentic_merge(
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


def _graduate_merged_task_sessions(
    project_dir: Path, queue_lookup: dict[str, str],
) -> None:
    """Graduate merged queue sessions into the main repo, then remove worktrees."""
    if not queue_lookup:
        return
    try:
        from otto import paths
        from otto.manifest import queue_index_path_for
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
        step = "queue manifest lookup"
        try:
            queue_manifest_path = queue_index_path_for(project_dir, task_id)
            if queue_manifest_path is None:
                raise ValueError("queue task id missing")
            queue_manifest = _read_json(queue_manifest_path)
            run_id = str(queue_manifest.get("run_id") or "").strip()
            if not run_id:
                raise ValueError("queue manifest missing run_id")

            step = "locate session"
            src_session_dir = paths.session_dir(wt_path, run_id)
            dst_session_dir = paths.session_dir(project_dir, run_id)
            if dst_session_dir.exists():
                logger.warning(
                    "cleanup-on-success: graduation skipped for %s; destination exists: %s -> %s",
                    task_id, src_session_dir, dst_session_dir,
                )
                continue
            if not src_session_dir.exists():
                raise FileNotFoundError(src_session_dir)

            step = "move session"
            dst_session_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_session_dir), str(dst_session_dir))

            step = "rewrite summary"
            summary_path = dst_session_dir / "summary.json"
            if summary_path.exists():
                summary = _read_json(summary_path)
                summary["merge_commit_sha"] = merge_commit_sha
                summary["merged_at"] = merged_at
                _atomic_write_json(summary_path, summary)

            step = "rewrite canonical manifest"
            canonical_manifest_path = dst_session_dir / "manifest.json"
            canonical_manifest = _read_json(canonical_manifest_path)
            _rewrite_manifest_after_graduation(
                canonical_manifest,
                old_session_dir=src_session_dir,
                new_session_dir=dst_session_dir,
                merge_commit_sha=merge_commit_sha,
                merged_at=merged_at,
            )
            _atomic_write_json(canonical_manifest_path, canonical_manifest)

            step = "rewrite queue index"
            queue_manifest = dict(canonical_manifest)
            queue_manifest["mirror_of"] = str(canonical_manifest_path.resolve())
            _atomic_write_json(queue_manifest_path, queue_manifest)

            step = "remove worktree"
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
        except Exception as exc:
            logger.warning(
                "cleanup-on-success: graduation failed for %s at step=%s: %s",
                task_id, step, exc,
            )
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
    """Cert all merged-branch stories in one call.

    Pruning (skip stories whose feature lives in files the merge didn't
    touch) and contradiction-flagging happen inline inside the cert agent
    via a merge_context preamble in the rendered stories section. The
    cert emits per-story verdicts (PASS / FAIL / SKIPPED / FLAG_FOR_HUMAN)
    that the orchestrator records directly — no separate planning call.

    `--full-verify` still passes merge context to the certifier, but with
    `allow_skip=False` so it tests every story while still surfacing
    cross-branch contradictions as FLAG_FOR_HUMAN.
    """
    # Collect all stories from merged branches
    all_stories = collect_stories_from_branches(
        project_dir=project_dir, branches=branches, queue_task_lookup=queue_lookup,
    )
    deduped, _ = dedupe_stories(all_stories)

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
    merge_context: dict[str, Any] = {
        "target": options.target,
        "diff_files": diff_files,
        "allow_skip": not options.full_verify,
    }

    from otto.config import resolve_intent
    from otto.certifier import run_agentic_certifier
    intent = resolve_intent(project_dir) or "(no intent.md found)"
    try:
        cert_report = await run_agentic_certifier(
            intent=intent,
            project_dir=project_dir,
            config=config,
            mode=str((config.get("queue") or {}).get("merge_certifier_mode", "standard")),
            budget=budget,
            stories=deduped,
            merge_context=merge_context,
        )
    except Exception as exc:
        logger.exception("certifier raised during post-merge verification")
        state.cert_passed = False
        write_state(project_dir, state)
        return MergeRunResult(
            success=False, merge_id=merge_id, state=state,
            cert_passed=False,
            note=f"certifier failed: {exc}",
        )

    from otto.certifier.report import CertificationOutcome
    cert_passed = cert_report.outcome == CertificationOutcome.PASSED
    state.cert_passed = cert_passed
    state.cert_run_id = cert_report.run_id
    write_state(project_dir, state)

    return MergeRunResult(
        success=cert_passed, merge_id=merge_id, state=state,
        cert_passed=cert_passed,
        cert_story_results=list(cert_report.story_results),
        note=(
            f"cert {'PASSED' if cert_passed else 'FAILED'} "
            f"({cert_report.outcome.value}); see otto_logs/sessions/{cert_report.run_id}/certify/proof-of-work.html"
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
    need manual follow-up; `--resume` itself is still deferred.
    """
    from otto.merge.conflict_agent import (
        ConsolidatedConflictContext,
        resolve_all_conflicts,
    )
    logger.info("consolidated agent-mode merge of %d branches", len(branches))

    # Phase 1: sequential merges. Conflicts get staged as marker-laden
    # commits so the loop can continue to the next branch (we resolve them
    # all in phase 2). We collect the per-branch conflict diffs BEFORE
    # committing the markers, because `git diff HEAD` against an
    # already-committed marker file returns empty.
    accumulated_conflict_files: list[str] = []
    accumulated_diffs: list[str] = []  # per-branch conflict diff (captured pre-commit)
    for branch in branches:
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
        # Continue to post-merge certification if not --no-certify
        if options.no_certify:
            if options.cleanup_on_success:
                _graduate_merged_task_sessions(project_dir, queue_lookup)
            return MergeRunResult(
                success=True, merge_id=merge_id, state=state,
                note="all clean merges, cert skipped per --no-certify",
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

    # Capture pre-state for orchestrator's single final validation
    pre_head = git_ops.head_sha(project_dir)
    pre_diff_files = set(git_ops.changed_files(project_dir))
    pre_untracked_files = set(git_ops.untracked_files(project_dir))
    expected_uu = set(accumulated_conflict_files)

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
        conflict_diff=diff,
        test_command=test_command,
    )
    attempt = await resolve_all_conflicts(
        project_dir=project_dir, config=config, ctx=ctx,
        pre_head=pre_head,
        expected_uu_files=expected_uu,
        pre_untracked_files=pre_untracked_files,
        pre_diff_files=pre_diff_files,
        budget=budget,
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
                f"for {len(expected_uu)} files across {conflicted_branch_count} "
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
        state.paused_branch_head = None
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
    add_r = git_ops.add_paths(project_dir, accumulated_conflict_files)
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
        state.paused_branch_head = None
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
        state.paused_branch_head = None
        write_state(project_dir, state)
        return MergeRunResult(
            success=False, merge_id=merge_id, state=state,
            note=f"git commit failed after resolution: {commit_r.stderr.strip()}",
        )
    final_head = git_ops.head_sha(project_dir)
    conflicted_branch_count = len(
        [outcome for outcome in state.outcomes if outcome.status == "merged_with_markers"]
    )
    _update_consolidated_conflict_outcomes(
        state=state,
        status="conflict_resolved",
        agent_invoked=True,
        merge_commit=final_head,
        note=(
            "resolved by consolidated agent in one shared call "
            f"(total cost ${attempt.cost_usd:.2f} across {conflicted_branch_count} "
            "conflicted branches)"
        ),
    )
    state.paused_stage = None
    state.paused_at_index = None
    state.paused_branch = None
    state.paused_branch_head = None
    write_state(project_dir, state)

    # Phase 4: post-merge certification (unless --no-certify)
    if options.no_certify:
        if options.cleanup_on_success:
            _graduate_merged_task_sessions(project_dir, queue_lookup)
        return MergeRunResult(
            success=True, merge_id=merge_id, state=state,
            note="cert skipped per --no-certify",
        )
    result = await _run_post_merge_verification(
        project_dir=project_dir, config=config, options=options,
        state=state, merge_id=merge_id, branches=branches,
        queue_lookup=queue_lookup, target_head_before=target_head_before,
        budget=budget,
    )
    if result.success and options.cleanup_on_success:
        _graduate_merged_task_sessions(project_dir, queue_lookup)
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
    # Queue tasks (best-effort; missing/corrupt queue.yml is not fatal here)
    try:
        from otto.queue.schema import load_queue
        for t in load_queue(project_dir):
            if t.branch in branches and t.resolved_intent:
                out[t.branch] = t.resolved_intent
    except (OSError, ValueError) as exc:
        logger.debug("intent gathering: skipping queue.yml: %s", exc)
    sessions = project_dir / "otto_logs" / "sessions"
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
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _rewrite_manifest_after_graduation(
    manifest: dict[str, Any],
    *,
    old_session_dir: Path,
    new_session_dir: Path,
    merge_commit_sha: str,
    merged_at: str,
) -> None:
    manifest["checkpoint_path"] = _relocate_session_path(
        manifest.get("checkpoint_path"),
        old_session_dir=old_session_dir,
        new_session_dir=new_session_dir,
    )
    manifest["proof_of_work_path"] = _relocate_session_path(
        manifest.get("proof_of_work_path"),
        old_session_dir=old_session_dir,
        new_session_dir=new_session_dir,
    )
    extra = manifest.get("extra")
    if not isinstance(extra, dict):
        extra = {}
    extra["merge_commit_sha"] = merge_commit_sha
    extra["merged_at"] = merged_at
    manifest["extra"] = extra


def _relocate_session_path(
    value: Any,
    *,
    old_session_dir: Path,
    new_session_dir: Path,
) -> str | None:
    if not value:
        return None
    src_path = Path(str(value))
    if src_path.is_absolute():
        try:
            rel = src_path.relative_to(old_session_dir)
        except ValueError:
            return str(src_path)
        return str((new_session_dir / rel).resolve())
    return str((new_session_dir / src_path).resolve())
