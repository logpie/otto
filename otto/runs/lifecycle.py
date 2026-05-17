"""Run-lifecycle helpers shared across all pipelines.

Originally lived in `otto/pipeline.py`. Phase C.3 deleted the legacy v3
pipeline; these helpers stayed because they are used by:

- `otto/agent.py` (process-group cleanup on agent crash/timeout)
- `otto/cli.py` (runtime metadata for the legacy spec phase)
- `otto/certifier/__init__.py` (atomic publisher / heartbeat / summary
  writers — being deleted in Phase C.2; the lifecycle helpers outlive it)

Keeping them in a dedicated module under `otto/runs/` clarifies their
role and lets `otto/pipeline.py` go away.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from otto.logstream import normalize_phase_breakdown
from otto.token_usage import (
    TOKEN_USAGE_KEYS,
    phase_breakdown_from_messages,
    total_token_usage_from_phases,
)


logger = logging.getLogger("otto.runs.lifecycle")


def _merge_phase_token_usage(
    breakdown: dict[str, dict[str, Any]],
    phase_usage: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    merged = {
        str(phase): dict(data)
        for phase, data in breakdown.items()
        if isinstance(data, dict)
    }
    for phase, usage in phase_usage.items():
        entry = merged.setdefault(str(phase), {})
        for key in TOKEN_USAGE_KEYS:
            entry.pop(key, None)
        for key in TOKEN_USAGE_KEYS:
            value = usage.get(key)
            if value:
                entry[key] = int(value)
        for key in ("duration_s", "cost_usd"):
            value = usage.get(key)
            if isinstance(value, int | float) and value > 0:
                entry[key] = float(value)
    return merged


def _write_session_summary(
    project_dir: Path,
    session_id: str,
    *,
    verdict: str,
    passed: bool,
    cost: float,
    duration: float,
    stories_passed: int,
    stories_tested: int,
    rounds: int,
    status: str = "completed",
    intent: str = "",
    command: str = "build",
    breakdown: dict[str, dict[str, Any]] | None = None,
    runtime_path: str = "",
    agent_routing: dict[str, dict[str, str | None]] | None = None,
) -> None:
    """Write the canonical summary artifact for a completed session.

    Includes `intent` and `command` so a single read of summary.json
    answers "what was this session about?" — no crossref to
    project-root intent.md required.
    """
    from otto import paths
    from otto.observability import write_json_file

    # Full precision preserved in JSON; round at display time only.
    summary: dict[str, Any] = {
        "run_id": session_id,
        "command": command,
        "intent": intent,
        "verdict": verdict,
        "passed": passed,
        "cost_usd": float(cost),
        "duration_s": duration,
        "stories_passed": stories_passed,
        "stories_tested": stories_tested,
        "status": status,
        "rounds": rounds,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    queue_task_id = os.environ.get("OTTO_QUEUE_TASK_ID")
    if queue_task_id:
        summary["queue_task_id"] = queue_task_id
    branch = _current_branch_name(project_dir)
    if branch:
        summary["branch"] = branch
    head_sha = _current_head_sha(project_dir)
    if head_sha:
        summary["head_sha"] = head_sha
    if runtime_path:
        summary["runtime_path"] = runtime_path
    if agent_routing:
        summary["agent_routing"] = agent_routing
        global_routing = agent_routing.get("global") or {}
        if global_routing.get("provider"):
            summary["provider"] = global_routing["provider"]
        if global_routing.get("model"):
            summary["model"] = global_routing["model"]
    message_phase_usage = phase_breakdown_from_messages(paths.session_dir(project_dir, session_id))
    if breakdown is not None or message_phase_usage:
        if breakdown is None:
            breakdown = {}
        if message_phase_usage:
            breakdown = _merge_phase_token_usage(breakdown, message_phase_usage)
            total_token_usage = total_token_usage_from_phases(message_phase_usage)
            if total_token_usage:
                summary["token_usage"] = total_token_usage
                for key in TOKEN_USAGE_KEYS:
                    value = total_token_usage.get(key)
                    if value:
                        summary[key] = int(value)
        primary_phase = "build"
        if "build" not in breakdown:
            if "spec" in breakdown:
                primary_phase = "spec"
            elif "certify" in breakdown:
                primary_phase = "certify"
        normalized_breakdown = normalize_phase_breakdown(
            float(duration),
            breakdown,
            primary_phase=primary_phase,
        )
        if normalized_breakdown is not None:
            summary["breakdown"] = normalized_breakdown
    summary_path = paths.session_summary(project_dir, session_id)
    try:
        write_json_file(summary_path, summary, strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Failed to write session summary {summary_path}: {exc}") from exc


def _runtime_metadata(project_dir: Path) -> dict[str, Any]:
    from otto import __version__
    from otto.observability import gather_runtime_metadata

    runtime = gather_runtime_metadata(project_dir)
    runtime["otto_version"] = __version__
    if not runtime.get("otto_commit"):
        runtime["otto_commit"] = runtime.get("git_commit", "")
    return runtime


def _intent_provenance_payload(intent: str, config: dict[str, Any]) -> dict[str, Any]:
    from otto.observability import sha256_text

    return {
        "source": str(config.get("_intent_source") or "cli-argument"),
        "fallback_reason": str(config.get("_intent_fallback_reason") or ""),
        "resolved_text": intent,
        "sha256": sha256_text(intent),
    }


def _spec_provenance_payload(spec: str | None, config: dict[str, Any]) -> dict[str, Any]:
    from otto.observability import sha256_text

    if not spec:
        return {"source": "none", "path": "", "sha256": ""}
    return {
        "source": str(config.get("_spec_source") or "spec-agent"),
        "path": str(config.get("_spec_path") or ""),
        "sha256": sha256_text(spec),
    }


def _record_prompt_provenance(
    project_dir: Path,
    session_id: str,
    *,
    template: str,
    rendered_text: str,
    intent: str,
    spec: str | None,
    config: dict[str, Any],
) -> None:
    from otto import paths
    from otto.observability import save_rendered_prompt, update_input_provenance

    session_dir = paths.session_dir(project_dir, session_id)
    prompt_entry = save_rendered_prompt(
        session_dir / "prompts",
        template=template,
        rendered_text=rendered_text,
    )
    update_input_provenance(
        session_dir,
        intent=_intent_provenance_payload(intent, config),
        spec=_spec_provenance_payload(spec, config),
        prompts=[prompt_entry],
    )


def _write_runtime_artifact(project_dir: Path, session_id: str) -> str:
    from otto import paths
    from otto.observability import write_runtime_metadata

    runtime_path = write_runtime_metadata(
        paths.session_dir(project_dir, session_id),
        _runtime_metadata(project_dir),
    )
    return str(runtime_path)


def _current_branch_name(project_dir: Path) -> str | None:
    from otto.merge.git_ops import try_current_branch

    return try_current_branch(project_dir)


def _current_head_sha(project_dir: Path) -> str | None:
    from otto.merge.git_ops import try_head_sha

    return try_head_sha(project_dir)


def _history_story_counts(stories: list[dict[str, Any]]) -> tuple[int, int, int]:
    passed_count = sum(1 for story in stories if story.get("passed") and not story.get("warn"))
    warn_count = sum(1 for story in stories if story.get("warn"))
    failed_count = sum(1 for story in stories if not story.get("passed") and not story.get("warn"))
    return passed_count, failed_count, warn_count


def _append_session_history(
    project_dir: Path,
    *,
    run_id: str,
    command: str,
    certifier_mode: str,
    intent: str,
    stories: list[dict[str, Any]],
    passed: bool,
    duration_s: float,
    total_cost_usd: float,
    certifier_cost_usd: float,
    rounds: int,
    started_at: str | None = None,
    finished_at: str | None = None,
    branch: str | None = None,
    worktree: str | None = None,
    spec_path: str | None = None,
) -> None:
    from otto import paths
    from otto.history import command_family, normalize_command_label
    from otto.runs.history import append_history_snapshot, build_terminal_snapshot

    passed_count, failed_count, warn_count = _history_story_counts(stories)
    run_type = command_family(command)
    primary_phase = "certify" if run_type == "certify" else "improve" if run_type == "improve" else "build"
    session_dir = paths.session_dir(project_dir, run_id)
    normalized_command = normalize_command_label(command)
    resolved_spec_path = str(spec_path or "").strip() or None
    artifacts = {
        "session_dir": str(session_dir),
        "manifest_path": str(session_dir / "manifest.json"),
        "checkpoint_path": None if run_type == "certify" else str(paths.session_checkpoint(project_dir, run_id)),
        "summary_path": str(paths.session_summary(project_dir, run_id)),
        "primary_log_path": str(session_dir / primary_phase / "narrative.log"),
        "extra_log_paths": [],
    }
    append_history_snapshot(
        project_dir,
        build_terminal_snapshot(
            run_id=run_id,
            domain="atomic",
            run_type=run_type,
            command=normalized_command,
            intent_meta={
                "summary": intent[:200],
                "intent_path": str(paths.session_intent(project_dir, run_id)),
                "spec_path": resolved_spec_path,
            },
            status="done" if passed else "failed",
            terminal_outcome="success" if passed else "failure",
            timing={
                "started_at": started_at,
                "finished_at": finished_at,
                "timestamp": finished_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "duration_s": duration_s,
            },
            metrics={"cost_usd": float(total_cost_usd)},
            git={"branch": branch or _current_branch_name(project_dir), "worktree": worktree},
            source={"resumable": run_type != "certify"},
            artifacts=artifacts,
            extra_fields={
                "passed": passed,
                "certifier_mode": certifier_mode,
                "mode": certifier_mode,
                "stories_passed": passed_count + warn_count,
                "stories_tested": len(stories),
                "passed_count": passed_count,
                "failed_count": failed_count,
                "warn_count": warn_count,
                "certify_rounds": rounds,
                "certifier_cost_usd": float(certifier_cost_usd),
            },
        ),
        strict=True,
    )


def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("ignoring unreadable lifecycle JSON %s: %s", path, exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def _append_cancelled_atomic_history(
    project_dir: Path,
    *,
    run_id: str,
    command: str,
    certifier_mode: str,
    intent: str,
    stories: list[dict[str, Any]] | None,
    duration_s: float,
    total_cost_usd: float,
    rounds: int,
    started_at: str | None = None,
    finished_at: str | None = None,
    branch: str | None = None,
    worktree: str | None = None,
    spec_path: str | None = None,
) -> None:
    from otto import paths
    from otto.history import command_family, normalize_command_label
    from otto.runs.history import append_history_snapshot, build_terminal_snapshot, read_history_rows

    dedupe_key = f"terminal_snapshot:{run_id}"
    if any(str(row.get("dedupe_key") or "") == dedupe_key for row in read_history_rows(paths.history_jsonl(project_dir))):
        return

    story_rows = list(stories or [])
    passed_count, failed_count, warn_count = _history_story_counts(story_rows)
    run_type = command_family(command)
    primary_phase = "certify" if run_type == "certify" else "improve" if run_type == "improve" else "build"
    session_dir = paths.session_dir(project_dir, run_id)
    normalized_command = normalize_command_label(command)
    resolved_spec_path = str(spec_path or "").strip() or None
    artifacts = {
        "session_dir": str(session_dir),
        "manifest_path": str(session_dir / "manifest.json"),
        "checkpoint_path": None if run_type == "certify" else str(paths.session_checkpoint(project_dir, run_id)),
        "summary_path": str(paths.session_summary(project_dir, run_id)),
        "primary_log_path": str(session_dir / primary_phase / "narrative.log"),
        "extra_log_paths": [],
    }
    append_history_snapshot(
        project_dir,
        build_terminal_snapshot(
            run_id=run_id,
            domain="atomic",
            run_type=run_type,
            command=normalized_command,
            intent_meta={
                "summary": intent[:200],
                "intent_path": str(paths.session_intent(project_dir, run_id)),
                "spec_path": resolved_spec_path,
            },
            status="cancelled",
            terminal_outcome="cancelled",
            timing={
                "started_at": started_at,
                "finished_at": finished_at,
                "timestamp": finished_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "duration_s": duration_s,
            },
            metrics={"cost_usd": float(total_cost_usd)},
            git={"branch": branch or _current_branch_name(project_dir), "worktree": worktree},
            source={"resumable": run_type != "certify"},
            artifacts=artifacts,
            extra_fields={
                "passed": False,
                "certifier_mode": certifier_mode,
                "mode": certifier_mode,
                "stories_passed": passed_count + warn_count,
                "stories_tested": len(story_rows),
                "passed_count": passed_count,
                "failed_count": failed_count,
                "warn_count": warn_count,
                "certify_rounds": rounds,
                "certifier_cost_usd": 0.0 if run_type == "certify" else None,
            },
        ),
        strict=True,
    )


def _persist_atomic_cancelled_terminal_state(
    project_dir: Path,
    *,
    run_id: str,
    command: str,
    certifier_mode: str,
    intent: str,
    stories: list[dict[str, Any]] | None = None,
    duration_s: float,
    total_cost_usd: float,
    rounds: int,
    breakdown: dict[str, dict[str, Any]] | None = None,
    runtime_path: str = "",
    spec_path: str | None = None,
    final_record: Any = None,
) -> None:
    from otto import paths

    _write_session_summary(
        project_dir,
        run_id,
        verdict="cancelled",
        passed=False,
        cost=total_cost_usd,
        duration=duration_s,
        stories_passed=0,
        stories_tested=len(stories or []),
        rounds=rounds,
        status="cancelled",
        intent=intent,
        command=command,
        breakdown=breakdown or None,
        runtime_path=runtime_path,
    )
    from otto.queue.runtime import is_queue_runner_child

    if is_queue_runner_child():
        return

    summary = _read_json_dict(paths.session_summary(project_dir, run_id))
    checkpoint = _read_json_dict(paths.session_checkpoint(project_dir, run_id))
    finished_at = str(summary.get("completed_at") or checkpoint.get("updated_at") or "").strip() or None
    started_at = None
    branch = None
    worktree = None
    if final_record is not None:
        started_at = final_record.timing.get("started_at")
        branch = final_record.git.get("branch")
        worktree = final_record.git.get("worktree")
    _append_cancelled_atomic_history(
        project_dir,
        run_id=run_id,
        command=command,
        certifier_mode=certifier_mode,
        intent=intent,
        stories=stories,
        duration_s=duration_s,
        total_cost_usd=total_cost_usd,
        rounds=rounds,
        started_at=started_at or str(checkpoint.get("started_at") or "").strip() or None,
        finished_at=finished_at,
        branch=branch or str(summary.get("branch") or "").strip() or None,
        worktree=worktree,
        spec_path=spec_path,
    )


def _repair_atomic_history(project_dir: Path) -> None:
    from otto.runs.atomic_repair import repair_atomic_history

    repair_atomic_history(project_dir)


def _atomic_artifacts(project_dir: Path, run_id: str, *, primary_phase: str) -> dict[str, Any]:
    from otto import paths

    session_dir = paths.session_dir(project_dir, run_id)
    primary_log = session_dir / primary_phase / "narrative.log"
    return {
        "session_dir": str(session_dir),
        "manifest_path": str(session_dir / "manifest.json"),
        "checkpoint_path": str(paths.session_checkpoint(project_dir, run_id)),
        "summary_path": str(paths.session_summary(project_dir, run_id)),
        "primary_log_path": str(primary_log),
        "extra_log_paths": [],
    }


def _atomic_registry_enabled() -> bool:
    from otto.queue.runtime import is_queue_runner_child

    return not is_queue_runner_child()


def _atomic_publisher(
    *,
    project_dir: Path,
    run_id: str,
    command: str,
    intent: str,
    primary_phase: str,
    spec_path: str | None = None,
    cwd: Path | None = None,
) -> Any:
    if not _atomic_registry_enabled():
        return None

    from otto import paths
    from otto.runs.registry import publisher_for

    command_label = command.replace(".", " ")
    return publisher_for(
        "atomic",
        command_label.split(" ", 1)[0] or "build",
        command,
        project_dir=project_dir,
        run_id=run_id,
        intent=intent,
        display_name=f"{command_label}: {intent[:80]}".strip(),
        cwd=cwd or project_dir,
        git={
            "branch": _current_branch_name(project_dir),
            "worktree": None,
            "target_branch": None,
            "head_sha": _current_head_sha(project_dir),
        },
        intent_meta={
            "intent_path": str(paths.session_intent(project_dir, run_id)),
            "spec_path": str(spec_path or "").strip() or None,
        },
        artifacts=_atomic_artifacts(project_dir, run_id, primary_phase=primary_phase),
        adapter_key=f"atomic.{command_label.split(' ', 1)[0] or 'build'}",
    )


def _finalize_atomic_publisher(
    publisher: Any,
    *,
    passed: bool,
    total_cost: float,
    duration_s: float,
    stories_passed: int,
    stories_tested: int,
    last_event: str,
    breakdown: dict[str, dict[str, Any]] | None = None,
) -> Any:
    if publisher is None:
        return None
    return publisher.finalize(
        status="done" if passed else "failed",
        terminal_outcome="success" if passed else "failure",
        updates={
            "metrics": {
                "cost_usd": float(total_cost or 0.0),
                "stories_passed": stories_passed,
                "stories_tested": stories_tested,
                "breakdown": breakdown or {},
            },
            "last_event": last_event,
        },
    )


def _ack_atomic_cancel_commands(project_dir: Path, run_id: str) -> bool:
    from otto import paths
    from otto.checkpoint import write_cancel_checkpoint_marker
    from otto.runs.registry import append_command_ack, begin_command_drain, finish_command_drain

    commands = begin_command_drain(
        paths.session_command_requests(project_dir, run_id),
        paths.session_command_requests_processing(project_dir, run_id),
        paths.session_command_acks(project_dir, run_id),
    )
    cancelled = False
    state_version: int | None = None
    for cmd in commands:
        kind = str(cmd.get("kind") or cmd.get("cmd") or "")
        if kind == "cancel":
            if not cancelled:
                checkpoint = write_cancel_checkpoint_marker(
                    project_dir,
                    run_id=run_id,
                    command=None,
                )
                state_version = int(checkpoint.get("current_round") or 0)
            cancelled = True
        append_command_ack(
            paths.session_command_acks(project_dir, run_id),
            cmd,
            writer_id=f"atomic:{run_id}",
            outcome="applied" if kind == "cancel" else "ignored",
            state_version=state_version,
        )
    if commands:
        finish_command_drain(paths.session_command_requests_processing(project_dir, run_id))
    return cancelled


def _raise_if_atomic_cancel_requested(project_dir: Path, run_id: str) -> None:
    if _ack_atomic_cancel_commands(project_dir, run_id):
        raise KeyboardInterrupt("cancelled by command")


def _make_atomic_terminal_callback(
    project_dir: Path,
    run_id: str,
    callback: Any,
) -> Any:
    from otto.runs.registry import HEARTBEAT_INTERVAL_S

    if callback is None:
        return None

    last_poll_at = 0.0

    def _wrapped(message: str) -> None:
        nonlocal last_poll_at
        callback(message)
        now = time.monotonic()
        if now - last_poll_at < HEARTBEAT_INTERVAL_S:
            return
        last_poll_at = now
        _raise_if_atomic_cancel_requested(project_dir, run_id)

    return _wrapped


def _make_atomic_heartbeat_callback(project_dir: Path, run_id: str) -> Any:
    def _heartbeat() -> None:
        _raise_if_atomic_cancel_requested(project_dir, run_id)

    return _heartbeat


def _cleanup_orphan_processes(
    project_dir: Path,
    process_group_id: int | None = None,
    process_start_time_ns: int | None = None,
) -> None:
    """Kill the spawned agent process group after timeout/crash."""
    if process_group_id is None:
        logger.debug("Orphan-process cleanup skipped: no tracked process group for %s", project_dir)
        return
    if not _process_group_identity_matches(process_group_id, process_start_time_ns):
        logger.warning(
            "Orphan-process cleanup skipped: process group %d no longer matches tracked agent identity",
            process_group_id,
        )
        return
    try:
        import signal

        os.killpg(process_group_id, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group_id, 0)
            except ProcessLookupError:
                logger.info("Cleaned up agent process group %d", process_group_id)
                return
            except PermissionError:
                return
            time.sleep(0.1)
        os.killpg(process_group_id, signal.SIGKILL)
        logger.warning("Force-killed agent process group %d", process_group_id)
    except ProcessLookupError:
        return
    except PermissionError:
        logger.debug("Process-group cleanup skipped: permission denied for %d", process_group_id)
    except OSError as exc:
        logger.debug("Process-group cleanup skipped: %s", exc)


def _process_group_identity_matches(process_group_id: int, process_start_time_ns: int | None) -> bool:
    if process_start_time_ns is None:
        return True
    try:
        import psutil
    except Exception:
        return True

    try:
        process = psutil.Process(process_group_id)
        actual_start_time_ns = int(process.create_time() * 1_000_000_000)
        if abs(actual_start_time_ns - process_start_time_ns) > 100_000_000:
            return False
        try:
            return os.getpgid(process_group_id) == process_group_id
        except ProcessLookupError:
            return False
    except psutil.NoSuchProcess:
        return False
    except Exception:
        return True
