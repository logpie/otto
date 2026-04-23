"""Agent-mode consolidated conflict resolution.

The orchestrator first attempts each `git merge --no-ff`, committing
marker-laden files on conflicts so later branches can keep landing. It
then invokes ONE agent session on the union of unresolved files. Full
tool access is intentional: the agent can run the project's test command
to self-verify and use Bash to inspect branch context with `git diff` /
`git show`.

Validation guarantees (`validate_post_agent`):
1. Out-of-scope edits — `post_diff − pre_diff ⊆ edit_scope.allowed_files`
2. Agent-created untracked files are delta-cleaned; cleanup failures fail closed
3. No conflict markers remain — content scan of `edit_scope.primary_files`
   (markers can live in committed files where `git diff --check` is blind)
4. HEAD unchanged (agent didn't `commit`/`reset`)

Codex provider is rejected outright because it does not reliably honor
tool restrictions globally, which would undermine the merge safety model.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from otto import paths
from otto.merge.edit_scope import EditScope
from otto.merge import git_ops

logger = logging.getLogger("otto.merge.conflict_agent")


@dataclass
class ConflictResolutionAttempt:
    success: bool
    note: str          # explanation, especially on failure
    agent_invoked: bool = True  # False only if pre-flight rejected the call
    cost_usd: float = 0.0
    retries_used: int = 0
    edited_files: set[str] = field(default_factory=set)
    edited_secondary_files: set[str] = field(default_factory=set)


@dataclass
class PostAgentValidationResult:
    ok: bool
    error: str | None = None
    edited_files: set[str] = field(default_factory=set)
    edited_primary_files: set[str] = field(default_factory=set)
    edited_secondary_files: set[str] = field(default_factory=set)


@dataclass
class ConsolidatedConflictContext:
    """Global context for agent-mode consolidated resolution.

    Built by the orchestrator after it has attempted every branch merge,
    committing marker-laden results on conflicts. Carries the union of
    unresolved files plus per-branch intent/story metadata so the agent
    can preserve each branch's behaviors.
    """
    target: str
    all_branches: list[str]                  # every branch the merge attempted
    all_intents: dict[str, str]              # branch → resolved intent
    all_stories: list[dict[str, Any]]        # union with source_branch tag
    conflict_files: list[str]                # ALL files with markers
    secondary_files: list[str]               # adjacent files allowed for coherence only
    branch_touch_union: list[str]            # deterministic merge blast radius
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


def _files_with_markers(project_dir: Path, files: list[str] | set[str]) -> list[str]:
    """Return the subset of `files` that still contain conflict markers.

    Used by the consolidated path: markers may live in committed files
    (where `git diff --check` is blind), so we scan content directly.
    Any column-zero conflict marker line is suspicious in a file that was
    previously unmerged, including partial or mangled marker triplets.
    This intentionally false-positives on literal marker examples in the
    conflict set so the merge fails closed instead of landing bad content.
    """
    out: list[str] = []
    for rel in files:
        p = project_dir / rel
        if not p.exists():
            continue
        try:
            with p.open("r", errors="replace") as fh:
                for line in fh:
                    if line.startswith(("<<<<<<<", "=======", ">>>>>>>")):
                        out.append(rel)
                        break
        except OSError:
            continue
    return sorted(out)


def _cleanup_agent_untracked_delta(project_dir: Path, new_untracked: set[str]) -> list[str]:
    """Best-effort remove only the untracked paths created during the agent session."""
    failed: list[str] = []
    for rel in sorted(new_untracked, key=lambda p: (len(Path(p).parts), p), reverse=True):
        path = project_dir / rel
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=False)
            elif path.exists() or path.is_symlink():
                path.unlink()
            else:
                continue
            logger.info("removed agent-created untracked path: %s", rel)
        except OSError:
            failed.append(rel)
    return failed


def validate_post_agent(
    *,
    project_dir: Path,
    pre_diff_files: set[str],
    edit_scope: EditScope,
    pre_untracked_files: set[str],
    pre_head: str,
) -> PostAgentValidationResult:
    """Validate orchestrator-side after the agent returns.

    Returns a structured result. Checks:
    1. Out-of-scope edits — `post_diff - pre_diff` must ⊆ `edit_scope.allowed_files`
    2. Agent-created untracked files are delta-cleaned; cleanup failures fail closed
    3. No conflict markers remain in `edit_scope.primary_files` (content scan —
       `git diff --check` is blind to markers in already-committed files)
    4. HEAD unchanged

    Staging happens AFTER this validation. On failure the merge bails;
    the user resolves manually.
    """
    post_diff_files = set(git_ops.changed_files(project_dir))
    delta = post_diff_files - pre_diff_files
    out_of_scope = delta - edit_scope.allowed_files
    if out_of_scope:
        return PostAgentValidationResult(
            ok=False,
            error=f"agent edited files outside conflict edit scope: {sorted(out_of_scope)!r}",
        )
    new_untracked = set(git_ops.untracked_files(project_dir)) - pre_untracked_files
    if new_untracked:
        failed_cleanup = set(_cleanup_agent_untracked_delta(project_dir, new_untracked))
        remaining_untracked = set(git_ops.untracked_files(project_dir)) - pre_untracked_files
        failed_cleanup |= remaining_untracked
        if failed_cleanup:
            return PostAgentValidationResult(
                ok=False,
                error=(
                    f"could not clean up agent-created files: {sorted(failed_cleanup)!r}. "
                    f"Inspect the working tree manually, then re-run the merge."
                ),
            )
    leftover = _files_with_markers(project_dir, edit_scope.primary_files)
    if leftover:
        return PostAgentValidationResult(
            ok=False,
            error=f"conflict markers still in files: {leftover!r}",
        )
    dc = git_ops.diff_check(project_dir)
    if not dc.ok:
        return PostAgentValidationResult(
            ok=False,
            error=f"git diff --check failed: {dc.stdout.strip()}\n{dc.stderr.strip()}",
        )
    cur_head = git_ops.head_sha(project_dir)
    if cur_head != pre_head:
        return PostAgentValidationResult(
            ok=False,
            error=f"HEAD changed during agent call: {pre_head} → {cur_head}",
        )
    return PostAgentValidationResult(
        ok=True,
        edited_files=delta,
        edited_primary_files=delta & edit_scope.primary_files,
        edited_secondary_files=delta & edit_scope.secondary_files,
    )


def render_consolidated_prompt(ctx: ConsolidatedConflictContext) -> str:
    """Render the consolidated agent-mode prompt."""
    from otto.prompts import _PROMPTS_DIR
    template = (_PROMPTS_DIR / "merger-conflict-agentic.md").read_text()
    files_listing = "\n".join(f"- {f}" for f in ctx.conflict_files)
    secondary_listing = "\n".join(f"- {f}" for f in ctx.secondary_files)
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
        .replace("{secondary_files_listing}", secondary_listing or "(none)")
        .replace("{conflict_diff}", ctx.conflict_diff[:80000])
        .replace("{test_command_section}", test_section)
    )


async def resolve_all_conflicts(
    *,
    project_dir: Path,
    config: dict[str, Any],
    ctx: ConsolidatedConflictContext,
    pre_head: str,
    edit_scope: EditScope,
    pre_untracked_files: set[str],
    pre_diff_files: set[str],
    budget: Any | None = None,
) -> ConflictResolutionAttempt:
    """Agent-mode consolidated resolver. ONE Claude session, full tools.

    Caller (orchestrator) captures pre_head / pre_diff_files /
    pre_untracked_files / edit_scope BEFORE this call. We invoke
    validate_post_agent ONCE on return; on failure the merge bails (the
    agent already had a full test-driven retry budget within its session).
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
                f"(got '{provider}'). Codex does not reliably honor tool "
                f"restrictions, which would break the orchestrator's safety guards."
            ),
        )

    prompt = render_consolidated_prompt(ctx)
    options = make_agent_options(project_dir, config)
    # NO disallowed_tools — match build/improve. Agent has full Bash, Edit,
    # Write, MultiEdit, Read, Grep, Glob. Orchestrator-side validation
    # catches the dangerous things after the session returns.
    timeout = budget.for_call() if budget is not None else None

    try:
        text, cost, _session, _breakdown = await run_agent_with_timeout(
            prompt,
            options,
            log_dir=paths.logs_dir(project_dir) / "merge" / "conflict-agent-agentic",
            timeout=timeout,
            project_dir=project_dir,
        )
    except AgentCallError as exc:
        return ConflictResolutionAttempt(
            success=False, cost_usd=0.0, retries_used=0,
            note=f"agent call error: {exc.reason}",
        )

    # Single orchestrator-level validation — the agent self-corrects within
    # its session via the project's test command + Bash (test-driven retry
    # at the agent layer is more powerful than re-rolling at this layer).
    validation = validate_post_agent(
        project_dir=project_dir,
        pre_diff_files=pre_diff_files,
        edit_scope=edit_scope,
        pre_untracked_files=pre_untracked_files,
        pre_head=pre_head,
    )
    if validation.ok:
        return ConflictResolutionAttempt(
            success=True, cost_usd=float(cost or 0), retries_used=0,
            note="resolved by agent-mode consolidated resolver",
            edited_files=validation.edited_files,
            edited_secondary_files=validation.edited_secondary_files,
        )
    return ConflictResolutionAttempt(
        success=False, cost_usd=float(cost or 0), retries_used=0,
        note=f"agent finished but validation failed: {validation.error}",
    )
