"""Checkpoint/resume for long-running otto operations.

Writes checkpoint after each round so runs can survive crashes,
pause/resume, and recover from errors. Works for both agent mode
(stores session_id for SDK resume) and split mode (stores round number).

Layout:
  otto_logs/sessions/<session_id>/checkpoint.json  — new location
  otto_logs/checkpoint.json                        — legacy (read-only fallback)

`run_id` is the same thing as `session_id` in the new layout (unified session
id: `<yyyy-mm-dd>-<HHMMSS>-<6hex>`). The CLI entrypoint allocates it once and
threads it through.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from otto import paths

logger = logging.getLogger("otto.checkpoint")

# Legacy top-level checkpoint path. Still READ (for upgrade safety). Never
# written by new code.
LEGACY_CHECKPOINT_FILE = "otto_logs/checkpoint.json"

POST_INITIAL_BUILD_PHASES = frozenset({"certify", "fix", "round_complete", "complete"})
# Phases that belong to the new spec-gate pre-build flow. Treated specially
# for --force / --resume semantics: clearing or overwriting them without
# explicit user intent loses potentially user-edited spec content.
SPEC_PHASES = frozenset({"spec", "spec_review", "spec_approved"})


def is_spec_phase(phase: str) -> bool:
    return phase in SPEC_PHASES


def spec_phase_completed(phase: str) -> bool:
    """True if the spec phase has been approved (or we're past it)."""
    return phase == "spec_approved" or phase in POST_INITIAL_BUILD_PHASES or phase == "build"


@dataclass
class ResumeState:
    """Resume state extracted from a checkpoint.

    ``resumed=False`` means a fresh run (either no checkpoint, or user chose
    not to resume). ``resumed=True`` means pipeline functions should pick up
    from ``start_round``/``total_cost``/``rounds`` (split mode) or
    ``session_id`` (agent mode).
    """

    session_id: str = ""
    start_round: int = 1
    total_cost: float = 0.0
    rounds: list[dict[str, Any]] = field(default_factory=list)
    resumed: bool = False
    prior_command: str = ""       # command that wrote the checkpoint (e.g. "build")
    command_mismatch: bool = False  # True if prior_command differs from expected
    current_round: int = 0        # last completed round from the checkpoint
    phase: str = ""               # current phase for split-mode resume
    target: str = ""              # persisted target goal for improve.target resume
    # Spec-gate fields (new; all empty/None for pre-spec checkpoints)
    intent: str = ""              # canonical intent (for resume without CLI intent)
    run_id: str = ""              # session_id in the new layout (kept as run_id for compat)
    spec_path: str = ""           # absolute path to approved/in-review spec.md
    spec_hash: str = ""           # sha256 of normalized spec content
    spec_version: int = 0         # regeneration counter (0 = never regenerated)
    spec_cost: float = 0.0        # cost of spec phase (subset of total_cost)


def print_resume_status(console: Any, state: ResumeState, resume_flag: bool, expected_command: str) -> None:
    """Print the banner describing how resume resolved.

    Shared between ``otto build`` and ``otto improve`` — the same three cases
    need the same wording, and the only reason this lives in checkpoint.py
    (rather than display.py) is that the caller always wants to print in
    lockstep with ``resolve_resume()``.
    """
    if resume_flag and not state.resumed:
        console.print("\n  [yellow]No checkpoint found — starting fresh.[/yellow]\n")
        return
    if not state.resumed:
        return
    if state.command_mismatch:
        console.print(
            f"\n  [yellow]\u26a0 Checkpoint is from `{state.prior_command}`, "
            f"resuming in `{expected_command}`.[/yellow]"
        )
    console.print(
        f"\n  [info]Resuming from round {state.start_round} "
        f"(${state.total_cost:.2f} spent so far)[/info]\n"
    )


def resolve_resume(
    project_dir: Path,
    resume: bool,
    expected_command: str,
) -> ResumeState:
    """Inspect the checkpoint and return resume state for the pipeline.

    - No checkpoint: returns fresh state.
    - Checkpoint exists but user did NOT pass --resume: clears the stale
      checkpoint and returns fresh state. Logs a warning.
    - Checkpoint exists and user passed --resume: extracts state. If the
      checkpoint's command differs from ``expected_command``, the mismatch is
      recorded on the state so the caller can warn.
    - --resume passed but no checkpoint: returns fresh state. Logs a warning.

    Does NOT print to the console — callers format output themselves.
    """
    checkpoint = load_checkpoint(project_dir)

    if not checkpoint:
        if resume:
            logger.info("No checkpoint found; starting fresh despite --resume")
        return ResumeState()

    if not resume:
        cr = checkpoint.get("current_round", 0)
        cost = checkpoint.get("total_cost", 0)
        cmd = checkpoint.get("command", "?")
        logger.info("Clearing stale checkpoint from `%s` (round %s, $%.2f)", cmd, cr, cost)
        clear_checkpoint(project_dir)
        return ResumeState()

    # resume=True and checkpoint exists
    prior_cmd = checkpoint.get("command", "") or ""
    current_round = checkpoint.get("current_round", 0) or 0
    return ResumeState(
        session_id=checkpoint.get("session_id", "") or "",
        start_round=current_round + 1,
        total_cost=float(checkpoint.get("total_cost", 0.0) or 0.0),
        rounds=list(checkpoint.get("rounds", []) or []),
        resumed=True,
        prior_command=prior_cmd,
        command_mismatch=bool(prior_cmd) and prior_cmd != expected_command,
        current_round=current_round,
        phase=checkpoint.get("phase", "") or "",
        target=checkpoint.get("target", "") or "",
        intent=checkpoint.get("intent", "") or "",
        run_id=checkpoint.get("run_id", "") or "",
        spec_path=checkpoint.get("spec_path", "") or "",
        spec_hash=checkpoint.get("spec_hash", "") or "",
        spec_version=int(checkpoint.get("spec_version", 0) or 0),
        spec_cost=float(checkpoint.get("spec_cost", 0.0) or 0.0),
    )


def initial_build_completed(phase: str) -> bool:
    """Return True only for explicit post-initial-build phases."""
    return phase in POST_INITIAL_BUILD_PHASES


def _checkpoint_path_for(project_dir: Path, run_id: str) -> Path:
    """Return the session-scoped checkpoint path for a run/session id."""
    return paths.session_checkpoint(project_dir, run_id)


def write_checkpoint(
    project_dir: Path,
    *,
    run_id: str,
    command: str,
    certifier_mode: str = "thorough",
    prompt_mode: str = "build",
    focus: str | None = None,
    target: str | None = None,
    max_rounds: int = 8,
    status: str = "in_progress",
    phase: str = "",
    session_id: str = "",
    current_round: int = 0,
    total_cost: float = 0.0,
    rounds: list[dict[str, Any]] | None = None,
    # Spec-gate fields (optional, preserved across writes via merge-with-prior)
    intent: str | None = None,
    spec_path: str | None = None,
    spec_hash: str | None = None,
    spec_version: int | None = None,
    spec_cost: float | None = None,
) -> None:
    """Write checkpoint to disk. Called after each round.

    `run_id` is the session_id in the new layout — the checkpoint lives at
    `otto_logs/sessions/<run_id>/checkpoint.json`. A "paused" pointer is
    set for `status in {paused, in_progress}` so `--resume` can find it.

    For spec-gate fields (`intent`, `spec_path`, `spec_hash`, `spec_version`,
    `spec_cost`): `None` preserves the prior checkpoint value (or default);
    explicit values overwrite. This lets a phase=build write not clobber the
    spec fields that were set at phase=spec_approved.
    """
    if not run_id:
        raise ValueError("write_checkpoint requires a non-empty run_id (session_id)")

    paths.ensure_session_scaffold(project_dir, run_id)
    checkpoint_path = _checkpoint_path_for(project_dir, run_id)

    # Merge spec fields with prior on-disk state (preserve across writes).
    prior = _read_prior(checkpoint_path)

    data = {
        "run_id": run_id,
        "command": command,
        "certifier_mode": certifier_mode,
        "prompt_mode": prompt_mode,
        "focus": focus,
        "target": target,
        "max_rounds": max_rounds,
        "status": status,
        "phase": phase,
        "session_id": session_id,
        "current_round": current_round,
        "total_cost": round(total_cost, 2),
        "rounds": rounds or [],
        "intent": intent if intent is not None else (prior.get("intent", "") if prior else ""),
        "spec_path": spec_path if spec_path is not None else (prior.get("spec_path", "") if prior else ""),
        "spec_hash": spec_hash if spec_hash is not None else (prior.get("spec_hash", "") if prior else ""),
        "spec_version": spec_version if spec_version is not None else (prior.get("spec_version", 0) if prior else 0),
        "spec_cost": round(
            float(spec_cost) if spec_cost is not None else float(prior.get("spec_cost", 0.0) if prior else 0.0),
            4,
        ),
        "started_at": _read_started_at(checkpoint_path),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    _write_checkpoint_file(checkpoint_path, data)
    # Update the `paused` pointer so --resume can locate the session.
    # We point at in_progress sessions too — a hard crash leaves status=in_progress
    # without a clean transition, and resolve_pointer preferentially returns
    # paused over in_progress when scanning.
    if status in ("in_progress", "paused"):
        paths.set_pointer(project_dir, paths.PAUSED_POINTER, run_id)
    logger.debug("Checkpoint written: round %d, status=%s, phase=%s", current_round, status, phase)


def _read_prior(checkpoint_path: Path) -> dict[str, Any] | None:
    """Read current on-disk checkpoint regardless of status. Returns None on error."""
    try:
        return json.loads(checkpoint_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _load_legacy_checkpoint(project_dir: Path) -> dict[str, Any] | None:
    """Read legacy top-level checkpoint.json (pre-restructure layout).

    Still readable so upgrades don't strand an active legacy paused run.
    Returns None on any error or when status is not active.
    """
    legacy_path = paths.legacy_checkpoint(project_dir)
    try:
        data = json.loads(legacy_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if data.get("status") not in ("in_progress", "paused"):
        return None
    return data


def load_checkpoint(project_dir: Path) -> dict[str, Any] | None:
    """Load checkpoint if one exists and is active (in_progress or paused).

    Tries in order:
      1. `paused` pointer → session_dir/checkpoint.json
      2. Scan fallback (built into resolve_pointer) over sessions/
      3. Legacy otto_logs/checkpoint.json (for upgrade safety)
    """
    # 1 & 2: new layout via pointer + scan fallback.
    session_path = paths.resolve_pointer(project_dir, paths.PAUSED_POINTER)
    if session_path is not None:
        cp_path = session_path / "checkpoint.json"
        try:
            data = json.loads(cp_path.read_text())
            if data.get("status") in ("in_progress", "paused"):
                return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    # 3: legacy fallback.
    legacy = _load_legacy_checkpoint(project_dir)
    if legacy is not None:
        return legacy

    return None


def clear_checkpoint(project_dir: Path) -> None:
    """Remove checkpoint file and the `paused` pointer.

    Also removes the legacy checkpoint file for complete upgrade-clean state.
    """
    # New layout: locate the active session checkpoint and remove it.
    session_path = paths.resolve_pointer(project_dir, paths.PAUSED_POINTER)
    if session_path is not None:
        cp_path = session_path / "checkpoint.json"
        if cp_path.exists():
            try:
                cp_path.unlink()
            except OSError as exc:
                logger.warning("Failed to unlink %s: %s", cp_path, exc)
    paths.clear_pointer(project_dir, paths.PAUSED_POINTER)

    # Legacy file, if still present (upgrade path).
    legacy_path = paths.legacy_checkpoint(project_dir)
    if legacy_path.exists():
        try:
            legacy_path.unlink()
        except OSError as exc:
            logger.warning("Failed to unlink legacy checkpoint: %s", exc)

    logger.debug("Checkpoint cleared")


def complete_checkpoint(project_dir: Path, total_cost: float = 0.0) -> None:
    """Mark checkpoint as completed (new layout) and clear the `paused` pointer.

    For legacy-layout checkpoints, updates the legacy file in place (the
    caller owns deletion).
    """
    session_path = paths.resolve_pointer(project_dir, paths.PAUSED_POINTER)
    if session_path is not None:
        cp_path = session_path / "checkpoint.json"
        if cp_path.exists():
            try:
                data = json.loads(cp_path.read_text())
                data["status"] = "completed"
                data["total_cost"] = round(total_cost, 2)
                data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
                _write_checkpoint_file(cp_path, data)
            except (json.JSONDecodeError, OSError):
                pass
        paths.clear_pointer(project_dir, paths.PAUSED_POINTER)
        return

    # Legacy fallback: update the legacy file in place.
    legacy_path = paths.legacy_checkpoint(project_dir)
    if not legacy_path.exists():
        return
    try:
        data = json.loads(legacy_path.read_text())
        data["status"] = "completed"
        data["total_cost"] = round(total_cost, 2)
        data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_checkpoint_file(legacy_path, data)
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


def _checkpoint_tmp_path(checkpoint_path: Path) -> Path:
    return checkpoint_path.with_name(checkpoint_path.name + ".tmp")


def _write_checkpoint_file(checkpoint_path: Path, data: dict[str, Any]) -> None:
    """Atomically replace the checkpoint file on disk."""
    tmp_path = _checkpoint_tmp_path(checkpoint_path)
    try:
        tmp_path.write_text(json.dumps(data, indent=2))
        os.replace(tmp_path, checkpoint_path)
    except OSError:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise
