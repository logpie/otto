"""Checkpoint/resume for long-running otto operations.

Writes checkpoint after each round so runs can survive crashes,
pause/resume, and recover from errors. Works for both agent mode
(stores agent_session_id for SDK resume) and split mode (stores round number).

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
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from otto import paths
from otto.display import rich_escape
from otto.theme import error_console
from otto.config import checkpoint_fingerprint

logger = logging.getLogger("otto.checkpoint")

POST_INITIAL_BUILD_PHASES = frozenset({"certify", "fix", "round_complete"})
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
    ``agent_session_id`` (agent mode).
    """

    agent_session_id: str = ""
    start_round: int = 1
    total_cost: float = 0.0
    total_duration: float = 0.0
    rounds: list[dict[str, Any]] = field(default_factory=list)
    resumed: bool = False
    prior_command: str = ""       # command that wrote the checkpoint (e.g. "build")
    command_mismatch: bool = False  # True if prior_command differs from expected
    fingerprint_mismatch: bool = False
    current_round: int = 0        # last completed round from the checkpoint
    phase: str = ""               # current phase for split-mode resume
    split_mode: bool | None = None  # resolved split mode; None for legacy checkpoints
    target: str = ""              # persisted target goal for improve.target resume
    session_started_at: str = ""
    child_session_ids: list[str] = field(default_factory=list)
    git_sha: str = ""
    git_status: str = ""
    prompt_hash: str = ""
    missing_paused_session_path: str = ""
    # Spec-gate fields (new; all empty/None for pre-spec checkpoints)
    intent: str = ""              # canonical intent (for resume without CLI intent)
    run_id: str = ""              # session_id in the new layout (kept as run_id for compat)
    spec_path: str = ""           # absolute path to approved/in-review spec.md
    spec_hash: str = ""           # sha256 of normalized spec content
    spec_version: int = 0         # regeneration counter (0 = never regenerated)
    spec_cost: float = 0.0        # cost of spec phase (subset of total_cost)
    completed_session_id: str = ""
    completed_verdict: str = ""
    completed_command: str = ""
    certifier_mode: str = ""
    prompt_mode: str = ""
    focus: str = ""
    max_rounds: int = 0
    last_activity: str = ""
    last_tool_name: str = ""
    last_tool_args_summary: str = ""
    last_story_id: str = ""
    last_operation_started_at: str = ""
    last_round_failures: list[str] = field(default_factory=list)
    last_diagnosis: str = ""
    dirty_files: list[str] = field(default_factory=list)

    @property
    def session_id(self) -> str:
        """Backward-compatible alias for one release."""
        return self.agent_session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        self.agent_session_id = value


def _checkpoint_agent_session_id(data: dict[str, Any]) -> str:
    """Read the SDK resume id from new or legacy checkpoint keys."""
    return data.get("agent_session_id") or data.get("session_id", "") or ""


def _checkpoint_run_id(data: dict[str, Any]) -> str:
    """Read the otto run id from new or legacy checkpoint keys."""
    return data.get("run_id") or data.get("build_id", "") or ""


def _normalize_checkpoint_data(data: dict[str, Any]) -> dict[str, Any]:
    """Fill upgrade-safe aliases on checkpoint reads."""
    normalized = dict(data)
    normalized["agent_session_id"] = _checkpoint_agent_session_id(data)
    normalized["run_id"] = _checkpoint_run_id(data)
    return normalized


def _prune_checkpoint_defaults(data: dict[str, Any]) -> dict[str, Any]:
    """Drop default-valued optional fields from serialized checkpoints."""
    if data.get("focus") is None:
        data.pop("focus", None)
    if data.get("target") is None:
        data.pop("target", None)
    if data.get("current_round", 0) == 0:
        data.pop("current_round", None)
    if not data.get("rounds"):
        data.pop("rounds", None)
    if not data.get("child_session_ids"):
        data.pop("child_session_ids", None)
    return data


