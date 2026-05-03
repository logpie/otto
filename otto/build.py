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
- Per-slice retries: 3 attempts (configurable via BuildBudget.per_slice_retries)
- Per-slice wall budget: 30 min (BuildBudget.per_slice_wall_s)
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
from otto.spec_compile import CheckKind, Slice, Spec
from otto.spec_state import emit

logger = logging.getLogger("otto.build")


# ---------------------------------------------------------------------------
# Status + budgets
# ---------------------------------------------------------------------------


class SliceStatus(str, Enum):
    PENDING = "pending"  # deps not yet met, OR not yet started
    IN_PROGRESS = "in_progress"
    PASSING = "passing"  # all checks pass; merge candidate
    BLOCKED = "blocked"  # exceeded retries / budget
    FAILED_SCOPE = "failed_scope"  # scope violation; treated as blocked


@dataclass
class BuildBudget:
    """Bounds shared across the build loop and audit-driven repair."""

    per_slice_retries: int = 3
    per_slice_wall_s: int = 30 * 60  # 30 minutes
    total_repair_s: int = 90 * 60  # 90 minutes shared with audit
    _spent_repair_s: float = 0.0

    def remaining_repair_s(self) -> float:
        return max(0.0, self.total_repair_s - self._spent_repair_s)

    def charge_repair(self, seconds: float) -> None:
        self._spent_repair_s += max(0.0, seconds)


@dataclass
class SliceResult:
    """Per-slice outcome of the build loop."""

    slice_id: str
    status: SliceStatus
    attempts: int
    branch: str
    worktree: Path
    last_evidence: list[Evidence] = field(default_factory=list)
    failure_narrative: str = ""
    cost_usd: float = 0.0
    wall_s: float = 0.0


@dataclass
class BuildResult:
    """Aggregate result of run_build."""

    spec_session_dir: Path
    slice_results: list[SliceResult] = field(default_factory=list)
    total_cost_usd: float = 0.0
    total_wall_s: float = 0.0

    @property
    def all_passing(self) -> bool:
        return bool(self.slice_results) and all(
            r.status == SliceStatus.PASSING for r in self.slice_results
        )

    @property
    def passing_ids(self) -> list[str]:
        return [r.slice_id for r in self.slice_results if r.status == SliceStatus.PASSING]

    @property
    def blocked_ids(self) -> list[str]:
        return [
            r.slice_id
            for r in self.slice_results
            if r.status in (SliceStatus.BLOCKED, SliceStatus.FAILED_SCOPE)
        ]


# ---------------------------------------------------------------------------
# Build agent abstraction (mockable)
# ---------------------------------------------------------------------------


@dataclass
class BuildAgentInput:
    """Input passed to a build-agent callable for one attempt on one slice."""

    spec: Spec
    slice: Slice
    project_dir: Path
    worktree: Path
    branch: str
    attempt: int  # 1-indexed
    last_failure_narrative: str = ""  # empty on first attempt
    log_dir: Path | None = None  # if set, agent writes narrative there


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


def ready_slices(
    spec: Spec,
    completed_ids: Iterable[str],
    in_progress_ids: Iterable[str] = (),
    skipped_ids: Iterable[str] = (),
) -> list[Slice]:
    """Return slices whose deps are all in `completed_ids` and not in flight or skipped.

    Args:
        completed_ids: Slices that have *successfully* completed (deps satisfied).
        in_progress_ids: Slices currently running.
        skipped_ids: Slices that have terminally failed (BLOCKED / FAILED_SCOPE).
            Their deps are NOT considered satisfied for downstream slices.
            Downstream slices should be marked BLOCKED separately by the caller
            once their deps include any skipped id.
    """
    completed = set(completed_ids)
    in_flight = set(in_progress_ids)
    skipped = set(skipped_ids)
    ready: list[Slice] = []
    for s in spec.slices:
        if s.id in completed or s.id in in_flight or s.id in skipped:
            continue
        if all(dep in completed for dep in (s.deps or [])):
            ready.append(s)
    return ready


