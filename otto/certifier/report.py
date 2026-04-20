"""Certifier report dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CertificationOutcome(Enum):
    """Top-level certification result."""
    PASSED = "passed"
    FAILED = "failed"
    INFRA_ERROR = "infra_error"


@dataclass
class CertificationReport:
    """Complete certification report.

    story_results / metric_value / metric_met are populated by the certifier
    after parsing agent output. They're optional so INFRA_ERROR / crash
    reports can be constructed without them.

    run_id (Phase 1.4): the standalone certifier's per-invocation directory
    name, e.g. ``certify-1734567890-12345``. Used by `otto certify` to locate
    the proof-of-work artifact when writing the manifest.
    """
    outcome: CertificationOutcome = CertificationOutcome.FAILED
    cost_usd: float = 0.0
    duration_s: float = 0.0
    story_results: list[dict[str, Any]] = field(default_factory=list)
    metric_value: str = ""
    metric_met: bool | None = None  # None = not a target run
    run_id: str = ""
