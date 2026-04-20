"""Phase 4.1+4.5+4.6: Python-driven merge orchestration.

The Python loop owns `git merge`. The agent is invoked **only** for
actual conflicts (one call per conflict, scoped to conflict files via
`disallowed_tools=["Bash"]` + post-call validation).

Bookkeeping conflicts (intent.md, otto.yaml) are handled by git's
union/ours merge drivers from Phase 1.6 — no Python normalization here.

Three resume modes (Phase 4.6):
- Mode A: clean tree, manual fix committed → verify HEAD is a merge commit
  with parents matching the snapshot, continue from next branch
- Mode B: dirty tree with UU markers (from --fast or agent giveup) →
  invoke conflict agent on current state
- Mode C: dirty tree without UU → refuse with instructions
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from otto.merge import git_ops
from otto.merge.conflict_agent import (
    ConflictContext,
    resolve_one_conflict,
)
from otto.merge.state import (
    BranchOutcome,
    MergeState,
    new_merge_id,
    write_state,
)
from otto.merge.stories import collect_stories_from_branches, dedupe_stories
from otto.merge.triage_agent import VerificationPlan, produce_verification_plan, write_plan

logger = logging.getLogger("otto.merge.orchestrator")


@dataclass
class MergeRunResult:
    success: bool
    merge_id: str
    state: MergeState
    plan: VerificationPlan | None = None
    cert_passed: bool | None = None
    note: str = ""


@dataclass
class MergeOptions:
    target: str = "main"
    no_certify: bool = False
    full_verify: bool = False
    fast: bool = False                  # pure git, bail on first conflict
    cleanup_on_success: bool = False    # remove worktrees after merge


def _resolve_branches(
    project_dir: Path,
    *,
    explicit_ids_or_branches: list[str] | None,
    all_done_queue_tasks: bool,
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
        for item in explicit_ids_or_branches:
            if item in by_id and by_id[item].branch:
                branches.append(by_id[item].branch)
                lookup[by_id[item].branch] = item
            elif git_ops.branch_exists(project_dir, item):
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
    if not git_ops.working_tree_clean(project_dir):
        return MergeRunResult(
            success=False, merge_id="", state=MergeState(),
            note=f"working tree must be clean before merge (uncommitted changes detected)",
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

    # Opt-in: queue.merge_mode = "consolidated" routes to the agent-mode
    # path (sequential git merges + commits with markers, then one agent
    # call resolves all markers globally). Default sequential mode below.
    use_consolidated = (
        not options.fast
        and bool((config.get("queue") or {}).get("merge_mode") == "consolidated")
    )
    if use_consolidated:
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

    # Per-branch loop (default sequential mode)
    for index, branch in enumerate(branches):
        branch_head_at_pause = git_ops.resolve_branch(project_dir, branch)
        result = git_ops.merge_no_ff(project_dir, branch)
        if result.ok:
            state.outcomes.append(BranchOutcome(
                branch=branch, status="merged",
                merge_commit=git_ops.head_sha(project_dir),
            ))
            write_state(project_dir, state)
            continue

        # Conflict path
        conflicts = git_ops.conflicted_files(project_dir)
        if not conflicts:
            # git merge failed but no UU files — likely a tree-level error
            state.outcomes.append(BranchOutcome(
                branch=branch, status="agent_giveup",
                note=f"git merge failed without conflict markers: {result.stderr.strip()[:200]}",
            ))
            state.paused_at_index = index
            state.paused_branch = branch
            state.paused_branch_head = branch_head_at_pause
            state.paused_stage = "manual_fix_required"
            write_state(project_dir, state)
            return MergeRunResult(
                success=False, merge_id=merge_id, state=state,
                note=f"git merge of {branch!r} failed: {result.stderr.strip()[:200]}",
            )

        if options.fast:
            # --fast: bail on first conflict, leave dirty for manual or --resume
            state.outcomes.append(BranchOutcome(
                branch=branch, status="agent_giveup",
                note="conflict; --fast mode does not invoke agent",
            ))
            state.paused_at_index = index
            state.paused_branch = branch
            state.paused_branch_head = branch_head_at_pause
            state.paused_stage = "manual_fix_required"
            write_state(project_dir, state)
            return MergeRunResult(
                success=False, merge_id=merge_id, state=state,
                note=(
                    f"conflict on {branch!r}; --fast mode requires manual "
                    f"resolution. Either fix conflicts and `git merge --continue`, "
                    f"then `otto merge --resume`, OR run `otto merge --resume` "
                    f"(without --fast) to invoke the conflict agent."
                ),
            )

        # Default mode: invoke conflict agent
        ctx = _build_conflict_context(
            project_dir=project_dir,
            target=options.target,
            branch=branch,
            branches_so_far=branches[: index + 1],
            queue_lookup=queue_lookup,
            conflicts=conflicts,
        )
        attempt = await resolve_one_conflict(
            project_dir=project_dir,
            config=config,
            ctx=ctx,
            budget=budget,
        )
        if not attempt.success:
            state.outcomes.append(BranchOutcome(
                branch=branch,
                status="agent_giveup",
                agent_invoked=attempt.agent_invoked,
                note=attempt.note,
            ))
            state.paused_at_index = index
            state.paused_branch = branch
            state.paused_branch_head = branch_head_at_pause
            state.paused_stage = "agent_giveup"
            write_state(project_dir, state)
            return MergeRunResult(
                success=False, merge_id=merge_id, state=state,
                note=(
                    f"conflict agent gave up on {branch!r}: {attempt.note}\n"
                    f"  Resolve manually then `otto merge --resume`."
                ),
            )

        # Stage + commit (agent only edits worktree; orchestrator stages)
        add_r = git_ops.add_paths(project_dir, conflicts)
        if not add_r.ok:
            state.outcomes.append(BranchOutcome(
                branch=branch, status="agent_giveup",
                agent_invoked=True,
                note=f"git add failed after agent resolution: {add_r.stderr.strip()}",
            ))
            write_state(project_dir, state)
            return MergeRunResult(
                success=False, merge_id=merge_id, state=state,
                note="git add failed; merge aborted",
            )
        # Sanity: no UU left after staging
        if git_ops.conflicted_files(project_dir):
            state.outcomes.append(BranchOutcome(
                branch=branch, status="agent_giveup",
                agent_invoked=True,
                note="UU entries remain after agent + staging",
            ))
            write_state(project_dir, state)
            return MergeRunResult(
                success=False, merge_id=merge_id, state=state,
                note=f"merge of {branch!r} still has UU entries",
            )
        commit_r = git_ops.commit_no_edit(project_dir)
        if not commit_r.ok:
            state.outcomes.append(BranchOutcome(
                branch=branch, status="agent_giveup",
                agent_invoked=True,
                note=f"git commit failed: {commit_r.stderr.strip()}",
            ))
            write_state(project_dir, state)
            return MergeRunResult(
                success=False, merge_id=merge_id, state=state,
                note="commit failed; merge aborted",
            )
        state.outcomes.append(BranchOutcome(
            branch=branch, status="conflict_resolved",
            agent_invoked=True,
            merge_commit=git_ops.head_sha(project_dir),
            note=f"resolved by agent (cost ${attempt.cost_usd:.2f}, retries {attempt.retries_used})",
        ))
        write_state(project_dir, state)

    # All branches merged. Run triage + cert phase (unless --no-certify).
    if options.no_certify:
        if options.cleanup_on_success:
            _cleanup_worktrees_for_merged_tasks(project_dir, queue_lookup)
        return MergeRunResult(
            success=True, merge_id=merge_id, state=state,
            note="cert skipped per --no-certify; verify manually with `otto certify`",
        )

    result = await _run_post_merge_verification(
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
    if result.success and options.cleanup_on_success:
        _cleanup_worktrees_for_merged_tasks(project_dir, queue_lookup)
    return result


def _cleanup_worktrees_for_merged_tasks(
    project_dir: Path, queue_lookup: dict[str, str],
) -> None:
    """Remove the worktrees of queue tasks whose branches just merged.

    Best-effort: failures are logged but never raised — by this point the
    merge succeeded and we don't want to surface errors that would make the
    user think their merge didn't land.

    Branches are preserved (`git worktree remove` only removes the worktree
    directory + admin files; the underlying branch still exists and is now
    pointed-at by the merge commit on target).
    """
    if not queue_lookup:
        return
    try:
        from otto.queue.schema import load_queue
    except ImportError:
        return
    tasks_by_id = {t.id: t for t in load_queue(project_dir)}
    cleaned: list[str] = []
    for task_id in set(queue_lookup.values()):
        task = tasks_by_id.get(task_id)
        if not task or not task.worktree:
            continue
        wt_path = project_dir / task.worktree
        if not wt_path.exists():
            continue
        try:
            import subprocess as _sp
            r = _sp.run(
                ["git", "worktree", "remove", "--force", str(wt_path)],
                cwd=project_dir, capture_output=True, text=True, check=False,
            )
            if r.returncode == 0:
                cleaned.append(task_id)
            else:
                logger.warning(
                    "cleanup-on-success: git worktree remove failed for %s: %s",
                    task_id, (r.stderr or "").strip(),
                )
        except Exception as exc:
            logger.warning("cleanup-on-success: worktree remove crashed for %s: %s", task_id, exc)
    if cleaned:
        logger.info("cleanup-on-success: removed worktrees for %s", cleaned)


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
    """Triage + certifier on must-verify subset."""
    # Collect all stories from merged branches
    all_stories = collect_stories_from_branches(
        project_dir=project_dir, branches=branches, queue_task_lookup=queue_lookup,
    )
    deduped, _ = dedupe_stories(all_stories)

    # Files changed by the merge
    diff_files = git_ops.changed_files_between(
        project_dir, target_head_before, git_ops.head_sha(project_dir),
    )

    plan = await produce_verification_plan(
        project_dir=project_dir,
        config=config,
        branches=branches,
        stories=deduped,
        merge_diff_files=diff_files,
        full_verify=options.full_verify,
        budget=budget,
    )
    write_plan(project_dir, merge_id, plan)
    state.verification_plan_path = str(
        (project_dir / "otto_logs" / "merge" / merge_id / "verify-plan.json")
    )
    write_state(project_dir, state)

    if not plan.must_verify:
        # No stories to verify — successful merge with no cert work needed
        state.cert_passed = True
        write_state(project_dir, state)
        return MergeRunResult(
            success=True, merge_id=merge_id, state=state, plan=plan,
            cert_passed=True,
            note="no stories required verification (skip-likely-safe covered all)",
        )

    # Run certifier on the must-verify subset
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
            stories=plan.must_verify,
        )
    except Exception as exc:
        logger.exception("certifier raised during post-merge verification")
        state.cert_passed = False
        write_state(project_dir, state)
        return MergeRunResult(
            success=False, merge_id=merge_id, state=state, plan=plan,
            cert_passed=False,
            note=f"certifier failed: {exc}",
        )

    from otto.certifier.report import CertificationOutcome
    cert_passed = cert_report.outcome == CertificationOutcome.PASSED
    state.cert_passed = cert_passed
    state.cert_run_id = cert_report.run_id
    write_state(project_dir, state)

    return MergeRunResult(
        success=cert_passed, merge_id=merge_id, state=state, plan=plan,
        cert_passed=cert_passed,
        note=(
            f"cert {'PASSED' if cert_passed else 'FAILED'} "
            f"({cert_report.outcome.value}); see otto_logs/certifier/{cert_report.run_id}/proof-of-work.html"
        ),
    )


# ----------------------------------------------------------------------
# Consolidated agent-mode merge — opt-in via queue.merge_mode = "consolidated"
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
    1. Sequential `git merge --no-ff <branch>` for each branch.
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
    of scope), the merge bails. State.json captures the partial outcome
    for `--resume` (deferred) or manual fix.
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
        # Continue to triage + cert if not --no-certify
        if options.no_certify:
            if options.cleanup_on_success:
                _cleanup_worktrees_for_merged_tasks(project_dir, queue_lookup)
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
            _cleanup_worktrees_for_merged_tasks(project_dir, queue_lookup)
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
        state.outcomes.append(BranchOutcome(
            branch="(consolidated)", status="agent_giveup",
            agent_invoked=attempt.agent_invoked,
            note=attempt.note,
        ))
        state.paused_stage = "agent_giveup"
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
        state.outcomes.append(BranchOutcome(
            branch="(consolidated)", status="agent_giveup",
            agent_invoked=True,
            note=f"git add failed after agent resolution: {add_r.stderr.strip()}",
        ))
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
        state.outcomes.append(BranchOutcome(
            branch="(consolidated)", status="agent_giveup",
            agent_invoked=True,
            note=f"git commit failed after resolution: {commit_r.stderr.strip()}",
        ))
        write_state(project_dir, state)
        return MergeRunResult(
            success=False, merge_id=merge_id, state=state,
            note=f"git commit failed after resolution: {commit_r.stderr.strip()}",
        )
    state.outcomes.append(BranchOutcome(
        branch="(consolidated)", status="conflict_resolved",
        merge_commit=git_ops.head_sha(project_dir),
        agent_invoked=True,
        note=f"resolved {len(accumulated_conflict_files)} files (cost ${attempt.cost_usd:.2f})",
    ))
    write_state(project_dir, state)

    # Phase 4: triage + cert (unless --no-certify)
    if options.no_certify:
        if options.cleanup_on_success:
            _cleanup_worktrees_for_merged_tasks(project_dir, queue_lookup)
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
        _cleanup_worktrees_for_merged_tasks(project_dir, queue_lookup)
    return result


def _build_conflict_context(
    *,
    project_dir: Path,
    target: str,
    branch: str,
    branches_so_far: list[str],
    queue_lookup: dict[str, str],
    conflicts: list[str],
) -> ConflictContext:
    """Build the context the conflict agent needs."""
    intents = _gather_intents(project_dir, branches_so_far, queue_lookup)
    stories = collect_stories_from_branches(
        project_dir=project_dir,
        branches=branches_so_far,
        queue_task_lookup=queue_lookup,
    )
    diff = git_ops.run_git(project_dir, "diff", "--merge").stdout
    return ConflictContext(
        target=target,
        branch_being_merged=branch,
        branch_intents=intents,
        branch_stories=stories,
        conflict_files=conflicts,
        conflict_diff=diff,
    )


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
    # Atomic build manifests
    builds = project_dir / "otto_logs" / "builds"
    if builds.exists():
        import json
        for run_dir in builds.iterdir():
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
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