def print_resume_status(console: Any, state: ResumeState, resume_flag: bool, expected_command: str) -> None:
    """Print the banner describing how resume resolved.

    Shared between ``otto build`` and ``otto improve`` — the same three cases
    need the same wording, and the only reason this lives in checkpoint.py
    (rather than display.py) is that the caller always wants to print in
    lockstep with ``resolve_resume()``.
    """
    if resume_flag and not state.resumed:
        if state.missing_paused_session_path:
            console.print(
                "\n  [yellow]Your paused session at "
                f"{state.missing_paused_session_path} was deleted; nothing to resume.[/yellow]\n"
            )
            return
        if state.completed_session_id:
            console.print(
                "\n  [yellow]Last run completed "
                f"(session {state.completed_session_id}, verdict {state.completed_verdict or '?'}). "
                f"Nothing to resume — start a new run with `{_resume_fresh_command_hint(expected_command)}`."
                "[/yellow]\n"
            )
            return
        console.print("\n  [yellow]No checkpoint found — starting fresh.[/yellow]\n")
        return
    if not state.resumed:
        return
    if state.command_mismatch:
        console.print(
            f"\n  [yellow]\u26a0 Checkpoint is from `{state.prior_command}`, "
            f"resuming in `{expected_command}`.[/yellow]"
        )
    phase = state.phase or ""
    if phase in {"spec", "spec_review"}:
        status = "Resuming spec review"
    elif phase in {"spec_approved", "build"}:
        status = "Resuming build agent (after spec approval)"
    elif phase == "certify":
        status = f"Resuming after certify round {state.current_round}"
    elif phase == "fix":
        status = f"Resuming after fix round {state.current_round}"
    else:
        status = f"Resuming from round {state.start_round} (phase={phase or 'unknown'})"
    console.print(
        f"\n  [info]{status} "
        f"(${state.total_cost:.2f} spent so far)[/info]\n"
    )
    if state.last_activity or state.last_operation_started_at:
        activity = state.last_activity or "unknown activity"
        when = state.last_operation_started_at or "unknown time"
        failures = ", ".join(state.last_round_failures) if state.last_round_failures else "none"
        console.print(
            "  [dim]resuming from: "
            f"{activity} at {when}, last round had: {failures}[/dim]"
        )
    if state.child_session_ids:
        console.print(
            "  [yellow]Prior child subagent sessions may still be orphaned; "
            "Otto will start fresh subagents on resume.[/yellow]"
        )


def _command_family(command: str) -> str:
    if command.startswith("improve."):
        return "improve"
    return command


def _resume_fresh_command_hint(expected_command: str) -> str:
    if expected_command == "build":
        return "otto build <intent>"
    if expected_command == "improve.target":
        return "otto improve target <goal>"
    if expected_command.startswith("improve."):
        return f"otto improve {expected_command.split('.', 1)[1]}"
    return f"otto {expected_command}"


def enforce_resume_command_match(
    state: ResumeState,
    expected_command: str,
    *,
    force_cross_command_resume: bool = False,
) -> None:
    """Reject cross-command resume unless the caller explicitly opts in."""
    if not state.resumed or not state.command_mismatch or force_cross_command_resume:
        return
    error_console.print(
        "[error]Checkpoint command mismatch.[/error]\n"
        f"  Checkpoint command: `{rich_escape(state.prior_command or '?')}`\n"
        f"  Requested command:  `{rich_escape(expected_command)}`\n"
        "  Resume with the original command, or pass `--force-cross-command-resume` to override."
    )
    raise SystemExit(2)


