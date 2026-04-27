"""Risk-based planning for post-merge verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from otto.merge import git_ops


HIGH_RISK_PATH_PARTS = (
    "auth",
    "permission",
    "security",
    "payment",
    "billing",
    "migration",
    "schema",
    "model",
    "database",
    "config",
    "deploy",
    "workflow",
)


@dataclass(frozen=True)
class MergeStoryPlan:
    story_id: str
    source_branch: str
    action: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "story_id": self.story_id,
            "source_branch": self.source_branch,
            "action": self.action,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MergeVerificationPlan:
    target: str
    risk_level: str
    verification_level: str
    allow_skip: bool
    changed_files: list[str]
    branches: list[dict[str, Any]]
    overlapping_files: list[str]
    high_risk_files: list[str]
    reasons: list[str]
    stories: list[MergeStoryPlan] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "risk_level": self.risk_level,
            "verification_level": self.verification_level,
            "allow_skip": self.allow_skip,
            "changed_files": list(self.changed_files),
            "branches": [dict(branch) for branch in self.branches],
            "overlapping_files": list(self.overlapping_files),
            "high_risk_files": list(self.high_risk_files),
            "reasons": list(self.reasons),
            "stories": [story.to_dict() for story in self.stories],
        }


def build_merge_verification_plan(
    *,
    project_dir: Path,
    target: str,
    branches: list[str],
    queue_lookup: dict[str, str],
    target_head_before: str,
    target_head_after: str,
    changed_files: list[str],
    stories: list[dict[str, Any]],
    outcomes: list[Any],
    full_verify: bool,
) -> MergeVerificationPlan:
    """Build a deterministic verification scope for a merged integration.

    The planner is intentionally conservative and structural. It gives the
    certifier a scope; it does not let the certifier decide from scratch how
    much of the product should be re-certified.
    """
    del target_head_after  # reserved for future report links and comparisons
    branch_records = _branch_records(
        project_dir=project_dir,
        target_head_before=target_head_before,
        branches=branches,
        queue_lookup=queue_lookup,
    )
    overlapping_files = _overlapping_files(branch_records)
    high_risk_files = sorted(
        path for path in set(changed_files) if _is_high_risk_path(path)
    )
    conflict_resolved = any(
        str(getattr(outcome, "status", "") or "") == "conflict_resolved"
        for outcome in outcomes
    )
    marker_conflict_seen = any(
        str(getattr(outcome, "status", "") or "") == "merged_with_markers"
        for outcome in outcomes
    )

    reasons: list[str] = []
    if full_verify:
        reasons.append("operator requested full merge verification")
    if conflict_resolved or marker_conflict_seen:
        reasons.append("merge conflict resolution touched the integration")
    if overlapping_files:
        reasons.append("multiple branches touched the same files")
    if high_risk_files:
        reasons.append("merge touched high-risk files")
    if len(branches) > 1 and not reasons:
        reasons.append("multiple branches landed together")
    if not reasons:
        reasons.append("single clean branch landed")

    if full_verify:
        risk_level = "full"
        verification_level = "full"
    elif conflict_resolved or marker_conflict_seen:
        risk_level = "conflict_resolved"
        verification_level = "full"
    elif overlapping_files or high_risk_files:
        risk_level = "risky_overlap"
        verification_level = "targeted"
    elif len(branches) > 1:
        risk_level = "clean_disjoint"
        verification_level = "selective"
    else:
        risk_level = "clean_single"
        verification_level = "selective"

    allow_skip = not full_verify and verification_level == "selective"
    story_plans = _story_plans(
        stories=stories,
        branch_records=branch_records,
        verification_level=verification_level,
        allow_skip=allow_skip,
    )

    return MergeVerificationPlan(
        target=target,
        risk_level=risk_level,
        verification_level=verification_level,
        allow_skip=allow_skip,
        changed_files=sorted(dict.fromkeys(changed_files)),
        branches=branch_records,
        overlapping_files=overlapping_files,
        high_risk_files=high_risk_files,
        reasons=reasons,
        stories=story_plans,
    )


def format_merge_verification_plan(plan: MergeVerificationPlan) -> str:
    """Render a compact, prompt-friendly merge verification plan."""
    lines: list[str] = [
        "## Merge Verification Plan",
        "",
        f"- Target: `{plan.target}`",
        f"- Risk level: `{plan.risk_level}`",
        f"- Verification level: `{plan.verification_level}`",
        f"- Story skipping allowed: `{'yes' if plan.allow_skip else 'no'}`",
        "",
        "Reasons:",
    ]
    lines.extend(f"- {reason}" for reason in plan.reasons)

    lines += ["", "Branches:"]
    for branch in plan.branches:
        files = branch.get("files") or []
        preview = ", ".join(f"`{path}`" for path in files[:6])
        if len(files) > 6:
            preview += f", ... (+{len(files) - 6} more)"
        if not preview:
            preview = "(no changed files resolved)"
        task = branch.get("task_id") or branch.get("branch")
        lines.append(f"- `{branch.get('branch')}` ({task}): {preview}")

    if plan.overlapping_files:
        lines += ["", "Overlapping files:"]
        lines.extend(f"- `{path}`" for path in plan.overlapping_files)

    if plan.high_risk_files:
        lines += ["", "High-risk files:"]
        lines.extend(f"- `{path}`" for path in plan.high_risk_files)

    if plan.stories:
        lines += ["", "Story scope:"]
        for story in plan.stories:
            lines.append(
                f"- `{story.story_id}` from `{story.source_branch or 'unknown'}`: "
                f"{story.action} - {story.reason}"
            )

    return "\n".join(lines).rstrip() + "\n"


def _branch_records(
    *,
    project_dir: Path,
    target_head_before: str,
    branches: list[str],
    queue_lookup: dict[str, str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for branch in branches:
        files = _files_changed_by_branch(project_dir, target_head_before, branch)
        records.append(
            {
                "branch": branch,
                "task_id": queue_lookup.get(branch, ""),
                "files": files,
            }
        )
    return records


def _files_changed_by_branch(project_dir: Path, target_head_before: str, branch: str) -> list[str]:
    if not target_head_before or not branch:
        return []
    result = git_ops.run_git(project_dir, "diff", "--name-only", f"{target_head_before}...{branch}")
    if not result.ok:
        return []
    return sorted(path for path in result.stdout.splitlines() if path)


def _overlapping_files(branch_records: list[dict[str, Any]]) -> list[str]:
    owners: dict[str, set[str]] = {}
    for branch in branch_records:
        branch_name = str(branch.get("branch") or "")
        for path in branch.get("files") or []:
            owners.setdefault(str(path), set()).add(branch_name)
    return sorted(path for path, branches in owners.items() if len(branches) > 1)


def _is_high_risk_path(path: str) -> bool:
    lowered = path.lower()
    if lowered in {"pyproject.toml", "package.json", "package-lock.json", "uv.lock"}:
        return True
    return any(part in lowered for part in HIGH_RISK_PATH_PARTS)


def _story_plans(
    *,
    stories: list[dict[str, Any]],
    branch_records: list[dict[str, Any]],
    verification_level: str,
    allow_skip: bool,
) -> list[MergeStoryPlan]:
    if not stories:
        return []
    if verification_level in {"full", "targeted"}:
        return [
            MergeStoryPlan(
                story_id=_story_id(story, index),
                source_branch=str(story.get("source_branch") or ""),
                action="CHECK",
                reason=f"{verification_level} merge verification requires this story",
            )
            for index, story in enumerate(stories, 1)
        ]

    first_story_by_branch: dict[str, str] = {}
    for index, story in enumerate(stories, 1):
        source = str(story.get("source_branch") or "")
        first_story_by_branch.setdefault(source, _story_id(story, index))

    known_branches = {str(branch.get("branch") or "") for branch in branch_records}
    plans: list[MergeStoryPlan] = []
    for index, story in enumerate(stories, 1):
        story_id = _story_id(story, index)
        source = str(story.get("source_branch") or "")
        if source not in known_branches:
            plans.append(MergeStoryPlan(story_id, source, "CHECK", "source branch could not be mapped"))
        elif first_story_by_branch.get(source) == story_id:
            plans.append(MergeStoryPlan(story_id, source, "CHECK", "representative story for this branch"))
        elif allow_skip:
            plans.append(MergeStoryPlan(story_id, source, "SKIP_ALLOWED", "clean disjoint merge; prior branch cert remains background evidence"))
        else:
            plans.append(MergeStoryPlan(story_id, source, "CHECK", "skip disabled"))
    return plans


def _story_id(story: dict[str, Any], index: int) -> str:
    value = story.get("story_id") or story.get("name") or story.get("summary") or f"story-{index}"
    return str(value).strip() or f"story-{index}"
