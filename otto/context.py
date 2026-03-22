"""Otto v4 pipeline context — shared state for PER orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TaskResult:
    """Result of a single task execution in the v4 pipeline."""
    task_key: str
    success: bool
    commit_sha: str | None = None
    worktree: Path | None = None
    cost_usd: float = 0.0
    error: str | None = None
    qa_report: str = ""
    diff_summary: str = ""
    duration_s: float = 0.0


class PipelineContext:
    """Shared mutable state for the PER orchestrator.

    All agents read from and write to this context. Thread-safe access is
    guaranteed by the orchestrator running agents under asyncio (single thread).
    """

    def __init__(self) -> None:
        self.learnings: list[str] = []
        self._research: dict[str, str] = {}
        self.results: dict[str, TaskResult] = {}  # task_key -> TaskResult
        self.session_ids: dict[str, str] = {}     # task_key -> session_id
        self.costs: dict[str, float] = {}         # task_key -> cost
        self.interrupted: bool = False
        self.pids: set[int] = set()               # tracked subprocess PIDs

    def add_research(self, key: str, content: str) -> None:
        """Store research findings by key (e.g., task_key or topic)."""
        self._research[key] = content

    def get_research(self, key: str) -> str | None:
        """Retrieve research findings by key."""
        return self._research.get(key)

    def add_success(self, result: TaskResult) -> None:
        """Record a successful task result."""
        self.results[result.task_key] = result
        if result.cost_usd > 0:
            self.costs[result.task_key] = result.cost_usd

    def add_failure(self, result: TaskResult) -> None:
        """Record a failed task result."""
        self.results[result.task_key] = result
        if result.cost_usd > 0:
            self.costs[result.task_key] = result.cost_usd

    @property
    def total_cost(self) -> float:
        """Total cost across all tasks."""
        return sum(self.costs.values())

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results.values() if r.success)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results.values() if not r.success)