def enforce_resume_available(state: ResumeState, *, resume_flag: bool, expected_command: str) -> None:
    """Reject `--resume` when the latest matching run has already completed."""
    if not resume_flag or state.resumed or not state.completed_session_id:
        return
    verdict = state.completed_verdict or "?"
    error_console.print(
        "[error]Nothing to resume.[/error]\n"
        f"  Last run completed (session {state.completed_session_id}, verdict {rich_escape(verdict)}).\n"
        f"  Start a new run with `{_resume_fresh_command_hint(expected_command)}`."
    )
    raise SystemExit(2)


def _summary_timestamp(summary_path: Path, data: dict[str, Any]) -> float:
    completed_at = data.get("completed_at")
    if isinstance(completed_at, str) and completed_at:
        try:
            return datetime.fromisoformat(completed_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    try:
        return summary_path.stat().st_mtime
    except OSError:
        return 0.0


def _find_recent_completed_session(
    project_dir: Path,
    *,
    expected_command: str,
) -> tuple[str, str, str] | None:
    root = paths.sessions_root(project_dir)
    if not root.exists():
        return None
    family = _command_family(expected_command)
    latest: tuple[float, str, str, str] | None = None
    for summary_path in root.glob("*/summary.json"):
        try:
            data = json.loads(summary_path.read_text())
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if data.get("status") != "completed":
            continue
        command = str(data.get("command") or "")
        if _command_family(command) != family:
            continue
        session_id = str(data.get("run_id") or summary_path.parent.name or "")
        if not session_id:
            continue
        verdict = str(data.get("verdict") or "")
        candidate = (_summary_timestamp(summary_path, data), session_id, verdict, command)
        if latest is None or candidate[0] >= latest[0]:
            latest = candidate
    if latest is None:
        return None
    _, session_id, verdict, command = latest
    return session_id, verdict, command


def resolve_resume(
    project_dir: Path,
    resume: bool,
    expected_command: str,
    *,
    force: bool = False,
    reject_incompatible: bool = False,
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
        missing_paused_session_path = ""
        if resume:
            missing = _missing_paused_session_path(project_dir)
            if missing is not None:
                missing_paused_session_path = str(missing)
        if resume:
            logger.info("No checkpoint found; starting fresh despite --resume")
            completed = _find_recent_completed_session(
                project_dir,
                expected_command=expected_command,
            )
            if completed is not None:
                session_id, verdict, command = completed
                return ResumeState(
                    missing_paused_session_path=missing_paused_session_path,
                    completed_session_id=session_id,
                    completed_verdict=verdict,
                    completed_command=command,
                )
        return ResumeState(missing_paused_session_path=missing_paused_session_path)

    if not resume:
        cr = checkpoint.get("current_round", 0)
        cost = checkpoint.get("total_cost", 0)
        cmd = checkpoint.get("command", "?")
        logger.info("Clearing stale checkpoint from `%s` (round %s, $%.2f)", cmd, cr, cost)
        run_id = _checkpoint_run_id(checkpoint)
        clear_checkpoint(project_dir, run_id=run_id or None)
        return ResumeState()

    # resume=True and checkpoint exists
    prior_cmd = checkpoint.get("command", "") or ""
    current_round = checkpoint.get("current_round", 0) or 0
    current_fingerprint = checkpoint_fingerprint(project_dir)
    git_sha = checkpoint.get("git_sha", "") or ""
    git_status = checkpoint.get("git_status", "") or ""
    prompt_hash = checkpoint.get("prompt_hash", "") or ""
    fingerprint_mismatch = bool(
        (git_sha and git_sha != current_fingerprint.get("git_sha", ""))
        or (git_status != current_fingerprint.get("git_status", ""))
        or (prompt_hash and prompt_hash != current_fingerprint.get("prompt_hash", ""))
    )
    command_mismatch = bool(prior_cmd) and prior_cmd != expected_command
    if reject_incompatible and not force:
        if command_mismatch:
            display_expected = expected_command.replace(".", " ")
            raise ValueError(
                f"Checkpoint is not from `{display_expected}`. "
                f"(found `{prior_cmd}` instead). "
                "Pass `--force --resume` to override."
            )
        if fingerprint_mismatch:
            raise ValueError(
                "Checkpoint fingerprint does not match the current code/prompt/worktree state. "
                "The paused run's git status differs from the current tree. "
                "Pass `--force --resume` to override."
            )
    if fingerprint_mismatch and force:
        logger.warning(
            "Resuming despite fingerprint mismatch (checkpoint git_sha=%s git_status=%r prompt_hash=%s, current git_sha=%s git_status=%r prompt_hash=%s)",
            git_sha,
            git_status,
            prompt_hash,
            current_fingerprint.get("git_sha", ""),
            current_fingerprint.get("git_status", ""),
            current_fingerprint.get("prompt_hash", ""),
        )
    return ResumeState(
        agent_session_id=_checkpoint_agent_session_id(checkpoint),
        start_round=current_round + 1,
        total_cost=float(checkpoint.get("total_cost", 0.0) or 0.0),
        total_duration=float(
            checkpoint.get("total_duration_so_far", checkpoint.get("total_duration", 0.0)) or 0.0
        ),
        rounds=list(checkpoint.get("rounds", []) or []),
        resumed=True,
        prior_command=prior_cmd,
        command_mismatch=command_mismatch,
        fingerprint_mismatch=fingerprint_mismatch,
        current_round=current_round,
        phase=checkpoint.get("phase", "") or "",
        split_mode=(bool(checkpoint["split_mode"]) if "split_mode" in checkpoint else None),
        target=checkpoint.get("target", "") or "",
        session_started_at=checkpoint.get("started_at", "") or "",
        child_session_ids=list(checkpoint.get("child_session_ids", []) or []),
        git_sha=git_sha,
        git_status=git_status,
        prompt_hash=prompt_hash,
        intent=checkpoint.get("intent", "") or "",
        run_id=_checkpoint_run_id(checkpoint),
        spec_path=checkpoint.get("spec_path", "") or "",
        spec_hash=checkpoint.get("spec_hash", "") or "",
        spec_version=int(checkpoint.get("spec_version", 0) or 0),
        spec_cost=float(checkpoint.get("spec_cost", 0.0) or 0.0),
        certifier_mode=checkpoint.get("certifier_mode", "") or "",
        prompt_mode=checkpoint.get("prompt_mode", "") or "",
        focus=checkpoint.get("focus", "") or "",
        max_rounds=int(checkpoint.get("max_rounds", 0) or 0),
        last_activity=checkpoint.get("last_activity", "") or "",
        last_tool_name=checkpoint.get("last_tool_name", "") or "",
        last_tool_args_summary=checkpoint.get("last_tool_args_summary", "") or "",
        last_story_id=checkpoint.get("last_story_id", "") or "",
        last_operation_started_at=checkpoint.get("last_operation_started_at", "") or "",
        last_round_failures=list(checkpoint.get("last_round_failures", []) or []),
        last_diagnosis=checkpoint.get("last_diagnosis", "") or "",
        dirty_files=list(checkpoint.get("dirty_files", []) or []),
    )


def _missing_paused_session_path(project_dir: Path) -> Path | None:
    """Return the paused session path when the pointer exists but the session is gone."""
    pointer_target = paths.resolve_pointer(project_dir, paths.PAUSED_POINTER)
    if pointer_target is not None:
        return None

    logs_dir = paths.logs_dir(project_dir)
    symlink_path = logs_dir / paths.PAUSED_POINTER
    if symlink_path.is_symlink():
        try:
            target = symlink_path.resolve(strict=False)
        except OSError:
            return None
        if not target.exists():
            return target

    pointer_file = logs_dir / f"{paths.PAUSED_POINTER}.txt"
    if pointer_file.exists():
        try:
            session_id = pointer_file.read_text().strip()
        except OSError:
            return None
        if session_id:
            target = paths.session_dir(project_dir, session_id)
            if not target.exists():
                return target
    return None


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
    split_mode: bool | None = None,
    session_id: str = "",
    current_round: int = 0,
    total_cost: float = 0.0,
    total_duration: float = 0.0,
    rounds: list[dict[str, Any]] | None = None,
    child_session_ids: list[str] | None = None,
    # Spec-gate fields (optional, preserved across writes via merge-with-prior)
    intent: str | None = None,
    spec_path: str | None = None,
    spec_hash: str | None = None,
    spec_version: int | None = None,
    spec_cost: float | None = None,
    last_activity: str | None = None,
    last_tool_name: str | None = None,
    last_tool_args_summary: str | None = None,
    last_story_id: str | None = None,
    last_operation_started_at: str | None = None,
    last_round_failures: list[str] | None = None,
    last_diagnosis: str | None = None,
    dirty_files: list[str] | None = None,
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
    fingerprint = checkpoint_fingerprint(project_dir)
    split_mode_value = (
        bool(split_mode)
        if split_mode is not None
        else (bool(prior.get("split_mode")) if prior and "split_mode" in prior else None)
    )

    # Cost fields stored at full precision — rounding only at display time.
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
        "agent_session_id": session_id,
        "current_round": current_round,
        "total_cost": float(total_cost),
        "total_cost_so_far": float(total_cost),
        "total_duration": float(total_duration),
        "total_duration_so_far": float(total_duration),
        "rounds": rounds or [],
        "child_session_ids": sorted(
            set(child_session_ids or (prior.get("child_session_ids", []) if prior else []) or [])
        ),
        "git_sha": fingerprint.get("git_sha", ""),
        "git_status": fingerprint.get("git_status", ""),
        "prompt_hash": fingerprint.get("prompt_hash", ""),
        "intent": intent if intent is not None else (prior.get("intent", "") if prior else ""),
        "spec_path": spec_path if spec_path is not None else (prior.get("spec_path", "") if prior else ""),
        "spec_hash": spec_hash if spec_hash is not None else (prior.get("spec_hash", "") if prior else ""),
        "spec_version": spec_version if spec_version is not None else (prior.get("spec_version", 0) if prior else 0),
        "spec_cost": float(
            spec_cost if spec_cost is not None
            else (prior.get("spec_cost", 0.0) if prior else 0.0)
        ),
        "last_activity": last_activity if last_activity is not None else (prior.get("last_activity", "") if prior else ""),
        "last_tool_name": last_tool_name if last_tool_name is not None else (prior.get("last_tool_name", "") if prior else ""),
        "last_tool_args_summary": (
            last_tool_args_summary if last_tool_args_summary is not None
            else (prior.get("last_tool_args_summary", "") if prior else "")
        ),
        "last_story_id": last_story_id if last_story_id is not None else (prior.get("last_story_id", "") if prior else ""),
        "last_operation_started_at": (
            last_operation_started_at if last_operation_started_at is not None
            else (prior.get("last_operation_started_at", "") if prior else "")
        ),
        "last_round_failures": (
            list(last_round_failures)
            if last_round_failures is not None
            else list(prior.get("last_round_failures", []) if prior else [])
        ),
        "last_diagnosis": last_diagnosis if last_diagnosis is not None else (prior.get("last_diagnosis", "") if prior else ""),
        "dirty_files": (
            list(dirty_files)
            if dirty_files is not None
            else list(prior.get("dirty_files", []) if prior else [])
        ),
        "started_at": _read_started_at(checkpoint_path),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if split_mode_value is not None:
        data["split_mode"] = split_mode_value
    if status == "completed":
        data = _prune_checkpoint_defaults(data)

    _write_checkpoint_file(checkpoint_path, data)
    # Update the `paused` pointer so --resume can locate the session.
    # We point at in_progress sessions too — a hard crash leaves status=in_progress
    # without a clean transition, and resolve_pointer preferentially returns
    # paused over in_progress when scanning.
    if status in ("in_progress", "paused"):
        paths.set_pointer(project_dir, paths.PAUSED_POINTER, run_id, strict=True)
    elif status == "completed":
        paths.clear_pointer(project_dir, paths.PAUSED_POINTER)
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


def _scan_active_session_checkpoint(project_dir: Path) -> dict[str, Any] | None:
    candidates: list[tuple[str, float, str, dict[str, Any]]] = []
    for cp_path in paths.sessions_root(project_dir).glob("*/checkpoint.json"):
        try:
            data = json.loads(cp_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        if data.get("status") not in ("in_progress", "paused"):
            continue
        try:
            mtime = cp_path.stat().st_mtime
        except OSError:
            mtime = 0.0
        candidates.append((str(data.get("updated_at") or ""), mtime, str(cp_path), data))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][3]


def load_checkpoint(project_dir: Path, run_id: str | None = None) -> dict[str, Any] | None:
    """Load checkpoint if one exists and is active (in_progress or paused).

    Tries in order:
      1. explicit run_id (if provided)
      2. `paused` pointer → session_dir/checkpoint.json
      3. active session checkpoint scan (crash between checkpoint and pointer write)
      4. Legacy otto_logs/checkpoint.json (for upgrade safety)
    """
    if run_id:
        cp_path = _checkpoint_path_for(project_dir, run_id)
        try:
            data = json.loads(cp_path.read_text())
            if data.get("status") in ("in_progress", "paused", "completed"):
                return _normalize_checkpoint_data(data)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    # 1 & 2: new layout via pointer.
    session_path = paths.resolve_pointer(project_dir, paths.PAUSED_POINTER)
    if session_path is not None:
        cp_path = session_path / "checkpoint.json"
        try:
            data = json.loads(cp_path.read_text())
            if data.get("status") in ("in_progress", "paused"):
                return _normalize_checkpoint_data(data)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    scanned = _scan_active_session_checkpoint(project_dir)
    if scanned is not None:
        return _normalize_checkpoint_data(scanned)

    # 4: legacy fallback.
    legacy = _load_legacy_checkpoint(project_dir)
    if legacy is not None:
        return _normalize_checkpoint_data(legacy)

    return None


def clear_checkpoint(project_dir: Path, run_id: str | None = None) -> None:
    """Remove checkpoint file and the `paused` pointer.

    Also removes the legacy checkpoint file for complete upgrade-clean state.
    """
    cp_path: Path | None = None
    if run_id:
        cp_path = _checkpoint_path_for(project_dir, run_id)
    else:
        session_path = paths.resolve_pointer(project_dir, paths.PAUSED_POINTER)
        if session_path is not None:
            cp_path = session_path / "checkpoint.json"

    if cp_path is not None and cp_path.exists():
        try:
            cp_path.unlink()
        except OSError as exc:
            logger.warning("Failed to unlink %s: %s", cp_path, exc)

    paused_target = paths.resolve_pointer(project_dir, paths.PAUSED_POINTER)
    if paused_target is not None and (run_id is None or paused_target.name == run_id):
        paths.clear_pointer(project_dir, paths.PAUSED_POINTER)

    # Legacy file, if still present (upgrade path).
    legacy_path = paths.legacy_checkpoint(project_dir)
    if run_id is None and legacy_path.exists():
        try:
            legacy_path.unlink()
        except OSError as exc:
            logger.warning("Failed to unlink legacy checkpoint: %s", exc)

    logger.debug("Checkpoint cleared")


def complete_checkpoint(
    project_dir: Path,
    total_cost: float = 0.0,
    *,
    run_id: str | None = None,
    total_duration: float | None = None,
    current_round: int | None = None,
    rounds: list[dict[str, Any]] | None = None,
) -> None:
    """Mark checkpoint as completed (new layout) and clear the `paused` pointer.

    ``current_round`` / ``rounds``, when provided, populate the matching
    fields on the completed checkpoint so forensic reads reflect real
    history. Previously agentic_v3 runs left these as ``0`` / ``[]`` even
    after multiple certify rounds. ``None`` preserves whatever was on disk.

    For legacy-layout checkpoints, updates the legacy file in place (the
    caller owns deletion).
    """
    def _apply(data: dict[str, Any]) -> None:
        data.update(_normalize_checkpoint_data(data))
        data["status"] = "completed"
        data["total_cost"] = float(total_cost)
        data["total_cost_so_far"] = float(total_cost)
        if total_duration is not None:
            data["total_duration"] = float(total_duration)
            data["total_duration_so_far"] = float(total_duration)
        if current_round is not None:
            data["current_round"] = current_round
        if rounds is not None:
            data["rounds"] = list(rounds)
        data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _prune_checkpoint_defaults(data)

    cp_path: Path | None = None
    if run_id:
        cp_path = _checkpoint_path_for(project_dir, run_id)
    else:
        session_path = paths.resolve_pointer(project_dir, paths.PAUSED_POINTER)
        if session_path is not None:
            cp_path = session_path / "checkpoint.json"

    if cp_path is not None:
        if cp_path.exists():
            try:
                data = _normalize_checkpoint_data(json.loads(cp_path.read_text()))
                _apply(data)
                _write_checkpoint_file(cp_path, data)
            except (json.JSONDecodeError, OSError):
                pass
        paused_target = paths.resolve_pointer(project_dir, paths.PAUSED_POINTER)
        if paused_target is not None and paused_target.name == cp_path.parent.name:
            paths.clear_pointer(project_dir, paths.PAUSED_POINTER)
        return

    # Legacy fallback: update the legacy file in place.
    legacy_path = paths.legacy_checkpoint(project_dir)
    if not legacy_path.exists():
        return
    try:
        data = _normalize_checkpoint_data(json.loads(legacy_path.read_text()))
        _apply(data)
        _write_checkpoint_file(legacy_path, data)
    except (json.JSONDecodeError, OSError):
        pass


def write_cancel_checkpoint_marker(
    project_dir: Path,
    *,
    run_id: str,
    command: str | None = None,
    note: str = "cancelled by command",
) -> dict[str, Any]:
    """Durably persist an in-flight cancel marker for replay-safe command acks."""
    if not run_id:
        raise ValueError("write_cancel_checkpoint_marker requires a non-empty run_id")

    paths.ensure_session_scaffold(project_dir, run_id)
    checkpoint_path = _checkpoint_path_for(project_dir, run_id)
    prior = _read_prior(checkpoint_path) or {}
    data = _normalize_checkpoint_data(prior)

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not data.get("run_id"):
        data["run_id"] = run_id
    if not data.get("command"):
        data["command"] = command or "build"
    if not data.get("status"):
        data["status"] = "in_progress"
    data["status"] = "paused"
    data["phase"] = "cancel_requested"
    data["updated_at"] = now_iso
    data["cancel_requested"] = True
    data["cancel_requested_at"] = now_iso
    data["cancel_note"] = note

    _write_checkpoint_file(checkpoint_path, data)
    paths.set_pointer(project_dir, paths.PAUSED_POINTER, run_id, strict=True)
    return data


def _read_started_at(checkpoint_path: Path) -> str:
    """Preserve original started_at from existing checkpoint."""
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if checkpoint_path.exists():
        try:
            data = json.loads(checkpoint_path.read_text())
            return data.get("started_at", now_iso)
        except (json.JSONDecodeError, OSError):
            pass
    return now_iso

def _write_checkpoint_file(checkpoint_path: Path, data: dict[str, Any]) -> None:
    """Atomically replace the checkpoint file on disk."""
    from otto.observability import write_json_atomic

    write_json_atomic(checkpoint_path, data)