def detect_scope_violations(
    slice_obj: Slice,
    spec: Spec,
    modified_paths: Iterable[str],
    *,
    project_root: Path | None = None,
) -> list[str]:
    """Return paths the slice modified that violate owned_paths write-scope.

    Rule (write-scope, not exclusion):
    - A path is allowed if it matches the slice's own `owned_paths` globs.
    - A path is allowed if it matches `spec.shared_scaffold` globs.
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
    own_globs = list(slice_obj.owned_paths or [])
    shared_globs = list(spec.shared_scaffold or [])
    # Transitive deps: every slice this slice depends on, recursively.
    transitive_dep_ids = _transitive_deps(slice_obj.id, spec)
    dep_globs: list[str] = []
    peer_globs: list[str] = []
    for s in spec.slices:
        if s.id == slice_obj.id:
            continue
        if s.id in transitive_dep_ids:
            dep_globs.extend(s.owned_paths or [])
        else:
            peer_globs.extend(s.owned_paths or [])

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


def _transitive_deps(slice_id: str, spec: Spec) -> set[str]:
    """Return all slices `slice_id` depends on, transitively (excluding self)."""
    by_id = {s.id: s for s in spec.slices}
    visited: set[str] = set()
    stack = list(by_id.get(slice_id, Slice(id=slice_id, title="")).deps or [])
    while stack:
        dep = stack.pop()
        if dep in visited or dep == slice_id:
            continue
        visited.add(dep)
        upstream = by_id.get(dep)
        if upstream is not None:
            stack.extend(upstream.deps or [])
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
    branch_for_slice: Callable[[Slice], str] | None = None,
    worktree_for_slice: Callable[[Slice], Path] | None = None,
    on_state_change: Callable[[str, str, dict[str, Any]], None] | None = None,
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
        branch_for_slice: Branch naming. Default: ``i2p/<spec_session>/<slice_id>``.
        worktree_for_slice: Worktree resolution. Default: project_dir
            (single-worktree mode for v1; future: separate worktrees per slice).
        on_state_change: Optional hook called as (slice_id, status, extra).
            Receives every status transition for testability and progress UI.

    Returns:
        BuildResult with per-slice outcomes.

    The build loop itself is sequential dispatch in v1 — slices that are
    ready run one at a time, in dep-topological order. Concurrency is a
    follow-up; the readiness logic is structured to support it.
    """
    budget = budget or BuildBudget()
    branch_for_slice = branch_for_slice or (
        lambda s: f"i2p/{session_dir.name}/{s.id}"
    )
    worktree_for_slice = worktree_for_slice or (lambda _s: project_dir)

    completed_ids: set[str] = set()
    blocked_ids: set[str] = set()
    results: list[SliceResult] = []
    total_t0 = time.monotonic()
    total_cost = 0.0

    def _emit_state(slice_id: str, status: SliceStatus, extra: dict[str, Any] | None = None) -> None:
        # Map our SliceStatus to the journal's recognized event kinds.
        # IN_PROGRESS → slice.started; PASSING → slice.merge.eligible
        # (slice is now a merge candidate); BLOCKED / FAILED_SCOPE → slice.blocked.
        # PENDING does not emit (no journal event before slice.started).
        kind_map = {
            SliceStatus.IN_PROGRESS: "slice.started",
            SliceStatus.PASSING: "slice.merge.eligible",
            SliceStatus.BLOCKED: "slice.blocked",
            SliceStatus.FAILED_SCOPE: "slice.blocked",
        }
        kind = kind_map.get(status)
        if kind is not None:
            payload = dict(extra or {})
            detail = str(payload.pop("narrative", ""))
            attempt = int(payload.pop("attempts", 0) or 0)
            try:
                emit(session_dir, kind, slice_id=slice_id, attempt=attempt, detail=detail, **payload)
            except OSError as exc:
                logger.warning("emit %s failed: %s", kind, exc)
        if on_state_change is not None:
            on_state_change(slice_id, status.value, extra or {})

    while True:
        ready = ready_slices(spec, completed_ids, skipped_ids=blocked_ids)
        if not ready:
            break
        # Sequential v1: pick the first ready slice. Stable ordering = spec order.
        next_slice = ready[0]
        slice_branch = branch_for_slice(next_slice)
        slice_worktree = worktree_for_slice(next_slice)
        _emit_state(next_slice.id, SliceStatus.IN_PROGRESS, {"branch": slice_branch})

        slice_result = await _run_slice(
            spec=spec,
            slice_obj=next_slice,
            project_dir=project_dir,
            worktree=slice_worktree,
            branch=slice_branch,
            session_dir=session_dir,
            build_agent=build_agent,
            base_url=base_url,
            budget=budget,
        )
        total_cost += slice_result.cost_usd
        results.append(slice_result)
        if slice_result.status == SliceStatus.PASSING:
            completed_ids.add(next_slice.id)
        else:
            blocked_ids.add(next_slice.id)
        _emit_state(
            next_slice.id,
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
        for s in spec.slices
        if s.id not in completed_ids and s.id not in blocked_ids
    ]
    for s in pending_unreachable:
        results.append(
            SliceResult(
                slice_id=s.id,
                status=SliceStatus.BLOCKED,
                attempts=0,
                branch="",
                worktree=project_dir,
                failure_narrative="dep blocked",
            )
        )
        _emit_state(s.id, SliceStatus.BLOCKED, {"narrative": "dep blocked"})

    return BuildResult(
        spec_session_dir=session_dir,
        slice_results=results,
        total_cost_usd=total_cost,
        total_wall_s=time.monotonic() - total_t0,
    )


