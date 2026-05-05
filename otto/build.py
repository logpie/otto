"""Build loop — Step 4 of the unified intent-to-product pipeline.

Reads an approved Spec, dispatches per-slice build agents, runs the
slice's checks, handles bounded retries with prompt-level reset, and
emits state events. Each slice's build agent is the same role
instantiated per slice in flight; on retry, the same agent (logically)
re-engages with a fresh conversation but its existing branch and
worktree state.

Build agents handle tasks → checks → fix retries in one logical session.
The merge step (Step 5 / `otto.merge_queue`) takes over when a slice
becomes a merge candidate.

Bounds:
- Per-group retries: 3 attempts (configurable via BuildBudget.per_group_retries_hard_cap;
  legacy alias `per_slice_retries_hard_cap` still accepted)
- Per-group wall budget: 30 min (BuildBudget.per_group_wall_s; legacy: per_slice_wall_s)
- Total repair budget: shared with audit retries (BuildBudget.total_repair_s)

`owned_paths` semantics — write-scope, not exclusion:
- Agents may *create* new files anywhere.
- Agents may *modify* existing files only if a path matches the slice's
  `owned_paths` globs.
- Modifying another slice's owned path is a scope violation; the attempt
  fails with a narrative pointing at the violating files.

For testability, agent invocation is abstracted via a `BuildAgentCallable`
protocol. The default implementation (`default_build_agent`) shells out
to `otto.agent.run_agent_with_timeout`; tests pass a mock instead.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Callable, Protocol

from otto.checks import Evidence, run_checks
from otto.spec_compile import CheckKind, Component, Feature, Group, Spec
from otto.spec_state import emit, is_group_aborted_by_user

logger = logging.getLogger("otto.build")


# ---------------------------------------------------------------------------
# Status + budgets
# ---------------------------------------------------------------------------


class GroupStatus(str, Enum):
    PENDING = "pending"  # deps not yet met, OR not yet started
    IN_PROGRESS = "in_progress"
    PASSING = "passing"  # all checks pass; merge candidate
    BLOCKED = "blocked"  # exceeded retries / budget
    FAILED_SCOPE = "failed_scope"  # scope violation; treated as blocked


def _default_per_group_retries_hard_cap() -> int:
    from otto import defaults
    return int(defaults.get("retries.check_loop.max_attempts_per_group"))


# Legacy alias — kept for any external import path; superseded by
# `_default_per_group_retries_hard_cap`.
_default_per_slice_retries_hard_cap = _default_per_group_retries_hard_cap


def _default_per_group_wall_s() -> int:
    from otto import defaults
    return int(defaults.get("retries.check_loop.timeout_per_attempt_s"))


_default_per_slice_wall_s = _default_per_group_wall_s


def _default_per_group_cost_usd() -> float:
    from otto import defaults
    return float(defaults.get("budgets.per_group_cost_usd"))


_default_per_slice_cost_usd = _default_per_group_cost_usd


def _default_total_repair_s() -> int:
    from otto import defaults
    return int(defaults.get("budgets.total_repair_wall_s"))


def _default_total_cost_usd() -> float:
    """Uncapped (None in defaults.py) becomes float('inf') for arithmetic."""
    from otto import defaults
    val = defaults.get("budgets.total_cost_usd")
    return float("inf") if val is None else float(val)


@dataclass(init=False)
class BuildBudget:
    """Bounds shared across the build loop and audit-driven repair.

    All numeric defaults are pulled from `otto/defaults.py` via
    `field(default_factory=...)`. This makes the values configurable
    via `otto.yaml` and CLI flags without touching call sites.

    Canonical field names use `per_group_*` (research §2 vocabulary).
    The legacy `per_slice_*` names remain as constructor kwargs and
    attribute aliases for back-compat — internal callers should prefer
    the canonical form.

    Bounds, in order of likely activation:
      - **Progress**: if attempt N's failure narrative matches attempt
        N-1 verbatim → STUCK, stop. (Most likely to fire first on real
        no-progress loops.)
      - **Cost**: cumulative group cost exceeds ``per_group_cost_usd``.
      - **Wall**: ``per_group_wall_s`` as backstop.
      - **Hard cap**: ``per_group_retries_hard_cap`` defends against
        runaway agents that keep producing different errors.
    """

    per_group_retries_hard_cap: int = field(default_factory=_default_per_group_retries_hard_cap)
    per_group_wall_s: int = field(default_factory=_default_per_group_wall_s)
    per_group_cost_usd: float = field(default_factory=_default_per_group_cost_usd)
    total_repair_s: int = field(default_factory=_default_total_repair_s)
    total_cost_usd: float = field(default_factory=_default_total_cost_usd)
    _spent_repair_s: float = 0.0
    _spent_cost_usd: float = 0.0

    def __init__(
        self,
        per_group_retries_hard_cap: int | None = None,
        per_group_wall_s: int | None = None,
        per_group_cost_usd: float | None = None,
        total_repair_s: int | None = None,
        total_cost_usd: float | None = None,
        _spent_repair_s: float = 0.0,
        _spent_cost_usd: float = 0.0,
        *,
        # Legacy aliases — accept the old per_slice_* names so existing
        # call sites and tests keep working. Passing both legacy and
        # canonical names for the same field raises TypeError.
        per_slice_retries_hard_cap: int | None = None,
        per_slice_wall_s: int | None = None,
        per_slice_cost_usd: float | None = None,
    ) -> None:
        def _pick(canonical: Any, legacy: Any, name: str, factory: Callable[[], Any]) -> Any:
            if canonical is not None and legacy is not None:
                raise TypeError(
                    f"BuildBudget: pass either {name} or its legacy alias, not both"
                )
            if canonical is not None:
                return canonical
            if legacy is not None:
                return legacy
            return factory()

        self.per_group_retries_hard_cap = _pick(
            per_group_retries_hard_cap,
            per_slice_retries_hard_cap,
            "per_group_retries_hard_cap",
            _default_per_group_retries_hard_cap,
        )
        self.per_group_wall_s = _pick(
            per_group_wall_s,
            per_slice_wall_s,
            "per_group_wall_s",
            _default_per_group_wall_s,
        )
        self.per_group_cost_usd = _pick(
            per_group_cost_usd,
            per_slice_cost_usd,
            "per_group_cost_usd",
            _default_per_group_cost_usd,
        )
        self.total_repair_s = (
            total_repair_s if total_repair_s is not None else _default_total_repair_s()
        )
        self.total_cost_usd = (
            total_cost_usd if total_cost_usd is not None else _default_total_cost_usd()
        )
        self._spent_repair_s = _spent_repair_s
        self._spent_cost_usd = _spent_cost_usd

    # -- Legacy attribute aliases (read + write) --------------------------
    # Keep `per_slice_*` working as both attribute reads and assignments
    # so any external code that mutates a BuildBudget directly still works.
    @property
    def per_slice_retries_hard_cap(self) -> int:
        return self.per_group_retries_hard_cap

    @per_slice_retries_hard_cap.setter
    def per_slice_retries_hard_cap(self, value: int) -> None:
        self.per_group_retries_hard_cap = value

    @property
    def per_slice_wall_s(self) -> int:
        return self.per_group_wall_s

    @per_slice_wall_s.setter
    def per_slice_wall_s(self, value: int) -> None:
        self.per_group_wall_s = value

    @property
    def per_slice_cost_usd(self) -> float:
        return self.per_group_cost_usd

    @per_slice_cost_usd.setter
    def per_slice_cost_usd(self, value: float) -> None:
        self.per_group_cost_usd = value

    def remaining_repair_s(self) -> float:
        return max(0.0, self.total_repair_s - self._spent_repair_s)

    def charge_repair(self, seconds: float) -> None:
        self._spent_repair_s += max(0.0, seconds)

    def charge_cost(self, dollars: float) -> None:
        self._spent_cost_usd += max(0.0, dollars)

    def remaining_total_cost_usd(self) -> float:
        return max(0.0, self.total_cost_usd - self._spent_cost_usd)


@dataclass
class GroupResult:
    """Per-slice outcome of the build loop."""

    group_id: str
    status: GroupStatus
    attempts: int
    branch: str
    worktree: Path
    last_evidence: list[Evidence] = field(default_factory=list)
    failure_narrative: str = ""
    scope_warnings: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    wall_s: float = 0.0


class ComponentStatus(str, Enum):
    """Lifecycle states for a Component build (research §2.6).

    Components are dispatched like Groups but produce no Feature verdict —
    they're shared infrastructure. Their lifecycle parallels Group's
    GroupStatus minus the verdict-bearing distinction.
    """
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PASSING = "passing"  # all checks pass; merge candidate
    BLOCKED = "blocked"  # exceeded retries / budget
    LANDED = "landed"


@dataclass
class ComponentResult:
    """Per-Component outcome of the build loop (research §2.6).

    Components are non-Feature dispatch units (WebSocket hub, search
    indexer, notification fan-out). They have owned_paths, dependencies,
    checks — but no audit verdict because they're verified transitively
    via the Features that consume them.
    """
    component_id: str
    status: ComponentStatus
    attempts: int
    branch: str
    worktree: Path
    last_evidence: list[Evidence] = field(default_factory=list)
    failure_narrative: str = ""
    scope_warnings: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    wall_s: float = 0.0


@dataclass
class BuildResult:
    """Aggregate result of run_build."""

    spec_session_dir: Path
    group_results: list[GroupResult] = field(default_factory=list)
    component_results: list[ComponentResult] = field(default_factory=list)
    total_cost_usd: float = 0.0
    total_wall_s: float = 0.0

    @property
    def all_passing(self) -> bool:
        return bool(self.group_results) and all(
            r.status == GroupStatus.PASSING for r in self.group_results
        )

    @property
    def passing_ids(self) -> list[str]:
        return [r.group_id for r in self.group_results if r.status == GroupStatus.PASSING]

    @property
    def blocked_ids(self) -> list[str]:
        return [
            r.group_id
            for r in self.group_results
            if r.status in (GroupStatus.BLOCKED, GroupStatus.FAILED_SCOPE)
        ]

    # A1b: Component accessors (research §2.6)
    @property
    def passing_component_ids(self) -> list[str]:
        return [
            r.component_id
            for r in self.component_results
            if r.status == ComponentStatus.PASSING
        ]

    @property
    def blocked_component_ids(self) -> list[str]:
        return [
            r.component_id
            for r in self.component_results
            if r.status == ComponentStatus.BLOCKED
        ]

    @property
    def all_components_passing(self) -> bool:
        """True if every Component reached PASSING status. Vacuously true
        when there are no Components in the spec.
        """
        return all(
            r.status == ComponentStatus.PASSING
            for r in self.component_results
        )


# ---------------------------------------------------------------------------
# Build agent abstraction (mockable)
# ---------------------------------------------------------------------------


@dataclass
class BuildAgentInput:
    """Input passed to a build-agent callable for one attempt on one slice.

    ``feature_id`` is the Layer 2 narrowing hook: when non-empty, the
    rendered prompt includes a "FIX ONLY THIS FEATURE" preamble pointing
    the build agent at one Feature inside the Group instead of the whole
    Group surface. Empty (default) means "build/repair the whole Group"
    — the original Phase A behaviour.
    """

    spec: Spec
    group: Group
    project_dir: Path
    worktree: Path
    branch: str
    attempt: int  # 1-indexed
    last_failure_narrative: str = ""  # empty on first attempt
    log_dir: Path | None = None  # if set, agent writes narrative there
    feature_id: str = ""  # Layer 2 narrowing: fix only this feature in the slice


@dataclass
class BuildAgentOutput:
    """What a build-agent callable returns after one attempt."""

    succeeded: bool  # the agent reported success (does NOT mean checks pass)
    cost_usd: float = 0.0
    wall_s: float = 0.0
    detail: str = ""  # short narrative of what happened


class BuildAgentCallable(Protocol):
    """Async callable signature for the per-slice build agent."""

    async def __call__(self, agent_input: BuildAgentInput) -> BuildAgentOutput:
        ...


# ---------------------------------------------------------------------------
# Slice readiness + scope enforcement
# ---------------------------------------------------------------------------


def ready_groups(
    spec: Spec,
    completed_ids: Iterable[str],
    in_progress_ids: Iterable[str] = (),
    skipped_ids: Iterable[str] = (),
) -> list[Group]:
    """Return slices whose deps are all in `completed_ids` and not in flight or skipped.

    Args:
        completed_ids: Slices that have *successfully* completed (deps satisfied).
        in_progress_ids: Slices currently running.
        skipped_ids: Slices that have terminally failed (BLOCKED / FAILED_SCOPE).
            Their deps are NOT considered satisfied for downstream slices.
            Downstream slices should be marked BLOCKED separately by the caller
            once their deps include any skipped id.

    `completed_ids` may contain Component ids — Groups whose deps include
    a Component id become ready once that Component's build PASSING.
    """
    completed = set(completed_ids)
    in_flight = set(in_progress_ids)
    skipped = set(skipped_ids)
    ready: list[Group] = []
    for s in spec.groups:
        if s.id in completed or s.id in in_flight or s.id in skipped:
            continue
        if all(dep in completed for dep in (s.dependencies or [])):
            ready.append(s)
    return ready


def ready_components(
    spec: Spec,
    completed_ids: Iterable[str],
    in_progress_ids: Iterable[str] = (),
    skipped_ids: Iterable[str] = (),
) -> list[Component]:
    """Return Components whose deps are all in `completed_ids` and not in flight or skipped.

    Mirrors `ready_groups` for Components (research §2.6, A1b.3).
    `completed_ids` is the union of completed Group ids AND completed
    Component ids — a Component may depend on a Group or another
    Component (cross-deps are id-only; the caller resolves the kind).
    """
    completed = set(completed_ids)
    in_flight = set(in_progress_ids)
    skipped = set(skipped_ids)
    ready: list[Component] = []
    for c in (spec.components or []):
        if c.id in completed or c.id in in_flight or c.id in skipped:
            continue
        if all(dep in completed for dep in (c.dependencies or [])):
            ready.append(c)
    return ready


def detect_scope_violations(
    group_obj: Group,
    spec: Spec,
    modified_paths: Iterable[str],
    *,
    project_root: Path | None = None,
) -> list[str]:
    """Return paths the slice modified that violate owned_paths write-scope.

    Rule (write-scope, not exclusion):
    - A path is allowed if it matches the slice's own `owned_paths` globs.
    - A path is allowed if it matches `spec.shared_scaffold` globs (legacy).
    - A path is allowed if it matches `spec.shared_paths` globs (A1b.4 —
      research §2.6: shared_paths are explicitly free for any Group or
      Component to add/modify; the merge queue serializes lands so
      simultaneous edits don't collide).
    - A path is allowed if it matches owned_paths of any slice in the
      slice's transitive deps. (Downstream slices extend foundations
      they depend on. Peers cannot trample each other.)
    - A path is allowed if it was newly created (file did not exist before).
    - Otherwise: violation if it matches a peer slice's `owned_paths`
      (a slice not in this slice's transitive deps).

    Newness is approximated: if `project_root` is provided, a path is
    "newly created" iff it does not currently exist on disk. In tests,
    callers pass `project_root=None` and we treat all paths as modifications
    (strictest).
    """
    own_globs = list(group_obj.owned_paths or [])
    # A1b.4: both shared_scaffold (legacy) and shared_paths (new) are
    # globally writeable by any unit (Group or Component).
    shared_globs = list(spec.shared_scaffold or []) + list(spec.shared_paths or [])
    # Transitive deps: every unit (Group or Component) this slice depends
    # on, recursively. A1b.4: Component owned_paths participate in the
    # peer-vs-dep partition exactly like Group owned_paths.
    transitive_dep_ids = _transitive_deps(group_obj.id, spec)
    dep_globs: list[str] = []
    peer_globs: list[str] = []
    for s in spec.groups:
        if s.id == group_obj.id:
            continue
        if s.id in transitive_dep_ids:
            dep_globs.extend(s.owned_paths or [])
        else:
            peer_globs.extend(s.owned_paths or [])
    for c in (spec.components or []):
        if c.id == group_obj.id:
            continue
        if c.id in transitive_dep_ids:
            dep_globs.extend(c.owned_paths or [])
        else:
            peer_globs.extend(c.owned_paths or [])

    violations: list[str] = []
    for raw in modified_paths:
        path = str(raw or "").strip()
        if not path:
            continue
        if _matches_any(path, own_globs):
            continue
        if _matches_any(path, shared_globs):
            continue
        if _matches_any(path, dep_globs):
            # Modifying a transitive dep's owned files is allowed —
            # downstream slices extend foundations they depend on.
            continue
        if not _matches_any(path, peer_globs):
            # Not under any slice's ownership — implicitly shared
            # (agents may add new top-level files like README.md).
            continue
        # Peer-slice ownership. Check if it's newly created.
        if project_root is not None:
            on_disk = (project_root / path).exists()
            if not on_disk:
                continue
        violations.append(path)
    return violations


def detect_dependency_scope_extensions(
    group_obj: Group,
    spec: Spec,
    modified_paths: Iterable[str],
) -> list[str]:
    """Return modified paths owned by transitive deps, not this Group.

    These edits are allowed by ``detect_scope_violations`` because
    downstream Groups often need to extend foundations. They are still
    important operator evidence: the Group modified more than its own
    declared ``owned_paths``.
    """
    own_globs = list(group_obj.owned_paths or [])
    shared_globs = list(spec.shared_scaffold or []) + list(spec.shared_paths or [])
    transitive_dep_ids = _transitive_deps(group_obj.id, spec)
    dep_globs: list[str] = []
    for s in spec.groups:
        if s.id in transitive_dep_ids:
            dep_globs.extend(s.owned_paths or [])
    for c in (spec.components or []):
        if c.id in transitive_dep_ids:
            dep_globs.extend(c.owned_paths or [])

    extensions: list[str] = []
    for raw in modified_paths:
        path = str(raw or "").strip()
        if not path:
            continue
        if _matches_any(path, own_globs) or _matches_any(path, shared_globs):
            continue
        if _matches_any(path, dep_globs):
            extensions.append(path)
    return extensions


def _transitive_deps(group_id: str, spec: Spec) -> set[str]:
    """Return all units `group_id` depends on, transitively (excluding self).

    Considers both Groups and Components — A1c.2 wires cross-deps via
    a single id namespace, so `_transitive_deps("group-a")` may return
    Component ids and vice-versa.
    """
    by_id: dict[str, object] = {s.id: s for s in spec.groups}
    for c in (spec.components or []):
        by_id.setdefault(c.id, c)
    seed = by_id.get(group_id)
    if seed is not None:
        seed_deps = list(getattr(seed, "deps", None) or getattr(seed, "dependencies", None) or [])
    else:
        seed_deps = []
    visited: set[str] = set()
    stack = list(seed_deps)
    while stack:
        dep = stack.pop()
        if dep in visited or dep == group_id:
            continue
        visited.add(dep)
        upstream = by_id.get(dep)
        if upstream is not None:
            up_deps = list(getattr(upstream, "deps", None) or getattr(upstream, "dependencies", None) or [])
            stack.extend(up_deps)
    return visited


def _matches_any(path: str, globs: list[str]) -> bool:
    from fnmatch import fnmatch

    for g in globs:
        text = str(g or "").strip()
        if not text:
            continue
        if fnmatch(path, text):
            return True
        # `**` recursive globs: fnmatch does not handle them; expand to two
        # patterns "x/**/y" → "x/*/y" + "x/y" + "x/*/*/y" up to 4 levels.
        # Pragmatic v1 — tests cover the common cases.
        if "**" in text:
            parts = text.split("**")
            # Pattern "a/**/b" matches a/b, a/*/b, a/*/*/b, etc.
            if len(parts) == 2:
                left, right = parts
                left = left.rstrip("/")
                right = right.lstrip("/")
                # Require one or more intermediate path components, including
                # zero (so a/**/b matches a/b)
                for depth in range(0, 6):
                    middle = "/".join(["*"] * depth) if depth else ""
                    if middle:
                        candidate = f"{left}/{middle}/{right}".replace("//", "/")
                    else:
                        candidate = f"{left}/{right}".replace("//", "/")
                    if fnmatch(path, candidate):
                        return True
    return False


# Path-segment patterns excluded from the no-progress hash. Pattern A
# fix: substring matching was leaky — `.log` matched `dialog.py`,
# `_s/` matched any path containing `_s/`. Match against PATH SEGMENTS
# (split on `/`), with explicit suffix matching for log extensions.
_HASH_NOISE_DIRS: frozenset[str] = frozenset({
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".tox",
    ".venv",
    "instance",
    ".otto",                # v2.2 amendment side-channel files
    "otto_logs",            # production session dir
    "_session",             # test session dir
    "_s",                   # alt test session dir
})
_HASH_NOISE_FILE_SUFFIXES: tuple[str, ...] = (
    ".log",
    ".pyc",
)
_HASH_NOISE_FILES: frozenset[str] = frozenset({
    ".coverage",
    "spec-state.jsonl",
})


def _is_hash_noise(path: str) -> bool:
    """Return True if `path` should be excluded from no-progress hashing.

    Splits on `/` and checks segments against directory/file sets, plus
    file-extension match. Strict — `.log` matches only files ending in
    `.log`, NOT `dialog.py`. `_s` matches only a directory segment named
    `_s`, NOT `_synthetic.py`.
    """
    if not path:
        return False
    segments = path.split("/")
    for seg in segments[:-1]:
        if seg in _HASH_NOISE_DIRS:
            return True
    last = segments[-1]
    if last in _HASH_NOISE_FILES:
        return True
    if last in _HASH_NOISE_DIRS:
        return True
    for suffix in _HASH_NOISE_FILE_SUFFIXES:
        if last.endswith(suffix):
            return True
    return False


def _hash_worktree_diff(worktree: Path) -> str:
    """Return a SHA-256 of the agent's tracked-file changes + new
    source files vs HEAD.

    Combines `git diff HEAD` (committed + unstaged tracked files) with
    the contents of any untracked files NOT matching common cache /
    build-artifact noise patterns. Used by the no-progress bound to
    detect agents that aren't making any real change between retries.

    Returns "" if git isn't available or the worktree isn't a repo.
    """
    import hashlib

    h = hashlib.sha256()
    try:
        diff = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
        )
        h.update(diff.stdout.encode("utf-8", errors="replace"))
        # Include untracked file contents (new files the agent created),
        # filtering common cache noise.
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
        )
        for path in sorted((untracked.stdout or "").splitlines()):
            if not path:
                continue
            if _is_hash_noise(path):
                continue
            file_path = worktree / path
            if file_path.is_file():
                try:
                    h.update(b"\x00")
                    h.update(path.encode("utf-8"))
                    h.update(b"\x00")
                    h.update(file_path.read_bytes())
                except OSError:
                    pass
    except (FileNotFoundError, OSError):
        return ""
    return h.hexdigest()


def _snapshot_worktree_files(worktree: Path) -> dict[str, tuple[int, int]]:
    """Snapshot (relative_path → (mtime_ns, size)) for all non-noise files.

    B3 fallback: when git isn't available (or the worktree isn't a
    repo), scope detection still needs to know which files the slice
    touched. A pre/post snapshot diff yields the modified set without
    relying on git.
    """
    import os
    out: dict[str, tuple[int, int]] = {}
    for root, dirs, files in os.walk(worktree, followlinks=False):
        # Skip noise dirs in-place so os.walk doesn't descend.
        dirs[:] = [d for d in dirs if not _is_hash_noise(d) and d != ".git"]
        for name in files:
            full = Path(root) / name
            try:
                rel = full.relative_to(worktree).as_posix()
            except ValueError:
                continue
            if _is_hash_noise(rel):
                continue
            try:
                st = full.stat()
            except OSError:
                continue
            out[rel] = (st.st_mtime_ns, st.st_size)
    return out


def _diff_snapshots(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> list[str]:
    """Files added or modified between two snapshots (B3 fallback)."""
    changed: list[str] = []
    for path, sig in after.items():
        if before.get(path) != sig:
            changed.append(path)
    return sorted(changed)


def _git_diff_modified_paths(worktree: Path, base_ref: str = "HEAD") -> list[str]:
    """Return paths the worktree has modified vs. base_ref (committed + uncommitted)."""
    try:
        committed = subprocess.run(
            ["git", "diff", "--name-only", base_ref],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
        )
        unstaged = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
        )
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []
    paths: list[str] = []
    for completed in (committed, unstaged, untracked):
        for line in (completed.stdout or "").splitlines():
            line = line.strip()
            if line and line not in paths:
                paths.append(line)
    return paths


# ---------------------------------------------------------------------------
# Pattern D — real per-slice branches
# ---------------------------------------------------------------------------
#
# In single-worktree mode (Phase A), the slice branch is the agent's
# isolation boundary: each slice's edits land on a fresh branch off
# `base_branch`, then merge_queue does a real `git merge` to integrate
# back. Sequential dispatch means we never need parallel worktrees;
# checking out per-slice branches in the shared worktree is enough.
#
# Why this matters: without real branches, every slice's "merge" was
# `git add -A && git commit` in the shared worktree against whatever
# state the previous slice left. The first over-reaching slice would
# leave its work in the worktree, and subsequent slices would see no
# diff (REDUNDANT) — which Pattern A made honest, but Pattern D fixes
# the architectural cause. With real branches, each slice starts from
# a clean base and can only contribute its own diff to its branch.


def _is_git_repo(worktree: Path) -> bool:
    """Cheap check: is `worktree` inside a git repo?"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=worktree, capture_output=True, text=True, check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def _ensure_clean_git_state(worktree: Path) -> bool:
    """Best-effort recovery: abort any in-progress merge/rebase/cherry-pick
    so subsequent `git checkout` and `git merge` can proceed.

    V9 fix: `git merge --abort` doesn't always run cleanly when called
    from inside Otto (observed in P2 — audit phase saw repeated
    "git is mid-MERGE_HEAD" because either a build-phase rogue merge
    by an LLM agent or some merge-queue path left MERGE_HEAD around).
    Without this, downstream branch ops fail and the audit fix-loop
    silently skips with no recovery (V10).

    Returns True if the worktree is now in a clean (no-mid-op) state,
    False if recovery couldn't complete.
    """
    if not _is_git_repo(worktree):
        return False
    git_dir_proc = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=worktree, capture_output=True, text=True, check=False,
    )
    if git_dir_proc.returncode != 0:
        return False
    gd = (worktree / git_dir_proc.stdout.strip()).resolve()
    # Abort each in-progress operation if its sentinel exists. The
    # abort commands are no-ops on a clean repo (they return non-zero
    # but don't corrupt state), so it's safe to call them defensively.
    if (gd / "MERGE_HEAD").exists():
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=worktree, capture_output=True, text=True, check=False,
        )
    if (gd / "REBASE_HEAD").exists() or (gd / "rebase-merge").exists() or (gd / "rebase-apply").exists():
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=worktree, capture_output=True, text=True, check=False,
        )
    if (gd / "CHERRY_PICK_HEAD").exists():
        subprocess.run(
            ["git", "cherry-pick", "--abort"],
            cwd=worktree, capture_output=True, text=True, check=False,
        )
    # Verify clean state now.
    for sentinel in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD"):
        if (gd / sentinel).exists():
            return False
    if (gd / "rebase-merge").exists() or (gd / "rebase-apply").exists():
        return False
    return True


