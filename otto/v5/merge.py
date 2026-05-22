"""Child-branch merge logic and integration-union propagation.

Extracted from ``otto/v5_runner.py``. Public surface remains on
``otto.v5_runner`` — every symbol here is re-exported. Cross-module
and patched runner symbols are dereferenced lazily via ``_v5r.X``
so test-time monkeypatches on ``otto.v5_runner`` are honoured.
"""

from __future__ import annotations

import contextlib
import enum
import fcntl
import hashlib
import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

from otto.lead import LeadResult
from otto.safe_slug import safe_slug
from otto.schemas import (
    VERDICT_CATASTROPHIC,
    VERDICT_MERGE_BLOCKED,
    VERDICT_PARTIAL,
    VERDICT_PASS,
    VERDICT_UNVERIFIED,
)
from otto.v5_branching import MergeWorktreeDirtyError
from otto.observability import iso_timestamp
from otto.queue.task_graph import (
    children_of,
    get_task,
    mark_reviewed_partial,
    read_graph,
    set_verdict,
    set_verdict_and_metadata,
    update_task_metadata,
)

logger = logging.getLogger("otto.v5_runner")

# Lazy parent-module reference for patchability.
from otto import v5_runner as _v5r  # noqa: E402


def _line_hash(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()[:16]

def _parse_added_lines_by_path(diff_text: str) -> dict[str, list[str]]:
    additions: dict[str, list[str]] = {}
    current_path: str | None = None
    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git "):
            current_path = None
            continue
        if raw_line.startswith("+++ "):
            marker = raw_line[4:].strip()
            if marker == "/dev/null":
                current_path = None
            elif marker.startswith("b/"):
                current_path = marker[2:]
            else:
                current_path = marker
            continue
        if not current_path or not raw_line.startswith("+") or raw_line.startswith("+++ "):
            continue
        line = raw_line[1:].rstrip()
        if line.strip():
            additions.setdefault(current_path, []).append(line)
    return additions

def _git_added_lines_by_path_between(
    worktree: Path,
    base_ref: str,
    head_ref: str,
) -> dict[str, list[str]]:
    if not base_ref or not head_ref:
        return {}
    diff_text = _v5r._git_capture(
        worktree,
        [
            "diff",
            "--unified=0",
            "--diff-filter=AM",
            f"{base_ref}..{head_ref}",
            "--",
        ],
        timeout=30,
    )
    if not diff_text:
        return {}
    return _parse_added_lines_by_path(diff_text)

def _task_id_for_integration_branch(project_dir: Path, integration_branch: str) -> str:
    graph = read_graph(project_dir)
    for task_id, entry in (graph.get("tasks") or {}).items():
        if isinstance(entry, dict) and entry.get("integration_branch") == integration_branch:
            return str(task_id)
    return _v5r.ROOT_TASK_ID if integration_branch == "main" else integration_branch

def _integration_union_empty_state(parent_integration_branch: str) -> dict[str, Any]:
    return {
        "schema_version": _v5r._INTEGRATION_UNION_GUARD_SCHEMA_VERSION,
        "parent_integration_branch": parent_integration_branch,
        "contributions": [],
        "touches": [],
        "_written_at": iso_timestamp(),
    }

def _integration_union_state_from_task(
    task: dict[str, Any],
    parent_integration_branch: str,
) -> dict[str, Any]:
    raw = task.get("integration_union_guard")
    if not isinstance(raw, dict):
        return _integration_union_empty_state(parent_integration_branch)
    if raw.get("schema_version") != _v5r._INTEGRATION_UNION_GUARD_SCHEMA_VERSION:
        return _integration_union_empty_state(parent_integration_branch)
    state = dict(raw)
    if not isinstance(state.get("contributions"), list):
        state["contributions"] = []
    if not isinstance(state.get("touches"), list):
        state["touches"] = []
    state["parent_integration_branch"] = parent_integration_branch
    return state

def _merge_integration_union_state(
    *,
    state: dict[str, Any],
    child_task_id: str,
    source_branch: str,
    base_ref: str,
    head_ref: str,
    additions_by_path: dict[str, list[str]],
    touched_paths: list[str],
) -> dict[str, Any]:
    next_state = dict(state)
    contributions = [
        dict(item)
        for item in (next_state.get("contributions") or [])
        if isinstance(item, dict)
    ]
    touches = [
        dict(item)
        for item in (next_state.get("touches") or [])
        if isinstance(item, dict)
    ]
    seen_contributions = {
        (
            str(item.get("child_task_id") or ""),
            str(item.get("path") or ""),
            str(item.get("line") or ""),
        )
        for item in contributions
    }
    seen_touches = {
        (
            str(item.get("child_task_id") or ""),
            str(item.get("path") or ""),
        )
        for item in touches
    }
    recorded_at = iso_timestamp()
    for path in touched_paths:
        touch_key = (child_task_id, path)
        if touch_key not in seen_touches:
            touches.append({
                "child_task_id": child_task_id,
                "path": path,
                "source_branch": source_branch,
                "base_ref": base_ref,
                "head_ref": head_ref,
                "recorded_at": recorded_at,
            })
            seen_touches.add(touch_key)
    for path, lines in additions_by_path.items():
        for line in lines:
            key = (child_task_id, path, line)
            if key in seen_contributions:
                continue
            contributions.append({
                "child_task_id": child_task_id,
                "path": path,
                "line": line,
                "line_hash": _line_hash(line),
                "source_branch": source_branch,
                "base_ref": base_ref,
                "head_ref": head_ref,
                "recorded_at": recorded_at,
            })
            seen_contributions.add(key)
    next_state["contributions"] = contributions
    next_state["touches"] = touches
    next_state["_written_at"] = recorded_at
    return next_state

def _integration_union_shared_paths(state: dict[str, Any]) -> set[str]:
    contributors_by_path: dict[str, set[str]] = {}
    for item in state.get("touches") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        child_task_id = str(item.get("child_task_id") or "")
        if path and child_task_id:
            contributors_by_path.setdefault(path, set()).add(child_task_id)
    return {
        path
        for path, child_ids in contributors_by_path.items()
        if len(child_ids) > 1
    }

def _foundation_contracts_by_path_from_union_state(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = state.get("foundation_contracts")
    contracts: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for path, item in raw.items():
            if isinstance(item, dict):
                contract = dict(item)
                contract.setdefault("path", path)
                contracts.append(contract)
    elif isinstance(raw, list):
        contracts = [dict(item) for item in raw if isinstance(item, dict)]
    return {
        _v5r._normalize_contract_path(str(contract.get("path") or "")): contract
        for contract in contracts
        if _v5r._normalize_contract_path(str(contract.get("path") or ""))
    }

def _semantic_union_contributor_allowed(
    *,
    child_task_id: str,
    contract: dict[str, Any],
    state: dict[str, Any],
) -> bool:
    owner_id = str(contract.get("owner_task_id") or "").strip()
    if child_task_id == owner_id:
        return True
    contributors = state.get("contributors")
    task = contributors.get(child_task_id) if isinstance(contributors, dict) else None
    if not isinstance(task, dict) or str(task.get("task_role") or "") != "contract_amendment":
        return False
    bound = task.get("contract_amendment")
    if not isinstance(bound, dict):
        bound = {}
    contract_path = _v5r._normalize_contract_path(str(contract.get("path") or ""))
    bound_path = _v5r._normalize_contract_path(
        str(bound.get("contract_path") or task.get("contract_amendment_path") or "")
    )
    bound_paths = [
        _v5r._normalize_contract_path(str(path))
        for path in (
            bound.get("contract_paths")
            or task.get("contract_amendment_paths")
            or ([bound_path] if bound_path else [])
        )
        if _v5r._normalize_contract_path(str(path))
    ]
    bound_owner = str(
        bound.get("owner_task_id") or task.get("contract_amendment_owner_task_id") or ""
    ).strip()
    return bool(
        bound_owner == owner_id
        and contract_path
        and any(_v5r._path_overlaps(contract_path, path) for path in bound_paths)
    )

def _semantic_union_required_export_present(final_text: str, export_name: str) -> bool:
    name = re.escape(export_name)
    patterns = [
        rf"\bexport\s+(?:async\s+)?function\s+{name}\b",
        rf"\bexport\s+(?:const|let|var|class|interface|type|enum)\s+{name}\b",
        rf"\bexport\s*\{{[^}}]*\b{name}\b[^}}]*\}}",
        rf"\bexports\.{name}\b",
        rf"\bmodule\.exports(?:\.{name}|\s*=\s*\{{[^}}]*\b{name}\b)",
    ]
    return any(re.search(pattern, final_text, flags=re.MULTILINE) for pattern in patterns)

def _semantic_union_text_contains_probe(final_text: str, probe: str) -> bool:
    normalized_probe = " ".join(str(probe).split())
    if not normalized_probe:
        return True
    normalized_text = " ".join(final_text.split())
    return normalized_probe in normalized_text

def _semantic_foundation_contract_satisfied(contract: dict[str, Any], final_text: str) -> bool:
    """Does this `check: semantic` contract's invariant hold in the final text?

    Audit F-4: a `semantic` contract with NO probes declared is the explicit
    "trust the owner" mode — the architect declared the file as semantic
    precisely because content evolves under owner authority. Returning
    False here forces literal line-preservation, which contradicts the
    documented semantics ("content may evolve as long as the public behavior
    is preserved"). Under-specified semantic contracts now succeed by
    default and emit a separate `semantic_contract_underspecified` advisory
    so operators can decide whether to tighten CHARTER.
    """
    required_exports = [
        str(value).strip()
        for value in (contract.get("required_exports") or [])
        if str(value).strip()
    ]
    behavior_probes = [
        str(value).strip()
        for value in (contract.get("behavior_probes") or contract.get("invariants") or [])
        if str(value).strip()
    ]
    if not required_exports and not behavior_probes:
        # Audit F-4: no probes declared → trust the owner. Previously this
        # returned False (failing semantic mode silently and forcing
        # literal-line-match), which produced false-demote noise. The
        # `semantic_contract_underspecified` advisory (emitted elsewhere)
        # surfaces this for operator attention without blocking the merge.
        return True
    return all(
        _semantic_union_required_export_present(final_text, export_name)
        for export_name in required_exports
    ) and all(
        _semantic_union_text_contains_probe(final_text, probe)
        for probe in behavior_probes
    )

def _integration_union_contributor_snapshot(task: dict[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "task_role": str(task.get("task_role") or "feature"),
    }
    for key in (
        "contract_amendment",
        "contract_amendment_path",
        "contract_amendment_paths",
        "contract_amendment_owner_task_id",
        "repair_route",
    ):
        if key in task:
            snapshot[key] = task.get(key)
    return snapshot

def _integration_union_missing_contributions(
    state: dict[str, Any],
    final_text_by_path: dict[str, str],
) -> list[dict[str, Any]]:
    """Audit F-5 / Phase E: literal line-preservation is now scoped to paths
    with an explicit foundation_contract entry. Pre-refactor: every shared
    path's contributed lines had to survive. That was the false-demote source
    for foundation-seeded feature-owned files (linkboard 2026-05-21). Post:
    the check is the cross-product of (declared contract × literal/semantic
    rules); shared paths without a contract become advisory via
    `_integration_union_undeclared_shared_paths` (not this function) so
    the architect's missing-declaration is still visible without demoting
    a perfectly-good merge.

    Phase B's foundation_seeded_feature_path check + the broader
    _foundation_isolation_feedback already catch the architect-declared
    partition violations BEFORE features dispatch; this function focuses on
    the narrower invariant — `check: literal` contracts must preserve
    their declared lines; `check: semantic` contracts must satisfy their
    declared probes (or trust the owner per Phase D when no probes).
    """
    shared_paths = _integration_union_shared_paths(state)
    foundation_contracts = _foundation_contracts_by_path_from_union_state(state)
    missing: list[dict[str, Any]] = []
    final_lines_by_path = {
        path: {line.rstrip() for line in text.splitlines()}
        for path, text in final_text_by_path.items()
    }
    for item in state.get("contributions") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        line = str(item.get("line") or "").rstrip()
        if not path or not line or path not in shared_paths:
            continue
        if line in final_lines_by_path.get(path, set()):
            continue
        normalized_path = _v5r._normalize_contract_path(path)
        contract = foundation_contracts.get(normalized_path)
        child_task_id = str(item.get("child_task_id") or "")
        if contract is None:
            # Phase E: no contract → no line-preservation gate. The
            # architect either didn't declare this path (Phase B's
            # foundation_seeded_feature_path / _foundation_isolation_feedback
            # surfaces that BEFORE we get here) or two siblings legitimately
            # touch a non-cross-cutting path (CHANGELOG.md, etc.). The
            # `_integration_union_undeclared_shared_paths` helper exposes
            # the same data as an advisory for operator visibility.
            continue
        if (
            str(contract.get("check") or "") == "semantic"
            and _semantic_union_contributor_allowed(
                child_task_id=child_task_id,
                contract=contract,
                state=state,
            )
            and _semantic_foundation_contract_satisfied(
                contract,
                final_text_by_path.get(path, ""),
            )
        ):
            continue
        missing.append({
            "path": path,
            "line": line,
            "line_hash": str(item.get("line_hash") or _line_hash(line)),
            "contributed_by": child_task_id,
            "source_branch": str(item.get("source_branch") or ""),
            "base_ref": str(item.get("base_ref") or ""),
            "head_ref": str(item.get("head_ref") or ""),
        })
    return missing


def _integration_union_undeclared_shared_paths(state: dict[str, Any]) -> list[str]:
    """Phase E advisory: shared paths touched by multiple children where the
    architect did NOT declare a foundation_contract. These don't block the
    merge but indicate the CHARTER partition is under-specified — operator
    can decide whether to tighten or accept.
    """
    shared_paths = _integration_union_shared_paths(state)
    contracted = set(_foundation_contracts_by_path_from_union_state(state).keys())
    return sorted({
        path for path in shared_paths
        if _v5r._normalize_contract_path(path) not in contracted
    })


@contextlib.contextmanager
def _integration_union_guard_lock(
    project_dir: Path,
    parent_integration_branch: str,
) -> Iterator[None]:
    """Serialize union-guard state updates for one integration target."""
    from otto.v5_branching import _git_common_dir

    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", parent_integration_branch).strip("-") or "target"
    lock_dir = _git_common_dir(project_dir) / "otto-union-guard-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{safe}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

def _integration_union_reason_text(feedback: dict[str, Any]) -> str:
    raw_missing = feedback.get("missing")
    missing: list[Any] = raw_missing if isinstance(raw_missing, list) else []
    rendered: list[str] = []
    for item in missing[:5]:
        if not isinstance(item, dict):
            continue
        line = str(item.get("line") or "")
        if len(line) > 160:
            line = line[:157] + "..."
        rendered.append(
            f"{item.get('path')} missing line contributed by "
            f"{item.get('contributed_by')}: {line}"
        )
    suffix = "; ".join(rendered) if rendered else "missing child-contributed lines"
    return f"integration union incomplete: {suffix}"

def _integration_union_feedback(
    *,
    parent_integration_branch: str,
    child_task_id: str,
    source_branch: str,
    base_ref: str,
    post_merge_ref: str,
    missing: list[dict[str, Any]],
    final_text_by_path: dict[str, str],
) -> dict[str, Any]:
    paths = sorted(dict.fromkeys(str(item.get("path") or "") for item in missing if item.get("path")))
    conflicts = [
        {
            "path": path,
            "base": final_text_by_path.get(path, ""),
            "ours": final_text_by_path.get(path, ""),
            "theirs": "\n".join(
                str(item.get("line") or "")
                for item in missing
                if item.get("path") == path and item.get("line")
            ),
        }
        for path in paths
    ]
    feedback: dict[str, Any] = {
        "kind": "integration_union_incomplete",
        "step_id": "integration_union_guard",
        "message": "",
        "paths": paths,
        "missing": missing,
        "child_task_id": child_task_id,
        "parent_integration_branch": parent_integration_branch,
        "source_branch": source_branch,
        "base_ref": base_ref,
        "post_merge_ref": post_merge_ref,
        "_written_at": iso_timestamp(),
        "integration_context": {
            "merge_refs": {
                "base_ref": base_ref,
                "ours_ref": parent_integration_branch,
                "theirs_ref": source_branch,
            },
            "conflict_packet": {
                "schema_version": 1,
                "source_branch": source_branch,
                "target_branch": parent_integration_branch,
                "unmerged_paths": paths,
                "conflicts": conflicts,
                "instruction": (
                    "Restore every missing child-contributed line listed by "
                    "the integration union guard. Preserve already integrated "
                    "target behavior and the source child behavior."
                ),
            },
            "integration_union_guard": {
                "kind": "integration_union_incomplete",
                "missing": missing,
                "paths": paths,
                "post_merge_ref": post_merge_ref,
            },
        },
    }
    feedback["message"] = _integration_union_reason_text(feedback)
    return feedback

def _record_and_check_integration_union(
    *,
    project_dir: Path,
    parent_integration_branch: str,
    child_task_id: str,
    source_branch: str,
    pre_merge_ref: str,
) -> dict[str, Any] | None:
    with _integration_union_guard_lock(
        project_dir,
        parent_integration_branch,
    ):
        parent_task_id = _v5r._parent_task_id_for_child(
            project_dir,
            child_task_id,
            parent_integration_branch,
        )
        parent_task = get_task(project_dir, parent_task_id) or {}
        tasks = (read_graph(project_dir).get("tasks") or {})
        state = _integration_union_state_from_task(parent_task, parent_integration_branch)
        state["foundation_contracts"] = _v5r._foundation_contracts_for_parent(
            project_dir,
            parent_task_id,
            tasks,
        )
        contributors = dict(state.get("contributors") or {})
        child_task = tasks.get(child_task_id) if isinstance(tasks, dict) else None
        contributors[child_task_id] = _integration_union_contributor_snapshot(
            child_task if isinstance(child_task, dict) else {}
        )
        state["contributors"] = contributors
        additions_by_path = _git_added_lines_by_path_between(
            project_dir,
            pre_merge_ref,
            source_branch,
        )
        touched_paths = _v5r._git_changed_paths_between_refs(
            project_dir,
            pre_merge_ref,
            source_branch,
        )
        state = _merge_integration_union_state(
            state=state,
            child_task_id=child_task_id,
            source_branch=source_branch,
            base_ref=pre_merge_ref,
            head_ref=source_branch,
            additions_by_path=additions_by_path,
            touched_paths=touched_paths,
        )
        update_task_metadata(project_dir, parent_task_id, integration_union_guard=state)
        shared_paths = _integration_union_shared_paths(state)
        final_text_by_path = {
            path: _v5r._git_show_text_at_ref(project_dir, parent_integration_branch, path)
            for path in shared_paths
        }
        missing = _integration_union_missing_contributions(state, final_text_by_path)
        if not missing:
            return None
        post_merge_ref = _v5r._git_capture(project_dir, ["rev-parse", parent_integration_branch])
        return _integration_union_feedback(
            parent_integration_branch=parent_integration_branch,
            child_task_id=child_task_id,
            source_branch=source_branch,
            base_ref=pre_merge_ref,
            post_merge_ref=post_merge_ref,
            missing=missing,
            final_text_by_path=final_text_by_path,
        )

def _integration_restore_branch(
    project_dir: Path,
    task_id: str,
    config: dict[str, Any],
) -> str:
    """Where ``project_dir`` should be restored after a task integration."""
    task = get_task(project_dir, task_id) or {}
    branch = str(task.get("integration_branch") or "").strip()
    return branch or _v5r._v5_root_branch(project_dir, config)

def _commit_integration_agent_changes(
    *,
    project_dir: Path,
    task_id: str,
    worktree_path: Path,
    result: LeadResult,
    on_event: Any = None,
) -> None:
    """Runner-owned commit for integration-agent edits."""
    if result.verdict == VERDICT_CATASTROPHIC:
        return
    from otto.v5_branching import commit_integration_worktree

    feedback = _v5r._foundation_contract_write_feedback(
        project_dir=project_dir,
        acting_task_id=task_id,
        parent_integration_branch=_integration_restore_branch(project_dir, task_id, {}),
        changed_paths=_v5r._git_diff_name_only(worktree_path),
        operation="integration_agent_commit",
    )
    ok, detail = commit_integration_worktree(
        worktree_path=worktree_path,
        task_id=task_id,
    )
    _v5r._emit(on_event, {
        "event": "integration_commit" if ok else "integration_commit_failed",
        "task_id": task_id,
        "worktree": str(worktree_path),
        "detail": detail,
    })

    if not ok:
        logger.warning("integration commit failed for %s: %s", task_id, detail)
        set_verdict(project_dir, task_id, VERDICT_MERGE_BLOCKED, cost_usd=result.cost_usd)
        result.verdict = VERDICT_MERGE_BLOCKED
        return
    if feedback is not None:
        _v5r._record_foundation_contract_write_annotation(
            project_dir=project_dir,
            task_id=task_id,
            result=result,
            feedback=feedback,
            on_event=on_event,
        )

def _commit_root_inline_changes(
    *,
    project_dir: Path,
    root_branch: str,
    result: LeadResult,
    on_event: Any = None,
) -> None:
    """Runner-owned commit for root inline builds."""
    if result.decomposition != "inline" or result.verdict == VERDICT_CATASTROPHIC:
        return

    from otto.v5_branching import commit_worktree, git_current_branch

    current_branch = git_current_branch(project_dir)
    if current_branch != root_branch:
        detail = (
            f"root inline finished on {current_branch!r}, expected {root_branch!r}; "
            "refusing runner commit"
        )
        _v5r._emit(on_event, {
            "event": "inline_commit_failed",
            "task_id": _v5r.ROOT_TASK_ID,
            "worktree": str(project_dir),
            "detail": detail,
        })
        logger.warning(detail)
        set_verdict(project_dir, _v5r.ROOT_TASK_ID, "merge_blocked", cost_usd=result.cost_usd)
        result.verdict = "merge_blocked"
        return

    feedback = _v5r._foundation_contract_write_feedback(
        project_dir=project_dir,
        acting_task_id=_v5r.ROOT_TASK_ID,
        parent_integration_branch=root_branch,
        changed_paths=_v5r._git_diff_name_only(project_dir),
        operation="root_inline_commit",
    )
    ok, detail = commit_worktree(worktree_path=project_dir, message="v5 inline build")
    _v5r._emit(on_event, {
        "event": "inline_commit" if ok else "inline_commit_failed",
        "task_id": _v5r.ROOT_TASK_ID,
        "worktree": str(project_dir),
        "detail": detail,
    })
    if not ok:
        logger.warning("root inline commit failed: %s", detail)
        set_verdict(project_dir, _v5r.ROOT_TASK_ID, VERDICT_MERGE_BLOCKED, cost_usd=result.cost_usd)
        result.verdict = VERDICT_MERGE_BLOCKED
        return
    if feedback is not None:
        _v5r._record_foundation_contract_write_annotation(
            project_dir=project_dir,
            task_id=_v5r.ROOT_TASK_ID,
            result=result,
            feedback=feedback,
            on_event=on_event,
        )

def _propagate_subtree_integration(
    *,
    project_dir: Path,
    task_id: str,
) -> tuple[bool, str, str, str]:
    """Merge a decomposed child's integration branch into its parent target."""
    from otto.v5_branching import integration_branch_name, merge_branch_into

    child_entry = get_task(project_dir, task_id) or {}
    parent_id = child_entry.get("parent_task_id") or _v5r.ROOT_TASK_ID
    target = "main" if parent_id == _v5r.ROOT_TASK_ID else integration_branch_name(parent_id)
    source = integration_branch_name(task_id)
    if source == target:
        detail = (
            "refusing subtree integration self-merge: source and target are both "
            f"{source!r} for task {task_id!r} with parent_task_id={parent_id!r}; "
            "propagation would otherwise be a silent no-op"
        )
        set_verdict(project_dir, task_id, "merge_blocked")
        return False, detail, source, target

    try:
        ok, detail = merge_branch_into(
            project_dir=project_dir,
            source_branch=source,
            target_branch=target,
        )
    except MergeWorktreeDirtyError as exc:
        detail = str(exc)
        set_verdict(project_dir, task_id, "merge_blocked")
        return False, detail, source, target
    if not ok:
        set_verdict(project_dir, task_id, "merge_blocked")
    return ok, detail, source, target

def _worktree_for_branch(project_dir: Path, branch: str) -> Path:
    listing = _v5r.subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        return project_dir
    current_path: Path | None = None
    for line in listing.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line[len("worktree "):].strip())
            continue
        if line.startswith("branch ") and current_path is not None:
            ref = line[len("branch "):].strip()
            if ref == branch or ref.endswith(f"/{branch}"):
                return current_path
    return project_dir

def _verify_child_branches_reached_parent(
    *,
    project_dir: Path,
    parent_task_id: str,
    on_event: Any = None,
) -> None:
    """Verify-only: emit an event for each passed child's branch state.

    Post Phase 2b (2026-05-21): the orchestrator no longer merges children
    into the parent integration branch — the integration Lead is the single
    merge authority (`lead-integration.md` Step 1). This function used to
    auto-recover unmerged children with `merge_child_into_integration`;
    that recovery is now gone. Integration will surface real merge conflicts
    when it runs its own `git merge i2p/build/<id>` for each child.

    We still emit the ancestry signal so resume logic / debugging can see
    where each child branch sits relative to the parent.
    """
    from otto.v5_branching import child_branch_name, integration_branch_name

    target = "main" if parent_task_id == _v5r.ROOT_TASK_ID else integration_branch_name(parent_task_id)
    for child_id in children_of(project_dir, parent_task_id):
        child = get_task(project_dir, child_id) or {}
        if not _task_entry_allows_upward_merge(child):
            continue

        branches = [child_branch_name(child_id)]
        if child.get("child_task_ids") or child.get("decomposition") == "emit":
            branches.append(integration_branch_name(child_id))

        for branch in dict.fromkeys(branches):
            ok, detail = _branch_is_ancestor(project_dir, branch, target)
            _v5r._emit(on_event, {
                "event": (
                    "child_branch_ancestry_ok"
                    if ok
                    else "child_branch_pending_integration_merge"
                ),
                "task_id": child_id,
                "branch": branch,
                "target": target,
                "detail": detail,
            })

def _branch_is_ancestor(project_dir: Path, branch: str, target: str) -> tuple[bool, str]:
    exists = _v5r.subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=str(project_dir),
        capture_output=True,
    )
    if exists.returncode != 0:
        return False, f"branch {branch!r} is missing"

    target_exists = _v5r.subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{target}"],
        cwd=str(project_dir),
        capture_output=True,
    )
    if target_exists.returncode != 0:
        return False, f"target branch {target!r} is missing"

    ancestor = _v5r.subprocess.run(
        ["git", "merge-base", "--is-ancestor", branch, target],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if ancestor.returncode == 0:
        return True, f"{branch} reaches {target}"
    detail = (ancestor.stderr or ancestor.stdout or "").strip()
    suffix = f": {detail}" if detail else ""
    return False, f"{branch} is not an ancestor of {target}{suffix}"

def _task_entry_allows_upward_merge(entry: dict[str, Any]) -> bool:
    # Delegates to the canonical predicate `entry_is_satisfactory_terminal`
    # so this stays in sync with `_child_result_allows_upward_merge` and
    # `subtask._verdict_satisfies_dependency`. Annotated partials
    # (chokepoint LAND path; `landed_with_annotation=True`) are
    # accepted as satisfactory — see [[project_v5_one_hard_gate_redesign]]
    # + plan-checkpoint-resume-v2.md Phase 0.
    from otto.queue.task_graph import entry_is_satisfactory_terminal
    return entry_is_satisfactory_terminal(entry)

def _child_result_allows_upward_merge(
    project_dir: Path,
    task_id: str,
    result: LeadResult,
) -> bool:
    # Phase 0 / Codex Plan Gate R2#4: the chokepoint writes
    # `landed_with_annotation=True` to the task entry (not to the result
    # payload), so the canonical check reads the entry. To handle the
    # not-yet-persisted case (mid-flight result before its annotation
    # has been written to the graph), we ALSO accept the result's own
    # in-memory reviewed_partial signal as a fallback.
    from otto.queue.task_graph import entry_is_satisfactory_terminal
    entry = get_task(project_dir, task_id) or {}
    if entry_is_satisfactory_terminal(entry):
        return True
    # Fallback for the not-yet-persisted case: a result that explicitly
    # carries a reviewed_partial signal and matches a non-merge_blocked
    # entry should still merge.
    if str(entry.get("verdict") or "") == VERDICT_MERGE_BLOCKED:
        return False
    if entry.get("merge_blocked_structured_reason") or entry.get("merge_blocked_reason"):
        return False
    if result.verdict == VERDICT_PASS:
        return True
    if result.verdict != VERDICT_PARTIAL:
        return False
    return _result_has_reviewed_partial(result)

def _result_has_reviewed_partial(result: LeadResult) -> bool:
    if result.verdict != VERDICT_PARTIAL or not isinstance(result.verify_result, dict):
        return False
    payload = result.verify_result
    return (
        payload.get("review_state") == "reviewed_partial"
        or payload.get("merge_review_state") == "reviewed_partial"
        or payload.get("reviewed_partial") is True
    )

def _record_reviewed_partial_if_present(
    project_dir: Path,
    task_id: str,
    result: LeadResult,
) -> None:
    if not _result_has_reviewed_partial(result):
        return
    payload = result.verify_result if isinstance(result.verify_result, dict) else {}
    mark_reviewed_partial(
        project_dir,
        task_id,
        reason=str(
            payload.get("reviewed_partial_reason")
            or payload.get("summary")
            or "partial explicitly reviewed before merge"
        ),
        reviewer=str(payload.get("reviewed_partial_by") or "agent-oracle"),
    )

def _block_child_before_upward_merge(
    *,
    project_dir: Path,
    child_task_id: str,
    result: LeadResult,
    reason: str,
    on_event: Any = None,
) -> LeadResult:
    logger.error("child %s blocked before upward merge: %s", child_task_id, reason)
    _record_task_merge_blocked_reason(
        project_dir=project_dir,
        task_id=child_task_id,
        result=result,
        reason=reason,
        origin="verification",
    )
    _v5r._emit(on_event, {
        "event": "child_merge_blocked",
        "task_id": child_task_id,
        "reason": reason,
    })
    return result

async def _ensure_child_merge_ready(
    *,
    project_dir: Path,
    child_task_id: str,
    child_worktree: Path,
    child_session_dir: Path,
    parent_integration_branch: str,
    original_intent: str,
    result: LeadResult,
    config: dict[str, Any],
    max_parallel: int,
    run_started_at: float | None,
    spec_path: Path,
    on_event: Any = None,
) -> LeadResult:
    """Post-refactor (2026-05-21): children's verdict reflects only their
    leaf-time self-verify outcome. The orchestrator no longer attempts
    upward merge or dispatches child-verify repair at child-finish time —
    those concerns belong to the integration Lead (the single merge
    authority, see lead-integration.md Step 1).

    This function previously: attempted git merge upward; on conflict
    dispatched a child-verify repair packet; on repair-pass refreshed the
    child's verdict; on residual partial applied LAND-with-annotation via
    the chokepoint. All of that work is now integration's responsibility.
    The integration Lead merges every child's `i2p/build/<task_id>`
    branch into the integration worktree and resolves conflicts via
    real `git merge` operations.

    Kept as a thin no-op pass-through for now to preserve the call
    site signature; the body will be removed entirely once the dispatch
    call site has been updated to skip this function. Until then, the
    function records reviewed-partial flags (a no-cost data
    propagation that integration also reads) and returns the result
    unchanged.

    Unused parameters retained to avoid breaking the call site during
    incremental rollout: child_worktree, child_session_dir,
    parent_integration_branch, original_intent, config, max_parallel,
    run_started_at, spec_path are all no-longer-used here but match the
    caller's keyword interface.
    """
    del child_worktree, child_session_dir, parent_integration_branch
    del original_intent, config, max_parallel, run_started_at, spec_path
    _record_reviewed_partial_if_present(project_dir, child_task_id, result)
    _v5r._emit(on_event, {
        "event": "child_merge_deferred_to_integration",
        "task_id": child_task_id,
        "verdict": result.verdict,
        "note": "integration Lead is the single merge authority post-refactor",
    })
    return result

class TerminalCause(enum.Enum):
    PRODUCT = "product"
    VERIFICATION = "verification"
    CONFLICT_RESIDUAL = "conflict_residual"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ENV_UNMEASURED = "env_unmeasured"
    INFRA_CORRUPT = "infra_corrupt"

class TerminalAction(enum.Enum):
    LAND_CONTINUE = "land_continue"
    LAND_STOP = "land_stop"
    HONEST_TERMINAL = "honest_terminal"

def resolve_terminal_outcome(*, cause: TerminalCause) -> TerminalAction:
    """The only land-vs-terminal decision. No default cause: a missing
    classification must be a hard error, never a silent default."""
    if cause is TerminalCause.INFRA_CORRUPT:
        return TerminalAction.HONEST_TERMINAL
    if cause in (TerminalCause.BUDGET_EXHAUSTED, TerminalCause.ENV_UNMEASURED):
        return TerminalAction.LAND_STOP
    return TerminalAction.LAND_CONTINUE

# Explicit origin/phase → cause map. Unmapped → PRODUCT (safe LAND) + warn.
_ORIGIN_CAUSE_MAP: dict[str, "TerminalCause"] = {
    "verification": TerminalCause.VERIFICATION,
    "foundation_clean_boot": TerminalCause.VERIFICATION,
    "contract": TerminalCause.VERIFICATION,
    "spec_contract": TerminalCause.VERIFICATION,
    "subtree_propagation": TerminalCause.CONFLICT_RESIDUAL,
    "merge_repair_helper": TerminalCause.CONFLICT_RESIDUAL,
    "merge": TerminalCause.CONFLICT_RESIDUAL,
    "union_guard": TerminalCause.CONFLICT_RESIDUAL,
    "budget": TerminalCause.BUDGET_EXHAUSTED,
    "deadline": TerminalCause.BUDGET_EXHAUSTED,
    "missing_toolchain": TerminalCause.ENV_UNMEASURED,
    "env": TerminalCause.ENV_UNMEASURED,
}


def _cause_from_origin(origin: str, phase: str | None) -> TerminalCause:
    """Derive a TerminalCause from the existing origin/phase strings the
    recording helpers already receive. Unmapped → PRODUCT (LAND): the safe
    fail direction (cannot hide a needed refusal — see chokepoint note)."""
    key = (origin or "").strip().lower()
    if key in _ORIGIN_CAUSE_MAP:
        return _ORIGIN_CAUSE_MAP[key]
    pkey = (phase or "").strip().lower()
    for token, cause in _ORIGIN_CAUSE_MAP.items():
        if token in key or (pkey and token in pkey):
            return cause
    logger.warning(
        "terminal chokepoint: unmapped origin=%r phase=%r → PRODUCT (LAND, "
        "safe default); add to _ORIGIN_CAUSE_MAP if a different cause fits",
        origin,
        phase,
    )
    return TerminalCause.PRODUCT

def _integration_terminal_verdict(
    *, blocks: bool, current_verdict: str, reason: str
) -> tuple[str, str]:
    """Post-agent integration terminal, routed through the chokepoint
    (Task #5, 2026-05-19; Linkboard e2e proved v5_runner.py:4757 was a
    deferred direct refusal that timed-out repair work hit). A post-agent
    smoke block is a VERIFICATION cause → LAND (`partial`) + annotation,
    never `merge_blocked`. `catastrophic` preserved; non-blocking passes
    through."""
    if current_verdict == VERDICT_CATASTROPHIC or not blocks:
        return current_verdict, (reason if blocks else "")
    if (
        resolve_terminal_outcome(cause=TerminalCause.VERIFICATION)
        is TerminalAction.HONEST_TERMINAL
    ):
        return "merge_blocked", reason
    return "partial", reason

def _record_task_merge_blocked_reason(
    *,
    project_dir: Path,
    task_id: str,
    result: LeadResult,
    reason: str,
    origin: str,
    structured_reason: dict[str, Any] | None = None,
) -> None:
    # Terminal chokepoint (v5 one-hard-gate keystone, 2026-05-19). Only
    # INFRA_CORRUPT refuses; every other cause LANDS + is annotated.
    cause = _cause_from_origin(origin, None)
    if resolve_terminal_outcome(cause=cause) is TerminalAction.HONEST_TERMINAL:
        metadata: dict[str, Any] = {
            "failure_reason": reason,
            "merge_blocked_origin": origin,
            "merge_blocked_reason": reason,
            "contract_amendment_retry_merge": False,
            "contract_amendment_retry_in_progress": False,
        }
        if structured_reason is not None:
            metadata["merge_blocked_structured_reason"] = structured_reason
        set_verdict_and_metadata(
            project_dir,
            task_id,
            "merge_blocked",
            cost_usd=result.cost_usd,
            metadata=metadata,
        )
        result.verdict = "merge_blocked"
        result.failure_reason = reason
        if result.verify_result is None:
            result.verify_result = {}
        if isinstance(result.verify_result, dict):
            result.verify_result["verdict"] = "merge_blocked"
            result.verify_result["summary"] = reason
            if structured_reason is not None:
                result.verify_result["structured_reason"] = structured_reason
        return

    # LAND: annotate, never refuse. The branch lands; the finding is
    # recorded for the proof packet, not used as a gate.
    land_metadata: dict[str, Any] = {
        "failure_reason": reason,
        "landed_with_annotation": True,
        "annotation_origin": origin,
        "annotation_detail": reason,
        "annotation_cause": cause.value,
        "contract_amendment_retry_merge": False,
        "contract_amendment_retry_in_progress": False,
    }
    if structured_reason is not None:
        land_metadata["annotation_structured_reason"] = structured_reason
    set_verdict_and_metadata(
        project_dir,
        task_id,
        "partial",
        cost_usd=result.cost_usd,
        metadata=land_metadata,
    )
    result.verdict = "partial"
    result.failure_reason = reason
    if result.verify_result is None:
        result.verify_result = {}
    if isinstance(result.verify_result, dict):
        result.verify_result["verdict"] = "partial"
        result.verify_result["summary"] = reason
        anns = result.verify_result.setdefault("annotations", [])
        if isinstance(anns, list):
            anns.append(
                {"origin": origin, "detail": reason, "cause": cause.value}
            )
        if structured_reason is not None:
            result.verify_result["structured_reason"] = structured_reason

def _record_structured_merge_failed(
    *,
    project_dir: Path,
    task_id: str,
    result: LeadResult,
    reason: str,
    origin: str,
    phase: str,
    structured_reason: dict[str, Any],
    on_event: Any = None,
) -> None:
    # Chokepoint-aware staging: only INFRA_CORRUPT keeps merge_blocked;
    # every other cause LANDS as 'partial' (delegate annotates). origin+phase
    # here vs origin-only in the delegate never diverge on the land/terminal
    # decision (unmapped → PRODUCT/LAND either way).
    _smf_terminal = (
        resolve_terminal_outcome(cause=_cause_from_origin(origin, phase))
        is TerminalAction.HONEST_TERMINAL
    )
    _smf_verdict = "merge_blocked" if _smf_terminal else "partial"
    try:
        result.verdict = _smf_verdict
        result.failure_reason = reason
        if not isinstance(result.verify_result, dict):
            result.verify_result = {}
        result.verify_result["verdict"] = _smf_verdict
        result.verify_result["summary"] = reason
        result.verify_result["structured_reason"] = structured_reason
    except Exception as exc:  # noqa: BLE001 - terminal fallback must not raise
        logger.warning(
            "failed to stage in-memory terminal reason for %s: %s",
            task_id,
            exc,
        )

    try:
        _record_task_merge_blocked_reason(
            project_dir=project_dir,
            task_id=task_id,
            result=result,
            reason=reason,
            origin=origin,
            structured_reason=structured_reason,
        )
    except Exception as exc:  # noqa: BLE001 - durable terminal recording is best-effort
        recording_error = {
            "type": type(exc).__name__,
            "message": str(exc),
            "_written_at": iso_timestamp(),
        }
        try:
            structured_reason["recording_error"] = recording_error
            if isinstance(result.verify_result, dict):
                result.verify_result["structured_reason"] = structured_reason
                result.verify_result["recording_error"] = recording_error
        except Exception:  # noqa: BLE001 - keep terminal recorder no-throw
            pass
        logger.warning(
            "failed to durably record merge_blocked for %s: %s",
            task_id,
            exc,
        )

    try:
        _v5r._emit(on_event, {
            "event": "merge_failed",
            "task_id": task_id,
            "phase": phase,
            "detail": reason,
            "structured_reason": structured_reason,
        })
    except Exception as exc:  # noqa: BLE001 - event sink must not reopen terminal path
        logger.warning("failed to emit structured merge_failed for %s: %s", task_id, exc)

def _record_foundation_contract_write_annotation(
    *,
    project_dir: Path,
    task_id: str,
    result: LeadResult,
    feedback: dict[str, Any],
    phase: str = "post_commit_annotation",
    on_event: Any = None,
) -> str:
    detail = _v5r._foundation_contract_write_block_detail(feedback)
    _record_structured_merge_failed(
        project_dir=project_dir,
        task_id=task_id,
        result=result,
        reason=detail,
        origin="foundation_contract_write_gate",
        phase=phase,
        structured_reason=feedback,
        on_event=on_event,
    )
    return detail

def _integration_union_guard_error_feedback(
    *,
    child_task_id: str,
    parent_integration_branch: str,
    source_branch: str,
    pre_merge_ref: str,
    exc: Exception,
    previous_feedback: dict[str, Any] | None = None,
    stale_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    feedback: dict[str, Any] = {
        "kind": "integration_union_guard_error",
        "step_id": "integration_union_guard",
        "message": (
            "integration union guard errored: "
            f"{type(exc).__name__}: {exc}"
        ),
        "task_id": child_task_id,
        "parent_integration_branch": parent_integration_branch,
        "source_branch": source_branch,
        "pre_merge_ref": pre_merge_ref,
        "exception": {
            "type": type(exc).__name__,
            "message": str(exc),
        },
        "_written_at": iso_timestamp(),
    }
    if previous_feedback is not None:
        feedback["previous_gate_feedback"] = previous_feedback
    if stale_feedback is not None:
        feedback["stale_feedback"] = stale_feedback
    return feedback

def _pre_merge_ref_unresolved_feedback(
    *,
    kind: str,
    child_task_id: str,
    parent_integration_branch: str,
    source_branch: str,
    detail: str,
    prior_repair_detail: str,
    previous_feedback: dict[str, Any] | None = None,
    stale_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    feedback: dict[str, Any] = {
        "kind": kind,
        "step_id": "child_merge_retry",
        "message": detail,
        "task_id": child_task_id,
        "parent_integration_branch": parent_integration_branch,
        "source_branch": source_branch,
        "prior_repair_detail": prior_repair_detail,
        "_written_at": iso_timestamp(),
    }
    if previous_feedback is not None:
        feedback["previous_gate_feedback"] = previous_feedback
    if stale_feedback is not None:
        feedback["stale_feedback"] = stale_feedback
    return feedback

def _child_merge_conflict_smoke_failed_feedback(
    *,
    child_task_id: str,
    parent_integration_branch: str,
    source_branch: str,
    pre_merge_ref: str,
    detail: str,
    oracle: dict[str, Any] | None = None,
    exc: Exception | None = None,
) -> dict[str, Any]:
    feedback: dict[str, Any] = {
        "kind": "child_merge_conflict_smoke_failed",
        "step_id": "child_merge_conflict_repair_smoke",
        "message": detail,
        "task_id": child_task_id,
        "parent_integration_branch": parent_integration_branch,
        "source_branch": source_branch,
        "pre_merge_ref": pre_merge_ref,
        "_written_at": iso_timestamp(),
    }
    if oracle is not None:
        feedback["oracle"] = oracle
    if exc is not None:
        feedback["exception"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
    return feedback

async def _commit_child_for_integration(
    *,
    project_dir: Path,
    child_task_id: str,
    child_worktree: Path,
    parent_integration_branch: str,
    result: LeadResult,
    on_event: Any = None,
) -> None:
    """Commit any uncommitted worktree changes onto the child's build
    branch and record the worktree-level foundation-contract annotation.

    Does NOT merge into the parent integration branch — that's the
    integration Lead's job (Step 1 of `lead-integration.md`). This
    function is the orchestrator's only post-child-finish job: leave
    the child's branch in a state where integration can `git merge
    i2p/build/<task_id>` and see all the work.
    """
    from otto.v5_branching import child_branch_name, commit_worktree

    source_branch = child_branch_name(child_task_id)
    commit_msg = f"v5 task {child_task_id}: {result.verdict}"
    worktree_contract_violation = _v5r._foundation_contract_write_feedback(
        project_dir=project_dir,
        acting_task_id=child_task_id,
        parent_integration_branch=parent_integration_branch,
        changed_paths=_v5r._git_diff_name_only(child_worktree),
        operation="child_worktree_commit",
    )
    ok, detail = commit_worktree(worktree_path=child_worktree, message=commit_msg)
    if not ok:
        logger.warning("commit_worktree(%s) failed: %s", child_task_id, detail)
        feedback = {
            "kind": "child_commit_failed",
            "step_id": "child_commit",
            "message": detail,
            "task_id": child_task_id,
            "source_branch": source_branch,
            "parent_integration_branch": parent_integration_branch,
            "_written_at": iso_timestamp(),
        }
        _record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=detail,
            origin="commit",
            phase="commit",
            structured_reason=feedback,
            on_event=on_event,
        )
        return
    if worktree_contract_violation is not None:
        annotate_detail = _v5r._foundation_contract_write_block_detail(
            worktree_contract_violation
        )
        _record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=annotate_detail,
            origin="foundation_contract_write_gate",
            phase="post_commit_annotation",
            structured_reason=worktree_contract_violation,
            on_event=on_event,
        )
    _v5r._emit(on_event, {
        "event": "child_committed_merge_deferred_to_integration",
        "task_id": child_task_id,
        "source_branch": source_branch,
        "parent_integration_branch": parent_integration_branch,
    })


async def _merge_child_branch(
    *,
    project_dir: Path,
    child_task_id: str,
    child_worktree: Path,
    child_session_dir: Path,
    parent_integration_branch: str,
    result: LeadResult,
    config: dict[str, Any],
    on_event: Any = None,
) -> None:
    """Commit the child's worktree changes and merge into parent's integration branch.

    Best-effort: on any failure, mark the child's verdict as merge_blocked
    (without crashing the parent run).
    """
    from otto.v5_branching import (
        child_branch_name,
        commit_worktree,
        merge_child_into_integration,
    )

    source_branch = child_branch_name(child_task_id)
    commit_msg = f"v5 task {child_task_id}: {result.verdict}"
    # Foundation-contract write violations are annotation-only. They must not
    # refuse before the child's coherent branch has landed.
    worktree_contract_violation = _v5r._foundation_contract_write_feedback(
        project_dir=project_dir,
        acting_task_id=child_task_id,
        parent_integration_branch=parent_integration_branch,
        changed_paths=_v5r._git_diff_name_only(child_worktree),
        operation="child_worktree_commit",
    )
    ok, detail = commit_worktree(worktree_path=child_worktree, message=commit_msg)
    if not ok:
        logger.warning("commit_worktree(%s) failed: %s", child_task_id, detail)
        feedback = {
            "kind": "child_commit_failed",
            "step_id": "child_commit",
            "message": detail,
            "task_id": child_task_id,
            "source_branch": source_branch,
            "parent_integration_branch": parent_integration_branch,
            "_written_at": iso_timestamp(),
        }
        _record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=detail,
            origin="commit",
            phase="commit",
            structured_reason=feedback,
            on_event=on_event,
        )
        return
    if worktree_contract_violation is not None:
        annotate_detail = _v5r._foundation_contract_write_block_detail(
            worktree_contract_violation
        )
        _record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=annotate_detail,
            origin="foundation_contract_write_gate",
            phase="post_commit_annotation",
            structured_reason=worktree_contract_violation,
            on_event=on_event,
        )

    pre_merge_ref = _v5r._git_capture(project_dir, ["rev-parse", parent_integration_branch])
    branch_delta_contract_violation = _v5r._foundation_contract_write_feedback(
        project_dir=project_dir,
        acting_task_id=child_task_id,
        parent_integration_branch=parent_integration_branch,
        changed_paths=_v5r._git_changed_paths_between_refs(project_dir, pre_merge_ref, source_branch),
        operation="child_branch_merge_delta",
    )
    if branch_delta_contract_violation is not None:
        annotate_detail = _v5r._foundation_contract_write_block_detail(
            branch_delta_contract_violation
        )
        _record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=annotate_detail,
            origin="foundation_contract_write_gate",
            phase="pre_merge_annotation",
            structured_reason=branch_delta_contract_violation,
            on_event=on_event,
        )
    try:
        ok, detail = merge_child_into_integration(
            project_dir=project_dir,
            child_task_id=child_task_id,
            parent_integration_branch=parent_integration_branch,
        )
    except MergeWorktreeDirtyError as exc:
        ok = False
        detail = str(exc)
    except Exception as exc:  # noqa: BLE001 - merge path must not escape post-commit
        ok = False
        detail = f"merge_child_into_integration crashed: {type(exc).__name__}: {exc}"
    if not ok and _v5r._looks_like_merge_conflict(detail):
        try:
            repaired, repair_detail = await _v5r._repair_child_merge_conflict_once(
                project_dir=project_dir,
                child_task_id=child_task_id,
                child_worktree=child_worktree,
                child_session_dir=child_session_dir,
                parent_integration_branch=parent_integration_branch,
                result=result,
                config=config,
                original_detail=detail,
                on_event=on_event,
            )
        except Exception as exc:  # noqa: BLE001 - repair helper crash is terminal-structured
            feedback = _v5r._child_repair_helper_crashed_feedback(
                child_task_id=child_task_id,
                parent_integration_branch=parent_integration_branch,
                source_branch=source_branch,
                pre_merge_ref=pre_merge_ref,
                origin="merge_conflict_repair",
                phase="merge_conflict_repair",
                exc=exc,
            )
            _record_structured_merge_failed(
                project_dir=project_dir,
                task_id=child_task_id,
                result=result,
                reason=str(feedback["message"]),
                origin="merge_conflict_repair",
                phase="merge_conflict_repair",
                structured_reason=feedback,
                on_event=on_event,
            )
            return
        if repaired:
            pre_merge_ref = _v5r._git_capture(project_dir, ["rev-parse", parent_integration_branch])
            conflict_repair_contract_violation = _v5r._foundation_contract_write_feedback(
                project_dir=project_dir,
                acting_task_id=child_task_id,
                parent_integration_branch=parent_integration_branch,
                changed_paths=_v5r._git_changed_paths_between_refs(project_dir, pre_merge_ref, source_branch),
                operation="merge_after_conflict_repair_delta",
            )
            if conflict_repair_contract_violation is not None:
                annotate_detail = _v5r._foundation_contract_write_block_detail(
                    conflict_repair_contract_violation
                )
                _record_structured_merge_failed(
                    project_dir=project_dir,
                    task_id=child_task_id,
                    result=result,
                    reason=annotate_detail,
                    origin="foundation_contract_write_gate",
                    phase="merge_conflict_repair_annotation",
                    structured_reason=conflict_repair_contract_violation,
                    on_event=on_event,
                )
            try:
                ok, detail = merge_child_into_integration(
                    project_dir=project_dir,
                    child_task_id=child_task_id,
                    parent_integration_branch=parent_integration_branch,
                )
            except MergeWorktreeDirtyError as exc:
                ok = False
                detail = str(exc)
            except Exception as exc:  # noqa: BLE001 - merge path must not escape post-commit
                ok = False
                detail = f"merge after conflict repair crashed: {type(exc).__name__}: {exc}"
            if ok:
                try:
                    oracle = _v5r._run_integration_smoke_preflight(
                        worktree_path=project_dir,
                        task_id=child_task_id,
                        phase="child_merge_conflict_repair",
                        spec_path=child_session_dir / "spec" / "spec.json",
                        journey_artifact_dir=(
                            child_session_dir
                            / "journeys"
                            / safe_slug("child_merge_conflict_repair", max_len=48)
                        ),
                        on_event=on_event,
                    )
                except Exception as exc:  # noqa: BLE001 - terminal block must stay structured
                    detail = (
                        "Child merge conflict repair smoke oracle crashed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    feedback = _child_merge_conflict_smoke_failed_feedback(
                        child_task_id=child_task_id,
                        parent_integration_branch=parent_integration_branch,
                        source_branch=source_branch,
                        pre_merge_ref=pre_merge_ref,
                        detail=detail,
                        exc=exc,
                    )
                    _record_structured_merge_failed(
                        project_dir=project_dir,
                        task_id=child_task_id,
                        result=result,
                        reason=detail,
                        origin="child_merge_conflict_smoke",
                        phase="child_merge_conflict_repair",
                        structured_reason=feedback,
                        on_event=on_event,
                    )
                    return
                try:
                    smoke_blocks = (
                        _v5r._preflight_repair_escalated(oracle)
                        or _v5r._integration_smoke_blocks(oracle)
                    )
                    detail = _v5r._preflight_blocking_summary(
                        "Child merge conflict repair smoke oracle failed",
                        oracle,
                    ) if smoke_blocks else ""
                except Exception as exc:  # noqa: BLE001 - smoke evaluation must stay structured
                    detail = (
                        "Child merge conflict repair smoke oracle evaluation crashed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    feedback = _child_merge_conflict_smoke_failed_feedback(
                        child_task_id=child_task_id,
                        parent_integration_branch=parent_integration_branch,
                        source_branch=source_branch,
                        pre_merge_ref=pre_merge_ref,
                        detail=detail,
                        oracle=oracle if isinstance(oracle, dict) else None,
                        exc=exc,
                    )
                    _record_structured_merge_failed(
                        project_dir=project_dir,
                        task_id=child_task_id,
                        result=result,
                        reason=detail,
                        origin="child_merge_conflict_smoke",
                        phase="child_merge_conflict_repair",
                        structured_reason=feedback,
                        on_event=on_event,
                    )
                    return
                if smoke_blocks:
                    if _v5r._route_out_of_scope_smoke_failure(
                        project_dir=project_dir,
                        child_task_id=child_task_id,
                        child_worktree=child_worktree,
                        child_session_dir=child_session_dir,
                        parent_integration_branch=parent_integration_branch,
                        source_branch=source_branch,
                        pre_merge_ref=pre_merge_ref,
                        smoke_payload=oracle,
                        result=result,
                        on_event=on_event,
                    ):
                        return
                    try:
                        oracle = await _v5r._run_integration_smoke_preflight_with_repair(
                            project_dir=project_dir,
                            worktree_path=project_dir,
                            task_id=child_task_id,
                            phase="child_merge_conflict_repair",
                            session_dir=child_session_dir,
                            config=config,
                            integration_branch=parent_integration_branch,
                            allowed_paths=_v5r._task_owned_paths(get_task(project_dir, child_task_id) or {}),
                            scope_policy="allowed_paths",
                            on_event=on_event,
                        )
                        if not (
                            _v5r._preflight_repair_escalated(oracle)
                            or _v5r._integration_smoke_blocks(oracle)
                        ):
                            smoke_blocks = False
                    except Exception as exc:  # noqa: BLE001 - terminal block must stay structured
                        detail = (
                            "Child merge conflict repair smoke oracle crashed: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        feedback = _child_merge_conflict_smoke_failed_feedback(
                            child_task_id=child_task_id,
                            parent_integration_branch=parent_integration_branch,
                            source_branch=source_branch,
                            pre_merge_ref=pre_merge_ref,
                            detail=detail,
                            oracle=oracle if isinstance(oracle, dict) else None,
                            exc=exc,
                        )
                        _record_structured_merge_failed(
                            project_dir=project_dir,
                            task_id=child_task_id,
                            result=result,
                            reason=detail,
                            origin="child_merge_conflict_smoke",
                            phase="child_merge_conflict_repair",
                            structured_reason=feedback,
                            on_event=on_event,
                        )
                        return
                if smoke_blocks:
                    detail = _v5r._preflight_blocking_summary(
                        "Child merge conflict repair smoke oracle failed",
                        oracle,
                    )
                    feedback = _child_merge_conflict_smoke_failed_feedback(
                        child_task_id=child_task_id,
                        parent_integration_branch=parent_integration_branch,
                        source_branch=source_branch,
                        pre_merge_ref=pre_merge_ref,
                        detail=detail,
                        oracle=oracle,
                    )
                    _record_structured_merge_failed(
                        project_dir=project_dir,
                        task_id=child_task_id,
                        result=result,
                        reason=detail,
                        origin="child_merge_conflict_smoke",
                        phase="child_merge_conflict_repair",
                        structured_reason=feedback,
                        on_event=on_event,
                    )
                    return
            else:
                try:
                    retry = await _v5r._repair_child_upward_merge_after_failure(
                        project_dir=project_dir,
                        child_task_id=child_task_id,
                        child_worktree=child_worktree,
                        child_session_dir=child_session_dir,
                        parent_integration_branch=parent_integration_branch,
                        result=result,
                        config=config,
                        detail=detail,
                        prior_repair_detail=repair_detail,
                        origin="stale_target_merge_gate",
                        terminal_phase="merge",
                        source_branch=source_branch,
                        run_smoke_preflight=True,
                        on_event=on_event,
                    )
                except Exception as exc:  # noqa: BLE001 - repair helper crash is terminal-structured
                    feedback = _v5r._child_repair_helper_crashed_feedback(
                        child_task_id=child_task_id,
                        parent_integration_branch=parent_integration_branch,
                        source_branch=source_branch,
                        pre_merge_ref=pre_merge_ref,
                        origin="stale_target_merge_gate",
                        phase="merge",
                        exc=exc,
                    )
                    _record_structured_merge_failed(
                        project_dir=project_dir,
                        task_id=child_task_id,
                        result=result,
                        reason=str(feedback["message"]),
                        origin="stale_target_merge_gate",
                        phase="merge",
                        structured_reason=feedback,
                        on_event=on_event,
                    )
                    return
                if retry.terminal_recorded:
                    return
                ok = retry.ok
                detail = retry.detail
                pre_merge_ref = retry.pre_merge_ref
        else:
            detail = f"{detail}; conflict repair attempt: {repair_detail}"
    if not ok and not _v5r._looks_like_merge_conflict(detail):
        mechanical_action, mechanical_detail = _v5r._handle_mechanical_merge_blocker(
            detail=detail,
            project_dir=project_dir,
            child_task_id=child_task_id,
            on_event=on_event,
        )
        if mechanical_action == "retry":
            try:
                ok, detail = merge_child_into_integration(
                    project_dir=project_dir,
                    child_task_id=child_task_id,
                    parent_integration_branch=parent_integration_branch,
                )
            except MergeWorktreeDirtyError as exc:
                ok = False
                detail = str(exc)
            except Exception as exc:  # noqa: BLE001 - merge path must not escape post-commit
                ok = False
                detail = f"merge after mechanical blocker commit crashed: {type(exc).__name__}: {exc}"
        elif mechanical_action == "terminal":
            detail = (
                "mechanical merge blocker could not be resolved deterministically: "
                f"{mechanical_detail}"
            )
        if ok:
            pass
        elif mechanical_action == "terminal":
            logger.warning("merge_child_into_integration(%s) failed: %s", child_task_id, detail)
            feedback = {
                "kind": "upward_merge_gate_mechanical_blocked",
                "step_id": "upward_merge_gate",
                "message": detail,
                "task_id": child_task_id,
                "source_branch": source_branch,
                "parent_integration_branch": parent_integration_branch,
                "pre_merge_ref": pre_merge_ref,
                "_written_at": iso_timestamp(),
            }
            _record_structured_merge_failed(
                project_dir=project_dir,
                task_id=child_task_id,
                result=result,
                reason=detail,
                origin="upward_merge_gate",
                phase="merge",
                structured_reason=feedback,
                on_event=on_event,
            )
            return
    if not ok and not _v5r._looks_like_merge_conflict(detail):
        try:
            repaired, repair_detail = await _v5r._repair_child_upward_merge_gate_once(
                project_dir=project_dir,
                child_task_id=child_task_id,
                child_worktree=child_worktree,
                child_session_dir=child_session_dir,
                parent_integration_branch=parent_integration_branch,
                result=result,
                config=config,
                original_detail=detail,
                on_event=on_event,
            )
        except Exception as exc:  # noqa: BLE001 - repair helper crash is terminal-structured
            feedback = _v5r._child_repair_helper_crashed_feedback(
                child_task_id=child_task_id,
                parent_integration_branch=parent_integration_branch,
                source_branch=source_branch,
                pre_merge_ref=pre_merge_ref,
                origin="upward_merge_gate",
                phase="upward_merge_gate",
                exc=exc,
            )
            _record_structured_merge_failed(
                project_dir=project_dir,
                task_id=child_task_id,
                result=result,
                reason=str(feedback["message"]),
                origin="upward_merge_gate",
                phase="upward_merge_gate",
                structured_reason=feedback,
                on_event=on_event,
            )
            return
        if repaired:
            pre_merge_ref = _v5r._git_capture(project_dir, ["rev-parse", parent_integration_branch])
            upward_repair_contract_violation = _v5r._foundation_contract_write_feedback(
                project_dir=project_dir,
                acting_task_id=child_task_id,
                parent_integration_branch=parent_integration_branch,
                changed_paths=_v5r._git_changed_paths_between_refs(project_dir, pre_merge_ref, source_branch),
                operation="merge_after_upward_repair_delta",
            )
            if upward_repair_contract_violation is not None:
                annotate_detail = _v5r._foundation_contract_write_block_detail(
                    upward_repair_contract_violation
                )
                _record_structured_merge_failed(
                    project_dir=project_dir,
                    task_id=child_task_id,
                    result=result,
                    reason=annotate_detail,
                    origin="foundation_contract_write_gate",
                    phase="upward_merge_gate_annotation",
                    structured_reason=upward_repair_contract_violation,
                    on_event=on_event,
                )
            try:
                ok, detail = merge_child_into_integration(
                    project_dir=project_dir,
                    child_task_id=child_task_id,
                    parent_integration_branch=parent_integration_branch,
                )
            except MergeWorktreeDirtyError as exc:
                ok = False
                detail = str(exc)
            except Exception as exc:  # noqa: BLE001 - merge path must not escape post-commit
                ok = False
                detail = f"merge after upward repair crashed: {type(exc).__name__}: {exc}"
        if not ok:
            detail = f"{detail}; upward merge gate repair attempt: {repair_detail}"
    if not ok:
        logger.warning("merge_child_into_integration(%s) failed: %s", child_task_id, detail)
        feedback = {
            "kind": "upward_merge_gate_blocked",
            "step_id": "upward_merge_gate",
            "message": detail,
            "task_id": child_task_id,
            "source_branch": source_branch,
            "parent_integration_branch": parent_integration_branch,
            "pre_merge_ref": pre_merge_ref,
            "_written_at": iso_timestamp(),
        }
        _record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=detail,
            origin="upward_merge_gate",
            phase="merge",
            structured_reason=feedback,
            on_event=on_event,
        )
        return

    if not pre_merge_ref:
        reason = "integration union guard could not resolve pre-merge ref"
        feedback = _pre_merge_ref_unresolved_feedback(
            kind="integration_union_pre_merge_ref_unresolved",
            child_task_id=child_task_id,
            parent_integration_branch=parent_integration_branch,
            source_branch=source_branch,
            detail=reason,
            prior_repair_detail="",
        )
        _record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=reason,
            origin="integration_union_guard",
            phase="integration_union_guard",
            structured_reason=feedback,
            on_event=on_event,
        )
        return

    try:
        union_feedback = _v5r._record_and_check_integration_union(
            project_dir=project_dir,
            parent_integration_branch=parent_integration_branch,
            child_task_id=child_task_id,
            source_branch=source_branch,
            pre_merge_ref=pre_merge_ref,
        )
    except Exception as exc:  # noqa: BLE001 - keep union guard failures structured
        feedback = _integration_union_guard_error_feedback(
            child_task_id=child_task_id,
            parent_integration_branch=parent_integration_branch,
            source_branch=source_branch,
            pre_merge_ref=pre_merge_ref,
            exc=exc,
        )
        _record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=str(feedback["message"]),
            origin="integration_union_guard",
            phase="integration_union_guard",
            structured_reason=feedback,
            on_event=on_event,
        )
        return
    if union_feedback is not None:
        detail = str(union_feedback.get("message") or _integration_union_reason_text(union_feedback))
        _v5r._emit(on_event, {
            "event": "integration_union_incomplete",
            "task_id": child_task_id,
            "into": parent_integration_branch,
            "detail": detail,
            "structured_reason": union_feedback,
        })
        parent_task_id = _v5r._parent_task_id_for_child(
            project_dir,
            child_task_id,
            parent_integration_branch,
        )
        amendment_contract = _v5r._foundation_contract_for_feedback_path(
            project_dir=project_dir,
            parent_task_id=parent_task_id,
            child_task_id=child_task_id,
            feedback=union_feedback,
        )
        if amendment_contract is not None:
            contract_path = _v5r._normalize_contract_path(str(amendment_contract.get("path") or ""))
            owner_id = str(amendment_contract.get("owner_task_id") or "").strip()
            current_attempts = _v5r._contract_amendment_attempt_count(
                get_task(project_dir, child_task_id) or {},
                contract_path,
            )
            if current_attempts >= _v5r.MAX_CONTRACT_AMENDMENT_ATTEMPTS:
                feedback = _v5r._contract_amendment_exhausted_feedback(
                    child_task_id=child_task_id,
                    parent_task_id=parent_task_id,
                    parent_integration_branch=parent_integration_branch,
                    source_branch=source_branch,
                    pre_merge_ref=pre_merge_ref,
                    contract_path=contract_path,
                    owner_id=owner_id,
                    union_feedback=union_feedback,
                    attempt_count=current_attempts,
                )
                _record_structured_merge_failed(
                    project_dir=project_dir,
                    task_id=child_task_id,
                    result=result,
                    reason=str(feedback["message"]),
                    origin="contract_amendment",
                    phase="foundation_contract_amendment",
                    structured_reason=feedback,
                    on_event=on_event,
                )
                return
            _v5r._schedule_foundation_contract_amendment(
                project_dir=project_dir,
                child_task_id=child_task_id,
                child_worktree=child_worktree,
                child_session_dir=child_session_dir,
                parent_task_id=parent_task_id,
                parent_integration_branch=parent_integration_branch,
                source_branch=source_branch,
                pre_merge_ref=pre_merge_ref,
                union_feedback=union_feedback,
                contract=amendment_contract,
                on_event=on_event,
            )
            return
        try:
            repaired, repair_detail = await _v5r._repair_child_upward_merge_gate_once(
                project_dir=project_dir,
                child_task_id=child_task_id,
                child_worktree=child_worktree,
                child_session_dir=child_session_dir,
                parent_integration_branch=parent_integration_branch,
                result=result,
                config=config,
                original_detail=detail,
                on_event=on_event,
                gate_feedback=union_feedback,
                origin="integration_union_guard",
            )
        except Exception as exc:  # noqa: BLE001 - repair helper crash is terminal-structured
            feedback = _v5r._child_repair_helper_crashed_feedback(
                child_task_id=child_task_id,
                parent_integration_branch=parent_integration_branch,
                source_branch=source_branch,
                pre_merge_ref=pre_merge_ref,
                origin="integration_union_guard",
                phase="integration_union_guard",
                exc=exc,
                previous_feedback=union_feedback,
            )
            _record_structured_merge_failed(
                project_dir=project_dir,
                task_id=child_task_id,
                result=result,
                reason=str(feedback["message"]),
                origin="integration_union_guard",
                phase="integration_union_guard",
                structured_reason=feedback,
                on_event=on_event,
            )
            return
        if not repaired:
            reason = f"{detail}; union repair attempt: {repair_detail}"
            _record_structured_merge_failed(
                project_dir=project_dir,
                task_id=child_task_id,
                result=result,
                reason=reason,
                origin="integration_union_guard",
                phase="integration_union_guard",
                structured_reason=union_feedback,
                on_event=on_event,
            )
            return

        pre_merge_ref = _v5r._git_capture(project_dir, ["rev-parse", parent_integration_branch])
        if not pre_merge_ref:
            reason = (
                "integration union repair retry could not resolve pre-merge ref; "
                f"union repair attempt: {repair_detail}; "
                f"original refusal: {union_feedback.get('message')}"
            )
            feedback = _pre_merge_ref_unresolved_feedback(
                kind="integration_union_pre_merge_ref_unresolved",
                child_task_id=child_task_id,
                parent_integration_branch=parent_integration_branch,
                source_branch=source_branch,
                detail=reason,
                prior_repair_detail=repair_detail,
                previous_feedback=union_feedback,
            )
            _record_structured_merge_failed(
                project_dir=project_dir,
                task_id=child_task_id,
                result=result,
                reason=reason,
                origin="integration_union_guard",
                phase="integration_union_guard",
                structured_reason=feedback,
                on_event=on_event,
            )
            return
        union_repair_contract_violation = _v5r._foundation_contract_write_feedback(
            project_dir=project_dir,
            acting_task_id=child_task_id,
            parent_integration_branch=parent_integration_branch,
            changed_paths=_v5r._git_changed_paths_between_refs(project_dir, pre_merge_ref, source_branch),
            operation="merge_after_integration_union_repair_delta",
        )
        if union_repair_contract_violation is not None:
            annotate_detail = _v5r._foundation_contract_write_block_detail(
                union_repair_contract_violation
            )
            _record_structured_merge_failed(
                project_dir=project_dir,
                task_id=child_task_id,
                result=result,
                reason=annotate_detail,
                origin="foundation_contract_write_gate",
                phase="integration_union_guard_annotation",
                structured_reason=union_repair_contract_violation,
                on_event=on_event,
            )
        try:
            ok, detail = merge_child_into_integration(
                project_dir=project_dir,
                child_task_id=child_task_id,
                parent_integration_branch=parent_integration_branch,
            )
        except MergeWorktreeDirtyError as exc:
            ok = False
            detail = str(exc)
        except Exception as exc:  # noqa: BLE001 - merge path must not escape post-commit
            ok = False
            detail = f"merge after integration union repair crashed: {type(exc).__name__}: {exc}"
        if not ok:
            try:
                retry = await _v5r._repair_child_upward_merge_after_failure(
                    project_dir=project_dir,
                    child_task_id=child_task_id,
                    child_worktree=child_worktree,
                    child_session_dir=child_session_dir,
                    parent_integration_branch=parent_integration_branch,
                    result=result,
                    config=config,
                    detail=detail,
                    prior_repair_detail=repair_detail,
                    origin="integration_union_guard",
                    terminal_phase="integration_union_guard",
                    source_branch=source_branch,
                    previous_feedback=union_feedback,
                    on_event=on_event,
                )
            except Exception as exc:  # noqa: BLE001 - repair helper crash is terminal-structured
                feedback = _v5r._child_repair_helper_crashed_feedback(
                    child_task_id=child_task_id,
                    parent_integration_branch=parent_integration_branch,
                    source_branch=source_branch,
                    pre_merge_ref=pre_merge_ref,
                    origin="integration_union_guard",
                    phase="integration_union_guard",
                    exc=exc,
                    previous_feedback=union_feedback,
                )
                _record_structured_merge_failed(
                    project_dir=project_dir,
                    task_id=child_task_id,
                    result=result,
                    reason=str(feedback["message"]),
                    origin="integration_union_guard",
                    phase="integration_union_guard",
                    structured_reason=feedback,
                    on_event=on_event,
                )
                return
            if retry.terminal_recorded:
                return
            ok = retry.ok
            detail = retry.detail
            pre_merge_ref = retry.pre_merge_ref

        try:
            followup_feedback = _v5r._record_and_check_integration_union(
                project_dir=project_dir,
                parent_integration_branch=parent_integration_branch,
                child_task_id=child_task_id,
                source_branch=source_branch,
                pre_merge_ref=pre_merge_ref,
            )
        except Exception as exc:  # noqa: BLE001 - keep union guard failures structured
            feedback = _integration_union_guard_error_feedback(
                child_task_id=child_task_id,
                parent_integration_branch=parent_integration_branch,
                source_branch=source_branch,
                pre_merge_ref=pre_merge_ref,
                exc=exc,
                previous_feedback=union_feedback,
            )
            _record_structured_merge_failed(
                project_dir=project_dir,
                task_id=child_task_id,
                result=result,
                reason=str(feedback["message"]),
                origin="integration_union_guard",
                phase="integration_union_guard",
                structured_reason=feedback,
                on_event=on_event,
            )
            return
        if followup_feedback is not None:
            followup_detail = str(
                followup_feedback.get("message")
                or _integration_union_reason_text(followup_feedback)
            )
            try:
                retry = await _v5r._repair_child_upward_merge_after_failure(
                    project_dir=project_dir,
                    child_task_id=child_task_id,
                    child_worktree=child_worktree,
                    child_session_dir=child_session_dir,
                    parent_integration_branch=parent_integration_branch,
                    result=result,
                    config=config,
                    detail=followup_detail,
                    prior_repair_detail=repair_detail,
                    origin="integration_union_guard",
                    terminal_phase="integration_union_guard",
                    source_branch=source_branch,
                    previous_feedback=followup_feedback,
                    check_union_after_merge=True,
                    emit_union_feedback=True,
                    on_event=on_event,
                )
            except Exception as exc:  # noqa: BLE001 - repair helper crash is terminal-structured
                feedback = _v5r._child_repair_helper_crashed_feedback(
                    child_task_id=child_task_id,
                    parent_integration_branch=parent_integration_branch,
                    source_branch=source_branch,
                    pre_merge_ref=pre_merge_ref,
                    origin="integration_union_guard",
                    phase="integration_union_guard",
                    exc=exc,
                    previous_feedback=followup_feedback,
                )
                _record_structured_merge_failed(
                    project_dir=project_dir,
                    task_id=child_task_id,
                    result=result,
                    reason=str(feedback["message"]),
                    origin="integration_union_guard",
                    phase="integration_union_guard",
                    structured_reason=feedback,
                    on_event=on_event,
                )
                return
            if retry.terminal_recorded:
                return
            ok = retry.ok
            detail = retry.detail
            pre_merge_ref = retry.pre_merge_ref

    if not ok:
        reason = f"child merge path ended without success: {detail}"
        feedback = {
            "kind": "child_merge_path_incomplete",
            "step_id": "child_merge",
            "message": reason,
            "task_id": child_task_id,
            "source_branch": source_branch,
            "parent_integration_branch": parent_integration_branch,
            "pre_merge_ref": pre_merge_ref,
            "_written_at": iso_timestamp(),
        }
        _record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=reason,
            origin="child_merge",
            phase="merge",
            structured_reason=feedback,
            on_event=on_event,
        )
        return

    _v5r._emit(on_event, {
        "event": "merged",
        "task_id": child_task_id,
        "into": parent_integration_branch,
    })
