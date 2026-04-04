"""Unified certifier report dataclasses.

These are the core types for the unified certifier. All tiers produce
TierResult objects, which aggregate into a CertificationReport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CertificationOutcome(Enum):
    """Top-level certification result.

    BLOCKED does not generate fix tasks — it's not a product bug,
    it's a classification or infrastructure issue.
    """
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class TierStatus(Enum):
    """Status of a single tier's execution."""
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"   # prerequisite tier failed
    SKIPPED = "skipped"   # not applicable for this product type


@dataclass
class Finding:
    """A single actionable finding from any tier."""
    tier: int
    severity: str                  # "critical", "important", "minor", "warning"
    category: str                  # "build", "endpoint", "regression", "journey", "edge-case"
    description: str
    diagnosis: str = ""
    fix_suggestion: str = ""
    evidence: dict[str, Any] | None = None
    story_id: str | None = None    # for tier 4 journey findings

    def to_journey_dict(self) -> dict[str, Any]:
        """Convert to legacy journey format for backward compat."""
        return {
            "name": self.description[:80],
            "story_id": self.story_id or "",
            "passed": False,
            "diagnosis": self.diagnosis,
            "fix_suggestion": self.fix_suggestion,
            "steps": [{
                "action": self.description,
                "outcome": "fail",
                "diagnosis": self.diagnosis,
                "fix_suggestion": self.fix_suggestion,
            }] if self.diagnosis else [],
        }


@dataclass
class TierResult:
    """Result of a single certification tier."""
    tier: int
    name: str                      # "structural", "probes", "regression", "journeys"
    status: TierStatus
    findings: list[Finding] = field(default_factory=list)
    blocked_by: str | None = None  # e.g. "tier_1:app_start"
    skip_reason: str | None = None
    duration_s: float = 0.0
    cost_usd: float = 0.0

    @property
    def passed(self) -> bool:
        return self.status == TierStatus.PASSED


@dataclass
class CertificationReport:
    """Complete certification report across all tiers."""
    product_type: str
    interaction: str               # "http", "cli", "library", "unknown"
    tiers: list[TierResult] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    outcome: CertificationOutcome = CertificationOutcome.FAILED
    cost_usd: float = 0.0
    duration_s: float = 0.0

    @property
    def passed(self) -> bool:
        return self.outcome == CertificationOutcome.PASSED

    def critical_findings(self) -> list[Finding]:
        """Findings that must be fixed (not warnings, not blocked derivatives)."""
        return [f for f in self.findings if f.severity in ("critical", "important")]

    def break_findings(self) -> list[Finding]:
        """All break findings (edge-case category), any severity."""
        return [f for f in self.findings if f.category == "edge-case"]

    def to_legacy_dict(self) -> dict[str, Any]:
        """Legacy compat for CLI display and telemetry. NOT for control flow."""
        # Journey-style entries from tier 4
        journeys: list[dict[str, Any]] = []
        for tier in self.tiers:
            if tier.tier == 4:
                for f in tier.findings:
                    if f.category != "edge-case":
                        journeys.append(f.to_journey_dict())
                break

        return {
            "product_passed": self.passed,
            "journeys": journeys,
            "break_findings": [
                {
                    "severity": f.severity,
                    "description": f.description,
                    "diagnosis": f.diagnosis,
                    "fix_suggestion": f.fix_suggestion,
                    "story_id": f.story_id,
                }
                for f in self.break_findings()
            ],
            "cost_usd": self.cost_usd,
            "duration_s": self.duration_s,
            "outcome": self.outcome.value,
        }
