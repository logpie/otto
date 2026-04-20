"""Phase 4.3: triage agent — produces verification plan after merges complete.

Inputs: list of merged branches + their stories + files changed by the
merge. Output: structured JSON with `must_verify`, `skip_likely_safe`,
`flag_for_human` story sets. The orchestrator persists this and passes
`must_verify` to the certifier.

Resilience: malformed JSON → retry up to 2x → fall back to "full union as
must_verify with warning."
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from otto.merge import git_ops

logger = logging.getLogger("otto.merge.triage_agent")

MAX_TRIAGE_RETRIES = 2


@dataclass
class VerificationPlan:
    must_verify: list[dict[str, Any]] = field(default_factory=list)
    skip_likely_safe: list[dict[str, Any]] = field(default_factory=list)
    flag_for_human: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    fallback_used: bool = False
    note: str = ""

    def total_count(self) -> int:
        return len(self.must_verify) + len(self.skip_likely_safe) + len(self.flag_for_human)


def _format_branches_listing(branches: list[str]) -> str:
    return "\n".join(f"- `{b}`" for b in branches) or "(none)"


def _format_stories_for_triage(stories: list[dict[str, Any]]) -> str:
    if not stories:
        return "(no stories)"
    lines = []
    for s in stories:
        name = s.get("name") or s.get("summary") or s.get("story_id") or "(unnamed)"
        src = s.get("source_branch", "?")
        desc = s.get("description") or ""
        lines.append(f"- **{name}** _(from `{src}`)_")
        if desc:
            lines.append(f"  {desc}")
    return "\n".join(lines)


def render_triage_prompt(
    *,
    branches: list[str],
    stories: list[dict[str, Any]],
    merge_diff_files: list[str],
    full_verify: bool = False,
) -> str:
    """Render merger-triage.md with substitutions.

    `full_verify=True` adds an instruction that disables `skip_likely_safe`.
    """
    from otto.prompts import _PROMPTS_DIR
    template = (_PROMPTS_DIR / "merger-triage.md").read_text()
    out = (
        template
        .replace("{merged_branches_listing}", _format_branches_listing(branches))
        .replace("{stories_section}", _format_stories_for_triage(stories))
        .replace("{merge_diff_files}", "\n".join(merge_diff_files) or "(none)")
    )
    if full_verify:
        out += (
            "\n\n## ADDITIONAL CONSTRAINT\n\n"
            "`--full-verify` mode is set. Do NOT put any story in "
            "`skip_likely_safe`. Every story must be in `must_verify` or "
            "`flag_for_human`.\n"
        )
    return out


def _extract_json(text: str) -> dict[str, Any] | None:
    """Find the JSON object in the agent's response.

    The prompt asks for ONLY JSON but agents sometimes wrap in fences or
    add prose. Try plain parse first, then look for ```json blocks, then
    fall back to greedy `{ ... }` regex.
    """
    text = text.strip()
    # Plain parse
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # Fenced json block
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    # Greedy single-object match
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return None


def _validate_plan_shape(
    data: dict[str, Any],
    *,
    input_stories: list[dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    """Check the agent's plan JSON has the right shape and covers all stories."""
    for key in ("must_verify", "skip_likely_safe", "flag_for_human"):
        if key not in data:
            return (False, f"missing key: {key}")
        if not isinstance(data[key], list):
            return (False, f"{key} must be a list, got {type(data[key]).__name__}")
        for entry in data[key]:
            if not isinstance(entry, dict):
                return (False, f"{key} entries must be objects, got {entry!r}")
            if "name" not in entry:
                return (False, f"{key} entry missing 'name': {entry!r}")
    if input_stories:
        total_entries = sum(len(data[key]) for key in ("must_verify", "skip_likely_safe", "flag_for_human"))
        if total_entries < len(input_stories):
            return (
                False,
                f"plan dropped stories: {total_entries} entries for {len(input_stories)} input stories",
            )
        expected_names = {str(story.get("name", "")).strip() for story in input_stories}
        covered_names = {
            str(entry.get("name", "")).strip()
            for key in ("must_verify", "skip_likely_safe", "flag_for_human")
            for entry in data[key]
        }
        missing = sorted(name for name in expected_names if name and name not in covered_names)
        if missing:
            return (False, f"plan missing input stories: {missing!r}")
    return (True, "")


async def produce_verification_plan(
    *,
    project_dir: Path,
    config: dict[str, Any],
    branches: list[str],
    stories: list[dict[str, Any]],
    merge_diff_files: list[str],
    full_verify: bool = False,
    budget: Any | None = None,
) -> VerificationPlan:
    """Run the triage agent and return a VerificationPlan.

    On agent failure or malformed output (after retries), fall back to
    "full union as must_verify, none skipped" with a warning note.
    """
    from otto.config import agent_provider
    from otto.agent import (
        AgentCallError,
        make_agent_options,
        run_agent_with_timeout,
    )
    if not stories:
        return VerificationPlan(
            note="no stories collected; nothing to verify",
        )

    provider = agent_provider(config)
    if provider != "claude":
        return VerificationPlan(
            must_verify=stories,
            fallback_used=True,
            note=(
                f"triage skipped (provider='{provider}' lacks tool restrictions); "
                f"falling back to full union as must_verify"
            ),
        )

    total_cost = 0.0
    last_error = ""
    for attempt in range(MAX_TRIAGE_RETRIES + 1):
        prompt = render_triage_prompt(
            branches=branches,
            stories=stories,
            merge_diff_files=merge_diff_files,
            full_verify=full_verify,
        )
        options = make_agent_options(project_dir, config)
        timeout = budget.for_call() if budget is not None else None

        try:
            text, cost, _session = await run_agent_with_timeout(
                prompt,
                options,
                log_path=project_dir / "otto_logs" / "merge" / "triage-agent.log",
                timeout=timeout,
                project_dir=project_dir,
            )
        except AgentCallError as exc:
            last_error = f"agent call error: {exc.reason}"
            logger.warning("triage attempt %d failed: %s", attempt + 1, last_error)
            continue
        total_cost += float(cost or 0)

        data = _extract_json(text or "")
        if data is None:
            last_error = "could not extract JSON from agent response"
            logger.warning("triage attempt %d: %s", attempt + 1, last_error)
            continue
        ok, err = _validate_plan_shape(data, input_stories=stories)
        if not ok:
            last_error = err
            logger.warning("triage attempt %d: invalid shape: %s", attempt + 1, err)
            continue

        # Success
        plan = VerificationPlan(
            must_verify=data["must_verify"],
            skip_likely_safe=data["skip_likely_safe"],
            flag_for_human=data["flag_for_human"],
            cost_usd=total_cost,
        )
        if full_verify and plan.skip_likely_safe:
            # Honor the constraint: in full-verify mode, move skip back to must_verify
            plan.must_verify.extend(plan.skip_likely_safe)
            plan.skip_likely_safe = []
            plan.note = "moved skip_likely_safe entries to must_verify (--full-verify)"
        return plan

    # Fallback after retries
    return VerificationPlan(
        must_verify=stories,
        cost_usd=total_cost,
        fallback_used=True,
        note=f"triage failed after {MAX_TRIAGE_RETRIES + 1} attempts; "
             f"falling back to full union as must_verify. Last error: {last_error}",
    )


def write_plan(project_dir: Path, merge_id: str, plan: VerificationPlan) -> Path:
    """Persist the verification plan to disk."""
    from otto.merge.state import merge_dir
    out_dir = merge_dir(project_dir, merge_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "verify-plan.json"
    payload = {
        "must_verify": plan.must_verify,
        "skip_likely_safe": plan.skip_likely_safe,
        "flag_for_human": plan.flag_for_human,
        "cost_usd": plan.cost_usd,
        "fallback_used": plan.fallback_used,
        "note": plan.note,
    }
    path.write_text(json.dumps(payload, indent=2))
    return path
