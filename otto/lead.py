"""The Lead primitive — v5's universal build agent.

A Lead is one ``query()`` call against the Claude SDK with Otto's custom MCP
tools attached. It runs at every level of the hierarchy. Differences between
"root Lead" and "sub-Lead" and "integration Lead" are PROMPT differences only;
the runner is the same.

Three prompt templates:
    lead.md             - planning + inline build (root or sub-task)
    lead-integration.md - integration phase after children resolve
    (test-agent.md)     - reserved for Phase 2+ build/test split

Best-effort invariants enforced here (per plan-v5 §13):
    1. Outer try/except wraps the whole session. On uncaught exception, write
       a summary.json with verdict=catastrophic + failure_reason.
    2. ResultMessage.total_cost_usd accumulates into task_graph; if missing,
       cost defaults to 0.0 (not crash).
    3. If Lead returned without calling mcp__otto__verify, the verdict is
       downgraded to `unverified` (audit is the only PASS gate).
    4. If Lead claimed pass in text but mcp__otto__verify never ran, the
       text claim is ignored; verdict computed from absence of verify.
    5. max_turns / max_budget_usd termination is a normal end-state, not crash.
    6. Always writes summary.json before returning, even on error.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from otto.journey_scope_policy import (
    ExecutionScope,
    infer_execution_scope,
    node_kind_for_scope,
)
from otto.queue.task_graph import (
    add_cost as graph_add_cost,
    record_task,
    set_verdict,
)

logger = logging.getLogger("otto.lead")


LeadKind = Literal["plan_or_inline", "integration"]
LeadVerdict = Literal[
    "pass", "partial", "unverified", "merge_blocked", "pending_children", "catastrophic"
]
_CANONICAL_VERDICTS = ("pass", "partial", "unverified", "merge_blocked")


def _canonical_decomposition(
    decomposition: str,
    emitted_subtask_ids: list[str],
    *,
    is_integration: bool,
) -> str:
    """Canonical decomposition mode for a Lead result.

    A non-integration Lead that submitted real child subtasks IS a
    decomposer: ``emitted_subtask_ids`` non-empty is ground truth that
    decomposition happened. A stray ``begin_inline`` (which sets the
    task-graph ``decomposition`` to ``"inline"``) must NOT silently
    discard actually-submitted children and let otto false-stamp
    ``partial`` on a zero-product inline worktree.

    fix12-115705 (2026-05-18): the root Lead correctly decided to
    decompose, submitted 5 child subtasks, then ALSO called
    ``begin_inline`` → graph entry was ``decomposition="inline",
    child_task_ids=[5]``. Both the lead.py verdict guard and
    v5_runner.py Phase D are gated on ``decomposition == "emit"``, so the
    5 children never ran and otto committed an empty "v5 inline build"
    and stamped a false ``partial``. Canonicalizing emitted⇒emit at the
    single graph-entry read point fixes both consumers
    consistent-by-construction (NOT gate-weakening: genuine inline with
    no emitted children, and integration Leads, are preserved).
    """
    if not is_integration and emitted_subtask_ids:
        return "emit"
    return decomposition
_RUNTIME_HINT_LABEL_RE = re.compile(
    r"^\s*(?:[-*+>]\s*)?(?:\d+[.)]\s*)?(?:#+\s*)?"
    + r"(?:[`*_]+)?(?P<label>[A-Za-z][A-Za-z0-9 _./-]{0,80}?)(?:[`*_]+)?\s*[:=]"
)
_VERDICT_SCHEMA_HINT_KEYS = frozenset(
    {
        "summary",
        "journeys",
        "intent_coverage",
        "evidence",
        "test_command",
        "decisions_appended",
    }
)


@dataclass
class LeadResult:
    """What a Lead session produced."""

    task_id: str
    verdict: LeadVerdict = "unverified"
    cost_usd: float = 0.0
    duration_s: float = 0.0
    agent_session_id: str = ""
    decomposition: str = "unknown"  # inline | emit | unknown
    emitted_subtask_ids: list[str] = field(default_factory=list)
    verify_called: bool = False
    verify_result: dict[str, Any] | None = None
    failure_reason: str = ""
    final_text: str = ""
    raw_messages_count: int = 0


async def run_lead(
    *,
    task_id: str,
    intent: str,
    project_dir: Path,
    session_dir: Path,
    integration_branch: str | None,
    config: dict[str, Any],
    kind: LeadKind = "plan_or_inline",
    child_summaries: list[dict[str, Any]] | None = None,
    preflight_result: dict[str, Any] | None = None,
    context_slice_note: str = "",
    decomp_runtime_context: dict[str, Any] | None = None,
    integration_packet_path: str = "",
    execution_scope: ExecutionScope | None = None,
) -> LeadResult:
    """Run one Lead session for one task. The single v5 build primitive.

    Args:
        task_id: this Lead's task_id; recorded in task_graph for parent linkage.
        intent: the semantic goal handed down from the parent (or user, at root).
        project_dir: project root.
        session_dir: this Lead's session_dir (otto_logs/sessions/<id>).
        integration_branch: where this Lead's worktree merges. None for root.
        config: project config (provider, model, budget, etc.).
        kind: ``plan_or_inline`` (uses lead.md) or ``integration`` (uses
              lead-integration.md after children resolve).
        child_summaries: only for kind=integration; child verdicts to inform.
        preflight_result: only for kind=integration; structured runner
            smoke_clean_deploy result for the merged integration worktree.
        context_slice_note: optional runner-authored note pointing a child at
            scoped session artifacts. Empty means use full repo context.
    """
    started = time.monotonic()
    resolved_execution_scope = execution_scope or infer_execution_scope(
        kind=kind,
        integration_branch=integration_branch,
    )
    record_task_kwargs: dict[str, Any] = {
        "task_id": task_id,
        "intent": intent,
    }
    if kind == "plan_or_inline":
        record_task_kwargs["integration_branch"] = integration_branch
    record_task(project_dir, **record_task_kwargs)

    result = LeadResult(task_id=task_id)
    failure_reason = ""
    worktree = project_dir

    try:
        # Build SDK options with our custom MCP server attached.
        from otto.agent import make_agent_options

        options = make_agent_options(project_dir, config, agent_type="build")

        # Force CWD to the session's worktree if it exists; otherwise project_dir.
        worktree = _resolve_worktree(project_dir, session_dir)
        try:
            options.cwd = str(worktree)
        except Exception:  # noqa: BLE001 — defensive
            pass

        from otto.mcp_tools import create_otto_mcp_server

        mcp_server = create_otto_mcp_server(
            task_id=task_id,
            project_dir=project_dir,
            session_dir=session_dir,
            integration_branch=integration_branch,
            execution_scope=resolved_execution_scope,
        )
        try:
            existing_mcp = dict(getattr(options, "mcp_servers", {}) or {})
        except Exception:  # noqa: BLE001
            existing_mcp = {}
        existing_mcp["otto"] = mcp_server
        try:
            options.mcp_servers = existing_mcp
        except Exception:  # noqa: BLE001
            logger.warning("could not attach mcp_servers to options; SDK build may not pick up tools")

        # Simplified architecture: ONE agent per node. No build/test agent
        # split, no orchestrator-only ACL. The agent reads intent, writes
        # code + tests + journey runners, runs them via Bash, iterates until
        # confident, writes its verdict to <session_dir>/verdict.json.
        # Verification depth follows scope:
        #   - leaf scope → component/unit tests via Bash
        #   - integration scope → end-to-end (Playwright) via Bash
        # No MCP verifier tool. Each agent runs its own loop.

        # Render the prompt.
        tier = str(config.get("v5_tier") or "auto")
        prompt_text = _render_prompt(
            kind=kind,
            task_id=task_id,
            intent=intent,
            session_dir=session_dir,
            integration_branch=integration_branch,
            child_summaries=child_summaries or [],
            preflight_result=preflight_result,
            tier=tier,
            context_slice_note=context_slice_note,
            decomp_runtime_context=decomp_runtime_context or {},
            integration_packet_path=integration_packet_path,
        )

        # Save the rendered prompt for observability.
        from otto.observability import save_rendered_prompt

        try:
            save_rendered_prompt(
                prompts_dir=session_dir / "prompts",
                template=("lead.md" if kind == "plan_or_inline" else "lead-integration.md"),
                rendered_text=prompt_text,
            )
        except Exception as exc:  # noqa: BLE001 — observability is best-effort
            logger.warning("save_rendered_prompt failed: %s", exc)

        # Track decomposition signals via a small in-process probe on the MCP server.
        # We can't easily intercept tool calls without hooks, so we rely on
        # task_graph state at the end + ResultMessage stream parsing.

        log_dir = session_dir / "lead"
        log_dir.mkdir(parents=True, exist_ok=True)

        from otto.agent import run_agent_with_timeout

        text, cost, agent_session_id, _breakdown = await run_agent_with_timeout(
            prompt_text,
            options,
            log_dir=log_dir,
            phase_name="LEAD",
            phase_label=("plan-or-inline" if kind == "plan_or_inline" else "integration"),
            timeout=int(config.get("run_budget_seconds") or 3600),
            project_dir=project_dir,
        )

        result.final_text = text or ""
        result.cost_usd = float(cost or 0.0)
        result.agent_session_id = agent_session_id

        # Update graph cost.
        try:
            graph_add_cost(project_dir, task_id, result.cost_usd)
        except Exception as exc:  # noqa: BLE001
            logger.warning("task_graph cost update failed: %s", exc)

        # Read decomposition decision and verify status from task_graph + stream logs.
        from otto.queue.task_graph import get_task

        graph_entry = get_task(project_dir, task_id) or {}
        result.emitted_subtask_ids = list(graph_entry.get("child_task_ids") or [])
        # Canonicalize at the single graph-entry read point so BOTH
        # consumers — the verdict guard below AND v5_runner Phase D
        # (_process_children) — see the authoritative decomposition: a
        # Lead that emitted real child subtasks is a decomposer even if
        # it also called begin_inline (else otto discards the submitted
        # children and false-stamps partial on a zero-product worktree).
        result.decomposition = _canonical_decomposition(
            graph_entry.get("decomposition") or "unknown",
            result.emitted_subtask_ids,
            is_integration=(kind == "integration"),
        )

        # Verdict comes from the agent's own verdict.json (written via Write).
        # Agents are responsible for running their own tests and reporting
        # honest results. The runner trusts the file the agent wrote, with
        # one exception: if the agent decomposed, it can't claim a leaf
        # verdict — its terminal state is pending_children until parent
        # integration runs.
        result.verify_called, result.verify_result = _read_agent_verdict(session_dir)
        if not result.verify_called and _malformed_verdict_context(session_dir) is not None:
            retry_cost = await _request_canonical_verdict_rewrite(
                session_dir=session_dir,
                options=options,
                project_dir=project_dir,
                timeout_s=int(config.get("run_budget_seconds") or 3600),
            )
            result.cost_usd += retry_cost
            result.verify_called, result.verify_result = _read_agent_verdict(session_dir)

        is_integration = kind == "integration"
        if not is_integration and result.decomposition == "emit" and result.emitted_subtask_ids:
            result.verdict = "pending_children"
        elif result.verify_called and result.verify_result:
            v = result.verify_result.get("verdict") or "unverified"
            if v in ("pass", "partial", "unverified", "merge_blocked"):
                result.verdict = v
            else:
                result.verdict = "unverified"
        elif is_integration:
            # Integration agent ended without writing verdict.json.
            # Don't force pending_children (would loop). Mark unverified.
            result.verdict = "unverified"
            failure_reason = _verdict_failure_reason(session_dir, integration=True)
        else:
            # Leaf agent ended without writing verdict.json.
            result.verdict = "unverified"
            failure_reason = _verdict_failure_reason(session_dir, integration=False)

        if (
            result.verify_called
            and isinstance(result.verify_result, dict)
            and result.verdict in ("pass", "partial", "unverified", "merge_blocked")
        ):
            try:
                from otto.v5_verification_plan import validate_lead_verdict

                runner_outcome = validate_lead_verdict(
                    project_dir=project_dir,
                    worktree_dir=worktree,
                    session_dir=session_dir,
                    agent_verdict=result.verify_result,
                    initial_verdict=result.verdict,
                    node_kind=node_kind_for_scope(resolved_execution_scope),
                    matrix_scope=_verification_matrix_scope(config),
                    execution_scope=resolved_execution_scope,
                )
                previous_verdict = result.verdict
                if runner_outcome.final_verdict in (
                    "pass",
                    "partial",
                    "unverified",
                    "merge_blocked",
                ):
                    result.verdict = runner_outcome.final_verdict  # type: ignore[assignment]
                if result.verdict != previous_verdict:
                    failure_reason = _runner_downgrade_failure_reason(runner_outcome)
                    result.verify_result["verdict"] = result.verdict
                    result.verify_result["runner_downgrade_reason"] = failure_reason
                    result.verify_result["failure_reason"] = failure_reason
                if runner_outcome.runner_checks_summary:
                    result.verify_result["runner_checks"] = runner_outcome.runner_checks_summary
                    existing_summary = str(result.verify_result.get("summary") or "").strip()
                    failed = [
                        c for c in runner_outcome.runner_checks_summary
                        if c.get("status") == "fail"
                    ]
                    skipped = [
                        c for c in runner_outcome.runner_checks_summary
                        if c.get("status") == "skipped"
                    ]
                    suffix = (
                        f"Runner checks: {len(failed)} failed, "
                        f"{len(skipped)} skipped. See verification_plan.json."
                    )
                    result.verify_result["summary"] = (
                        f"{existing_summary}\n\n{suffix}" if existing_summary else suffix
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("runner verification plan failed: %s", exc)
                if result.verdict == "pass":
                    result.verdict = "unverified"
                    if isinstance(result.verify_result, dict):
                        result.verify_result["verdict"] = "unverified"
                        existing_summary = str(result.verify_result.get("summary") or "").strip()
                        failure_summary = (
                            "Runner verification plan failed; invalidating "
                            f"self-reported pass: {type(exc).__name__}: {exc}"
                        )
                        result.verify_result["summary"] = (
                            f"{existing_summary}\n\n{failure_summary}"
                            if existing_summary
                            else failure_summary
                        )
                        result.verify_result["runner_verification_error"] = {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        }
                    failure_reason = (
                        f"runner verification plan failed: {type(exc).__name__}: {exc}"
                    )

    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.exception("Lead session crashed for task %s", task_id)
        result.verdict = "catastrophic"
        failure_reason = f"{type(exc).__name__}: {exc}"

    finally:
        result.duration_s = time.monotonic() - started
        result.failure_reason = failure_reason

        # Persist verdict + summary for resumability.
        try:
            set_verdict(project_dir, task_id, result.verdict, cost_usd=result.cost_usd)
        except Exception as exc:  # noqa: BLE001
            logger.warning("task_graph set_verdict failed: %s", exc)
        try:
            _write_summary(session_dir, result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("summary.json write failed: %s", exc)
        try:
            _write_skipped_report(session_dir, result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("skipped_report.md write failed: %s", exc)

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_worktree(project_dir: Path, session_dir: Path) -> Path:
    """Return the worktree path the Lead should CWD into.

    If the session_dir has a paired worktree (created by v5_runner via
    add_worktree), use it. Otherwise fall back to project_dir.
    """
    # By convention, v5_runner creates worktree at session_dir / "worktree" or
    # at <project_dir>/.worktrees/<task_id>. Check both.
    candidates = [
        session_dir / "worktree",
        project_dir / ".worktrees" / session_dir.name,
    ]
    for candidate in candidates:
        if candidate.is_dir() and (candidate / ".git").exists():
            return candidate
    return project_dir


def _read_prompt_template(name: str) -> str:
    """Read a prompt template from otto/prompts/."""
    here = Path(__file__).resolve().parent
    path = here / "prompts" / name
    return path.read_text(encoding="utf-8")


def _interpolate_prompt(template: str, values: dict[str, str]) -> str:
    """Replace ``{key}`` placeholders without breaking on literal braces.

    Uses str.replace per key — does NOT use .format() which would error on
    literal `{` or `}` in the template (e.g., shell glob patterns). Only
    explicitly-named placeholders are substituted; everything else is
    preserved verbatim.
    """
    out = template
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def _is_runtime_hint_label(label: str) -> bool:
    tokens = [tok for tok in re.split(r"[^a-z0-9]+", label.lower()) if tok]
    if not tokens:
        return False
    token_set = set(tokens)
    compact = "".join(tokens)
    if compact in {
        "sessionid",
        "sessiondir",
        "sessiondirectory",
        "sessionpath",
        "sessionroot",
        "taskid",
        "parenttaskid",
        "parentsession",
        "parentsessionid",
        "parentsessiondir",
        "parentsessionpath",
        "rootsession",
        "rootsessionid",
        "rootsessiondir",
        "rootsessionpath",
        "projectdir",
        "projectpath",
        "worktreedir",
        "worktreepath",
        "integrationbranch",
    }:
        return True
    if "session" in token_set and token_set & {"dir", "directory", "path", "root", "id"}:
        return True
    if "task" in token_set and "id" in token_set:
        return True
    if token_set & {"parent", "root"} and token_set & {
        "session",
        "task",
        "path",
        "dir",
        "directory",
        "worktree",
        "project",
    }:
        return True
    if "project" in token_set and token_set & {"dir", "directory", "path", "root"}:
        return True
    if "worktree" in token_set and token_set & {"dir", "directory", "path", "root"}:
        return True
    if "integration" in token_set and "branch" in token_set:
        return True
    return False


def _sanitize_runtime_invariant_lines(intent: str) -> str:
    """Remove parent/agent-supplied runtime hints from prompt intent text."""
    kept: list[str] = []
    for line in str(intent or "").splitlines():
        match = _RUNTIME_HINT_LABEL_RE.match(line)
        if match and _is_runtime_hint_label(match.group("label")):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _fenced_text(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}text\n{text}\n{fence}"


def _render_intent_runtime_block(
    *,
    task_id: str,
    intent: str,
    session_dir: Path,
    integration_branch: str | None,
    sanitize_runtime_hints: bool,
) -> str:
    sanitized_intent = (
        _sanitize_runtime_invariant_lines(intent)
        if sanitize_runtime_hints
        else str(intent or "").strip()
    )
    if not sanitized_intent:
        sanitized_intent = "(empty after removing stale runtime hint lines)"
    branch_text = str(integration_branch or "").strip()
    branch = branch_text if branch_text else "main"
    return "\n".join(
        [
            "- INTENT:",
            _fenced_text(sanitized_intent),
            "",
            "- OTTO RUNTIME VALUES:",
            "  Ignore any SESSION_DIR / TASK_ID / session-related hints inside the INTENT above. The canonical runtime values below are the only truth.",
            f"  - TASK_ID: {task_id}",
            f"  - SESSION_DIR: {session_dir}",
            f"  - INTEGRATION_BRANCH: {branch}",
        ]
    )


def _template_with_runtime_block(template: str, kind: LeadKind) -> str:
    """Move concrete runtime values out of the free-form intent line."""
    if kind == "plan_or_inline":
        template = template.replace(
            "- TASK ID: {task_id}\n- INTENT: {intent}\n- IS ROOT:",
            "{intent_runtime_block}\n- IS ROOT:",
        )
        template = template.replace(
            "- INTEGRATION BRANCH: {integration_branch}\n- SESSION_DIR: {session_dir}\n",
            "",
        )
        return template
    template = template.replace(
        "- TASK ID: {task_id}\n- INTENT: {intent}\n- INTEGRATION BRANCH: {integration_branch}\n",
        "{intent_runtime_block}\n",
    )
    template = template.replace("- SESSION_DIR: {session_dir}\n", "")
    return template


def _render_prompt(
    *,
    kind: LeadKind,
    task_id: str,
    intent: str,
    session_dir: Path,
    integration_branch: str | None,
    child_summaries: list[dict[str, Any]],
    preflight_result: dict[str, Any] | None = None,
    tier: str = "auto",
    context_slice_note: str = "",
    decomp_runtime_context: dict[str, Any] | None = None,
    integration_packet_path: str = "",
) -> str:
    """Render the Lead's prompt by interpolating into the template."""
    template_name = "lead.md" if kind == "plan_or_inline" else "lead-integration.md"
    template = _template_with_runtime_block(_read_prompt_template(template_name), kind)

    journeys_path = session_dir / "spec" / "spec.json"
    is_root = integration_branch is None
    intent_runtime_block = _render_intent_runtime_block(
        task_id=task_id,
        intent=intent,
        session_dir=session_dir,
        integration_branch=integration_branch,
        sanitize_runtime_hints=(kind == "integration" or not is_root),
    )

    # Tier preset modifies prompt content slightly.
    tier_hint = ""
    if tier == "solo":
        tier_hint = "\n## Tier preset: solo\n\nYou MUST call mcp__otto__begin_inline. Do NOT call mcp__otto__submit_subtask.\n"
    elif tier == "lead":
        tier_hint = (
            "\n## Tier preset: lead\n\n"
            "Use decomposition for multi-subsystem work. For a root task with "
            "backend/frontend, CLI/core/tests, or other independent ownership "
            "surfaces, call mcp__otto__submit_subtask for a shallow architect/"
            "scaffold task plus parallel vertical build leaves. Keep the tree "
            "flat unless the intent explicitly asks for recursive sub-decomposition.\n"
        )
    elif tier == "modular":
        if is_root:
            tier_hint = (
                "\n## Tier preset: modular\n\n"
                "You MUST use architecture-first decomposition. Do not build "
                "the whole product inline at the root. First submit one "
                "architect/scaffold child that establishes shared contracts, "
                "then submit 2-5 vertical build leaves. If the intent explicitly "
                "asks for recursive decomposition, make one substantial child "
                "own that nested subtree instead of flattening everything at "
                "the root.\n"
            )
        else:
            tier_hint = (
                "\n## Tier preset: modular\n\n"
                "Honor the architecture-first shape from the root Lead. You "
                "MUST build this scoped child inline. Do not call "
                "mcp__otto__submit_subtask from a non-root Lead.\n"
            )

    if kind == "plan_or_inline":
        runtime_context_text = json.dumps(
            decomp_runtime_context or {},
            indent=2,
            sort_keys=True,
            default=str,
        )
        integration_branch_text = str(integration_branch).strip() if integration_branch is not None else ""
        if not integration_branch_text:
            integration_branch_text = "main"
        return _interpolate_prompt(template, {
            "task_id": task_id,
            "intent": intent,
            "intent_runtime_block": intent_runtime_block,
            "is_root": str(is_root).lower(),
            "journeys_path": str(journeys_path),
            "integration_branch": integration_branch_text,
            "session_dir": str(session_dir),
            "decomp_runtime_context": runtime_context_text,
            "context_slice_note": (
                context_slice_note
                or "No scoped context slice for this Lead; use repo-root CHARTER.md and decisions.md."
            ),
        }) + tier_hint
    else:
        def _fmt_child(s: dict[str, Any]) -> str:
            verdict = s.get("verdict", "?")
            tid = s.get("task_id", "?")
            line = (
                f"  - {tid}: verdict={verdict}, "
                f"intent={(s.get('intent') or '')[:80]!r}"
            )
            # For merge_blocked children, surface the build branch so the
            # integration Lead can re-attempt `git merge <branch>` per
            # Step 0b instead of treating the missing work as missing
            # feature.
            if verdict == "merge_blocked":
                branch = s.get("build_branch")
                if branch:
                    line += f"\n      build_branch=`{branch}`"
                hint = s.get("recovery_hint")
                if hint:
                    line += f"\n      recovery_hint: {hint}"
            return line

        summary_text = "\n".join(_fmt_child(s) for s in child_summaries) \
            or "  (no children)"
        rendered_preflight = preflight_result or {
            "check": "smoke_clean_deploy",
            "passed": None,
            "issues": [],
            "note": "not run",
        }
        integration_branch_text = str(integration_branch).strip() if integration_branch is not None else ""
        if not integration_branch_text:
            integration_branch_text = "main"
        preflight_text = json.dumps(
            rendered_preflight,
            indent=2,
            sort_keys=True,
        )
        return _interpolate_prompt(template, {
            "task_id": task_id,
            "intent": intent,
            "intent_runtime_block": intent_runtime_block,
            "integration_branch": integration_branch_text,
            "child_summaries": summary_text,
            "preflight_result": preflight_text,
            "journeys_path": str(journeys_path),
            "session_dir": str(session_dir),
            "integration_packet_path": integration_packet_path,
        })


