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
    legacy_results: list[dict[str, Any]] | None = None,
    executor_results: list[dict[str, Any]] | None = None,
    registered_executor_levels: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Compute every journey verdict through one fail-closed path.

    Unit 1 registers no executors, so legacy results supply compatibility
    verdicts. Once a controller executor is registered for a level, missing or
    malformed executor proof becomes non-pass and takes precedence over the
    legacy adapter.
    """

    legacy_by_id = _index_results(legacy_results or [])
    executor_by_id = _index_results(executor_results or [])
    registered = set(registered_executor_levels or set())
    out: list[dict[str, Any]] = []
    for journey in journeys:
        jid = str(journey.get("id") or "").strip() or "<unnamed>"
        level = str(journey.get("verification_level") or "").strip()
        if jid in executor_by_id:
            out.append(_normalize_executor_verdict(jid, executor_by_id[jid]))
            continue
        if level in registered:
            out.append(_fail_closed(
                jid,
                source="journey_verdict_sink",
                detail=f"registered executor for {level} produced no usable result",
            ))
            continue
        if level in {"ui", "api"}:
            applicability = applicability_for(execution_scope, cast(VerificationLevel, level))
            if applicability == "fail":
                out.append(_fail_closed(
                    jid,
                    source="journey_verdict_sink",
                    detail=f"journey applicability policy failed for {execution_scope}/{level}",
                ))
                continue
        else:
            out.append(_fail_closed(
                jid,
                source="journey_verdict_sink",
                detail="journey missing verification_level",
            ))
            continue
        if jid in legacy_by_id:
            out.append(_normalize_legacy_verdict(jid, legacy_by_id[jid]))
            continue
        out.append(_fail_closed(
            jid,
            source="journey_verdict_sink",
            detail="no executor result and no legacy adapter verdict",
        ))
    return out


def failed_journey_ids(verdicts: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("id"))
        for item in verdicts
        if item.get("id") and item.get("passed") is not True
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


def _normalize_legacy_verdict(jid: str, result: dict[str, Any]) -> dict[str, Any]:
    passed = bool(result.get("passed") is True)
    return {
        "id": jid,
        "passed": passed,
        "detail": str(result.get("detail") or ("legacy adapter passed" if passed else "legacy adapter failed")),
        "source": "legacy_adapter",
        "proof": False,
        "status": "pass" if passed else "fail",
    }


def _fail_closed(jid: str, *, source: str, detail: str) -> dict[str, Any]:
    return {
        "id": jid,
        "passed": False,
        "detail": detail,
        "source": source,
        "proof": False,
        "status": "unverified",
    }
