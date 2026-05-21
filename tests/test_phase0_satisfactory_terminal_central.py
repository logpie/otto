"""Phase 0 (plan-checkpoint-resume-v2.md): centralize the "is this task
in a satisfactory terminal state" predicate so annotated partials (chokepoint
LAND path) are accepted by both upward-merge AND dependency-satisfaction.

Pre-2026-05-20, three sites had independent strict implementations:
  - _child_result_allows_upward_merge (v5_runner)
  - _task_entry_allows_upward_merge   (v5_runner)
  - _verdict_satisfies_dependency     (subtask.py)

They all required `pass` OR `partial + reviewed_partial`. Annotated
partials (chokepoint LAND path) set `landed_with_annotation=True` but
NOT `review_state="reviewed_partial"`, so the work never merged + never
satisfied dependents.

Phase 0 introduces `task_graph.entry_is_satisfactory_terminal` as the
single source of truth, used by all four sites (the three above plus
the new `_build_decomp_runtime_context` caller).

Codex Plan Gate APPROVED at R5; R1#3 was the critical finding.
"""
from __future__ import annotations

import inspect
import re

import pytest

from otto.queue.task_graph import entry_is_satisfactory_terminal
from otto.queue.subtask import _verdict_satisfies_dependency
from otto.v5_runner import _task_entry_allows_upward_merge


# --- The wider semantics ---------------------------------------------


@pytest.mark.parametrize("entry,expected", [
    # Pass alone is satisfactory
    ({"verdict": "pass"}, True),
    # Human-reviewed partial is satisfactory (existing path)
    ({"verdict": "partial", "review_state": "reviewed_partial"}, True),
    # NEW: otto-annotated partial via chokepoint LAND is satisfactory
    ({"verdict": "partial", "landed_with_annotation": True}, True),
    # Combination: both flags set → satisfactory
    ({"verdict": "partial", "review_state": "reviewed_partial",
      "landed_with_annotation": True}, True),
    # Bare partial without annotation or review → NOT satisfactory
    ({"verdict": "partial"}, False),
    ({"verdict": "partial", "review_state": ""}, False),
    # merge_blocked → never satisfactory regardless of other flags
    ({"verdict": "merge_blocked", "landed_with_annotation": True}, False),
    # unverified → not satisfactory
    ({"verdict": "unverified"}, False),
    ({"verdict": "unverified", "landed_with_annotation": True}, False),
    # Stale merge_blocked metadata disqualifies (even if verdict reset)
    ({"verdict": "pass", "merge_blocked_reason": "stale"}, False),
    ({"verdict": "partial", "landed_with_annotation": True,
      "merge_blocked_structured_reason": {"kind": "stale"}}, False),
    # blocked_on_task_id or blocked_pending_contract_amendment → not satisfactory
    ({"verdict": "pass", "blocked_pending_contract_amendment": True}, False),
    ({"verdict": "pass", "blocked_on_task_id": "v5-other"}, False),
    # Garbage input
    (None, False),
    ({}, False),
    ([], False),
])
def test_entry_is_satisfactory_terminal_truth_table(entry, expected):
    assert entry_is_satisfactory_terminal(entry) is expected, (
        f"entry={entry!r} expected={expected}"
    )


# --- The wider semantics propagate through all callers ---------------


def test_task_entry_allows_upward_merge_accepts_annotated_partial():
    """The Codex R1#3 critical case: annotated partial (chokepoint LAND
    path) must now pass `_task_entry_allows_upward_merge`."""
    entry = {"verdict": "partial", "landed_with_annotation": True}
    assert _task_entry_allows_upward_merge(entry) is True


def test_task_entry_allows_upward_merge_rejects_bare_partial():
    """Sanity: bare partial without annotation or review still refused."""
    entry = {"verdict": "partial"}
    assert _task_entry_allows_upward_merge(entry) is False


def test_verdict_satisfies_dependency_accepts_annotated_partial_entry():
    """take_ready() / _globally_dependency_satisfied_task_ids feed full
    task entries — annotated partials must unblock dependents."""
    entry = {"verdict": "partial", "landed_with_annotation": True}
    assert _verdict_satisfies_dependency(entry) is True


def test_verdict_satisfies_dependency_legacy_two_arg_form_still_works():
    """Backward compat: legacy callers pass (verdict, review_state). Pass
    + None still passes; partial + reviewed_partial still passes."""
    assert _verdict_satisfies_dependency("pass") is True
    assert _verdict_satisfies_dependency("partial", "reviewed_partial") is True
    assert _verdict_satisfies_dependency("partial") is False
    assert _verdict_satisfies_dependency("merge_blocked") is False


# --- Drift guard: source-level ratchet against future divergence -----


def test_no_caller_duplicates_the_predicate_logic():
    """Ratchet against the dup-drift class that originally caused the
    cascade. If a future refactor inlines the predicate logic at any
    site, this fails loudly.

    Looks for the specific shape of the OLD strict predicate:
        verdict == "partial" and ... == "reviewed_partial"
    in `v5_runner._task_entry_allows_upward_merge`,
    `v5_runner._child_result_allows_upward_merge`, and
    `subtask._verdict_satisfies_dependency` bodies. If found, fail.
    """
    from otto import v5_runner
    from otto.queue import subtask

    sites = [
        v5_runner._task_entry_allows_upward_merge,
        v5_runner._child_result_allows_upward_merge,
        subtask._verdict_satisfies_dependency,
    ]
    bad_pattern = re.compile(
        r'verdict\s*==\s*"partial"\s*and\s+.*=\s*"reviewed_partial"',
        re.MULTILINE | re.DOTALL,
    )
    offenders: list[str] = []
    for fn in sites:
        src = inspect.getsource(fn)
        if bad_pattern.search(src):
            offenders.append(fn.__qualname__)
    assert not offenders, (
        f"Inlined strict predicate detected in {offenders} — "
        f"this is the dup-drift anti-pattern Phase 0 eliminated. "
        f"Delegate to `task_graph.entry_is_satisfactory_terminal` "
        f"instead. See plan-checkpoint-resume-v2.md Phase 0."
    )


def test_all_three_sites_use_entry_is_satisfactory_terminal():
    """Positive ratchet: all three sites should reference the
    canonical helper (either by import or by call). Source-level so
    we catch removals."""
    from otto import v5_runner
    from otto.queue import subtask

    sites = [
        v5_runner._task_entry_allows_upward_merge,
        v5_runner._child_result_allows_upward_merge,
        subtask._verdict_satisfies_dependency,
    ]
    for fn in sites:
        src = inspect.getsource(fn)
        assert "entry_is_satisfactory_terminal" in src, (
            f"{fn.__qualname__} no longer references the canonical "
            f"helper `entry_is_satisfactory_terminal`. Phase 0 guard "
            f"failed."
        )