def _verification_matrix_scope(config: dict[str, Any]) -> str:
    """Return the v5 runner verification matrix policy.

    Default is ``integration_only``: leaves prove local work, while integration
    runs the full structured matrix against the merged product.
    """
    plan = config.get("verification_plan") if isinstance(config, dict) else None
    value = plan.get("matrix_scope") if isinstance(plan, dict) else None
    if value in {"leaf", "integration_only"}:
        return str(value)
    return "integration_only"


def _runner_downgrade_failure_reason(runner_outcome: Any) -> str:
    failed = [
        c for c in (getattr(runner_outcome, "runner_checks_summary", []) or [])
        if isinstance(c, dict) and c.get("status") == "fail"
    ]
    if failed:
        details = []
        for check in failed[:5]:
            kind = str(check.get("kind") or check.get("id") or "runner_check")
            detail = str(check.get("detail") or "failed").strip()
            details.append(f"{kind}: {detail}")
        suffix = "; ".join(details)
        return f"runner verification downgraded verdict to {runner_outcome.final_verdict}: {suffix}"
    journey_failures = list(getattr(runner_outcome, "journey_failures", []) or [])
    if journey_failures:
        joined = ", ".join(str(item) for item in journey_failures[:10])
        return (
            "runner verification downgraded verdict to "
            f"{runner_outcome.final_verdict}: missing passed journeys: {joined}"
        )
    return f"runner verification downgraded verdict to {runner_outcome.final_verdict}"


