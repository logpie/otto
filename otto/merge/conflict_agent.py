"""Phase 4.2: per-conflict agent invocation with Python-enforced scope.

Called by the orchestrator AFTER `git merge` returns conflict status.
The agent is given:
- The list of conflicted files
- Both branches' resolved intents
- Both branches' stories
- The conflict diff

It must edit ONLY the conflicted files and remove ALL conflict markers.
After it returns, the orchestrator validates:
1. delta = post_diff - pre_diff ⊆ conflict_files (no scope creep)
2. `git diff --check` passes (no leftover markers)
3. HEAD unchanged (agent didn't commit/reset)
Then orchestrator stages + commits.

Codex round 4 finding: argv check is too weak; Bash is disabled at the
SDK level; Codex provider is rejected (it ignores disallowed_tools).

Per CLAUDE.md: `disallowed_tools=["Bash"]` is the strongest constraint we
have. Combined with post-call diff-name-only validation, agent escape is
caught and triggers retry-from-snapshot.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from otto.merge import git_ops

logger = logging.getLogger("otto.merge.conflict_agent")


# How many times the orchestrator will retry the agent on validation failure
MAX_AGENT_RETRIES = 2


@dataclass
class ConflictResolutionAttempt:
    success: bool
    note: str          # explanation, especially on failure
    agent_invoked: bool = True  # False only if pre-flight rejected the call
    cost_usd: float = 0.0
    retries_used: int = 0


@dataclass
class ConflictContext:
    """Inputs the agent needs."""
    target: str
    branch_being_merged: str
    branch_intents: dict[str, str]   # branch → resolved intent
    branch_stories: list[dict[str, Any]]   # tagged with source_branch
    conflict_files: list[str]
    conflict_diff: str               # `git diff --merge` output


@dataclass
class ConsolidatedConflictContext:
    """Global context for agent-mode consolidated resolution.

    Used when the orchestrator has attempted all branch merges sequentially
    (committing-with-markers on conflicts) and now invokes the agent ONCE on
    the union of all unresolved files.
    """
    target: str
    all_branches: list[str]                  # every branch the merge attempted
    all_intents: dict[str, str]              # branch → resolved intent
    all_stories: list[dict[str, Any]]        # union with source_branch tag
    conflict_files: list[str]                # ALL files with markers
    conflict_diff: str                       # full diff covering every conflict
    test_command: str | None = None          # project's test command (from config)


def _format_branch_intents(intents: dict[str, str]) -> str:
    if not intents:
        return "(no intent metadata available for these branches)"
    lines = []
    for branch, intent in intents.items():
        lines.append(f"- **`{branch}`**: {intent}")
    return "\n".join(lines)


def _format_stories(stories: list[dict[str, Any]]) -> str:
    if not stories:
        return "(no stories recorded for these branches)"
    lines = []
    for s in stories:
        name = s.get("name") or s.get("summary") or s.get("story_id") or "(unnamed)"
        src = s.get("source_branch", "?")
        desc = s.get("description") or ""
        lines.append(f"- **{name}** _(from `{src}`)_")
        if desc:
            lines.append(f"  {desc}")
    return "\n".join(lines)


def render_conflict_prompt(ctx: ConflictContext) -> str:
    """Render the merger-conflict.md prompt with `ctx` substituted in."""
    from otto.prompts import _PROMPTS_DIR
    template = (_PROMPTS_DIR / "merger-conflict.md").read_text()
    files_listing = "\n".join(f"- {f}" for f in ctx.conflict_files)
    return (
        template
        .replace("{target}", ctx.target)
        .replace("{branch_being_merged}", ctx.branch_being_merged)
        .replace("{branch_intents_section}", _format_branch_intents(ctx.branch_intents))
        .replace("{stories_section}", _format_stories(ctx.branch_stories))
        .replace("{conflict_files_listing}", files_listing or "(none?)")
        .replace("{conflict_diff}", ctx.conflict_diff[:50000])  # cap to fit context
    )


def snapshot_conflict_files(project_dir: Path, files: list[str]) -> dict[str, bytes]:
    """Read raw bytes of each conflicted file (with markers intact) for
    retry restoration. Returns dict of {path → bytes}."""
    out: dict[str, bytes] = {}
    for f in files:
        p = project_dir / f
        if p.exists():
            out[f] = p.read_bytes()
    return out


def restore_conflict_files(
    project_dir: Path,
    snapshot: dict[str, bytes],
    *,
    pre_untracked_files: set[str] | None = None,
) -> None:
    """Write back snapshotted contents and remove new untracked files."""
    for f, data in snapshot.items():
        p = project_dir / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    if pre_untracked_files is None:
        return
    extra_untracked = sorted(
        set(git_ops.untracked_files(project_dir)) - pre_untracked_files,
        key=lambda path: len(Path(path.rstrip("/")).parts),
        reverse=True,
    )
    for rel_path in extra_untracked:
        path = project_dir / rel_path.rstrip("/")
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists() or path.is_symlink():
            path.unlink(missing_ok=True)


def validate_post_agent(
    *,
    project_dir: Path,
    pre_diff_files: set[str],
    expected_uu_files: set[str],
    pre_untracked_files: set[str],
    pre_head: str,
) -> tuple[bool, str | None]:
    """Validate orchestrator-side after the agent returns.

    Returns (ok, error_message). Sequence (per Codex round-3 fix):
    1. Path scope: post_diff - pre_diff must ⊆ expected_uu_files
       (auto-merged files were in pre_diff so don't count as agent edits)
    2. No new untracked files were created
    3. `git diff --check` passes
    4. HEAD unchanged

    NOTE: staging happens AFTER this validation. If we fail before staging,
    the index is still unmerged and retry can restore worktree only.
    """
    post_diff_files = set(git_ops.changed_files(project_dir))
    delta = post_diff_files - pre_diff_files
    out_of_scope = delta - expected_uu_files
    if out_of_scope:
        return (False, f"agent edited files outside conflict set: {sorted(out_of_scope)!r}")
    new_untracked = set(git_ops.untracked_files(project_dir)) - pre_untracked_files
    if new_untracked:
        return (False, f"agent created untracked files: {sorted(new_untracked)!r}")
    dc = git_ops.diff_check(project_dir)
    if not dc.ok:
        return (False, f"git diff --check failed: {dc.stdout.strip()}\n{dc.stderr.strip()}")
    cur_head = git_ops.head_sha(project_dir)
    if cur_head != pre_head:
        return (False, f"HEAD changed during agent call: {pre_head} → {cur_head}")
    return (True, None)


async def resolve_one_conflict(
    *,
    project_dir: Path,
    config: dict[str, Any],
    ctx: ConflictContext,
    budget: Any | None = None,
) -> ConflictResolutionAttempt:
    """Invoke the conflict agent, validate, retry up to MAX_AGENT_RETRIES.

    Provider gate: orchestrator should reject non-claude providers before
    merge starts. This function keeps the same check as a final safeguard.
    """
    from otto.config import agent_provider
    from otto.agent import (
        AgentCallError,
        make_agent_options,
        run_agent_with_timeout,
    )
    provider = agent_provider(config)
    if provider != "claude":
        return ConflictResolutionAttempt(
            success=False,
            agent_invoked=False,
            note=(
                f"conflict resolution requires the 'claude' provider "
                f"(got '{provider}'); the codex provider ignores "
                f"disallowed_tools so the Bash-disabled scope cannot be "
                f"enforced. Switch otto.yaml provider, or merge manually."
            ),
        )

    pre_head = git_ops.head_sha(project_dir)
    pre_diff_files = set(git_ops.changed_files(project_dir))
    pre_untracked_files = set(git_ops.untracked_files(project_dir))
    pre_contents = snapshot_conflict_files(project_dir, ctx.conflict_files)
    expected_uu = set(ctx.conflict_files)

    total_cost = 0.0
    last_note = ""
    for attempt in range(MAX_AGENT_RETRIES + 1):
        prompt = render_conflict_prompt(ctx)
        options = make_agent_options(project_dir, config)
        # Disallow Bash entirely — agent must use Edit/Write/MultiEdit only.
        # This blocks any shell-out, including `git`.
        #
        # F12 note: we previously also disallowed `Write`, forcing Edit/MultiEdit.
        # Measured in P6 rerun: that change made conflict resolution 2-3× SLOWER
        # ($7.80 + $11.42 vs $2.41 baseline) because the agent did multiple
        # plan→edit→verify cycles, each triggering its own extended-thinking
        # phase. Reverted; drift prevention is now handled post-agent by
        # `validate_post_agent` (checks no out-of-scope files were modified
        # and HEAD unchanged). Write on a conflict file can still reformat
        # the file — but the orchestrator catches out-of-scope drift via the
        # delta-files check.
        options.disallowed_tools = list(set((options.disallowed_tools or []) + ["Bash"]))
        timeout = budget.for_call() if budget is not None else None

        try:
            _text, cost, _session = await run_agent_with_timeout(
                prompt,
                options,
                log_path=project_dir / "otto_logs" / "merge" / "conflict-agent.log",
                timeout=timeout,
                project_dir=project_dir,
            )
        except AgentCallError as exc:
            return ConflictResolutionAttempt(
                success=False,
                cost_usd=total_cost,
                retries_used=attempt,
                note=f"agent call error: {exc.reason}",
            )
        total_cost += float(cost or 0)

        ok, err = validate_post_agent(
            project_dir=project_dir,
            pre_diff_files=pre_diff_files,
            expected_uu_files=expected_uu,
            pre_untracked_files=pre_untracked_files,
            pre_head=pre_head,
        )
        if ok:
            return ConflictResolutionAttempt(
                success=True,
                cost_usd=total_cost,
                retries_used=attempt,
                note=f"resolved on attempt {attempt + 1}",
            )

        # Validation failed — restore worktree and retry
        last_note = err or "unknown validation failure"
        logger.warning("conflict agent attempt %d failed: %s", attempt + 1, last_note)
        restore_conflict_files(
            project_dir,
            pre_contents,
            pre_untracked_files=pre_untracked_files,
        )

    return ConflictResolutionAttempt(
        success=False,
        cost_usd=total_cost,
        retries_used=MAX_AGENT_RETRIES,
        note=f"gave up after {MAX_AGENT_RETRIES + 1} attempts; last error: {last_note}",
    )


# ════════════════════════════════════════════════════════════════════════
# Agent-mode consolidated resolver (F13)
# ════════════════════════════════════════════════════════════════════════
#
# Design rationale (audit-grounded — see e2e-findings.md F12):
# - Single agent session for ALL conflicts across all branches (not per-branch)
# - Bash allowed → agent can run project tests to verify merged code WORKS
# - No retry loop in Python — agent self-corrects within its session
# - Single final orchestrator validation (HEAD unchanged + no out-of-scope edits)
#
# Why: per-branch agent calls + Python retry loops + Edit-only patching
# (F12) all measured SLOWER than single-call Write-allowed (revert).
# This is the next-level architectural change: matches `otto build`'s
# agent-mode philosophy. Agent gets full context, full tools, full trust.
#
# Caveats:
# - Allows Bash → agent could run destructive git/shell commands.
#   Mitigation: HEAD-unchanged check catches reset/commit; out-of-scope
#   edit check catches `rm` outside conflict files. Same guards otto/build
#   relies on, plus stricter (HEAD must NOT change).
# - No retry safety net → if agent stops with markers remaining, merge
#   bails. User must manually fix or re-invoke.

def render_consolidated_prompt(ctx: ConsolidatedConflictContext) -> str:
    """Render the consolidated agent-mode prompt."""
    from otto.prompts import _PROMPTS_DIR
    template = (_PROMPTS_DIR / "merger-conflict-agentic.md").read_text()
    files_listing = "\n".join(f"- {f}" for f in ctx.conflict_files)
    branches_listing = "\n".join(f"- {b}" for b in ctx.all_branches)
    test_section = (
        f"After resolving, verify with `{ctx.test_command}`."
        if ctx.test_command
        else "(No project test command detected — verify code reasonableness yourself.)"
    )
    return (
        template
        .replace("{target}", ctx.target)
        .replace("{branches_listing}", branches_listing)
        .replace("{branch_intents_section}", _format_branch_intents(ctx.all_intents))
        .replace("{stories_section}", _format_stories(ctx.all_stories))
        .replace("{conflict_files_listing}", files_listing or "(none)")
        .replace("{conflict_diff}", ctx.conflict_diff[:80000])
        .replace("{test_command_section}", test_section)
    )


async def resolve_all_conflicts(
    *,
    project_dir: Path,
    config: dict[str, Any],
    ctx: ConsolidatedConflictContext,
    pre_head: str,
    expected_uu_files: set[str],
    pre_untracked_files: set[str],
    pre_diff_files: set[str],
    budget: Any | None = None,
) -> ConflictResolutionAttempt:
    """Agent-mode consolidated resolver. ONE Claude session, full tools.

    Caller (orchestrator) is responsible for:
    - Capturing pre_head, pre_diff_files, pre_untracked_files, expected_uu_files
      BEFORE calling this function.
    - Calling validate_post_agent ONCE after this returns (no retry-from-snapshot).

    The agent has Bash to run tests, Read/Edit/MultiEdit/Write/Grep/Glob.
    No tool restrictions beyond what build/improve agents have.
    """
    from otto.config import agent_provider
    from otto.agent import (
        AgentCallError,
        make_agent_options,
        run_agent_with_timeout,
    )
    provider = agent_provider(config)
    if provider != "claude":
        return ConflictResolutionAttempt(
            success=False,
            agent_invoked=False,
            note=(
                f"agent-mode conflict resolution requires the 'claude' provider "
                f"(got '{provider}'). Codex ignores tool restrictions, which "
                f"would break the orchestrator's safety guards."
            ),
        )

    prompt = render_consolidated_prompt(ctx)
    options = make_agent_options(project_dir, config)
    # NO disallowed_tools — match build/improve. Agent has full Bash, Edit,
    # Write, MultiEdit, Read, Grep, Glob. Orchestrator's HEAD-unchanged and
    # out-of-scope-files checks catch the dangerous things.
    timeout = budget.for_call() if budget is not None else None

    try:
        text, cost, _session = await run_agent_with_timeout(
            prompt,
            options,
            log_path=project_dir / "otto_logs" / "merge" / "conflict-agent-agentic.log",
            timeout=timeout,
            project_dir=project_dir,
        )
    except AgentCallError as exc:
        return ConflictResolutionAttempt(
            success=False, cost_usd=0.0, retries_used=0,
            note=f"agent call error: {exc.reason}",
        )

    # Single final validation. No retry-from-snapshot (the whole point of
    # agent mode is that the agent self-corrects within its session).
    ok, err = validate_post_agent(
        project_dir=project_dir,
        pre_diff_files=pre_diff_files,
        expected_uu_files=expected_uu_files,
        pre_untracked_files=pre_untracked_files,
        pre_head=pre_head,
    )
    if ok:
        return ConflictResolutionAttempt(
            success=True, cost_usd=float(cost or 0), retries_used=0,
            note="resolved by agent-mode consolidated resolver",
        )
    return ConflictResolutionAttempt(
        success=False, cost_usd=float(cost or 0), retries_used=0,
        note=f"agent finished but validation failed: {err}",
    )
