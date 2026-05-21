"""Regression: `_merge_child_branch` must LAND the child's work even when the
agent touched a foundation-contract path. Pre-fix (2026-05-20), early returns
at the foundation_contract_write_gate orphaned ALL the child's uncommitted
work in the worktree — including unrelated files. The locked campaign
invariant (`always LAND a coherent product; bugs acceptable; discarded work
is not`) requires the violation be recorded as an annotation AFTER the
commit/merge lands, not as a pre-commit refusal.

Discovered on iTracker cc-i2p-2 e2e: the auth-workspace child appended one
fix to `backend/tests/conftest.py` (foundation-owned) and the gate dropped
its 20+ unrelated auth files (RegisterPage, LoginPage, auth router, …) — the
repair turn then spent ~21 min rebuilding them from scratch.
"""
from __future__ import annotations

import inspect
import re

from otto import v5_runner


def _merge_child_branch_src() -> str:
    return inspect.getsource(v5_runner._merge_child_branch)


def _runner_src() -> str:
    return inspect.getsource(v5_runner)


def test_all_five_gates_exist_and_invoke_helper() -> None:
    """The function has 5 foundation_contract_write_feedback callers (one
    per integration phase). If a new one is added, this test fails loudly
    so the implementer updates the no-refusal test below."""
    src = _merge_child_branch_src()
    assert src.count("_foundation_contract_write_feedback(") == 5, (
        "_merge_child_branch should call _foundation_contract_write_feedback "
        "5 times (worktree, branch-delta, conflict-repair, upward-repair, "
        "integration-union-repair). If a new gate was added or one removed, "
        "update this test and verify no-refusal coverage."
    )


def test_all_foundation_write_gate_callers_are_annotation_only() -> None:
    """Every foundation contract write gate in v5_runner must be
    LAND-then-annotate. This ratchets the whole module, not only
    `_merge_child_branch`."""
    src = _runner_src()
    assert src.count("_foundation_contract_write_feedback(") == 14, (
        "v5_runner should have the helper definition plus 13 call sites. "
        "If this count changes, update this test and verify every caller "
        "commits or merges before recording foundation_contract_write_gate."
    )
    forbidden = [
        "return False, _foundation_contract_write_block_detail(feedback)",
        "detail = _foundation_contract_write_block_detail(feedback)\n"
        "        _emit(on_event, {\n"
        '            "event": "integration_commit_failed"',
        "detail = _foundation_contract_write_block_detail(feedback)\n"
        "        _emit(on_event, {\n"
        '            "event": "inline_commit_failed"',
        'origin="foundation_contract_write_gate",\n'
        "            structured_reason=feedback,\n"
        "        )\n"
        "        return",
    ]
    for pattern in forbidden:
        assert pattern not in src
    assert '"post_commit_annotation"' in src
    assert "_record_foundation_contract_write_annotation(" in src


def test_no_gate_uses_legacy_block_detail_refusal_pattern() -> None:
    """Pre-fix bug pattern (now forbidden):

        if feedback is not None:
            detail = _foundation_contract_write_block_detail(feedback)
            _record_structured_merge_failed(... origin="foundation_contract_write_gate" ...)
            return

    The `detail` variable name (vs. the new `annotate_detail`) is the
    unique signature of the discard-before-commit pattern. The post-fix
    annotation block uses `annotate_detail = …` and has NO trailing
    `return` within the if-block.

    This test fails the moment the legacy pattern is reintroduced — much
    cheaper to maintain than an integration test."""
    src = _merge_child_branch_src()
    # The old pattern: `detail = _foundation_contract_write_block_detail(`
    # at any indentation. The new pattern uses `annotate_detail = `.
    legacy = re.findall(r"\bdetail = _foundation_contract_write_block_detail\(", src)
    assert legacy == [], (
        "Legacy refusal-before-commit pattern detected in "
        "_merge_child_branch: `detail = _foundation_contract_write_block_detail(...)` "
        "indicates a gate that discards the child's work. Use "
        "`annotate_detail = …` and drop the trailing `return` so the work "
        "still lands (see [[project_v5_one_hard_gate_redesign]])."
    )


def test_all_annotations_use_post_commit_phase_names() -> None:
    """Each of the 5 gates uses a distinct `phase=` string that ends in
    `_annotation` or starts with `post_`, marking it as a LAND-then-annotate
    site rather than a pre-commit refusal."""
    src = _merge_child_branch_src()
    expected_phases = {
        '"post_commit_annotation"',
        '"pre_merge_annotation"',
        '"merge_conflict_repair_annotation"',
        '"upward_merge_gate_annotation"',
        '"integration_union_guard_annotation"',
    }
    missing = {p for p in expected_phases if p not in src}
    assert not missing, (
        f"Expected annotation phase markers missing from _merge_child_branch: {missing}"
    )


def test_helper_signature_unchanged() -> None:
    """Sanity: the _foundation_contract_write_feedback helper itself is
    unchanged in its contract — it still returns a feedback dict when a
    non-owner touches a foundation contract path, and None otherwise."""
    fn = v5_runner._foundation_contract_write_feedback
    sig = inspect.signature(fn)
    expected_kwargs = {
        "project_dir",
        "acting_task_id",
        "parent_integration_branch",
        "changed_paths",
        "operation",
    }
    actual_kwargs = set(sig.parameters.keys())
    assert expected_kwargs <= actual_kwargs, (
        f"_foundation_contract_write_feedback signature regressed; "
        f"missing: {expected_kwargs - actual_kwargs}"
    )