async def _run_slice(
    *,
    spec: Spec,
    slice_obj: Slice,
    project_dir: Path,
    worktree: Path,
    branch: str,
    session_dir: Path,
    build_agent: BuildAgentCallable,
    base_url: str | None,
    budget: BuildBudget,
) -> SliceResult:
    """Run one slice through tasks→checks→fix retries.

    Returns SliceResult with PASSING / BLOCKED / FAILED_SCOPE.
    """
    slice_t0 = time.monotonic()
    last_failure = ""
    last_evidence: list[Evidence] = []
    cost_total = 0.0
    attempt = 0
    raw_log_dir = session_dir / "build" / slice_obj.id

    while attempt < budget.per_slice_retries:
        attempt += 1
        elapsed = time.monotonic() - slice_t0
        if elapsed >= budget.per_slice_wall_s:
            return SliceResult(
                slice_id=slice_obj.id,
                status=SliceStatus.BLOCKED,
                attempts=attempt - 1,
                branch=branch,
                worktree=worktree,
                last_evidence=last_evidence,
                failure_narrative=f"per-slice wall budget exhausted after {elapsed:.0f}s",
                cost_usd=cost_total,
                wall_s=elapsed,
            )
        if budget.remaining_repair_s() <= 0 and attempt > 1:
            return SliceResult(
                slice_id=slice_obj.id,
                status=SliceStatus.BLOCKED,
                attempts=attempt - 1,
                branch=branch,
                worktree=worktree,
                last_evidence=last_evidence,
                failure_narrative="total repair budget exhausted (audit + build)",
                cost_usd=cost_total,
                wall_s=time.monotonic() - slice_t0,
            )

        attempt_t0 = time.monotonic()
        agent_input = BuildAgentInput(
            spec=spec,
            slice=slice_obj,
            project_dir=project_dir,
            worktree=worktree,
            branch=branch,
            attempt=attempt,
            last_failure_narrative=last_failure,
            log_dir=raw_log_dir,
        )
        try:
            agent_output = await build_agent(agent_input)
        except Exception as exc:
            last_failure = f"agent crashed on attempt {attempt}: {type(exc).__name__}: {exc}"
            attempt_wall = time.monotonic() - attempt_t0
            if attempt > 1:
                budget.charge_repair(attempt_wall)
            emit(
                session_dir,
                "slice.attempt.failed",
                slice_id=slice_obj.id,
                attempt=attempt,
                detail=last_failure,
            )
            continue

        cost_total += agent_output.cost_usd
        attempt_wall = time.monotonic() - attempt_t0
        if attempt > 1:
            budget.charge_repair(attempt_wall)

        if not agent_output.succeeded:
            last_failure = agent_output.detail or "agent reported failure"
            emit(
                session_dir,
                "slice.attempt.failed",
                slice_id=slice_obj.id,
                attempt=attempt,
                detail=last_failure,
            )
            continue

        # Scope check: agent must not have modified other slices' owned files.
        try:
            modified = _git_diff_modified_paths(worktree)
        except Exception as exc:
            modified = []
            logger.warning("git diff failed for %s: %s", slice_obj.id, exc)
        violations = detect_scope_violations(
            slice_obj, spec, modified, project_root=worktree
        )
        if violations:
            return SliceResult(
                slice_id=slice_obj.id,
                status=SliceStatus.FAILED_SCOPE,
                attempts=attempt,
                branch=branch,
                worktree=worktree,
                last_evidence=last_evidence,
                failure_narrative=(
                    f"scope violation: modified {len(violations)} path(s) outside "
                    f"owned_paths: {', '.join(violations[:5])}"
                    + (f" (+{len(violations) - 5} more)" if len(violations) > 5 else "")
                ),
                cost_usd=cost_total,
                wall_s=time.monotonic() - slice_t0,
            )

        # Run slice's deterministic checks.
        emit(
            session_dir,
            "slice.check.started",
            slice_id=slice_obj.id,
            attempt=attempt,
        )
        evidence_pairs = run_checks(
            list(slice_obj.checks),
            project_dir=project_dir,
            cwd=worktree,
            base_url=base_url,
            raw_log_dir=raw_log_dir / f"attempt-{attempt:02d}",
        )
        last_evidence = [ev for _check, ev in evidence_pairs]
        all_pass = all(ev.passed for ev in last_evidence)
        emit(
            session_dir,
            "slice.check.finished",
            slice_id=slice_obj.id,
            attempt=attempt,
            detail=("pass" if all_pass else "fail"),
            details=[ev.detail for ev in last_evidence],
        )

        if all_pass:
            return SliceResult(
                slice_id=slice_obj.id,
                status=SliceStatus.PASSING,
                attempts=attempt,
                branch=branch,
                worktree=worktree,
                last_evidence=last_evidence,
                failure_narrative="",
                cost_usd=cost_total,
                wall_s=time.monotonic() - slice_t0,
            )

        # Otherwise: prepare narrative for next attempt's prompt-level reset.
        failed_summaries = [ev.detail for ev in last_evidence if not ev.passed]
        last_failure = (
            f"checks failed on attempt {attempt}: " + "; ".join(failed_summaries[:5])
        )
        emit(
            session_dir,
            "slice.attempt.failed",
            slice_id=slice_obj.id,
            attempt=attempt,
            detail=last_failure,
        )

    # Out of retries.
    return SliceResult(
        slice_id=slice_obj.id,
        status=SliceStatus.BLOCKED,
        attempts=attempt,
        branch=branch,
        worktree=worktree,
        last_evidence=last_evidence,
        failure_narrative=last_failure or "exceeded per-slice retry budget",
        cost_usd=cost_total,
        wall_s=time.monotonic() - slice_t0,
    )


