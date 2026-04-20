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
        # F12: disallow Write too — the agent must patch conflict regions via
        # Edit/MultiEdit only. Rationale: `Write` rewrites the whole file,
        # which is 5-20× slower (the agent regenerates unchanged lines) and
        # risks accidentally reformatting / "improving" code outside the
        # conflict region. Measured in P5 bench: a single `Write` call took
        # ~10 min to generate; `Edit` on the conflict block finishes in secs.
        # Bash is also disabled — agent must not run `git` or shell commands.
        options.disallowed_tools = list(set(
            (options.disallowed_tools or []) + ["Bash", "Write"]
        ))
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
