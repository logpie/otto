"""Certifier report dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CertificationOutcome(Enum):
    """Top-level certification result."""
    PASSED = "passed"
    FAILED = "failed"


@dataclass
class CertificationReport:
    """Complete certification report.

    story_results / metric_value / metric_met are populated by the certifier
    after parsing agent output. Infra failures propagate as exceptions rather
    than synthetic outcome values.
    """
    outcome: CertificationOutcome = CertificationOutcome.FAILED
    cost_usd: float = 0.0
    duration_s: float = 0.0
    story_results: list[dict[str, Any]] = field(default_factory=list)
    metric_value: str = ""
    metric_met: bool | None = None  # None = not a target run
