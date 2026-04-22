"""Phase 4.3: collect stories from per-branch manifests + proof-of-work.

For each branch being merged, find its manifest (queue or atomic), read
the proof_of_work_path from the manifest, and parse the stories. Returns
a flat list of stories tagged with their source branch.

The post-merge certifier uses this list to verify the merged story union.
Per-story pruning happens inline via the merge_context preamble rather
than a separate verification-plan step.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("otto.merge.stories")


def collect_stories_from_branches(
    *,
    project_dir: Path,
    branches: list[str],
    queue_task_lookup: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return a list of story dicts tagged with `source_branch`.

    `queue_task_lookup` maps branch name → queue task id, so we can find
    queue manifests at `<project>/otto_logs/queue/<task-id>/manifest.json`.
    For branches without a queue task entry (atomic-mode `otto build` /
    `otto improve`), we look for a manifest under `otto_logs/builds/*/`
    that records this branch.

    Manifests without a proof_of_work_path (e.g., crashed runs or branches
    not from otto) contribute zero stories and are noted in the log.
    """
    out: list[dict[str, Any]] = []
    for branch in branches:
        manifest = find_manifest_for_branch(
            project_dir=project_dir,
            branch=branch,
            queue_task_lookup=queue_task_lookup or {},
        )
        if manifest is None:
            logger.info("no manifest found for branch %s — skipping stories", branch)
            continue
        pow_path = manifest.get("proof_of_work_path")
        if not pow_path:
            logger.info("manifest for %s has no proof_of_work_path — skipping", branch)
            continue
        pow_file = Path(pow_path)
        if not pow_file.exists():
            logger.info("proof-of-work file missing for %s: %s", branch, pow_file)
            continue
        try:
            pow_data = json.loads(pow_file.read_text())
        except json.JSONDecodeError as exc:
            logger.warning("malformed proof-of-work for %s: %s", branch, exc)
            continue
        stories = pow_data.get("stories") or []
        for s in stories:
            entry = dict(s)
            entry["source_branch"] = branch
            out.append(entry)
    return out


def find_manifest_for_branch(
    *,
    project_dir: Path,
    branch: str,
    queue_task_lookup: dict[str, str],
) -> dict[str, Any] | None:
    """Try queue path first (deterministic), then scan atomic build dirs."""
    # Queue mode
    task_id = queue_task_lookup.get(branch)
    if task_id:
        p = project_dir / "otto_logs" / "queue" / task_id / "manifest.json"
        if p.exists():
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                pass
    # Atomic mode (new per-session layout): scan otto_logs/sessions/*/manifest.json
    sessions = project_dir / "otto_logs" / "sessions"
    if sessions.exists():
        for run_dir in sessions.iterdir():
            if not run_dir.is_dir():
                continue
            mp = run_dir / "manifest.json"
            if not mp.exists():
                continue
            try:
                m = json.loads(mp.read_text())
            except json.JSONDecodeError:
                continue
            if m.get("branch") == branch:
                return m
    # Legacy atomic mode: scan otto_logs/builds/*/manifest.json + certifier/*
    for legacy in (project_dir / "otto_logs" / "builds",
                   project_dir / "otto_logs" / "certifier"):
        if not legacy.exists():
            continue
        for run_dir in legacy.iterdir():
            if not run_dir.is_dir():
                continue
            mp = run_dir / "manifest.json"
            if not mp.exists():
                continue
            try:
                m = json.loads(mp.read_text())
            except json.JSONDecodeError:
                continue
            if m.get("branch") == branch:
                return m
    return None


def dedupe_stories(stories: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """De-duplicate stories by (name, summary). Returns (unique, dropped).

    "Later wins" — when two stories share the same identifier, keep the
    later one (later in the input list, i.e., later-merged branch).
    """
    seen: dict[str, dict[str, Any]] = {}
    dropped: list[dict[str, Any]] = []
    for s in stories:
        key = _story_key(s)
        if key in seen:
            dropped.append(seen[key])
        seen[key] = s
    return (list(seen.values()), dropped)


def _story_key(story: dict[str, Any]) -> str:
    name = story.get("name") or story.get("summary") or story.get("story_id") or ""
    return str(name).strip().lower()
