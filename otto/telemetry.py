"""Otto v4 telemetry — structured JSONL event writer with v3 dual-write."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Event dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TaskStarted:
    event: str = "task_started"
    task_key: str = ""
    task_id: int = 0
    prompt: str = ""
    strategy: str = "direct"
    timestamp: float = 0.0


@dataclass
class TaskMerged:
    event: str = "task_merged"
    task_key: str = ""
    task_id: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    diff_summary: str = ""
    timestamp: float = 0.0


@dataclass
class TaskFailed:
    event: str = "task_failed"
    task_key: str = ""
    task_id: int = 0
    error: str = ""
    cost_usd: float = 0.0
    duration_s: float = 0.0
    timestamp: float = 0.0


@dataclass
class PhaseCompleted:
    event: str = "phase_completed"
    task_key: str = ""
    phase: str = ""
    status: str = ""
    time_s: float = 0.0
    cost_usd: float = 0.0
    detail: str = ""
    timestamp: float = 0.0


@dataclass
class VerifyCompleted:
    event: str = "verify_completed"
    task_key: str = ""
    passed: bool = False
    tiers: list[dict[str, Any]] = field(default_factory=list)
    duration_s: float = 0.0
    timestamp: float = 0.0


@dataclass
class AgentToolCall:
    event: str = "agent_tool"
    task_key: str = ""
    name: str = ""
    detail: str = ""
    timestamp: float = 0.0


@dataclass
class ResearchComplete:
    event: str = "research_complete"
    task_key: str = ""
    query: str = ""
    summary: str = ""
    timestamp: float = 0.0


@dataclass
class BatchCompleted:
    event: str = "batch_completed"
    batch_index: int = 0
    tasks_passed: int = 0
    tasks_failed: int = 0
    timestamp: float = 0.0


@dataclass
class PlanCreated:
    event: str = "plan_created"
    total_batches: int = 0
    total_tasks: int = 0
    timestamp: float = 0.0


@dataclass
class AllDone:
    event: str = "all_done"
    total_passed: int = 0
    total_failed: int = 0
    total_missing_or_interrupted: int = 0
    total_cost: float = 0.0
    total_duration_s: float = 0.0
    timestamp: float = 0.0


# Union of all event types
TelemetryEvent = (
    TaskStarted | TaskMerged | TaskFailed | PhaseCompleted | VerifyCompleted |
    AgentToolCall | ResearchComplete | BatchCompleted | PlanCreated | AllDone
)


# ---------------------------------------------------------------------------
# Telemetry writer
# ---------------------------------------------------------------------------

class Telemetry:
    """Write-only JSONL telemetry with optional v3 dual-write.

    Events are appended to ``<log_dir>/v4_events.jsonl``. When legacy mode is
    enabled, events are also translated to v3 formats (pilot_results.jsonl,
    live-state.json) so that ``otto status -w`` and ``otto show`` still work.
    """

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._events_path = log_dir / "v4_events.jsonl"
        self._legacy_enabled = False
        self._legacy_results_path = log_dir / "pilot_results.jsonl"
        self._legacy_live_state_path = log_dir / "live-state.json"

    def enable_legacy_write(self) -> None:
        """Enable dual-write to v3 formats."""
        self._legacy_enabled = True
        # Clear live-state (current snapshot, not historical)
        # pilot_results.jsonl is append-only — don't clear it
        if self._legacy_live_state_path.exists():
            try:
                self._legacy_live_state_path.unlink()
            except OSError:
                pass

    def log(self, event: TelemetryEvent) -> None:
        """Append an event to the JSONL log. Fire-and-forget — never raises."""
        try:
            # Set timestamp if not already set
            if hasattr(event, "timestamp") and not event.timestamp:
                event.timestamp = time.time()

            data = asdict(event)
            line = json.dumps(data, default=str)
            with open(self._events_path, "a") as f:
                f.write(line + "\n")

            if self._legacy_enabled:
                self._write_legacy(event, data)
        except Exception:
            pass  # fire-and-forget

    def _write_legacy(self, event: TelemetryEvent, data: dict[str, Any]) -> None:
        """Translate v4 events to v3 pilot_results.jsonl and live-state.json."""
        try:
            if isinstance(event, TaskStarted):
                self._emit_legacy_progress({
                    "event": "phase", "task_key": event.task_key,
                    "name": "prepare", "status": "running",
                    "time_s": 0, "cost": 0,
                })
            elif isinstance(event, AgentToolCall):
                self._emit_legacy_progress({
                    "event": "agent_tool", "task_key": event.task_key,
                    "name": event.name, "detail": event.detail,
                })
            elif isinstance(event, VerifyCompleted):
                status = "done" if event.passed else "fail"
                self._emit_legacy_progress({
                    "event": "phase", "task_key": event.task_key,
                    "name": "test", "status": status,
                    "time_s": event.duration_s, "cost": 0,
                })
            elif isinstance(event, TaskMerged):
                self._emit_legacy_result({
                    "tool": "run_task_with_qa",
                    "success": True, "status": "passed",
                    "cost_usd": event.cost_usd,
                    "error": "",
                    "diff_summary": event.diff_summary,
                    "qa_report": "",
                    "phase_timings": {},
                })
            elif isinstance(event, TaskFailed):
                self._emit_legacy_result({
                    "tool": "run_task_with_qa",
                    "success": False, "status": "failed",
                    "cost_usd": event.cost_usd,
                    "error": event.error,
                    "diff_summary": "",
                    "qa_report": "",
                    "phase_timings": {},
                })
        except Exception:
            pass

    def _emit_legacy_progress(self, data: dict[str, Any]) -> None:
        """Write a progress event to pilot_results.jsonl."""
        data.setdefault("tool", "progress")
        with open(self._legacy_results_path, "a") as f:
            f.write(json.dumps(data) + "\n")

    def _emit_legacy_result(self, data: dict[str, Any]) -> None:
        """Write a task result to pilot_results.jsonl."""
        with open(self._legacy_results_path, "a") as f:
            f.write(json.dumps(data) + "\n")

    @property
    def events_path(self) -> Path:
        return self._events_path
