"""Shared repair-evidence contract for audit verdict routing.

Audit output reaches Layer 2 through several paths: normal structured parser,
timeout recovery from ``feature-verdicts.json``, and product-quality projection.
Keep the evidence/actionability field shape here so those paths cannot drift.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RepairEvidence:
    """Typed view of the fields that make an audit finding repair-actionable."""

    raw_verdict: str = ""
    evidence_refs: tuple[str, ...] = ()
    check_evidence_refs: tuple[str, ...] = ()
    severity_findings: tuple[str, ...] = ()
    quality_findings: tuple[str, ...] = ()
    surface: str = ""
    methodology: str = ""
    evidence_completeness: str = ""
    coverage_confidence: str = ""

    @property
    def has_evidence_payload(self) -> bool:
        return bool(
            self.evidence_refs
            or self.check_evidence_refs
            or self.severity_findings
            or self.quality_findings
            or self.has_positive_strength
        )

    @property
    def has_positive_strength(self) -> bool:
        return self.evidence_completeness in {"full", "partial", "proxy_only"} or (
            self.coverage_confidence in {"high", "medium"}
        )

    @property
    def is_explicit_failing_verdict(self) -> bool:
        return self.raw_verdict in {"failed", "partial"}

    @property
    def is_actionable_for_repair(self) -> bool:
        return self.is_explicit_failing_verdict or self.has_evidence_payload


def repair_evidence_from_payload(payload: Any) -> RepairEvidence:
    """Extract the shared repair-evidence shape from a mapping or object."""
    return RepairEvidence(
        raw_verdict=_clean(
            _read(payload, "raw_verdict")
            or _read(payload, "verdict")
            or _read(payload, "status")
        ),
        evidence_refs=_string_tuple(_read(payload, "evidence_refs")),
        check_evidence_refs=_string_tuple(_read(payload, "check_evidence_refs")),
        severity_findings=_string_tuple(_read(payload, "severity_findings")),
        quality_findings=_string_tuple(_read(payload, "quality_findings")),
        surface=_text(_read(payload, "surface")),
        methodology=_text(_read(payload, "methodology")),
        evidence_completeness=_clean(_read(payload, "evidence_completeness")),
        coverage_confidence=_clean(_read(payload, "coverage_confidence")),
    )


def repair_evidence_payload_fields(payload: Any) -> dict[str, Any]:
    """Return non-empty repair-evidence fields for a Layer 2 verdict payload."""
    evidence = repair_evidence_from_payload(payload)
    out: dict[str, Any] = {}
    for key in ("surface", "methodology", "evidence_completeness", "coverage_confidence"):
        value = getattr(evidence, key)
        if value:
            out[key] = value
    for key in ("check_evidence_refs", "severity_findings", "quality_findings"):
        values = getattr(evidence, key)
        if values:
            out[key] = list(values)
    return out


def _read(payload: Any, key: str) -> Any:
    if isinstance(payload, Mapping):
        return payload.get(key)
    return getattr(payload, key, None)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _clean(value: Any) -> str:
    return str(value or "").strip().casefold()


def _text(value: Any) -> str:
    return str(value or "").strip()
