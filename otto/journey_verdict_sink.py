"""Central fail-closed behavior-journey verdict resolution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from otto.journey_scope_policy import ExecutionScope, VerificationLevel, applicability_for
from otto.schemas import VERDICT_UNVERIFIED

PASSING_STATUSES = frozenset({"pass", "passed"})
NON_PASS_STATUSES = frozenset({"fail", "failed", "unverified", "skip", "skipped", "defer", "deferred"})

# Agent's verdict.json detail needs to be at least this many chars to be
# considered "the agent actually observed something" vs "the agent wrote a
# stub". Keeps the fail-closed invariant: an agent that just stamped
# `passed: True` with no observation does not earn a pass.
_MIN_CREDIBLE_DETAIL_CHARS = 40


def resolve_journey_verdicts(
    *,
    journeys: list[dict[str, Any]],
    execution_scope: ExecutionScope,
    executor_results: list[dict[str, Any]] | None = None,
    registered_executor_levels: set[str] | None = None,
    session_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Compute every journey verdict through one fail-closed path.

    Controller-run executor results are the only pass source. For applicable
    journeys, missing or malformed executor proof becomes non-pass.

    Phase-1 unified verifier: when ``session_dir`` is given, also pulls
    agent-self-verified executor results from any verdict.json under
    ``session_dir`` (top-level OR ``integration/verdict.json``). The
    integration Lead drives behavior journeys via chrome-devtools MCP
    in-session and records per-journey verdicts + evidence in its own
    verdict.json — those records are the post-Phase-1 equivalent of what
    the deprecated Python journey runner used to write to
    ``journey-verdicts.json``. Without this, every UI journey would
    silently fail-closed as "registered executor for ui produced no
    usable result" simply because the deterministic Python runner no
    longer runs.
    """
    aggregated: list[dict[str, Any]] = list(executor_results or [])
    if session_dir is not None:
        aggregated.extend(agent_self_verified_executor_results(session_dir))
    executor_results = aggregated

    executor_by_id = _index_results(executor_results or [])
    registered = set(registered_executor_levels or {"ui", "api"})
    out: list[dict[str, Any]] = []
    for journey in journeys:
        jid = str(journey.get("id") or "").strip() or "<unnamed>"
        level = str(journey.get("verification_level") or "").strip()
        if level in {"ui", "api"}:
            applicability = applicability_for(execution_scope, cast(VerificationLevel, level))
            if applicability == "fail":
                out.append(_fail_closed(
                    journey,
                    source="journey_verdict_sink",
                    detail=f"journey applicability policy failed for {execution_scope}/{level}",
                ))
                continue
            if applicability in {"skip", "defer"}:
                out.append(_not_applicable(
                    jid,
                    status=applicability,
                    detail=f"journey applicability policy {applicability} for {execution_scope}/{level}",
                ))
                continue
        else:
            out.append(_fail_closed(
                journey,
                source="journey_verdict_sink",
                detail="journey missing verification_level",
            ))
            continue

        if jid in executor_by_id:
            out.append(_normalize_executor_verdict(jid, executor_by_id[jid]))
            continue
        if level in registered:
            out.append(_fail_closed(
                journey,
                source="journey_verdict_sink",
                detail=f"registered executor for {level} produced no usable result",
            ))
            continue
        out.append(_fail_closed(
            journey,
            source="journey_verdict_sink",
            detail=f"no registered executor for verification_level {level!r}",
        ))
    return out


