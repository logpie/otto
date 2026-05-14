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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

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
    record_task(
        project_dir,
        task_id=task_id,
        intent=intent,
        integration_branch=integration_branch,
    )

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
        result.decomposition = graph_entry.get("decomposition") or "unknown"
        result.emitted_subtask_ids = list(graph_entry.get("child_task_ids") or [])

        # Verdict comes from the agent's own verdict.json (written via Write).
        # Agents are responsible for running their own tests and reporting
        # honest results. The runner trusts the file the agent wrote, with
        # one exception: if the agent decomposed, it can't claim a leaf
        # verdict — its terminal state is pending_children until parent
        # integration runs.
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
            failure_reason = (
                "Integration agent did not write verdict.json; "
                "marking unverified."
            )
        else:
            # Leaf agent ended without writing verdict.json.
            result.verdict = "unverified"
            failure_reason = (
                "Agent did not write verdict.json; cannot certify pass. "
                "Agents must write their verdict to <session_dir>/verdict.json "
                "after running tests."
            )

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
                    node_kind=("integration" if is_integration else "leaf"),
                    matrix_scope=_verification_matrix_scope(config),
                )
                if runner_outcome.final_verdict in (
                    "pass",
                    "partial",
                    "unverified",
                    "merge_blocked",
                ):
                    result.verdict = runner_outcome.final_verdict  # type: ignore[assignment]
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
) -> str:
    """Render the Lead's prompt by interpolating into the template."""
    template_name = "lead.md" if kind == "plan_or_inline" else "lead-integration.md"
    template = _read_prompt_template(template_name)

    journeys_path = session_dir / "spec" / "spec.json"
    is_root = integration_branch is None

    # Tier preset modifies prompt content slightly.
    tier_hint = ""
    if tier == "solo":
        tier_hint = "\n## Tier preset: solo\n\nYou MUST call mcp__otto__begin_inline. Do NOT call mcp__otto__submit_subtask.\n"
    elif tier == "modular":
        tier_hint = "\n## Tier preset: modular\n\nThis intent involves multiple subsystems. Strongly prefer mcp__otto__submit_subtask over inline build. Aim for ≥3 subtasks.\n"

    if kind == "plan_or_inline":
        return _interpolate_prompt(template, {
            "task_id": task_id,
            "intent": intent,
            "is_root": str(is_root).lower(),
            "journeys_path": str(journeys_path),
            "integration_branch": str(integration_branch or "main"),
            "session_dir": str(session_dir),
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
        preflight_text = json.dumps(
            rendered_preflight,
            indent=2,
            sort_keys=True,
        )
        return _interpolate_prompt(template, {
            "task_id": task_id,
            "intent": intent,
            "integration_branch": str(integration_branch or "main"),
            "child_summaries": summary_text,
            "preflight_result": preflight_text,
            "journeys_path": str(journeys_path),
            "session_dir": str(session_dir),
        })


def _verification_matrix_scope(config: dict[str, Any]) -> str:
    """Return the v5 runner verification matrix policy.

    Backward compatible default is ``leaf`` which preserves the historical full
    matrix at every node. New v6 runs can opt into ``integration_only`` under
    ``verification_plan.matrix_scope``.
    """
    plan = config.get("verification_plan") if isinstance(config, dict) else None
    value = plan.get("matrix_scope") if isinstance(plan, dict) else None
    if value in {"leaf", "integration_only"}:
        return str(value)
    return "leaf"


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
            if isinstance(payload, dict) and payload.get("verdict"):
                return True, payload
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
            if isinstance(payload, dict) and payload.get("verdict"):
                logger.warning(
                    "agent wrote verdict.json to %s instead of %s — recovering",
                    misplaced, candidate,
                )
                try:
                    candidate.write_text(
                        misplaced.read_text(encoding="utf-8"), encoding="utf-8"
                    )
                except OSError:
                    pass
                return True, payload
        except (OSError, json.JSONDecodeError):
            pass

    # Legacy fallback: pre-simplification verify-result.json
    legacy = session_dir / "verify" / "verify-result.json"
    if legacy.exists():
        try:
            payload = json.loads(legacy.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("verdict"):
                return True, payload
        except (OSError, json.JSONDecodeError):
            pass

    # Rescue: agent inlined the verdict JSON in its final text message but
    # forgot to write the file. Walk lead/messages.jsonl looking for a JSON
    # object with the expected shape in the last assistant turn.
    rescued = _rescue_verdict_from_messages(session_dir)
    if rescued is not None:
        try:
            (session_dir / "verdict.json").write_text(
                json.dumps(rescued, indent=2) + "\n", encoding="utf-8",
            )
        except OSError:
            pass
        return True, rescued

    return False, None


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
    agents sometimes inline the JSON in their final message instead. Parse
    the message stream as a fallback so we don't lose work to a missing file.
    """
    messages_paths = [
        session_dir / "lead" / "messages.jsonl",
        session_dir / "integration" / "lead" / "messages.jsonl",
    ]
    for path in messages_paths:
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
            if not isinstance(msg, dict) or msg.get("type") != "assistant":
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
    """Find the first JSON object containing a `verdict` field in ``text``.

    Looks for fenced ```json blocks first, then for any balanced-brace JSON
    object. Returns the parsed dict if it has the verdict schema we expect.
    """
    import re

    # Try fenced ```json blocks first.
    for m in re.finditer(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL):
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict) and obj.get("verdict") in (
                "pass", "partial", "unverified", "merge_blocked",
            ):
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
                    if isinstance(obj, dict) and obj.get("verdict") in (
                        "pass", "partial", "unverified", "merge_blocked",
                    ):
                        return obj
                except json.JSONDecodeError:
                    pass
                start = -1
    return None


def _detect_verify(log_dir: Path) -> tuple[bool, dict[str, Any] | None]:
    """Determine if mcp__otto__verify ran and capture its result.

    Two sources, in order of authority:
      1. ``<session_dir>/verify/verify-result.json`` — written by the verify
         tool itself when it runs. AUTHORITATIVE.
      2. SDK message stream (messages.jsonl etc.) — fallback for cases where
         the verify tool short-circuited before writing.
    """
    # Source 1: verify-result.json (the verify tool writes this on success).
    # log_dir is .../session_dir/lead; verify writes to .../session_dir/verify/.
    session_dir = log_dir.parent
    verify_result_path = session_dir / "verify" / "verify-result.json"
    if verify_result_path.exists():
        try:
            payload = json.loads(verify_result_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("verdict"):
                return True, payload
        except (OSError, json.JSONDecodeError):
            pass

    # Source 2: walk messages.jsonl for the tool_use marker. Stream parsing
    # for the tool_result block is less reliable than reading verify-result.json
    # directly, so we use this only to confirm the tool was attempted.
    candidates = [log_dir / "messages.jsonl"]
    for cand in (log_dir / "stream.jsonl", log_dir / "narrative.jsonl"):
        candidates.append(cand)
    seen_call = False
    for path in candidates:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            blocks = msg.get("blocks") or []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = (block.get("name") or "").lower()
                    if "verify" in name and "otto" in name:
                        seen_call = True
        if seen_call:
            break
    return seen_call, None


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
