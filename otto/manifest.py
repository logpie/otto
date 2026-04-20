"""Manifest contract for otto runs (Phase 1.4).

At the end of every successful otto invocation (`otto build`, `otto improve`,
`otto certify`), we write a `manifest.json` recording the artifact paths,
cost, duration, branch, and metadata. This is the contract the queue runner
and merge orchestrator rely on to find each task's output without inferring
paths.

**Path policy:**
- If `OTTO_QUEUE_TASK_ID` env var is set (queue runner spawned us):
    `<project>/otto_logs/queue/<task-id>/manifest.json`  (deterministic)
- Else (atomic mode):
    `<fallback_dir>/manifest.json`  (alongside the run's checkpoint)

**Why two paths:** atomic mode preserves the existing per-run directory
layout; queue mode needs a deterministic path the watcher can find by
task id (the watcher doesn't know the internal `build_id`).

Manifests are written atomically (`tempfile + rename`).

See plan-parallel.md §3.2, §5 Step 1.4, §4 decision log entry on
"Manifests at deterministic per-task path".
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

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
    exit_status: str = "success"       # 'success' | 'failure'
    schema_version: int = 1
    extra: dict[str, object] = field(default_factory=dict)


def manifest_path_for(
    *,
    project_dir: Path,
    fallback_dir: Path,
    queue_task_id: str | None = None,
) -> Path:
    """Compute where the manifest should be written.

    `queue_task_id` defaults to the value of $OTTO_QUEUE_TASK_ID.
    """
    if queue_task_id is None:
        queue_task_id = _queue_task_id_from_env()
    else:
        queue_task_id = _validate_queue_task_id(queue_task_id)
    if queue_task_id:
        # Queue runner sets OTTO_QUEUE_PROJECT_DIR to the main project so the
        # spawned otto (whose cwd is the worktree) and the watcher (cwd =
        # main project) resolve to the SAME manifest path. Without this, the
        # watcher would never find the spawned child's manifest.
        env_dir = os.environ.get(QUEUE_PROJECT_DIR_ENV)
        anchor = Path(env_dir) if env_dir else project_dir
        return anchor / "otto_logs" / "queue" / queue_task_id / "manifest.json"
    return fallback_dir / "manifest.json"


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
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(manifest), indent=2, sort_keys=False))
    tmp.replace(path)
    return path


def make_manifest(
    *,
    command: str,
    argv: list[str],
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
    return Manifest(
        command=command,
        argv=list(argv),
        queue_task_id=None,            # filled by write_manifest()
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
