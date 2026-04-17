"""Checkpoint/resume for long-running otto operations.

Writes checkpoint after each round so runs can survive crashes,
pause/resume, and recover from errors. Works for both agent mode
(stores session_id for SDK resume) and split mode (stores round number).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("otto.checkpoint")

CHECKPOINT_FILE = "otto_logs/checkpoint.json"


def write_checkpoint(
    project_dir: Path,
    *,
    run_id: str,
    command: str,
    mode: str,
    certifier_mode: str = "thorough",
    prompt_mode: str = "build",
    branch: str = "",
    focus: str | None = None,
    target: str | None = None,
    max_rounds: int = 8,
    status: str = "in_progress",
    session_id: str = "",
    current_round: int = 0,
    head_sha: str = "",
    total_cost: float = 0.0,
    rounds: list[dict[str, Any]] | None = None,
) -> None:
    """Write checkpoint to disk. Called after each round."""
    checkpoint_path = project_dir / CHECKPOINT_FILE
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "run_id": run_id,
        "command": command,
        "mode": mode,
        "certifier_mode": certifier_mode,
        "prompt_mode": prompt_mode,
        "branch": branch,
        "focus": focus,
        "target": target,
        "max_rounds": max_rounds,
        "status": status,
        "session_id": session_id,
        "current_round": current_round,
        "head_sha": head_sha,
        "total_cost": round(total_cost, 2),
        "rounds": rounds or [],
        "started_at": _read_started_at(checkpoint_path),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    checkpoint_path.write_text(json.dumps(data, indent=2))
    logger.debug("Checkpoint written: round %d, status=%s", current_round, status)


def load_checkpoint(project_dir: Path) -> dict[str, Any] | None:
    """Load checkpoint if one exists and is active (in_progress or paused)."""
    checkpoint_path = project_dir / CHECKPOINT_FILE
    if not checkpoint_path.exists():
        return None

    try:
        data = json.loads(checkpoint_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    if data.get("status") not in ("in_progress", "paused"):
        return None

    return data


def clear_checkpoint(project_dir: Path) -> None:
    """Remove checkpoint file (run completed or user chose fresh start)."""
    checkpoint_path = project_dir / CHECKPOINT_FILE
    if checkpoint_path.exists():
        checkpoint_path.unlink()
        logger.debug("Checkpoint cleared")


def complete_checkpoint(project_dir: Path, total_cost: float = 0.0) -> None:
    """Mark checkpoint as completed."""
    checkpoint_path = project_dir / CHECKPOINT_FILE
    if not checkpoint_path.exists():
        return

    try:
        data = json.loads(checkpoint_path.read_text())
        data["status"] = "completed"
        data["total_cost"] = round(total_cost, 2)
        data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        checkpoint_path.write_text(json.dumps(data, indent=2))
    except (json.JSONDecodeError, OSError):
        pass


def _read_started_at(checkpoint_path: Path) -> str:
    """Preserve original started_at from existing checkpoint."""
    if checkpoint_path.exists():
        try:
            data = json.loads(checkpoint_path.read_text())
            return data.get("started_at", time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        except (json.JSONDecodeError, OSError):
            pass
    return time.strftime("%Y-%m-%dT%H:%M:%SZ")