def _setup_group_branch(worktree: Path, *, branch: str, parent_ref: str) -> bool:
    """Create or reset `branch` off `parent_ref`, then check it out.

    `parent_ref` is the git ref this slice should base its work on:
    - For a slice with no deps: base_branch (e.g., "main").
    - For a slice with deps: the last-built dep's branch tip
      (so the slice sees its dep's work and can build on top).

    Returns True on success, False if any git op failed (including
    "not a git repo"). On failure, callers should fall back to
    single-worktree behavior — silent corruption of the user's repo
    state is a worse outcome than a missing per-slice branch.

    Pattern D: this is the entry into a slice's isolation boundary.
    Every slice starts from a clean copy of its parent ref, so its
    only contribution is what its build agent writes on top.
    """
    if not _is_git_repo(worktree):
        return False
    # V9 fix: try to recover from any in-progress merge/rebase/cherry-pick
    # before doing branch ops. Used to silently skip with a warning, which
    # caused audit fix-loops to abandon BLOCKED slices without surfacing
    # WHY (V10).
    if not _ensure_clean_git_state(worktree):
        logger.warning(
            "git is mid-merge/rebase/cherry-pick in %s and abort failed; "
            "skipping slice branch setup",
            worktree,
        )
        return False
    # Verify parent_ref exists.
    parent_check = subprocess.run(
        ["git", "rev-parse", "--verify", parent_ref],
        cwd=worktree, capture_output=True, text=True, check=False,
    )
    if parent_check.returncode != 0:
        return False
    # Reset any uncommitted state so the new branch starts clean.
    subprocess.run(
        ["git", "reset", "--hard", "HEAD"],
        cwd=worktree, capture_output=True, text=True, check=False,
    )
    subprocess.run(
        # V18: also preserve user-owned project config files (otto.yaml,
        # intent.md). These are untracked at project root by default
        # but are essential inputs Otto reads each slice; clobbering
        # them between slices breaks subsequent build/merge/audit
        # phases that consult test_command etc.
        ["git", "clean", "-fdx",
         "-e", ".otto/", "-e", "_otto_*", "-e", "_session/", "-e", "otto_logs/",
         "-e", "otto.yaml", "-e", "intent.md"],
        cwd=worktree, capture_output=True, text=True, check=False,
    )
    # `git checkout -B <branch> <parent_ref>` creates or resets branch
    # to point at parent_ref and checks it out.
    co_slice = subprocess.run(
        ["git", "checkout", "-B", branch, parent_ref],
        cwd=worktree, capture_output=True, text=True, check=False,
    )
    return co_slice.returncode == 0


