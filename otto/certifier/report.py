"""Certifier report dataclasses."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CertificationOutcome(Enum):
    """Top-level certification result."""
    PASSED = "passed"
    FAILED = "failed"
    INFRA_ERROR = "infra_error"


@dataclass
class CertificationReport:
    """Complete certification report."""
    outcome: CertificationOutcome = CertificationOutcome.FAILED
    cost_usd: float = 0.0
    duration_s: float = 0.0
