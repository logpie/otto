"""JSON serializers for Mission Control clients."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from otto.config import DEFAULTS
from otto.config import agent_effort
from otto.config import agent_model
from otto.config import agent_provider
from otto.config import get_max_rounds
from otto.config import get_max_turns_per_call
from otto.config import get_run_budget
from otto.config import get_spec_timeout
from otto.config import load_config
from otto.config import resolve_certifier_mode
from otto.mission_control.actions import ActionResult, ActionState
from otto.mission_control.model import (
    ArtifactRef,
    DetailView,
    HistoryItem,
    LiveRunItem,
    MissionControlFilters,
    MissionControlState,
    ProjectStats,
    StaleOverlay,
)
from otto.runs.schema import RunRecord


def serialize_project(project_dir: Path) -> dict[str, Any]:
    project_dir = Path(project_dir).resolve(strict=False)
    return {
        "path": str(project_dir),
        "name": project_dir.name,
        "branch": _git_output(project_dir, ["branch", "--show-current"]) or None,
        "dirty": bool(_git_output(project_dir, ["status", "--porcelain"])),
        "head_sha": _git_output(project_dir, ["rev-parse", "--short", "HEAD"]) or None,
        "defaults": _project_defaults(project_dir),
    }


def serialize_filters(filters: MissionControlFilters) -> dict[str, Any]:
    return {
        "active_only": filters.active_only,
        "type": filters.type_filter,
        "outcome": filters.outcome_filter,
        "query": filters.query,
        "history_page": filters.history_page,
    }


def serialize_state(project_dir: Path, state: MissionControlState) -> dict[str, Any]:
    return {
        "project": serialize_project(project_dir),
        "filters": serialize_filters(state.filters),
        "focus": state.focus,
        "selection": {
            "run_id": state.selection.run_id,
            "origin_pane": state.selection.origin_pane,
            "artifact_index": state.selection.artifact_index,
            "log_index": state.selection.log_index,
        },
        "selected_run_ids": sorted(state.selected_run_ids),
        "project_stats": serialize_project_stats(state.project_stats),
        "live": {
            "items": [serialize_live_item(item) for item in state.live_runs.items],
            "total_count": state.live_runs.total_count,
            "active_count": state.live_runs.active_count,
            "refresh_interval_s": state.live_runs.refresh_interval_s,
        },
        "history": {
            "items": [serialize_history_item(item) for item in state.history_page.items],
            "page": state.history_page.page,
            "page_size": state.history_page.page_size,
            "total_rows": state.history_page.total_rows,
            "total_pages": state.history_page.total_pages,
        },
        "banner": state.last_event_banner,
    }


def serialize_live_item(item: LiveRunItem) -> dict[str, Any]:
    record = item.record
    display_status = _display_status(record.status, item.overlay)
    return {
        **_record_summary(record),
        "display_status": display_status,
        "active": _is_effectively_active(record.status, item.overlay),
        "display_id": item.display_id,
        "branch_task": item.branch_task,
        "elapsed_s": item.elapsed_s,
        "elapsed_display": item.elapsed_display,
        "cost_usd": item.cost_usd,
        "cost_display": item.cost_display,
        "token_usage": dict(item.token_usage),
        "last_event": item.event,
        "progress": item.progress,
        "row_label": item.row_label,
        "overlay": serialize_overlay(item.overlay),
    }


def serialize_history_item(item: HistoryItem) -> dict[str, Any]:
    row = item.row
    return {
        "run_id": row.run_id,
        "domain": row.domain,
        "run_type": row.run_type,
        "command": row.command,
        "status": row.status,
        "terminal_outcome": row.terminal_outcome,
        "queue_task_id": row.queue_task_id,
        "merge_id": row.merge_id,
        "branch": row.branch,
        "worktree": row.worktree,
        "summary": item.summary,
        "intent": row.intent,
        "completed_at_display": item.completed_at_display,
        "outcome_display": item.outcome_display,
        "duration_s": row.duration_s,
        "duration_display": item.duration_display,
        "cost_usd": row.cost_usd,
        "cost_display": item.cost_display,
        "token_usage": dict(item.token_usage),
        "resumable": row.resumable,
        "adapter_key": row.adapter_key,
    }


def serialize_project_stats(stats: ProjectStats) -> dict[str, Any]:
    return {
        "active_count": stats.active_count,
        "history_count": stats.history_count,
        "success_count": stats.success_count,
        "failed_count": stats.failed_count,
        "total_duration_s": stats.total_duration_s,
        "duration_display": stats.duration_display,
        "reported_cost_usd": stats.reported_cost_usd,
        "cost_display": stats.cost_display,
        "token_usage": dict(stats.token_usage),
        "total_tokens": stats.total_tokens,
        "token_display": stats.token_display,
        "stories_passed": stats.stories_passed,
        "stories_tested": stats.stories_tested,
    }


def serialize_detail(detail: DetailView) -> dict[str, Any]:
    record = detail.record
    argv = record.source.get("argv")
    run_config = run_config_from_argv(
        Path(record.project_dir),
        argv,
        source=_record_config_source(record),
        metrics=record.metrics,
    )
    return {
        **_record_summary(record),
        "display_status": _display_status(record.status, detail.overlay),
        "active": _is_effectively_active(record.status, detail.overlay),
        "source": detail.source,
        "title": detail.detail.title,
        "summary_lines": list(detail.detail.summary_lines),
        "overlay": serialize_overlay(detail.overlay),
        "artifacts": [serialize_artifact(artifact, index) for index, artifact in enumerate(detail.artifacts)],
        "log_paths": list(detail.log_paths),
        "selected_log_index": detail.selected_log_index,
        "selected_log_path": detail.selected_log_path,
        "legal_actions": [serialize_action_state(action) for action in detail.legal_actions],
        "phase_timeline": _phase_timeline(record, run_config),
        "record": record.to_dict(),
    }


def serialize_artifact(artifact: ArtifactRef, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "label": artifact.label,
        "path": artifact.path,
        "kind": artifact.kind,
        "exists": artifact.exists,
    }


def serialize_action_state(action: ActionState) -> dict[str, Any]:
    return {
        "key": action.key,
        "label": action.label,
        "enabled": action.enabled,
        "reason": action.reason,
        "preview": action.preview,
    }


def serialize_action_result(result: ActionResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "message": result.message,
        "severity": result.severity,
        "modal_title": result.modal_title,
        "modal_message": result.modal_message,
        "refresh": result.refresh,
        "clear_banner": result.clear_banner,
    }


def serialize_overlay(overlay: StaleOverlay | None) -> dict[str, Any] | None:
    if overlay is None:
        return None
    return {
        "level": overlay.level,
        "label": overlay.label,
        "reason": overlay.reason,
        "writer_alive": overlay.writer_alive,
    }


def _display_status(status: str | None, overlay: StaleOverlay | None) -> str:
    if overlay is not None and overlay.level == "stale":
        return "stale"
    return str(status or "")


def _is_effectively_active(status: str | None, overlay: StaleOverlay | None) -> bool:
    if str(status or "") in {"done", "failed", "cancelled", "removed", "interrupted"}:
        return False
    if overlay is not None and overlay.level == "stale":
        return False
    return True


def _record_summary(record: RunRecord) -> dict[str, Any]:
    argv = record.source.get("argv")
    run_config = run_config_from_argv(
        Path(record.project_dir),
        argv,
        source=_record_config_source(record),
        metrics=record.metrics,
    )
    return {
        "run_id": record.run_id,
        "domain": record.domain,
        "run_type": record.run_type,
        "command": record.command,
        "display_name": record.display_name,
        "status": record.status,
        "terminal_outcome": record.terminal_outcome,
        "project_dir": record.project_dir,
        "cwd": record.cwd,
        "queue_task_id": _first_string(record.identity.get("queue_task_id")),
        "merge_id": _first_string(record.identity.get("merge_id")),
        "branch": _first_string(record.git.get("branch")),
        "worktree": _first_string(record.git.get("worktree")),
        "provider": run_config["provider"],
        "model": run_config["model"],
        "reasoning_effort": run_config["reasoning_effort"],
        "certifier_mode": run_config["certifier_mode"],
        "skip_product_qa": run_config["skip_product_qa"],
        "build_config": run_config,
        "run_config": run_config,
        "adapter_key": record.adapter_key,
        "version": record.version,
    }


def _record_config_source(record: RunRecord) -> dict[str, Any]:
    source = dict(record.source) if isinstance(record.source, dict) else {}
    if record.run_type in {"build", "improve", "certify"}:
        source.setdefault("command_family", record.run_type)
    return source


def run_config_from_argv(
    project_dir: Path,
    argv: Any,
    *,
    source: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = source if isinstance(source, dict) else {}
    metrics = metrics if isinstance(metrics, dict) else {}
    defaults = _project_defaults(project_dir)
    provider_override = _first_string(
        source.get("provider"),
        source.get("agent_provider"),
        metrics.get("provider"),
        _argv_option(argv, "--provider"),
    )
    model_override = _first_string(
        source.get("model"),
        source.get("agent_model"),
        metrics.get("model"),
        _argv_option(argv, "--model"),
    )
    effort_override = _first_string(
        source.get("reasoning_effort"),
        source.get("effort"),
        metrics.get("reasoning_effort"),
        _argv_option(argv, "--effort", "--reasoning-effort"),
    )
    certifier_mode = _first_string(
        source.get("certifier_mode"),
        source.get("mode"),
        metrics.get("certifier_mode"),
        metrics.get("mode"),
        _argv_certifier_mode(argv),
        _argv_command_default_mode(argv),
        defaults.get("certifier_mode"),
    )
    skip_product_qa = _first_bool(
        source.get("skip_product_qa"),
        metrics.get("skip_product_qa"),
        _argv_flag(argv, "--no-qa", "--skip-product-qa"),
        defaults.get("skip_product_qa"),
    )
    run_budget_seconds = _first_int(
        source.get("run_budget_seconds"),
        metrics.get("run_budget_seconds"),
        _argv_int_option(argv, "--budget"),
        defaults.get("run_budget_seconds"),
    )
    max_certify_rounds = _first_int(
        source.get("max_certify_rounds"),
        metrics.get("max_certify_rounds"),
        _argv_int_option(argv, "--rounds", "-n"),
        defaults.get("max_certify_rounds"),
    )
    max_turns_per_call = _first_int(
        source.get("max_turns_per_call"),
        metrics.get("max_turns_per_call"),
        _argv_int_option(argv, "--max-turns"),
        defaults.get("max_turns_per_call"),
    )
    strict_mode = _first_bool(
        source.get("strict_mode"),
        metrics.get("strict_mode"),
        _argv_flag(argv, "--strict"),
        defaults.get("strict_mode"),
    )
    split_mode = _first_bool(
        source.get("split_mode"),
        metrics.get("split_mode"),
        _argv_split_mode(argv),
        defaults.get("split_mode"),
    )
    command_family = _first_string(
        source.get("command_family"),
        metrics.get("command_family"),
        _argv_command_family(argv),
    )
    allow_dirty_repo = _first_bool(
        source.get("allow_dirty_repo"),
        metrics.get("allow_dirty_repo"),
        _argv_flag(argv, "--allow-dirty"),
        defaults.get("allow_dirty_repo"),
    )
    agents = _agents_with_overrides(
        project_dir,
        provider=provider_override,
        model=model_override,
        effort=effort_override,
        agent_overrides=_agent_overrides_from_sources(source, metrics, argv),
        fallback=defaults,
    )
    if command_family == "improve":
        improver_agent = _agent_override_from_argv(argv, "improver")
        target_agent = "fix" if split_mode else "build"
        for key, value in improver_agent.items():
            if not value:
                continue
            resolved_key = "reasoning_effort" if key == "effort" else key
            agents.setdefault(target_agent, {})[resolved_key] = value
    primary_agent_name = "certifier" if command_family == "certify" else "build"
    if command_family == "improve" and split_mode:
        primary_agent_name = "fix"
    build_agent = agents.get(primary_agent_name, {})
    return {
        "command_family": command_family,
        "provider": _first_string(build_agent.get("provider"), provider_override, defaults.get("provider")),
        "model": _first_string(build_agent.get("model"), model_override, defaults.get("model")),
        "reasoning_effort": _first_string(
            build_agent.get("reasoning_effort"),
            effort_override,
            defaults.get("reasoning_effort"),
        ),
        "certifier_mode": certifier_mode,
        "skip_product_qa": skip_product_qa,
        "certification": _certification_label(certifier_mode, skip_product_qa),
        "planning": _argv_planning(argv),
        "spec_file_path": _argv_option(argv, "--spec-file"),
        "run_budget_seconds": run_budget_seconds,
        "spec_timeout": _first_int(defaults.get("spec_timeout")),
        "max_certify_rounds": max_certify_rounds,
        "max_turns_per_call": max_turns_per_call,
        "strict_mode": strict_mode,
        "split_mode": split_mode,
        "allow_dirty_repo": allow_dirty_repo,
        "default_branch": _first_string(defaults.get("default_branch")),
        "test_command": _first_string(defaults.get("test_command")),
        "queue": {
            "concurrent": _first_int(defaults.get("queue_concurrent")),
            "task_timeout_s": _first_float(defaults.get("queue_task_timeout_s")),
            "worktree_dir": _first_string(defaults.get("queue_worktree_dir")),
            "on_watcher_restart": _first_string(defaults.get("queue_on_watcher_restart")),
            "merge_certifier_mode": _first_string(defaults.get("queue_merge_certifier_mode")),
        },
        "agents": agents,
        "config_file_exists": bool(defaults.get("config_file_exists")),
        "config_error": _first_string(defaults.get("config_error")),
    }


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_int(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _first_float(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _argv_option(argv: Any, *names: str) -> str | None:
    if not isinstance(argv, (list, tuple)):
        return None
    parts = [str(part) for part in argv]
    for index, part in enumerate(parts):
        for name in names:
            if part == name:
                if index + 1 >= len(parts):
                    continue
                value = parts[index + 1].strip()
                if value and not value.startswith("--"):
                    return value
            prefix = f"{name}="
            if part.startswith(prefix):
                value = part[len(prefix):].strip()
                if value:
                    return value
    return None


def _argv_int_option(argv: Any, *names: str) -> int | None:
    value = _argv_option(argv, *names)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _first_bool(*values: Any) -> bool:
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
            return True
        if isinstance(value, str) and value.strip().lower() in {"0", "false", "no", "off"}:
            return False
    return False


def _argv_flag(argv: Any, *names: str) -> bool | None:
    if not isinstance(argv, (list, tuple)):
        return None
    parts = [str(part) for part in argv]
    return any(part in names for part in parts)


def _argv_split_mode(argv: Any) -> bool | None:
    if _argv_flag(argv, "--split"):
        return True
    if _argv_flag(argv, "--agentic"):
        return False
    return None


def _argv_planning(argv: Any) -> str:
    if _argv_option(argv, "--spec-file"):
        return "spec_file"
    if _argv_flag(argv, "--spec"):
        return "spec_auto" if _argv_flag(argv, "--yes") else "spec_review"
    return "direct"


def _argv_command_family(argv: Any) -> str | None:
    if not isinstance(argv, (list, tuple)) or not argv:
        return None
    command = str(argv[0]).strip()
    return command if command in {"build", "improve", "certify"} else None


def _argv_certifier_mode(argv: Any) -> str | None:
    if not isinstance(argv, (list, tuple)):
        return None
    parts = {str(part) for part in argv}
    for mode in ("fast", "standard", "thorough"):
        if f"--{mode}" in parts:
            return mode
    return None


def _argv_command_default_mode(argv: Any) -> str | None:
    if not isinstance(argv, (list, tuple)):
        return None
    parts = [str(part) for part in argv]
    if len(parts) < 2 or parts[0] != "improve":
        return None
    if parts[1] == "bugs":
        return "thorough"
    if parts[1] == "feature":
        return "hillclimb"
    if parts[1] == "target":
        return "target"
    return None


def _certification_label(mode: str | None, skip_product_qa: bool) -> str:
    if skip_product_qa:
        return "skipped"
    resolved = mode or "fast"
    if resolved in {"hillclimb", "target"}:
        return f"{resolved} evaluation"
    return f"{resolved} certification"


def _phase_timeline(record: Any, run_config: dict[str, Any]) -> list[dict[str, Any]]:
    breakdown = record.metrics.get("breakdown")
    if not isinstance(breakdown, dict):
        breakdown = {}
    command_family = _first_string(run_config.get("command_family"))
    if run_config.get("split_mode") and command_family == "improve":
        phases = ["certify", "fix"]
    else:
        phases = ["build", "certify", "fix"] if run_config.get("split_mode") else ["agentic"]
    if run_config.get("skip_product_qa"):
        phases = ["build"]
    timeline: list[dict[str, Any]] = []
    for phase in phases:
        data = breakdown.get(phase if phase != "agentic" else "build")
        if not isinstance(data, dict):
            data = {}
        agent_key = "build" if phase == "agentic" else ("certifier" if phase == "certify" else phase)
        agent = (run_config.get("agents") or {}).get(agent_key, {}) or {}
        status = "pending"
        if data:
            status = "done" if record.status in {"done", "failed", "cancelled", "removed", "interrupted"} else "active"
        elif record.status in {"done", "failed", "cancelled", "removed", "interrupted"}:
            status = "skipped"
        timeline.append({
            "phase": phase,
            "label": _phase_label(phase, command_family=command_family),
            "status": status,
            "duration_s": _first_float(data.get("duration_s")),
            "cost_usd": _first_float(data.get("cost_usd")),
            "rounds": _first_int(data.get("rounds")),
            "token_usage": _token_usage_from_mapping(data),
            "provider": _first_string(agent.get("provider"), run_config.get("provider")),
            "model": _first_string(agent.get("model"), run_config.get("model")),
            "reasoning_effort": _first_string(
                agent.get("reasoning_effort"),
                agent.get("effort"),
                run_config.get("reasoning_effort"),
            ),
        })
    return timeline


def _phase_label(phase: str, *, command_family: str | None = None) -> str:
    if command_family == "improve":
        return {
            "agentic": "Agentic improve session",
            "certify": "Evaluate",
            "fix": "Improve / fix",
        }.get(phase, phase.replace("_", " ").title())
    return {
        "agentic": "Agentic session",
        "build": "Build",
        "certify": "Certify",
        "fix": "Fix",
    }.get(phase, phase.replace("_", " ").title())


def _token_usage_from_mapping(mapping: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for key in (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
    ):
        value = _first_int(mapping.get(key))
        if value:
            result[key] = value
    return result


def _project_defaults(project_dir: Path) -> dict[str, Any]:
    config_path = project_dir / "otto.yaml"
    config_exists = config_path.exists()
    try:
        config = load_config(config_path)
        queue = config.get("queue") if isinstance(config.get("queue"), dict) else {}
        return {
            "provider": agent_provider(config),
            "model": agent_model(config),
            "reasoning_effort": agent_effort(config),
            "certifier_mode": resolve_certifier_mode(config),
            "skip_product_qa": bool(config.get("skip_product_qa")),
            "run_budget_seconds": get_run_budget(config),
            "spec_timeout": get_spec_timeout(config),
            "max_certify_rounds": get_max_rounds(config),
            "max_turns_per_call": get_max_turns_per_call(config),
            "strict_mode": bool(config.get("strict_mode")),
            "split_mode": bool(config.get("split_mode")),
            "allow_dirty_repo": bool(config.get("allow_dirty_repo")),
            "default_branch": _first_string(config.get("default_branch")),
            "test_command": _first_string(config.get("test_command")),
            "queue_concurrent": _first_int(queue.get("concurrent")),
            "queue_task_timeout_s": _first_float(queue.get("task_timeout_s")),
            "queue_worktree_dir": _first_string(queue.get("worktree_dir")),
            "queue_on_watcher_restart": _first_string(queue.get("on_watcher_restart")),
            "queue_merge_certifier_mode": _first_string(queue.get("merge_certifier_mode")),
            "config_file_exists": config_exists,
            "config_error": None,
        }
    except Exception as exc:
        queue_defaults = DEFAULTS["queue"] if isinstance(DEFAULTS.get("queue"), dict) else {}
        return {
            "provider": DEFAULTS["provider"],
            "model": DEFAULTS["model"],
            "reasoning_effort": DEFAULTS["effort"],
            "certifier_mode": DEFAULTS["certifier_mode"],
            "skip_product_qa": DEFAULTS["skip_product_qa"],
            "run_budget_seconds": DEFAULTS["run_budget_seconds"],
            "spec_timeout": DEFAULTS["spec_timeout"],
            "max_certify_rounds": DEFAULTS["max_certify_rounds"],
            "max_turns_per_call": DEFAULTS["max_turns_per_call"],
            "strict_mode": DEFAULTS["strict_mode"],
            "split_mode": DEFAULTS["split_mode"],
            "allow_dirty_repo": DEFAULTS["allow_dirty_repo"],
            "default_branch": DEFAULTS["default_branch"],
            "test_command": DEFAULTS["test_command"],
            "queue_concurrent": queue_defaults.get("concurrent"),
            "queue_task_timeout_s": queue_defaults.get("task_timeout_s"),
            "queue_worktree_dir": queue_defaults.get("worktree_dir"),
            "queue_on_watcher_restart": queue_defaults.get("on_watcher_restart"),
            "queue_merge_certifier_mode": queue_defaults.get("merge_certifier_mode"),
            "config_file_exists": config_exists,
            "config_error": str(exc),
        }


def _agents_with_overrides(
    project_dir: Path,
    *,
    provider: str | None,
    model: str | None,
    effort: str | None,
    agent_overrides: dict[str, dict[str, str | None]] | None = None,
    fallback: dict[str, Any],
) -> dict[str, dict[str, str | None]]:
    config_path = project_dir / "otto.yaml"
    agent_overrides = agent_overrides or {}
    try:
        config = load_config(config_path)
    except Exception:
        return {
            name: {
                "provider": _first_string(
                    agent_overrides.get(name, {}).get("provider"),
                    provider,
                    fallback.get("provider"),
                ),
                "model": _first_string(
                    agent_overrides.get(name, {}).get("model"),
                    model,
                    fallback.get("model"),
                ),
                "reasoning_effort": _first_string(
                    agent_overrides.get(name, {}).get("effort"),
                    effort,
                    fallback.get("reasoning_effort"),
                ),
            }
            for name in ("build", "certifier", "spec", "fix")
        }
    overrides = {key: value for key, value in {"provider": provider, "model": model, "effort": effort}.items() if value}
    phase_overrides = {
        name: {key: value for key, value in values.items() if value}
        for name, values in agent_overrides.items()
    }
    if overrides:
        config = dict(config)
        config["_cli_overrides"] = dict(overrides)
    if phase_overrides:
        config = dict(config)
        cli_overrides = dict(config.get("_cli_overrides") or {})
        cli_overrides["agents"] = phase_overrides
        config["_cli_overrides"] = cli_overrides
    return {
        name: {
            "provider": agent_provider(config, name),
            "model": agent_model(config, name),
            "reasoning_effort": agent_effort(config, name),
        }
        for name in ("build", "certifier", "spec", "fix")
    }


def _agent_overrides_from_sources(
    source: dict[str, Any],
    metrics: dict[str, Any],
    argv: Any,
) -> dict[str, dict[str, str | None]]:
    source_agents = source.get("agents") if isinstance(source.get("agents"), dict) else {}
    metrics_agents = metrics.get("agents") if isinstance(metrics.get("agents"), dict) else {}
    result: dict[str, dict[str, str | None]] = {}
    for name in ("build", "certifier", "fix"):
        source_agent = source_agents.get(name) if isinstance(source_agents, dict) else {}
        metrics_agent = metrics_agents.get(name) if isinstance(metrics_agents, dict) else {}
        if not isinstance(source_agent, dict):
            source_agent = {}
        if not isinstance(metrics_agent, dict):
            metrics_agent = {}
        result[name] = {
            "provider": _first_string(
                source_agent.get("provider"),
                metrics_agent.get("provider"),
                _argv_option(argv, f"--{name}-provider"),
            ),
            "model": _first_string(
                source_agent.get("model"),
                metrics_agent.get("model"),
                _argv_option(argv, f"--{name}-model"),
            ),
            "effort": _first_string(
                source_agent.get("effort"),
                source_agent.get("reasoning_effort"),
                metrics_agent.get("effort"),
                metrics_agent.get("reasoning_effort"),
                _argv_option(argv, f"--{name}-effort"),
            ),
        }
    return result


def _agent_override_from_argv(argv: Any, name: str) -> dict[str, str | None]:
    return {
        "provider": _argv_option(argv, f"--{name}-provider"),
        "model": _argv_option(argv, f"--{name}-model"),
        "effort": _argv_option(argv, f"--{name}-effort"),
    }


def _git_output(project_dir: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
