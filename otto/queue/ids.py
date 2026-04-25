"""Phase 2.2: queue task ID generation with permanent identity.

Slug from intent (or mode + counter). Dedup against ALL prior IDs in
queue.yml regardless of status — task IDs are permanent for the file's
lifetime. This prevents manifest path collisions and state-key shadowing
when a user re-enqueues an intent that matches a previously-removed task's
slug.

Reserved words (`ls`, `show`, `rm`, `cancel`, `run`) are refused to keep
CLI parsing unambiguous.

Defers slug normalization to `otto.branching.slugify_intent`, which handles
unicode/long/empty edge cases including the hash-suffix dedup.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from otto.branching import RESERVED_TASK_IDS, slugify_intent


def generate_task_id(
    *,
    intent: str | None,
    command: str,
    existing_ids: Iterable[str],
    explicit_as: str | None = None,
) -> str:
    """Pick a task ID. Returns a unique slug.

    Precedence:
      1. ``explicit_as`` if provided (validated, must not collide).
      2. Slug from intent.
      3. Fallback ``<command>-<seq>`` if intent is missing.

    Collisions get ``-2``, ``-3`` etc. appended. Permanent for queue lifetime.
    """
    existing = set(existing_ids)
    if explicit_as is not None:
        candidate = _normalise_explicit(explicit_as)
        if candidate in existing:
            raise ValueError(f"task id {candidate!r} already exists in queue")
        return candidate
    if intent and intent.strip():
        base = slugify_intent(intent)
    else:
        base = _next_command_seq(command, existing)
        # _next_command_seq already returns a free id; no further dedup needed
        return base
    return _dedup(base, existing)


def _normalise_explicit(value: str) -> str:
    """Validate an explicit task id from --as. Reject reserved + bad chars."""
    v = value.strip().lower()
    if not v:
        raise ValueError("explicit task id cannot be empty")
    if v in RESERVED_TASK_IDS:
        raise ValueError(
            f"task id {v!r} is reserved (would collide with `otto queue {v}` verb)"
        )
    # Same grammar as slugify output: lowercase alnum + hyphens
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", v):
        raise ValueError(
            f"task id {v!r} must match [a-z0-9]+(-[a-z0-9]+)*  (lowercase, "
            "alphanumeric + hyphens, no leading/trailing/consecutive hyphens)"
        )
    return v


def _next_command_seq(command: str, existing: set[str]) -> str:
    """For intent-less enqueue (e.g. `otto queue improve bugs`), return
    `<command>-N` where N is the next free integer."""
    base = command
    n = 1
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _dedup(base: str, existing: set[str]) -> str:
    """If `base` is already taken, return `base-2`, `base-3`, ..."""
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def validate_after_refs(
    *,
    after: Iterable[str],
    self_id: str,
    all_ids: Iterable[str],
) -> None:
    """Sanity-check `after` references at enqueue time.

    Raises ValueError if:
    - any referenced id doesn't exist in queue.yml (yet)
    - the task lists itself in after (trivial cycle)
    """
    after_list = list(after)
    if self_id in after_list:
        raise ValueError(f"task {self_id!r}: cannot depend on itself")
    known = set(all_ids)
    missing = [a for a in after_list if a not in known and a != self_id]
    if missing:
        raise ValueError(
            f"task {self_id!r}: after references unknown task(s): {missing!r}"
        )


def detect_cycles(
    *,
    edges: dict[str, list[str]],
) -> list[list[str]]:
    """Return all simple cycles in the dependency graph.

    `edges` maps task id → list of ids it depends on (after).
    Returns list of cycles (each a list of ids in dependency order).
    Empty list if acyclic.
    """
    cycles: list[list[str]] = []
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {n: WHITE for n in edges}
    parent: dict[str, str | None] = {n: None for n in edges}

    def visit(node: str) -> None:
        color[node] = GRAY
        for dep in edges.get(node, []):
            if dep not in color:
                # External reference — not a cycle by itself
                continue
            if color[dep] == GRAY:
                # Reconstruct cycle from parent chain
                cycle = [dep]
                cur = node
                while cur is not None and cur != dep:
                    cycle.append(cur)
                    cur = parent[cur]
                if cur == dep:
                    cycle.append(dep)
                cycles.append(list(reversed(cycle)))
            elif color[dep] == WHITE:
                parent[dep] = node
                visit(dep)
        color[node] = BLACK

    for node in edges:
        if color[node] == WHITE:
            visit(node)
    return cycles
