"""Central fail-closed behavior-journey verdict resolution."""

from __future__ import annotations

from typing import Any, cast

from otto.journey_scope_policy import ExecutionScope, VerificationLevel, applicability_for

PASSING_STATUSES = frozenset({"pass", "passed"})
NON_PASS_STATUSES = frozenset({"fail", "failed", "unverified", "skip", "skipped", "defer", "deferred"})


def resolve_journey_verdicts(
    *,
    journeys: list[dict[str, Any]],
    execution_scope: ExecutionScope,
    executor_results: list[dict[str, Any]] | None = None,
    registered_executor_levels: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Compute every journey verdict through one fail-closed path.

    Controller-run executor results are the only pass source. For applicable
    journeys, missing or malformed executor proof becomes non-pass.
    """

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


def failed_journey_ids(verdicts: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("id"))
        for item in verdicts
        if item.get("id")
        and (
            str(item.get("status") or "").strip().lower() in {"fail", "failed", "unverified"}
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
