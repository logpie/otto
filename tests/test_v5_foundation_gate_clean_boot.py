"""Regression: the FOUNDATION-GATE CLEAN-BOOT PROBE fires once, at the right
moment, and ONLY when the merged foundation is genuinely ready — it never
weakens or front-runs the existing foundation-contract scheduler gate.

Root cause / motivation (2026-05-18 zoom-out, patches-to-protocols): the
decomposition/build half is solved (fix-5/6/7/11/13), but a recurring
*terminal* class remained — emergent whole-product preconditions (frontend
strict-tsc, composable store types, single ORM table namespace, WS port,
start.sh venv/deps) are invisible to isolated feature children and surface
only at the single terminal ``clean_deploy``, where ONE bounded repair turn
must absorb all of them at once. Confirmed across 2 diverse runs
(fix10-092844 backend dup-table; fix13-122604 frontend won't tsc-build).

Fix (consistent-by-construction, WITH precedent — mirrors the existing
``foundation_contracts_missing_after_pass`` re-entry and the
``scheduler_feedback`` foundation-contract gate): after the foundation
sibling has passed+merged and its contracts are valid
(``scheduler_feedback is None``), clean-boot-probe the MERGED foundation
scaffold via the existing ``_run_integration_smoke_preflight_with_repair``
BEFORE dispatching the ~50min feature children; a failed probe re-enters the
same bounded ``_reenter_or_block_architect_contract`` foundation-repair
(origin="foundation_clean_boot"). This shifts the boot/build-clean class
LEFT into a cheap early foundation-repair instead of the terminal one-shot.

The regression-critical NEW decision is *when* the probe is allowed to fire.
It is extracted into the pure helper ``_foundation_clean_boot_probe_targets``
so it is unit-testable without driving the full async ``_process_children``
loop. These tests lock it down:

  (a) all preconditions met  -> targets returned (probe FIRES)
  (b) ``scheduler_feedback`` is not None (a foundation-contract re-entry —
      the existing gate owns it) -> NO probe (gate UNAFFECTED — this is the
      anti-regression / not-gate-weakening guard)
  (c) parent already probed this run -> NO probe (dedupe: the probe must not
      re-run every scheduler iteration)
  (d) foundation not upward-mergeable (blocked / merge_blocked / only
      unreviewed-partial) -> NO probe (do not probe a non-ready foundation)
  (e) no feature child ready to dispatch -> NO probe (nothing to shift-left)

RED before the helper existed (import / behavior absent); GREEN after.
"""

from __future__ import annotations

from pathlib import Path

from otto.v5_runner import (
    _foundation_clean_boot_probe_targets,
    _seed_foundation_gate_spec,
)

PARENT = "v5-root"
FOUNDATION = "v5-foundation"


def _passed_foundation() -> dict[str, object]:
    """A foundation task entry that IS upward-mergeable (passed, unblocked)."""
    return {
        "parent_task_id": PARENT,
        "task_role": "foundation",
        "verdict": "pass",
    }


def _feature_graph_entry() -> dict[str, object]:
    return {"parent_task_id": PARENT, "task_role": "feature", "verdict": "pass"}


def test_all_preconditions_met_probe_fires() -> None:
    """(a) foundation passed+merged, contracts valid (scheduler_feedback
    None), a feature is ready, not yet probed -> both target lists non-empty
    so the clean-boot probe fires before feature dispatch."""
    graph = {FOUNDATION: _passed_foundation(), "v5-feat-a": _feature_graph_entry()}
    foundation_ids, ready_features = _foundation_clean_boot_probe_targets(
        scheduler_feedback=None,
        parent_task_id=PARENT,
        already_probed=set(),
        graph_tasks=graph,
        ready=[{"task_id": "v5-feat-a"}],
    )
    assert foundation_ids == [FOUNDATION]
    assert [e["task_id"] for e in ready_features] == ["v5-feat-a"]


def test_scheduler_feedback_present_no_probe_gate_unaffected() -> None:
    """(b) NOT-gate-weakening guard: when scheduler_feedback is not None the
    foundation is in a contract re-entry that the EXISTING gate owns — the
    clean-boot probe must NOT fire and must not interfere."""
    graph = {FOUNDATION: _passed_foundation(), "v5-feat-a": _feature_graph_entry()}
    foundation_ids, ready_features = _foundation_clean_boot_probe_targets(
        scheduler_feedback={"kind": "foundation_contracts_missing_after_pass"},
        parent_task_id=PARENT,
        already_probed=set(),
        graph_tasks=graph,
        ready=[{"task_id": "v5-feat-a"}],
    )
    assert foundation_ids == []
    assert ready_features == []


def test_already_probed_this_run_deduped() -> None:
    """(c) dedupe: a parent already probed this run must not re-probe — the
    probe is once-per-run, not once-per-scheduler-iteration."""
    graph = {FOUNDATION: _passed_foundation(), "v5-feat-a": _feature_graph_entry()}
    foundation_ids, ready_features = _foundation_clean_boot_probe_targets(
        scheduler_feedback=None,
        parent_task_id=PARENT,
        already_probed={PARENT},
        graph_tasks=graph,
        ready=[{"task_id": "v5-feat-a"}],
    )
    assert foundation_ids == []
    assert ready_features == []


