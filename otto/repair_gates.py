"""Repair-gate policy for audit findings.

Layer 2 repair is driven by actionable failing evidence, not by status names
alone. A non-passing feature verdict can mean either "the auditor found a real
product failure" or "the auditor could not evaluate this feature"; only the
former should spend a repair attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RepairGateDecision:
    action: str
    actionable: bool
    reason: str


REPAIR_NOW = "repair_now"
NO_REPAIR = "no_repair"

_NON_REPAIRABLE_CODE_KEYS = (
    "non_repairable_reason",
    "failure_kind",
    "error_kind",
    "blocker_kind",
    "provider_error_kind",
    "reason_code",
)

_NON_REPAIRABLE_REASONS_BY_CODE = {
    "provider_auth_exhausted": "provider authentication is exhausted; a coding agent cannot refresh credentials",
    "provider_auth_missing": "provider authentication is missing; a coding agent cannot create credentials",
    "provider_permission_denied": "provider permissions deny the request; code repair cannot grant access",
    "provider_quota_exhausted": "provider quota is exhausted; code repair cannot add quota",
}


def repair_gate_for_verdict(verdict_payload: dict[str, Any]) -> RepairGateDecision:
    """Classify whether an audit feature verdict should trigger repair."""
    verdict = _clean(verdict_payload.get("verdict"))
    if verdict in {"", "passed"}:
        return RepairGateDecision(NO_REPAIR, False, "feature already passed")

    non_repairable_reason = _typed_non_repairable_reason(verdict_payload)
    if non_repairable_reason is not None:
        return RepairGateDecision(NO_REPAIR, False, non_repairable_reason)

    # The legacy audit schema has no dedicated actionability boolean.
    # `failed`/`partial` are the auditor's explicit failing-finding signal;
    # ambiguous states such as `blocked`/`missing` need evidence below.
    if verdict in {"failed", "partial"} or _has_actionable_failing_evidence(verdict_payload):
        return RepairGateDecision(REPAIR_NOW, True, "audit finding is actionable")

    return RepairGateDecision(
        NO_REPAIR,
        False,
        "audit verdict has no actionable failing evidence",
    )


def _clean(value: Any) -> str:
    return str(value or "").strip().casefold()


def _typed_non_repairable_reason(payload: dict[str, Any]) -> str | None:
    for key in _NON_REPAIRABLE_CODE_KEYS:
        reason = _non_repairable_reason_for_code(payload.get(key))
        if reason is not None:
            return reason
    provider_error = payload.get("provider_error")
    if isinstance(provider_error, dict):
        for key in ("kind", "code", "error_kind", "failure_kind"):
            reason = _non_repairable_reason_for_code(provider_error.get(key))
            if reason is not None:
                return reason
    return None


def _non_repairable_reason_for_code(value: Any) -> str | None:
    code = _clean(value)
    if not code:
        return None
    return _NON_REPAIRABLE_REASONS_BY_CODE.get(code)


def _has_actionable_failing_evidence(payload: dict[str, Any]) -> bool:
    """Return True when an ambiguous verdict has concrete repair evidence.

    The primary signal is evidence attached to the failing finding. Product-wide
    quality findings and severity findings are also auditor-authored repair
    findings, even though they may not have per-feature artifact refs. Optional
    evidence-strength metadata counts only when it positively claims the
    feature was actually evaluated.
    """
    for key in ("evidence_refs", "check_evidence_refs", "severity_findings", "quality_findings"):
        if _list_has_values(payload.get(key)):
            return True
    return _has_positive_evidence_strength(payload)


def _list_has_values(value: Any) -> bool:
    return isinstance(value, list) and any(str(item).strip() for item in value)


def _has_positive_evidence_strength(payload: dict[str, Any]) -> bool:
    completeness = _clean(payload.get("evidence_completeness"))
    if completeness in {"full", "partial", "proxy_only"}:
        return True

    confidence = _clean(payload.get("coverage_confidence"))
    return confidence in {"high", "medium"}
