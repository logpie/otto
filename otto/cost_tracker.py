"""Append-only cost ledger for Otto task accounting."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
import time


@dataclass(frozen=True)
class CostEntry:
    """One immutable ledger entry."""

    kind: str
    amount_usd: float
    task_key: str | None = None
    allocations: dict[str, float] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


class CostTracker:
    """Thread-safe append-only ledger with task/run aggregation helpers."""

    def __init__(self) -> None:
        self._entries: list[CostEntry] = []
        self._lock = Lock()

    def record(
        self,
        *,
        kind: str,
        amount_usd: float | None = None,
        task_key: str | None = None,
        allocations: dict[str, float] | None = None,
    ) -> CostEntry | None:
        normalized_allocations = self._normalize_allocations(allocations)
        normalized_amount = self._normalize_amount(amount_usd, normalized_allocations)
        if normalized_amount <= 0:
            return None
        if task_key and normalized_allocations:
            raise ValueError("record() accepts either task_key or allocations, not both")
        if not task_key and not normalized_allocations:
            raise ValueError("record() requires task_key or allocations")

        entry = CostEntry(
            kind=str(kind),
            amount_usd=normalized_amount,
            task_key=str(task_key) if task_key else None,
            allocations=normalized_allocations,
        )
        with self._lock:
            self._entries.append(entry)
        return entry

    def entries(self) -> list[CostEntry]:
        with self._lock:
            return list(self._entries)

    def run_total(self) -> float:
        with self._lock:
            return sum(entry.amount_usd for entry in self._entries)

    def task_total(self, task_key: str) -> float:
        key = str(task_key)
        with self._lock:
            return sum(self._task_share(entry, key) for entry in self._entries)

    def task_phase_breakdown(self, task_key: str) -> dict[str, float]:
        key = str(task_key)
        breakdown: dict[str, float] = {}
        with self._lock:
            for entry in self._entries:
                share = self._task_share(entry, key)
                if share <= 0:
                    continue
                breakdown[entry.kind] = breakdown.get(entry.kind, 0.0) + share
        return breakdown

    def task_snapshot(self, task_key: str) -> dict[str, object]:
        key = str(task_key)
        breakdown: dict[str, float] = {}
        entry_count = 0
        with self._lock:
            for entry in self._entries:
                share = self._task_share(entry, key)
                if share <= 0:
                    continue
                entry_count += 1
                breakdown[entry.kind] = breakdown.get(entry.kind, 0.0) + share
        return {
            "task_key": key,
            "total_cost_usd": sum(breakdown.values()),
            "phase_costs": breakdown,
            "entry_count": entry_count,
        }

    def __contains__(self, task_key: object) -> bool:
        if not isinstance(task_key, str):
            return False
        return self.task_total(task_key) > 0

    @staticmethod
    def _normalize_allocations(allocations: dict[str, float] | None) -> dict[str, float]:
        if not allocations:
            return {}
        normalized: dict[str, float] = {}
        for key, value in allocations.items():
            amount = float(value or 0.0)
            if amount <= 0:
                continue
            normalized[str(key)] = normalized.get(str(key), 0.0) + amount
        return normalized

    @staticmethod
    def _normalize_amount(amount_usd: float | None, allocations: dict[str, float]) -> float:
        if amount_usd is None:
            return sum(allocations.values())
        return float(amount_usd or 0.0)

    @staticmethod
    def _task_share(entry: CostEntry, task_key: str) -> float:
        if entry.task_key == task_key:
            return entry.amount_usd
        return float(entry.allocations.get(task_key, 0.0) or 0.0)