def test_foundation_not_upward_mergeable_no_probe() -> None:
    """(d) a foundation that is merge_blocked / blocked / only
    unreviewed-partial is NOT upward-mergeable -> no probe (do not clean-boot
    a foundation that has not actually merged)."""
    blocked = {
        "parent_task_id": PARENT,
        "task_role": "foundation",
        "verdict": "merge_blocked",
    }
    foundation_ids, ready_features = _foundation_clean_boot_probe_targets(
        scheduler_feedback=None,
        parent_task_id=PARENT,
        already_probed=set(),
        graph_tasks={FOUNDATION: blocked, "v5-feat-a": _feature_graph_entry()},
        ready=[{"task_id": "v5-feat-a"}],
    )
    assert foundation_ids == []
    assert ready_features == []

    unreviewed_partial = {
        "parent_task_id": PARENT,
        "task_role": "foundation",
        "verdict": "partial",
    }
    fids, feats = _foundation_clean_boot_probe_targets(
        scheduler_feedback=None,
        parent_task_id=PARENT,
        already_probed=set(),
        graph_tasks={
            FOUNDATION: unreviewed_partial,
            "v5-feat-a": _feature_graph_entry(),
        },
        ready=[{"task_id": "v5-feat-a"}],
    )
    assert fids == []
    assert feats == []


def test_no_ready_feature_no_probe() -> None:
    """(e) a mergeable foundation but NO feature ready to dispatch -> nothing
    to shift-left -> no probe."""
    graph = {FOUNDATION: _passed_foundation()}
    foundation_ids, ready_features = _foundation_clean_boot_probe_targets(
        scheduler_feedback=None,
        parent_task_id=PARENT,
        already_probed=set(),
        graph_tasks=graph,
        ready=[],
    )
    assert foundation_ids == []
    assert ready_features == []


def test_reviewed_partial_foundation_is_mergeable_probe_fires() -> None:
    """A reviewed-partial foundation IS upward-mergeable (mirrors
    _task_entry_allows_upward_merge) so the probe still fires — the gate is
    not stricter than the established merge predicate."""
    rp = {
        "parent_task_id": PARENT,
        "task_role": "foundation",
        "verdict": "partial",
        "review_state": "reviewed_partial",
    }
    foundation_ids, ready_features = _foundation_clean_boot_probe_targets(
        scheduler_feedback=None,
        parent_task_id=PARENT,
        already_probed=set(),
        graph_tasks={FOUNDATION: rp, "v5-feat-a": _feature_graph_entry()},
        ready=[{"task_id": "v5-feat-a"}],
    )
    assert foundation_ids == [FOUNDATION]
    assert [e["task_id"] for e in ready_features] == ["v5-feat-a"]


# --- _seed_foundation_gate_spec: the f2aa00b25 SILENT-NO-OP regression -------
#
# Root cause (f2aa00b25 validation run v5-itracker-fgate-143401, 2026-05-18):
# the probe block WAS entered (foundation_gate dir created) but
# `_run_integration_smoke_preflight_with_repair` derives
# `spec_path = session_dir/"spec"/"spec.json"`; every OTHER caller passes a
# spec-bearing integration session dir, but the probe passed a fresh empty
# `<root_session>/foundation_gate` dir => spec_path missing => the clean-deploy
# oracle had no spec => vacuous non-blocking pass => the probe was a SILENT
# NO-OP (empty dir, ~10s, features dispatched with no clean-boot; ZERO
# `foundation_clean_boot` anywhere in otto_logs). _seed_foundation_gate_spec
# makes the probe session spec-bearing from the canonical compiled spec at the
# parent root session — honoring the same contract every other caller honors.


def test_seed_copies_canonical_spec_into_probe_session(tmp_path: Path) -> None:
    """THE fix: foundation_gate sits under the root session; seed its
    spec/spec.json from the parent root session's canonical compiled spec so
    the preflight (spec_path = session_dir/spec/spec.json) actually runs."""
    root = tmp_path / "2026-01-01-000000-abc123"
    (root / "spec").mkdir(parents=True)
    (root / "spec" / "spec.json").write_text('{"flat": true}')
    fg = root / "foundation_gate"
    fg.mkdir()

    assert _seed_foundation_gate_spec(fg) is True
    seeded = fg / "spec" / "spec.json"
    assert seeded.exists()
    assert seeded.read_text() == '{"flat": true}'


def test_seed_returns_false_when_canonical_spec_missing(tmp_path: Path) -> None:
    """Degenerate run (no compiled spec at root session): return False so the
    caller makes it OBSERVABLE (emit foundation_clean_boot_skipped) instead of
    the original SILENT no-op. Never fabricate a spec."""
    root = tmp_path / "2026-01-01-000000-def456"
    fg = root / "foundation_gate"
    fg.mkdir(parents=True)

    assert _seed_foundation_gate_spec(fg) is False
    assert not (fg / "spec" / "spec.json").exists()


def test_seed_idempotent_when_probe_spec_already_present(
    tmp_path: Path,
) -> None:
    """Already-seeded probe session: return True without overwriting (probe
    runs once; re-entry must not clobber)."""
    root = tmp_path / "2026-01-01-000000-aaa111"
    (root / "spec").mkdir(parents=True)
    (root / "spec" / "spec.json").write_text('{"canonical": 1}')
    fg = root / "foundation_gate"
    (fg / "spec").mkdir(parents=True)
    (fg / "spec" / "spec.json").write_text('{"already": "here"}')

    assert _seed_foundation_gate_spec(fg) is True
    # not overwritten by the canonical copy
    assert (fg / "spec" / "spec.json").read_text() == '{"already": "here"}'