def _setup_group_branch_with_deps(
    worktree: Path,
    *,
    branch: str,
    primary_parent_ref: str,
    additional_dep_refs: list[str],
) -> bool:
    """V12 fix: set up a slice branch with multiple deps.

    Pattern D's original `_setup_group_branch(parent_ref=last_dep)` only
    follows ONE dep. For DAG specs where a slice has sibling deps from
    different branches (e.g. P5 SSG: `builder` depends on both
    `link_rewriting` and `feeds`, which are siblings off `rendering`),
    that loses sibling-dep code from the slice's branch. The build
    agent then writes code against incompatible API guesses, and the
    merge phase conflicts (V13 in P5).

    This helper:
      1. Creates/resets `branch` off `primary_parent_ref` (typically
         the deepest dep, last in topo order).
      2. For each `additional_dep_refs`: skip if already an ancestor
         of HEAD (its commits are reachable transitively); otherwise
         `git merge --no-edit` it in.
      3. On any merge conflict during step 2: abort the merge cleanly
         and return False (caller should fall back to single-parent
         setup via `_setup_group_branch`, or surface a build-time
         error if even that fails).

    The slice's branch ends up containing the union of all deps'
    contributions — exactly the integrated state the slice's build
    agent needs to write code against.
    """
    if not _setup_group_branch(worktree, branch=branch, parent_ref=primary_parent_ref):
        return False
    for dep_ref in additional_dep_refs:
        if not dep_ref or dep_ref == primary_parent_ref:
            continue
        is_ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", dep_ref, "HEAD"],
            cwd=worktree, capture_output=True, text=True, check=False,
        )
        if is_ancestor.returncode == 0:
            continue
        msg = f"i2p({branch.split('/')[-1]}): merge dep {dep_ref}"
        merge = subprocess.run(
            ["git", "merge", "--no-edit", "--no-ff", "-m", msg, dep_ref],
            cwd=worktree, capture_output=True, text=True, check=False,
        )
        if merge.returncode != 0:
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=worktree, capture_output=True, text=True, check=False,
            )
            logger.warning(
                "slice %s: dep-merge of %s into branch failed: rc=%d stdout=%r stderr=%r",
                branch, dep_ref, merge.returncode,
                (merge.stdout or "").strip()[:300],
                (merge.stderr or "").strip()[:300],
            )
            return False
    return True