def _read_agent_verdict(session_dir: Path) -> tuple[bool, dict[str, Any] | None]:
    """Read the agent-authored verdict.json from session_dir.

    In the simplified architecture, agents run their own tests via Bash and
    write their structured verdict to ``<session_dir>/verdict.json``. The
    runner reads it after the agent's session ends. No MCP verifier; no
    side-channel; just a file the agent wrote.

    Expected format:
      {"verdict": "pass" | "partial" | "unverified" | "merge_blocked",
       "journeys": [{"id": "...", "passed": bool, "detail": "..."}],
       "summary": "...",
       "evidence": ["path1", "path2"]}

    Returns (True, payload) when the file exists and parses; (False, None)
    otherwise. Falls back to legacy ``verify/verify-result.json`` for
    backwards-compat with sessions still using the old MCP verifier.
    """
    # Primary: agent-authored verdict.json
    candidate = session_dir / "verdict.json"
    if candidate.exists():
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            canonical = _canonicalize_verdict_payload(payload)
            if canonical is not None:
                _rewrite_canonical_verdict(candidate, canonical)
                return True, canonical
        except (OSError, json.JSONDecodeError):
            pass

    # Fallback: agent may have misplaced verdict.json inside the worktree
    # (typically worktree/<subsystem>/verdict.json) instead of session_dir.
    # This has shown up enough times that we recover from it explicitly,
    # then write a warning so we can see how often agents are doing this.
    misplaced = _find_misplaced_verdict(session_dir)
    if misplaced is not None:
        try:
            payload = json.loads(misplaced.read_text(encoding="utf-8"))
            canonical = _canonicalize_verdict_payload(payload)
            if canonical is not None:
                logger.warning(
                    "agent wrote verdict.json to %s instead of %s — recovering",
                    misplaced, candidate,
                )
                _record_verdict_recovery_warning(
                    session_dir,
                    {
                        "kind": "worktree_verdict_file_recovered",
                        "source_path": str(misplaced),
                        "canonical_path": str(candidate),
                    },
                )
                try:
                    candidate.write_text(
                        json.dumps(canonical, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                except OSError:
                    pass
                return True, canonical
        except (OSError, json.JSONDecodeError):
            pass

    # Rescue: the agent may have called Write with a valid verdict payload but
    # targeted a stale session path that came from poisoned intent text. Parse
    # the durable tool-use log and copy only canonical-shaped verdict objects
    # into this child's actual session_dir.
    write_rescue = _rescue_verdict_from_write_tool_inputs(session_dir)
    if write_rescue is not None:
        rescued, warning = write_rescue
        canonical = _canonicalize_verdict_payload(rescued)
        if canonical is not None:
            logger.warning(
                "agent Write tool targeted %s instead of %s — recovering verdict",
                warning.get("tool_path"),
                candidate,
            )
            warning["canonical_path"] = str(candidate)
            _record_verdict_recovery_warning(session_dir, warning)
            try:
                candidate.write_text(
                    json.dumps(canonical, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
            return True, canonical

    # Legacy fallback: pre-simplification verify-result.json
    legacy = session_dir / "verify" / "verify-result.json"
    if legacy.exists():
        try:
            payload = json.loads(legacy.read_text(encoding="utf-8"))
            canonical = _canonicalize_verdict_payload(payload)
            if canonical is not None:
                return True, canonical
        except (OSError, json.JSONDecodeError):
            pass

    # Rescue: agent inlined the verdict JSON in its final text message but
    # forgot to write the file. Walk lead/messages.jsonl looking for a JSON
    # object with the expected shape in the last assistant turn.
    rescued = _rescue_verdict_from_messages(session_dir)
    if rescued is not None:
        canonical = _canonicalize_verdict_payload(rescued)
        if canonical is None:
            return False, None
        try:
            (session_dir / "verdict.json").write_text(
                json.dumps(canonical, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        return True, canonical

    return False, None


def _record_verdict_recovery_warning(session_dir: Path, warning: dict[str, Any]) -> None:
    payload = {
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": "verdict_recovery_warning",
        **warning,
    }
    try:
        narrative = session_dir / "lead" / "narrative.log"
        narrative.parent.mkdir(parents=True, exist_ok=True)
        with narrative.open("a", encoding="utf-8") as fh:
            fh.write("[verdict_recovery_warning] ")
            fh.write(json.dumps(payload, sort_keys=True))
            fh.write("\n")
    except OSError:
        pass


def _rescue_verdict_from_write_tool_inputs(
    session_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    for path in _verdict_message_paths(session_dir):
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in reversed(text.splitlines()):
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            for block in msg.get("blocks") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                if str(block.get("name") or "").lower() != "write":
                    continue
                tool_input = block.get("input")
                if not isinstance(tool_input, dict):
                    continue
                payload = _extract_verdict_payload_from_write_input(tool_input)
                if payload is None:
                    continue
                tool_path = str(
                    tool_input.get("file_path")
                    or tool_input.get("path")
                    or tool_input.get("filename")
                    or ""
                )
                return payload, {
                    "kind": "write_tool_verdict_recovered",
                    "messages_path": str(path),
                    "tool_path": tool_path,
                }
    return None


def _extract_verdict_payload_from_write_input(tool_input: dict[str, Any]) -> dict[str, Any] | None:
    raw_target = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("filename")
    target = str(raw_target or "")
    if target and Path(target).name != "verdict.json":
        return None
    raw_content = (
        tool_input.get("content")
        or tool_input.get("text")
        or tool_input.get("file_content")
        or tool_input.get("contents")
    )
    if not isinstance(raw_content, str):
        return None
    content = raw_content.strip()
    if not content:
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = _extract_verdict_json(content)
    if _is_canonical_shaped_verdict_payload(parsed):
        return parsed
    return None


def _is_canonical_shaped_verdict_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if "verdict" not in payload:
        return False
    if "journeys" not in payload or not isinstance(payload.get("journeys"), list):
        return False
    if not (_VERDICT_SCHEMA_HINT_KEYS & set(payload)):
        return False
    return _canonicalize_verdict_payload(payload) is not None


def _verdict_message_paths(session_dir: Path) -> list[Path]:
    return [
        session_dir / "lead" / "messages.jsonl",
        session_dir / "integration" / "lead" / "messages.jsonl",
    ]


async def _read_agent_verdict_with_rewrite(
    session_dir: Path,
    *,
    rewrite_once: Any,
) -> tuple[bool, dict[str, Any] | None, bool]:
    """Read verdict, invoking one canonical rewrite callback if needed."""
    called, payload = _read_agent_verdict(session_dir)
    if called:
        return called, payload, False
    context = _malformed_verdict_context(session_dir)
    if context is None:
        return False, None, False
    result = rewrite_once(context)
    if hasattr(result, "__await__"):
        await result
    called, payload = _read_agent_verdict(session_dir)
    return called, payload, True


async def _request_canonical_verdict_rewrite(
    *,
    session_dir: Path,
    options: Any,
    project_dir: Path,
    timeout_s: int,
) -> float:
    context = _malformed_verdict_context(session_dir)
    if context is None:
        return 0.0
    prompt = (
        "Otto could not parse your verdict.json because it was not in the "
        "canonical schema. Rewrite only the verdict file at this exact path:\n\n"
        f"{session_dir / 'verdict.json'}\n\n"
        "Use this JSON shape:\n"
        '{"verdict":"pass|partial|unverified|merge_blocked","summary":"...",'
        '"journeys":[],"evidence":[]}\n\n'
        "Original verdict.json content:\n"
        "```json\n"
        f"{context['original_text']}\n"
        "```\n\n"
        "Do not edit product code. Do not run a broad repair. Write the "
        "canonical verdict and stop."
    )
    try:
        from otto.agent import run_agent_with_timeout

        _text, cost, _agent_session_id, _breakdown = await run_agent_with_timeout(
            prompt,
            options,
            log_dir=session_dir / "lead" / "verdict-rewrite-01",
            phase_name="VERDICT_REWRITE",
            phase_label="verdict-rewrite",
            timeout=max(30, min(timeout_s, 300)),
            project_dir=project_dir,
        )
        return float(cost or 0.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("canonical verdict rewrite retry failed: %s", exc)
        return 0.0


def _rewrite_canonical_verdict(path: Path, payload: dict[str, Any]) -> None:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = None
    if existing == payload:
        return
    try:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


def _canonicalize_verdict_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    verdict = _verdict_token_to_canonical(payload.get("verdict"))
    if verdict in _CANONICAL_VERDICTS:
        canonical = dict(payload)
        if verdict == "pass" and not _pass_payload_has_evidence(canonical):
            verdict = "unverified"
        canonical["verdict"] = verdict
        canonical.setdefault("journeys", [])
        canonical.setdefault("evidence", list(payload.get("deliverables") or []))
        return canonical

    for key in ("status", "result", "outcome", "terminal_outcome", "state"):
        verdict = _verdict_token_to_canonical(payload.get(key))
        if verdict is not None:
            if verdict == "pass" and _tests_explicitly_fail(payload):
                verdict = "partial"
            elif verdict == "pass" and not _pass_payload_has_evidence(payload):
                verdict = "unverified"
            return _noncanonical_verdict(payload, verdict, source={key: payload.get(key)})

    for key in ("passed", "success", "ok"):
        if isinstance(payload.get(key), bool):
            verdict = "pass" if payload.get(key) else "partial"
            if verdict == "pass" and not _pass_payload_has_evidence(payload):
                verdict = "unverified"
            return _noncanonical_verdict(payload, verdict, source={key: payload.get(key)})
    return None


def _pass_payload_has_evidence(payload: dict[str, Any]) -> bool:
    journeys = payload.get("journeys")
    if isinstance(journeys, list) and bool(journeys):
        return True
    evidence = payload.get("evidence")
    if isinstance(evidence, list) and bool(evidence):
        return True
    deliverables = payload.get("deliverables")
    if isinstance(deliverables, list) and bool(deliverables):
        return True
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, list) and bool(artifacts):
        return True
    runner_checks = payload.get("runner_checks")
    if isinstance(runner_checks, list) and bool(runner_checks):
        return True
    if ("tests" in payload or "checks" in payload) and not _tests_explicitly_fail(payload):
        return True
    test_command = payload.get("test_command")
    if isinstance(test_command, str) and test_command.strip():
        return True
    coverage = payload.get("intent_coverage")
    if isinstance(coverage, dict):
        built = coverage.get("built")
        if isinstance(built, list) and bool(built):
            return True
    return False


def _verdict_token_to_canonical(value: Any) -> str | None:
    if isinstance(value, bool):
        return "pass" if value else "partial"
    token = str(value or "").strip().lower().replace("-", "_")
    if token in {"pass", "passed", "success", "succeeded", "ok", "done"}:
        return "pass"
    if token in {"partial", "warning", "warn", "fail", "failed", "failure", "error", "errored"}:
        return "partial"
    if token in {"unverified", "unknown", "unclear", "skipped"}:
        return "unverified"
    if token in {"merge_blocked", "blocked"}:
        return "merge_blocked"
    return None


def _noncanonical_verdict(
    payload: dict[str, Any],
    verdict: str,
    *,
    source: dict[str, Any],
) -> dict[str, Any]:
    canonical = dict(payload)
    canonical["verdict"] = verdict
    canonical.setdefault("summary", _summary_from_noncanonical(payload))
    canonical.setdefault("journeys", [])
    canonical.setdefault("evidence", list(payload.get("deliverables") or []))
    canonical["canonicalized_from"] = {
        **source,
        "keys": sorted(str(key) for key in payload.keys()),
    }
    return canonical


def _summary_from_noncanonical(payload: dict[str, Any]) -> str:
    summary = str(payload.get("summary") or payload.get("message") or "").strip()
    return summary or "Non-canonical verdict was mapped to canonical Otto schema."


def _tests_explicitly_fail(payload: dict[str, Any]) -> bool:
    tests = payload.get("tests", payload.get("checks"))
    return _value_indicates_failure(tests)


def _value_indicates_failure(value: Any) -> bool:
    if isinstance(value, bool):
        return not value
    if isinstance(value, str):
        return value.strip().lower() in {
            "fail", "failed", "failure", "error", "errored", "timeout"
        }
    if isinstance(value, (int, float)):
        # Generic "non-zero indicates failure" — applies to exit_code,
        # returncode, error_count, etc. NOTE: this is wrong for COUNT keys
        # like `passed`, which is why the dict branch handles `passed`
        # specially (bool/string only, not int recursion). Do not call this
        # branch directly on a `passed: N` count.
        return value != 0
    if isinstance(value, list):
        return any(_value_indicates_failure(item) for item in value)
    if isinstance(value, dict):
        for key in ("failed", "failures", "errors"):
            raw = value.get(key)
            if isinstance(raw, bool) and raw:
                return True
            if isinstance(raw, (int, float)) and raw > 0:
                return True
        # `passed` is a COUNT/BOOL — non-zero N is SUCCESS, not failure.
        # Recurse only when the value is bool or string (where 'pass'/'fail'
        # words actually carry failure semantics). Pre-2026-05-20 this
        # branch recursed unconditionally, causing `passed: 24` to be
        # interpreted as failure (24 != 0). That bug demoted Opus-shape
        # verdicts on the iTracker run, cascading to ~$120 in wasted
        # repair work. See [[feedback_journey_oracle_truth]] and the
        # foundation_contract / composite_landing_gate / upward_merge_gate
        # discard chain it triggered.
        passed_raw = value.get("passed")
        if isinstance(passed_raw, bool) and not passed_raw:
            return True
        if isinstance(passed_raw, str) and _value_indicates_failure(passed_raw):
            return True
        if "status" in value and _value_indicates_failure(value.get("status")):
            return True
        if "exit_code" in value and _value_indicates_failure(value.get("exit_code")):
            return True
        if "returncode" in value and _value_indicates_failure(value.get("returncode")):
            return True
        return any(
            _value_indicates_failure(item)
            for item in value.values()
            if isinstance(item, (dict, list, bool, str))
        )
    return False


def _verdict_failure_reason(session_dir: Path, *, integration: bool) -> str:
    context = _malformed_verdict_context(session_dir)
    if context is not None:
        original = str(context.get("original_text") or "")
        excerpt = original[:800] + ("..." if len(original) > 800 else "")
        return (
            "Agent wrote verdict.json, but Otto could not parse it as the "
            "canonical verdict schema. "
            f"path={context.get('path')}; error={context.get('error')}. "
            "Expected keys include verdict, summary, journeys, intent_coverage, "
            "evidence, and test_command. Excerpt:\n"
            f"{excerpt}"
        )
    if integration:
        return "Integration agent did not write verdict.json; marking unverified."
    return (
        "Agent did not write verdict.json; cannot certify pass. "
        "Agents must write their verdict to <session_dir>/verdict.json "
        "after running tests."
    )


def _malformed_verdict_context(session_dir: Path) -> dict[str, Any] | None:
    candidates = [session_dir / "verdict.json"]
    misplaced = _find_misplaced_verdict(session_dir)
    if misplaced is not None and misplaced not in candidates:
        candidates.append(misplaced)
    for path in candidates:
        if not path.exists():
            continue
        try:
            original_text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            parsed = json.loads(original_text)
        except json.JSONDecodeError as exc:
            return {
                "path": str(path),
                "original_text": original_text,
                "error": f"json_decode_error:{exc}",
            }
        if _canonicalize_verdict_payload(parsed) is None:
            return {
                "path": str(path),
                "original_text": original_text,
                "error": "noncanonical_shape",
            }
    return None


def _find_misplaced_verdict(session_dir: Path) -> Path | None:
    """Search the agent's worktree for a misplaced verdict.json.

    Agents are told to write to ``<session_dir>/verdict.json`` but some
    sessions instead drop it under the worktree (e.g.
    ``worktree/frontend/verdict.json``). When the canonical path is
    missing, check the obvious worktree locations first, then fall back
    to a bounded glob excluding noise dirs.
    """
    worktree = session_dir / "worktree"
    try:
        if worktree.is_symlink():
            real = worktree.resolve()
        elif worktree.exists():
            real = worktree
        else:
            return None
    except OSError:
        return None
    # Check the obvious spots (cheap, common-case).
    for sub in ("", "frontend", "backend", "api", "src", "web", "client", "server"):
        candidate = (real / sub / "verdict.json") if sub else (real / "verdict.json")
        if candidate.exists():
            return candidate
    # Bounded scan — first hit wins. Skip noise dirs.
    noise = {"node_modules", ".venv", "dist", "build", "__pycache__", ".git"}
    try:
        for p in real.rglob("verdict.json"):
            if any(part in noise for part in p.parts):
                continue
            return p
    except OSError:
        return None
    return None


def _rescue_verdict_from_messages(session_dir: Path) -> dict[str, Any] | None:
    """Search the last assistant turn(s) for an inline JSON verdict object.

    The agent's prompt asks it to write verdict.json via the Write tool, but
    agents sometimes inline the JSON in their final message instead. Providers
    do not all serialize that final answer the same way: Claude-style logs
    commonly expose assistant text blocks, while Codex/OpenAI-style logs may
    put the final answer on a terminal ``result`` record. Parse both so we
    don't lose work to a missing file.
    """
    for path in _verdict_message_paths(session_dir):
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        # Walk lines newest-first, looking for assistant blocks with text.
        lines = list(reversed(text.splitlines()))
        for raw in lines:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "result" and msg.get("subtype") == "success":
                structured_output = msg.get("structured_output")
                if _canonicalize_verdict_payload(structured_output) is not None:
                    return structured_output
                result_text = msg.get("result")
                if isinstance(result_text, str):
                    payload = _extract_verdict_json(result_text)
                    if payload is not None:
                        return payload
                continue
            if msg.get("type") != "assistant":
                continue
            for block in msg.get("blocks") or []:
                if block.get("type") != "text":
                    continue
                content = block.get("text") or ""
                payload = _extract_verdict_json(content)
                if payload is not None:
                    return payload
    return None


def _extract_verdict_json(text: str) -> dict[str, Any] | None:
    """Find the first JSON object containing a recognizable verdict in ``text``.

    Looks for fenced ```json blocks first, then for any balanced-brace JSON
    object. Returns the parsed dict if it has the verdict schema we expect.
    """
    import re

    # Try fenced ```json blocks first.
    for m in re.finditer(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL):
        try:
            obj = json.loads(m.group(1))
            if _canonicalize_verdict_payload(obj) is not None:
                return obj
        except json.JSONDecodeError:
            continue

    # Fall back to balanced-brace scan.
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = text[start:i+1]
                try:
                    obj = json.loads(candidate)
                    if _canonicalize_verdict_payload(obj) is not None:
                        return obj
                except json.JSONDecodeError:
                    pass
                start = -1
    return None


def _write_summary(session_dir: Path, result: LeadResult) -> None:
    """Write summary.json for this Lead's session."""
    session_dir.mkdir(parents=True, exist_ok=True)
    summary_path = session_dir / "summary.json"
    data: dict[str, Any] = {
        "schema_version": 1,
        "task_id": result.task_id,
        "verdict": result.verdict,
        "cost_usd": result.cost_usd,
        "duration_s": result.duration_s,
        "agent_session_id": result.agent_session_id,
        "decomposition": result.decomposition,
        "emitted_subtask_ids": list(result.emitted_subtask_ids),
        "verify_called": result.verify_called,
        "verify_result": result.verify_result,
        "failure_reason": result.failure_reason,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    summary_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _format_skipped_item(item: Any) -> str:
    if isinstance(item, dict):
        feature = str(item.get("feature") or item.get("id") or item.get("name") or "").strip()
        reason = str(item.get("reason") or item.get("gap") or item.get("detail") or "").strip()
        if feature and reason:
            return f"{feature}: {reason}"
        if feature:
            return feature
        if reason:
            return reason
        return json.dumps(item, sort_keys=True)
    return str(item).strip()


def _write_skipped_report(session_dir: Path, result: LeadResult) -> Path | None:
    """Append operator-visible action items for skipped intent coverage."""
    verify_result = result.verify_result if isinstance(result.verify_result, dict) else {}
    coverage = verify_result.get("intent_coverage") if isinstance(verify_result, dict) else None
    if not isinstance(coverage, dict):
        return None
    skipped = coverage.get("skipped") or []
    if not isinstance(skipped, list) or not skipped:
        return None

    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path = session_dir / "skipped_report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"## {timestamp} task={result.task_id} verdict={result.verdict}",
        "",
        "Manual follow-up required for skipped intent items:",
        "",
    ]
    for item in skipped:
        label = _format_skipped_item(item)
        if label:
            lines.append(f"- {label}")
    lines.append("")
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path
