"""Checkpoint/resume regression.

A v5 run interrupted/killed after decomposition (e.g. budget exceeded during
children_and_subtree_integration — observed every run #14/#15/#16) previously
had to rebuild compile + scaffold + every feature (~40min) just to retry
integration, even though that work is persisted in the task graph + per-child
git branches. `_resume_root_from_checkpoint` lets the pipeline skip compile +
root decomposition + child rebuild and resume straight at integration.

Resume must be SAFE: it only triggers on a genuine completed-decomposition
checkpoint, and a fresh project / terminal root / intent drift / missing spec
/ opt-out all fall back to unchanged fresh-run behavior (→ None).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from otto.queue.task_graph import SCHEMA_VERSION, task_graph_path
from otto.v5_runner import ROOT_TASK_ID, _resume_root_from_checkpoint


def _project(tasks: dict, *, with_spec: bool = True) -> tuple[Path, Path]:
    d = Path(tempfile.mkdtemp())
    gp = task_graph_path(d)
    gp.parent.mkdir(parents=True, exist_ok=True)
    gp.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "tasks": tasks}),
        encoding="utf-8",
    )
    if with_spec:
        sp = d / "otto_logs" / "sessions" / "s1" / "spec" / "spec.json"
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text('{"behavior_journeys": []}', encoding="utf-8")
    return d, d / "root_session"


_RESUMABLE = {
    "root": {"intent": "build X", "verdict": "pending_children"},
    "c1": {"parent_task_id": "root", "verdict": "pass", "task_role": "foundation"},
    "c2": {"parent_task_id": "root", "verdict": "pass", "task_role": "feature"},
}


def test_resumable_checkpoint_returns_synth_root_result() -> None:
    d, rs = _project(_RESUMABLE)
    out = _resume_root_from_checkpoint(
        project_dir=d, config={}, root_session_dir=rs, intent="build X"
    )
    assert out is not None
    spec, root_result = out
    assert spec is None  # FlatSpec not needed past Phase C; spec.json file is the artifact
    assert root_result.task_id == ROOT_TASK_ID
    assert root_result.decomposition == "emit"
    assert sorted(root_result.emitted_subtask_ids) == ["c1", "c2"]
    # spec.json checkpointed into the new root session dir (Phase E reads it).
    assert (rs / "spec" / "spec.json").exists()


def test_fresh_project_no_graph_returns_none() -> None:
    # The default-path guarantee: no graph → unchanged fresh-run behavior.
    d = Path(tempfile.mkdtemp())
    assert (
        _resume_root_from_checkpoint(
            project_dir=d, config={}, root_session_dir=d / "rs", intent="x"
        )
        is None
    )


def test_failed_but_iterable_root_is_resumable() -> None:
    # Phase 1.2-A (2026-05-19): the fast fix→resume→re-run-integration-only
    # loop. A run that ended `partial` or `merge_blocked` is iterable — after
    # fixing code, resume must skip the ~40min rebuild and re-enter
    # integration on the persisted task graph + per-child branches.
    for verdict in ("partial", "merge_blocked"):
        tasks = dict(_RESUMABLE)
        tasks["root"] = {"intent": "build X", "verdict": verdict}
        d, rs = _project(tasks)
        out = _resume_root_from_checkpoint(
            project_dir=d, config={}, root_session_dir=rs, intent="build X"
        )
        assert out is not None, f"{verdict} must be resumable for iteration"
        _spec, root_result = out
        # Synth root re-enters as pending_children; integration phase will
        # overwrite the persisted verdict with its new outcome.
        assert root_result.task_id == ROOT_TASK_ID
        assert root_result.decomposition == "emit"
        assert sorted(root_result.emitted_subtask_ids) == ["c1", "c2"]


def test_success_or_catastrophic_root_returns_none() -> None:
    # Only success (`pass`) and unrecoverable `catastrophic` block resume.
    # The remaining `_RESUMABLE`-pattern terminals (partial, merge_blocked)
    # are intentionally resumable — see test_failed_but_iterable_root above.
    for verdict in ("pass", "catastrophic"):
        tasks = dict(_RESUMABLE)
        tasks["root"] = {"intent": "build X", "verdict": verdict}
        d, rs = _project(tasks)
        assert (
            _resume_root_from_checkpoint(
                project_dir=d, config={}, root_session_dir=rs, intent="build X"
            )
            is None
        ), verdict


def test_intent_drift_returns_none() -> None:
    # Resuming an old decomposition for a changed intent would be wrong.
    d, rs = _project(_RESUMABLE)
    assert (
        _resume_root_from_checkpoint(
            project_dir=d, config={}, root_session_dir=rs, intent="DIFFERENT INTENT"
        )
        is None
    )


def test_no_children_returns_none() -> None:
    d, rs = _project({"root": {"intent": "build X", "verdict": "pending_children"}})
    assert (
        _resume_root_from_checkpoint(
            project_dir=d, config={}, root_session_dir=rs, intent="build X"
        )
        is None
    )


def test_missing_spec_checkpoint_returns_none() -> None:
    d, rs = _project(_RESUMABLE, with_spec=False)
    assert (
        _resume_root_from_checkpoint(
            project_dir=d, config={}, root_session_dir=rs, intent="build X"
        )
        is None
    )


def test_opt_out_returns_none() -> None:
    d, rs = _project(_RESUMABLE)
    assert (
        _resume_root_from_checkpoint(
            project_dir=d,
            config={"v5_resume_from_checkpoint": False},
            root_session_dir=rs,
            intent="build X",
        )
        is None
    )