def _commit_group_work(worktree: Path, *, group_id: str, branch: str) -> bool:
    """Stage and commit the slice's work to its branch.

    Called at the END of a slice's successful build, so the slice
    branch has a real commit (or no commit if there's no diff,
    surfaced as REDUNDANT downstream).

    Returns True if a commit was made or there were no changes;
    False on git failure.
    """
    if not _is_git_repo(worktree):
        return False
    # V14 fix: stage all user changes via plain `git add -A` (so the
    # project's .gitignore works normally), then DEFENSIVELY unstage
    # Otto's runtime artifact paths. Two steps because:
    #   - explicit pathspec exclusions on `git add` make git complain
    #     with rc=1 when the excluded path is also gitignored
    #     ("paths are ignored by one of your .gitignore files"), even
    #     though the staging of everything else succeeded;
    #   - users who DON'T gitignore `_session/` etc. would otherwise
    #     get Otto runtime artifacts committed into slice branches,
    #     causing sibling slices to commit divergent journal contents
    #     and merge phase to hit spurious conflicts on internal state.
    # `git reset HEAD -- <path>` unstages without affecting working
    # tree; safe even when the path isn't currently tracked.
    add = subprocess.run(
        ["git", "add", "-A"],
        cwd=worktree, capture_output=True, text=True, check=False,
    )
    if add.returncode != 0:
        return False
    for runtime_path in ("_session", "otto_logs", ".otto", ".playwright-cli"):
        subprocess.run(
            ["git", "reset", "HEAD", "--", runtime_path],
            cwd=worktree, capture_output=True, text=True, check=False,
        )
        subprocess.run(
            ["git", "rm", "--cached", "-rf", "--ignore-unmatch", "--quiet", runtime_path],
            cwd=worktree, capture_output=True, text=True, check=False,
        )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree, capture_output=True, text=True, check=False,
    )
    # V14 nuance: --porcelain reports BOTH staged changes (e.g. ` A foo`,
    # `M  bar`) and untracked files (`?? path`). We deliberately excluded
    # `_session/`, `otto_logs/`, etc. from staging — those still appear
    # as untracked. They must NOT count as "changes to commit" or
    # `git commit` fails with "nothing added". Filter to staged-only.
    staged_lines = [
        line for line in (status.stdout or "").splitlines()
        if line.strip() and not line.startswith("??")
    ]
    if not staged_lines:
        # Nothing to commit. Slice contributed no diff; merge_queue
        # will surface this as REDUNDANT.
        return True
    msg = f"i2p({group_id}): build slice on {branch}"
    commit = subprocess.run(
        ["git", "commit", "-q", "-m", msg, "--no-verify"],
        cwd=worktree, capture_output=True, text=True, check=False,
    )
    return commit.returncode == 0


# ---------------------------------------------------------------------------
# The build loop
# ---------------------------------------------------------------------------