# ---------------------------------------------------------------------------
# Default build-agent implementation
# ---------------------------------------------------------------------------


def _build_agent_prompt(agent_input: BuildAgentInput) -> str:
    """Compose the per-attempt prompt for the build agent.

    Fresh prompt on retry: clear conversation, re-state the spec context,
    call out the previous attempt's failure narrative without rehashing
    its reasoning.

    Also surfaces the project's own contract surface so the agent doesn't
    invent its own (Microfeed bench learning: agent built `{follower,
    following}` against a contract that uses `{follower, target}` because
    the build prompt didn't say "read the existing test files").
    """
    s = agent_input.slice
    spec = agent_input.spec
    lines: list[str] = []
    lines.append(f"# Build slice `{s.id}` — {s.title}")
    lines.append("")
    lines.append(
        f"You are working on slice {s.id} of an approved Spec for: "
        f"{spec.intent!r} (project_kind={spec.project_kind})."
    )
    lines.append(f"Branch: {agent_input.branch}")
    lines.append(f"Worktree: {agent_input.worktree}")
    lines.append("")
    if s.deps:
        lines.append(f"This slice depends on (already landed): {', '.join(s.deps)}")
    lines.append("")
    # Surface the project's contract surface — test_command in otto.yaml +
    # any seeded test/contract files. The agent must read these BEFORE
    # designing APIs to avoid drifting from the contract.
    contract_lines = _project_contract_summary(agent_input.project_dir)
    if contract_lines:
        lines.append("## Project contract surface (READ THESE FIRST)")
        lines.append(
            "The project root has these existing contract artifacts. Your "
            "implementation must satisfy them — do NOT invent your own API "
            "shapes when the contract pins them down."
        )
        lines.extend(contract_lines)
        lines.append("")
    lines.append("## What you must do (slice tasks)")
    for i, task in enumerate(s.tasks or [], 1):
        lines.append(f"  {i}. {task}")
    lines.append("")
    # Compute transitive deps for accurate write-scope summary in the prompt.
    transitive_deps = _transitive_deps(s.id, spec)
    dep_owned: list[tuple[str, str]] = []
    peer_owned: list[tuple[str, str]] = []
    for other in spec.slices:
        if other.id == s.id:
            continue
        if other.id in transitive_deps:
            dep_owned.extend((other.id, g) for g in (other.owned_paths or []))
        else:
            peer_owned.extend((other.id, g) for g in (other.owned_paths or []))

    lines.append("## Write-scope rules (the build runtime ENFORCES these)")
    lines.append("")
    lines.append("**You MAY MODIFY:**")
    if s.owned_paths:
        for g in s.owned_paths:
            lines.append(f"  - your owned: `{g}`")
    if dep_owned:
        for did, g in dep_owned:
            lines.append(f"  - dep `{did}`'s: `{g}` (you depend on this slice)")
    if spec.shared_scaffold:
        for g in spec.shared_scaffold:
            lines.append(f"  - shared scaffold: `{g}`")
    if not s.owned_paths and not dep_owned and not spec.shared_scaffold:
        lines.append("  (none declared — you may only CREATE new files)")
    lines.append("")
    if peer_owned:
        lines.append(
            "**FORBIDDEN — these belong to PEER slices (not your dependencies). "
            "Do NOT create or modify them. The build runtime will reject your "
            "attempt if you do, and the slice will be BLOCKED.**"
        )
        for sid, g in peer_owned:
            lines.append(f"  - peer `{sid}`'s: `{g}`")
    lines.append("")
    lines.append(
        "You MAY also create NEW files outside any declared scope (e.g. helper "
        "modules, fixtures). The forbidden list above only applies to files "
        "owned by peer slices."
    )
    lines.append("")
    lines.append(
        "**Stay in your lane.** Build only what this slice's tasks ask for. "
        "Do NOT pre-emptively build features that belong to later slices "
        "(they have their own dedicated build agents and will fail if you "
        "trample their files)."
    )
    lines.append("")
    lines.append("## Slice acceptance checks")
    for i, c in enumerate(s.checks or [], 1):
        lines.append(f"  {i}. {_describe_check(c)}")
    lines.append("")
    if agent_input.attempt > 1 and agent_input.last_failure_narrative:
        lines.append("## Previous attempt failed")
        lines.append(agent_input.last_failure_narrative)
        lines.append("")
        lines.append(
            "Start over with a fresh approach. Do NOT rehash your previous "
            "reasoning. Re-read the spec and the current branch state and "
            "build to satisfy the checks."
        )
        lines.append("")
    lines.append("## Project structure decisions (binding)")
    payload = (spec.structure.payload or {}) if spec.structure else {}
    lines.append("```json")
    import json as _json

    lines.append(_json.dumps(payload, indent=2, sort_keys=True))
    lines.append("```")
    lines.append("")
    lines.append("Make all changes. When done, just confirm completion.")
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
        except Exception:
            pass
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
    try:
        config = load_config(config_path)
    except Exception:
        config = {}
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
            phase_label=f"slice/{agent_input.slice.id}/attempt-{agent_input.attempt}",
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


# Re-export for tests / call sites.
__all__ = [
    "BuildAgentCallable",
    "BuildAgentInput",
    "BuildAgentOutput",
    "BuildBudget",
    "BuildResult",
    "SliceResult",
    "SliceStatus",
    "default_build_agent",
    "detect_scope_violations",
    "ready_slices",
    "run_build",
]


