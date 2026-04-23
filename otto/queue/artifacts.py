"""Queue session preservation helpers for worktree cleanup."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from otto import paths
from otto.manifest import queue_index_path_for
from otto.runs.history import append_history_snapshot, read_history_rows

_ARTIFACT_PATH_KEYS = (
    "session_dir",
    "manifest_path",
    "summary_path",
    "checkpoint_path",
    "primary_log_path",
)


def preserve_queue_session_artifacts(
    project_dir: Path,
    *,
    task_id: str,
    worktree_path: Path,
    merge_commit_sha: str | None = None,
    merged_at: str | None = None,
    refuse_existing_destination: bool = False,
    strict: bool = False,
) -> dict[str, Any] | None:
    """Mirror one queue session into the project and repair durable references."""

    queue_manifest_path = queue_index_path_for(project_dir, task_id)
    if queue_manifest_path is None:
        if strict:
            raise ValueError("queue task id missing")
        return None
    if not queue_manifest_path.exists():
        if strict:
            raise FileNotFoundError(queue_manifest_path)
        return None

    queue_manifest = _read_json(queue_manifest_path)
    run_id = _queue_run_id(queue_manifest)
    if not run_id:
        if strict:
            raise ValueError("queue manifest missing run_id")
        return None

    src_session_dir = paths.session_dir(worktree_path, run_id)
    dst_session_dir = paths.session_dir(project_dir, run_id)
    moved = False

    if src_session_dir.exists():
        if dst_session_dir.exists():
            if refuse_existing_destination:
                raise FileExistsError(f"{src_session_dir} -> {dst_session_dir}")
        else:
            dst_session_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_session_dir), str(dst_session_dir))
            moved = True
    elif not dst_session_dir.exists():
        if strict:
            raise FileNotFoundError(src_session_dir)
        return None

    _rewrite_summary(
        dst_session_dir / "summary.json",
        merge_commit_sha=merge_commit_sha,
        merged_at=merged_at,
    )
    _rewrite_manifest_and_queue_index(
        queue_manifest_path,
        queue_manifest,
        old_session_dir=src_session_dir,
        new_session_dir=dst_session_dir,
        merge_commit_sha=merge_commit_sha,
        merged_at=merged_at,
    )
    _rewrite_history_snapshot(
        project_dir,
        run_id=run_id,
        old_session_dir=src_session_dir,
        new_session_dir=dst_session_dir,
    )

    return {
        "run_id": run_id,
        "src_session_dir": src_session_dir,
        "dst_session_dir": dst_session_dir,
        "moved": moved,
    }


def _rewrite_summary(
    summary_path: Path,
    *,
    merge_commit_sha: str | None,
    merged_at: str | None,
) -> None:
    if not summary_path.exists():
        return
    if not merge_commit_sha and not merged_at:
        return
    summary = _read_json(summary_path)
    if merge_commit_sha:
        summary["merge_commit_sha"] = merge_commit_sha
    if merged_at:
        summary["merged_at"] = merged_at
    _atomic_write_json(summary_path, summary)


def _rewrite_manifest_and_queue_index(
    queue_manifest_path: Path,
    queue_manifest: dict[str, Any],
    *,
    old_session_dir: Path,
    new_session_dir: Path,
    merge_commit_sha: str | None,
    merged_at: str | None,
) -> None:
    canonical_manifest_path = new_session_dir / "manifest.json"
    canonical_manifest = (
        _read_json(canonical_manifest_path)
        if canonical_manifest_path.exists()
        else dict(queue_manifest)
    )
    _rewrite_manifest_after_session_relocation(
        canonical_manifest,
        old_session_dir=old_session_dir,
        new_session_dir=new_session_dir,
        merge_commit_sha=merge_commit_sha,
        merged_at=merged_at,
    )
    _atomic_write_json(canonical_manifest_path, canonical_manifest)

    mirrored_manifest = dict(canonical_manifest)
    mirrored_manifest["mirror_of"] = str(canonical_manifest_path.resolve())
    _atomic_write_json(queue_manifest_path, mirrored_manifest)


def _rewrite_manifest_after_session_relocation(
    manifest: dict[str, Any],
    *,
    old_session_dir: Path,
    new_session_dir: Path,
    merge_commit_sha: str | None,
    merged_at: str | None,
) -> None:
    manifest["checkpoint_path"] = _relocate_session_path(
        manifest.get("checkpoint_path"),
        old_session_dir=old_session_dir,
        new_session_dir=new_session_dir,
    )
    manifest["proof_of_work_path"] = _relocate_session_path(
        manifest.get("proof_of_work_path"),
        old_session_dir=old_session_dir,
        new_session_dir=new_session_dir,
    )
    if merge_commit_sha or merged_at:
        extra = manifest.get("extra")
        if not isinstance(extra, dict):
            extra = {}
        if merge_commit_sha:
            extra["merge_commit_sha"] = merge_commit_sha
        if merged_at:
            extra["merged_at"] = merged_at
        manifest["extra"] = extra


def _rewrite_history_snapshot(
    project_dir: Path,
    *,
    run_id: str,
    old_session_dir: Path,
    new_session_dir: Path,
) -> None:
    dedupe_key = f"terminal_snapshot:{run_id}"
    latest: dict[str, Any] | None = None
    for row in reversed(read_history_rows(paths.history_jsonl(project_dir))):
        if str(row.get("dedupe_key") or "") == dedupe_key:
            latest = dict(row)
            break
    if latest is None:
        return

    repaired = dict(latest)
    artifacts = dict(repaired.get("artifacts") or {})
    changed = False

    for key in _ARTIFACT_PATH_KEYS:
        source_value = artifacts[key] if key in artifacts else repaired.get(key)
        relocated = _relocate_existing_artifact_path(
            source_value,
            old_session_dir=old_session_dir,
            new_session_dir=new_session_dir,
        )
        if repaired.get(key) != relocated:
            repaired[key] = relocated
            changed = True
        if artifacts.get(key) != relocated:
            artifacts[key] = relocated
            changed = True

    source_extra_logs = artifacts.get("extra_log_paths", repaired.get("extra_log_paths", []))
    relocated_extra_logs = [
        relocated
        for path in source_extra_logs
        if (
            relocated := _relocate_existing_artifact_path(
                path,
                old_session_dir=old_session_dir,
                new_session_dir=new_session_dir,
            )
        )
    ]
    if repaired.get("extra_log_paths") != relocated_extra_logs:
        repaired["extra_log_paths"] = relocated_extra_logs
        changed = True
    if artifacts.get("extra_log_paths") != relocated_extra_logs:
        artifacts["extra_log_paths"] = relocated_extra_logs
        changed = True
    repaired["artifacts"] = artifacts

    if changed:
        append_history_snapshot(project_dir, repaired, strict=True)


def _queue_run_id(queue_manifest: dict[str, Any]) -> str | None:
    run_id = str(queue_manifest.get("run_id") or "").strip()
    if run_id:
        return run_id
    for key in ("mirror_of", "checkpoint_path", "proof_of_work_path"):
        session_id = _session_id_from_artifact_path(queue_manifest.get(key))
        if session_id:
            return session_id
    return None


def _session_id_from_artifact_path(path_value: Any) -> str | None:
    text = str(path_value or "").strip()
    if not text:
        return None
    try:
        path = Path(text).expanduser()
        parts = path.parts
        index = parts.index(paths.SESSIONS_DIR_NAME)
    except (OSError, ValueError):
        return None
    if index + 1 >= len(parts):
        return None
    session_id = str(parts[index + 1]).strip()
    return session_id or None


def _relocate_existing_artifact_path(
    value: Any,
    *,
    old_session_dir: Path,
    new_session_dir: Path,
) -> str | None:
    relocated = _relocate_session_path(
        value,
        old_session_dir=old_session_dir,
        new_session_dir=new_session_dir,
    )
    if not relocated:
        return None
    try:
        path = Path(relocated).expanduser()
        return str(path.resolve()) if path.exists() else None
    except OSError:
        return None


def _relocate_session_path(
    value: Any,
    *,
    old_session_dir: Path,
    new_session_dir: Path,
) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    src_path = Path(text).expanduser()
    normalized_old = old_session_dir.expanduser().resolve()
    normalized_new = new_session_dir.expanduser().resolve()
    if src_path.is_absolute():
        normalized_src = src_path.resolve(strict=False)
        try:
            rel = normalized_src.relative_to(normalized_old)
        except ValueError:
            return str(src_path)
        return str((normalized_new / rel).resolve())
    return str((normalized_new / src_path).resolve())


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return data


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
