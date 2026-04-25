"""Schema for Otto's canonical live run records."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


RUN_RECORD_SCHEMA_VERSION = 1
STATUS_VALUES = {
    "queued",
    "starting",
    "initializing",
    "running",
    "terminating",
    "paused",
    "interrupted",
    "done",
    "failed",
    "cancelled",
    "removed",
}
TERMINAL_STATUSES = {"done", "failed", "cancelled", "removed", "interrupted"}


@dataclass
class RunRecord:
    """Canonical discovery record for one long-running Otto run."""

    run_id: str
    domain: str
    run_type: str
    command: str
    display_name: str
    status: str
    project_dir: str
    cwd: str
    terminal_outcome: str | None = None
    schema_version: int = RUN_RECORD_SCHEMA_VERSION
    writer: dict[str, Any] = field(default_factory=dict)
    identity: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    timing: dict[str, Any] = field(default_factory=dict)
    git: dict[str, Any] = field(default_factory=dict)
    intent: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    adapter_key: str = ""
    last_event: str = ""
    version: int = 1
    extra_fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = deepcopy(self.extra_fields)
        data.update({
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "domain": self.domain,
            "run_type": self.run_type,
            "command": self.command,
            "display_name": self.display_name,
            "status": self.status,
            "terminal_outcome": self.terminal_outcome,
            "project_dir": self.project_dir,
            "cwd": self.cwd,
            "writer": deepcopy(self.writer),
            "identity": deepcopy(self.identity),
            "source": deepcopy(self.source),
            "timing": deepcopy(self.timing),
            "git": deepcopy(self.git),
            "intent": deepcopy(self.intent),
            "artifacts": deepcopy(self.artifacts),
            "metrics": deepcopy(self.metrics),
            "adapter_key": self.adapter_key,
            "last_event": self.last_event,
            "version": self.version,
        })
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunRecord":
        if not isinstance(data, dict):
            raise ValueError("run record must be a JSON object")
        if data.get("schema_version") != RUN_RECORD_SCHEMA_VERSION:
            raise ValueError(
                "run record schema_version mismatch "
                f"(got {data.get('schema_version')!r}, expected {RUN_RECORD_SCHEMA_VERSION})"
            )
        run_id = str(data.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("run record missing run_id")
        status = str(data.get("status") or "").strip()
        if status not in STATUS_VALUES:
            raise ValueError(f"run record {run_id}: invalid status {status!r}")

        known = {
            "schema_version",
            "run_id",
            "domain",
            "run_type",
            "command",
            "display_name",
            "status",
            "terminal_outcome",
            "project_dir",
            "cwd",
            "writer",
            "identity",
            "source",
            "timing",
            "git",
            "intent",
            "artifacts",
            "metrics",
            "adapter_key",
            "last_event",
            "version",
        }
        extra = {k: deepcopy(v) for k, v in data.items() if k not in known}
        return cls(
            schema_version=RUN_RECORD_SCHEMA_VERSION,
            run_id=run_id,
            domain=str(data.get("domain") or ""),
            run_type=str(data.get("run_type") or ""),
            command=str(data.get("command") or ""),
            display_name=str(data.get("display_name") or ""),
            status=status,
            terminal_outcome=(
                str(data["terminal_outcome"])
                if data.get("terminal_outcome") is not None
                else None
            ),
            project_dir=str(data.get("project_dir") or ""),
            cwd=str(data.get("cwd") or ""),
            writer=dict(data.get("writer") or {}),
            identity=dict(data.get("identity") or {}),
            source=dict(data.get("source") or {}),
            timing=dict(data.get("timing") or {}),
            git=dict(data.get("git") or {}),
            intent=dict(data.get("intent") or {}),
            artifacts=dict(data.get("artifacts") or {}),
            metrics=dict(data.get("metrics") or {}),
            adapter_key=str(data.get("adapter_key") or ""),
            last_event=str(data.get("last_event") or ""),
            version=int(data.get("version") or 1),
            extra_fields=extra,
        )


def is_terminal_status(status: str | None) -> bool:
    return str(status or "") in TERMINAL_STATUSES
