"""Queue enqueue service shared by CLI and web clients."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from otto.branching import compute_branch_name
from otto.config import first_touch_bookkeeping, load_config
from otto.queue.ids import generate_task_id, validate_after_refs
from otto.queue.schema import QueueTask, append_task, load_queue


@dataclass(slots=True)
class QueueEnqueueResult:
    task: QueueTask
    warnings: list[str] = field(default_factory=list)


def enqueue_task(
    project_dir: Path,
    *,
    command: str,
    raw_args: list[str],
    intent: str | None,
    after: list[str],
    explicit_as: str | None,
    resumable: bool,
    focus: str | None = None,
    target: str | None = None,
    explicit_intent: str | None = None,
    base_ref: str | None = None,
) -> QueueEnqueueResult:
    """Append a queue task and return its immutable definition."""
    project_dir = Path(project_dir)
    if "--resume" in raw_args:
        raise ValueError(
            "--resume is not allowed in queued commands. The queue runner "
            "injects --resume automatically when it respawns a task after a watcher restart."
        )
    if explicit_intent is not None and _looks_like_flag(explicit_intent):
        raise ValueError(
            f"Intent looks like a CLI flag ({explicit_intent!r}), not a description. "
            "Did you forget to quote the intent? Examples:\n"
            "    otto queue build \"add csv export\" --as csv\n"
            "    otto queue build \"add csv export\" --as csv -- --fast --rounds 3\n"
            "  Note: intent must come BEFORE `--`. Anything after `--` is passed through to the inner otto build."
        )

    config = load_config(project_dir / "otto.yaml")
    warnings: list[str] = []
    try:
        first_touch_bookkeeping(project_dir, config)
    except Exception as exc:  # pragma: no cover - best-effort filesystem setup
        warnings.append(f"bookkeeping setup skipped: {exc}")

    try:
        existing = load_queue(project_dir)
    except ValueError as exc:
        message = str(exc)
        if not message.startswith("queue.yml is malformed"):
            message = f"queue.yml is malformed: {message}"
        raise ValueError(message) from exc
    existing_ids = [task.id for task in existing]
    task_id = generate_task_id(
        intent=intent,
        command=command,
        existing_ids=existing_ids,
        explicit_as=explicit_as,
    )
    if after:
        validate_after_refs(after=after, self_id=task_id, all_ids=existing_ids)

    argv = [command, *raw_args]
    worktree_dir = str(config.get("queue", {}).get("worktree_dir", ".worktrees"))
    task = QueueTask(
        id=task_id,
        command_argv=argv,
        after=after,
        resumable=resumable,
        added_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        resolved_intent=intent,
        focus=focus,
        target=target,
        branch=compute_branch_name(command, task_id),
        worktree=str(Path(worktree_dir) / task_id),
        base_ref=base_ref,
    )
    append_task(project_dir, task)
    return QueueEnqueueResult(task=task, warnings=warnings)


def _looks_like_flag(value: str) -> bool:
    value = str(value or "").strip()
    return value.startswith("-") and len(value) > 1