async def run_build(
    spec: Spec,
    *,
    project_dir: Path,
    session_dir: Path,
    build_agent: BuildAgentCallable,
    base_url: str | None = None,
    budget: BuildBudget | None = None,
    base_branch: str = "main",
    branch_for_group: Callable[[Group], str] | None = None,
    worktree_for_group: Callable[[Group], Path] | None = None,
    on_state_change: Callable[[str, str, dict[str, Any]], None] | None = None,
    skip_components: Iterable[str] | None = None,
) -> BuildResult:
    """Execute the build loop for an approved Spec.

    Args:
        spec: The approved Spec.
        project_dir: Project root (git worktree top).
        session_dir: Session directory (where state.jsonl lives).
        build_agent: Callable that does one attempt at a slice. Tests pass
            a mock; production passes `default_build_agent`.
        base_url: If the slice's checks include ApiProbe / StateInvariant
            with HTTP, the build host's base URL.
        budget: Bounds; defaults to BuildBudget().
        branch_for_group: Branch naming. Default: ``i2p/<spec_session>/<group_id>``.
        worktree_for_group: Worktree resolution. Default: project_dir
            (single-worktree mode for v1; future: separate worktrees per slice).
        on_state_change: Optional hook called as (group_id, status, extra).
            Receives every status transition for testability and progress UI.

    Returns:
        BuildResult with per-slice outcomes.

    The build loop itself is sequential dispatch in v1 — slices that are
    ready run one at a time, in dep-topological order. Concurrency is a
    follow-up; the readiness logic is structured to support it.
    """
    budget = budget or BuildBudget()
    branch_for_group = branch_for_group or (
        lambda s: f"i2p/{session_dir.name}/{s.id}"
    )
    worktree_for_group = worktree_for_group or (lambda _s: project_dir)

    completed_ids: set[str] = set()
    blocked_ids: set[str] = set()
    results: list[GroupResult] = []
    component_results: list[ComponentResult] = []
    total_t0 = time.monotonic()
    total_cost = 0.0

    # Resume: skip ids the prior attempt already landed. We seed
    # completed_ids so dependent units become ready, and synthesise
    # PASSING result entries so render's slice/component accounting
    # still accounts for them honestly (cost=0, attempts=0, narrative
    # "resume: skipped — landed in a prior attempt").
    skip_set: set[str] = {str(s) for s in (skip_components or ())}
    if skip_set:
        spec_groups_by_id = {g.id: g for g in spec.groups}
        spec_components_by_id = {
            c.id: c for c in (getattr(spec, "components", None) or [])
        }
        for sid in skip_set:
            if sid in spec_groups_by_id:
                results.append(
                    GroupResult(
                        group_id=sid,
                        status=GroupStatus.PASSING,
                        attempts=0,
                        branch="",
                        worktree=project_dir,
                        failure_narrative="resume: skipped — landed in a prior attempt",
                    )
                )
                completed_ids.add(sid)
            elif sid in spec_components_by_id:
                component_results.append(
                    ComponentResult(
                        component_id=sid,
                        status=ComponentStatus.PASSING,
                        attempts=0,
                        branch="",
                        worktree=project_dir,
                        failure_narrative="resume: skipped — landed in a prior attempt",
                    )
                )
                completed_ids.add(sid)
            else:
                logger.warning(
                    "resume: skip_components contains id %r that is "
                    "not in spec.groups or spec.components — ignoring",
                    sid,
                )

    def _emit_state(group_id: str, status: GroupStatus, extra: dict[str, Any] | None = None) -> None:
        # Map our GroupStatus to the journal's recognized event kinds.
        # IN_PROGRESS → slice.started; PASSING → slice.merge.eligible
        # (slice is now a merge candidate); BLOCKED / FAILED_SCOPE → slice.blocked.
        # PENDING does not emit (no journal event before slice.started).
        kind_map = {
            GroupStatus.IN_PROGRESS: "group.started",
            GroupStatus.PASSING: "group.merge.eligible",
            GroupStatus.BLOCKED: "group.blocked",
            GroupStatus.FAILED_SCOPE: "group.blocked",
        }
        kind = kind_map.get(status)
        if kind is not None:
            payload = dict(extra or {})
            detail = str(payload.pop("narrative", ""))
            attempt = int(payload.pop("attempts", 0) or 0)
            try:
                emit(session_dir, kind, group_id=group_id, attempt=attempt, detail=detail, **payload)
            except OSError as exc:
                logger.warning("emit %s failed: %s", kind, exc)
        if on_state_change is not None:
            on_state_change(group_id, status.value, extra or {})

    def _emit_component_state(component_id: str, status: ComponentStatus, extra: dict[str, Any] | None = None) -> None:
        # Components reuse the slice.* event-kind vocabulary — the id
        # namespace is unified (A1c.2), and downstream consumers
        # (mission control, render) already consume slice.* events keyed
        # by id. We tag the event with `component_id=` in extras so
        # consumers that care can distinguish.
        kind_map = {
            ComponentStatus.IN_PROGRESS: "group.started",
            ComponentStatus.PASSING: "group.merge.eligible",
            ComponentStatus.BLOCKED: "group.blocked",
        }
        kind = kind_map.get(status)
        if kind is not None:
            payload = dict(extra or {})
            detail = str(payload.pop("narrative", ""))
            attempt = int(payload.pop("attempts", 0) or 0)
            payload.setdefault("component_id", component_id)
            try:
                emit(session_dir, kind, group_id=component_id, attempt=attempt, detail=detail, **payload)
            except OSError as exc:
                logger.warning("emit %s failed: %s", kind, exc)
        if on_state_change is not None:
            on_state_change(component_id, status.value, extra or {})

    # Pattern D: track each completed slice's branch so dependent
    # slices can branch off them and see their work.
    branch_by_group: dict[str, str] = {}

    while True:
        ready = ready_groups(spec, completed_ids, skipped_ids=blocked_ids)
        ready_comps = ready_components(spec, completed_ids, skipped_ids=blocked_ids)
        if not ready and not ready_comps:
            break
        # A1b.3: dispatch Groups first (when any are ready) for stable
        # ordering of existing tests; otherwise dispatch a Component.
        # Both unit kinds share the same dep-readiness gate, so this is
        # equivalent to a single-pick scheduler.
        if not ready and ready_comps:
            next_component = ready_comps[0]
            comp_branch = branch_for_group(_component_as_slice(next_component))
            comp_worktree = worktree_for_group(_component_as_slice(next_component))
            primary_parent_ref_c: str
            additional_dep_refs_c: list[str] = []
            comp_deps = list(next_component.dependencies or [])
            if comp_deps:
                primary_parent_ref_c = branch_by_group.get(comp_deps[-1], base_branch)
                additional_dep_refs_c = [
                    branch_by_group[d]
                    for d in comp_deps[:-1]
                    if d in branch_by_group
                ]
            else:
                primary_parent_ref_c = base_branch
            if additional_dep_refs_c:
                branch_real_c = _setup_group_branch_with_deps(
                    comp_worktree, branch=comp_branch,
                    primary_parent_ref=primary_parent_ref_c,
                    additional_dep_refs=additional_dep_refs_c,
                )
                if not branch_real_c:
                    branch_real_c = _setup_group_branch(
                        comp_worktree, branch=comp_branch, parent_ref=primary_parent_ref_c,
                    )
            else:
                branch_real_c = _setup_group_branch(
                    comp_worktree, branch=comp_branch, parent_ref=primary_parent_ref_c,
                )
            _emit_component_state(
                next_component.id, ComponentStatus.IN_PROGRESS,
                {"branch": comp_branch, "branch_real": branch_real_c},
            )
            comp_result = await _run_component(
                spec=spec,
                component=next_component,
                project_dir=project_dir,
                worktree=comp_worktree,
                branch=comp_branch,
                session_dir=session_dir,
                build_agent=build_agent,
                base_url=base_url,
                budget=budget,
            )
            if branch_real_c and comp_result.status == ComponentStatus.PASSING:
                committed_c = _commit_group_work(
                    comp_worktree, group_id=next_component.id, branch=comp_branch,
                )
                if committed_c:
                    branch_by_group[next_component.id] = comp_branch
                else:
                    logger.warning(
                        "component %s: failed to commit work to branch %s — marking BLOCKED",
                        next_component.id, comp_branch,
                    )
                    import dataclasses as _dc
                    comp_result = _dc.replace(
                        comp_result,
                        status=ComponentStatus.BLOCKED,
                        failure_narrative=(
                            comp_result.failure_narrative
                            or f"failed to commit work to component branch {comp_branch}"
                        ),
                    )
            total_cost += comp_result.cost_usd
            component_results.append(comp_result)
            if comp_result.status == ComponentStatus.PASSING:
                completed_ids.add(next_component.id)
            else:
                blocked_ids.add(next_component.id)
            _emit_component_state(
                next_component.id,
                comp_result.status,
                {
                    "attempts": comp_result.attempts,
                    "wall_s": comp_result.wall_s,
                    "cost_usd": comp_result.cost_usd,
                    "narrative": comp_result.failure_narrative,
                },
            )
            continue
        # Sequential v1: pick the first ready slice. Stable ordering = spec order.
        next_group = ready[0]
        group_branch = branch_for_group(next_group)
        group_worktree = worktree_for_group(next_group)

        # Pattern D: choose the parent ref based on deps. A slice with
        # no deps branches off `base_branch`. A slice with deps branches
        # off the LAST dep's tip — so it sees that dep's work AND
        # (transitively) all earlier deps via that dep's own branch
        # ancestry. V12 fix: when the slice has multiple deps that may
        # be siblings (DAG topology, not linear chain), the last-dep
        # primary parent doesn't include sibling deps. Merge the rest
        # in via `_setup_group_branch_with_deps` so the slice branch
        # contains the integrated state of ALL its deps.
        primary_parent_ref: str
        additional_dep_refs: list[str] = []
        if next_group.dependencies:
            primary_parent_ref = branch_by_group.get(next_group.dependencies[-1], base_branch)
            additional_dep_refs = [
                branch_by_group[d]
                for d in next_group.dependencies[:-1]
                if d in branch_by_group
            ]
        else:
            primary_parent_ref = base_branch

        # Create a real per-slice branch so the slice's edits
        # accumulate on its own branch — no taint from peer slices,
        # real merges downstream. Falls back gracefully if the worktree
        # isn't a git repo (tests, certain bench fixtures); the build
        # still works, just without branch isolation.
        if additional_dep_refs:
            branch_real = _setup_group_branch_with_deps(
                group_worktree, branch=group_branch,
                primary_parent_ref=primary_parent_ref,
                additional_dep_refs=additional_dep_refs,
            )
            # Fall back to single-parent setup if the dep-merge produced a
            # conflict — preserves the build run instead of failing the
            # slice purely from V12. The slice will then fail at merge time
            # with an honest conflict, surfaced via V4.
            if not branch_real:
                logger.warning(
                    "slice %s: multi-dep branch setup failed (likely "
                    "sibling-dep conflict); falling back to single-parent",
                    next_group.id,
                )
                branch_real = _setup_group_branch(
                    group_worktree, branch=group_branch, parent_ref=primary_parent_ref,
                )
        else:
            branch_real = _setup_group_branch(
                group_worktree, branch=group_branch, parent_ref=primary_parent_ref,
            )
        if not branch_real:
            logger.info(
                "slice %s: per-slice branch setup skipped (not a git repo or "
                "base branch missing); using single-worktree mode",
                next_group.id,
            )
        _emit_state(
            next_group.id, GroupStatus.IN_PROGRESS,
            {"branch": group_branch, "branch_real": branch_real},
        )

        slice_result = await _run_slice(
            spec=spec,
            group_obj=next_group,
            project_dir=project_dir,
            worktree=group_worktree,
            branch=group_branch,
            session_dir=session_dir,
            build_agent=build_agent,
            base_url=base_url,
            budget=budget,
        )

        # Pattern D: commit the slice's work to its branch so merge_queue
        # can do a real `git merge`. Only run when branch setup succeeded.
        # B2/B4 fix: if the commit fails, the slice branch may be empty
        # or dirty — downstream slices MUST NOT branch off it. Mark
        # the slice BLOCKED, do NOT add to branch_by_group, and emit
        # a blocked event so resume reconstructs reality.
        if branch_real and slice_result.status == GroupStatus.PASSING:
            committed = _commit_group_work(
                group_worktree, group_id=next_group.id, branch=group_branch,
            )
            if committed:
                branch_by_group[next_group.id] = group_branch
            else:
                logger.warning(
                    "slice %s: failed to commit work to branch %s — marking BLOCKED",
                    next_group.id, group_branch,
                )
                import dataclasses as _dc
                slice_result = _dc.replace(
                    slice_result,
                    status=GroupStatus.BLOCKED,
                    failure_narrative=(
                        slice_result.failure_narrative
                        or f"failed to commit work to slice branch {group_branch}"
                    ),
                )

        total_cost += slice_result.cost_usd
        results.append(slice_result)
        if slice_result.status == GroupStatus.PASSING:
            completed_ids.add(next_group.id)
        else:
            blocked_ids.add(next_group.id)
        _emit_state(
            next_group.id,
            slice_result.status,
            {
                "attempts": slice_result.attempts,
                "wall_s": slice_result.wall_s,
                "cost_usd": slice_result.cost_usd,
                "narrative": slice_result.failure_narrative,
            },
        )

    # Mark slices that never ran (because a dep was blocked) as PENDING+blocked.
    pending_unreachable = [
        s
        for s in spec.groups
        if s.id not in completed_ids and s.id not in blocked_ids
    ]
    for s in pending_unreachable:
        results.append(
            GroupResult(
                group_id=s.id,
                status=GroupStatus.BLOCKED,
                attempts=0,
                branch="",
                worktree=project_dir,
                failure_narrative="dep blocked",
            )
        )
        _emit_state(s.id, GroupStatus.BLOCKED, {"narrative": "dep blocked"})

    # A1b.3: Components that never ran (because a dep was blocked) are
    # also recorded as BLOCKED, mirroring Group dep-block propagation.
    pending_unreachable_components = [
        c
        for c in (spec.components or [])
        if c.id not in completed_ids and c.id not in blocked_ids
        and c.id not in {r.component_id for r in component_results}
    ]
    for c in pending_unreachable_components:
        component_results.append(
            ComponentResult(
                component_id=c.id,
                status=ComponentStatus.BLOCKED,
                attempts=0,
                branch="",
                worktree=project_dir,
                failure_narrative="dep blocked",
            )
        )
        _emit_component_state(c.id, ComponentStatus.BLOCKED, {"narrative": "dep blocked"})

    return BuildResult(
        spec_session_dir=session_dir,
        group_results=results,
        component_results=component_results,
        total_cost_usd=total_cost,
        total_wall_s=time.monotonic() - total_t0,
    )


def _spec_edit_invalidation_reason(session_dir: Path, group_id: str) -> str:
    """Return the reason string from the most recent
    `group.invalidated_by_spec_edit` event for ``group_id``, or "" if
    none exist.

    A6: read each attempt by streaming the journal — cheap, append-only.
    Honest about silent failure: if the journal can't be read, we
    return "" rather than erroring out (the build loop must keep going
    on the agent's existing work).
    """
    journal = session_dir / "spec-state.jsonl"
    if not journal.exists():
        return ""
    try:
        with journal.open("r", encoding="utf-8") as fh:
            lines = list(fh)
    except OSError:
        return ""
    # Walk backwards: latest invalidation wins. We don't dedupe earlier
    # ones — every emit is a fresh signal — but the latest reason is
    # the most informative.
    import json as _json
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = _json.loads(raw)
        except _json.JSONDecodeError:
            continue
        if (
            payload.get("kind") == "group.invalidated_by_spec_edit"
            and str(payload.get("group_id") or "") == group_id
        ):
            return str(payload.get("detail") or "spec edit")
    return ""


