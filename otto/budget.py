"""Run budget: total wall-clock cap across an entire otto invocation.

The single user-facing timeout knob (default 3600s = 1h). Caps the full
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
    timeout = budget.for_call()
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("otto.budget")


@dataclass
class RunBudget:
    """Wall-clock budget tracker for a single otto invocation."""

    total: float                    # seconds allotted for the whole run
    start: float | None = None
    session_started_at: float | None = None

    def __post_init__(self) -> None:
        if self.start is None and self.session_started_at is None:
            self.session_started_at = time.time()

    @classmethod
    def start_from(
        cls,
        config: dict[str, Any],
        *,
        session_started_at: str | float | None = None,
    ) -> RunBudget:
        """Build a RunBudget from config. Reads `run_budget_seconds`."""
        from otto.config import get_run_budget
        started = time.time()
        if isinstance(session_started_at, int | float):
            started = float(session_started_at)
        elif isinstance(session_started_at, str) and session_started_at:
            try:
                started = datetime.fromisoformat(
                    session_started_at.replace("Z", "+00:00")
                ).astimezone(timezone.utc).timestamp()
            except ValueError:
                started = time.time()
        return cls(total=float(get_run_budget(config)), session_started_at=started)

    def elapsed(self) -> float:
        if self.start is not None:
            return max(0.0, time.monotonic() - self.start)
        started = self.session_started_at if self.session_started_at is not None else time.time()
        return max(0.0, time.time() - started)

    def remaining(self) -> float:
        return max(0.0, self.total - self.elapsed())

    def exhausted(self) -> bool:
        return self.remaining() <= 0

    def for_call(self) -> int:
        """Per-agent-call timeout in seconds.

        Returns remaining budget as int. Callers must check `exhausted()`
        BEFORE calling; this method does NOT floor at a positive value, so
        `asyncio.wait_for` will correctly raise TimeoutError immediately if
        called with a non-positive timeout.
        """
        return int(self.remaining())
