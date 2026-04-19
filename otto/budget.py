"""Run budget: total wall-clock cap across an entire otto invocation.

Replaces per-agent-call `agent_timeout` / `certifier_timeout` as the primary
user-facing knob. One number (default 3600s = 1h) that caps the full
`otto build` / `otto certify` / `otto improve` run, no matter how many
internal agent calls the pipeline makes.

Usage:
    budget = RunBudget.start_from(config)
    # ... pass budget through the pipeline ...

    # Before each agent call:
    if budget.exhausted():
        write_paused_checkpoint(...)
        return

    # Compute per-call timeout (shrinks as budget drains):
    timeout = budget.for_call(safety_cap=some_safety_value)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("otto.budget")


@dataclass
class RunBudget:
    """Wall-clock budget tracker for a single otto invocation."""

    total: float                    # seconds allotted for the whole run
    start: float = field(default_factory=time.monotonic)

    @classmethod
    def start_from(cls, config: dict[str, Any]) -> RunBudget:
        """Build a RunBudget from config. Reads `run_budget_seconds`."""
        from otto.config import get_run_budget
        return cls(total=float(get_run_budget(config)))

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def remaining(self) -> float:
        return max(0.0, self.total - self.elapsed())

    def exhausted(self) -> bool:
        return self.remaining() <= 0

    def for_call(self, safety_cap: int | None = None) -> int:
        """Per-agent-call timeout in seconds.

        Returns the lesser of `remaining()` and `safety_cap`. Callers must
        check `exhausted()` BEFORE calling; this method does NOT floor at a
        positive value, so `asyncio.wait_for` will correctly raise
        TimeoutError immediately if called with a non-positive timeout.

        `safety_cap` exists as a belt-and-suspenders escape hatch for the
        deprecated `agent_timeout` config key — when both are set, the
        smaller bounds the call.
        """
        remaining = int(self.remaining())
        # Ignore zero/negative caps — they'd force an immediate timeout even
        # when budget is plenty. Treat them as "no cap" (user misconfig).
        if safety_cap is not None and safety_cap > 0:
            remaining = min(remaining, safety_cap)
        return remaining