def agent_self_verified_executor_results(session_dir: Path) -> list[dict[str, Any]]:
    """Convert the integration Lead's verdict.json journeys[] into
    executor-result entries the fail-closed sink will accept.

    The integration Lead's prompt requires it to (a) drive each behavior
    journey live via chrome-devtools / Bash, (b) save evidence files
    (screenshots, etc.) under ``integration/screenshots/`` or similar,
    and (c) record per-journey verdicts in ``verdict.json``. Those
    records are the post-Phase-1 equivalent of what the deprecated
    Python journey runner used to write to ``journey-verdicts.json``.

    Credibility (so the fail-closed invariant survives): a journey
    earns ``proof_usable=True`` only when ALL of:

      1. ``passed: True`` in the agent's verdict
      2. ``detail`` is at least ``_MIN_CREDIBLE_DETAIL_CHARS`` chars
         (rejects stub claims like just ``"passed"`` with no
         observation)
      3. Parent ``verdict.json`` has a non-empty ``evidence`` list
         (the prompt requires evidence; an agent that forgot still
         fails-closed)

    Without all 3, the entry still appears in the result so the sink
    can produce an honest signal, but ``proof_usable=False`` and the
    sink's ``_normalize_executor_verdict`` will mark it
    ``status=unverified`` — same as today's behavior.
    """
    out: list[dict[str, Any]] = []
    for path in (
        session_dir / "verdict.json",
        session_dir / "integration" / "verdict.json",
    ):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        journeys = payload.get("journeys") or []
        if not isinstance(journeys, list):
            continue
        evidence = payload.get("evidence") or []
        has_evidence = isinstance(evidence, list) and any(
            isinstance(e, str) and e.strip() and not e.strip().startswith("#")
            for e in evidence
        )
        for journey in journeys:
            if not isinstance(journey, dict):
                continue
            jid = str(journey.get("id") or "").strip()
            if not jid:
                continue
            passed = bool(journey.get("passed"))
            detail = str(journey.get("detail") or "")
            credible = (
                passed and len(detail) >= _MIN_CREDIBLE_DETAIL_CHARS and has_evidence
            )
            out.append({
                "id": jid,
                "status": "pass" if passed else "fail",
                "proof_usable": credible,
                "detail": detail or "agent self-verified (no detail)",
                "source": "integration_lead_self_verify",
            })
    return out


def failed_journey_ids(verdicts: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("id"))
        for item in verdicts
        if item.get("id")
        and (
            str(item.get("status") or "").strip().lower() in {"fail", "failed", VERDICT_UNVERIFIED}
            or (
                "status" not in item
                and item.get("passed") is not True
            )
        )
    ]


def _index_results(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for item in results:
        jid = str(item.get("id") or "").strip()
        if jid:
            indexed[jid] = item
    return indexed


def _normalize_executor_verdict(jid: str, result: dict[str, Any]) -> dict[str, Any]:
    status = str(result.get("status") or result.get("verdict") or "").strip().lower()
    proof_usable = bool(result.get("proof_usable") is True)
    source = str(result.get("source") or "executor")
    if status in PASSING_STATUSES and proof_usable:
        return {
            "id": jid,
            "passed": True,
            "detail": str(result.get("detail") or "controller executor passed"),
            "source": source,
            "proof": True,
            "status": "pass",
        }
    if status in PASSING_STATUSES and not proof_usable:
        detail = "controller executor result was pass but proof_usable=false"
    else:
        detail = str(result.get("detail") or f"controller executor status={status or 'missing'}")
    return {
        "id": jid,
        "passed": False,
        "detail": detail,
        "source": source,
        "proof": proof_usable,
        "status": status if status in NON_PASS_STATUSES else "unverified",
    }


def _fail_closed(journey: dict[str, Any], *, source: str, detail: str) -> dict[str, Any]:
    """Build a fail-closed verdict that preserves journey metadata downstream.

    Downstream consumers (proof-packet rendering, feature audits) link
    journey verdicts back to features via `feature_id` and co. Earlier
    versions of this helper dropped those keys; the audit at
    archive/audits/audit-journey.md flagged the loss as a medium bug.
    """
    jid = str(journey.get("id") or "").strip() or "<unnamed>"
    result: dict[str, Any] = {
        "id": jid,
        "passed": False,
        "detail": detail,
        "source": source,
        "proof": False,
        "status": "unverified",
    }
    for key in ("feature_id", "covers_primary_actions", "group_id", "verification_level"):
        if key in journey:
            result[key] = journey[key]
    return result


def _not_applicable(jid: str, *, status: str, detail: str) -> dict[str, Any]:
    return {
        "id": jid,
        "passed": False,
        "detail": detail,
        "source": "journey_scope_policy",
        "proof": False,
        "status": status,
    }