async def _run_slice(
    *,
    spec: Spec,
    group_obj: Group,
    project_dir: Path,
    worktree: Path,
    branch: str,
    session_dir: Path,
    build_agent: BuildAgentCallable,
    base_url: str | None,
    budget: BuildBudget,
) -> GroupResult:
    """Run one slice through tasks→checks→fix retries.

    Returns GroupResult with PASSING / BLOCKED / FAILED_SCOPE.
    """
    slice_t0 = time.monotonic()
    last_failure = ""
    last_evidence: list[Evidence] = []
    accumulated_scope_warnings: list[str] = []
    cost_total = 0.0
    attempt = 0
    raw_log_dir = session_dir / "build" / group_obj.id
    # B3 fix: pre-slice snapshot of files for scope detection fallback.
    # When git is unavailable, we can't `git diff` to find modified
    # files, but a pre/post snapshot diff still gives us the truth.
    # In a git repo, this is cheap-but-unused (the git path wins).
    pre_slice_snapshot = _snapshot_worktree_files(worktree)
    # For progress detection: hash of the agent's diff vs base after
    # each attempt. If two consecutive attempts produce the same diff
    # hash, the agent isn't changing anything — stuck. This is stronger
    # than comparing check failure messages (which can be identical
    # while the agent makes incremental progress).
    prior_diff_hash: str = ""
    current_diff_hash: str = ""

    while attempt < budget.per_group_retries_hard_cap:
        attempt += 1
        elapsed = time.monotonic() - slice_t0

        # A7: operator-initiated abort. If the journal carries a
        # `group.aborted_by_user` event for THIS group, exit the retry
        # loop early as BLOCKED. The merge queue treats aborted ids as
        # BLOCKED (skipping merge), so the run continues with other
        # groups. We check before issuing each attempt so an abort fired
        # mid-retry cleanly stops on the next iteration.
        if is_group_aborted_by_user(session_dir, group_obj.id):
            return GroupResult(
                group_id=group_obj.id,
                status=GroupStatus.BLOCKED,
                attempts=attempt - 1,
                branch=branch,
                worktree=worktree,
                last_evidence=last_evidence,
                failure_narrative="aborted_by_user",
                scope_warnings=list(accumulated_scope_warnings),
                cost_usd=cost_total,
                wall_s=time.monotonic() - slice_t0,
            )

        # A6: mid-build spec edit invalidation. If the journal carries a
        # `group.invalidated_by_spec_edit` event for THIS group, the
        # spec the agent has been building against is stale. Abort the
        # in-place attempt; do not commit the worktree. The runner
        # decides whether to re-dispatch (currently: yes, once per run).
        invalidation_reason = _spec_edit_invalidation_reason(
            session_dir, group_obj.id
        )
        if invalidation_reason:
            return GroupResult(
                group_id=group_obj.id,
                status=GroupStatus.BLOCKED,
                attempts=attempt - 1,
                branch=branch,
                worktree=worktree,
                last_evidence=last_evidence,
                failure_narrative=(
                    f"invalidated by spec edit: {invalidation_reason}"
                ),
                scope_warnings=list(accumulated_scope_warnings),
                cost_usd=cost_total,
                wall_s=time.monotonic() - slice_t0,
            )

        # Bound 1: progress. If the agent's diff at end of attempt N
        # matches attempt N-1, the agent isn't producing any work →
        # stop. Strong signal: even an agent making partial progress
        # will produce a different diff between attempts.
        if (
            attempt > 2
            and current_diff_hash
            and current_diff_hash == prior_diff_hash
        ):
            return GroupResult(
                group_id=group_obj.id,
                status=GroupStatus.BLOCKED,
                attempts=attempt - 1,
                branch=branch,
                worktree=worktree,
                last_evidence=last_evidence,
                failure_narrative=(
                    f"no progress: agent produced identical diff on attempts "
                    f"{attempt - 2} and {attempt - 1}; stuck"
                ),
                scope_warnings=list(accumulated_scope_warnings),
                cost_usd=cost_total,
                wall_s=time.monotonic() - slice_t0,
            )

        # Bound 2: per-group cost ceiling.
        if cost_total >= budget.per_group_cost_usd:
            return GroupResult(
                group_id=group_obj.id,
                status=GroupStatus.BLOCKED,
                attempts=attempt - 1,
                branch=branch,
                worktree=worktree,
                last_evidence=last_evidence,
                failure_narrative=(
                    f"per-group cost ceiling reached "
                    f"(${cost_total:.2f} >= ${budget.per_group_cost_usd:.2f})"
                ),
                scope_warnings=list(accumulated_scope_warnings),
                cost_usd=cost_total,
                wall_s=time.monotonic() - slice_t0,
            )

        # Bound 3: total run cost (shared with audit).
        if budget.remaining_total_cost_usd() <= 0 and attempt > 1:
            return GroupResult(
                group_id=group_obj.id,
                status=GroupStatus.BLOCKED,
                attempts=attempt - 1,
                branch=branch,
                worktree=worktree,
                last_evidence=last_evidence,
                failure_narrative=(
                    f"total run cost ceiling reached "
                    f"(${budget._spent_cost_usd:.2f} >= ${budget.total_cost_usd:.2f})"
                ),
                scope_warnings=list(accumulated_scope_warnings),
                cost_usd=cost_total,
                wall_s=time.monotonic() - slice_t0,
            )

        # Bound 4: per-group wall budget (backstop).
        if elapsed >= budget.per_group_wall_s:
            return GroupResult(
                group_id=group_obj.id,
                status=GroupStatus.BLOCKED,
                attempts=attempt - 1,
                branch=branch,
                worktree=worktree,
                last_evidence=last_evidence,
                failure_narrative=f"per-slice wall budget exhausted after {elapsed:.0f}s",
                scope_warnings=list(accumulated_scope_warnings),
                cost_usd=cost_total,
                wall_s=elapsed,
            )

        # Bound 5: total repair budget (shared with audit).
        if budget.remaining_repair_s() <= 0 and attempt > 1:
            return GroupResult(
                group_id=group_obj.id,
                status=GroupStatus.BLOCKED,
                attempts=attempt - 1,
                branch=branch,
                worktree=worktree,
                last_evidence=last_evidence,
                failure_narrative="total repair budget exhausted (audit + build)",
                scope_warnings=list(accumulated_scope_warnings),
                cost_usd=cost_total,
                wall_s=time.monotonic() - slice_t0,
            )

        prior_diff_hash = current_diff_hash

        attempt_t0 = time.monotonic()
        agent_input = BuildAgentInput(
            spec=spec,
            group=group_obj,
            project_dir=project_dir,
            worktree=worktree,
            branch=branch,
            attempt=attempt,
            last_failure_narrative=last_failure,
            log_dir=raw_log_dir,
        )

        # v2 phase 4 (observability): archive the rendered prompt
        # alongside the agent's narrative log so post-hoc review can
        # answer "did the agent see X instruction?" without parsing
        # messages.jsonl. Cheap; one file per attempt.
        try:
            prompt_dir = session_dir / "build" / group_obj.id / f"attempt-{attempt:02d}"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            (prompt_dir / "prompt.md").write_text(
                _build_agent_prompt(agent_input), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("failed to archive prompt for %s attempt %d: %s",
                           group_obj.id, attempt, exc)
        try:
            agent_output = await build_agent(agent_input)
        except Exception as exc:
            last_failure = f"agent crashed on attempt {attempt}: {type(exc).__name__}: {exc}"
            attempt_wall = time.monotonic() - attempt_t0
            if attempt > 1:
                budget.charge_repair(attempt_wall)
            emit(
                session_dir,
                "group.attempt.failed",
                group_id=group_obj.id,
                attempt=attempt,
                detail=last_failure,
            )
            continue

        cost_total += agent_output.cost_usd
        budget.charge_cost(agent_output.cost_usd)
        attempt_wall = time.monotonic() - attempt_t0
        if attempt > 1:
            budget.charge_repair(attempt_wall)

        if not agent_output.succeeded:
            last_failure = agent_output.detail or "agent reported failure"
            emit(
                session_dir,
                "group.attempt.failed",
                group_id=group_obj.id,
                attempt=attempt,
                detail=last_failure,
            )
            continue

        # v2.2 amendment side-channel: did the agent write
        # `.otto/amendment_request.json`? If so, validate via
        # request_amendment, mutate spec in place, persist. The amended
        # state takes effect for THIS attempt's scope check.
        from otto.spec_amend import consume_amendment_request

        amended_spec, amendment_result = consume_amendment_request(
            worktree, spec, group_id=group_obj.id, session_dir=session_dir
        )
        if amendment_result is not None:
            if amendment_result.accepted and amended_spec is not spec:
                # Apply in place: replace the matching slice in spec.groups
                # so subsequent slices in this build session see the change.
                for index, s in enumerate(spec.groups):
                    if s.id == group_obj.id:
                        spec.groups[index] = amended_spec.groups[index]
                        break
                spec.amendments.extend(
                    amended_spec.amendments[len(spec.amendments):]
                )
                # Refresh group_obj for downstream use this attempt.
                group_obj = next(s for s in spec.groups if s.id == group_obj.id)
                emit(
                    session_dir,
                    "amendment.applied",
                    group_id=group_obj.id,
                    attempt=attempt,
                    detail=(amendment_result.amendment.reason or "")[:200] if amendment_result.amendment else "",
                    trigger_event_id=amendment_result.amendment.trigger_event_id if amendment_result.amendment else "",
                )
            elif amendment_result.rejection is not None:
                emit(
                    session_dir,
                    "amendment.rejected",
                    group_id=group_obj.id,
                    attempt=attempt,
                    detail=amendment_result.rejection.message[:200],
                    code=amendment_result.rejection.code,
                )

        # Scope check: detect modifications outside the slice's declared
        # owned_paths + transitive deps + shared_scaffold. Soft-warning
        # mode: don't block the slice — just log the warnings and let
        # the slice's own checks + cross-slice checks + audit catch any
        # actual behavior regressions. A modification that crossed a
        # declared scope boundary is interesting documentation, not
        # automatically harmful.
        # B3 fix: try git first; if it returns no paths (likely because
        # we're in single-worktree fallback or not a repo), fall back
        # to filesystem-snapshot diff. Without a fallback, the first
        # over-reaching slice in a non-git fixture passes scope check
        # vacuously — exactly the symptom Pattern D was meant to fix.
        try:
            modified = _git_diff_modified_paths(worktree)
        except Exception as exc:
            modified = []
            logger.warning("git diff failed for %s: %s", group_obj.id, exc)
        if not modified and not _is_git_repo(worktree):
            post_snapshot = _snapshot_worktree_files(worktree)
            modified = _diff_snapshots(pre_slice_snapshot, post_snapshot)
            if modified:
                logger.info(
                    "slice %s: scope detection via filesystem snapshot "
                    "(%d modified path(s))",
                    group_obj.id, len(modified),
                )
        scope_warnings = detect_scope_violations(
            group_obj, spec, modified, project_root=worktree
        )
        for path in detect_dependency_scope_extensions(group_obj, spec, modified):
            if path not in scope_warnings:
                scope_warnings.append(path)
        if scope_warnings:
            logger.info(
                "group %s: scope warnings (%d path(s) outside own scope): %s",
                group_obj.id,
                len(scope_warnings),
                ", ".join(scope_warnings[:5]),
            )
            # v2.2: emit a dedicated scope.warning event with a stable
            # event_id so the slice agent can cite it in a follow-up
            # request_amendment call (e.g., to legitimately add the
            # peer to its deps list).
            emit(
                session_dir,
                "scope.warning",
                group_id=group_obj.id,
                attempt=attempt,
                detail=(
                    f"scope warning (non-blocking): modified {len(scope_warnings)} "
                    f"path(s) outside the slice's own owned_paths: "
                    f"{', '.join(scope_warnings[:5])}"
                ),
                paths=list(scope_warnings),
            )
            for w in scope_warnings:
                if w not in accumulated_scope_warnings:
                    accumulated_scope_warnings.append(w)

        # Run slice's deterministic checks.
        emit(
            session_dir,
            "group.check.started",
            group_id=group_obj.id,
            attempt=attempt,
        )
        evidence_pairs = run_checks(
            list(group_obj.checks),
            project_dir=project_dir,
            cwd=worktree,
            base_url=base_url,
            raw_log_dir=raw_log_dir / f"attempt-{attempt:02d}",
        )
        last_evidence = [ev for _check, ev in evidence_pairs]
        all_pass = all(ev.passed for ev in last_evidence)
        emit(
            session_dir,
            "group.check.finished",
            group_id=group_obj.id,
            attempt=attempt,
            detail=("pass" if all_pass else "fail"),
            details=[ev.detail for ev in last_evidence],
        )

        if all_pass:
            return GroupResult(
                group_id=group_obj.id,
                status=GroupStatus.PASSING,
                attempts=attempt,
                branch=branch,
                worktree=worktree,
                last_evidence=last_evidence,
                failure_narrative="",
                scope_warnings=list(accumulated_scope_warnings),
                cost_usd=cost_total,
                wall_s=time.monotonic() - slice_t0,
            )

        # Otherwise: prepare narrative for next attempt's prompt-level reset.
        failed_summaries = [ev.detail for ev in last_evidence if not ev.passed]
        last_failure = (
            f"checks failed on attempt {attempt}: " + "; ".join(failed_summaries[:5])
        )
        # Snapshot the agent's work-so-far for the no-progress bound.
        current_diff_hash = _hash_worktree_diff(worktree)
        emit(
            session_dir,
            "group.attempt.failed",
            group_id=group_obj.id,
            attempt=attempt,
            detail=last_failure,
        )

    # Out of retries.
    return GroupResult(
        group_id=group_obj.id,
        status=GroupStatus.BLOCKED,
        attempts=attempt,
        branch=branch,
        worktree=worktree,
        last_evidence=last_evidence,
        failure_narrative=last_failure or "exceeded per-slice retry budget",
        scope_warnings=list(accumulated_scope_warnings),
        cost_usd=cost_total,
        wall_s=time.monotonic() - slice_t0,
    )


# ---------------------------------------------------------------------------
# A1b.3 — Component dispatch (research §2.6)
# ---------------------------------------------------------------------------


def _component_as_slice(component: Component) -> Group:
    """Adapt a Component to the Slice surface so existing helpers
    (branch_for_group, worktree_for_group, BuildAgentInput) accept it.

    Components have no `tasks`; we synthesize a single task line from the
    component's description so the build agent has a concrete brief.
    Component dependencies map to Slice deps. Owned paths and checks
    pass through verbatim.
    """
    description = component.description or component.name
    return Group(
        id=component.id,
        name=component.name,
        feature_ids=[description] if description else [],
        dependencies=list(component.dependencies or []),
        owned_paths=list(component.owned_paths or []),
        checks=list(component.checks or []),
    )


async def _run_component(
    *,
    spec: Spec,
    component: Component,
    project_dir: Path,
    worktree: Path,
    branch: str,
    session_dir: Path,
    build_agent: BuildAgentCallable,
    base_url: str | None,
    budget: BuildBudget,
) -> ComponentResult:
    """Run one Component through tasks→checks→fix retries (research §2.6).

    Mirrors `_run_slice` but emits a `ComponentResult`. Components have
    no Feature verdict — they're shared infrastructure, audited
    transitively via the Features that consume them.
    """
    adapter = _component_as_slice(component)
    slice_result = await _run_slice(
        spec=spec,
        group_obj=adapter,
        project_dir=project_dir,
        worktree=worktree,
        branch=branch,
        session_dir=session_dir,
        build_agent=build_agent,
        base_url=base_url,
        budget=budget,
    )
    if slice_result.status == GroupStatus.PASSING:
        comp_status = ComponentStatus.PASSING
    else:
        comp_status = ComponentStatus.BLOCKED
    return ComponentResult(
        component_id=component.id,
        status=comp_status,
        attempts=slice_result.attempts,
        branch=slice_result.branch,
        worktree=slice_result.worktree,
        last_evidence=slice_result.last_evidence,
        failure_narrative=slice_result.failure_narrative,
        scope_warnings=slice_result.scope_warnings,
        cost_usd=slice_result.cost_usd,
        wall_s=slice_result.wall_s,
    )


# ---------------------------------------------------------------------------
# Default build-agent implementation
# ---------------------------------------------------------------------------


def _build_agent_prompt(agent_input: BuildAgentInput) -> str:
    """Compose the per-attempt prompt for the build agent.

    Pattern C fix: every section is explicitly scoped. Whole-product
    signals (intent, structure, done_means, contract test) are
    framed as CONTEXT — not the slice's responsibility. Slice-specific
    sections (tasks, scope, acceptance checks) are PRIMARY.

    The previous prompt mixed whole-product signals with slice-narrow
    signals without per-slice filtering. The first slice (no deps,
    foundational paths) read the whole-product surface as its personal
    checklist and built everything; subsequent slices found their work
    already done and no-op-merged.
    """
    s = agent_input.group
    spec = agent_input.spec
    lines: list[str] = []
    lines.append(f"# Build slice `{s.id}` — {s.name}")
    lines.append("")

    # === LAYER 2 NARROWING (when feature_id is set) ===
    # If this dispatch targets a single failing Feature within the Group,
    # surface a sharp "fix only this" preamble so the agent doesn't
    # re-touch passing siblings. The narrowing supplements (does not
    # replace) the Group's normal task/scope/checks framing — the agent
    # still needs the Group-level rules below to know its write-scope.
    if agent_input.feature_id:
        narrowed: Feature | None = next(
            (f for f in spec.features if f.id == agent_input.feature_id),
            None,
        )
        if narrowed is not None:
            lines.append("## FIX ONLY THE FAILING FEATURE")
            lines.append("")
            lines.append(
                f"This is a Layer 2 repair dispatch for feature "
                f"`{narrowed.name}` (id=`{narrowed.id}`) inside group "
                f"`{s.id}`. Other features in this group already passed "
                f"audit — DO NOT modify them or their behaviour. Touch "
                f"only the code paths required to make this one feature "
                f"pass its acceptance criteria."
            )
            if narrowed.description:
                lines.append("")
                lines.append(f"**Feature description**: {narrowed.description}")
            if narrowed.acceptance_detail:
                lines.append("")
                lines.append(
                    f"**Acceptance detail**: {narrowed.acceptance_detail}"
                )
            if agent_input.last_failure_narrative:
                lines.append("")
                lines.append("**Previous audit detail (why it failed)**:")
                lines.append(agent_input.last_failure_narrative)
            lines.append("")

    # === SLICE FRAMING (primary, slice-narrow) ===
    total = len(spec.groups)
    pos = next((i + 1 for i, sl in enumerate(spec.groups) if sl.id == s.id), 0)
    if total > 1:
        lines.append(
            f"You are slice **{pos} of {total}** (`{s.id}`) — one of "
            f"several agents collaborating on a multi-slice product. "
            f"Each slice owns a narrow vertical of the product; you are "
            f"NOT building the whole product. Other slices will deliver "
            f"the parts not in your task list."
        )
    else:
        lines.append(
            f"You are the only slice (`{s.id}`) in this build. Implement "
            f"the slice tasks below; nothing depends on you that you "
            f"don't see."
        )
    lines.append("")
    if s.dependencies:
        lines.append(f"This slice depends on (already landed): {', '.join(s.dependencies)}")
    elif total > 1:
        lines.append(
            f"This is a slice with no deps — your worktree is empty "
            f"except for project seed files. **You are NOT responsible "
            f"for whole-product features mentioned in the intent or "
            f"done_means below — those belong to slices {pos+1}..{total}.**"
        )
    lines.append("")

    # === TASKS (primary, the slice's job) ===
    lines.append("## What you must do (slice tasks — THIS is your job)")
    if s.feature_ids:
        for i, task in enumerate(s.feature_ids, 1):
            lines.append(f"  {i}. {task}")
    else:
        lines.append("  (no tasks declared — likely a structural slice; "
                     "create only files matching `## Scope` below)")
    lines.append("")

    # === SCOPE (primary, with own/dep/shared labeled separately) ===
    transitive_deps = _transitive_deps(s.id, spec)
    dep_owned: list[tuple[str, str]] = []
    peer_owned: list[tuple[str, str]] = []
    for other in spec.groups:
        if other.id == s.id:
            continue
        if other.id in transitive_deps:
            dep_owned.extend((other.id, g) for g in (other.owned_paths or []))
        else:
            peer_owned.extend((other.id, g) for g in (other.owned_paths or []))

    lines.append("## Scope (what you may write/modify)")
    lines.append("")
    if s.owned_paths:
        lines.append("**Yours (write here):**")
        for g in s.owned_paths:
            lines.append(f"  - `{g}`")
        lines.append("")
    if dep_owned:
        lines.append("**Dep-owned (extend if your tasks require it):**")
        for did, g in dep_owned:
            lines.append(f"  - `{g}` (owned by `{did}`)")
        lines.append("")
    if spec.shared_scaffold:
        lines.append("**Shared scaffold (any slice may extend):**")
        for g in spec.shared_scaffold:
            lines.append(f"  - `{g}`")
        lines.append("")
    if not s.owned_paths and not dep_owned and not spec.shared_scaffold:
        lines.append("(No declared paths — create only new files matching your tasks.)")
        lines.append("")
    lines.append(
        "**Hard rule**: do NOT write files outside the lists above. "
        "If you need to (e.g., to make a check pass), STOP and request "
        "an amendment via `.otto/amendment_request.json` rather than "
        "silently over-reaching."
    )
    lines.append("")
    # V16b: even when the scope check would allow modifying a transitive
    # dep's owned file (the "downstream slices extend foundations"
    # exception), specific FILE PATTERNS are extension points where
    # modification by sibling slices causes unrecoverable merge
    # conflicts. Spell this out so the agent treats them as read-only.
    lines.append(
        "**App entry points are read-only across slices.** Even if a "
        "transitive dep's `owned_paths` includes one of these — "
        "`app.py`, `app/__init__.py`, `wsgi.py`, `main.py`, `cli.py`, "
        "`models.py`, `db/__init__.py`, `routes.py`, `urls.py`, "
        "`server.py`, `index.ts`, `index.js`, `cmd/main.go` — DO NOT "
        "modify them. Multiple slices each editing the same entry-point "
        "to register their own routes/blueprints/models will hit a "
        "merge conflict that Otto's fix-loop cannot reliably resolve. "
        "Instead: the foundation slice provides a registration point "
        "(auto-discovery loop or explicit list); your slice creates a "
        "NEW file in your own subdirectory (e.g. `routes/<your_slice>.py`, "
        "`blueprints/<your_slice>.py`, `app/<your_feature>.py`) that "
        "exports `bp`/`router`/`Model` per the convention. If foundation "
        "didn't establish a registration point, request an amendment "
        "rather than editing the entry point yourself."
    )
    lines.append("")
    # V8 fix: build agents had unrestricted git/bash and one slice in
    # P2 ran `git merge other-slice-branch` to grab files from another
    # slice. That breaks branch isolation: the slice's own contribution
    # gets entangled with another's, downstream merges conflict, and
    # Otto's merge attribution is wrong. Forbid git mutations explicitly.
    lines.append("**Git is read-only for slice agents.**")
    lines.append(
        "You MAY run `git log`, `git show`, `git diff`, `git status`, "
        "`git ls-files` to inspect history. You MUST NOT run `git "
        "commit`, `git merge`, `git checkout`, `git rebase`, `git "
        "reset`, `git push`, `git stash`, `git cherry-pick`, `git "
        "branch -f/-D`, `git tag`, `git rm`, or any other command "
        "that mutates the repo's state. Otto manages branches, commits, "
        "and merges automatically. If you think you need a file that "
        "doesn't exist on your branch (e.g. another slice's source), "
        "create the file yourself within your scope OR request an "
        "amendment via `.otto/amendment_request.json` to widen your "
        "scope. Do NOT pull in another slice's branch via git merge."
    )
    lines.append("")

    # === SLICE ACCEPTANCE CHECKS (primary, narrow) ===
    lines.append("## Slice acceptance checks (your slice passes when these pass)")
    if s.checks:
        for i, c in enumerate(s.checks, 1):
            lines.append(f"  {i}. {_describe_check(c)}")
    else:
        lines.append("  (no checks declared — slice vacuously passes)")
    lines.append("")

    # === RETRY CONTEXT (if applicable) ===
    if agent_input.attempt > 1 and agent_input.last_failure_narrative:
        lines.append("## Previous attempt failed")
        lines.append(agent_input.last_failure_narrative)
        lines.append("")
        lines.append(
            "Re-read your slice tasks above. Make ONLY the changes needed "
            "to satisfy the slice's acceptance checks. Do NOT widen scope "
            "to make whole-product tests pass — that's other slices' job."
        )
        lines.append("")

    # === CONTEXT BLOCK (whole-product signals, clearly framed as informational) ===
    lines.append("---")
    lines.append("")
    lines.append("## Whole-product context (informational — NOT your responsibility)")
    lines.append("")
    lines.append(
        "Everything below describes the FULL product across ALL slices. "
        "It's here for context (so you understand how your slice fits "
        "the whole), NOT as your personal checklist. **Do NOT implement "
        "anything below that isn't in your slice tasks above.** Whole-"
        "product features are delivered by the combined work of all "
        "slices; the audit verifies the integrated product, not each "
        "slice individually."
    )
    lines.append("")

    # Original intent — collapsed under context.
    lines.append("### Original intent (whole product)")
    lines.append(f"> {spec.intent}")
    lines.append("")
    lines.append(f"`project_kind`: {spec.project_kind}")
    lines.append("")

    # done_means — explicitly cross-slice.
    if spec.done_means:
        lines.append("### Cross-slice done-means (combined product success criteria)")
        lines.append(
            "The audit checks these against the integrated product. "
            "Your slice contributes only items covered by your tasks above."
        )
        for item in spec.done_means:
            lines.append(f"  - {item}")
        lines.append("")

    # Contract surface — reframed: read for context, not "make it pass".
    contract_lines = _project_contract_summary(agent_input.project_dir)
    if contract_lines:
        lines.append("### Project contract surface (read for API/data shapes)")
        lines.append(
            "Existing contract files. Read these to learn API shapes "
            "(field names, request/response formats) so your slice "
            "doesn't drift. **Do NOT try to make the whole-product "
            "test pass yourself** — your slice's narrow check is "
            "above. The contract test runs at end-of-run against the "
            "integrated product."
        )
        lines.extend(contract_lines)
        lines.append("")

    # Project structure — filtered to slice's portion when possible.
    payload = (spec.structure.payload or {}) if spec.structure else {}
    if payload:
        lines.append("### Project structure (binding contracts)")
        lines.append(
            "Naming and shape decisions for the whole product. "
            "Reference these when writing code that touches them. "
            "Other slices use the same source of truth."
        )
        lines.append("```json")
        import json as _json
        lines.append(_json.dumps(payload, indent=2, sort_keys=True))
        lines.append("```")
        lines.append("")

    # Final instruction — reinforces narrowness.
    lines.append("---")
    lines.append("")
    lines.append(
        "**Final instruction**: Implement ONLY the tasks under `## What "
        "you must do` above, writing ONLY to paths under `## Scope`. "
        "If a whole-product feature in the context block isn't in your "
        "tasks, another slice will deliver it. When your slice's tasks "
        "are done and your acceptance checks pass, confirm completion."
    )
    return "\n".join(lines)


def _project_contract_summary(project_dir: Path) -> list[str]:
    """Surface contract-shaped files in the project root for the build prompt.

    Returns a list of bullet lines (already markdown-formatted) listing:
    * otto.yaml's test_command, if present
    * intent.md (first 2KB)
    * tests/run_acceptance.py and any tests/contract*.py / tests/conftest.py
      (paths only — agent reads them via Read tool)

    Empty list when nothing relevant is found.
    """
    bullets: list[str] = []
    # otto.yaml test_command
    yaml_path = project_dir / "otto.yaml"
    if yaml_path.is_file():
        try:
            from otto.config import load_config

            config = load_config(yaml_path)
            test_command = str(config.get("test_command") or "").strip()
            if test_command:
                bullets.append(
                    f"- **test_command** (otto.yaml): `{test_command}` — this is the "
                    f"contract test the audit will run at the end. Make it pass."
                )
        except Exception as exc:
            # Pattern F: log instead of silently passing. A malformed
            # otto.yaml that disappears from the prompt is a debugging
            # nightmare; at least leave a trail.
            logger.warning(
                "could not load otto.yaml for prompt instructions at %s: %s",
                yaml_path, exc,
            )
    # intent.md
    intent_md = project_dir / "intent.md"
    if intent_md.is_file():
        bullets.append(f"- **intent.md** at `{intent_md}` — read it for product intent")
    # Existing test / contract files
    contract_paths: list[Path] = []
    tests_dir = project_dir / "tests"
    if tests_dir.is_dir():
        for name in (
            "run_acceptance.py",
            "conftest.py",
            "test_contract.py",
            "test_acceptance.py",
        ):
            candidate = tests_dir / name
            if candidate.is_file():
                contract_paths.append(candidate)
    if contract_paths:
        bullets.append(
            "- **Existing test/contract files** — read these to learn the API "
            "shapes (request/response field names) the contract pins down:"
        )
        for p in contract_paths:
            bullets.append(f"    - `{p.relative_to(project_dir)}`")
    return bullets


def _describe_check(check: CheckKind) -> str:
    name = type(check).__name__
    if name == "RepoTestCheck":
        cmd = " ".join(getattr(check, "command", ()) or ())
        return f"RepoTestCheck: `{cmd}` exits 0"
    if name == "PytestCheck":
        return f"PytestCheck: pytest selector `{getattr(check, 'selector', '')}`"
    if name == "BrowserJourney":
        cmd = " ".join(getattr(check, "command", ()) or ())
        return f"BrowserJourney: `{cmd}` succeeds and produces evidence"
    if name == "ApiProbe":
        return (
            f"ApiProbe: {getattr(check, 'method', 'GET')} {getattr(check, 'path', '/')} "
            f"→ {getattr(check, 'expect_status', 200)}"
        )
    if name == "StateInvariant":
        return f"StateInvariant: {getattr(check, 'description', '') or getattr(check, 'expression', '')}"
    return name


async def default_build_agent(agent_input: BuildAgentInput) -> BuildAgentOutput:
    """Default build-agent implementation that drives an LLM via otto.agent.

    Builds a prompt from the slice tasks + checks + spec structure, runs
    the agent with timeout, treats agent crash as failure (caller will
    retry with fresh prompt).

    Uses `make_agent_options(agent_type="build")` to inherit provider
    credentials and the project's `otto.yaml` agent configuration —
    constructing AgentOptions manually skips that auth setup and the
    spawned subprocess crashes with "Not logged in".
    """
    from otto.agent import make_agent_options, run_agent_with_timeout
    from otto.agent import AgentCallError
    from otto.config import load_config

    prompt = _build_agent_prompt(agent_input)
    log_dir = agent_input.log_dir or (agent_input.worktree / "_otto_build_logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_subdir = log_dir / f"attempt-{agent_input.attempt:02d}"
    log_subdir.mkdir(parents=True, exist_ok=True)

    config_path = agent_input.project_dir / "otto.yaml"
    # Pattern F: distinguish "file missing" (fine, use defaults) from
    # "file malformed/unreadable" (hard fail). A broken otto.yaml that
    # silently becomes {} causes the build agent to run with default
    # options instead of the project's configured venv/test_command —
    # the run "succeeds" but produces wrong output.
    config: dict = {}
    if config_path.exists():
        try:
            config = load_config(config_path)
        except Exception as exc:
            raise RuntimeError(
                f"otto.yaml at {config_path} is unreadable: {exc}"
            ) from exc
    options = make_agent_options(agent_input.project_dir, config, agent_type="build")
    # The slice's worktree is the agent's working directory. AgentOptions is
    # a mutable dataclass; mutate in place rather than reconstruct.
    options.cwd = str(agent_input.worktree)
    options.permission_mode = "acceptEdits"  # build agents may edit owned files

    t0 = time.monotonic()
    try:
        text, cost, _session_id, _breakdown = await run_agent_with_timeout(
            prompt,
            options,
            log_dir=log_subdir,
            phase_name="BUILD",
            phase_label=f"slice/{agent_input.group.id}/attempt-{agent_input.attempt}",
            timeout=None,
            project_dir=agent_input.project_dir,
        )
        return BuildAgentOutput(
            succeeded=True,
            cost_usd=cost or 0.0,
            wall_s=time.monotonic() - t0,
            detail=text[:500],
        )
    except AgentCallError as exc:
        return BuildAgentOutput(
            succeeded=False,
            cost_usd=0.0,
            wall_s=time.monotonic() - t0,
            detail=f"agent error: {exc}",
        )


# ---------------------------------------------------------------------------
# Public-name aliases (research §2 vocabulary: Group/feature, not Slice)
# ---------------------------------------------------------------------------
#
# `run_build` is the canonical async dispatch entry. `build_groups` is the
# preferred name under the new vocabulary; `build_slices` is kept for any
# external caller that referenced the legacy name. All three are the same
# coroutine — additive aliases on top of `run_build`.
build_groups = run_build
build_slices = run_build  # legacy back-compat alias


# Re-export for tests / call sites.
__all__ = [
    "BuildAgentCallable",
    "BuildAgentInput",
    "BuildAgentOutput",
    "BuildBudget",
    "BuildResult",
    "ComponentResult",
    "ComponentStatus",
    "GroupResult",
    "GroupStatus",
    "build_groups",
    "build_slices",
    "default_build_agent",
    "detect_dependency_scope_extensions",
    "detect_scope_violations",
    "ready_components",
    "ready_groups",
    "run_build",
]
