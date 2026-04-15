"""Unified certifier report dataclasses.

These are the core types for the unified certifier. All tiers produce
TierResult objects, which aggregate into a CertificationReport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CertificationOutcome(Enum):
    """Top-level certification result."""
    PASSED = "passed"
    FAILED = "failed"
    INFRA_ERROR = "infra_error"


class TierStatus(Enum):
    """Status of a single tier's execution."""
    PASSED = "passed"
    FAILED = "failed"


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



@dataclass
class TierResult:
    """Result of a single certification tier."""
    tier: int
    name: str                      # "structural", "probes", "regression", "journeys"
    status: TierStatus
    findings: list[Finding] = field(default_factory=list)
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
