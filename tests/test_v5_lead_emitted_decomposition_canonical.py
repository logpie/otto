"""Regression: a Lead that emitted real child subtasks is authoritatively a
DECOMPOSER (decomposition=emit), even if it ALSO called begin_inline.

Root cause (fix12-115705, 2026-05-18, --tier modular iTracker, terminal
Verdict: partial $0.8427 / 945s, ZERO product): the root Lead correctly
decided to decompose ("1 scaffold foundation + 4 parallel feature leaves")
and submitted 5 real child subtasks (child_task_ids populated), then ALSO
called ``mcp__otto__begin_inline``. The task-graph entry ended up
``decomposition="inline"`` with ``child_task_ids=[5 ids]`` — a
contradiction. otto/lead.py:261-263 (``decomposition == "emit" and
emitted_subtask_ids`` -> pending_children) was FALSE because decomposition
was "inline", so the Lead's "partial" verdict.json was accepted; and
otto/v5_runner.py:4381 (Phase D, also gated on ``decomposition == "emit"``)
was SKIPPED so the 5 emitted children NEVER ran. otto committed the empty
inline worktree ("v5 inline build" = only FEATURE_PARTITION.json) and
stamped a FALSE ``partial`` for a run that built literally nothing.

Fix (consistent-by-construction, NOT gate-weakening, WITH precedent —
lead.py:262 already couples decomposition+emitted; v5_preflight.py:68 has
the architect analog): ``emitted_subtask_ids`` non-empty on a
non-integration Lead is ground truth that decomposition happened, so the
canonical decomposition is ``emit`` regardless of a stray begin_inline. A
pure helper ``_canonical_decomposition`` is applied right after the
task-graph read so BOTH consumers (the lead.py:262 verdict guard AND the
v5_runner.py:4381 Phase D child-runner) see the canonical value: the
planned decomposition actually executes instead of being silently
discarded + false-partialed. This RUNS the work (strengthens
anti-false-pass); it does not weaken any gate — genuine inline (no emitted
children) and integration are preserved.

RED before the fix (the bug scenario asserted "emit" but the helper did
not exist / returned "inline"); GREEN after. The genuine-inline and
integration cases guard that this is not gate relaxation.
"""

from __future__ import annotations

from otto.lead import _canonical_decomposition


def test_emitted_children_plus_stray_begin_inline_is_emit() -> None:
    """THE bug: lead submitted real child subtasks AND called begin_inline
    (decomposition=='inline'). emitted_subtask_ids is authoritative => emit,
    so Phase D runs the children instead of false-partialing nothing."""
    assert (
        _canonical_decomposition("inline", ["v5-a", "v5-b"], is_integration=False)
        == "emit"
    )


def test_emitted_children_with_unknown_decomposition_is_emit() -> None:
    assert (
        _canonical_decomposition("unknown", ["v5-scaffold"], is_integration=False)
        == "emit"
    )


def test_normal_emit_unchanged() -> None:
    assert (
        _canonical_decomposition("emit", ["v5-a"], is_integration=False) == "emit"
    )


def test_genuine_inline_no_children_preserved_not_gate_weakening() -> None:
    """Guard: a real inline build (NO emitted children) stays inline — the
    fix does not force emit on genuine leaf/inline nodes."""
    assert _canonical_decomposition("inline", [], is_integration=False) == "inline"
    assert _canonical_decomposition("unknown", [], is_integration=False) == "unknown"


def test_integration_never_decomposes_preserved() -> None:
    """Guard: integration Leads never decompose; emitted ids (if any) must
    not flip them to emit."""
    assert (
        _canonical_decomposition("inline", ["v5-a"], is_integration=True)
        == "inline"
    )
