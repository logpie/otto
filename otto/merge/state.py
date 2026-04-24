"""Persisted merge-run state for Otto's consolidated merge flow.

`<project>/otto_logs/merge/<merge-id>/state.json` records:
- target branch + sha at start
- branches in queue (in order)
- per-branch outcome
- optional manual-follow-up hints if the consolidated resolver fails

The file is used for reporting, debugging, and post-mortem inspection.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Literal

from otto import paths

MERGE_STATE_SCHEMA_VERSION = 1
BranchStatus = Literal[
    "merged",
    "merged_with_markers",
    "skipped",
    "conflict_resolved",
    "agent_giveup",
    "pending",
]


@dataclass
class BranchOutcome:
    """Result of merging one branch into target.

    Valid statuses:
    - `merged`
    - `merged_with_markers`
    - `skipped`
    - `conflict_resolved`
    - `agent_giveup`
    - `pending`
    """
    branch: str
    status: BranchStatus
    merge_commit: str | None = None   # SHA of the merge commit, when applicable
    agent_invoked: bool = False
    note: str | None = None


@dataclass
class MergeState:
    """Per-merge-run state. Lives at otto_logs/merge/<merge-id>/state.json."""
    schema_version: int = MERGE_STATE_SCHEMA_VERSION
    merge_id: str = ""
    started_at: str = ""
    finished_at: str | None = None
    target: str = ""                          # branch we're merging into
    target_head_before: str = ""              # SHA of target HEAD at start
    status: str = "running"
    terminal_outcome: str | None = None
    note: str | None = None
    branches_in_order: list[str] = field(default_factory=list)
    outcomes: list[BranchOutcome] = field(default_factory=list)
    # Manual follow-up hints if the merge stops after a consolidated failure:
    paused_at_index: int | None = None        # index into branches_in_order
    paused_branch: str | None = None
    paused_stage: str | None = None           # currently only "manual_fix_required"
    # Final verification:
    cert_run_id: str | None = None
    cert_passed: bool | None = None


def merge_dir(project_dir: Path, merge_id: str) -> Path:
    return paths.merge_dir(project_dir) / merge_id


def state_path(project_dir: Path, merge_id: str) -> Path:
    return merge_dir(project_dir, merge_id) / "state.json"


def write_state(project_dir: Path, state: MergeState) -> Path:
    """Atomic write of merge state.json."""
    path = state_path(project_dir, state.merge_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = asdict(state)
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=False))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def load_state(project_dir: Path, merge_id: str) -> MergeState:
    """Read merge state.json. Raises FileNotFoundError if missing."""
    path = state_path(project_dir, merge_id)
    if not path.exists():
        raise FileNotFoundError(f"merge state not found: {path}")
    data = json.loads(path.read_text())
    if data.get("schema_version") != MERGE_STATE_SCHEMA_VERSION:
        raise ValueError(
            f"{path}: schema_version mismatch (got {data.get('schema_version')!r})"
        )
    outcomes = [BranchOutcome(**o) for o in data.get("outcomes", [])]
    allowed_keys = {f.name for f in fields(MergeState)}
    filtered = {k: v for k, v in data.items() if k in allowed_keys}
    filtered["outcomes"] = outcomes
    return MergeState(**filtered)


def find_latest_merge_id(project_dir: Path) -> str | None:
    """Return the most recent merge_id with state.json present, or None."""
    merges_dir = paths.merge_dir(project_dir)
    if not merges_dir.exists():
        return None
    candidates = []
    for sub in merges_dir.iterdir():
        if not sub.is_dir():
            continue
        sp = sub / "state.json"
        if sp.exists():
            candidates.append((sp.stat().st_mtime, sub.name))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def new_merge_id() -> str:
    """Human-readable merge id: merge-<timestamp>-<pid>."""
    return f"merge-{int(time.time())}-{os.getpid()}"
