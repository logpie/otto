"""Manifest contract for otto runs.

Every run writes its canonical manifest at the session root:

    otto_logs/sessions/<run-id>/manifest.json

Queue-backed runs additionally mirror that manifest to the queue index:

    otto_logs/queue/<task-id>/manifest.json

The queue copy is a control-plane index keyed by task slug. It is never the
authoritative record; it mirrors the canonical session-root manifest and adds
`mirror_of` pointing at it.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from otto import paths
from otto.observability import write_json_atomic

QUEUE_TASK_ENV = "OTTO_QUEUE_TASK_ID"
QUEUE_PROJECT_DIR_ENV = "OTTO_QUEUE_PROJECT_DIR"
QUEUE_TASK_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:-\d+)?$")


@dataclass
class Manifest:
    """Schema for the per-run manifest. JSON-serialised via `asdict()`.

    Fields populated by callers; `manifest_path` is set by `write_manifest`.
    """

    command: str                       # 'build' | 'improve' | 'certify'
    argv: list[str]                    # full argv passed to otto, verbatim
    queue_task_id: str | None          # set when OTTO_QUEUE_TASK_ID env var present
    run_id: str                        # otto's internal id (build_id or similar)
    branch: str | None                 # branch this run produced commits on
    checkpoint_path: str | None        # absolute path to checkpoint.json (or None)
    proof_of_work_path: str | None     # absolute path to proof-of-work.json (or None)
    cost_usd: float
    duration_s: float
    started_at: str                    # ISO 8601
    finished_at: str                   # ISO 8601
    head_sha: str | None               # `git rev-parse HEAD` at end of run
    resolved_intent: str | None        # the intent actually used by the run
    focus: str | None = None
    target: str | None = None
    exit_status: str = "success"       # 'success' | 'failure' | 'paused'
    schema_version: int = 1
    extra: dict[str, object] = field(default_factory=dict)


def manifest_path_for(
    *,
    project_dir: Path,
    fallback_dir: Path,
    queue_task_id: str | None = None,
) -> Path:
    """Return the canonical session-root manifest path for a run."""
    del project_dir
    if queue_task_id is not None:
        queue_task_id = _validate_queue_task_id(queue_task_id)
    return fallback_dir / "manifest.json"


def queue_index_path_for(project_dir: Path, task_id: str | None) -> Path | None:
    """Return the queue manifest index path for a task slug."""
    if not task_id:
        return None
    task_id = _validate_queue_task_id(task_id)
    return paths.queue_manifest_path(project_dir, task_id)


def _validate_queue_task_id(queue_task_id: str) -> str:
    """Validate queue task IDs before using them in filesystem paths."""
    if not queue_task_id or not QUEUE_TASK_ID_RE.fullmatch(queue_task_id):
        raise ValueError(
            f"Invalid {QUEUE_TASK_ENV} {queue_task_id!r}. Expected a lowercase "
            "slug like 'add-csv-export' or 'add-csv-export-2'."
        )
    return queue_task_id


def _queue_task_id_from_env() -> str | None:
    """Return the validated queue task id from the environment, if present."""
    if QUEUE_TASK_ENV not in os.environ:
        return None
    return _validate_queue_task_id(os.environ[QUEUE_TASK_ENV])


def current_head_sha(project_dir: Path) -> str | None:
    """Return the current HEAD SHA, or None if not a git repo / no commits."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    return None


def write_manifest(
    manifest: Manifest,
    *,
    project_dir: Path,
    fallback_dir: Path,
) -> Path:
    """Atomically write the manifest. Returns the path written.

    - Determines path via `manifest_path_for()`
    - Creates parent dir if needed
    - Writes via `tempfile + rename` for atomicity
    """
    # Auto-fill queue_task_id from env if not explicitly set in the dataclass
    if manifest.queue_task_id is None:
        manifest.queue_task_id = _queue_task_id_from_env()
    elif manifest.queue_task_id:
        manifest.queue_task_id = _validate_queue_task_id(manifest.queue_task_id)

    path = manifest_path_for(
        project_dir=project_dir,
        fallback_dir=fallback_dir,
        queue_task_id=manifest.queue_task_id,
    )
    payload = asdict(manifest)
    _atomic_write_json(path, payload)

    anchor_project_dir = Path(os.environ.get(QUEUE_PROJECT_DIR_ENV) or project_dir)
    queue_path = queue_index_path_for(anchor_project_dir, manifest.queue_task_id)
    if queue_path is not None:
        mirror_payload = dict(payload)
        mirror_payload["mirror_of"] = str(path.resolve())
        _atomic_write_json(queue_path, mirror_payload)
    return path


def make_manifest(
    *,
    command: str,
    argv: list[str],
    queue_task_id: str | None = None,
    run_id: str,
    branch: str | None,
    checkpoint_path: Path | None,
    proof_of_work_path: Path | None,
    cost_usd: float,
    duration_s: float,
    started_at: str,
    head_sha: str | None,
    resolved_intent: str | None,
    focus: str | None = None,
    target: str | None = None,
    exit_status: str = "success",
) -> Manifest:
    """Construct a Manifest with `finished_at` set to now and `argv` shallow-copied."""
    if queue_task_id:
        queue_task_id = _validate_queue_task_id(queue_task_id)
    else:
        queue_task_id = None
    return Manifest(
        command=command,
        argv=list(argv),
        queue_task_id=queue_task_id,
        run_id=run_id,
        branch=branch,
        checkpoint_path=str(checkpoint_path) if checkpoint_path else None,
        proof_of_work_path=str(proof_of_work_path) if proof_of_work_path else None,
        cost_usd=float(cost_usd),
        duration_s=float(duration_s),
        started_at=started_at,
        finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        head_sha=head_sha,
        resolved_intent=resolved_intent,
        focus=focus,
        target=target,
        exit_status=exit_status,
    )


def now_iso() -> str:
    """Helper for callers that want a started_at timestamp at run start."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    write_json_atomic(path, payload, sort_keys=False)
